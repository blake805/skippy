# Skippy

Skippy is a locally-hosted AI workbench: a coding, reverse-engineering and
research agent that runs on a Mac Studio (M3 Ultra, 512GB unified memory) with
no cloud LLM in the runtime path, plus the apps and hardware that put it
everywhere work happens — a native Mac client, an iPhone client, a Cursor/VS
Code extension, a wireless voice puck, and a battery-powered bench probe that
gives the agent physical hands on real hardware over Wi-Fi or Bluetooth.

**The models are local; the facts are not.** Skippy reads the web — searching
and reading pages through a research API, and doing it on his own when he is not
sure enough about an answer to leave it standing. Everything fetched is treated
as untrusted input, and no page can reach a tool that changes anything. See
[Web research](#web-research-and-checking-his-own-work).

## The system at a glance

```
                        ┌────────────────────────────────────────┐
                        │  Mac Studio — the hub                  │
   SkippyMac (macOS) ──▶│  skippy_factory.py :8000               │
   SkippyPhone (iOS) ──▶│   /ws/factory  chat·code·RE·git·files  │
   SkippyClient      ──▶│   /ws/voice    full-duplex speech      │
   Cursor extension ───▶│                                        │
                        │  mlx_lm.server :8080/:8081             │
   core2-voice ── WiFi ─▶│   fast 30B · heavy 480B (Qwen3-Coder) │
                        │                                        │
   core2-devio ─ WiFi ──▶│  project memory · note packs · NAS    │
       └──── BLE ──▶ bridge (MacBook) ──▶ hub                    │
                        └────────────────────────────────────────┘
```

One hub, one protocol, many seats. Everything speaks JSON over two WebSocket
lanes on port 8000: `/ws/factory` for chat, agent runs, git, files and device
I/O, and `/ws/voice` for speech. Clients are interchangeable — a run started
from the phone streams its timeline to the Mac app, and an approval card
appears wherever the run was started.

## What Skippy does

### Coding agent

Multi-file features in real repositories, driven by a local 480B-parameter
model. The loop thinks, calls tools, observes, and repeats until it decides it
is finished — and only `finished` counts as success. It reads with sandboxed
`list_dir`/`read_file`/`grep`/`glob_files`, writes through one atomic
`apply_patch`, and checks its own work with `run_command` (allowlisted,
shell-free — test runners, linters, type checkers, builds). Details below under
[Architecture](#architecture).

### Reverse-engineering mode

A separate tool table for taking unknown binaries and devices apart without
being able to run or modify them: evidence-bearing findings in per-target note
packs, function-at-a-time disassembly and decompilation (rizin + the Ghidra
decompiler, no JVM), firmware carving with unblob inside a hardened container,
and an inspection-only command allowlist. Weakness findings carry severity and
flow into project memory as work items, so the next *coding* session opens
knowing what needs fixing.

### Web research, and checking his own work

A third mode of the same agent loop, with the web instead of a repository. It
plans sub-questions, searches, reads several sources over a few rounds, records
each claim against the pages that support it, and writes an answer with
citations. The product is a **brief** — the sources as they read on the day, the
claims, and the answer — filed under the question, so asking it again next month
opens the work rather than repeating it.

More usefully, it fires without being asked. When a turn is about something with
a date on it, or when Skippy's own verdict on what he just said is shaky, he
answers now, checks behind it, and follows up with what he found. See
[The research gate](#the-research-gate).

### Device I/O — real hardware on the bench

RE mode can touch physical hardware through device bridges, addressed by
`host=`:

- **`host="studio"`** — serial/USB hardware plugged into the Mac Studio.
- **`host="macbook"`** — the SkippyMac app registers as a bridge ("Share
  devices" in Settings), sharing whatever is plugged into the laptop.
- **`host="bench"`** — the Core2 bench node (below): UART, I2C, GPIO and ADC
  over the air.

Reads happen straight away. **Any write — serial bytes, an I2C write, driving a
pin — stops for an approval card** on the machine that started the run, with a
sequence number and an "Approve all writes" escape hatch for repetitive jobs.

### The wireless bench node

`firmware/core2-devio` turns an M5Stack Core2 into a carry-along probe: clip it
to a target and the agent can enumerate ports, talk UART, scan and read I2C,
drive pins and sample analog inputs. It is deliberately a dumb executor — every
decision, and every approval, stays on the hub.

One protocol, two transports. On the shop network the node holds its own
WebSocket to the hub over Wi-Fi. Away from any network it is a BLE peripheral,
relayed to the hub by either `skippy_ble_bridge.py` (Python, bleak) or
SkippyMac's built-in CoreBluetooth bridge — so a MacBook is the only other
thing you need in the room. BLE wins when both links are possible, and the hub
only ever sees one node.

The node has a high-contrast touchscreen UI (scrollable action log, pause both
radios with a fingertip before putting your hands in the circuit), a light bar
that carries link state across the room, and a chirp on every write. It reports
battery, signal and uptime every 15 seconds; the app's RE dashboard lists every
node the hub has heard from — online or not — as a pickable "BENCH NODES"
group. See `firmware/core2-devio/README.md` and ADRs 0020/0021.

### Voice

Full-duplex speech through `skippy_voice.py`: Whisper transcription in,
Kokoro synthesis out, server-side VAD, and barge-in. Seats include the Mac app
(spacebar push-to-talk), the phone (hold-to-talk or full duplex), and
`firmware/core2-voice` — a wireless mic/speaker puck that streams raw PCM, so
model changes never require a reflash. Voice sessions end by writing a summary
into project memory.

### Git and GitHub

The hub owns the git surface, and both the agent and the app use it:

- **Repo panel (SkippyMac)** — per-repo status, branches, diffs, commit, pull,
  push, create a new repo, clone one of yours, and a read-only file explorer
  (folder tree plus viewer that refuses binaries).
- **New repo** creates both the local repository in a workspace root and the
  GitHub repository, wired together as `origin`.
- **Auth** is a GitHub personal access token pasted once into the app's
  Settings; the hub stores it (`~/.skippy/github_token`, mode 0600) and feeds
  git over HTTPS through a `GIT_ASKPASS` helper, so the token never lands in a
  remote URL or the repo config.
- **The agent** has `git_commit`, `git_branch`, `git_push` and `git_pull`
  tools; anything that touches a remote stops for approval first.

### Project memory across sessions

A run's record is written automatically when it ends, and the next run on the
same workspace roots opens with the relevant history already in context —
conventions, decisions, open weaknesses, and recent sessions including failed
ones. Stale entries say so: every entry records the commit and paths it
concerns, and a path that no longer exists gets marked, because a misinformed
session is worse off than a blind one. Recall is deterministic keyword scoring,
no embedding backend required. See [ADR 0013](docs/adr/0013-project-memory.md).

## The apps

| App | Platform | What it is |
| --- | --- | --- |
| `apps/SkippyMac` | macOS | The main seat: chat / code / RE modes with the full agent timeline, approval cards, the RE dashboard (studio, MacBook and bench devices, memory maps, hex diffs, findings), the Repo panel with GitHub and the file explorer, a built-in BLE bridge for the bench node, voice with push-to-talk, and a device bridge for hardware plugged into the Mac. Dark theme throughout. |
| `apps/SkippyPhone` | iOS | The same Work / Voice / Settings pages speaking the same protocol. Full agent timeline and approval sheets; full-duplex or hold-to-talk voice using the hardware echo canceller. No device bridge — a phone has no ports to share. |
| `apps/SkippyServer` | macOS | Menu-bar app that boots the model servers and the backend on the Studio, and shows the voice token. |
| `apps/SkippyClient` | macOS/iOS | The original lightweight chat client. |
| `apps/PhotoFrame` | macOS/tablet | An off-mission companion: a rotating photo frame with captions and music for the living room. |
| `cursor_client/` | VS Code/Cursor | Sideloaded extension: Skippy's multi-file edits land in the editor as one undo step, and every patch comes back with the diagnostics it caused. |

Away from the shop LAN, the apps reach the hub over Tailscale rather than port
forwarding — see [docs/cloud-access.md](docs/cloud-access.md).

## Architecture

### Models, addressed by role

Models are addressed by **role**, never by size or port. `skippy_llm.py` owns
the mapping, so changing weights is configuration rather than a code change:

| Role | Port | Model | Used for |
| --- | --- | --- | --- |
| `fast` | 8080 | Qwen3-Coder-30B-A3B-Instruct-4bit | Cheap turns, triage, routing |
| `heavy` | 8081 | Qwen3-Coder-480B-A35B-Instruct-4bit | The agent loop: multi-file edits, RE analysis |
| `compressor` | 8080 | Qwen3-Coder-30B-A3B-Instruct-4bit | Squeezing oversized tool output |

Two server processes, not three — `compressor` shares `fast`'s. Override any
role with `SKIPPY_<ROLE>_URL`, `SKIPPY_<ROLE>_MODEL`, and
`SKIPPY_<ROLE>_MAX_TOKENS`. `GET /health` reports what each role actually
resolved to. See [ADR 0007](docs/adr/0007-model-roles-and-cloud-escalation.md)
for the benchmark behind these choices.

Serve them with `HF_HUB_OFFLINE=1` set — without it `mlx_lm.server` phones the
Hugging Face API at runtime and dies with a 401 when the call fails:

```bash
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
  mlx_lm.server --model mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit --port 8080 --host 127.0.0.1
```

The `SkippyServer` menu-bar app boots the models plus the backend for you.

**Cloud escalation is opt-in and never silent.** A role may point at any
OpenAI-compatible hosted endpoint, but without `SKIPPY_ALLOW_CLOUD=1` resolving
an off-machine role raises `CloudNotAllowed`. `is_local` is computed from the
URL, and only loopback counts — a LAN or tailnet address is treated as
off-machine on purpose.

### The path sandbox

`SKIPPY_WORKSPACE_ROOTS` is an `os.pathsep`-separated list of directories, and
it is the only thing that grants filesystem access:

```bash
export SKIPPY_WORKSPACE_ROOTS="$HOME/skippy-workspaces/skippy:$HOME/skippy-workspaces/symatix"
```

It defaults to **empty**, so an unconfigured agent can reach nothing. Every
path a tool touches is resolved — symlinks followed, `..` collapsed — and then
required to land inside a root, so `../../.ssh/id_ed25519` is a hard error
rather than a prompt. `GET /health` reports the roots actually in effect.
[ADR 0008](docs/adr/0008-path-sandbox.md) covers the boundary and, importantly,
what it does not defend against.

### The agent loop

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
`stopped_without_finish`, or `cancelled`. **Only `finished` is success** — a
run that exhausted its step budget may have changed real files, but the model
never decided it was done, and reporting that as success would hide a stalled
run.

Tool calls travel as native OpenAI `tool_calls`, so every call gets exactly one
`tool` message in reply and the transcript only ever grows.
[ADR 0010](docs/adr/0010-agent-loop-native-tool-calling.md) covers the loop,
including what it does when the model gets stuck.

### How edits are applied

`apply_patch` is the only way anything gets written, and it is all-or-nothing:
a list of edits spanning any number of files is validated against staged
content first, and if any one edit is bad, nothing is written and every problem
is reported together. A rename touching five files is one call, not five, so a
half-applied refactor is not a state the repo can reach.

Edits are byte-for-byte search/replace rather than line numbers, which go stale
as soon as an earlier edit shifts them. An ambiguous search is rejected instead
of guessed at. `dry_run` returns the diff without writing. Files that are not
valid UTF-8, look binary, or exceed 8MB are refused rather than rewritten, and
CRLF line endings are preserved. Pre-images go to `patch_journal_root()` with a
manifest that documents how to restore them.
[ADR 0009](docs/adr/0009-atomic-multi-file-patching.md) has the details,
including the three data-destroying bugs this replaced.

### How it checks its own work

`run_command` runs a single test runner, linter, type checker, build tool or
read-only git command, without a shell. This is what makes the difference
between code that looks right and code that has been run: in the live run for
[ADR 0011](docs/adr/0011-command-execution.md) the model wrote a test file with
a missing import, ran the suite, read its own failure, fixed the cause and
re-ran to green.

Be clear about what the allowlist does. `pytest` executes `conftest.py`, and
the agent can write `conftest.py`, so **"may run pytest" and "may execute
arbitrary code" are the same permission.** The allowlist is accident prevention
— no `rm -rf`, no `git push --force`, no `curl | sh` — not containment, and
there is deliberately no approval-gated shell tool, because asking permission
for the loud path while the quiet one is open is theatre. Real containment
means running the whole thing in a VM.

What is guaranteed: no shell interpretation, a bounded timeout that kills the
entire process tree, bounded output that keeps the head and the tail, no stdin,
and an allowlisted environment so a repo's test suite is never handed your API
keys. Extend the program list per machine with `SKIPPY_EXTRA_COMMANDS`;
installers and anything that fetches are excluded by default.

### Cursor integration

Sideload `cursor_client/` and Skippy's edits land in the editor instead of
behind its back. A multi-file change is one undo step (a single
`vscode.WorkspaceEdit`), and every patch comes back with the diagnostics your
language servers produce for the files it touched — *waited for* rather than
sampled, because reading them the instant an edit lands returns the state from
before it.

There is one `apply_patch`, and it routes to the editor when one is attached
and writes to disk when not; nothing in the tool schema mentions Cursor. Both
sides implement the same search-and-replace semantics and run the same 27-case
parity table in `tests/fixtures/patch_parity.json`. Deliberately absent: any
way for the editor to run a command — that would be a second execution path
with none of the policy in `skippy_exec.py`. See
[ADR 0014](docs/adr/0014-cursor-integration.md).

### Reverse-engineering mode, in depth

```python
outcome = await skippy_agent.run_task(
    "Identify this binary: what it links against, what it exports, and what it is",
    sandbox,
    mode="re",
    target="/opt/samples/mystery_tool",
)
```

RE mode differs from coding mode in these things, and nothing else:

- **The notes are the deliverable.** `note_finding` writes one markdown file
  per finding under `notes_root()`, in a pack keyed by the target's resolved
  path, so next month's session accumulates onto this one instead of
  re-deriving it. If the target's bytes have changed since the pack was
  started, every read of it says so.
- **Evidence and confidence are mandatory.** A finding with no evidence is
  refused; confidence is `speculative` / `likely` / `confirmed`, recorded
  separately. Corrections supersede rather than overwrite. The loop also logs
  every inspection command and its output, so a run that dies at step nine
  leaves the evidence rather than nothing.
- **Findings can name work.** A `weakness` finding carries a severity — `low`
  through `critical` — and raises a work item in project memory, which is how
  the next coding session on the same repos opens knowing what needs fixing.
- **It carves containers, in a container.** `extract_artifact` runs unblob over
  30-odd formats, recursively, into a quarantine directory inside the note
  pack. No network, no capabilities, read-only input, image pinned by digest,
  and a watchdog for decompression bombs.
- **It reads code a function at a time.** `list_symbols`, then
  `disassemble_function` or `decompile`, each returning one function — rizin
  and the Ghidra decompiler as self-contained C++, covering x86-64, ARM,
  AArch64, MIPS, RISC-V and Xtensa. rizin is deliberately *not* in the command
  allowlist: its `-c` argument has a shell escape in it, so it is only invoked
  with an argument vector Skippy builds, always `-N`, never `-w`.
- **It can touch hardware, with approval.** The device tools (`list_devices`,
  `serial_*`, `i2c_*`, `gpio_io`, `adc_read`, `usb_*`) route to a bridge by
  `host=`; every write stops for an on-screen approval card. Exchanges are
  bounded request/response — a time-boxed capture of at most 30 seconds and
  4KB. A long trace belongs on a logic analyzer.
- **It cannot run the artifact.** No `apply_patch`, and `run_command` switches
  to an inspection-only allowlist (`file`, `strings`, `nm`, `otool`, `objdump`,
  `xxd`, …) with tools constrained to their read-only forms — `lipo -info` yes,
  `lipo -create` no. The mode is set by the loop and stripped from the model's
  arguments.

Carving needs a container runtime (`brew install podman && podman machine
start`); without one, `extract_artifact` says so and the rest of RE mode
carries on. Disassembly needs the pinned rizin source build — the Homebrew
bottle silently lacks the Xtensa and RISC-V plugins; ADR 0018 records the
commits. See ADRs
[0012](docs/adr/0012-reverse-engineering-mode.md),
[0015](docs/adr/0015-note-pack-identity.md),
[0016](docs/adr/0016-loop-captured-evidence.md),
[0017](docs/adr/0017-weakness-findings-and-handoff.md),
[0018](docs/adr/0018-rizin-structured-tools.md),
[0019](docs/adr/0019-containerised-extraction.md).

### Web research

The "no network in the runtime path" constraint has been dropped on purpose.
What replaces it is narrower and, for tools that read the internet, more
important: **the network is an input, and everything it returns is untrusted.**

```bash
export SKIPPY_TAVILY_KEY="tvly-..."     # the search backend's key
export SKIPPY_SEARCH_BACKEND=tavily     # swap providers without touching code
```

- **Untrusted by construction.** Every fetched page and every search snippet
  arrives fenced and labelled as data, with the "not instructions" warning
  beside the content on every observation rather than once in a system prompt. A
  page that forges the closing fence has its marker defanged on the way in.
  Research mode is offered no `apply_patch`, no filesystem tools and an empty
  command table, so the worst a hostile page can reach is another search.
- **Chunks, not truncation.** `web_fetch` strips scripts, navigation and page
  furniture with stdlib `HTMLParser`, prefers `<main>`/`<article>`, and splits
  long pages into numbered chunks sized to stay under the loop's compression
  threshold — so a version table two-thirds down a page is still reachable, and
  a citation survives the trip intact.
- **Fetches are vetted.** http/https only, and loopback, LAN and link-local
  addresses are refused by default: the model picks these URLs, and the
  interesting local targets are the model servers and the bench node.
  `SKIPPY_RESEARCH_ALLOW_PRIVATE=1` opens it deliberately.
- **A claim must cite a page this run actually read.** Sources are logged to the
  brief by the loop after every fetch — the text, not just the URL — and
  `note_claim` refuses a citation that does not match one. Asked for citations
  it cannot find, a model writes a plausible URL; a fabricated citation is
  indistinguishable from a real one at a glance. `confirmed` additionally needs
  two sources on different sites.
- **The answer is written by a separate pass**, from the claims and citations
  rather than from the transcript — so a run that ran out of steps still
  produces an answer, and the researching model's last-page-read bias does not
  drive the prose.
- **Briefs go stale and say so.** Sources carry the date they were read; past 30
  days, every read of the brief warns before handing anything back.

Findings also land in project memory with their source URLs and the date, ranked
above other matches in `recall_project`, which is what makes the third time a
subject comes up cost nothing.

### The research gate

Skippy decides for himself when an answer needs checking. Self-reported
confidence is poorly calibrated — worst exactly when the model is fluent and
wrong — so the trigger is layered, and none of the three is trusted alone:

1. **Cheap signals**, free: recency words, versions and years, capitalized
   product names, and on the other side the vocabulary of thinking out loud.
   These settle the easy cases with no model call at all.
2. **A fast-model gate before the answer**, the same cold-classifier pattern as
   the voice action router: needs-research / answer-directly / pure-ideation. It
   runs only when the cheap layer found something to date the question by,
   because otherwise it is a model call added to every reply including "good
   morning".
3. **The answering model's own verdict afterwards**, behind the delivered reply
   so it costs no latency. Escalates only on a low confidence **and** a
   non-empty list of checkable claims — a low number with nothing concrete
   behind it is modesty about an opinion, and researching that finds nothing.

Explicit instructions outrank all of it in both directions: "just tell me" and
"go check that" are honoured without a model call. The bias is otherwise toward
answering, because a false check interrupts someone's thinking while a false
answer leaves things exactly as they were.

Nothing blocks. The turn is answered now, the check runs behind it, and the
result arrives on its own — in chat as a `chat` event carrying
`kind: "research"` (sent after `done`, so a client can style it as a late
follow-up), and out loud as a spoken announcement trimmed to three sentences
with the citations left in the brief. Budgets: three runs per conversation, five
sources and six searches per run, with in-session, brief and project-memory
caching so a repeated question costs nothing. A run somebody explicitly asked
for is uncapped.

```bash
export SKIPPY_AUTO_RESEARCH=0           # never check unasked
export SKIPPY_RESEARCH_CONFIDENCE=0.75  # the layer-3 threshold
```

With no search key configured the gate is off entirely: every autonomous check
would otherwise end in an apology for not being able to check.

**The threshold is derived, not chosen.** `tests/fixtures/gate_cases.json` holds
labelled turns — should-research, should-answer, pure ideation, with the
self-check each answer produced — and `python -m tests.gate_eval` sweeps the
threshold against them under a cost model that prices a missed check at twice a
needless one. A test fails if the shipped constant drifts from what the set
picks. `--live` runs the real classifier over the same turns, which is the half
no offline test can cover; run it whenever `RESEARCH_GATE` is edited.

See [ADR 0022](docs/adr/0022-web-research.md) and
[ADR 0023](docs/adr/0023-autonomous-research-trigger.md).

### Why transcripts are append-only

`mlx_lm.server` caches prompts by prefix, worth roughly 20x on the `heavy`
role: a 12K-token prefill measures 59.9s cold against 2.8–3.3s warm. Editing an
already-sent message invalidates that cache, so `skippy_llm.Transcript` exposes
no way to delete or rewrite a turn. Use `fold()` when context genuinely has to
be shed; it returns a new transcript and logs the cost.

Tool interaction uses native OpenAI-style function calling — schemas in
`tool_schemas.py`, parsed server-side by `mlx_lm.server`.
`parse_leaked_tool_calls` in `skippy_factory.py` recovers the malformed
XML-style calls Qwen3-Coder occasionally emits instead.

## File map

| File | Purpose |
| --- | --- |
| `skippy_llm.py` | Model role registry, inference, cloud policy, append-only transcripts |
| `skippy_sandbox.py` | The path boundary every filesystem tool goes through |
| `skippy_fs.py` | Read-only workspace tools: `list_dir`, `read_file`, `grep`, `glob_files` |
| `skippy_edit.py` | The write path: `apply_patch`, atomic across any number of files |
| `skippy_exec.py` | `run_command`: allowlisted, shell-free execution |
| `skippy_git.py` | Git for agent and app: status, diff, commit, branch, push, pull, new, clone |
| `skippy_github.py` | GitHub: PAT storage, askpass helper, REST client (whoami, create, list repos) |
| `skippy_re.py` | RE note packs: evidence-bearing findings and the command log behind them |
| `skippy_research.py` | The web: search behind a backend interface, page fetching, boilerplate stripping, untrusted framing |
| `skippy_brief.py` | Research briefs: sources as they read, claims that must cite them, the answer |
| `skippy_gate.py` | Deciding without being asked when an answer needs checking |
| `skippy_rizin.py` | Function-scoped disassembly and decompilation |
| `skippy_extract.py` | Carving firmware images inside a hardened container |
| `skippy_device.py` | Device I/O routing and write approvals for the studio, MacBook and bench bridges |
| `skippy_ble_bridge.py` | Python BLE-to-hub relay for the bench node (bleak) |
| `skippy_memory.py` | Project memory: sessions, decisions, work items, marked when stale |
| `skippy_voice.py` | The voice lane: VAD, Whisper in, Kokoro out, barge-in |
| `skippy_tasks.py` | Runs a task for a connected client: one at a time, cancellable |
| `skippy_cursor.py` | Bridge to the editor: routes patches through it, brings diagnostics back |
| `cursor_client/` | The sideloaded VS Code-compatible extension |
| `skippy_agent.py` | The agent loop: think, call tools, observe, repeat |
| `skippy_dispatch.py` | Runs one tool by name, turning every failure into an observation |
| `prompts.py` | Every system prompt: the modes, the personas, the routers and the research gate |
| `skippy_paths.py` | Where NAS-backed state lives, and which repos are in scope |
| `skippy_factory.py` | FastAPI server: websocket hub, voice, git/files actions, endpoints |
| `tools.py` | Assorted context helpers (memory, directory maps, code RAG) |
| `tool_schemas.py` | OpenAI-format function schemas for native tool calling |
| `apps/` | SkippyMac, SkippyPhone, SkippyServer, SkippyClient, PhotoFrame |
| `firmware/core2-voice/` | Wireless mic/speaker puck for the voice lane |
| `firmware/core2-devio/` | The bench IO node: UART/I2C/GPIO/ADC over Wi-Fi or BLE |
| `docs/adr/` | Architecture decision records, 0001–0023 |

## Tests

```bash
pip install -r requirements-test.txt
python -m pytest
```

The suite needs no model server, no weights, no NAS, no API key and no network.
`tests/fake_llm.py` stands in for `mlx_lm.server`, `tests/fake_bridge.py` stands
in for a bench node, and the web tools are covered against a scripted search
backend and an `httpx.MockTransport` — a module whose whole job is talking to
the internet still has to be testable with the internet unplugged. Chroma,
Whisper and Kokoro are loaded on first use rather than at import; adding an
import-time dependency on any of them will break collection. The SkippyMac unit
tests run with
`xcodebuild test -project apps/SkippyMac/SkippyMac.xcodeproj -scheme SkippyMac -destination 'platform=macOS'`.

CI runs the Python suite three ways: normally, again inside a network namespace
with only loopback available, and `pyflakes` over every module with no
exclusions. The namespace run is about the *tests*, not about Skippy: he now
reads the web on purpose, but a suite that needed the internet would be one that
fails for reasons that have nothing to do with the code.

Two things are measured rather than asserted, and both need a model, so both are run by
hand rather than in CI. What CI checks is that the harnesses themselves are sound — in
particular that no grader passes before the agent has done anything, which is the way an
eval quietly stops measuring:

```bash
python -m tests.agent_eval --save  # does the coding agent finish the job? (the scoreboard)
python -m tests.gate_eval          # score the research gate against the labelled set
python -m tests.gate_eval --live   # ... using the real classifier, if a model is up
```

The agent scoreboard is the regression test for `prompts.py`: every task names the line
of `AGENT_SYSTEM` it defends, and each run reports which tasks changed verdict since the
last one. See [benchmarks/README.md](benchmarks/README.md).

## Setup

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

Download the Kokoro voice files (`kokoro-v1.0.int8.onnx`, `voices-v1.0.bin`)
into the repo root, start the MLX servers on ports 8080/8081, then:

```bash
python3 skippy_factory.py                      # loopback only
SKIPPY_BIND_HOST=0.0.0.0 \
SKIPPY_FACTORY_TOKEN=some-long-secret \
SKIPPY_VOICE_TOKEN=another-long-secret \
python3 skippy_factory.py                      # LAN/tailnet, token-gated
```

Long-term memory lives in ChromaDB at `/Volumes/skippy_memory/chroma_db`, so
the NAS must be mounted before the backend starts.

## Security notes

- The hub binds **loopback by default**. A non-loopback bind is a deliberate
  act: set `SKIPPY_FACTORY_TOKEN` (gates `/ws/factory`, the lane that can start
  agent runs) and `SKIPPY_VOICE_TOKEN` (gates `/ws/voice`), and prefer a
  private interface — Tailscale — over `0.0.0.0`. The boot log says out loud
  which protection you have.
- The bench node's factory token travels its BLE link too: the first line a
  central sends must be a hello carrying it, or the node disconnects. A
  `devices*` client can send replies and a hello and nothing else, so a
  compromised node cannot drive the agent.
- The GitHub PAT is held by the hub at `~/.skippy/github_token` (mode 0600) and
  injected via `GIT_ASKPASS` — it never appears in remote URLs, repo config, or
  the transcript. Scope it minimally: Contents read/write, Administration
  read/write (repo creation), Metadata read.
- Web content, fetched pages, and any decompiled or third-party source is
  untrusted input to the agent loop. Keep human-approval gates on destructive
  tools.
- The command allowlist stops accidents, not a determined agent. Anything that
  can run `pytest` can run arbitrary code, because it can also write
  `conftest.py`. Treat the workspace roots as the real blast radius, and run
  against repos you can restore from git. If you ever point Skippy at code you
  do not trust, put the whole process in a VM; no setting in this repo
  substitutes for that.
