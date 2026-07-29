"""A scripted OpenAI-compatible chat server, so the agent is testable offline.

Real MLX weights are non-deterministic and absent in CI, but what needs coverage
is the contract around them: tool-call shape, observation feedback, stop
conditions, retry behaviour, and whether the transcript stays append-only. This
server hands back a fixed sequence of assistant turns and records every request.

Replies are built with the helpers below rather than raw dicts, because the shape
matters. `tool_call` produces the native OpenAI `tool_calls` field that
`mlx_lm.server` parses server-side; `leaked_tool_call` produces the malformed
XML-in-content form Qwen3-Coder emits when it drops the opening frame token,
which `skippy_llm.parse_leaked_tool_calls` has to recover.
"""

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Request

COMPRESSOR_MARKER = "You are a data extraction node"


@dataclass
class Reply:
    """One scripted assistant turn."""

    content: str = ""
    tool_calls: List[dict] = field(default_factory=list)
    status: int = 200

    def as_message(self) -> dict:
        message: Dict[str, Any] = {"role": "assistant", "content": self.content}
        if self.tool_calls:
            message["tool_calls"] = self.tool_calls
        return message


def text(content: str) -> Reply:
    """A plain prose turn with no tool call."""
    return Reply(content=content)


def tool_call(name: str, thought: str = "", call_id: str = "call_1", **args) -> Reply:
    """A well-formed native tool call, as mlx_lm.server would parse it."""
    return Reply(
        content=thought,
        tool_calls=[{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
    )


def tool_calls(*names_and_args, thought: str = "") -> Reply:
    """Several tool calls in one turn, to test that the loop rejects or serializes them."""
    calls = []
    for index, (name, args) in enumerate(names_and_args, start=1):
        calls.append({
            "id": f"call_{index}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        })
    return Reply(content=thought, tool_calls=calls)


def leaked_tool_call(name: str, thought: str = "", **args) -> Reply:
    """The XML form that lands in `content` when the model omits <tool_call>."""
    params = "".join(
        f"<parameter={key}>{value if isinstance(value, str) else json.dumps(value)}</parameter>"
        for key, value in args.items()
    )
    return Reply(content=f"{thought}<function={name}>{params}</function>")


def malformed_tool_call(name: str, raw_arguments: str = "{not json") -> Reply:
    """Arguments the server passes through but which are not valid JSON."""
    return Reply(tool_calls=[{
        "id": "call_1",
        "type": "function",
        "function": {"name": name, "arguments": raw_arguments},
    }])


def http_error(status: int = 500) -> Reply:
    """Make the endpoint fail, for retry and error-propagation tests."""
    return Reply(status=status)


class FakeLLM:
    def __init__(self, host: str = "127.0.0.1", port: int = 8770):
        self.host = host
        self.port = port
        self.script: List[Reply] = []
        self.requests: List[Dict[str, Any]] = []
        self.compressor_reply = "[compressed summary]"
        self.exhausted_reply = tool_call("finish", summary="Script exhausted.")
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

    def load(self, replies: List[Reply]):
        with self._lock:
            self.script = list(replies)
            self.requests = []

    @property
    def remaining(self) -> int:
        with self._lock:
            return len(self.script)

    @property
    def call_count(self) -> int:
        with self._lock:
            return len(self.requests)

    def last_messages(self) -> List[dict]:
        with self._lock:
            return self.requests[-1]["messages"] if self.requests else []

    def last_payload(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self.requests[-1]) if self.requests else {}

    def tools_offered(self) -> List[str]:
        """Names of the tool schemas sent on the most recent request."""
        payload = self.last_payload()
        return [t.get("function", {}).get("name", "") for t in payload.get("tools") or []]

    def observations(self) -> List[str]:
        """Every tool result the caller fed back, in order."""
        seen: List[str] = []
        with self._lock:
            for request in self.requests:
                for message in request["messages"]:
                    if message.get("role") != "tool":
                        continue
                    content = message.get("content") or ""
                    if content not in seen:
                        seen.append(content)
        return seen

    def prefix_broken_at(self) -> Optional[int]:
        """Index of the first request that did not extend its predecessor.

        mlx_lm.server caches by prefix, so an agent loop must only ever append.
        Returns None when every request was a strict extension of the last.
        """
        with self._lock:
            history = [r["messages"] for r in self.requests]
        for index in range(1, len(history)):
            previous, current = history[index - 1], history[index]
            if len(current) < len(previous) or current[:len(previous)] != previous:
                return index
        return None

    # -- server -----------------------------------------------------------

    def _build_app(self) -> FastAPI:
        app = FastAPI()

        @app.post("/v1/chat/completions")
        async def completions(request: Request):
            from fastapi.responses import JSONResponse

            body = await request.json()
            messages = body.get("messages", [])
            joined = "\n".join(str(message.get("content", "")) for message in messages)

            # Compression is infrastructure, not an agent turn; never consume script.
            if COMPRESSOR_MARKER in joined:
                return self._envelope(body, Reply(content=self.compressor_reply))

            with self._lock:
                self.requests.append(body)
                reply = self.script.pop(0) if self.script else self.exhausted_reply

            if reply.status != 200:
                return JSONResponse(
                    status_code=reply.status, content={"error": "scripted failure"}
                )
            return self._envelope(body, reply)

        return app

    @staticmethod
    def _envelope(body: dict, reply: Reply) -> dict:
        return {
            "id": "fake-1",
            "object": "chat.completion",
            "model": body.get("model", "fake"),
            "choices": [{"index": 0, "message": reply.as_message(), "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
