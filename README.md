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

### How it checks its own work

`run_command` runs a single test runner, linter, type checker, build tool or
read-only git command, without a shell. This is what makes the difference between code
that looks right and code that has been run: in the live run for
[ADR 0011](docs/adr/0011-command-execution.md) the model wrote a test file with a
missing import, ran the suite, read its own failure, fixed the cause and re-ran to
green. Before this existed, that broken file would have shipped with a confident
summary.

Be clear about what the allowlist does. `pytest` executes `conftest.py`, and the agent
can write `conftest.py`, so **"may run pytest" and "may execute arbitrary code" are the
same permission.** The allowlist is accident prevention — no `rm -rf`, no
`git push --force`, no `curl | sh` — not containment, and there is deliberately no
approval-gated shell tool, because asking permission for the loud path while the quiet
one is open is theatre. Real containment means running the whole thing in a VM.

What is guaranteed: no shell interpretation, a bounded timeout that kills the entire
process tree, bounded output that keeps the head and the tail, no stdin, and an
allowlisted environment so a repo's test suite is never handed your API keys. Extend
the program list per machine with `SKIPPY_EXTRA_COMMANDS`; installers and anything that
fetches are excluded by default.

### Cursor integration

Sideload `cursor_client/` and Skippy's edits land in the editor instead of behind its
back. Two things change:

- **A multi-file change is one undo step.** Edits go through a single
  `vscode.WorkspaceEdit`, so ⌘Z reverses the whole thing. Files no longer appear to
  mutate on disk with no undo history.
- **Every patch reports what it broke.** A successful edit comes back with the
  diagnostics your language servers produce for the files it touched, in the same
  observation — not as a separate call the model has to remember to make. Diagnostics
  are *waited for* rather than sampled: reading them the instant an edit lands returns
  the state from before it, which would tell the agent a new error is a clean bill of
  health.

There is one `apply_patch`, and it routes to the editor when one is attached and writes
to disk when not. Nothing in the tool schema mentions Cursor, because a model asked to
choose on the basis of state it cannot see will choose wrong.

The editor never decides what an edit *means*. The server validates, resolves paths
against the sandbox, and stages the final text of each file; the extension is handed
that text and puts it there. Both sides still implement search-and-replace — the editor
has to plan against unsaved buffers — so both run the same 27-case table in
`tests/fixtures/patch_parity.json`. If they disagreed, Skippy would give different
answers depending on whether your editor happened to be open.

Deliberately absent: any way for the editor to run a command. That would be a second
execution path with none of the policy in `skippy_exec.py`, behind a socket that has no
authentication yet. See [ADR 0014](docs/adr/0014-cursor-integration.md).

### Project memory across sessions

A run's record is written automatically when it ends, and the next run on the same
workspace roots opens with the relevant history already in context:

```text
## What you already know about this project (skippy)
Conventions established here:
- test command: python -m pytest -q

Decisions from earlier sessions:
- [0003] Retries belong in the transport [MAY BE OUT OF DATE: net/client.py no longer exists]

Open weaknesses found in earlier reverse-engineering sessions:
- [0001] [critical, confirmed] Firmware update accepts unsigned images
  evidence: finding 0004 in pack firmware-bin-a3f81c2e

Recent sessions, newest first:
- 2026-07-29T21:31 [finished] Moved retries into the transport (touched net/pool.py)
- 2026-07-28T16:02 [max_steps] Got halfway through the auth migration; refresh path unfinished
```

Four choices worth knowing about:

- **Recall is not optional.** The history goes in the opening message rather than
  waiting on the model to call a tool. A tool the model may call is one it mostly will
  not — RE mode had to announce its note pack for the same reason. `recall_project`
  exists for older or more specific questions than the opening block covers.
- **Stale memory says so.** Every entry records the commit it was written at and the
  paths it concerns; a path that no longer exists gets marked. This is the difference
  between memory that helps and memory that hurts: "retries live in `client.py`" is
  confidently wrong after a refactor, and a misinformed session is worse off than a
  blind one. Superseded decisions are dropped from the opening block and marked in
  recall.
- **Failed runs are recorded too.** A run that ran out of steps halfway through a
  migration is the most useful thing for the next session to know, so the record is
  written on every terminal outcome, not just success.
- **It carries work across modes.** A weakness recorded in an RE session becomes a work
  item here, so the coding session that can actually fix it opens knowing about it. The
  item points at the finding rather than restating it, `resolve_work_item` closes it, and
  the resolution is a new record rather than an edit.

Sessions are JSON and decisions are markdown under `sessions_root()/projects/<id>`,
keyed by the basenames of your workspace roots, so nothing has to be named by hand.
Recall is deterministic keyword scoring — no embedding backend required, which is also
what keeps it testable. See [ADR 0013](docs/adr/0013-project-memory.md).

### Reverse-engineering mode

```python
outcome = await skippy_agent.run_task(
    "Identify this binary: what it links against, what it exports, and what it is",
    sandbox,
    mode="re",
    target="/opt/samples/mystery_tool",
)
```

RE mode differs from coding mode in four things, and nothing else:

- **The notes are the deliverable.** A coding task leaves a diff and the repo remembers
  it; an RE task changes nothing, so anything not written down is lost when the
  transcript folds. `note_finding` writes one markdown file per finding under
  `notes_root()`, in a pack keyed by the target's resolved path, so next month's session
  accumulates onto this one instead of re-deriving it — and two products that both ship a
  `firmware.bin` get two packs rather than one confusing one. If the target's bytes have
  changed since the pack was started, every read of it says so.
- **Evidence and confidence are mandatory.** A finding with no evidence is refused:
  "the header is 32 bytes" is worthless later, "`otool -h` reports sizeofcmds 0x20" can
  be rechecked. Confidence is `speculative` / `likely` / `confirmed`, recorded
  separately, because the thing that ruins an investigation is a guess getting cited as
  fact by everything built on it. Corrections supersede rather than overwrite, and a
  superseded finding is marked as such on every path that reads it.
  The loop also logs every inspection command it runs and what the command printed, so a
  run that dies at step nine leaves the evidence rather than nothing, and a finding can be
  checked against the output it came from. Conclusions are still the model's to write;
  after six commands with nothing recorded, it gets told so.
- **Findings can name work.** A `weakness` finding carries a severity — `low` through
  `critical`, alongside its confidence, because a speculative critical and a confirmed one
  are different work. Recording one raises a work item in project memory, which is how the
  next coding session on the same repos opens knowing what needs fixing. Finding it in RE
  mode and fixing it in coding mode is the workflow; there is deliberately no
  report generator.
- **It reads code a function at a time.** `list_symbols`, then `disassemble_function` or
  `decompile`, each returning one function rather than a region — which is what keeps the
  heavy model's context small enough to be affordable. Behind them are rizin and the
  Ghidra decompiler as self-contained C++, so no JVM and no Ghidra install, covering
  x86-64, ARM, AArch64, MIPS, RISC-V and Xtensa. rizin is deliberately *not* in the
  command allowlist: its `-c` argument is a command language with a shell escape in it, so
  it is only ever invoked with an argument vector Skippy builds, always with `-N` and
  never with `-w`, and the symbol you ask for is resolved to an address before anything
  reaches a command string.
- **It cannot run the artifact.** There is no `apply_patch`, and `run_command` switches
  to an inspection-only allowlist: `file`, `strings`, `nm`, `otool`, `objdump`,
  `dwarfdump`, `xxd`, `c++filt` and friends, with no interpreter, build tool or test
  runner in it. Tools that read by default and write when asked are constrained to their
  read-only form — `lipo -info` yes, `lipo -create` no; `plutil -p` yes, `-convert` no;
  `tar -tf` yes, `tar xf` no. The mode is set by the loop and stripped from the model's
  arguments, so it cannot ask for the coding table.

**What it cannot do yet.** There is no carving or extraction, so there is no path from a
firmware image to the files inside it — a packed blob can be described from the outside
and read as code once you know where the code is, but nothing here unpacks a container.
Decompiled parameter lists are unreliable on Xtensa and RISC-V, where rz-ghidra cannot
match the calling convention; the tool says so in its output rather than leaving you to
find out. And running the target under a debugger is a separate need that wants a VM
rather than another entry in the allowlist; the refusal says so, and the model records the
question instead. See
[ADR 0012](docs/adr/0012-reverse-engineering-mode.md),
[0015](docs/adr/0015-note-pack-identity.md),
[0016](docs/adr/0016-loop-captured-evidence.md),
[0017](docs/adr/0017-weakness-findings-and-handoff.md) and
[0018](docs/adr/0018-rizin-structured-tools.md).

Disassembly needs the pinned rizin build, which is a source build rather than a Homebrew
one: rizin gates its Xtensa and RISC-V plugins on a bundled capstone that the Homebrew
bottle does not use, so `brew install rizin` silently lacks both. ADR 0018 records the
commits and the one-line rz-ghidra build fix RISC-V needs. Without it the RE mode still
works; the two tools report that they are unavailable and the static allowlist carries on.

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
| `skippy_exec.py` | `run_command`: allowlisted, shell-free execution so it can test its own changes |
| `skippy_re.py` | Reverse-engineering note packs: evidence-bearing findings, and the command log behind them |
| `skippy_rizin.py` | Function-scoped disassembly and decompilation, with rizin kept out of the allowlist |
| `skippy_memory.py` | Project memory: sessions, decisions and work items, carried into the next run and marked when stale |
| `skippy_tasks.py` | Runs a task for a connected client: one at a time, cancellable, events follow the client |
| `skippy_cursor.py` | Bridge to the editor: routes patches through it and brings diagnostics back |
| `cursor_client/` | The sideloaded VS Code-compatible extension |
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
- The command allowlist stops accidents, not a determined agent. Anything that can run
  `pytest` can run arbitrary code, because it can also write `conftest.py`. Treat the
  workspace roots as the real blast radius, and run against repos you can restore from
  git. If you ever point Skippy at code you do not trust, put the whole process in a VM;
  no setting in this repo substitutes for that.
