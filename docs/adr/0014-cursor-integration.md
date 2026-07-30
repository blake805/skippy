# 0014 — Cursor integration: one patch tool, and diagnostics the agent cannot forget to read

Status: accepted
Date: 2026-07-30
Implements and amends [ADR 0004](0004-cursor-rpc.md). Depends on the agent being
reachable at all, which arrived in the same body of work.

## Context

ADR 0004 accepted the shape of this in Phase 4 — a sideloaded VS Code-compatible
extension connecting to the hub as a websocket client — and specified the actions, the
per-action timeouts, and the single `WorkspaceEdit` for undo. It was never built here.

Two things had changed since it was written, and one thing it did not say.

## Decisions

### There is one `apply_patch`, not two

ADR 0004 described a `cursor_apply_patch` alongside the existing tool. That makes the
model choose between two tools on the basis of state it cannot observe — whether an
editor happens to be attached — and it will choose wrong. The routing is now invisible:
`apply_patch` goes through the editor when there is one and writes to disk when there is
not, and nothing in the tool schema mentions Cursor.

This is the same principle as the three preceding slices. Anything that must happen is
done by the loop, not requested of the model.

### The editor is a writer, not a second implementation of patching

`skippy_edit.apply_patch` gained one parameter: a `writer` that replaces the write step
and nothing else. Validation, path resolution, the staged-content model, the diff, the
patch journal and the rollback all stay exactly where they were.

That matters because this file is where every dangerous bug in this project has lived —
UTF-8 corruption, CRLF rewriting, an off-by-one in occurrence handling — and a second
copy of it in TypeScript would be a second place for those to come back. The editor is
handed the final text of each file and asked to put it there. It never re-runs a search.

Two consequences follow. The editor is never handed a path that escapes the workspace
roots, because validation happened before it was consulted. And the patch journal is
written even when the editor applies the change: the editor's undo stack lasts as long
as the window, while the journal is what covers a crash.

### Diagnostics come back attached to the patch

ADR 0004 listed `get_diagnostics` as an action and said nothing about when to call it.
Left as a tool, the agent would have to remember to ask after every edit — and on the
evidence of the two previous slices, where the model batched its RE findings at the end
against explicit instructions and never once called `recall_project` because the opening
message already had what it needed, it would not.

So a successful patch returns the editor's diagnostics for the files it touched, in the
same observation. A clean patch says so explicitly, because silence is ambiguous between
"nothing wrong" and "nobody looked".

**Only the diagnostics this change caused are reported.** The first live run handed the
agent every diagnostic for a file it had touched. It could not tell which of them its own
edit had caused, tried to fix one that had been there all along, patched a second time,
re-read the file, and burned five of its steps before the repetition guard stopped it.
Any repository with a pre-existing warning in a file being edited would do that on every
patch.

So the editor's diagnostics are snapshotted before the write and diffed against the state
after it. Attribution ignores line numbers, because inserting two lines at the top of a
file moves everything below and position-keyed comparison would blame the patch for all of
it. It counts rather than subtracts, so a change that adds a *second* instance of an
existing problem is still reported. Pre-existing diagnostics are reduced to a count and
explicitly called unrelated, because saying nothing about them leaves the agent unsure
whether anything was checked at all.

**Diagnostics are waited for, not sampled.** Language servers re-analyse asynchronously,
so reading diagnostics the instant an edit lands returns the state from *before* it: a
new error looks like a clean bill of health, and an error the patch just fixed still
looks broken. Either one teaches the agent something false about its own change. The
extension waits for a diagnostics event covering the touched files, then for a short
quiet period — analysis arrives in bursts, a syntax pass then a type pass, and returning
after the first would report a half-formed picture — and gives up after a ceiling so a
file with no language server does not hang the call.

### There is no command execution action

ADR 0004 listed `run_task`, and the implementation it came from used Node's `exec`: a
real shell, an arbitrary command string, no allowlist. That predates
[ADR 0011](0011-command-execution.md), which built a deliberately narrow execution path
— deny by default, no shell, no metacharacters, a scrubbed environment, per-program
denylists for destructive subcommands.

Shipping `run_task` would have been a second execution path with none of that policy,
reachable through a websocket that has no authentication. The action is dropped. The
server already knows how to run things safely and the editor does not need to.

### Refusing is not the same as failing

The fallback to a direct write happens when the editor is absent or cannot apply the
change. It deliberately does **not** happen when the user declines the edit: writing it
to disk anyway would be the exact opposite of what they just asked for.

