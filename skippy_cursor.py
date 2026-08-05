"""Bridge to the Cursor/VS Code extension.

The extension connects to the hub as `client_id=cursor` and answers RPCs. That gives
the agent two things the filesystem cannot: the editor's live diagnostics, and edits
that land in the editor's own undo stack instead of appearing as changes on disk that
the user never asked for and cannot undo.

Wire protocol. Server sends:

    {"action": "get_diagnostics", "task_id": "<uuid>", "paths": ["..."]}

Extension replies on the same socket, echoing `task_id`:

    {"task_id": "<uuid>", "ok": true, "result": {...}}
    {"task_id": "<uuid>", "ok": false, "error": "..."}

`task_id` is what routes the reply back to the waiting coroutine, so the extension must
echo it verbatim.

Two decisions worth stating.

**The model is never told whether Cursor is attached.** There is one `apply_patch`, and
it routes to the editor when there is one and writes to disk when there is not. An
earlier design had a separate `cursor_apply_patch` tool; that makes the model choose
between two tools on the basis of state it cannot see, and it will choose wrong.

**Validation always happens on the server, before the editor is involved.** The editor
is never handed a path, so it is never handed one that escapes the workspace roots, and
a patch that the server would reject is rejected identically whether or not Cursor is
running. The whole risk of having an editor-side implementation is that the two drift;
`tests/test_cursor.py` and `cursor_client/test/patches.test.js` check the same table of
cases against both.
"""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Sequence

import skippy_edit
from skippy_sandbox import Sandbox, ToolResult, cap_text

logger = logging.getLogger("skippy_cursor")

CURSOR_CLIENT_ID = "cursor"

# How long the human has to answer a code-edit approval card before it fails.
# Generous, like the device-write gate in ADR 0005: the person may be reading a
# large diff, or across the room, and a premature timeout is worse than a wait.
CODE_APPROVAL_TIMEOUT = 600.0

# The diff shown in an approval card is bounded — a 40,000-line generated-file
# rewrite is not something a human reads, and shipping it to a phone over a
# cellular link is worse. The full diff still reaches the transcript.
MAX_APPROVAL_DIFF_CHARS = 40_000

# A workspace edit legitimately takes far longer than a query for the open file list,
# so timeouts are per action rather than one global value. The old global 10 seconds
# would have failed every interesting call.
ACTION_TIMEOUTS: Dict[str, float] = {
    "ping": 5.0,
    "get_workspace_roots": 10.0,
    "get_open_files": 10.0,
    "get_diagnostics": 30.0,
    "apply_patches": 120.0,
}
DEFAULT_TIMEOUT = 30.0

MAX_DIAGNOSTICS = 60
MAX_DIAGNOSTIC_CHARS = 6_000


class CursorBridge:
    def __init__(self, hub, client_id: str = CURSOR_CLIENT_ID):
        self.hub = hub
        self.client_id = client_id

    @property
    def connected(self) -> bool:
        return self.client_id in getattr(self.hub, "active_connections", {})

    async def call(
        self, action: str, payload: Optional[dict] = None, timeout: Optional[float] = None
    ) -> dict:
        if not self.connected:
            return {"ok": False, "error": f"Cursor client '{self.client_id}' is not connected."}

        request = dict(payload or {})
        request["action"] = action
        response = await self.hub.execute_tool_on_client(
            self.client_id,
            request,
            timeout=timeout or ACTION_TIMEOUTS.get(action, DEFAULT_TIMEOUT),
        )

        if not isinstance(response, dict):
            return {"ok": False, "error": f"Malformed reply for '{action}': {response!r}"}
        if response.get("error"):
            return {"ok": False, "error": str(response["error"])}
        if response.get("ok") is False:
            return {
                "ok": False,
                "error": str(response.get("error") or "Cursor reported a failure."),
            }

        result = response.get("result")
        if result is None:
            # Tolerate an extension that answers with a flat payload.
            result = {
                key: value
                for key, value in response.items()
                if key not in ("task_id", "ok", "action")
            }
        return {"ok": True, "result": result}

    # -- convenience wrappers --------------------------------------------

    async def workspace_roots(self) -> List[str]:
        response = await self.call("get_workspace_roots")
        if not response["ok"]:
            logger.info("Cursor workspace roots unavailable: %s", response["error"])
            return []
        resolved = []
        for root in response["result"].get("roots") or []:
            path = root.get("path") if isinstance(root, dict) else root
            if isinstance(path, str) and path:
                resolved.append(path)
        return resolved

    async def open_files(self) -> dict:
        return await self.call("get_open_files")

    async def diagnostics(self, paths: Optional[Sequence[str]] = None, settle: bool = False) -> dict:
        return await self.call(
            "get_diagnostics", {"paths": list(paths or []), "settle": bool(settle)}
        )

    async def apply_patches(self, edits: List[dict]) -> dict:
        return await self.call("apply_patches", {"edits": edits})


