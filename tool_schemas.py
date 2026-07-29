# tool_schemas.py
#
# OpenAI-format function schemas for native tool calling via mlx_lm.server.
# These replace the old "output ONLY this exact JSON" text protocol: the
# schemas are sent in the `tools` field of the chat completion request and
# the model responds with structured `tool_calls`.
#
# What remains here is the research and context half of the toolset. The
# filesystem, patch, and terminal schemas arrive with the agent runtime.

_SCHEMAS = {
    "list_dir": {
        "description": (
            "Lists a directory tree inside the workspace. Start here to orient in an "
            "unfamiliar repo before reading anything."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to list. Omit it to list every workspace root, which is how you find out what repositories exist."},
                "depth": {"type": "integer", "description": "How many levels deep, 1-6. Defaults to 2."},
            },
            "required": [],
        },
    },
    "read_file": {
        "description": (
            "Reads a text file with line numbers. Pass start_line/end_line for a window "
            "into a large file rather than pulling all of it into context."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path, relative to the workspace root."},
                "start_line": {"type": "integer", "description": "First line to read, 1-based."},
                "end_line": {"type": "integer", "description": "Last line to read, inclusive."},
            },
            "required": ["path"],
        },
    },
    "grep": {
        "description": (
            "Searches file contents by regular expression and returns path:line:text. "
            "The fastest way to find where something is defined or used."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression to search for."},
                "path": {"type": "string", "description": "Directory to search. Omit it to search every workspace root."},
                "glob": {"type": "string", "description": "Restrict to matching filenames, e.g. '*.py'."},
                "max_results": {"type": "integer", "description": "Cap on lines returned, 1-500. Defaults to 50."},
                "ignore_case": {"type": "boolean", "description": "Case-insensitive search."},
            },
            "required": ["pattern"],
        },
    },
    "glob_files": {
        "description": "Finds files by path pattern, e.g. '**/test_*.py'. Use when you know the shape of the name but not the location.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern, e.g. '**/*.swift'."},
                "path": {"type": "string", "description": "Directory to search from. Omit it to search every workspace root."},
            },
            "required": ["pattern"],
        },
    },
    "get_system_time": {
        "description": "Returns the current system date and time.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "web_search": {
        "description": "Searches the web and returns the top results as JSON.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search terms."}},
            "required": ["query"],
        },
    },
    "read_website": {
        "description": "Fetches a web page and returns its readable text content.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "Full URL to read."}},
            "required": ["url"],
        },
    },
    "search_memory": {
        "description": "Searches Skippy's long-term memory (ChromaDB on the NAS) for saved facts.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search phrase."}},
            "required": ["query"],
        },
    },
    "save_memory": {
        "description": "Permanently saves a fact to Skippy's long-term memory.",
        "parameters": {
            "type": "object",
            "properties": {"fact": {"type": "string", "description": "The fact to store."}},
            "required": ["fact"],
        },
    },
    "github_manager": {
        "description": "Performs GitHub operations (clone a repo, list issues, create an issue) via the gh CLI.",
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repository URL or owner/name."},
                "action": {"type": "string", "enum": ["clone", "list_issues", "create_issue"]},
                "title": {"type": "string", "description": "Issue title (create_issue only)."},
                "body": {"type": "string", "description": "Issue body (create_issue only)."},
            },
            "required": ["repo", "action"],
        },
    },
    "read_directory_structure": {
        "description": "Maps a folder's file tree using the tree command.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Directory to map."},
                "max_depth": {"type": "integer", "description": "Tree depth, default 2."},
            },
            "required": ["path"],
        },
    },
    "ingest_codebase_to_rag": {
        "description": "Recursively chunks and embeds a codebase folder into ChromaDB code memory.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Directory to ingest."}},
            "required": ["path"],
        },
    },
    "search_codebase": {
        "description": "Searches previously ingested code memory in ChromaDB and returns a compressed summary of the matches.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search terms."}},
            "required": ["query"],
        },
    },
}


def _wrap(name: str) -> dict:
    schema = _SCHEMAS[name]
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": schema["description"],
            "parameters": schema["parameters"],
        },
    }


FILESYSTEM_TOOLS = ("list_dir", "read_file", "grep", "glob_files")


def filesystem_tools() -> list:
    """The read-only workspace tools, wrapped for the `tools` request field."""
    return [_wrap(name) for name in FILESYSTEM_TOOLS]


def research_tools() -> list:
    """Every schema here, wrapped for the `tools` field of a completion request."""
    return [_wrap(name) for name in _SCHEMAS]

