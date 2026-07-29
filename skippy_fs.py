"""Read-only filesystem tools: list, read, search, glob.

Nothing here mutates anything. Every path goes through `Sandbox.resolve` first,
and results produced by walking or globbing are re-checked against the sandbox
before being returned, because a symlink encountered mid-walk can lead outside a
root even when the starting point was inside it.
"""

import asyncio
import fnmatch
import logging
import os
import re
import shutil
from pathlib import Path
from typing import List, Optional

from skippy_sandbox import (
    MAX_FILE_CHARS,
    PRUNED_DIRS,
    Sandbox,
    SandboxError,
    ToolResult,
    cap_text,
)

logger = logging.getLogger("skippy_fs")

MAX_LISTING_ENTRIES = 400
MAX_GLOB_RESULTS = 500
GREP_TIMEOUT_S = 60.0

# Reading a file means holding it in memory, so refuse the ones that would hurt.
# RE targets are exactly the files that trip this, which is why the message
# points somewhere useful instead of just failing.
MAX_READ_BYTES = 8 * 1024 * 1024


def _is_probably_binary(path: str) -> bool:
    """A NUL in the first 8KB. Crude, and the same heuristic git uses."""
    try:
        with open(path, "rb") as handle:
            return b"\x00" in handle.read(8192)
    except OSError:
        return False


def list_dir(sandbox: Sandbox, path: Optional[str] = None, depth: int = 2) -> ToolResult:
    # With several roots there is no single current directory, so "." and an omitted
    # path both mean the whole workspace. Anything else would let an agent call
    # list_dir() to orient itself and never learn that the other repos exist.
    if path in (None, "", ".") and len(sandbox.roots) > 1:
        return _list_all_roots(sandbox, depth)

    root = sandbox.resolve(path or ".", must_exist=True)
    if not os.path.isdir(root):
        return ToolResult(False, f"{sandbox.relative(root)} is not a directory; use read_file.")

    depth = max(1, min(int(depth or 2), 6))
    lines: List[str] = [sandbox.relative(root) + "/"]
    truncated = False

    def walk(current: str, level: int, prefix: str):
        nonlocal truncated
        if level > depth or truncated:
            return
        try:
            entries = sorted(os.scandir(current), key=lambda e: (not e.is_dir(), e.name))
        except OSError as exc:
            lines.append(f"{prefix}[unreadable: {exc}]")
            return
        for entry in entries:
            # Prune by name, not by leading dot: an agent working on this repo
            # needs to see .github/workflows and .gitignore.
            if entry.name in PRUNED_DIRS:
                continue
            if len(lines) >= MAX_LISTING_ENTRIES:
                truncated = True
                return
            if entry.is_dir(follow_symlinks=False):
                lines.append(f"{prefix}{entry.name}/")
                walk(entry.path, level + 1, prefix + "  ")
            elif entry.is_symlink():
                # Shown, but never followed, and flagged when it leaves the sandbox.
                target = os.readlink(entry.path)
                escapes = "" if sandbox.contains(entry.path) else "  [outside workspace]"
                lines.append(f"{prefix}{entry.name} -> {target}{escapes}")
            else:
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    size = 0
                lines.append(f"{prefix}{entry.name} ({size}b)")

    walk(root, 1, "  ")
    if truncated:
        lines.append(f"  ... [listing truncated at {MAX_LISTING_ENTRIES} entries]")
    return ToolResult(
        True,
        f"Listed {sandbox.relative(root)} to depth {depth}.",
        "\n".join(lines),
        {"truncated": truncated},
    )


def _list_all_roots(sandbox: Sandbox, depth: int) -> ToolResult:
    """One listing per root, so a multi-repo workspace is visible in a single call."""
    sections, truncated = [], False
    for root in sandbox.roots:
        result = list_dir(sandbox, root, depth=depth)
        if not result.ok:
            sections.append(f"{sandbox.relative(root)}/  [{result.summary}]")
            continue
        truncated = truncated or bool(result.data.get("truncated"))
        sections.append(result.content)
    return ToolResult(
        True,
        f"Listed {len(sandbox.roots)} workspace roots to depth {depth}.",
        "\n\n".join(sections),
        {"truncated": truncated, "roots": len(sandbox.roots)},
    )


