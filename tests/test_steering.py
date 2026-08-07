"""Talking to a run that is already working.

Before this, the only thing you could say to a working agent was "stop". Watching one
head down the wrong path meant killing it and starting over, which throws away the good
half of the work along with the bad — and the wrong path is usually only visible once it
has been taken, so a better opening prompt is not the fix.

The design rests on the transcript being append-only: a steering message is an ordinary
user turn arriving late, which costs nothing and breaks no prompt cache. Editing the
task in place would have meant re-prefilling everything, which is the cost ADR 0001
exists to avoid.
"""

import asyncio

import pytest

import skippy_agent
import skippy_tasks
from skippy_sandbox import Sandbox
from tests import fake_llm as fl


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "calc").mkdir(parents=True)
    (root / "calc" / "ops.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "calc" / "other.py").write_text("def sub(a, b):\n    return a - b\n")
    return root


@pytest.fixture
def box(repo):
    return Sandbox([str(repo)])


def read(call_id, path="calc/ops.py"):
    return fl.tool_call("read_file", call_id=call_id, path=path)


def finish(call_id="c9", summary="Done."):
    return fl.tool_call("finish", call_id=call_id, summary=summary)


# -- the loop ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_steer_reaches_the_model_as_a_user_turn(box, routed_llm):
    routed_llm.load([read("c1"), read("c2"), finish()])
    loop = skippy_agent.AgentLoop("Tidy the calc package", box)
    assert loop.steer("actually leave other.py alone")

    await loop.run()

    steers = [
        message["content"]
        for request in routed_llm.requests
        for message in request["messages"]
        if message.get("role") == "user" and "other.py alone" in (message.get("content") or "")
    ]
    assert steers
    # Framed as a correction to work in progress. Read as more of the brief, a model
    # tends to start again rather than adjust.
    assert "not as a new task" in steers[0]


@pytest.mark.asyncio
async def test_it_lands_at_the_next_step_not_mid_tool(box, routed_llm):
    """A tool that is midway through writing files finishes first — the same rule
    cancellation follows, for the same reason."""
    routed_llm.load([read("c1"), read("c2"), read("c3"), finish()])
    loop = skippy_agent.AgentLoop("Tidy up", box)

    delivered_at = []
    original = loop._deliver_steering

    async def watch():
        before = len(loop.transcript.messages)
        await original()
        if len(loop.transcript.messages) != before:
            delivered_at.append(loop.step)

    loop._deliver_steering = watch
    loop.steer("stop reading and start editing")
    await loop.run()

    assert delivered_at == [1]


@pytest.mark.asyncio
async def test_several_things_said_at_once_all_arrive_in_order(box, routed_llm):
    routed_llm.load([read("c1"), finish()])
    loop = skippy_agent.AgentLoop("Tidy up", box)
    loop.steer("first thing")
    loop.steer("second thing")

    await loop.run()
    seen = [
        m["content"] for m in loop.transcript.messages
        if m["role"] == "user" and "while you are working" in m["content"]
    ]
    assert len(seen) == 2
    assert "first thing" in seen[0] and "second thing" in seen[1]


@pytest.mark.asyncio
async def test_an_empty_steer_is_not_delivered(box, routed_llm):
    routed_llm.load([finish()])
    loop = skippy_agent.AgentLoop("Tidy up", box)

    assert not loop.steer("   ")
    assert not loop.steer("")
    await loop.run()
    assert loop._steering == []


@pytest.mark.asyncio
async def test_steering_is_visible_to_the_client(box, routed_llm):
    events = []

    async def emit(event):
        events.append(event)

    routed_llm.load([read("c1"), finish()])
    loop = skippy_agent.AgentLoop("Tidy up", box, emit=emit)
    loop.steer("leave other.py alone")
    await loop.run()

    steered = [e for e in events if e["type"] == "agent_steered"]
    assert len(steered) == 1
    assert steered[0]["content"] == "leave other.py alone"


@pytest.mark.asyncio
async def test_the_transcript_still_only_grows(box, routed_llm):
    """The whole reason this is cheap: a late message is an append, not an edit."""
    routed_llm.load([read("c1"), read("c2"), finish()])
    loop = skippy_agent.AgentLoop("Tidy up", box)
    loop.steer("change of plan")
    await loop.run()

    assert routed_llm.prefix_broken_at() is None


# -- the runner and the wire ------------------------------------------------

class FakeSocket:
    def __init__(self):
        self.sent = []

    async def send_json(self, payload):
        self.sent.append(payload)

    def chats(self):
        return [m.get("content", "") for m in self.sent if m.get("type") == "chat"]


class FakeHub:
    def __init__(self):
        self.active_connections = {}


@pytest.fixture
def runner(repo):
    hub = FakeHub()
    hub.active_connections["phone"] = FakeSocket()
    made = skippy_tasks.TaskRunner(hub, roots_provider=lambda: [str(repo)])
    return made, hub.active_connections["phone"]


@pytest.mark.asyncio
async def test_the_runner_passes_a_steer_to_the_running_loop(runner, routed_llm):
    made, socket = runner
    routed_llm.load([read(f"c{n}") for n in range(12)])
    await made.start("phone", {"text": "tidy the package", "mode": "Agent"})

    for _ in range(100):
        if made._loops.get("phone") is not None and made.is_running("phone"):
            break
        await asyncio.sleep(0.01)

    assert made.steer("phone", "leave other.py alone")
    made.cancel("phone")
    task = made._tasks.get("phone")
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)


@pytest.mark.asyncio
async def test_steering_nothing_says_so(runner):
    made, _ = runner
    assert not made.steer("phone", "hello?")


@pytest.mark.asyncio
async def test_a_chat_turn_cannot_be_steered(runner, routed_llm):
    """One completion, already streaming. There is no step boundary to land on."""
    made, socket = runner
    routed_llm.load([fl.text("Thinking about it.")])
    await made.start("phone", {"text": "hey skippy", "mode": "Chat"})

    # Whether or not the turn is still in flight, this must not raise or claim success.
    assert made.steer("phone", "actually never mind") is False
    task = made._tasks.get("phone")
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
