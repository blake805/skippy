import os
import json
import re
import asyncio
import logging
import tempfile
import subprocess
import base64
import io
import uuid
from typing import Dict, Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File

# --- IMPORT MODULARIZED LOGIC ---
from prompts import PROMPTS
import tools
import skippy_agent
import skippy_cursor
import skippy_llm
import skippy_paths
import skippy_sessions
from skippy_llm import MODELS

# --- SETUP LOGGING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("skippy_factory")

# --- TRI-SERVER ROUTING (roles live in skippy_llm.py) ---
LOCAL_70B_URL = MODELS["fast"].url                      # fast router / triage / QA
LOCAL_405B_URL = MODELS["heavy"].url                    # heavy coding brain
LOCAL_COMPRESSOR_URL = MODELS["compressor"].url         # RAG / tool-dump compressor

MODEL_70B_NAME = MODELS["fast"].model
MODEL_405B_NAME = MODELS["heavy"].model
MODEL_COMPRESSOR_NAME = MODELS["compressor"].model

# --- CONNECT NAS MEMORY ---
NAS_MEMORY_PATH = skippy_paths.chroma_path()
_chroma_state: dict = {}

def get_chroma():
    """Lazily open the Chroma store so importing this module never needs the NAS."""
    if "client" not in _chroma_state:
        import chromadb

        client = chromadb.PersistentClient(path=NAS_MEMORY_PATH)
        _chroma_state["client"] = client
        _chroma_state["memory"] = client.get_or_create_collection(name="skippy_longterm")
        _chroma_state["code"] = client.get_or_create_collection(name="skippy_code_projects")
    return _chroma_state

class _LazyCollection:
    """Stand-in that resolves to a real Chroma collection on first attribute access.

    Keeps the historic `memory_collection` / `code_collection` module globals usable
    by `tools.py` without paying the Chroma import cost at startup.
    """

    def __init__(self, key: str):
        self._key = key

    def _target(self):
        return get_chroma()[self._key]

    def __getattr__(self, name):
        return getattr(self._target(), name)

memory_collection = _LazyCollection("memory")
code_collection = _LazyCollection("code")

# Per-project sessions + Chroma collections scoped to a project_id, for the agent
# lane. The shop lane keeps using the two global collections above.
session_store = skippy_sessions.SessionStore()

# --- DYNAMIC SKILLS DIRECTORY ---
SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")
os.makedirs(SKILLS_DIR, exist_ok=True)

# --- VOICE ENGINES (lazy; loaded on first transcription / TTS request) ---
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

# --- ASYNC HELPERS ---
async def query_model_async(messages: list, temp: float = 0.2, url: str = None, model_name: str = None, stop_sequences: list = None) -> str:
    """Back-compat shim over `skippy_llm.query_model`.

    Existing call sites address endpoints by URL; resolve that back to a role so the
    request picks up the role's `max_tokens` instead of a flat 4096 cap.
    """
    target_url = url or MODELS["fast"].url
    resolved = skippy_llm.endpoint_for_url(target_url)
    if resolved is None:
        logger.warning("No registered role for %s; falling back to 'fast' limits.", target_url)
        resolved = MODELS["fast"]
    return await skippy_llm.query_model(
        messages,
        role=resolved.role,
        temp=temp,
        stop=stop_sequences,
    )

