"""Scoring the coding agent on whether it actually finishes the job.

The unit suite says every part works. It says nothing about whether Skippy, handed a
task, does it — and that is the number that decides whether any of the rest was worth
building. Without it, `prompts.py` is several hundred lines of behaviour with no
regression test at all, and every edit to it is an argument nobody can settle.

So: real tasks, against throwaway copies of the small repos in `fixtures/eval_repos`,
graded by machine. Pass or fail, plus what it cost.

**Graders are objective on purpose.** Tests green, a symbol present, a file untouched, a
summary matching a pattern. No model judges the output. A model-as-judge would be
cheaper to write and would quietly grade fluency, which is the one thing that needs no
help — the failures worth catching are the confident ones, and those read beautifully.

**Traps count as much as features.** Half of what separates a good agent from a
plausible one is what it declines to do: not editing a test to make it pass, not
inventing work when the thing it was asked about does not exist, not touching files the
task never mentioned. Several tasks pass only by changing nothing.

**Runs are independent.** Project memory is off (`remember=False`). An eval where the
seventh run benefits from what the third wrote down is not running the same task twice,
and the scoreboard stops comparing to itself.

**A dead endpoint is not a verdict.** The loop catches `ModelError` and returns a
`failed` outcome rather than raising, so a dropped connection used to be graded like any
other run. That is wrong in both directions: a task the server killed halfway scores as
the agent leaving the tree broken, and a task whose edits happened to land before the
server died scores as a clean pass. Such runs are marked ERROR, retried once, and left
out of the pass rate.

Run it:

    python -m tests.agent_eval                 # the whole set, against the live model
    python -m tests.agent_eval --task rename_across_files --verbose
    python -m tests.agent_eval --save          # write the scoreboard, diff against last

This needs a model server, and it is slow — ten tasks at up to forty steps each on the
heavy role is a coffee, not a keystroke. What CI checks instead is in
`test_agent_eval.py`: that the set is well formed, that every grader fails on the
untouched repo, and that the harness scores a scripted pass and a scripted failure
correctly. A grader that cannot fail is the easiest mistake here and the hardest to
notice.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURE = os.path.join(HERE, "fixtures", "agent_tasks.json")
REPOS = os.path.join(HERE, "fixtures", "eval_repos")
SCOREBOARD = os.path.join(os.path.dirname(HERE), "benchmarks", "agent")

# Generous, because running out of steps is a result worth recording rather than a
# harness limit to tune away. A task that needs more than this is a task the set should
# not contain yet.
DEFAULT_MAX_STEPS = 25

TEST_COMMAND = [sys.executable, "-m", "pytest", "-q"]

_PRUNED = {"__pycache__", ".pytest_cache", ".git"}


# ---------------------------------------------------------------------------
# The set
# ---------------------------------------------------------------------------

@dataclass
class Task:
    name: str
    repo: str
    prompt: str
    graders: List[dict]
    pins: str = ""

    @property
    def repo_path(self) -> str:
        return os.path.join(REPOS, self.repo)


def load(path: str = FIXTURE) -> List[Task]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    tasks = [
        Task(
            name=raw["name"],
            repo=raw["repo"],
            prompt=raw["prompt"],
            graders=raw["graders"],
            pins=raw.get("pins", ""),
        )
        for raw in payload["tasks"]
    ]
    names = [task.name for task in tasks]
    if len(set(names)) != len(names):
        raise ValueError("two tasks share a name; the scoreboard keys on it")
    return tasks


# ---------------------------------------------------------------------------
# Grading
# ---------------------------------------------------------------------------

def tree_hashes(root: str) -> Dict[str, str]:
    """Every file under root, by relative path, hashed. How 'changed' is decided."""
    found = {}
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _PRUNED]
        for name in files:
            path = os.path.join(base, name)
            try:
                with open(path, "rb") as handle:
                    digest = hashlib.sha256(handle.read()).hexdigest()
            except OSError:
                continue
            found[os.path.relpath(path, root)] = digest
    return found


def changed_files(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    names = set(before) | set(after)
    return sorted(name for name in names if before.get(name) != after.get(name))


def run_tests(root: str, timeout: float = 120.0) -> bool:
    """The repo's own suite, green or not.

    Run here rather than trusted from the agent's account of it. "I ran the tests and
    they pass" is exactly the claim under evaluation.
    """
    try:
        result = subprocess.run(
            TEST_COMMAND, cwd=root, capture_output=True, text=True, timeout=timeout,
            env={**os.environ, "PYTHONPATH": root, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _read(root: str, path: str) -> str:
    try:
        with open(os.path.join(root, path), encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def grade_one(grader: dict, root: str, changed: List[str], outcome) -> Optional[str]:
    """None when the grader is satisfied, otherwise why it is not."""
    kind = grader["kind"]

    if kind == "tests_pass":
        return None if run_tests(root) else "the test suite does not pass"

    if kind == "contains":
        body = _read(root, grader["path"])
        if not body:
            return f"{grader['path']} is missing or empty"
        if re.search(grader["pattern"], body):
            return None
        return f"{grader['path']} does not match /{grader['pattern']}/"

    if kind == "not_contains":
        if re.search(grader["pattern"], _read(root, grader["path"])):
            return f"{grader['path']} still matches /{grader['pattern']}/"
        return None

    if kind == "absent_everywhere":
        hits = []
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if d not in _PRUNED]
            for name in files:
                if not name.endswith((".py", ".md")):
                    continue
                path = os.path.join(base, name)
                if re.search(grader["pattern"], _read(root, os.path.relpath(path, root))):
                    hits.append(os.path.relpath(path, root))
        return f"/{grader['pattern']}/ still appears in {', '.join(hits)}" if hits else None

    if kind == "unchanged":
        return f"{grader['path']} was modified" if grader["path"] in changed else None

    if kind == "no_files_changed":
        return f"changed {', '.join(changed)}" if changed else None

    if kind == "only_changed":
        allowed = set(grader["paths"])
        extra = [name for name in changed if name not in allowed]
        return f"also changed {', '.join(extra)}" if extra else None

    if kind == "status_is":
        actual = getattr(outcome, "status", "")
        return None if actual == grader["status"] else f"status was '{actual}'"

    if kind == "summary_matches":
        summary = getattr(outcome, "summary", "") or ""
        if re.search(grader["pattern"], summary):
            return None
        return f"the summary does not match /{grader['pattern']}/"

    raise ValueError(f"unknown grader kind: {kind!r}")


@dataclass
class Result:
    task: str
    passed: bool = False
    failures: List[str] = field(default_factory=list)
    status: str = ""
    steps: int = 0
    tool_calls: int = 0
    changed: List[str] = field(default_factory=list)
    seconds: float = 0.0
    error: str = ""

    @property
    def errored(self) -> bool:
        """The run never reached a verdict, so it says nothing about the agent."""
        return bool(self.error)

    def line(self) -> str:
        mark = "ERROR" if self.errored else ("PASS" if self.passed else "FAIL")
        if self.errored:
            detail = "  <- " + self.error
        else:
            detail = "" if self.passed else "  <- " + "; ".join(self.failures)
        return (
            f"  {mark:5} {self.task:34} {self.status:22} "
            f"{self.steps:3} steps  {self.seconds:6.1f}s{detail}"
        )


def grade(task: Task, root: str, changed: List[str], outcome) -> List[str]:
    failures = []
    for grader in task.graders:
        problem = grade_one(grader, root, changed, outcome)
        if problem:
            failures.append(problem)
    return failures


# ---------------------------------------------------------------------------
# Running one task
# ---------------------------------------------------------------------------

def checkout(task: Task, into: str) -> str:
    """A throwaway copy of the task's repo, which the agent may edit freely."""
    root = os.path.join(into, task.repo)
    shutil.copytree(task.repo_path, root, ignore=shutil.ignore_patterns(*_PRUNED))
    return root


