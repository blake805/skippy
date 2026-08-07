"""Tests for the model role registry, cloud policy, and append-only transcript."""

import json

import httpx
import pytest

import skippy_llm


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    """Every test starts from documented defaults, not the developer's shell."""
    for key in list(skippy_llm.os.environ):
        if key.startswith("SKIPPY_"):
            monkeypatch.delenv(key, raising=False)
    skippy_llm.reload_registry()
    yield
    skippy_llm.reload_registry()


def mock_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def reply(content="", tool_calls=None, status=200):
    message = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls

    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(status, text="upstream exploded")
        return httpx.Response(200, json={"choices": [{"message": message}]})

    return handler


# --- registry resolution ---

def test_default_roles_are_local_and_match_cached_weights():
    assert set(skippy_llm.MODELS) == {"fast", "heavy", "compressor", "voice"}
    for role in ("fast", "heavy", "compressor", "voice"):
        assert skippy_llm.endpoint(role).is_local

    assert "480B" in skippy_llm.endpoint("heavy").model
    assert "30B-A3B" in skippy_llm.endpoint("fast").model
    # The voice role is chat-tuned, not the coder weights: the coder model
    # answering out loud was stilted and loop-prone.
    assert "Instruct-2507" in skippy_llm.endpoint("voice").model
    assert "Coder" not in skippy_llm.endpoint("voice").model


def test_compressor_shares_the_fast_server_but_not_the_32k_model():
    fast, comp = skippy_llm.endpoint("fast"), skippy_llm.endpoint("compressor")
    assert comp.url == fast.url
    # Qwen2.5-Coder-32B held this role and caps at 32K context, which is smaller
    # than a single objdump region. Regressing to it would break RE compression.
    assert "Qwen2.5" not in comp.model


def test_unknown_role_names_the_known_ones():
    with pytest.raises(skippy_llm.ModelError) as exc:
        skippy_llm.endpoint("enormous")
    assert "compressor" in str(exc.value)


def test_env_overrides_url_model_and_max_tokens(monkeypatch):
    monkeypatch.setenv("SKIPPY_HEAVY_URL", "http://127.0.0.1:9999/v1/chat/completions")
    monkeypatch.setenv("SKIPPY_HEAVY_MODEL", "some/other-model")
    monkeypatch.setenv("SKIPPY_HEAVY_MAX_TOKENS", "222")
    skippy_llm.reload_registry()

    heavy = skippy_llm.endpoint("heavy")
    assert heavy.url.endswith(":9999/v1/chat/completions")
    assert heavy.model == "some/other-model"
    assert heavy.max_tokens == 222


def test_garbage_max_tokens_falls_back_instead_of_crashing(monkeypatch):
    monkeypatch.setenv("SKIPPY_FAST_MAX_TOKENS", "lots")
    skippy_llm.reload_registry()
    assert skippy_llm.endpoint("fast").max_tokens == 4096


# --- cloud policy (ADR 0007) ---

def test_offmachine_role_is_refused_by_default(monkeypatch):
    monkeypatch.setenv("SKIPPY_HEAVY_URL", "https://api.openai.com/v1/chat/completions")
    skippy_llm.reload_registry()

    with pytest.raises(skippy_llm.CloudNotAllowed) as exc:
        skippy_llm.endpoint("heavy")
    assert "SKIPPY_ALLOW_CLOUD" in str(exc.value)


def test_offmachine_role_resolves_once_cloud_is_allowed(monkeypatch):
    monkeypatch.setenv("SKIPPY_HEAVY_URL", "https://api.openai.com/v1/chat/completions")
    monkeypatch.setenv("SKIPPY_ALLOW_CLOUD", "1")
    skippy_llm.reload_registry()
    assert skippy_llm.endpoint("heavy").url.startswith("https://api.openai.com")


def test_cloud_use_is_always_logged(monkeypatch, caplog):
    monkeypatch.setenv("SKIPPY_HEAVY_URL", "https://api.anthropic.com/v1/chat/completions")
    monkeypatch.setenv("SKIPPY_ALLOW_CLOUD", "1")
    skippy_llm.reload_registry()

    with caplog.at_level("WARNING", logger="skippy_llm"):
        skippy_llm.endpoint("heavy")
    assert any("CLOUD" in r.message for r in caplog.records)


