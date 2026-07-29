# ADR 0006 — Skippy is a single-runtime coding agent; the shop lane is archived

- **Status:** accepted
- **Date:** 2026-07-29

> Numbering note: ADRs 0001–0005 were written on the `cursor/skippy-agent-runtime-f411`
> branch and arrive on `main` with the runtime port in the next slice. This one is 0006 so
> that port needs no renumbering.

## Context

Skippy began as a machine-shop assistant. `SkippyPipeline` was a fixed four-stage assembly
line — Architect, Triage, Engineer, QA, Summarizer — where each stage made one model call
and handed a string to the next. That shape suits "write me a feed-and-speed calculator."
It cannot implement a feature across four files in two repositories, because there is no
point in the pipeline where the model can look at a result and decide what to do next.

The target is a continuous agent loop: think, call a tool, observe, repeat until done. That
is a different control flow, not a tuning of the existing one.

An earlier plan kept both, dispatching on mode behind the existing `ConnectionManager` hub,
so shop work would continue while the agent loop was built alongside it. This ADR reverses
that decision.

## Decision

Delete the shop lane from `main` rather than keeping it alive behind a dispatch switch.
Preserve it at annotated tag `shop-v1`.

Removed: `SkippyPipeline` (710 lines), the 5-minute autonomy heartbeat and the
`skippy_goals.json` ledger, `execute_python_code` and `run_bash_command_stream`, the seven
shop-only tools (Tormach SSH and status, shop skills, goal ledger, sandbox test), the
`skills/` directory, all six mode prompts, and the Engineer/QA schemas.

Kept: model routing, `parse_leaked_tool_calls`, `query_model_message`, `ConnectionManager`,
Whisper transcription and Kokoro TTS, the ChromaDB connection, and the nine research and
context tools.

## Consequences

**Skippy does nothing until the agent loop lands.** The websocket endpoint answers with a
"runtime not installed" message. That is roughly four PRs of dead air, and it is the real
cost of this decision.

Accepted because the shop lane was already dormant — the server had not run since July 17
and the NAS was offline — and because keeping 733 lines of shop code importable would have
roughly doubled the surface area of the next three slices. It tangles directly with the
hub, `prompts.py`, `tool_schemas.py`, and `tools.py`, which is exactly the drag that gets
refactors abandoned halfway.

The Tormach integration is gone from `main`. If the mill comes back into scope it returns
as a tool on the new runtime, recovered from `shop-v1`, not as a pipeline stage.

`SkippyClient` still sends the old mode names. It keeps connecting and will render the
"runtime not installed" reply; its mode picker is reworked when the lanes are real.

## Alternatives considered

**Dual runtime behind the mode switch.** Rejected: the cost is paid on every subsequent
slice rather than once here, in exchange for keeping a lane nobody is using.

**Leave the shop code in place but unreferenced.** Rejected: dead code that still imports
is worse than deleted code — it has to be kept compiling and it misleads readers about
what the system does. The tag preserves it better than `main` would.
