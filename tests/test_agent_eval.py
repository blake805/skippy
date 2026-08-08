"""The agent scoreboard's own tests.

Scoring the agent needs a model, so the scoreboard itself is run by hand. What runs here
is everything about it that can be wrong without a model being involved — and the list
is longer than it looks, because a broken eval is worse than no eval: it produces a
number, the number looks fine, and it is measuring nothing.

Three things are checked. That the task set is well formed. That **every grader fails on
the untouched repo**, which is the property that makes a pass mean something. And that
the harness scores a real run correctly, by driving one end to end with the scripted
model — the same trick the rest of the suite uses to test the loop offline.
"""

import json
import os
import shutil

import pytest

from tests import agent_eval as ev
from tests import fake_llm as fl


@pytest.fixture(scope="module")
def tasks():
    return ev.load()


@pytest.fixture
def checkout(tmp_path):
    """A throwaway copy of a task's repo, as the harness makes one."""
    def make(task):
        return ev.checkout(task, str(tmp_path))
    return make


# -- the set ----------------------------------------------------------------

def test_the_set_is_big_enough_and_varied(tasks):
    assert len(tasks) >= 8
    assert len({task.repo for task in tasks}) >= 3


def test_every_task_names_the_prompt_line_it_defends(tasks):
    """A failing task should say what to go and change. Without this the scoreboard
    tells you the number went down and nothing else."""
    for task in tasks:
        assert task.pins.strip(), task.name


def test_every_task_repo_exists_and_starts_green(tasks):
    """Except where a failing suite is the point: those tasks are about fixing it, and
    the grader below proves the repo starts red."""
    red_on_purpose = {"feeds", "probe", "planner"}
    for task in tasks:
        assert os.path.isdir(task.repo_path), task.repo
        if task.repo not in red_on_purpose:
            assert ev.run_tests(task.repo_path), f"{task.repo} does not start green"


def test_the_repos_that_should_start_broken_do(tasks):
    """The 'fix the cause' tasks and the planner task depend on a red suite, and a
    fixture quietly repaired would turn them into tasks that pass by doing nothing."""
    for repo in ("feeds", "probe", "planner"):
        assert not ev.run_tests(os.path.join(ev.REPOS, repo)), repo


def test_traps_are_a_real_share_of_the_set(tasks):
    """Half of what separates a good agent from a plausible one is what it declines to
    do, and a set that only measures features would score a busy fabricator top marks."""
    trap_kinds = {"no_files_changed", "unchanged", "only_changed"}
    traps = [
        task for task in tasks
        if any(grader["kind"] in trap_kinds for grader in task.graders)
    ]
    assert len(traps) >= 4


# -- the graders can fail ---------------------------------------------------

@pytest.mark.parametrize("name", [task.name for task in ev.load()])
def test_every_task_fails_on_the_untouched_repo(name, checkout):
    """The property the whole scoreboard rests on.

    A grader that is already satisfied before the agent does anything measures nothing
    and quietly inflates the number. This is the exact mistake the research gate's
    labelled set made in its first draft — there, every correct answer had an empty
    checkable list, so the threshold looked free at any value. Cheap to check, and
    impossible to spot by reading.
    """
    task = next(t for t in ev.load() if t.name == name)
    root = checkout(task)

    class Untouched:
        status = "finished"
        summary = "I did nothing at all."

    failures = ev.grade(task, root, changed=[], outcome=Untouched())
    assert failures, f"{name} passes without the agent doing anything"


def test_each_grader_kind_is_exercised_by_the_set(tasks):
    """An unused grader kind is untested code in the thing doing the measuring."""
    used = {grader["kind"] for task in tasks for grader in task.graders}
    assert used >= {
        "tests_pass", "contains", "unchanged", "no_files_changed",
        "only_changed", "status_is", "summary_matches", "absent_everywhere",
    }


def test_an_unknown_grader_kind_is_loud(tmp_path):
    """Silently skipping a grader it does not recognise would be a scoreboard that
    scores less than it claims to."""
    with pytest.raises(ValueError, match="unknown grader"):
        ev.grade_one({"kind": "vibes"}, str(tmp_path), [], None)


# -- the graders themselves -------------------------------------------------

def test_tests_pass_reads_the_repo_rather_than_the_agents_account_of_it(tasks):
    units = os.path.join(ev.REPOS, "units")
    assert ev.run_tests(units)
    assert not ev.run_tests(os.path.join(ev.REPOS, "feeds"))