class CodeApprover:
    """The in-app approval gate for code edits.

    The Cursor extension applies hub edits silently by default (its own
    `confirmPatches` is off), so without this gate a coding run edits the tree
    with no human in the loop — fine for a trusted local agent, wrong for the
    "brainstorm in the app, approve in the app" flow the desktop is built for.

    This shows the unified diff on the run's own socket and waits, reusing the
    exact `request_authorization` channel device writes travel (ADR 0005). It
    is the twin of `DeviceService.approve_write`, and returns the same way: a
    failed `ToolResult` means declined, `None` means proceed.

    Two deliberate differences from the device gate. Code edits are undoable and
    a disconnected app must not wedge the workflow, so a missing socket fails
    *open* (write, log a warning) rather than closed. And "approve all for this
    task" latches the gate off for the rest of the run, because approving every
    line of a fifteen-file refactor one card at a time is how a good feature
    becomes one nobody uses.
    """

    def __init__(
        self,
        hub: Any = None,
        client_id: str = "",
        timeout: float = CODE_APPROVAL_TIMEOUT,
    ):
        self.hub = hub
        self.client_id = client_id
        self.timeout = timeout
        self.enabled = True
        self._test_approver = None  # tests install an async (payload)->reply here

    async def approve(self, summary: str, diff: str, files: Sequence[dict]) -> Optional[ToolResult]:
        if not self.enabled:
            return None
        payload = {
            "type": "code_auth",
            "explanation": summary or "Skippy wants to change your files.",
            "diff": cap_text(diff or "", MAX_APPROVAL_DIFF_CHARS),
            "files": list(files or []),
        }
        reply = await self._request(payload)
        status = str(reply.get("status", "")).upper()
        if status == "APPROVE":
            if str(reply.get("scope", "")).lower() == "all":
                # The rest of this run writes without asking again.
                self.enabled = False
            return None
        reason = reply.get("reason") or "you declined the change in the app"
        return ToolResult(
            False,
            f"The edit was not applied: {reason}.",
            data={"declined": True},
        )

    async def _request(self, payload: dict) -> dict:
        override = self._test_approver
        if override is not None:
            return await override(payload)
        if self.hub is None or not self.client_id:
            logger.info("No client bound for code approval; applying without a gate.")
            return {"status": "APPROVE"}
        socket_ = getattr(self.hub, "active_connections", {}).get(self.client_id)
        if socket_ is None:
            logger.warning(
                "Code approval requested but client '%s' is offline; applying "
                "without a gate so the run is not wedged.", self.client_id,
            )
            return {"status": "APPROVE"}
        return await self.hub.request_authorization(socket_, payload, timeout=self.timeout)


def build_code_approver(hub: Any, client_id: str) -> Optional["CodeApprover"]:
    """A gate for this run, or None when approvals are turned off.

    SKIPPY_CODE_APPROVAL=off disables the gate entirely (edits apply straight
    through, the pre-gate behaviour). Any other value, or unset, means the app
    is the approval surface.
    """
    if os.environ.get("SKIPPY_CODE_APPROVAL", "app").strip().lower() in ("off", "0", "false", "no"):
        return None
    return CodeApprover(hub=hub, client_id=client_id)


