import os
import json
import re
import shlex
import httpx
import asyncio
import logging
import tempfile
import subprocess
import base64
import io
import uuid
import soundfile as sf
from typing import Dict, Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
import chromadb
import whisper
from kokoro_onnx import Kokoro

# --- IMPORT MODULARIZED LOGIC ---
from prompts import PROMPTS
import tools
from tool_schemas import get_architect_tools, get_engineer_tools, QA_TEST_TOOL, QA_VERDICT_TOOL

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

# --- DYNAMIC SKILLS DIRECTORY ---
SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")
os.makedirs(SKILLS_DIR, exist_ok=True)

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

async def execute_python_code(code: str, cli_args: list = None) -> str:
    if re.search(r'\bfunction\s+\w+\s*\(|\bvar\s+\w+\s*=', code) or code.strip().startswith("//"):
        return "SKIPPED EXECUTION: Code appears to be JavaScript/CPS/C++. Proceeding with static analysis only."
    try:
        run_id = str(uuid.uuid4())[:8]
        temp_dir = os.path.join(tempfile.gettempdir(), f"skippy_run_{run_id}")
        os.makedirs(temp_dir, exist_ok=True)
        temp_path = os.path.join(temp_dir, "test_script.py")
        
        with open(temp_path, 'w', encoding='utf-8') as f:
            f.write(code)

        process = await asyncio.create_subprocess_exec("python3", temp_path, *(cli_args or []), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
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

        # Report the exit code honestly: a script that prints an error and
        # sys.exit(1)s used to be reported as "SUCCESSFUL OUTPUT", which
        # tricked QA into approving broken code.
        if process.returncode != 0:
            return f"SCRIPT FAILED (exit code {process.returncode}).\nSTDERR:\n{errors}\nSTDOUT:\n{output}"
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

# --- PIPELINE STATE MACHINE ---
class SkippyPipeline:
    def __init__(self, websocket: Optional[WebSocket], payload: dict, manager: ConnectionManager):
        self.ws = websocket
        self.manager = manager
        self.mode = payload.get("mode", "Shop")
        self.user_input = payload.get("text", "")
        self.chat_history = payload.get("history", [])
        self.use_tts = payload.get("use_tts", False)

        # Binary attachments (images etc.) arrive base64-encoded from the
        # client and are saved locally so tools like edit_image can use them.
        attachment = payload.get("attachment")
        if attachment and attachment.get("data_base64"):
            saved_path = self._save_attachment(attachment)
            if saved_path:
                self.user_input += (
                    f"\n\n[SYSTEM NOTE: The user attached a binary file, saved on the Mac Studio at: "
                    f"{saved_path} — for images, use the edit_image tool with this exact path.]"
                )

        self.blueprint = ""
        self.is_direct_reply = False
        self.success = False
        self.execution_result = ""

    def _save_attachment(self, attachment: dict) -> Optional[str]:
        try:
            uploads_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
            os.makedirs(uploads_dir, exist_ok=True)
            # Sanitize the filename and keep names unique across uploads.
            raw_name = os.path.basename(attachment.get("name", "upload.bin"))
            safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name) or "upload.bin"
            stem, ext = os.path.splitext(safe_name)
            path = os.path.join(uploads_dir, safe_name)
            counter = 1
            while os.path.exists(path):
                path = os.path.join(uploads_dir, f"{stem}_{counter}{ext}")
                counter += 1
            with open(path, "wb") as f:
                f.write(base64.b64decode(attachment["data_base64"]))
            logger.info(f"Attachment saved: {path}")
            return path
        except Exception as e:
            logger.error(f"Failed to save attachment: {e}")
            return None

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

    async def dispatch_architect_tool(self, tool_name: str, args: dict) -> str:
        """Executes one Architect tool call and returns its result string."""
        if tool_name == "get_system_time":
            return tools.get_system_time()
        elif tool_name == "web_search":
            return await tools.web_search(args.get("query", ""))
        elif tool_name == "read_website":
            return await tools.read_website(args.get("url", ""))
        elif tool_name == "search_memory":
            return await tools.search_memory(args.get("query", ""), memory_collection)
        elif tool_name == "save_memory":
            return await tools.save_memory(args.get("fact", ""), memory_collection)
        elif tool_name == "send_to_tormach":
            return await tools.send_to_tormach(args.get("local_file_path", ""))
        elif tool_name == "check_device_status":
            return str(await tools.check_device_status(args.get("ip_address", "")))
        elif tool_name == "run_shop_skill":
            return await tools.run_shop_skill(args.get("skill_name", ""), args.get("arguments", ""))
        elif tool_name == "manage_goals":
            return tools.manage_goals(
                action=args.get("action", ""),
                task=args.get("task"),
                task_id=args.get("task_id")
            )
        elif tool_name == "vscode_get_active_file":
            await self.send_log(f"\n*(Architect is reaching out to VS Code...)*\n")
            response = await self.manager.execute_tool_on_client(
                "vscode", 
                {"action": "get_active_file"}, 
                timeout=5.0
            )
            return str(response.get("content", response))
        elif tool_name == "tormach_ssh":
            command = args.get("command", "")
            explanation = args.get("explanation", "Executing SSH command on Tormach PathPilot.")
            
            await self.send_log(f"\n⚠️ [Architect] Requested Tormach SSH: {command}\nWaiting for human authorization...\n")
            if self.ws:
                auth_data = await self.manager.request_authorization(
                    self.ws, {"type": "terminal_auth", "command": command, "explanation": explanation}
                )
                if auth_data.get("status") == "APPROVE":
                    await self.send_log(f"✅ Authorization GRANTED. Connecting to PathPilot...\n")
                    return await tools.execute_tormach_ssh(command)
                else:
                    await self.send_log(f"❌ Authorization DENIED by human.\n")
                    return "USER DENIED SSH EXECUTION. Find a workaround."
            else:
                return "HEADLESS ERROR: Cannot request SSH authorization without UI client attached."
        elif tool_name == "github_manager":
            await self.send_log(f"\n*(Architect is interacting with GitHub: {args.get('action')}...)*\n")
            return await tools.execute_github_manager(
                repo=args.get("repo", ""),
                action=args.get("action", ""),
                title=args.get("title"),
                body=args.get("body")
            )
        elif tool_name == "read_directory_structure":
            target_path = args.get("path", "")
            depth = int(args.get("max_depth", 2))
            await self.send_log(f"\n*(Architect is mapping directory: {target_path}...)*\n")
            return await tools.read_directory_structure(target_path, max_depth=depth)
        elif tool_name == "ingest_codebase_to_rag":
            target_path = args.get("path", "")
            await self.send_log(f"\n*(Architect is chunking and embedding {target_path} into ChromaDB...)*\n")
            return await tools.ingest_codebase_to_rag(target_path, code_collection)
        elif tool_name == "generate_image":
            await self.send_log(f"\n🎨 *(Skippy is painting: \"{args.get('prompt', '')[:80]}...\")*\n")
            return await tools.generate_image(
                prompt=args.get("prompt", ""),
                negative_prompt=args.get("negative_prompt", ""),
                width=int(args.get("width", 1024)),
                height=int(args.get("height", 1024)),
            )
        elif tool_name == "edit_image":
            await self.send_log(f"\n🎨 *(Skippy is editing {args.get('image_path', '')}...)*\n")
            return await tools.edit_image(
                image_path=args.get("image_path", ""),
                prompt=args.get("prompt", ""),
                strength=float(args.get("strength", 0.55)),
                negative_prompt=args.get("negative_prompt", ""),
            )
        elif tool_name == "search_codebase":
            # --- COMPRESSOR INTERCEPT FOR SEARCH ---
            search_query = args.get("query", "")
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
            return f"COMPRESSED MEMORY RESULT:\n{compressed_result}"
        return f"ERROR: Unknown tool '{tool_name}'."

    async def phase_1_research(self, enriched_input: str):
        arch_messages = [{"role": "system", "content": PROMPTS.get(self.mode, PROMPTS["Shop"])["architect"]}]
        for msg in self.chat_history[-10:]:
            role = "user" if msg.startswith("You:") else "assistant"
            content = msg.replace("You: ", "").replace("Skippy: ", "")
            arch_messages.append({"role": role, "content": content})
            
        arch_messages.append({"role": "user", "content": enriched_input})
        
        architect_tools = get_architect_tools(self.mode)
        last_tool_signature = None
        repeat_count = 0

        for _ in range(8):
            await self.send_log("\n[Architect] Analyzing and researching...\n")
            message = await query_model_message(
                arch_messages, 
                temp=0.2, 
                url=LOCAL_70B_URL, 
                model_name=MODEL_70B_NAME,
                tool_schemas=architect_tools,
                repetition_penalty=1.05
            )
            
            if not message["tool_calls"]:
                # Plain text with no tool call: the sanctioned handoff is the
                # wake_engineer tool, so bare text is normally a conversational
                # answer. Only treat it as a blueprint if it reads like work
                # instructions (mentions the Engineer/blueprint or promises
                # implementation), so casual questions don't trigger Phase 2.
                content = message["content"]
                blueprint_like = re.search(
                    r"blueprint|the engineer|i(?:'ll| will)(?: now)? (?:create|write|build|make|generate|implement)|let me (?:create|write|build)",
                    content, re.IGNORECASE
                )
                if blueprint_like:
                    self.blueprint = content
                else:
                    await self.send_log("\n[Architect] Direct conversation detected (plain-text answer).\n")
                    await self.send_chat(content)
                    if self.ws: await speak_text(content, self.ws, self.use_tts)
                    self.is_direct_reply = True
                break

            # Dedupe identical calls within the batch and cap the batch size:
            # degenerate sampling can emit the same web_search dozens of times
            # in one turn, which would hammer the tools for minutes.
            unique_calls = []
            seen_signatures = set()
            for tc in message["tool_calls"]:
                sig = json.dumps({"name": tc["name"], "arguments": tc["arguments"]}, sort_keys=True)
                if sig not in seen_signatures:
                    seen_signatures.add(sig)
                    unique_calls.append(tc)
            dropped = len(message["tool_calls"]) - len(unique_calls)
            if len(unique_calls) > 5:
                dropped += len(unique_calls) - 5
                unique_calls = unique_calls[:5]
            if dropped:
                await self.send_log(f"⚠️ *Dropped {dropped} duplicate/excess tool calls from the Architect's batch.*\n")
            message["tool_calls"] = unique_calls

            # Loop breaker: an identical batch of tool calls means the model is
            # stuck (e.g. re-running a skill that doesn't exist). Nudge once,
            # then force a blueprint from the raw request.
            tool_signature = json.dumps(
                [{"name": tc["name"], "arguments": tc["arguments"]} for tc in message["tool_calls"]],
                sort_keys=True
            )
            if tool_signature == last_tool_signature:
                repeat_count += 1
            else:
                last_tool_signature = tool_signature
                repeat_count = 0
            if repeat_count == 1:
                await self.send_log("⚠️ *Architect repeated the same tool call. Nudging it to change strategy...*\n")
                arch_messages.append(assistant_turn(message))
                for tc in message["tool_calls"]:
                    arch_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": "SYSTEM: You just repeated the EXACT same tool call and it did not work the first time. Do NOT call it again. Either use a DIFFERENT tool, use direct_reply, or call wake_engineer with your blueprint now."})
                continue
            elif repeat_count >= 2:
                await self.send_log("⚠️ *Architect is stuck in a tool loop. Forcing handoff to the Engineer with the raw request.*\n")
                self.blueprint = f"ARCHITECT STALLED. Implement the user's request directly:\n{enriched_input}"
                break

            arch_messages.append(assistant_turn(message))
            terminal = False

            for tc in message["tool_calls"]:
                tool_name, args = tc["name"], tc["arguments"]
                await self.send_log(f"*(Architect is using {tool_name}...)*\n")

                if tool_name == "wake_engineer":
                    blueprint = args.get("blueprint", "")
                    if blueprint.strip():
                        self.blueprint = blueprint
                        await self.send_log("\n[Architect] Handing blueprint to the Engineer.\n")
                        terminal = True
                        break
                    arch_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": "SYSTEM: Your wake_engineer call had an empty 'blueprint' field. Re-send it with the full plain-English instructions for the Engineer."})
                    continue

                if tool_name == "direct_reply":
                    reply_msg = args.get("message", "I have your answer.")

                    # Guard against "promissory" direct replies ("I'll now create
                    # a blueprint...") which would end the pipeline before the
                    # Engineer ever runs. Nudge once, then force the handoff.
                    promissory = re.search(
                        r"blueprint|the engineer|i(?:'ll| will)(?: now)? (?:create|write|build|make|generate)|let me (?:create|write|build)",
                        reply_msg, re.IGNORECASE
                    )
                    if promissory:
                        if not getattr(self, "_direct_reply_nudged", False):
                            self._direct_reply_nudged = True
                            await self.send_log("⚠️ *Architect tried to end the pipeline while promising future work. Nudging it to hand off to the Engineer...*\n")
                            arch_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": "SYSTEM: You used direct_reply to PROMISE work instead of doing it. direct_reply ends the pipeline immediately — no Engineer will run. Call wake_engineer NOW with the full blueprint."})
                            continue
                        await self.send_log("⚠️ *Architect repeated a promissory direct_reply. Treating its message as the blueprint and waking the Engineer.*\n")
                        self.blueprint = reply_msg
                        terminal = True
                        break

                    await self.send_log("\n[Architect] Direct conversation detected.\n")
                    await self.send_chat(reply_msg)
                    if self.ws: await speak_text(reply_msg, self.ws, self.use_tts)
                    self.is_direct_reply = True
                    terminal = True
                    break

                try:
                    tool_result = await self.dispatch_architect_tool(tool_name, args)
                except Exception as e:
                    tool_result = f"TOOL ERROR ({tool_name}): {str(e)}"
                arch_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": str(tool_result)})

            if terminal:
                break
        else:
            self.blueprint = "RESEARCH LIMIT REACHED. Proceeding with gathered context. Original request:\n" + enriched_input

        if not self.is_direct_reply:
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
        
        engineer_tools = get_engineer_tools(self.mode)
        qa_feedback = "" 
        code_to_test = ""
        
        for attempt in range(4):
            engine_name = "405B Model" if is_complex else "70B Model"
            await self.send_log(f"\n[Engineer] Coding session started on {engine_name}. Attempt {attempt + 1}/4...\n")
            
            current_eng_messages = base_eng_messages.copy()
            if qa_feedback: current_eng_messages.append({"role": "user", "content": qa_feedback})
            
            eng_message = await query_model_message(current_eng_messages, temp=0.1, url=eng_url, model_name=eng_model, tool_schemas=engineer_tools)
            engineer_response = eng_message["content"]
            await self.send_log(f"----- ENGINEER RESPONSE -----\n{engineer_response}\n-----------------------------\n")
            
            code_to_test = ""
            eng_tool_call = eng_message["tool_calls"][0] if eng_message["tool_calls"] else None
            
            if eng_tool_call:
                tool_name, args = eng_tool_call["name"], eng_tool_call["arguments"]

                if tool_name == "request_terminal_execution":
                    command = args.get("command", "")
                    explanation = args.get("explanation", "")
                    
                    await self.send_log(f"\n⚠️ [Engineer] Requested God Mode: {command}\nWaiting for human authorization...\n")
                    if self.ws:
                        auth_data = await self.manager.request_authorization(
                            self.ws, {"type": "terminal_auth", "command": command, "explanation": explanation}
                        )
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
                    
                elif tool_name == "patch_file":
                    patches = args.get("patches", [])
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

                else:
                    qa_feedback = f"You called an unknown tool '{tool_name}'. You have no file-writing tools — output the complete code in a markdown block instead."
                    continue

            if not code_to_test:
                code_match = re.findall(r'`{3}(?:python|javascript|cpp)?\n(.*?)`{3}', engineer_response, re.DOTALL)
                if code_match: code_to_test = code_match[-1].strip()
                elif "```" in engineer_response:
                    fallback_match = re.findall(r'`{3}\n(.*?)`{3}', engineer_response, re.DOTALL)
                    if fallback_match: code_to_test = fallback_match[-1].strip()
                else:
                    code_to_test = engineer_response

            if not code_to_test.strip():
                qa_feedback = "You failed to output any valid code or patch call."
                continue

            await self.send_log("\n[Execution Engine] Spinning up secure sandbox to test Engineer's draft...\n")
            self.execution_result = await execute_python_code(code_to_test)
            
            await self.send_log(f"\n[QA Lead] Executing automated code review & analyzing runtime logs...\n")
            
            qa_prompt_injection = f"Code to review:\n{code_to_test}\n\nLive Terminal Execution Output:\n{self.execution_result}\n\nArchitect's Blueprint (Script Requirements):\n{self.blueprint}"
            
            qa_messages = [
                {"role": "system", "content": PROMPTS.get(self.mode, PROMPTS["Shop"]).get("qa", "")},
                {"role": "user", "content": qa_prompt_injection}
            ]
            qa_review = ""
            qa_data = {}
            # QA may run the script with real arguments (limited budget) before it
            # must submit a verdict; once the budget is spent the verdict tool is
            # the only one offered, so it cannot test forever.
            test_rounds_left = 2
            qa_tests_failed = False
            for qa_try in range(6):
                testing_allowed = test_rounds_left > 0
                qa_tools = [QA_TEST_TOOL, QA_VERDICT_TOOL] if testing_allowed else [QA_VERDICT_TOOL]
                qa_message = await query_model_message(qa_messages, temp=0.1, url=LOCAL_70B_URL, model_name=MODEL_70B_NAME, tool_schemas=qa_tools, repetition_penalty=1.05)
                qa_review = qa_message["content"] or qa_review
                
                verdict_call = None
                test_calls = []
                for tc in qa_message["tool_calls"]:
                    if tc["name"] == "submit_verdict":
                        verdict_call = tc
                    elif tc["name"] == "run_script_test":
                        test_calls.append(tc)

                if test_calls and testing_allowed:
                    test_rounds_left -= 1
                    qa_messages.append(assistant_turn(qa_message))
                    for tc in test_calls:
                        raw_args = tc["arguments"].get("arguments", "")
                        try:
                            cli_args = shlex.split(raw_args)
                        except ValueError:
                            cli_args = raw_args.split()
                        await self.send_log(f"*(QA is testing the script with args: {raw_args})*\n")
                        test_output = await execute_python_code(code_to_test, cli_args=cli_args)
                        if test_output.startswith(("SCRIPT FAILED", "TRACEBACK")):
                            qa_tests_failed = True
                        qa_messages.append({"role": "tool", "tool_call_id": tc["id"], "content": test_output})
                    if test_rounds_left <= 0:
                        qa_messages.append({"role": "user", "content": "SYSTEM: Your test budget is exhausted. Based on the outputs above, call submit_verdict NOW. If the outputs were correct, APPROVE; otherwise FAIL with feedback."})
                    elif verdict_call:
                        # Verdict stacked with tests in one turn: ignore it and
                        # make QA re-judge with the fresh test results in hand.
                        qa_messages.append({"role": "user", "content": "SYSTEM: Review the test output above, then call submit_verdict."})
                    continue

                if verdict_call:
                    qa_data = verdict_call["arguments"]
                    # Deterministic backstop: QA cannot approve code whose own
                    # test runs failed, no matter how it rationalizes them.
                    if qa_data.get("status") == "APPROVE" and qa_tests_failed:
                        await self.send_log("⚠️ *QA tried to APPROVE code that failed its own test runs. Overriding to FAIL.*\n")
                        qa_data = {"status": "FAIL", "feedback": "Backend override: your own run_script_test calls returned failures (non-zero exit / traceback). The code does not work on the blueprint's example inputs. Fix the parsing/logic so the tested inputs succeed."}
                    break

                # QA responded in prose without a verdict — demand it.
                qa_messages.append({"role": "assistant", "content": qa_message["content"]})
                qa_messages.append({"role": "user", "content": "SYSTEM: You did not call submit_verdict. Call it NOW with your verdict (APPROVE/FAIL/DEPLOY) based on the review and test outputs above. Do not write anything else."})

            await self.send_log(f"----- QA VERDICT REPORT -----\n{qa_review}\nVerdict: {json.dumps(qa_data) if qa_data else '(no verdict tool call)'}\n-----------------------------\n")

            status = qa_data.get("status", "")
            # No verdict call or explicit FAIL both count as a rejection.
            is_fail = (not qa_data) or status == "FAIL"
            
            if not is_fail:
                try:
                    if status == "APPROVE":
                        save_path = qa_data.get("save_path", "~/Desktop/skippy_output.py")
                        # Overwrite the stale no-args probe result so the
                        # summarizer reports the real outcome.
                        self.execution_result = f"QA tested the script with real inputs and APPROVED it. Saved to {save_path}."
                        
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
                            auth_data = await self.manager.request_authorization(self.ws, {
                                "type": "deployment_auth",
                                "target_file": target_file,
                                "summary": summary,
                                "content": code_to_test
                            })
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
                critique = qa_data.get("feedback") or qa_review or "QA did not submit a verdict — treat the draft as rejected and improve it."
                # Cap the critique so a degenerate QA response can't flood the
                # Engineer's context on the next attempt.
                if len(critique) > 2000:
                    critique = critique[:2000] + "\n[...QA feedback truncated...]"
                qa_feedback = f"QA FAILED ON PREVIOUS ATTEMPT. Terminal Output was:\n{self.execution_result}\nQA Feedback:\n{critique}\nEngineer, fix these issues."
                await self.send_log(f"\n🔄 *QA rejected code iteration {attempt + 1}. Routing critique back to development engine...*\n")

    async def phase_3_summarize(self):
        await self.send_log("\n[Executive Summarizer] Formatting final response for the user...\n")
        status_text = "Success" if self.success else "Failure"
        summary_messages = [
            {"role": "system", "content": PROMPTS.get(self.mode, PROMPTS["Shop"]).get("summarizer", "")},
            {"role": "user", "content": f"Task: {self.user_input}\nBlueprint: {self.blueprint}\nOutcome: {status_text}\nQA Feedback/Results: {self.execution_result}\nWrite the conversational summary."}
        ]
        
        # Repetition penalty + tighter token cap prevent the degenerate
        # repeated-sentence loops the summarizer occasionally falls into.
        summary_message = await query_model_message(summary_messages, temp=0.4, url=LOCAL_70B_URL, model_name=MODEL_70B_NAME, max_tokens=1024, repetition_penalty=1.1)
        final_summary = summary_message["content"]
        await self.send_chat(final_summary)
        if self.ws: await speak_text(final_summary, self.ws, self.use_tts)

# --- THE AUTONOMOUS HEARTBEAT LOOP ---
async def skippy_heartbeat():
    """Background task that wakes Skippy up every 5 minutes."""
    while True:
        await asyncio.sleep(300) 
        
        current_time = tools.get_system_time()
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
                data = {"mode": "Shop", "text": raw_input, "history": [], "use_tts": False}
                
            if "task_id" in data:
                hub.resolve_response(data["task_id"], data)
                continue

            # Auth replies (APPROVE/DENY) go to the waiting pipeline, not a new one.
            if "status" in data and "text" not in data:
                if hub.resolve_auth(websocket, data):
                    continue

            pipeline = SkippyPipeline(websocket, data, hub)
            asyncio.create_task(pipeline.run())

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