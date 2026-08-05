"""Real-time speech-to-speech brainstorming over one websocket.

This is the voice lane: `/ws/voice`. It is deliberately not the agent loop. The
agent lane (`/ws/factory`) runs tools and can take minutes per turn; a voice
conversation dies at two seconds of silence. So this endpoint runs a separate,
latency-shaped pipeline — VAD -> STT -> fast model -> TTS — and never calls a
tool. What it shares with the rest of Skippy is the part that matters: the same
model registry (`skippy_llm`), and the same project memory (`skippy_memory`),
injected at session start and written back at session end, so what gets
brainstormed out loud is not lost to the next coding session.

Wire protocol (one websocket, both directions):

  client -> server
    binary frames   16-bit little-endian mono PCM at 16 kHz, any chunk size
    {"type":"start", "duplex": bool}    optional; duplex=false disables
                                        VAD barge-in (half-duplex hardware
                                        like the M5Stack Core2 cannot hear
                                        itself talk, so it sends "interrupt")
    {"type":"interrupt"}                stop speaking now
    {"type":"end"}                      close the session and save memory

  server -> client
    binary frames   16-bit LE mono PCM at SKIPPY_VOICE_OUT_RATE (default 16 kHz)
    {"type":"state","state":"listening"|"thinking"|"speaking"}
    {"type":"partial","text":...}       VAD heard speech start (for a display)
    {"type":"transcript","text":...}    what the user said, final
    {"type":"reply","text":...}         each sentence as it is synthesized
    {"type":"audio_start"} / {"type":"audio_end"}
                                        playback bracket; half-duplex clients
                                        switch their I2S direction on these
    {"type":"audio_cancel"}             drop any buffered audio immediately
    {"type":"metrics", ...}             per-turn latency numbers
    {"type":"error","message":...}

Latency is the design constraint, and three choices fall out of it. TTS starts
on the first complete sentence of the model's token stream rather than the full
reply. Audio is sent as fast as it is synthesized rather than paced to real
time — which is why `audio_cancel` exists, because bytes already in the
client's jitter buffer cannot be unsent. And everything model-shaped runs in a
worker thread so the websocket reader never stops reading; the reader is the
only thing that can hear an interrupt.

Every engine is selected by environment variable and loaded on first use, so
importing this module needs nothing beyond the standard library and FastAPI —
the same rule skippy_factory follows, for the same CI reason.

  SKIPPY_VOICE_TOKEN        shared secret; required on connect when set
  SKIPPY_VOICE_ROLE         model role for the brain (default "voice", which
                            falls back to "fast" if its server is down)
  SKIPPY_VOICE_STT          "whisper" | "whisper:<size>" | "mlx:<model_id>"
  SKIPPY_VOICE_TTS          "kokoro" | "mlx:<model_id>"
  SKIPPY_VOICE_TTS_VOICE    voice name (default "am_michael")
  SKIPPY_VOICE_TTS_REF      reference WAV for voice-cloning models; overrides
                            the voice name (chatterbox/CSM style engines)
  SKIPPY_VOICE_TTS_SPEED    speaking rate (default 1.15, kokoro only)
  SKIPPY_VOICE_VAD          "auto" | "silero" | "energy" (default "auto")
  SKIPPY_VOICE_SILENCE_MS   trailing silence that ends an utterance (400)
  SKIPPY_VOICE_MIN_SPEECH_MS  shorter bursts are discarded as noise (250)
  SKIPPY_VOICE_BARGE_MS     speech must persist this long to interrupt a reply
                            in flight (250) — one VAD frame is as often the
                            speaker leaking into the mic as it is the user
  SKIPPY_VOICE_OUT_RATE     output sample rate (default 16000; a full-duplex
                            desktop client may prefer 24000)
"""

import asyncio
import json
import logging
import os
import re
import struct
import tempfile
import threading
import time
import wave
from collections import deque
from typing import AsyncIterator, Callable, Iterator, List, Optional, Sequence, Tuple

import httpx
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import prompts
import skippy_gate
import skippy_llm
import skippy_paths

logger = logging.getLogger("skippy_voice")

router = APIRouter()

# --- WIRE CONSTANTS ---
IN_RATE = 16_000
# 512 samples = 32 ms at 16 kHz. Not arbitrary: Silero's streaming model only
# accepts 512-sample windows at this rate, so the whole pipeline frames to it.
FRAME_SAMPLES = 512
FRAME_BYTES = FRAME_SAMPLES * 2
# ~320 ms of audio kept from before the VAD tripped. Endpointing always fires
# late; without pre-roll every utterance loses its first syllable and "skippy"
# transcribes as "ippy".
PREROLL_FRAMES = 10
AUDIO_CHUNK_BYTES = 4096


def _env(name: str, default: str) -> str:
    return os.environ.get(name, "").strip() or default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        logger.warning("Ignoring non-integer %s=%r; using %d", name, raw, default)
        return default


def out_rate() -> int:
    return _env_int("SKIPPY_VOICE_OUT_RATE", 16_000)


# ---------------------------------------------------------------------------
# Pure helpers. No third-party imports: these are what the test suite covers
# under the light dependency set.
# ---------------------------------------------------------------------------

def rms_pcm16(frame: bytes) -> float:
    """Root-mean-square of a 16-bit PCM frame, in raw sample units."""
    count = len(frame) // 2
    if not count:
        return 0.0
    samples = struct.unpack(f"<{count}h", frame[: count * 2])
    return (sum(s * s for s in samples) / count) ** 0.5


_MARKDOWN_NOISE = re.compile(r"```.*?```|`[^`]*`|\*\*|__|(?<!\w)[*_](?!\w)", re.DOTALL)
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")


def clean_for_tts(text: str) -> str:
    """Strip the markdown that reads fine and speaks terribly."""
    text = _LINK.sub(r"\1", text)
    text = _MARKDOWN_NOISE.sub(" ", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    return " ".join(text.split())


# The subset of Chatterbox Turbo's sound tags we let through to the engine.
# Anything else inside brackets is deleted rather than spoken — the model
# improvised "[snaps fingers]" in a live session and the voice said the words
# "snaps fingers". Chatterbox also performs "cough", "sniff", "shush" and
# "clear throat", but those are stripped on purpose: the voice model developed
# a tic of opening nearly every sentence with [cough] or [clear throat], and
# hearing it performed is exactly as annoying as it sounds.
CHATTERBOX_TAGS = frozenset({
    "laugh", "sigh", "gasp", "groan", "chuckle",
})

_BRACKET_TAG = re.compile(r"\[([^\[\]]{1,30})\]")


def strip_stage_directions(text: str, allowed: frozenset = frozenset()) -> str:
    """Drop bracketed stage directions the speech engine cannot perform.

    Tags in `allowed` pass through (the engine turns them into actual sounds);
    everything else in brackets is deleted rather than spoken.
    """
    def keep_or_drop(match: re.Match) -> str:
        return match.group(0) if match.group(1).strip().lower() in allowed else " "

    return " ".join(_BRACKET_TAG.sub(keep_or_drop, text).split())


_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"'\)\]]*\s")


