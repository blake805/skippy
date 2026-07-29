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

Models are addressed by **role**, never by size or port. `skippy_llm.py` owns the
mapping, so changing weights is configuration rather than a code change:

| Role | Port | Model | Used for |
| --- | --- | --- | --- |
| `fast` | 8080 | Qwen3-Coder-30B-A3B-Instruct-4bit | Cheap turns, triage, routing |
| `heavy` | 8081 | Qwen3-Coder-480B-A35B-Instruct-4bit | The agent loop: multi-file edits, RE analysis |
| `compressor` | 8080 | Qwen3-Coder-30B-A3B-Instruct-4bit | Squeezing oversized tool output |

Two server processes, not three — `compressor` shares `fast`'s. Override any role with
`SKIPPY_<ROLE>_URL`, `SKIPPY_<ROLE>_MODEL`, and `SKIPPY_<ROLE>_MAX_TOKENS`. `GET /health`
reports what each role actually resolved to. See
[ADR 0007](docs/adr/0007-model-roles-and-cloud-escalation.md) for the benchmark behind
these choices.

Serve them with `HF_HUB_OFFLINE=1` set. Without it `mlx_lm.server` calls the Hugging Face
API to check each model's revision, which both reaches the network at runtime and takes
the server down with a 401 when the call fails:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  mlx_lm.server --model mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit --port 8080 --host 127.0.0.1
```

The `SkippyServer` menu-bar app boots the models plus the backend for you.

### Cloud escalation

Local by default. A role may point at any OpenAI-compatible hosted endpoint, but
reaching off-machine is opt-in and never silent:

```bash
export SKIPPY_ALLOW_CLOUD=1
export SKIPPY_HEAVY_URL="https://api.example.com/v1/chat/completions"
export SKIPPY_HEAVY_API_KEY="..."
```

Without `SKIPPY_ALLOW_CLOUD=1`, resolving an off-machine role raises `CloudNotAllowed`.
`is_local` is computed from the URL rather than declared, and only loopback counts — a
LAN or tailnet address is treated as off-machine on purpose.

### Which repositories Skippy can touch

`SKIPPY_WORKSPACE_ROOTS` is an `os.pathsep`-separated list of directories, and it
is the only thing that grants filesystem access:

```bash
export SKIPPY_WORKSPACE_ROOTS="$HOME/skippy-workspaces/skippy:$HOME/skippy-workspaces/symatix"
```

It defaults to **empty**, so an unconfigured agent can reach nothing. Every path a
tool touches is resolved — symlinks followed, `..` collapsed — and then required to
land inside a root, so `../../.ssh/id_ed25519` is a hard error rather than a
prompt. `GET /health` reports the roots actually in effect.
[ADR 0008](docs/adr/0008-path-sandbox.md) covers the boundary and, importantly,
what it does not defend against.

### Running a task

```python
import asyncio, skippy_agent, skippy_fs, skippy_paths

outcome = asyncio.run(skippy_agent.run_task(
    "Add a mm_to_thou helper, export it, and add a test.",
    skippy_fs.build_sandbox(),
    journal_dir=skippy_paths.patch_journal_root(),
))
print(outcome.status, outcome.files_changed)
```

The loop stops for one of four reasons and says which: `finished`, `max_steps`,
`stopped_without_finish`, or `cancelled`. **Only `finished` is success** — a run that
exhausted its step budget may have changed real files, but the model never decided
it was done, and reporting that as success would hide a stalled run.

Tool calls travel as native OpenAI `tool_calls`, so every call gets exactly one
`tool` message in reply and the transcript only ever grows.
[ADR 0010](docs/adr/0010-agent-loop-native-tool-calling.md) covers the loop,
including what it does when the model gets stuck.

### How edits are applied

`apply_patch` is the only way anything gets written, and it is all-or-nothing: a
list of edits spanning any number of files is validated against staged content
first, and if any one edit is bad, nothing is written and every problem is reported
together. A rename touching five files is one call, not five, so a half-applied
refactor is not a state the repo can reach.

Edits are byte-for-byte search/replace rather than line numbers, which go stale as
soon as an earlier edit shifts them. An ambiguous search is rejected instead of
guessed at. `dry_run` returns the diff without writing.

Files that are not valid UTF-8, look binary, or exceed 8MB are refused rather than
rewritten, and CRLF line endings are preserved. Pre-images go to
`patch_journal_root()` with a manifest that documents how to restore them.
[ADR 0009](docs/adr/0009-atomic-multi-file-patching.md) has the details, including
the three data-destroying bugs this replaced.

### Why transcripts are append-only

`mlx_lm.server` caches prompts by prefix, worth roughly 20x on the `heavy` role: a
12K-token prefill measures 59.9s cold against 2.8–3.3s warm. Editing an already-sent
message invalidates that cache and forces a full re-prefill, so `skippy_llm.Transcript`
exposes no way to delete or rewrite a turn. Use `fold()` when context genuinely has to
be shed; it returns a new transcript and logs the cost.

Tool interaction uses native OpenAI-style function calling — schemas in `tool_schemas.py`,
parsed server-side by `mlx_lm.server`. `parse_leaked_tool_calls` in `skippy_factory.py`
recovers the malformed XML-style calls Qwen3-Coder occasionally emits instead.

### File map

| File | Purpose |
| --- | --- |
| `skippy_llm.py` | Model role registry, inference, cloud policy, append-only transcripts |
| `skippy_sandbox.py` | The path boundary every filesystem tool goes through |
| `skippy_fs.py` | Read-only workspace tools: `list_dir`, `read_file`, `grep`, `glob_files` |
| `skippy_edit.py` | The write path: `apply_patch`, atomic across any number of files |
| `skippy_agent.py` | The agent loop: think, call tools, observe, repeat |
| `skippy_dispatch.py` | Runs one tool by name, turning every failure into an observation |
| `prompts.py` | The system prompt, and the fold-summary extraction prompt |
| `skippy_paths.py` | Where NAS-backed state lives, and which repos are in scope |
| `skippy_factory.py` | FastAPI server: websocket hub, voice, transcription, endpoints |
| `tools.py` | Research and context tools (web, memory, GitHub, directory maps, code RAG) |
| `tool_schemas.py` | OpenAI-format function schemas for native tool calling |
| `apps/SkippyServer/` | macOS app that boots the model servers and backend |
| `apps/SkippyClient/` | macOS/iOS chat client |
| `docs/adr/` | Architecture decision records |

## Tests

```bash
pip install -r requirements-test.txt
python -m pytest
```

`requirements-test.txt` is the light dependency set CI installs, so a local run
matches CI exactly. The suite needs no model server, no weights, no NAS, and no
network — `tests/fake_llm.py` stands in for `mlx_lm.server`. Chroma, Whisper and
Kokoro are loaded on first use rather than at import, which is what keeps
`skippy_factory` importable without them. Adding an import-time dependency on any
of them will break collection.

CI runs the suite three ways: normally, again inside a network namespace with only
loopback available so nothing can quietly reach the internet, and `pyflakes` over
every module with no exclusions.

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
