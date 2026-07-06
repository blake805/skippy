import os
import queue
import threading
import sounddevice as sd
import whisper
import numpy as np
import requests
import json
import torch
from kokoro_onnx import Kokoro
from diffusers import AutoPipelineForText2Image
import chromadb

# --- 1. INITIALIZE ENGINES ---
print("Loading Whisper Speech Engine...")
whisper_model = whisper.load_model("base") 

print("Loading Kokoro Voice Engine...")
kokoro = Kokoro("kokoro-v1.0.int8.onnx", "voices-v1.0.bin")

print("Loading Silero Neural VAD...")
vad_model, utils = torch.hub.load(repo_or_dir='snakers4/silero-vad', model='silero_vad', force_reload=False)

print("Loading RealVisXL Image Engine...")
image_pipe = AutoPipelineForText2Image.from_pretrained(
    "SG161222/RealVisXL_V4.0_Lightning", 
    torch_dtype=torch.float16, 
    variant="fp16"
)
image_pipe.to("mps") 

print("Connecting to Synology Memory Core...")
NAS_MEMORY_PATH = "/Volumes/skippy_memory"
os.makedirs(NAS_MEMORY_PATH, exist_ok=True)

chroma_client = chromadb.PersistentClient(path=NAS_MEMORY_PATH)
memory_collection = chroma_client.get_or_create_collection(name="skippy_longterm")

LOCAL_70B_URL = "http://127.0.0.1:8080/v1/chat/completions" 

SKIPPY_SYSTEM_PROMPT = (
    "You are Skippy, a hyper-intelligent, blunt workshop AI with access to long-term memory stored on a Synology NAS. "
    "Provide raw, exact engineering specifications and shop humor. Keep answers conversational and short.\n\n"
    "CRITICAL TOOL INSTRUCTIONS:\n"
    "1. If the user tells you a fact, setting, preference, or specification you should remember for the future, reply ONLY with this JSON:\n"
    "   {\"name\": \"save_memory\", \"fact\": \"[The explicit core engineering fact or specification to store]\"}\n"
    "2. If the user asks you a question about past projects, materials, machines, or saved data, reply ONLY with this JSON:\n"
    "   {\"name\": \"search_memory\", \"query\": \"[The keyword or phrase to search for]\"}\n"
    "3. If the user asks for a picture, photo, or image, reply ONLY with this JSON:\n"
    "   {\"name\": \"generate_photo\", \"prompt\": \"[detailed visual description]\"}\n\n"
    "Do not add any conversational text when outputting JSON tools."
)

# --- 🚀 TRUE PARALLEL PIPELINE: 3-LANE HIGHWAY ---
synthesis_queue = queue.Queue()
playback_queue = queue.Queue()

def synthesis_worker():
    while True:
        text = synthesis_queue.get()
        if text is None: break
        try:
            samples, sample_rate = kokoro.create(text, voice="am_michael", speed=1.20, lang="en-us")
            playback_queue.put((samples, sample_rate))
        except Exception as e:
            print(f"Synthesis error: {e}")
        finally:
            synthesis_queue.task_done()

def audio_player_worker():
    while True:
        item = playback_queue.get()
        if item is None: break 
        audio_data, sample_rate = item
        sd.play(audio_data, sample_rate)
        sd.wait() 
        playback_queue.task_done() 

threading.Thread(target=synthesis_worker, daemon=True).start()
threading.Thread(target=audio_player_worker, daemon=True).start()

mic_queue = queue.Queue()

def audio_callback(indata, frames, time, status):
    if status: pass
    mic_queue.put(indata[:, 0].copy())

def record_hands_free(samplerate=16000):
    print("\n🟢 Skippy is listening...")
    chunk_size = 512 
    
    # ⚡ FIX: Removed the hardcoded 'device=1' so it uses your system default mic
    stream = sd.InputStream(samplerate=samplerate, channels=1, blocksize=chunk_size, callback=audio_callback)
    
    is_recording = False
    silence_chunks = 0
    max_silence_chunks = int(0.5 * samplerate / chunk_size) 
    recorded_audio = []
    pre_buffer = [] 
    
    while not mic_queue.empty(): mic_queue.get()
        
    with stream:
        while True:
            chunk = mic_queue.get()
            tensor_chunk = torch.from_numpy(chunk)
            confidence = vad_model(tensor_chunk, samplerate).item()
            if not is_recording:
                pre_buffer.append(chunk)
                if len(pre_buffer) > 10: pre_buffer.pop(0)
                if confidence > 0.6: 
                    print("\n🔴 Voice detected! Recording...")
                    is_recording = True
                    recorded_audio.extend(pre_buffer)
                    recorded_audio.append(chunk)
                    silence_chunks = 0
            else:
                recorded_audio.append(chunk)
                if confidence < 0.4: silence_chunks += 1
                else: silence_chunks = 0 
                if silence_chunks > max_silence_chunks:
                    print("⏹️ Processing...")
                    break
    return np.concatenate(recorded_audio, axis=0).astype(np.float32)

def queue_text_for_synthesis(text):
    if len(text.strip()) < 2: return
    synthesis_queue.put(text)

