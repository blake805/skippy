"""The task runner: what happens between a person sending a request and getting work.

Before this existed the agent loop was complete, tested, and unreachable. These tests
are about the lifecycle around a run rather than the run itself — that a second request
does not trample the first, that cancel arrives while work is in flight, and that a
dropped connection loses the events rather than the work.
"""

import asyncio
import os

import pytest

import skippy_tasks
from skippy_tasks import TaskRunner, agent_mode_for
from tests import fake_llm as fl


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "calc").mkdir(parents=True)
    (root / "calc" / "ops.py").write_text("def add(a, b):\n    return a + b\n")
    (root / "README.md").write_text("# calc\n")
    return root


def finish(summary="done", call_id="call_1"):
    return fl.tool_call("finish", call_id=call_id, summary=summary)


class FakeSocket:
    def __init__(self, fail: bool = False):
        self.sent = []
        self.fail = fail

    async def send_json(self, payload):
        if self.fail:
            raise RuntimeError("socket is gone")
        self.sent.append(payload)

    def types(self):
        return [message.get("type") for message in self.sent]

    def chats(self):
        return [m.get("content", "") for m in self.sent if m.get("type") == "chat"]


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
def runner(hub, repo):
    return TaskRunner(hub, roots_provider=lambda: [str(repo)])


async def settle(runner, client_id="phone", timeout=5.0):
    """Wait for the client's run to finish."""
    task = runner._tasks.get(client_id)
    if task is not None:
        await asyncio.wait_for(asyncio.shield(task), timeout=timeout)


# --- mode mapping ---

@pytest.mark.parametrize("wire,expected", [
    ("Agent", "coding"),
    ("agent", "coding"),
    ("", "coding"),
    (None, "coding"),
    ("Chat", "chat"),
    ("chat", "chat"),
    ("RE", "re"),
    ("re", "re"),
    ("Reverse Engineering", "re"),
    ("reverse-engineering", "re"),
])
def test_the_wire_mode_maps_to_an_agent_mode(wire, expected):
    """The protocol's `mode` predates the agent's own and means something else, so it
    is translated rather than passed through."""
    assert agent_mode_for(wire) == expected


# --- starting work ---

@pytest.mark.asyncio
async def test_a_request_runs_the_agent_and_reports_its_summary(runner, socket, routed_llm):
    routed_llm.load([finish("Added the feature.")])
    await runner.start("phone", {"text": "add a feature", "mode": "Agent"})
    await settle(runner)

    assert "agent_start" in socket.types()
    assert socket.types()[-1] == "done"
    assert any("Added the feature" in text for text in socket.chats())


@pytest.mark.asyncio
async def test_history_from_the_client_reaches_the_model(runner, socket, routed_llm):
    """A follow-up sent from the app carries its prior turns so the run continues
    the thread. The runner only checks it is a list; the loop validates contents."""
    routed_llm.load([finish("Continued.")])
    await runner.start("phone", {
        "text": "now the other file",
        "mode": "Agent",
        "history": [
            {"role": "user", "content": "rename add to plus"},
            {"role": "assistant", "content": "renamed it"},
        ],
    })
    await settle(runner)

    seen = [m.get("content") for m in routed_llm.last_messages()]
    assert "rename add to plus" in seen
    assert "renamed it" in seen


@pytest.mark.asyncio
async def test_a_non_list_history_is_ignored_rather_than_crashing(runner, socket, routed_llm):
    routed_llm.load([finish("Done.")])
    await runner.start("phone", {"text": "a task", "mode": "Agent", "history": "oops"})
    await settle(runner)
    assert socket.types()[-1] == "done"
    assert any("Done" in text for text in socket.chats())


@pytest.mark.asyncio
async def test_an_empty_request_is_refused_without_starting_a_run(runner, socket):
    await runner.start("phone", {"text": "   ", "mode": "Agent"})
    assert not runner.is_running("phone")
    assert "agent_start" not in socket.types()
    assert socket.types()[-1] == "done"


