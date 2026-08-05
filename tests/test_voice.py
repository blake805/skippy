"""The realtime voice lane.

Importing `skippy_voice` under the light dependency set is part of the
coverage, same as `skippy_factory`: every engine (whisper, kokoro, torch,
mlx-audio, numpy) is behind a lazy accessor, and the endpointing, chunking and
wire logic must all be testable without any of them installed.
"""

import asyncio
import json
import struct
import time

import httpx
import pytest
from fastapi.testclient import TestClient

import skippy_voice
from skippy_voice import (
    EnergyVAD,
    Endpointer,
    FRAME_BYTES,
    SentenceChunker,
    CHATTERBOX_TAGS,
    clean_for_tts,
    resample_pcm16,
    strip_stage_directions,
    rms_pcm16,
    _authorized,
)


def loud_frame(amplitude: int = 8000) -> bytes:
    samples = [amplitude if i % 2 else -amplitude for i in range(skippy_voice.FRAME_SAMPLES)]
    return struct.pack(f"<{len(samples)}h", *samples)


def quiet_frame() -> bytes:
    return b"\x00" * FRAME_BYTES


# --- pure helpers ---

def test_rms_of_silence_is_zero_and_of_a_tone_is_its_amplitude():
    assert rms_pcm16(quiet_frame()) == 0.0
    assert abs(rms_pcm16(loud_frame(1000)) - 1000) < 1.0


def test_clean_for_tts_strips_what_reads_fine_but_speaks_terribly():
    text = "Use `run_command` for that. **Really.** See [the docs](http://x) or ```code\nblock\n```."
    cleaned = clean_for_tts(text)
    assert "`" not in cleaned and "**" not in cleaned
    assert "http" not in cleaned
    assert "the docs" in cleaned
    assert "block" not in cleaned  # code blocks are dropped, not read aloud


def test_stage_directions_the_engine_cannot_perform_are_dropped_not_spoken():
    text = "[snaps fingers] Right. [chuckle] Remember the bracket? [dramatic pause]"
    filtered = strip_stage_directions(text, CHATTERBOX_TAGS)
    assert filtered == "Right. [chuckle] Remember the bracket?"
    # An engine with no tag support (Kokoro) gets plain prose only.
    assert strip_stage_directions(text) == "Right. Remember the bracket?"


def test_throat_noises_are_stripped_even_though_the_engine_performs_them():
    # Chatterbox can perform [cough] and [clear throat], but the voice model
    # developed a tic of opening sentences with them, so they are not in the
    # allowed set — the listener gets the sentence, not the phlegm.
    text = "[cough] So here is the plan. [clear throat] First, the enclosure."
    filtered = strip_stage_directions(text, CHATTERBOX_TAGS)
    assert filtered == "So here is the plan. First, the enclosure."


def test_sentence_chunker_emits_the_first_sentence_the_moment_it_closes():
    chunker = SentenceChunker()
    out = []
    for token in ["Sure", ".", " That", " could", " work", "!", " Bu"]:
        out.extend(chunker.push(token))
    assert out == ["Sure.", "That could work!"]
    assert chunker.flush() == "Bu"
    assert chunker.flush() is None


def test_sentence_chunker_emits_the_first_clause_early_at_a_comma():
    chunker = SentenceChunker()
    out = []
    for token in ["Aluminum would work well", " for the enclosure,", " and it also"]:
        out.extend(chunker.push(token))
    # The first clause goes to TTS at the comma, not at the eventual period.
    assert out == ["Aluminum would work well for the enclosure,"]
    # After the first emission, commas no longer split ordinary sentences.
    out = chunker.push(" machines easily, sheds heat, and looks good. And")
    assert out == ["and it also machines easily, sheds heat, and looks good."]


def test_sentence_chunker_force_splits_a_clause_that_never_ends():
    chunker = SentenceChunker()
    rambling = ("blah " * 30) + ", " + ("blah " * 30)
    out = chunker.push(rambling)
    assert out, "a very long clause with a comma must still be emitted"
    assert all(len(s) <= SentenceChunker.FORCE_AT + 10 for s in out)


def test_resample_halves_and_doubles_sample_counts():
    pcm = struct.pack("<8h", *range(8))
    down = resample_pcm16(pcm, 32000, 16000)
    up = resample_pcm16(pcm, 16000, 32000)
    assert len(down) == 4 * 2
    assert len(up) == 16 * 2
    assert resample_pcm16(pcm, 16000, 16000) is pcm


