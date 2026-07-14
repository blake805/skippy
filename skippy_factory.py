import os
import json
import re
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

# --- SETUP LOGGING ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("skippy_factory")

# --- TRI-SERVER ROUTING ---
LOCAL_70B_URL = "http://127.0.0.1:8080/v1/chat/completions"
LOCAL_405B_URL = "http://127.0.0.1:8081/v1/chat/completions"
LOCAL_COMPRESSOR_URL = "http://127.0.0.1:8082/v1/chat/completions" # <-- NEW COMPRESSOR NODE

MODEL_70B_NAME = "mlx-community/Llama-3.3-70B-Instruct-4bit"
MODEL_405B_NAME = "mlx-community/Meta-Llama-3.1-405B-4bit"
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

# --- JSON EXTRACTION ---
def extract_json_block(text: str) -> Optional[str]:
    """Finds the first balanced, valid top-level {...} block in the text.

    Replaces the old regex approach: non-greedy `\\{.*?\\}` truncated at the
    first '}' (breaking nested JSON like patch_file), and greedy `\\{.*\\}`
    swallowed everything between the first '{' and last '}'. This scanner
    tracks brace depth while respecting string literals and escapes, and
    validates each candidate with json.loads before returning it.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        candidate = text[start:i + 1]
                        try:
                            json.loads(candidate)
                            return candidate
                        except json.JSONDecodeError:
                            break
        start = text.find("{", start + 1)
    return None

# --- ASYNC HELPERS ---
async def query_model_async(messages: list, temp: float = 0.2, url: str = LOCAL_70B_URL, model_name: str = MODEL_70B_NAME, stop_sequences: list = None) -> str:
    payload = {
        "model": model_name,
        "messages": messages,
        "temperature": temp,
        "max_tokens": 4096 
    }
    if stop_sequences:
        payload["stop"] = stop_sequences
    async with httpx.AsyncClient() as client:
        for attempt in range(3):
            try:
                response = await client.post(url, json=payload, timeout=600.0)
                if response.status_code == 200:
                    return response.json()["choices"][0]["message"]["content"].strip()
            except Exception:
                pass
            await asyncio.sleep(2.0 * (2 ** attempt))
        return f"System Error: Failed to connect to MLX Server at {url}."

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
            
            json_block = extract_json_block(arch_response)
            if json_block:
                try:
                    tool_data = json.loads(json_block)
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
            json_block = extract_json_block(engineer_response)
            
            if json_block:
                try:
                    tool_data = json.loads(json_block)
                    
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
    uvicorn.run("skippy_factory:app", host="0.0.0.0", port=8000, reload=False)