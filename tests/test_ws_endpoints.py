"""End-to-end coverage of the websocket lanes.

This is the Phase 2 exit criterion: a task arrives over `/ws/agent`, the agent
edits several files in a real repo, runs the tests, and reports success -- with no
Cursor client involved. The same tests pin the shop lane's behaviour so the
SwiftUI clients cannot regress.
"""

import os

import pytest
from fastapi.testclient import TestClient

from tests.fake_llm import raw_tool_call, tool_call

DIVIDE_BODY = (
    "def subtract(left: float, right: float) -> float:\n    return left - right\n\n\n"
    "def divide(left: float, right: float) -> float:\n"
    '    if right == 0:\n        raise ZeroDivisionError("right must be non-zero")\n'
    "    return left / right"
)


@pytest.fixture
def factory_client(routed_llm):
    import skippy_factory

    with TestClient(skippy_factory.app) as client:
        yield client


def drain(socket, stop_type="done", limit=400):
    events = []
    for _ in range(limit):
        event = socket.receive_json()
        events.append(event)
        if event.get("type") == stop_type:
            break
    return events


def of_type(events, event_type):
    return [event for event in events if event.get("type") == event_type]


def test_agent_endpoint_lands_a_multi_file_change(factory_client, routed_llm, sample_repo):
    routed_llm.load(
        [
            tool_call("read_file", thought="Reading the module.", path="calc/ops.py"),
            raw_tool_call(
                {
                    "tool": "apply_patch",
                    "args": {
                        "edits": [
                            {
                                "path": "calc/ops.py",
                                "action": "edit",
                                "search": "def subtract(left: float, right: float) -> float:\n    return left - right",
                                "replace": DIVIDE_BODY,
                            },
                            {
                                "path": "calc/__init__.py",
                                "action": "edit",
                                "search": 'from .ops import add, subtract\n\n__all__ = ["add", "subtract"]',
                                "replace": 'from .ops import add, divide, subtract\n\n__all__ = ["add", "divide", "subtract"]',
                            },
                            {
                                "path": "tests/test_divide.py",
                                "action": "create",
                                "content": (
                                    "import pytest\n\nfrom calc import divide\n\n\n"
                                    "def test_divide():\n    assert divide(6, 3) == 2\n\n\n"
                                    "def test_divide_by_zero():\n"
                                    "    with pytest.raises(ZeroDivisionError):\n        divide(1, 0)\n"
                                ),
                            },
                        ]
                    },
                }
            ),
            tool_call("run_tests", command="python3 -m pytest -q"),
            tool_call(
                "finish",
                summary="Added divide(), exported it, and covered both branches.",
                files_changed=["calc/ops.py", "calc/__init__.py", "tests/test_divide.py"],
            ),
        ]
    )

    with factory_client.websocket_connect("/ws/agent?client_id=pytest") as socket:
        socket.send_json(
            {
                "type": "agent_task",
                "project_id": "sample",
                "text": "Add a divide() helper, export it, and cover it with a test.",
                "workspace_roots": [sample_repo],
                "max_steps": 12,
            }
        )
        events = drain(socket)

    done = of_type(events, "agent_done")
    assert len(done) == 1
    assert done[0]["status"] == "success", done[0]["summary"]
    assert set(done[0]["files_changed"]) == {
        "calc/ops.py",
        "calc/__init__.py",
        "tests/test_divide.py",
    }

    patches = of_type(events, "agent_patch")
    assert len(patches) == 1 and patches[0]["diff"]

    tests_run = [
        event for event in of_type(events, "agent_tool_result") if event["tool"] == "run_tests"
    ]
    assert tests_run and tests_run[0]["ok"]

    with open(os.path.join(sample_repo, "calc", "ops.py"), encoding="utf-8") as handle:
        assert "def divide" in handle.read()
    assert os.path.exists(os.path.join(sample_repo, "tests", "test_divide.py"))

    # Legacy event types are still emitted so existing SwiftUI clients keep working.
    assert of_type(events, "log")
    assert of_type(events, "chat")
    assert of_type(events, "done")


def test_agent_events_carry_session_and_step(factory_client, routed_llm, sample_repo):
    routed_llm.load(
        [
            tool_call("list_dir", path="."),
            tool_call("finish", summary="Looked around.", files_changed=[]),
        ]
    )

    with factory_client.websocket_connect("/ws/agent") as socket:
        socket.send_json(
            {"type": "agent_task", "text": "look around", "workspace_roots": [sample_repo]}
        )
        events = drain(socket)

    agent_events = [event for event in events if event.get("type", "").startswith("agent_")]
    assert agent_events
    session_ids = {event["session_id"] for event in agent_events}
    assert len(session_ids) == 1
    assert all("step" in event for event in agent_events)

    call = of_type(events, "agent_tool_call")[0]
    assert call["tool"] == "list_dir"
    assert call["call_id"]


