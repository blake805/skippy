"""Running commands: test runners, linters, builds, read-only git.

This closes the loop. Up to here the agent could write code but not find out
whether it works, so its only verification was re-reading what it had just written.
Watching a live run make that gap concrete — it wrote a scratch verification script,
realised it had no way to execute it, and deleted it a step later — is what
motivated this module.

## What the allowlist is, and what it is not

It is worth being exact, because the natural assumption is wrong.

`pytest` imports `conftest.py` and executes its module body before collecting a
single test. The agent can write `conftest.py`. Therefore "the agent may run pytest"
and "the agent may execute arbitrary code" are the same permission, and no list of
allowed program names changes that. The same is true of `npm test` (package.json
scripts), `cargo test` (build.rs), and `make test` (the Makefile).

So the allowlist here is **accident prevention, not containment.** It exists so that
a model which has misread a situation cannot casually run `rm -rf`, `git push
--force`, or `curl | sh` — mistakes that are plausible, easy to make, and hard to
undo. It does not, and cannot, stop a determined or prompt-injected agent.

Two things follow from that. There is no arbitrary-shell tool here, not even an
approval-gated one: asking permission for the loud path while the quiet path is
already open is theatre, and a prompt that appears often enough gets a reflexive
yes. And real containment is not a code change in this file — it needs the whole run
inside a VM or container, which is a deployment decision.

What this module *does* guarantee is narrower and still worth having: no shell
interpretation, a bounded runtime, bounded output, no orphaned processes, and no
secrets handed to whatever the repo's test suite decides to do.
"""

import asyncio
import logging
import os
import re
import shlex
import signal
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set

from skippy_sandbox import Sandbox, ToolResult, cap_text

logger = logging.getLogger("skippy_exec")

DEFAULT_TIMEOUT = 300.0
MAX_TIMEOUT = 1800.0
MAX_OUTPUT_BYTES = 40_000

# Rejected outright rather than passed through as literal argv entries. Nothing is
# interpreted by a shell here, so `pytest && rm -rf /` would run pytest with "&&"
# and "rm" as filenames — safe, but baffling to debug. Better to say why.
# Longest first, so `&&` is reported rather than `&`.
SHELL_OPERATORS = ("&&", "||", ">>", "$(", "${", ";", "|", "`", ">", "<", "&")

# git subcommands that only read. Note that some of these still have destructive
# flags, which is what GIT_FORBIDDEN_FLAGS is for: `git branch -D` deletes.
GIT_READ_ONLY = {
    "status", "diff", "log", "show", "rev-parse", "ls-files", "blame",
    "describe", "shortlog", "branch", "tag", "remote", "config", "stash",
}
GIT_FORBIDDEN_FLAGS = {
    "-d", "-D", "--delete", "--force", "-f", "--hard", "-m", "-M",
    "--prune", "--unset", "--replace-all", "--add", "--global", "--system",
}
# Subcommands that read or write depending on how they are called, so one of these
# tokens has to be present to prove the read intent. `git stash` bare stashes the
# agent's own uncommitted work; `git config x y` writes.
GIT_REQUIRED_TOKENS = {
    "stash": {"list", "show"},
    "remote": {"-v", "--verbose", "show", "get-url"},
    "config": {"--get", "--get-all", "--list", "-l"},
}

# python -m <module> for verification modules only. `python -c` is excluded: not
# because it is more dangerous than pytest — it is not — but because a script in the
# repo is journaled, reviewable and re-runnable, while inline code leaves no trace
# of what was executed.
_PYTHON_NAME = re.compile(r"^python(\d+(\.\d+)?)?$")

PYTHON_MODULES = {
    "pytest", "unittest", "mypy", "ruff", "black", "flake8", "pyflakes",
    "compileall", "json.tool", "http.server", "pip",
}


@dataclass(frozen=True)
class Rule:
    """What a program is allowed to be asked to do."""

    subcommands: Optional[Set[str]] = None  # None means any first argument
    forbidden_flags: Set[str] = field(default_factory=set)
    note: str = ""


