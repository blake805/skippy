"""Handing a question to a reader with its own conversation.

This is context management rather than delegation, and the distinction is what the
tests are about. "Which callers depend on this signature" costs fifteen steps of file
reading, and those file contents would otherwise sit in the parent's transcript for the
rest of the run — re-prefilled on every subsequent step, folded eventually, crowding out
the task. The child answers somewhere else and the parent keeps a paragraph.

So the invariants worth pinning are: only the answer comes back, the child cannot edit
or run or spawn, and neither its budget nor its record leaks into the parent's.
"""

import pytest

import skippy_agent
import skippy_dispatch
import tool_schemas
from skippy_sandbox import Sandbox
from tests import fake_llm as fl


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "net").mkdir(parents=True)
    (root / "net" / "client.py").write_text(
        "RETRY_LIMIT = 3\n\n\ndef fetch(url):\n    for attempt in range(RETRY_LIMIT):\n"
        "        pass\n"
    )
    (root / "net" / "transport.py").write_text("from .client import RETRY_LIMIT\n")
    return root


@pytest.fixture
def box(repo):
    return Sandbox([str(repo)])


def investigate(question="Where is the retry limit set?", call_id="c1", **extra):
    return fl.tool_call("investigate", call_id=call_id, question=question, **extra)


def finish(call_id="c9", summary="Done.", **extra):
    return fl.tool_call("finish", call_id=call_id, summary=summary, **extra)


ANSWERED = "RETRY_LIMIT = 3 in net/client.py line 1; transport.py imports it."


# -- the shape of it --------------------------------------------------------

def test_the_reader_can_only_read(box):
    child = skippy_agent.AgentLoop("Where is retry set?", box, mode="investigate")
    offered = {t["function"]["name"] for t in child.tools()}

    assert offered == {"list_dir", "read_file", "grep", "glob_files", "finish"}
    # No recursion: the limit is the absence of the tool rather than a depth counter,
    # because a budget that can spawn things with budgets is not a budget.
    assert "investigate" not in offered
    assert "apply_patch" not in offered
    assert "run_command" not in offered


def test_the_reader_gets_a_short_budget_of_its_own(box):
    child = skippy_agent.AgentLoop("Q?", box, mode="investigate")
    assert child.max_steps == skippy_agent.SUBAGENT_MAX_STEPS
    assert child.max_steps < skippy_agent.DEFAULT_MAX_STEPS


def test_the_reader_has_its_own_prompt(box):
    import prompts

    child = skippy_agent.AgentLoop("Q?", box, mode="investigate")
    assert child.transcript.messages[0]["content"] == prompts.INVESTIGATE_SYSTEM


def test_coding_mode_can_spawn_one_and_the_reader_cannot(box):
    coding = {t["function"]["name"] for t in tool_schemas.workspace_tools()}
    assert "investigate" in coding
    assert "investigate" not in {t["function"]["name"] for t in tool_schemas.investigation_tools()}


@pytest.mark.asyncio
async def test_the_dispatcher_refuses_it_coherently(box):
    """It never arrives there — the loop takes it first — but a stray call should say
    what is true rather than 'unknown tool'."""
    result = await skippy_dispatch.dispatch("investigate", {"question": "?"}, box)
    assert not result.ok
    assert "agent loop" in result.summary


# -- only the answer comes back ---------------------------------------------

@pytest.mark.asyncio
async def test_the_parent_gets_the_answer_and_not_the_reading(box, routed_llm):
    """The whole point: the parent pays a paragraph for work that cost several steps of
    file contents, and none of those contents land in its transcript."""
    routed_llm.load([
        investigate(),
        # the child's run
        fl.tool_call("grep", call_id="g1", pattern="RETRY_LIMIT"),
        fl.tool_call("read_file", call_id="g2", path="net/client.py"),
        fl.tool_call("finish", call_id="g3", summary=ANSWERED),
        # back in the parent
        finish(summary="Found it."),
    ])
    outcome = await skippy_agent.run_task("Raise the retry limit", box)

    assert outcome.status == "finished"
    observations = routed_llm.observations()
    assert any(ANSWERED in o for o in observations)

    # The parent's final transcript holds the answer and not the file it came from.
    last = routed_llm.last_messages()
    text = "\n".join(str(m.get("content") or "") for m in last)
    assert ANSWERED in text
    assert "def fetch(url)" not in text


@pytest.mark.asyncio
async def test_a_reader_that_ran_out_of_steps_is_reported_as_incomplete(box, routed_llm):
    """Its last words read exactly like an answer, which is why the failure has to be
    named rather than passed up as one."""
    routed_llm.load(
        [investigate()]
        + [fl.tool_call("grep", call_id=f"g{n}", pattern=f"thing{n}") for n in range(12)]
        + [finish(summary="Gave up on that.")]
    )
    await skippy_agent.run_task("Raise the retry limit", box)

    incomplete = [o for o in routed_llm.observations() if "did not finish" in o]
    assert incomplete
    assert "ERROR" in incomplete[0]


