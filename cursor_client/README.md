# Skippy Cursor client

A sideloaded extension that connects Cursor (or VS Code) to the Skippy hub, so Skippy can
apply edits you can undo and read the diagnostics your language servers produce.

It is a websocket *client*: it connects out to the hub and answers requests. Skippy's
agent loop is the thing that initiates work, and it also has to run with no editor
attached, which is why this is not an MCP server.

## Build and install

```bash
cd cursor_client
npm install
npm test          # compiles, then runs the patch semantics and parity tests
npx vsce package --out skippy-cursor-client.vsix
```

Then in Cursor: **Extensions → … → Install from VSIX**, and pick the file. There is no
marketplace listing.

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `skippy.serverUrl` | `ws://127.0.0.1:8000/ws/factory` | The hub. Use the Studio's hostname when Cursor runs on another machine. |
| `skippy.clientId` | `cursor` | The id the hub addresses RPCs to. Leave it alone unless you are running two editors. |
| `skippy.autoConnect` | `true` | Connect when the window opens. |
| `skippy.confirmPatches` | `false` | Ask before applying an edit set. Off by default because edits arrive as a single undo step, so rejecting one afterwards costs a keystroke. |

The status bar shows the connection, and **Skippy: Show connection status** opens the log
if something looks wrong.

## What it does

| Action | Purpose |
| --- | --- |
| `ping` | Liveness. |
| `get_workspace_roots` | The editor's open folders. |
| `get_open_files` | Which files are open, which is active, which are unsaved. |
| `get_diagnostics` | Errors and warnings, optionally after waiting for analysis to settle. |
| `apply_patches` | Apply an edit set as one `WorkspaceEdit`, then return diagnostics for what changed. |

Every reply echoes the request's `task_id`; that is what routes it back to the agent
coroutine waiting on it.

## Two things worth knowing

**It does not run commands.** An earlier design had a `run_task` action that shelled out.
That would have been a second execution path with none of the policy the server's
`run_command` enforces — no allowlist, a real shell — behind a socket with no
authentication. The server runs things; the editor does not.

**It does not decide what an edit means.** The server validates the patch, resolves the
paths against the sandbox, and stages the exact final text of every file. This extension
is handed that text and puts it there. `src/patches.ts` still implements the search and
replace semantics, because the editor needs to plan against unsaved buffers, and
`test/parity.test.js` checks it against the same table of cases as the server —
`tests/fixtures/patch_parity.json`. If those two ever disagree, Skippy gives different
answers depending on whether your editor happens to be open, which is the worst kind of
bug to own.
