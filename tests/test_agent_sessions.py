"""The agent loop wired to project memory, plus heartbeat project routing."""

import json
import os

import pytest

from skippy_agent import SkippyAgent
from skippy_sessions import SessionStore
from tests.fake_llm import raw_tool_call, tool_call
from tests.test_agent_loop import RecordingSocket, StubHub


@pytest.fixture
def store(tmp_path):
    return SessionStore(root=str(tmp_path / "projects"))


def payload(sample_repo, **overrides):
    body = {
        "mode": "Agent",
        "project_id": "shop-jarvis",
        "text": "Rename the add() helper's parameters.",
        "workspace_roots": [sample_repo],
        "max_steps": 10,
    }
    body.update(overrides)
    return body


async def test_a_run_persists_a_session_transcript(routed_llm, store, sample_repo):
    routed_llm.load(
        [
            tool_call("read_file", thought="Looking at ops.", path="calc/ops.py"),
            tool_call("finish", summary="No change needed.", files_changed=[]),
        ]
    )

    agent = SkippyAgent(
        RecordingSocket(), payload(sample_repo, session_id="s-persist"), StubHub(), session_store=store
    )
    outcome = await agent.run()

    assert outcome.status == "success"
    saved = store.load_session("shop-jarvis", "s-persist")
    assert saved["task"] == "Rename the add() helper's parameters."
    assert saved["status"] == "success"
    assert saved["summary"] == "No change needed."
    assert [turn["tool"] for turn in saved["turns"]] == ["read_file"]
    assert saved["turns"][0]["thought"] == "Looking at ops."
    assert saved["workspace_roots"] == [sample_repo]


async def test_patch_pre_images_land_beside_the_session(routed_llm, store, sample_repo):
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
                                "replace": "return left + right  # touched",
                            }
                        ]
                    },
                }
            ),
            tool_call("finish", summary="Touched ops.", files_changed=["calc/ops.py"]),
        ]
    )

    await SkippyAgent(
        RecordingSocket(), payload(sample_repo, session_id="s-patch"), StubHub(), session_store=store
    ).run()

    backups = os.path.join(store.patches_dir("shop-jarvis"), "s-patch", "1")
    assert os.path.isdir(backups)
    pre_image = open(os.path.join(backups, "calc__ops.py.orig"), encoding="utf-8").read()
    assert "# touched" not in pre_image
    assert "return left + right" in pre_image

    manifest = json.loads(open(os.path.join(backups, "manifest.json"), encoding="utf-8").read())
    assert manifest[0]["action"] == "edit"


async def test_a_second_session_inherits_earlier_decisions(routed_llm, store, sample_repo):
    """Phase 3 exit criterion, end to end through the loop."""
    routed_llm.load(
        [
            tool_call(
                "save_decision",
                title="divide() raises on zero",
                body="We chose ZeroDivisionError over returning None so callers cannot ignore it.",
                tags=["api"],
            ),
            tool_call("finish", summary="Recorded the decision.", files_changed=[]),
        ]
    )
    first = SkippyAgent(
        RecordingSocket(), payload(sample_repo, session_id="s-one"), StubHub(), session_store=store
    )
    await first.run()

    saved = store.load_session("shop-jarvis", "s-one")
    assert saved["decisions"] == ["dec-0001"]

    # A fresh session, days later, asking about the same area.
    routed_llm.load([tool_call("finish", summary="Already decided.", files_changed=[])])
    second = SkippyAgent(
        RecordingSocket(),
        payload(sample_repo, session_id="s-two", text="Should divide() return None on zero?"),
        StubHub(),
        session_store=store,
    )
    await second.run()

    opening_prompt = routed_llm.requests[0]["messages"][-1]["content"]
    assert "RELEVANT PROJECT MEMORY" in opening_prompt
    assert "ZeroDivisionError" in opening_prompt


async def test_project_conventions_reach_the_prompt(routed_llm, store, sample_repo):
    store.ensure_project(
        "shop-jarvis",
        workspace_roots=[sample_repo],
        conventions={"test_command": "python3 -m pytest -q", "package_manager": "pip"},
    )
    routed_llm.load([tool_call("finish", summary="ok", files_changed=[])])

    await SkippyAgent(
        RecordingSocket(), payload(sample_repo), StubHub(), session_store=store
    ).run()

    opening_prompt = routed_llm.requests[0]["messages"][-1]["content"]
    assert "PROJECT CONVENTIONS" in opening_prompt
    assert "python3 -m pytest -q" in opening_prompt


