"""A run may not report success on edits it never executed.

`AGENT_SYSTEM` has always said "a change you have not executed is a guess", and until
now that was advice a run could ignore: edit five files, run nothing, call finish, be
recorded as a success. This is the same rule the note pack and the source log already
follow — ADR 0013's "anything that must happen is done by the loop" — applied to the
moment the claim is actually made.

The balance being struck is the interesting part. Refusing forever would be worse than
never checking: a repository with no test suite, a broken toolchain or a genuinely
blocked task all have to be able to end. So the loop objects exactly once, and what it
buys is not a green tree but an honest summary.
"""

import pytest

import skippy_agent
import skippy_exec
from skippy_sandbox import Sandbox
from tests import fake_llm as fl


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "calc").mkdir(parents=True)
    (root / "calc" / "ops.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_ops.py").write_text(
        "from calc.ops import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    return root


@pytest.fixture
def box(repo):
    return Sandbox([str(repo)])


def edit(call_id="c1", path="calc/ops.py", search="a + b", replace="a + b  # noqa"):
    return fl.tool_call(
        "apply_patch", call_id=call_id,
        edits=[{"path": path, "action": "edit", "search": search, "replace": replace}],
    )


def finish(call_id="c9", summary="Done."):
    return fl.tool_call("finish", call_id=call_id, summary=summary)


def command(cmd, call_id="c2"):
    return fl.tool_call("run_command", call_id=call_id, command=cmd)


async def run(box, script, llm, **kwargs):
    llm.load(script)
    return await skippy_agent.run_task("Change the thing", box, **kwargs)


# -- what counts as evidence ------------------------------------------------

def test_a_test_run_is_evidence_and_a_git_diff_is_not():
    """`git diff` shows what changed, which is not the same as showing it works —
    counting it would let a run discharge the requirement by reading its own patch."""
    assert skippy_exec.is_verification("python -m pytest -q")
    assert skippy_exec.is_verification("pytest")
    assert skippy_exec.is_verification("ruff check .")
    assert skippy_exec.is_verification("cargo test")
    assert not skippy_exec.is_verification("git diff")
    assert not skippy_exec.is_verification("git status")
    assert not skippy_exec.is_verification("")


def test_a_versioned_python_counts_the_same_as_pytest():
    """The exact gap that once hid a real bug in the command allowlist: `python3.11 -m
    pytest` is how sys.executable spells itself inside a virtualenv."""
    assert skippy_exec.is_verification("python3.11 -m pytest tests/")
    assert skippy_exec.is_verification("python3 -m pytest")


def test_an_unparseable_command_is_not_evidence():
    assert not skippy_exec.is_verification('pytest "unclosed')


# -- the gate ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_finishing_on_an_unrun_edit_is_sent_back_once(box, routed_llm):
    outcome = await run(box, [edit(), finish(), finish(call_id="c10")], routed_llm)

    objections = [o for o in routed_llm.observations() if "have not run anything" in o]
    assert objections
    assert "calc/ops.py" in objections[0]
    # And the second finish is honoured: a run that cannot end is worse than one that
    # ends unverified.
    assert outcome.status == "finished"


@pytest.mark.asyncio
async def test_a_green_test_run_finishes_first_time(box, routed_llm):
    outcome = await run(box, [
        edit(),
        command("python -m pytest -q"),
        finish("c9", "Edited and the suite is green."),
    ], routed_llm)

    assert outcome.status == "finished"
    assert not [o for o in routed_llm.observations() if "have not run anything" in o]


@pytest.mark.asyncio
async def test_a_red_test_run_is_sent_back_with_a_different_message(box, repo, routed_llm):
    """Not the same objection: the model did the thing it was asked to do and the news
    is bad, so the push-back is about not reporting a red tree as done."""
    outcome = await run(box, [
        edit(search="return a + b", replace="return a - b"),
        command("python -m pytest -q"),
        finish(),
        finish(call_id="c10", summary="Left it failing: the change breaks test_add."),
    ], routed_llm)

    objections = [o for o in routed_llm.observations() if "did not pass" in o]
    assert objections
    assert "Do not report this as done" in objections[0]
    assert outcome.status == "finished"
    assert "breaks test_add" in outcome.summary


@pytest.mark.asyncio
async def test_editing_after_a_green_run_invalidates_it(box, routed_llm):
    """"The tests passed" from before an edit is the most misleading state a run can
    finish in, so a patch resets the verdict rather than inheriting it."""
    await run(box, [
        edit(),
        command("python -m pytest -q"),
        edit(call_id="c3", search="def add", replace="def add  # second edit"),
        finish(),
        finish(call_id="c10"),
    ], routed_llm)

    assert [o for o in routed_llm.observations() if "have not run anything" in o]


@pytest.mark.asyncio
async def test_a_run_that_changed_nothing_finishes_freely(box, routed_llm):
    """A question, an investigation, a task that turned out to need no change: there is
    nothing to have broken, so there is nothing to prove."""
    outcome = await run(box, [
        fl.tool_call("read_file", call_id="c1", path="calc/ops.py"),
        finish(summary="No change needed; it already does that."),
    ], routed_llm)

    assert outcome.status == "finished"
    assert routed_llm.remaining == 0


@pytest.mark.asyncio
async def test_a_documentation_change_is_not_asked_to_prove_itself(box, repo, routed_llm):
    """Editing a README cannot break a test, and demanding a suite run for one teaches
    the model to run the suite as a ritual — which is the habit that makes a green tree
    stop meaning anything."""
    (repo / "README.md").write_text("# calc\n")
    outcome = await run(box, [
        edit(path="README.md", search="# calc", replace="# calc\n\nInstall pytest first."),
        finish(summary="Documented it."),
    ], routed_llm)

    assert outcome.status == "finished"
    assert routed_llm.remaining == 0


@pytest.mark.asyncio
async def test_a_code_change_alongside_a_doc_change_still_counts(box, repo, routed_llm):
    """The exception is documentation, not "the run also touched documentation"."""
    (repo / "README.md").write_text("# calc\n")
    await run(box, [
        fl.tool_calls(
            ("apply_patch", {"edits": [
                {"path": "README.md", "action": "edit", "search": "# calc",
                 "replace": "# calc\n\nNotes."},
                {"path": "calc/ops.py", "action": "edit", "search": "a + b",
                 "replace": "a + b  # touched"},
            ]}),
        ),
        finish(),
        finish(call_id="c10"),
    ], routed_llm)

    objections = [o for o in routed_llm.observations() if "have not run anything" in o]
    assert objections
    assert "calc/ops.py" in objections[0]
    # The README is not what it is being asked about.
    assert "README.md" not in objections[0]


@pytest.mark.asyncio
async def test_the_objection_is_visible_to_the_client(box, routed_llm):
    """A run that visibly pushed back on itself is worth seeing in the timeline; a
    silent extra step looks like the model dithering."""
    events = []

    async def emit(event):
        events.append(event)

    await run(box, [edit(), finish(), finish(call_id="c10")], routed_llm, emit=emit)
    refusals = [e for e in events if e["type"] == "agent_finish_refused"]
    assert len(refusals) == 1
    assert "have not run anything" in refusals[0]["reason"]


@pytest.mark.asyncio
async def test_the_transcript_stays_valid_when_a_finish_is_refused(box, routed_llm):
    """Native tool calling requires every call to be answered exactly once, including
    the one the loop declined to act on."""
    await run(box, [edit(), finish(), finish(call_id="c10")], routed_llm)

    for request in routed_llm.requests:
        messages = request["messages"]
        for index, message in enumerate(messages):
            if message.get("role") != "assistant" or not message.get("tool_calls"):
                continue
            expected = [c["id"] for c in message["tool_calls"]]
            answers = []
            for following in messages[index + 1:]:
                if following.get("role") != "tool":
                    break
                answers.append(following["tool_call_id"])
            assert answers == expected


@pytest.mark.asyncio
async def test_a_refused_finish_does_not_end_the_run_early(box, routed_llm):
    """The model must be able to go and do the thing it was asked for, which means the
    loop keeps going rather than stopping with an unhappy status."""
    outcome = await run(box, [
        edit(),
        finish(),
        command("python -m pytest -q", call_id="c3"),
        finish(call_id="c10", summary="Ran the suite; green."),
    ], routed_llm)

    assert outcome.status == "finished"
    assert "green" in outcome.summary
    assert outcome.steps == 4


# -- what the run leaves behind ---------------------------------------------

@pytest.mark.asyncio
async def test_a_green_command_is_remembered_as_how_this_project_is_tested(
    box, routed_llm, tmp_path
):
    """Working out the test command costs a session several steps of guessing, and the
    answer is the same every time. The loop watched it happen, so nothing has to ask."""
    import skippy_memory

    memory_root = str(tmp_path / "projects")
    routed_llm.load([edit(), command("python -m pytest -q"), finish()])
    await skippy_agent.run_task("Change it", box, memory_root=memory_root)

    memory = skippy_memory.open_project(root=memory_root, workspace_roots=list(box.roots))
    assert memory.meta["conventions"]["test command"] == "python -m pytest -q"
    # And it is in the block the next session opens with, rather than waiting to be
    # asked for.
    assert "python -m pytest -q" in memory.opening_context()


@pytest.mark.asyncio
async def test_a_failing_command_is_not_remembered_as_the_way_to_test(
    box, routed_llm, tmp_path
):
    """"How you test this project" is a command that worked. A red one may be the right
    command or may be a typo, and there is no way to tell from here."""
    import skippy_memory

    memory_root = str(tmp_path / "projects")
    routed_llm.load([
        edit(search="return a + b", replace="return a - b"),
        command("python -m pytest -q"),
        finish(),
        finish(call_id="c10", summary="Left it red."),
    ])
    await skippy_agent.run_task("Break it", box, memory_root=memory_root)

    memory = skippy_memory.open_project(root=memory_root, workspace_roots=list(box.roots))
    assert "test command" not in (memory.meta.get("conventions") or {})


@pytest.mark.asyncio
async def test_running_out_of_steps_is_unaffected(box, routed_llm):
    """The gate must not turn a budget exhaustion into something else."""
    routed_llm.load([edit(call_id=f"c{n}") for n in range(6)])
    outcome = await skippy_agent.run_task("Change it", box, max_steps=3)
    assert outcome.status == "max_steps"