def read_file(
    sandbox: Sandbox,
    path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> ToolResult:
    target = sandbox.resolve(path, must_exist=True)
    display = sandbox.relative(target)

    if os.path.isdir(target):
        return ToolResult(False, f"{display} is a directory; use list_dir.")

    size = os.path.getsize(target)
    if size > MAX_READ_BYTES:
        return ToolResult(
            False,
            f"{display} is {size / 1_048_576:.1f}MB, over the {MAX_READ_BYTES // 1_048_576}MB "
            f"read limit. Use grep to search it, or read a line range.",
            "",
            {"size": size},
        )

    if _is_probably_binary(target):
        return ToolResult(
            False,
            f"{display} looks binary ({size}b). Reading it as text would produce noise; "
            "use a disassembler or a hex tool for this one.",
            "",
            {"size": size, "binary": True},
        )

    with open(target, "r", encoding="utf-8", errors="replace") as handle:
        raw = handle.read()

    all_lines = raw.splitlines()
    total = len(all_lines)
    if total == 0:
        # Handled up front: otherwise `last` computes to 0 and the inverted-range
        # guard below rejects a perfectly readable empty file.
        return ToolResult(
            True, f"Read {display} (empty file).", "",
            {"path": display, "total_lines": 0, "truncated": False},
        )

    first = max(1, int(start_line)) if start_line else 1
    last = min(total, int(end_line)) if end_line else total
    if total and first > total:
        return ToolResult(False, f"start_line {first} is past end of file ({total} lines).")
    if last < first:
        return ToolResult(False, f"end_line {last} is before start_line {first}.")

    selected = all_lines[first - 1:last]
    # Line numbers included so the model can cite a range back without recounting.
    body = "\n".join(f"{first + idx:6d}| {line}" for idx, line in enumerate(selected))
    truncated = len(body) > MAX_FILE_CHARS
    if truncated:
        body = cap_text(body, MAX_FILE_CHARS)

    window = f"lines {first}-{last} of {total}"
    return ToolResult(
        True,
        f"Read {display} ({window}){' [truncated]' if truncated else ''}.",
        body,
        {"path": display, "total_lines": total, "truncated": truncated},
    )


def _rg_available() -> bool:
    return shutil.which("rg") is not None


async def grep(
    sandbox: Sandbox,
    pattern: str,
    path: Optional[str] = None,
    glob: Optional[str] = None,
    max_results: int = 50,
    ignore_case: bool = False,
) -> ToolResult:
    if not pattern:
        return ToolResult(False, "grep requires a 'pattern'.")

    try:
        re.compile(pattern)
    except re.error as exc:
        # Report it as a tool error the model can correct, not an exception.
        return ToolResult(False, f"Invalid regular expression: {exc}")

    # No path means the whole workspace, which is every root rather than just the
    # primary one. Searching only the primary would silently miss matches in the
    # other repos, and cross-repo work is the point of having several roots.
    search_roots = [sandbox.resolve(path, must_exist=True)] if path else list(sandbox.roots)
    limit = max(1, min(int(max_results or 50), 500))

    if _rg_available():
        argv = [
            "rg", "--line-number", "--no-heading", "--color", "never",
            "--max-columns", "400",
            # --hidden so .github and .gitignore are searchable, but never .git
            # internals, which are enormous and meaningless here.
            "--hidden", "--glob", "!.git/",
        ]
        for pruned in sorted(PRUNED_DIRS):
            argv += ["--glob", f"!{pruned}/"]
        if ignore_case:
            argv.append("--ignore-case")
        if glob:
            argv += ["--glob", glob]
        argv += ["--regexp", pattern, *search_roots]

        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=GREP_TIMEOUT_S)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            return ToolResult(False, f"grep timed out after {GREP_TIMEOUT_S:.0f}s.")
        if process.returncode not in (0, 1):  # 1 just means no matches
            return ToolResult(False, f"ripgrep failed: {stderr.decode(errors='replace')[:400]}")
        matches = stdout.decode(errors="replace").splitlines()
    else:
        matches = []
        for root in search_roots:
            matches += await asyncio.to_thread(
                _python_grep, sandbox, root, pattern, glob, ignore_case, limit * 4 - len(matches)
            )
            if len(matches) >= limit * 4:
                break

    trimmed = matches[:limit]
    rendered = "\n".join(_shorten_match(sandbox, line) for line in trimmed)
    note = f" (showing first {limit})" if len(matches) > limit else ""
    if not trimmed:
        return ToolResult(True, f"No matches for /{pattern}/.", "", {"matches": 0})
    return ToolResult(
        True,
        f"{len(matches)} match(es) for /{pattern}/{note}.",
        rendered,
        {"matches": len(matches)},
    )


