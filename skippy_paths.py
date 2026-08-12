"""Filesystem roots for Skippy's state.

Production expects the Synology shares mounted at `/Volumes/skippy_*`. When they
are absent (CI, a laptop, a headless test) `SKIPPY_MEMORY_ROOT` and
`SKIPPY_WORKSPACES_ROOT` redirect everything under `~/.skippy`, which is what
keeps the modules importable without the NAS.

Note that workspace *roots* — the repos the agent may touch — are a separate
concept from `workspaces_root()`, which is only a default place to look for them.
The sandbox in `skippy_sandbox.py` is what actually constrains access.
"""

import json
import os

DEFAULT_MEMORY_ROOT = "/Volumes/skippy_memory"
DEFAULT_WORKSPACES_ROOT = os.path.expanduser("~/skippy-workspaces")
FALLBACK_MEMORY_ROOT = "~/.skippy/memory"
FALLBACK_WORKSPACES_ROOT = "~/.skippy/workspaces"


def _resolve(env_name: str, preferred: str, fallback: str) -> str:
    override = os.environ.get(env_name, "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if os.path.isdir(preferred):
        return preferred
    return os.path.abspath(os.path.expanduser(fallback))


def memory_root() -> str:
    return _resolve("SKIPPY_MEMORY_ROOT", DEFAULT_MEMORY_ROOT, FALLBACK_MEMORY_ROOT)


def workspaces_root() -> str:
    return _resolve(
        "SKIPPY_WORKSPACES_ROOT", DEFAULT_WORKSPACES_ROOT, FALLBACK_WORKSPACES_ROOT
    )


def chroma_path(ensure: bool = True) -> str:
    override = os.environ.get("SKIPPY_CHROMA_PATH", "").strip()
    path = os.path.abspath(os.path.expanduser(override)) if override else os.path.join(
        memory_root(), "chroma_db"
    )
    if ensure:
        os.makedirs(path, exist_ok=True)
    return path


def sessions_root(ensure: bool = True) -> str:
    path = os.path.join(memory_root(), "sessions")
    if ensure:
        os.makedirs(path, exist_ok=True)
    return path


def patch_journal_root(ensure: bool = True) -> str:
    """Pre-images written before each patch, kept outside the repos being edited.

    Outside deliberately: a journal inside a workspace would show up in the repo's
    own git status, and a patch that touched it would be journalling into the thing
    it is protecting.
    """
    path = os.path.join(memory_root(), "patch_journal")
    if ensure:
        os.makedirs(path, exist_ok=True)
    return path


def notes_root(ensure: bool = True) -> str:
    """Reverse-engineering note packs.

    Plain files rather than a Chroma collection, and outside the workspace roots. RE
    findings are the durable product of a session — often the only product, since the
    target is not being changed — so they must survive an unmounted NAS, be readable
    without Skippy running, and diff sensibly. Indexing them for search is a layer
    that can be added on top; making search the storage would mean losing the notes
    whenever the vector store is unavailable.
    """
    path = os.path.join(memory_root(), "notes")
    if ensure:
        os.makedirs(path, exist_ok=True)
    return path


def briefs_root(ensure: bool = True) -> str:
    """Research briefs: the sources read for a question and the claims they support.

    Beside the note packs rather than inside them, and plain files for the same
    reasons: a brief is the only durable product of a research run, it has to be
    readable by a person with no Skippy running, and a page's text as it was on the day
    we read it is worth keeping long after the page itself has changed.
    """
    path = os.path.join(memory_root(), "briefs")
    if ensure:
        os.makedirs(path, exist_ok=True)
    return path


DEFAULT_WORKSPACE_ROOTS_FILE = "~/.skippy/workspace_roots.json"


def workspace_roots_file() -> str:
    """Where hub-managed roots live: machine-local, because they are paths on
    this machine. `SKIPPY_WORKSPACE_ROOTS_FILE` overrides it for tests."""
    override = os.environ.get("SKIPPY_WORKSPACE_ROOTS_FILE", "").strip()
    return os.path.abspath(os.path.expanduser(override or DEFAULT_WORKSPACE_ROOTS_FILE))


def _file_workspace_roots() -> list:
    """Roots added at runtime (the app's "new workspace"), tolerant of a missing
    or corrupt file for the same reason every memory read is."""
    try:
        with open(workspace_roots_file(), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    roots = data.get("roots") if isinstance(data, dict) else None
    if not isinstance(roots, list):
        return []
    return [os.path.abspath(os.path.expanduser(str(r))) for r in roots if str(r).strip()]


def add_workspace_root(path: str) -> None:
    """Persist one more root. Read-merge-write through a tmp file, same atomic
    pattern as the memory store's `_write_json`."""
    path = os.path.abspath(os.path.expanduser(str(path)))
    roots = _file_workspace_roots()
    if path in roots:
        return
    roots.append(path)
    target = workspace_roots_file()
    os.makedirs(os.path.dirname(target), exist_ok=True)
    tmp = f"{target}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump({"roots": roots}, handle, indent=2)
    os.replace(tmp, target)


def configured_workspace_roots() -> list:
    """The repos the agent may touch: `SKIPPY_WORKSPACE_ROOTS` (os.pathsep)
    merged with the hub-managed roots file.

    Empty by default. An agent with no roots can reach nothing, which is the
    right failure mode for a misconfiguration. Re-read on every call — that is
    what lets a workspace created from the app exist without a hub restart.
    """
    raw = os.environ.get("SKIPPY_WORKSPACE_ROOTS", "").strip()
    roots = [
        os.path.abspath(os.path.expanduser(part))
        for part in raw.split(os.pathsep)
        if part.strip()
    ] if raw else []
    for extra in _file_workspace_roots():
        if extra not in roots:
            roots.append(extra)
    return roots