async def run_task(task: Task, max_steps: int = DEFAULT_MAX_STEPS, verbose: bool = False) -> Result:
    import skippy_agent
    from skippy_sandbox import Sandbox

    result = Result(task=task.name)
    with tempfile.TemporaryDirectory(prefix="skippy-eval-") as scratch:
        root = checkout(task, scratch)
        before = tree_hashes(root)

        async def emit(event: dict) -> None:
            if not verbose:
                return
            if event.get("type") == "agent_tool_call":
                print(f"      {event['step']:>2} {event['tool']} {json.dumps(event['args'])[:100]}")
            elif event.get("type") == "agent_thought":
                print(f"      {event['step']:>2} … {' '.join(event['content'].split())[:100]}")

        started = time.monotonic()
        try:
            outcome = await skippy_agent.run_task(
                task.prompt,
                Sandbox([root]),
                max_steps=max_steps,
                emit=emit,
                # Off on purpose: a run that opens knowing what an earlier run of the
                # same task wrote down is not the same run, and the scoreboard would
                # stop comparing like with like.
                remember=False,
                journal_dir=os.path.join(scratch, "journal"),
            )
        except Exception as exc:  # a harness or endpoint failure, not a task failure
            result.error = f"{type(exc).__name__}: {exc}"
            result.seconds = time.monotonic() - started
            return result

        result.seconds = time.monotonic() - started
        result.status = outcome.status
        result.steps = outcome.steps
        result.tool_calls = outcome.tool_calls
        result.changed = changed_files(before, tree_hashes(root))

        # `failed` has exactly one source in the loop: the endpoint was unreachable. It
        # is the same event as the exception above, caught one level down and turned into
        # an outcome, and grading it would score the server rather than the agent.
        if outcome.status == "failed":
            result.error = outcome.summary or "the model endpoint failed"
            return result

        result.failures = grade(task, root, result.changed, outcome)
        result.passed = not result.failures
    return result