_RULES: Dict[str, Rule] = {
    # Test runners and task tools. Each of these can execute repo-defined code by
    # design; see the module docstring.
    "pytest": Rule(),
    "tox": Rule(),
    "nox": Rule(),
    "make": Rule(subcommands={"test", "check", "build", "all", "lint", "fmt", "format"}),
    "cargo": Rule(subcommands={"test", "build", "check", "clippy", "fmt", "tree"}),
    "go": Rule(subcommands={"test", "build", "vet", "fmt", "list"}),
    "swift": Rule(subcommands={"test", "build"}),
    "npm": Rule(subcommands={"test", "run", "ls", "audit"}),
    "yarn": Rule(subcommands={"test", "run"}),
    "pnpm": Rule(subcommands={"test", "run"}),
    "node": Rule(),
    "python": Rule(),  # covers python3 and python3.N via _PYTHON_NAME
    # Linters and type checkers: the cheapest useful feedback there is.
    "ruff": Rule(),
    "mypy": Rule(),
    "black": Rule(),
    "flake8": Rule(),
    "pyflakes": Rule(),
    "eslint": Rule(),
    "prettier": Rule(),
    "tsc": Rule(),
    "cargo-clippy": Rule(),
    "gofmt": Rule(),
    "swiftformat": Rule(),
    "shellcheck": Rule(),
    # Read-only git. Writes are deliberately absent: the agent already has an
    # atomic write path with a journal, and letting it commit or check out is a
    # separate decision that belongs with session checkpointing.
    "git": Rule(subcommands=GIT_READ_ONLY, forbidden_flags=GIT_FORBIDDEN_FLAGS),
}

# Environment is allowlisted, not filtered. A denylist of secret-looking names is a
# guessing game, and the code that inherits this is the repo's own test suite —
# which, when reverse-engineering someone else's project, is not code to hand an
# API key to.
ENV_PASSTHROUGH = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "TMPDIR", "TZ",
    "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "CONDA_PREFIX",
    "NODE_PATH", "NVM_DIR",
    "CARGO_HOME", "RUSTUP_HOME", "GOPATH", "GOROOT", "GOCACHE", "GOMODCACHE",
    "JAVA_HOME", "SDKROOT", "DEVELOPER_DIR", "TOOLCHAINS",
)


class CommandRejected(Exception):
    """The command is not something the agent is allowed to ask for."""


def extra_commands() -> Set[str]:
    """Programs the user has opted into, via SKIPPY_EXTRA_COMMANDS.

    The default list covers verification. Anything beyond it — installers, package
    managers that fetch, deployment tools — is the user's call to make explicitly
    rather than something inherited from a default.
    """
    raw = os.environ.get("SKIPPY_EXTRA_COMMANDS", "").strip()
    return {part.strip() for part in raw.split(",") if part.strip()}


def allowed_programs() -> List[str]:
    return sorted(set(_RULES) | extra_commands())


def _unquoted_operator(command: str) -> Optional[str]:
    """The first shell operator appearing outside quotes, or None.

    Quote-aware on purpose. A plain substring scan would reject
    `pytest -k "slow; fast"`, where the semicolon is a legitimate argument, while
    matching only whole tokens would miss `pytest; rm -rf /` — shlex splits that into
    `pytest;` as one token, so the operator hides inside the program name.

    Nothing here is a safety check. There is no shell, so `$(whoami)` reaches the
    program as four literal characters. The point is that a model which believes it
    has a shell gets told so, instead of a baffling error from pytest about a file
    called `rm`.
    """
    quote: Optional[str] = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote:
            if char == quote:
                quote = None
            index += 1
            continue
        if char in ("'", '"'):
            quote = char
            index += 1
            continue
        for operator in SHELL_OPERATORS:
            if command.startswith(operator, index):
                return operator
        index += 1
    return None


