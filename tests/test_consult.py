"""Putting one hard question to a stronger model, and nothing else.

`consult` is not a sub-run: there is no child loop, no toolset, no budget of steps —
the parent packages a question and the files it names, the reasoner thinks once, and
a single observation comes back. What the tests pin is the discipline around that
call: which modes are offered the tool and only when it could actually succeed, that
RE material cannot reach an off-machine reasoner without its own explicit consent on
top of the global cloud gate, that failure is honest (an errored consult never reads
like an answer), and that the attached files reach the reasoner without ever landing
in the parent's transcript.
"""

import pytest

import prompts
import skippy_agent
import skippy_dispatch
import skippy_llm
import tool_schemas
from skippy_sandbox import Sandbox
from tests import fake_llm as fl


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch):
    """Registry state must match this test's env, before and after. Same pattern as
    routed_llm: undo the env first, then rebuild, so no test leaves a reasoner
    configured for the rest of the suite."""
    skippy_llm.reload_registry()
    yield
    monkeypatch.undo()
    skippy_llm.reload_registry()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "net").mkdir(parents=True)
    (root / "net" / "client.py").write_text(
        "RETRY_LIMIT = 3\n\n\ndef fetch(url):\n    for attempt in range(RETRY_LIMIT):\n"
        "        pass\n"
    )
    return root


@pytest.fixture
def box(repo):
    return Sandbox([str(repo)])


@pytest.fixture
def local_reasoner(routed_llm, monkeypatch):
    """Both consult roles pointed at the scripted server, so consults can run offline."""
    for prefix in ("SKIPPY_REASONER", "SKIPPY_REASONER_RE"):
        monkeypatch.setenv(f"{prefix}_URL", routed_llm.base_url)
        monkeypatch.setenv(f"{prefix}_MODEL", "fake/test-model")
    skippy_llm.reload_registry()
    yield routed_llm


def consult(question="Which approach should I take?", call_id="c1", **extra):
    return fl.tool_call("consult", call_id=call_id, question=question, **extra)


def finish(call_id="c9", summary="Done.", **extra):
    return fl.tool_call("finish", call_id=call_id, summary=summary, **extra)


ANSWER = "Take approach B: RETRY_LIMIT is shared, so widening it breaks transport."


def answer_with(text):
    async def fake_query(messages, **kwargs):
        fake_query.calls.append({"messages": list(messages), **kwargs})
        return text
    fake_query.calls = []
    return fake_query


# -- who is offered the tool, and when ---------------------------------------

def test_the_schema_is_offered_to_both_working_modes_and_not_to_readers():
    coding = {t["function"]["name"] for t in tool_schemas.workspace_tools()}
    re_mode = {t["function"]["name"] for t in tool_schemas.re_tools()}
    child = {t["function"]["name"] for t in tool_schemas.investigation_tools()}
    assert "consult" in coding
    assert "consult" in re_mode
    # Same recursion bound as investigate: the child is not offered the tool at all.
    assert "consult" not in child


def test_coding_mode_withholds_consult_while_the_cloud_gate_is_shut(box):
    """The default reasoner is hosted, so with the gate shut every call could only
    fail. A tool that can only say no is not offered."""
    skippy_llm.reload_registry()
    loop = skippy_agent.AgentLoop("t", box)
    assert "consult" not in {t["function"]["name"] for t in loop.tools()}


def test_coding_mode_offers_consult_once_cloud_is_allowed(box, monkeypatch):
    monkeypatch.setenv("SKIPPY_ALLOW_CLOUD", "1")
    skippy_llm.reload_registry()
    loop = skippy_agent.AgentLoop("t", box)
    assert "consult" in {t["function"]["name"] for t in loop.tools()}


def test_re_mode_withholds_consult_while_no_local_model_is_configured(box, tmp_path):
    skippy_llm.reload_registry()
    loop = skippy_agent.AgentLoop("t", box, mode="re", notes_root=str(tmp_path / "notes"))
    assert "consult" not in {t["function"]["name"] for t in loop.tools()}


def test_re_mode_offers_consult_once_local_weights_are_named(box, tmp_path, monkeypatch):
    monkeypatch.setenv("SKIPPY_REASONER_RE_MODEL", "mlx-community/some-thinker")
    skippy_llm.reload_registry()
    loop = skippy_agent.AgentLoop("t", box, mode="re", notes_root=str(tmp_path / "notes"))
    assert "consult" in {t["function"]["name"] for t in loop.tools()}