# ---------------------------------------------------------------------------
# The scoreboard
# ---------------------------------------------------------------------------

def summarize(results: List[Result]) -> dict:
    # Over the tasks that produced a verdict, not over the tasks attempted. A run where
    # the endpoint died three times is a run of seven tasks, and dividing by ten would
    # report a regression that no prompt change could fix.
    graded = [r for r in results if not r.errored]
    passed = [r for r in graded if r.passed]
    return {
        "recorded": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "tasks": len(results),
        "graded": len(graded),
        "errored": len(results) - len(graded),
        "passed": len(passed),
        "pass_rate": round(len(passed) / len(graded), 3) if graded else 0.0,
        "steps_total": sum(r.steps for r in results),
        "seconds_total": round(sum(r.seconds for r in results), 1),
        "results": {
            r.task: {
                "passed": r.passed,
                "errored": r.errored,
                "status": r.status,
                "steps": r.steps,
                "tool_calls": r.tool_calls,
                "seconds": round(r.seconds, 1),
                "changed": r.changed,
                "failures": r.failures,
                "error": r.error,
            }
            for r in results
        },
    }


def previous() -> Optional[dict]:
    try:
        names = sorted(n for n in os.listdir(SCOREBOARD) if n.endswith(".json"))
    except OSError:
        return None
    if not names:
        return None
    with open(os.path.join(SCOREBOARD, names[-1]), encoding="utf-8") as handle:
        return json.load(handle)


