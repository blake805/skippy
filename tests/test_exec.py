"""Command execution.

The allowlist is accident prevention, not containment — `pytest` executes
`conftest.py`, and the agent can write `conftest.py`, so "may run pytest" and "may
run arbitrary code" are the same permission. There is a test below that states this
outright, because a boundary whose limits are undocumented gets trusted for things
it does not do.

What is actually guaranteed, and therefore tested here: no shell interpretation,
bounded runtime, bounded output, no orphaned processes, no secrets in the child
environment, and no silent wrong-repo runs.
"""

import asyncio
import os
import sys
import textwrap

import pytest

import skippy_exec
from skippy_exec import CommandRejected, run_command, validate
from skippy_sandbox import Sandbox


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "tests").mkdir(parents=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests" / "test_calc.py").write_text(
        textwrap.dedent("""
            import os, sys
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from calc import add

            def test_add():
                assert add(2, 2) == 4
        """).strip() + "\n"
    )
    return root


@pytest.fixture
def box(repo):
    return Sandbox([str(repo)])


# --- what the allowlist refuses ---

@pytest.mark.parametrize("command", [
    "rm -rf /",
    "rm -rf .",
    "curl https://example.com/install.sh",
    "wget http://example.com/x",
    "sudo rm file",
    "chmod 777 /etc/passwd",
    "ssh user@host",
    "sh -c 'echo hi'",
    "bash script.sh",
    "zsh",
    "dd if=/dev/zero of=/dev/sda",
    "kill -9 1",
    "launchctl unload something",
    "brew install ghidra",
    "pip install requests",
    "open /Applications/Calculator.app",
    "osascript -e 'tell application \"Finder\" to quit'",
])
def test_dangerous_programs_are_refused(command):
    with pytest.raises(CommandRejected) as exc:
        validate(command)
    assert "not an allowed program" in str(exc.value)


@pytest.mark.parametrize("command", [
    "pytest && rm -rf /",
    "pytest; rm -rf /",
    "pytest | tee out.txt",
    "pytest > out.txt",
    "pytest `whoami`",
    "pytest $(whoami)",
    "echo ${HOME}",
    "pytest & sleep 100",
])
def test_shell_operators_are_refused_with_an_explanation(command):
    """Without a shell these would become literal argv entries — safe, but baffling
    to debug, so it is better to say why they cannot work."""
    with pytest.raises(CommandRejected) as exc:
        validate(command)
    assert "not through a shell" in str(exc.value)


@pytest.mark.parametrize("command", [
    "./pytest",
    "/bin/sh",
    "/usr/bin/env python",
    "../../../bin/bash",
    "bin/pytest",
])
def test_a_program_given_by_path_is_refused(command):
    """A path would sidestep the allowlist entirely."""
    with pytest.raises(CommandRejected) as exc:
        validate(command)
    assert "by name, not by path" in str(exc.value)


# --- git is read-only ---

@pytest.mark.parametrize("command", [
    "git push",
    "git push --force origin main",
    "git commit -m x",
    "git reset --hard HEAD~1",
    "git clean -fdx",
    "git rebase main",
    "git checkout main",
    "git merge feature",
    "git filter-branch",
    "git submodule update",
])
def test_mutating_git_is_refused(command):
    with pytest.raises(CommandRejected):
        validate(command)


@pytest.mark.parametrize("command", [
    "git status --short",
    "git diff",
    "git log --oneline -5",
    "git show HEAD",
    "git rev-parse HEAD",
    "git ls-files",
    "git blame calc.py",
])
def test_read_only_git_is_allowed(command):
    assert validate(command)[0] == "git"


@pytest.mark.parametrize("command", [
    "git branch -D feature",
    "git branch -d feature",
    "git branch -m old new",
    "git tag -d v1",
])
def test_destructive_flags_on_read_only_git_subcommands_are_refused(command):
    """`branch` and `tag` read by default but delete with a flag, which is exactly
    the kind of thing a program-name allowlist alone would wave through."""
    with pytest.raises(CommandRejected) as exc:
        validate(command)
    assert "destroy work" in str(exc.value)


def test_bare_git_stash_is_refused_but_listing_is_allowed():
    """`git stash` with no arguments hides the agent's own uncommitted work."""
    with pytest.raises(CommandRejected):
        validate("git stash")
    assert validate("git stash list")


