# Migration: shop-jarvis (stale clone) → blake805/skippy

Written 2026-08-14, after a session that evaluated and extended Skippy from the
stale checkout at `~/shop-jarvis`. Copy this file into the new workspace, use it
to port the work, then delete it — it describes a one-time migration.

## The situation, in three sentences

`blake805/shop-jarvis` was renamed on GitHub to `blake805/skippy`; they are the
same repository, and `~/shop-jarvis` is a clone that fell 20 commits behind while
development continued from the Mac Studio. Everything the newer history has —
`firmware/core2-devio` and `core2-voice`, ADR 0020/0021 (wireless bench node, BLE
transport), ADR 0022/0023 (web research and its autonomous trigger), ADR
0015-re-device-io, the `investigate` and `consult` tools, websocket token auth,
`note_bridge`, SkippyPhone voice work — is real and current. Four local commits
were made in the stale clone on 2026-08-14 and exist nowhere else; this document
is the guide to porting what is worth porting.

## DO THIS FIRST — security

- **Never push the stale clone's `main`.** Local commit `c712aa2` tracked
  `run_ble_bridge.command` containing a live factory token, and the repository is
  PUBLIC. Upstream commit `843f053` deliberately moved tokens out of tracked
  launchers into a secrets file. Pushing that branch (or any branch containing it)
  publishes the token.
- **Rotate that factory token anyway** (the one in the old clone's
  `run_ble_bridge.command`); it has been sitting in a plaintext launcher on the
  MacBook regardless of git.
