"""One-shot websocket client to smoke-test the skippy_factory pipeline."""
import asyncio
import json
import sys

import websockets


async def main():
    text = sys.argv[1] if len(sys.argv) > 1 else (
        "Write a short Python script that prints the first 10 Fibonacci numbers."
    )
    uri = "ws://127.0.0.1:8000/ws/factory?client_id=testclient"
    async with websockets.connect(uri, max_size=None) as ws:
        payload = {"mode": "Shop", "text": text, "history": [], "use_tts": False}
        await ws.send(json.dumps(payload))
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=900)
            except asyncio.TimeoutError:
                print("TIMEOUT waiting for pipeline message", flush=True)
                return 1
            msg = json.loads(raw)
            mtype = msg.get("type")
            if mtype == "log":
                print(f"[LOG] {msg.get('content', '').strip()}", flush=True)
            elif mtype == "chat":
                print(f"[CHAT] {msg.get('content', '').strip()}", flush=True)
            elif mtype == "write_file":
                print(f"[WRITE_FILE] path={msg.get('path')} ({len(msg.get('content', ''))} chars)", flush=True)
            elif mtype in ("terminal_auth", "deployment_auth"):
                print(f"[AUTH REQUEST] {mtype}: denying for smoke test", flush=True)
                await ws.send(json.dumps({"status": "DENY"}))
            elif mtype == "done":
                print("[DONE] Pipeline finished.", flush=True)
                return 0
            else:
                print(f"[{mtype}] {str(msg)[:200]}", flush=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
