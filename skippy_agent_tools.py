"""The coding agent's toolbelt: sandboxed filesystem, search, git, and patching.

Everything here is scoped to an explicit set of workspace roots. A tool that
touches a path resolves it through `Sandbox.resolve`, which follows symlinks and
then requires the result to sit inside a root -- so a model that asks to write
`../../.ssh/authorized_keys` gets a hard error rather than a confirmation prompt.

The shop toolbelt in `tools.py` is untouched; this is a separate lane.
"""

import asyncio
import difflib
import fnmatch
import json
import logging
import os
import shlex
import shutil
import tempfile
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

import skippy_cursor

logger = logging.getLogger("skippy_agent_tools")

MAX_FILE_CHARS = 120_000
MAX_TOOL_OUTPUT_CHARS = 24_000
MAX_DIFF_CHARS = 20_000
DEFAULT_TEST_TIMEOUT = 300.0

PRUNED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "venv", ".venv", "dist", "build",
    ".build", ".next", "target", ".DS_Store", ".idea", ".gradle",
}

# `run_tests` never asks for human approval, so it may only ever launch a test
# runner. Anything else has to go through `run_terminal` and its auth gate.
TEST_COMMAND_ALLOWLIST = (
    "pytest",
    "python -m pytest",
    "python3 -m pytest",
    "python -m unittest",
    "python3 -m unittest",
    "npm test",
    "npm run test",
    "yarn test",
    "pnpm test",
    "swift test",
    "cargo test",
    "go test",
    "make test",
    "make check",
)

SHELL_METACHARACTERS = (";", "&&", "||", "|", "`", "$(", ">", "<", "\n")


class SandboxError(Exception):
    """A path or command fell outside what the agent is allowed to touch."""


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
            "content": _cap(self.content, MAX_TOOL_OUTPUT_CHARS),
            "data": self.data,
        }

    def as_observation(self) -> str:
        head = ("OK: " if self.ok else "ERROR: ") + self.summary
        if not self.content:
            return head
        return f"{head}\n{_cap(self.content, MAX_TOOL_OUTPUT_CHARS)}"


def _cap(text: str, limit: int) -> str:
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    keep = limit // 2
    omitted = len(text) - (keep * 2)
    return f"{text[:keep]}\n\n... [omitted {omitted} chars] ...\n\n{text[-keep:]}"


# ---------------------------------------------------------------------------
# Sandbox
# ---------------------------------------------------------------------------

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
        self.roots = resolved

    @property
    def primary(self) -> str:
        return self.roots[0]

    def _within_roots(self, path: str) -> bool:
        return any(path == root or path.startswith(root + os.sep) for root in self.roots)

    def resolve(self, path: str, must_exist: bool = False) -> str:
        if not path or not str(path).strip():
            raise SandboxError("Empty path.")
        raw = os.path.expanduser(str(path).strip())
        if not os.path.isabs(raw):
            raw = os.path.join(self.primary, raw)

        real = os.path.realpath(raw)
        if not self._within_roots(real):
            raise SandboxError(
                f"Path '{path}' resolves outside the workspace roots {self.roots}."
            )
        # Defense in depth for paths that do not exist yet: confirm the directory
        # they would be created in is also inside a root. A root is its own
        # boundary, so it is exempt from the parent check.
        if real not in self.roots:
            real_parent = os.path.realpath(os.path.dirname(real))
            if not self._within_roots(real_parent):
                raise SandboxError(f"Parent directory of '{path}' is outside the workspace roots.")
        if must_exist and not os.path.exists(real):
            raise SandboxError(f"Path does not exist: {path}")
        return real

    def relative(self, path: str) -> str:
        for root in self.roots:
            if path == root or path.startswith(root + os.sep):
                rel = os.path.relpath(path, root)
                return rel if len(self.roots) == 1 else os.path.join(os.path.basename(root), rel)
        return path

    def repo_root_for(self, path: str) -> Optional[str]:
        current = path if os.path.isdir(path) else os.path.dirname(path)
        while self._within_roots(current):
            if os.path.isdir(os.path.join(current, ".git")):
                return current
            parent = os.path.dirname(current)
            if parent == current:
                break
            current = parent
        return None


# ---------------------------------------------------------------------------
# Tool context
# ---------------------------------------------------------------------------

ApproveFn = Callable[[str, str], Awaitable[bool]]
EmitFn = Callable[[dict], Awaitable[None]]


