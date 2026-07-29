"""The write path: one tool, all-or-nothing.

`apply_patch` takes a list of edits spanning any number of files, validates every
one of them against staged content, and only then writes. If any edit is bad,
nothing is written at all. That is the whole design, and the reason is that a
half-applied multi-file change is worse than a rejected one: the repo no longer
compiles, the agent's next observation is a wall of unrelated errors, and neither
the model nor the user can tell which parts landed.

Staging also means several edits to the same file compose. Each one validates
against the result of the previous, so a rename touching four call sites in one
file works in a single call.

Edits are byte-for-byte search/replace rather than line numbers or diff hunks.
Line numbers go stale the moment an earlier edit shifts them, and unified-diff
context is something models reproduce badly. Exact text either matches or it does
not, and when it does not the error says so and the model re-reads the file.

Text safety matters more here than anywhere else in the codebase, because this is
the only module that can destroy data. Files are decoded strictly, so a file that
is not UTF-8 is refused rather than mangled; line endings are preserved; and the
write itself is atomic, so an interrupted patch cannot truncate a file.
"""

import difflib
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from skippy_sandbox import (
    MAX_TOOL_OUTPUT_CHARS,
    Sandbox,
    SandboxError,
    ToolResult,
    cap_text,
)

logger = logging.getLogger("skippy_edit")

MAX_DIFF_CHARS = MAX_TOOL_OUTPUT_CHARS
# Same ceiling as read_file. A file too large to show the model is also a file it
# has no business rewriting wholesale.
MAX_EDIT_BYTES = 8 * 1024 * 1024

VALID_ACTIONS = ("edit", "create", "delete")


class PatchError(Exception):
    """A staged file could not be read as editable text."""


@dataclass
class _PlannedWrite:
    path: str
    action: str
    before: Optional[str]  # None means the file did not exist
    after: Optional[str]   # None means delete
    newline: str


def _decode(path: str) -> Tuple[str, str]:
    """Read a file as text, or refuse it. Returns (normalized_text, newline).

    Strict UTF-8, deliberately. Lineage B read with ``errors="replace"`` and wrote
    the result back, which silently destroys every byte it could not decode — a
    latin-1 copyright sign in an old C header becomes U+FFFD and the original byte
    is gone. Refusing to edit a file is recoverable; corrupting it is not.

    Line endings are normalized to \\n for matching and the original style is
    returned so the write can restore it. Without this, editing one word in a CRLF
    file rewrites every line in it, producing a whole-file diff that buries the
    actual change and shows up as noise in review.
    """
    size = os.path.getsize(path)
    if size > MAX_EDIT_BYTES:
        raise PatchError(
            f"file is {size / 1_048_576:.1f}MB, over the "
            f"{MAX_EDIT_BYTES // 1_048_576}MB edit limit"
        )

    with open(path, "rb") as handle:
        raw = handle.read()

    if b"\x00" in raw[:8192]:
        raise PatchError("file looks binary (NUL byte); refusing to rewrite it as text")

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchError(
            f"file is not valid UTF-8 (byte {exc.start} of {len(raw)}); refusing to "
            "rewrite it, because decoding it loosely would destroy the bytes it "
            "cannot represent"
        ) from None

    # CRLF wins only if it is the dominant style, so a stray \r\n in an LF file
    # does not flip the whole file over on the next edit.
    crlf = text.count("\r\n")
    lf_only = text.count("\n") - crlf
    newline = "\r\n" if crlf > lf_only else "\n"
    return text.replace("\r\n", "\n"), newline


def _encode(text: str, newline: str) -> bytes:
    if newline == "\r\n":
        text = text.replace("\n", "\r\n")
    return text.encode("utf-8")


def _replace_nth(text: str, search: str, replace: str, which: int) -> str:
    """Replace the Nth *non-overlapping* occurrence, 1-based.

    The step of len(search) is what makes this agree with `str.count` and
    `str.replace`, both of which are non-overlapping. Lineage B advanced by one
    character, so for search "aa" in "aaaa" it accepted occurrence=2 (count says
    there are 2) and then edited the span at index 1 — a match that overlaps the
    first one and is not the one the model asked for.
    """
    position = -1
    for _ in range(which):
        position = text.find(search, position + 1 if position < 0 else position + len(search))
        if position == -1:
            raise PatchError(f"occurrence {which} of the search text disappeared while staging")
    return text[:position] + replace + text[position + len(search):]


