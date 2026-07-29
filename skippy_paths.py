"""Filesystem roots for Skippy's state.

Production expects the Synology shares mounted at `/Volumes/skippy_*`. When they
are absent (CI, a laptop, a headless test) `SKIPPY_MEMORY_ROOT` and
`SKIPPY_WORKSPACES_ROOT` redirect everything under `~/.skippy`, which is what
keeps the modules importable without the NAS.

Note that workspace *roots* — the repos the agent may touch — are a separate
concept from `workspaces_root()`, which is only a default place to look for them.
The sandbox in `skippy_sandbox.py` is what actually constrains access.
"""

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


def configured_workspace_roots() -> list:
    """The repos the agent may touch, from `SKIPPY_WORKSPACE_ROOTS` (os.pathsep).

    Empty by default. An agent with no roots can reach nothing, which is the
    right failure mode for a misconfiguration.
    """
    raw = os.environ.get("SKIPPY_WORKSPACE_ROOTS", "").strip()
    if not raw:
        return []
    return [
        os.path.abspath(os.path.expanduser(part))
        for part in raw.split(os.pathsep)
        if part.strip()
    ]