@dataclass
class ToolContext:
    sandbox: Sandbox
    backup_dir: Optional[str] = None
    dry_run: bool = False
    memory: Any = None
    approve: Optional[ApproveFn] = None
    emit: Optional[EmitFn] = None
    auto_approve: Dict[str, bool] = field(default_factory=dict)
    session_id: str = ""
    cursor: Any = None

    async def request_approval(self, command: str, explanation: str) -> bool:
        if self.auto_approve.get("terminal"):
            return True
        if self.approve is None:
            return False
        return await self.approve(command, explanation)


# ---------------------------------------------------------------------------
# Tool specs (single source of truth for names and argument shapes)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolSpec:
    name: str
    args: str
    doc: str
    mutates: bool = False
    requires_auth: bool = False


TOOL_SPECS: Sequence[ToolSpec] = (
    ToolSpec("list_dir", '{"path": "<dir>", "depth": 2}', "Map a directory tree."),
    ToolSpec(
        "read_file",
        '{"path": "<file>", "start_line": null, "end_line": null}',
        "Read a file with line numbers. Use start_line/end_line for large files.",
    ),
    ToolSpec(
        "grep",
        '{"pattern": "<regex>", "path": null, "glob": null, "max_results": 50, "ignore_case": false}',
        "Regex search across the workspace (ripgrep).",
    ),
    ToolSpec("glob_files", '{"pattern": "**/*.py", "path": null}', "List files matching a glob."),
    ToolSpec(
        "apply_patch",
        '{"edits": [{"path": "<file>", "action": "edit|create|delete", "search": "<exact text>", '
        '"replace": "<new text>", "content": "<full file for create>"}]}',
        "Apply a multi-file edit. All-or-nothing: if any edit fails to validate, nothing is written.",
        mutates=True,
    ),
    ToolSpec(
        "run_tests",
        '{"command": "pytest -q", "cwd": null, "timeout": 300}',
        "Run a test command. Only test runners are permitted here.",
    ),
    ToolSpec(
        "run_terminal",
        '{"command": "<shell>", "cwd": null, "explanation": "<why>"}',
        "Run an arbitrary shell command. Requires human approval.",
        mutates=True,
        requires_auth=True,
    ),
    ToolSpec("git_status", '{"repo": null}', "Show working tree status."),
    ToolSpec("git_diff", '{"repo": null, "staged": false, "path": null}', "Show the current diff."),
    ToolSpec("git_log", '{"repo": null, "limit": 10}', "Show recent commits."),
    ToolSpec("git_branch", '{"repo": null, "name": "<branch>", "create": true}', "Create or switch branch."),
    ToolSpec("git_commit", '{"repo": null, "message": "<msg>", "add_all": true}', "Stage and commit.", mutates=True),
    ToolSpec(
        "git_push",
        '{"repo": null, "remote": "origin", "branch": null}',
        "Push a branch. Requires human approval.",
        mutates=True,
        requires_auth=True,
    ),
    ToolSpec(
        "search_project_memory",
        '{"query": "<what you need to recall>", "k": 8}',
        "Search this project's prior decisions, session notes, and indexed code.",
    ),
    ToolSpec(
        "save_decision",
        '{"title": "<short title>", "body": "<what and why>", "tags": ["..."]}',
        "Record a durable decision in project memory.",
        mutates=True,
    ),
    ToolSpec(
        "cursor_apply_patch",
        '{"edits": [{"path": "<file>", "action": "edit", "search": "...", "replace": "..."}]}',
        "Same edit shape as apply_patch, but routed through the editor so the change lands in "
        "the user's undo stack. Falls back to a direct write when Cursor is not attached.",
        mutates=True,
    ),
    ToolSpec(
        "cursor_diagnostics",
        '{"paths": ["<file>"]}',
        "Fetch the editor's live errors and warnings. Prefer this over guessing after an edit.",
    ),
    ToolSpec("cursor_open_files", "{}", "List the files the user currently has open."),
    ToolSpec(
        "finish",
        '{"summary": "<what you did>", "files_changed": ["<path>"]}',
        "End the task. Only call this once the work is actually done and verified.",
    ),
)

TOOL_SPECS_BY_NAME: Dict[str, ToolSpec] = {spec.name: spec for spec in TOOL_SPECS}


def render_tool_spec() -> str:
    lines = []
    for spec in TOOL_SPECS:
        flags = []
        if spec.requires_auth:
            flags.append("needs human approval")
        if spec.mutates:
            flags.append("mutating")
        suffix = f"  [{', '.join(flags)}]" if flags else ""
        lines.append(f'- {spec.name}: {spec.args}\n    {spec.doc}{suffix}')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------

