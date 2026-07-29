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

import skippy_edit
import skippy_fs
from skippy_sandbox import Sandbox, SandboxError, ToolResult

logger = logging.getLogger("skippy_dispatch")

# Sync tools run in a thread so a large read or a slow disk cannot block the event
# loop, which would stall the websocket the UI is watching.
_SYNC_TOOLS = {
    "list_dir": skippy_fs.list_dir,
    "read_file": skippy_fs.read_file,
    "glob_files": skippy_fs.glob_files,
    "apply_patch": skippy_edit.apply_patch,
}

_ASYNC_TOOLS = {
    "grep": skippy_fs.grep,
}

# Handled by the loop itself, not here, but named so that dispatch can give a
# coherent error if it ever arrives out of place.
CONTROL_TOOLS = ("finish",)

TOOL_NAMES = tuple(sorted(set(_SYNC_TOOLS) | set(_ASYNC_TOOLS) | set(CONTROL_TOOLS)))


def _expected(name: str) -> str:
    """The parameter list, for a bad-arguments message the model can act on."""
    handler = _SYNC_TOOLS.get(name) or _ASYNC_TOOLS.get(name)
    if handler is None:
        return ""
    params = [
        p for p in inspect.signature(handler).parameters
        if p not in ("sandbox", "journal_dir")
    ]
    return ", ".join(params)


async def dispatch(
    name: str,
    args: Optional[dict],
    sandbox: Sandbox,
    journal_dir: Optional[str] = None,
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

    # Never model-controlled.
    args.pop("sandbox", None)
    args.pop("journal_dir", None)
    if name == "apply_patch" and journal_dir:
        args["journal_dir"] = journal_dir

    try:
        if name in _SYNC_TOOLS:
            return await asyncio.to_thread(handler, sandbox, **args)
        return await handler(sandbox, **args)
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
