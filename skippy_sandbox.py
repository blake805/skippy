"""The path boundary every filesystem tool goes through.

Each tool that touches a path resolves it with `Sandbox.resolve`, which follows
symlinks first and then requires the result to sit inside a declared workspace
root. A model asking to read `../../.ssh/id_ed25519` gets a hard error, not a
confirmation prompt — there is no "allow once", because the model is not the one
who should be deciding.

Resolving before checking is the important ordering. Checking a path textually
and then opening it would let a symlink inside a root point anywhere on the disk.

What this does not defend against, stated plainly:

- **TOCTOU.** `resolve` validates, then the caller opens. A symlink swapped in
  between would defeat it. Closing that means holding file descriptors through
  every tool, which is not worth it for a single-user agent on a local machine.
- **Hard links.** A hard link inside a root to a file outside it is
  indistinguishable from the real file at the filesystem level.
- **Anything reachable through a root.** A root containing a symlink to `/` grants
  the whole disk. Roots are trusted input; the model never chooses them.
"""

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

logger = logging.getLogger("skippy_sandbox")

MAX_FILE_CHARS = 120_000
MAX_TOOL_OUTPUT_CHARS = 24_000

# Pruned from listings and searches: noise, or enormous, or both. Note that this
# is *not* "everything starting with a dot" — an agent working on this repo needs
# to see `.github/workflows/` and `.gitignore`.
PRUNED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "venv", ".venv", "dist", "build",
    ".build", ".next", "target", ".idea", ".gradle", ".tox", ".eggs",
    "site-packages", ".DS_Store",
}


class SandboxError(Exception):
    """A path fell outside what the agent is allowed to touch."""


def cap_text(text: str, limit: int) -> str:
    """Trim from the middle, keeping both ends.

    The head carries structure and the tail carries the failure, so dropping
    either loses more than dropping the middle.
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
    keep = limit // 2
    omitted = len(text) - (keep * 2)
    return f"{text[:keep]}\n\n... [omitted {omitted} chars] ...\n\n{text[-keep:]}"


@dataclass
class ToolResult:
    ok: bool
    summary: str
    content: str = ""
    data: Dict[str, Any] = field(default_factory=dict)

    def as_event(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "summary": self.summary,
            "content": cap_text(self.content, MAX_TOOL_OUTPUT_CHARS),
            "data": self.data,
        }

    def as_observation(self) -> str:
        """What the model sees. The ok/error prefix is load-bearing: the model
        has to be able to tell a failed tool from an empty result."""
        head = ("OK: " if self.ok else "ERROR: ") + self.summary
        if not self.content:
            return head
        return f"{head}\n{cap_text(self.content, MAX_TOOL_OUTPUT_CHARS)}"


class Sandbox:
    def __init__(self, roots: Sequence[str]):
        if not roots:
            raise SandboxError("At least one workspace root is required.")

        resolved: List[str] = []
        for root in roots:
            candidate = os.path.realpath(os.path.expanduser(str(root)))
            if not os.path.isdir(candidate):
                raise SandboxError(f"Workspace root does not exist: {root}")
            if candidate not in resolved:
                resolved.append(candidate)

        # Drop roots nested inside another root. They grant nothing extra, and
        # keeping both makes `relative()` ambiguous about which name to display.
        pruned: List[str] = []
        for candidate in resolved:
            parent = next(
                (r for r in resolved if r != candidate and candidate.startswith(r + os.sep)),
                None,
            )
            if parent:
                logger.warning("Workspace root %s is inside %s; dropping the nested one.",
                               candidate, parent)
                continue
            pruned.append(candidate)

        self.roots = pruned

    def __repr__(self) -> str:
        return f"Sandbox(roots={self.roots})"

    @property
    def primary(self) -> str:
        """Where relative paths are interpreted from."""
        return self.roots[0]

    def _within_roots(self, path: str) -> bool:
        # The separator matters: without it, root "/a/b" would accept "/a/bad".
        return any(path == root or path.startswith(root + os.sep) for root in self.roots)

    def _bases(self, path: str) -> List[str]:
        """Which directories a relative path could be interpreted from, in order.

        `relative()` prefixes the root's name when there are several roots, so the paths
        every discovery tool prints back — grep, glob_files, list_dir — look like
        `repo-name/src/thing.py`. Joining those onto `primary` gives
        `<primary>/repo-name/src/thing.py`, which does not exist for any root including
        the primary one. So with more than one root, no file the agent found could be
        read using the name it was shown: a live run called glob_files, got one match,
        and then failed to read that exact path five times before giving up.

        The invariant being restored is that `resolve(relative(p))` is `p`. A leading
        segment naming a root means the rest is relative to that root; anything else
        keeps the old meaning of relative-to-primary, which is what single-root setups
        and hand-written paths rely on.
        """
        head, _, _ = path.replace(os.sep, "/").partition("/")
        # Several roots can share a basename (~/a/proj and ~/b/proj), which already
        # makes `relative()` ambiguous. Offer every candidate and let existence on
        # disk decide, rather than silently picking the first.
        named = [root for root in self.roots if os.path.basename(root) == head]
        return [os.path.dirname(root) for root in named] + [self.primary]

    def resolve(self, path: str, must_exist: bool = False) -> str:
        """Return an absolute, symlink-resolved path, or raise SandboxError."""
        if path is None or not str(path).strip():
            raise SandboxError("Empty path.")

        raw = str(path).strip()
        # A NUL byte would raise ValueError out of the os layer, which callers
        # would see as an unexpected crash rather than a rejected path.
        if "\x00" in raw:
            raise SandboxError("Path contains a NUL byte.")

        raw = os.path.expanduser(raw)
        if not os.path.isabs(raw):
            candidates = [os.path.join(base, raw) for base in self._bases(raw)]
            # The first that exists wins; failing that, the first that is inside a root,
            # so a path being created still resolves and still gets range-checked.
            raw = next(
                (c for c in candidates if os.path.exists(c)),
                next(
                    (c for c in candidates if self._within_roots(os.path.realpath(c))),
                    candidates[-1],
                ),
            )

        try:
            real = os.path.realpath(raw)
        except OSError as exc:
            raise SandboxError(f"Could not resolve '{path}': {exc}") from None

        # One check, because `realpath` above already collapsed `..` and followed
        # every symlink in the path — including in the parent of a file that does
        # not exist yet. That makes a separate parent check unreachable: for `real`
        # to be inside a root, every ancestor down to that root is inside it too.
        if not self._within_roots(real):
            raise SandboxError(
                f"Path '{path}' resolves outside the workspace roots {self.roots}."
            )

        if must_exist and not os.path.exists(real):
            raise SandboxError(f"Path does not exist: {path}")
        return real

    def contains(self, path: str) -> bool:
        """Non-raising check, for filtering candidates produced by glob or walk."""
        try:
            self.resolve(path)
            return True
        except SandboxError:
            return False

    def relative(self, path: str) -> str:
        """A short display path. Prefixed with the root's name when there are several."""
        for root in self.roots:
            if path == root or path.startswith(root + os.sep):
                rel = os.path.relpath(path, root)
                if len(self.roots) == 1:
                    return rel
                return os.path.join(os.path.basename(root), rel) if rel != "." else os.path.basename(root)
        return path

    def repo_root_for(self, path: str):
        """The nearest enclosing git repo, or None. Never walks past a root."""
        current = path if os.path.isdir(path) else os.path.dirname(path)
        while self._within_roots(current):
            if os.path.isdir(os.path.join(current, ".git")):
                return current
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return None
