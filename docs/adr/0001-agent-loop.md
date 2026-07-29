# ADR 0001 — The coding agent is a continuous tool loop, not an assembly line

Status: accepted (Phase 2)

## Context

The shop pipeline (`SkippyPipeline`) is a fixed four-stage assembly line:
Architect writes a plain-English blueprint, a triage step picks a model, an
Engineer emits one code blob, QA approves it, and the result is saved — with the
Shop QA prompt requiring `skills/<name>.py` as the destination.

That shape is a good fit for "write me a feed-and-speed calculator". It cannot
express "add a parameter to this function, update its three call sites, and fix
the test that breaks", because:

- The Engineer sees a blueprint, not the code. It cannot look anything up.
- One turn produces one artifact, so there is no way to emit several files.
- QA gates on "does this script run", which is meaningless for a diff.
- The `skills/` destination is wrong for edits to an existing repo.

## Decision

Code work goes through a separate `SkippyAgent` loop: think, call exactly one
tool, read the observation, repeat, until the model calls `finish`. Tools cover
reading, searching, patching, testing, git, and project memory.

One model drives the whole loop. `heavy` (GLM-5.2) both plans and writes code;
`fast` is used only for compressing oversized observations and for hub-side
classification. Alternating models between steps was considered and rejected: it
destroys prompt-cache locality on MLX and produces inconsistent tool syntax
between turns. `SKIPPY_AGENT_PLANNER_ROLE` exists so a cheap-planner split can be
A/B tested later without a refactor.

The loop is guarded by a step budget, repeated-identical-call detection,
unparseable-output nudges, and cooperative cancellation.

## Consequences

- Multi-file work becomes possible, which was the point.
- `PROMPTS["Agent"]` is intentionally free of the `skills/` contract and the QA
  APPROVE JSON. Forcing agent output through that gate would reintroduce the
  bottleneck.
- Two lanes now exist. `/ws/factory` dispatches on `mode`, so a payload without
  `mode: "Agent"` behaves exactly as it did before and the SwiftUI clients are
  unaffected.
- The shop lane is *not* migrated. It works, the Tormach path is safety-critical,
  and a rewrite buys nothing here.