async def execute_python_code(code: str) -> str:
    if re.search(r'\bfunction\s+\w+\s*\(|\bvar\s+\w+\s*=', code) or code.strip().startswith("//"):
        return "SKIPPED EXECUTION: Code appears to be JavaScript/CPS/C++. Proceeding with static analysis only."
    try:
        run_id = str(uuid.uuid4())[:8]
        temp_dir = os.path.join(tempfile.gettempdir(), f"skippy_run_{run_id}")
        os.makedirs(temp_dir, exist_ok=True)
        temp_path = os.path.join(temp_dir, "test_script.py")
        
        with open(temp_path, 'w', encoding='utf-8') as f:
            f.write(code)

        process = await asyncio.create_subprocess_exec("python3", temp_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return "EXECUTION TIMEOUT: Code took longer than 10 seconds to run. (Expected for Web Servers)"
        finally:
            if os.path.exists(temp_path): os.remove(temp_path)
            if os.path.exists(temp_dir): os.rmdir(temp_dir)

        output = stdout.decode().strip()
        errors = stderr.decode().strip()
        
        errors = errors.replace(temp_path, "your_script.py")
        output = output.replace(temp_path, "your_script.py")

        if errors and process.returncode == 2 and "the following arguments are required:" in errors.lower():
            return f"SUCCESSFUL DRY-RUN: Script successfully initialized argparse and rejected empty arguments. (This is expected for CLI tools).\nArgparse Output:\n{errors}"

        if errors: return f"TRACEBACK / ERRORS:\n{errors}\n\nOUTPUT:\n{output}"
        elif output: return f"SUCCESSFUL OUTPUT:\n{output}"
        else: return "SUCCESS: Code ran without errors."
    except Exception as e:
        return f"EXECUTION SYSTEM ERROR: {str(e)}"

async def run_bash_command_stream(command: str, websocket: WebSocket) -> str:
    if not websocket: return "TERMINAL OUTPUT: Error, headless execution not supported for interactive stream."
    try:
        await websocket.send_json({"type": "terminal_stream_start"})
        process = await asyncio.create_subprocess_shell(command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        output_buffer = ""
        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=30.0)
            except asyncio.TimeoutError:
                process.kill()
                output_buffer += "\n[TIMEOUT: Command exceeded 30s limit.]"
                await websocket.send_json({"type": "terminal_stream", "content": "\n[TIMEOUT: Command exceeded 30s limit.]"})
                break
            if not line: break
            decoded_line = line.decode('utf-8', errors='replace')
            output_buffer += decoded_line
            await websocket.send_json({"type": "terminal_stream", "content": decoded_line})
        await process.wait()
        return f"TERMINAL OUTPUT:\n{output_buffer}"
    except Exception as e:
        return f"SYSTEM ERROR: {str(e)}"

# --- MULTI-CLIENT CONNECTION MANAGER ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}
        self.pending_responses: Dict[str, asyncio.Future] = {}

    async def connect(self, websocket: WebSocket, client_id: str):
        await websocket.accept()
        self.active_connections[client_id] = websocket
        logger.info(f"Client Connected: {client_id}")

    def disconnect(self, client_id: str):
        if client_id in self.active_connections:
            del self.active_connections[client_id]
            logger.info(f"Client Disconnected: {client_id}")

    async def execute_tool_on_client(self, target_client: str, payload: dict, timeout: float = 10.0) -> dict:
        websocket = self.active_connections.get(target_client)
        if websocket is None:
            return {"error": f"Client '{target_client}' is offline."}

        task_id = str(uuid.uuid4())
        payload["task_id"] = task_id

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending_responses[task_id] = future

        try:
            await websocket.send_json(payload)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return {"error": f"Timeout: '{target_client}' did not respond within {timeout} seconds."}
        except Exception as exc:
            return {"error": f"Transport failure talking to '{target_client}': {exc}"}
        finally:
            self.pending_responses.pop(task_id, None)

    async def request_on_socket(self, websocket: WebSocket, payload: dict, timeout: float = 300.0) -> dict:
        """Ask a specific socket a question and await the reply through the hub.

        The endpoint loop owns the only `receive_text()` call on a socket, so anything
        that needs an answer mid-task has to round-trip through `pending_responses`
        rather than reading the socket itself.
        """
        task_id = str(uuid.uuid4())
        payload["task_id"] = task_id

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending_responses[task_id] = future

        try:
            await websocket.send_json(payload)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            return {"status": "TIMEOUT", "error": f"No reply within {timeout} seconds."}
        except Exception as exc:
            return {"status": "ERROR", "error": f"Transport failure: {exc}"}
        finally:
            self.pending_responses.pop(task_id, None)

    def resolve_response(self, task_id: str, data: dict):
        future = self.pending_responses.pop(task_id, None)
        if future is not None and not future.done():
            future.set_result(data)

hub = ConnectionManager()

# RPC channel to the Cursor extension (client_id=cursor). Inert until it connects.
cursor_bridge = skippy_cursor.CursorBridge(hub)

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

# --- PIPELINE STATE MACHINE ---
class SkippyPipeline:
    def __init__(self, websocket: Optional[WebSocket], payload: dict, manager: ConnectionManager):
        self.ws = websocket
        self.manager = manager
        self.mode = payload.get("mode", "Shop")
        self.user_input = payload.get("text", "")
        self.chat_history = payload.get("history", [])
        self.use_tts = payload.get("use_tts", False)
        
        self.blueprint = ""
        self.is_direct_reply = False
        self.success = False
        self.execution_result = ""

    async def send_log(self, msg: str):
        if not self.ws:
            logger.info(f"HEADLESS LOG: {msg.strip()}")
            return
        try:
            await self.ws.send_json({"type": "log", "content": msg})
        except Exception:
            pass 

    async def send_chat(self, msg: str):
        if not self.ws:
            logger.info(f"HEADLESS CHAT: {msg.strip()}")
            return
        try:
            await self.ws.send_json({"type": "chat", "content": msg})
        except Exception:
            pass

    def smart_truncate(self, filepath: str, content: str, max_chars: int = 15000) -> str:
        if len(content) <= max_chars:
            return content
            
        ext = os.path.splitext(filepath)[1].lower()
        if ext in ['.gcode', '.nc', '.tap']:
            head_size = 4000
            tail_size = 2000
            head = content[:head_size]
            tail = content[-tail_size:]
            omitted = len(content) - (head_size + tail_size)
            return f"{head}\n\n... [✂️ OMITTED {omitted} CHARS OF MIDDLE TOOLPATHS] ...\n\n{tail}"
        elif ext in ['.log', '.csv']:
            head_size = 2000
            tail_size = max_chars - head_size
            head = content[:head_size]
            tail = content[-tail_size:]
            omitted = len(content) - max_chars
            return f"{head}\n\n... [✂️ OMITTED {omitted} CHARS OF LOG HISTORY] ...\n\n{tail}"
        else:
            half = max_chars // 2
            head = content[:half]
            tail = content[-half:]
            omitted = len(content) - max_chars
            return f"{head}\n\n... [✂️ OMITTED {omitted} CHARS OF MIDDLE CONTENT] ...\n\n{tail}"

    async def phase_0_inject_files(self) -> str:
        enriched_input = self.user_input
        file_paths = re.findall(r'(?:~|/Users/[\w-]+)/(?:Desktop|Documents|Downloads)/[\w\.-]+', self.user_input)
        for path in file_paths:
            expanded_path = os.path.expanduser(path)
            if os.path.exists(expanded_path):
                try:
                    with open(expanded_path, 'r', errors='ignore') as f:
                        raw_content = f.read()
                    safe_content = self.smart_truncate(expanded_path, raw_content)
                    enriched_input = f"--- INJECTED FILE: {path} ---\n{safe_content}\n\n{enriched_input}"
                    if len(raw_content) > len(safe_content):
                        await self.send_log(f"✂️ *Smart Injector truncated {path} from {len(raw_content)} to {len(safe_content)} chars.*\n")
                    else:
                        await self.send_log(f"💾 *Smart Injector loaded {path} into context ({len(safe_content)} chars)*\n")
                except Exception as e:
                    await self.send_log(f"⚠️ *Failed to inject file {path}: {str(e)}*\n")
        return enriched_input

    def auto_claim_pending_tasks(self) -> bool:
        try:
            goals_file = os.path.join(os.path.dirname(__file__), "skippy_goals.json")
            if os.path.exists(goals_file):
                with open(goals_file, "r") as f:
                    data = json.load(f)
                
                changed = False
                for t in data.get("tasks", []):
                    if t.get("status") == "pending":
                        t["status"] = "in_progress"
                        changed = True
                        
                if changed:
                    with open(goals_file, "w") as f:
                        json.dump(data, f, indent=2)
                    return True
        except Exception as e:
            logger.error(f"Failed to auto-claim tasks: {e}")
        return False

    def auto_complete_active_task(self) -> bool:
        """Deterministically marks 'in_progress' tasks as 'completed' in the ledger."""
        try:
            goals_file = os.path.join(os.path.dirname(__file__), "skippy_goals.json")
            if os.path.exists(goals_file):
                with open(goals_file, "r") as f:
                    data = json.load(f)
                
                changed = False
                for t in data.get("tasks", []):
                    if t.get("status") == "in_progress":
                        t["status"] = "completed"
                        changed = True
                        
                if changed:
                    with open(goals_file, "w") as f:
                        json.dump(data, f, indent=2)
                return changed
        except Exception as e:
            logger.error(f"Failed to auto-complete tasks: {e}")
        return False

    async def run(self):
        try:
            await self.send_log(f"⚙️ *Skippy processing initiated... (Mode: {self.mode})*\n")
            enriched_input = await self.phase_0_inject_files()
            await self.phase_1_research(enriched_input)
            
            if self.is_direct_reply:
                if self.ws: await self.ws.send_json({"type": "done"})
                return
            
            if self.auto_claim_pending_tasks():
                await self.send_log("🔒 *Auto-claimed pending tasks in ledger to prevent heartbeat race conditions.*\n")
                
            await self.phase_2_engineer_and_qa(enriched_input)
            await self.phase_3_summarize()
            
            if self.ws: await self.ws.send_json({"type": "done"})
        except Exception as e:
            logger.error(f"Pipeline Error: {e}")
            if self.ws:
                try:
                    await self.send_log(f"❌ *Fatal Pipeline Crash: {str(e)}*\n")
                    await self.ws.send_json({"type": "done"})
                except Exception:
                    pass

    async def phase_1_research(self, enriched_input: str):
        arch_messages = [{"role": "system", "content": PROMPTS.get(self.mode, PROMPTS["Shop"])["architect"]}]
        for msg in self.chat_history[-10:]:
            role = "user" if msg.startswith("You:") else "assistant"
            content = msg.replace("You: ", "").replace("Skippy: ", "")
            arch_messages.append({"role": role, "content": content})
            
        arch_messages.append({"role": "user", "content": enriched_input})
        
        for _ in range(8):
            await self.send_log("\n[Architect] Analyzing and researching...\n")
            arch_response = await query_model_async(
                arch_messages, 
                temp=0.2, 
                url=LOCAL_70B_URL, 
                model_name=MODEL_70B_NAME,
                stop_sequences=["TOOL RESULT:", "Observation:"]
            )
            
            json_match = re.search(r'\{.*?\}', arch_response, re.DOTALL)
            if json_match:
                try:
                    tool_data = json.loads(json_match.group(0))
                    tool_name = tool_data.get("name")
                    await self.send_log(f"*(Architect is using {tool_name}...)*\n")
                    
                    if tool_name == "direct_reply":
                        reply_msg = tool_data.get("message", "I have your answer.")
                        await self.send_log("\n[Architect] Direct conversation detected.\n")
                        await self.send_chat(reply_msg)
                        if self.ws: await speak_text(reply_msg, self.ws, self.use_tts)
                        self.is_direct_reply = True
                        break
                        
                    tool_result = "No data found."
                    if tool_name == "get_system_time": tool_result = tools.get_system_time()
                    elif tool_name == "web_search": tool_result = await tools.web_search(tool_data.get("query", ""))
                    elif tool_name == "read_website": tool_result = await tools.read_website(tool_data.get("url", ""))
                    elif tool_name == "search_memory": tool_result = await tools.search_memory(tool_data.get("query", ""), memory_collection)
                    elif tool_name == "save_memory": tool_result = await tools.save_memory(tool_data.get("fact", ""), memory_collection)
                    elif tool_name == "send_to_tormach": tool_result = await tools.send_to_tormach(tool_data.get("local_file_path", ""))
                    elif tool_name == "check_device_status": tool_result = str(await tools.check_device_status(tool_data.get("ip_address", "")))
                    elif tool_name == "run_shop_skill": tool_result = await tools.run_shop_skill(tool_data.get("skill_name", ""), tool_data.get("arguments", ""))
                    elif tool_name == "manage_goals":
                        tool_result = tools.manage_goals(
                            action=tool_data.get("action", ""),
                            task=tool_data.get("task"),
                            task_id=tool_data.get("task_id")
                        )
                    elif tool_name == "vscode_get_active_file":
                        await self.send_log(f"\n*(Architect is reaching out to VS Code...)*\n")
                        response = await self.manager.execute_tool_on_client(
                            "vscode", 
                            {"action": "get_active_file"}, 
                            timeout=5.0
                        )
                        tool_result = str(response.get("content", response))
                    elif tool_name == "tormach_ssh":
                        command = tool_data.get("command", "")
                        explanation = tool_data.get("explanation", "Executing SSH command on Tormach PathPilot.")
                        
                        await self.send_log(f"\n⚠️ [Architect] Requested Tormach SSH: {command}\nWaiting for human authorization...\n")
                        if self.ws:
                            await self.ws.send_json({"type": "terminal_auth", "command": command, "explanation": explanation})
                            auth_reply = await self.ws.receive_text()
                            auth_data = json.loads(auth_reply)
                            if auth_data.get("status") == "APPROVE":
                                await self.send_log(f"✅ Authorization GRANTED. Connecting to PathPilot...\n")
                                tool_result = await tools.execute_tormach_ssh(command)
                            else:
                                await self.send_log(f"❌ Authorization DENIED by human.\n")
                                tool_result = "USER DENIED SSH EXECUTION. Find a workaround."
                        else:
                            tool_result = "HEADLESS ERROR: Cannot request SSH authorization without UI client attached."
                    elif tool_name == "github_manager":
                        await self.send_log(f"\n*(Architect is interacting with GitHub: {tool_data.get('action')}...)*\n")
                        tool_result = await tools.execute_github_manager(
                            repo=tool_data.get("repo", ""),
                            action=tool_data.get("action", ""),
                            title=tool_data.get("title"),
                            body=tool_data.get("body")
                        )
                    elif tool_name == "read_directory_structure":
                        target_path = tool_data.get("path", "")
                        depth = int(tool_data.get("max_depth", 2))
                        await self.send_log(f"\n*(Architect is mapping directory: {target_path}...)*\n")
                        tool_result = await tools.read_directory_structure(target_path, max_depth=depth)
                    elif tool_name == "ingest_codebase_to_rag":
                        target_path = tool_data.get("path", "")
                        await self.send_log(f"\n*(Architect is chunking and embedding {target_path} into ChromaDB...)*\n")
                        tool_result = await tools.ingest_codebase_to_rag(target_path, code_collection)

                    # --- COMPRESSOR INTERCEPT FOR SEARCH ---
                    elif tool_name == "search_codebase":
                        search_query = tool_data.get("query", "")
                        await self.send_log(f"\n*(Architect is searching code memory for: {search_query}...)*\n")
                        
                        raw_tool_result = await tools.search_codebase(search_query, code_collection)
                        
                        await self.send_log(f"*(Compressing search results via 32B Node to protect Architect's context window...)*\n")
                        compressor_prompt = f"""You are a data extraction node. 
The Architect needs to know: '{search_query}'. 
Here is the raw codebase data pulled from ChromaDB: 
{raw_tool_result}

Extract ONLY the specific math, logic, formulas, variable mappings, or architecture required to answer the query. Do not use conversational filler. Keep it incredibly dense and under 400 words."""
                        
                        compressed_result = await query_model_async(
                            [{"role": "user", "content": compressor_prompt}], 
                            temp=0.1, 
                            url=LOCAL_COMPRESSOR_URL, 
                            model_name=MODEL_COMPRESSOR_NAME
                        )
                        tool_result = f"COMPRESSED MEMORY RESULT:\n{compressed_result}"
                        
                    arch_messages.append({"role": "assistant", "content": arch_response})
                    arch_messages.append({"role": "user", "content": f"TOOL RESULT:\n{tool_result}\nIf you need more info, use another tool. If you can answer directly without code, use direct_reply. Otherwise, provide the final blueprint."})
                    continue
                except json.JSONDecodeError:
                    pass
            
            self.blueprint = arch_response
            break
        else:
            self.blueprint = "RESEARCH LIMIT REACHED. Proceeding with gathered context:\n" + arch_response

        await self.send_log(f"\n----- ARCHITECT BLUEPRINT GENERATED -----\n{self.blueprint}\n----------------------------------------\n")

    async def phase_2_engineer_and_qa(self, enriched_input: str):
        await self.send_log("\n[Triage Cop] Evaluating task complexity for routing...\n")
        if self.mode == "Developer":
            is_complex = True
            await self.send_log("*(Developer Mode detected. Bypassing triage and routing to 405B Kraken...)*\n")
        else:
            triage_prompt = f"Review this blueprint:\n{self.blueprint}\nIf this requires complex architectural changes, large Python scripts, or generating G-code/firmware, output ONLY the word: COMPLEX. If it is a simple short script, basic calculation, or a single bash command, output ONLY the word: SIMPLE."
            triage_verdict = await query_model_async([{"role": "user", "content": triage_prompt}], temp=0.1, url=LOCAL_70B_URL, model_name=MODEL_70B_NAME)
            is_complex = "COMPLEX" in triage_verdict.upper()
            if is_complex: await self.send_log("*(Triage Cop classified task as COMPLEX. Routing to 405B Kraken...)*\n")
            else: await self.send_log("*(Triage Cop classified task as SIMPLE. Routing to 70B Fast Worker...)*\n")

        eng_url = LOCAL_405B_URL if is_complex else LOCAL_70B_URL
        eng_model = MODEL_405B_NAME if is_complex else MODEL_70B_NAME
        
        dev_context = ""
        if self.mode == "Developer":
            try:
                with open(__file__, 'r', encoding='utf-8') as f:
                    dev_context = f"\n\n--- CURRENT SYSTEM SOURCE CODE ({os.path.basename(__file__)}) ---\n{f.read()}"
                await self.send_log("🧠 *Auto-injected current source code into Developer context.*\n")
            except Exception as e:
                await self.send_log(f"⚠️ *Failed to auto-inject source code: {str(e)}*\n")

        base_eng_messages = [
            {"role": "system", "content": PROMPTS.get(self.mode, PROMPTS["Shop"]).get("engineer", "")},
            {"role": "user", "content": f"Blueprint:\n{self.blueprint}{dev_context}"}
        ]
        
        qa_feedback = "" 
        code_to_test = ""
        
        for attempt in range(4):
            engine_name = "405B Model" if is_complex else "70B Model"
            await self.send_log(f"\n[Engineer] Coding session started on {engine_name}. Attempt {attempt + 1}/4...\n")
            
            current_eng_messages = base_eng_messages.copy()
            if qa_feedback: current_eng_messages.append({"role": "user", "content": qa_feedback})
            
            engineer_response = await query_model_async(current_eng_messages, temp=0.1, url=eng_url, model_name=eng_model)
            await self.send_log(f"----- ENGINEER RESPONSE -----\n{engineer_response}\n-----------------------------\n")
            
            code_to_test = ""
            json_match = re.search(r'\{.*\}', engineer_response, re.DOTALL)
            
            if json_match:
                try:
                    tool_data = json.loads(json_match.group(0))
                    
                    if tool_data.get("name") == "request_terminal_execution":
                        command = tool_data.get("command", "")
                        explanation = tool_data.get("explanation", "")
                        
                        await self.send_log(f"\n⚠️ [Engineer] Requested God Mode: {command}\nWaiting for human authorization...\n")
                        if self.ws:
                            await self.ws.send_json({"type": "terminal_auth", "command": command, "explanation": explanation})
                            auth_reply = await self.ws.receive_text()
                            auth_data = json.loads(auth_reply)
                            if auth_data.get("status") == "APPROVE":
                                await self.send_log(f"✅ Authorization GRANTED. Executing: {command}...\n")
                                cmd_output = await run_bash_command_stream(command, self.ws)
                                await self.send_log(f"----- EXECUTION FINISHED -----\n")
                                qa_feedback = f"COMMAND EXECUTED. Output:\n{cmd_output}\nProceed with coding."
                            else:
                                await self.send_log(f"❌ Authorization DENIED by human.\n")
                                qa_feedback = "USER DENIED COMMAND EXECUTED. Find a workaround."
                        else:
                            qa_feedback = "HEADLESS ERROR: Cannot request god mode without UI attached."
                        continue 
                        
                    elif tool_data.get("name") == "patch_file":
                        patches = tool_data.get("patches", [])
                        with open(__file__, 'r', encoding='utf-8') as f:
                            patched_code = f.read()
                            
                        patch_success = True
                        patch_errors = ""
                        
                        for idx, p in enumerate(patches):
                            st = p.get("search_text", "")
                            rt = p.get("replace_text", "")
                            if st and st not in patched_code:
                                patch_success = False
                                patch_errors += f"\n- Patch {idx+1} Failed: Exact `search_text` not found. Check indentation."
                            elif st:
                                patched_code = patched_code.replace(st, rt)
                                
                        if not patch_success:
                            qa_feedback = f"PATCHING FAILED:{patch_errors}\nEnsure your search_text perfectly matches the source code exactly."
                            await self.send_log(f"❌ Patching Failed:{patch_errors}\n")
                            continue 
                            
                        code_to_test = patched_code
                        await self.send_log("✅ Code Patch applied successfully to virtual sandbox.\n")
                        
                except json.JSONDecodeError: 
                    pass

            if not code_to_test:
                code_match = re.findall(r'`{3}(?:python|javascript|cpp)?\n(.*?)`{3}', engineer_response, re.DOTALL)
                if code_match: code_to_test = code_match[-1].strip()
                elif "```" in engineer_response:
                    fallback_match = re.findall(r'`{3}\n(.*?)`{3}', engineer_response, re.DOTALL)
                    if fallback_match: code_to_test = fallback_match[-1].strip()
                else:
                    code_to_test = engineer_response

            if not code_to_test.strip():
                qa_feedback = "You failed to output any valid code or patch JSON."
                continue

            await self.send_log("\n[Execution Engine] Spinning up secure sandbox to test Engineer's draft...\n")
            self.execution_result = await execute_python_code(code_to_test)
            
            await self.send_log(f"\n[QA Lead] Executing automated code review & analyzing runtime logs...\n")
            
            qa_prompt_injection = f"Code to review:\n{code_to_test}\n\nLive Terminal Execution Output:\n{self.execution_result}\n\nArchitect's Blueprint (Script Requirements):\n{self.blueprint}"
            
            qa_review = await query_model_async([
                {"role": "system", "content": PROMPTS.get(self.mode, PROMPTS["Shop"]).get("qa", "")},
                {"role": "user", "content": qa_prompt_injection}
            ], temp=0.1, url=LOCAL_70B_URL, model_name=MODEL_70B_NAME)
            
            await self.send_log(f"----- QA VERDICT REPORT -----\n{qa_review}\n-----------------------------\n")

            is_fail = qa_review.strip().startswith("FAIL") or "FAIL:" in qa_review
            
            if not is_fail and ("APPROVE" in qa_review or "DEPLOY" in qa_review) and "{" in qa_review:
                try:
                    json_match = re.search(r'\{\s*"status"\s*:\s*"(?:APPROVE|DEPLOY)".*?\}\s*$', qa_review, re.DOTALL)
                    if not json_match: raise ValueError("Could not isolate the JSON block at the end of the response.")
                    
                    qa_data = json.loads(json_match.group(0))
                    status = qa_data.get("status")
                    
                    if status == "APPROVE":
                        save_path = qa_data.get("save_path", "~/Desktop/skippy_output.py")
                        
                        if save_path.startswith("skills/") or "skills" in save_path:
                            safe_name = os.path.basename(save_path)
                            local_filepath = os.path.join(SKILLS_DIR, safe_name)
                            with open(local_filepath, "w", encoding="utf-8") as f:
                                f.write(code_to_test)
                            await self.send_log(f"\n[Success] QA Sign-off acquired. Skill permanently saved to Mac Studio at {local_filepath}.\n")
                        else:
                            if self.ws:
                                await self.ws.send_json({"type": "write_file", "path": save_path, "content": code_to_test})
                                await self.send_log(f"\n[Success] QA Sign-off acquired. Payload transmitted to MacBook for native disk write.\n")
                            else:
                                fallback_name = os.path.basename(save_path)
                                fallback_path = os.path.join(os.path.dirname(__file__), fallback_name)
                                with open(fallback_path, 'w', encoding='utf-8') as f:
                                    f.write(code_to_test)
                                await self.send_log(f"\n[Success] Headless QA Sign-off acquired. Saved locally to {fallback_path} since UI is detached.\n")
                                
                        self.success = True
                        
                        # --- NEW DETERMINISTIC LEDGER COMPLETION ---
                        if self.auto_complete_active_task():
                            await self.send_log("\n✅ *Backend Auto-Completed active task in ledger following QA Approval.*\n")
                            
                        break
                        
                    elif status == "DEPLOY":
                        target_file = qa_data.get("target_file", "skippy_factory.py")
                        summary = qa_data.get("summary", "Code upgrade.")
                        
                        await self.send_log(f"\n⚠️ [QA Lead] Requested DEPLOYMENT to {target_file}\nWaiting for human authorization...\n")
                        if self.ws:
                            await self.ws.send_json({
                                "type": "deployment_auth",
                                "target_file": target_file,
                                "summary": summary,
                                "content": code_to_test
                            })
                            auth_reply = await self.ws.receive_text()
                            auth_data = json.loads(auth_reply)
                            
                            if auth_data.get("status") == "APPROVE":
                                await self.send_log(f"✅ Deployment AUTHORIZED. Overwriting {target_file}...\n")
                                expanded_path = os.path.expanduser(target_file)
                                if not "/" in expanded_path: expanded_path = os.path.join(os.getcwd(), expanded_path)
                                    
                                with open(expanded_path, 'w', encoding='utf-8') as f:
                                    f.write(code_to_test)
                                    
                                await self.send_log(f"----- DEPLOYMENT FINISHED -----\n")
                                self.success = True
                                
                                # --- NEW DETERMINISTIC LEDGER COMPLETION ---
                                if self.auto_complete_active_task():
                                    await self.send_log("\n✅ *Backend Auto-Completed active task in ledger following Deployment.*\n")
                                    
                                break
                            else:
                                await self.send_log(f"❌ Deployment DENIED by human.\n")
                                qa_feedback = "USER DENIED DEPLOYMENT AUTHORIZATION. Revise the code or abort."
                                continue 
                        else:
                            qa_feedback = "HEADLESS ERROR: Cannot request deployment authorization without UI attached."
                            continue
                except Exception as e:
                    await self.send_log(f"\n[Write Error] Exception raised during payload routing: {str(e)}\n")
                    break
            else:
                qa_feedback = f"QA FAILED ON PREVIOUS ATTEMPT. Terminal Output was:\n{self.execution_result}\nQA Feedback:\n{qa_review}\nEngineer, fix these issues."
                await self.send_log(f"\n🔄 *QA rejected code iteration {attempt + 1}. Routing critique back to development engine...*\n")

    async def phase_3_summarize(self):
        await self.send_log("\n[Executive Summarizer] Formatting final response for the user...\n")
        status_text = "Success" if self.success else "Failure"
        summary_messages = [
            {"role": "system", "content": PROMPTS.get(self.mode, PROMPTS["Shop"]).get("summarizer", "")},
            {"role": "user", "content": f"Task: {self.user_input}\nBlueprint: {self.blueprint}\nOutcome: {status_text}\nQA Feedback/Results: {self.execution_result}\nWrite the conversational summary."}
        ]
        
        final_summary = await query_model_async(summary_messages, temp=0.4, url=LOCAL_70B_URL, model_name=MODEL_70B_NAME)
        await self.send_chat(final_summary)
        if self.ws: await speak_text(final_summary, self.ws, self.use_tts)

# --- THE AUTONOMOUS HEARTBEAT LOOP ---
GOALS_FILE = os.path.join(os.path.dirname(__file__), "skippy_goals.json")


def claim_pending_project_tasks() -> list:
    """Pull ledger tasks that name a `project_id` and hand them to the agent.

    Tasks without a `project_id` are left entirely alone, so the shop's ledger
    behaviour is unchanged. Claimed tasks flip to `in_progress` here so the next
    tick does not dispatch them a second time.
    """
    if not os.path.exists(GOALS_FILE):
        return []
    try:
        with open(GOALS_FILE, "r") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []

    claimed = []
    changed = False
    for task in data.get("tasks", []):
        if task.get("project_id") and task.get("status") == "pending":
            task["status"] = "in_progress"
            claimed.append(dict(task))
            changed = True

    if changed:
        try:
            with open(GOALS_FILE, "w") as handle:
                json.dump(data, handle, indent=2)
        except OSError as exc:
            logger.error(f"Could not claim project tasks in the ledger: {exc}")
            return []
    return claimed


async def skippy_heartbeat():
    """Background task that wakes Skippy up every 5 minutes."""
    while True:
        await asyncio.sleep(300) 
        
        current_time = tools.get_system_time()

        # Project work goes to the coding agent, which can actually resume a
        # multi-file task; a blank Shop tick cannot.
        try:
            for task in claim_pending_project_tasks():
                logger.info(
                    "Heartbeat dispatching project task %s to the agent (project=%s)",
                    task.get("id"),
                    task.get("project_id"),
                )
                asyncio.create_task(
                    skippy_agent.run_agent_task(
                        None,
                        {
                            "mode": task.get("mode", "Agent"),
                            "text": task.get("task", ""),
                            "project_id": task["project_id"],
                            "session_id": task.get("session_id"),
                            "workspace_roots": task.get("workspace_roots"),
                        },
                        hub,
                        session_store=session_store,
                        cursor_bridge=cursor_bridge,
                    )
                )
        except Exception as e:
            logger.error(f"Heartbeat project dispatch failure: {str(e)}")

        system_injection = (
            f"[SYSTEM TICK] - Timestamp: {current_time}. "
            "Wake up. Use 'manage_goals' (action: 'view') to check your ledger. "
            "If tasks exist, execute the next step. If idle, report status."
        )
        
        logger.info(f"Triggering Skippy Heartbeat at {current_time}")
        
        try:
            payload = {"mode": "Shop", "text": system_injection, "history": [], "use_tts": False}
            headless_pipeline = SkippyPipeline(websocket=None, payload=payload, manager=hub)
            asyncio.create_task(headless_pipeline.run())
        except Exception as e:
            logger.error(f"Heartbeat loop failure: {str(e)}")

# --- FASTAPI LIFESPAN ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    heartbeat_task = asyncio.create_task(skippy_heartbeat())
    logger.info("Skippy Synthetic Autonomy Engine Initialized.")
    yield  
    heartbeat_task.cancel()
    logger.info("Skippy Synthetic Autonomy Engine Offline.")

app = FastAPI(title="Skippy Assembly Line API (Ultimate Routing Edition)", lifespan=lifespan)

# --- MULTI-CLIENT WEBSOCKET ENDPOINTS ---
# Modes that belong to the coding agent rather than the shop assembly line.
AGENT_MODES = {"Agent", "RE"}

# Clients that exist purely to answer RPCs. A stray message from one of these must
# never spawn a Shop pipeline.
RPC_ONLY_CLIENTS = {"cursor", "vscode"}


async def _serve_socket(websocket: WebSocket, client_id: str, default_mode: str):
    await hub.connect(websocket, client_id)

    try:
        while True:
            raw_input = await websocket.receive_text()
            try:
                data = json.loads(raw_input)
            except json.JSONDecodeError:
                data = {"mode": default_mode, "text": raw_input, "history": [], "use_tts": False}

            # Replies to anything the server asked for: RPC results and auth decisions.
            if "task_id" in data:
                hub.resolve_response(data["task_id"], data)
                continue

            message_type = data.get("type")

            if message_type == "agent_cancel":
                session_id = data.get("session_id", "")
                found = skippy_agent.cancel_session(session_id)
                await websocket.send_json(
                    {"type": "agent_cancelled", "session_id": session_id, "found": found}
                )
                continue

            if message_type in ("hello", "ping", "register"):
                await websocket.send_json({"type": "hello_ack", "client_id": client_id})
                continue

            if client_id in RPC_ONLY_CLIENTS and not data.get("text"):
                logger.info("Ignoring non-task message from RPC client '%s': %s", client_id, message_type)
                continue

            mode = data.get("mode") or default_mode
            if mode in AGENT_MODES:
                data.setdefault("mode", mode)
                asyncio.create_task(
                    skippy_agent.run_agent_task(
                        websocket,
                        data,
                        hub,
                        session_store=session_store,
                        speak=speak_text,
                        cursor_bridge=cursor_bridge,
                    )
                )
                continue

            pipeline = SkippyPipeline(websocket, data, hub)
            asyncio.create_task(pipeline.run())

    except WebSocketDisconnect:
        hub.disconnect(client_id)


@app.websocket("/ws/factory")
async def factory_endpoint(websocket: WebSocket, client_id: str = "swiftui"):
    """Shop lane by default; `mode: "Agent"` routes the same socket to SkippyAgent."""
    await _serve_socket(websocket, client_id, default_mode="Shop")


@app.websocket("/ws/agent")
async def agent_endpoint(websocket: WebSocket, client_id: str = "agent"):
    """Coding-agent lane. Identical handler, different default mode."""
    await _serve_socket(websocket, client_id, default_mode="Agent")

@app.get("/ping")
async def ping():
    return {"status": "Skippy is awake and the event loop is spinning!"}

@app.post("/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    input_path = f"incoming_{file.filename}"
    with open(input_path, "wb") as buffer: buffer.write(await file.read())
    result = get_whisper().transcribe(input_path, fp16=False)
    os.remove(input_path) 
    return {"text": result["text"].strip()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("skippy_factory:app", host="0.0.0.0", port=8000, reload=False)