import os
import json
import re
import asyncio
import logging
import tempfile
import base64
import io
import uuid
from typing import Dict
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File

# --- SETUP LOGGING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("skippy_factory")

# Models are addressed by role (fast / heavy / compressor); skippy_llm owns the
# url and weight mapping, and refuses to reach off-machine unless asked to.
import skippy_llm
import skippy_fs
import skippy_tasks
import skippy_voice
from skippy_sandbox import SandboxError

# Chroma, Whisper and Kokoro are all loaded on first use rather than at import.
# Two reasons. Importing this module no longer requires the NAS to be mounted or
# ~700MB of model weights to be present, which is what makes the hub and the
# endpoints testable in CI. And `python skippy_factory.py` used to load Whisper
# and Kokoro *twice* — once when __main__ ran the module top level, then again
# when uvicorn imported `skippy_factory` by name.

# --- CONNECT NAS MEMORY ---
import skippy_paths

_chroma_state: dict = {}

def get_chroma() -> dict:
    """Open the Chroma store on first use."""
    if "client" not in _chroma_state:
        import chromadb

        client = chromadb.PersistentClient(path=skippy_paths.chroma_path())
        _chroma_state["client"] = client
        _chroma_state["memory"] = client.get_or_create_collection(name="skippy_longterm")
        _chroma_state["code"] = client.get_or_create_collection(name="skippy_code_projects")
    return _chroma_state

class _LazyCollection:
    """Resolves to a real Chroma collection on first attribute access.

    Keeps `memory_collection` / `code_collection` usable as module globals by
    `tools.py` without paying the Chroma import at startup.
    """

    def __init__(self, key: str):
        self._key = key

    def _target(self):
        return get_chroma()[self._key]

    def __getattr__(self, name):
        return getattr(self._target(), name)

memory_collection = _LazyCollection("memory")
code_collection = _LazyCollection("code")

# --- VOICE ENGINES (loaded on first transcription / TTS request) ---
_voice_state: dict = {}

def get_whisper():
    if "whisper" not in _voice_state:
        import whisper

        logger.info("Loading Whisper Speech Engine...")
        _voice_state["whisper"] = whisper.load_model("base")
    return _voice_state["whisper"]

def get_kokoro():
    if "kokoro" not in _voice_state:
        from kokoro_onnx import Kokoro

        logger.info("Loading Kokoro Voice Engine...")
        _voice_state["kokoro"] = Kokoro("kokoro-v1.0.int8.onnx", "voices-v1.0.bin")
    return _voice_state["kokoro"]

# --- MULTI-CLIENT CONNECTION MANAGER ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.pending_responses: Dict[str, asyncio.Future] = {}
        # Pending human-authorization futures keyed by websocket identity.
        # The endpoint loop is the ONLY reader of each websocket; pipelines
        # wait on these futures instead of calling receive() themselves,
        # which would race the endpoint loop for incoming frames.
        self.pending_auth: Dict[int, asyncio.Future] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket
        logger.info(f"Client Connected: {client_id}")

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]
            logger.info(f"Client Disconnected: {client_id}")

    async def execute_tool_on_client(self, target_client: str, payload: dict, timeout=10.0) -> dict:
        if target_client not in self.active_connections:
            return {"error": f"Client '{target_client}' is offline."}
        
        task_id = str(uuid.uuid4())
        payload["task_id"] = task_id
        
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending_responses[task_id] = future

        # The send is inside the try because a dead socket raises: without this,
        # the exception escapes to the caller and the future is stranded, so every
        # later assumption that pending_responses drains is quietly false.
        try:
            await self.active_connections[target_client].send_json(payload)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return {"error": f"Timeout: '{target_client}' did not respond within {timeout} seconds."}
        except Exception as exc:
            return {"error": f"Transport failure sending to '{target_client}': {exc}"}
        finally:
            # pop, not del: resolve_response may already have removed it.
            self.pending_responses.pop(task_id, None)

    def resolve_response(self, task_id: str, data: dict):
        if task_id in self.pending_responses:
            future = self.pending_responses[task_id]
            if not future.done():
                future.set_result(data)
            del self.pending_responses[task_id]

    async def request_authorization(self, websocket: WebSocket, payload: dict, timeout: float = 300.0) -> dict:
        """Sends an auth request and waits for the human's reply, delivered by
        the endpoint loop via resolve_auth(). Returns the reply dict, or a
        DENY-equivalent on timeout/disconnect."""
        key = id(websocket)
        future = asyncio.get_running_loop().create_future()
        self.pending_auth[key] = future
        try:
            await websocket.send_json(payload)
            return await asyncio.wait_for(future, timeout=timeout)
        except (asyncio.TimeoutError, Exception):
            return {"status": "DENY", "reason": "timeout or connection error"}
        finally:
            self.pending_auth.pop(key, None)

    def resolve_auth(self, websocket: WebSocket, data: dict) -> bool:
        """Called by the endpoint loop when a frame arrives while an auth
        request is pending on this websocket. Returns True if consumed."""
        future = self.pending_auth.get(id(websocket))
        if future and not future.done():
            future.set_result(data)
            return True
        return False

