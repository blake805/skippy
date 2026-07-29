# Websocket contract for the SwiftUI clients

The SwiftUI shop UI is a first-class client of this server and its own codebase, so
the wire format is a contract rather than an implementation detail. This document
is the reference for it.

Read this before changing anything in `_serve_socket`, `SkippyPipeline`, or
`SkippyAgent` that touches `send_json`.

## Connecting

| Endpoint | Default lane | Used by |
| --- | --- | --- |
| `/ws/factory?client_id=swiftui` | Shop | the SwiftUI app |
| `/ws/agent?client_id=<id>` | Agent | coding-agent clients |

`client_id` identifies the connection in the hub so the server can address RPCs to
a specific client. It defaults to `swiftui` on `/ws/factory`, which is why the
existing app works without passing it.

Both endpoints run the same handler; only the default `mode` differs. Sending
`{"mode": "Agent"}` on `/ws/factory` routes that one task to the coding agent, so a
single socket can drive both lanes.

## Client to server

### Start a shop task

Either raw text, or JSON:

```jsonc
{"mode": "Shop", "text": "chipload for 6061 with a 3-flute 1/4 endmill?",
 "history": ["You: ...", "Skippy: ..."], "use_tts": true}
```

Raw (non-JSON) text is accepted and treated as `mode: "Shop"`. `mode` may be any of
`Shop`, `Software`, `CNC`, `Developer`, `Whiteboard`.

### Start an agent task

```jsonc
{"type": "agent_task", "mode": "Agent", "project_id": "shop-jarvis",
 "session_id": "<optional, to resume>", "text": "add retry to query_model",
 "workspace_roots": ["/Volumes/skippy_workspaces/shop-jarvis"],
 "auto_approve": {"terminal": false, "git_push": false},
 "max_steps": 40, "dry_run": false}
```

`workspace_roots` may be omitted if the project is registered (see
`skippy_sessions.SessionStore.ensure_project`).

### Cancel a running agent task

```jsonc
{"type": "agent_cancel", "session_id": "s-abc123"}
```

### Answer an authorization request

See the next section — this is the one shape that is changing.

### Handshake

`{"type": "hello"}`, `{"type": "ping"}`, or `{"type": "register"}` are answered with
`{"type": "hello_ack", "client_id": "<id>"}` and start no work.

## Authorization replies must echo `task_id`

> **Breaking change.** This arrives with PR #5 (`docs/adr/0005-approval-routing.md`).
> Update the SwiftUI app before the server that serves your shop is restarted on
> that code.

The server used to read the socket directly to collect an approval, racing the
endpoint's own read loop (two coroutines, one socket, whichever was parked in
`receive_text` won). Approvals now round-trip through the hub instead, keyed by a
`task_id` — the same mechanism the agent lane and the Cursor RPCs already use.

The outbound events are otherwise unchanged. `terminal_auth` still carries `command`
and `explanation`; `deployment_auth` still carries `target_file`, `summary`, and
`content`. `{"status": "APPROVE"}` still means approve, and anything else — a
denial, a timeout, a closed socket — fails closed.

The single new obligation is to send the request's `task_id` back verbatim:

```jsonc
// server -> client
{"type": "terminal_auth", "command": "df -h", "explanation": "check disk",
 "task_id": "9f2c48e1-..."}

// client -> server
{"status": "APPROVE", "task_id": "9f2c48e1-..."}
```

**If the app does not echo it**, the reply is treated as a brand new Shop task and
the waiting pipeline stalls for the full 600-second `HUMAN_APPROVAL_TIMEOUT` before
denying. That is a visible ten-minute hang, not a silent mis-authorization, which is
the correct way for this to fail — but it does mean the Tormach SSH gate and the
Developer-mode deploy gate stop working until the client is updated.

On the app side this is small: keep the `task_id` from the auth event alongside
whatever state holds the pending prompt, and include it in the reply.

```swift
// When a terminal_auth or deployment_auth event arrives:
pendingAuthTaskID = event["task_id"] as? String

// When the user taps Approve or Deny:
var reply: [String: Any] = ["status": approved ? "APPROVE" : "DENY"]
if let taskID = pendingAuthTaskID { reply["task_id"] = taskID }
send(reply)
```

### Never put `task_id` on an ordinary message

`_serve_socket` routes **any** inbound message carrying a `task_id` to
`hub.resolve_response` and returns immediately, before it considers dispatching
work. A chat payload that happens to include a `task_id` field is therefore
swallowed as a reply to something and never runs. Reserve the field for replies.

## Server to client

Every event is a JSON object with a `type`.

### Both lanes

| `type` | Payload | Meaning |
| --- | --- | --- |
| `log` | `content` | Progress text for the activity view |
| `chat` | `content` | Conversational output for the transcript |
| `done` | — | This task is finished; stop the spinner |
| `hello_ack` | `client_id` | Handshake answered |

### Shop lane

| `type` | Payload | Meaning |
| --- | --- | --- |
| `audio` | `data` (base64 WAV) | A spoken sentence, when `use_tts` is set |
| `terminal_stream_start` | — | An authorized command is about to stream |
| `terminal_stream` | `content` | One line of live command output |
| `terminal_auth` | `command`, `explanation`, `task_id` | Approve or deny a shell/SSH command |
| `deployment_auth` | `target_file`, `summary`, `content`, `task_id` | Approve or deny overwriting a source file |
| `write_file` | `path`, `content` | Write this payload to disk natively |

### Agent lane

| `type` | Payload | Meaning |
| --- | --- | --- |
| `agent_step` | `phase`, `content`, `step` | The model's reasoning for this step |
| `agent_tool_call` | `tool`, `args`, `call_id`, `step` | A tool is about to run; bulky args are trimmed |
| `agent_tool_result` | `call_id`, `tool`, `ok`, `summary`, `content` | What the tool returned |
| `agent_patch` | `files[{path, action, added, removed}]`, `diff`, `via` | Files changed; `via` is `cursor` or `filesystem` |
| `agent_done` | `status`, `summary`, `files_changed`, `steps` | Terminal state: `success`, `failed`, `max_steps`, or `cancelled` |
| `agent_cancelled` | `session_id`, `found` | A cancel request was acknowledged |

Every `agent_*` event also carries `session_id` and `step`.

The agent lane emits `log`, `chat`, and `done` as well, so a client that only knows
the shop vocabulary still shows progress and a final answer — it just will not
render diffs or per-tool detail. Unknown `type` values should be ignored rather
than treated as errors; that is what keeps this list extensible.