class SentenceChunker:
    """Turns a token stream into speakable sentences as early as possible.

    The first sentence is what the user hears the latency of, so it is emitted
    the moment its terminator arrives — and before anything has been emitted at
    all, a comma is good enough: the first clause of the first sentence going
    to TTS early is worth more than perfect phrasing, because every character
    the model has yet to generate and Kokoro has yet to synthesize is silence
    the user is sitting in. The force threshold exists for the model that
    writes a 400-character clause with no punctuation at all.
    """

    FORCE_AT = 220
    EARLY_FIRST_AT = 30

    def __init__(self):
        self._buf = ""
        self._emitted = False

    def push(self, token: str) -> List[str]:
        self._buf += token
        out: List[str] = []
        while True:
            match = _SENTENCE_END.search(self._buf)
            if match:
                sentence, self._buf = self._buf[: match.end()].strip(), self._buf[match.end():]
                if sentence:
                    out.append(sentence)
                    self._emitted = True
                continue
            if not self._emitted and len(self._buf) > self.EARLY_FIRST_AT:
                # First emission: the earliest clause boundary past the floor.
                cuts = [self._buf.find(mark, 15) for mark in (",", ";", ":")]
                cut = min(c for c in cuts if c != -1) if any(c != -1 for c in cuts) else -1
            elif len(self._buf) > self.FORCE_AT:
                cut = max(self._buf.rfind(mark, 0, self.FORCE_AT + 1) for mark in (",", ";", ":"))
                if cut <= 40:
                    cut = -1
            else:
                return out
            if cut == -1:
                return out
            sentence, self._buf = self._buf[: cut + 1].strip(), self._buf[cut + 1:]
            out.append(sentence)
            self._emitted = True

    def flush(self) -> Optional[str]:
        rest, self._buf = self._buf.strip(), ""
        return rest or None