def test_git_config_writes_are_refused():
    with pytest.raises(CommandRejected):
        validate("git config user.email evil@example.com")
    assert validate("git config --get user.email")


# --- python ---

def test_python_dash_c_is_refused_for_auditability():
    """Not because it is more dangerous than pytest — it is not — but because a
    script on disk is journaled and reviewable while inline code leaves no trace."""
    with pytest.raises(CommandRejected) as exc:
        validate("python -c 'import os; os.remove(\"x\")'")
    assert "apply_patch" in str(exc.value)


def test_bare_python_is_refused():
    with pytest.raises(CommandRejected) as exc:
        validate("python")
    assert "interactive" in str(exc.value)


def test_reading_a_program_from_stdin_is_refused():
    with pytest.raises(CommandRejected):
        validate("python -")


@pytest.mark.parametrize("command", [
    "python -m pytest -q",
    "python3 -m pytest tests/",
    "python -m mypy calc.py",
    "python -m ruff check .",
    "python script.py",
])
def test_useful_python_invocations_are_allowed(command):
    assert validate(command)


@pytest.mark.parametrize("name", ["python", "python3", "python3.11", "python3.13", "python2"])
def test_versioned_python_names_are_the_same_program(name):
    """`sys.executable` inside a virtualenv is spelled `python3.11`, so an allowlist of
    only the unversioned names refuses the most ordinary invocation there is. Found by
    running the suite through the `pytest` console script instead of `python -m pytest`,
    which changes how `sys.executable` reports itself."""
    assert validate(f"{name} -m pytest -q")
    # And the python-specific rules still apply to the versioned spelling.
    with pytest.raises(CommandRejected):
        validate(f"{name} -c 'print(1)'")


def test_the_running_interpreter_is_always_runnable():
    """Whatever this suite is running under must itself be an allowed program,
    otherwise the agent cannot run the tests of the project it is working in."""
    assert validate(f"{os.path.basename(sys.executable)} -m pytest -q")


def test_an_unlisted_python_module_is_refused():
    with pytest.raises(CommandRejected) as exc:
        validate("python -m http.client")
    assert "Allowed modules" in str(exc.value)


# --- subcommand rules ---

@pytest.mark.parametrize("command", [
    "npm publish",
    "npm install express",
    "cargo publish",
    "cargo install ripgrep",
    "go install example.com/x",
    "make deploy",
    "yarn publish",
])
def test_unlisted_subcommands_are_refused(command):
    with pytest.raises(CommandRejected) as exc:
        validate(command)
    assert "not allowed" in str(exc.value)


@pytest.mark.parametrize("command", [
    "pytest -q",
    "npm test",
    "npm run lint",
    "cargo test --all",
    "go test ./...",
    "swift test",
    "make check",
    "ruff check .",
    "mypy .",
    "tsc --noEmit",
    "eslint src/",
])
def test_the_verification_surface_is_allowed(command):
    assert validate(command)


def test_the_refusal_message_lists_what_is_allowed(box):
    """A bare refusal makes the model guess again; naming the options lets it recover."""
    with pytest.raises(CommandRejected) as exc:
        validate("rustc main.rs")
    message = str(exc.value)
    assert "pytest" in message and "git" in message
    assert "finish summary" in message


def test_the_user_can_opt_into_extra_programs(monkeypatch):
    with pytest.raises(CommandRejected):
        validate("rustc main.rs")
    monkeypatch.setenv("SKIPPY_EXTRA_COMMANDS", "rustc, binwalk")
    assert validate("rustc main.rs") == ["rustc", "main.rs"]
    assert validate("binwalk -e firmware.bin")


# --- actually running things ---

@pytest.mark.asyncio
async def test_a_passing_test_suite_reports_success(box, repo):
    result = await run_command(box, f"{os.path.basename(sys.executable)} -m pytest -q tests/")
    assert result.ok, result.content
    assert "exited 0" in result.summary
    assert "1 passed" in result.content


@pytest.mark.asyncio
async def test_a_failing_test_suite_reports_failure_and_shows_why(box, repo):
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    result = await run_command(box, f"{os.path.basename(sys.executable)} -m pytest -q tests/")
    assert not result.ok
    assert "exited 1" in result.summary
    # The failure detail is the whole point; a bare exit code would be useless.
    assert "assert" in result.content
    assert result.data["exit_code"] == 1


