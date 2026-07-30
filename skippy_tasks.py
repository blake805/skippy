"""Running agent tasks on behalf of a connected client.

Until this module existed, `skippy_agent` was complete, tested, and unreachable:
`/ws/factory` answered every message with "the agent runtime is not installed yet",
and the only way to run a task was to call `run_task()` from Python. This is the part
that lets a person actually talk to it.

Three things decide the shape of this.

**The receive loop must keep reading.** The endpoint is the only reader of its
websocket — pipelines wait on futures rather than calling `receive()` themselves, or
they race it — so a task cannot be awaited inline. It runs as a background task and the
endpoint goes straight back to reading, which is also what makes `cancel` able to
arrive at all.

**Events are addressed to a client, not to a socket.** A run is looked up by
`client_id` at send time instead of closing over the websocket it started on. Wanting
Skippy from a phone away from home is one of the reasons this project exists, and
mobile connections drop; resolving the socket per event means a client that reconnects
picks the run back up mid-flight instead of watching a finished task it can no longer
hear.

**A dropped connection does not cancel the work.** Killing a fifteen-minute refactor
because a phone changed towers would be worse than the alternative, and the outcome
lands in project memory either way, so reconnecting and asking what happened works.
Undeliverable events are dropped rather than buffered: they are progress reports, and
a queue of stale ones has no value to a client that has missed the middle.
"""

import asyncio
import logging
from typing import Any, Callable, Dict, Optional

import skippy_agent
import skippy_paths
from skippy_sandbox import Sandbox, SandboxError

logger = logging.getLogger("skippy_tasks")

# The wire protocol's `mode` is the client's selected mode and predates the agent's
# own notion of one, so it is mapped rather than passed through.
_RE_ALIASES = frozenset({"re", "reverse engineering", "reverse-engineering", "reversing"})


def agent_mode_for(wire_mode: Any) -> str:
    return "re" if str(wire_mode or "").strip().lower() in _RE_ALIASES else "coding"


class TaskRunner:
    """One agent task at a time per client, cancellable, events streamed out."""

    def __init__(self, hub, roots_provider: Optional[Callable[[], list]] = None):
        self.hub = hub
        # Injected so a test does not need the environment, and so the Cursor bridge
        # can later offer the editor's open folders instead.
        self.roots_provider = roots_provider or skippy_paths.configured_workspace_roots
        self._tasks: Dict[str, asyncio.Task] = {}
        self._loops: Dict[str, Any] = {}

    def is_running(self, client_id: str) -> bool:
        task = self._tasks.get(client_id)
        return task is not None and not task.done()

    async def send(self, client_id: str, payload: dict) -> bool:
        """Deliver one message to whatever socket that client currently holds.

        Resolved per call rather than captured, so a reconnect during a long run
        continues to see it.
        """
        socket = getattr(self.hub, "active_connections", {}).get(client_id)
        if socket is None:
            return False
        try:
            await socket.send_json(payload)
            return True
        except Exception:
            # The client vanished mid-run. Not fatal: the work continues and the
            # outcome is recorded, so there is something to come back to.
            logger.info("Dropped an event for '%s': the socket is gone.", client_id)
            return False

    async def start(self, client_id: str, request: dict) -> None:
        """Begin a run for this client, or explain why not."""
        if self.is_running(client_id):
            await self.send(client_id, {
                "type": "chat",
                "content": (
                    "I am still working on the previous request. Send 'cancel' to stop "
                    "it, and then ask again."
                ),
            })
            await self.send(client_id, {"type": "done"})
            return

        task_text = str(request.get("text") or "").strip()
        if not task_text:
            await self.send(client_id, {"type": "chat", "content": "There was no request in that message."})
            await self.send(client_id, {"type": "done"})
            return

        roots = self.roots_provider() or []
        try:
            sandbox = Sandbox(roots)
        except SandboxError as exc:
            # Named precisely, because "no workspace roots" is a configuration
            # mistake the user can fix and a silent empty run is not.
            await self.send(client_id, {
                "type": "chat",
                "content": (
                    f"I have no workspace to work in ({exc}). Set SKIPPY_WORKSPACE_ROOTS "
                    "to the repositories I should be able to touch, then restart me."
                ),
            })
            await self.send(client_id, {"type": "done"})
            return

        mode = agent_mode_for(request.get("mode"))
        target = str(request.get("target") or "")
        self._tasks[client_id] = asyncio.create_task(
            self._run(client_id, task_text, sandbox, mode, target)
        )

    async def _run(self, client_id: str, task: str, sandbox: Sandbox, mode: str, target: str) -> None:
        async def emit(event: dict) -> None:
            await self.send(client_id, event)

        loop = skippy_agent.AgentLoop(
            task, sandbox, emit=emit, mode=mode, target=target,
            journal_dir=skippy_paths.patch_journal_root(),
        )
        self._loops[client_id] = loop
        try:
            outcome = await loop.run()
            await self.send(client_id, {"type": "chat", "content": outcome.summary})
        except Exception as exc:
            # A crash in the runtime is the operator's problem, but the person waiting
            # deserves to hear that it stopped rather than watch nothing arrive.
            logger.exception("Agent run for '%s' failed.", client_id)
            await self.send(client_id, {
                "type": "chat",
                "content": f"The run stopped on an internal error: {exc}",
            })
        finally:
            self._loops.pop(client_id, None)
            self._tasks.pop(client_id, None)
            await self.send(client_id, {"type": "done"})

    def cancel(self, client_id: str) -> bool:
        """Ask the run to stop at its next step boundary.

        The loop's own flag rather than `Task.cancel()`: stopping between steps leaves
        a consistent transcript and still records an outcome, where cancelling the
        coroutine could abandon a tool call half-way and lose the account of it.
        """
        loop = self._loops.get(client_id)
        if loop is None or not self.is_running(client_id):
            return False
        loop.cancel()
        return True

    async def shutdown(self) -> None:
        for loop in list(self._loops.values()):
            loop.cancel()
        tasks = [task for task in self._tasks.values() if not task.done()]
        if tasks:
            # Bounded: shutdown cannot wait on a step that is stuck on a model call.
            await asyncio.wait(tasks, timeout=10.0)
