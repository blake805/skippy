"""Regression cover for the shop lane after the model cutover.

None of this behaviour is supposed to change. The point of these tests is to make
it obvious if it does: the Tormach/skills/goals path is the production workload and
the agent work must not disturb it.
"""

import asyncio
import json
import logging
import os

import pytest
from fastapi import WebSocketDisconnect

import skippy_factory
from skippy_factory import SkippyPipeline
from tests.test_agent_loop import RecordingSocket


class ShopHub:
    def __init__(self):
        self.active_connections = {}

    async def execute_tool_on_client(self, target, payload, timeout=10.0):
        return {"error": f"Client '{target}' is offline."}


class ApprovalHub(ShopHub):
    """Stands in for the hub's `request_on_socket`, answering from a script.

    Stamps a `task_id` and puts the payload on the socket exactly as the real hub
    does, so the outbound wire format stays assertable. An empty script means
    nobody answered, which is the timeout case.
    """

    def __init__(self, *replies):
        super().__init__()
        self.replies = list(replies)
        self.asked = []

    async def request_on_socket(self, websocket, payload, timeout=300.0):
        payload["task_id"] = f"task-{len(self.asked)}"
        self.asked.append(payload)
        await websocket.send_json(payload)
        return self.replies.pop(0) if self.replies else {"status": "TIMEOUT"}


class StrictSocket(RecordingSocket):
    """A socket the pipeline is forbidden to read.

    `_serve_socket` owns the only `receive_text()` call on a socket; a second
    reader in the pipeline is the defect these tests cover.
    """

    async def receive_text(self):
        raise AssertionError("SkippyPipeline must not read the socket directly")


def architect(tool: dict) -> str:
    return f"Thought: acting.\nAction: {json.dumps(tool)}"


BLUEPRINT = "Thought: I have enough.\nAction: BLUEPRINT: Write a function that returns 4."
APPROVED_CODE = "```python\nprint('hello from the shop')\n```"
QA_APPROVE = 'Looks good.\n{"status": "APPROVE", "save_path": "skills/shop_demo.py"}'


# ---------------------------------------------------------------------------
# Model routing
# ---------------------------------------------------------------------------

def test_role_aliases_point_at_the_new_fleet():
    assert skippy_factory.LOCAL_70B_URL == skippy_factory.MODELS["fast"].url
    assert skippy_factory.LOCAL_405B_URL == skippy_factory.MODELS["heavy"].url
    assert skippy_factory.LOCAL_COMPRESSOR_URL == skippy_factory.MODELS["compressor"].url
    assert skippy_factory.MODEL_70B_NAME == skippy_factory.MODELS["fast"].model
    assert skippy_factory.MODEL_405B_NAME == skippy_factory.MODELS["heavy"].model
    assert skippy_factory.MODEL_COMPRESSOR_NAME == skippy_factory.MODELS["compressor"].model


async def test_a_role_gets_its_own_max_tokens(routed_llm, monkeypatch):
    """The heavy path must not inherit the old flat 4096 cap."""
    import skippy_llm

    monkeypatch.setenv("SKIPPY_HEAVY_MAX_TOKENS", "12345")
    skippy_llm.reload_registry()
    try:
        routed_llm.load(["ok"])
        await skippy_factory.query_model_async([{"role": "user", "content": "hi"}], role="heavy")
    finally:
        monkeypatch.undo()
        skippy_llm.reload_registry()

    assert routed_llm.requests[0]["max_tokens"] == 12345


def test_a_distinct_url_maps_back_to_its_role():
    """The legacy url= path is a reverse lookup, which is why role= is preferred."""
    import skippy_llm

    for role in ("fast", "heavy", "compressor"):
        found = skippy_llm.endpoint_for_url(skippy_llm.MODELS[role].url)
        assert found is not None and found.role == role
    assert skippy_llm.endpoint_for_url("http://127.0.0.1:9/v1/chat/completions") is None