@pytest.mark.asyncio
async def test_the_working_directory_is_the_workspace_root(box, repo):
    result = await run_command(box, "git status --short")
    # Not a git repo, so this fails, but it must have run *in* the workspace.
    assert result.data["cwd"] == "."


@pytest.mark.asyncio
async def test_an_explicit_cwd_is_honoured(box, repo):
    result = await run_command(
        box, f"{os.path.basename(sys.executable)} -m pytest -q .", cwd="tests"
    )
    assert result.data["cwd"] == "tests"


@pytest.mark.asyncio
async def test_a_cwd_outside_the_sandbox_is_refused(box, tmp_path):
    from skippy_sandbox import SandboxError

    with pytest.raises(SandboxError):
        await run_command(box, "pytest", cwd="../..")


@pytest.mark.asyncio
async def test_several_roots_require_an_explicit_cwd(repo, tmp_path):
    """Silently picking the first root means running a suite in the wrong repository
    and reporting the result as though it answered the question."""
    second = tmp_path / "other"
    second.mkdir()
    box = Sandbox([str(repo), str(second)])
    result = await run_command(box, "pytest -q")
    assert not result.ok
    assert "'cwd' is required" in result.summary
    assert "repo" in result.summary and "other" in result.summary


@pytest.mark.asyncio
async def test_a_missing_program_is_distinguished_from_a_forbidden_one(box, monkeypatch):
    monkeypatch.setenv("SKIPPY_EXTRA_COMMANDS", "definitely-not-installed-xyz")
    result = await run_command(box, "definitely-not-installed-xyz --help")
    assert not result.ok
    assert "is not installed" in result.summary


# --- timeouts and orphans ---

@pytest.mark.asyncio
async def test_a_hanging_command_is_killed_and_partial_output_kept(box, repo):
    (repo / "slow.py").write_text(
        "import sys, time\nprint('started', flush=True)\ntime.sleep(60)\n"
    )
    result = await run_command(box, f"{os.path.basename(sys.executable)} slow.py", timeout=2)
    assert not result.ok
    assert result.data["timed_out"] is True
    # Partial output shows how far it got, which is usually what identifies the hang.
    assert "started" in result.content


@pytest.mark.asyncio
async def test_a_timeout_kills_the_whole_process_tree(box, repo, tmp_path):
    """Killing only the direct child leaves orphans: a timed-out `npm test` would
    leave node holding whatever port it bound, and the next run fails confusingly.

    The grandchild's output goes to DEVNULL rather than being inherited. That detail
    is what makes this test work at all: an inherited pipe keeps the parent's stdout
    open, so the reader blocks until the orphan finishes on its own and the orphan
    has stopped ticking by the time the assertion runs. The first version of this
    test passed against an implementation that only killed the direct child.
    """
    marker = tmp_path / "child_alive.txt"
    (repo / "spawner.py").write_text(textwrap.dedent(f"""
        import subprocess, sys, time
        child = subprocess.Popen(
            [sys.executable, "-c",
             "import time\\nfor _ in range(300):\\n"
             "    open({str(marker)!r}, 'a').write('tick\\\\n')\\n"
             "    time.sleep(0.1)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print("spawned", child.pid, flush=True)
        time.sleep(60)
    """).strip() + "\n")

    result = await run_command(box, f"{os.path.basename(sys.executable)} spawner.py", timeout=2)
    assert result.data["timed_out"] is True
    assert marker.exists(), "the grandchild never started, so this proves nothing"

    size_at_kill = marker.stat().st_size
    await asyncio.sleep(1.0)
    assert marker.stat().st_size == size_at_kill, "the grandchild survived the timeout kill"


