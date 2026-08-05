"""Talk to Skippy from the Mac, before any hardware exists.

This is the development client for the `/ws/voice` lane: it speaks the same
wire protocol the M5Stack Core2 firmware speaks, so the whole pipeline — VAD,
STT, the brain, TTS, interruption — can be built and latency-tuned at the desk.
Unlike the Core2, a Mac is full duplex: the microphone keeps streaming while
Skippy talks, which is what lets the server's own VAD implement real barge-in.
Start talking over him and he stops.

Usage:

    python clients/voice_client.py                       # localhost:8000
    python clients/voice_client.py --url ws://192.168.1.151:8000/ws/voice \
        --token "$SKIPPY_VOICE_TOKEN"

Requires sounddevice (`pip install sounddevice`), which needs no system setup
on macOS. Ctrl-C to quit; the server saves the session to project memory on
disconnect.
"""

import argparse
import asyncio
import contextlib
import json
import queue
import sys

import sounddevice as sd
import websockets

IN_RATE = 16_000
BLOCK = 512  # samples per callback, matches the server's VAD framing


def parse_args():
    parser = argparse.ArgumentParser(description="Skippy voice client")
    parser.add_argument("--url", default="ws://127.0.0.1:8000/ws/voice")
    parser.add_argument("--token", default="", help="SKIPPY_VOICE_TOKEN, if the server sets one")
    parser.add_argument("--half-duplex", action="store_true",
                        help="mimic the Core2: mute the mic while Skippy speaks")
    return parser.parse_args()


class Player:
    """A jitter-buffered output stream that can be flushed on interruption."""

    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._leftover = b""
        self._stream = None
        self.rate = IN_RATE
        self.playing = False

    def start(self, rate: int):
        if self._stream is not None and rate == self.rate:
            return
        self.stop()
        self.rate = rate
        self._stream = sd.RawOutputStream(
            samplerate=rate, channels=1, dtype="int16",
            blocksize=BLOCK, callback=self._callback,
        )
        self._stream.start()

    def _callback(self, outdata, frames, time_info, status):
        needed = frames * 2
        buf = self._leftover
        while len(buf) < needed:
            try:
                buf += self._queue.get_nowait()
            except queue.Empty:
                break
        chunk, self._leftover = buf[:needed], buf[needed:]
        outdata[: len(chunk)] = chunk
        if len(chunk) < needed:
            outdata[len(chunk):] = b"\x00" * (needed - len(chunk))
        self.playing = bool(chunk)

    def feed(self, pcm: bytes):
        self._queue.put(pcm)

    def flush(self):
        """Throw away everything not yet played. This is what audio_cancel means."""
        with contextlib.suppress(queue.Empty):
            while True:
                self._queue.get_nowait()
        self._leftover = b""
        self.playing = False

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


async def run(args):
    url = args.url + (f"?token={args.token}" if args.token else "")
    mic_queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()
    player = Player()
    state = {"speaking": False}

    def on_mic(indata, frames, time_info, status):
        if status:
            print(f"[mic] {status}", file=sys.stderr)
        if args.half_duplex and state["speaking"]:
            return  # the Core2 cannot hear itself talk; optionally, neither do we
        loop.call_soon_threadsafe(mic_queue.put_nowait, bytes(indata))

    mic = sd.RawInputStream(
        samplerate=IN_RATE, channels=1, dtype="int16", blocksize=BLOCK, callback=on_mic,
    )

    async with websockets.connect(url, max_size=None) as ws:
        await ws.send(json.dumps({"type": "start", "duplex": not args.half_duplex}))
        print("Connected. Just talk; Ctrl-C to quit.")
        mic.start()

        async def pump_mic():
            while True:
                await ws.send(await mic_queue.get())

        async def pump_server():
            async for message in ws:
                if isinstance(message, bytes):
                    player.feed(message)
                    continue
                data = json.loads(message)
                kind = data.get("type")
                if kind == "state":
                    print(f"\r[{data['state']:9}] ", end="", flush=True)
                elif kind == "transcript":
                    print(f"\nYou: {data['text']}")
                elif kind == "reply":
                    print(f"Skippy: {data['text']}")
                elif kind == "audio_start":
                    state["speaking"] = True
                    player.start(int(data.get("rate", IN_RATE)))
                elif kind == "audio_end":
                    state["speaking"] = False
                elif kind == "audio_cancel":
                    state["speaking"] = False
                    player.flush()
                    print("\n[interrupted]")
                elif kind == "metrics":
                    print(
                        f"[latency] stt={data.get('stt_ms')}ms  "
                        f"first_token={data.get('llm_first_token_ms')}ms  "
                        f"first_audio={data.get('first_audio_ms')}ms  "
                        f"total={data.get('total_ms')}ms"
                    )
                elif kind == "error":
                    print(f"\n[server error] {data.get('message')}", file=sys.stderr)

        try:
            await asyncio.gather(pump_mic(), pump_server())
        finally:
            mic.stop()
            mic.close()
            player.stop()


def main():
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
