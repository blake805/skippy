# ADR 0005 — One reader per socket: human approvals round-trip through the hub

Status: accepted

## Context

`SkippyPipeline` collected human approvals by reading the websocket itself:
`await self.ws.receive_text()` at three sites — the `tormach_ssh` authorization in
`phase_1_research`, and both `request_terminal_execution` and the `DEPLOY`
overwrite gate in `phase_2_engineer_and_qa`.

At the same time `_serve_socket` sits in `while True: await websocket.receive_text()`
on that same socket. Two coroutines reading one socket is a race: whichever is
parked in `receive_text` when a frame arrives gets it, and the other keeps waiting.

It usually worked only because one pipeline tended to be in flight at a time. It
was racy by construction — the endpoint spawns pipelines with
`asyncio.create_task` — and an agent task sharing the socket made it fire
regularly. The failure is nasty in both directions: an approval meant for the
pipeline gets swallowed by the endpoint loop and re-dispatched as a brand new
Shop task, or a user's next chat message gets consumed as an authorization
answer. On the Tormach path that second case means a chat message could read as
`{"status": "APPROVE"}`'s absence — or worse, a stale approval could land on the
wrong request.

The agent lane already had the answer. `hub.request_on_socket(websocket, payload,
timeout)` stamps a `task_id` on the outbound payload and awaits a future in
`pending_responses`; `_serve_socket` already routes *any* inbound message
carrying a `task_id` to `hub.resolve_response` before it considers dispatching
work. `SkippyAgent._approve` has used this since Phase 2.

## Decision

`_serve_socket` owns the only `receive_text()` call on a socket. Anything needing
an answer mid-task goes through `pending_responses`.

The three shop sites now call `SkippyPipeline.await_authorization(payload)`, which
delegates to `hub.request_on_socket` and returns a bool. The decision rule is
unchanged and strict: `reply["status"] == "APPROVE"` and nothing else. A denial, a
timeout, or a dead socket all fail closed. What gets authorized did not change —
only how the answer is collected.

The timeout is 600 seconds (`HUMAN_APPROVAL_TIMEOUT`). A human has to physically
walk over to the machine and read the request before answering, so the window is
deliberately generous.

## Client-side change required

**The outbound wire format is unchanged.** `terminal_auth` still carries `command`
and `explanation`; `deployment_auth` still carries `target_file`, `summary`, and
`content`. `{"status": "APPROVE"}` still means approve.

The one new obligation: **the reply must echo the `task_id` from the request
verbatim.** Both events now carry a `task_id` field, exactly as the Cursor RPC
actions in ADR 0004 do.

```json
// server → client
{"type": "terminal_auth", "command": "...", "explanation": "...", "task_id": "9f2c..."}
// client → server
{"status": "APPROVE", "task_id": "9f2c..."}
```

A SwiftUI client that does not echo `task_id` will have its reply treated as a new
Shop task, and the pipeline will sit until the 600-second timeout and then deny.
That is a visible ten-minute stall rather than a silent mis-delivery, which is the
right way for this to fail while the clients are updated. The clients must be
updated to round-trip the field.

## Consequences

- Concurrent tasks on one socket each get their own answer, because the futures
  are keyed by `task_id`. Two pipelines, or a pipeline and an agent task, can now
  hold approval requests open simultaneously without stealing from each other.
- An approval reply can no longer be mistaken for a new task, and a chat message
  can no longer be mistaken for an approval.
- Headless operation is untouched. Every site keeps its `if self.ws:` guard and
  the `websocket=None` heartbeat path still reports the same "HEADLESS ERROR"
  strings to the model.
- The shop lane still is not migrated to the agent loop (ADR 0001 stands). This
  fixes the transport underneath it and nothing else.