def list_dir(ctx: ToolContext, path: str = ".", depth: int = 2) -> ToolResult:
    root = ctx.sandbox.resolve(path or ".", must_exist=True)
    if not os.path.isdir(root):
        return ToolResult(False, f"{ctx.sandbox.relative(root)} is not a directory.")

    depth = max(1, min(int(depth or 2), 6))
    lines: List[str] = [ctx.sandbox.relative(root) + "/"]
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
            if entry.name in PRUNED_DIRS or entry.name.startswith("."):
                continue
            if len(lines) >= 400:
                truncated = True
                return
            if entry.is_dir(follow_symlinks=False):
                lines.append(f"{prefix}{entry.name}/")
                walk(entry.path, level + 1, prefix + "  ")
            else:
                try:
                    size = entry.stat(follow_symlinks=False).st_size
                except OSError:
                    size = 0
                lines.append(f"{prefix}{entry.name} ({size}b)")

    walk(root, 1, "  ")
    if truncated:
        lines.append("  ... [listing truncated at 400 entries]")
    return ToolResult(True, f"Listed {ctx.sandbox.relative(root)} to depth {depth}.", "\n".join(lines))


def read_file(
    ctx: ToolContext,
    path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None,
) -> ToolResult:
    target = ctx.sandbox.resolve(path, must_exist=True)
    if os.path.isdir(target):
        return ToolResult(False, f"{ctx.sandbox.relative(target)} is a directory; use list_dir.")

    with open(target, "r", encoding="utf-8", errors="replace") as handle:
        raw = handle.read()

    all_lines = raw.splitlines()
    total = len(all_lines)
    first = max(1, int(start_line)) if start_line else 1
    last = min(total, int(end_line)) if end_line else total
    if first > total:
        return ToolResult(False, f"start_line {first} is past end of file ({total} lines).")

    selected = all_lines[first - 1 : last]
    body = "\n".join(f"{first + idx:6d}| {line}" for idx, line in enumerate(selected))
    truncated = len(body) > MAX_FILE_CHARS
    if truncated:
        body = _cap(body, MAX_FILE_CHARS)

    window = f"lines {first}-{last} of {total}"
    return ToolResult(
        True,
        f"Read {ctx.sandbox.relative(target)} ({window}){' [truncated]' if truncated else ''}.",
        body,
        {"path": ctx.sandbox.relative(target), "total_lines": total, "truncated": truncated},
    )


def _rg_available() -> bool:
    return shutil.which("rg") is not None


async def grep(
    ctx: ToolContext,
    pattern: str,
    path: Optional[str] = None,
    glob: Optional[str] = None,
    max_results: int = 50,
    ignore_case: bool = False,
) -> ToolResult:
    if not pattern:
        return ToolResult(False, "grep requires a 'pattern'.")
    search_root = ctx.sandbox.resolve(path, must_exist=True) if path else ctx.sandbox.primary
    limit = max(1, min(int(max_results or 50), 500))

    if _rg_available():
        argv = ["rg", "--line-number", "--no-heading", "--color", "never", "--max-columns", "400"]
        if ignore_case:
            argv.append("--ignore-case")
        if glob:
            argv += ["--glob", glob]
        argv += ["--regexp", pattern, search_root]
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=60.0)
        except asyncio.TimeoutError:
            process.kill()
            return ToolResult(False, "grep timed out after 60s.")
        if process.returncode not in (0, 1):
            return ToolResult(False, f"ripgrep failed: {stderr.decode(errors='replace')[:400]}")
        matches = stdout.decode(errors="replace").splitlines()
    else:
        matches = _python_grep(search_root, pattern, glob, ignore_case, limit * 4)

    trimmed = matches[:limit]
    rendered = "\n".join(
        line.replace(ctx.sandbox.primary + os.sep, "") for line in trimmed
    )
    note = f" (showing first {limit})" if len(matches) > limit else ""
    if not trimmed:
        return ToolResult(True, f"No matches for /{pattern}/.", "", {"matches": 0})
    return ToolResult(
        True,
        f"{len(matches)} match(es) for /{pattern}/{note}.",
        rendered,
        {"matches": len(matches)},
    )


def _python_grep(
    root: str, pattern: str, glob: Optional[str], ignore_case: bool, cap: int
) -> List[str]:
    import re

    flags = re.IGNORECASE if ignore_case else 0
    compiled = re.compile(pattern, flags)
    hits: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in PRUNED_DIRS and not d.startswith(".")]
        for name in filenames:
            if glob and not fnmatch.fnmatch(name, glob):
                continue
            full = os.path.join(dirpath, name)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as handle:
                    for number, line in enumerate(handle, start=1):
                        if compiled.search(line):
                            hits.append(f"{full}:{number}:{line.rstrip()}")
                            if len(hits) >= cap:
                                return hits
            except OSError:
                continue
    return hits


