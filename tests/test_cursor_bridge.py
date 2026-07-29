"""The Cursor RPC lane: bridge semantics, agent tools, and a simulated extension."""

import json
import os
import threading

from fastapi.testclient import TestClient

import skippy_agent_tools as agent_tools
import skippy_cursor
from skippy_agent import SkippyAgent
from skippy_agent_tools import ToolContext
from skippy_cursor import CursorBridge, format_diagnostics
from tests.fake_llm import raw_tool_call, tool_call
from tests.test_agent_loop import RecordingSocket, StubHub


class ScriptedHub:
    """A hub whose `cursor` client answers from a canned action -> reply map."""

    def __init__(self, replies=None, connected=True):
        self.replies = dict(replies or {})
        self.active_connections = {"cursor": object()} if connected else {}
        self.calls = []

    async def execute_tool_on_client(self, client_id, payload, timeout=10.0):
        self.calls.append({"client_id": client_id, "timeout": timeout, **payload})
        return self.replies.get(payload["action"], {"error": "no script for this action"})


# ---------------------------------------------------------------------------
# Bridge
# ---------------------------------------------------------------------------

async def test_call_wraps_a_successful_reply():
    hub = ScriptedHub({"get_open_files": {"ok": True, "result": {"files": ["a.py"]}}})
    response = await CursorBridge(hub).call("get_open_files")
    assert response == {"ok": True, "result": {"files": ["a.py"]}}


async def test_call_accepts_a_flat_reply():
    hub = ScriptedHub({"get_workspace_roots": {"roots": [{"path": "/tmp/x"}]}})
    response = await CursorBridge(hub).call("get_workspace_roots")
    assert response["ok"] is True
    assert response["result"]["roots"] == [{"path": "/tmp/x"}]


async def test_call_surfaces_a_hub_transport_error():
    hub = ScriptedHub({"get_diagnostics": {"error": "Timeout: 'cursor' did not respond"}})
    response = await CursorBridge(hub).call("get_diagnostics")
    assert response["ok"] is False
    assert "Timeout" in response["error"]


async def test_call_surfaces_an_explicit_extension_failure():
    hub = ScriptedHub({"apply_patches": {"ok": False, "error": "file is read-only"}})
    response = await CursorBridge(hub).call("apply_patches")
    assert response == {"ok": False, "error": "file is read-only"}


async def test_disconnected_bridge_short_circuits():
    hub = ScriptedHub(connected=False)
    bridge = CursorBridge(hub)
    assert bridge.connected is False
    response = await bridge.call("get_open_files")
    assert response["ok"] is False
    assert "not connected" in response["error"]
    assert hub.calls == []


async def test_each_action_gets_its_own_timeout():
    hub = ScriptedHub({action: {"ok": True, "result": {}} for action in skippy_cursor.ACTION_TIMEOUTS})
    bridge = CursorBridge(hub)
    for action in skippy_cursor.ACTION_TIMEOUTS:
        await bridge.call(action)

    by_action = {call["action"]: call["timeout"] for call in hub.calls}
    assert by_action["get_open_files"] == 10.0
    assert by_action["apply_patches"] == 120.0
    assert by_action["run_task"] == 300.0


async def test_workspace_roots_normalizes_both_shapes():
    hub = ScriptedHub(
        {"get_workspace_roots": {"ok": True, "result": {"roots": [{"name": "a", "path": "/tmp/a"}, "/tmp/b", {}]}}}
    )
    assert await CursorBridge(hub).workspace_roots() == ["/tmp/a", "/tmp/b"]


def test_diagnostics_are_rendered_for_a_model():
    rendered = format_diagnostics(
        {
            "diagnostics": [
                {"path": "calc/ops.py", "line": 12, "col": 5, "severity": "Error", "message": "undefined name", "source": "pyright"},
                {"path": "calc/__init__.py", "line": 3, "severity": "warning", "message": "unused import"},
            ]
        }
    )
    assert "error: calc/ops.py:12:5 undefined name  [pyright]" in rendered
    assert "warning: calc/__init__.py:3 unused import" in rendered