# --- endpointing ---

def make_endpointer() -> Endpointer:
    return Endpointer(EnergyVAD(), silence_ms=200, min_speech_ms=100)


def test_endpointer_yields_one_utterance_with_preroll():
    ep = make_endpointer()
    events = []
    for _ in range(5):
        events.extend(ep.feed(quiet_frame()))
    for _ in range(10):
        events.extend(ep.feed(loud_frame()))
    for _ in range(12):
        events.extend(ep.feed(quiet_frame()))

    kinds = [kind for kind, _ in events]
    assert kinds == ["speech_start", "speech_confirmed", "utterance"]
    pcm = events[-1][1]
    # Pre-roll: the utterance carries frames from before the VAD tripped.
    assert len(pcm) > 10 * FRAME_BYTES


def test_endpointer_discards_a_burst_too_short_to_be_speech():
    ep = make_endpointer()
    events = []
    events.extend(ep.feed(loud_frame()))  # one frame ~ a cough
    for _ in range(12):
        events.extend(ep.feed(quiet_frame()))
    assert [kind for kind, _ in events] == ["speech_start"]


def test_endpointer_confirms_sustained_speech_exactly_once():
    ep = Endpointer(EnergyVAD(), silence_ms=200, min_speech_ms=100, barge_confirm_ms=150)
    events = []
    for _ in range(10):
        events.extend(ep.feed(loud_frame()))
    kinds = [kind for kind, _ in events]
    assert kinds[0] == "speech_start"
    assert kinds.count("speech_confirmed") == 1


def test_a_transient_too_short_to_confirm_never_confirms():
    ep = Endpointer(EnergyVAD(), silence_ms=200, min_speech_ms=100, barge_confirm_ms=150)
    events = list(ep.feed(loud_frame()))  # one 32ms frame: a click, a pop
    for _ in range(12):
        events.extend(ep.feed(quiet_frame()))
    assert [kind for kind, _ in events] == ["speech_start"]


def test_endpointer_reframes_arbitrary_chunk_sizes():
    ep = make_endpointer()
    blob = loud_frame() * 10 + quiet_frame() * 12
    events = []
    for i in range(0, len(blob), 700):  # deliberately not frame-aligned
        events.extend(ep.feed(blob[i:i + 700]))
    assert [kind for kind, _ in events] == ["speech_start", "speech_confirmed", "utterance"]


def test_energy_vad_noise_floor_does_not_learn_from_speech():
    vad = EnergyVAD()
    floor_before = vad.floor
    for _ in range(50):
        assert vad.is_speech(loud_frame())
    assert vad.floor == floor_before


# --- streaming plumbing: interruption must actually stop the work ---

def test_a_cancelled_consumer_stops_the_synthesis_thread():
    produced = []

    def factory():
        for i in range(200):
            produced.append(i)
            time.sleep(0.005)
            yield b"x"

    async def flow():
        stream = skippy_voice._iterate_in_thread(factory)
        async for _ in stream:
            break  # barge-in: the consumer walks away after the first chunk
        await stream.aclose()  # what task cancellation does to the generator
        await asyncio.sleep(0.05)  # let the worker notice the stop signal
        count = len(produced)
        await asyncio.sleep(0.2)
        # At most the chunk in flight completes; the sentence does not.
        assert len(produced) <= count + 1, "producer kept synthesizing after the consumer left"

    asyncio.run(flow())


# --- the brain's fallback: a hung server must not beat a dead one ---

def make_stream_role(calls, primary_fails):
    async def fake_stream_role(role, messages, temp):
        calls.append(role)
        if role != "fast" and primary_fails:
            raise httpx.ReadTimeout("server up, reply never came")
        yield "hello "
        yield "there."
    return fake_stream_role


def test_stream_chat_falls_back_when_the_voice_server_hangs(monkeypatch):
    monkeypatch.delenv("SKIPPY_VOICE_ROLE", raising=False)
    calls = []
    monkeypatch.setattr(skippy_voice, "_stream_role", make_stream_role(calls, primary_fails=True))

    async def collect():
        return [token async for token in skippy_voice.stream_chat([])]

    assert asyncio.run(collect()) == ["hello ", "there."]
    assert calls == ["voice", "fast"]