def apply_patch(
    sandbox: Sandbox,
    edits: Sequence[dict],
    dry_run: bool = False,
    journal_dir: Optional[str] = None,
) -> ToolResult:
    """Validate every edit against staged content, then write all of them or none.

    dry_run returns the same diff without touching the disk, so the agent can
    check its own work before committing to it.
    """
    if not edits or not isinstance(edits, (list, tuple)):
        return ToolResult(False, "apply_patch requires a non-empty 'edits' list.")

    staged: Dict[str, Optional[str]] = {}
    original: Dict[str, Optional[str]] = {}
    newlines: Dict[str, str] = {}
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
            target = sandbox.resolve(raw_path)
        except SandboxError as exc:
            problems.append(f"edit {index} ({raw_path}): {exc}")
            continue

        where = f"edit {index} ({raw_path})"

        # Stage the file once, on first mention, so later edits to the same file
        # see the earlier ones.
        if target not in staged:
            if os.path.isdir(target):
                problems.append(f"{where}: path is a directory")
                continue
            existing = None
            newline = "\n"
            if os.path.isfile(target):
                try:
                    existing, newline = _decode(target)
                except (PatchError, OSError) as exc:
                    problems.append(f"{where}: {exc}")
                    continue
            staged[target] = existing
            original[target] = existing
            newlines[target] = newline
            order.append(target)

        current = staged[target]

        if action == "create":
            content = edit.get("content")
            if content is None:
                problems.append(f"{where}: 'create' requires 'content'")
                continue
            if current is not None and not edit.get("overwrite"):
                problems.append(
                    f"{where}: file already exists; use action 'edit', or pass "
                    '"overwrite": true to replace it wholesale'
                )
                continue
            staged[target] = str(content).replace("\r\n", "\n")
            actions[target] = "create" if original[target] is None else "overwrite"

        elif action == "delete":
            if current is None:
                problems.append(f"{where}: cannot delete, file does not exist")
                continue
            staged[target] = None
            actions[target] = "delete"

        elif action == "edit":
            if current is None:
                problems.append(f"{where}: file does not exist; use action 'create'")
                continue
            search = edit.get("search")
            if not search:
                problems.append(f"{where}: 'edit' requires a non-empty 'search'")
                continue
            replace = edit.get("replace")
            if replace is None:
                problems.append(f"{where}: 'edit' requires 'replace' (use \"\" to delete the text)")
                continue

            # Normalized the same way the file was, so a model that echoes back
            # CRLF text it read from a CRLF file still matches.
            search = str(search).replace("\r\n", "\n")
            replace = str(replace).replace("\r\n", "\n")

            if edit.get("replace_all") and edit.get("occurrence") is not None:
                problems.append(f"{where}: pass either 'replace_all' or 'occurrence', not both")
                continue

            hits = current.count(search)
            if hits == 0:
                problems.append(
                    f"{where}: 'search' text not found. It must match the file byte-for-byte "
                    "including indentation and blank lines; re-read the file and retry"
                )
                continue

            try:
                if edit.get("replace_all"):
                    staged[target] = current.replace(search, replace)
                elif edit.get("occurrence") is not None:
                    try:
                        which = int(edit["occurrence"])
                    except (TypeError, ValueError):
                        problems.append(f"{where}: 'occurrence' must be an integer")
                        continue
                    if which < 1 or which > hits:
                        problems.append(
                            f"{where}: 'occurrence' {which} is out of range; found {hits}"
                        )
                        continue
                    staged[target] = _replace_nth(current, search, replace, which)
                elif hits > 1:
                    problems.append(
                        f"{where}: 'search' matched {hits} times. Include more surrounding "
                        'context to make it unique, or pass "replace_all": true or '
                        '"occurrence": <n>'
                    )
                    continue
                else:
                    staged[target] = current.replace(search, replace, 1)
            except PatchError as exc:
                problems.append(f"{where}: {exc}")
                continue

            actions.setdefault(target, "edit")

        else:
            problems.append(
                f"{where}: unknown action '{action}'. Valid actions: {', '.join(VALID_ACTIONS)}"
            )

    if problems:
        # Every problem at once, not just the first, so the model can fix a batch in
        # one more turn instead of one per round trip.
        return ToolResult(
            False,
            f"Patch rejected; {len(problems)} problem(s) found and nothing was written.",
            "\n".join(problems),
            {"problems": problems},
        )

    planned = [
        _PlannedWrite(path, actions.get(path, "edit"), original[path], staged[path], newlines[path])
        for path in order
        if original[path] != staged[path]
    ]
    if not planned:
        return ToolResult(
            True, "Patch was a no-op; every file already had the requested content.", "",
            {"files": []},
        )

    diff, file_reports = _render_diff(sandbox, planned)

    if dry_run:
        return ToolResult(
            True,
            f"Dry run: {len(planned)} file(s) would change. Nothing was written.",
            diff,
            {"files": file_reports, "diff": diff, "dry_run": True},
        )

    journal = _open_journal(journal_dir, planned, sandbox) if journal_dir else None

    written: List[str] = []
    failed_on: Optional[str] = None
    try:
        for item in planned:
            failed_on = item.path
            if item.after is None:
                os.remove(item.path)
            else:
                _atomic_write(item.path, _encode(item.after, item.newline))
            written.append(item.path)
    except Exception as exc:
        restored, unrestored = _rollback(planned, written)
        if unrestored:
            # The repo is now in a mixed state. Say so plainly and point at the
            # pre-images, because this is the one failure the user has to act on.
            return ToolResult(
                False,
                f"Write failed on {sandbox.relative(failed_on)} AND rollback failed for "
                f"{len(unrestored)} file(s). The workspace is in a mixed state. "
                + (f"Pre-images are in {journal}." if journal else "No journal was configured."),
                f"{exc}\n\nNot restored:\n" + "\n".join(unrestored),
                {"mixed_state": True, "unrestored": unrestored, "journal": journal},
            )
        return ToolResult(
            False,
            f"Write failed on {sandbox.relative(failed_on)}; rolled back {restored} file(s), "
            "so the workspace is unchanged.",
            str(exc),
            {"rolled_back": restored},
        )

    summary = ", ".join(
        f"{r['path']} (+{r['added']}/-{r['removed']})" for r in file_reports
    )
    return ToolResult(
        True,
        f"Applied {len(planned)} file change(s): {summary}",
        diff,
        {"files": file_reports, "diff": diff, "journal": journal},
    )