def test_explicit_session_id_is_honoured(factory_client, routed_llm, sample_repo):
    routed_llm.load([tool_call("finish", summary="ok", files_changed=[])])

    with factory_client.websocket_connect("/ws/agent") as socket:
        socket.send_json(
            {
                "type": "agent_task",
                "session_id": "s-fixed-id",
                "text": "noop",
                "workspace_roots": [sample_repo],
            }
        )
        events = drain(socket)

    assert of_type(events, "agent_done")[0]["session_id"] == "s-fixed-id"


def test_factory_endpoint_routes_agent_mode_to_the_agent(factory_client, routed_llm, sample_repo):
    routed_llm.load([tool_call("finish", summary="Nothing needed.", files_changed=[])])

    with factory_client.websocket_connect("/ws/factory?client_id=swiftui") as socket:
        socket.send_json(
            {
                "mode": "Agent",
                "text": "check the repo",
                "workspace_roots": [sample_repo],
            }
        )
        events = drain(socket)

    assert of_type(events, "agent_done")[0]["status"] == "success"


def test_factory_endpoint_still_defaults_to_the_shop_pipeline(factory_client, routed_llm, monkeypatch):
    """A payload without a mode must reach SkippyPipeline, exactly as before."""
    import skippy_factory

    seen = {}

    class SpyPipeline:
        def __init__(self, websocket, payload, manager):
            seen["payload"] = payload
            self.ws = websocket

        async def run(self):
            await self.ws.send_json({"type": "done"})

    monkeypatch.setattr(skippy_factory, "SkippyPipeline", SpyPipeline)

    with factory_client.websocket_connect("/ws/factory") as socket:
        socket.send_json({"text": "what feed rate for 6061?", "history": []})
        drain(socket)

    assert seen["payload"]["text"] == "what feed rate for 6061?"
    assert seen["payload"].get("mode") is None


def test_plain_text_on_the_factory_socket_is_treated_as_shop(factory_client, routed_llm, monkeypatch):
    import skippy_factory

    seen = {}

    class SpyPipeline:
        def __init__(self, websocket, payload, manager):
            seen["payload"] = payload
            self.ws = websocket

        async def run(self):
            await self.ws.send_json({"type": "done"})

    monkeypatch.setattr(skippy_factory, "SkippyPipeline", SpyPipeline)

    with factory_client.websocket_connect("/ws/factory") as socket:
        socket.send_text("hello skippy")
        drain(socket)

    assert seen["payload"]["mode"] == "Shop"


def test_rpc_client_chatter_never_starts_a_pipeline(factory_client, routed_llm, monkeypatch):
    import skippy_factory

    started = []

    class SpyPipeline:
        def __init__(self, websocket, payload, manager):
            started.append(payload)
            self.ws = websocket

        async def run(self):
            await self.ws.send_json({"type": "done"})

    monkeypatch.setattr(skippy_factory, "SkippyPipeline", SpyPipeline)

    with factory_client.websocket_connect("/ws/factory?client_id=cursor") as socket:
        socket.send_json({"type": "hello", "roots": ["/tmp"]})
        assert socket.receive_json()["type"] == "hello_ack"
        socket.send_json({"type": "diagnostics_changed", "path": "a.py"})
        socket.send_json({"type": "ping"})
        assert socket.receive_json()["type"] == "hello_ack"

    assert started == []


def test_cancel_message_is_acknowledged(factory_client, routed_llm):
    with factory_client.websocket_connect("/ws/agent") as socket:
        socket.send_json({"type": "agent_cancel", "session_id": "s-not-running"})
        reply = socket.receive_json()

    assert reply == {"type": "agent_cancelled", "session_id": "s-not-running", "found": False}


def test_task_id_replies_never_start_a_pipeline(factory_client, routed_llm, monkeypatch):
    """Auth decisions and RPC results are replies, not new tasks."""
    import skippy_factory

    started = []

    class SpyPipeline:
        def __init__(self, websocket, payload, manager):
            started.append(payload)
            self.ws = websocket

        async def run(self):
            await self.ws.send_json({"type": "done"})

    monkeypatch.setattr(skippy_factory, "SkippyPipeline", SpyPipeline)

    with factory_client.websocket_connect("/ws/factory") as socket:
        socket.send_json({"task_id": "t-unknown", "status": "APPROVE", "text": "approve"})
        socket.send_json({"type": "ping"})
        assert socket.receive_json()["type"] == "hello_ack"

    assert started == []


def test_missing_roots_reports_a_useful_error_over_the_socket(factory_client, routed_llm):
    with factory_client.websocket_connect("/ws/agent") as socket:
        socket.send_json({"type": "agent_task", "text": "do work", "project_id": "unknown-project"})
        events = drain(socket)

    done = of_type(events, "agent_done")[0]
    assert done["status"] == "failed"
    assert "workspace_roots" in done["summary"]