@pytest.mark.asyncio
async def test_a_question_with_no_question_is_refused(box, routed_llm):
    routed_llm.load([
        fl.tool_call("investigate", call_id="c1", question="   "),
        finish(),
    ])
    await skippy_agent.run_task("Do the thing", box)
    assert any("needs a 'question'" in o for o in routed_llm.observations())


@pytest.mark.asyncio
async def test_a_run_cannot_spend_its_life_spawning_readers(box, routed_llm):
    script = []
    for n in range(skippy_agent.SUBAGENT_LIMIT + 1):
        script.append(investigate(f"Question {n}?", call_id=f"c{n}"))
        script.append(fl.tool_call("finish", call_id=f"g{n}", summary=f"Answer {n}."))
    script.append(finish(summary="Enough."))
    routed_llm.load(script)

    await skippy_agent.run_task("Do the thing", box)
    refusals = [o for o in routed_llm.observations() if "investigations" in o]
    assert refusals
    assert str(skippy_agent.SUBAGENT_LIMIT) in refusals[0]


# -- what it must not leak --------------------------------------------------

@pytest.mark.asyncio
async def test_the_reader_records_no_session_of_its_own(box, routed_llm, tmp_path):
    """A fragment of an investigation is not something a later session should open
    with, and the answer is already going where it is needed."""
    import skippy_memory

    memory_root = str(tmp_path / "projects")
    routed_llm.load([
        investigate(),
        fl.tool_call("finish", call_id="g1", summary=ANSWERED),
        finish(summary="Raised it."),
    ])
    await skippy_agent.run_task("Raise the retry limit", box, memory_root=memory_root)

    memory = skippy_memory.open_project(root=memory_root, workspace_roots=list(box.roots))
    sessions = memory.sessions(limit=99)
    assert len(sessions) == 1
    assert "Raise the retry limit" in sessions[0]["task"]


@pytest.mark.asyncio
async def test_the_readers_steps_do_not_come_out_of_the_parents_budget(box, routed_llm):
    """They cost wall time, not steps: the parent spent one call on the question."""
    routed_llm.load([
        investigate(),
        fl.tool_call("grep", call_id="g1", pattern="RETRY"),
        fl.tool_call("read_file", call_id="g2", path="net/client.py"),
        fl.tool_call("finish", call_id="g3", summary=ANSWERED),
        finish(summary="Done."),
    ])
    outcome = await skippy_agent.run_task("Raise the retry limit", box)

    assert outcome.steps == 2


@pytest.mark.asyncio
async def test_the_reading_is_visible_in_the_timeline(box, routed_llm):
    """A silent gap on an expensive step looks like a hang; the events are forwarded
    with a marker so a client can nest them under the call that caused them."""
    events = []

    async def emit(event):
        events.append(event)

    routed_llm.load([
        investigate(),
        fl.tool_call("grep", call_id="g1", pattern="RETRY"),
        fl.tool_call("finish", call_id="g2", summary=ANSWERED),
        finish(),
    ])
    await skippy_agent.run_task("Raise the retry limit", box, emit=emit)

    sub = [e for e in events if e.get("sub")]
    assert sub
    assert {e["parent_step"] for e in sub} == {1}
    assert any(e["type"] == "agent_tool_call" and e["tool"] == "grep" for e in sub)
    # The parent's own events are not marked.
    assert not [e for e in events if e.get("sub") and e.get("tool") == "investigate"]


@pytest.mark.asyncio
async def test_the_reader_shares_the_sandbox_and_not_a_way_out_of_it(box, routed_llm, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("not for the agent")

    routed_llm.load([
        investigate("What is in ../secret.txt?"),
        fl.tool_call("read_file", call_id="g1", path=str(outside)),
        fl.tool_call("finish", call_id="g2", summary="Could not read it."),
        finish(),
    ])
    await skippy_agent.run_task("Have a look", box)

    assert any("Sandbox violation" in o for o in routed_llm.observations())


@pytest.mark.asyncio
async def test_a_where_hint_reaches_the_reader(box, routed_llm):
    routed_llm.load([
        investigate(call_id="c1", where="net/"),
        fl.tool_call("finish", call_id="g1", summary=ANSWERED),
        finish(),
    ])
    await skippy_agent.run_task("Raise the retry limit", box)

    openings = [
        m["content"]
        for request in routed_llm.requests
        for m in request["messages"]
        if m.get("role") == "user" and "Start from net/" in (m.get("content") or "")
    ]
    assert openings
