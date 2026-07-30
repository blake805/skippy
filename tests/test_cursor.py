"""The Cursor bridge, and the parity that keeps two patch implementations honest.

The editor exists to give the agent two things the filesystem cannot: live diagnostics,
and edits the user can undo. Both introduce the same risk — a second implementation of
patch semantics — so the first half of this file checks that the server and the editor
agree on a shared table of cases, and the second half checks the routing: that the
editor is used when it is there, that its absence changes nothing, and that a refusal is
never quietly turned into a write the user did not want.
"""

import asyncio
import json
import os

import pytest

import skippy_cursor
import skippy_edit
from skippy_cursor import CursorBridge, format_diagnostics, new_diagnostics
from skippy_sandbox import Sandbox

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "patch_parity.json")
with open(FIXTURE, encoding="utf-8") as handle:
    PARITY_CASES = json.load(handle)["cases"]


# ---------------------------------------------------------------------------
# Parity with the editor implementation
# ---------------------------------------------------------------------------

def test_the_parity_table_is_populated():
    """A silently empty table would make every parity test vacuously pass, which is
    the one way a check like this fails without anyone noticing."""
    assert len(PARITY_CASES) >= 20


@pytest.mark.parametrize("case", PARITY_CASES, ids=lambda c: c["name"])
def test_the_server_agrees_with_the_editor(case, tmp_path):
    """The same table runs through cursor_client/test/parity.test.js.

    If these two disagree, the model gets different answers depending on whether Cursor
    happens to be attached — a bug that comes and goes with the state of the editor.
    """
    root = tmp_path / "workspace"
    root.mkdir()
    for relative, content in case["files"].items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))

    result = skippy_edit.apply_patch(Sandbox([str(root)]), case["edits"])
    assert result.ok is case["expect"]["ok"], f"{case['name']}: {result.summary}"

    for relative, expected in (case["expect"].get("files") or {}).items():
        target = root / relative
        if expected is None:
            assert not target.exists(), f"{relative} should be gone"
        else:
            assert target.read_bytes().decode("utf-8") == expected, f"{relative} content"


# ---------------------------------------------------------------------------
# The bridge
# ---------------------------------------------------------------------------

class FakeHub:
    """Stands in for the ConnectionManager: records RPCs and answers them."""

    def __init__(self, connected=True, reply=None, error=None):
        self.active_connections = {"cursor": object()} if connected else {}
        self.calls = []
        self.reply = reply
        self.error = error

    async def execute_tool_on_client(self, target, payload, timeout=10.0):
        self.calls.append((target, payload, timeout))
        if self.error:
            return {"error": self.error}
        if callable(self.reply):
            return self.reply(payload)
        return self.reply if self.reply is not None else {"ok": True, "result": {}}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "calc").mkdir(parents=True)
    (root / "calc" / "ops.py").write_text("def add(a, b):\n    return a + b\n")
    return root


@pytest.fixture
def box(repo):
    return Sandbox([str(repo)])


def editor_that_writes(root):
    """An editor that actually performs the write, the way the real one does."""

    def reply(payload):
        if payload.get("action") == "get_diagnostics":
            # Nothing wrong before the patch, so anything after it is attributable.
            return {"ok": True, "result": {"diagnostics": []}}
        if payload.get("action") != "apply_patches":
            return {"ok": True, "result": {}}
        applied = []
        for edit in payload["edits"]:
            path = edit["path"]
            if edit.get("action") == "delete":
                os.remove(path)
            else:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(edit["content"])
            applied.append(path)
        return {"ok": True, "result": {"applied": applied, "failed": [], "diagnostics": []}}

    return reply


# --- connection state ---

def test_the_bridge_knows_when_the_editor_is_absent():
    assert CursorBridge(FakeHub(connected=False)).connected is False
    assert CursorBridge(FakeHub(connected=True)).connected is True


