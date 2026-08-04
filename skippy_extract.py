"""Carving firmware images, in a container because the extractors cannot be trusted.

Extraction is the one operation in the RE lane that points a stack of format parsers at
a hostile blob and asks them to write files. Everything else here reads. See
[ADR 0019](docs/adr/0019-containerised-extraction.md) for the decision; the reasoning
that shapes this module is below.

**unblob's own defences are good and are not the point.** It has a `FileSystem` API that
confines extractor writes to the extraction directory, it refuses handlers that bypass
it, and it has no auto-loading plugin directory — plugins need an explicit
`--plugins-path`, which is exactly the property whose absence turns binwalk 2.x's path
traversal into remote code execution. None of that covers the ~20 third-party extractor
binaries it drives. `7z`, `sasquatch`, `jefferson` and `ubireader` are separate
processes, and unblob's own site lists path traversals it had to fix in two of them plus
an integer overflow in Yara. On Linux, Landlock covers those subprocesses. On macOS
`restrict_access` raises and unblob logs "Sandboxing FS access is unavailable on this
system, skipping" — so the layer that covers the risky population is the layer we would
not have.

**So extraction never runs on the host.** On macOS a container is not a weaker substitute
for the VM boundary ADR 0012 identified: Podman runs a Linux VM, so containerising buys
the VM boundary, switches Landlock back on by supplying a Linux kernel, and provides the
extractors, which are not packaged for macOS anyway. Host-side extraction would give up
all three to save a dependency.

**The likelier hazard is not code execution.** It is a 4 KB file that expands to 200 GB,
or a recursion that never bottoms out. Memory, process count, recursion depth and total
output size are all capped, and the last of those needs a watchdog because no container
flag bounds the size of a bind-mounted write.

**Output is evidence, and lands in the pack.** Never a workspace root — that is somewhere
Skippy writes code — and never a path the model chooses.
"""

import asyncio
import json
import logging
import os
import shlex
import shutil
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from skippy_sandbox import ToolResult, cap_text

logger = logging.getLogger("skippy_extract")

# Pinned by digest, not by tag. unblob's own docs recommend `--pull always` because the
# project moves fast, which is the right advice for a person at a terminal and the wrong
# property for a tool whose output becomes recorded evidence: a finding that cites an
# extraction should be reproducible, and `:latest` means the image that produced it is
# already gone. Updating this constant is a deliberate act with a diff.
UNBLOB_IMAGE = (
    "ghcr.io/onekey-sec/unblob"
    "@sha256:fbd5e0652a72cadfb63e65b236f52494b83a98261dc75d07d7c62eb645d945ea"
)

# Podman first, deliberately: rootless and daemonless, so there is no root daemon on the
# host to be part of the attack surface. The others are accepted because refusing a
# working runtime to make a point would only mean extraction never happens.
RUNTIMES = ("podman", "docker", "nerdctl")
RUNTIME_ENV = "SKIPPY_CONTAINER_RUNTIME"

# Caps. Each of these is the answer to a specific way extraction goes wrong rather than a
# round number: a decompression bomb (output size), a recursive container (depth), a
# parser that allocates on a length field it read from the file (memory), and a fork bomb
# in an extractor (pids).
MAX_OUTPUT_BYTES = 4 * 1024 * 1024 * 1024
DEFAULT_DEPTH = 4
MAX_DEPTH = 10
CONTAINER_MEMORY = "2g"
CONTAINER_CPUS = "2"
CONTAINER_PIDS = "512"
DEFAULT_TIMEOUT = 900.0
MAX_TIMEOUT = 3600.0

# How often the watchdog measures the output tree. Frequent enough that a fast bomb is
# stopped before it fills the disk, infrequent enough that walking the tree is not itself
# the load.
WATCHDOG_INTERVAL = 2.0

# unblob writes its machine-readable report here, inside the output mount because that is
# the only writable path the container has. Excluded from the extracted tree everywhere,
# since it is our instrumentation rather than something that came out of the image.
REPORT_NAME = "unblob-report.json"

