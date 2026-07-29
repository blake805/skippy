"""Agent loop coverage: parsing, tool feedback, guards, and one full multi-file run."""

import json
import os

import pytest

import skippy_agent
from skippy_agent import SkippyAgent, extract_json_objects, parse_tool_call
from tests.fake_llm import raw_tool_call, tool_call

DIVIDE_BODY = (
    "def subtract(left: float, right: float) -> float:\n    return left - right\n\n\n"
    "def divide(left: float, right: float) -> float:\n"
    '    if right == 0:\n        raise ZeroDivisionError("right must be non-zero")\n'
    "    return left / right"
)


class RecordingSocket:
    """Stands in for a Starlette WebSocket, capturing the event stream."""

    def __init__(self, replies=None):
        self.sent = []
        self.replies = list(replies or [])

    async def send_json(self, payload):
        self.sent.append(payload)

    def of_type(self, event_type):
        return [event for event in self.sent if event.get("type") == event_type]


class StubHub:
    """Answers `request_on_socket` with a canned approval decision."""

    def __init__(self, status="APPROVE"):
        self.status = status
        self.asked = []

    async def request_on_socket(self, websocket, payload, timeout=300.0):
        self.asked.append(payload)
        return {"status": self.status}


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parses_a_fenced_tool_call_with_prose_around_it():
    text = 'I should look at the file first.\n\n```json\n{"tool": "read_file", "args": {"path": "calc/ops.py"}}\n```'
    name, args, error = parse_tool_call(text)
    assert (name, args, error) == ("read_file", {"path": "calc/ops.py"}, None)


def test_parses_nested_payloads_a_brace_regex_would_truncate():
    payload = {
        "tool": "apply_patch",
        "args": {"edits": [{"path": "a.py", "search": "if x: {y}", "replace": "if x: {z}"}]},
    }
    name, args, error = parse_tool_call(raw_tool_call(payload))
    assert error is None
    assert name == "apply_patch"
    assert args["edits"][0]["replace"] == "if x: {z}"


def test_tolerates_the_flat_shop_style_call():
    name, args, error = parse_tool_call('{"name": "read_file", "path": "x.py"}')
    assert (name, args, error) == ("read_file", {"path": "x.py"}, None)


def test_prefers_a_known_tool_over_an_illustrative_object():
    text = 'Example shape: {"foo": 1}\n\n```json\n{"tool": "list_dir", "args": {"path": "."}}\n```'
    name, _, error = parse_tool_call(text)
    assert (name, error) == ("list_dir", None)


def test_prose_only_response_is_not_a_tool_call():
    name, args, error = parse_tool_call("I think the bug is in ops.py.")
    assert name is None and args is None and error


def test_extract_json_objects_finds_each_top_level_object():
    found = extract_json_objects('noise {"a": {"b": 1}} more {"c": [1, 2]} tail')
    assert found == [{"a": {"b": 1}}, {"c": [1, 2]}]


# ---------------------------------------------------------------------------
# Loop behaviour
# ---------------------------------------------------------------------------

def _payload(sample_repo, **overrides):
    payload = {
        "mode": "Agent",
        "project_id": "sample",
        "text": "Add a divide() helper, export it, and cover it with a test.",
        "workspace_roots": [sample_repo],
        "max_steps": 12,
    }
    payload.update(overrides)
    return payload