@pytest.mark.asyncio
async def test_a_call_with_no_editor_fails_without_reaching_the_hub():
    hub = FakeHub(connected=False)
    result = await CursorBridge(hub).call("get_open_files")
    assert not result["ok"]
    assert "not connected" in result["error"]
    assert hub.calls == []


@pytest.mark.asyncio
async def test_each_action_gets_its_own_timeout():
    """One global timeout would have failed every interesting call: a workspace edit
    legitimately takes far longer than a query for the open file list."""
    hub = FakeHub()
    bridge = CursorBridge(hub)
    await bridge.call("get_open_files")
    await bridge.call("apply_patches", {"edits": []})

    timeouts = {payload["action"]: timeout for _, payload, timeout in hub.calls}
    assert timeouts["apply_patches"] > timeouts["get_open_files"]


@pytest.mark.asyncio
async def test_a_transport_error_is_reported_not_raised():
    result = await CursorBridge(FakeHub(error="Timeout: no answer")).call("get_open_files")
    assert not result["ok"]
    assert "Timeout" in result["error"]


@pytest.mark.asyncio
async def test_a_malformed_reply_is_reported():
    result = await CursorBridge(FakeHub(reply="not a dict")).call("get_open_files")
    assert not result["ok"]
    assert "Malformed" in result["error"]


@pytest.mark.asyncio
async def test_a_flat_reply_is_tolerated():
    """An extension that answers without a 'result' envelope still works."""
    hub = FakeHub(reply={"ok": True, "task_id": "x", "files": [{"path": "/a.py"}]})
    result = await CursorBridge(hub).call("get_open_files")
    assert result["ok"]
    assert result["result"]["files"] == [{"path": "/a.py"}]


@pytest.mark.asyncio
async def test_workspace_roots_come_back_as_plain_paths():
    hub = FakeHub(reply={"ok": True, "result": {"roots": [
        {"name": "repo", "path": "/work/repo"},
        {"name": "broken"},
        "/work/other",
    ]}})
    assert await CursorBridge(hub).workspace_roots() == ["/work/repo", "/work/other"]


@pytest.mark.asyncio
async def test_workspace_roots_are_empty_rather_than_an_error_when_absent():
    assert await CursorBridge(FakeHub(connected=False)).workspace_roots() == []


# --- diagnostics rendering ---

def test_diagnostics_render_for_a_model_to_act_on():
    text = format_diagnostics({"diagnostics": [
        {"path": "/a.py", "line": 12, "col": 5, "severity": "error",
         "message": "undefined name 'foo'", "source": "pyright"},
    ]})
    assert "error: /a.py:12:5 undefined name 'foo'  [pyright]" == text


def test_no_diagnostics_renders_as_nothing():
    assert format_diagnostics({"diagnostics": []}) == ""
    assert format_diagnostics(None) == ""
    assert format_diagnostics({}) == ""


def test_a_flood_of_diagnostics_is_capped_and_says_so():
    entries = [
        {"path": f"/f{n}.py", "line": n, "severity": "warning", "message": "x"}
        for n in range(500)
    ]
    text = format_diagnostics(entries)
    assert "more]" in text
    assert len(text) < skippy_cursor.MAX_DIAGNOSTIC_CHARS + 200


def test_a_diagnostic_missing_fields_still_renders():
    assert "?" in format_diagnostics([{"message": "something"}])


# --- routing ---

@pytest.mark.asyncio
async def test_with_no_editor_the_patch_is_written_directly(box, repo):
    result = await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 0"}],
        bridge=CursorBridge(FakeHub(connected=False)),
    )
    assert result.ok
    assert "a + b + 0" in (repo / "calc" / "ops.py").read_text()
    assert not result.data.get("applied_in_editor")


