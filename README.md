# Skippy — Local Shop AI

Skippy is a fully-local, multi-agent AI assistant running on a Mac Studio (M3 Ultra, 512GB
unified memory). It serves as a senior coding architect/engineer and the day-to-day brain
for a machine shop (CNC, laser, 3D printing), with voice I/O, long-term memory, and
human-approved access to shop machines.

## Architecture

All inference is local via MLX-served OpenAI-compatible endpoints:

| Node | Port | Model | Role |
| --- | --- | --- | --- |
| Fast worker | 8080 | Llama-3.3-70B-Instruct-4bit | Architect, triage, QA, summarizer |
| Kraken | 8081 | Llama-3.1-405B-4bit | Complex engineering tasks |
| Compressor | 8082 | Qwen2.5-Coder-32B-4bit | Compresses RAG results to protect context |

### The pipeline (`skippy_factory.py` — current production server)

1. **Phase 0 — Smart Injector**: file paths mentioned in the prompt are read and injected
   into context (with smart truncation for G-code/logs).
2. **Phase 1 — Architect**: ReAct tool loop (web search, memory, RAG, GitHub, Tormach SSH,
   goal ledger, ...) that produces a plain-English blueprint or a direct reply.
3. **Phase 2 — Engineer + QA**: a triage step routes the blueprint to the 70B or 405B
   engineer; drafts are executed in a subprocess sandbox and reviewed by a QA agent, up to
   4 iterations. Approved skills are saved to `skills/`; self-upgrades ("Developer" mode)
   require explicit human authorization over the websocket before deployment.
4. **Phase 3 — Summarizer**: conversational wrap-up, optionally spoken via Kokoro TTS.

A background **heartbeat** wakes Skippy every 5 minutes to work on the goal ledger
(`skippy_goals.json`) autonomously.

### File map

| File | Purpose |
| --- | --- |
| `skippy_factory.py` | Main FastAPI server: multi-agent pipeline, websocket hub, heartbeat |
| `tools.py` | Tool implementations (search, memory, Tormach, GitHub, RAG, skills, goals) |
| `prompts.py` | System prompts per mode (Shop / Software / CNC / Developer / Whiteboard) |
| `skills/` | Reusable Python skills Skippy has written and QA-approved |
| `skippy_api.py` | Legacy single-agent ReAct websocket server (superseded by the factory) |
| `skippy.py` | Legacy standalone voice loop (VAD → Whisper → LLM → Kokoro) |
| `skippy_web.py` / `skippy_ui.py` | Gradio UIs (multi-agent assembly line / image generation) |

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Download the Kokoro voice files (`kokoro-v1.0.int8.onnx`, `voices-v1.0.bin`) into the repo
root, start your MLX servers on ports 8080/8081/8082, then:

```bash
python3 skippy_factory.py
```

### Required environment variables

Machine credentials are **never** stored in the repo. Add to `~/.zshrc`:

```bash
export TORMACH_IP="192.168.1.219"
export TORMACH_USER="operator"
export TORMACH_SSH_KEY="~/.ssh/tormach_ed25519"   # preferred: key auth
# export TORMACH_PASSWORD="..."                    # fallback: password auth
```

To set up key auth on the PathPilot controller:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/tormach_ed25519 -N ""
ssh-copy-id -i ~/.ssh/tormach_ed25519.pub operator@192.168.1.219
```

## Security notes

- Destructive tools (terminal, Tormach SSH, self-deployment) require explicit human
  approval through the UI websocket before execution.
- The factory binds to `0.0.0.0` for LAN clients (MacBook UI, VS Code bridge). Do not
  port-forward it past the LAN; there is no authentication layer yet.
- Web content (search results, fetched pages) is untrusted input to the agent loop —
  keep the human-approval gates in place.