@pytest.mark.asyncio
async def test_a_second_request_does_not_trample_the_first(runner, socket, routed_llm):
    """Two agents editing the same tree at once is how you get a corrupted repo and
    two half-applied changes with no account of either."""
    routed_llm.load([
        fl.tool_call("read_file", call_id="c1", path="calc/ops.py"),
        finish("Done.", call_id="c2"),
    ])
    await runner.start("phone", {"text": "first task", "mode": "Agent"})
    await runner.start("phone", {"text": "second task", "mode": "Agent"})

    assert any("still working" in text for text in socket.chats())
    await settle(runner)


@pytest.mark.asyncio
async def test_a_run_that_finished_does_not_block_the_next_one(runner, socket, routed_llm):
    routed_llm.load([finish("First done."), finish("Second done.")])
    await runner.start("phone", {"text": "first", "mode": "Agent"})
    await settle(runner)
    assert not runner.is_running("phone")

    await runner.start("phone", {"text": "second", "mode": "Agent"})
    await settle(runner)
    assert any("Second done" in text for text in socket.chats())


@pytest.mark.asyncio
async def test_two_clients_can_work_at_the_same_time(hub, repo, routed_llm):
    """The limit is one run per client, not one for the whole server."""
    runner = TaskRunner(hub, roots_provider=lambda: [str(repo)])
    phone, laptop = FakeSocket(), FakeSocket()
    hub.active_connections["phone"] = phone
    hub.active_connections["laptop"] = laptop

    routed_llm.load([finish("Done one."), finish("Done two.")])
    await runner.start("phone", {"text": "task one", "mode": "Agent"})
    await runner.start("laptop", {"text": "task two", "mode": "Agent"})
    await settle(runner, "phone")
    await settle(runner, "laptop")

    assert phone.types()[-1] == "done"
    assert laptop.types()[-1] == "done"


# --- the chat lane ---

@pytest.mark.asyncio
async def test_chat_answers_without_running_the_agent(runner, socket, routed_llm):
    """A greeting is a conversation, not a task. No agent_start, no tool nudges,
    no stopped_without_finish — one reply and a clean done."""
    routed_llm.load([fl.text("Hey. What are we thinking about today?")])
    await runner.start("phone", {"text": "hey skippy", "mode": "Chat"})
    await settle(runner)

    assert "agent_start" not in socket.types()
    assert socket.types()[-1] == "done"
    assert any("What are we thinking about" in text for text in socket.chats())


@pytest.mark.asyncio
async def test_chat_carries_the_conversation_history(runner, socket, routed_llm):
    routed_llm.load([fl.text("Titanium, obviously.")])
    await runner.start("phone", {
        "text": "which one would you pick?",
        "mode": "Chat",
        "history": [
            {"role": "user", "content": "aluminum or titanium for the bracket"},
            {"role": "assistant", "content": "depends on the load"},
        ],
    })
    await settle(runner)

    seen = [m.get("content") for m in routed_llm.last_messages()]
    assert "aluminum or titanium for the bracket" in seen
    assert "depends on the load" in seen


@pytest.mark.asyncio
async def test_chat_works_with_no_workspace_roots(hub, socket, routed_llm):
    """Talking to Skippy must not require a configured workspace: the conversation
    is how a misconfiguration would be discovered."""
    runner = TaskRunner(hub, roots_provider=lambda: [])
    routed_llm.load([fl.text("Hello there.")])
    await runner.start("phone", {"text": "hi", "mode": "Chat"})
    await settle(runner)

    assert any("Hello there" in text for text in socket.chats())
    assert socket.types()[-1] == "done"


@pytest.mark.asyncio
async def test_chat_failure_is_reported_as_a_reply(runner, socket, routed_llm):
    routed_llm.load([fl.http_error(500), fl.http_error(500), fl.http_error(500)])
    await runner.start("phone", {"text": "hi", "mode": "Chat"})
    await settle(runner, timeout=30.0)

    assert any("could not answer" in text for text in socket.chats())
    assert socket.types()[-1] == "done"


@pytest.mark.asyncio
async def test_chat_blocks_a_second_request_like_any_run(runner, socket, routed_llm):
    routed_llm.load([fl.text("First answer."), fl.text("unused")])
    await runner.start("phone", {"text": "hi", "mode": "Chat"})
    if runner.is_running("phone"):
        await runner.start("phone", {"text": "again", "mode": "Chat"})
        assert any("still working" in text for text in socket.chats())
    await settle(runner)