@pytest.mark.asyncio
async def test_with_no_bridge_at_all_the_patch_is_written_directly(box, repo):
    """The agent must work identically with or without an editor attached."""
    result = await skippy_cursor.apply_patch(
        box, [{"path": "calc/ops.py", "search": "a + b", "replace": "a - b"}], bridge=None
    )
    assert result.ok
    assert "a - b" in (repo / "calc" / "ops.py").read_text()


@pytest.mark.asyncio
async def test_with_an_editor_the_edit_goes_through_it(box, repo):
    hub = FakeHub(reply=editor_that_writes(repo))
    result = await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "a + b", "replace": "a * b"}],
        bridge=CursorBridge(hub),
    )
    assert result.ok
    assert result.data["applied_in_editor"] is True
    assert "a * b" in (repo / "calc" / "ops.py").read_text()
    assert [p["action"] for _, p, _ in hub.calls] == ["get_diagnostics", "apply_patches"]


@pytest.mark.asyncio
async def test_the_editor_is_sent_final_content_not_a_search_to_rerun(box, repo):
    """The server has already staged the exact text. Re-deriving the edit in the editor
    is what would let the two implementations disagree about a patch they have both
    already accepted."""
    hub = FakeHub(reply=editor_that_writes(repo))
    await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "a + b", "replace": "a * b"}],
        bridge=CursorBridge(hub),
    )
    edits = next(p["edits"] for _, p, _ in hub.calls if p["action"] == "apply_patches")
    assert "search" not in edits[0]
    assert "a * b" in edits[0]["content"]


@pytest.mark.asyncio
async def test_the_editor_is_never_handed_a_path_outside_the_workspace(box):
    """Validation happens on the server first, so an escaping path is refused before
    the editor could be asked to write it."""
    hub = FakeHub(reply=editor_that_writes("/"))
    result = await skippy_cursor.apply_patch(
        box,
        [{"path": "../../etc/passwd", "action": "create", "content": "x"}],
        bridge=CursorBridge(hub),
    )
    assert not result.ok
    assert hub.calls == []


@pytest.mark.asyncio
async def test_an_invalid_patch_is_refused_before_the_editor_is_involved(box):
    hub = FakeHub(reply=editor_that_writes("/tmp"))
    result = await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "text that is not there", "replace": "x"}],
        bridge=CursorBridge(hub),
    )
    assert not result.ok
    assert hub.calls == []


@pytest.mark.asyncio
async def test_a_dry_run_never_reaches_the_editor(box, repo):
    hub = FakeHub(reply=editor_that_writes(repo))
    result = await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "a + b", "replace": "a % b"}],
        bridge=CursorBridge(hub),
        dry_run=True,
    )
    assert result.ok
    assert result.data["dry_run"] is True
    assert hub.calls == []
    assert "a % b" not in (repo / "calc" / "ops.py").read_text()


# --- when the editor cannot or will not ---

@pytest.mark.asyncio
async def test_an_editor_failure_falls_back_to_writing_directly(box, repo):
    """Better than failing the task. But it costs the user their single undo step, so
    the result has to say it happened."""
    hub = FakeHub(reply={"ok": False, "error": "extension is wedged"})
    result = await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "a + b", "replace": "a ** b"}],
        bridge=CursorBridge(hub),
    )
    assert result.ok
    assert "a ** b" in (repo / "calc" / "ops.py").read_text()
    assert "written to disk directly" in result.summary


@pytest.mark.asyncio
async def test_a_declined_edit_is_not_written_behind_the_users_back(box, repo):
    """The one case where falling back would be exactly wrong: the user said no."""
    hub = FakeHub(reply={"ok": True, "result": {
        "applied": [],
        "failed": [{"index": 0, "path": str(repo / "calc" / "ops.py"), "reason": "user declined the edit"}],
    }})
    before = (repo / "calc" / "ops.py").read_text()
    result = await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "a + b", "replace": "a / b"}],
        bridge=CursorBridge(hub),
    )
    assert not result.ok
    assert result.data.get("declined") is True
    assert (repo / "calc" / "ops.py").read_text() == before


