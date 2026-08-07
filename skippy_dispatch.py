"""Running one tool by name, turning every possible failure into an observation.

The agent loop must always get something back. An exception escaping a tool would
strand the run: the model never learns what went wrong, so it cannot correct
itself, and the transcript is left with a tool call that has no answer. So every
failure mode here becomes a `ToolResult` with `ok=False` and a message written for
the model rather than for a log file.

Two arguments are injected rather than accepted from the model. The sandbox, because
letting a tool call choose its own roots would defeat the point of having them. And
the patch journal, for the same reason — an agent that can redirect where its own
pre-images go can arrange for them not to exist.
"""

import asyncio
import inspect
import logging
from typing import Any, Dict, Optional

import skippy_brief
import skippy_device
import skippy_exec
import skippy_cursor
import skippy_fs
import skippy_git
import skippy_memory
import skippy_re
import skippy_research
import skippy_extract
import skippy_rizin
from skippy_sandbox import Sandbox, SandboxError, ToolResult

logger = logging.getLogger("skippy_dispatch")

# Sync tools run in a thread so a large read or a slow disk cannot block the event
# loop, which would stall the websocket the UI is watching.
_SYNC_TOOLS = {
    "list_dir": skippy_fs.list_dir,
    "read_file": skippy_fs.read_file,
    "glob_files": skippy_fs.glob_files,
    "note_finding": skippy_re.note_finding,
    "read_notes": skippy_re.read_notes,
    "note_claim": skippy_brief.note_claim,
    "read_brief": skippy_brief.read_brief,
    "record_decision": skippy_memory.record_decision,
    "recall_project": skippy_memory.recall_project,
    "resolve_work_item": skippy_memory.resolve_work_item,
}

# These take a `pack` the loop opens, and only exist when there is one. The disassembly
# tools are here for a second reason as well as the first: the pack is where the target
# artifact is recorded, so taking the pack is what stops a tool call from naming its own
# target and reading a file the session was never pointed at.
_NOTES_TOOLS = (
    "note_finding", "read_notes", "list_symbols", "disassemble_function", "decompile",
    "extract_artifact", "list_extracted",
)

# These take the project memory the loop opens, for the same reason.
_MEMORY_TOOLS = ("record_decision", "recall_project", "resolve_work_item")

# These take the research session the loop opens: the search backend and the HTTP
# client behind it. Injected for the same reason as the sandbox — a tool call that could
# name its own backend could name its own endpoint, and the whole value of routing the
# web through one object is that there is one place the rules live.
_RESEARCH_TOOLS = skippy_research.RESEARCH_TOOLS

# These take the brief the loop opens, the way the notes tools take the pack: it is
# where a run's sources live, and taking it is what stops a claim being filed against
# another question's evidence.
_BRIEF_TOOLS = ("note_claim", "read_brief")

# Live hardware. First argument is the DeviceService the loop owns; they are
# async because writes wait on a human approval and remote hosts go over RPC.
_DEVICE_TOOLS = skippy_device.DEVICE_TOOLS

_ASYNC_TOOLS = {
    "grep": skippy_fs.grep,
    "run_command": skippy_exec.run_command,
    # Async because it may route the write through the attached editor.
    "apply_patch": skippy_cursor.apply_patch,
    # Async because each starts a rizin process. Never allowlisted for `run_command`:
    # rizin's -c is a command language with a shell escape in it (ADR 0018).
    "list_symbols": skippy_rizin.list_symbols,
    "disassemble_function": skippy_rizin.disassemble_function,
    "decompile": skippy_rizin.decompile,
    # Async because each starts a container. Extraction is the one RE operation that
    # writes files, and it writes them into the pack rather than a workspace root.
    "extract_artifact": skippy_extract.extract_artifact,
    "list_extracted": skippy_extract.list_extracted,
    # Async for the subprocess, and git_commit for its approval wait.
    "git_status": skippy_git.git_status,
    "git_diff": skippy_git.git_diff,
    "git_branch": skippy_git.git_branch,
    "git_commit": skippy_git.git_commit,
    # Remote sync: writes beyond the bench, so both gate through the approver.
    "git_push": skippy_git.git_push,
    "git_pull": skippy_git.git_pull,
    "list_devices": skippy_device.list_devices,
    "serial_open": skippy_device.serial_open,
    "serial_io": skippy_device.serial_io,
    "serial_close": skippy_device.serial_close,
    "usb_transfer": skippy_device.usb_transfer,
    "usb_control": skippy_device.usb_control,
    "net_connect": skippy_device.net_connect,
    "net_io": skippy_device.net_io,
    "net_scan": skippy_device.net_scan,
    # Pins and buses. Bridge-only: there is no local backend to fall back to.
    "i2c_scan": skippy_device.i2c_scan,
    "i2c_io": skippy_device.i2c_io,
    "gpio_io": skippy_device.gpio_io,
    "adc_read": skippy_device.adc_read,
    # The web. Async because both are network round trips, and neither touches the disk.
    "web_search": skippy_research.web_search,
    "web_fetch": skippy_research.web_fetch,
}

# Handled by the loop itself, not here, but named so that dispatch can give a
# coherent error if it ever arrives out of place. `investigate` spawns a run, and what
# that spends is steps — which the loop owns and the dispatcher knows nothing about.
CONTROL_TOOLS = ("finish", "investigate")

TOOL_NAMES = tuple(sorted(set(_SYNC_TOOLS) | set(_ASYNC_TOOLS) | set(CONTROL_TOOLS)))


