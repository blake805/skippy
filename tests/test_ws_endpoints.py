"""The HTTP and websocket surface.

Importing `skippy_factory` here is itself part of the coverage: it must not need
the NAS mounted, Chroma installed, or ~700MB of Whisper and Kokoro weights on
disk. If that regresses, this whole file stops collecting in CI.
"""

import importlib
import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import skippy_factory

    with TestClient(skippy_factory.app) as test_client:
        yield test_client


# --- import weight ---

def test_importing_the_server_loads_no_models_and_touches_no_nas():
    import skippy_factory

    # Both engines are behind accessors; nothing is resolved until first use.
    assert skippy_factory._voice_state == {}
    assert skippy_factory._chroma_state == {}


def test_the_module_imports_without_chroma_whisper_or_kokoro(monkeypatch):
    """Reimport with the heavy packages made unavailable."""
    import builtins

    blocked = {"chromadb", "whisper", "kokoro_onnx", "soundfile", "torch"}
    real_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        if name.split(".")[0] in blocked:
            raise ImportError(f"{name} is blocked for this test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    import skippy_factory

    module = importlib.reload(skippy_factory)
    assert module.app is not None


# --- HTTP ---

def test_ping(client):
    response = client.get("/ping")
    assert response.status_code == 200
    assert "awake" in response.json()["status"]


def test_health_reports_every_role_and_the_cloud_flag(client):
    body = client.get("/health").json()
    assert body["cloud_allowed"] is False
    assert set(body["roles"]) == {"fast", "heavy", "compressor", "voice"}
    for role in body["roles"].values():
        assert role["local"] is True
        assert role["model"]
        assert role["url"]


def test_health_reports_a_missing_workspace_config_instead_of_failing(client, monkeypatch):
    monkeypatch.delenv("SKIPPY_WORKSPACE_ROOTS", raising=False)
    body = client.get("/health").json()
    assert body["workspace_roots"] == []
    assert "SKIPPY_WORKSPACE_ROOTS" in body["workspace_roots_error"]


def test_health_reports_configured_workspace_roots(client, tmp_path, monkeypatch):
    (tmp_path / "a_repo").mkdir()
    monkeypatch.setenv("SKIPPY_WORKSPACE_ROOTS", str(tmp_path / "a_repo"))
    body = client.get("/health").json()
    assert body["workspace_roots"] == [os.path.realpath(str(tmp_path / "a_repo"))]
    assert body["workspace_roots_error"] is None


def test_health_surfaces_an_offmachine_role(client, monkeypatch):
    import skippy_llm

    monkeypatch.setenv("SKIPPY_HEAVY_URL", "https://api.example.com/v1/chat/completions")
    monkeypatch.setenv("SKIPPY_ALLOW_CLOUD", "1")
    skippy_llm.reload_registry()
    try:
        body = client.get("/health").json()
        assert body["cloud_allowed"] is True
        assert body["roles"]["heavy"]["local"] is False
        assert body["roles"]["fast"]["local"] is True
    finally:
        monkeypatch.undo()
        skippy_llm.reload_registry()


# --- websocket ---

def test_a_message_with_no_workspace_configured_says_so_and_names_the_fix(client):
    """The suite configures no roots, so this is the path a fresh install hits. A
    silent empty run would look like the agent simply found nothing to do."""
    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        socket.send_json({"mode": "Agent", "text": "add a feature", "history": [], "use_tts": False})
        first = socket.receive_json()
        second = socket.receive_json()

    assert first["type"] == "chat"
    assert "SKIPPY_WORKSPACE_ROOTS" in first["content"]
    assert second["type"] == "done"


def test_bare_text_is_accepted_as_a_message(client):
    """Older clients send unwrapped strings; that must not kill the connection."""
    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        socket.send_text("not json at all")
        assert socket.receive_json()["type"] == "chat"
        assert socket.receive_json()["type"] == "done"


def test_several_messages_on_one_connection_each_get_an_answer(client):
    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        for _ in range(3):
            socket.send_json({"mode": "Agent", "text": "hello", "history": []})
            assert socket.receive_json()["type"] == "chat"
            assert socket.receive_json()["type"] == "done"


def test_a_task_id_reply_is_routed_and_not_treated_as_a_new_message(client):
    """RPC replies must reach their waiting future, never spawn work."""
    import skippy_factory

    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        socket.send_json({"task_id": "nobody-is-waiting", "result": "x"})
        # Nothing is waiting, so this is dropped: no chat reply comes back.
        socket.send_json({"mode": "Agent", "text": "hello", "history": []})
        assert socket.receive_json()["type"] == "chat"
        assert socket.receive_json()["type"] == "done"

    assert skippy_factory.hub.pending_responses == {}


def test_an_approval_shaped_reply_is_not_treated_as_a_new_message(client):
    """The ADR 0005 bridge: a bare {"status": ...} with no pending gate is dropped."""
    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        socket.send_json({"status": "APPROVE"})
        socket.send_json({"mode": "Agent", "text": "hello", "history": []})
        assert socket.receive_json()["type"] == "chat"
        assert socket.receive_json()["type"] == "done"


def test_status_when_idle_reports_not_running_and_starts_nothing(client):
    """The cockpit polls this on reconnect; it must never be mistaken for a task."""
    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        socket.send_json({"action": "status"})
        reply = socket.receive_json()
        assert reply["type"] == "status"
        assert reply["running"] is False
        # The socket answers the next real message normally: nothing was started.
        socket.send_json({"mode": "Chat", "text": "", "history": []})
        assert socket.receive_json()["type"] == "chat"
        assert socket.receive_json()["type"] == "done"


def test_memory_action_returns_the_project_snapshot(client, tmp_path, monkeypatch):
    """The context rail's data: structured, not the model-facing prose block."""
    import skippy_memory

    repo = tmp_path / "a_repo"
    repo.mkdir()
    monkeypatch.setenv("SKIPPY_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.setenv("SKIPPY_WORKSPACE_ROOTS", str(repo))

    memory = skippy_memory.open_project(workspace_roots=[str(repo)])
    memory.add_decision("Use wsproto", "The websockets backend races its own pings.")
    memory.record_session(
        task="wire the voice lane", status="done", summary="Voice lane is live.",
        files_changed=["skippy_voice.py"], mode="coding",
    )
    memory.learn_convention("test_command", "pytest -q")

    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        socket.send_json({"action": "memory"})
        reply = socket.receive_json()

    assert reply["type"] == "memory"
    assert reply["project_id"] == "a-repo"
    assert reply["conventions"] == {"test_command": "pytest -q"}
    assert [d["title"] for d in reply["decisions"]] == ["Use wsproto"]
    assert reply["decisions"][0]["superseded"] is False
    assert reply["sessions"][0]["summary"] == "Voice lane is live."
    assert reply["sessions"][0]["files_changed"] == ["skippy_voice.py"]


def test_memory_action_with_no_workspace_still_answers(client, tmp_path, monkeypatch):
    """A misconfigured hub shows an empty rail, not a dead socket."""
    monkeypatch.setenv("SKIPPY_MEMORY_ROOT", str(tmp_path / "memory"))
    monkeypatch.delenv("SKIPPY_WORKSPACE_ROOTS", raising=False)

    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        socket.send_json({"action": "memory"})
        reply = socket.receive_json()

    assert reply["type"] == "memory"
    assert reply["project_id"] == "unscoped"
    assert reply["decisions"] == []
    assert reply["sessions"] == []


def test_re_notes_lists_packs_and_then_their_findings(client, tmp_path, monkeypatch):
    """The findings notebook: pack list first, then one pack's findings."""
    import skippy_re

    monkeypatch.setenv("SKIPPY_MEMORY_ROOT", str(tmp_path / "memory"))
    notes = str(tmp_path / "memory" / "notes")
    os.makedirs(notes, exist_ok=True)

    pack = skippy_re.open_pack(notes, target="acme-fob.bin", title="ACME key fob")
    pack.add(
        kind="structure", title="Header is 32 bytes",
        body="The load command at +0x18 gives the first section offset 0x20.",
        evidence="otool -l shows offset 0x20 at +0x18", confidence="confirmed",
        location="+0x18",
    )

    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        socket.send_json({"action": "re_notes"})
        listing = socket.receive_json()
        assert listing["type"] == "re_notes"
        assert len(listing["packs"]) == 1
        pack_id = listing["packs"][0]["pack_id"]
        assert listing["packs"][0]["target"] == "acme-fob.bin"

        socket.send_json({"action": "re_notes", "pack_id": pack_id})
        detail = socket.receive_json()
        assert detail["type"] == "re_notes"
        assert detail["target"] == "acme-fob.bin"
        assert detail["findings"][0]["title"] == "Header is 32 bytes"
        assert detail["findings"][0]["confidence"] == "confirmed"
        assert detail["findings"][0]["superseded"] is False


def test_re_add_finding_writes_a_human_authored_finding(client, tmp_path, monkeypatch):
    monkeypatch.setenv("SKIPPY_MEMORY_ROOT", str(tmp_path / "memory"))

    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        socket.send_json({
            "action": "re_add_finding",
            "target": "acme-fob.bin",
            "kind": "constant",
            "title": "Rolling code seed",
            "body": "The seed is the little-endian u32 at +0x40.",
            "evidence": "xxd +0x40 shows 0x11223344 matching the captured frame",
            "confidence": "likely",
            "location": "+0x40",
        })
        saved = socket.receive_json()
        assert saved["type"] == "re_finding_saved"
        assert saved["ok"] is True

        socket.send_json({"action": "re_notes", "pack_id": saved["pack_id"]})
        detail = socket.receive_json()
        assert detail["findings"][0]["title"] == "Rolling code seed"
        assert detail["findings"][0]["kind"] == "constant"


def test_re_add_finding_still_refuses_evidence_free_assertions(client, tmp_path, monkeypatch):
    """The dashboard is a human, but an unrecheckable finding is worthless anyway."""
    monkeypatch.setenv("SKIPPY_MEMORY_ROOT", str(tmp_path / "memory"))

    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        socket.send_json({
            "action": "re_add_finding",
            "target": "acme-fob.bin",
            "kind": "structure",
            "title": "Something I did not verify",
            "body": "It is definitely like this.",
            "confidence": "confirmed",
        })
        reply = socket.receive_json()
        assert reply["type"] == "re_finding_saved"
        assert "evidence" in reply["error"].lower()


def test_re_devices_answers_even_with_no_hardware(client):
    """The device panel: studio enumeration must return a list, empty is fine."""
    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        socket.send_json({"action": "re_devices", "host": "studio"})
        reply = socket.receive_json()
    assert reply["type"] == "re_devices"
    assert isinstance(reply["devices"], list)


def _seed_repo(path):
    import subprocess

    path.mkdir(parents=True, exist_ok=True)
    def run(*args):
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=t@example.com", *args],
            cwd=path, capture_output=True, check=True,
        )
    run("init", "-q", "-b", "main")
    run("config", "user.name", "Test")
    run("config", "user.email", "t@example.com")
    (path / "app.py").write_text("x = 1\n")
    run("add", "-A")
    run("commit", "-q", "-m", "initial")
    return path


def test_git_action_lists_repos_and_then_one_repo_in_full(client, tmp_path, monkeypatch):
    """The repo panel's data: headline list first, then branch/changes/diffs."""
    repo = _seed_repo(tmp_path / "proj")
    (repo / "app.py").write_text("x = 2\n")
    monkeypatch.setenv("SKIPPY_WORKSPACE_ROOTS", str(repo))

    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        socket.send_json({"action": "git"})
        listing = socket.receive_json()
        assert listing["type"] == "git"
        assert len(listing["repos"]) == 1
        assert listing["repos"][0]["name"] == "proj"
        assert listing["repos"][0]["branch"] == "main"
        assert listing["repos"][0]["changes"] == 1

        socket.send_json({"action": "git", "repo": "proj"})
        detail = socket.receive_json()
        assert detail["type"] == "git"
        assert detail["branch"] == "main"
        assert "main" in detail["branches"]
        assert detail["changes"][0]["path"] == "app.py"
        assert "+x = 2" in detail["diff"]
        assert detail["last_commit"]["subject"] == "initial"


def test_git_action_with_no_roots_answers_with_a_reason(client, monkeypatch):
    """A misconfigured hub shows an empty panel, not a dead socket."""
    monkeypatch.delenv("SKIPPY_WORKSPACE_ROOTS", raising=False)
    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        socket.send_json({"action": "git"})
        reply = socket.receive_json()
    assert reply["type"] == "git"
    assert "workspace root" in reply["error"].lower()


def test_git_commit_action_commits_from_the_panel_without_a_card(client, tmp_path, monkeypatch):
    """A human-clicked commit is its own approval; it must not wait on a card."""
    repo = _seed_repo(tmp_path / "proj")
    (repo / "app.py").write_text("x = 3\n")
    monkeypatch.setenv("SKIPPY_WORKSPACE_ROOTS", str(repo))

    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        socket.send_json({
            "action": "git_commit", "repo": "proj", "message": "bump x from the panel",
        })
        reply = socket.receive_json()
        assert reply["type"] == "git_result"
        assert reply["ok"] is True
        assert reply["commit"]

        socket.send_json({"action": "git", "repo": "proj"})
        detail = socket.receive_json()
        assert detail["changes"] == []
        assert detail["last_commit"]["subject"] == "bump x from the panel"


def test_git_commit_action_with_nothing_staged_reports_the_error(client, tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path / "proj")
    monkeypatch.setenv("SKIPPY_WORKSPACE_ROOTS", str(repo))
    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        socket.send_json({"action": "git_commit", "repo": "proj", "message": "empty"})
        reply = socket.receive_json()
    assert reply["type"] == "git_result"
    assert "nothing to commit" in reply["error"].lower()


def test_git_branch_action_creates_and_reports_the_new_branch(client, tmp_path, monkeypatch):
    repo = _seed_repo(tmp_path / "proj")
    monkeypatch.setenv("SKIPPY_WORKSPACE_ROOTS", str(repo))
    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        socket.send_json({
            "action": "git_branch", "repo": "proj", "name": "feature/panel", "create": True,
        })
        reply = socket.receive_json()
        assert reply["type"] == "git_result"
        assert reply["ok"] is True

        socket.send_json({"action": "git", "repo": "proj"})
        detail = socket.receive_json()
        assert detail["branch"] == "feature/panel"


def test_disconnect_deregisters_the_client(client):
    import skippy_factory

    with client.websocket_connect("/ws/factory?client_id=goodbye"):
        assert "goodbye" in skippy_factory.hub.active_connections
    assert "goodbye" not in skippy_factory.hub.active_connections


# --- where the hub listens ---
#
# There is no authentication on /ws/factory: client_id is a query parameter, and any
# message that is not a reply, a greeting or a cancel starts an agent run. So the bind
# address is the only thing between the local network and an agent that can edit the
# workspace roots and run commands. ADR 0014 accepted the missing authentication on the
# stated grounds that the bind was loopback; it was 0.0.0.0, and the SkippyServer boot
# line runs `python skippy_factory.py`, which took that default.

def test_the_default_bind_is_loopback(monkeypatch):
    import skippy_factory

    monkeypatch.delenv("SKIPPY_BIND_HOST", raising=False)
    assert skippy_factory.bind_host() == "127.0.0.1"


def test_a_deliberate_override_is_honoured(monkeypatch):
    """Remote access is a real requirement. It just has to be asked for."""
    import skippy_factory

    monkeypatch.setenv("SKIPPY_BIND_HOST", "100.64.1.2")
    assert skippy_factory.bind_host() == "100.64.1.2"


def test_binding_beyond_loopback_says_so_loudly(monkeypatch, caplog):
    """Silently exposing an unauthenticated agent is the failure being prevented."""
    import logging

    import skippy_factory

    monkeypatch.setenv("SKIPPY_BIND_HOST", "0.0.0.0")
    with caplog.at_level(logging.WARNING):
        assert skippy_factory.bind_host() == "0.0.0.0"
    logged = " ".join(record.getMessage() for record in caplog.records).lower()
    assert "no authentication" in logged


def test_loopback_does_not_warn(monkeypatch, caplog):
    import logging

    import skippy_factory

    monkeypatch.setenv("SKIPPY_BIND_HOST", "127.0.0.1")
    with caplog.at_level(logging.WARNING):
        skippy_factory.bind_host()
    assert not [r for r in caplog.records if "no authentication" in r.getMessage()]


def test_an_empty_override_falls_back_to_loopback(monkeypatch):
    """An env var that is present but blank must not become a bind to everything."""
    import skippy_factory

    monkeypatch.setenv("SKIPPY_BIND_HOST", "   ")
    assert skippy_factory.bind_host() == "127.0.0.1"
