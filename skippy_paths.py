"""Filesystem roots for Skippy's NAS-backed state.

Production expects the Synology shares mounted at `/Volumes/skippy_*`. When they
are absent (CI, a laptop, a headless test) `SKIPPY_MEMORY_ROOT` /
`SKIPPY_WORKSPACES_ROOT` redirect everything under `~/.skippy`, which is what
makes `skippy_factory` importable without the NAS.
"""

import os

DEFAULT_MEMORY_ROOT = "/Volumes/skippy_memory"
DEFAULT_WORKSPACES_ROOT = "/Volumes/skippy_workspaces"
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
    path = os.path.join(memory_root(), "chroma_db")
    if ensure:
        os.makedirs(path, exist_ok=True)
    return path


def sessions_root(ensure: bool = True) -> str:
    path = os.path.join(memory_root(), "sessions")
    if ensure:
        os.makedirs(path, exist_ok=True)
    return path
