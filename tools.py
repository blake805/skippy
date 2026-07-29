import os
import json
import asyncio
import datetime
import subprocess
import urllib.request
from typing import Any
from duckduckgo_search import DDGS
import uuid
import paramiko

# --- DYNAMIC SKILLS DIRECTORY ---
# This automatically creates a 'skills' folder in the same directory as this file
SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")
os.makedirs(SKILLS_DIR, exist_ok=True)

# ==========================================
# 🤖 SYNTHETIC AUTONOMY TOOLS (Goal Ledger & Sandbox)
# ==========================================
GOALS_FILE = os.path.join(os.path.dirname(__file__), "skippy_goals.json")

def manage_goals(action: str, task: str = None, task_id: int = None) -> str:
    """Manages Skippy's internal persistent goal ledger."""
    if not os.path.exists(GOALS_FILE):
        with open(GOALS_FILE, "w") as f:
            json.dump({"tasks": []}, f)
            
    with open(GOALS_FILE, "r") as f:
        data = json.load(f)
        
    tasks = data.get("tasks", [])

    if action == "view":
        if not tasks:
            return "Goal ledger is empty. You are idle."
        
        # Format the JSON into a highly readable bulleted list for the LLM
        formatted_list = "CURRENT GOAL LEDGER:\n"
        for t in tasks:
            formatted_list += f"- [ID: {t['id']}] STATUS: {t['status'].upper()} | TASK: {t['task']}\n"
        return formatted_list

    elif action == "add":
        if not task:
            return "Error: 'task' description required to add a goal."
        new_id = max([t.get("id", 0) for t in tasks] + [0]) + 1
        tasks.append({"id": new_id, "task": task, "status": "pending", "added_at": datetime.datetime.now().isoformat()})
        with open(GOALS_FILE, "w") as f:
            json.dump({"tasks": tasks}, f, indent=2)
        return f"Task added with ID {new_id}."

    elif action == "start":
        if task_id is None:
            return "Error: 'task_id' required to start a goal."
        for t in tasks:
            if t["id"] == task_id:
                t["status"] = "in_progress"
                with open(GOALS_FILE, "w") as f:
                    json.dump({"tasks": tasks}, f, indent=2)
                return f"Task {task_id} marked as in_progress."
        return f"Error: Task ID {task_id} not found."

    elif action == "complete":
        if task_id is None:
            return "Error: 'task_id' required to complete a goal."
        for t in tasks:
            if t["id"] == task_id:
                t["status"] = "completed"
                t["completed_at"] = datetime.datetime.now().isoformat()
                
                # Filter out completed tasks so the ledger doesn't bloat endlessly
                tasks = [tsk for tsk in tasks if tsk["status"] != "completed"]
                
                with open(GOALS_FILE, "w") as f:
                    json.dump({"tasks": tasks}, f, indent=2)
                return f"Task {task_id} marked as completed and cleared from ledger."
        return f"Error: Task ID {task_id} not found."
    
    return "Error: Invalid action. Use 'view', 'add', 'start', or 'complete'."

def sandbox_test(script_path: str) -> str:
    """
    Executes a python script in a protected subprocess sandbox.
    Catches infinite loops via a 15-second timeout and returns stdout/stderr tracebacks.
    """
    expanded_path = os.path.expanduser(script_path)
    if not os.path.exists(expanded_path):
        return f"Error: File {expanded_path} does not exist."
        
    try:
        result = subprocess.run(
            ["python3", expanded_path],
            capture_output=True,
            text=True,
            timeout=15,
            check=False
        )
        
        output = f"EXIT CODE: {result.returncode}\n"
        if result.stdout:
            output += f"STDOUT:\n{result.stdout}\n"
        if result.stderr:
            output += f"STDERR:\n{result.stderr}\n"
            
        return output

    except subprocess.TimeoutExpired:
        return "CRITICAL ERROR: Execution timed out after 15 seconds. Infinite loop detected."
    except Exception as e:
        return f"SYSTEM ERROR: Failed to execute sandbox environment: {str(e)}"

# ==========================================
# 🛠️ STANDARD SHOP TOOLS
# ==========================================

def get_system_time() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

