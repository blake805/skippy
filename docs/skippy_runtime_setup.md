# Skippy runtime setup (Mac Studio + Synology NAS)

Everything below is a one-time prerequisite for the dual-runtime build. Nothing in
the repo needs the NAS or MLX to *import* any more — that is what makes the tests
runnable anywhere — but the agent obviously needs real weights to be useful.

## 1. Disk and shares

- [ ] Confirm free space on the Studio SSD. GLM-5.2 at ~3.5bpw is roughly **330GB**
      of active weights, and they should live on local SSD, not the NAS.
- [ ] Create NAS shares and confirm they mount:
      - `/Volumes/skippy_memory` — Chroma, sessions, decisions, patch pre-images
      - `/Volumes/skippy_models` — cold model store
      - `/Volumes/skippy_workspaces` — cloned repos the agent works in
- [ ] Clone the repos you want Skippy touching into `/Volumes/skippy_workspaces/`.
- [ ] Back up `skippy_goals.json` and `skills/` before the first agent run.

If a share is missing, `skippy_paths` falls back to `~/.skippy/` rather than
failing — useful for a laptop, but not what you want in production. Set the
override explicitly if you relocate anything:

```bash
export SKIPPY_MEMORY_ROOT=/Volumes/skippy_memory
export SKIPPY_WORKSPACES_ROOT=/Volumes/skippy_workspaces
```

## 2. Models

- [ ] Confirm the **actual** Hugging Face repo ids for the fleet. The defaults in
      `skippy_llm.py` encode the intent, not a guarantee that the id resolves.
- [ ] Download to `skippy_models`, then copy the active set to the Studio SSD.
- [ ] Export the ids:

```bash
export SKIPPY_HEAVY_MODEL="<glm-5.2 mlx ~3.5bpw repo id>"
export SKIPPY_FAST_MODEL="<qwen3.6-35B-A3B or 27B mlx repo id>"
export SKIPPY_COMP_MODEL="$SKIPPY_FAST_MODEL"
```

| Role | Port | Purpose |
| --- | --- | --- |
| `fast` | 8080 | Shop architect, triage, QA, summaries |
| `heavy` | 8081 | The coding brain: drives the whole agent loop |
| `compressor` | 8082 | Squeezes oversized tool output and RAG dumps |

## 3. Software

- [ ] `pip install -r requirements.txt`
- [ ] `pip install -U mlx-lm` (a patched build if GLM-5.2 needs int8 MLA-KV)
- [ ] `brew install ripgrep git gh tree`
      - `rg` backs the agent's `grep` tool (there is a slower pure-Python fallback)
      - `tree` and `gh` are hardcoded to `/opt/homebrew/bin/` in `tools.py`
- [ ] Ports 8080/8081/8082 free; `:8000` reachable from the machine running Cursor.

## 4. Start the fleet

```bash
./scripts/serve_models.sh all        # fast + compressor + heavy
./scripts/serve_models.sh status
python3 scripts/model_smoke_test.py --code
```

The script reads role config straight out of `skippy_llm.py`, so it cannot drift
from what the server will request. It also enforces the one-heavy-resident rule:
starting a second heavy role fails rather than quietly swapping to disk.

For a long-context GLM session that needs the whole machine:

```bash
./scripts/serve_models.sh solo heavy   # unloads fast + compressor first
```

Note that with `fast` and `compressor` down, observation compression and the shop
lane stop working. That is the trade.

## 5. Start Skippy

```bash
python3 skippy_factory.py     # :8000
```

- `/ws/factory` — shop lane by default; `mode: "Agent"` routes to the coding agent
- `/ws/agent` — coding lane by default
- `/ping`, `/transcribe` — unchanged

## 6. Cursor

See [`cursor_client/README.md`](../cursor_client/README.md). Build the `.vsix`,
install it from the Command Palette, and point `skippy.serverUrl` at the Studio.

## Registering a project

Workspace roots can come from the payload, from a project's `meta.json`, or from
Cursor. To register them up front:

```python
import skippy_sessions

store = skippy_sessions.SessionStore()
store.ensure_project(
    "shop-jarvis",
    workspace_roots=["/Volumes/skippy_workspaces/shop-jarvis"],
    conventions={"test_command": "python3 -m pytest -q", "package_manager": "pip"},
)

import asyncio
asyncio.run(store.index_workspace("shop-jarvis", "/Volumes/skippy_workspaces/shop-jarvis"))
```

Then a task needs nothing but a `project_id`:

```json
{"type": "agent_task", "project_id": "shop-jarvis", "text": "add retry to query_model"}
```

## Autonomous project work

Ledger tasks in `skippy_goals.json` that carry a `project_id` are picked up by the
heartbeat and handed to the agent instead of the shop pipeline:

```json
{"id": 7, "task": "add divide() to calc", "status": "pending", "project_id": "shop-jarvis"}
```

Tasks without a `project_id` are left completely alone.

## Before you restart the shop server on new code

The SwiftUI clients speak a documented websocket contract — see
[`swiftui_client_contract.md`](swiftui_client_contract.md). One change in flight
requires updating the app:

- **Authorization replies must echo `task_id`** (PR #5, ADR 0005). Until the
  SwiftUI app round-trips that field, the Tormach SSH gate and the Developer-mode
  deploy gate stall for 600 seconds and then deny. It fails closed, so nothing is
  authorized by accident, but those gates stop working. Update the client, or add
  a server-side bridge, before this code serves the shop.

Do development in a clone that is *not* the directory the shop server runs from.

## Known gaps

- `tools.execute_tormach_ssh` still has a plaintext password. PR #1 addresses it;
  it should not reach a public branch.
- The shop pipeline's terminal-approval path reads the socket directly, racing the
  endpoint loop. The agent lane routes approvals through the hub instead. PR #5
  fixes the shop lane the same way; see the note above for the client-side
  consequence.
- Reverse-engineering mode (Phase 5) is not implemented. `mode: "RE"` currently
  reaches the agent with the standard Agent prompt and no RE tooling.
