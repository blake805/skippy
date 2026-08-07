"""The gate wired into the two conversational lanes.

Both lanes do the same three things and deliver them differently: answer now, check
behind the answer, follow up when it lands. What is tested here is the wiring rather
than the deciding — that a check never blocks or delays a reply, that it does not take
the client's one run slot, that the follow-up arrives marked as one, and that the
persona is only told it is checking when a check actually started.
"""

import asyncio

import pytest

import skippy_gate
import skippy_research
import skippy_tasks
import skippy_voice
from skippy_tasks import TaskRunner
from skippy_voice import EnergyVAD, VoiceSession


@pytest.fixture(autouse=True)
def keyed(monkeypatch):
    monkeypatch.setenv(skippy_research.TAVILY_KEY_ENV, "tvly-test")


@pytest.fixture(autouse=True)
def no_real_research(monkeypatch, tmp_path):
    """Every check in this file resolves through a scripted answer.

    The research loop has its own tests; here it stands in for "something slow that
    finishes later", which is the only property these tests care about.
    """
    async def fake_check(question, conversation, **kwargs):
        # Mirrors the real one's contract with the conversation — recall first, spend a
        # run only on something new — because that contract is what several of these
        # tests are about.
        remembered = conversation.recall(question)
        if remembered is not None:
            return remembered
        conversation.runs += 1
        result = skippy_gate.Result(
            question=question,
            answer=f"Checked: {question} — the answer is 400 IPM [S1].",
            brief_id="brief-1",
            sources=2,
        )
        conversation.remember(result)
        return result

    monkeypatch.setattr(skippy_gate, "check", fake_check)


class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    def types(self):
        return [message.get("type") for message in self.sent]

    def chats(self):
        return [m.get("content", "") for m in self.sent if m.get("type") == "chat"]

    def research(self):
        return [m for m in self.sent if m.get("kind") == "research"]


class FakeHub:
    def __init__(self):
        self.active_connections = {}


@pytest.fixture
def hub():
    return FakeHub()


@pytest.fixture
def socket(hub):
    connection = FakeSocket()
    hub.active_connections["phone"] = connection
    return connection


