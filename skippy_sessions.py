"""Per-project session and memory store on the NAS.

Layout under `$SKIPPY_MEMORY_ROOT`:

    sessions/projects/<project_id>/meta.json
    sessions/projects/<project_id>/sessions/<session_id>.json
    sessions/projects/<project_id>/decisions/<decision_id>.md
    sessions/projects/<project_id>/patches/<session_id>/<step>/

Scoping matters: one global vector collection turns every project's history into
noise for every other project. Chroma collections are per project
(`proj_<slug>_code`, `proj_<slug>_notes`), and decisions are plain markdown with
YAML front matter so they stay readable straight off the share.

Chroma is optional. When it is unavailable the store falls back to deterministic
keyword scoring over decisions and session history, which keeps the agent working
(and testable) without an embedding backend.
"""

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import skippy_llm
import skippy_paths
from skippy_agent_tools import ToolResult

logger = logging.getLogger("skippy_sessions")

MAX_TURN_ARG_CHARS = 2_000
MAX_RESULT_SUMMARY_CHARS = 1_000
MEMORY_COMPRESS_THRESHOLD = 6_000
SCHEMA_VERSION = 1


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")
    return slug or "project"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


def _read_json(path: str, default):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return default


def _write_json(path: str, payload) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(temp, path)


def _truncate(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"... [+{len(value) - limit} chars]"
    if isinstance(value, dict):
        return {key: _truncate(item, limit) for key, item in value.items()}
    if isinstance(value, list):
        return [_truncate(item, limit) for item in value]
    return value


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

@dataclass
class Session:
    store: "SessionStore"
    project_id: str
    session_id: str
    task: str
    mode: str
    workspace_roots: List[str]
    started_at: str = field(default_factory=_now)
    turns: List[dict] = field(default_factory=list)
    files_touched: List[str] = field(default_factory=list)
    decisions: List[str] = field(default_factory=list)
    status: str = "running"
    summary: str = ""
    ended_at: Optional[str] = None

    @property
    def path(self) -> str:
        return os.path.join(self.store.sessions_dir(self.project_id), f"{self.session_id}.json")

    def backup_dir(self, step: int) -> str:
        """Where apply_patch stashes pre-images for this step. Not created eagerly."""
        return os.path.join(
            self.store.patches_dir(self.project_id), self.session_id, str(step)
        )

    def to_dict(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "session_id": self.session_id,
            "project_id": self.project_id,
            "task": self.task,
            "mode": self.mode,
            "status": self.status,
            "summary": self.summary,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "workspace_roots": self.workspace_roots,
            "model": {
                role: skippy_llm.MODELS[role].model
                for role in ("fast", "heavy", "compressor")
                if role in skippy_llm.MODELS
            },
            "files_touched": self.files_touched,
            "decisions": self.decisions,
            "turns": self.turns,
        }

    def save(self) -> None:
        _write_json(self.path, self.to_dict())

    def record_turn(
        self,
        step: int,
        tool: str,
        args: Optional[dict] = None,
        ok: bool = True,
        result_summary: str = "",
        thought: str = "",
    ) -> None:
        self.turns.append(
            {
                "step": step,
                "tool": tool,
                "args": _truncate(args or {}, MAX_TURN_ARG_CHARS),
                "ok": ok,
                "result_summary": _truncate(result_summary, MAX_RESULT_SUMMARY_CHARS),
                "thought": _truncate(thought, MAX_RESULT_SUMMARY_CHARS),
                "ts": _now(),
            }
        )
        self.save()

    def finish(
        self, status: str, summary: str = "", files_changed: Optional[Sequence[str]] = None
    ) -> None:
        self.status = status
        self.summary = summary
        self.ended_at = _now()
        for path in files_changed or []:
            if path not in self.files_touched:
                self.files_touched.append(path)
        self.save()
        self.store.on_session_finished(self)


# ---------------------------------------------------------------------------
# Project memory
# ---------------------------------------------------------------------------

class ProjectMemory:
    """Search and write side of a single project's memory."""

    def __init__(self, store: "SessionStore", project_id: str):
        self.store = store
        self.project_id = project_id

    # -- writes -----------------------------------------------------------

    async def save_decision(
        self, title: str, body: str, tags: Optional[Sequence[str]] = None, session_id: str = ""
    ) -> ToolResult:
        decision_id = self.store.next_decision_id(self.project_id)
        tag_list = [str(tag) for tag in (tags or [])]
        front_matter = "\n".join(
            [
                "---",
                f"id: {decision_id}",
                f"title: {json.dumps(title)}",
                f"tags: [{', '.join(json.dumps(tag) for tag in tag_list)}]",
                f"session_id: {json.dumps(session_id)}",
                f"project_id: {json.dumps(self.project_id)}",
                f"ts: {_now()}",
                "---",
                "",
            ]
        )
        path = os.path.join(self.store.decisions_dir(self.project_id), f"{decision_id}.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"{front_matter}# {title}\n\n{body}\n")

        self.store.index_note(
            self.project_id,
            document=f"DECISION {title}\n{body}\ntags: {', '.join(tag_list)}",
            metadata={"kind": "decision", "id": decision_id, "title": title},
            doc_id=f"{self.project_id}:{decision_id}",
        )
        return ToolResult(
            True, f"Recorded decision {decision_id}: {title}", "", {"decision_id": decision_id}
        )

    def record_session_note(self, session: Session) -> None:
        tools_used = sorted({turn["tool"] for turn in session.turns})
        document = (
            f"SESSION {session.session_id} ({session.status})\n"
            f"task: {session.task}\n"
            f"outcome: {session.summary}\n"
            f"files: {', '.join(session.files_touched) or 'none'}\n"
            f"tools: {', '.join(tools_used) or 'none'}"
        )
        self.store.index_note(
            self.project_id,
            document=document,
            metadata={"kind": "session", "id": session.session_id, "status": session.status},
            doc_id=f"{self.project_id}:{session.session_id}",
        )

    # -- reads ------------------------------------------------------------

    def decisions(self) -> List[dict]:
        directory = self.store.decisions_dir(self.project_id)
        if not os.path.isdir(directory):
            return []
        entries = []
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".md"):
                continue
            path = os.path.join(directory, name)
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    text = handle.read()
            except OSError:
                continue
            entries.append(
                {"id": name[:-3], "title": _front_matter_value(text, "title"), "text": text}
            )
        return entries

    def recent_sessions(self, limit: int = 10) -> List[dict]:
        directory = self.store.sessions_dir(self.project_id)
        if not os.path.isdir(directory):
            return []
        paths = [
            os.path.join(directory, name)
            for name in os.listdir(directory)
            if name.endswith(".json")
        ]
        paths.sort(key=lambda path: os.path.getmtime(path), reverse=True)
        return [_read_json(path, {}) for path in paths[:limit]]

    async def search(self, query: str, k: int = 8) -> ToolResult:
        notes = self.store.query_notes(self.project_id, query, k)
        if not notes:
            notes = self._keyword_notes(query, k)
        code = self.store.query_code(self.project_id, query, max(2, k // 2))

        blocks = []
        if notes:
            blocks.append("PRIOR DECISIONS AND SESSIONS:\n" + "\n\n".join(notes))
        if code:
            blocks.append("INDEXED CODE:\n" + "\n\n".join(code))
        if not blocks:
            return ToolResult(
                True,
                f"No project memory yet for '{self.project_id}'.",
                "",
                {"hits": 0},
            )

        content = "\n\n".join(blocks)
        if len(content) > MEMORY_COMPRESS_THRESHOLD:
            try:
                content = await skippy_llm.compress(
                    content, instruction=f"An agent needs to recall: {query}"
                )
            except Exception:
                logger.warning("Memory compression failed; returning raw hits.")
        return ToolResult(
            True,
            f"{len(notes) + len(code)} memory hit(s) for '{query}'.",
            content,
            {"hits": len(notes) + len(code)},
        )

    def _keyword_notes(self, query: str, k: int) -> List[str]:
        """Deterministic fallback when no vector backend is available."""
        terms = {term for term in re.findall(r"[a-z0-9_]{3,}", (query or "").lower())}
        if not terms:
            return []

        scored = []
        for decision in self.decisions():
            haystack = decision["text"].lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, decision["text"].strip()))
        for session in self.recent_sessions(limit=40):
            haystack = json.dumps(session).lower()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append(
                    (
                        score,
                        f"SESSION {session.get('session_id')} ({session.get('status')})\n"
                        f"task: {session.get('task')}\n"
                        f"outcome: {session.get('summary')}\n"
                        f"files: {', '.join(session.get('files_touched') or []) or 'none'}",
                    )
                )
        scored.sort(key=lambda item: item[0], reverse=True)
        return [text for _, text in scored[:k]]


def _front_matter_value(text: str, key: str) -> str:
    match = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
    if not match:
        return ""
    raw = match.group(1).strip()
    try:
        return json.loads(raw)
    except ValueError:
        return raw


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

class SessionStore:
    def __init__(self, root: Optional[str] = None, chroma_client_factory=None):
        self._root = root
        self._chroma_factory = chroma_client_factory
        self._chroma: Any = None
        self._chroma_tried = False
        self._collections: Dict[str, Any] = {}
        self._memories: Dict[str, ProjectMemory] = {}

    # -- paths ------------------------------------------------------------

    @property
    def root(self) -> str:
        return self._root or os.path.join(skippy_paths.sessions_root(ensure=False), "projects")

    def project_dir(self, project_id: str, ensure: bool = False) -> str:
        path = os.path.join(self.root, slugify(project_id))
        if ensure:
            os.makedirs(path, exist_ok=True)
        return path

    def sessions_dir(self, project_id: str) -> str:
        return os.path.join(self.project_dir(project_id), "sessions")

    def decisions_dir(self, project_id: str) -> str:
        return os.path.join(self.project_dir(project_id), "decisions")

    def patches_dir(self, project_id: str) -> str:
        return os.path.join(self.project_dir(project_id), "patches")

    def meta_path(self, project_id: str) -> str:
        return os.path.join(self.project_dir(project_id), "meta.json")

    # -- projects ---------------------------------------------------------

    def project_meta(self, project_id: str) -> Optional[dict]:
        return _read_json(self.meta_path(project_id), None)

    def list_projects(self) -> List[str]:
        if not os.path.isdir(self.root):
            return []
        return sorted(
            name
            for name in os.listdir(self.root)
            if os.path.isfile(os.path.join(self.root, name, "meta.json"))
        )

    def ensure_project(
        self,
        project_id: str,
        name: Optional[str] = None,
        workspace_roots: Optional[Sequence[str]] = None,
        conventions: Optional[dict] = None,
    ) -> dict:
        self.project_dir(project_id, ensure=True)
        meta = self.project_meta(project_id) or {
            "schema_version": SCHEMA_VERSION,
            "project_id": project_id,
            "name": name or project_id,
            "created_at": _now(),
            "workspace_roots": [],
            "repos": [],
            "conventions": {},
            "chroma_collections": {
                "code": f"proj_{slugify(project_id)}_code",
                "notes": f"proj_{slugify(project_id)}_notes",
            },
            "stats": {"sessions": 0, "files_touched": 0},
        }

        for root in workspace_roots or []:
            if root not in meta["workspace_roots"]:
                meta["workspace_roots"].append(root)
        if conventions:
            meta["conventions"].update(conventions)
        meta["repos"] = _discover_repos(meta["workspace_roots"])
        meta["updated_at"] = _now()
        _write_json(self.meta_path(project_id), meta)
        return meta

    # -- sessions ---------------------------------------------------------

    def start_session(
        self,
        project_id: str,
        session_id: str,
        task: str,
        mode: str,
        workspace_roots: Sequence[str],
    ) -> Session:
        self.ensure_project(project_id, workspace_roots=list(workspace_roots))
        os.makedirs(self.sessions_dir(project_id), exist_ok=True)

        existing = _read_json(
            os.path.join(self.sessions_dir(project_id), f"{session_id}.json"), None
        )
        session = Session(
            store=self,
            project_id=project_id,
            session_id=session_id,
            task=task,
            mode=mode,
            workspace_roots=list(workspace_roots),
        )
        if existing:
            # Resuming an existing session id: keep its history and file list.
            session.turns = existing.get("turns", [])
            session.files_touched = existing.get("files_touched", [])
            session.decisions = existing.get("decisions", [])
            session.started_at = existing.get("started_at", session.started_at)
        session.save()
        return session

    def load_session(self, project_id: str, session_id: str) -> Optional[dict]:
        return _read_json(
            os.path.join(self.sessions_dir(project_id), f"{session_id}.json"), None
        )

    def on_session_finished(self, session: Session) -> None:
        meta = self.project_meta(session.project_id) or self.ensure_project(session.project_id)
        stats = meta.setdefault("stats", {"sessions": 0, "files_touched": 0})
        stats["sessions"] = len(
            [
                name
                for name in os.listdir(self.sessions_dir(session.project_id))
                if name.endswith(".json")
            ]
        )
        touched = set(meta.get("all_files_touched") or [])
        touched.update(session.files_touched)
        meta["all_files_touched"] = sorted(touched)
        stats["files_touched"] = len(touched)
        meta["last_session"] = {
            "session_id": session.session_id,
            "status": session.status,
            "summary": session.summary,
            "ended_at": session.ended_at,
        }
        meta["updated_at"] = _now()
        _write_json(self.meta_path(session.project_id), meta)
        try:
            self.memory_for(session.project_id).record_session_note(session)
        except Exception:
            logger.exception("Failed to index session note for %s", session.session_id)

    def next_decision_id(self, project_id: str) -> str:
        directory = self.decisions_dir(project_id)
        highest = 0
        if os.path.isdir(directory):
            for name in os.listdir(directory):
                match = re.match(r"dec-(\d+)\.md$", name)
                if match:
                    highest = max(highest, int(match.group(1)))
        return f"dec-{highest + 1:04d}"

    def memory_for(self, project_id: str) -> ProjectMemory:
        if project_id not in self._memories:
            self._memories[project_id] = ProjectMemory(self, project_id)
        return self._memories[project_id]

    # -- chroma -----------------------------------------------------------

    def _client(self):
        if self._chroma_tried:
            return self._chroma
        self._chroma_tried = True
        try:
            if self._chroma_factory is not None:
                self._chroma = self._chroma_factory()
            else:
                import chromadb

                self._chroma = chromadb.PersistentClient(path=skippy_paths.chroma_path())
        except Exception as exc:
            logger.warning("Vector memory unavailable (%s); using keyword fallback.", exc)
            self._chroma = None
        return self._chroma

    def collection(self, project_id: str, kind: str):
        client = self._client()
        if client is None:
            return None
        name = f"proj_{slugify(project_id)}_{kind}"
        if name not in self._collections:
            try:
                self._collections[name] = client.get_or_create_collection(name=name)
            except Exception as exc:
                logger.warning("Could not open collection %s: %s", name, exc)
                return None
        return self._collections[name]

    def index_note(self, project_id: str, document: str, metadata: dict, doc_id: str) -> None:
        collection = self.collection(project_id, "notes")
        if collection is None:
            return
        try:
            collection.upsert(documents=[document], metadatas=[metadata], ids=[doc_id])
        except Exception:
            try:
                collection.add(documents=[document], metadatas=[metadata], ids=[doc_id])
            except Exception:
                logger.exception("Failed to index note %s", doc_id)

    def _query(self, project_id: str, kind: str, query: str, k: int) -> List[str]:
        collection = self.collection(project_id, kind)
        if collection is None or not query:
            return []
        try:
            results = collection.query(query_texts=[query], n_results=max(1, k))
        except Exception:
            logger.exception("Query against %s memory failed", kind)
            return []
        documents = (results.get("documents") or [[]])[0]
        metadatas = (results.get("metadatas") or [[]])[0] or [{}] * len(documents)
        rendered = []
        for document, metadata in zip(documents, metadatas):
            source = (metadata or {}).get("source") or (metadata or {}).get("id") or ""
            rendered.append(f"--- {source} ---\n{document}" if source else document)
        return rendered

    def query_notes(self, project_id: str, query: str, k: int) -> List[str]:
        return self._query(project_id, "notes", query, k)

    def query_code(self, project_id: str, query: str, k: int) -> List[str]:
        return self._query(project_id, "code", query, k)

    async def index_workspace(self, project_id: str, path: str) -> ToolResult:
        """Ingest a workspace into this project's code collection."""
        collection = self.collection(project_id, "code")
        if collection is None:
            return ToolResult(False, "Vector memory unavailable; cannot index the workspace.")
        import tools

        message = await tools.ingest_codebase_to_rag(path, collection, project_id=project_id)
        return ToolResult(True, message)


def _discover_repos(roots: Sequence[str]) -> List[dict]:
    repos = []
    for root in roots:
        if not os.path.isdir(os.path.join(root, ".git")):
            continue
        repos.append(
            {
                "path": root,
                "remote": _git_value(root, ["config", "--get", "remote.origin.url"]),
                "default_branch": _git_value(root, ["branch", "--show-current"]),
            }
        )
    return repos


def _git_value(repo: str, args: Sequence[str]) -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "-C", repo, *args], capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip()
    except Exception:
        return ""