def validate(command: str) -> List[str]:
    """Parse and check a command, returning argv. Raises CommandRejected."""
    if not command or not command.strip():
        raise CommandRejected("No command given.")

    if "\n" in command:
        raise CommandRejected("One command per call; this looks like a script.")

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise CommandRejected(f"Could not parse the command ({exc}).") from None
    if not argv:
        raise CommandRejected("No command given.")

    found = _unquoted_operator(command)
    if found:
        raise CommandRejected(
            f"Commands are run directly, not through a shell, so '{found}' has no meaning "
            "here. Run one program per call."
        )

    program = os.path.basename(argv[0])
    if _PYTHON_NAME.match(program):
        # python, python3, python3.11, python3.13 — all the same program with the same
        # rules. Listing only the unversioned names looked fine until a real invocation
        # arrived as `python3.11 -m pytest`, which is exactly how `sys.executable`
        # spells itself inside a virtualenv.
        program = "python"

    if os.path.basename(argv[0]) != argv[0]:
        # An absolute or relative path would sidestep the allowlist entirely
        # (./pytest, /usr/bin/env, ../../bin/sh).
        raise CommandRejected(
            f"Give the program by name, not by path. Received '{argv[0]}'."
        )

    if program in extra_commands():
        return argv

    rule = _RULES.get(program)
    if rule is None:
        raise CommandRejected(
            f"'{program}' is not an allowed program. Allowed: {', '.join(allowed_programs())}. "
            "This list is for running tests, linters and builds; if you need something "
            "else, say so in your finish summary instead."
        )

    args = argv[1:]

    if program == "python":
        _validate_python(args)
        return argv

    if rule.subcommands is not None:
        subcommand = next((a for a in args if not a.startswith("-")), None)
        if subcommand is None:
            if program == "git":
                raise CommandRejected("git needs a subcommand, e.g. `git status`.")
        elif subcommand not in rule.subcommands:
            raise CommandRejected(
                f"'{program} {subcommand}' is not allowed. Allowed subcommands: "
                f"{', '.join(sorted(rule.subcommands))}."
            )
        elif program == "git" and subcommand in GIT_REQUIRED_TOKENS:
            required = GIT_REQUIRED_TOKENS[subcommand]
            if not required & set(args):
                raise CommandRejected(
                    f"'git {subcommand}' can write, so it needs one of "
                    + ", ".join(sorted(required))
                    + f" to be read-only. E.g. `git {subcommand} {sorted(required)[0]}`."
                )

    for flag in args:
        if flag in rule.forbidden_flags:
            raise CommandRejected(
                f"'{flag}' can destroy work, so it is not allowed with {program}."
            )

    return argv


def _validate_python(args: Sequence[str]) -> None:
    if not args:
        raise CommandRejected("Bare `python` would open an interactive prompt that nothing can answer.")

    if "-c" in args:
        raise CommandRejected(
            "`python -c` is not allowed. Write the script into the workspace with "
            "apply_patch and run it by filename, so what executed is on disk and reviewable."
        )
    if "-" in args:
        raise CommandRejected("Reading a program from stdin is not allowed; use a file.")

    if args[0] == "-m":
        if len(args) < 2:
            raise CommandRejected("`python -m` needs a module name.")
        module = args[1]
        if module not in PYTHON_MODULES:
            raise CommandRejected(
                f"`python -m {module}` is not allowed. Allowed modules: "
                f"{', '.join(sorted(PYTHON_MODULES))}."
            )
        return

    # Otherwise it must be running a file, which the sandbox check on cwd plus the
    # path check below keeps inside the workspace.
    if args[0].startswith("-"):
        raise CommandRejected(
            f"'{args[0]}' is not an allowed python flag. Use `python -m <module>` or "
            "`python <script.py>`."
        )


def _child_env() -> Dict[str, str]:
    env = {name: os.environ[name] for name in ENV_PASSTHROUGH if name in os.environ}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    # Otherwise a git operation needing credentials blocks forever on a prompt that
    # no one is there to answer, and the tool just times out with no explanation.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PYTHONUNBUFFERED"] = "1"
    # Test suites that detect CI often disable interactive progress bars, which
    # keeps output readable and smaller.
    env["CI"] = "1"
    return env


