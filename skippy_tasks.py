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
import os
import time
from typing import Any, Callable, Dict, List, Optional

import prompts
import skippy_agent
import skippy_cursor
import skippy_device
import skippy_gate
import skippy_llm
import skippy_paths
from skippy_sandbox import Sandbox, SandboxError

logger = logging.getLogger("skippy_tasks")

# The wire protocol's `mode` is the client's selected mode and predates the agent's
# own notion of one, so it is mapped rather than passed through.
_RE_ALIASES = frozenset({"re", "reverse engineering", "reverse-engineering", "reversing"})
# Chat is its own lane, not an agent mode: no sandbox, no tools, no finish().
# Before this existed, the Mac app's Chat tab ran "hey skippy" through the
# agent loop, which nudged the model to call tools twice and then reported
# "stopped_without_finish" — a conversation cannot be a task.
_CHAT_ALIASES = frozenset({"chat", "talk", "conversation"})
# Answering a question from the web is a run like any other: it takes a slot, streams
# its steps, can be cancelled, and reports when it lands. What it does not take is a
# repository — the sandbox goes with it only because every run has one.
_RESEARCH_ALIASES = frozenset({"research", "web", "look up", "lookup"})


def agent_mode_for(wire_mode: Any) -> str:
    lowered = str(wire_mode or "").strip().lower()
    if lowered in _RE_ALIASES:
        return "re"
    if lowered in _CHAT_ALIASES:
        return "chat"
    if lowered in _RESEARCH_ALIASES:
        return "research"
    return "coding"


class _ChatRun:
    """Cancellation handle for a chat turn, shaped like AgentLoop for cancel()."""

    def __init__(self, task_factory: Callable[[], Optional[asyncio.Task]]):
        self._task_factory = task_factory

    def cancel(self) -> None:
        task = self._task_factory()
        if task is not None:
            task.cancel()


