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
    # At least one of these must appear. For tools whose default behaviour writes and
    # which only read when asked: bare `unzip` extracts, `unzip -l` lists.
    required_any: Optional[Set[str]] = None
    note: str = ""


CODING_RULES: Dict[str, Rule] = {
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

# Static inspection, for reverse-engineering work. Deliberately contains no
# interpreter, no build tool and no test runner: RE mode must not be able to *run*
# the artifact it is analysing. Dynamic analysis is a real need and a different
# thing, and it needs the VM boundary that ADR 0011 identifies as the only real
# containment — not an entry added to this table.
#
# Several of these read by default and write when asked, which is the trap the
# `forbidden_flags` and `required_any` entries below are for. `codesign -s` signs,
# `lipo -create` writes a new binary, `plutil -convert` rewrites a plist in place,
# and bare `unzip` extracts over the target directory.
INSPECTION_RULES: Dict[str, Rule] = {
    "file": Rule(),
    "strings": Rule(),
    "nm": Rule(),
    "size": Rule(),
    "otool": Rule(),
    "objdump": Rule(),
    "dwarfdump": Rule(),
    "hexdump": Rule(),
    "xxd": Rule(forbidden_flags={"-r", "--revert"}),
    "c++filt": Rule(),
    "swift-demangle": Rule(),
    "lipo": Rule(
        forbidden_flags={
            "-create", "-extract", "-extract_family", "-remove", "-replace",
            "-thin", "-output", "-o",
        },
        required_any={"-info", "-detailed_info", "-archs", "-verify_arch"},
    ),
    "codesign": Rule(
        forbidden_flags={
            "-s", "--sign", "-f", "--force", "--remove-signature", "--deep",
        },
        required_any={"-d", "--display", "-v", "--verify", "--verbose", "-dv", "--entitlements"},
    ),
    "plutil": Rule(
        forbidden_flags={"-convert", "-insert", "-replace", "-remove", "-o"},
        required_any={"-p", "-lint"},
    ),
    "unzip": Rule(required_any={"-l", "-v", "-t", "-p", "-Z"}),
    "tar": Rule(),  # checked by _validate_tar, whose flag clusters need real parsing
    "git": Rule(subcommands=GIT_READ_ONLY, forbidden_flags=GIT_FORBIDDEN_FLAGS),
}

# Research mode runs no commands at all. The empty table is not an oversight and not a
# placeholder: a research run is offered no `run_command` in the first place, and this
# is what makes that hold a second time if one ever reaches the dispatcher. Nothing a
# page said should be able to become a process on this machine.
RESEARCH_RULES: Dict[str, Rule] = {}

# A sub-run answering a question about the code runs nothing either, and for a sharper
# reason than research does: it exists inside another run that is already working on the
# same tree. Two things executing against one working copy with no coordination between
# them is how you get a test result that describes neither state.
INVESTIGATION_RULES: Dict[str, Rule] = {}

MODES = {
    "coding": CODING_RULES,
    "re": INSPECTION_RULES,
    "research": RESEARCH_RULES,
    "investigate": INVESTIGATION_RULES,
}
DEFAULT_MODE = "coding"

# Programs whose exit code says something about whether an edit works. The agent loop
# uses this to tell "I ran the tests" from "I looked at the tests", so that finishing a
# run can require evidence rather than a claim.
#
# `git` is deliberately absent even though it is allowlisted: `git diff` shows what
# changed, which is not the same as showing that it works, and counting it would let a
# run discharge the requirement by reading its own patch back. `python` is present
# because `python -m pytest` is how the suite is usually invoked here, and because
# running a script in the repo is executing the change — which is the whole point.
VERIFICATION_PROGRAMS = frozenset({
    "pytest", "tox", "nox", "make", "cargo", "go", "swift", "npm", "yarn", "pnpm",
    "node", "python", "ruff", "mypy", "black", "flake8", "pyflakes", "eslint",
    "prettier", "tsc", "cargo-clippy", "gofmt", "swiftformat", "shellcheck",
})


def is_verification(command: str) -> bool:
    """True when this command's exit code is evidence about an edit.

    Parsed the same way `validate` parses it — basename, versioned python names folded
    together — so that `python3.11 -m pytest` counts exactly as `pytest` does. Anything
    unparseable is not evidence.
    """
    try:
        argv = shlex.split(str(command or ""))
    except ValueError:
        return False
    if not argv:
        return False
    program = os.path.basename(argv[0])
    if _PYTHON_NAME.match(program):
        program = "python"
    return program in VERIFICATION_PROGRAMS

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


def rules_for(mode: str) -> Dict[str, Rule]:
    table = MODES.get(str(mode or DEFAULT_MODE).lower())
    if table is None:
        raise CommandRejected(
            f"Unknown execution mode '{mode}'. Known modes: {', '.join(sorted(MODES))}."
        )
    return table


def allowed_programs(mode: str = DEFAULT_MODE) -> List[str]:
    return sorted(set(rules_for(mode)) | extra_commands())


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


def _shell_alternative(operator: str) -> str:
    """What to do instead, named for the operator that was used.

    "Run one program per call" is true and useless: it says what is forbidden without
    saying how to get the result. A live RE run spent three of its eighteen steps
    retrying pipes against that message, because nothing in it suggested a different
    approach. A refusal is part of the interface — if the model cannot act on it, it
    retries until the budget is gone.
    """
    if operator in ("|",):
        return (
            "The command's full output is returned to you, so there is nothing to pipe "
            "into: run it without the pipe and read the result. To search inside files "
            "rather than output, use the grep tool."
        )
    if operator in (">", ">>", "<"):
        return (
            "Output is returned to you rather than written to a file, so redirection is "
            "not needed. To create a file, use apply_patch."
        )
    if operator in ("&&", "||", ";", "\n"):
        return "Make one call per program; you will see each result before deciding the next."
    if operator in ("`", "$("):
        return (
            "Substitute the value yourself: run the inner command in its own call, read "
            "the result, then use it in the next command."
        )
    if operator == "&":
        return "Everything runs in the foreground and returns when it finishes."
    return "Run one program per call."


def validate(command: str, mode: str = DEFAULT_MODE) -> List[str]:
    """Parse and check a command, returning argv. Raises CommandRejected."""
    table = rules_for(mode)
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
            f"here. {_shell_alternative(found)}"
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

    rule = table.get(program)
    if rule is None:
        if mode == "re":
            purpose = (
                "This list is for static inspection of an artifact; nothing here can run "
                "it. Running the target is dynamic analysis, which needs a VM rather than "
                "another entry on this list."
            )
        elif mode == "research":
            purpose = (
                "A research run executes nothing at all: it reads the web and records what "
                "it found. Answer from your sources, and say in your finish summary what "
                "you could not establish without running something."
            )
        elif mode == "investigate":
            purpose = (
                "You are answering a question by reading, inside a run that is already "
                "working on this tree. Read the code and say what you found; if the "
                "answer needs something run, say so and let the caller decide."
            )
        else:
            purpose = (
                "This list is for running tests, linters and builds; if you need something "
                "else, say so in your finish summary instead."
            )
        raise CommandRejected(
            f"'{program}' is not an allowed program. Allowed: "
            f"{', '.join(allowed_programs(mode))}. {purpose}"
        )

    args = argv[1:]

    if program == "python":
        _validate_python(args)
        return argv

    if program == "tar":
        _validate_tar(args)
        return argv

    if rule.required_any is not None and not rule.required_any & set(args):
        raise CommandRejected(
            f"'{program}' writes unless told to read, so it needs one of "
            + ", ".join(sorted(rule.required_any))
            + f". E.g. `{program} {sorted(rule.required_any)[0]} <file>`."
        )

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


def _validate_tar(args: Sequence[str]) -> None:
    """Listing an archive is allowed; extracting or creating one is not.

    Needs its own parser because tar takes a bundled flag cluster that may or may not
    have a leading dash — `tar tf x.tar`, `tar -tf x.tar` and `tar --list -f x.tar`
    are all the same request, and `tar xf x.tar` unpacks over the working directory.
    """
    if not args:
        raise CommandRejected("tar needs arguments, e.g. `tar -tf archive.tar`.")

    long_forms = {a for a in args if a.startswith("--")}
    if long_forms & {"--extract", "--create", "--append", "--update", "--delete"}:
        raise CommandRejected("tar may only list an archive here, not extract or modify one.")

    # The mode letter lives in the first non-`--` argument, dash or not.
    cluster = next((a for a in args if not a.startswith("--")), "")
    letters = set(cluster.lstrip("-"))
    writing = letters & set("xcruAd")
    if writing:
        raise CommandRejected(
            f"tar '{''.join(sorted(writing))}' would extract or modify the archive. "
            "Only listing is allowed: `tar -tf archive.tar`."
        )
    if "t" not in letters and "--list" not in long_forms:
        raise CommandRejected("tar needs 't' (list), e.g. `tar -tf archive.tar`.")


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
    mode: str = DEFAULT_MODE,
) -> ToolResult:
    """Run one allowlisted program in the workspace and return its output.

    `mode` selects which allowlist applies and is set by the agent loop, never by the
    model — otherwise RE mode could ask for the coding table and run the artifact.
    """
    try:
        argv = validate(command, mode)
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
