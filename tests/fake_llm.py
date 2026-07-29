"""A scripted OpenAI-compatible chat server, so the agent loop is testable offline.

Real MLX weights are non-deterministic and unavailable in CI, but the loop's
contract (tool-call syntax, observation feedback, patch application, stop
conditions) is exactly what needs coverage. This server hands back a fixed
sequence of assistant turns and records every request it saw.
"""

import json
import threading
import time
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Request

COMPRESSOR_MARKER = "You are a data extraction node"


def tool_call(name: str, thought: str = "", **args) -> str:
    """Render an assistant turn in the shape the Agent prompt demands."""
    body = json.dumps({"tool": name, "args": args}, indent=2)
    prefix = f"{thought}\n\n" if thought else ""
    return f"{prefix}```json\n{body}\n```"


def raw_tool_call(payload: dict, thought: str = "") -> str:
    prefix = f"{thought}\n\n" if thought else ""
    return f"{prefix}```json\n{json.dumps(payload, indent=2)}\n```"


class FakeLLM:
    def __init__(self, host: str = "127.0.0.1", port: int = 8770):
        self.host = host
        self.port = port
        self.script: List[str] = []
        self.requests: List[Dict[str, Any]] = []
        self.compressor_reply = "[compressed summary]"
        self.exhausted_reply = tool_call(
            "finish", summary="Script exhausted.", files_changed=[]
        )
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.app = self._build_app()

    # -- lifecycle --------------------------------------------------------

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}/v1/chat/completions"

    def start(self, timeout: float = 10.0):
        config = uvicorn.Config(self.app, host=self.host, port=self.port, log_level="error")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if getattr(self._server, "started", False):
                return
            time.sleep(0.05)
        raise RuntimeError("FakeLLM failed to start")

    def stop(self):
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10.0)

    # -- scripting --------------------------------------------------------

    def load(self, replies: List[str]):
        with self._lock:
            self.script = list(replies)
            self.requests = []

    @property
    def remaining(self) -> int:
        with self._lock:
            return len(self.script)

    def last_messages(self) -> List[dict]:
        with self._lock:
            return self.requests[-1]["messages"] if self.requests else []

    def observations(self) -> List[str]:
        """Every OBSERVATION the loop fed back, in order."""
        seen: List[str] = []
        with self._lock:
            for request in self.requests:
                for message in request["messages"]:
                    content = message.get("content") or ""
                    if content.startswith("OBSERVATION:") and content not in seen:
                        seen.append(content)
        return seen

    # -- server -----------------------------------------------------------

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def completions(request: Request):
            body = await request.json()
            messages = body.get("messages", [])
            joined = "\n".join(str(message.get("content", "")) for message in messages)

            # Compression calls are infrastructure, not agent turns; never consume script.
            if COMPRESSOR_MARKER in joined:
                content = self.compressor_reply
            else:
                with self._lock:
                    self.requests.append(body)
                    content = self.script.pop(0) if self.script else self.exhausted_reply

            return {
                "id": "fake-1",
                "object": "chat.completion",
                "model": body.get("model", "fake"),
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }

        return app