async def web_search(query: str) -> str:
    try: 
        # Offload blocking DDGS call to thread
        return await asyncio.to_thread(lambda: json.dumps(DDGS().text(query, max_results=4)))
    except Exception as e: 
        return f"Search error: {str(e)}"

async def read_website(url: str) -> str:
    try:
        req = urllib.request.Request(f"[https://r.jina.ai/](https://r.jina.ai/){url}", headers={'User-Agent': 'Mozilla/5.0'})
        response = await asyncio.to_thread(urllib.request.urlopen, req, timeout=15)
        return response.read().decode('utf-8')[:5000] 
    except Exception as e: 
        return f"Failed to read website: {str(e)}"

async def search_memory(query: str, collection: Any) -> str:
    try:
        # Offload ChromaDB query to thread
        results = await asyncio.to_thread(lambda: collection.query(query_texts=[query], n_results=3))
        docs = results.get('documents', [[]])[0]
        return "\n".join(docs) if docs else "No matching records found in Synology NAS."
    except Exception as e: 
        return f"Memory search error: {str(e)}"

async def save_memory(fact: str, collection: Any) -> str:
    try:
        doc_id = str(uuid.uuid4())
        await asyncio.to_thread(lambda: collection.add(documents=[fact], ids=[doc_id]))
        return "Fact successfully saved to Synology NAS."
    except Exception as e: 
        return f"Memory save error: {str(e)}"

async def send_to_tormach(local_file_path: str) -> str:
    try:
        expanded_path = os.path.expanduser(local_file_path)
        cmd = f"scp {expanded_path} operator@192.168.1.219:~/gcode/"
        process = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        if process.returncode == 0: 
            return f"SUCCESS: Transferred {expanded_path} to Tormach PathPilot."
        else: 
            return f"ERROR transferring file: {stderr.decode('utf-8')}"
    except Exception as e: 
        return f"SYSTEM ERROR during transfer: {str(e)}"

async def check_device_status(ip_address: str) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            "ping", "-c", "1", "-W", "1", ip_address,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL
        )
        await process.communicate()
        return "ONLINE" if process.returncode == 0 else "OFFLINE"
    except Exception as e:
        return f"SYSTEM ERROR checking device: {str(e)}"

# ==========================================
# 🧠 DYNAMIC SKILL REGISTRY CAPABILITIES
# ==========================================

async def save_new_skill(skill_name: str, code: str, description: str) -> str:
    """Allows Skippy to save a reusable Python tool permanently to his disk."""
    safe_name = skill_name.replace(" ", "_").lower()
    if not safe_name.endswith(".py"): safe_name += ".py"
        
    filepath = os.path.join(SKILLS_DIR, safe_name)
    
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(f'"""\nDESCRIPTION: {description}\n"""\n\n')
            f.write(code)
        return f"SUCCESS: Skill '{skill_name}' saved to {filepath}. You can now run this using the run_shop_skill tool."
    except Exception as e:
        return f"FAILED to save skill: {str(e)}"

async def run_shop_skill(skill_name: str, arguments: str = "") -> str:
    """Allows Skippy to execute a previously saved skill from his library."""
    safe_name = skill_name.replace(" ", "_").lower()
    if not safe_name.endswith(".py"): safe_name += ".py"
        
    filepath = os.path.join(SKILLS_DIR, safe_name)
    
    if not os.path.exists(filepath):
        available = [f.replace(".py", "") for f in os.listdir(SKILLS_DIR) if f.endswith(".py")]
        return f"ERROR: Skill '{skill_name}' does not exist. Available skills: {available}"
        
    try:
        cmd = f"python3 {filepath} {arguments}".strip()
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15.0)
        except asyncio.TimeoutError:
            process.kill()
            return f"ERROR: Skill '{skill_name}' timed out after 15 seconds."
            
        output = stdout.decode().strip()
        errors = stderr.decode().strip()
        
        if errors: return f"SKILL EXECUTED WITH ERRORS:\n{errors}\n\nOUTPUT:\n{output}"
        return f"SKILL SUCCESS OUTPUT:\n{output}"
    except Exception as e:
        return f"SYSTEM ERROR running skill: {str(e)}"

