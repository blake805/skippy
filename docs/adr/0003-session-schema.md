# ADR 0003 — Project-scoped sessions on the NAS, with per-project vector collections

Status: accepted (Phase 3), amended by ADR 0006

> **Amendment.** The final bullet under Consequences describes heartbeat ticks
> carrying a `project_id` and the shop goal ledger being unaffected. Both the
> 5-minute heartbeat and the ledger were deleted by
> [ADR 0006](0006-single-runtime-coding-agent.md); ignore that bullet. The session,
> decision, and per-project collection schema is unaffected.

## Context

Memory before this change was two global Chroma collections,
`skippy_longterm` and `skippy_code_projects`, plus whatever was in the
conversation's `history` array. Two problems:

1. Every project's code and notes shared one vector space, so a search for
   "retry logic" in one repo surfaced chunks from unrelated repos. The more
   projects are indexed, the worse recall gets.
2. Nothing durable survived a chat. A new session started blind, so decisions were
   re-litigated and the same dead ends were re-explored.

## Decision

State lives on the NAS under `$SKIPPY_MEMORY_ROOT`:

```text
sessions/projects/<project_id>/meta.json
sessions/projects/<project_id>/sessions/<session_id>.json
sessions/projects/<project_id>/decisions/<decision_id>.md
sessions/projects/<project_id>/patches/<session_id>/<step>/
```

- `meta.json` holds workspace roots, discovered git repos and remotes, project
  conventions (test command, package manager), collection names, and rolling stats.
- One JSON file per session records the task, status, models used, files touched,
  linked decisions, and the full turn transcript with per-turn tool, arguments,
  and result summary. Bulky arguments are truncated on disk; the full pre-images
  live in `patches/`.
- Decisions are markdown with YAML front matter. Plain text on purpose: these are
  the highest-value artifacts and should be readable straight off the share
  without Skippy running.
- Chroma collections are `proj_<slug>_code` and `proj_<slug>_notes`.

The vector backend is optional. When Chroma is unavailable the store falls back to
deterministic keyword scoring over decisions and session history.

## Consequences

- A later session on the same project opens with the relevant prior decisions and
  conventions already in context, which is the Phase 3 exit criterion.
- Search is scoped, so recall does not degrade as more projects are added.
- The fallback means the agent still works — and stays testable — on a machine
  with no embedding backend, which is what makes CI coverage of memory possible.
- Code chunk ids are derived from path and chunk index rather than a random uuid,
  so re-indexing a project updates chunks in place instead of stacking duplicate
  copies of every file. This was a real defect in the original ingest.
- Heartbeat ticks can now carry a `project_id` and resume real project work. Tasks
  without one are untouched, so the shop ledger behaves exactly as before.
