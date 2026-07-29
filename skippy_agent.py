"""SkippyAgent: the Cursor-class coding loop.

This is deliberately *not* the shop assembly line. There is no architect ->
engineer -> QA handoff and no requirement that output land in `skills/`. One
model reasons, calls tools, reads the results, and keeps going until it decides
the task is done -- which is what makes multi-file work possible.

The shop pipeline in `skippy_factory.SkippyPipeline` is untouched; requests only
reach here when the payload asks for mode "Agent" (or "RE").
"""

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import skippy_agent_tools as agent_tools
import skippy_llm
import skippy_paths
from prompts import PROMPTS
from skippy_agent_tools import Sandbox, SandboxError, ToolContext, ToolResult

logger = logging.getLogger("skippy_agent")

DEFAULT_MAX_STEPS = 40
HARD_MAX_STEPS = 200
COMPRESS_THRESHOLD = 8_000
MAX_HISTORY_CHARS = 220_000
NUDGE_LIMIT = 3
REPEAT_LIMIT = 3

# Registry of in-flight runs so an inbound agent_cancel can reach them.
ACTIVE_AGENTS: Dict[str, "SkippyAgent"] = {}


def cancel_session(session_id: str) -> bool:
    agent = ACTIVE_AGENTS.get(session_id)
    if agent is None:
        return False
    agent.cancel()
    return True


# ---------------------------------------------------------------------------
# Tool-call parsing
# ---------------------------------------------------------------------------

FENCE_PATTERN = re.compile(r"```(?:json|JSON)?\s*\n(.*?)```", re.DOTALL)
RESERVED_KEYS = {"tool", "name", "args", "arguments", "thought", "reasoning"}


def extract_json_objects(text: str) -> List[dict]:
    """Pull every top-level JSON object out of a blob of prose.

    Uses `raw_decode` rather than a regex because tool payloads (notably
    `apply_patch`) nest objects and contain braces inside string literals, which
    any brace-counting regex gets wrong.
    """
    decoder = json.JSONDecoder()
    found: List[dict] = []
    index = 0
    length = len(text)
    while index < length:
        index = text.find("{", index)
        if index == -1:
            break
        try:
            obj, end = decoder.raw_decode(text, index)
        except ValueError:
            index += 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
            index = end
        else:
            index += 1
    return found


def _normalize_call(obj: dict) -> Optional[Tuple[str, dict]]:
    name = obj.get("tool") or obj.get("name")
    if not isinstance(name, str) or not name:
        return None
    raw_args = obj.get("args")
    if not isinstance(raw_args, dict):
        raw_args = obj.get("arguments")
    if not isinstance(raw_args, dict):
        raw_args = {key: value for key, value in obj.items() if key not in RESERVED_KEYS}
    return name, raw_args


def parse_tool_call(text: str) -> Tuple[Optional[str], Optional[dict], Optional[str]]:
    """Return (tool_name, args, parse_error) for the model's latest turn."""
    if not text or not text.strip():
        return None, None, "Empty response."

    candidates: List[dict] = []
    for block in FENCE_PATTERN.findall(text):
        candidates.extend(extract_json_objects(block))
    if not candidates:
        candidates = extract_json_objects(text)

    calls = [call for call in (_normalize_call(obj) for obj in candidates) if call]
    if not calls:
        return None, None, "No JSON tool call found."

    known = [call for call in calls if call[0] in agent_tools.TOOL_SPECS_BY_NAME]
    chosen = (known or calls)[-1]
    return chosen[0], chosen[1], None


def strip_tool_call(text: str) -> str:
    """The prose the model wrote around its tool call, for the activity log."""
    without_fences = FENCE_PATTERN.sub("", text)
    brace = without_fences.find("{")
    if brace != -1:
        without_fences = without_fences[:brace]
    return without_fences.strip()


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

@dataclass
class AgentOutcome:
    status: str = "failed"
    summary: str = ""
    files_changed: List[str] = field(default_factory=list)
    steps: int = 0