def _render_diff(sandbox: Sandbox, planned: Sequence[_PlannedWrite]) -> Tuple[str, List[dict]]:
    """A unified diff plus per-file line counts.

    The diff is what the user reviews and what the agent re-reads to confirm the
    change landed the way it intended, so it is generated from the staged content
    rather than by re-reading the disk.
    """
    chunks: List[str] = []
    reports: List[dict] = []
    for item in planned:
        rel = sandbox.relative(item.path)
        chunk = list(
            difflib.unified_diff(
                (item.before or "").splitlines(keepends=True),
                (item.after or "").splitlines(keepends=True),
                fromfile=f"a/{rel}" if item.before is not None else "/dev/null",
                tofile=f"b/{rel}" if item.after is not None else "/dev/null",
                n=3,
            )
        )
        # A file whose last line has no trailing newline would otherwise splice
        # into the next hunk header and produce an unreadable diff.
        chunks.append("".join(line if line.endswith("\n") else line + "\n" for line in chunk))
        reports.append({
            "path": rel,
            "action": item.action,
            "added": sum(1 for l in chunk if l.startswith("+") and not l.startswith("+++")),
            "removed": sum(1 for l in chunk if l.startswith("-") and not l.startswith("---")),
        })
    return cap_text("".join(chunks), MAX_DIFF_CHARS), reports


def _atomic_write(path: str, payload: bytes) -> None:
    """Write via a temp file in the same directory, then rename over the target.

    os.replace is atomic within a filesystem, so an interrupted write leaves the
    original intact rather than a truncated file. The temp file has to be in the
    same directory for that to hold, which is why this cannot use /tmp.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    mode = os.stat(path).st_mode if os.path.exists(path) else None

    handle, temp_path = tempfile.mkstemp(dir=directory, prefix=".skippy-", suffix=".tmp")
    try:
        with os.fdopen(handle, "wb") as writer:
            writer.write(payload)
            writer.flush()
            os.fsync(writer.fileno())
        if mode is not None:
            # Preserved so patching an executable script does not silently
            # un-executable it.
            os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise


def _open_journal(
    journal_dir: str, planned: Sequence[_PlannedWrite], sandbox: Sandbox
) -> Optional[str]:
    """Save pre-images before writing, in a directory that says how to use them.

    Lineage B wrote these too, but nothing ever read them: rollback worked from
    memory and no code or tool consumed the manifest, so they were an artifact
    that looked like a safety net without being one. They are kept here because
    they cover what in-memory rollback cannot — a crash or a kill signal partway
    through — but only on the condition that the manifest records absolute paths
    and ships a restore command, so recovery is a real procedure rather than a
    directory of orphaned .orig files.
    """
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target_dir = os.path.join(journal_dir, stamp)
        os.makedirs(target_dir, exist_ok=True)

        entries = []
        for index, item in enumerate(planned):
            if item.before is None:
                # Nothing to restore; recovery is to delete the created file.
                entries.append({
                    "path": item.path, "action": item.action, "pre_image": None,
                })
                continue
            name = f"{index:03d}_{os.path.basename(item.path)}.orig"
            with open(os.path.join(target_dir, name), "wb") as handle:
                handle.write(_encode(item.before, item.newline))
            entries.append({"path": item.path, "action": item.action, "pre_image": name})

        manifest = {
            "created": stamp,
            "roots": sandbox.roots,
            "restore": (
                "For each entry with a pre_image, copy it back over 'path'. For entries "
                "with pre_image null, delete 'path' — it did not exist before the patch."
            ),
            "files": entries,
        }
        with open(os.path.join(target_dir, "manifest.json"), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2)
        return target_dir
    except OSError as exc:
        # A journal that cannot be written must not block the patch; the in-memory
        # rollback is the primary guarantee and it is unaffected.
        logger.warning("Could not write patch journal to %s: %s", journal_dir, exc)
        return None


def _rollback(planned: Sequence[_PlannedWrite], written: Sequence[str]) -> Tuple[int, List[str]]:
    """Undo the writes that already landed. Returns (restored, unrestored_paths)."""
    by_path = {item.path: item for item in planned}
    restored, unrestored = 0, []
    for path in written:
        item = by_path[path]
        try:
            if item.before is None:
                if os.path.exists(path):
                    os.remove(path)
            else:
                _atomic_write(path, _encode(item.before, item.newline))
            restored += 1
        except Exception as exc:
            logger.error("Rollback failed for %s: %s", path, exc)
            unrestored.append(f"{path}: {exc}")
    return restored, unrestored