def _expected(name: str) -> str:
    """The parameter list, for a bad-arguments message the model can act on."""
    handler = _SYNC_TOOLS.get(name) or _ASYNC_TOOLS.get(name)
    if handler is None:
        return ""
    params = [
        p for p in inspect.signature(handler).parameters
        if p not in (
            "sandbox", "journal_dir", "mode", "pack", "memory", "service", "session",
            "brief",
        )
    ]
    return ", ".join(params)


async def dispatch(
    name: str,
    args: Optional[dict],
    sandbox: Sandbox,
    journal_dir: Optional[str] = None,
    mode: str = skippy_exec.DEFAULT_MODE,
    notes_pack: Optional[Any] = None,
    memory: Optional[Any] = None,
    cursor: Optional[Any] = None,
    devices: Optional[Any] = None,
    approver: Optional[Any] = None,
    research: Optional[Any] = None,
    brief: Optional[Any] = None,
) -> ToolResult:
    """Run one tool. Never raises."""
    args: Dict[str, Any] = dict(args or {})

    if name in CONTROL_TOOLS:
        return ToolResult(
            False,
            f"'{name}' is handled by the agent loop and cannot be dispatched as a tool.",
        )

    handler = _SYNC_TOOLS.get(name) or _ASYNC_TOOLS.get(name)
    if handler is None:
        # Listing the real names is what turns a hallucinated tool into a
        # recoverable mistake. Without it the model tends to guess again.
        return ToolResult(
            False,
            f"Unknown tool '{name}'. Available tools: {', '.join(TOOL_NAMES)}",
        )

    if "_malformed_arguments" in args:
        raw = args.pop("_malformed_arguments")
        return ToolResult(
            False,
            f"Arguments for '{name}' were not valid JSON, so the call could not be read. "
            f"Expected fields: {_expected(name)}.",
            str(raw)[:400],
        )

    # Never model-controlled. `mode` belongs here for the same reason as the sandbox:
    # a model in RE mode that could request the coding table could execute the
    # artifact it was asked to analyse, which is the one thing the split prevents.
    for injected in (
        "sandbox", "journal_dir", "mode", "pack", "memory", "bridge", "writer",
        "service", "approver", "session", "brief",
    ):
        args.pop(injected, None)
    if name == "apply_patch":
        if journal_dir:
            args["journal_dir"] = journal_dir
        # Routed through the editor when one is attached, so the change is a single
        # undo step. The model is not told which happened and has no way to ask: it
        # would only be a choice it could get wrong.
        args["bridge"] = cursor
        # The human-in-the-app gate. None means no gate (headless, or approvals
        # turned off), and apply_patch writes as before.
        args["approver"] = approver
    if name in ("git_commit", "git_push", "git_pull"):
        # The same human gate as apply_patch: a commit is a write to history,
        # and push/pull are writes beyond the bench entirely.
        args["approver"] = approver
    if name == "run_command":
        args["mode"] = mode
    # The notes tools write to a pack rather than to the workspace, so they take the
    # pack as their first argument where every other tool takes the sandbox.
    if name in _NOTES_TOOLS:
        if notes_pack is None:
            tail = (
                "This looks like a coding task; record what you found in your finish summary."
                if name in ("note_finding", "read_notes")
                else "This looks like a coding task, which has a repository rather than a "
                     "target artifact to read."
            )
            return ToolResult(
                False,
                f"'{name}' needs a note pack, which only reverse-engineering mode opens. "
                + tail,
            )
        first = notes_pack
    elif name in _MEMORY_TOOLS:
        if memory is None:
            return ToolResult(
                False,
                f"'{name}' needs project memory, which is unavailable for this run "
                "(the memory root may not be mounted). Put anything worth keeping in "
                "your finish summary instead.",
            )
        first = memory
    elif name in _BRIEF_TOOLS:
        if brief is None:
            return ToolResult(
                False,
                f"'{name}' needs a research brief, which only a research run opens. "
                "This looks like a coding or reverse-engineering task; record what you "
                "found in your finish summary, or with record_decision if it is a choice "
                "a later session needs.",
            )
        first = brief
    elif name in _RESEARCH_TOOLS:
        if research is None:
            return ToolResult(
                False,
                f"'{name}' needs a research session, which only a research run opens. "
                "This run has no way to reach the web: answer from what you can read "
                "here, and say plainly which parts you could not check.",
            )
        first = research
    elif name in _DEVICE_TOOLS:
        if devices is None:
            return ToolResult(
                False,
                f"'{name}' needs a device service, which only reverse-engineering mode "
                "opens. This looks like a coding task.",
            )
        first = devices
    else:
        first = sandbox

    try:
        if name in _SYNC_TOOLS:
            return await asyncio.to_thread(handler, first, **args)
        return await handler(first, **args)
    except SandboxError as exc:
        # Expected often enough to be a normal observation rather than an error:
        # the model asked for something outside the workspace and needs to know.
        return ToolResult(False, f"Sandbox violation: {exc}")
    except TypeError as exc:
        return ToolResult(
            False,
            f"Bad arguments for '{name}': {exc}. Expected fields: {_expected(name)}.",
        )
    except FileNotFoundError as exc:
        return ToolResult(False, f"Not found: {exc}")
    except Exception as exc:
        # A crash is a bug in Skippy, not in the model's request, so it is logged
        # with a traceback here and reported plainly to the model.
        logger.exception("Tool '%s' crashed", name)
        return ToolResult(False, f"Tool '{name}' raised {type(exc).__name__}: {exc}")