class _Output:
    """Head and tail of a stream, with the middle dropped.

    Both ends matter: the head carries the command's own banner and the tail carries
    the failure summary, which is the part that answers "did my change work".

    Kept as an object rather than a return value so that output survives the reader
    task being cancelled. A command that times out has usually printed the most
    interesting thing it will ever print.
    """

    def __init__(self, limit: int):
        self.keep = max(limit // 2, 1)
        self.head = bytearray()
        self.tail = bytearray()
        self.truncated = False

    def feed(self, chunk: bytes) -> None:
        if len(self.head) < self.keep:
            room = self.keep - len(self.head)
            self.head += chunk[:room]
            chunk = chunk[room:]
        if chunk:
            self.tail += chunk
            if len(self.tail) > self.keep:
                self.truncated = True
                del self.tail[:-self.keep]

    def text(self) -> str:
        joiner = b"\n\n... [output truncated] ...\n\n" if self.truncated else b""
        return (bytes(self.head) + joiner + bytes(self.tail)).decode("utf-8", errors="replace")


async def _drain(stream, into: _Output) -> None:
    """Read until EOF, always. A full pipe would block the child forever, so an
    unexpectedly chatty command has to be drained even when its output is discarded."""
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return
        into.feed(chunk)


async def run_command(
    sandbox: Sandbox,
    command: str,
    cwd: Optional[str] = None,
    timeout: Optional[float] = None,
) -> ToolResult:
    """Run one allowlisted program in the workspace and return its output."""
    try:
        argv = validate(command)
    except CommandRejected as exc:
        return ToolResult(False, str(exc))

    if cwd:
        workdir = sandbox.resolve(cwd, must_exist=True)
        if not os.path.isdir(workdir):
            return ToolResult(False, f"cwd '{cwd}' is not a directory.")
    elif len(sandbox.roots) > 1:
        # With several roots there is no sensible default, and silently picking the
        # first one means running a test suite in the wrong repository and reporting
        # the result as if it answered the question.
        return ToolResult(
            False,
            "There are several workspace roots, so 'cwd' is required. Roots: "
            + ", ".join(sandbox.relative(r) for r in sandbox.roots),
        )
    else:
        workdir = sandbox.primary

    # Only None means "use the default"; an explicit 0 is a mistake, not a request
    # for five minutes. Same trap as max_steps in the agent loop.
    requested = DEFAULT_TIMEOUT if timeout is None else float(timeout)
    if requested <= 0:
        return ToolResult(False, "timeout must be a positive number of seconds.")
    limit = min(requested, MAX_TIMEOUT)

    try:
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workdir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=_child_env(),
            stdin=asyncio.subprocess.DEVNULL,
            # Its own process group, so a timeout can kill the whole tree. Without
            # this, killing `npm test` leaves node running and killing pytest leaves
            # whatever it spawned holding the port it bound.
            start_new_session=True,
        )
    except FileNotFoundError:
        return ToolResult(
            False,
            f"'{argv[0]}' is allowed but is not installed on this machine.",
        )
    except OSError as exc:
        return ToolResult(False, f"Could not start '{argv[0]}': {exc}")

    collected = _Output(MAX_OUTPUT_BYTES)
    reader = asyncio.ensure_future(_drain(process.stdout, collected))
    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=limit)
    except asyncio.TimeoutError:
        timed_out = True
        await _terminate_tree(process)

    # Bounded, because the pipe stays open as long as *anything* holds the write end.
    # A grandchild that escaped the kill would otherwise make this await outlast the
    # timeout it is supposed to enforce — which was a real bug here, found by
    # mutation testing: with the process-group kill removed, a timed-out command
    # still blocked for the full runtime of its orphan.
    if not reader.done():
        try:
            await asyncio.wait_for(asyncio.shield(reader), timeout=5.0)
        except asyncio.TimeoutError:
            reader.cancel()
            logger.warning(
                "Output pipe for `%s` stayed open after the process exited; "
                "something it spawned is still running.", command,
            )
    output = collected.text()
    truncated = collected.truncated
    where = sandbox.relative(workdir)

    if timed_out:
        # Partial output is the useful part of a timeout: it shows how far the
        # command got and often which test is hanging.
        return ToolResult(
            False,
            f"`{command}` timed out after {limit:g}s in {where} and was killed.",
            output,
            {"command": command, "timed_out": True, "cwd": where},
        )

    code = process.returncode
    note = " [output truncated]" if truncated else ""
    return ToolResult(
        code == 0,
        f"`{command}` exited {code} in {where}.{note}",
        cap_text(output, MAX_OUTPUT_BYTES) or "(no output)",
        {"command": command, "exit_code": code, "cwd": where, "truncated": truncated},
    )


async def _terminate_tree(process) -> None:
    """SIGTERM the child's process group, then SIGKILL what is left.

    Refuses to signal our own group. `start_new_session=True` above is what puts the
    child in a group of its own, and if that ever stops holding, the child shares
    Skippy's group and `killpg` would take down the agent, the server and everything
    else in it. Mutation testing demonstrated this: with `start_new_session` removed,
    the timeout test killed the test runner and the surrounding shell rather than
    reporting a failure. A guard is cheap, and the alternative failure is severe and
    silent.
    """
    try:
        group = os.getpgid(process.pid)
    except ProcessLookupError:
        return

    if group == os.getpgrp():
        logger.error(
            "Child %s shares Skippy's process group; killing only the child so as not "
            "to signal ourselves. Orphaned grandchildren may survive.", process.pid,
        )
        try:
            process.kill()
            await asyncio.wait_for(process.wait(), timeout=5.0)
        except (ProcessLookupError, asyncio.TimeoutError):
            pass
        return

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(group, sig)
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=5.0)
            return
        except asyncio.TimeoutError:
            continue
    logger.error("Process group %s survived SIGKILL.", group)
