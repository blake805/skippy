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


async def test_request_on_socket_carries_a_denial_through_unchanged(hub):
    socket = FakeSocket()

    async def deny():
        while not socket.sent:
            await asyncio.sleep(0.01)
        hub.resolve_response(socket.sent[-1]["task_id"], {"status": "DENY"})

    denier = asyncio.create_task(deny())
    reply = await hub.request_on_socket(
        socket, {"type": "deployment_auth", "target_file": "x.py"}, timeout=5.0
    )
    await denier

    assert reply["status"] == "DENY"
    assert hub.pending_responses == {}


async def test_request_on_socket_times_out_without_leaking(hub):
    reply = await hub.request_on_socket(FakeSocket(), {"type": "terminal_auth"}, timeout=0.05)
    assert reply["status"] == "TIMEOUT"
    assert hub.pending_responses == {}


async def test_two_approvals_on_one_socket_each_get_their_own_answer(hub):
    """The defect this guards: two coroutines awaiting approval on a shared socket.

    Answering out of order proves the routing is keyed by `task_id` and not by
    whoever happens to be parked on the socket.
    """
    socket = FakeSocket()

    async def answer_the_second_one_first():
        while len(socket.sent) < 2:
            await asyncio.sleep(0.01)
        first, second = socket.sent[0], socket.sent[1]
        hub.resolve_response(second["task_id"], {"status": "APPROVE"})
        await asyncio.sleep(0.01)
        hub.resolve_response(first["task_id"], {"status": "DENY"})

    responder = asyncio.create_task(answer_the_second_one_first())
    denied, approved = await asyncio.gather(
        hub.request_on_socket(socket, {"type": "terminal_auth", "command": "rm -rf /"}, timeout=5.0),
        hub.request_on_socket(socket, {"type": "terminal_auth", "command": "ls"}, timeout=5.0),
    )
    await responder

    assert denied["status"] == "DENY", "the dangerous command must keep its own answer"
    assert approved["status"] == "APPROVE"
    assert socket.sent[0]["task_id"] != socket.sent[1]["task_id"]
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


# ---------------------------------------------------------------------------
# Legacy approval bridge bookkeeping
#
# A SwiftUI client that predates ADR 0005 answers an auth request without the
# task_id. `_serve_socket` can only bridge that reply if the hub can say which
# pending futures belong to a socket and which of those are human approvals
# rather than RPCs. That context lives in `PendingRequest`.
# ---------------------------------------------------------------------------


async def test_request_on_socket_registers_an_approval_on_that_socket(hub):
    socket = FakeSocket()

    waiter = asyncio.create_task(
        hub.request_on_socket(socket, {"type": "terminal_auth", "command": "ls"}, timeout=5.0)
    )
    while not socket.sent:
        await asyncio.sleep(0.01)

    assert hub.pending_approvals_on(socket) == [socket.sent[0]["task_id"]]
    assert hub.pending_approvals_on(FakeSocket()) == [], "another socket sees nothing"

    hub.resolve_response(socket.sent[0]["task_id"], {"status": "DENY"})
    await waiter
    assert hub.pending_approvals_on(socket) == []
    assert hub.pending_responses == {}


async def test_an_rpc_future_is_never_an_approval_candidate(hub):
    """An agent RPC and a shop approval share the socket; only the approval may
    ever be matched to a legacy reply, so only it shows up as a candidate."""
    socket = FakeSocket()
    await hub.connect(socket, "swiftui")

    rpc = asyncio.create_task(
        hub.execute_tool_on_client("swiftui", {"action": "get_active_file"}, timeout=5.0)
    )
    approval = asyncio.create_task(
        hub.request_on_socket(socket, {"type": "terminal_auth", "command": "ls"}, timeout=5.0)
    )
    while len(socket.sent) < 2:
        await asyncio.sleep(0.01)

    approval_id = next(p["task_id"] for p in socket.sent if p.get("type") == "terminal_auth")
    rpc_id = next(p["task_id"] for p in socket.sent if p.get("action") == "get_active_file")

    assert hub.pending_approvals_on(socket) == [approval_id]

    # Resolving the sole candidate — what the bridge does — must not touch the RPC.
    hub.resolve_response(approval_id, {"status": "APPROVE"})
    assert (await approval)["status"] == "APPROVE"
    assert not rpc.done(), "the agent's future must never be resolved by a legacy shop reply"

    # And the Cursor-style reply, which always carries task_id, lands as before.
    hub.resolve_response(rpc_id, {"content": "main.py"})
    assert (await rpc) == {"content": "main.py"}
    assert hub.pending_responses == {}


async def test_two_pending_approvals_are_both_reported(hub):
    """Two candidates means the bridge must refuse to guess; the hub's job is
    just to report both so `_serve_socket` can see the ambiguity."""
    socket = FakeSocket()

    first = asyncio.create_task(
        hub.request_on_socket(socket, {"type": "terminal_auth", "command": "a"}, timeout=5.0)
    )
    second = asyncio.create_task(
        hub.request_on_socket(socket, {"type": "deployment_auth", "target_file": "x.py"}, timeout=5.0)
    )
    while len(socket.sent) < 2:
        await asyncio.sleep(0.01)

    assert sorted(hub.pending_approvals_on(socket)) == sorted(p["task_id"] for p in socket.sent)

    for payload in socket.sent:
        hub.resolve_response(payload["task_id"], {"status": "DENY"})
    await asyncio.gather(first, second)
    assert hub.pending_responses == {}


async def test_disconnect_removes_the_client(hub):
    await hub.connect(FakeSocket(), "swiftui")
    assert "swiftui" in hub.active_connections
    hub.disconnect("swiftui")
    assert "swiftui" not in hub.active_connections
    hub.disconnect("swiftui")