@pytest.fixture
def runner(hub, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    return TaskRunner(hub, roots_provider=lambda: [str(root)])


def script(monkeypatch, module, *answers):
    """Answer the lane's model calls in order: gate, reply, self-check."""
    queue = list(answers)

    async def fake_query(messages, role="fast", **kwargs):
        return queue.pop(0) if queue else ""

    monkeypatch.setattr(module.skippy_llm, "query_text", fake_query)


async def drain(runner, client_id="phone", timeout=5.0):
    """Wait for the turn and then for anything it started behind itself."""
    task = runner._tasks.get(client_id)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
    for _ in range(200):
        if not runner._side_tasks:
            return
        await asyncio.sleep(0.01)


# -- the chat lane ----------------------------------------------------------

@pytest.mark.asyncio
async def test_a_factual_turn_is_answered_now_and_checked_behind_it(runner, socket, monkeypatch):
    script(
        monkeypatch, skippy_tasks,
        '{"decision": "research", "question": "What is the Series 4 rapid rate?"}',
        "Somewhere around 400 IPM, though let me confirm that.",
    )
    await runner.start("phone", {"text": "what is the current rapid rate on it", "mode": "Chat"})
    await drain(runner)

    kinds = socket.types()
    # The reply lands first and the turn closes; the check arrives afterwards.
    assert kinds.index("chat") < kinds.index("done")
    assert "400 IPM" in socket.chats()[0]

    follow_ups = socket.research()
    assert len(follow_ups) == 1
    assert follow_ups[0]["type"] == "chat"
    assert "Series 4 rapid rate" in follow_ups[0]["question"]
    assert follow_ups[0]["brief"] == "brief-1"
    assert follow_ups[0]["sources"] == 2
    # A client that ignores `kind` still renders something sensible.
    assert "400 IPM" in follow_ups[0]["content"]


@pytest.mark.asyncio
async def test_the_check_arrives_after_done_rather_than_holding_the_turn_open(
    runner, socket, monkeypatch
):
    """The whole UX rests on this: a conversation that stops dead while Skippy reads
    the internet is worse than one that answers from memory."""
    script(
        monkeypatch, skippy_tasks,
        '{"decision": "research", "question": "Is it still supported?"}',
        "I believe so, checking.",
    )
    await runner.start("phone", {"text": "is that board still supported these days", "mode": "Chat"})
    await drain(runner)

    kinds = socket.types()
    assert kinds[-1] == "chat"
    assert kinds.index("done") < len(kinds) - 1


@pytest.mark.asyncio
async def test_the_persona_is_told_it_is_checking(runner, socket, monkeypatch):
    seen = []

    async def fake_query(messages, role="fast", **kwargs):
        seen.append(messages)
        if len(seen) == 1:
            return '{"decision": "research", "question": "What is the rapid rate?"}'
        return "Let me check that one."

    monkeypatch.setattr(skippy_tasks.skippy_llm, "query_text", fake_query)
    await runner.start("phone", {"text": "what is the current rapid rate", "mode": "Chat"})
    await drain(runner)

    notes = [m for m in seen[1] if m["role"] == "system" and "SYSTEM NOTE" in m["content"]]
    assert notes and "do not invent" in notes[0]["content"]


@pytest.mark.asyncio
async def test_ideation_is_answered_without_a_check(runner, socket, monkeypatch):
    script(monkeypatch, skippy_tasks, "Aluminum, obviously. It machines like butter.")
    await runner.start("phone", {"text": "what if we made the enclosure aluminum", "mode": "Chat"})
    await drain(runner)

    assert socket.research() == []
    assert "Aluminum" in socket.chats()[0]


@pytest.mark.asyncio
async def test_a_hedged_answer_is_checked_after_the_fact(runner, socket, monkeypatch):
    """The second layer, end to end: the gate let it through and the model's own
    verdict on what it said sent it back."""
    # No pre-answer classifier call here: nothing in the wording dates the question, so
    # the cheap layer lets it through and the model's own verdict is what catches it.
    script(
        monkeypatch, skippy_tasks,
        "I think it is 2.7.1, if I remember right.",
        '{"confidence": 0.3, "checkable": ["firmware 2.7.1"], "question": "What firmware?"}',
    )
    await runner.start("phone", {"text": "which firmware does that spindle need", "mode": "Chat"})
    await drain(runner)

    follow_ups = socket.research()
    assert len(follow_ups) == 1
    assert "What firmware?" in follow_ups[0]["question"]


@pytest.mark.asyncio
async def test_a_confident_answer_is_left_alone(runner, socket, monkeypatch):
    script(
        monkeypatch, skippy_tasks,
        "Water boils at 100 degrees at sea level.",
        '{"confidence": 0.98, "checkable": [], "question": ""}',
    )
    await runner.start("phone", {"text": "what temperature does water boil at", "mode": "Chat"})
    await drain(runner)
    assert socket.research() == []


@pytest.mark.asyncio
async def test_one_turn_never_starts_two_checks(runner, socket, monkeypatch):
    """The pre-answer gate already sent it; grading an answer that was hedged on
    purpose must not send it again."""
    script(
        monkeypatch, skippy_tasks,
        '{"decision": "research", "question": "What is the rapid rate?"}',
        "Checking, but I think 400 IPM.",
        '{"confidence": 0.1, "checkable": ["400 IPM"], "question": "What is the rapid rate?"}',
    )
    await runner.start("phone", {"text": "what is the current rapid rate", "mode": "Chat"})
    await drain(runner)
    assert len(socket.research()) == 1


@pytest.mark.asyncio
async def test_a_check_does_not_take_the_clients_run_slot(runner, socket, monkeypatch):
    """Otherwise asking a question with a fact in it would lock the conversation until
    the internet answered."""
    script(
        monkeypatch, skippy_tasks,
        '{"decision": "research", "question": "What is the rapid rate?"}',
        "Checking.",
    )
    await runner.start("phone", {"text": "what is the current rapid rate", "mode": "Chat"})
    await drain(runner)
    assert not runner.is_running("phone")


@pytest.mark.asyncio
async def test_the_budget_is_spent_across_turns_not_reset_by_them(runner, socket, monkeypatch):
    """A budget that reset every message would not be a budget."""
    runner.conversation("phone").max_runs = 2
    for n in range(4):
        script(
            monkeypatch, skippy_tasks,
            f'{{"decision": "research", "question": "Question {n}?"}}',
            "Checking.",
        )
        await runner.start("phone", {"text": f"what is the latest thing {n}", "mode": "Chat"})
        await drain(runner)

    assert len(socket.research()) == 2
    assert runner.conversation("phone").runs == 2


@pytest.mark.asyncio
async def test_a_question_asked_twice_is_checked_once(runner, socket, monkeypatch):
    for _ in range(2):
        script(
            monkeypatch, skippy_tasks,
            '{"decision": "research", "question": "What is the rapid rate?"}',
            "Checking.",
        )
        await runner.start("phone", {"text": "what is the current rapid rate", "mode": "Chat"})
        await drain(runner)

    # Two follow-ups, because the user asked twice and deserves an answer twice — but
    # only one run was spent.
    assert len(socket.research()) == 2
    assert runner.conversation("phone").runs == 1


@pytest.mark.asyncio
async def test_a_broken_gate_does_not_break_the_conversation(runner, socket, monkeypatch):
    calls = {"n": 0}

    async def fake_query(messages, role="fast", **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("classifier is down")
        return "Answering anyway."

    monkeypatch.setattr(skippy_tasks.skippy_llm, "query_text", fake_query)
    await runner.start("phone", {"text": "what is the current rapid rate", "mode": "Chat"})
    await drain(runner)

    assert "Answering anyway." in socket.chats()
    assert socket.types()[-1] == "done"


@pytest.mark.asyncio
async def test_shutdown_does_not_wait_for_a_check(runner, socket, monkeypatch):
    """Nobody is blocked on one, and its sources are written to the brief as they are
    read, so a shutdown that waited for the internet would not be a shutdown."""
    started = asyncio.Event()

    async def slow_check(question, conversation, **kwargs):
        started.set()
        await asyncio.sleep(30)
        raise AssertionError("should have been cancelled")

    monkeypatch.setattr(skippy_gate, "check", slow_check)
    script(
        monkeypatch, skippy_tasks,
        '{"decision": "research", "question": "q?"}',
        "Checking.",
    )
    await runner.start("phone", {"text": "what is the current rapid rate", "mode": "Chat"})
    task = runner._tasks.get("phone")
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
    await asyncio.wait_for(started.wait(), timeout=5.0)

    await asyncio.wait_for(runner.shutdown(), timeout=5.0)
    await asyncio.sleep(0)
    assert all(t.cancelled() or t.done() for t in runner._side_tasks)


# -- the voice lane ---------------------------------------------------------

def make_session(monkeypatch):
    monkeypatch.setattr(skippy_voice, "build_vad", EnergyVAD)

    async def send_json(payload):
        pass

    async def send_bytes(data):
        pass

    return VoiceSession(websocket=None, send_json=send_json, send_bytes=send_bytes)


@pytest.mark.asyncio
async def test_voice_checks_in_the_background_and_speaks_up_after(monkeypatch):
    session = make_session(monkeypatch)
    announced = []
    session.announce = announced.append

    script(
        monkeypatch, skippy_gate,
        '{"decision": "research", "question": "What is the Series 4 rapid rate?"}',
    )
    started = await session._maybe_check("what is the current rapid rate on the series 4")
    assert started
    # The persona is told to acknowledge, out loud, and not to invent the answer.
    note = skippy_gate.acknowledgment(session._checking, spoken=True)
    assert "out loud" in note

    await asyncio.gather(*session._background)
    assert len(announced) == 1
    assert "I checked that." in announced[0]
    assert "400 IPM" in announced[0]
    # Out loud, a wall of citations is unusable; the brief has them.
    assert "brief" in announced[0]


@pytest.mark.asyncio
async def test_voice_leaves_brainstorming_alone(monkeypatch):
    session = make_session(monkeypatch)
    script(monkeypatch, skippy_gate, '{"decision": "research", "question": "x"}')

    assert not await session._maybe_check("what if we made the enclosure aluminum")
    assert session._background == []


@pytest.mark.asyncio
async def test_voice_escalates_a_hedged_answer_after_speaking(monkeypatch):
    session = make_session(monkeypatch)
    announced = []
    session.announce = announced.append

    script(
        monkeypatch, skippy_gate,
        '{"confidence": 0.2, "checkable": ["2.7.1"], "question": "What firmware?"}',
    )
    await session._check_after("which firmware", "I think 2.7.1, off the top of my head.")
    await asyncio.gather(*session._background)

    assert len(announced) == 1
    assert "What firmware?" in announced[0]


@pytest.mark.asyncio
async def test_voice_stops_checking_when_the_budget_is_gone(monkeypatch):
    session = make_session(monkeypatch)
    session.research.max_runs = 1
    session.research.runs = 1

    script(monkeypatch, skippy_gate, '{"decision": "research", "question": "Something new?"}')
    assert not await session._maybe_check("what is the latest firmware release")
    assert session._background == []


@pytest.mark.asyncio
async def test_a_failed_check_is_admitted_out_loud(monkeypatch):
    session = make_session(monkeypatch)
    announced = []
    session.announce = announced.append

    async def failing_check(question, conversation, **kwargs):
        return skippy_gate.Result(question=question, error="the search backend was down")

    monkeypatch.setattr(skippy_gate, "check", failing_check)
    await session._deliver_check(skippy_gate.Decision(True, question="q?"))

    assert len(announced) == 1
    assert "could not" in announced[0]
    assert "unverified" in announced[0]


@pytest.mark.asyncio
async def test_an_action_utterance_is_not_also_researched(monkeypatch):
    """Two cold classifier calls before a spoken reply would cost the second of
    latency this lane exists to protect."""
    session = make_session(monkeypatch)
    calls = {"n": 0}

    async def counting(messages, role="fast", **kwargs):
        calls["n"] += 1
        return '{"action": "none"}'

    monkeypatch.setattr(skippy_voice.skippy_llm, "query_text", counting)
    monkeypatch.setattr(skippy_gate.skippy_llm, "query_text", counting)
    session.history = [{"role": "user", "content": "start a task to fix the failing tests"}]

    # The router runs; it is the gate that must not, when an action was performed.
    assert await session._route_and_act() is None
    assert calls["n"] == 1