def test_a_mid_stream_failure_does_not_restart_the_reply(monkeypatch):
    # Tokens already yielded are already being spoken; a fallback here would
    # say the reply twice. The failure surfaces instead.
    monkeypatch.delenv("SKIPPY_VOICE_ROLE", raising=False)

    async def dies_after_a_token(role, messages, temp):
        yield "First half of the reply, "
        raise skippy_voice.skippy_llm.ModelError("died mid-stream")

    monkeypatch.setattr(skippy_voice, "_stream_role", dies_after_a_token)

    async def collect():
        tokens = []
        with pytest.raises(skippy_voice.skippy_llm.ModelError):
            async for token in skippy_voice.stream_chat([]):
                tokens.append(token)
        return tokens

    assert asyncio.run(collect()) == ["First half of the reply, "]


# --- auth ---

def test_voice_endpoint_is_open_when_no_token_is_configured(monkeypatch):
    monkeypatch.delenv("SKIPPY_VOICE_TOKEN", raising=False)
    assert _authorized(None)


def test_voice_endpoint_requires_the_exact_token_when_one_is_set(monkeypatch):
    monkeypatch.setenv("SKIPPY_VOICE_TOKEN", "secret")
    assert not _authorized(None)
    assert not _authorized("wrong")
    assert _authorized("secret")


def test_a_bad_token_is_refused_at_the_handshake(monkeypatch):
    import skippy_factory

    monkeypatch.setenv("SKIPPY_VOICE_TOKEN", "secret")
    with TestClient(skippy_factory.app) as client:
        with pytest.raises(Exception):
            with client.websocket_connect("/ws/voice?token=wrong") as ws:
                ws.receive_json()


# --- one full turn over the wire, with every engine faked ---

class FakeSTT:
    def transcribe(self, wav_path: str) -> str:
        return "what if the enclosure were aluminum"


class FakeTTS:
    def synthesize(self, text: str) -> bytes:
        return b"\x01\x00" * 100


async def fake_stream_chat(messages, role=None, temp=0.7):
    for token in ["Aluminum would work. ", "It machines easily."]:
        yield token
        await asyncio.sleep(0)


async def looping_stream_chat(messages, role=None, temp=0.7):
    # The degenerate case the first live test hit: the model repeating itself.
    for _ in range(20):
        yield "Printing is fine. What's your production plan? "
        await asyncio.sleep(0)


def test_a_spoken_utterance_comes_back_as_transcript_reply_and_audio(monkeypatch):
    import skippy_factory

    monkeypatch.delenv("SKIPPY_VOICE_TOKEN", raising=False)
    monkeypatch.setattr(skippy_voice, "build_vad", EnergyVAD)
    monkeypatch.setattr(skippy_voice, "get_stt", FakeSTT)
    monkeypatch.setattr(skippy_voice, "get_tts", FakeTTS)
    monkeypatch.setattr(skippy_voice, "stream_chat", fake_stream_chat)
    monkeypatch.setattr(skippy_voice, "_voice_system_prompt", lambda: "You are Skippy.")

    async def no_save(history):
        return None

    monkeypatch.setattr(skippy_voice, "_save_session", no_save)

    with TestClient(skippy_factory.app) as client:
        with client.websocket_connect("/ws/voice") as ws:
            assert ws.receive_json() == {"type": "state", "state": "listening"}

            ws.send_json({"type": "start", "duplex": False})
            for _ in range(10):
                ws.send_bytes(loud_frame())
            for _ in range(20):
                ws.send_bytes(quiet_frame())

            texts, audio_bytes = [], 0
            while True:
                message = ws.receive()
                if "bytes" in message and message["bytes"] is not None:
                    audio_bytes += len(message["bytes"])
                    continue
                data = json.loads(message["text"])
                texts.append(data)
                if data.get("type") == "metrics":
                    break

            kinds = [t["type"] for t in texts]
            assert "transcript" in kinds
            assert "audio_start" in kinds and "audio_end" not in kinds[: kinds.index("audio_start")]
            replies = [t["text"] for t in texts if t["type"] == "reply"]
            assert replies == ["Aluminum would work.", "It machines easily."]
            transcript = next(t for t in texts if t["type"] == "transcript")
            assert transcript["text"] == "what if the enclosure were aluminum"
            assert audio_bytes == 2 * 200  # two sentences of fake PCM


