# App: selectable project context, resumable chats, workspace creation

Design handoff. Three features the app is missing, the code that has to change,
and the decisions already made. Written from a session that mapped the
architecture but did not start the build.

## What the user asked for

1. **Selectable project context in chat, and jumping back into a past chat.**
   Today the chat header has no project picker, and the "Recent sessions" list
   in the context rail is display-only — you cannot tap a past conversation and
   continue it.
2. **Creating a new project/workspace from the app's Work/Repo section.** Today
   the Repo panel can make a git repo *under* an existing workspace root, but
   there is no way to add a new workspace root / project.

## Decisions already made with the user

- **One folder = one project = its own memory and chat list.** Picking a
  "project" in chat scopes which project memory is injected and where the chat
  transcript is stored. This maps to `skippy_memory`'s existing `project_id`.
- **Chats live on the NAS.** As of this session the Synology `skippy_memory`
  share is mounted at `/Volumes/skippy_memory` and `memory_root()` resolves
  there, so transcripts stored under `memory_root()` are automatically shared
  between the Studio and the MacBook. That shared store is what makes "resume a
  chat from either machine" work.
- **Degrade, never crash.** A transcript write that fails (NAS unmounted) must
  cost the chat its persistence, not the turn — matching the existing rule for
  sessions ("the NAS being unmounted should cost the session its continuity,
  not its existence").
- **Runs stay sandboxed to workspace roots exactly as now.** The picker scopes
  memory and chat storage first; narrowing the sandbox to the selected
  project's root is an optional later step, not required for v1.

## How it works today (verified this session)

### Hub
- `skippy_paths.configured_workspace_roots()` reads `SKIPPY_WORKSPACE_ROOTS`
  (os.pathsep list). There is **no runtime API to change roots** — set at boot
  by SkippyServer / `run_hub.sh`. `memory_root()` defaults to
  `/Volumes/skippy_memory`, falling back to `~/.skippy/memory` when unmounted.
- `TaskRunner` (`skippy_tasks.py`) takes a `roots_provider` callable
  (`__init__` ~L87; defaults to `configured_workspace_roots`). `start()`
  (~L608) reads only `text`, `mode`, `history`, `target` off a run request —
  **no `project` / `workspace` / `session_id` field.** Coding/RE runs sandbox
  **all** configured roots; chat runs use no sandbox.
- `_chat_messages()` (~L717) opens project memory via
  `skippy_memory.open_project(workspace_roots=self.roots_provider())` and
  injects `opening_context()`. Chat history is whatever the client sends as
  `history` (client-side only; capped ~40 turns).
- `/ws/factory` router (`skippy_factory.py` ~L340-483) dispatches actions:
  `status`, `memory`, `git`, `git_commit/branch/push/pull`, `git_new`,
  `git_clone`, `github`, `files`, `file`, `cancel`, `steer`, `re_*`; anything
  else → `runner.start()`. Add new actions here.
- **`_run_chat` does not call `record_session`** — chat produces no memory
  today at all. Code/RE runs record on terminal outcomes (`skippy_agent` ~L669).

### Memory (`skippy_memory.py`)
- `ProjectMemory` (~L153) stores under `{memory_root}/sessions/projects/{project_id}/`
  with `sessions/`, `decisions/`, `work_items/`, `research/`, `meta.json`.
- `record_session()` (~L214) / `sessions()` (~L259) store and read **summaries**,
  not transcripts. `session_id` is a time-stamp filename, **not a resume handle**.
- `open_project(root, workspace_roots, project_id)` (~L924) — already accepts an
  explicit `project_id`; clients just never send one.
- `project_id_for(roots)` (~L99) derives an id from root basenames.
- `list_projects()` (~L943) exists in Python but is **not on the wire**.
- Atomic writes via `_write_json` (~L129); reads tolerate missing/corrupt via
  `_read_json`.

### Apps
- **Mac** (`apps/SkippyMac/SkippyMac/`): `SidebarPage` = work / reverse / repo /
  voice / settings (`Models.swift` ~L21). Work → `ChatView`; Repo → `RepoView`
  (git panel with New-repo/Clone sheets → `git_new`/`git_clone`). `FactoryClient`
  (~L209) `send()` builds the run payload (`text`/`mode`/`history`/`target`) —
  add `project` here. Context rail `SessionRow` shows sessions but is not
  tappable. `gitSelectedRepo` scopes the panel only, not runs.
- **Phone** (`apps/SkippyPhone/`): tabs work / voice / memory / settings; no Repo
  page; same run wire format; same gaps.
- Both talk the same `/ws/factory` JSON. Build the wire protocol once, wire both.

## Proposed build order

1. **Transcript store (hub).** New `skippy_memory` methods: `save_chat` /
   `list_chats` / `load_chat` under `{project}/chats/{chat_id}.json`
   (append turns; degrade on write failure). New factory actions `chats` and
   `chat_open`. Run/chat requests carry a `chat_id`; the hub appends each
   user+assistant turn. **This alone delivers "jump back into a chat."**
2. **Project selection.** `projects` action (wrap `list_projects`). Add
   `project` to the run payload; thread it into `open_project(project_id=...)`
   and the transcript store. Project picker in the chat header (both apps).
3. **Workspace creation.** `workspace_new` action: create a folder under
   `workspaces_root()` (`~/skippy-workspaces`), `git init`, add it to a
   hub-managed roots list persisted to a config file that `run_hub.sh` /
   SkippyServer read at boot (roots become hub state, not pure env). New-project
   UI in Work/Repo.

Validate hub changes with the test suite (`python -m pytest`, run outside the
sandbox — `git init` and temp-dir tests need real fs/network). Swift builds go
through the MacBook over SSH (see below); there is no Xcode on the Studio.

## Operational notes for the new session

- **Swift builds run on the MacBook**, `blake@192.168.1.248` (SSH keys already
  work). Source copies live at `~/skippy-build/SkippyMac` and
  `~/skippy-build/SkippyPhone`; sync with `rsync`, build with `xcodebuild`.
  Mac app: `-configuration Release -derivedDataPath ../dd-mac`, then
  `ditto` the `.app` into `/Applications`. Phone: signing needs Xcode's GUI
  (Apple ID session) — the user hits Run in Xcode with the phone attached;
  command-line signing fails (`No Account for Team`). iPhone device id
  `00008140-000425110062201C`.
- **Hub restart is pending** from the NAS switch — the running hub predates the
  mount. Restart it so memory writes go entirely to the NAS, not split with the
  local fallback. (Hub is launched outside a user LaunchAgent — via SkippyServer
  / launchd; confirm how before bouncing.)
- Repo git root: `/Users/blakeweinberg/skippy`, remote
  `github.com/blake805/skippy`, branch `main` (pushed through `a6e2eb5`).
- Secrets convention: machine-specific secrets in `~/.skippy_secrets` (sourced
  by `run_hub.sh`) and the Keychain; never in tracked files.
