# Skippy Cursor Client

Gives Skippy hands inside Cursor: live diagnostics, the list of open files, and
multi-file edits that land in the editor's own undo stack.

The extension is a websocket **client** of the Skippy hub. It registers as
`client_id=cursor`, and the server addresses RPCs to that id.

## Build and install

```bash
cd cursor_client
npm install
npm run compile
npm test                     # patch semantics must match the server
npx @vscode/vsce package     # produces skippy-cursor-client.vsix
```

Then in Cursor: Command Palette -> *Extensions: Install from VSIX...* and pick the
`.vsix`. There is no marketplace listing; this is a private, sideloaded extension.

## Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `skippy.serverUrl` | `ws://127.0.0.1:8000/ws/factory` | Hub URL. Use the Mac Studio's hostname when Cursor runs elsewhere. |
| `skippy.clientId` | `cursor` | Must stay `cursor` unless you also change `skippy_cursor.CURSOR_CLIENT_ID`. |
| `skippy.autoConnect` | `true` | Connect when the window opens. |
| `skippy.confirmPatches` | `false` | Ask before applying an edit set. |
| `skippy.taskTimeoutMs` | `240000` | Kill a `run_task` command after this long. |

Commands: **Skippy: Connect to hub**, **Skippy: Disconnect from hub**,
**Skippy: Show connection status**. A status bar item shows the current state.

## Protocol

The hub sends a request; the extension replies on the same socket, echoing
`task_id` verbatim. That echo is what routes the reply back to the agent
coroutine waiting on it — drop it and the call times out.

```jsonc
// server -> extension
{"action": "get_diagnostics", "task_id": "3f2a...", "paths": ["/abs/path.py"]}

// extension -> server
{"task_id": "3f2a...", "ok": true, "result": {"diagnostics": [...]}}
{"task_id": "3f2a...", "ok": false, "error": "file is read-only"}
```

| Action | Request fields | Result |
| --- | --- | --- |
| `get_workspace_roots` | — | `{roots: [{name, path}]}` |
| `get_open_files` | — | `{files: [{path, active, dirty, language}]}` |
| `get_diagnostics` | `paths` (empty means all) | `{diagnostics: [{path, line, col, severity, message, source}]}` |
| `apply_patches` | `edits` | `{applied: [path], failed: [{index, path, reason}]}` |
| `create_file` | `path`, `content` | same as `apply_patches` |
| `run_task` | `command`, `cwd` | `{exit_code, output}` |

Edits use the same search/replace shape as the server's `apply_patch`, and
`src/patches.ts` deliberately reimplements those semantics rather than
approximating them: a `search` string must be unique unless `replace_all` or
`occurrence` says otherwise, and the whole set either applies or none of it does.
Non-`action` messages (the agent's progress and chat events) are ignored.

Open dirty buffers are read in preference to disk, so an edit is computed against
what the user is actually looking at. Touched documents are saved after the edit
so the agent's next `run_tests` sees the change.