def test_changed_files_notices_edits_additions_and_deletions(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("original\n")
    (root / "pkg" / "gone.py").write_text("bye\n")
    before = ev.tree_hashes(str(root))

    (root / "pkg" / "a.py").write_text("edited\n")
    (root / "pkg" / "new.py").write_text("hello\n")
    (root / "pkg" / "gone.py").unlink()

    assert ev.changed_files(before, ev.tree_hashes(str(root))) == [
        os.path.join("pkg", "a.py"),
        os.path.join("pkg", "gone.py"),
        os.path.join("pkg", "new.py"),
    ]


def test_pycache_is_not_mistaken_for_a_change(tmp_path):
    """Running the suite writes bytecode. Counting that as collateral damage would fail
    every no-files-changed task for a reason that has nothing to do with the agent."""
    root = tmp_path / "repo"
    (root / "pkg" / "__pycache__").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("x = 1\n")
    before = ev.tree_hashes(str(root))
    (root / "pkg" / "__pycache__" / "a.cpython-311.pyc").write_bytes(b"\x00\x01")

    assert ev.changed_files(before, ev.tree_hashes(str(root))) == []


def test_summary_and_status_graders_read_the_outcome(tmp_path):
    class Outcome:
        status = "max_steps"
        summary = "Ran out of road in calc.py."

    assert ev.grade_one({"kind": "status_is", "status": "finished"}, str(tmp_path), [], Outcome())
    assert not ev.grade_one({"kind": "status_is", "status": "max_steps"}, str(tmp_path), [], Outcome())
    assert not ev.grade_one(
        {"kind": "summary_matches", "pattern": r"calc\.py"}, str(tmp_path), [], Outcome()
    )
    assert ev.grade_one(
        {"kind": "summary_matches", "pattern": "nonsense"}, str(tmp_path), [], Outcome()
    )


# -- the harness, end to end ------------------------------------------------

def patch_call(path: str, search: str, replace: str, call_id: str = "c1"):
    return fl.tool_call(
        "apply_patch", call_id=call_id,
        edits=[{"path": path, "action": "edit", "search": search, "replace": replace}],
    )


@pytest.mark.asyncio
async def test_the_harness_scores_a_real_pass(routed_llm, monkeypatch):
    """Driven by the scripted model, so the whole path — checkout, run, diff, grade —
    is exercised without a real one. If this passes and the graders above can fail,
    the scoreboard measures what it says it does."""
    task = next(t for t in ev.load() if t.name == "stay_in_scope")
    routed_llm.load([
        patch_call(
            "README.md",
            "Unit conversions for shop work.",
            "Unit conversions for shop work. Install pytest first: `pip install pytest`.",
        ),
        fl.tool_call("finish", call_id="c2", summary="Added the install line."),
    ])

    result = await ev.run_task(task, max_steps=6)
    assert result.passed, result.failures
    assert result.status == "finished"
    assert result.changed == ["README.md"]
    assert result.steps == 2


@pytest.mark.asyncio
async def test_the_harness_scores_a_real_failure(routed_llm):
    """The agent does something plausible and wrong — edits the right file, but not in
    the way the task asked for."""
    task = next(t for t in ev.load() if t.name == "stay_in_scope")
    routed_llm.load([
        patch_call("README.md", "# units", "# units\n\nSome unrelated prose."),
        fl.tool_call("finish", call_id="c2", summary="Done!"),
    ])

    result = await ev.run_task(task, max_steps=6)
    assert not result.passed
    assert any("install" in failure for failure in result.failures)


@pytest.mark.asyncio
async def test_collateral_damage_fails_a_task_that_otherwise_passed(routed_llm):
    """The scope grader earning its place: the asked-for change is made, and something
    else is touched on the way past."""
    task = next(t for t in ev.load() if t.name == "stay_in_scope")
    routed_llm.load([
        fl.tool_calls(
            ("apply_patch", {"edits": [
                {"path": "README.md", "action": "edit",
                 "search": "Unit conversions for shop work.",
                 "replace": "Unit conversions. Install pytest with `pip install pytest`."},
                {"path": "units/convert.py", "action": "edit",
                 "search": "MM_PER_INCH = 25.4", "replace": "MM_PER_INCH = 25.4  # tidied"},
            ]}),
        ),
        fl.tool_call("finish", call_id="c2", summary="Tidied things up."),
    ])

    result = await ev.run_task(task, max_steps=6)
    assert not result.passed
    assert any("convert.py" in failure for failure in result.failures)


@pytest.mark.asyncio
async def test_a_run_that_never_finishes_is_not_a_pass(routed_llm):
    """The right file state without a finish call is a diff with no handoff. The loop's
    own contract says running out of steps is never success, and the board must not
    disagree with the thing it measures — this was live for one afternoon and scored a
    max_steps wander as the day's headline pass."""
    task = next(t for t in ev.load() if t.name == "stay_in_scope")
    routed_llm.load([
        patch_call(
            "README.md",
            "Unit conversions for shop work.",
            "Unit conversions for shop work. Install pytest first: `pip install pytest`.",
        ),
        # And then the model dithers until the budget runs out, never calling finish.
        fl.tool_call("read_file", call_id="c2", path="README.md"),
        fl.tool_call("read_file", call_id="c3", path="README.md"),
    ])

    result = await ev.run_task(task, max_steps=3)
    assert result.status == "max_steps"
    assert not result.passed
    assert any("never called finish" in failure for failure in result.failures)


@pytest.mark.asyncio
async def test_a_run_that_edits_nothing_fails_a_task_that_needs_an_edit(routed_llm):
    task = next(t for t in ev.load() if t.name == "add_a_helper")
    routed_llm.load([fl.tool_call("finish", call_id="c1", summary="Looks fine to me.")])

    result = await ev.run_task(task, max_steps=4)
    assert not result.passed
    assert result.changed == []


@pytest.mark.asyncio
async def test_the_harness_survives_a_dead_endpoint(routed_llm):
    """A broken model server is a harness failure, not a task failure, and the two must
    not look the same on the scoreboard."""
    task = next(t for t in ev.load() if t.name == "add_a_helper")
    routed_llm.load([fl.http_error(500)] * 4)

    result = await ev.run_task(task, max_steps=2)
    assert result.errored
    assert not result.passed
    assert not result.failures, "a run with no verdict must not carry graded failures"


@pytest.mark.asyncio
async def test_an_endpoint_that_dies_after_a_good_edit_is_not_a_pass(routed_llm):
    """Found in the first live baseline, and the more dangerous half of the bug.

    A task whose edits happened to land before the server dropped the connection
    satisfied every grader and was recorded green, with the crash visible only as a
    status nobody reads. Failures inflate a board loudly; passes inflate it silently.
    """
    task = next(t for t in ev.load() if t.name == "stay_in_scope")
    routed_llm.load([
        patch_call(
            "README.md",
            "Unit conversions for shop work.",
            "Unit conversions for shop work. Install pytest first: `pip install pytest`.",
        ),
        *[fl.http_error(500)] * 3,
    ])

    result = await ev.run_task(task, max_steps=4)
    assert result.errored
    assert not result.passed
    assert result.changed == ["README.md"], "the edit did land; that is the trap"


@pytest.mark.asyncio
async def test_a_task_the_endpoint_killed_is_retried(routed_llm):
    """Fifteen minutes of board should not come back with a hole in it because a local
    server dropped one connection."""
    task = next(t for t in ev.load() if t.name == "stay_in_scope")
    routed_llm.load([
        *[fl.http_error(500)] * 3,
        patch_call(
            "README.md",
            "Unit conversions for shop work.",
            "Unit conversions for shop work. Install pytest first: `pip install pytest`.",
        ),
        fl.tool_call("finish", call_id="c2", summary="Added the install line."),
    ])

    results = await ev.run_all([task], max_steps=4)
    assert len(results) == 1
    assert results[0].passed, results[0].failures or results[0].error


@pytest.mark.asyncio
async def test_a_graded_failure_is_not_retried(routed_llm):
    """The retry is for infrastructure. Re-rolling a task the agent got wrong is how a
    scoreboard talks itself into a better number than it earned."""
    task = next(t for t in ev.load() if t.name == "stay_in_scope")
    routed_llm.load([
        patch_call("README.md", "# units", "# units\n\nSome unrelated prose."),
        fl.tool_call("finish", call_id="c2", summary="Done!"),
    ])

    results = await ev.run_all([task], max_steps=4)
    assert not results[0].passed
    assert not results[0].errored
    assert routed_llm.remaining == 0, "the script was consumed exactly once"


@pytest.mark.asyncio
async def test_the_agent_cannot_reach_out_of_its_checkout(routed_llm, tmp_path):
    """The eval hands the agent a sandbox over a temporary copy. If a task could reach
    the fixture itself, one run would poison every later one."""
    task = next(t for t in ev.load() if t.name == "stay_in_scope")
    pristine = ev.tree_hashes(task.repo_path)
    routed_llm.load([
        patch_call("README.md", "# units", "# units (edited)"),
        fl.tool_call("finish", call_id="c2", summary="Edited."),
    ])

    await ev.run_task(task, max_steps=6)
    assert ev.tree_hashes(task.repo_path) == pristine


# -- the report -------------------------------------------------------------

def test_the_report_says_which_tasks_moved():
    """A pass rate on its own does not tell you whether the last change helped."""
    now = [
        ev.Result(task="a", passed=True, status="finished", steps=3),
        ev.Result(task="b", passed=False, status="max_steps", steps=25,
                  failures=["the test suite does not pass"]),
        ev.Result(task="c", passed=True, status="finished", steps=5),
    ]
    against = {
        "recorded": "2026-08-01T00:00:00",
        "pass_rate": 0.667,
        "results": {
            "a": {"passed": False}, "b": {"passed": True},
        },
    }
    text = ev.report(now, against=against)

    assert "2/3 passed" in text
    assert "FIXED a" in text
    assert "BROKE b" in text
    assert "NEW   c" in text
    assert "the test suite does not pass" in text


def test_an_errored_task_is_left_out_of_the_pass_rate():
    """Dividing by the tasks attempted rather than the tasks scored reports a regression
    that no prompt change could fix, and hides a real one behind the noise."""
    board = ev.summarize([
        ev.Result(task="a", passed=True, status="finished"),
        ev.Result(task="b", passed=True, status="finished"),
        ev.Result(task="c", status="failed", error="Model unavailable: disconnected"),
    ])

    assert board["tasks"] == 3
    assert board["graded"] == 2
    assert board["errored"] == 1
    assert board["pass_rate"] == 1.0
    assert board["results"]["c"]["errored"] is True


def test_a_board_of_nothing_but_errors_does_not_divide_by_zero():
    board = ev.summarize([ev.Result(task="a", status="failed", error="endpoint down")])
    assert board["pass_rate"] == 0.0
    assert board["graded"] == 0


def test_the_report_does_not_call_an_errored_task_a_regression():
    """It passed last time and there is no verdict this time. Reporting BROKE would send
    someone to read a prompt when they should be reading a server log."""
    now = [
        ev.Result(task="a", passed=True, status="finished", steps=3),
        ev.Result(task="b", status="failed", steps=9, error="Model unavailable: gone"),
    ]
    against = {
        "recorded": "2026-08-01T00:00:00",
        "pass_rate": 1.0,
        "results": {"a": {"passed": True}, "b": {"passed": True}},
    }
    text = ev.report(now, against=against)

    assert "1/1 passed" in text
    assert "1 errored and not scored" in text
    assert "ERROR b" in text
    assert "BROKE" not in text


def test_the_report_does_not_credit_a_reboot_as_a_fix():
    """The previous board had no verdict for this task, so passing now is not a fix."""
    now = [ev.Result(task="a", passed=True, status="finished", steps=3)]
    against = {
        "recorded": "2026-08-01T00:00:00",
        "pass_rate": 0.0,
        "errored": 1,
        "results": {"a": {"passed": False, "errored": True}},
    }
    text = ev.report(now, against=against)

    assert "no verdict last run" in text
    assert "FIXED" not in text


def test_the_scoreboard_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "SCOREBOARD", str(tmp_path / "agent"))
    board = ev.summarize([ev.Result(task="a", passed=True, status="finished", steps=2)])
    path = ev.save(board)

    assert os.path.isfile(path)
    assert ev.previous()["pass_rate"] == 1.0
    with open(path, encoding="utf-8") as handle:
        assert json.load(handle)["results"]["a"]["passed"] is True


def test_no_previous_scoreboard_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ev, "SCOREBOARD", str(tmp_path / "never-written"))
    assert ev.previous() is None


def test_the_command_line_can_list_and_filter(capsys):
    assert ev.main(["--list"]) == 0
    listed = capsys.readouterr().out
    assert "rename_across_files" in listed

    assert ev.main(["--list", "--task", "add_a_helper"]) == 0
    filtered = capsys.readouterr().out
    assert "add_a_helper" in filtered
    assert "rename_across_files" not in filtered

    with pytest.raises(SystemExit):
        ev.main(["--task", "no_such_task"])


def test_the_fixtures_are_copied_not_used_in_place(tmp_path):
    """Every run starts from the same bytes, or the second run of a task is a different
    task."""
    task = next(t for t in ev.load() if t.name == "add_a_helper")
    first = ev.checkout(task, str(tmp_path / "one"))
    shutil.rmtree(os.path.join(first, "units"))
    second = ev.checkout(task, str(tmp_path / "two"))

    assert os.path.isdir(os.path.join(second, "units"))
    assert ev.tree_hashes(second) == ev.tree_hashes(task.repo_path)