def test_diagnostics_render_is_capped():
    entries = [{"path": "a.py", "line": index, "message": "x"} for index in range(150)]
    rendered = format_diagnostics({"diagnostics": entries}, limit=10)
    assert len(rendered.splitlines()) == 11
    assert "140 more" in rendered


def test_no_diagnostics_renders_empty():
    assert format_diagnostics({"diagnostics": []}) == ""


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------

async def test_cursor_apply_patch_routes_through_the_editor(sandbox, sample_repo):
    hub = ScriptedHub({"apply_patches": {"ok": True, "result": {"applied": ["calc/ops.py"], "failed": []}}})
    ctx = ToolContext(sandbox=sandbox, cursor=CursorBridge(hub))

    result = await agent_tools.cursor_apply_patch(
        ctx,
        [{"path": "calc/ops.py", "action": "edit", "search": "return left + right", "replace": "return 42"}],
    )

    assert result.ok
    assert result.data["via"] == "cursor"
    assert result.data["files"][0]["path"] == "calc/ops.py"
    assert "return 42" in result.data["diff"]
    # The editor owns the write, so the file on disk is untouched by us.
    with open(os.path.join(sample_repo, "calc", "ops.py"), encoding="utf-8") as handle:
        assert "return 42" not in handle.read()
    # And it was handed an absolute path inside the sandbox.
    sent = hub.calls[0]["edits"][0]["path"]
    assert os.path.isabs(sent)
    assert sent.startswith(sandbox.primary)


async def test_cursor_apply_patch_falls_back_to_disk_when_detached(sandbox, sample_repo):
    ctx = ToolContext(sandbox=sandbox, cursor=CursorBridge(ScriptedHub(connected=False)))

    result = await agent_tools.cursor_apply_patch(
        ctx,
        [{"path": "calc/ops.py", "action": "edit", "search": "return left + right", "replace": "return 42"}],
    )

    assert result.ok
    assert result.data["via"] == "filesystem"
    assert "Cursor not attached" in result.summary
    with open(os.path.join(sample_repo, "calc", "ops.py"), encoding="utf-8") as handle:
        assert "return 42" in handle.read()


async def test_cursor_apply_patch_falls_back_when_the_editor_errors(sandbox, sample_repo):
    hub = ScriptedHub({"apply_patches": {"ok": False, "error": "workspace is read-only"}})
    ctx = ToolContext(sandbox=sandbox, cursor=CursorBridge(hub))

    result = await agent_tools.cursor_apply_patch(
        ctx,
        [{"path": "calc/ops.py", "action": "edit", "search": "return left + right", "replace": "return 42"}],
    )

    assert result.ok
    assert result.data["via"] == "filesystem"
    assert "read-only" in result.summary
    with open(os.path.join(sample_repo, "calc", "ops.py"), encoding="utf-8") as handle:
        assert "return 42" in handle.read()


async def test_cursor_apply_patch_reports_per_edit_rejections(sandbox, sample_repo):
    hub = ScriptedHub(
        {"apply_patches": {"ok": True, "result": {"applied": [], "failed": [{"path": "calc/ops.py", "reason": "dirty buffer"}]}}}
    )
    ctx = ToolContext(sandbox=sandbox, cursor=CursorBridge(hub))

    result = await agent_tools.cursor_apply_patch(
        ctx,
        [{"path": "calc/ops.py", "action": "edit", "search": "return left + right", "replace": "return 42"}],
    )

    assert not result.ok
    assert "dirty buffer" in result.content
    with open(os.path.join(sample_repo, "calc", "ops.py"), encoding="utf-8") as handle:
        assert "return 42" not in handle.read()


async def test_cursor_apply_patch_validates_before_involving_the_editor(sandbox):
    hub = ScriptedHub({"apply_patches": {"ok": True, "result": {"failed": []}}})
    ctx = ToolContext(sandbox=sandbox, cursor=CursorBridge(hub))

    result = await agent_tools.cursor_apply_patch(
        ctx, [{"path": "../escape.py", "action": "create", "content": "pwned"}]
    )

    assert not result.ok
    assert "outside the workspace roots" in result.content
    assert hub.calls == []