async def test_a_legacy_url_caller_still_reaches_a_model(routed_llm):
    import skippy_llm

    routed_llm.load(["ok"])
    reply = await skippy_factory.query_model_async(
        [{"role": "user", "content": "hi"}], url=skippy_llm.MODELS["compressor"].url
    )
    assert reply == "ok"


async def test_an_unknown_url_falls_back_to_fast(routed_llm):
    import skippy_llm

    routed_llm.load(["ok"])
    reply = await skippy_factory.query_model_async(
        [{"role": "user", "content": "hi"}], url="http://127.0.0.1:9999/v1/chat/completions"
    )
    assert reply == "ok"
    assert routed_llm.requests[0]["max_tokens"] == skippy_llm.MODELS["fast"].max_tokens


# ---------------------------------------------------------------------------
# Pipeline behaviour
# ---------------------------------------------------------------------------

async def test_direct_reply_short_circuits_the_pipeline(routed_llm):
    routed_llm.load([architect({"name": "direct_reply", "message": "0.004 inches per tooth."})])

    socket = RecordingSocket()
    pipeline = SkippyPipeline(socket, {"mode": "Shop", "text": "chipload for 6061?"}, ShopHub())
    await pipeline.run()

    assert pipeline.is_direct_reply
    assert any("0.004 inches per tooth." in event.get("content", "") for event in socket.of_type("chat"))
    assert socket.of_type("done")
    # No engineer, no QA, no summarizer.
    assert len(routed_llm.requests) == 1


async def test_a_shop_tool_result_is_fed_back_to_the_architect(routed_llm):
    routed_llm.load(
        [
            architect({"name": "get_system_time"}),
            architect({"name": "direct_reply", "message": "Told you the time."}),
        ]
    )

    pipeline = SkippyPipeline(
        RecordingSocket(), {"mode": "Shop", "text": "what time is it?"}, ShopHub()
    )
    await pipeline.run()

    second_turn = routed_llm.requests[1]["messages"][-1]["content"]
    assert second_turn.startswith("TOOL RESULT:")


async def test_approved_code_still_lands_in_skills(routed_llm, tmp_path, monkeypatch):
    monkeypatch.setattr(skippy_factory, "SKILLS_DIR", str(tmp_path / "skills"))
    os.makedirs(skippy_factory.SKILLS_DIR, exist_ok=True)

    routed_llm.load([BLUEPRINT, "SIMPLE", APPROVED_CODE, QA_APPROVE, "All done, obviously."])

    socket = RecordingSocket()
    pipeline = SkippyPipeline(socket, {"mode": "Shop", "text": "write a demo skill"}, ShopHub())
    await pipeline.run()

    assert pipeline.success
    saved = os.path.join(skippy_factory.SKILLS_DIR, "shop_demo.py")
    assert os.path.exists(saved)
    assert "hello from the shop" in open(saved, encoding="utf-8").read()
    assert any("All done" in event.get("content", "") for event in socket.of_type("chat"))


def role_spy(monkeypatch):
    """Record the role of every model call the pipeline makes."""
    seen = []
    original = skippy_factory.query_model_async

    async def spy(messages, temp=0.2, url=None, model_name=None, stop_sequences=None, role=None):
        seen.append(role)
        return await original(messages, temp, url, model_name, stop_sequences, role)

    monkeypatch.setattr(skippy_factory, "query_model_async", spy)
    return seen


async def test_triage_routes_a_complex_task_to_the_heavy_role(routed_llm, monkeypatch):
    """COMPLEX now means GLM-5.2 on the heavy role, not the retired 405B."""
    seen = role_spy(monkeypatch)
    routed_llm.load([BLUEPRINT, "COMPLEX"] + ["FAIL: no"] * 10)

    pipeline = SkippyPipeline(RecordingSocket(), {"mode": "Shop", "text": "big job"}, ShopHub())
    await pipeline.phase_1_research("big job")
    await pipeline.phase_2_engineer_and_qa("big job")

    assert seen[0] == "fast", "architect stays on the fast role"
    assert seen[1] == "fast", "triage stays on the fast role"
    assert "heavy" in seen, "the engineer should be on the heavy role"