async def test_multi_file_edit_run_end_to_end(routed_llm, sample_repo):
    routed_llm.load(
        [
            tool_call("read_file", thought="Let me read the module.", path="calc/ops.py"),
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
                },
                thought="Editing three files in one atomic patch.",
            ),
            tool_call("run_tests", thought="Now verify.", command="python3 -m pytest -q"),
            tool_call(
                "finish",
                thought="Green.",
                summary="Added divide() with zero handling, exported it, and covered both branches.",
                files_changed=["calc/ops.py", "calc/__init__.py", "tests/test_divide.py"],
            ),
        ]
    )

    socket = RecordingSocket()
    agent = SkippyAgent(socket, _payload(sample_repo), StubHub())
    outcome = await agent.run()

    assert outcome.status == "success", outcome.summary
    assert set(outcome.files_changed) == {
        "calc/ops.py",
        "calc/__init__.py",
        "tests/test_divide.py",
    }

    # The files really changed on disk.
    with open(os.path.join(sample_repo, "calc", "ops.py"), encoding="utf-8") as handle:
        assert "def divide" in handle.read()
    with open(os.path.join(sample_repo, "calc", "__init__.py"), encoding="utf-8") as handle:
        assert "divide" in handle.read()
    assert os.path.exists(os.path.join(sample_repo, "tests", "test_divide.py"))

    # And the tests the agent wrote actually pass.
    test_results = [
        event for event in socket.of_type("agent_tool_result") if event.get("tool") == "run_tests"
    ]
    assert test_results and test_results[0]["ok"], test_results

    patch_events = socket.of_type("agent_patch")
    assert len(patch_events) == 1
    assert {report["path"] for report in patch_events[0]["files"]} == set(outcome.files_changed)
    assert patch_events[0]["diff"]

    done = socket.of_type("agent_done")
    assert len(done) == 1 and done[0]["status"] == "success"
    assert socket.of_type("done")

    # Tool results were fed back as observations, in order.
    observations = routed_llm.observations()
    assert len(observations) == 3
    assert "def subtract" in observations[0]
    assert "Applied 3 file change(s)" in observations[1]


async def test_workspace_tree_is_front_loaded_so_no_step_is_wasted(routed_llm, sample_repo):
    routed_llm.load([tool_call("finish", summary="nothing to do", files_changed=[])])
    agent = SkippyAgent(RecordingSocket(), _payload(sample_repo), StubHub())
    await agent.run()

    first_request = routed_llm.requests[0]["messages"]
    system, user = first_request[0], first_request[-1]
    assert "EXACTLY ONE tool call" in system["content"]
    assert "apply_patch" in system["content"]
    assert "WORKSPACE TREE" in user["content"]
    assert "ops.py" in user["content"]
    assert "TASK:" in user["content"]


async def test_failed_patch_is_reported_back_so_the_model_can_retry(routed_llm, sample_repo):
    routed_llm.load(
        [
            raw_tool_call(
                {
                    "tool": "apply_patch",
                    "args": {
                        "edits": [
                            {"path": "calc/ops.py", "action": "edit", "search": "NOT PRESENT", "replace": "x"}
                        ]
                    },
                }
            ),
            tool_call("finish", summary="Gave up after a bad patch.", files_changed=[]),
        ]
    )

    socket = RecordingSocket()
    outcome = await SkippyAgent(socket, _payload(sample_repo), StubHub()).run()

    assert outcome.status == "success"
    assert outcome.files_changed == []
    assert not socket.of_type("agent_patch")
    failure = socket.of_type("agent_tool_result")[0]
    assert failure["ok"] is False
    assert "'search' text not found" in routed_llm.observations()[0]