async def test_cursor_apply_patch_honours_dry_run(sandbox, sample_repo):
    hub = ScriptedHub({"apply_patches": {"ok": True, "result": {"failed": []}}})
    ctx = ToolContext(sandbox=sandbox, cursor=CursorBridge(hub), dry_run=True)

    result = await agent_tools.cursor_apply_patch(
        ctx,
        [{"path": "calc/ops.py", "action": "edit", "search": "return left + right", "replace": "return 42"}],
    )

    assert result.ok
    assert result.data["dry_run"] is True
    assert hub.calls == []


async def test_cursor_diagnostics_tool(sandbox):
    hub = ScriptedHub(
        {"get_diagnostics": {"ok": True, "result": {"diagnostics": [{"path": "calc/ops.py", "line": 4, "severity": "error", "message": "bad"}]}}}
    )
    ctx = ToolContext(sandbox=sandbox, cursor=CursorBridge(hub))

    result = await agent_tools.cursor_diagnostics(ctx, paths=["calc/ops.py"])

    assert result.ok
    assert result.data["count"] == 1
    assert "error: calc/ops.py:4 bad" in result.content
    assert os.path.isabs(hub.calls[0]["paths"][0])


async def test_cursor_diagnostics_without_the_editor_points_elsewhere(sandbox):
    ctx = ToolContext(sandbox=sandbox, cursor=CursorBridge(ScriptedHub(connected=False)))
    result = await agent_tools.cursor_diagnostics(ctx)
    assert not result.ok
    assert "run_tests" in result.summary


async def test_cursor_open_files_tool(sandbox):
    hub = ScriptedHub(
        {"get_open_files": {"ok": True, "result": {"files": [{"path": "a.py", "active": True, "dirty": True}, {"path": "b.py"}]}}}
    )
    ctx = ToolContext(sandbox=sandbox, cursor=CursorBridge(hub))

    result = await agent_tools.cursor_open_files(ctx)

    assert result.ok
    assert "a.py  [active, unsaved]" in result.content
    assert "b.py" in result.content


def test_cursor_tools_are_advertised_in_the_prompt():
    spec = agent_tools.render_tool_spec()
    for name in ("cursor_apply_patch", "cursor_diagnostics", "cursor_open_files"):
        assert name in spec
        assert name in agent_tools.TOOL_SPECS_BY_NAME


# ---------------------------------------------------------------------------
# Loop integration
# ---------------------------------------------------------------------------

async def test_agent_adopts_workspace_roots_from_cursor(routed_llm, sample_repo):
    hub = ScriptedHub({"get_workspace_roots": {"ok": True, "result": {"roots": [{"path": sample_repo}]}}})
    bridge = CursorBridge(hub)
    routed_llm.load(
        [tool_call("list_dir", path="."), tool_call("finish", summary="ok", files_changed=[])]
    )

    agent = SkippyAgent(
        RecordingSocket(),
        {"mode": "Agent", "project_id": "from-cursor", "text": "look around", "max_steps": 5},
        StubHub(),
        cursor_bridge=bridge,
    )
    outcome = await agent.run()

    assert outcome.status == "success"
    assert agent.sandbox.roots == [os.path.realpath(sample_repo)]