def test_a_model_stuck_in_a_loop_is_cut_off_at_the_first_repeat(monkeypatch):
    import skippy_factory

    monkeypatch.delenv("SKIPPY_VOICE_TOKEN", raising=False)
    monkeypatch.setattr(skippy_voice, "build_vad", EnergyVAD)
    monkeypatch.setattr(skippy_voice, "get_stt", FakeSTT)
    monkeypatch.setattr(skippy_voice, "get_tts", FakeTTS)
    monkeypatch.setattr(skippy_voice, "stream_chat", looping_stream_chat)
    monkeypatch.setattr(skippy_voice, "_voice_system_prompt", lambda: "You are Skippy.")

    async def no_save(history):
        return None

    monkeypatch.setattr(skippy_voice, "_save_session", no_save)

    with TestClient(skippy_factory.app) as client:
        with client.websocket_connect("/ws/voice") as ws:
            ws.receive_json()
            for _ in range(10):
                ws.send_bytes(loud_frame())
            for _ in range(20):
                ws.send_bytes(quiet_frame())

            replies = []
            while True:
                message = ws.receive()
                if "bytes" in message and message["bytes"] is not None:
                    continue
                data = json.loads(message["text"])
                if data.get("type") == "reply":
                    replies.append(data["text"])
                if data.get("type") == "metrics":
                    break

            # Each distinct sentence spoken once; the repeats never made it out.
            assert replies == ["Printing is fine.", "What's your production plan?"]


# --- the action lane: the voice brain's hands ---

from skippy_voice import VoiceSession, parse_route, wants_action  # noqa: E402


def test_the_gate_is_broad_and_pure_chat_never_pays_the_router_toll():
    assert wants_action("can you start a task to fix the failing tests")
    assert wants_action("cancel that")
    assert wants_action("what did we decide about the enclosure")
    assert wants_action("hand this to the heavy model")
    # Ordinary brainstorming skips the router call entirely — no added latency.
    assert not wants_action("what if the enclosure were aluminum")
    assert not wants_action("good morning skippy")


def test_parse_route_survives_prose_wrapping_and_downgrades_garbage():
    good = parse_route('Sure! {"action": "start_task", "text": "Fix it", "mode": "coding"} done')
    assert good == {"action": "start_task", "text": "Fix it", "mode": "coding"}
    assert parse_route('{"action": "none"}') is None
    assert parse_route("I would not do anything here.") is None
    assert parse_route('{"action":') is None
    assert parse_route("") is None


class FakeRunner:
    def __init__(self, running=False):
        self.running = running
        self.started = []
        self.cancelled = []

    def is_running(self, client_id):
        return self.running

    async def start(self, client_id, request):
        self.started.append((client_id, request))

    def cancel(self, client_id):
        self.cancelled.append(client_id)
        return self.running


class FakeHub:
    def __init__(self):
        self.active_connections = {}


def make_session(monkeypatch, runner=None, hub=None):
    monkeypatch.setattr(skippy_voice, "build_vad", EnergyVAD)
    sent = {"json": [], "bytes": []}

    async def send_json(payload):
        sent["json"].append(payload)

    async def send_bytes(data):
        sent["bytes"].append(data)

    session = VoiceSession(websocket=None, send_json=send_json, send_bytes=send_bytes)
    hub = hub or FakeHub()
    runner = runner or FakeRunner()
    monkeypatch.setattr(VoiceSession, "_factory", staticmethod(lambda: (hub, runner)))
    return session, runner, hub, sent


def test_start_task_dispatches_to_the_runner_and_registers_a_tap(monkeypatch):
    session, runner, hub, _ = make_session(monkeypatch)
    note = asyncio.run(session._perform(
        {"action": "start_task", "text": "Fix the reconnect test", "mode": "coding"}
    ))
    assert runner.started == [(session.client_id, {"text": "Fix the reconnect test", "mode": "Agent"})]
    # The runner reports to hub.active_connections; the tap is standing there.
    assert hub.active_connections[session.client_id] is session._tap
    # The persona is told the truth: started, running in background, no results yet.
    assert "started" in note and "background" in note


def test_a_second_task_is_refused_not_silently_queued(monkeypatch):
    session, runner, _, _ = make_session(monkeypatch, runner=FakeRunner(running=True))
    note = asyncio.run(session._perform(
        {"action": "start_task", "text": "Another thing", "mode": "coding"}
    ))
    assert runner.started == []
    assert "already running" in note


def test_task_completion_is_announced_once_with_the_summary(monkeypatch):
    session, _, _, _ = make_session(monkeypatch)
    announced = []
    session.announce = announced.append

    async def flow():
        tap = skippy_voice._TaskTap(session)
        await tap.send_json({"type": "chat", "content": "All 12 tests pass now. The bug was a stale mock."})
        await tap.send_json({"type": "done"})
        await tap.send_json({"type": "done"})  # a duplicate done must not re-announce

    asyncio.run(flow())
    assert len(announced) == 1
    assert "All 12 tests pass now." in announced[0]