# -- RE containment -----------------------------------------------------------

@pytest.fixture
def re_loop_with_cloud_reasoner(box, tmp_path, monkeypatch):
    """An RE run whose reasoner_re has been (mis)pointed at a hosted API, with the
    global cloud gate open — the configuration where only the RE gate stands between
    a disassembly listing and the internet."""
    monkeypatch.setenv("SKIPPY_REASONER_RE_URL", "https://api.example.com/v1/chat/completions")
    monkeypatch.setenv("SKIPPY_REASONER_RE_MODEL", "hosted/thinker")
    monkeypatch.setenv("SKIPPY_ALLOW_CLOUD", "1")
    skippy_llm.reload_registry()
    yield skippy_agent.AgentLoop(
        "t", box, mode="re", notes_root=str(tmp_path / "notes"), remember=False
    )
    skippy_llm.reload_registry()


def test_re_material_does_not_reach_an_offmachine_reasoner_by_default(
    re_loop_with_cloud_reasoner,
):
    """The global gate alone must not open the RE door: withheld from the menu, and
    refused by name if called anyway."""
    loop = re_loop_with_cloud_reasoner
    assert "consult" not in {t["function"]["name"] for t in loop.tools()}


@pytest.mark.asyncio
async def test_the_re_refusal_names_the_consent_variable(re_loop_with_cloud_reasoner):
    result = await re_loop_with_cloud_reasoner._consult({"question": "What is this?"})
    assert not result.ok
    assert "SKIPPY_RE_ALLOW_CLOUD" in result.summary
    assert "never leaves this machine" in result.summary


def test_the_re_door_opens_only_with_its_own_consent(
    re_loop_with_cloud_reasoner, monkeypatch
):
    """The operator may decide RE consults can go off-machine — but it takes the
    second variable, set deliberately, on top of the first."""
    monkeypatch.setenv("SKIPPY_RE_ALLOW_CLOUD", "1")
    loop = re_loop_with_cloud_reasoner
    assert "consult" in {t["function"]["name"] for t in loop.tools()}


# -- the call itself ----------------------------------------------------------

@pytest.mark.asyncio
async def test_the_answer_comes_back_and_the_files_never_enter_the_parent(
    box, local_reasoner
):
    """The reasoner sees the attached file; the parent sees only the answer."""
    local_reasoner.load([
        consult(paths=["net/client.py"]),
        fl.text(ANSWER),          # the reasoner's reply
        finish(summary="Chose B."),
    ])
    outcome = await skippy_agent.run_task("Decide the retry design", box)

    assert outcome.status == "finished"
    assert any(ANSWER in o for o in local_reasoner.observations())

    # The reasoner's request carried the prompt, the file, and the question.
    reasoner_requests = [
        r for r in local_reasoner.requests
        if r["messages"] and r["messages"][0].get("content") == prompts.CONSULT_SYSTEM
    ]
    assert len(reasoner_requests) == 1
    sent = reasoner_requests[0]["messages"][1]["content"]
    assert "RETRY_LIMIT = 3" in sent
    assert "Which approach should I take?" in sent

    # The parent's transcript holds the answer, not the file it was reasoned from.
    parent_text = "\n".join(
        str(m.get("content") or "") for m in local_reasoner.last_messages()
    )
    assert ANSWER in parent_text
    assert "def fetch(url)" not in parent_text


@pytest.mark.asyncio
async def test_the_observation_says_who_answered(box, local_reasoner):
    """Attribution is part of honesty: a later reader of the transcript can tell
    this paragraph came from a different model than the one driving."""
    local_reasoner.load([consult(), fl.text(ANSWER), finish()])
    await skippy_agent.run_task("Decide", box)
    consulted = [o for o in local_reasoner.observations() if "Consulted" in o]
    assert consulted
    assert "fake/test-model" in consulted[0]


@pytest.mark.asyncio
async def test_a_question_with_no_question_is_refused(box, local_reasoner):
    local_reasoner.load([
        fl.tool_call("consult", call_id="c1", question="  "),
        finish(),
    ])
    await skippy_agent.run_task("Decide", box)
    assert any("needs a 'question'" in o for o in local_reasoner.observations())