async def test_roots_are_recovered_from_project_meta(routed_llm, store, sample_repo):
    store.ensure_project("shop-jarvis", workspace_roots=[sample_repo])
    routed_llm.load(
        [
            tool_call("list_dir", path="."),
            tool_call("finish", summary="Found it.", files_changed=[]),
        ]
    )

    body = payload(sample_repo)
    body.pop("workspace_roots")
    outcome = await SkippyAgent(
        RecordingSocket(), body, StubHub(), session_store=store
    ).run()

    assert outcome.status == "success"
    assert store.load_session("shop-jarvis", outcome_session_id(store)) is not None


def outcome_session_id(store):
    sessions = os.listdir(store.sessions_dir("shop-jarvis"))
    assert len(sessions) == 1
    return sessions[0][: -len(".json")]


async def test_project_stats_accumulate_across_sessions(routed_llm, store, sample_repo):
    for index, name in enumerate(("s-a", "s-b")):
        routed_llm.load(
            [tool_call("finish", summary=f"run {index}", files_changed=[f"file{index}.py"])]
        )
        await SkippyAgent(
            RecordingSocket(), payload(sample_repo, session_id=name), StubHub(), session_store=store
        ).run()

    meta = store.project_meta("shop-jarvis")
    assert meta["stats"]["sessions"] == 2
    assert meta["all_files_touched"] == ["file0.py", "file1.py"]
    assert meta["last_session"]["session_id"] == "s-b"


async def test_a_crashing_session_is_still_closed_out(routed_llm, store, sample_repo, monkeypatch):
    import skippy_agent

    async def explode(*args, **kwargs):
        raise RuntimeError("boom")

    routed_llm.load([tool_call("list_dir", path=".")])
    monkeypatch.setattr(skippy_agent.agent_tools, "dispatch", explode)

    outcome = await SkippyAgent(
        RecordingSocket(), payload(sample_repo, session_id="s-crash"), StubHub(), session_store=store
    ).run()

    assert outcome.status == "failed"
    assert "boom" in outcome.summary
    saved = store.load_session("shop-jarvis", "s-crash")
    assert saved["status"] == "failed"
    assert saved["ended_at"]


# ---------------------------------------------------------------------------
# Heartbeat routing
# ---------------------------------------------------------------------------

@pytest.fixture
def goals_file(tmp_path, monkeypatch):
    import skippy_factory

    path = tmp_path / "skippy_goals.json"
    monkeypatch.setattr(skippy_factory, "GOALS_FILE", str(path))
    return path


def write_goals(path, tasks):
    path.write_text(json.dumps({"tasks": tasks}, indent=2), encoding="utf-8")


def test_heartbeat_claims_only_project_tasks(goals_file):
    import skippy_factory

    write_goals(
        goals_file,
        [
            {"id": 1, "task": "deburr the fixture plate", "status": "pending"},
            {"id": 2, "task": "add divide()", "status": "pending", "project_id": "shop-jarvis"},
            {"id": 3, "task": "already running", "status": "in_progress", "project_id": "other"},
        ],
    )

    claimed = skippy_factory.claim_pending_project_tasks()

    assert [task["id"] for task in claimed] == [2]
    assert claimed[0]["project_id"] == "shop-jarvis"

    ledger = json.loads(goals_file.read_text(encoding="utf-8"))["tasks"]
    by_id = {task["id"]: task for task in ledger}
    # The shop task is untouched; only the project task was claimed.
    assert by_id[1]["status"] == "pending"
    assert by_id[2]["status"] == "in_progress"
    assert by_id[3]["status"] == "in_progress"


def test_claimed_tasks_are_not_dispatched_twice(goals_file):
    import skippy_factory

    write_goals(goals_file, [{"id": 1, "task": "x", "status": "pending", "project_id": "p"}])

    assert len(skippy_factory.claim_pending_project_tasks()) == 1
    assert skippy_factory.claim_pending_project_tasks() == []


def test_missing_or_corrupt_ledger_is_survivable(goals_file):
    import skippy_factory

    assert skippy_factory.claim_pending_project_tasks() == []
    goals_file.write_text("{not json", encoding="utf-8")
    assert skippy_factory.claim_pending_project_tasks() == []
