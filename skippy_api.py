import os
import json
import httpx
import asyncio
import base64
import io
import subprocess
import urllib.request
import urllib.parse
import soundfile as sf
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import whisper
import chromadb
from kokoro_onnx import Kokoro
from ddgs import DDGS

app = FastAPI(title="Skippy The Autonomous Agent")

# --- 1. INITIALIZE ENGINES ---
print("🚀 Initializing Skippy API Server (Agentic ReAct Edition)...")
whisper_model = whisper.load_model("base")
kokoro = Kokoro("kokoro-v1.0.int8.onnx", "voices-v1.0.bin")

NAS_MEMORY_PATH = "/Volumes/skippy_memory/chroma_db"
os.makedirs(NAS_MEMORY_PATH, exist_ok=True)
chroma_client = chromadb.PersistentClient(path=NAS_MEMORY_PATH)
memory_collection = chroma_client.get_or_create_collection(name="skippy_longterm")
LOCAL_70B_URL = "http://127.0.0.1:8080/v1/chat/completions"

SKIPPY_SYSTEM_PROMPT = """You are Skippy, a hyper-intelligent AI. You have a slightly sarcastic personality, but prioritize being helpful and precise. Dial back the insults.

You operate in an Autonomous THOUGHT LOOP. You can use multiple tools in a row to research and solve problems.
Tools:
1. Web Search: {"name": "web_search", "query": "<search terms>"}
2. Read Website: {"name": "read_website", "url": "<url>"}
3. Terminal: {"name": "run_terminal", "command": "<bash command>"}
4. Read File: {"name": "read_file", "path": "<absolute file path>"}
5. Write File: {"name": "write_file", "path": "<absolute file path>", "content": "<exact file content>"}

CRITICAL RULES:
- If you need to use a tool, you MUST NOT think out loud. Your ENTIRE output must be ONLY the raw JSON block. The very first character you type must be '{'.
- Once you have the final answer, speak it out loud normally."""

# --- 2. TOOL FUNCTIONS ---
def run_terminal(command):
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=10)
        return result.stdout if result.stdout else result.stderr
    except Exception as e:
        return str(e)

def web_search(query):
    try:
        results = DDGS().text(query, max_results=4)
        return json.dumps(results)
    except Exception as e:
        return f"Search error: {str(e)}"

def read_website(url):
    try:
        jina_url = f"https://r.jina.ai/{url}"
        req = urllib.request.Request(jina_url, headers={'User-Agent': 'Mozilla/5.0'})
        response = urllib.request.urlopen(req, timeout=15)
        text = response.read().decode('utf-8')
        return text[:5000] 
    except Exception as e:
        return f"Failed to read website: {str(e)}"

def read_file(path):
    try:
        expanded_path = os.path.expanduser(path)
        with open(expanded_path, 'r', encoding='utf-8') as f:
            content = f.read()
            # Cap at 10,000 characters to prevent memory blowouts on the 70B model
            if len(content) > 10000:
                return content[:10000] + "\n\n...(File truncated due to size limits)..."
            return content
    except Exception as e:
        return f"Failed to read file: {str(e)}"

