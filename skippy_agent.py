"""The agent loop: think, call tools, observe, repeat.

One role drives the whole loop (ADR 0001). The transcript only ever grows, because
mlx_lm.server caches prompts by prefix and rewriting any earlier message forces a
full re-prefill of everything after it — measured at roughly 60s against 3s on the
heavy role. `skippy_llm.Transcript` enforces that; this module's job is to never
work around it.

Native tool calling, not fenced JSON. That means the loop owes the server a `tool`
message for *every* tool call in an assistant message, including the ones it decides
not to run. An assistant turn with three tool calls and two answers is a malformed
transcript, and the failure surfaces later as confused output rather than an error.

The loop stops for one of four reasons, and says which: the model called `finish`,
it ran out of steps, it stopped calling tools, or it was cancelled. "Ran out of
steps" is never reported as success, however much was accomplished.
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional

import prompts
import skippy_dispatch
import skippy_exec
import skippy_llm
import skippy_paths
import skippy_re
import tool_schemas
from skippy_sandbox import Sandbox, ToolResult, cap_text

logger = logging.getLogger("skippy_agent")

DEFAULT_MAX_STEPS = 40
HARD_MAX_STEPS = 200

# Above this, an observation goes through the compressor first. The heavy role
# prefills at ~200 tok/s, so 8000 characters of raw tool output costs about ten
# seconds of latency on every subsequent step, not just the one that produced it.
COMPRESS_THRESHOLD = 8_000

# Folding is expensive (it invalidates the prompt cache), so this is generous
# enough that a normal task never reaches it.
MAX_TRANSCRIPT_CHARS = 220_000
FOLD_KEEP_LAST = 8

# Two consecutive turns with no tool call means the model has stopped working. One
# nudge, because the first is usually the model narrating instead of acting and it
# recovers; a second means it believes it is done and is not going to call finish.
NUDGE_LIMIT = 2

# Identical call this many times in the recent window and the loop intervenes.
REPEAT_LIMIT = 3
REPEAT_WINDOW = 6

FINISH_SCHEMA = {
    "type": "function",
    "function": {
        "name": "finish",
        "description": (
            "Ends the task. Call this when the work is done, or when you are blocked "
            "and cannot continue. Always call it rather than just describing what you did."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "What you changed and why, or what is blocking you.",
                },
                "files_changed": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Paths you modified, if any.",
                },
            },
            "required": ["summary"],
        },
    },
}


@dataclass
class AgentOutcome:
    status: str  # finished | max_steps | stopped_without_finish | cancelled | failed
    summary: str = ""
    steps: int = 0
    files_changed: List[str] = field(default_factory=list)
    tool_calls: int = 0
    # The RE equivalent of files_changed: what the run left behind. Zero for a coding
    # run, which has no pack.
    findings: int = 0
    pack_id: str = ""

    @property
    def ok(self) -> bool:
        """Only an explicit finish counts. A run that exhausted its budget may well
        have done useful work, but reporting it as success would hide the fact that
        the model never decided it was done."""
        return self.status == "finished"


class Cancelled(Exception):
    """Raised internally when a run is cancelled between steps."""


class AgentLoop:
    def __init__(
        self,
        task: str,
        sandbox: Sandbox,
        *,
        max_steps: Optional[int] = None,
        emit: Optional[Callable[[dict], Awaitable[None]]] = None,
        journal_dir: Optional[str] = None,
        role: Optional[str] = None,
        extra_context: str = "",
        mode: str = skippy_exec.DEFAULT_MODE,
        notes_root: Optional[str] = None,
        target: str = "",
    ):
        if not task or not str(task).strip():
            raise ValueError("An agent run needs a task.")

        self.mode = str(mode or skippy_exec.DEFAULT_MODE).lower()
        if self.mode not in skippy_exec.MODES:
            raise ValueError(
                f"Unknown mode {mode!r}. Known modes: {', '.join(sorted(skippy_exec.MODES))}."
            )

        self.task = str(task).strip()
        self.sandbox = sandbox
        # Only None means "use the default". An explicit 0 clamps to 1 rather than
        # falling through to 40 — this knob decides how much unattended editing
        # happens, so a caller passing 0 by mistake must not get a full run.
        budget = DEFAULT_MAX_STEPS if max_steps is None else int(max_steps)
        self.max_steps = max(1, min(budget, HARD_MAX_STEPS))
        self._emit = emit
        self.journal_dir = journal_dir
        self.role = role or skippy_llm.AGENT_PLANNER_ROLE

        self.step = 0
        self.tool_calls = 0
        self.files_changed: List[str] = []
        self._cancelled = False
        self._nudges = 0
        self._recent_calls: List[str] = []
        self._folds = 0

        # RE mode gets a pack keyed by the target, so a second session on the same
        # artifact accumulates onto the first instead of starting over. The loop opens
        # it, never the model — see `open_pack`.
        self.notes_pack = None
        if self.mode == "re":
            root = notes_root or skippy_paths.notes_root()
            self.notes_pack = skippy_re.open_pack(
                root, target=target or "", title=self.task[:80]
            )

        system = prompts.RE_SYSTEM if self.mode == "re" else prompts.AGENT_SYSTEM
        self.transcript = skippy_llm.Transcript(system=system)
        opening = "Workspace roots:\n" + "\n".join(
            f"- {sandbox.relative(root)} ({root})" for root in sandbox.roots
        )
        if self.notes_pack is not None:
            findings = len(self.notes_pack.finding_files())
            opening += f"\n\nNote pack: {self.notes_pack.pack_id} ({findings} finding(s) so far)"
            if findings:
                # Said here rather than left to the prompt, because the single most
                # wasteful thing an RE session can do is re-derive last week's work.
                opening += ". Call read_notes before starting; this target has been looked at before."
        if extra_context:
            opening += f"\n\n{extra_context}"
        self.transcript.append({"role": "user", "content": f"{opening}\n\nTask: {self.task}"})

    # -- plumbing ---------------------------------------------------------

    def cancel(self) -> None:
        """Ask the run to stop. Takes effect at the next step boundary, so a tool
        already running is allowed to finish rather than being torn down mid-write."""
        self._cancelled = True

    async def emit(self, event: dict) -> None:
        if self._emit is None:
            return
        try:
            await self._emit(event)
        except Exception:
            # A dead websocket must not kill a run that is midway through editing
            # files. The run is the valuable thing; the UI can reconnect.
            logger.warning("Event sink failed; continuing without it.", exc_info=True)
            self._emit = None

    def tools(self) -> List[dict]:
        offered = (
            tool_schemas.re_tools() if self.mode == "re" else tool_schemas.workspace_tools()
        )
        return offered + [FINISH_SCHEMA]

    # -- the loop ---------------------------------------------------------

    async def run(self) -> AgentOutcome:
        await self.emit({"type": "agent_start", "task": self.task, "max_steps": self.max_steps})
        try:
            outcome = await self._loop()
        except Cancelled:
            outcome = AgentOutcome(
                status="cancelled",
                summary=f"Cancelled after {self.step} step(s).",
                steps=self.step,
                files_changed=list(self.files_changed),
                tool_calls=self.tool_calls,
            )
        except skippy_llm.ModelError as exc:
            # Distinguished from a task failure: nothing is wrong with the task, the
            # model endpoint is unreachable or misconfigured.
            outcome = AgentOutcome(
                status="failed",
                summary=f"Model unavailable: {exc}",
                steps=self.step,
                files_changed=list(self.files_changed),
                tool_calls=self.tool_calls,
            )
        await self.emit({
            "type": "agent_done",
            "status": outcome.status,
            "summary": outcome.summary,
            "steps": outcome.steps,
            "files_changed": outcome.files_changed,
        })
        return outcome

    def _outcome(self, status: str, summary: str) -> AgentOutcome:
        return AgentOutcome(
            status=status,
            summary=summary,
            steps=self.step,
            files_changed=list(self.files_changed),
            tool_calls=self.tool_calls,
            findings=len(self.notes_pack.finding_files()) if self.notes_pack else 0,
            pack_id=self.notes_pack.pack_id if self.notes_pack else "",
        )

    async def _loop(self) -> AgentOutcome:
        while self.step < self.max_steps:
            if self._cancelled:
                raise Cancelled()

            self.step += 1
            await self._fold_if_needed()

            message = await skippy_llm.query_message(
                self.transcript.messages,
                role=self.role,
                temp=0.1,
                tools=self.tools(),
            )

            thought = message["content"]
            calls = message["tool_calls"]
            if thought:
                await self.emit({"type": "agent_thought", "step": self.step, "content": thought})

            # Appended before the tools run, so the transcript is in a valid state
            # even if a tool raises something unforeseen.
            self.transcript.append(skippy_llm.assistant_turn(message))

            if not calls:
                self._nudges += 1
                if self._nudges >= NUDGE_LIMIT:
                    return self._outcome(
                        "stopped_without_finish",
                        thought or "The model stopped calling tools without calling finish.",
                    )
                self.transcript.append({
                    "role": "user",
                    "content": (
                        "You did not call a tool. Continue working on the task by calling a "
                        "tool, or call finish if it is complete or you are blocked."
                    ),
                })
                continue

            self._nudges = 0
            finish: Optional[dict] = None

            for index, call in enumerate(calls):
                name, args = call["name"], call["arguments"]

                if name == "finish":
                    finish = args
                    # Answered so the transcript stays valid; the loop exits below.
                    self._answer(call, ToolResult(True, "Run ended."))
                    # Anything the model asked for after finish is not run, but must
                    # still be answered or the assistant turn is left malformed.
                    for skipped in calls[index + 1:]:
                        self._answer(
                            skipped,
                            ToolResult(False, "Not executed: the run ended with finish."),
                        )
                    break

                result = await self._run_tool(call)
                observation = await self._observe(name, result)
                self._answer(call, result, observation)

            if finish is not None:
                summary = str(finish.get("summary") or "").strip() or "Task reported complete."
                for path in finish.get("files_changed") or []:
                    if isinstance(path, str) and path not in self.files_changed:
                        self.files_changed.append(path)
                return self._outcome("finished", summary)

        # What survived the run is mode-specific, and reporting the wrong one makes a
        # productive run look empty: an RE run never changes a file, so "files changed:
        # none" is both true and actively misleading about work that is sitting on disk.
        if self.notes_pack is not None:
            kept = f"Findings recorded: {len(self.notes_pack.finding_files())} in pack {self.notes_pack.pack_id}"
        else:
            kept = f"Files changed: {', '.join(self.files_changed) or 'none'}"
        return self._outcome(
            "max_steps",
            f"Ran out of steps after {self.max_steps} without finishing. {kept}.",
        )

    async def _run_tool(self, call: dict) -> ToolResult:
        name, args = call["name"], call["arguments"]
        self.tool_calls += 1
        await self.emit({
            "type": "agent_tool_call",
            "step": self.step,
            "call_id": call["id"],
            "tool": name,
            "args": redact(args),
        })

        if self._is_looping(name, args):
            result = ToolResult(
                False,
                f"You have called {name} with identical arguments {REPEAT_LIMIT} times without "
                "making progress. Change your approach, or call finish and explain what is "
                "blocking you.",
            )
        else:
            result = await skippy_dispatch.dispatch(
                name, args, self.sandbox, journal_dir=self.journal_dir,
                mode=self.mode, notes_pack=self.notes_pack,
            )

        # apply_patch is the only tool that reports files, and it reports them the
        # same way whether or not the write was a dry run.
        if result.ok and result.data.get("files") and not result.data.get("dry_run"):
            for report in result.data["files"]:
                path = report.get("path")
                if path and path not in self.files_changed:
                    self.files_changed.append(path)
            await self.emit({
                "type": "agent_patch",
                "step": self.step,
                "files": result.data["files"],
                "diff": result.data.get("diff", ""),
            })

        event = result.as_event()
        event.update({
            "type": "agent_tool_result",
            "step": self.step,
            "call_id": call["id"],
            "tool": name,
        })
        await self.emit(event)
        return result

    def _answer(self, call: dict, result: ToolResult, observation: Optional[str] = None) -> None:
        """Append the `tool` message that answers one call. Every call gets one."""
        self.transcript.append({
            "role": "tool",
            "tool_call_id": call["id"],
            "content": observation if observation is not None else result.as_observation(),
        })

    async def _observe(self, name: str, result: ToolResult) -> str:
        """What the model actually sees, compressed if it is too big to be worth it."""
        raw = result.as_observation()
        if len(raw) <= COMPRESS_THRESHOLD:
            return raw

        await self.emit({"type": "agent_compress", "step": self.step, "tool": name,
                         "chars": len(raw)})
        try:
            condensed = await skippy_llm.compress(
                result.content,
                instruction=f"The agent ran `{name}` while working on: {self.task}",
            )
        except Exception:
            # Truncation is worse than compression but much better than failing the
            # step, and the model is told which it got.
            logger.warning("Compression failed; truncating instead.", exc_info=True)
            return cap_text(raw, COMPRESS_THRESHOLD) + "\n[truncated: compression unavailable]"

        head = ("OK: " if result.ok else "ERROR: ") + result.summary
        return f"{head}\n[compressed from {len(raw)} chars]\n{condensed}"

    def _is_looping(self, name: str, args: dict) -> bool:
        """True when the same call keeps coming back.

        Compared over a window rather than against only the previous call, because
        the common stuck pattern is alternating between two calls, which a
        compare-with-last check never catches.
        """
        signature = json.dumps({"t": name, "a": args}, sort_keys=True, default=str)
        self._recent_calls.append(signature)
        del self._recent_calls[:-REPEAT_WINDOW]
        return self._recent_calls.count(signature) >= REPEAT_LIMIT

    async def _fold_if_needed(self) -> None:
        """Compact the transcript when it gets too long, and say so.

        This is the one operation that breaks the prompt cache on purpose, so the
        next step pays a full prefill. It is loud for that reason.
        """
        size = sum(len(m.get("content") or "") for m in self.transcript.messages)
        if size <= MAX_TRANSCRIPT_CHARS:
            return

        await self.emit({"type": "agent_fold", "step": self.step, "chars": size})
        history = "\n\n".join(
            f"[{m.get('role')}] {m.get('content') or ''}" for m in self.transcript.messages
        )
        try:
            summary = await skippy_llm.compress(
                history, instruction=prompts.FOLD_SUMMARY, word_budget=800
            )
        except Exception:
            logger.warning("Fold summary failed; keeping the transcript as-is.", exc_info=True)
            return

        self.transcript = self.transcript.fold(FOLD_KEEP_LAST, summary)
        self._folds += 1


def redact(args: Optional[dict]) -> Dict[str, Any]:
    """Trim bulky arguments out of the event stream.

    The full diff is emitted separately by the patch event, so repeating whole file
    bodies here would only bloat the socket.
    """
    trimmed: Dict[str, Any] = {}
    for key, value in (args or {}).items():
        if key == "edits" and isinstance(value, list):
            trimmed[key] = [
                {"path": e.get("path"), "action": e.get("action", "edit")}
                for e in value if isinstance(e, dict)
            ]
        elif isinstance(value, str) and len(value) > 600:
            trimmed[key] = value[:600] + f"... [+{len(value) - 600} chars]"
        else:
            trimmed[key] = value
    return trimmed


async def run_task(
    task: str,
    sandbox: Sandbox,
    *,
    max_steps: Optional[int] = None,
    emit: Optional[Callable[[dict], Awaitable[None]]] = None,
    journal_dir: Optional[str] = None,
    role: Optional[str] = None,
    mode: str = skippy_exec.DEFAULT_MODE,
    notes_root: Optional[str] = None,
    target: str = "",
) -> AgentOutcome:
    """Convenience entry point for one task."""
    loop = AgentLoop(
        task, sandbox, max_steps=max_steps, emit=emit, journal_dir=journal_dir,
        role=role, mode=mode, notes_root=notes_root, target=target,
    )
    return await loop.run()