async def test_triage_keeps_a_simple_task_on_the_fast_role(routed_llm, monkeypatch):
    seen = role_spy(monkeypatch)
    routed_llm.load([BLUEPRINT, "SIMPLE"] + ["FAIL: no"] * 10)

    pipeline = SkippyPipeline(RecordingSocket(), {"mode": "Shop", "text": "small job"}, ShopHub())
    await pipeline.phase_1_research("small job")
    await pipeline.phase_2_engineer_and_qa("small job")

    assert "heavy" not in seen


async def test_developer_mode_bypasses_triage_for_the_heavy_role(routed_llm, monkeypatch):
    seen = role_spy(monkeypatch)
    routed_llm.load(["something that is not valid patch json"] * 10)

    pipeline = SkippyPipeline(RecordingSocket(), {"mode": "Developer", "text": "upgrade"}, ShopHub())
    pipeline.blueprint = "Add a feature."
    await pipeline.phase_2_engineer_and_qa("upgrade")

    # No triage call: the first request already goes to the heavy role.
    assert seen[0] == "heavy"


async def test_search_codebase_results_go_through_the_compressor(routed_llm, monkeypatch):
    async def fake_search(query, collection, n_results=3):
        return "raw chunk one\nraw chunk two"

    monkeypatch.setattr(skippy_factory.tools, "search_codebase", fake_search)
    routed_llm.compressor_reply = "Dense summary of the code."
    routed_llm.load(
        [
            architect({"name": "search_codebase", "query": "feed rate math"}),
            architect({"name": "direct_reply", "message": "Here you go."}),
        ]
    )

    pipeline = SkippyPipeline(
        RecordingSocket(), {"mode": "Shop", "text": "how is feed rate computed?"}, ShopHub()
    )
    await pipeline.run()

    follow_up = routed_llm.requests[-1]["messages"][-1]["content"]
    assert "COMPRESSED MEMORY RESULT:" in follow_up
    assert "Dense summary of the code." in follow_up


