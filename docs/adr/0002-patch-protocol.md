# ADR 0002 — Patches are search/replace edit sets, applied atomically

Status: accepted (Phase 2)

## Context

The agent has to change several files per turn. The candidates were a unified
diff with line numbers, whole-file rewrites, and search/replace edits.

Unified diffs require the model to count lines correctly in a file it only
partially read. Local quantized models get this wrong often, and a
plausible-but-wrong hunk header either fails to apply or applies in the wrong
place. Whole-file rewrites blow the token budget on large files and invite the
model to silently drop code it did not think was important.

## Decision

`apply_patch` takes an `edits` list. Each edit names a `path` and an `action`:

```json
{"tool": "apply_patch", "args": {"edits": [
  {"path": "src/a.py", "action": "edit", "search": "<exact text>", "replace": "<new text>"},
  {"path": "src/b.py", "action": "create", "content": "<full file>"},
  {"path": "src/c.py", "action": "delete"}
]}}
```

Rules:

- `search` must match byte-for-byte, and must be **unique** in the file. An
  ambiguous match is rejected with a message telling the model to add context.
  `replace_all` and `occurrence: <n>` are the explicit escape hatches.
- All edits are validated against *staged* content before anything is written, so
  several edits to one file stack in order and one bad edit aborts the whole set.
  A rejected batch leaves the working tree exactly as it was.
- Writes go through a temp file plus `os.replace`, preserving file mode.
- Pre-images are stashed under the session's `patches/<session>/<step>/` with a
  manifest, so a bad change is recoverable.
- The result carries a unified diff and per-file line counts for display. The
  diff is *generated*, never parsed.

## Consequences

- The failure mode is "nothing happened, here is why", not "half of a refactor".
  This is the single most important property: a partially applied edit set leaves
  a repo that neither the model nor the user can reason about.
- The model must read before it writes, since `search` has to be exact. The Agent
  prompt says so explicitly.
- `cursor_client/src/patches.ts` reimplements these semantics rather than
  approximating them. If the two drift, the same edit set behaves differently
  depending on whether Cursor is attached, so both sides are tested against the
  same cases (`tests/test_apply_patch.py`, `cursor_client/test/patches.test.js`).
- This shape is already familiar in the codebase: `PROMPTS["Developer"]` uses the
  same `search_text`/`replace_text` idea for self-modification.