MAX_TREE_ENTRIES = 300
MAX_SUMMARY_CHARS = 12_000


class ExtractError(Exception):
    """Extraction could not be attempted, or could not be trusted."""


def runtime_path() -> str:
    """The container runtime to use, or raise with what to install.

    Honours an environment override so a machine with a runtime somewhere unusual is
    usable, and is otherwise a fixed preference order rather than anything the model
    influences.
    """
    override = os.environ.get(RUNTIME_ENV)
    if override:
        found = shutil.which(override) or (override if os.path.isabs(override) else "")
        if found:
            return found
        raise ExtractError(f"{RUNTIME_ENV} names '{override}', which is not executable.")
    for name in RUNTIMES:
        found = shutil.which(name)
        if found:
            return found
    raise ExtractError(
        "No container runtime found, so extraction is unavailable. Extraction is not "
        "run on the host by design — see ADR 0019 — because unblob's sandboxing of "
        "third-party extractors is Linux-only. Install one with `brew install podman` "
        "and start its VM with `podman machine start`."
    )


def available() -> bool:
    try:
        runtime_path()
    except ExtractError:
        return False
    return True


def _is_podman(runtime: str) -> bool:
    return os.path.basename(runtime).startswith("podman")


def container_argv(
    runtime: str,
    input_dir: str,
    input_name: str,
    output_dir: str,
    name: str,
    depth: int = DEFAULT_DEPTH,
) -> List[str]:
    """The full argument vector, hardened.

    Separated from running it so the safety properties can be asserted by tests without
    a runtime present, which matters because these flags are the containment and CI has
    no VM. Every one of them is checked in `tests/test_extract.py`.
    """
    argv = [
        runtime, "run", "--rm",
        "--name", name,
        # Extraction has no reason to reach the network, and an extractor that does is
        # either exfiltrating or fetching, both of which we want to fail rather than
        # observe. This also removes the callback half of any code-execution bug.
        "--network", "none",
        # Nothing here needs a capability. An extractor asking for one is asking to do
        # something other than parse a file.
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        # The root filesystem is not writable; the two paths a process legitimately
        # needs are supplied as tmpfs, so a parser that writes outside the extraction
        # directory fails instead of leaving something behind in the image.
        "--read-only",
        "--tmpfs", "/tmp:size=512m,mode=1777",
        "--tmpfs", "/run:size=64m",
        "--memory", CONTAINER_MEMORY,
        # Without this a memory cap just pushes the allocation into swap.
        "--memory-swap", CONTAINER_MEMORY,
        "--cpus", CONTAINER_CPUS,
        "--pids-limit", CONTAINER_PIDS,
        # Read-only input: the artifact under investigation is not ours to modify, and
        # this is the same guarantee the RE lane makes everywhere else.
        "-v", f"{input_dir}:/data/input:ro",
        "-v", f"{output_dir}:/data/output",
    ]

    # So the extracted files come out belonging to us and are readable afterwards.
    # Rootless Podman already maps our uid to the container's root, so keep-id is the
    # way to say it there; Docker needs to be told explicitly.
    if _is_podman(runtime):
        argv += ["--userns", "keep-id"]
    else:
        argv += ["--user", f"{os.getuid()}:{os.getgid()}"]

    argv += [
        # Never `--pull always`: see UNBLOB_IMAGE.
        "--pull", "missing",
        UNBLOB_IMAGE,
        "--extract-dir", "/data/output",
        "--report", f"/data/output/{REPORT_NAME}",
        "--depth", str(depth),
        # Matched to the CPU cap. unblob defaults to one worker per host core, which
        # inside a two-CPU container means a dozen processes contending over two.
        "--process-num", CONTAINER_CPUS,
        f"/data/input/{input_name}",
    ]
    return argv


def _tree_size(root: str) -> int:
    total = 0
    for base, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(base, name)).st_size
            except OSError:
                continue
    return total