@pytest.mark.asyncio
async def test_a_missing_file_fails_the_whole_consult(box, local_reasoner):
    """Refused whole rather than sent partial: advice about code the reasoner never
    saw is worse than no advice, and nothing downstream could tell."""
    local_reasoner.load([
        consult(paths=["net/client.py", "net/nonexistent.py"]),
        finish(),
    ])
    await skippy_agent.run_task("Decide", box)
    failures = [o for o in local_reasoner.observations() if "Could not attach" in o]
    assert failures
    assert "net/nonexistent.py" in failures[0]


@pytest.mark.asyncio
async def test_a_path_outside_the_sandbox_is_refused(box, local_reasoner, tmp_path):
    outside = tmp_path / "secret.txt"
    outside.write_text("not for the reasoner")
    local_reasoner.load([consult(paths=[str(outside)]), finish()])
    await skippy_agent.run_task("Decide", box)
    refused = [o for o in local_reasoner.observations() if "Could not attach" in o]
    assert refused
    assert "outside the workspace roots" in refused[0]


@pytest.mark.asyncio
async def test_paths_that_arrive_as_a_string_still_work(box, local_reasoner, monkeypatch):
    """The parser fallback hands structured arguments over as raw text, and consult is
    loop-handled so dispatch's type repair never sees it. Both string shapes — a JSON
    array and a bare path — must coerce rather than fail."""
    loop = skippy_agent.AgentLoop("t", box)
    fake = answer_with(ANSWER)
    monkeypatch.setattr(skippy_llm, "query_text", fake)

    for raw in ('["net/client.py"]', "net/client.py"):
        result = await loop._consult({"question": "Q?", "paths": raw})
        assert result.ok, result.summary
        assert result.data["paths"] == ["net/client.py"]


# -- honest failure -----------------------------------------------------------

@pytest.mark.asyncio
async def test_a_dead_reasoner_is_a_failed_result_not_an_answer(box, local_reasoner, monkeypatch):
    loop = skippy_agent.AgentLoop("t", box)

    async def dead(messages, **kwargs):
        raise skippy_llm.ModelError("Role 'reasoner' at ... failed after 3 attempts.")

    monkeypatch.setattr(skippy_llm, "query_text", dead)
    result = await loop._consult({"question": "Q?"})
    assert not result.ok
    assert "consult failed" in result.summary
    assert "no answer" in result.summary


@pytest.mark.asyncio
async def test_an_empty_answer_is_a_failed_result(box, local_reasoner):
    local_reasoner.load([consult(), fl.text(""), finish()])
    await skippy_agent.run_task("Decide", box)
    assert any("returned nothing" in o for o in local_reasoner.observations())


# -- the budget ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_run_cannot_spend_its_life_consulting(box, local_reasoner, monkeypatch):
    loop = skippy_agent.AgentLoop("t", box)
    fake = answer_with(ANSWER)
    monkeypatch.setattr(skippy_llm, "query_text", fake)

    for n in range(skippy_agent.CONSULT_LIMIT):
        assert (await loop._consult({"question": f"Q{n}?"})).ok
    refused = await loop._consult({"question": "One more?"})
    assert not refused.ok
    assert str(skippy_agent.CONSULT_LIMIT) in refused.summary
    assert len(fake.calls) == skippy_agent.CONSULT_LIMIT


@pytest.mark.asyncio
async def test_a_consult_that_never_reached_the_reasoner_costs_nothing(
    box, local_reasoner, monkeypatch
):
    """A bad path is the model's mistake to correct, not a spent escalation."""
    loop = skippy_agent.AgentLoop("t", box)
    fake = answer_with(ANSWER)
    monkeypatch.setattr(skippy_llm, "query_text", fake)

    failed = await loop._consult({"question": "Q?", "paths": ["nope.py"]})
    assert not failed.ok
    for n in range(skippy_agent.CONSULT_LIMIT):
        assert (await loop._consult({"question": f"Q{n}?"})).ok


# -- stray dispatch -----------------------------------------------------------

@pytest.mark.asyncio
async def test_the_dispatcher_refuses_it_coherently(box):
    """It never arrives there — the loop takes it first — but a stray call should say
    what is true rather than 'unknown tool'."""
    result = await skippy_dispatch.dispatch("consult", {"question": "?"}, box)
    assert not result.ok
    assert "agent loop" in result.summary