# --- chat transcripts and project selection on the wire ---

@pytest.mark.asyncio
async def test_a_chat_with_a_chat_id_is_persisted_and_resumable(runner, socket, routed_llm, repo):
    routed_llm.load([fl.text("Aluminum, for the weight.")])
    await runner.start("phone", {
        "text": "which metal for the bracket?", "mode": "Chat", "chat_id": "abc-123",
    })
    await settle(runner)

    snapshot = runner.chat_open_snapshot("", "abc-123")
    turns = snapshot["chat"]["turns"]
    assert [t["role"] for t in turns] == ["user", "assistant"]
    assert "which metal" in turns[0]["content"]
    assert "Aluminum" in turns[1]["content"]


@pytest.mark.asyncio
async def test_a_chat_without_a_chat_id_is_not_persisted(runner, socket, routed_llm):
    """An older client sends no chat_id and gets exactly the old behavior."""
    routed_llm.load([fl.text("Hello.")])
    await runner.start("phone", {"text": "hi", "mode": "Chat"})
    await settle(runner)
    assert runner.memory_snapshot()["chats"] == []


@pytest.mark.asyncio
async def test_an_unsafe_chat_id_costs_persistence_not_the_reply(runner, socket, routed_llm):
    routed_llm.load([fl.text("Still answering.")])
    await runner.start("phone", {"text": "hi", "mode": "Chat", "chat_id": "../escape"})
    await settle(runner)
    assert any("Still answering" in text for text in socket.chats())
    assert runner.memory_snapshot()["chats"] == []


@pytest.mark.asyncio
async def test_an_agent_run_lands_in_the_transcript_too(runner, socket, routed_llm):
    """The conversation around a run resumes like any chat; the step-by-step
    record stays in the session summary."""
    routed_llm.load([finish("Added the feature.")])
    await runner.start("phone", {"text": "add a feature", "mode": "Agent", "chat_id": "run-chat"})
    await settle(runner)

    turns = runner.chat_open_snapshot("", "run-chat")["chat"]["turns"]
    assert "add a feature" in turns[0]["content"]
    assert "Added the feature" in turns[1]["content"]


@pytest.mark.asyncio
async def test_a_picked_project_scopes_the_chat_and_its_transcript(runner, socket, routed_llm):
    """The picker's contract: memory in, transcript out, both under the chosen id."""
    import skippy_memory

    skippy_memory.open_project(project_id="other-project").learn_convention(
        "test command", "make check-other"
    )
    routed_llm.load([fl.text("Noted.")])
    await runner.start("phone", {
        "text": "hi", "mode": "Chat", "project": "other-project", "chat_id": "c1",
    })
    await settle(runner)

    # The picked project's memory reached the prompt...
    seen = " ".join(m.get("content") or "" for m in routed_llm.last_messages())
    assert "make check-other" in seen
    # ...and the transcript landed under it, not under the roots' default.
    assert runner.chat_open_snapshot("other-project", "c1")["chat"]["turns"]
    assert runner.memory_snapshot()["chats"] == []


@pytest.mark.asyncio
async def test_picking_a_project_does_not_rescope_its_roots(runner, routed_llm, repo):
    """Opening a picked project must not append the current workspace roots into
    its meta — that would quietly merge two projects' identities."""
    import skippy_memory

    skippy_memory.open_project(project_id="other-project")
    routed_llm.load([fl.text("ok")])
    await runner.start("phone", {"text": "hi", "mode": "Chat", "project": "other-project"})
    await settle(runner)

    reopened = skippy_memory.open_project(project_id="other-project")
    assert str(repo) not in reopened.meta["workspace_roots"]


def test_the_memory_snapshot_carries_the_chat_list(runner):
    memory = runner._open_memory()
    memory.append_chat("c1", [{"role": "user", "content": "a conversation"}])
    snapshot = runner.memory_snapshot()
    assert snapshot["chats"][0]["chat_id"] == "c1"
    assert snapshot["chats"][0]["title"] == "a conversation"


