"""A scripted device bridge, so the pins-and-buses tools are testable with no bench.

The real bridge is a Core2 on the bench (`firmware/core2-devio/`) or a Mac
sharing its ports. What needs coverage on this side is the contract between
them, pinned by ADR 0020: which client id a `host` resolves to, the exact
request each tool puts on the wire, and what the tool makes of the reply.

So this stands in for `ConnectionManager`, not for the hardware. It records
every request it is asked to send and answers from a per-action script, and an
unscripted action is an error rather than a silent empty reply — a tool calling
something nobody expected should fail a test, not pass one. It also plays the
approval channel, which is what lets a test assert the thing that matters most
about a write: the card goes to the client that started the run, never to the
node holding the wires.

Import-light on purpose: no websocket, no server, no event loop of its own, so
the loopback-only CI run has nothing to reach for.
"""

from typing import Any, Dict, List, Optional, Tuple

# The client that started the run, and therefore the one the approval card
# belongs to.
RUN_CLIENT_ID = "ui"


class _Socket:
    """Stands in for a websocket; identity is all the hub uses it for."""

    def __init__(self, client_id: str):
        self.client_id = client_id


class FakeBridge:
    """The hub, with a bridge (and the run's own client) connected."""

    def __init__(self, *client_ids: str, approve: bool = True):
        ids = list(client_ids) or ["devices"]
        ids.append(RUN_CLIENT_ID)
        self.active_connections: Dict[str, _Socket] = {cid: _Socket(cid) for cid in ids}
        self.approve = approve
        # Every request the hub was asked to send, in order: (client_id, payload).
        self.sent: List[Tuple[str, dict]] = []
        # Every device_auth card, as (client_id it was shown on, payload).
        self.cards: List[Tuple[str, dict]] = []
        self._results: Dict[str, Any] = {}

    # -- scripting ---------------------------------------------------------

    def answer(self, action: str, result: Any) -> "FakeBridge":
        """Reply to `action` with `{"ok": true, "result": result}`.

        A callable is passed the request, for an answer that depends on what was
        asked — a register read that echoes back the register, say.
        """
        self._results[action] = result
        return self

    def fail(self, action: str, error: str) -> "FakeBridge":
        """Reply to `action` the way a node refuses something: ok false."""
        self._results[action] = RuntimeError(error)
        return self

    # -- what was asked ----------------------------------------------------

    def request(self, action: str) -> dict:
        """The last request sent for an action, without the routing fields."""
        for _, payload in reversed(self.sent):
            if payload.get("action") == action:
                return {k: v for k, v in payload.items() if k not in ("action", "task_id")}
        raise AssertionError(
            f"no {action} request was sent; saw {[p.get('action') for _, p in self.sent]}"
        )

    def target(self, action: str) -> str:
        """Which client id an action was routed to."""
        for client_id, payload in reversed(self.sent):
            if payload.get("action") == action:
                return client_id
        raise AssertionError(f"no {action} request was sent")

    # -- the ConnectionManager surface DeviceService uses ------------------

    async def execute_tool_on_client(
        self, client_id: str, payload: dict, timeout: float = 10.0,
    ) -> dict:
        if client_id not in self.active_connections:
            return {"error": f"Client '{client_id}' is offline."}
        self.sent.append((client_id, dict(payload)))
        action = payload.get("action", "")
        if action not in self._results:
            return {"error": f"FakeBridge has no scripted answer for '{action}'."}
        result = self._results[action]
        if isinstance(result, Exception):
            return {"ok": False, "error": str(result)}
        if callable(result):
            result = result(payload)
        return {"task_id": payload.get("task_id"), "ok": True, "result": result}

    async def request_authorization(
        self, socket: Any, payload: dict, timeout: float = 300.0,
    ) -> dict:
        self.cards.append((getattr(socket, "client_id", "?"), dict(payload)))
        if self.approve:
            return {"status": "APPROVE"}
        return {"status": "DENY", "reason": "not today"}


def bridged_service(bridge: Optional[FakeBridge] = None):
    """A DeviceService whose remote calls and approvals both go through `bridge`."""
    import skippy_device

    return skippy_device.DeviceService(hub=bridge, client_id=RUN_CLIENT_ID)