@pytest.mark.asyncio
async def test_an_editor_timeout_still_gets_the_change_written(box, repo):
    hub = FakeHub(error="Timeout: 'cursor' did not respond within 120 seconds.")
    result = await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "a + b", "replace": "a // b"}],
        bridge=CursorBridge(hub),
    )
    assert result.ok
    assert "a // b" in (repo / "calc" / "ops.py").read_text()


# --- diagnostics fed back with the patch ---

@pytest.mark.asyncio
async def test_diagnostics_come_back_attached_to_the_patch_result(box, repo):
    """Not left to a follow-up call. The agent needs to know what its own edit broke,
    and a separate round trip is one the model has to remember to make."""
    def reply(payload):
        result = editor_that_writes(repo)(payload)
        if payload["action"] == "apply_patches":
            result["result"]["diagnostics"] = [
                {"path": str(repo / "calc" / "ops.py"), "line": 2, "col": 12,
                 "severity": "error", "message": "undefined name 'b'", "source": "pyright"},
            ]
        return result

    result = await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "a + b", "replace": "a + c"}],
        bridge=CursorBridge(FakeHub(reply=reply)),
    )
    assert result.ok
    assert "undefined name 'b'" in result.content
    assert "introduced 1 diagnostic" in result.summary


@pytest.mark.asyncio
async def test_a_clean_patch_says_there_were_no_diagnostics(box, repo):
    """Silence is ambiguous: it could mean clean or it could mean nobody looked."""
    result = await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 1"}],
        bridge=CursorBridge(FakeHub(reply=editor_that_writes(repo))),
    )
    assert result.ok
    assert "no diagnostics" in result.summary.lower()


@pytest.mark.asyncio
async def test_the_diagnostics_request_asks_the_editor_to_wait_for_them():
    """Diagnostics are produced asynchronously, so reading them the instant an edit
    lands returns the state from before it."""
    hub = FakeHub()
    await CursorBridge(hub).diagnostics(["/a.py"], settle=True)
    assert hub.calls[0][1]["settle"] is True


# --- the journal survives routing ---

@pytest.mark.asyncio
async def test_the_patch_journal_is_written_even_when_the_editor_applies(box, repo, tmp_path):
    """The editor's undo stack lasts as long as the window. The journal is what covers
    a crash, so routing must not quietly drop it."""
    journal = tmp_path / "journal"
    result = await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "a + b", "replace": "a | b"}],
        bridge=CursorBridge(FakeHub(reply=editor_that_writes(repo))),
        journal_dir=str(journal),
    )
    assert result.ok
    assert result.data.get("journal")
    assert list(journal.iterdir()), "the journal directory should have an entry"


# --- the loop wiring ---

@pytest.mark.asyncio
async def test_the_dispatcher_will_not_take_a_bridge_from_the_model(box, repo):
    """Same rule as the sandbox: a model that could supply its own bridge could send
    edits to something other than the user's editor."""
    import skippy_dispatch

    hub = FakeHub(reply=editor_that_writes(repo))
    result = await skippy_dispatch.dispatch(
        "apply_patch",
        {
            "edits": [{"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 2"}],
            "bridge": "something else",
            "writer": "also not allowed",
        },
        box,
        cursor=CursorBridge(hub),
    )
    assert result.ok
    assert result.data["applied_in_editor"] is True


@pytest.mark.asyncio
async def test_the_loop_passes_the_editor_through_to_the_patch_tool(box, repo, routed_llm):
    import skippy_agent
    from tests import fake_llm as fl

    hub = FakeHub(reply=editor_that_writes(repo))
    routed_llm.load([
        fl.tool_call("apply_patch", call_id="c1", edits=[
            {"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 3"}
        ]),
        fl.tool_call("finish", call_id="c2", summary="done"),
    ])
    outcome = await skippy_agent.run_task(
        "Change the sum", box, cursor=CursorBridge(hub), remember=False
    )
    assert outcome.status == "finished"
    assert "a + b + 3" in (repo / "calc" / "ops.py").read_text()
    assert any(payload["action"] == "apply_patches" for _, payload, _ in hub.calls)