# ==========================================
# 🚀 TORMACH PATHPILOT SSH INTEGRATION
# ==========================================

async def execute_tormach_ssh(command: str) -> str:
    """Securely executes a command on the Tormach PathPilot controller via SSH."""
    
    tormach_ip = "192.168.1.219"  
    tormach_user = "operator"
    tormach_password = "Mason0613!" # Switched to password authentication
    
    def _run_ssh():
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # Connect using password auth
        ssh.connect(
            hostname=tormach_ip, 
            username=tormach_user, 
            password=tormach_password,
            timeout=10.0
        )
        
        stdin, stdout, stderr = ssh.exec_command(command)
        out = stdout.read().decode('utf-8').strip()
        err = stderr.read().decode('utf-8').strip()
        ssh.close()
        return out, err

    try:
        out, err = await asyncio.to_thread(_run_ssh)
        
        if err:
            return f"--- SSH EXECUTION ERROR ---\n{err}\n--- OUTPUT ---\n{out}"
        if out:
            return f"--- SSH EXECUTION SUCCESS ---\n{out}"
        else:
            return "--- SSH EXECUTION SUCCESS ---\n(Command executed successfully with no terminal output)"
            
    except Exception as e:
        return f"--- SSH CONNECTION FAILED ---\n{str(e)}"
    
# ==========================================
# 🐙 GITHUB CLI INTEGRATION (Goal B)
# ==========================================

