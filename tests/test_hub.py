"""ConnectionManager: request/reply routing, timeouts, and future bookkeeping.

The agent runs several concurrent tasks per socket and asks for human approval
mid-run, so a leaked or mis-routed future here strands a whole session.
"""

import asyncio

import pytest


class FakeSocket:
    def __init__(self):
        self.sent = []
        self.accepted = False

    async def accept(self):
        self.accepted = True

    async def send_json(self, payload):
        self.sent.append(payload)


class BrokenSocket(FakeSocket):
    async def send_json(self, payload):
        raise RuntimeError("socket closed")


@pytest.fixture
def hub():
    from skippy_factory import ConnectionManager

    return ConnectionManager()


async def test_execute_tool_on_client_round_trip(hub):
    socket = FakeSocket()
    await hub.connect(socket, "cursor")

    async def answer():
        while not socket.sent:
            await asyncio.sleep(0.01)
        hub.resolve_response(socket.sent[-1]["task_id"], {"roots": ["/tmp/x"]})

    responder = asyncio.create_task(answer())
    result = await hub.execute_tool_on_client("cursor", {"action": "get_workspace_roots"}, timeout=5.0)
    await responder

    assert result == {"roots": ["/tmp/x"]}
    assert socket.sent[-1]["action"] == "get_workspace_roots"
    assert hub.pending_responses == {}


async def test_offline_client_is_reported_not_awaited(hub):
    result = await hub.execute_tool_on_client("cursor", {"action": "get_diagnostics"}, timeout=0.1)
    assert "offline" in result["error"]
    assert hub.pending_responses == {}


async def test_timeout_releases_the_future(hub):
    await hub.connect(FakeSocket(), "cursor")
    result = await hub.execute_tool_on_client("cursor", {"action": "slow"}, timeout=0.05)
    assert "Timeout" in result["error"]
    assert hub.pending_responses == {}


async def test_transport_failure_is_reported_and_cleaned_up(hub):
    await hub.connect(BrokenSocket(), "cursor")
    result = await hub.execute_tool_on_client("cursor", {"action": "anything"}, timeout=1.0)
    assert "Transport failure" in result["error"]
    assert hub.pending_responses == {}


async def test_request_on_socket_awaits_an_approval(hub):
    socket = FakeSocket()

    async def approve():
        while not socket.sent:
            await asyncio.sleep(0.01)
        hub.resolve_response(socket.sent[-1]["task_id"], {"status": "APPROVE"})

    approver = asyncio.create_task(approve())
    reply = await hub.request_on_socket(
        socket, {"type": "terminal_auth", "command": "ls"}, timeout=5.0
    )
    await approver

    assert reply["status"] == "APPROVE"
    assert socket.sent[-1]["type"] == "terminal_auth"
    assert hub.pending_responses == {}


async def test_request_on_socket_times_out_without_leaking(hub):
    reply = await hub.request_on_socket(FakeSocket(), {"type": "terminal_auth"}, timeout=0.05)
    assert reply["status"] == "TIMEOUT"
    assert hub.pending_responses == {}


async def test_unknown_task_id_is_ignored(hub):
    hub.resolve_response("nope", {"status": "APPROVE"})
    assert hub.pending_responses == {}


async def test_concurrent_requests_do_not_cross_wires(hub):
    socket = FakeSocket()
    await hub.connect(socket, "cursor")

    async def respond_in_reverse():
        while len(socket.sent) < 3:
            await asyncio.sleep(0.01)
        for payload in reversed(socket.sent):
            hub.resolve_response(payload["task_id"], {"echo": payload["n"]})

    responder = asyncio.create_task(respond_in_reverse())
    results = await asyncio.gather(
        *(hub.execute_tool_on_client("cursor", {"n": index}, timeout=5.0) for index in range(3))
    )
    await responder

    assert results == [{"echo": 0}, {"echo": 1}, {"echo": 2}]
    assert hub.pending_responses == {}


async def test_disconnect_removes_the_client(hub):
    await hub.connect(FakeSocket(), "swiftui")
    assert "swiftui" in hub.active_connections
    hub.disconnect("swiftui")
    assert "swiftui" not in hub.active_connections
    hub.disconnect("swiftui")