def test_a_broken_router_downgrades_to_plain_conversation(monkeypatch):
    session, _, _, _ = make_session(monkeypatch)
    session.history = [{"role": "user", "content": "start a task please"}]

    async def broken(*args, **kwargs):
        raise RuntimeError("model server down")

    monkeypatch.setattr(skippy_voice.skippy_llm, "query_text", broken)
    assert asyncio.run(session._route_and_act()) is None


def test_heavy_answers_arrive_as_a_spoken_announcement(monkeypatch):
    session, _, _, _ = make_session(monkeypatch)
    announced = []
    session.announce = announced.append

    async def slow_genius(messages, role="fast", **kwargs):
        assert role == "heavy"
        return "Forty-two. The trick is asking the right question."

    monkeypatch.setattr(skippy_voice.skippy_llm, "query_text", slow_genius)

    async def flow():
        note = await session._perform({"action": "ask_heavy", "question": "meaning of life?"})
        assert "heavy model" in note and "announce" in note
        await asyncio.gather(*session._background)

    asyncio.run(flow())
    assert len(announced) == 1
    assert "Forty-two." in announced[0]


def test_announcements_wait_out_the_transcription_window(monkeypatch):
    # From utterance close to responder creation, the endpointer is idle and
    # no responder task exists — the only thing telling _announce the room is
    # not actually quiet is the turn flag.
    session, _, _, _ = make_session(monkeypatch)
    assert session._quiet()
    session._turn_active = True
    assert not session._quiet()
    session._turn_active = False
    assert session._quiet()


def test_the_turn_flag_covers_stt_and_clears_after(monkeypatch):
    session, _, _, _ = make_session(monkeypatch)
    seen = {}

    class SpyingSTT:
        def transcribe(self, wav_path):
            seen["active_during_stt"] = session._turn_active
            return ""  # empty transcript: the turn ends without a responder

    monkeypatch.setattr(skippy_voice, "get_stt", SpyingSTT)
    asyncio.run(session._on_utterance(b"\x00" * FRAME_BYTES))
    assert seen["active_during_stt"] is True
    assert session._turn_active is False


def test_a_short_blip_does_not_barge_in_but_sustained_speech_does(monkeypatch):
    """The speaker-echo bug: on built-in speakers the mic hears Skippy's own
    voice, and cancelling on the first VAD frame cut every reply off with
    nobody talking. A reply in flight now survives transients; only speech
    that persists past the confirm window interrupts it."""
    session, _, _, sent = make_session(monkeypatch)
    cancelled = []

    async def fake_cancel():
        cancelled.append(True)

    session._cancel_response = fake_cancel
    monkeypatch.setattr(VoiceSession, "_responding", lambda self: True)

    async def flow():
        await session.handle_audio(loud_frame())  # one frame: echo, a pop
        assert cancelled == [], "one 32ms frame must not cancel the reply"
        for _ in range(12):  # past the 250ms confirm window: a real barge-in
            await session.handle_audio(loud_frame())
        assert cancelled, "sustained speech must interrupt the reply"

    asyncio.run(flow())
    # The partial marker goes out with the barge-in, not before.
    assert {"type": "partial", "text": ""} in sent["json"]


def test_half_duplex_hardware_never_barges_in(monkeypatch):
    """A Core2 cannot hear the user while it plays audio; what its mic picks
    up mid-reply is the room. The endpointer is reset instead of cancelled."""
    session, _, _, sent = make_session(monkeypatch)
    session.duplex = False
    cancelled = []

    async def fake_cancel():
        cancelled.append(True)

    session._cancel_response = fake_cancel
    monkeypatch.setattr(VoiceSession, "_responding", lambda self: True)

    async def flow():
        for _ in range(20):
            await session.handle_audio(loud_frame())

    asyncio.run(flow())
    assert cancelled == []
    assert sent["json"] == []


def test_memory_search_admits_when_it_finds_nothing(monkeypatch):
    session, _, _, _ = make_session(monkeypatch)
    monkeypatch.setattr(skippy_voice, "_search_memory", lambda query, limit=4: "")
    note = asyncio.run(session._perform({"action": "search_memory", "query": "enclosure decision"}))
    assert "found nothing" in note