def save(board: dict) -> str:
    os.makedirs(SCOREBOARD, exist_ok=True)
    path = os.path.join(SCOREBOARD, f"{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(board, handle, indent=2, sort_keys=True)
    return path


def report(results: List[Result], against: Optional[dict] = None) -> str:
    board = summarize(results)
    headline = (
        f"{board['passed']}/{board['graded']} passed "
        f"({board['pass_rate']:.0%}), {board['steps_total']} steps, "
        f"{board['seconds_total']:.0f}s"
    )
    if board["errored"]:
        headline += f", {board['errored']} errored and not scored"
    lines = ["", headline, ""]
    lines += [r.line() for r in results]

    if against:
        # The only number that matters is the one that moved. A pass rate on its own
        # says nothing about whether the last change helped.
        lines += ["", f"Against {against.get('recorded', 'the previous run')}:"]
        moved = False
        for result in results:
            was = (against.get("results") or {}).get(result.task)
            if result.errored:
                lines.append(f"  ERROR {result.task}, no verdict this run")
                moved = True
            elif was is None:
                lines.append(f"  NEW   {result.task}")
                moved = True
            elif was.get("errored"):
                # No comparable baseline: the previous board recorded the endpoint
                # dying, and calling that a fix would credit the agent for a reboot.
                lines.append(
                    f"  {'PASS' if result.passed else 'FAIL'}  {result.task}, "
                    "no verdict last run"
                )
                moved = True
            elif was["passed"] != result.passed:
                lines.append(
                    f"  {'FIXED' if result.passed else 'BROKE'} {result.task}"
                )
                moved = True
        delta = board["pass_rate"] - against.get("pass_rate", 0.0)
        lines.append(f"  pass rate {delta:+.0%}")
        if board["errored"] or against.get("errored"):
            lines.append("  (rates cover different task sets; compare the lines above)")
        if not moved:
            lines.append("  no task changed verdict")
    return "\n".join(lines)


async def run_all(
    tasks: List[Task],
    max_steps: int = DEFAULT_MAX_STEPS,
    verbose: bool = False,
    retries: int = 1,
) -> List[Result]:
    """Every task in order, retrying the ones the endpoint killed.

    The retry is for infrastructure only. A task that ran and failed its graders is not
    retried, because the whole point is to measure how often that happens — but a
    fifteen-minute board coming back with holes in it because a local server dropped
    three connections is a cost with nothing to learn from.
    """
    results = []
    for task in tasks:
        print(f"  .. {task.name}", flush=True)
        result = await run_task(task, max_steps=max_steps, verbose=verbose)
        for _ in range(retries):
            if not result.errored:
                break
            print(f"     retrying, {result.error}", flush=True)
            result = await run_task(task, max_steps=max_steps, verbose=verbose)
        results.append(result)
    return results


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Score the coding agent on real tasks.")
    parser.add_argument("--task", action="append", help="run only these (repeatable)")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("--verbose", action="store_true", help="stream each step")
    parser.add_argument(
        "--retry-errors", type=int, default=1, metavar="N",
        help="re-run a task the endpoint killed, N times (default 1)",
    )
    parser.add_argument("--save", action="store_true", help="write the scoreboard")
    parser.add_argument("--list", action="store_true", help="show the set and exit")
    args = parser.parse_args(argv)

    tasks = load()
    if args.task:
        wanted = set(args.task)
        unknown = wanted - {t.name for t in tasks}
        if unknown:
            parser.error(f"no such task(s): {', '.join(sorted(unknown))}")
        tasks = [t for t in tasks if t.name in wanted]

    if args.list:
        for task in tasks:
            print(f"{task.name:34} [{task.repo}] {task.pins}")
        return 0

    results = asyncio.run(run_all(
        tasks,
        max_steps=args.max_steps,
        verbose=args.verbose,
        retries=args.retry_errors,
    ))
    print(report(results, against=previous()))
    if args.save:
        print(f"\nSaved {os.path.relpath(save(summarize(results)))}")
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":  # pragma: no cover - a benchmark, run by hand
    sys.path.insert(0, os.path.dirname(HERE))
    raise SystemExit(main())