def resample_pcm16(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    """Linear-interpolation resample. numpy when present, stdlib when not."""
    if src_rate == dst_rate or not pcm:
        return pcm
    try:
        import numpy as np

        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        dst_len = max(1, int(round(len(samples) * dst_rate / src_rate)))
        positions = np.linspace(0, len(samples) - 1, dst_len)
        resampled = np.interp(positions, np.arange(len(samples)), samples)
        return np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()
    except ImportError:
        count = len(pcm) // 2
        samples = struct.unpack(f"<{count}h", pcm[: count * 2])
        dst_len = max(1, int(round(count * dst_rate / src_rate)))
        out = []
        step = (count - 1) / max(1, dst_len - 1) if dst_len > 1 else 0.0
        for i in range(dst_len):
            pos = i * step
            lo = int(pos)
            hi = min(lo + 1, count - 1)
            frac = pos - lo
            value = samples[lo] * (1 - frac) + samples[hi] * frac
            out.append(max(-32768, min(32767, int(round(value)))))
        return struct.pack(f"<{dst_len}h", *out)


def write_wav(path: str, pcm: bytes, rate: int = IN_RATE) -> None:
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(pcm)


# ---------------------------------------------------------------------------
# Voice activity detection and endpointing
# ---------------------------------------------------------------------------

class EnergyVAD:
    """RMS against an adaptive noise floor. The fallback that always imports.

    The floor is learned from non-speech frames only, so a long monologue does
    not teach the detector that talking is silence. It adapts upward slowly and
    downward fast: a door slam should not raise the bar for the next minute.
    """

    def __init__(self, floor: float = 150.0, ratio: float = 3.5, minimum: float = 350.0):
        self.floor = floor
        self.ratio = ratio
        self.minimum = minimum

    def is_speech(self, frame: bytes) -> bool:
        level = rms_pcm16(frame)
        speech = level > max(self.minimum, self.floor * self.ratio)
        if not speech:
            alpha = 0.05 if level > self.floor else 0.2
            self.floor = (1 - alpha) * self.floor + alpha * level
        return speech

    def reset(self) -> None:
        pass


class SileroVAD:
    """The neural detector, when torch and silero-vad are installed."""

    def __init__(self):
        import torch  # noqa: F401 — fail here, at construction, not per frame
        from silero_vad import load_silero_vad

        self._torch = torch
        self._model = load_silero_vad()
        self.threshold = 0.5

    def is_speech(self, frame: bytes) -> bool:
        tensor = self._torch.frombuffer(bytearray(frame), dtype=self._torch.int16)
        tensor = tensor.to(self._torch.float32) / 32768.0
        with self._torch.no_grad():
            probability = self._model(tensor, IN_RATE).item()
        return probability >= self.threshold

    def reset(self) -> None:
        self._model.reset_states()


def build_vad():
    choice = _env("SKIPPY_VOICE_VAD", "auto").lower()
    if choice in ("auto", "silero"):
        try:
            vad = SileroVAD()
            logger.info("VAD: silero")
            return vad
        except Exception as exc:
            if choice == "silero":
                raise
            logger.info("VAD: silero unavailable (%s), using energy detector", exc)
    logger.info("VAD: adaptive energy")
    return EnergyVAD()


class Endpointer:
    """Frames in, utterances out.

    Buffers incoming PCM into fixed VAD windows, tracks a speaking/idle state,
    and yields events: ("speech_start", b"") the frame speech begins,
    ("speech_confirmed", b"") once the speech has persisted long enough to be
    a person rather than a transient, and ("utterance", pcm) once trailing
    silence closes it. Utterances shorter than the minimum are dropped without
    an event — a cough is not a question.
    """

    def __init__(
        self,
        vad,
        silence_ms: Optional[int] = None,
        min_speech_ms: Optional[int] = None,
        barge_confirm_ms: Optional[int] = None,
    ):
        self.vad = vad
        frame_ms = 1000 * FRAME_SAMPLES / IN_RATE
        self._silence_frames = max(1, int((silence_ms or _env_int("SKIPPY_VOICE_SILENCE_MS", 400)) / frame_ms))
        self._min_speech_frames = max(1, int((min_speech_ms or _env_int("SKIPPY_VOICE_MIN_SPEECH_MS", 250)) / frame_ms))
        self._confirm_frames = max(1, int((barge_confirm_ms or _env_int("SKIPPY_VOICE_BARGE_MS", 250)) / frame_ms))
        self._pending = b""
        self._preroll: deque = deque(maxlen=PREROLL_FRAMES)
        self._utterance: List[bytes] = []
        self._speech_frames = 0
        self._silent_run = 0
        self.in_speech = False

    def feed(self, data: bytes) -> List[Tuple[str, bytes]]:
        events: List[Tuple[str, bytes]] = []
        self._pending += data
        while len(self._pending) >= FRAME_BYTES:
            frame, self._pending = self._pending[:FRAME_BYTES], self._pending[FRAME_BYTES:]
            events.extend(self._frame(frame))
        return events

    def _frame(self, frame: bytes) -> List[Tuple[str, bytes]]:
        speech = self.vad.is_speech(frame)
        if not self.in_speech:
            if speech:
                self.in_speech = True
                self._utterance = list(self._preroll) + [frame]
                self._speech_frames = 1
                self._silent_run = 0
                self._preroll.clear()
                return [("speech_start", b"")]
            self._preroll.append(frame)
            return []

        self._utterance.append(frame)
        if speech:
            self._speech_frames += 1
            self._silent_run = 0
            if self._speech_frames == self._confirm_frames:
                return [("speech_confirmed", b"")]
            return []

        self._silent_run += 1
        if self._silent_run < self._silence_frames:
            return []

        pcm = b"".join(self._utterance)
        spoken_enough = self._speech_frames >= self._min_speech_frames
        self.reset(keep_vad_state=True)
        return [("utterance", pcm)] if spoken_enough else []

    def reset(self, keep_vad_state: bool = False) -> None:
        self.in_speech = False
        self._utterance = []
        self._speech_frames = 0
        self._silent_run = 0
        self._preroll.clear()
        if not keep_vad_state:
            self.vad.reset()


# ---------------------------------------------------------------------------
# STT backends
# ---------------------------------------------------------------------------

class WhisperSTT:
    """The default: openai-whisper, already a dependency of this repo."""

    def __init__(self, size: str = "base"):
        import whisper

        logger.info("STT: whisper %s", size)
        self._model = whisper.load_model(size)

    def transcribe(self, wav_path: str) -> str:
        result = self._model.transcribe(wav_path, fp16=False, language="en")
        return str(result["text"]).strip()


class MlxSTT:
    """Any mlx-audio STT model (Parakeet, Qwen3-ASR, Voxtral, ...)."""

    def __init__(self, model_id: str):
        from mlx_audio.stt.utils import load

        logger.info("STT: mlx-audio %s", model_id)
        self._model = load(model_id)

    def transcribe(self, wav_path: str) -> str:
        result = self._model.generate(wav_path)
        return str(getattr(result, "text", result)).strip()


def build_stt():
    spec = _env("SKIPPY_VOICE_STT", "whisper")
    if spec.startswith("mlx:"):
        return MlxSTT(spec[4:])
    if spec.startswith("whisper:"):
        return WhisperSTT(spec.split(":", 1)[1])
    return WhisperSTT()


# ---------------------------------------------------------------------------
# TTS backends. synthesize() returns PCM16 bytes already at out_rate().
# ---------------------------------------------------------------------------

class KokoroTTS:
    """The default: kokoro-onnx with the weights already at the repo root."""

    def __init__(self):
        from kokoro_onnx import Kokoro

        logger.info("TTS: kokoro-onnx")
        self._model = Kokoro("kokoro-v1.0.int8.onnx", "voices-v1.0.bin")
        self.voice = _env("SKIPPY_VOICE_TTS_VOICE", "am_michael")
        self.speed = float(_env("SKIPPY_VOICE_TTS_SPEED", "1.15"))

    def synthesize(self, text: str) -> bytes:
        import numpy as np

        samples, rate = self._model.create(text, voice=self.voice, speed=self.speed, lang="en-us")
        pcm = np.clip(np.asarray(samples) * 32767.0, -32768, 32767).astype(np.int16).tobytes()
        return resample_pcm16(pcm, rate, out_rate())


class MlxTTS:
    """Any mlx-audio TTS model (Kokoro-82M, Chatterbox Turbo, Qwen3-TTS, ...)."""

    def __init__(self, model_id: str):
        from mlx_audio.tts.utils import load_model

        logger.info("TTS: mlx-audio %s", model_id)
        self._model = load_model(model_id)
        self.voice = _env("SKIPPY_VOICE_TTS_VOICE", "am_michael")
        # A reference clip turns a cloning model (Chatterbox, CSM) into a
        # specific person; voice-bank models like Kokoro use names instead.
        # The conditioning is computed once here: passing ref_audio through
        # generate() re-encodes the whole clip per sentence, which measured
        # ~1.7s added to every reply's first audio.
        self.ref_audio = _env("SKIPPY_VOICE_TTS_REF", "") or None
        self._ref_prepared = False
        if self.ref_audio and hasattr(self._model, "prepare_conditionals"):
            self._model.prepare_conditionals(self.ref_audio)
            self._ref_prepared = True
        # Chatterbox generates autoregressively and can yield audio every few
        # hundred milliseconds; waiting for the full clause instead measured
        # ~900ms of avoidable dead air per reply. Kokoro is near-instant per
        # call, so buying complexity there gains nothing.
        self.can_stream = "chatterbox" in model_id.lower()
        self.tags = CHATTERBOX_TAGS if "chatterbox" in model_id.lower() else frozenset()

    def _kwargs(self) -> dict:
        if self._ref_prepared:
            return {}
        if self.ref_audio:
            return {"ref_audio": self.ref_audio}
        return {"voice": self.voice}

    @staticmethod
    def _to_pcm(result, rate: int) -> tuple:
        import numpy as np

        rate = int(getattr(result, "sample_rate", rate))
        audio = np.asarray(result.audio, dtype=np.float32)
        pcm = np.clip(audio * 32767.0, -32768, 32767).astype(np.int16).tobytes()
        return resample_pcm16(pcm, rate, out_rate()), rate

    def synthesize(self, text: str) -> bytes:
        return b"".join(self.synthesize_stream(text, streaming=False))

    def synthesize_stream(self, text: str, streaming: bool = True) -> Iterator[bytes]:
        kwargs = self._kwargs()
        streamed = streaming and self.can_stream
        if streamed:
            kwargs.update(stream=True, streaming_interval=0.4)
        rate = 24_000
        for result in self._model.generate(text=text, **kwargs):
            pcm, rate = self._to_pcm(result, rate)
            if pcm:
                yield _seam_fade(pcm) if streamed else pcm


def _seam_fade(pcm: bytes, samples: int = 32) -> bytes:
    """Linear-ramp the first and last ~2ms of a chunk to zero.

    Chatterbox's streaming decoder re-decodes the sentence as tokens accrue and
    slices off the unplayed part; the slice boundary does not line up sample
    for sample with what was already sent, and the step lands as an audible
    click every chunk. A 2ms ramp at each seam is below the ear's threshold
    for speech but removes the discontinuity.
    """
    import numpy as np

    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
    n = min(samples, len(audio) // 2)
    if n > 0:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        audio[:n] *= ramp
        audio[-n:] *= ramp[::-1]
    return audio.astype(np.int16).tobytes()


async def _iterate_in_thread(factory: Callable[[], Iterator[bytes]]) -> AsyncIterator[bytes]:
    """Drive a blocking generator on a worker thread, yielding on the loop.

    The synthesis generators hold the GIL-releasing MLX work; running them
    inline would stall the websocket's heartbeat exactly the way the old
    whole-clause synthesize did, just in smaller pieces.

    The stop event is the barge-in path's reach into the worker: when the
    consumer is cancelled mid-sentence, the producer must not keep
    synthesizing the rest of it into a queue nobody will drain — that work
    runs on the same GPU that is about to transcribe the interruption. The
    check sits between chunks, so at worst the chunk in flight completes.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    done = object()
    stop = threading.Event()

    def produce() -> None:
        try:
            for item in factory():
                if stop.is_set():
                    break
                loop.call_soon_threadsafe(queue.put_nowait, item)
            loop.call_soon_threadsafe(queue.put_nowait, done)
        except BaseException as exc:  # surfaces on the consumer side
            loop.call_soon_threadsafe(queue.put_nowait, exc)

    loop.run_in_executor(None, produce)
    try:
        while True:
            item = await queue.get()
            if item is done:
                return
            if isinstance(item, BaseException):
                raise item
            yield item
    finally:
        stop.set()


def build_tts():
    spec = _env("SKIPPY_VOICE_TTS", "kokoro")
    if spec.startswith("mlx:"):
        return MlxTTS(spec[4:])
    return KokoroTTS()


# Engines are process-wide: loading Whisper per websocket would add seconds to
# every reconnect, and there is one microphone in the shop anyway.
_engines: dict = {}


def get_stt():
    if "stt" not in _engines:
        _engines["stt"] = build_stt()
    return _engines["stt"]


def get_tts():
    if "tts" not in _engines:
        _engines["tts"] = build_tts()
    return _engines["tts"]


# ---------------------------------------------------------------------------
# The brain: streaming chat against a role from skippy_llm's registry
# ---------------------------------------------------------------------------

async def stream_chat(
    messages: Sequence[dict],
    role: Optional[str] = None,
    temp: float = 0.7,
) -> AsyncIterator[str]:
    """Yield content tokens from a role's endpoint as they generate.

    skippy_llm.query_message waits for the whole completion, which is right for
    the agent loop and wrong here: time-to-first-sentence is the product. Same
    registry, same local/cloud policy, different transport.

    The default role is "voice" (a chat-tuned model on its own port). If that
    server cannot produce a reply — refused connection, timeout, or an error
    status — the stream falls back to "fast" once, loudly: a degraded
    brainstorm beats a dead microphone, and a hung server must not behave
    worse than a dead one. The one case that does not fall back is a failure
    after tokens have already flowed: those tokens are already being spoken,
    and restarting the reply on another model would say it twice.
    """
    wanted = role or _env("SKIPPY_VOICE_ROLE", "voice")
    emitted = False
    try:
        async for token in _stream_role(wanted, messages, temp):
            emitted = True
            yield token
        return
    except (httpx.HTTPError, skippy_llm.ModelError) as exc:
        if wanted == "fast" or emitted:
            raise
        logger.warning(
            "Voice role '%s' failed before its first token (%s); falling back "
            "to 'fast'. Start its server (or set SKIPPY_VOICE_ROLE) for the "
            "intended brain.", wanted, exc,
        )
    async for token in _stream_role("fast", messages, temp):
        yield token


async def _stream_role(role: str, messages: Sequence[dict], temp: float) -> AsyncIterator[str]:
    target = skippy_llm.endpoint(role)
    payload = {
        "model": target.model,
        "messages": list(messages),
        "temperature": temp,
        "max_tokens": target.max_tokens,
        "stream": True,
        # This lane only ever generates prose, so the repetition penalty that
        # skippy_llm forbids for code is safe — and necessary: the first live
        # test of this pipeline had the fast model repeat the same three
        # sentences fifteen times, exactly the degenerate loop that module's
        # comments describe.
        "repetition_penalty": 1.05,
        "repetition_context_size": 512,
    }
    headers = {"Authorization": f"Bearer {target.api_key}"} if target.api_key else None

    async with httpx.AsyncClient() as client:
        async with client.stream("POST", target.url, json=payload, headers=headers, timeout=120.0) as response:
            if response.status_code != 200:
                body = (await response.aread())[:400]
                raise skippy_llm.ModelError(f"Voice role HTTP {response.status_code}: {body!r}")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    delta = json.loads(data)["choices"][0].get("delta", {})
                except (ValueError, KeyError, IndexError):
                    continue
                token = delta.get("content")
                if token:
                    yield token


# Only offered to the model when the engine can honor it: Kokoro would read
# "[chuckle]" out loud, letter by bracket. Kept in lockstep with
# CHATTERBOX_TAGS — advertising a tag the filter strips (as an earlier
# version did with [cough]) teaches the model a habit the listener never
# hears the point of.
_EXPRESSIVE_TAGS_NOTE = (
    "Your speech engine understands exactly these bracketed expressive tags "
    "and no others: [chuckle], [laugh], [sigh], [gasp], [groan]. Use one "
    "occasionally, where a person would actually make the sound — at most "
    "one per reply. Never invent other bracketed actions ([snaps fingers], "
    "[pause]) and never narrate what you are doing in words; anything you "
    "cannot say out loud does not belong in the reply."
)


def _voice_system_prompt() -> str:
    """The persona plus whatever project memory can be reached right now."""
    parts = [prompts.VOICE_SYSTEM]
    if _actions_enabled():
        parts.append(prompts.VOICE_CAPABILITIES)
    if "chatterbox" in _env("SKIPPY_VOICE_TTS", "kokoro").lower():
        parts.append(_EXPRESSIVE_TAGS_NOTE)
    memory = _open_memory()
    if memory is not None:
        try:
            context = memory.opening_context()
            if context:
                parts.append(context)
        except Exception as exc:
            logger.warning("Could not read project memory: %s", exc)
    return "\n\n".join(parts)


def _open_memory():
    """Project memory for the configured workspace roots, or None.

    None rather than an exception: the NAS being unmounted should cost the
    session its continuity, not its existence.
    """
    try:
        import skippy_memory
        import skippy_paths

        roots = skippy_paths.configured_workspace_roots()
        return skippy_memory.open_project(workspace_roots=roots or [])
    except Exception as exc:
        logger.warning("Project memory unavailable: %s", exc)
        return None


def _search_memory(query: str, limit: int = 4) -> str:
    """A few matching session summaries and decisions, formatted to be read aloud.

    Naive substring scoring over the memory files. The corpus is dozens of
    short records, not a search problem; the model reading the results does
    the actual relevance judgment.
    """
    memory = _open_memory()
    if memory is None:
        return ""
    words = [w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) > 2]
    scored: List[tuple] = []
    try:
        for session in memory.sessions(limit=40):
            text = f"{session.get('task', '')} {session.get('summary', '')}"
            hits = sum(1 for w in words if w in text.lower())
            if hits or not words:
                scored.append((hits, f"Session ({session.get('mode', '?')}): {_first_sentences(text, 3, 500)}"))
        for decision in memory.decisions():
            body = decision.get("text", "")
            hits = sum(1 for w in words if w in body.lower())
            if hits:
                title = decision.get("front", {}).get("title", "") or "untitled"
                scored.append((hits, f"Decision '{title}': {_first_sentences(body, 3, 500)}"))
    except Exception as exc:
        logger.warning("Memory search failed: %s", exc)
        return ""
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return "\n".join(entry for _, entry in scored[:limit])


async def _save_session(history: List[dict]) -> Optional[str]:
    """Summarize the conversation and record it as a voice-mode session.

    A brainstorm that only ever existed as sound is the failure mode this whole
    feature was warned about; two sentences of summary in project memory is what
    makes tomorrow's coding session able to say "you talked about this."
    """
    turns = [m for m in history if m["role"] in ("user", "assistant")]
    if len(turns) < 2:
        return None

    conversation = "\n".join(f"{m['role']}: {m['content']}" for m in turns)
    try:
        summary = await skippy_llm.query_text(
            [{"role": "user", "content": (
                "Summarize this spoken brainstorming session in under 120 words. "
                "Capture the ideas raised, any direction chosen, and open questions. "
                "Facts only, no narration.\n\n" + conversation[-8000:]
            )}],
            role=_env("SKIPPY_VOICE_ROLE", "fast"),
            temp=0.2,
        )
    except Exception as exc:
        logger.warning("Could not summarize the voice session: %s", exc)
        summary = conversation[:1500]

    memory = _open_memory()
    if memory is None:
        return None
    try:
        session_id = memory.record_session(
            task=turns[0]["content"][:500],
            status="completed",
            summary=summary,
            mode="voice",
        )
        logger.info("Voice session saved as %s", session_id)
        return session_id
    except Exception as exc:
        logger.warning("Could not record the voice session: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Actions: the voice lane's hands
#
# The conversation stays streaming and tool-free — that is what keeps the
# reply under a second. Actions run beside it: a cheap keyword gate decides
# whether an utterance *might* be a request to do something, a cold routing
# call turns it into JSON, and the result comes back as a system note the
# persona reads out in its own voice. Long work (agent runs, the heavy
# model) is dispatched into the background and announced when it lands, so
# "run the tests" costs the user one spoken acknowledgment, not a silence.
# ---------------------------------------------------------------------------

def _actions_enabled() -> bool:
    return _env("SKIPPY_VOICE_ACTIONS", "1") not in ("0", "false", "no")


# Deliberately broad: a miss here only means Skippy answers with talk instead
# of action, and the user rephrases. The router call it gates is what decides.
_ACTION_WORDS = re.compile(
    r"\b(task|start|run|running|ran|cancel|stop|status|progress|done|finish|"
    r"build|test|tests|fix|implement|refactor|rewrite|rename|debug|deploy|"
    r"code|coding|repo|repository|file|firmware|reverse|memory|remember|"
    r"recall|decid\w+|decision\w*|discussed|heavy|big model|"
    r"think hard|deep dive)\b",
    re.IGNORECASE,
)


def wants_action(text: str) -> bool:
    return bool(_ACTION_WORDS.search(text))


def parse_route(raw: str) -> Optional[dict]:
    """The router's JSON line, or None for anything malformed.

    Malformed output downgrades to conversation rather than erroring: the
    router is a small model at temperature zero, and the cost of it having a
    bad day should be a chatty answer, not a broken turn.
    """
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        return None
    try:
        decision = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(decision, dict):
        return None
    action = str(decision.get("action") or "none").strip().lower()
    if action in ("", "none"):
        return None
    decision["action"] = action
    return decision


def _first_sentences(text: str, limit: int = 2, max_chars: int = 400) -> str:
    parts = re.split(r"(?<=[.!?…])\s+", " ".join(str(text).split()))
    return " ".join(parts[:limit])[:max_chars].strip()


class _TaskTap:
    """Impersonates a factory client so a voice-dispatched run reports here.

    TaskRunner streams events to hub.active_connections[client_id]; a voice
    session has no factory socket, so this stands in for one. Only send_json
    is ever called on a connection by the runner's send().
    """

    def __init__(self, session: "VoiceSession"):
        self._session = session
        self.last_event = ""
        self.summary = ""
        self.finished = False

    async def send_json(self, payload: dict) -> None:
        kind = payload.get("type")
        text = str(payload.get("content") or payload.get("text") or "").strip()
        if kind == "chat" and text:
            self.summary = text
        elif kind == "done":
            if self.finished:
                return
            self.finished = True
            outcome = _first_sentences(self.summary) or "It finished, but left no summary."
            self._session.announce(f"That task you asked for is done. {outcome}")
        elif text:
            self.last_event = text[:300]


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

class VoiceSession:
    """One connected voice client: endpointing, turn-taking, interruption."""

    MAX_HISTORY = 24  # message pairs beyond this are dropped oldest-first

    def __init__(self, websocket: WebSocket, send_json: Callable, send_bytes: Callable):
        self.websocket = websocket
        self._send_json = send_json
        self._send_bytes = send_bytes
        self.endpointer = Endpointer(build_vad())
        self.history: List[dict] = []
        self.system_prompt: Optional[str] = None
        self.duplex = True
        self._responder: Optional[asyncio.Task] = None
        self._speaking = False
        # True from the moment an utterance closes until its responder task
        # exists. During that window (mostly STT) the endpointer reads idle
        # and _responding() is False, so without this flag an announcement
        # would call the room quiet, start talking, and then have its
        # _responder slot overwritten by the turn's own task — leaving it
        # speaking over the reply with no way to barge in on it.
        self._turn_active = False
        # Action lane state. The client id is unique per session so two open
        # voice clients (the Mac and the phone) each get their own task slot
        # and their own completion announcements.
        self.client_id = f"voice-{os.urandom(3).hex()}"
        self._tap: Optional[_TaskTap] = None
        self._background: List[asyncio.Task] = []
        # The research budget and cache for this conversation, and the decision behind
        # a check currently in flight. Per session, because that is the scope a budget
        # means anything in.
        self.research = skippy_gate.Conversation()
        self._checking: Optional[skippy_gate.Decision] = None

    # -- inbound ----------------------------------------------------------

    async def handle_audio(self, data: bytes) -> None:
        for event, payload in self.endpointer.feed(data):
            if event == "speech_start":
                if self._responding():
                    if not self.duplex:
                        # Half-duplex hardware cannot be hearing the user while
                        # it plays audio; this is the room talking, not them.
                        self.endpointer.reset(keep_vad_state=True)
                    # Full duplex: not a barge-in yet. On built-in speakers a
                    # single VAD frame is as often Skippy's own voice leaking
                    # into the mic as it is the user; cancelling here cut
                    # replies off mid-sentence with nobody talking. Barge-in
                    # waits for speech_confirmed.
                    continue
                await self._send_json({"type": "partial", "text": ""})
            elif event == "speech_confirmed":
                if self._responding() and self.duplex:
                    await self._cancel_response()  # barge-in
                    await self._send_json({"type": "partial", "text": ""})
            elif event == "utterance":
                await self._on_utterance(payload)

    async def handle_control(self, data: dict) -> None:
        kind = data.get("type")
        if kind == "start":
            self.duplex = bool(data.get("duplex", True))
            logger.info("Voice session start (duplex=%s)", self.duplex)
        elif kind == "interrupt":
            await self._cancel_response()
        elif kind == "end":
            await self._cancel_response()
            session_id = await _save_session(self.history)
            await self._send_json({"type": "session_saved", "session_id": session_id})
            self.history = []

    # -- one turn ---------------------------------------------------------

    async def _on_utterance(self, pcm: bytes) -> None:
        self._turn_active = True
        try:
            await self._cancel_response()
            await self._send_json({"type": "state", "state": "thinking"})
            heard_at = time.monotonic()

            def transcribe() -> str:
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                    path = tmp.name
                try:
                    write_wav(path, pcm)
                    return get_stt().transcribe(path)
                finally:
                    os.unlink(path)

            try:
                text = await asyncio.to_thread(transcribe)
            except Exception as exc:
                logger.exception("STT failed")
                await self._send_json({"type": "error", "message": f"Transcription failed: {exc}"})
                await self._send_json({"type": "state", "state": "listening"})
                return

            stt_ms = int(1000 * (time.monotonic() - heard_at))
            if not text or len(text.strip(" .")) < 2:
                await self._send_json({"type": "state", "state": "listening"})
                return

            await self._send_json({"type": "transcript", "text": text})
            self.history.append({"role": "user", "content": text})
            self._responder = asyncio.create_task(self._respond(heard_at, stt_ms))
        finally:
            # Cleared only after the responder task (if any) is in its slot,
            # so _quiet() never has a gap between "turn ended" and "reply
            # visible". Once the task exists, _responding() carries the guard.
            self._turn_active = False

    async def _respond(self, heard_at: float, stt_ms: int) -> None:
        """Stream the model, speak sentence by sentence, report the numbers."""
        if self.system_prompt is None:
            self.system_prompt = await asyncio.to_thread(_voice_system_prompt)

        # The action lane runs before the reply is generated, so the persona
        # reports what actually happened instead of improvising what might.
        note = None
        latest = self.history[-1]["content"] if self.history else ""
        if _actions_enabled() and latest and wants_action(latest):
            note = await self._route_and_act()

        # The research gate runs only when the utterance was not an action. Two cold
        # classifier calls before a spoken reply would cost the second of latency this
        # lane exists to protect, and an utterance that was a request to do something is
        # not also a question to go and check.
        checking = False
        if note is None and latest:
            checking = await self._maybe_check(latest)
            if checking:
                note = skippy_gate.acknowledgment(self._checking, spoken=True)

        trimmed = self.history[-2 * self.MAX_HISTORY:]
        messages = [{"role": "system", "content": self.system_prompt}] + trimmed
        if note:
            messages.append({"role": "system", "content": note})

        chunker = SentenceChunker()
        spoken: List[str] = []
        seen: set = set()
        max_sentences = _env_int("SKIPPY_VOICE_MAX_SENTENCES", 8)
        first_token_ms: Optional[int] = None
        first_audio_ms: Optional[int] = None
        sentences = 0

        def should_stop(sentence: str) -> bool:
            """True when the model has started looping or lecturing.

            Both cuts exist because of the first live test: the fast model at
            this endpoint ignores repetition_penalty and, at greedy-looking
            sampling, repeated the same three sentences fifteen times. A
            duplicate sentence within one reply is never right out loud, and
            neither is a ninth sentence — this is a conversation.
            """
            nonlocal sentences
            key = re.sub(r"[^a-z0-9]+", " ", sentence.lower()).strip()
            if key and key in seen:
                return True
            seen.add(key)
            sentences += 1
            return sentences > max_sentences

        async def speak(text: str) -> None:
            tts = get_tts()
            speakable = strip_stage_directions(
                clean_for_tts(text), getattr(tts, "tags", frozenset())
            )
            if not speakable:
                return
            got_audio = False

            async def deliver(pcm: bytes) -> None:
                nonlocal got_audio, first_audio_ms
                if not pcm:
                    return
                if not self._speaking:
                    self._speaking = True
                    await self._send_json({"type": "audio_start", "rate": out_rate()})
                    await self._send_json({"type": "state", "state": "speaking"})
                if first_audio_ms is None:
                    first_audio_ms = int(1000 * (time.monotonic() - heard_at))
                if not got_audio:
                    got_audio = True
                    await self._send_json({"type": "reply", "text": text})
                for i in range(0, len(pcm), AUDIO_CHUNK_BYTES):
                    await self._send_bytes(pcm[i:i + AUDIO_CHUNK_BYTES])

            # Engines that generate slower than Kokoro (Chatterbox) can hand
            # over audio while they are still generating the rest; forward it
            # as it lands rather than sitting on it.
            if getattr(tts, "can_stream", False):
                async for pcm in _iterate_in_thread(lambda: tts.synthesize_stream(speakable)):
                    await deliver(pcm)
            else:
                await deliver(await asyncio.to_thread(tts.synthesize, speakable))
            if got_audio:
                spoken.append(text)

        # Prosody grouping: the first sentence is synthesized alone, because it
        # is the latency the user hears. Everything after rides in ~GROUP_CHARS
        # batches — Kokoro resets its intonation at every synthesis call, so
        # sentence-at-a-time delivery reads fine and *sounds* like a list being
        # dictated. Larger units restore the natural downdrift across sentences.
        GROUP_CHARS = 200
        pending: List[str] = []

        async def flush_group() -> None:
            if pending:
                await speak(" ".join(pending))
                pending.clear()

        async def enqueue(sentence: str) -> None:
            if not spoken and not pending:
                await speak(sentence)
                return
            pending.append(sentence)
            if sum(len(s) for s in pending) >= GROUP_CHARS:
                await flush_group()

        try:
            stopped_early = False
            async for token in stream_chat(messages):
                if first_token_ms is None:
                    first_token_ms = int(1000 * (time.monotonic() - heard_at))
                for sentence in chunker.push(token):
                    if should_stop(sentence):
                        stopped_early = True
                        break
                    await enqueue(sentence)
                if stopped_early:
                    break
            if not stopped_early:
                tail = chunker.flush()
                if tail and not should_stop(tail):
                    await enqueue(tail)
            await flush_group()

            # History holds what was actually said, not what the model would
            # have gone on to generate.
            reply = " ".join(spoken).strip()
            if reply:
                self.history.append({"role": "assistant", "content": reply})
            # The second layer, behind the delivered reply so it costs no latency: ask
            # the model that just answered whether what it said should be checked.
            # Skipped when a check is already running on this turn — grading an answer
            # that was deliberately hedged would start a second run on the same thing.
            if reply and not checking:
                self._background.append(
                    asyncio.create_task(self._check_after(latest, reply))
                )
            await self._send_json({
                "type": "metrics",
                "stt_ms": stt_ms,
                "llm_first_token_ms": first_token_ms,
                "first_audio_ms": first_audio_ms,
                "total_ms": int(1000 * (time.monotonic() - heard_at)),
                "sentences": sentences,
            })
            logger.info(
                "Turn: stt=%sms first_token=%sms first_audio=%sms total=%sms",
                stt_ms, first_token_ms, first_audio_ms,
                int(1000 * (time.monotonic() - heard_at)),
            )
        except asyncio.CancelledError:
            # Interrupted mid-reply. Keep what was actually said, marked as cut
            # off, so the model does not believe it delivered the unspoken half.
            said = " ".join(spoken).strip()
            if said:
                self.history.append({"role": "assistant", "content": said + " [interrupted]"})
            raise
        except Exception as exc:
            logger.exception("Voice response failed")
            await self._send_json({"type": "error", "message": str(exc)})
        finally:
            if self._speaking:
                self._speaking = False
                await self._send_json({"type": "audio_end"})
            await self._send_json({"type": "state", "state": "listening"})

    # -- actions ------------------------------------------------------------

    @staticmethod
    def _factory():
        """The hub and runner, resolved late.

        skippy_factory imports this module at startup; importing it back at
        module scope would be a cycle. By the time a session is routing an
        utterance, the factory has long since finished loading.
        """
        import skippy_factory
        return skippy_factory.hub, skippy_factory.runner

    async def _route_and_act(self) -> Optional[str]:
        """Ask the router what the utterance wants; do it; return the note.

        None means "just conversation" — either the router said so or it
        failed, and in both cases the right fallback is a normal reply.
        """
        latest = self.history[-1]["content"]
        recent = "\n".join(
            f"{m['role']}: {m['content']}" for m in self.history[-5:-1]
        ) or "(none)"
        try:
            raw = await skippy_llm.query_text(
                [
                    {"role": "system", "content": prompts.VOICE_ROUTER},
                    {"role": "user", "content": f"Recent turns:\n{recent}\n\nLatest utterance: {latest}"},
                ],
                role=_env("SKIPPY_VOICE_ROLE", "voice"),
                temp=0.0,
                max_tokens=200,
                attempts=1,
                timeout=30.0,
            )
        except Exception as exc:
            logger.warning("Voice action router unavailable: %s", exc)
            return None
        decision = parse_route(raw)
        if decision is None:
            return None
        logger.info("Voice action: %s", decision)
        try:
            return await self._perform(decision)
        except Exception as exc:
            logger.exception("Voice action failed")
            return (
                f"SYSTEM NOTE: you tried to {decision.get('action')} but it failed "
                f"internally ({exc}). Tell the user plainly, in one sentence."
            )

    async def _perform(self, decision: dict) -> Optional[str]:
        action = decision["action"]
        hub, runner = self._factory()

        if action == "start_task":
            text = str(decision.get("text") or "").strip()
            if not text:
                return None
            if runner.is_running(self.client_id):
                return (
                    "SYSTEM NOTE: a task is already running for this session, so you "
                    "did NOT start a new one. Offer to cancel it or wait."
                )
            if self._tap is None:
                self._tap = _TaskTap(self)
                hub.active_connections[self.client_id] = self._tap
            self._tap.finished = False
            self._tap.summary = ""
            wire_mode = "RE" if str(decision.get("mode", "")).lower() == "re" else "Agent"
            await runner.start(self.client_id, {"text": text, "mode": wire_mode})
            return (
                f"SYSTEM NOTE: you just started a real agent task: \"{text}\" "
                f"(mode {wire_mode}). It runs in the background and you will announce "
                "out loud when it finishes. Acknowledge in one confident sentence; do "
                "not invent progress or results."
            )

        if action == "task_status":
            if runner.is_running(self.client_id):
                latest = (self._tap.last_event if self._tap else "") or "no update yet"
                return (
                    f"SYSTEM NOTE: the task is still running. Latest activity: "
                    f"{latest}. Report that briefly."
                )
            summary = (self._tap.summary if self._tap else "")
            if summary:
                return (
                    f"SYSTEM NOTE: the last task finished. Its summary: "
                    f"{_first_sentences(summary, 3, 600)}. Report that briefly."
                )
            return "SYSTEM NOTE: no task is running and none has finished this session."

        if action == "cancel_task":
            stopped = runner.cancel(self.client_id)
            return (
                "SYSTEM NOTE: you cancelled the running task." if stopped
                else "SYSTEM NOTE: there was no running task to cancel."
            )

        if action == "search_memory":
            query = str(decision.get("query") or "").strip()
            found = await asyncio.to_thread(_search_memory, query)
            if not found:
                return (
                    f"SYSTEM NOTE: you searched project memory for \"{query}\" and "
                    "found nothing relevant. Say so honestly."
                )
            return (
                f"SYSTEM NOTE: project memory results for \"{query}\":\n{found}\n"
                "Answer from these notes, briefly, and flag anything that might be stale."
            )

        if action == "ask_heavy":
            question = str(decision.get("question") or "").strip()
            if not question:
                return None
            job = asyncio.create_task(self._ask_heavy(question))
            self._background.append(job)
            return (
                "SYSTEM NOTE: you handed that question to the heavy model. It is slow — "
                "a minute or more — and you will announce the answer out loud when it "
                "arrives. Say that in one sentence; do not attempt the answer yourself now."
            )

        return None

    # -- checking his own work ----------------------------------------------
    #
    # Same shape as ask_heavy below: acknowledge in the reply, work in the background,
    # speak up when it lands. Nothing here ever makes the user wait — a spoken
    # conversation that stops for ten seconds while Skippy reads the internet is a
    # broken conversation, however good the eventual answer is.

    async def _maybe_check(self, text: str) -> bool:
        """Decide whether to check this turn, and start doing it. See skippy_gate."""
        decision = await skippy_gate.pre_answer(
            text, self.history[:-1], role=_env("SKIPPY_VOICE_ROLE", "voice")
        )
        return self._start_check(decision)

    async def _check_after(self, text: str, reply: str) -> None:
        decision = await skippy_gate.post_answer(
            text, reply, role=_env("SKIPPY_VOICE_ROLE", "voice")
        )
        self._start_check(decision)

    def _start_check(self, decision) -> bool:
        """Begin a background check, unless this conversation cannot afford one.

        False means the answer stands unchecked and the persona is told nothing —
        promising to go and verify something and then not doing it is worse than never
        having offered.
        """
        if not decision:
            return False
        conversation = self.research
        if conversation.key(decision.question) in conversation.in_flight:
            return False
        if conversation.recall(decision.question) is None and not conversation.allows():
            logger.info("Research budget spent for this session; answering unchecked.")
            return False
        logger.info("Checking %r (%s: %s)", decision.question, decision.layer, decision.reason)
        self._checking = decision
        self._background.append(asyncio.create_task(self._deliver_check(decision)))
        return True

    async def _deliver_check(self, decision) -> None:
        try:
            result = await skippy_gate.check(
                decision.question,
                self.research,
                roots=skippy_paths.configured_workspace_roots(),
            )
        except Exception:
            logger.exception("Background check failed")
            return
        spoken = skippy_gate.report(result, spoken=True)
        if not spoken:
            return
        if result.error:
            self.announce(spoken)
            return
        # Out loud, the answer is trimmed to something a person can listen to. The full
        # write-up with its citations is in the brief, and saying where it is is more
        # use than reading forty seconds of URLs aloud.
        self.announce(
            f"I checked that. {_first_sentences(spoken, 3, 600)} "
            "The sources are in the brief if you want them."
        )

    async def _ask_heavy(self, question: str) -> None:
        try:
            answer = await skippy_llm.query_text(
                [{"role": "user", "content": (
                    "Answer for a spoken conversation: plain prose, no markdown, "
                    "at most four sentences.\n\n" + question
                )}],
                role="heavy",
                temp=0.3,
                timeout=600.0,
            )
            spoken = _first_sentences(answer, 4, 700) or "It came back empty."
            self.announce(f"The heavy model came back on your question. {spoken}")
        except Exception as exc:
            logger.warning("Heavy model question failed: %s", exc)
            self.announce("Bad news: the heavy model choked on that question, so no deep answer this time.")

    # -- announcements ------------------------------------------------------

    def announce(self, text: str) -> None:
        """Say something outside a turn — a task finished, the heavy model answered.

        Fire-and-forget by design: callers (the task tap, the heavy job) are
        not in a position to await speech.
        """
        self._background.append(asyncio.create_task(self._announce(text)))

    def _quiet(self) -> bool:
        """True when nothing is speaking, being heard, or between the two.

        The middle condition is the subtle one: from utterance close to
        responder creation (mostly STT time) the other two read false, and an
        announcement started in that window would talk over the coming reply.
        """
        return (
            not self._responding()
            and not getattr(self.endpointer, "in_speech", False)
            and not self._turn_active
        )

    async def _announce(self, text: str) -> None:
        # Wait for a quiet moment: never talk over a reply in flight or over
        # the user mid-sentence. If quiet never comes within five minutes,
        # drop it — the result is still in history-adjacent state (task
        # summary, memory), and a stale announcement is worse than none.
        for _ in range(600):
            if self._quiet():
                break
            await asyncio.sleep(0.5)
        else:
            return
        self._responder = asyncio.create_task(self._speak_only(text))

    async def _speak_only(self, text: str) -> None:
        """TTS one prepared line, with the same wire protocol as a turn."""
        tts = get_tts()
        speakable = strip_stage_directions(
            clean_for_tts(text), getattr(tts, "tags", frozenset())
        )
        if not speakable:
            return
        try:
            await self._send_json({"type": "audio_start", "rate": out_rate()})
            await self._send_json({"type": "state", "state": "speaking"})
            await self._send_json({"type": "reply", "text": text})
            self._speaking = True
            if getattr(tts, "can_stream", False):
                async for pcm in _iterate_in_thread(lambda: tts.synthesize_stream(speakable)):
                    for i in range(0, len(pcm), AUDIO_CHUNK_BYTES):
                        await self._send_bytes(pcm[i:i + AUDIO_CHUNK_BYTES])
            else:
                pcm = await asyncio.to_thread(tts.synthesize, speakable)
                for i in range(0, len(pcm), AUDIO_CHUNK_BYTES):
                    await self._send_bytes(pcm[i:i + AUDIO_CHUNK_BYTES])
            # The persona said this; it must remember having said it.
            self.history.append({"role": "assistant", "content": text})
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Announcement failed")
        finally:
            self._speaking = False
            await self._send_json({"type": "audio_end"})
            await self._send_json({"type": "state", "state": "listening"})

    # -- interruption -----------------------------------------------------

    def _responding(self) -> bool:
        return self._responder is not None and not self._responder.done()

    async def _cancel_response(self) -> None:
        if not self._responding():
            return
        self._responder.cancel()
        try:
            await self._responder
        except (asyncio.CancelledError, Exception):
            pass
        self._responder = None
        # Told explicitly: audio already sent is sitting in the client's jitter
        # buffer, and only the client can throw it away.
        await self._send_json({"type": "audio_cancel"})

    async def close(self) -> None:
        if self._responding():
            self._responder.cancel()
            try:
                await self._responder
            except (asyncio.CancelledError, Exception):
                pass
        # Pending announcements have no ear to land in; the dispatched agent
        # task itself keeps running under the runner and records its outcome
        # in project memory, so nothing of substance is lost.
        for job in self._background:
            job.cancel()
        if self._tap is not None:
            try:
                hub, _ = self._factory()
                if hub.active_connections.get(self.client_id) is self._tap:
                    del hub.active_connections[self.client_id]
            except Exception:
                pass
        await _save_session(self.history)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

def _authorized(token: Optional[str]) -> bool:
    """True if this connection may proceed.

    When SKIPPY_VOICE_TOKEN is set it must match — that is the whole mechanism
    that makes a non-loopback bind for the Core2 defensible. When it is unset,
    loopback callers are the only ones who can reach us anyway under the
    default bind, so the connection is allowed and non-loopback operators get
    the warning at boot instead.
    """
    expected = os.environ.get("SKIPPY_VOICE_TOKEN", "").strip()
    if not expected:
        return True
    return bool(token) and token == expected


@router.websocket("/ws/voice")
async def voice_endpoint(websocket: WebSocket, token: Optional[str] = None):
    if not _authorized(token):
        # 1008 = policy violation. Closed before accept so an unauthenticated
        # LAN scanner learns nothing, not even the protocol.
        await websocket.close(code=1008)
        return

    await websocket.accept()
    logger.info("Voice client connected: %s", websocket.client)

    # Warm the engines now, during the connection's silence, rather than on the
    # first utterance. Loading is not enough: the MLX backends compile kernels
    # on their first real inference (measured 12.7s for Kokoro-82M-bf16 cold
    # against 88ms warm), so the prewarm pushes one tiny job through each.
    def _warm() -> None:
        stt = get_stt()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            path = tmp.name
        try:
            write_wav(path, b"\x00" * IN_RATE)  # one second of silence
            stt.transcribe(path)
        finally:
            os.unlink(path)
        get_tts().synthesize("Ready.")

    async def prewarm() -> None:
        try:
            await asyncio.to_thread(_warm)
            logger.info("Voice engines warm.")
        except Exception as exc:
            logger.warning("Engine prewarm failed (the first turn will pay it): %s", exc)

    prewarm_task = asyncio.create_task(prewarm())

    # A lock per session: sends come from both the reader loop and the
    # responder task, and interleaving two coroutines' writes corrupts frames.
    send_lock = asyncio.Lock()

    async def send_json(payload: dict) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def send_bytes(data: bytes) -> None:
        async with send_lock:
            await websocket.send_bytes(data)

    session = VoiceSession(websocket, send_json, send_bytes)
    await send_json({"type": "state", "state": "listening"})

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                await session.handle_audio(message["bytes"])
            elif message.get("text"):
                try:
                    data = json.loads(message["text"])
                except json.JSONDecodeError:
                    continue
                await session.handle_control(data)
    except WebSocketDisconnect:
        pass
    finally:
        logger.info("Voice client disconnected: %s", websocket.client)
        prewarm_task.cancel()
        await session.close()