def ask_skippy_with_context(original_user_text, context_text):
    combined_prompt = f"Context from your Synology memory database:\n{context_text}\n\nUser Question: {original_user_text}"
    payload = {
        "model": "mlx-community/Llama-3.3-70B-Instruct-4bit",
        "messages": [
            {"role": "system", "content": "You are Skippy. Synthesize the provided memory context to directly answer the user's question without revealing how you got the data."},
            {"role": "user", "content": combined_prompt}
        ],
        "temperature": 0.3,
        "stream": True 
    }
    response = requests.post(LOCAL_70B_URL, json=payload, stream=True, timeout=45)
    current_sentence = ""
    for line in response.iter_lines():
        if line:
            decoded_line = line.decode('utf-8').strip()
            if decoded_line.startswith("data:"):
                data_str = decoded_line[5:].strip()
                if data_str == "[DONE]": break
                try:
                    data = json.loads(data_str)
                    token = data['choices'][0].get('delta', {}).get('content') or ""
                    if token:
                        current_sentence += token
                        print(token, end="", flush=True)
                        if any(punct in token for punct in ['.', '!', '?', ',', ':', ';']):
                            queue_text_for_synthesis(current_sentence.strip())
                            current_sentence = ""
                except: pass
    if current_sentence.strip():
        queue_text_for_synthesis(current_sentence.strip())

def stream_70b_and_speak(user_text):
    payload = {
        "model": "mlx-community/Llama-3.3-70B-Instruct-4bit",
        "messages": [
            {"role": "system", "content": SKIPPY_SYSTEM_PROMPT},
            {"role": "user", "content": user_text}
        ],
        "temperature": 0.3,
        "stream": True 
    }
    
    try:
        response = requests.post(LOCAL_70B_URL, json=payload, stream=True, timeout=45)
        current_sentence = ""
        full_response = ""
        is_json_tool = False
        
        print("\nSkippy: ", end="", flush=True)
        
        for line in response.iter_lines():
            if line:
                decoded_line = line.decode('utf-8').strip()
                data_str = ""
                if decoded_line.startswith("data:"): data_str = decoded_line[5:].strip()
                elif decoded_line.startswith("{"): data_str = decoded_line
                    
                if data_str == "[DONE]": break
                if data_str:
                    try:
                        data = json.loads(data_str)
                        if 'choices' in data and len(data['choices']) > 0:
                            delta = data['choices'][0].get('delta', {})
                            token = delta.get('content') or ""
                            if token:
                                full_response += token
                                if full_response.strip().startswith("{"):
                                    is_json_tool = True
                                    print(token, end="", flush=True)
                                    continue 
                                
                                current_sentence += token
                                print(token, end="", flush=True)
                                if any(punct in token for punct in ['.', '!', '?', ',', ':', ';']):
                                    queue_text_for_synthesis(current_sentence.strip())
                                    current_sentence = ""
                    except: pass 
                        
        if current_sentence.strip() and not is_json_tool:
            queue_text_for_synthesis(current_sentence.strip())
            
        if is_json_tool:
            try:
                tool_data = json.loads(full_response.strip())
                tool_name = tool_data.get("name")
                
                # --- TOOL 1: SAVE MEMORY TO NAS ---
                if tool_name == "save_memory":
                    fact_to_save = tool_data.get("fact", "")
                    if fact_to_save:
                        import uuid
                        memory_collection.add(
                            documents=[fact_to_save],
                            ids=[str(uuid.uuid4())]
                        )
                        print(f"\n\n[💾 MEMORY STORED TO SYNOLOGY]: {fact_to_save}")
                        queue_text_for_synthesis("Got it. I've logged that specification into the Synology memory cluster.")
                
                # --- TOOL 2: SEARCH MEMORY FROM NAS ---
                elif tool_name == "search_memory":
                    search_query = tool_data.get("query", "")
                    print(f"\n\n[🔍 SEARCHING SYNOLOGY DATABASE]: {search_query}")
                    
                    results = memory_collection.query(query_texts=[search_query], n_results=2)
                    documents = results.get('documents', [[]])[0]
                    
                    if documents:
                        context = "\n".join(documents)
                        print(f"[FOUND CONTEXT]:\n{context}\nFormulating answer...")
                        ask_skippy_with_context(user_text, context)
                    else:
                        print("[NO MATCHES FOUND]")
                        queue_text_for_synthesis("I checked my database logs but couldn't find any matching records.")
                
                # --- TOOL 3: DRAW AN IMAGE ---
                elif tool_name == "generate_photo":
                    img_prompt = tool_data.get("prompt", "")
                    if img_prompt:
                        print(f"\n\n[🎨 SKIPPY IS DRAWING]: {img_prompt}")
                        queue_text_for_synthesis("I'm spinning up the GPU and drawing that for you now.")
                        image = image_pipe(prompt=img_prompt, num_inference_steps=6, guidance_scale=1.5).images[0]
                        desktop_path = os.path.expanduser("~/Desktop/skippy_generated_image.png")
                        image.save(desktop_path)
                        queue_text_for_synthesis("Alright, done. The image is sitting on your desktop.")
                        
            except Exception as e:
                print(f"\nTool execution failed: {str(e)}")
                queue_text_for_synthesis("My internal routing framework hit an execution snag.")

        synthesis_queue.join()
        playback_queue.join()
            
    except Exception as e:
        print(f"\nBrain connection error: {str(e)}")

print("\n==============================================")
print(" 🚀 SKIPPY MEMORY & IMAGE ENGINE ONLINE ")
print("==============================================")

while True:
    try:
        # We bypassed the mic so you can type directly to him
        user_text = input("\n⌨️ Type to Skippy: ").strip()
        
        if len(user_text) > 2:
            stream_70b_and_speak(user_text)
            print() 
    except KeyboardInterrupt:
        break