def format_diagnostics(entries: Any, limit: int = MAX_DIAGNOSTICS) -> str:
    """Render the extension's diagnostics into something a model can act on."""
    if isinstance(entries, dict):
        entries = entries.get("diagnostics")
    if not entries:
        return ""
    lines = []
    for entry in list(entries)[:limit]:
        if not isinstance(entry, dict):
            continue
        location = f"{entry.get('path', '?')}:{entry.get('line', '?')}"
        column = entry.get("col")
        if column not in (None, ""):
            location += f":{column}"
        source = entry.get("source")
        suffix = f"  [{source}]" if source else ""
        severity = str(entry.get("severity", "info")).lower()
        lines.append(f"{severity}: {location} {entry.get('message', '')}{suffix}")
    if len(entries) > limit:
        lines.append(f"... [{len(entries) - limit} more]")
    return cap_text("\n".join(lines), MAX_DIAGNOSTIC_CHARS)


def _fingerprint(entry: dict) -> tuple:
    """Identify a diagnostic in a way that survives the edit that follows it.

    Deliberately excludes the line number. Inserting two lines at the top of a file
    moves every diagnostic below it, and keying on position would report all of them as
    newly caused by the patch.
    """
    return (
        str(entry.get("path", "")),
        str(entry.get("severity", "")).lower(),
        str(entry.get("message", "")),
        str(entry.get("source", "")),
    )


def new_diagnostics(before: Sequence[dict], after: Sequence[dict]) -> List[dict]:
    """The diagnostics this change is responsible for.

    A live run showed why this matters. The agent was handed every diagnostic for a file
    it had touched, could not tell which of them its own edit had caused, tried to fix
    one that was there all along, patched a second time, re-read the file, and burned
    five of its steps before the repetition guard stopped it. Any repository with
    pre-existing warnings would do that on every single patch.

    Counted rather than set-subtracted, so a change that adds a *second* instance of a
    problem that already existed once is still reported.
    """
    counts: Dict[tuple, int] = {}
    for entry in before:
        if isinstance(entry, dict):
            counts[_fingerprint(entry)] = counts.get(_fingerprint(entry), 0) + 1

    fresh = []
    for entry in after:
        if not isinstance(entry, dict):
            continue
        key = _fingerprint(entry)
        if counts.get(key, 0) > 0:
            counts[key] -= 1
            continue
        fresh.append(entry)
    return fresh


def _blocking_editor_writer(bridge: CursorBridge, loop: asyncio.AbstractEventLoop):
    """Adapt the async RPC to the synchronous writer `apply_patch` expects.

    `apply_patch` runs in a worker thread, so the call is scheduled back onto the event
    loop that owns the websocket. Doing the RPC from the thread directly would touch the
    socket from outside the loop, which is exactly the race the hub's comments warn
    about elsewhere.
    """

    def call(coro, timeout: float):
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)

    def write(planned: Sequence[Any]) -> List[str]:
        # Taken before anything is written, so the diagnostics that follow can be
        # attributed. No settle wait: this is the state the editor already holds.
        paths = [item.path for item in planned]
        before: List[dict] = []
        try:
            snapshot = call(bridge.diagnostics(paths), ACTION_TIMEOUTS["get_diagnostics"] + 5.0)
            if snapshot.get("ok"):
                before = list((snapshot.get("result") or {}).get("diagnostics") or [])
        except Exception:
            # Attribution is a nicety; losing it must not cost the patch.
            logger.info("Could not read diagnostics before the patch.", exc_info=True)

        edits = []
        for item in planned:
            if item.after is None:
                edits.append({"path": item.path, "action": "delete"})
            elif item.before is None:
                edits.append({"path": item.path, "action": "create", "content": item.after})
            else:
                # The server has already staged the exact final text, so the editor is
                # told to replace the file wholesale rather than re-running a search.
                # Re-deriving the edit there is what would let the two implementations
                # disagree about a patch they have both already agreed to.
                edits.append({
                    "path": item.path,
                    "action": "create",
                    "content": item.after,
                    "overwrite": True,
                })

        response = call(bridge.apply_patches(edits), ACTION_TIMEOUTS["apply_patches"] + 15.0)
        if not response.get("ok"):
            raise RuntimeError(response.get("error") or "Cursor refused the edit.")

        result = response.get("result") or {}
        failed = result.get("failed") or []
        if failed:
            reasons = [
                f"{item.get('path', '?')}: {item.get('reason', '?')}"
                for item in failed
                if isinstance(item, dict)
            ]
            # Recorded on the writer rather than left to be read back out of the error
            # message. `apply_patch` reports a failure as a string, so recognising a
            # decline by searching that string would turn any change to the extension's
            # wording into a silent write of a change the user had just refused.
            write.declined = any(
                "declined" in str(item.get("reason", "")).lower()
                for item in failed
                if isinstance(item, dict)
            )
            raise RuntimeError(f"Cursor rejected the edit ({'; '.join(reasons)})")

        applied = [str(path) for path in (result.get("applied") or [])]
        after = list(result.get("diagnostics") or [])
        write.diagnostics = new_diagnostics(before, after)
        write.pre_existing = len(after) - len(write.diagnostics)
        return applied

    write.diagnostics = []
    write.pre_existing = 0
    write.declined = False
    return write