hub = ConnectionManager()

# Owns the one-run-per-client lifecycle. Built on the same hub so a run's events
# follow the client rather than the socket it started on.
runner = skippy_tasks.TaskRunner(hub)

# --- TTS HELPER ---
async def speak_text(text: str, websocket: WebSocket, use_tts: bool):
    if not use_tts or not websocket: return
    
    clean_text = " ".join([line for line in text.split("\n") if not line.startswith("`") and not line.startswith("{")])
    if not clean_text.strip(): return
    sentences = re.split(r'(?<=[.!?]) +', clean_text)
    
    def generate_tts(t):
        import soundfile as sf

        samples, sample_rate = get_kokoro().create(t, voice="am_michael", speed=1.25, lang="en-us")
        wav_io = io.BytesIO()
        sf.write(wav_io, samples, sample_rate, format='WAV')
        return base64.b64encode(wav_io.getvalue()).decode('utf-8')
        
    for sentence in sentences:
        if len(sentence.strip()) > 1:
            try:
                wav_base64 = await asyncio.to_thread(generate_tts, sentence.strip())
                await websocket.send_json({"type": "audio", "data": wav_base64})
            except Exception as e:
                logger.error(f"TTS Stream Error: {e}")

# --- FASTAPI LIFESPAN ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Log the resolved roles at boot so a misconfigured or off-machine endpoint is
    # obvious immediately rather than at the first inference call.
    logger.info("Model roles:\n%s", skippy_llm.describe_registry())
    if not skippy_llm.cloud_allowed():
        logger.info("Cloud escalation is off. Set SKIPPY_ALLOW_CLOUD=1 to enable it.")
    try:
        logger.info("Workspace roots: %s", skippy_fs.build_sandbox().roots)
    except SandboxError as exc:
        # Not fatal: the server is still useful for voice and health checks, and a
        # loud warning at boot beats a confusing failure on the first tool call.
        logger.warning("No workspace access: %s", exc)
    logger.info("Skippy core online.")
    yield
    await runner.shutdown()
    logger.info("Skippy core offline.")

app = FastAPI(title="Skippy Coding & RE Agent API", lifespan=lifespan)

# The realtime speech-to-speech lane (/ws/voice). Lives in its own module with
# its own engines and wire protocol; see skippy_voice's docstring for both.
app.include_router(skippy_voice.router)

# --- MULTI-CLIENT WEBSOCKET ENDPOINT ---
@app.websocket("/ws/factory")
async def factory_endpoint(websocket: WebSocket, client_id: str = "swiftui"):
    await hub.connect(websocket, client_id)
    
    try:
        while True:
            raw_input = await websocket.receive_text()
            try:
                data = json.loads(raw_input)
            except json.JSONDecodeError:
                data = {"mode": "Agent", "text": raw_input, "history": [], "use_tts": False}

            if "task_id" in data:
                hub.resolve_response(data["task_id"], data)
                continue

            # Auth replies (APPROVE/DENY) go to the task that asked, not a new one.
            if "status" in data and "text" not in data:
                if hub.resolve_auth(websocket, data):
                    continue

            # The editor extension announces itself when it connects. It answers RPCs
            # and does not start work of its own, so without this its greeting would
            # launch an agent run with "hello" as the task.
            if data.get("type") == "hello":
                logger.info("Client '%s' announced itself.", client_id)
                continue

            # Read-only queries for the app's cockpit. Answered inline — neither
            # starts work, so neither goes through the runner's task lifecycle.
            if data.get("action") == "status":
                payload = runner.status(client_id)
                payload["type"] = "status"
                await websocket.send_json(payload)
                continue

            if data.get("action") == "memory":
                # Off-thread: the snapshot stats decision paths on what may be a
                # slow NAS mount, and this loop is the socket's only reader.
                payload = await asyncio.to_thread(runner.memory_snapshot)
                payload["type"] = "memory"
                await websocket.send_json(payload)
                continue

            # RE dashboard queries. Read-only, answered inline. re_notes with a
            # pack_id returns that pack's findings; without one, the pack list.
            if data.get("action") == "re_notes":
                payload = await asyncio.to_thread(
                    runner.re_snapshot, str(data.get("pack_id") or "")
                )
                payload["type"] = "re_notes"
                await websocket.send_json(payload)
                continue

            if data.get("action") == "re_devices":
                payload = await runner.re_devices(str(data.get("host") or "studio"))
                payload["type"] = "re_devices"
                await websocket.send_json(payload)
                continue

            if data.get("action") == "re_add_finding":
                payload = await asyncio.to_thread(runner.re_add_finding, data)
                payload["type"] = "re_finding_saved"
                await websocket.send_json(payload)
                continue

            # Repo panel queries and actions. `git` is read-only; `git_commit`
            # and `git_branch` write, but only on the human's explicit click —
            # the approval card gates the agent's commits, not these.
            if data.get("action") == "git":
                payload = await runner.git_snapshot(str(data.get("repo") or ""))
                payload["type"] = "git"
                await websocket.send_json(payload)
                continue

            if data.get("action") == "git_commit":
                payload = await runner.git_commit_action(data)
                payload["type"] = "git_result"
                await websocket.send_json(payload)
                continue

            if data.get("action") == "git_branch":
                payload = await runner.git_branch_action(data)
                payload["type"] = "git_result"
                await websocket.send_json(payload)
                continue

            if data.get("action") == "cancel":
                stopped = runner.cancel(client_id)
                await websocket.send_json({
                    "type": "chat",
                    "content": "Stopping at the next step." if stopped else "Nothing is running.",
                })
                continue

            # Started rather than awaited: this loop is the only reader of the socket,
            # so awaiting the run here would mean no cancel could ever arrive.
            await runner.start(client_id, data)

    except WebSocketDisconnect:
        hub.disconnect(client_id)

