import gradio as gr
import requests
import json
import os
import re

LOCAL_70B_URL = "http://127.0.0.1:8080/v1/chat/completions"

def query_model(system_prompt, user_text, temp=0.2):
    payload = {
        "model": "mlx-community/Llama-3.3-70B-Instruct-4bit",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_text}
        ],
        "temperature": temp
    }
    try:
        response = requests.post(LOCAL_70B_URL, json=payload, timeout=120)
        if response.status_code == 200:
            return response.json()["choices"][0]["message"]["content"].strip()
        return f"HTTP Error: {response.status_code}"
    except Exception as e:
        return f"System Error: {str(e)}"

# --- AGENT PROMPTS ---
ARCHITECT_PROMPT = """You are the Architect. Read the user's request and any provided file contents.
Write a strict, step-by-step blueprint of what needs to be coded or fixed.
Do not write the final code. Just the logic and architecture plan."""

ENGINEER_PROMPT = """You are the Engineer. Read the Architect's blueprint.
Write the pure, raw Python code to execute the plan.
Output the code inside standard markdown ```python ``` blocks.
Do not add conversational text."""

QA_PROMPT = """You are the QA Lead. Review the Engineer's code against the Architect's plan.
Look for missing imports, syntax errors, and logic flaws.
If it fails, output: FAIL: [detailed reasons].
If it passes and is ready to save, output ONLY this exact JSON format:
{"status": "APPROVE", "save_path": "~/Desktop/filename.py"}"""

def extract_file_contents(prompt):
    """Smart File Injector: Automatically finds paths and reads them into the prompt."""
    paths = re.findall(r'(~?/[^\s]+(?:/[^\s]+)*\.\w+)', prompt)
    injections = ""
    for path in paths:
        try:
            expanded = os.path.expanduser(path)
            if os.path.exists(expanded):
                with open(expanded, 'r', encoding='utf-8') as f:
                    injections += f"\n\n--- INJECTED FILE CONTENTS: {path} ---\n{f.read()[:10000]}\n---------------------------\n"
        except Exception as e:
            injections += f"\n\n--- FILE ERROR: Could not read {path} ---\n"
    return prompt + injections

def run_assembly_line(user_input, history):
    if not user_input.strip():
        yield history, ""
        return

    # Initialize chat history item and background log trace
    history.append([user_input, "⚙️ *Skippy Assembly Line initiated... Connecting to local 70B model.*"])
    logs = "=== ⚡ SKIPPY MULTI-AGENT LOGS ===\n"
    yield history, logs

    # 1. Smart Injector
    enriched_prompt = extract_file_contents(user_input)
    if enriched_prompt != user_input:
        logs += "[Smart Injector] Local file paths recognized. Appending raw contents to model context.\n"
        yield history, logs

    # 2. Architect Steps In
    logs += "\n[Architect] Analyzing instructions and forming technical blueprint...\n"
    yield history, logs
    
    blueprint = query_model(ARCHITECT_PROMPT, enriched_prompt, temp=0.2)
    
    logs += f"----- ARCHITECT BLUEPRINT GENERATED -----\n{blueprint}\n----------------------------------------\n"
    history[-1][1] = "👷 *Architect blueprint finalized. Engineering is processing your code...*"
    yield history, logs

    # 3. Engineer & QA Iterative Loop
    engineer_code = ""
    success = False

    for attempt in range(4):
        logs += f"\n[Engineer] Coding session started. Attempt {attempt + 1}/4...\n"
        yield history, logs
        
        engineer_code = query_model(ENGINEER_PROMPT, f"Blueprint:\n{blueprint}", temp=0.1)
        
        logs += f"----- ENGINEER CODE SECTIONS DRAFTED -----\n{engineer_code}\n------------------------------------------\n"
        logs += f"[QA Lead] Executing automated review on code safety and constraints...\n"
        yield history, logs
        
        qa_review = query_model(QA_PROMPT, f"Code to review:\n{engineer_code}\n\nOriginal Request:\n{user_input}", temp=0.1)
        
        logs += f"----- QA VERDICT REPORT -----\n{qa_review}\n-----------------------------\n"
        yield history, logs

        # Check for approval pattern
        if "APPROVE" in qa_review and "{" in qa_review:
            try:
                # Decouple and isolate JSON payload
                json_str = qa_review[qa_review.find("{"):qa_review.rfind("}")+1]
                qa_data = json.loads(json_str)
                save_path = os.path.expanduser(qa_data.get("save_path", "~/Desktop/skippy_output.py"))

                # Parse the raw python lines
                code_to_save = engineer_code
                if "```python" in engineer_code:
                    code_to_save = engineer_code.split("```python")[1].split("```")[0].strip()
                elif "```" in engineer_code:
                    code_to_save = engineer_code.split("```")[1].split("```")[0].strip()

                # Native OS writing block
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, 'w', encoding='utf-8') as f:
                    f.write(code_to_save)

                logs += f"\n[Success] QA Sign-off acquired. Native payload deployed to local disk path: {save_path}\n"
                
                # Show the clean UI output to the user
                history[-1][1] = f"✅ **Compilation Successful!**\n\nThe code passed all QA gates and was saved locally to:\n`{save_path}`\n\n### Executable Blueprint Output:\n```python\n{code_to_save}\n```"
                success = True
                yield history, logs
                break
            except Exception as e:
                history[-1][1] = f"❌ **File System Error:** Failed writing script to disk. {str(e)}"
                logs += f"\n[Write Error] Exception raised during file save: {str(e)}\n"
                yield history, logs
                return
        else:
            # Inject structural feedback loop into context
            blueprint += f"\n\nQA FAILED ON PREVIOUS ATTEMPT. Feedback:\n{qa_review}\nEngineer, fix these issues."
            history[-1][1] = f"🔄 *QA rejected code iteration {attempt + 1}. Routing critique logs back to development engine...*"
            yield history, logs

    if not success:
        history[-1][1] = "❌ **Pipeline Terminated.** QA rejected code optimizations over maximum allowed attempts. Expand the debug window below to analyze the stack trace."
        logs += "\n=== ASSEMBLY LINE CLOSURE - STATUS: FAILURE ===\n"
        yield history, logs

# --- GRADIO CUSTOM BLOCKS UI LAYOUT ---
with gr.Blocks(title="⚡ Skippy: AI Assembly Line", theme=gr.themes.Default()) as demo:
    gr.Markdown("# ⚡ Skippy: AI Assembly Line")
    gr.Markdown("Multi-Agent Coding Factory. Optimized for complex reasoning and local file creation.")

    chatbot = gr.Chatbot(label="Skippy Stream Window", bubble_chat_colors=None)
    
    with gr.Row():
        user_msg = gr.Textbox(
            label="Input Command", 
            placeholder="E.g., Read ~/Desktop/broken.py, repair constraints, and save to fixed.py",
            lines=2,
            scale=4
        )
        submit_btn = gr.Button("Execute Pipeline", variant="primary", scale=1)

    # COLLAPSIBLE DEBUG CONTAINER
    with gr.Accordion("🕵️ Agent Factory Internal Logs (Debug Console)", open=False):
        debug_output = gr.Code(
            label="Live Model Thread Streams", 
            language="markdown", 
            lines=18
        )

    # Wire events
    submit_event = submit_btn.click(
        fn=run_assembly_line,
        inputs=[user_msg, chatbot],
        outputs=[chatbot, debug_output]
    )
    # Clear the input textbox instantly upon submission
    submit_event.then(fn=lambda: "", outputs=user_msg)

if __name__ == "__main__":
    demo.launch(server_name="0.0.0.0", server_port=7860)