- Set up the new checkout at `~/skippy` (the path SkippyServer's boot expects),
  and either delete `~/shop-jarvis` or rename it `~/shop-jarvis-ARCHIVED` once
  porting is done. Two live checkouts is how this divergence happened.

## What the stale clone has that upstream lacks

Reference commits (in `~/shop-jarvis`, local only): `8177ec4` Cursor context,
`c712aa2` SkippyMac+BLE copy, `aab2b10` explorer sub-agents + token accounting,
`6b5a78d` speculative decoding toggle. Do NOT cherry-pick `aab2b10` or `c712aa2`
wholesale — upstream rewrote the same files (+835 lines in `skippy_agent.py`
alone) and has its own overlapping designs. Port by re-implementing, using the
stale clone as reference.

### 1. Token-honest fold accounting (port first; small, real bug fix)

Upstream `skippy_agent.py` (~line 1451) still triggers folds on
`sum(len(m.get("content"))…)` — a character proxy that ignores `tool_calls`
payloads entirely, so a transcript of `apply_patch` calls looks nearly empty to
the very check meant to bound it. Upstream `skippy_llm.py` never reads `usage`.
The fix, as implemented in the stale clone (`aab2b10`):

- `query_message` returns `prompt_tokens` from the response `usage`; treat
  missing/zero/negative as None (a placeholder taken as a measurement tells the
  loop its transcript is weightless).
- The loop keeps `_last_prompt_tokens` from the previous step, folds on a token
  threshold (60k was chosen for a 256k window) when usage exists, falls back to
  the char sum — now including serialized `tool_calls` — when it does not, and
  resets the token count to None after a fold (it describes a dead transcript).
- Include tool calls in the fold-summary history flattening too, or the summary
  never sees what the run actually did.
- `tests/fake_llm.py` must then report a realistic `prompt_tokens` estimate
  instead of 0, or the token path silently never engages in tests.

### 2. Concurrent sub-agent batching (adapt to `investigate`, don't add a rival)

Upstream already has the sub-agent: `investigate` (read-only, own budget
`SUBAGENT_MAX_STEPS`, never records a session, cannot edit/run/spawn) plus
`consult` (budgeted question to a reasoner role). The stale clone built the same
concept independently as `spawn_explorer` — do not port it as a second tool. What
upstream lacks is the concurrency pattern: `investigate` runs serially. Port from
`aab2b10`:

- Batch *consecutive* investigate calls from one assistant turn, run them under
  an `asyncio.Semaphore(3)` (they share one fast-role server; a fourth queues),
  and answer them **in original call order** so the one-tool-reply-per-call
  transcript contract holds regardless of finish order.
- Propagate parent `cancel()` to running children.
- Prompt guidance: independent investigations go in one turn as separate calls.
- Check whether upstream's investigate events are distinguishable client-side;
  the stale clone wrapped child events as `subagent_event` and rendered them in
  SkippyMac with a `↳` prefix (`FactoryClient.swift`, `case "subagent_event"`).
  Port the UI part only if upstream doesn't already mark child events.
- Worth stealing from the stale clone's explorer prompt even if the tool name
  dies: mandatory file citations in reports, "say what you did not find", and
  the rule against delegating files the orchestrator is about to edit (edits are
  byte-for-byte; reports are summaries).

### 3. Speculative decoding toggle (port as-is; nothing upstream touches it)

`6b5a78d` adds to `apps/SkippyServer/SkippyServer/ContentView.swift`: a
UserDefaults-backed "Speculative Decoding" toggle that boots the heavy role with
`--draft-model mlx-community/Qwen3-0.6B-4bit --num-draft-tokens 5` (mlx_lm ≥
0.21). Same output, faster decode when the draft guesses well — code at temp 0.1
is the best case. Notes that matter: the draft must already be in the HF cache
(servers boot with `HF_HUB_OFFLINE=1`, so a missing draft fails the boot);
batching is disabled on that server (irrelevant — 8081 serves one loop);
benchmark acceptance on real tasks before trusting the speedup. Upstream's
ContentView has diverged — re-apply the three small edits by hand.

### 4. Cursor-side context (rewrite against upstream, don't copy)

Upstream has no `AGENTS.md` and no `.cursor/rules/`. The stale clone's versions
(`8177ec4`) are good skeletons but state facts that are wrong upstream (they
predate consult/investigate/web research/token auth). Rewrite `AGENTS.md` for
the new workspace covering: the append-only transcript invariant and its cache
economics; apply_patch as the only write path; role-based model addressing;
"only finished is success"; the sandbox; push-based memory with staleness
marking; RE mode's restrictions; the investigate/consult budgets; web research
and its autonomous trigger (ADR 0022/0023); device I/O and the bench-node
transports (ADR 0015-re-device-io, 0020, 0021); token auth on `/ws/factory`;
test-suite rules (offline, fake_llm, lazy Chroma/Whisper/Kokoro imports, patch
parity table, pyflakes-clean). Keep the two scoped rules (RE invariants for
`skippy_re.py`/`skippy_rizin.py`/`skippy_extract.py`; test conventions for
`tests/**`) after checking each claim against upstream code.

### 5. ADR numbering

The stale clone created `docs/adr/0021-explorer-subagents.md`, which collides
with upstream's `0021-ble-node-transport.md`. If any of the explorer design
rationale is worth keeping after adapting to `investigate`, write it as **0024**
(0022 and 0023 are taken). Upstream also has two ADRs numbered 0015
(`note-pack-identity` and `re-device-io`) — a pre-existing collision worth
fixing in the same pass.

## Gaps that are still open upstream (verified 2026-08-14)

Ranked by expected value; none of these exist in upstream code today:

1. **`ask_user` primitive.** Approval futures exist for edits and device writes,
   but a blocked agent cannot ask a question mid-run; its only move is `finish`.
   The same future mechanism in `skippy_factory.py` extends naturally.
2. **Checkpoint commits.** Git is read-only in the exec allowlist, so a 40-step
   run has only the patch journal for restore points. A loop-owned `git commit`
   on its own work (never push) is the fix.
3. **Todo/plan primitive.** Long runs hold their plan in the transcript, where
   folding can eat it. A small loop-owned task list, surfaced in events and
   preserved across folds, keeps long runs on the rails.
4. **Session distillation ("dreaming").** ADR 0013 admits sessions get
   recency-truncated, never distilled into a standing project summary; ADR 0023
   covers autonomous *web research*, not this. An idle-time fast-role job that
   folds accumulated session records into a standing summary closes it.
5. **Extraction file-count bound.** unblob carving bounds size/depth/time but
   not the number of files produced; a ten-million-empty-files bomb passes the
   size cap. Documented in the README's "what it cannot do yet".
6. **`tools.py` in the stale clone was orphaned dead code** — verify upstream
   removed or absorbed it into ADR 0022's web research before assuming.

## Suggested order of work in the new workspace

1. Security items (top of this file), fresh clone, boot the test suite
   (`pip install -r requirements-test.txt && python -m pytest`) to establish a
   green baseline.
2. New `AGENTS.md` + `.cursor/rules/` (item 4) — do this before any agent work,
   so every session after it starts oriented.
3. Fold accounting fix (item 1) with its tests.
4. Speculative decoding toggle (item 3), then benchmark it on the Studio.
5. Concurrent investigate batching (item 2).
6. Open-gap primitives (ask_user first), each behind its own ADR.