class TaskRunner:
    """One agent task at a time per client, cancellable, events streamed out."""

    def __init__(self, hub, roots_provider: Optional[Callable[[], list]] = None):
        self.hub = hub
        # One bridge for the life of the runner. Whether an editor is actually there is
        # decided per call, so connecting Cursor mid-session just starts working.
        self.cursor = skippy_cursor.CursorBridge(hub)
        # Injected so a test does not need the environment, and so the Cursor bridge
        # can later offer the editor's open folders instead.
        self.roots_provider = roots_provider or skippy_paths.configured_workspace_roots
        self._tasks: Dict[str, asyncio.Task] = {}
        self._loops: Dict[str, Any] = {}
        # What each client is running right now, for the app's task cards. Kept
        # here rather than derived from the asyncio.Task because the task object
        # knows it is alive but not what it is doing.
        self._meta: Dict[str, dict] = {}
        # Per-client research budget and cache, kept across turns. Never cleared on a
        # turn boundary: a budget that reset every message would not be a budget.
        self._conversations: Dict[str, skippy_gate.Conversation] = {}
        # Background checks. Deliberately not in `_tasks`: they must not occupy the
        # client's one run slot, or asking a question with a fact in it would lock the
        # conversation until the internet answered.
        self._side_tasks: set = set()

    def is_running(self, client_id: str) -> bool:
        task = self._tasks.get(client_id)
        return task is not None and not task.done()

    def status(self, client_id: str) -> dict:
        """What this client's run looks like right now.

        Exists for reconnects: events are fire-and-forget, so a client that was
        away has no other way to learn whether the run it started is still going.
        """
        if not self.is_running(client_id):
            return {"running": False}
        meta = self._meta.get(client_id) or {}
        out = {"running": True}
        out.update(meta)
        if "started" in out:
            out["elapsed"] = round(time.time() - out["started"], 1)
        return out

    def _open_memory(self, project_id: str = ""):
        """This client's project memory: the picked project, or the roots' default.

        An explicit pick opens by id alone — passing the current workspace roots
        would append them into that project's meta and quietly re-scope it.
        """
        import skippy_memory

        if project_id:
            return skippy_memory.open_project(project_id=project_id)
        return skippy_memory.open_project(workspace_roots=self.roots_provider() or [])

    def memory_snapshot(self, project_id: str = "") -> dict:
        """Project memory shaped for a UI panel, not for a prompt.

        The opening-context string is written for the model; a client rendering a
        context rail needs the parts separately, so this returns structure. Errors
        come back as data — a hub without its NAS should show an empty rail with a
        reason, not drop the socket.
        """
        try:
            memory = self._open_memory(project_id)
        except Exception as exc:
            return {"error": str(exc)}

        superseded = memory.superseded_ids()
        decisions = []
        for item in memory.decisions():
            front = item["front"]
            decisions.append({
                "id": front.get("id", ""),
                "title": front.get("title", ""),
                "recorded": front.get("recorded", ""),
                "superseded": front.get("id") in superseded,
                "stale_paths": memory.stale_paths(item),
            })
        sessions = [
            {
                "session_id": record.get("session_id", ""),
                "recorded": record.get("recorded", ""),
                "status": record.get("status", ""),
                "task": str(record.get("task", ""))[:200],
                "summary": str(record.get("summary", ""))[:400],
                "files_changed": record.get("files_changed") or [],
                "mode": record.get("mode", ""),
            }
            for record in memory.sessions()
        ]
        return {
            "project_id": memory.project_id,
            "conventions": memory.meta.get("conventions") or {},
            "decisions": decisions,
            "sessions": sessions,
            # Transcript headlines ride along so the rail's chat list refreshes on
            # the same request it already makes; an older client ignores the field.
            "chats": memory.chats(),
        }

    def projects_snapshot(self) -> dict:
        """Every project in the memory store, plus which one the roots default to.

        The pickers offer these ids rather than free text, because an unknown id
        silently creates a fresh project directory.
        """
        import skippy_memory

        roots = self.roots_provider() or []
        try:
            projects = skippy_memory.list_projects()
        except Exception as exc:
            return {"error": str(exc)}
        return {
            "default": skippy_memory.project_id_for(roots),
            "projects": projects,
        }

    def chat_open_snapshot(self, project_id: str, chat_id: str) -> dict:
        """One full transcript, for a client resuming a past chat."""
        try:
            memory = self._open_memory(project_id)
        except Exception as exc:
            return {"error": str(exc)}
        record = memory.load_chat(chat_id)
        if record is None:
            return {"error": f"No chat '{chat_id}' in project '{memory.project_id}'."}
        return {"project_id": memory.project_id, "chat": record}

    def _record_chat_turns(self, project_id: str, chat_id: str, turns: list, mode: str) -> None:
        """File this exchange under its transcript. Failure costs persistence only."""
        if not chat_id:
            return
        try:
            memory = self._open_memory(project_id)
            memory.append_chat(chat_id, turns, mode=mode)
        except Exception:
            logger.warning("Chat %r was not persisted.", chat_id, exc_info=True)

    # -- RE dashboard -----------------------------------------------------

    def re_snapshot(self, pack_id: str = "") -> dict:
        """RE note packs for the dashboard's findings notebook.

        With no pack_id: the list of packs, newest-updated first. With one:
        that pack's findings in full, each flagged if a later finding has
        superseded it. Errors come back as data so a missing NAS shows an empty
        notebook with a reason, not a dropped socket.
        """
        import skippy_paths
        import skippy_re

        root = skippy_paths.notes_root()
        try:
            packs = skippy_re.list_packs(root)
        except Exception as exc:
            return {"error": str(exc)}

        packs.sort(key=lambda p: p.get("updated") or "", reverse=True)
        if not pack_id:
            return {"packs": packs}

        try:
            pack = skippy_re.open_pack(root, pack_id=pack_id)
        except Exception as exc:
            return {"error": str(exc)}

        superseded = pack.superseded_ids()
        findings = []
        for item in pack.read_findings():
            front = item["front"]
            findings.append({
                "id": front.get("id", ""),
                "kind": front.get("kind", ""),
                "title": front.get("title", ""),
                "confidence": front.get("confidence", ""),
                "location": front.get("location", ""),
                "recorded": front.get("recorded", ""),
                "superseded": front.get("id") in superseded,
                "text": item["text"],
            })
        return {
            "pack_id": pack.pack_id,
            "target": pack.meta.get("target", ""),
            "title": pack.meta.get("title", ""),
            "findings": findings,
        }

    def re_add_finding(self, request: dict) -> dict:
        """Record a human-authored finding into a pack.

        The agent's own `note_finding` goes through the loop with its evidence
        and confidence gates; this is the person at the dashboard jotting one
        down, so it writes directly. It still refuses the same empty-evidence
        case, because a finding nobody can recheck is worthless whoever wrote it.
        """
        import skippy_paths
        import skippy_re

        pack_id = str(request.get("pack_id") or "").strip()
        target = str(request.get("target") or "").strip()
        title = str(request.get("title") or "").strip()
        if not pack_id and not target and not title:
            return {"error": "A finding needs a pack_id, a target, or a title to file under."}

        root = skippy_paths.notes_root()
        try:
            pack = skippy_re.open_pack(root, target=target, title=title, pack_id=pack_id)
        except Exception as exc:
            return {"error": str(exc)}

        result = skippy_re.note_finding(
            pack,
            kind=str(request.get("kind") or ""),
            title=title,
            body=str(request.get("body") or ""),
            evidence=str(request.get("evidence") or ""),
            confidence=str(request.get("confidence") or ""),
            location=str(request.get("location") or ""),
            supersedes=str(request.get("supersedes") or ""),
        )
        if not result.ok:
            return {"error": result.summary}
        out = {"ok": True, "pack_id": pack.pack_id, "summary": result.summary}
        out.update(result.data or {})
        return out

    async def re_devices(self, host: str = "studio") -> dict:
        """Enumerate hardware on the studio for the dashboard's device panel.

        The MacBook's own devices are enumerated by that app's device bridge;
        this covers what is plugged into the hub itself.
        """
        import skippy_device

        service = skippy_device.DeviceService(hub=self.hub, client_id="")
        try:
            result = await skippy_device.list_devices(service, host=host)
        except Exception as exc:
            return {"error": str(exc)}
        if not result.ok:
            return {"error": result.summary}
        data = result.data or {}
        return {"host": data.get("host", host), "devices": data.get("devices", [])}

    # -- repo panel ---------------------------------------------------------

    def _git_sandbox(self):
        """A sandbox over the configured roots, or None with a reason."""
        try:
            return Sandbox(self.roots_provider() or []), None
        except SandboxError as exc:
            return None, str(exc)

    def _repo_argument(self, sandbox: Sandbox, name: str):
        """Map the app's display name back to a path skippy_git can resolve."""
        import skippy_git

        if not name:
            return None
        for entry in skippy_git.list_repos(sandbox):
            if entry["name"] == name:
                return entry["path"]
        return name

    async def git_snapshot(self, repo: str = "") -> dict:
        """The repo panel's data: every repo's headline, or one repo in full.

        With no repo: each workspace root that is a git repository, with its
        branch and change count. With one: branch, branches, changed files, the
        working-tree and staged diffs, and the last commit. Errors come back as
        data so a rootless hub shows an empty panel with a reason.
        """
        import skippy_git

        sandbox, error = self._git_sandbox()
        if sandbox is None:
            return {"error": error}

        if not repo:
            repos = []
            for entry in skippy_git.list_repos(sandbox):
                status = await skippy_git.git_status(sandbox, entry["path"])
                data = status.data if status.ok else {}
                repos.append({
                    "name": entry["name"],
                    "branch": data.get("branch", ""),
                    "changes": len(data.get("changes", [])),
                    "ahead": data.get("ahead", 0),
                    "behind": data.get("behind", 0),
                    "last_commit": data.get("last_commit", {}),
                })
            return {"repos": repos}

        target = self._repo_argument(sandbox, repo)
        status = await skippy_git.git_status(sandbox, target)
        if not status.ok:
            return {"error": status.summary}
        branches = await skippy_git.git_branch(sandbox, target)
        working = await skippy_git.git_diff(sandbox, target)
        staged = await skippy_git.git_diff(sandbox, target, staged=True)
        data = status.data
        return {
            "repo": repo,
            "branch": data.get("branch", ""),
            "ahead": data.get("ahead", 0),
            "behind": data.get("behind", 0),
            "changes": data.get("changes", []),
            "branches": branches.data.get("branches", []) if branches.ok else [],
            "last_commit": data.get("last_commit", {}),
            "diff": working.content,
            "staged_diff": staged.content,
            "untracked": working.data.get("untracked", []),
        }

    async def git_commit_action(self, request: dict) -> dict:
        """Commit from the repo panel. No approval card: the human wrote the
        message and clicked Commit, which *is* the approval — the card exists
        for agent-initiated commits, where the human is not the author."""
        import skippy_git

        sandbox, error = self._git_sandbox()
        if sandbox is None:
            return {"error": error}
        paths = request.get("paths")
        result = await skippy_git.git_commit(
            sandbox,
            str(request.get("message") or ""),
            repo=self._repo_argument(sandbox, str(request.get("repo") or "")),
            paths=paths if isinstance(paths, list) and paths else None,
            approver=None,
        )
        if not result.ok:
            return {"error": result.summary}
        out = {"ok": True, "summary": result.summary}
        out.update(result.data or {})
        return out

    async def git_branch_action(self, request: dict) -> dict:
        """Create or switch a branch from the repo panel."""
        import skippy_git

        sandbox, error = self._git_sandbox()
        if sandbox is None:
            return {"error": error}
        result = await skippy_git.git_branch(
            sandbox,
            repo=self._repo_argument(sandbox, str(request.get("repo") or "")),
            name=str(request.get("name") or "") or None,
            create=bool(request.get("create")),
        )
        if not result.ok:
            return {"error": result.summary}
        out = {"ok": True, "summary": result.summary}
        out.update(result.data or {})
        return out

    async def git_push_action(self, request: dict) -> dict:
        """Push from the repo panel; the click is the approval, as with commit."""
        import skippy_git

        sandbox, error = self._git_sandbox()
        if sandbox is None:
            return {"error": error}
        result = await skippy_git.git_push(
            sandbox, repo=self._repo_argument(sandbox, str(request.get("repo") or "")),
        )
        if not result.ok:
            return {"error": result.summary}
        out = {"ok": True, "summary": result.summary}
        out.update(result.data or {})
        return out

    async def git_pull_action(self, request: dict) -> dict:
        """Pull (ff-only) from the repo panel."""
        import skippy_git

        sandbox, error = self._git_sandbox()
        if sandbox is None:
            return {"error": error}
        result = await skippy_git.git_pull(
            sandbox, repo=self._repo_argument(sandbox, str(request.get("repo") or "")),
        )
        if not result.ok:
            return {"error": result.summary}
        out = {"ok": True, "summary": result.summary}
        out.update(result.data or {})
        return out

    async def git_new_action(self, request: dict) -> dict:
        """Create a repo under the workspace root, with a GitHub twin when possible."""
        import skippy_git

        sandbox, error = self._git_sandbox()
        if sandbox is None:
            return {"error": error}
        result = await skippy_git.git_new(
            sandbox,
            name=str(request.get("name") or ""),
            private=bool(request.get("private", True)),
        )
        if not result.ok:
            return {"error": result.summary}
        out = {"ok": True, "summary": result.summary}
        out.update(result.data or {})
        return out

    async def git_clone_action(self, request: dict) -> dict:
        """Clone one of the account's GitHub repos into the workspace root."""
        import skippy_git

        sandbox, error = self._git_sandbox()
        if sandbox is None:
            return {"error": error}
        result = await skippy_git.git_clone(sandbox, str(request.get("full_name") or ""))
        if not result.ok:
            return {"error": result.summary}
        out = {"ok": True, "summary": result.summary}
        out.update(result.data or {})
        return out

    async def workspace_new_action(self, request: dict) -> dict:
        """Create a new workspace root: a folder under `workspaces_root()`, a git
        repo, and an entry in the hub-managed roots file.

        The roots provider re-reads that file per call, so the new workspace is
        usable — sandbox, repo panel, project picker — without a hub restart.
        """
        import subprocess

        import skippy_memory

        name = str(request.get("name") or "").strip()
        if not name or name != os.path.basename(name) or name.startswith(".") or name in ("..",):
            return {"error": "A workspace needs a plain folder name."}

        base = skippy_paths.workspaces_root()
        path = os.path.join(base, name)
        if os.path.exists(path):
            return {"error": f"'{name}' already exists under {base}."}

        def create() -> dict:
            os.makedirs(path)
            result = subprocess.run(
                ["git", "init", "-q", path], capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return {"error": f"git init failed: {result.stderr.strip() or 'unknown error'}"}
            skippy_paths.add_workspace_root(path)
            # Open the project now so it appears in the picker immediately rather
            # than after its first run.
            project = skippy_memory.open_project(workspace_roots=[path])
            return {"ok": True, "name": name, "path": path, "project_id": project.project_id}

        try:
            return await asyncio.to_thread(create)
        except Exception as exc:
            return {"error": f"Could not create the workspace: {exc}"}

    async def github_action(self, request: dict) -> dict:
        """The GitHub connection itself: status, set_token, repos.

        The token arrives here once, is stored server-side, and is never sent
        back — status answers with the login it maps to, which is all the app
        needs to show "connected as ...".
        """
        import skippy_github

        op = str(request.get("op") or "status")

        if op == "set_token":
            token = str(request.get("token") or "")
            if not token.strip():
                skippy_github.set_token("")
                return {"op": op, "connected": False}
            try:
                who = await skippy_github.whoami(token.strip())
            except skippy_github.GitHubError as exc:
                return {"op": op, "error": str(exc)}
            # Only a token that answered /user gets stored.
            skippy_github.set_token(token)
            return {"op": op, "connected": True, "login": who["login"], "name": who["name"]}

        if op == "status":
            if not skippy_github.get_token():
                return {"op": op, "connected": False}
            try:
                who = await skippy_github.whoami()
            except skippy_github.GitHubError as exc:
                return {"op": op, "connected": False, "error": str(exc)}
            return {"op": op, "connected": True, "login": who["login"], "name": who["name"]}

        if op == "repos":
            try:
                repos = await skippy_github.list_repos()
            except skippy_github.GitHubError as exc:
                return {"op": op, "error": str(exc)}
            return {"op": op, "repos": repos}

        return {"op": op, "error": f"Unknown github op '{op}'."}

    # -- file explorer (read-only, repo-scoped) ------------------------------

    _FILE_READ_CAP = 200_000  # chars; the viewer is for reading, not archaeology

    def _explorer_target(self, request: dict):
        """Resolve {repo, path} to (repo_root, absolute_path) or (None, error)."""
        import skippy_git

        sandbox, error = self._git_sandbox()
        if sandbox is None:
            return None, None, error
        repo = str(request.get("repo") or "")
        repo_path = self._repo_argument(sandbox, repo)
        if not repo_path:
            return None, None, "A repo is required."
        try:
            root = sandbox.resolve(repo_path, must_exist=True)
        except SandboxError as exc:
            return None, None, f"Sandbox violation: {exc}"
        root = sandbox.repo_root_for(root) or root

        rel = str(request.get("path") or "").strip().lstrip("/")
        try:
            target = sandbox.resolve(os.path.join(root, rel) if rel else root, must_exist=True)
        except SandboxError as exc:
            return None, None, f"Sandbox violation: {exc}"
        if not (target == root or target.startswith(root + os.sep)):
            return None, None, f"'{rel}' is not inside the repo."
        return root, target, None

    async def files_action(self, request: dict) -> dict:
        """One directory level of a repo, for the app's lazy folder tree."""
        root, target, error = self._explorer_target(request)
        if error:
            return {"error": error}
        if not os.path.isdir(target):
            return {"error": "Not a directory."}

        def scan() -> list:
            entries = []
            for name in sorted(os.listdir(target)):
                if name == ".git":
                    continue
                full = os.path.join(target, name)
                is_dir = os.path.isdir(full)
                size = 0
                if not is_dir:
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        size = 0
                entries.append({"name": name, "dir": is_dir, "size": size})
            entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
            return entries

        entries = await asyncio.to_thread(scan)
        return {
            "repo": str(request.get("repo") or ""),
            "path": str(request.get("path") or ""),
            "entries": entries,
        }

    async def file_action(self, request: dict) -> dict:
        """One file's text, size-capped; binary is refused rather than mangled."""
        root, target, error = self._explorer_target(request)
        if error:
            return {"error": error}
        if os.path.isdir(target):
            return {"error": "That is a directory, not a file."}

        def read() -> dict:
            try:
                with open(target, "rb") as fh:
                    head = fh.read(self._FILE_READ_CAP + 1)
            except OSError as exc:
                return {"error": f"Could not read the file: {exc}"}
            if b"\x00" in head[:8192]:
                return {"error": "This looks like a binary file; the viewer only shows text."}
            truncated = len(head) > self._FILE_READ_CAP
            text = head[: self._FILE_READ_CAP].decode("utf-8", errors="replace")
            return {"text": text, "truncated": truncated}

        result = await asyncio.to_thread(read)
        if "error" in result:
            return result
        return {
            "repo": str(request.get("repo") or ""),
            "path": str(request.get("path") or ""),
            "text": result["text"],
            "truncated": result["truncated"],
        }

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

        mode = agent_mode_for(request.get("mode"))
        history = request.get("history")
        if not isinstance(history, list):
            history = None
        # Which project's memory this run opens with, and which transcript it
        # appends to. Both optional: an older client sends neither and gets the
        # roots-derived project and no persistence, exactly as before.
        project = str(request.get("project") or "").strip()
        chat_id = str(request.get("chat_id") or "").strip()

        self._meta[client_id] = {
            "task": task_text[:300],
            "mode": mode,
            "started": time.time(),
        }
        if project:
            self._meta[client_id]["project"] = project

        if mode == "chat":
            # No sandbox and no workspace requirement: a conversation must work
            # even when the roots are misconfigured, because talking to Skippy
            # is how you would find that out.
            self._tasks[client_id] = asyncio.create_task(
                self._run_chat(client_id, task_text, history, project, chat_id)
            )
            self._loops[client_id] = _ChatRun(lambda: self._tasks.get(client_id))
            return

        roots = self.roots_provider() or []
        try:
            sandbox = Sandbox(roots)
        except SandboxError as exc:
            self._meta.pop(client_id, None)
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

        target = str(request.get("target") or "")
        # A client sends its prior turns so a follow-up ("now do the same for the
        # other file") continues the thread instead of starting cold. AgentLoop
        # validates the contents; here we only require it to be a list.
        self._tasks[client_id] = asyncio.create_task(
            self._run(client_id, task_text, sandbox, mode, target, history, project, chat_id)
        )

    async def _run(
        self, client_id: str, task: str, sandbox: Sandbox, mode: str, target: str,
        history: Optional[list] = None, project: str = "", chat_id: str = "",
    ) -> None:
        async def emit(event: dict) -> None:
            await self.send(client_id, event)

        # Filled when there is an exchange worth persisting; written just before
        # `done` so a client's refresh-on-done sees it, without delaying the reply.
        transcript_turns: Optional[list] = None

        devices = None
        if mode == "re":
            # Bound to this client so write approvals land on the person who
            # started the run, and so remote host=macbook RPCs know which hub
            # to talk through.
            devices = skippy_device.DeviceService(hub=self.hub, client_id=client_id)
        # Code edits are approved in the app on the same socket that started the
        # run. None when SKIPPY_CODE_APPROVAL=off, which restores silent writes.
        approver = skippy_cursor.build_code_approver(self.hub, client_id)
        # A picked project scopes which memory the run opens with and records to.
        # None lets the loop derive one from the sandbox roots, exactly as before.
        memory = None
        if project:
            try:
                memory = self._open_memory(project)
            except Exception:
                logger.warning("Project %r unavailable; the loop will use its default.",
                               project, exc_info=True)
        loop = skippy_agent.AgentLoop(
            task, sandbox, emit=emit, mode=mode, target=target,
            journal_dir=skippy_paths.patch_journal_root(), cursor=self.cursor,
            history=history, devices=devices, approver=approver, memory=memory,
        )
        self._loops[client_id] = loop
        try:
            outcome = await loop.run()
            # A research run's product is the answer, not the model's account of having
            # looked things up. Reporting the summary there would hand back "I searched
            # and read three pages" and leave the answer on disk.
            reply = outcome.answer or outcome.summary
            await self.send(client_id, {"type": "chat", "content": reply})
            # The exchange lands in the transcript so the conversation around a run
            # is resumable like any chat; the step-by-step record is the session's.
            if reply:
                transcript_turns = [
                    {"role": "user", "content": task},
                    {"role": "assistant", "content": reply},
                ]
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
            self._meta.pop(client_id, None)
            # Persist before `done` so the client's refresh-on-done sees the
            # new transcript. A failed write still costs persistence only.
            if transcript_turns and chat_id:
                await asyncio.to_thread(
                    self._record_chat_turns, project, chat_id, transcript_turns, mode,
                )
            await self.send(client_id, {"type": "done"})

    def _chat_messages(self, text: str, history: Optional[list], project: str = "") -> List[dict]:
        """System persona, project memory when reachable, prior turns, the message."""
        system = prompts.CHAT_SYSTEM
        try:
            memory = self._open_memory(project)
            context = memory.opening_context()
            if context:
                system += f"\n\n{context}"
        except Exception:
            # Continuity is a nicety here; the conversation is the point.
            logger.warning("Chat is running without project memory.", exc_info=True)

        messages: List[dict] = [{"role": "system", "content": system}]
        messages.extend(skippy_agent._clean_history(history))
        messages.append({"role": "user", "content": text})
        return messages

    def conversation(self, client_id: str) -> skippy_gate.Conversation:
        """This client's research budget and cache, kept across turns.

        Per client rather than per turn: the budget only means anything over a
        conversation, and the point of the cache is that the second time a subject comes
        up it costs nothing.
        """
        existing = self._conversations.get(client_id)
        if existing is None:
            existing = skippy_gate.Conversation()
            self._conversations[client_id] = existing
        return existing

    async def _run_chat(
        self, client_id: str, text: str, history: Optional[list],
        project: str = "", chat_id: str = "",
    ) -> None:
        """One conversational turn: no tools, no sandbox, one reply.

        Plus, since the research capability exists, a check on the way in and a check on
        the way out — neither of which the reply waits for. See skippy_gate: the turn is
        answered now, and anything worth verifying arrives afterwards as its own message.
        """
        # Written just before `done` — see _run for why.
        transcript_turns: Optional[list] = None
        try:
            messages = self._chat_messages(text, history, project)
            decision = await skippy_gate.pre_answer(text, history or [])
            if decision and self._start_check(client_id, decision):
                # A note rather than a canned line, so the acknowledgment sounds like
                # Skippy instead of like a progress bar.
                messages.append({"role": "system", "content": skippy_gate.acknowledgment(decision)})

            reply = await skippy_llm.query_text(
                messages,
                role="fast",
                temp=0.6,
                # Prose only in this lane, so the penalty skippy_llm forbids for
                # code is safe — and needed, for the same degenerate-repetition
                # reason the voice lane sets it.
                repetition_penalty=1.05,
            )
            await self.send(client_id, {
                "type": "chat",
                "content": reply or "I have nothing to say to that, which is a first.",
            })
            if reply:
                transcript_turns = [
                    {"role": "user", "content": text},
                    {"role": "assistant", "content": reply},
                ]
            # Behind the delivered reply, so its cost is not latency anyone waits
            # through. Skipped when the pre-answer gate already started a check: asking
            # the model to grade an answer it just hedged on purpose would start a
            # second run on the same question.
            if not decision and reply:
                self._side(client_id, self._check_after(client_id, text, reply))
        except asyncio.CancelledError:
            await self.send(client_id, {"type": "chat", "content": "Stopped."})
        except Exception as exc:
            logger.exception("Chat turn for '%s' failed.", client_id)
            await self.send(client_id, {
                "type": "chat",
                "content": f"I could not answer that: {exc}",
            })
        finally:
            self._loops.pop(client_id, None)
            self._tasks.pop(client_id, None)
            self._meta.pop(client_id, None)
            # Persist before `done` so the client's refresh-on-done sees the
            # new transcript. A failed write still costs persistence only.
            if transcript_turns and chat_id:
                await asyncio.to_thread(
                    self._record_chat_turns, project, chat_id, transcript_turns, "chat",
                )
            await self.send(client_id, {"type": "done"})

    # -- autonomous checking ----------------------------------------------
    #
    # A check is not a run: it does not take the client's slot, it cannot be started by
    # asking for it, and the conversation never waits for it. It goes out afterwards as
    # a `chat` event carrying `kind: "research"`, which is a field a client may style or
    # ignore — arriving after `done` is the point, because the alternative is a
    # conversation that stops dead while Skippy reads the internet.

    def _side(self, client_id: str, coro) -> None:
        """Run something beside the conversation, without taking the client's slot."""
        task = asyncio.create_task(coro)
        self._side_tasks.add(task)
        task.add_done_callback(self._side_tasks.discard)

    def _start_check(self, client_id: str, decision: skippy_gate.Decision) -> bool:
        """Begin a background check. False when this conversation cannot afford one.

        Reported back so the caller knows whether to tell the persona it is checking:
        promising to verify something and then not doing it is worse than not offering.
        """
        conversation = self.conversation(client_id)
        if conversation.key(decision.question) in conversation.in_flight:
            return False
        if conversation.recall(decision.question) is None and not conversation.allows():
            logger.info("Research budget spent for '%s'; answering unchecked.", client_id)
            return False
        logger.info(
            "Checking %r for '%s' (%s: %s)",
            decision.question, client_id, decision.layer, decision.reason,
        )
        self._side(client_id, self._deliver_check(client_id, decision))
        return True

    async def _deliver_check(self, client_id: str, decision: skippy_gate.Decision) -> None:
        result = await skippy_gate.check(
            decision.question,
            self.conversation(client_id),
            roots=self.roots_provider() or [],
        )
        if not result.answer and not result.error:
            return
        await self.send(client_id, {
            "type": "chat",
            # The field a client keys off to render this as a late follow-up rather
            # than as a reply to whatever the user most recently said.
            "kind": "research",
            "question": result.question,
            "brief": result.brief_id,
            "sources": result.sources,
            "cached": result.cached,
            "content": skippy_gate.report(result),
        })

    async def _check_after(self, client_id: str, text: str, reply: str) -> None:
        """The second layer: ask the model that answered whether it should be checked."""
        decision = await skippy_gate.post_answer(text, reply)
        if decision:
            self._start_check(client_id, decision)

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

    def steer(self, client_id: str, text: str) -> bool:
        """Pass a message to this client's running agent. False when there is nothing
        to say it to, or nothing to say.

        A chat turn cannot be steered — it is one completion and it is already streaming
        — so this is agent runs only, which is also where it matters.
        """
        loop = self._loops.get(client_id)
        if loop is None or not self.is_running(client_id):
            return False
        steer = getattr(loop, "steer", None)
        if steer is None:
            return False
        return bool(steer(text))

    async def shutdown(self) -> None:
        for loop in list(self._loops.values()):
            loop.cancel()
        # Background checks are cancelled outright rather than waited for. Nobody is
        # blocked on one, its sources are already written to the brief as they are read,
        # and a shutdown that waited for the internet would not be a shutdown.
        for task in list(self._side_tasks):
            task.cancel()
        tasks = [task for task in self._tasks.values() if not task.done()]
        if tasks:
            # Bounded: shutdown cannot wait on a step that is stuck on a model call.
            await asyncio.wait(tasks, timeout=10.0)