@pytest.mark.asyncio
async def test_the_task_runner_offers_the_editor_to_every_run(repo):
    import skippy_tasks

    class Hub:
        active_connections = {}

    runner = skippy_tasks.TaskRunner(Hub(), roots_provider=lambda: [str(repo)])
    assert isinstance(runner.cursor, CursorBridge)
    # Resolved per call, so attaching Cursor mid-session simply starts working.
    assert runner.cursor.connected is False
    Hub.active_connections["cursor"] = object()
    assert runner.cursor.connected is True


# --- the writer adapter ---

@pytest.mark.asyncio
async def test_the_editor_write_happens_on_the_event_loop_not_the_worker_thread(box, repo):
    """The websocket belongs to the event loop. Touching it from the thread that runs
    apply_patch is the race the hub warns about elsewhere."""
    loops = []

    def reply(payload):
        loops.append(asyncio.get_event_loop())
        return editor_that_writes(repo)(payload)

    running = asyncio.get_running_loop()
    result = await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 4"}],
        bridge=CursorBridge(FakeHub(reply=reply)),
    )
    assert result.ok
    assert loops and loops[0] is running


@pytest.mark.asyncio
async def test_a_decline_is_recognised_without_reading_it_out_of_an_error_string(box, repo):
    """The decline is signalled structurally. Recognising it by searching the failure
    message would mean any change to the extension's wording silently turns a refusal
    into a write of the change the user had just refused — the one outcome the fallback
    must never produce."""
    hub = FakeHub(reply={"ok": True, "result": {
        "applied": [],
        # Deliberately not the wording the extension actually uses.
        "failed": [{"index": 0, "path": "x", "reason": "DECLINED by the human at the keyboard"}],
    }})
    before = (repo / "calc" / "ops.py").read_text()
    result = await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "a + b", "replace": "a ^ b"}],
        bridge=CursorBridge(hub),
    )
    assert not result.ok
    assert result.data.get("declined") is True
    assert (repo / "calc" / "ops.py").read_text() == before


@pytest.mark.asyncio
async def test_a_rejection_that_is_not_a_decline_still_falls_back(box, repo):
    """An editor that cannot do it is different from a user who will not have it."""
    hub = FakeHub(reply={"ok": True, "result": {
        "applied": [],
        "failed": [{"index": 0, "path": "x", "reason": "editor rejected the workspace edit"}],
    }})
    result = await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "a + b", "replace": "a ^ b"}],
        bridge=CursorBridge(hub),
    )
    assert result.ok
    assert "a ^ b" in (repo / "calc" / "ops.py").read_text()


# --- attribution: which diagnostics this change actually caused ---

def test_a_pre_existing_diagnostic_is_not_blamed_on_the_change():
    """The live failure this exists for: the agent was handed every diagnostic for a
    file it touched, could not tell which its own edit caused, tried to fix one that was
    there all along, patched again, and burned five steps before the repetition guard
    stopped it."""
    old = {"path": "/a.py", "line": 3, "severity": "warning", "message": "unused import",
           "source": "ruff"}
    assert new_diagnostics([old], [old]) == []


def test_a_diagnostic_the_change_introduced_is_reported():
    old = {"path": "/a.py", "line": 3, "severity": "warning", "message": "unused import"}
    new = {"path": "/a.py", "line": 9, "severity": "error", "message": "undefined name 'x'"}
    assert new_diagnostics([old], [old, new]) == [new]


def test_a_diagnostic_that_moved_is_not_reported_as_new():
    """Inserting lines shifts everything below. Keying on position would blame the patch
    for every diagnostic it pushed down the file."""
    before = [{"path": "/a.py", "line": 3, "severity": "error", "message": "boom"}]
    after = [{"path": "/a.py", "line": 41, "severity": "error", "message": "boom"}]
    assert new_diagnostics(before, after) == []