@pytest.mark.asyncio
async def test_a_timeout_is_not_extended_by_a_process_holding_the_pipe(box, repo, tmp_path):
    """The timeout has to bound the tool, not just the direct child.

    A grandchild inheriting stdout keeps the pipe open after its parent dies, so
    draining to EOF can outlast the timeout by however long the orphan runs.
    """
    (repo / "leaky.py").write_text(textwrap.dedent("""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        print("spawned", flush=True)
        time.sleep(30)
    """).strip() + "\n")

    started = asyncio.get_running_loop().time()
    result = await run_command(box, f"{os.path.basename(sys.executable)} leaky.py", timeout=2)
    elapsed = asyncio.get_running_loop().time() - started

    assert result.data["timed_out"] is True
    # 2s timeout plus the bounded 5s drain grace, with headroom. Not 30s.
    assert elapsed < 12, f"the timeout was extended to {elapsed:.0f}s by the open pipe"


@pytest.mark.asyncio
async def test_the_kill_refuses_to_signal_skippys_own_process_group(box, repo, monkeypatch):
    """If the child ever ends up in our group, killpg would take down the agent, the
    server, and everything else in the process.

    Found by mutation testing rather than by design: with `start_new_session` removed,
    the timeout test killed the test runner and its shell instead of failing.
    """
    (repo / "slow2.py").write_text("import time\nprint('go', flush=True)\ntime.sleep(60)\n")

    # Report the child as sharing our group, which is what a missing
    # start_new_session would produce.
    monkeypatch.setattr(os, "getpgid", lambda pid: os.getpgrp())
    killed_groups = []
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed_groups.append(pgid))

    result = await run_command(box, f"{os.path.basename(sys.executable)} slow2.py", timeout=2)

    assert result.data["timed_out"] is True
    assert killed_groups == [], "killpg was called on Skippy's own process group"


@pytest.mark.asyncio
async def test_the_timeout_is_capped(box, repo):
    (repo / "quick.py").write_text("print('hi')\n")
    result = await run_command(
        box, f"{os.path.basename(sys.executable)} quick.py", timeout=99999
    )
    assert result.ok


@pytest.mark.asyncio
async def test_a_non_positive_timeout_is_refused(box):
    assert not (await run_command(box, "pytest", timeout=0)).ok


# --- output handling ---

@pytest.mark.asyncio
async def test_enormous_output_is_bounded_but_keeps_both_ends(box, repo):
    """A command that prints hundreds of megabytes must not be read into memory, and
    the tail is what carries the verdict."""
    (repo / "loud.py").write_text(
        "print('FIRST LINE')\n"
        "for i in range(200000):\n"
        "    print('x' * 200)\n"
        "print('LAST LINE')\n"
    )
    result = await run_command(box, f"{os.path.basename(sys.executable)} loud.py", timeout=120)
    assert result.ok
    assert len(result.content) < skippy_exec.MAX_OUTPUT_BYTES * 2
    assert "FIRST LINE" in result.content
    assert "LAST LINE" in result.content
    assert result.data["truncated"] is True


@pytest.mark.asyncio
async def test_stderr_is_included(box, repo):
    (repo / "noisy.py").write_text(
        "import sys\nsys.stderr.write('a warning\\n')\nsys.exit(3)\n"
    )
    result = await run_command(box, f"{os.path.basename(sys.executable)} noisy.py")
    assert not result.ok
    assert "a warning" in result.content
    assert result.data["exit_code"] == 3


@pytest.mark.asyncio
async def test_a_silent_command_says_so_rather_than_returning_nothing(box, repo):
    (repo / "quiet.py").write_text("pass\n")
    result = await run_command(box, f"{os.path.basename(sys.executable)} quiet.py")
    assert result.ok
    assert "no output" in result.content


# --- the child environment ---

@pytest.mark.asyncio
async def test_secrets_are_not_handed_to_the_child(box, repo, monkeypatch):
    """The code that inherits this environment is the repo's own test suite. When
    reverse-engineering someone else's project, that is not code to give an API key."""
    for name in ("OPENAI_API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY",
                 "HF_TOKEN", "MY_COMPANY_PASSWORD", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "s3cret-value")

    (repo / "dump.py").write_text("import os\nprint(chr(10).join(sorted(os.environ)))\n")
    result = await run_command(box, f"{os.path.basename(sys.executable)} dump.py")
    assert result.ok
    assert "s3cret-value" not in result.content
    for name in ("OPENAI_API_KEY", "GITHUB_TOKEN", "AWS_SECRET_ACCESS_KEY", "HF_TOKEN"):
        assert name not in result.content