async def test_editor_mediated_patch_still_emits_agent_patch(routed_llm, sample_repo):
    hub = ScriptedHub({"apply_patches": {"ok": True, "result": {"applied": ["calc/ops.py"], "failed": []}}})
    routed_llm.load(
        [
            raw_tool_call(
                {
                    "tool": "cursor_apply_patch",
                    "args": {
                        "edits": [
                            {
                                "path": "calc/ops.py",
                                "action": "edit",
                                "search": "return left + right",
                                "replace": "return 42",
                            }
                        ]
                    },
                }
            ),
            tool_call("finish", summary="Applied via Cursor.", files_changed=["calc/ops.py"]),
        ]
    )

    socket = RecordingSocket()
    outcome = await SkippyAgent(
        socket,
        {
            "mode": "Agent",
            "text": "tweak add()",
            "workspace_roots": [sample_repo],
            "max_steps": 5,
        },
        StubHub(),
        cursor_bridge=CursorBridge(hub),
    ).run()

    assert outcome.status == "success"
    assert outcome.files_changed == ["calc/ops.py"]
    patch_event = socket.of_type("agent_patch")[0]
    assert patch_event["via"] == "cursor"
    assert "return 42" in patch_event["diff"]


# ---------------------------------------------------------------------------
# A simulated extension over the real socket
# ---------------------------------------------------------------------------

def test_a_connected_extension_answers_rpcs_over_the_hub(routed_llm, sample_repo):
    """Stands in for cursor_client/: connect as client_id=cursor and echo task_id."""
    import skippy_factory

    diagnostics = [{"path": "calc/ops.py", "line": 2, "severity": "error", "message": "boom"}]
    answered = threading.Event()
    collected = {}

    with TestClient(skippy_factory.app) as client:
        with client.websocket_connect("/ws/factory?client_id=cursor") as extension:
            extension.send_json({"type": "hello"})
            assert extension.receive_json()["type"] == "hello_ack"

            def extension_loop():
                try:
                    while not answered.is_set():
                        request = extension.receive_json()
                        if request.get("action") != "get_diagnostics":
                            continue
                        collected["request"] = request
                        extension.send_json(
                            {
                                "task_id": request["task_id"],
                                "ok": True,
                                "result": {"diagnostics": diagnostics},
                            }
                        )
                        answered.set()
                except Exception:
                    answered.set()

            worker = threading.Thread(target=extension_loop, daemon=True)
            worker.start()

            routed_llm.load(
                [
                    tool_call("cursor_diagnostics", paths=["calc/ops.py"]),
                    tool_call("finish", summary="Saw the diagnostics.", files_changed=[]),
                ]
            )

            with client.websocket_connect("/ws/agent") as agent_socket:
                agent_socket.send_json(
                    {
                        "type": "agent_task",
                        "text": "check for errors",
                        "workspace_roots": [sample_repo],
                        "max_steps": 5,
                    }
                )
                events = []
                for _ in range(200):
                    event = agent_socket.receive_json()
                    events.append(event)
                    if event.get("type") == "done":
                        break

            answered.set()
            worker.join(timeout=5)

    assert collected["request"]["action"] == "get_diagnostics"
    assert collected["request"]["task_id"]

    results = [
        event
        for event in events
        if event.get("type") == "agent_tool_result" and event.get("tool") == "cursor_diagnostics"
    ]
    assert results and results[0]["ok"]
    assert "error: calc/ops.py:2 boom" in results[0]["content"]

    done = [event for event in events if event.get("type") == "agent_done"]
    assert done[0]["status"] == "success"


def test_extension_manifest_matches_the_documented_protocol():
    """The extension is part of the contract; keep its wiring honest."""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    manifest_path = os.path.join(root, "cursor_client", "package.json")
    manifest = json.loads(open(manifest_path, encoding="utf-8").read())

    assert manifest["main"] == "./out/extension.js"
    commands = {command["command"] for command in manifest["contributes"]["commands"]}
    assert {"skippy.connect", "skippy.disconnect", "skippy.status"} <= commands

    settings = manifest["contributes"]["configuration"]["properties"]
    assert "skippy.serverUrl" in settings
    assert "skippy.clientId" in settings
    assert settings["skippy.clientId"]["default"] == "cursor"

    source = open(os.path.join(root, "cursor_client", "src", "extension.ts"), encoding="utf-8").read()
    for action in skippy_cursor.ACTION_TIMEOUTS:
        assert f'case "{action}"' in source, f"extension does not handle {action}"
    assert "task_id" in source