async def test_goal_ledger_still_auto_claims_and_completes(routed_llm, tmp_path, monkeypatch):
    goals = tmp_path / "skippy_goals.json"
    goals.write_text(json.dumps({"tasks": [{"id": 1, "task": "x", "status": "pending"}]}), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(skippy_factory.os.path, "dirname", lambda _: str(tmp_path))
    monkeypatch.setattr(skippy_factory, "SKILLS_DIR", str(tmp_path / "skills"))
    os.makedirs(skippy_factory.SKILLS_DIR, exist_ok=True)

    routed_llm.load([BLUEPRINT, "SIMPLE", APPROVED_CODE, QA_APPROVE, "Done."])

    pipeline = SkippyPipeline(RecordingSocket(), {"mode": "Shop", "text": "do it"}, ShopHub())
    await pipeline.run()

    ledger = json.loads(goals.read_text(encoding="utf-8"))["tasks"]
    assert ledger[0]["status"] == "completed"


async def test_headless_pipeline_survives_without_a_socket(routed_llm):
    """The heartbeat runs the pipeline with websocket=None."""
    routed_llm.load([architect({"name": "direct_reply", "message": "Ledger empty. Idling."})])

    pipeline = SkippyPipeline(None, {"mode": "Shop", "text": "[SYSTEM TICK]"}, ShopHub())
    await pipeline.run()

    assert pipeline.is_direct_reply


@pytest.mark.parametrize("mode", ["Shop", "Software", "CNC", "Developer", "Whiteboard"])
def test_every_shop_mode_still_has_its_prompt_block(mode):
    from prompts import PROMPTS

    assert set(PROMPTS[mode]) >= {"architect", "engineer", "qa", "summarizer"}


def test_adding_the_agent_prompt_did_not_disturb_the_shop_lookup():
    from prompts import PROMPTS

    # The pipeline's fallback must never resolve to the Agent block.
    assert PROMPTS.get("NotAMode", PROMPTS["Shop"]) is PROMPTS["Shop"]
    assert "architect" not in PROMPTS["Agent"]
    assert "skills/" in PROMPTS["Shop"]["qa"]


# ---------------------------------------------------------------------------
# Human approval gates
#
# All three used to read the socket directly while `_serve_socket` was reading
# the same socket, so the answer went to whichever coroutine happened to be
# parked in `receive_text`. They now round-trip through the hub keyed by
# `task_id`. What gets authorized is unchanged; only the transport moved.
# ---------------------------------------------------------------------------

SSH_REQUEST = {
    "name": "tormach_ssh",
    "command": "halui.machine.off",
    "explanation": "Drop the spindle before the operator opens the door.",
}
QA_DEPLOY_TEMPLATE = 'Ship it.\n{{"status": "DEPLOY", "target_file": "{path}", "summary": "Upgrade."}}'

# `socket=None` means headless, so the helpers need a separate "not supplied".
UNSET = object()


def tormach_spy(monkeypatch):
    """Record Tormach SSH commands instead of reaching for the real machine."""
    commands = []

    async def fake_ssh(command):
        commands.append(command)
        return "PathPilot: machine off."

    monkeypatch.setattr(skippy_factory.tools, "execute_tormach_ssh", fake_ssh)
    return commands


async def run_ssh_gate(routed_llm, hub, socket=UNSET):
    """Drive phase 1 to the `tormach_ssh` gate, then let the Architect wrap up."""
    routed_llm.load(
        [
            architect(SSH_REQUEST),
            architect({"name": "direct_reply", "message": "Done."}),
        ]
    )
    pipeline = SkippyPipeline(
        StrictSocket() if socket is UNSET else socket,
        {"mode": "Shop", "text": "kill the spindle"},
        hub,
    )
    await pipeline.run()
    return routed_llm.requests[-1]["messages"][-1]["content"]


async def test_an_approved_tormach_command_still_reaches_pathpilot(routed_llm, monkeypatch):
    commands = tormach_spy(monkeypatch)

    feedback = await run_ssh_gate(routed_llm, ApprovalHub({"status": "APPROVE"}))

    assert commands == ["halui.machine.off"]
    assert "PathPilot: machine off." in feedback


async def test_a_denied_tormach_command_never_runs(routed_llm, monkeypatch):
    commands = tormach_spy(monkeypatch)

    feedback = await run_ssh_gate(routed_llm, ApprovalHub({"status": "DENY"}))

    assert commands == []
    assert "USER DENIED SSH EXECUTION" in feedback


async def test_an_unanswered_tormach_request_fails_closed(routed_llm, monkeypatch):
    """Safety-critical: no answer must never read as approval."""
    commands = tormach_spy(monkeypatch)

    feedback = await run_ssh_gate(routed_llm, ApprovalHub())  # empty script == timeout

    assert commands == []
    assert "USER DENIED SSH EXECUTION" in feedback


@pytest.mark.parametrize("status", ["approve", "Approve", "MAYBE", "", None])
async def test_only_an_exact_approve_authorizes_the_machine(routed_llm, monkeypatch, status):
    """The old code compared `== "APPROVE"`. Broadening that would widen the gate."""
    commands = tormach_spy(monkeypatch)

    await run_ssh_gate(routed_llm, ApprovalHub({"status": status}))

    assert commands == []


async def test_the_terminal_auth_event_keeps_the_keys_swiftui_expects(routed_llm, monkeypatch):
    tormach_spy(monkeypatch)
    hub = ApprovalHub({"status": "APPROVE"})
    socket = StrictSocket()

    await run_ssh_gate(routed_llm, hub, socket=socket)

    event = socket.of_type("terminal_auth")[0]
    assert event["command"] == "halui.machine.off"
    assert event["explanation"] == SSH_REQUEST["explanation"]
    # The one addition: clients must echo this back for the reply to be routed.
    assert event["task_id"] == hub.asked[0]["task_id"]


async def test_a_headless_tormach_request_is_refused_without_a_socket(routed_llm, monkeypatch):
    """The heartbeat runs with websocket=None and must not authorize anything."""
    commands = tormach_spy(monkeypatch)

    feedback = await run_ssh_gate(routed_llm, ApprovalHub({"status": "APPROVE"}), socket=None)

    assert commands == []
    assert "HEADLESS ERROR" in feedback


def bash_spy(monkeypatch):
    """Record god-mode commands instead of running a real shell."""
    commands = []

    async def fake_stream(command, websocket):
        commands.append(command)
        return "total 0"

    monkeypatch.setattr(skippy_factory, "run_bash_command_stream", fake_stream)
    return commands


async def run_god_mode_gate(routed_llm, monkeypatch, tmp_path, hub):
    monkeypatch.setattr(skippy_factory, "SKILLS_DIR", str(tmp_path / "skills"))
    os.makedirs(skippy_factory.SKILLS_DIR, exist_ok=True)
    routed_llm.load(
        [
            "SIMPLE",
            json.dumps({"name": "request_terminal_execution", "command": "ls /tmp", "explanation": "peek"}),
            APPROVED_CODE,
            QA_APPROVE,
        ]
    )
    pipeline = SkippyPipeline(StrictSocket(), {"mode": "Shop", "text": "look around"}, hub)
    pipeline.blueprint = "List a directory, then write the demo skill."
    await pipeline.phase_2_engineer_and_qa("look around")
    # The turn after the gate carries the verdict back to the Engineer.
    return routed_llm.requests[2]["messages"][-1]["content"]


async def test_an_approved_god_mode_command_runs(routed_llm, monkeypatch, tmp_path):
    commands = bash_spy(monkeypatch)

    feedback = await run_god_mode_gate(
        routed_llm, monkeypatch, tmp_path, ApprovalHub({"status": "APPROVE"})
    )

    assert commands == ["ls /tmp"]
    assert "COMMAND EXECUTED" in feedback


async def test_a_denied_god_mode_command_never_runs(routed_llm, monkeypatch, tmp_path):
    commands = bash_spy(monkeypatch)

    feedback = await run_god_mode_gate(
        routed_llm, monkeypatch, tmp_path, ApprovalHub({"status": "DENY"})
    )

    assert commands == []
    assert "USER DENIED" in feedback


async def test_an_unanswered_god_mode_request_fails_closed(routed_llm, monkeypatch, tmp_path):
    commands = bash_spy(monkeypatch)

    feedback = await run_god_mode_gate(routed_llm, monkeypatch, tmp_path, ApprovalHub())

    assert commands == []
    assert "USER DENIED" in feedback


async def run_deploy_gate(routed_llm, tmp_path, hub, socket=UNSET):
    target = tmp_path / "deploy_target.py"
    target.write_text("# original\n", encoding="utf-8")
    routed_llm.load(
        [
            "SIMPLE",
            APPROVED_CODE,
            QA_DEPLOY_TEMPLATE.format(path=str(target)),
            # A denial sends the Engineer round again; empty turns end the loop
            # without touching the sandbox.
            "",
            "",
            "",
        ]
    )
    pipeline = SkippyPipeline(
        StrictSocket() if socket is UNSET else socket,
        {"mode": "Shop", "text": "upgrade yourself"},
        hub,
    )
    pipeline.blueprint = "Rewrite the target file."
    await pipeline.phase_2_engineer_and_qa("upgrade yourself")
    return pipeline, target


async def test_an_approved_deployment_overwrites_the_target(routed_llm, tmp_path):
    pipeline, target = await run_deploy_gate(routed_llm, tmp_path, ApprovalHub({"status": "APPROVE"}))

    assert pipeline.success
    assert "hello from the shop" in target.read_text(encoding="utf-8")


async def test_a_denied_deployment_leaves_the_target_alone(routed_llm, tmp_path):
    pipeline, target = await run_deploy_gate(routed_llm, tmp_path, ApprovalHub({"status": "DENY"}))

    assert not pipeline.success
    assert target.read_text(encoding="utf-8") == "# original\n"


async def test_an_unanswered_deployment_fails_closed(routed_llm, tmp_path):
    pipeline, target = await run_deploy_gate(routed_llm, tmp_path, ApprovalHub())

    assert not pipeline.success
    assert target.read_text(encoding="utf-8") == "# original\n"


async def test_the_deployment_auth_event_keeps_the_keys_swiftui_expects(routed_llm, tmp_path):
    hub = ApprovalHub({"status": "APPROVE"})
    socket = StrictSocket()

    _, target = await run_deploy_gate(routed_llm, tmp_path, hub, socket=socket)

    event = socket.of_type("deployment_auth")[0]
    assert event["target_file"] == str(target)
    assert event["summary"] == "Upgrade."
    assert "hello from the shop" in event["content"]
    assert event["task_id"] == hub.asked[0]["task_id"]


async def test_two_pipelines_on_one_socket_each_get_their_own_answer():
    """The race the fix removes, against the real ConnectionManager.

    Both pipelines hold an approval open on one socket and the answers arrive in
    the opposite order. Keyed by `task_id`, each lands where it belongs; read off
    the raw socket, the deploy gate would have eaten the SSH denial.
    """
    hub = skippy_factory.ConnectionManager()
    socket = StrictSocket()
    deploy = SkippyPipeline(socket, {"mode": "Shop", "text": "deploy"}, hub)
    ssh = SkippyPipeline(socket, {"mode": "Shop", "text": "ssh"}, hub)

    async def answer_in_reverse():
        while not (socket.of_type("deployment_auth") and socket.of_type("terminal_auth")):
            await asyncio.sleep(0.01)
        hub.resolve_response(socket.of_type("terminal_auth")[0]["task_id"], {"status": "DENY"})
        await asyncio.sleep(0.01)
        hub.resolve_response(socket.of_type("deployment_auth")[0]["task_id"], {"status": "APPROVE"})

    responder = asyncio.create_task(answer_in_reverse())
    deploy_ok, ssh_ok = await asyncio.gather(
        deploy.await_authorization({"type": "deployment_auth", "target_file": "a.py"}, timeout=5.0),
        ssh.await_authorization({"type": "terminal_auth", "command": "rm -rf /"}, timeout=5.0),
    )
    await responder

    assert deploy_ok is True
    assert ssh_ok is False, "the SSH denial must not be delivered to the deploy gate"
    assert hub.pending_responses == {}


# ---------------------------------------------------------------------------
# Legacy approval bridge
#
# The SwiftUI app does not echo `task_id` yet (docs/adr/0005-approval-routing.md),
# so until it ships, `_serve_socket` bridges a reply-shaped message — no task_id,
# but a "status" field — to the sole pending approval on that socket. Ambiguity
# is never guessed at, every bridged reply is logged at WARNING, and
# SKIPPY_STRICT_AUTH_TASK_ID=1 turns the whole thing off.
# ---------------------------------------------------------------------------

DISCONNECT = object()


class LiveSocket(RecordingSocket):
    """Drives `_serve_socket` directly: scripted inbound, recorded outbound."""

    def __init__(self):
        super().__init__()
        self.inbound = asyncio.Queue()

    async def accept(self):
        pass

    async def receive_text(self):
        item = await self.inbound.get()
        if item is DISCONNECT:
            raise WebSocketDisconnect(1000)
        return item

    def push(self, payload: dict):
        self.inbound.put_nowait(json.dumps(payload))

    def hang_up(self):
        self.inbound.put_nowait(DISCONNECT)


@pytest.fixture
def bridge_hub(monkeypatch):
    """A fresh ConnectionManager wired into `_serve_socket`, plus a pipeline spy
    that records what would have been dispatched as a new Shop task."""
    hub = skippy_factory.ConnectionManager()
    monkeypatch.setattr(skippy_factory, "hub", hub)

    dispatched = []

    class SpyPipeline:
        def __init__(self, websocket, payload, manager):
            dispatched.append(payload)

        async def run(self):
            pass

    monkeypatch.setattr(skippy_factory, "SkippyPipeline", SpyPipeline)
    return hub, dispatched


async def open_socket(hub):
    socket = LiveSocket()
    server = asyncio.create_task(skippy_factory._serve_socket(socket, "swiftui", "Shop"))
    while "swiftui" not in hub.active_connections:
        await asyncio.sleep(0.01)
    return socket, server


async def hold_approval(hub, socket, payload, sent_before=0):
    """Park an approval request on the socket, as `await_authorization` does."""
    waiter = asyncio.create_task(hub.request_on_socket(socket, payload, timeout=5.0))
    while len(socket.sent) <= sent_before:
        await asyncio.sleep(0.01)
    return waiter, socket.sent[-1]["task_id"]


async def close_socket(socket, server):
    socket.hang_up()
    await asyncio.wait_for(server, timeout=2.0)


def warnings_from(caplog):
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


async def test_a_legacy_reply_is_bridged_to_the_single_pending_approval(bridge_hub, caplog):
    hub, dispatched = bridge_hub
    socket, server = await open_socket(hub)
    waiter, task_id = await hold_approval(
        hub, socket, {"type": "terminal_auth", "command": "ls", "explanation": "peek"}
    )

    with caplog.at_level(logging.WARNING, logger="skippy_factory"):
        socket.push({"status": "APPROVE"})
        reply = await asyncio.wait_for(waiter, timeout=2.0)

    assert reply["status"] == "APPROVE", "the bridge must deliver the approval"
    assert dispatched == [], "the reply must not become a new Shop task"
    assert any(task_id in message for message in warnings_from(caplog)), (
        "every bridged reply is logged at WARNING with the task_id it matched"
    )
    assert hub.pending_responses == {}
    await close_socket(socket, server)


async def test_two_pending_approvals_are_never_guessed_between(bridge_hub, caplog):
    hub, dispatched = bridge_hub
    socket, server = await open_socket(hub)
    first, _ = await hold_approval(hub, socket, {"type": "terminal_auth", "command": "a"})
    second, _ = await hold_approval(
        hub, socket, {"type": "deployment_auth", "target_file": "x.py"}, sent_before=1
    )

    with caplog.at_level(logging.WARNING, logger="skippy_factory"):
        socket.push({"status": "APPROVE"})
        while not dispatched:
            await asyncio.sleep(0.01)

    assert not first.done() and not second.done(), "neither gate may receive a guessed answer"
    assert dispatched == [{"status": "APPROVE"}], "today's behaviour: it becomes a new task"
    assert any("refusing to guess" in message for message in warnings_from(caplog))

    for payload in socket.sent:
        hub.resolve_response(payload["task_id"], {"status": "DENY"})
    await asyncio.gather(first, second)
    await close_socket(socket, server)


async def test_a_legacy_reply_with_nothing_pending_falls_through(bridge_hub, caplog):
    hub, dispatched = bridge_hub
    socket, server = await open_socket(hub)

    with caplog.at_level(logging.WARNING, logger="skippy_factory"):
        socket.push({"status": "APPROVE"})
        while not dispatched:
            await asyncio.sleep(0.01)

    assert dispatched == [{"status": "APPROVE"}]
    assert any("no approval is pending" in message for message in warnings_from(caplog))
    await close_socket(socket, server)


async def test_an_agent_rpc_is_invisible_to_the_bridge(bridge_hub):
    """An agent task's RPC and a shop approval pending on one socket: the legacy
    reply must land on the approval and never on the agent's future."""
    hub, dispatched = bridge_hub
    socket, server = await open_socket(hub)

    rpc = asyncio.create_task(
        hub.execute_tool_on_client("swiftui", {"action": "get_active_file"}, timeout=5.0)
    )
    while not socket.sent:
        await asyncio.sleep(0.01)
    approval, _ = await hold_approval(
        hub, socket, {"type": "terminal_auth", "command": "ls"}, sent_before=1
    )

    socket.push({"status": "APPROVE"})
    reply = await asyncio.wait_for(approval, timeout=2.0)

    assert reply["status"] == "APPROVE"
    assert not rpc.done(), "the agent's future must never be resolved by a legacy shop reply"
    assert dispatched == []

    # The RPC reply carries its task_id, as Cursor replies always do, and is
    # routed exactly as before — the bridge never sees it.
    rpc_id = next(p["task_id"] for p in socket.sent if p.get("action") == "get_active_file")
    socket.push({"task_id": rpc_id, "content": "main.py"})
    assert (await asyncio.wait_for(rpc, timeout=2.0)) == {"task_id": rpc_id, "content": "main.py"}
    assert hub.pending_responses == {}
    await close_socket(socket, server)


async def test_strict_mode_refuses_a_reply_without_task_id(bridge_hub, caplog, monkeypatch):
    """SKIPPY_STRICT_AUTH_TASK_ID=1 is the switch to flip once the SwiftUI app
    echoes task_id: the bridge is off and the legacy reply is dropped outright."""
    monkeypatch.setenv("SKIPPY_STRICT_AUTH_TASK_ID", "1")
    hub, dispatched = bridge_hub
    socket, server = await open_socket(hub)
    waiter, task_id = await hold_approval(hub, socket, {"type": "terminal_auth", "command": "ls"})

    with caplog.at_level(logging.WARNING, logger="skippy_factory"):
        socket.push({"status": "APPROVE"})
        socket.push({"type": "ping"})  # a later message proves the reply was consumed
        while not socket.of_type("hello_ack"):
            await asyncio.sleep(0.01)

    assert not waiter.done(), "strict mode must not bridge; the gate times out closed"
    assert dispatched == [], "strict mode must not dispatch the reply as a task either"
    assert any("SKIPPY_STRICT_AUTH_TASK_ID" in message for message in warnings_from(caplog))

    hub.resolve_response(task_id, {"status": "DENY"})
    await waiter
    await close_socket(socket, server)


async def test_a_modern_reply_with_task_id_bypasses_the_bridge_entirely(bridge_hub):
    """Once the app echoes task_id, routing is exact even with several approvals
    pending — the bridge's one-candidate rule never comes into play."""
    hub, dispatched = bridge_hub
    socket, server = await open_socket(hub)
    first, first_id = await hold_approval(hub, socket, {"type": "terminal_auth", "command": "a"})
    second, second_id = await hold_approval(
        hub, socket, {"type": "deployment_auth", "target_file": "x.py"}, sent_before=1
    )

    socket.push({"status": "APPROVE", "task_id": second_id})
    assert (await asyncio.wait_for(second, timeout=2.0))["status"] == "APPROVE"
    assert not first.done()

    socket.push({"status": "DENY", "task_id": first_id})
    assert (await asyncio.wait_for(first, timeout=2.0))["status"] == "DENY"
    assert dispatched == []
    assert hub.pending_responses == {}
    await close_socket(socket, server)