async def apply_patch(
    sandbox: Sandbox,
    edits: Sequence[dict],
    bridge: Optional[CursorBridge] = None,
    dry_run: bool = False,
    journal_dir: Optional[str] = None,
    approver: Optional[CodeApprover] = None,
) -> ToolResult:
    """Apply an edit set, through the editor when one is attached.

    Falls back to writing directly whenever the editor is absent *or refuses*. A
    refusal is usually the user declining the change, and in that case a silent local
    write would be the opposite of what they asked for — so the fallback happens only
    for a transport failure, and a declined edit is reported as a failure.

    When an `approver` is present and this is not a dry run, the human is shown the
    exact diff first and can decline before anything is written — whether the write
    would go to the editor or to disk.
    """
    if approver is not None and not dry_run:
        # Stage the change without touching anything, so the card shows the real
        # diff and a validation failure is reported now rather than after a
        # pointless approval. The staged text is deterministic from these edits,
        # so what is approved is exactly what gets written a moment later.
        preview = await asyncio.to_thread(
            skippy_edit.apply_patch, sandbox, edits, dry_run=True
        )
        if not preview.ok:
            return preview
        declined = await approver.approve(
            preview.summary,
            preview.data.get("diff", ""),
            preview.data.get("files", []),
        )
        if declined is not None:
            return declined

    local = None
    if bridge is None or not bridge.connected or dry_run:
        return await asyncio.to_thread(
            skippy_edit.apply_patch, sandbox, edits, dry_run=dry_run, journal_dir=journal_dir
        )

    writer = _blocking_editor_writer(bridge, asyncio.get_running_loop())
    result = await asyncio.to_thread(
        skippy_edit.apply_patch, sandbox, edits, journal_dir=journal_dir, writer=writer
    )

    if result.ok:
        result.data["applied_in_editor"] = True
        # Only what this change caused. Handing over every problem in a file it happened
        # to touch is what sent a live run chasing a pre-existing warning for five steps.
        rendered = format_diagnostics(writer.diagnostics)
        existing = getattr(writer, "pre_existing", 0)
        if rendered:
            # Attached to the patch result rather than left for a follow-up call: a
            # separate round trip is one the model has to remember to make, which on the
            # evidence of the RE and memory work it will not.
            result.data["diagnostics"] = writer.diagnostics
            result.content = (
                f"{result.content}\n\nThis change introduced "
                f"{len(writer.diagnostics)} editor diagnostic(s):\n{rendered}"
            )
            result.summary = (
                f"{result.summary} This introduced {len(writer.diagnostics)} diagnostic(s)."
            )
        elif existing:
            # Named so the agent does not go looking for them, and does not mistake the
            # file being clean for the file being untouched by problems.
            result.summary = (
                f"{result.summary} No new diagnostics ({existing} pre-existing one(s) in "
                "these files are unrelated to this change)."
            )
        else:
            result.summary = f"{result.summary} The editor reports no diagnostics."
        return result

    if writer.declined:
        return ToolResult(
            False,
            "You declined the change in the editor, so nothing was written.",
            result.content,
            {"declined": True},
        )

    # The editor was there but could not do it. Writing directly is better than
    # failing the task, and saying so matters: the user loses the single undo step.
    logger.info("Editor could not apply the patch (%s); writing directly.", result.summary)
    local = await asyncio.to_thread(
        skippy_edit.apply_patch, sandbox, edits, journal_dir=journal_dir
    )
    if local.ok:
        local.summary = (
            f"{local.summary} (Cursor could not apply this, so it was written to disk "
            "directly and is not a single undo step in the editor.)"
        )
    return local
