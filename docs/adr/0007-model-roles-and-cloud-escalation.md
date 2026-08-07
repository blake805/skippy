# ADR 0007 — Three model roles, local by default, cloud as deliberate escalation

- **Status:** accepted
- **Date:** 2026-07-29
- **Amends:** [ADR 0001](0001-agent-loop.md) (which named GLM-5.2 as `heavy`)
- **Amended by:** [ADR 0022](0022-web-research.md) — the "no network at runtime"
  property this ADR treats as a background fact no longer holds. The policy below is
  unchanged and still governs *models*: it is about where inference happens and what
  leaves the machine with it. Reading a public page sends a URL, not the workspace, and
  ADR 0022 covers the separate risks that come with it.

## Context

The original success criteria included "no cloud LLM required at runtime". That was
relaxed during review: the operator reverse-engineers their own hardware, so the
confidentiality argument for a hard local-only rule is weaker than assumed, and the
capability gap to a frontier model is real.

But "cloud is fine" and "cloud by default" are different positions. The reason to
keep local as the default is not ideology, it is cost shape: an agent loop re-sends
every tool observation on the next turn, so a thirty-call task runs to hundreds of
thousands of tokens. Metered, that changes how the tool gets used — you stop letting
it explore, which is most of its value.

### What the benchmark says

Two runs against an agent-loop-shaped workload: a 12K-token preamble, then four
sequential steps sharing a growing prefix.

| | Cold prefill (12K) | Warm step | Decode | Held tool discipline |
| --- | --- | --- | --- | --- |
| Qwen3-Coder-480B-A35B-4bit | 59.9s (~200 tok/s) | 2.8–3.3s | 13.5 tok/s | **4 of 4 steps** |
| Qwen3-Coder-30B-A3B-4bit | 8.6s (~1400 tok/s) | 1.3–1.8s | 38 tok/s | 1 of 4 steps |

The last column decides it. The 30B emitted a valid tool call on step one and drifted
into prose for the rest; the 480B stayed on task throughout. Discipline over many
steps is what determines whether a long task converges, so the 480B takes the heavy
role despite being 7x slower to prefill and 3x slower to decode.

The 480B's weakness is prefill, not intelligence. That reframes the performance
problem: the fix is keeping its context small and pre-digested, which is a tooling
concern, not a reason to shop for different weights.

## Decision

**Three roles, addressed by name.** `fast` for cheap turns and triage, `heavy` for the
agent loop, `compressor` for squeezing oversized observations. `skippy_llm` owns the
role → (url, weights, token budget) mapping, so changing models is configuration.

**Defaults are the weights actually in the local cache**, not aspirational repo ids.
The predecessor of this module defaulted `heavy` to `avlp12/GLM-5.2-Alis-MLX-Dynamic-3.5bpw`
with a comment conceding the id might not exist — a config error that surfaces as a
confusing runtime failure.

| Role | Port | Weights |
| --- | --- | --- |
| `fast` | 8080 | Qwen3-Coder-30B-A3B-Instruct-4bit |
| `heavy` | 8081 | Qwen3-Coder-480B-A35B-Instruct-4bit |
| `compressor` | 8080 | Qwen3-Coder-30B-A3B-Instruct-4bit |

**The compressor moves off Qwen2.5-Coder-32B.** That model caps at 32K context, which
is smaller than a single `objdump` region — it would fail exactly when RE work needs
compression most. Qwen3-Coder-30B-A3B has 256K, so `compressor` shares both the
weights and the server process of `fast`, taking the fleet from three model servers
to two. The heavy role is the one whose prompt cache matters, and it keeps 8081 to
itself; neither `fast` nor `compressor` holds a long shared prefix, so the contention
is not worth another 16GB process. `SKIPPY_COMP_URL` splits them if that turns out to
be wrong.

**Local means loopback.** `is_local` is computed from the URL, never declared, so
pointing a role at a hosted API cannot accidentally be marked local. A LAN or tailnet
address counts as off-machine — deliberately conservative, because the guarantee worth
having is "these bytes did not leave this machine" and a tailnet peer is another
machine.

**Cloud is opt-in, and never silent.** Resolving an off-machine role raises
`CloudNotAllowed` unless `SKIPPY_ALLOW_CLOUD=1`. When it is allowed, every resolution
logs at WARNING with the role and host, the boot log prints the whole registry, and
`GET /health` reports which roles are local. Any OpenAI-compatible endpoint works;
`SKIPPY_<ROLE>_API_KEY` becomes a bearer token.

**Endpoint failures raise.** `query_message` raises `ModelError` instead of returning
`"System Error: Failed to connect..."` as message content. An agent loop cannot
distinguish that string from something the model said, so it would plan against it.

**Transcripts are append-only.** `Transcript` has no delete, no pop, no item
assignment, and `messages` returns copies. Prompt caching is worth ~20x on the heavy
role and it keys on the prefix, so mutating a sent message forces a full re-prefill.
The predecessor trimmed history with `del self.messages[2:4]`, silently buying a
60-second re-prefill whenever a memory routine decided the transcript was long.
Shedding context is still sometimes necessary, so `fold()` exists — it returns a new
transcript and logs the cache cost, making it a deliberate act at the call site
rather than a hidden side effect.

## Consequences

- Local is the workhorse and cloud is a rare escalation, so token spend stays near
  zero by default while the ceiling is liftable per workspace.
- Qwen2.5-Coder-32B and Llama-3.3-70B are no longer served. Their weights stay on
  disk; there is 3.1TB free and nothing to gain from deleting them.
- The compressor is now load-bearing for performance rather than a nicety. At ~200
  tok/s prefill, every 2000 tokens of raw observation costs ~10s before the heavy
  model generates anything, so a 40-step task with uncompressed observations is
  roughly ten minutes of waiting.
- Because `is_local` is derived from the URL, a tailnet-served model is refused by
  default even though the operator may consider it private. That is the intended
  bias, and `SKIPPY_ALLOW_CLOUD=1` is the escape hatch.
- Not yet measured: speculative decoding. `mlx_lm.server` supports `--draft-model`
  and the 30B and 480B share an identical 151,936-token vocabulary, so the 30B could
  plausibly lift the 13.5 tok/s decode. Worth a measurement once the loop exists,
  not a promise.

## Alternatives considered

**Drop the 480B and lean on cloud for heavy work.** Tempting on the numbers — it frees
252GB and removes the prefill bottleneck. Rejected because it inverts the cost shape:
the heavy role runs every step of every task, so metering it is precisely what makes
an agent too expensive to let explore.

**Make `heavy` cloud and `fast` local from the start.** Same objection, and it would
have meant the default configuration could not run offline at all.

**Keep Qwen2.5-Coder-32B as a dedicated compressor.** Rejected on the 32K context cap
alone. A compressor that fails on large inputs is inverted — large inputs are the only
reason it exists.