def glob_files(ctx: ToolContext, pattern: str, path: Optional[str] = None) -> ToolResult:
    if not pattern:
        return ToolResult(False, "glob_files requires a 'pattern'.")
    root = ctx.sandbox.resolve(path, must_exist=True) if path else ctx.sandbox.primary
    from pathlib import Path

    found: List[str] = []
    for candidate in Path(root).glob(pattern):
        if not candidate.is_file():
            continue
        parts = set(candidate.relative_to(root).parts)
        if parts & PRUNED_DIRS:
            continue
        found.append(str(candidate.relative_to(root)))
        if len(found) >= 500:
            break
    found.sort()
    return ToolResult(
        True, f"{len(found)} file(s) matching {pattern}.", "\n".join(found), {"count": len(found)}
    )


# ---------------------------------------------------------------------------
# apply_patch
# ---------------------------------------------------------------------------

@dataclass
class _PlannedWrite:
    path: str
    action: str
    before: Optional[str]
    after: Optional[str]


def apply_patch(ctx: ToolContext, edits: Sequence[dict]) -> ToolResult:
    """Validate every edit against staged content, then write all or nothing."""
    if not edits or not isinstance(edits, (list, tuple)):
        return ToolResult(False, "apply_patch requires a non-empty 'edits' list.")

    staged: Dict[str, Optional[str]] = {}
    original: Dict[str, Optional[str]] = {}
    order: List[str] = []
    actions: Dict[str, str] = {}
    problems: List[str] = []

    for index, edit in enumerate(edits):
        if not isinstance(edit, dict):
            problems.append(f"edit {index}: expected an object, got {type(edit).__name__}")
            continue
        action = str(edit.get("action") or "edit").lower()
        raw_path = edit.get("path")
        try:
            target = ctx.sandbox.resolve(raw_path)
        except SandboxError as exc:
            problems.append(f"edit {index} ({raw_path}): {exc}")
            continue

        if target not in staged:
            existing = None
            if os.path.isfile(target):
                with open(target, "r", encoding="utf-8", errors="replace") as handle:
                    existing = handle.read()
            elif os.path.isdir(target):
                problems.append(f"edit {index} ({raw_path}): path is a directory")
                continue
            staged[target] = existing
            original[target] = existing
            order.append(target)

        current = staged[target]

        if action == "create":
            content = edit.get("content")
            if content is None:
                problems.append(f"edit {index} ({raw_path}): 'create' requires 'content'")
                continue
            if current is not None and not edit.get("overwrite"):
                problems.append(
                    f"edit {index} ({raw_path}): file already exists; use action 'edit' "
                    "or pass \"overwrite\": true"
                )
                continue
            staged[target] = str(content)
            actions[target] = "create" if original[target] is None else "overwrite"

        elif action == "delete":
            if current is None:
                problems.append(f"edit {index} ({raw_path}): cannot delete, file does not exist")
                continue
            staged[target] = None
            actions[target] = "delete"

        elif action == "edit":
            if current is None:
                problems.append(
                    f"edit {index} ({raw_path}): file does not exist; use action 'create'"
                )
                continue
            search = edit.get("search")
            if not search:
                problems.append(f"edit {index} ({raw_path}): 'edit' requires non-empty 'search'")
                continue
            replace = edit.get("replace")
            if replace is None:
                problems.append(f"edit {index} ({raw_path}): 'edit' requires 'replace'")
                continue
            search = str(search)
            replace = str(replace)
            hits = current.count(search)
            if hits == 0:
                problems.append(
                    f"edit {index} ({raw_path}): 'search' text not found. It must match the file "
                    "byte-for-byte including indentation; re-read the file and retry"
                )
                continue

            if edit.get("replace_all"):
                staged[target] = current.replace(search, replace)
            elif edit.get("occurrence") is not None:
                try:
                    which = int(edit["occurrence"])
                except (TypeError, ValueError):
                    problems.append(f"edit {index} ({raw_path}): 'occurrence' must be an integer")
                    continue
                if which < 1 or which > hits:
                    problems.append(
                        f"edit {index} ({raw_path}): 'occurrence' {which} out of range "
                        f"(found {hits})"
                    )
                    continue
                staged[target] = _replace_nth(current, search, replace, which)
            elif hits > 1:
                problems.append(
                    f"edit {index} ({raw_path}): 'search' matched {hits} times. Include more "
                    "surrounding context, or pass \"replace_all\": true / \"occurrence\": <n>"
                )
                continue
            else:
                staged[target] = current.replace(search, replace, 1)

            actions.setdefault(target, "edit")

        else:
            problems.append(f"edit {index} ({raw_path}): unknown action '{action}'")

    if problems:
        return ToolResult(
            False,
            f"Patch rejected; {len(problems)} problem(s) found and nothing was written.",
            "\n".join(problems),
            {"problems": problems},
        )

    planned = [
        _PlannedWrite(path, actions.get(path, "edit"), original[path], staged[path])
        for path in order
        if original[path] != staged[path]
    ]
    if not planned:
        return ToolResult(True, "Patch was a no-op; file contents already match.", "", {"files": []})

    diff_chunks: List[str] = []
    file_reports: List[dict] = []
    for item in planned:
        rel = ctx.sandbox.relative(item.path)
        before_lines = (item.before or "").splitlines(keepends=True)
        after_lines = (item.after or "").splitlines(keepends=True)
        chunk = list(
            difflib.unified_diff(
                before_lines,
                after_lines,
                fromfile=f"a/{rel}",
                tofile=f"b/{rel}",
                n=3,
            )
        )
        diff_chunks.append("".join(chunk))
        added = sum(1 for line in chunk if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in chunk if line.startswith("-") and not line.startswith("---"))
        file_reports.append(
            {"path": rel, "action": item.action, "added": added, "removed": removed}
        )

    diff = _cap("".join(diff_chunks), MAX_DIFF_CHARS)

    if ctx.dry_run:
        return ToolResult(
            True,
            f"Dry run: {len(planned)} file(s) would change (nothing written).",
            diff,
            {"files": file_reports, "diff": diff, "dry_run": True},
        )

    written: List[str] = []
    try:
        for item in planned:
            _backup(ctx, item)
            if item.after is None:
                os.remove(item.path)
            else:
                _atomic_write(item.path, item.after)
            written.append(item.path)
    except Exception as exc:
        _rollback(planned, written)
        return ToolResult(
            False,
            f"Write failed on {ctx.sandbox.relative(item.path)}; rolled back {len(written)} file(s).",
            str(exc),
        )

    summary = ", ".join(
        f"{report['path']} (+{report['added']}/-{report['removed']})" for report in file_reports
    )
    return ToolResult(
        True,
        f"Applied {len(planned)} file change(s): {summary}",
        diff,
        {"files": file_reports, "diff": diff},
    )


