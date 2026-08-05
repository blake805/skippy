"""Time the voice pipeline's stages, per candidate model.

The live pipeline already reports per-turn latency over the websocket (the
"metrics" message). This harness answers the other question: which STT, TTS
and brain to configure in the first place. It runs each candidate directly —
no websocket, no VAD — and prints load time and inference time per stage, so a
bake-off is one command per contender rather than an afternoon of edits.

Examples:

    # The defaults that ship in this repo
    python clients/voice_bench.py --wav sample.wav

    # STT contenders (mlx-audio must be installed for mlx: specs)
    python clients/voice_bench.py --wav sample.wav \
        --stt whisper --stt whisper:small --stt mlx:mlx-community/parakeet-tdt-0.6b-v3

    # TTS contenders
    python clients/voice_bench.py \
        --tts kokoro --tts mlx:mlx-community/Kokoro-82M-bf16

    # Brain roles (whatever skippy_llm's registry maps them to)
    python clients/voice_bench.py --role fast --role heavy

Record a test WAV with: python clients/voice_bench.py --record sample.wav
Speak for a few seconds; it stops on Ctrl-C. 16 kHz mono, same as the wire.
"""

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import skippy_voice  # noqa: E402


BENCH_TEXT = (
    "Aluminum would work well for the enclosure. It machines easily, "
    "sheds heat, and you already have stock on the shelf."
)
BENCH_PROMPT = [
    {"role": "system", "content": "You are Skippy, brainstorming out loud. Answer in two sentences."},
    {"role": "user", "content": "What if we milled the enclosure out of aluminum instead of printing it?"},
]


def timed(label: str, fn):
    start = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - start
    print(f"    {label:24} {elapsed * 1000:8.0f} ms")
    return result, elapsed


def bench_stt(spec: str, wav_path: str):
    print(f"\nSTT: {spec}")
    os.environ["SKIPPY_VOICE_STT"] = spec
    try:
        engine, _ = timed("load", skippy_voice.build_stt)
        (text, _) = timed("transcribe (cold)", lambda: engine.transcribe(wav_path))
        timed("transcribe (warm)", lambda: engine.transcribe(wav_path))
        print(f"    text: {text[:100]!r}")
    except Exception as exc:
        print(f"    FAILED: {exc}")


def bench_tts(spec: str):
    print(f"\nTTS: {spec}")
    os.environ["SKIPPY_VOICE_TTS"] = spec
    try:
        engine, _ = timed("load", skippy_voice.build_tts)
        first_sentence = BENCH_TEXT.split(". ")[0] + "."
        (pcm, cold) = timed("first sentence (cold)", lambda: engine.synthesize(first_sentence))
        timed("first sentence (warm)", lambda: engine.synthesize(first_sentence))
        (full, _) = timed("full reply", lambda: engine.synthesize(BENCH_TEXT))
        rate = skippy_voice.out_rate()
        audio_seconds = len(full) / 2 / rate
        print(f"    audio: {audio_seconds:.1f}s of speech at {rate} Hz "
              f"(first-sentence RTF {cold / max(0.01, len(pcm) / 2 / rate):.2f})")
    except Exception as exc:
        print(f"    FAILED: {exc}")


async def bench_role(role: str):
    print(f"\nBrain role: {role}")
    start = time.perf_counter()
    first = None
    tokens = 0
    try:
        async for token in skippy_voice.stream_chat(BENCH_PROMPT, role=role):
            if first is None:
                first = time.perf_counter() - start
            tokens += 1
        total = time.perf_counter() - start
        print(f"    first token          {first * 1000 if first else 0:8.0f} ms")
        print(f"    full reply           {total * 1000:8.0f} ms  ({tokens} chunks)")
    except Exception as exc:
        print(f"    FAILED: {exc}")


def record(path: str):
    import sounddevice as sd

    print("Recording 16 kHz mono. Speak, then Ctrl-C to stop.")
    chunks = []

    def cb(indata, frames, time_info, status):
        chunks.append(bytes(indata))

    try:
        with sd.RawInputStream(samplerate=16000, channels=1, dtype="int16", callback=cb):
            while True:
                time.sleep(0.1)
    except KeyboardInterrupt:
        pass
    skippy_voice.write_wav(path, b"".join(chunks))
    print(f"Wrote {path} ({sum(len(c) for c in chunks) / 32000:.1f}s)")


def main():
    parser = argparse.ArgumentParser(description="Voice pipeline bake-off harness")
    parser.add_argument("--wav", help="16 kHz mono WAV to transcribe")
    parser.add_argument("--record", metavar="PATH", help="record a test WAV and exit")
    parser.add_argument("--stt", action="append", default=[],
                        help="STT spec: whisper | whisper:<size> | mlx:<model_id> (repeatable)")
    parser.add_argument("--tts", action="append", default=[],
                        help="TTS spec: kokoro | mlx:<model_id> (repeatable)")
    parser.add_argument("--role", action="append", default=[],
                        help="brain role from skippy_llm's registry (repeatable)")
    args = parser.parse_args()

    if args.record:
        record(args.record)
        return

    if not (args.stt or args.tts or args.role):
        args.stt = ["whisper"] if args.wav else []
        args.tts = ["kokoro"]
        args.role = ["fast"]

    for spec in args.stt:
        if not args.wav:
            print("STT benchmarks need --wav (record one with --record sample.wav)")
            break
        bench_stt(spec, args.wav)
    for spec in args.tts:
        bench_tts(spec)
    for role in args.role:
        asyncio.run(bench_role(role))


if __name__ == "__main__":
    main()