@app.get("/ping")
async def ping():
    return {"status": "Skippy is awake and the event loop is spinning!"}

@app.get("/health")
async def health():
    """Which weights serve each role, whether any is off-machine, and what Skippy can see."""
    # Reported rather than constructed: a bad root should show up here as a plain
    # error instead of taking the endpoint down.
    try:
        roots = skippy_fs.build_sandbox().roots
        roots_error = None
    except SandboxError as exc:
        roots, roots_error = [], str(exc)

    return {
        "cloud_allowed": skippy_llm.cloud_allowed(),
        "roles": {
            role: {"model": target.model, "url": target.url, "local": target.is_local}
            for role, target in skippy_llm.MODELS.items()
        },
        "workspace_roots": roots,
        "workspace_roots_error": roots_error,
    }

@app.post("/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    # Temp file avoids path issues from client-supplied filenames, and running
    # Whisper in a worker thread keeps the event loop (websockets, heartbeat)
    # responsive during transcription.
    suffix = os.path.splitext(file.filename or "audio.wav")[1] or ".wav"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        input_path = tmp.name
    try:
        result = await asyncio.to_thread(get_whisper().transcribe, input_path, fp16=False)
    finally:
        os.remove(input_path)
    return {"text": result["text"].strip()}

DEFAULT_BIND_HOST = "127.0.0.1"
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def bind_host() -> str:
    """Where to listen. Loopback unless someone deliberately says otherwise.

    This used to be `0.0.0.0`, which put the whole agent on the local network. There
    is no authentication on `/ws/factory` — `client_id` is a query parameter — and any
    message that is not a reply, a greeting or a cancel starts an agent run. So on a
    `0.0.0.0` bind, anything that can reach port 8000 can edit files in the workspace
    roots and run commands through `run_command`: write a script with apply_patch, run
    it with the interpreter, and the allowlist has been walked around entirely.

    ADR 0014 accepted the missing authentication on the stated grounds that the bind
    was loopback. It was not; the app's own boot line is `python skippy_factory.py`,
    which took this default. Binding loopback is what makes that reasoning true.

    The override exists because remote access is a real requirement, and the answer
    there is a private interface (Tailscale) rather than a public one — so a bind to a
    non-loopback address is a deliberate act that says so out loud in the log.
    """
    host = os.environ.get("SKIPPY_BIND_HOST", "").strip() or DEFAULT_BIND_HOST
    if host not in LOOPBACK_HOSTS:
        logger.warning(
            "Binding %s, which is not loopback. /ws/factory has no authentication and "
            "can start agent runs, so anything that can reach this port can edit your "
            "workspace roots and run commands. Only do this on a private interface.",
            host,
        )
    return host


if __name__ == "__main__":
    import uvicorn
    # ws_max_size raised so base64-encoded photo attachments (e.g. iPhone JPGs)
    # fit in a single websocket message (default is 16MB).
    #
    # ws="wsproto" rather than the default websockets backend: the legacy
    # websockets protocol asserts in _drain_helper when a ping/pong control
    # frame written by its own read loop races an application data frame,
    # which is exactly the traffic shape of /ws/voice — a heartbeating client
    # receiving audio bursts. The first Core2 that connected crashed the
    # endpoint this way. wsproto serializes writes and has no such race.
    uvicorn.run(
        "skippy_factory:app",
        host=bind_host(),
        port=int(os.environ.get("SKIPPY_PORT", "8000")),
        reload=False,
        ws="wsproto",
        ws_max_size=100 * 1024 * 1024,
    )