def _replace_nth(text: str, search: str, replace: str, which: int) -> str:
    position = -1
    for _ in range(which):
        position = text.find(search, position + 1)
        if position == -1:
            return text
    return text[:position] + replace + text[position + len(search) :]


def _atomic_write(path: str, content: str) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    mode = os.stat(path).st_mode if os.path.exists(path) else None
    handle, temp_path = tempfile.mkstemp(dir=directory, prefix=".skippy-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as writer:
            writer.write(content)
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def _backup(ctx: ToolContext, item: _PlannedWrite) -> None:
    if not ctx.backup_dir or item.before is None:
        return
    rel = ctx.sandbox.relative(item.path).replace(os.sep, "__")
    os.makedirs(ctx.backup_dir, exist_ok=True)
    with open(os.path.join(ctx.backup_dir, rel + ".orig"), "w", encoding="utf-8") as handle:
        handle.write(item.before)
    manifest = os.path.join(ctx.backup_dir, "manifest.json")
    entries = []
    if os.path.exists(manifest):
        try:
            with open(manifest, "r", encoding="utf-8") as handle:
                entries = json.load(handle)
        except (OSError, ValueError):
            entries = []
    entries.append({"path": item.path, "backup": rel + ".orig", "action": item.action})
    with open(manifest, "w", encoding="utf-8") as handle:
        json.dump(entries, handle, indent=2)


def _rollback(planned: Sequence[_PlannedWrite], written: Sequence[str]) -> None:
    by_path = {item.path: item for item in planned}
    for path in written:
        item = by_path[path]
        try:
            if item.before is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                _atomic_write(path, item.before)
        except Exception as exc:
            logger.error("Rollback failed for %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------

def _is_allowed_test_command(command: str) -> bool:
    normalized = " ".join(command.split())
    return any(
        normalized == allowed or normalized.startswith(allowed + " ")
        for allowed in TEST_COMMAND_ALLOWLIST
    )


async def _exec(argv: Sequence[str], cwd: str, timeout: float) -> tuple:
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return None, f"[timed out after {timeout:g}s]"
    return process.returncode, stdout.decode(errors="replace")


async def run_tests(
    ctx: ToolContext,
    command: str,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
) -> ToolResult:
    if not command or not command.strip():
        return ToolResult(False, "run_tests requires a 'command'.")
    if any(token in command for token in SHELL_METACHARACTERS):
        return ToolResult(
            False,
            "run_tests does not accept shell operators. Use a single test command, "
            "or run_terminal (which asks for approval).",
        )
    if not _is_allowed_test_command(command):
        return ToolResult(
            False,
            f"'{command.split()[0]}' is not a permitted test runner. Allowed prefixes: "
            f"{', '.join(TEST_COMMAND_ALLOWLIST)}. Use run_terminal for anything else.",
        )

    workdir = ctx.sandbox.resolve(cwd, must_exist=True) if cwd else ctx.sandbox.primary
    limit = float(timeout or DEFAULT_TEST_TIMEOUT)
    code, output = await _exec(shlex.split(command), workdir, limit)
    if code is None:
        return ToolResult(False, f"Test command timed out after {limit:g}s.", output)
    ok = code == 0
    return ToolResult(
        ok,
        f"`{command}` exited {code} in {ctx.sandbox.relative(workdir)}.",
        output,
        {"exit_code": code, "command": command},
    )


async def run_terminal(
    ctx: ToolContext,
    command: str,
    cwd: Optional[str] = None,
    explanation: str = "",
    timeout: float = 120.0,
) -> ToolResult:
    if not command or not command.strip():
        return ToolResult(False, "run_terminal requires a 'command'.")
    workdir = ctx.sandbox.resolve(cwd, must_exist=True) if cwd else ctx.sandbox.primary

    approved = await ctx.request_approval(command, explanation or "Agent shell command")
    if not approved:
        return ToolResult(
            False,
            "Human denied the command. Find another way; do not retry the same command.",
            "",
            {"denied": True},
        )

    process = await asyncio.create_subprocess_shell(
        command,
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        return ToolResult(False, f"Command timed out after {timeout:g}s.")
    output = stdout.decode(errors="replace")
    return ToolResult(
        process.returncode == 0,
        f"`{command}` exited {process.returncode}.",
        output,
        {"exit_code": process.returncode},
    )


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------

GIT_AUTHOR_NAME = os.environ.get("SKIPPY_GIT_AUTHOR_NAME", "Skippy Agent")
GIT_AUTHOR_EMAIL = os.environ.get("SKIPPY_GIT_AUTHOR_EMAIL", "skippy@localhost")


def _repo_for(ctx: ToolContext, repo: Optional[str]) -> str:
    base = ctx.sandbox.resolve(repo, must_exist=True) if repo else ctx.sandbox.primary
    found = ctx.sandbox.repo_root_for(base)
    if found is None:
        raise SandboxError(f"No git repository at or above {ctx.sandbox.relative(base)}.")
    return found


async def _git(repo: str, args: Sequence[str], timeout: float = 60.0) -> tuple:
    argv = [
        "git",
        "-c",
        f"user.name={GIT_AUTHOR_NAME}",
        "-c",
        f"user.email={GIT_AUTHOR_EMAIL}",
        *args,
    ]
    return await _exec(argv, repo, timeout)


async def git_status(ctx: ToolContext, repo: Optional[str] = None) -> ToolResult:
    root = _repo_for(ctx, repo)
    code, output = await _git(root, ["status", "--short", "--branch"])
    return ToolResult(code == 0, f"git status in {ctx.sandbox.relative(root)}.", output)


async def git_diff(
    ctx: ToolContext,
    repo: Optional[str] = None,
    staged: bool = False,
    path: Optional[str] = None,
) -> ToolResult:
    root = _repo_for(ctx, repo)
    args = ["diff"]
    if staged:
        args.append("--staged")
    if path:
        args += ["--", os.path.relpath(ctx.sandbox.resolve(path), root)]
    code, output = await _git(root, args)
    label = "staged diff" if staged else "working tree diff"
    if code == 0 and not output.strip():
        return ToolResult(True, f"No {label} in {ctx.sandbox.relative(root)}.")
    return ToolResult(code == 0, f"{label} for {ctx.sandbox.relative(root)}.", _cap(output, MAX_DIFF_CHARS))


async def git_log(ctx: ToolContext, repo: Optional[str] = None, limit: int = 10) -> ToolResult:
    root = _repo_for(ctx, repo)
    count = max(1, min(int(limit or 10), 100))
    code, output = await _git(root, ["log", f"-{count}", "--oneline", "--decorate"])
    return ToolResult(code == 0, f"Last {count} commit(s) in {ctx.sandbox.relative(root)}.", output)


async def git_branch(
    ctx: ToolContext,
    repo: Optional[str] = None,
    name: Optional[str] = None,
    create: bool = True,
) -> ToolResult:
    root = _repo_for(ctx, repo)
    if not name:
        code, output = await _git(root, ["branch", "--show-current"])
        return ToolResult(code == 0, f"Current branch: {output.strip() or 'unknown'}.", output)
    args = ["checkout", "-b", name] if create else ["checkout", name]
    code, output = await _git(root, args)
    if code != 0 and create and "already exists" in output:
        code, output = await _git(root, ["checkout", name])
    return ToolResult(code == 0, f"Branch '{name}' {'ready' if code == 0 else 'failed'}.", output)


async def git_commit(
    ctx: ToolContext,
    repo: Optional[str] = None,
    message: str = "",
    add_all: bool = True,
) -> ToolResult:
    if not message.strip():
        return ToolResult(False, "git_commit requires a 'message'.")
    root = _repo_for(ctx, repo)
    if ctx.dry_run:
        return ToolResult(True, "Dry run: skipped git_commit.")
    if add_all:
        code, output = await _git(root, ["add", "-A"])
        if code != 0:
            return ToolResult(False, "git add failed.", output)
    code, output = await _git(root, ["commit", "-m", message])
    if code != 0 and "nothing to commit" in output:
        return ToolResult(False, "Nothing to commit.", output)
    return ToolResult(code == 0, f"Commit {'created' if code == 0 else 'failed'}.", output)


async def git_push(
    ctx: ToolContext,
    repo: Optional[str] = None,
    remote: str = "origin",
    branch: Optional[str] = None,
) -> ToolResult:
    root = _repo_for(ctx, repo)
    if not branch:
        _, current = await _git(root, ["branch", "--show-current"])
        branch = current.strip()
    if not branch:
        return ToolResult(False, "Could not determine the branch to push.")

    if not ctx.auto_approve.get("git_push"):
        approved = await ctx.request_approval(
            f"git push -u {remote} {branch}", f"Push {branch} from {ctx.sandbox.relative(root)}"
        )
        if not approved:
            return ToolResult(False, "Human denied the push.", "", {"denied": True})
    if ctx.dry_run:
        return ToolResult(True, "Dry run: skipped git_push.")
    code, output = await _git(root, ["push", "-u", remote, branch], timeout=180.0)
    return ToolResult(code == 0, f"Push of '{branch}' {'succeeded' if code == 0 else 'failed'}.", output)


# ---------------------------------------------------------------------------
# Project memory (backed by skippy_sessions when a store is attached)
# ---------------------------------------------------------------------------

async def search_project_memory(ctx: ToolContext, query: str, k: int = 8) -> ToolResult:
    if ctx.memory is None:
        return ToolResult(
            True,
            "Project memory is not attached to this session; rely on the files in the workspace.",
        )
    if not query:
        return ToolResult(False, "search_project_memory requires a 'query'.")
    return await ctx.memory.search(query, k=int(k or 8))


async def save_decision(
    ctx: ToolContext, title: str, body: str, tags: Optional[Sequence[str]] = None
) -> ToolResult:
    if ctx.memory is None:
        return ToolResult(True, "Project memory is not attached; decision not persisted.")
    if not title or not body:
        return ToolResult(False, "save_decision requires both 'title' and 'body'.")
    if ctx.dry_run:
        return ToolResult(True, "Dry run: skipped save_decision.")
    return await ctx.memory.save_decision(
        title=title, body=body, tags=list(tags or []), session_id=ctx.session_id
    )


# ---------------------------------------------------------------------------
# Cursor-mediated tools
# ---------------------------------------------------------------------------

async def cursor_apply_patch(ctx: ToolContext, edits: Sequence[dict]) -> ToolResult:
    """Apply an edit set through the editor, falling back to a direct write.

    The patch is validated and diffed locally first, for two reasons: the model gets
    the same rejection messages either way, and the editor is never handed a path
    that escapes the workspace roots.
    """
    preview = apply_patch(replace(ctx, dry_run=True, backup_dir=None), edits)
    if not preview.ok:
        return preview
    if ctx.dry_run:
        return preview
    if not preview.data.get("files"):
        return preview

    bridge = ctx.cursor
    if bridge is None or not bridge.connected:
        result = apply_patch(ctx, edits)
        if result.ok:
            result.summary += " (Cursor not attached; wrote to disk directly.)"
            result.data["via"] = "filesystem"
        return result

    absolute_edits = []
    for edit in edits:
        prepared = dict(edit)
        prepared["path"] = ctx.sandbox.resolve(edit.get("path"))
        absolute_edits.append(prepared)

    response = await bridge.apply_patches(absolute_edits)
    if not response["ok"]:
        fallback = apply_patch(ctx, edits)
        if fallback.ok:
            fallback.summary += f" (Cursor refused the edit: {response['error']}; wrote to disk.)"
            fallback.data["via"] = "filesystem"
            return fallback
        return ToolResult(
            False,
            f"Cursor could not apply the patch ({response['error']}) and the direct write "
            f"also failed.",
            fallback.content,
        )

    failed = (response["result"] or {}).get("failed") or []
    if failed:
        return ToolResult(
            False,
            f"Cursor rejected {len(failed)} edit(s); nothing was applied.",
            json.dumps(failed, indent=2),
            {"failed": failed},
        )

    data = dict(preview.data)
    data.pop("dry_run", None)
    data["via"] = "cursor"
    return ToolResult(
        True,
        preview.summary.replace("Dry run: ", "").replace(
            " would change (nothing written).", " changed via Cursor."
        ),
        preview.content,
        data,
    )


async def cursor_diagnostics(ctx: ToolContext, paths: Optional[Sequence[str]] = None) -> ToolResult:
    bridge = ctx.cursor
    if bridge is None or not bridge.connected:
        return ToolResult(
            False,
            "Cursor is not attached, so editor diagnostics are unavailable. Use run_tests or "
            "read the code instead.",
        )
    resolved = [ctx.sandbox.resolve(path) for path in (paths or [])]
    response = await bridge.diagnostics(resolved)
    if not response["ok"]:
        return ToolResult(False, f"Could not fetch diagnostics: {response['error']}")

    rendered = skippy_cursor.format_diagnostics(response["result"])
    if not rendered:
        return ToolResult(True, "Cursor reports no diagnostics.", "", {"count": 0})
    count = len(rendered.splitlines())
    return ToolResult(True, f"{count} diagnostic(s) from Cursor.", rendered, {"count": count})


async def cursor_open_files(ctx: ToolContext) -> ToolResult:
    bridge = ctx.cursor
    if bridge is None or not bridge.connected:
        return ToolResult(False, "Cursor is not attached.")
    response = await bridge.open_files()
    if not response["ok"]:
        return ToolResult(False, f"Could not list open files: {response['error']}")

    files = (response["result"] or {}).get("files") or []
    lines = []
    for entry in files:
        if isinstance(entry, dict):
            flags = [flag for flag, on in (("active", entry.get("active")), ("unsaved", entry.get("dirty"))) if on]
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            lines.append(f"{entry.get('path', '?')}{suffix}")
        else:
            lines.append(str(entry))
    return ToolResult(True, f"{len(files)} file(s) open in Cursor.", "\n".join(lines))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_SYNC_TOOLS: Dict[str, Callable[..., ToolResult]] = {
    "list_dir": list_dir,
    "read_file": read_file,
    "glob_files": glob_files,
    "apply_patch": apply_patch,
}

_ASYNC_TOOLS: Dict[str, Callable[..., Awaitable[ToolResult]]] = {
    "grep": grep,
    "run_tests": run_tests,
    "run_terminal": run_terminal,
    "git_status": git_status,
    "git_diff": git_diff,
    "git_log": git_log,
    "git_branch": git_branch,
    "git_commit": git_commit,
    "git_push": git_push,
    "search_project_memory": search_project_memory,
    "save_decision": save_decision,
    "cursor_apply_patch": cursor_apply_patch,
    "cursor_diagnostics": cursor_diagnostics,
    "cursor_open_files": cursor_open_files,
}


async def dispatch(name: str, args: dict, ctx: ToolContext) -> ToolResult:
    """Run one tool by name, converting every failure mode into a ToolResult.

    The agent loop must always get an observation back; an exception here would
    strand the run instead of letting the model correct itself.
    """
    args = dict(args or {})
    handler = _SYNC_TOOLS.get(name) or _ASYNC_TOOLS.get(name)
    if handler is None:
        known = ", ".join(sorted(TOOL_SPECS_BY_NAME))
        return ToolResult(False, f"Unknown tool '{name}'. Available tools: {known}")

    # Tools that can honour dry_run themselves do so (apply_patch still renders a
    # diff, git_* report what they would do). Arbitrary shell cannot be simulated.
    if ctx.dry_run and name == "run_terminal":
        return ToolResult(True, "Dry run: refused to execute a shell command.")

    try:
        if name in _SYNC_TOOLS:
            return await asyncio.to_thread(handler, ctx, **args)
        return await handler(ctx, **args)
    except SandboxError as exc:
        return ToolResult(False, f"Sandbox violation: {exc}")
    except TypeError as exc:
        spec = TOOL_SPECS_BY_NAME[name]
        return ToolResult(False, f"Bad arguments for '{name}': {exc}. Expected {spec.args}")
    except FileNotFoundError as exc:
        return ToolResult(False, f"Not found: {exc}")
    except Exception as exc:
        logger.exception("Tool '%s' crashed", name)
        return ToolResult(False, f"Tool '{name}' raised {type(exc).__name__}: {exc}")