def test_opening_a_missing_chat_names_the_problem(runner):
    result = runner.chat_open_snapshot("", "no-such-chat")
    assert "no-such-chat" in result["error"]


def test_the_projects_snapshot_lists_the_store_and_the_default(runner, repo):
    import skippy_memory

    skippy_memory.open_project(workspace_roots=[str(repo)])
    skippy_memory.open_project(project_id="another")
    snapshot = runner.projects_snapshot()
    ids = {p["project_id"] for p in snapshot["projects"]}
    assert snapshot["default"] == skippy_memory.project_id_for([str(repo)])
    assert snapshot["default"] in ids and "another" in ids


# --- creating a workspace ---

@pytest.mark.asyncio
async def test_workspace_new_creates_a_repo_and_a_root_without_a_restart(runner):
    import skippy_paths

    result = await runner.workspace_new_action({"name": "gadget"})
    assert result["ok"] is True
    assert os.path.isdir(os.path.join(result["path"], ".git"))
    # The provider re-reads the roots file per call: no restart needed.
    assert result["path"] in skippy_paths.configured_workspace_roots()
    # And the project is already in the picker.
    ids = {p["project_id"] for p in runner.projects_snapshot()["projects"]}
    assert result["project_id"] in ids


@pytest.mark.asyncio
async def test_workspace_new_refuses_a_name_that_already_exists(runner):
    await runner.workspace_new_action({"name": "gadget"})
    result = await runner.workspace_new_action({"name": "gadget"})
    assert "already exists" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "  ", "a/b", "..", ".hidden", "/abs"])
async def test_workspace_new_refuses_an_unsafe_name(runner, bad):
    result = await runner.workspace_new_action({"name": bad})
    assert "error" in result


# --- no workspace ---

@pytest.mark.asyncio
async def test_no_workspace_roots_is_reported_with_the_fix(hub, socket):
    """A configuration mistake the user can fix, so it must be named. A silent empty
    run looks like the agent found nothing to do."""
    runner = TaskRunner(hub, roots_provider=lambda: [])
    await runner.start("phone", {"text": "do something", "mode": "Agent"})

    assert not runner.is_running("phone")
    assert any("SKIPPY_WORKSPACE_ROOTS" in text for text in socket.chats())
    assert socket.types()[-1] == "done"


# --- cancellation ---

@pytest.mark.asyncio
async def test_cancel_stops_a_run_in_flight(runner, socket, routed_llm):
    routed_llm.load([fl.tool_call("read_file", call_id=f"c{n}", path="calc/ops.py") for n in range(20)])
    await runner.start("phone", {"text": "a long task", "mode": "Agent"})

    # Let it get going, then stop it.
    for _ in range(50):
        await asyncio.sleep(0.02)
        if runner._loops.get("phone") and runner._loops["phone"].step > 0:
            break
    assert runner.cancel("phone") is True
    await settle(runner, timeout=10.0)

    assert any("ancel" in text for text in socket.chats())
    assert socket.types()[-1] == "done"


@pytest.mark.asyncio
async def test_cancelling_nothing_says_so(runner):
    assert runner.cancel("phone") is False


@pytest.mark.asyncio
async def test_cancel_uses_the_loops_own_flag_not_task_cancellation(runner, socket, routed_llm):
    """Killing the coroutine could abandon a tool call half-way and lose the account
    of it; stopping between steps still produces an outcome."""
    routed_llm.load([fl.tool_call("read_file", call_id=f"c{n}", path="calc/ops.py") for n in range(20)])
    await runner.start("phone", {"text": "a long task", "mode": "Agent"})
    task = runner._tasks["phone"]

    for _ in range(50):
        await asyncio.sleep(0.02)
        if runner._loops.get("phone") and runner._loops["phone"].step > 0:
            break
    runner.cancel("phone")
    await settle(runner, timeout=10.0)

    assert not task.cancelled()


# --- the connection dropping ---

