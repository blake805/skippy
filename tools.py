import os
import json
import asyncio
import datetime
import urllib.request
from typing import Any
from ddgs import DDGS
import uuid


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
        req = urllib.request.Request(f"https://r.jina.ai/{url}", headers={'User-Agent': 'Mozilla/5.0'})
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

async def execute_github_manager(repo: str, action: str, title: str = None, body: str = None) -> str:
    """Manages GitHub operations using the host's `gh` CLI."""
    workspace_dir = "/tmp/skippy_workspaces/"
    os.makedirs(workspace_dir, exist_ok=True)
    
    # Use absolute path for Apple Silicon Homebrew installations
    GH_PATH = "/opt/homebrew/bin/gh"
    
    # Pre-flight check (exec with arg lists everywhere so LLM-generated titles
    # or repo names containing quotes can't inject shell commands)
    auth_check = await asyncio.create_subprocess_exec(GH_PATH, "auth", "status", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
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
            
        process = await asyncio.create_subprocess_exec(GH_PATH, "repo", "clone", repo, target_dir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        _, stderr = await process.communicate()
        
        if process.returncode == 0:
            return json.dumps({"status": "success", "message": "Cloned successfully.", "path": target_dir})
        return json.dumps({"error": "Clone failed.", "details": stderr.decode().strip()})

    elif action == "list_issues":
        process = await asyncio.create_subprocess_exec(GH_PATH, "issue", "list", "--repo", repo, "--json", "number,title,state,url", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        stdout, stderr = await process.communicate()
        if process.returncode == 0:
            return stdout.decode().strip()
        return json.dumps({"error": "Failed to fetch issues.", "details": stderr.decode().strip()})

    elif action == "create_issue":
        if not title: return json.dumps({"error": "Missing 'title' for issue."})
        args = [GH_PATH, "issue", "create", "--repo", repo, "--title", title]
        if body: args += ["--body", body]
        
        process = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
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
        
    process = await asyncio.create_subprocess_exec(TREE_PATH, "-L", str(max_depth), expanded_path, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    stdout, stderr = await process.communicate()
    
    if process.returncode == 0:
        return stdout.decode().strip()
    return json.dumps({"error": "Failed to map directory. Is 'tree' installed via Homebrew?", "details": stderr.decode().strip()})

# ==========================================
# 🧠 CODEBASE INGESTION VIA RAG (Goal C)
# ==========================================

async def ingest_codebase_to_rag(path: str, collection) -> str:
    """Recursively processes code files in a folder and saves chunks into ChromaDB."""
    expanded_path = os.path.expanduser(path)
    if not os.path.exists(expanded_path):
        return json.dumps({"error": f"Path {expanded_path} does not exist."})
        
    supported_extensions = {'.py', '.swift', '.js', '.ts', '.cpp', '.h', '.ino', '.json', '.md', '.txt', '.java', '.c'}
    chunk_count = 0
    
    # Helper to chunk text synchronously to avoid thread-blocking inside the loop
    def chunk_text(text, max_chars=1500):
        return [text[i:i+max_chars] for i in range(0, len(text), max_chars)]

    for root, dirs, files in os.walk(expanded_path):
        # Ignore hidden directory tracking
        dirs[:] = [d for d in dirs if not d.startswith('.')]
        
        for file in files:
            ext = os.path.splitext(file)[1].lower()
            if ext in supported_extensions:
                file_path = os.path.join(root, file)
                try:
                    with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                        content = f.read()
                    
                    if not content.strip():
                        continue
                        
                    chunks = chunk_text(content)
                    for i, chunk in enumerate(chunks):
                        chunk_id = f"code_{uuid.uuid4()}"
                        collection.add(
                            documents=[chunk],
                            metadatas=[{"source": file_path, "chunk_index": i, "filename": file}],
                            ids=[chunk_id]
                        )
                        chunk_count += 1
                except Exception:
                    continue

    return f"SUCCESS: Ingested {chunk_count} code chunks from {expanded_path} into ChromaDB memory."

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