"""The HTTP and websocket surface.

Importing `skippy_factory` here is itself part of the coverage: it must not need
the NAS mounted, Chroma installed, or ~700MB of Whisper and Kokoro weights on
disk. If that regresses, this whole file stops collecting in CI.
"""

import importlib

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
    assert set(body["roles"]) == {"fast", "heavy", "compressor"}
    for role in body["roles"].values():
        assert role["local"] is True
        assert role["model"]
        assert role["url"]


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

def test_a_chat_message_gets_an_answer_and_a_done(client):
    with client.websocket_connect("/ws/factory?client_id=test") as socket:
        socket.send_json({"mode": "Agent", "text": "add a feature", "history": [], "use_tts": False})
        first = socket.receive_json()
        second = socket.receive_json()

    assert first["type"] == "chat"
    # Until the agent runtime lands, the honest answer is that it is missing.
    assert "not installed" in first["content"]
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


def test_disconnect_deregisters_the_client(client):
    import skippy_factory

    with client.websocket_connect("/ws/factory?client_id=goodbye"):
        assert "goodbye" in skippy_factory.hub.active_connections
    assert "goodbye" not in skippy_factory.hub.active_connections