@pytest.mark.asyncio
async def test_a_dropped_connection_does_not_kill_the_work(runner, hub, socket, routed_llm, repo):
    """Wanting Skippy from a phone is a reason this project exists, and mobile
    connections drop. Losing a long refactor to a change of cell tower would be worse
    than losing the progress messages."""
    routed_llm.load([
        fl.tool_call("apply_patch", call_id="c1", edits=[
            {"path": "calc/ops.py", "search": "a + b", "replace": "a + b  # done"}
        ]),
        finish("Finished while you were away.", call_id="c2"),
    ])
    await runner.start("phone", {"text": "a task", "mode": "Agent"})
    hub.active_connections.pop("phone")  # the client vanishes
    await settle(runner, timeout=15.0)

    assert "# done" in (repo / "calc" / "ops.py").read_text()


@pytest.mark.asyncio
async def test_a_reconnecting_client_picks_the_run_back_up(runner, hub, routed_llm):
    """Events are addressed to a client, not to the socket the run started on.

    The swap is triggered by the first delivered event rather than by a sleep, so it
    lands mid-run every time instead of racing a fast fake model to the finish.
    """
    replacement = FakeSocket()

    class Vanishing(FakeSocket):
        async def send_json(self, payload):
            await super().send_json(payload)
            hub.active_connections["phone"] = replacement

    hub.active_connections["phone"] = Vanishing()
    routed_llm.load([
        fl.tool_call("read_file", call_id="c1", path="calc/ops.py"),
        finish("All done.", call_id="c2"),
    ])
    await runner.start("phone", {"text": "a task", "mode": "Agent"})
    await settle(runner, timeout=15.0)

    assert any("All done" in text for text in replacement.chats())
    assert replacement.types()[-1] == "done"


@pytest.mark.asyncio
async def test_a_send_to_a_broken_socket_does_not_break_the_run(hub, repo, routed_llm):
    runner = TaskRunner(hub, roots_provider=lambda: [str(repo)])
    hub.active_connections["phone"] = FakeSocket(fail=True)

    routed_llm.load([finish("Finished anyway.")])
    await runner.start("phone", {"text": "a task", "mode": "Agent"})
    await settle(runner)
    assert not runner.is_running("phone")


@pytest.mark.asyncio
async def test_sending_to_an_absent_client_reports_failure_quietly(runner):
    assert await runner.send("nobody", {"type": "chat", "content": "x"}) is False


# --- crashes and shutdown ---

@pytest.mark.asyncio
async def test_a_crash_in_the_runtime_still_closes_the_exchange(runner, socket, monkeypatch):
    """Otherwise the person waiting sees nothing arrive and no explanation."""
    class Exploding:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            raise RuntimeError("something broke inside")

        def cancel(self):
            pass

    monkeypatch.setattr(skippy_tasks.skippy_agent, "AgentLoop", Exploding)
    await runner.start("phone", {"text": "a task", "mode": "Agent"})
    await settle(runner)

    assert any("something broke inside" in text for text in socket.chats())
    assert socket.types()[-1] == "done"


@pytest.mark.asyncio
async def test_a_crash_clears_the_slot_so_the_client_is_not_stuck(runner, socket, monkeypatch):
    class Exploding:
        def __init__(self, *args, **kwargs):
            pass

        async def run(self):
            raise RuntimeError("broke")

        def cancel(self):
            pass

    monkeypatch.setattr(skippy_tasks.skippy_agent, "AgentLoop", Exploding)
    await runner.start("phone", {"text": "a task", "mode": "Agent"})
    await settle(runner)
    assert not runner.is_running("phone")


@pytest.mark.asyncio
async def test_shutdown_stops_in_flight_runs(runner, socket, routed_llm):
    routed_llm.load([fl.tool_call("read_file", call_id=f"c{n}", path="calc/ops.py") for n in range(20)])
    await runner.start("phone", {"text": "a long task", "mode": "Agent"})
    for _ in range(50):
        await asyncio.sleep(0.02)
        if runner._loops.get("phone") and runner._loops["phone"].step > 0:
            break

    await runner.shutdown()
    assert not runner.is_running("phone")


@pytest.mark.asyncio
async def test_shutdown_with_nothing_running_is_harmless(runner):
    await runner.shutdown()