def test_lan_and_tailnet_addresses_count_as_offmachine(monkeypatch):
    # Deliberately conservative: the guarantee worth having is "did not leave this
    # machine", and a tailnet peer is still another machine.
    monkeypatch.setenv("SKIPPY_FAST_URL", "http://100.101.102.103:8080/v1/chat/completions")
    skippy_llm.reload_registry()
    with pytest.raises(skippy_llm.CloudNotAllowed):
        skippy_llm.endpoint("fast")


def test_api_key_is_sent_as_bearer_when_configured(monkeypatch):
    monkeypatch.setenv("SKIPPY_FAST_API_KEY", "sk-secret")
    skippy_llm.reload_registry()
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    async def run():
        async with mock_client(handler) as client:
            return await skippy_llm.query_text([{"role": "user", "content": "x"}], client=client)

    import asyncio
    assert asyncio.run(run()) == "hi"
    assert seen["auth"] == "Bearer sk-secret"


def test_no_auth_header_when_no_key_configured():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})

    async def run():
        async with mock_client(handler) as client:
            await skippy_llm.query_text([{"role": "user", "content": "x"}], client=client)

    import asyncio
    asyncio.run(run())
    assert seen["auth"] is None


# --- query_message ---

async def test_tool_call_arguments_are_parsed_into_a_dict():
    calls = [{"id": "c1", "function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}]
    async with mock_client(reply(tool_calls=calls)) as client:
        message = await skippy_llm.query_message(
            [{"role": "user", "content": "x"}], tools=[{"x": 1}], client=client
        )
    assert message["tool_calls"][0]["name"] == "read_file"
    assert message["tool_calls"][0]["arguments"] == {"path": "a.py"}


async def test_malformed_tool_arguments_are_surfaced_not_swallowed():
    calls = [{"id": "c1", "function": {"name": "read_file", "arguments": "{not json"}}]
    async with mock_client(reply(tool_calls=calls)) as client:
        message = await skippy_llm.query_message(
            [{"role": "user", "content": "x"}], tools=[{"x": 1}], client=client
        )
    assert message["tool_calls"][0]["arguments"] == {"_malformed_arguments": "{not json"}


async def test_leaked_xml_tool_calls_are_recovered():
    leaked = 'sure thing<function=read_file><parameter=path>a.py</parameter></function>'
    async with mock_client(reply(content=leaked)) as client:
        message = await skippy_llm.query_message(
            [{"role": "user", "content": "x"}], tools=[{"x": 1}], client=client
        )
    assert message["tool_calls"][0]["name"] == "read_file"
    assert message["tool_calls"][0]["arguments"] == {"path": "a.py"}
    assert message["content"] == "sure thing"


async def test_a_dead_endpoint_raises_instead_of_returning_prose():
    # The predecessor returned "System Error: Failed to connect..." as content,
    # which an agent loop cannot distinguish from something the model said.
    async with mock_client(reply(status=500)) as client:
        with pytest.raises(skippy_llm.ModelError) as exc:
            await skippy_llm.query_message(
                [{"role": "user", "content": "x"}], attempts=1, client=client
            )
    assert "HTTP 500" in str(exc.value)


async def test_a_retry_does_not_send_the_identical_request():
    """Found live. mlx_lm's Qwen3-Coder tool parser raises on a tool call it cannot
    parse, which kills its handler thread and closes the socket with no response —
    arriving here as a bare disconnect that looks like a dead endpoint. At temperature
    0.1 against a warm prompt cache the retry regenerated the same unparseable call and
    died the same way, three times, so the retry budget bought nothing."""
    temps = []

    def handler(request: httpx.Request) -> httpx.Response:
        temps.append(json.loads(request.content)["temperature"])
        if len(temps) < 3:
            raise httpx.RemoteProtocolError("Server disconnected", request=request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with mock_client(handler) as client:
        message = await skippy_llm.query_message(
            [{"role": "user", "content": "x"}], temp=0.1, client=client
        )

    assert message["content"] == "ok"
    assert temps[0] == 0.1, "the first attempt asks for exactly what was requested"
    assert len(set(temps)) == 3, f"every attempt should differ, got {temps}"
    assert max(temps) <= 1.0


async def test_retry_temperature_stays_in_range():
    temps = []

    def handler(request: httpx.Request) -> httpx.Response:
        temps.append(json.loads(request.content)["temperature"])
        raise httpx.RemoteProtocolError("Server disconnected", request=request)

    async with mock_client(handler) as client:
        with pytest.raises(skippy_llm.ModelError):
            await skippy_llm.query_message(
                [{"role": "user", "content": "x"}], temp=0.95, attempts=3, client=client
            )
    assert all(0.0 <= t <= 1.0 for t in temps), temps


async def test_repetition_penalty_widens_the_context_window():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with mock_client(handler) as client:
        await skippy_llm.query_message(
            [{"role": "user", "content": "x"}], repetition_penalty=1.05, client=client
        )
    # The server default of 20 tokens is too short to catch sentence-length loops.
    assert seen["repetition_context_size"] == 512


async def test_role_selects_the_right_url_and_model():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["model"] = json.loads(request.content)["model"]
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    async with mock_client(handler) as client:
        await skippy_llm.query_message([{"role": "user", "content": "x"}], role="heavy", client=client)
    assert ":8081" in seen["url"]
    assert seen["model"] == skippy_llm.DEFAULT_HEAVY_MODEL


# --- assistant_turn ---

def test_assistant_turn_reserializes_tool_calls_for_the_next_request():
    turn = skippy_llm.assistant_turn({
        "content": "thinking",
        "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "a.py"}}],
    })
    assert turn["role"] == "assistant"
    assert turn["tool_calls"][0]["type"] == "function"
    assert json.loads(turn["tool_calls"][0]["function"]["arguments"]) == {"path": "a.py"}


def test_assistant_turn_omits_empty_content():
    turn = skippy_llm.assistant_turn({"content": "", "tool_calls": []})
    assert "content" not in turn
    assert "tool_calls" not in turn


# --- Transcript: append-only (ADR 0001) ---

def test_transcript_has_no_way_to_delete_or_rewrite_a_turn():
    # This is the whole point of the type. `del messages[2:4]` in the predecessor
    # silently cost a full re-prefill, measured at ~60s on the heavy role.
    transcript = skippy_llm.Transcript(system="be helpful")
    for forbidden in ("pop", "remove", "__delitem__", "__setitem__", "insert", "clear"):
        assert not hasattr(transcript, forbidden), f"Transcript must not expose {forbidden}"


def test_mutating_the_returned_messages_cannot_corrupt_the_prefix():
    transcript = skippy_llm.Transcript(system="be helpful")
    transcript.append({"role": "user", "content": "hello"})

    escaped = transcript.messages
    escaped[0]["content"] = "be unhelpful"
    del escaped[1]

    assert transcript.messages[0]["content"] == "be helpful"
    assert len(transcript) == 2


def test_appending_preserves_order_and_the_system_turn_stays_first():
    transcript = skippy_llm.Transcript(system="be helpful")
    transcript.extend([
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
    ])
    assert [m["role"] for m in transcript.messages] == ["system", "user", "assistant"]


def test_fold_returns_a_new_transcript_and_leaves_the_original_intact():
    transcript = skippy_llm.Transcript(system="be helpful")
    for i in range(6):
        transcript.append({"role": "user", "content": f"turn {i}"})

    folded = transcript.fold(keep_last=2, summary="we discussed six things")

    assert len(transcript) == 7, "fold must not mutate the original"
    assert folded is not transcript
    assert folded.messages[0]["content"] == "be helpful"
    assert "we discussed six things" in folded.messages[1]["content"]
    assert [m["content"] for m in folded.messages[2:]] == ["turn 4", "turn 5"]
    # No message dict is shared between the two transcripts.
    assert not any(a is b for a in transcript._messages for b in folded._messages)


def test_fold_warns_because_it_invalidates_the_prompt_cache(caplog):
    transcript = skippy_llm.Transcript(system="s")
    for i in range(4):
        transcript.append({"role": "user", "content": f"turn {i}"})

    with caplog.at_level("WARNING", logger="skippy_llm"):
        transcript.fold(keep_last=1, summary="...")
    assert any("prompt cache" in r.message for r in caplog.records)


def test_fold_rejects_a_negative_keep():
    with pytest.raises(ValueError):
        skippy_llm.Transcript(system="s").fold(keep_last=-1, summary="...")
