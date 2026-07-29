# Skippy — Local Coding & Reverse-Engineering Agent

Skippy is a fully-local coding agent running on a Mac Studio (M3 Ultra, 512GB unified
memory). The goal is Cursor-class capability — multi-file features in real repositories,
project memory that survives across sessions, work spanning several repos at once, and a
reverse-engineering mode — with no cloud LLM in the runtime path.

> **Status: mid-refactor.** The shop assembly line that used to live here is archived at
> tag `shop-v1` and has been removed from `main`. The agent runtime that replaces it is
> not installed yet, so the websocket endpoint currently accepts connections and replies
> that the runtime is missing. See [ADR 0006](docs/adr/0006-single-runtime-coding-agent.md)
> for why the archive happened first.

## Architecture

All inference is local via MLX-served OpenAI-compatible endpoints:

| Node | Port | Model | Role |
| --- | --- | --- | --- |
| Fast | 8080 | Qwen3-Coder-30B-A3B-Instruct-4bit | Cheap turns, triage, summarization |
| Heavy | 8081 | Qwen3-Coder-480B-A35B-Instruct-4bit | Multi-file edits, RE analysis |
| Compressor | 8082 | Qwen2.5-Coder-32B-Instruct-4bit | Compresses retrieval results to protect context |

Serve them with `HF_HUB_OFFLINE=1` set. Without it `mlx_lm.server` calls the Hugging Face
API to check each model's revision, which both reaches the network at runtime and takes
the server down with a 401 when the call fails:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  mlx_lm.server --model mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit --port 8080 --host 127.0.0.1
```

The `SkippyServer` menu-bar app boots all three plus the backend for you.

Tool interaction uses native OpenAI-style function calling — schemas in `tool_schemas.py`,
parsed server-side by `mlx_lm.server`. `parse_leaked_tool_calls` in `skippy_factory.py`
recovers the malformed XML-style calls Qwen3-Coder occasionally emits instead.

### File map

| File | Purpose |
| --- | --- |
| `skippy_factory.py` | FastAPI server: model routing, websocket hub, voice, transcription |
| `tools.py` | Research and context tools (web, memory, GitHub, directory maps, code RAG) |
| `tool_schemas.py` | OpenAI-format function schemas for native tool calling |
| `apps/SkippyServer/` | macOS app that boots the model servers and backend |
| `apps/SkippyClient/` | macOS/iOS chat client |

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Download the Kokoro voice files (`kokoro-v1.0.int8.onnx`, `voices-v1.0.bin`) into the repo
root, start the MLX servers on ports 8080/8081/8082, then:

```bash
python3 skippy_factory.py
```

Long-term memory lives in ChromaDB at `/Volumes/skippy_memory/chroma_db`, so the NAS must
be mounted before the backend starts.

## Security notes

- The backend binds to `0.0.0.0` for LAN clients and **has no authentication layer**. Do
  not port-forward it or expose it to a network you don't control. Remote access is
  planned via Tailscale plus a bearer token on the websocket handshake; until that lands,
  LAN only.
- Web content (search results, fetched pages) and any decompiled or third-party source is
  untrusted input to the agent loop. Keep human-approval gates on destructive tools.
