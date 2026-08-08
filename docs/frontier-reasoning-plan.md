# Frontier-level reasoning for Skippy — design handoff

- **Status:** plan, not yet built. Promote to an ADR (next free number) when the shape is accepted.
- **Date:** 2026-08-07
- **Builds on:** [ADR 0001](adr/0001-agent-loop.md) (one role drives the loop),
  [ADR 0007](adr/0007-model-roles-and-cloud-escalation.md) (roles, local default, cloud gate),
  [ADR 0012](adr/0012-reverse-engineering-mode.md) (RE mode).

## Goal

Give Skippy frontier-level reasoning at the hard decision points without giving up the
things that make a local agent worth having: a warm prompt cache, near-zero token cost
by default, and code that does not leave the machine.

## Hardware context

The operator runs a **Mac Studio, 512 GB unified memory**. This is load-bearing for the
plan: a large local reasoner fits *resident* alongside the existing fleet, so the
local-reasoner path is real rather than aspirational.

Rough current residency (4-bit): `heavy` 480B ≈ 250–270 GB, `fast`/`compressor` shared
30B ≈ 17 GB, `voice` 30B ≈ 17 GB. That leaves ~180–200 GB free — enough for a
Qwen3-235B-Thinking-class model (~130 GB at 4-bit) to stay resident without evicting
anything. Verify real numbers before committing; a long 480B context plus a reasoner KV
cache is the case that could squeeze.

## The three options that were weighed

1. **Cloud frontier model as an escalation role.** Strongest reasoning, near-zero
   memory/integration cost (the registry already handles off-machine URLs, the cloud
   gate, and the `CLOUD:` log line). Costs: code leaves the machine, per-call money, a
   network/vendor dependency, and **safety-filter refusals** — see below.
2. **Local reasoning model on a second port.** Real on 512 GB. No privacy trade-off, no
   per-call cost, no network dependency, so the loop can consult freely. Costs: a strong
   local thinker is frontier-*shaped*, not frontier; thinking traces are slow to decode
   locally; another server process to keep healthy.
3. **Scaffolding with current weights** (plan-before-edit, critic pass over the diff
   before finish, best-of-2). Zero new dependencies, works today, compounds with either
   model path. Bounded by the 480B's ceiling; every scaffold needs its own A/B.

## Decision (proposed)

**Do 1 + 2 together, split by mode, and expose both through one new `consult` tool.
Layer 3 on top over time.** The registry already resolves roles per-mode, so this is a
config split, not a code fork.

- **Coding mode → cloud reasoner.** A `reasoner` role pointing at a hosted frontier
  model, behind the existing `SKIPPY_ALLOW_CLOUD` gate.
- **RE mode → local reasoner.** A `reasoner` role pointing at a local port (e.g. 8082),
  a thinking model resident on the 512 GB box.

### Why RE stays local — the safety-filter problem

The operator reverse-engineers their own hardware, but frontier cloud models
**non-deterministically refuse** RE-shaped prompts (firmware, disassembly, protection
circumvention). A refusal mid-run is worse than a flat outage: it reads like an answer,
and the loop has to treat it like a dead endpoint. During this design session a single
exploratory message already tripped a provider safety filter, which is what prompted
the handoff.

The benchmark evidence makes keeping RE local nearly free of downside:

- The most jailbreak-resistant top models (Claude Fable 5, GPT-5.6 Sol) are the ones
  *most* likely to false-positive on legitimate RE — best coding model and most
  refusal-prone are the same model.
- On AgentRE-Bench, RE performance is dominated by **hallucination calibration, not
  reasoning depth** — a small non-thinking model (Gemini 3.1 Flash Lite) beat every
  frontier reasoner. So the frontier premium is smallest exactly where the refusal tax
  is highest.
- "Harness beats model" for security work (same model scored 100% vs 0% across
  harnesses). Skippy's structured RE tools + recover loop already are that good harness.

So routing RE to the cloud would take on the refusal risk *and* the privacy risk for a
small quality gain. Local wins on RE.

### Model recommendation for the cloud (coding) role

**Claude Fable 5** as the default — currently tops SWE-bench Verified (~95%), leads the
Intelligence Index and composite reasoning, and its 1M-token flat-rate context suits a
`consult` that packages repo context. **GPT-5.6 Sol** is the fallback / second-opinion
provider. Treat this as a well-supported default, not gospel: coding leaderboards move
monthly, so the real arbiter is a scoreboard A/B on our own tasks.

Numbers above are from 2026 benchmark aggregators (AI/ML API blog, alcconsulting,
AgentRE-Bench, a DSU harness study) — noisy, vendor-adjacent, and worth re-checking.

## The `consult` tool — shape

Model it on `_investigate` in `skippy_agent.py`, which is the sanctioned seam for a
sub-run on a different brain (see the `SKIPPY_SUBAGENT_ROLE` comment: a sub-run has its
own transcript, hence its own prompt cache, so a different role costs the parent's cache
nothing — the one place this does not fight ADR 0001).

`consult` is actually *simpler* than `investigate`: it needs **no child toolset at
all**. The parent packages the relevant code + a self-contained question, the reasoner
thinks, one answer comes back as a single observation. Reuse the worked patterns from
`_investigate`:

- **Per-run limit**, like `SUBAGENT_LIMIT` (4) — a loop that learns "when stuck,
  consult" will consult often; cap it.
- **Honest failure** — a refused/failed consult comes back as a failed `ToolResult`
  marked incomplete, never as a plausible-looking answer (mirror the investigate
  "reader did not finish" handling).
- **Event relay** with `sub: True` / `parent_step` markers so the timeline nests it.
- **No memory writes** — a consult is not something a later session should open with.

Trigger options to A/B: model-invoked tool vs loop-invoked on signals (two finish-gate
pushbacks, a suite red across two fix attempts).

## Code anchors

- `skippy_llm.py` — `_build_registry()` (~L98) add the `reasoner` role; `ModelEndpoint`,
  `endpoint()`, `cloud_allowed()` already do the gating/logging. Defaults belong to
  weights actually in cache (ADR 0007 lesson).
- `skippy_agent.py` — `_investigate()` (~L1018) is the template; `SUBAGENT_*` constants
  (~L50–65). Handle `consult` in the loop, not the dispatcher (it spends steps; the loop
  owns budgets).
- `tool_schemas.py` — add a `consult` schema; `workspace_tools()` / `re_tools()` decide
  which modes offer it. `investigation_tools()` shows the "omit the tool to bound
  recursion" pattern.
- `prompts.py` — a line telling the model when consulting is worth a step (and, for the
  loop-trigger variant, nothing in the prompt at all).

## Open questions / A/B plan

- Does a `consult` actually move pass rate, or just spend steps? Scoreboard A/B, same
  method as the verification-rule experiment (5v5 per arm, watch for new failure modes,
  not just the mean). `python -m tests.agent_eval --save`.
- Which local thinking model for the RE reasoner, and does it stay resident with a long
  480B context live? Measure memory before committing.
- Cloud provider fallback: if Fable 5 refuses or errors mid-run, does the loop retry on
  GPT-5.6 Sol, or fail honestly? (Lean honest-fail first.)
- Is `investigate` itself worth offering in RE mode? (Disassembly listings are long; the
  compressor carries that load today.)

## What NOT to do

- Do **not** make the loop driver cloud. ADR 0001/0007 stand: the heavy role runs every
  step, its warm cache is worth ~20x, and metering it is what makes an agent too
  expensive to let explore. `consult` is an escalation *at decision points*, not a
  driver swap.
- Do **not** route RE code/artifacts off-machine. See the safety + privacy argument
  above.