def test_a_second_instance_of_an_existing_problem_is_reported():
    """Counted rather than set-subtracted, so duplicating a mistake still shows up."""
    entry = {"path": "/a.py", "line": 3, "severity": "error", "message": "boom"}
    assert len(new_diagnostics([entry], [entry, dict(entry, line=8)])) == 1


def test_a_fixed_diagnostic_does_not_appear_as_new():
    old = [{"path": "/a.py", "line": 3, "severity": "error", "message": "boom"}]
    assert new_diagnostics(old, []) == []


def test_diagnostics_from_different_sources_are_distinct():
    base = {"path": "/a.py", "line": 3, "severity": "error", "message": "boom"}
    before = [dict(base, source="ruff")]
    after = [dict(base, source="ruff"), dict(base, source="pyright")]
    assert new_diagnostics(before, after) == [dict(base, source="pyright")]


@pytest.mark.asyncio
async def test_only_the_new_diagnostics_reach_the_agent(box, repo):
    target = str(repo / "calc" / "ops.py")
    stale = {"path": target, "line": 1, "severity": "warning",
             "message": "this was already here", "source": "ruff"}
    fresh = {"path": target, "line": 2, "severity": "error",
             "message": "this one is your fault", "source": "ruff"}

    def reply(payload):
        if payload["action"] == "get_diagnostics":
            return {"ok": True, "result": {"diagnostics": [stale]}}
        result = editor_that_writes(repo)(payload)
        result["result"]["diagnostics"] = [stale, fresh]
        return result

    result = await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 5"}],
        bridge=CursorBridge(FakeHub(reply=reply)),
    )
    assert result.ok
    assert "this one is your fault" in result.content
    assert "this was already here" not in result.content
    assert "introduced 1 diagnostic" in result.summary


@pytest.mark.asyncio
async def test_pre_existing_problems_are_counted_so_the_agent_stops_looking(box, repo):
    """Silence about them would leave the agent unsure whether anything was checked."""
    target = str(repo / "calc" / "ops.py")
    stale = {"path": target, "line": 1, "severity": "warning", "message": "old news"}

    def reply(payload):
        if payload["action"] == "get_diagnostics":
            return {"ok": True, "result": {"diagnostics": [stale]}}
        result = editor_that_writes(repo)(payload)
        result["result"]["diagnostics"] = [stale]
        return result

    result = await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 6"}],
        bridge=CursorBridge(FakeHub(reply=reply)),
    )
    assert result.ok
    assert "No new diagnostics" in result.summary
    assert "1 pre-existing" in result.summary


@pytest.mark.asyncio
async def test_the_before_snapshot_does_not_wait_for_analysis(box, repo):
    """It is the state the editor already holds; waiting would only delay the patch."""
    settles = []

    def reply(payload):
        if payload["action"] == "get_diagnostics":
            settles.append(payload.get("settle"))
            return {"ok": True, "result": {"diagnostics": []}}
        return editor_that_writes(repo)(payload)

    await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 7"}],
        bridge=CursorBridge(FakeHub(reply=reply)),
    )
    assert settles == [False]


@pytest.mark.asyncio
async def test_losing_the_before_snapshot_does_not_cost_the_patch(box, repo):
    """Attribution is a nicety. The edit is the point."""
    def reply(payload):
        if payload["action"] == "get_diagnostics":
            return {"error": "editor is busy"}
        return editor_that_writes(repo)(payload)

    result = await skippy_cursor.apply_patch(
        box,
        [{"path": "calc/ops.py", "search": "a + b", "replace": "a + b + 8"}],
        bridge=CursorBridge(FakeHub(reply=reply)),
    )
    assert result.ok
    assert "a + b + 8" in (repo / "calc" / "ops.py").read_text()
