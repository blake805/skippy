# ADR 0004 — Cursor attaches as a websocket RPC client, not the other way round

Status: accepted (Phase 4)

## Context

Skippy needs two things only the editor has: live diagnostics from the language
servers, and a way to change files that the user can undo. Writing to disk behind
the editor's back produces "file changed on disk" prompts and leaves no undo
history.

Options considered:

- **An MCP server that Cursor calls.** Cursor drives; Skippy answers. Wrong
  direction — Skippy's agent loop is the thing that needs to initiate work, and it
  also needs to run headless from the heartbeat with no editor attached.
- **A language server.** Far more protocol than is needed here.
- **A websocket client extension.** The hub already multiplexes clients by
  `client_id` and already has a request/reply mechanism (`execute_tool_on_client`
  plus `pending_responses` keyed by `task_id`). The `vscode_get_active_file` tool
  was an early version of exactly this.

## Decision

A sideloaded VS Code-compatible extension (`cursor_client/`) connects to
`ws://<studio>:8000/ws/factory?client_id=cursor`. The server sends
`{action, task_id, ...}`; the extension replies on the same socket, echoing
`task_id` verbatim.

Actions: `get_workspace_roots`, `get_open_files`, `get_diagnostics`,
`apply_patches`, `create_file`, `run_task`. Timeouts are per action — a workspace
edit or a test run legitimately takes minutes, while the open-file list should
answer instantly. The old global 10-second default would have failed every
interesting call.

Edits are applied as one `vscode.WorkspaceEdit`, so a multi-file change is a single
undo step. Open dirty buffers are read in preference to disk, so edits are computed
against what the user is actually looking at, and touched documents are saved
afterwards so the agent's next `run_tests` sees the change.

## Consequences

- The agent works identically with or without Cursor. `cursor_apply_patch`
  validates and diffs locally first, then routes to the editor if it is attached
  and falls back to a direct write if it is not — including when the editor
  refuses. Diagnostics simply become unavailable, and the tool says so and points
  at `run_tests`.
- Because validation happens server-side, the editor is never handed a path that
  escapes the workspace roots.
- Two hub defects had to be fixed for this to work: `execute_tool_on_client` never
  released `pending_responses` on success, and any inbound message from a
  non-SwiftUI client spawned a Shop pipeline — which would have fired on every
  RPC reply.
- The extension is sideloaded as a `.vsix`. There is no marketplace listing and no
  intention of one.