def write_file(path, content):
    try:
        expanded_path = os.path.expanduser(path)
        os.makedirs(os.path.dirname(expanded_path), exist_ok=True)
        with open(expanded_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return f"Success. Wrote to {expanded_path}"
    except Exception as e:
        return f"Failed to write file: {str(e)}"

# --- 3. THE BACKGROUND AUDIO WORKER ---
async def audio_worker(websocket: WebSocket, sentence_queue: asyncio.Queue):
    while True:
        sentence = await sentence_queue.get()
        if sentence is None: break
        
        def generate_tts(text):
            samples, sample_rate = kokoro.create(text, voice="am_michael", speed=1.25, lang="en-us")
            wav_io = io.BytesIO()
            sf.write(wav_io, samples, sample_rate, format='WAV')
            return base64.b64encode(wav_io.getvalue()).decode('utf-8')
            
        try:
            wav_base64 = await asyncio.to_thread(generate_tts, sentence)
            await websocket.send_json({"type": "audio", "data": wav_base64})
        except Exception:
            pass
        finally:
            sentence_queue.task_done()

# --- 4. THE STREAMING FIREHOSE ---
async def stream_llama_and_audio(websocket: WebSocket, messages: list):
    payload = {
        "model": "mlx-community/Llama-3.3-70B-Instruct-4bit",
        "messages": messages,
        "temperature": 0.3,
        "stream": True 
    }
    
    sentence_queue = asyncio.Queue()
    audio_task = asyncio.create_task(audio_worker(websocket, sentence_queue))
    
    is_tool_call = False
    first_chunk_checked = False
    full_buffer = ""
    sentence_buffer = ""
    
    async with httpx.AsyncClient() as client:
        request = client.build_request("POST", LOCAL_70B_URL, json=payload, timeout=90.0)
        response = await client.send(request, stream=True)
        
        async for line in response.aiter_lines():
            if line.startswith("data: "):
                data_str = line[6:]
                if data_str == "[DONE]": break
                try:
                    data_json = json.loads(data_str)
                    chunk = data_json["choices"][0]["delta"].get("content", "")
                    full_buffer += chunk
                    
                    if not first_chunk_checked and full_buffer.strip():
                        if full_buffer.strip().startswith("{"):
                            is_tool_call = True 
                        first_chunk_checked = True
                        
                    if is_tool_call:
                        continue 
                        
                    if chunk:
                        await websocket.send_json({"type": "text", "content": chunk})
                        
                    sentence_buffer += chunk
                    if any(punct in chunk for punct in ['.', '?', '!', '\n']):
                        clean_sentence = sentence_buffer.strip()
                        if len(clean_sentence) > 2:
                            await sentence_queue.put(clean_sentence)
                        sentence_buffer = ""
                except Exception:
                    continue
                    
    if not is_tool_call:
        clean_sentence = sentence_buffer.strip()
        if len(clean_sentence) > 2:
            await sentence_queue.put(clean_sentence)
            
    await sentence_queue.join()
    await sentence_queue.put(None)
    await audio_task
    
    return is_tool_call, full_buffer

# --- 5. THE AUTONOMOUS ROUTER (REACT LOOP) ---
@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()
    print("⚡ Mac Studio Autonomous WebSocket Opened.")
    try:
        while True:
            user_message = await websocket.receive_text()
            messages = [
                {"role": "system", "content": SKIPPY_SYSTEM_PROMPT},
                {"role": "user", "content": user_message}
            ]
            
            # The Reasoning Loop (Max 5 tool hops to prevent infinite loops)
            for step in range(5):
                is_tool, raw_response = await stream_llama_and_audio(websocket, messages)
                
                if is_tool:
                    try:
                        tool_data = json.loads(raw_response)
                        tool_name = tool_data.get("name")
                        
                        # Tell the UI what he is doing in the background
                        action_msg = f"\n*(Skippy is using {tool_name}...)*\n"
                        await websocket.send_json({"type": "text", "content": action_msg})
                        
                        tool_result = "No data found."
                        if tool_name == "web_search":
                            tool_result = web_search(tool_data.get("query", ""))
                        elif tool_name == "read_website":
                            tool_result = read_website(tool_data.get("url", ""))
                        elif tool_name == "run_terminal":
                            tool_result = run_terminal(tool_data.get("command", ""))
                        elif tool_name == "read_file":
                            tool_result = read_file(tool_data.get("path", ""))
                        elif tool_name == "write_file":
                            tool_result = write_file(tool_data.get("path", ""), tool_data.get("content", ""))
                            
                        # Feed the result back into his context window and LOOP!
                        messages.append({"role": "assistant", "content": raw_response})
                        messages.append({"role": "user", "content": f"TOOL RESULT:\n{tool_result}\nIf you have the complete answer, speak to the user. If you need to search, read, or write more, output ONLY a JSON tool call starting exactly with '{{'. DO NOT explain your thought process."})
                        continue 
                        
                    except json.JSONDecodeError:
                        messages.append({"role": "assistant", "content": raw_response})
                        messages.append({"role": "user", "content": "JSONDecodeError: You must output ONLY valid JSON. Start your response with '{'."})
                        continue
                else:
                    break
                    
            await websocket.send_json({"type": "done"})
            
    except WebSocketDisconnect:
        print("❌ MacBook Disconnected.")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
