import os
import json
import re
import asyncio
import logging
import tempfile
import base64
import io
import uuid
import soundfile as sf
from typing import Dict
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
import chromadb
import whisper
from kokoro_onnx import Kokoro

# --- SETUP LOGGING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("skippy_factory")

# Models are addressed by role (fast / heavy / compressor); skippy_llm owns the
# url and weight mapping, and refuses to reach off-machine unless asked to.
import skippy_llm

# --- CONNECT NAS MEMORY ---
NAS_MEMORY_PATH = "/Volumes/skippy_memory/chroma_db"
os.makedirs(NAS_MEMORY_PATH, exist_ok=True)
chroma_client = chromadb.PersistentClient(path=NAS_MEMORY_PATH)
memory_collection = chroma_client.get_or_create_collection(name="skippy_longterm")
code_collection = chroma_client.get_or_create_collection(name="skippy_code_projects")

# --- INITIALIZE VOICE ENGINES ---
logger.info("Loading Whisper Speech Engine...")
whisper_model = whisper.load_model("base")
logger.info("Loading Kokoro Voice Engine...")
kokoro = Kokoro("kokoro-v1.0.int8.onnx", "voices-v1.0.bin")

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
        
        await self.active_connections[target_client].send_json(payload)
        
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
            return response
        except asyncio.TimeoutError:
            del self.pending_responses[task_id]
            return {"error": f"Timeout: '{target_client}' did not respond within {timeout} seconds."}

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

# --- TTS HELPER ---
async def speak_text(text: str, websocket: WebSocket, use_tts: bool):
    if not use_tts or not websocket: return
    
    clean_text = " ".join([line for line in text.split("\n") if not line.startswith("`") and not line.startswith("{")])
    if not clean_text.strip(): return
    sentences = re.split(r'(?<=[.!?]) +', clean_text)
    
    def generate_tts(t):
        samples, sample_rate = kokoro.create(t, voice="am_michael", speed=1.25, lang="en-us")
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
    logger.info("Skippy core online. Agent runtime not yet installed.")
    yield
    logger.info("Skippy core offline.")

app = FastAPI(title="Skippy Coding & RE Agent API", lifespan=lifespan)

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

            # The shop assembly line was archived at tag `shop-v1`; the agent loop
            # that replaces it lands in a later slice. Answer honestly rather than
            # dropping the message on the floor, so clients show something useful.
            await websocket.send_json({
                "type": "chat",
                "content": (
                    "The agent runtime is not installed yet. The shop pipeline was "
                    "archived at tag `shop-v1`; the coding and RE agent replaces it."
                ),
            })
            await websocket.send_json({"type": "done"})

    except WebSocketDisconnect:
        hub.disconnect(client_id)

@app.get("/ping")
async def ping():
    return {"status": "Skippy is awake and the event loop is spinning!"}

@app.get("/health")
async def health():
    """Which weights are actually serving each role, and whether any is off-machine."""
    return {
        "cloud_allowed": skippy_llm.cloud_allowed(),
        "roles": {
            role: {"model": target.model, "url": target.url, "local": target.is_local}
            for role, target in skippy_llm.MODELS.items()
        },
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
        result = await asyncio.to_thread(whisper_model.transcribe, input_path, fp16=False)
    finally:
        os.remove(input_path)
    return {"text": result["text"].strip()}

if __name__ == "__main__":
    import uvicorn
    # ws_max_size raised so base64-encoded photo attachments (e.g. iPhone JPGs)
    # fit in a single websocket message (default is 16MB).
    uvicorn.run("skippy_factory:app", host="0.0.0.0", port=8000, reload=False, ws_max_size=100 * 1024 * 1024)