async def test_sandbox_escape_is_refused_mid_loop(routed_llm, sample_repo, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("untouched", encoding="utf-8")

    routed_llm.load(
        [
            raw_tool_call(
                {
                    "tool": "apply_patch",
                    "args": {
                        "edits": [
                            {"path": str(victim), "action": "create", "content": "pwned", "overwrite": True}
                        ]
                    },
                }
            ),
            tool_call("finish", summary="Refused.", files_changed=[]),
        ]
    )

    socket = RecordingSocket()
    await SkippyAgent(socket, _payload(sample_repo), StubHub()).run()

    assert victim.read_text(encoding="utf-8") == "untouched"
    assert "outside the workspace roots" in socket.of_type("agent_tool_result")[0]["content"]


async def test_non_test_command_is_blocked_from_run_tests(routed_llm, sample_repo):
    routed_llm.load(
        [
            tool_call("run_tests", command="rm -rf calc"),
            tool_call("finish", summary="Blocked.", files_changed=[]),
        ]
    )

    socket = RecordingSocket()
    await SkippyAgent(socket, _payload(sample_repo), StubHub()).run()

    assert os.path.isdir(os.path.join(sample_repo, "calc"))
    blocked = socket.of_type("agent_tool_result")[0]
    assert blocked["ok"] is False
    assert "not a permitted test runner" in blocked["summary"]
    assert "not a permitted test runner" in routed_llm.observations()[0]


async def test_run_terminal_waits_for_approval_and_honours_a_denial(routed_llm, sample_repo):
    routed_llm.load(
        [
            tool_call("run_terminal", command="touch sneaky.txt", explanation="create a file"),
            tool_call("finish", summary="Denied, stopping.", files_changed=[]),
        ]
    )

    hub = StubHub(status="DENY")
    socket = RecordingSocket()
    await SkippyAgent(socket, _payload(sample_repo), hub).run()

    assert hub.asked and hub.asked[0]["type"] == "terminal_auth"
    assert hub.asked[0]["command"] == "touch sneaky.txt"
    assert not os.path.exists(os.path.join(sample_repo, "sneaky.txt"))
    assert "denied" in socket.of_type("agent_tool_result")[0]["summary"].lower()


async def test_auto_approve_skips_the_human_gate(routed_llm, sample_repo):
    routed_llm.load(
        [
            tool_call("run_terminal", command="touch approved.txt", explanation="create a file"),
            tool_call("finish", summary="Done.", files_changed=[]),
        ]
    )

    hub = StubHub()
    await SkippyAgent(
        RecordingSocket(), _payload(sample_repo, auto_approve={"terminal": True}), hub
    ).run()

    assert hub.asked == []
    assert os.path.exists(os.path.join(sample_repo, "approved.txt"))


async def test_step_budget_terminates_a_runaway_loop(routed_llm, sample_repo):
    routed_llm.load([tool_call("list_dir", path=".") for _ in range(20)])

    socket = RecordingSocket()
    outcome = await SkippyAgent(socket, _payload(sample_repo, max_steps=4), StubHub()).run()

    assert outcome.status == "max_steps"
    assert outcome.steps == 4
    assert socket.of_type("agent_done")[0]["status"] == "max_steps"


async def test_repeated_identical_calls_get_a_course_correction(routed_llm, sample_repo):
    routed_llm.load([tool_call("list_dir", path=".") for _ in range(8)])

    socket = RecordingSocket()
    await SkippyAgent(socket, _payload(sample_repo, max_steps=8), StubHub()).run()

    nudges = [
        event
        for event in socket.of_type("agent_tool_result")
        if "identical arguments" in event.get("summary", "")
    ]
    assert nudges, [event.get("summary") for event in socket.of_type("agent_tool_result")]


async def test_unparseable_output_is_nudged_then_accepted_as_a_final_answer(routed_llm, sample_repo):
    routed_llm.load(["I cannot find the file." for _ in range(5)])

    outcome = await SkippyAgent(RecordingSocket(), _payload(sample_repo), StubHub()).run()

    assert outcome.status == "failed"
    assert "cannot find" in outcome.summary
    assert len(routed_llm.requests) == skippy_agent.NUDGE_LIMIT


async def test_missing_workspace_root_fails_with_actionable_guidance(routed_llm):
    socket = RecordingSocket()
    outcome = await SkippyAgent(
        socket, {"mode": "Agent", "text": "do something", "workspace_roots": ["/nope/nowhere"]}, StubHub()
    ).run()

    assert outcome.status == "failed"
    assert "workspace_roots" in socket.of_type("agent_done")[0]["summary"]
    assert routed_llm.requests == []


async def test_cancellation_stops_the_loop(routed_llm, sample_repo):
    routed_llm.load([tool_call("list_dir", path=".") for _ in range(6)])

    agent = SkippyAgent(RecordingSocket(), _payload(sample_repo), StubHub())
    original_dispatch = skippy_agent.agent_tools.dispatch

    async def dispatch_then_cancel(name, args, ctx):
        result = await original_dispatch(name, args, ctx)
        # Stand in for an inbound agent_cancel arriving mid-step.
        assert skippy_agent.cancel_session(agent.session_id)
        return result

    skippy_agent.agent_tools.dispatch = dispatch_then_cancel
    try:
        outcome = await agent.run()
    finally:
        skippy_agent.agent_tools.dispatch = original_dispatch

    assert outcome.status == "cancelled"
    assert outcome.steps == 1
    assert routed_llm.remaining == 5


async def test_dry_run_leaves_the_tree_untouched(routed_llm, sample_repo):
    with open(os.path.join(sample_repo, "calc", "ops.py"), encoding="utf-8") as handle:
        before = handle.read()

    routed_llm.load(
        [
            raw_tool_call(
                {
                    "tool": "apply_patch",
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
            tool_call("finish", summary="Dry run complete.", files_changed=["calc/ops.py"]),
        ]
    )

    socket = RecordingSocket()
    outcome = await SkippyAgent(socket, _payload(sample_repo, dry_run=True), StubHub()).run()

    assert outcome.status == "success"
    with open(os.path.join(sample_repo, "calc", "ops.py"), encoding="utf-8") as handle:
        assert handle.read() == before
    assert socket.of_type("agent_patch")[0]["diff"]


async def test_oversized_tool_output_is_compressed(routed_llm, sample_repo):
    filler = "\n".join(f"line {index} of padding" for index in range(2000))
    with open(os.path.join(sample_repo, "big.txt"), "w", encoding="utf-8") as handle:
        handle.write(filler)

    routed_llm.compressor_reply = "The file is 2000 lines of padding."
    routed_llm.load(
        [
            tool_call("read_file", path="big.txt"),
            tool_call("finish", summary="Read it.", files_changed=[]),
        ]
    )

    await SkippyAgent(RecordingSocket(), _payload(sample_repo), StubHub()).run()

    observation = routed_llm.observations()[0]
    assert "[compressed]" in observation
    assert "The file is 2000 lines of padding." in observation
    assert len(observation) < skippy_agent.COMPRESS_THRESHOLD


async def test_git_tools_operate_inside_the_repo(routed_llm, sample_git_repo):
    routed_llm.load(
        [
            tool_call("git_status"),
            raw_tool_call(
                {
                    "tool": "apply_patch",
                    "args": {"edits": [{"path": "CHANGELOG.md", "action": "create", "content": "# changelog\n"}]},
                }
            ),
            tool_call("git_commit", message="Add changelog", add_all=True),
            tool_call("git_log", limit=1),
            tool_call("finish", summary="Committed.", files_changed=["CHANGELOG.md"]),
        ]
    )

    socket = RecordingSocket()
    outcome = await SkippyAgent(
        socket, _payload(sample_git_repo, workspace_roots=[sample_git_repo]), StubHub()
    ).run()

    assert outcome.status == "success"
    results = {event["tool"]: event for event in socket.of_type("agent_tool_result")}
    assert results["git_commit"]["ok"], results["git_commit"]
    assert "Add changelog" in results["git_log"]["content"]


async def test_model_outage_surfaces_as_a_failed_run(sample_repo, monkeypatch):
    import skippy_llm

    monkeypatch.setenv("SKIPPY_HEAVY_URL", "http://127.0.0.1:1/v1/chat/completions")
    skippy_llm.reload_registry()
    try:
        outcome = await SkippyAgent(
            RecordingSocket(), _payload(sample_repo), StubHub()
        ).run()
    finally:
        monkeypatch.undo()
        skippy_llm.reload_registry()

    assert outcome.status == "failed"
    assert "Model unreachable" in outcome.summary


async def test_active_registry_is_cleaned_up(routed_llm, sample_repo):
    routed_llm.load([tool_call("finish", summary="done", files_changed=[])])
    agent = SkippyAgent(RecordingSocket(), _payload(sample_repo), StubHub())
    await agent.run()
    assert agent.session_id not in skippy_agent.ACTIVE_AGENTS


def test_redaction_keeps_events_small():
    trimmed = skippy_agent._redact(
        {
            "edits": [{"path": "a.py", "action": "edit", "search": "x" * 5000, "replace": "y" * 5000}],
            "content": "z" * 2000,
            "path": "short.py",
        }
    )
    assert trimmed["edits"] == [{"path": "a.py", "action": "edit"}]
    assert len(trimmed["content"]) < 700
    assert trimmed["path"] == "short.py"
    assert len(json.dumps(trimmed)) < 1500


@pytest.mark.parametrize("mode", ["Agent", "RE"])
def test_re_mode_reuses_the_agent_system_prompt(mode, sample_repo):
    agent = SkippyAgent(RecordingSocket(), _payload(sample_repo, mode=mode), StubHub())
    prompt = agent._system_prompt()
    assert "{{TOOL_SPEC}}" not in prompt
    assert "apply_patch" in prompt
    assert "skills/" not in prompt