class SkippyAgent:
    def __init__(
        self,
        websocket,
        payload: dict,
        manager,
        session_store: Any = None,
        speak=None,
        cursor_bridge: Any = None,
    ):
        self.ws = websocket
        self.hub = manager
        self.store = session_store
        self.speak = speak
        self.cursor = cursor_bridge
        self.payload = payload or {}

        self.mode = self.payload.get("mode", "Agent")
        self.task = (self.payload.get("text") or self.payload.get("task") or "").strip()
        self.project_id = self.payload.get("project_id") or "scratch"
        self.session_id = self.payload.get("session_id") or f"s-{uuid.uuid4().hex[:12]}"
        self.dry_run = bool(self.payload.get("dry_run"))
        self.use_tts = bool(self.payload.get("use_tts"))
        self.auto_approve = dict(self.payload.get("auto_approve") or {})
        self.max_steps = max(1, min(int(self.payload.get("max_steps") or DEFAULT_MAX_STEPS), HARD_MAX_STEPS))

        self.planner_role = self.payload.get("planner_role") or skippy_llm.AGENT_PLANNER_ROLE
        self.step = 0
        self.messages: List[dict] = []
        self.files_changed: List[str] = []
        self.final_summary = ""
        self.session = None
        self.sandbox: Optional[Sandbox] = None
        self.ctx: Optional[ToolContext] = None
        self._cancelled = False
        self._nudges = 0
        self._last_call: Optional[str] = None
        self._repeats = 0

    # -- plumbing ---------------------------------------------------------

    def cancel(self):
        self._cancelled = True

    async def emit(self, event: dict):
        event.setdefault("session_id", self.session_id)
        event.setdefault("step", self.step)
        if self.ws is None:
            logger.info("HEADLESS AGENT %s: %s", event.get("type"), event.get("summary") or event.get("content", ""))
            return
        try:
            await self.ws.send_json(event)
        except Exception:
            pass

    async def log(self, message: str):
        """Legacy `log` event so existing SwiftUI clients still show progress."""
        await self.emit({"type": "log", "content": message})

    async def chat(self, message: str):
        await self.emit({"type": "chat", "content": message})

    async def _approve(self, command: str, explanation: str) -> bool:
        if self.ws is None:
            return False
        await self.log(f"\n⚠️ *Agent requests approval:* `{command}`\n")
        reply = await self.hub.request_on_socket(
            self.ws,
            {"type": "terminal_auth", "command": command, "explanation": explanation},
            timeout=600.0,
        )
        approved = str(reply.get("status", "")).upper() == "APPROVE"
        await self.log("✅ Approved.\n" if approved else "❌ Denied.\n")
        return approved

    # -- setup ------------------------------------------------------------

    async def _resolve_roots(self) -> List[str]:
        candidates = list(self.payload.get("workspace_roots") or [])
        if not candidates and self.store is not None:
            meta = self.store.project_meta(self.project_id)
            candidates = list((meta or {}).get("workspace_roots") or [])
        if not candidates and self.cursor is not None and self.cursor.connected:
            candidates = await self.cursor.workspace_roots()
            if candidates:
                await self.log(f"📎 *Adopted {len(candidates)} workspace root(s) from Cursor.*\n")
        if not candidates:
            default_root = os.path.join(skippy_paths.workspaces_root(), self.project_id)
            if os.path.isdir(default_root):
                candidates = [default_root]
        return candidates

    async def _build_preamble(self) -> str:
        """Front-load the cheap context the model would otherwise burn steps on."""
        parts = [
            f"PROJECT: {self.project_id}",
            f"SESSION: {self.session_id}",
            "WORKSPACE ROOTS:\n" + "\n".join(f"  - {root}" for root in self.sandbox.roots),
        ]
        if self.dry_run:
            parts.append("DRY RUN: no writes will be committed to disk. Report what you would change.")

        tree = agent_tools.list_dir(self.ctx, ".", depth=2)
        if tree.ok:
            parts.append("WORKSPACE TREE (depth 2):\n" + tree.content)

        if self.store is not None:
            meta = self.store.project_meta(self.project_id) or {}
            conventions = meta.get("conventions") or {}
            if conventions:
                parts.append(
                    "PROJECT CONVENTIONS:\n"
                    + "\n".join(f"  {key}: {value}" for key, value in conventions.items())
                )
            recall = await agent_tools.search_project_memory(self.ctx, self.task, k=6)
            if recall.ok and recall.content:
                parts.append("RELEVANT PROJECT MEMORY:\n" + recall.content)

        return "\n\n".join(parts)

    # -- main loop --------------------------------------------------------

    async def run(self) -> AgentOutcome:
        outcome = AgentOutcome()
        ACTIVE_AGENTS[self.session_id] = self

        try:
            if not self.task:
                await self.emit({"type": "agent_done", "status": "failed", "summary": "No task text supplied."})
                await self.emit({"type": "done"})
                return outcome

            roots = await self._resolve_roots()
            try:
                self.sandbox = Sandbox(roots)
            except SandboxError as exc:
                message = (
                    f"Cannot start: {exc} Send 'workspace_roots' in the payload, or register "
                    f"them on project '{self.project_id}'."
                )
                await self.log(f"❌ {message}\n")
                await self.emit({"type": "agent_done", "status": "failed", "summary": message})
                await self.emit({"type": "done"})
                outcome.summary = message
                return outcome

            if self.store is not None:
                self.session = self.store.start_session(
                    project_id=self.project_id,
                    session_id=self.session_id,
                    task=self.task,
                    mode=self.mode,
                    workspace_roots=self.sandbox.roots,
                )

            self.ctx = ToolContext(
                sandbox=self.sandbox,
                dry_run=self.dry_run,
                memory=self.store.memory_for(self.project_id) if self.store else None,
                approve=self._approve,
                emit=self.emit,
                auto_approve=self.auto_approve,
                session_id=self.session_id,
                cursor=self.cursor,
            )

            await self.log(
                f"🤖 *Agent session `{self.session_id}` starting on "
                f"{self.sandbox.relative(self.sandbox.primary) or self.sandbox.primary} "
                f"(mode: {self.mode}, model role: {self.planner_role})*\n"
            )

            system_prompt = self._system_prompt()
            preamble = await self._build_preamble()
            self.messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"{preamble}\n\nTASK:\n{self.task}"},
            ]
            for entry in self._prior_thread():
                self.messages.insert(-1, entry)

            outcome = await self._loop()

        except Exception as exc:
            logger.exception("Agent crashed")
            outcome.status = "failed"
            outcome.summary = f"Agent crashed: {type(exc).__name__}: {exc}"
            await self.log(f"❌ *{outcome.summary}*\n")
        finally:
            ACTIVE_AGENTS.pop(self.session_id, None)
            outcome.files_changed = self.files_changed
            outcome.steps = self.step
            if self.session is not None:
                try:
                    self.session.finish(
                        status=outcome.status,
                        summary=outcome.summary,
                        files_changed=self.files_changed,
                    )
                except Exception:
                    logger.exception("Failed to persist session %s", self.session_id)

            await self.emit(
                {
                    "type": "agent_done",
                    "status": outcome.status,
                    "summary": outcome.summary,
                    "files_changed": self.files_changed,
                    "steps": outcome.steps,
                }
            )
            if outcome.summary:
                await self.chat(outcome.summary)
                if self.speak is not None and self.use_tts and self.ws is not None:
                    try:
                        await self.speak(outcome.summary, self.ws, True)
                    except Exception:
                        pass
            await self.emit({"type": "done"})

        return outcome

    def _system_prompt(self) -> str:
        block = PROMPTS.get(self.mode) or PROMPTS["Agent"]
        system = block.get("system") or PROMPTS["Agent"]["system"]
        return system.replace("{{TOOL_SPEC}}", agent_tools.render_tool_spec())

    def _prior_thread(self) -> List[dict]:
        history = self.payload.get("history") or []
        entries: List[dict] = []
        for line in history[-10:]:
            if not isinstance(line, str):
                continue
            role = "user" if line.startswith("You:") else "assistant"
            entries.append(
                {"role": role, "content": line.replace("You: ", "", 1).replace("Skippy: ", "", 1)}
            )
        return entries

    async def _loop(self) -> AgentOutcome:
        outcome = AgentOutcome(status="max_steps")

        while self.step < self.max_steps:
            if self._cancelled:
                outcome.status = "cancelled"
                outcome.summary = f"Cancelled by request after {self.step} step(s)."
                return outcome

            self.step += 1
            self._trim_history()

            await self.emit({"type": "agent_step", "phase": "think", "content": ""})
            try:
                response = await skippy_llm.query_model(
                    self.messages,
                    role=self.planner_role,
                    temp=0.1,
                    stop=["OBSERVATION:", "TOOL RESULT:"],
                    raise_on_error=True,
                )
            except skippy_llm.ModelError as exc:
                outcome.status = "failed"
                outcome.summary = f"Model unreachable: {exc}"
                return outcome

            thought = strip_tool_call(response)
            if thought:
                await self.emit({"type": "agent_step", "phase": "think", "content": thought})
                await self.log(f"\n[Agent step {self.step}] {thought}\n")

            name, args, parse_error = parse_tool_call(response)

            if name is None:
                self._nudges += 1
                if self._nudges >= NUDGE_LIMIT:
                    outcome.status = "success" if self.files_changed else "failed"
                    outcome.summary = thought or response.strip()
                    return outcome
                self.messages.append({"role": "assistant", "content": response})
                self.messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"{parse_error} You must respond with exactly one JSON tool call in a "
                            '```json fenced block, e.g. {"tool": "read_file", "args": {"path": "..."}}. '
                            'When the task is complete call {"tool": "finish", "args": '
                            '{"summary": "...", "files_changed": ["..."]}}.'
                        ),
                    }
                )
                continue

            self._nudges = 0

            if name == "finish":
                outcome.status = "success"
                outcome.summary = str(args.get("summary") or "").strip() or "Task reported complete."
                for path in args.get("files_changed") or []:
                    if isinstance(path, str) and path not in self.files_changed:
                        self.files_changed.append(path)
                await self.log(f"\n✅ *Agent finished after {self.step} step(s).*\n")
                return outcome

            call_id = f"c-{uuid.uuid4().hex[:8]}"
            await self.emit(
                {"type": "agent_tool_call", "tool": name, "args": _redact(args), "call_id": call_id}
            )

            if self._is_looping(name, args):
                observation = (
                    f"You have called {name} with identical arguments {REPEAT_LIMIT} times. "
                    "That is not making progress. Either try a different approach or call finish "
                    "and explain what is blocking you."
                )
                result = ToolResult(False, observation)
            else:
                if self.session is not None:
                    # Pre-images for this step land beside the session on the NAS.
                    self.ctx.backup_dir = self.session.backup_dir(self.step)
                result = await agent_tools.dispatch(name, args, self.ctx)

            if name == "save_decision" and result.ok and self.session is not None:
                decision_id = result.data.get("decision_id")
                if decision_id and decision_id not in self.session.decisions:
                    self.session.decisions.append(decision_id)

            # Any tool that reports file changes feeds the patch stream, so the
            # Cursor-mediated path emits the same events as the direct one.
            if result.ok and result.data.get("files"):
                for report in result.data["files"]:
                    path = report.get("path")
                    if path and path not in self.files_changed:
                        self.files_changed.append(path)
                await self.emit(
                    {
                        "type": "agent_patch",
                        "files": result.data["files"],
                        "diff": result.data.get("diff", ""),
                        "via": result.data.get("via", "filesystem"),
                    }
                )

            event = result.as_event()
            event.update({"type": "agent_tool_result", "call_id": call_id, "tool": name})
            await self.emit(event)
            await self.log(f"    ↳ {result.summary}\n")

            observation = await self._observation(name, result)
            self.messages.append({"role": "assistant", "content": response})
            self.messages.append({"role": "user", "content": f"OBSERVATION:\n{observation}"})

            if self.session is not None:
                try:
                    self.session.record_turn(
                        step=self.step,
                        tool=name,
                        args=args,
                        ok=result.ok,
                        result_summary=result.summary,
                        thought=thought,
                    )
                except Exception:
                    logger.exception("Failed to record turn %d", self.step)

        outcome.summary = (
            f"Hit the {self.max_steps}-step budget without calling finish. "
            f"Files changed so far: {', '.join(self.files_changed) or 'none'}."
        )
        return outcome

    def _is_looping(self, name: str, args: dict) -> bool:
        signature = json.dumps({"t": name, "a": args}, sort_keys=True, default=str)
        if signature == self._last_call:
            self._repeats += 1
        else:
            self._last_call = signature
            self._repeats = 1
        return self._repeats > REPEAT_LIMIT

    async def _observation(self, tool_name: str, result: ToolResult) -> str:
        raw = result.as_observation()
        if len(raw) <= COMPRESS_THRESHOLD:
            return raw
        await self.log("    ↳ *(compressing oversized tool output via the :8082 node)*\n")
        try:
            condensed = await skippy_llm.compress(
                result.content,
                instruction=f"The agent ran `{tool_name}` while working on: {self.task}",
            )
            head = ("OK: " if result.ok else "ERROR: ") + result.summary
            return f"{head}\n[compressed]\n{condensed}"
        except Exception:
            logger.warning("Compression failed; falling back to truncation.")
            return agent_tools._cap(raw, COMPRESS_THRESHOLD)

    def _trim_history(self):
        """Drop the oldest observation pairs when the transcript gets too long.

        The system prompt and the original task always survive; those carry the
        contract and the goal.
        """
        while len(self.messages) > 4:
            total = sum(len(message.get("content") or "") for message in self.messages)
            if total <= MAX_HISTORY_CHARS:
                return
            del self.messages[2:4]


def _redact(args: dict) -> dict:
    """Trim bulky argument bodies out of the event stream (the diff is emitted separately)."""
    trimmed: Dict[str, Any] = {}
    for key, value in (args or {}).items():
        if isinstance(value, str) and len(value) > 600:
            trimmed[key] = value[:600] + f"... [+{len(value) - 600} chars]"
        elif key == "edits" and isinstance(value, list):
            trimmed[key] = [
                {
                    "path": (edit or {}).get("path"),
                    "action": (edit or {}).get("action", "edit"),
                }
                for edit in value
                if isinstance(edit, dict)
            ]
        else:
            trimmed[key] = value
    return trimmed


async def run_agent_task(
    websocket,
    payload: dict,
    manager,
    session_store: Any = None,
    speak=None,
    cursor_bridge: Any = None,
) -> AgentOutcome:
    """Entry point used by the websocket endpoints in `skippy_factory`."""
    agent = SkippyAgent(
        websocket,
        payload,
        manager,
        session_store=session_store,
        speak=speak,
        cursor_bridge=cursor_bridge,
    )
    return await agent.run()