@pytest.mark.asyncio
async def test_the_toolchain_environment_survives(box, repo):
    """Scrubbing must not be so aggressive that the tools stop working."""
    (repo / "env.py").write_text("import os\nprint(os.environ.get('PATH', 'MISSING'))\n")
    result = await run_command(box, f"{os.path.basename(sys.executable)} env.py")
    assert result.ok
    assert result.content.strip() not in ("MISSING", "")


@pytest.mark.asyncio
async def test_git_never_waits_for_a_credential_prompt(box):
    """Without GIT_TERMINAL_PROMPT=0 a git call needing credentials blocks until the
    timeout and reports nothing useful."""
    assert skippy_exec._child_env()["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.asyncio
async def test_a_prompting_command_gets_eof_rather_than_blocking(box, repo):
    """Behavioural half: a command that reads stdin finishes instead of waiting.

    This cannot distinguish DEVNULL from inherited stdin under pytest, which closes
    stdin itself — so the assertion below pins the wiring directly. Stated rather
    than dressed up as a behavioural test, because a test that proves less than it
    appears to is worse than one that admits its scope.
    """
    (repo / "asks.py").write_text(
        "try:\n    input('name? ')\nexcept EOFError:\n    print('no stdin')\n"
    )
    result = await run_command(box, f"{os.path.basename(sys.executable)} asks.py", timeout=10)
    assert result.ok
    assert "no stdin" in result.content


@pytest.mark.asyncio
async def test_the_child_is_given_no_stdin(box, repo, monkeypatch):
    """Structural, for the reason given above. In production Skippy is a server whose
    stdin may be a terminal, and an inherited one turns a prompting command into a
    wait for the full timeout."""
    seen = {}
    real = asyncio.create_subprocess_exec

    async def spy(*argv, **kwargs):
        seen.update(kwargs)
        return await real(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)
    (repo / "quiet2.py").write_text("pass\n")
    await run_command(box, f"{os.path.basename(sys.executable)} quiet2.py")
    assert seen["stdin"] == asyncio.subprocess.DEVNULL
    assert seen["start_new_session"] is True


# --- the honest limits ---

@pytest.mark.asyncio
async def test_an_allowlisted_pytest_still_executes_repo_code(box, repo):
    """Stated outright, because the natural assumption is that the allowlist confines
    what runs, and it does not. pytest imports conftest.py and executes its module
    body, and the agent can write conftest.py with apply_patch. So "may run pytest"
    and "may execute arbitrary code" are the same permission.

    This is why there is no approval-gated shell tool: asking permission for the loud
    path while this one is open would be theatre. Real containment needs the whole
    run inside a VM, which is a deployment decision, not a change to this file.
    """
    (repo / "conftest.py").write_text(
        "import pathlib\n"
        "pathlib.Path(__file__).parent.joinpath('side_effect.txt').write_text('ran')\n"
    )
    result = await run_command(box, f"{os.path.basename(sys.executable)} -m pytest -q tests/")
    assert result.ok
    assert (repo / "side_effect.txt").read_text() == "ran"


def test_the_allowed_list_is_reported_for_the_docs():
    programs = skippy_exec.allowed_programs()
    assert "pytest" in programs and "git" in programs
    # Nothing that fetches or installs by default; that is the user's call.
    assert "pip" not in programs and "brew" not in programs and "curl" not in programs


# --- through the dispatcher ---

@pytest.mark.asyncio
async def test_dispatch_routes_run_command(box, repo):
    import skippy_dispatch

    result = await skippy_dispatch.dispatch(
        "run_command", {"command": f"{os.path.basename(sys.executable)} -m pytest -q tests/"}, box
    )
    assert result.ok
    assert "1 passed" in result.content


@pytest.mark.asyncio
async def test_a_refused_command_reads_as_an_error_to_the_model(box):
    import skippy_dispatch

    result = await skippy_dispatch.dispatch("run_command", {"command": "rm -rf /"}, box)
    assert not result.ok
    assert result.as_observation().startswith("ERROR: ")


# --- the two execution modes ---
#
# Coding mode runs the repo's own tests, which means running repo-defined code by
# design. RE mode analyses someone else's artifact, where running it is the thing
# that must not happen by accident. Same tool, two tables.

def test_re_mode_cannot_run_anything(box):
    """The point of the split. Every one of these executes code, and in RE mode the
    code in question belongs to the artifact under analysis."""
    for command in (
        "python -m pytest",
        "pytest",
        "node index.js",
        "make",
        "cargo test",
        "go test ./...",
        "npm test",
        "swift test",
        "ruby script.rb",
    ):
        with pytest.raises(skippy_exec.CommandRejected):
            skippy_exec.validate(command, mode="re")


def test_the_re_refusal_explains_that_running_it_needs_a_vm(box):
    """A refusal the model cannot act on becomes a retry. This one says what the
    actual answer is, so it stops asking."""
    with pytest.raises(skippy_exec.CommandRejected) as exc:
        skippy_exec.validate("python -m pytest", mode="re")
    message = str(exc.value)
    assert "static inspection" in message
    assert "VM" in message


@pytest.mark.parametrize("command", [
    "file target.dylib",
    "strings -n 8 target.dylib",
    "nm -gU target.dylib",
    "otool -h target.dylib",
    "objdump -d target.dylib",
    "xxd target.dylib",
    "hexdump -C target.dylib",
    "size target.dylib",
    "dwarfdump target.dylib",
    "c++filt _ZN3fooEv",
])
def test_re_mode_allows_static_inspection(command):
    assert skippy_exec.validate(command, mode="re")[0] == command.split()[0]


def test_coding_mode_still_runs_tests(box):
    assert skippy_exec.validate("pytest -q", mode="coding")


def test_the_default_mode_is_coding(box):
    assert skippy_exec.validate("pytest -q") == ["pytest", "-q"]


def test_an_unknown_mode_is_refused_rather_than_falling_back(box):
    """Falling back to a default would mean a typo in the mode silently grants the
    wider table, which is the wrong direction to fail."""
    with pytest.raises(skippy_exec.CommandRejected) as exc:
        skippy_exec.validate("file x", mode="reverse")
    assert "Unknown execution mode" in str(exc.value)


# --- tools that read by default and write when asked ---
#
# The trap in the RE table: these are all legitimate inspection tools whose own
# flags turn them into writers, so allowlisting the program name is not enough.

@pytest.mark.parametrize("command,allowed", [
    ("lipo -info target", True),
    ("lipo -archs target", True),
    ("lipo -detailed_info target", True),
    ("lipo target -thin arm64 -output out", False),
    ("lipo -create a b -output c", False),
    ("lipo -remove arm64 target -output out", False),
    ("codesign -d --entitlements - target", True),
    ("codesign -dv target", True),
    ("codesign -s ident target", False),
    ("codesign -f -s ident target", False),
    ("codesign --remove-signature target", False),
    ("plutil -p Info.plist", True),
    ("plutil -lint Info.plist", True),
    ("plutil -convert xml1 Info.plist", False),
    ("plutil -replace Key -string v Info.plist", False),
    ("unzip -l app.ipa", True),
    ("unzip -p app.ipa file", True),
    ("unzip app.ipa", False),
    ("unzip -o app.ipa", False),
    ("xxd target", True),
    ("xxd -r dump", False),
])
def test_read_only_flags_are_required_where_the_default_writes(command, allowed):
    if allowed:
        assert skippy_exec.validate(command, mode="re")
    else:
        with pytest.raises(skippy_exec.CommandRejected):
            skippy_exec.validate(command, mode="re")


def test_the_write_by_default_refusal_names_a_working_alternative(box):
    with pytest.raises(skippy_exec.CommandRejected) as exc:
        skippy_exec.validate("unzip app.ipa", mode="re")
    message = str(exc.value)
    assert "-l" in message


@pytest.mark.parametrize("command,allowed", [
    ("tar -tf archive.tar", True),
    ("tar tf archive.tar", True),
    ("tar -tvf archive.tar", True),
    ("tar --list -f archive.tar", True),
    ("tar xf archive.tar", False),
    ("tar -xf archive.tar", False),
    ("tar -xzf archive.tar.gz", False),
    ("tar cf archive.tar dir", False),
    ("tar --extract -f archive.tar", False),
    ("tar --create -f archive.tar dir", False),
    ("tar -rf archive.tar extra", False),
    ("tar -df archive.tar", False),
    ("tar archive.tar", False),
])
def test_tar_may_only_list(command, allowed):
    """tar needs its own parser: the mode letter may or may not have a leading dash,
    and it arrives bundled with the others, so `xf` and `tf` differ by one character
    in a cluster that a flag set cannot see."""
    if allowed:
        assert skippy_exec.validate(command, mode="re")
    else:
        with pytest.raises(skippy_exec.CommandRejected):
            skippy_exec.validate(command, mode="re")


def test_git_stays_read_only_in_re_mode():
    assert skippy_exec.validate("git log --oneline", mode="re")
    with pytest.raises(skippy_exec.CommandRejected):
        skippy_exec.validate("git push", mode="re")


def test_no_interpreter_or_build_tool_is_in_the_inspection_table():
    """Guards the table itself, so a later addition has to be deliberate."""
    executors = {
        "python", "python3", "node", "ruby", "perl", "php", "pytest", "make",
        "cargo", "go", "npm", "npx", "yarn", "pnpm", "swift", "gradle", "mvn",
        "cmake", "bazel", "dotnet", "tox", "rake", "sh", "bash", "zsh",
    }
    assert not (executors & set(skippy_exec.INSPECTION_RULES))


def test_both_tables_share_the_shell_and_path_rules():
    """The mode picks the program table, not the parsing rules."""
    for mode in ("coding", "re"):
        with pytest.raises(skippy_exec.CommandRejected):
            skippy_exec.validate("file x; rm -rf /", mode=mode)
        with pytest.raises(skippy_exec.CommandRejected):
            skippy_exec.validate("./file x", mode=mode)
        with pytest.raises(skippy_exec.CommandRejected):
            skippy_exec.validate("file x | sh", mode=mode)


@pytest.mark.asyncio
async def test_the_mode_is_not_model_controlled(box, repo):
    """A model in RE mode that could ask for the coding table could run the artifact
    it is analysing, which is the one thing the split exists to prevent."""
    import skippy_dispatch

    result = await skippy_dispatch.dispatch(
        "run_command",
        {"command": f"{os.path.basename(sys.executable)} -m pytest -q tests/", "mode": "coding"},
        box,
        mode="re",
    )
    assert not result.ok
    assert "static inspection" in result.summary


@pytest.mark.asyncio
async def test_re_mode_actually_inspects_a_real_file(box, repo):
    (repo / "sample.bin").write_bytes(b"\x00\x01MAGICSTRING\x00\x02" + b"\xff" * 64)
    result = await run_command(box, "strings -n 6 sample.bin", mode="re")
    assert result.ok
    assert "MAGICSTRING" in result.content


@pytest.mark.asyncio
async def test_the_extra_commands_escape_hatch_is_per_user_not_per_mode(box, monkeypatch):
    """SKIPPY_EXTRA_COMMANDS is the user's decision and applies in both modes; the
    model cannot reach it."""
    monkeypatch.setenv("SKIPPY_EXTRA_COMMANDS", "radare2")
    assert skippy_exec.validate("radare2 -v", mode="re")
    assert skippy_exec.validate("radare2 -v", mode="coding")


# --- refusals have to name the alternative ---
#
# A live RE run spent three of eighteen steps retrying pipes, because the refusal said
# what was forbidden without saying how to get the result. A refusal the model cannot
# act on is a retry loop that ends when the budget does.

@pytest.mark.parametrize("command,expected", [
    ("nm target | grep foo", "grep tool"),
    ("otool -tv target > out.txt", "apply_patch"),
    ("file a && file b", "one call per program"),
    ("file a; file b", "one call per program"),
    ("strings `which ls`", "inner command"),
    ("strings $(which ls)", "inner command"),
    ("pytest &", "foreground"),
])
def test_a_shell_operator_refusal_names_what_to_do_instead(command, expected):
    with pytest.raises(skippy_exec.CommandRejected) as exc:
        skippy_exec.validate(command)
    assert expected in str(exc.value), f"{command!r} -> {exc.value}"


def test_every_rejected_operator_gets_a_specific_alternative():
    """Guards the mapping itself: a new operator added to the detector without an
    alternative would fall back to the generic text that caused the retry loop."""
    for operator in ("|", ">", ">>", "<", "&&", "||", ";", "`", "$(", "&", "\n"):
        assert skippy_exec._shell_alternative(operator) != "Run one program per call.", (
            f"{operator!r} has no specific alternative"
        )