async def execute_github_manager(repo: str, action: str, title: str = None, body: str = None) -> str:
    """Manages GitHub operations using the host's `gh` CLI."""
    workspace_dir = "/tmp/skippy_workspaces/"
    os.makedirs(workspace_dir, exist_ok=True)
    
    # Use absolute path for Apple Silicon Homebrew installations
    GH_PATH = "/opt/homebrew/bin/gh"
    
    # Pre-flight check
    auth_check = await asyncio.create_subprocess_shell(f"{GH_PATH} auth status", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    _, stderr = await auth_check.communicate()
    
    if auth_check.returncode != 0:
        return json.dumps({
            "error": "GitHub CLI is not authenticated or PATH is wrong.", 
            "details": stderr.decode().strip()
        })

    if action == "clone":
        repo_name = repo.split("/")[-1].replace(".git", "")
        target_dir = os.path.join(workspace_dir, repo_name)
        
        if os.path.exists(target_dir):
            return json.dumps({"status": "success", "message": "Repo already exists.", "path": target_dir})
            
        cmd = f"{GH_PATH} repo clone {repo} {target_dir}"
        process = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await process.communicate()
        
        if process.returncode == 0:
            return json.dumps({"status": "success", "message": "Cloned successfully.", "path": target_dir})
        return json.dumps({"error": "Clone failed.", "details": stderr.decode().strip()})

    elif action == "list_issues":
        cmd = f"{GH_PATH} issue list --repo {repo} --json number,title,state,url"
        process = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            return stdout.decode().strip()
        return json.dumps({"error": "Failed to fetch issues.", "details": stderr.decode().strip()})

    elif action == "create_issue":
        if not title: return json.dumps({"error": "Missing 'title' for issue."})
        cmd = f"{GH_PATH} issue create --repo {repo} --title '{title}'"
        if body: cmd += f" --body '{body}'"
        
        process = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            return json.dumps({"status": "success", "url": stdout.decode().strip()})
        return json.dumps({"error": "Issue creation failed.", "details": stderr.decode().strip()})

    return json.dumps({"error": f"Unsupported action: '{action}'."})

# ==========================================
# 📂 DIRECTORY MAPPING (Goal D)
# ==========================================

async def read_directory_structure(path: str, max_depth: int = 2) -> str:
    """Runs the 'tree' command to map a folder's architecture."""
    expanded_path = os.path.expanduser(path)
    if not os.path.exists(expanded_path):
        return json.dumps({"error": f"Path {expanded_path} does not exist."})
        
    # Use absolute path for Homebrew on Apple Silicon
    TREE_PATH = "/opt/homebrew/bin/tree"
        
    cmd = f"{TREE_PATH} -L {max_depth} {expanded_path}"
    process = await asyncio.create_subprocess_shell(cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()
    
    if process.returncode == 0:
        return stdout.decode().strip()
    return json.dumps({"error": "Failed to map directory. Is 'tree' installed via Homebrew?", "details": stderr.decode().strip()})

# ==========================================
# 🧠 CODEBASE INGESTION VIA RAG (Goal C)
# ==========================================

SKIP_INGEST_DIRS = {
    "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", "venv", ".venv",
    "dist", "build", ".build", "target", ".next",
}


async def ingest_codebase_to_rag(path: str, collection, project_id: str = None) -> str:
    """Recursively processes code files in a folder and saves chunks into ChromaDB.

    Chunk ids are derived from the path and line range rather than a random uuid, so
    re-ingesting a project updates chunks in place instead of stacking duplicate
    copies of every file. Metadata carries the repo-relative path and the chunk's
    line range so a search hit can be turned back into a `read_file` call.
    """
    expanded_path = os.path.expanduser(path)
    if not os.path.exists(expanded_path):
        return json.dumps({"error": f"Path {expanded_path} does not exist."})

    supported_extensions = {'.py', '.swift', '.js', '.ts', '.cpp', '.h', '.ino', '.json', '.md', '.txt', '.java', '.c'}
    chunk_count = 0
    file_count = 0

    def chunk_by_lines(lines, max_chars=1500):
        """Split on line boundaries so a chunk never bisects a statement."""
        chunks, current, size, start = [], [], 0, 1
        for offset, line in enumerate(lines, start=1):
            if current and size + len(line) > max_chars:
                chunks.append((start, offset - 1, "".join(current)))
                current, size, start = [], 0, offset
            current.append(line)
            size += len(line)
        if current:
            chunks.append((start, len(lines), "".join(current)))
        return chunks

    for root, dirs, files in os.walk(expanded_path):
        dirs[:] = [d for d in dirs if not d.startswith('.') and d not in SKIP_INGEST_DIRS]

        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext not in supported_extensions:
                continue
            file_path = os.path.join(root, file)
            relative_path = os.path.relpath(file_path, expanded_path)
            try:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    lines = f.readlines()

                if not any(line.strip() for line in lines):
                    continue

                file_count += 1
                for index, (first_line, last_line, chunk) in enumerate(chunk_by_lines(lines)):
                    metadata = {
                        "source": file_path,
                        "relative_path": relative_path,
                        "filename": file,
                        "extension": ext,
                        "chunk_index": index,
                        "start_line": first_line,
                        "end_line": last_line,
                    }
                    if project_id:
                        metadata["project_id"] = project_id
                    chunk_id = f"code:{project_id or 'global'}:{relative_path}:{index}"
                    document = f"{relative_path} (lines {first_line}-{last_line}):\n{chunk}"
                    try:
                        collection.upsert(documents=[document], metadatas=[metadata], ids=[chunk_id])
                    except AttributeError:
                        collection.add(documents=[document], metadatas=[metadata], ids=[chunk_id])
                    chunk_count += 1
            except Exception:
                continue

    scope = f" for project '{project_id}'" if project_id else ""
    return (
        f"SUCCESS: Ingested {chunk_count} code chunks from {file_count} file(s) in "
        f"{expanded_path}{scope} into ChromaDB memory."
    )

# ==========================================
# 🔍 CODEBASE SEARCH (Goal E)
# ==========================================

async def search_codebase(query: str, collection, n_results: int = 3) -> str:
    """Searches the ChromaDB code memory for relevant chunks."""
    try:
        results = collection.query(
            query_texts=[query],
            n_results=n_results
        )
        
        if not results['documents'][0]:
            return json.dumps({"message": "No relevant code found in memory."})
            
        formatted_results = []
        for i in range(len(results['documents'][0])):
            doc = results['documents'][0][i]
            meta = results['metadatas'][0][i]
            source = meta.get('source', 'Unknown File')
            formatted_results.append(f"--- FILE: {source} ---\n{doc}\n")
            
        return "\n\n".join(formatted_results)
    except Exception as e:
        return json.dumps({"error": f"Search failed: {str(e)}"})