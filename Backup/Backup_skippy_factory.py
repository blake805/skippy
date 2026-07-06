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
import soundfile as sf
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

app = FastAPI(title="Skippy Assembly Line API (Ultimate Routing Edition)")

# --- DUAL SERVER ROUTING ---
LOCAL_70B_URL = "http://127.0.0.1:8080/v1/chat/completions"
LOCAL_405B_URL = "http://127.0.0.1:8081/v1/chat/completions"
MODEL_70B_NAME = "mlx-community/Llama-3.3-70B-Instruct-4bit"
MODEL_405B_NAME = "mlx-community/Meta-Llama-3.1-405B-4bit"

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
    if "function" in code or "var " in code or code.strip().startswith("//"):
        return "SKIPPED EXECUTION: Code appears to be JavaScript/CPS/C++. Proceeding with static analysis only."
    try:
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as temp_file:
            temp_file.write(code)
            temp_path = temp_file.name

        process = await asyncio.create_subprocess_exec("python3", temp_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            return "EXECUTION TIMEOUT: Code took longer than 10 seconds to run. (Expected for Web Servers)"
        finally:
            os.remove(temp_path)

        output = stdout.decode().strip()
        errors = stderr.decode().strip()
        if errors: return f"TRACEBACK / ERRORS:\n{errors}\n\nOUTPUT:\n{output}"
        elif output: return f"SUCCESSFUL OUTPUT:\n{output}"
        else: return "SUCCESS: Code ran without errors."
    except Exception as e:
        return f"EXECUTION SYSTEM ERROR: {str(e)}"

async def run_bash_command_stream(command: str, websocket: WebSocket) -> str:
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
import uuid
from typing import Dict

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
        
        # Generate a unique task ID to track the response
        task_id = str(uuid.uuid4())
        payload["task_id"] = task_id
        
        # Create a future to pause the pipeline until the client replies
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending_responses[task_id] = future
        
        # Dispatch to the target client (e.g., VS Code)
        await self.active_connections[target_client].send_json(payload)
        
        # Wait for the client to reply
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

# Initialize the global hub
hub = ConnectionManager()

# --- TTS HELPER ---
async def speak_text(text: str, websocket: WebSocket, use_tts: bool):
    if not use_tts: return
    
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
    def __init__(self, websocket: WebSocket, payload: dict, manager: ConnectionManager):
        self.ws = websocket
        self.manager = manager # <--- ADDED MANAGER
        self.mode = payload.get("mode", "Shop")
        self.user_input = payload.get("text", "")
        self.chat_history = payload.get("history", [])
        self.use_tts = payload.get("use_tts", False)
        
        # Pipeline State
        self.blueprint = ""
        self.is_direct_reply = False
        self.success = False
        self.execution_result = ""

    async def send_log(self, msg: str):
        try:
            await self.ws.send_json({"type": "log", "content": msg})
        except Exception:
            pass # Fails silently if the MacBook disconnected

    async def send_chat(self, msg: str):
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

    async def run(self):
        try:
            await self.send_log(f"⚙️ *Skippy processing initiated... (Mode: {self.mode})*")
            enriched_input = await self.phase_0_inject_files()
            await self.phase_1_research(enriched_input)
            
            if self.is_direct_reply:
                await self.ws.send_json({"type": "done"})
                return
                
            await self.phase_2_engineer_and_qa(enriched_input)
            await self.phase_3_summarize()
            
            await self.ws.send_json({"type": "done"})
        except Exception as e:
            logger.error(f"Pipeline Error: {e}")
            try:
                await self.send_log(f"❌ *Fatal Pipeline Crash: {str(e)}*\n")
                await self.ws.send_json({"type": "done"})
            except Exception:
                pass # Socket is already dead, just let the pipeline die peacefully

    async def phase_1_research(self, enriched_input: str):
        arch_messages = [{"role": "system", "content": PROMPTS.get(self.mode, PROMPTS["Shop"])["architect"]}]
        for msg in self.chat_history[-10:]:
            role = "user" if msg.startswith("You:") else "assistant"
            content = msg.replace("You: ", "").replace("Skippy: ", "")
            arch_messages.append({"role": role, "content": content})
            
        arch_messages.append({"role": "user", "content": enriched_input})
        
        for _ in range(8):
            await self.send_log("\n[Architect] Analyzing and researching...\n")
            # PASS THE STOP SEQUENCES SO SKIPPY DOESN'T HALLUCINATE OBSERVATIONS
            arch_response = await query_model_async(
                arch_messages, 
                temp=0.2, 
                url=LOCAL_70B_URL, 
                model_name=MODEL_70B_NAME,
                stop_sequences=["TOOL RESULT:", "Observation:"]
            )
            
            # NON-GREEDY REGEX MATCH (.*? instead of .*)
            json_match = re.search(r'\{.*?\}', arch_response, re.DOTALL)
            if json_match:
                try:
                    tool_data = json.loads(json_match.group(0))
                    tool_name = tool_data.get("name")
                    await self.send_log(f"*(Architect is using {tool_name}...)*\n")
                    
                    if tool_name == "direct_reply":
                        reply_msg = tool_data.get("message", "I have your answer.")
                        await self.send_log("\n[Architect] Direct conversation detected. Bypassing assembly line.\n")
                        await self.send_chat(reply_msg)
                        await speak_text(reply_msg, self.ws, self.use_tts)
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
                    
                    # --- NEW: VS CODE HUB INTERCEPT ---
                    elif tool_name == "vscode_get_active_file":
                        await self.send_log(f"\n*(Architect is reaching out to VS Code...)*\n")
                        # Pause the pipeline, ask VS Code, and wait 5 seconds for a reply
                        response = await self.manager.execute_tool_on_client(
                            "vscode", 
                            {"action": "get_active_file"}, 
                            timeout=5.0
                        )
                        tool_result = str(response.get("content", response))
                    
                    # --- NEW: TORMACH SSH WITH HUMAN-IN-THE-LOOP ---
                    elif tool_name == "tormach_ssh":
                        command = tool_data.get("command", "")
                        explanation = tool_data.get("explanation", "Executing SSH command on Tormach PathPilot.")
                        
                        await self.send_log(f"\n⚠️ [Architect] Requested Tormach SSH: {command}\nWaiting for human authorization...\n")
                        await self.ws.send_json({"type": "terminal_auth", "command": command, "explanation": explanation})
                        
                        auth_reply = await self.ws.receive_text()
                        auth_data = json.loads(auth_reply)
                        
                        if auth_data.get("status") == "APPROVE":
                            await self.send_log(f"✅ Authorization GRANTED. Connecting to PathPilot...\n")
                            tool_result = await tools.execute_tormach_ssh(command)
                        else:
                            await self.send_log(f"❌ Authorization DENIED by human.\n")
                            tool_result = "USER DENIED SSH EXECUTION. You cannot run this command. Find a workaround or ask the user for help."
                        
                        # --- NEW: GITHUB MANAGER ---
                    elif tool_name == "github_manager":
                        await self.send_log(f"\n*(Architect is interacting with GitHub: {tool_data.get('action')}...)*\n")
                        tool_result = await tools.execute_github_manager(
                            repo=tool_data.get("repo", ""),
                            action=tool_data.get("action", ""),
                            title=tool_data.get("title"),
                            body=tool_data.get("body")
                        )

                    # --- NEW: DIRECTORY MAPPING ---
                    elif tool_name == "read_directory_structure":
                        target_path = tool_data.get("path", "")
                        depth = int(tool_data.get("max_depth", 2))
                        await self.send_log(f"\n*(Architect is mapping directory: {target_path}...)*\n")
                        tool_result = await tools.read_directory_structure(target_path, max_depth=depth)

                    # --- NEW: RAG CODEBASE INGESTION ---
                    elif tool_name == "ingest_codebase_to_rag":
                        target_path = tool_data.get("path", "")
                        await self.send_log(f"\n*(Architect is chunking and embedding {target_path} into ChromaDB...)*\n")
                        tool_result = await tools.ingest_codebase_to_rag(target_path, code_collection)

                        # --- NEW: CODEBASE SEARCH ---
                    elif tool_name == "search_codebase":
                        search_query = tool_data.get("query", "")
                        await self.send_log(f"\n*(Architect is searching code memory for: {search_query}...)*\n")
                        tool_result = await tools.search_codebase(search_query, code_collection)
                        
                    arch_messages.append({"role": "assistant", "content": arch_response})
                    arch_messages.append({"role": "user", "content": f"TOOL RESULT:\n{tool_result}\nIf you need more info, use another tool. If you can answer directly without code, use direct_reply. Otherwise, provide the final blueprint."})
                    continue
                except json.JSONDecodeError:
                    pass
            
            self.blueprint = arch_response
            break
        else:
            self.blueprint = "RESEARCH LIMIT REACHED. Proceeding with gathered context:\n" + arch_response

        await self.send_log(f"----- ARCHITECT BLUEPRINT GENERATED -----\n{self.blueprint}\n----------------------------------------\n")
        

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
                            qa_feedback = "USER DENIED COMMAND EXECUTION. Find a workaround or proceed without it."
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

            await self.send_log("[Execution Engine] Spinning up secure sandbox to test Engineer's draft...\n")
            self.execution_result = await execute_python_code(code_to_test)
            
            await self.send_log(f"[QA Lead] Executing automated code review & analyzing runtime logs...\n")
            qa_prompt_injection = f"Code to review:\n{code_to_test}\n\nLive Terminal Execution Output:\n{self.execution_result}\n\nOriginal Request:\n{self.user_input}"
            
            qa_review = await query_model_async([
                {"role": "system", "content": PROMPTS.get(self.mode, PROMPTS["Shop"]).get("qa", "")},
                {"role": "user", "content": qa_prompt_injection}
            ], temp=0.1, url=LOCAL_70B_URL, model_name=MODEL_70B_NAME)
            
            await self.send_log(f"----- QA VERDICT REPORT -----\n{qa_review}\n-----------------------------\n")

            if ("APPROVE" in qa_review or "DEPLOY" in qa_review) and "{" in qa_review:
                try:
                    json_match = re.search(r'\{\s*"status"\s*:\s*"(?:APPROVE|DEPLOY)".*?\}', qa_review, re.DOTALL)
                    if not json_match: raise ValueError("Could not isolate the JSON block.")
                    
                    qa_data = json.loads(json_match.group(0))
                    status = qa_data.get("status")
                    
                    if status == "APPROVE":
                        save_path = qa_data.get("save_path", "~/Desktop/skippy_output.py")
                        
                        # 🚨 THE SURGICAL FIX: Intercept the skills/ directory routing
                        if save_path.startswith("skills/") or "skills" in save_path:
                            safe_name = os.path.basename(save_path)
                            local_filepath = os.path.join(SKILLS_DIR, safe_name)
                            with open(local_filepath, "w", encoding="utf-8") as f:
                                f.write(code_to_test)
                            await self.send_log(f"\n[Success] QA Sign-off acquired. Skill permanently saved to Mac Studio at {local_filepath}.\n")
                        else:
                            await self.ws.send_json({"type": "write_file", "path": save_path, "content": code_to_test})
                            await self.send_log(f"\n[Success] QA Sign-off acquired. Payload transmitted to MacBook for native disk write.\n")
                            
                        self.success = True
                        break
                        
                    elif status == "DEPLOY":
                        target_file = qa_data.get("target_file", "skippy_factory.py")
                        summary = qa_data.get("summary", "Code upgrade.")
                        
                        await self.send_log(f"\n⚠️ [QA Lead] Requested DEPLOYMENT to {target_file}\nWaiting for human authorization...\n")
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
                            break
                        else:
                            await self.send_log(f"❌ Deployment DENIED by human.\n")
                            qa_feedback = "USER DENIED DEPLOYMENT AUTHORIZATION. Revise the code or abort."
                            continue 
                except Exception as e:
                    await self.send_log(f"\n[Write Error] Exception raised during payload routing: {str(e)}\n")
                    break
            else:
                qa_feedback = f"QA FAILED ON PREVIOUS ATTEMPT. Terminal Output was:\n{self.execution_result}\nQA Feedback:\n{qa_review}\nEngineer, fix these issues."
                await self.send_log(f"🔄 *QA rejected code iteration {attempt + 1}. Routing critique back to development engine...*")

    async def phase_3_summarize(self):
        await self.send_log("\n[Executive Summarizer] Formatting final response for the user...\n")
        status_text = "Success" if self.success else "Failure"
        summary_messages = [
            {"role": "system", "content": PROMPTS.get(self.mode, PROMPTS["Shop"]).get("summarizer", "")},
            {"role": "user", "content": f"Task: {self.user_input}\nBlueprint: {self.blueprint}\nOutcome: {status_text}\nQA Feedback/Results: {self.execution_result}\nWrite the conversational summary."}
        ]
        
        final_summary = await query_model_async(summary_messages, temp=0.4, url=LOCAL_70B_URL, model_name=MODEL_70B_NAME)
        await self.send_chat(final_summary)
        await speak_text(final_summary, self.ws, self.use_tts)

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
                
            # If the payload has a task_id, it's a silent response from a client (like VS Code)
            if "task_id" in data:
                hub.resolve_response(data["task_id"], data)
                continue
                
            # Otherwise, it's a standard user prompt. 
            # We use create_task so the loop doesn't block incoming tool responses!
            pipeline = SkippyPipeline(websocket, data, hub)
            asyncio.create_task(pipeline.run())

    except WebSocketDisconnect:
        hub.disconnect(client_id)

@app.get("/ping")
async def ping():
    return {"status": "Skippy is awake and the event loop is spinning!"}

@app.post("/transcribe")
async def transcribe_audio(file: UploadFile = File(...)):
    input_path = f"incoming_{file.filename}"
    with open(input_path, "wb") as buffer: buffer.write(await file.read())
    result = whisper_model.transcribe(input_path, fp16=False)
    os.remove(input_path) 
    return {"text": result["text"].strip()}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("skippy_factory:app", host="0.0.0.0", port=8000, reload=True)