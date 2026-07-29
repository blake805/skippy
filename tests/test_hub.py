"""ConnectionManager: request/reply routing, timeouts, and future bookkeeping.

A leaked or mis-routed future here strands a whole task, and the failure is
invisible until something hangs for the full timeout. These tests cover the hub
as it exists today, including one limitation it still has.
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


async def wait_for_send(socket, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not socket.sent:
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("nothing was sent on the socket")
        await asyncio.sleep(0.01)


# --- connection bookkeeping ---

async def test_connect_accepts_and_registers(hub):
    socket = FakeSocket()
    await hub.connect(socket, "cursor")
    assert socket.accepted
    assert hub.active_connections["cursor"] is socket


async def test_disconnect_is_idempotent(hub):
    await hub.connect(FakeSocket(), "cursor")
    hub.disconnect("cursor")
    hub.disconnect("cursor")  # must not raise
    assert "cursor" not in hub.active_connections


# --- execute_tool_on_client (RPC to an editor or client) ---

async def test_rpc_round_trip_returns_the_reply_and_releases_the_future(hub):
    socket = FakeSocket()
    await hub.connect(socket, "cursor")

    async def answer():
        await wait_for_send(socket)
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


async def test_a_dead_socket_is_reported_and_does_not_leak_a_future(hub):
    """A send that raises must not strand the future or escape to the caller.

    Without this, one dead client turns every later `pending_responses == {}`
    assumption false and the exception surfaces somewhere unrelated.
    """
    await hub.connect(BrokenSocket(), "cursor")
    result = await hub.execute_tool_on_client("cursor", {"action": "anything"}, timeout=1.0)
    assert "error" in result
    assert hub.pending_responses == {}


async def test_two_rpcs_on_one_client_each_get_their_own_answer(hub):
    """Routing is keyed by task_id, so out-of-order answers still land correctly."""
    socket = FakeSocket()
    await hub.connect(socket, "cursor")

    first = asyncio.create_task(hub.execute_tool_on_client("cursor", {"action": "one"}, timeout=5.0))
    second = asyncio.create_task(hub.execute_tool_on_client("cursor", {"action": "two"}, timeout=5.0))

    while len(socket.sent) < 2:
        await asyncio.sleep(0.01)
    ids = {payload["action"]: payload["task_id"] for payload in socket.sent}

    # Answer in reverse order on purpose.
    hub.resolve_response(ids["two"], {"which": "two"})
    hub.resolve_response(ids["one"], {"which": "one"})

    assert await first == {"which": "one"}
    assert await second == {"which": "two"}
    assert hub.pending_responses == {}


def test_resolving_an_unknown_task_id_is_a_no_op(hub):
    hub.resolve_response("not-a-real-task", {"anything": True})  # must not raise
    assert hub.pending_responses == {}


async def test_a_late_reply_after_timeout_is_discarded(hub):
    socket = FakeSocket()
    await hub.connect(socket, "cursor")
    result = await hub.execute_tool_on_client("cursor", {"action": "slow"}, timeout=0.05)
    assert "Timeout" in result["error"]

    # The client eventually answers. It must not blow up or resurrect the future.
    hub.resolve_response(socket.sent[-1]["task_id"], {"too": "late"})
    assert hub.pending_responses == {}


# --- human approvals ---

async def test_approval_round_trip(hub):
    socket = FakeSocket()

    async def approve():
        await wait_for_send(socket)
        hub.resolve_auth(socket, {"status": "APPROVE"})

    approver = asyncio.create_task(approve())
    reply = await hub.request_authorization(socket, {"type": "terminal_auth", "command": "ls"}, timeout=5.0)
    await approver

    assert reply["status"] == "APPROVE"
    assert socket.sent[-1]["type"] == "terminal_auth"
    assert hub.pending_auth == {}


async def test_a_denial_passes_through_unchanged(hub):
    socket = FakeSocket()

    async def deny():
        await wait_for_send(socket)
        hub.resolve_auth(socket, {"status": "DENY"})

    denier = asyncio.create_task(deny())
    reply = await hub.request_authorization(socket, {"type": "deployment_auth"}, timeout=5.0)
    await denier

    assert reply["status"] == "DENY"
    assert hub.pending_auth == {}


async def test_approval_timeout_fails_closed(hub):
    reply = await hub.request_authorization(FakeSocket(), {"type": "terminal_auth"}, timeout=0.05)
    assert reply["status"] == "DENY"
    assert hub.pending_auth == {}


async def test_a_dead_socket_fails_closed_rather_than_raising(hub):
    reply = await hub.request_authorization(BrokenSocket(), {"type": "terminal_auth"}, timeout=1.0)
    assert reply["status"] == "DENY"
    assert hub.pending_auth == {}


def test_resolve_auth_reports_whether_it_consumed_the_message(hub):
    # The endpoint loop uses this return value to decide between routing the
    # frame to a waiting gate and treating it as a new request.
    assert hub.resolve_auth(FakeSocket(), {"status": "APPROVE"}) is False


async def test_only_one_approval_per_socket_is_supported_today(hub):
    """Documents a real limitation, so fixing it is a deliberate change.

    `pending_auth` is keyed by `id(websocket)`, so a second concurrent approval
    on the same socket overwrites the first and the first waiter can only time
    out. ADR 0005 replaces this with `task_id` keying, and that arrives with the
    agent runtime — at which point this test should be replaced by one asserting
    that both approvals get their own answer.
    """
    socket = FakeSocket()

    first = asyncio.create_task(hub.request_authorization(socket, {"type": "a"}, timeout=0.3))
    await wait_for_send(socket)
    second = asyncio.create_task(hub.request_authorization(socket, {"type": "b"}, timeout=5.0))
    await asyncio.sleep(0.05)

    hub.resolve_auth(socket, {"status": "APPROVE"})

    # The reply goes to whichever future currently owns the socket key — the second.
    assert (await second)["status"] == "APPROVE"
    assert (await first)["status"] == "DENY"
