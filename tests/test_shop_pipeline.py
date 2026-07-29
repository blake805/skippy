"""Regression cover for the shop lane after the model cutover.

None of this behaviour is supposed to change. The point of these tests is to make
it obvious if it does: the Tormach/skills/goals path is the production workload and
the agent work must not disturb it.
"""

import json
import os

import pytest

import skippy_factory
from skippy_factory import SkippyPipeline
from tests.test_agent_loop import RecordingSocket


class ShopHub:
    def __init__(self):
        self.active_connections = {}

    async def execute_tool_on_client(self, target, payload, timeout=10.0):
        return {"error": f"Client '{target}' is offline."}


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