That distinction is carried structurally, as a flag set on the writer, rather than by
searching the failure message for the word "declined". `apply_patch` reports failures as
strings, and recognising a decline by pattern-matching one would mean any change to the
extension's wording silently turns a refusal into a write of the refused change.

## Consequences

The agent works identically with or without Cursor. Without it: patches are written
directly and diagnostics are simply unavailable. With it: patches are one undo step and
every edit reports what it broke. Nothing in between requires the model to know which
world it is in.

Two patch implementations now exist, and the only real defence against them drifting is
that they are checked against the same table. `tests/fixtures/patch_parity.json` holds 27
cases — ambiguity, occurrence counting, literal `$&` replacement, CRLF, non-ASCII, tabs,
all-or-nothing, create and delete — and both `tests/test_cursor.py` and
`cursor_client/test/parity.test.js` run it. Both assert the table is populated, because a
silently empty fixture is the one way a parity check passes while testing nothing.

The extension is sideloaded as a `.vsix`. There is no marketplace listing and no
intention of one.

### Defects found porting the earlier implementation

Four places where the editor's patch semantics had drifted from the server's. Each one
would have produced a different result depending on whether Cursor was attached.

- **`String.prototype.replace` expands `$&`, `$1`, `` $` `` and `$'` in the replacement**
  even when the pattern is a plain string. A replacement containing a dollar sign became
  something else entirely, while Python's `str.replace` is literal. Any patch touching a
  shell variable, a regex, or a `$1` in a snippet.
- **`occurrence` counted overlapping matches.** Advancing by one character rather than by
  the length of the search means that for `"aa"` in `"aaaa"`, the count reports two
  occurrences and occurrence 2 resolves to the span at index 1 — overlapping the first,
  and not the one the model asked for. The server had exactly this bug and had already
  fixed it; the port carried the original.
- **`replace_all` together with `occurrence` was silently resolved** in favour of
  `replace_all`, where the server refuses both. The editor would have applied an edit the
  server rejects.
- **A missing occurrence returned the text unchanged**, which combined with the counting
  bug to report a successful edit that changed nothing. A silent no-op presented as a
  success is worse than an error.

And one I introduced: injecting the writer left `failed_on` unset on the failure path, so
a writer that raised sent `None` to `sandbox.relative()` and crashed instead of reporting
the failure. A writer applies the whole set or none of it, so there is no single file to
name, and the message now says so.

### Live verification

Driven over the socket against the 480B with a client speaking the extension's protocol
and performing the writes.

First run, before attribution existed: a diagnostic was planted on every patch. The agent
patched, re-read, patched again, re-read, hit the repetition guard, and tried `cat` and
`python -c` to get at the file — five steps spent on a problem it had not caused. That is
the run that produced the attribution decision above.

Second run, with attribution, and a genuinely pre-existing warning reported both before
and after: the agent ignored it, made the change, wrote itself a script to check the
constants were wired up, ran it through `run_command`, and deleted it. The pre-existing
`unused_helper` was left alone. Its summary noted the change was "consistent with the
existing `DEFAULT_RETRIES` constant that was added in a previous session" — project memory
from ADR 0013 feeding the same run.

### Not addressed

`/ws/factory` has no authentication. `client_id` is a query parameter, so anything that
can reach the port can register as `cursor` and answer RPCs — or impersonate the editor.
Dropping `run_task` removes the worst of what impersonation would buy. It has to be
solved before the port is reachable from anywhere else, which is the remote-access work.

> **Correction.** This section originally said the missing authentication was
> "acceptable while the bind is loopback." The bind was not loopback: `skippy_factory.py`
> passed `host="0.0.0.0"`, and the SkippyServer boot line is `python skippy_factory.py`,
> so the deployed hub listened on every interface. Worse than editor impersonation, any
> message on that socket which is not a reply, a greeting or a cancel starts an agent
> run — so anything on the local network could edit the workspace roots and execute
> commands, and `apply_patch` followed by the interpreter walks around the `run_command`
> allowlist entirely. The default is now `127.0.0.1`, overridable through
> `SKIPPY_BIND_HOST` with a warning naming what the exposure is, because remote access
> is a real requirement and the answer there is a private interface rather than a public
> one. The reasoning above is now true rather than assumed.

Workspace roots still come from `SKIPPY_WORKSPACE_ROOTS` rather than from the editor's
open folders. `get_workspace_roots` is implemented on both sides and unused; wiring it up
means deciding what happens when the editor's folders and the configured roots disagree,
and the sandbox is the wrong place to be casual.

The extension does not show a diff for approval — `confirmPatches` is a modal count of
files, not a review. Edits arrive as one undo step, so rejecting one afterwards costs a
keystroke, which is why this is defensible for now rather than right.
