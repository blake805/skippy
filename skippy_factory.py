import os
import json
import re
import httpx
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

# --- TRI-SERVER ROUTING ---
LOCAL_70B_URL = "http://127.0.0.1:8080/v1/chat/completions"
LOCAL_405B_URL = "http://127.0.0.1:8081/v1/chat/completions"
LOCAL_COMPRESSOR_URL = "http://127.0.0.1:8082/v1/chat/completions" # <-- NEW COMPRESSOR NODE

MODEL_70B_NAME = "mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit"
MODEL_405B_NAME = "mlx-community/Qwen3-Coder-480B-A35B-Instruct-4bit"
MODEL_COMPRESSOR_NAME = "mlx-community/Qwen2.5-Coder-32B-Instruct-4bit" # <-- NEW COMPRESSOR MODEL

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

# --- LEAKED TOOL CALL RECOVERY ---
def parse_leaked_tool_calls(content: str):
    """Recovers Qwen3-Coder XML-style tool calls that leaked into plain content.

    The model occasionally omits the opening <tool_call> frame token, so the
    server's state machine never enters tool-parsing mode and the raw
    <function=name><parameter=key>value</parameter></function> text lands in
    `content`. This parses those blocks into (tool_calls, cleaned_content).
    """
    calls = []
    for m in re.finditer(r'<function=([\w.-]+)>(.*?)</function>', content, re.DOTALL):
        name = m.group(1)
        args = {}
        for pm in re.finditer(r'<parameter=([\w.-]+)>\n?(.*?)\n?</parameter>', m.group(2), re.DOTALL):
            value = pm.group(2).strip()
            # Structured values (arrays, objects, numbers, booleans) arrive as
            # JSON text; plain strings like "APPROVE" stay strings.
            try:
                args[pm.group(1)] = json.loads(value)
            except (json.JSONDecodeError, ValueError):
                args[pm.group(1)] = value
        calls.append({"id": str(uuid.uuid4()), "name": name, "arguments": args})
    if calls:
        content = re.sub(r'<function=[\w.-]+>.*?</function>', '', content, flags=re.DOTALL)
        content = content.replace("<tool_call>", "").replace("</tool_call>", "").strip()
    return calls, content

# --- ASYNC HELPERS ---
async def query_model_message(messages: list, temp: float = 0.2, url: str = LOCAL_70B_URL, model_name: str = MODEL_70B_NAME, stop_sequences: list = None, tool_schemas: list = None, max_tokens: int = 4096, repetition_penalty: float = None) -> dict:
    # repetition_penalty ~1.05 stops the degenerate sentence-repetition loops
    # prose roles (Architect/QA/Summarizer) fall into at low temps, but it MUST
    # NOT be applied to the Engineer: penalizing repeated tokens corrupts code
    # (e.g. regexes lose their closing parentheses).
    """Queries an MLX server and returns the full assistant message dict.

    With `tool_schemas` set, the model can respond with native structured
    `tool_calls` (parsed server-side by mlx_lm) instead of JSON-in-text.
    Returns {"content": str, "tool_calls": [{"id", "name", "arguments"(dict)}]}.
    """
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temp,
        "max_tokens": max_tokens
    }
    if stop_sequences:
        payload["stop"] = stop_sequences
    if tool_schemas:
        payload["tools"] = tool_schemas
    if repetition_penalty:
        payload["repetition_penalty"] = repetition_penalty
        # The server's default penalty window is 20 tokens — too short to catch
        # the sentence-length loops these models produce. Widen it.
        payload["repetition_context_size"] = 512
    async with httpx.AsyncClient() as client:
        for attempt in range(3):
            try:
                response = await client.post(url, json=payload, timeout=600.0)
                if response.status_code == 200:
                    message = response.json()["choices"][0]["message"]
                    tool_calls = []
                    for tc in message.get("tool_calls") or []:
                        func = tc.get("function", {})
                        raw_args = func.get("arguments", "{}")
                        try:
                            args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                        except json.JSONDecodeError:
                            args = {"_malformed_arguments": raw_args}
                        tool_calls.append({
                            "id": tc.get("id", str(uuid.uuid4())),
                            "name": func.get("name", ""),
                            "arguments": args,
                        })
                    content = (message.get("content") or "").strip()
                    if tool_schemas and "<function=" in content:
                        leaked_calls, content = parse_leaked_tool_calls(content)
                        tool_calls.extend(leaked_calls)
                    return {"content": content, "tool_calls": tool_calls}
            except Exception:
                pass
            await asyncio.sleep(2.0 * (2 ** attempt))
        return {"content": f"System Error: Failed to connect to MLX Server at {url}.", "tool_calls": []}

async def query_model_async(messages: list, temp: float = 0.2, url: str = LOCAL_70B_URL, model_name: str = MODEL_70B_NAME, stop_sequences: list = None) -> str:
    """Text-only convenience wrapper (triage, compressor, summarizer)."""
    message = await query_model_message(messages, temp=temp, url=url, model_name=model_name, stop_sequences=stop_sequences)
    return message["content"]

def assistant_turn(message: dict) -> dict:
    """Rebuilds an assistant message (with tool_calls) for the conversation history."""
    turn = {"role": "assistant"}
    if message["content"]:
        turn["content"] = message["content"]
    if message["tool_calls"]:
        turn["tool_calls"] = [
            {
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])},
            }
            for tc in message["tool_calls"]
        ]
    return turn

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