def _shorten_match(sandbox: Sandbox, line: str) -> str:
    """Rewrite the absolute path in `path:line:text` to a workspace-relative one."""
    for root in sandbox.roots:
        if line.startswith(root + os.sep):
            head, _, tail = line.partition(":")
            return f"{sandbox.relative(head)}:{tail}"
    return line


def _python_grep(
    sandbox: Sandbox,
    root: str,
    pattern: str,
    glob: Optional[str],
    ignore_case: bool,
    cap: int,
) -> List[str]:
    """Fallback for machines without ripgrep. Same output shape."""
    compiled = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    hits: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in PRUNED_DIRS]
        for name in filenames:
            if glob and not fnmatch.fnmatch(name, glob):
                continue
            full = os.path.join(dirpath, name)
            # os.walk can arrive here through a symlinked directory, so re-check.
            if not sandbox.contains(full):
                continue
            try:
                if _is_probably_binary(full):
                    continue
                with open(full, "r", encoding="utf-8", errors="replace") as handle:
                    for number, line in enumerate(handle, start=1):
                        if compiled.search(line):
                            hits.append(f"{full}:{number}:{line.rstrip()}")
                            if len(hits) >= cap:
                                return hits
            except OSError:
                continue
    return hits


def glob_files(sandbox: Sandbox, pattern: str, path: Optional[str] = None) -> ToolResult:
    if not pattern:
        return ToolResult(False, "glob_files requires a 'pattern'.")
    # As with grep: no path means every root, not just the primary one.
    roots = [sandbox.resolve(path, must_exist=True)] if path else list(sandbox.roots)

    found: List[str] = []
    for root in roots:
        try:
            candidates = Path(root).glob(pattern)
        except (ValueError, NotImplementedError) as exc:
            return ToolResult(False, f"Invalid glob pattern '{pattern}': {exc}")

        for candidate in candidates:
            if not candidate.is_file():
                continue
            text = str(candidate)
            # pathlib does not validate anything; a symlinked directory inside the
            # root can produce a match that lives outside it.
            if not sandbox.contains(text):
                continue
            if set(candidate.relative_to(root).parts) & PRUNED_DIRS:
                continue
            # Root-qualified when there are several, so two same-named files in
            # different repos stay distinguishable.
            found.append(sandbox.relative(text))
            if len(found) >= MAX_GLOB_RESULTS:
                break
        if len(found) >= MAX_GLOB_RESULTS:
            break

    found.sort()
    return ToolResult(
        True,
        f"{len(found)} file(s) matching {pattern}.",
        "\n".join(found),
        {"count": len(found)},
    )


def build_sandbox(roots: Optional[List[str]] = None) -> Sandbox:
    """Sandbox from explicit roots, or from SKIPPY_WORKSPACE_ROOTS."""
    import skippy_paths

    chosen = roots if roots else skippy_paths.configured_workspace_roots()
    if not chosen:
        raise SandboxError(
            "No workspace roots configured. Set SKIPPY_WORKSPACE_ROOTS to a "
            f"{os.pathsep}-separated list of directories Skippy may work in."
        )
    return Sandbox(chosen)