async def _watch_output(runtime: str, name: str, output_dir: str, cap: int) -> Optional[str]:
    """Kill the container if the extraction outgrows its cap.

    Needed because no runtime flag bounds writes to a bind mount, and a decompression
    bomb is a likelier hostile payload than a code-execution bug — it needs no
    vulnerability at all, just a compression ratio. Returns the reason if it fired.
    """
    while True:
        await asyncio.sleep(WATCHDOG_INTERVAL)
        try:
            size = await asyncio.to_thread(_tree_size, output_dir)
        except OSError:
            return None
        if size > cap:
            logger.warning("Extraction exceeded %d bytes; killing %s", cap, name)
            try:
                killer = await asyncio.create_subprocess_exec(
                    runtime, "kill", name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            except OSError:
                pass
            return (
                f"Extraction was stopped after writing {size // (1024 * 1024)} MB, over "
                f"the {cap // (1024 * 1024)} MB limit. Either this image is much larger "
                "than expected or something in it expands without bound — a nested "
                "archive that contains itself does this. What was written before the "
                "stop is kept."
            )


async def _run_container(argv: List[str], name: str, output_dir: str, timeout: float
                         ) -> Tuple[int, str, str]:
    """Run one extraction. Returns (exit code, output, watchdog reason)."""
    runtime = argv[0]
    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise ExtractError(f"Could not start '{runtime}': {exc}") from exc
    except OSError as exc:
        raise ExtractError(f"Could not start '{runtime}': {exc}") from exc

    watchdog = asyncio.create_task(_watch_output(runtime, name, output_dir, MAX_OUTPUT_BYTES))
    try:
        out, _ = await asyncio.wait_for(process.communicate(), timeout=timeout)
        stopped = ""
    except asyncio.TimeoutError:
        out = b""
        stopped = (
            f"Extraction did not finish within {timeout:.0f}s and was stopped. Anything "
            "written before the stop is kept; a lower depth may complete."
        )
        await _kill(runtime, name, process)
    finally:
        watchdog.cancel()
        try:
            reason = await watchdog
        except asyncio.CancelledError:
            reason = None

    return process.returncode or 0, (out or b"").decode("utf-8", "replace"), stopped or (reason or "")


async def _kill(runtime: str, name: str, process) -> None:
    try:
        killer = await asyncio.create_subprocess_exec(
            runtime, "kill", name,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    except OSError:
        pass
    try:
        process.kill()
    except ProcessLookupError:
        pass


# --- what came out ---------------------------------------------------------

def walk_quarantine(root: str, limit: int = MAX_TREE_ENTRIES) -> Tuple[List[str], int, int]:
    """The extracted tree as lines, plus (file count, total bytes).

    The filesystem is the ground truth here rather than unblob's report, because it is
    always there — including when a run was killed part way, which is exactly when a
    person most wants to know what was produced.
    """
    lines: List[str] = []
    count = 0
    total = 0
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            if name == REPORT_NAME:
                continue
            full = os.path.join(base, name)
            relative = os.path.relpath(full, root)
            count += 1
            try:
                stat = os.lstat(full)
            except OSError:
                continue
            total += stat.st_size
            if len(lines) < limit:
                kind = "link" if os.path.islink(full) else "file"
                lines.append(f"{stat.st_size:>12,}  {kind}  {relative}")
    return lines, count, total


def read_report(output_dir: str) -> Dict[str, Any]:
    """Pull the useful facts out of unblob's report, defensively.

    Parsed opportunistically rather than depended on: the schema is not ours and the
    tool is under heavy development, so anything unexpected degrades to "no report
    detail" instead of failing an extraction that actually worked.

    Two things are worth lifting out. The formats found, which answers "what is this
    image made of" in a line instead of a file tree. And any path traversal unblob
    blocked, which is not an error to swallow — a firmware image that tries to write
    outside the extraction directory has told us something about itself.
    """
    path = os.path.join(output_dir, REPORT_NAME)
    summary: Dict[str, Any] = {"formats": [], "problems": [], "chunks": 0}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, ValueError):
        return summary

    formats: Dict[str, int] = {}
    problems: List[str] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            handler = node.get("handler_name") or node.get("handler")
            if isinstance(handler, str) and handler:
                formats[handler] = formats.get(handler, 0) + 1
                summary["chunks"] += 1
            severity = str(node.get("severity") or "")
            problem = node.get("problem") or node.get("message")
            if problem and ("traversal" in str(problem).lower() or severity.lower() == "error"):
                text = str(problem)
                if node.get("path"):
                    text = f"{text} ({node['path']})"
                if text not in problems:
                    problems.append(text)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(data)
    summary["formats"] = sorted(formats.items(), key=lambda item: -item[1])
    summary["problems"] = problems[:20]
    return summary


# --- paths, none of them model-chosen --------------------------------------

def quarantine_root(pack: Any) -> str:
    root = getattr(pack, "quarantine_dir", "")
    if not root:
        raise ExtractError("This note pack has no quarantine directory.")
    os.makedirs(root, exist_ok=True)
    return root


def resolve_in_quarantine(pack: Any, path: str) -> str:
    """Turn a model-supplied relative path into a real one inside the quarantine.

    The model does get to name a file here, unlike everywhere else in this module, since
    the whole point of the chain is to read code found inside an image. So the path is
    resolved and then checked to be under the quarantine — after symlink resolution,
    because extraction produces symlinks and one of them pointing at /etc is a thing
    unblob reports rather than something it can always prevent.
    """
    root = os.path.realpath(quarantine_root(pack))
    candidate = str(path or "").strip()
    if not candidate:
        raise ExtractError("A path inside the extracted files is required.")
    if os.path.isabs(candidate):
        resolved = os.path.realpath(candidate)
    else:
        resolved = os.path.realpath(os.path.join(root, candidate))
    if resolved != root and not resolved.startswith(root + os.sep):
        raise ExtractError(
            f"'{candidate}' is outside this pack's extracted files. Paths are relative "
            "to the extraction directory; use list_extracted to see what is there."
        )
    if not os.path.exists(resolved):
        raise ExtractError(
            f"'{candidate}' does not exist in the extracted files. Use list_extracted "
            "to see what came out of the image."
        )
    return resolved


def _next_extraction_dir(root: str, label: str) -> str:
    existing = [name for name in os.listdir(root) if os.path.isdir(os.path.join(root, name))]
    sequence = f"{len(existing) + 1:04d}"
    from skippy_re import slugify

    path = os.path.join(root, f"{sequence}-{slugify(label, 'extraction')}")
    os.makedirs(path, exist_ok=True)
    return path


# --- the tools -------------------------------------------------------------

async def extract_artifact(pack: Any, path: str = "", depth: Optional[int] = None) -> ToolResult:
    """Carve the target, or a file already extracted from it, into the pack's quarantine."""
    try:
        root = quarantine_root(pack)
        if path:
            subject = resolve_in_quarantine(pack, path)
        else:
            subject = str((getattr(pack, "meta", None) or {}).get("target") or "")
            if not subject:
                return ToolResult(False, "This session has no target artifact to extract.")
            subject = os.path.realpath(os.path.expanduser(subject))
        if not os.path.isfile(subject):
            return ToolResult(False, f"'{path or subject}' is not a readable file.")
        runtime = runtime_path()
    except ExtractError as exc:
        return ToolResult(False, str(exc))

    requested = DEFAULT_DEPTH if depth is None else int(depth)
    if requested < 1:
        return ToolResult(False, "depth must be at least 1.")
    bounded = min(requested, MAX_DEPTH)

    output_dir = _next_extraction_dir(root, os.path.basename(subject))
    name = f"skippy-unblob-{uuid.uuid4().hex[:12]}"
    argv = container_argv(
        runtime,
        input_dir=os.path.dirname(subject),
        input_name=os.path.basename(subject),
        output_dir=output_dir,
        name=name,
        depth=bounded,
    )

    started = time.time()
    try:
        code, output, stopped = await _run_container(argv, name, output_dir, DEFAULT_TIMEOUT)
    except ExtractError as exc:
        return ToolResult(False, str(exc))
    elapsed = time.time() - started

    return _present(pack, subject, output_dir, argv, code, output, stopped, elapsed, bounded)


def _present(pack, subject, output_dir, argv, code, output, stopped, elapsed, depth) -> ToolResult:
    lines, count, total = walk_quarantine(output_dir)
    report = read_report(output_dir)
    relative = os.path.relpath(output_dir, quarantine_root(pack))

    body: List[str] = []
    if report["formats"]:
        # The pre-digested answer to "what is this made of", ahead of the file list,
        # because it is usually the whole question.
        body.append("Formats identified: " + ", ".join(
            f"{handler} x{n}" if n > 1 else handler for handler, n in report["formats"][:15]
        ))
    if report["problems"]:
        # Surfaced rather than logged. An image that attempts a path traversal during
        # extraction has said something about itself, and it is very likely a weakness
        # in whatever produced it.
        body.append(
            "\nExtraction problems unblob blocked — worth recording as findings:\n"
            + "\n".join(f"  - {problem}" for problem in report["problems"])
        )
    if lines:
        shown = f" (first {len(lines)} of {count})" if count > len(lines) else ""
        body.append(f"\nExtracted files{shown}:\n" + "\n".join(lines))
    if not count and not report["formats"]:
        tail = output.strip().splitlines()[-8:]
        body.append(
            "Nothing was extracted. unblob recognised no container in this file, which "
            "is itself a finding: it may be raw code, encrypted, or a format unblob "
            "does not handle."
        )
        if tail:
            body.append("\nunblob said:\n" + "\n".join(tail))

    if stopped:
        body.insert(0, f"WARNING: {stopped}\n")

    summary = (
        f"Extracted {os.path.basename(subject)} to quarantine/{relative}: "
        f"{count} file(s), {total // 1024} KB, depth {depth}, {elapsed:.0f}s."
    )
    if stopped:
        summary = f"Extraction of {os.path.basename(subject)} was stopped. " + summary
    elif not count:
        summary = f"No container found in {os.path.basename(subject)}."

    return ToolResult(
        # A file with nothing in it is a true answer, not a failure. Only being unable
        # to try is a failure, and those returned earlier.
        True,
        summary,
        cap_text("\n".join(body), MAX_SUMMARY_CHARS),
        data={
            "quarantine": relative,
            "file_count": count,
            "bytes": total,
            "formats": [handler for handler, _ in report["formats"]],
            "problems": report["problems"],
            "stopped": bool(stopped),
            "exit_code": code,
            "command": shlex.join(argv),
        },
    )


async def list_extracted(pack: Any, path: str = "") -> ToolResult:
    """What is in the pack's quarantine, from earlier extractions in this or any session."""
    try:
        root = quarantine_root(pack)
        base = resolve_in_quarantine(pack, path) if path else root
    except ExtractError as exc:
        return ToolResult(False, str(exc))

    if os.path.isfile(base):
        size = os.path.getsize(base)
        return ToolResult(
            True,
            f"{os.path.relpath(base, root)} is a file of {size:,} bytes.",
            data={"path": os.path.relpath(base, root), "bytes": size},
        )

    lines, count, total = walk_quarantine(base)
    if not count:
        return ToolResult(
            True,
            "Nothing has been extracted into this pack yet. Use extract_artifact to "
            "carve the target if it looks like a container.",
        )
    shown = f" (first {len(lines)} of {count})" if count > len(lines) else ""
    return ToolResult(
        True,
        f"{count} extracted file(s), {total // 1024} KB{shown}.",
        "\n".join(lines),
        data={"file_count": count, "bytes": total},
    )
