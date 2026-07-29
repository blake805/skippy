# tool_schemas.py
#
# OpenAI-format function schemas for native tool calling via mlx_lm.server.
# These replace the old "output ONLY this exact JSON" text protocol: the
# schemas are sent in the `tools` field of the chat completion request and
# the model responds with structured `tool_calls`.

_SCHEMAS = {
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
    "send_to_tormach": {
        "description": "Transfers a local G-code file to the Tormach PathPilot controller via SCP.",
        "parameters": {
            "type": "object",
            "properties": {"local_file_path": {"type": "string", "description": "Absolute path of the local file to send."}},
            "required": ["local_file_path"],
        },
    },
    "check_device_status": {
        "description": "Pings a device on the shop network and reports ONLINE or OFFLINE.",
        "parameters": {
            "type": "object",
            "properties": {"ip_address": {"type": "string", "description": "IP address to ping."}},
            "required": ["ip_address"],
        },
    },
    "run_shop_skill": {
        "description": (
            "Runs an EXISTING Python skill from the skills/ library with command-line "
            "arguments and returns its output. This CANNOT create, edit, or save skills — "
            "to create a NEW skill, call wake_engineer with a blueprint instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skill_name": {"type": "string", "description": "Name of the existing skill (without .py)."},
                "arguments": {"type": "string", "description": "Command-line arguments to pass, as a single string."},
            },
            "required": ["skill_name"],
        },
    },
    "tormach_ssh": {
        "description": (
            "Executes a shell command on the Tormach PathPilot controller over SSH. "
            "Requires explicit human approval before it runs."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Shell command to execute on PathPilot."},
                "explanation": {"type": "string", "description": "One-sentence summary of why, shown to the human approver."},
            },
            "required": ["command", "explanation"],
        },
    },
    "vscode_get_active_file": {
        "description": "Asks the connected VS Code client for the contents of the currently active file.",
        "parameters": {"type": "object", "properties": {}, "required": []},
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
    "manage_goals": {
        "description": "Views or edits Skippy's persistent goal ledger.",
        "parameters": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["view", "add", "start", "complete"]},
                "task": {"type": "string", "description": "Task description (add only)."},
                "task_id": {"type": "integer", "description": "Task ID (start/complete only)."},
            },
            "required": ["action"],
        },
    },
    "generate_image": {
        "description": (
            "Generates a hyperrealistic image from a text description using the local "
            "Pony Realism model. Describe the scene in detail: subject, materials, "
            "lighting, camera angle. Returns the saved file path — always tell the user "
            "the exact path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed scene description (subject, materials, lighting, camera)."},
                "negative_prompt": {"type": "string", "description": "Things to avoid in the image (optional)."},
                "width": {"type": "integer", "description": "Image width in pixels. Use SDXL sizes: 1024, 832, 1216. Default 1024."},
                "height": {"type": "integer", "description": "Image height in pixels. Use SDXL sizes: 1024, 832, 1216. Default 1024."},
            },
            "required": ["prompt"],
        },
    },
    "edit_image": {
        "description": (
            "Edits an existing image file guided by a text prompt (img2img with the "
            "Pony Realism model). Use strength to control how much changes: 0.2-0.4 "
            "subtle touch-up, 0.5-0.6 moderate restyle, 0.7-0.9 heavy transformation. "
            "Returns the new file path — always tell the user the exact path."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Absolute path of the source image on the Mac Studio."},
                "prompt": {"type": "string", "description": "Description of the desired result."},
                "strength": {"type": "number", "description": "How strongly to change the image, 0.05-1.0. Default 0.55."},
                "negative_prompt": {"type": "string", "description": "Things to avoid (optional)."},
            },
            "required": ["image_path", "prompt"],
        },
    },
    "direct_reply": {
        "description": (
            "Delivers a final conversational answer to the user and ENDS the pipeline "
            "immediately — the Engineer will never run after this. Use it ONLY for casual "
            "chat or answers that need no code. NEVER use it to announce future work."
        ),
        "parameters": {
            "type": "object",
            "properties": {"message": {"type": "string", "description": "The conversational answer."}},
            "required": ["message"],
        },
    },
    "wake_engineer": {
        "description": (
            "Hands off to the Engineer to write code. This is the ONLY way code gets "
            "written. The blueprint must be complete plain-English instructions: formulas, "
            "inputs, outputs, and the save path. Do not include code in the blueprint."
        ),
        "parameters": {
            "type": "object",
            "properties": {"blueprint": {"type": "string", "description": "Complete plain-English instructions for the Engineer."}},
            "required": ["blueprint"],
        },
    },
}

# Which tools each Architect mode may call (mirrors the old per-mode prompt lists).
_MODE_TOOL_NAMES = {
    "Shop": [
        "web_search", "read_website", "search_memory", "save_memory", "send_to_tormach",
        "direct_reply", "get_system_time", "check_device_status", "run_shop_skill",
        "tormach_ssh", "vscode_get_active_file", "github_manager",
        "read_directory_structure", "ingest_codebase_to_rag", "search_codebase",
        "manage_goals", "generate_image", "edit_image", "wake_engineer",
    ],
    "Software": [
        "web_search", "read_website", "search_memory", "save_memory", "send_to_tormach",
        "direct_reply", "get_system_time", "check_device_status", "run_shop_skill",
        "tormach_ssh", "vscode_get_active_file", "github_manager",
        "read_directory_structure", "ingest_codebase_to_rag", "search_codebase",
        "wake_engineer",
    ],
    "CNC": [
        "web_search", "read_website", "search_memory", "save_memory", "send_to_tormach",
        "direct_reply", "get_system_time", "check_device_status", "run_shop_skill",
        "tormach_ssh", "wake_engineer",
    ],
    "Electronics": [
        "web_search", "read_website", "search_memory", "save_memory",
        "direct_reply", "get_system_time", "check_device_status", "run_shop_skill",
        "wake_engineer",
    ],
    "Developer": [
        "web_search", "read_website", "search_memory", "save_memory",
        "check_device_status", "get_system_time", "github_manager",
        "read_directory_structure", "ingest_codebase_to_rag", "search_codebase",
        "wake_engineer",
    ],
    "Whiteboard": [
        "web_search", "read_website", "search_memory", "save_memory",
        "direct_reply", "get_system_time", "generate_image", "edit_image",
    ],
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


def get_architect_tools(mode: str) -> list:
    names = _MODE_TOOL_NAMES.get(mode, _MODE_TOOL_NAMES["Shop"])
    return [_wrap(n) for n in names]


_ENGINEER_SCHEMAS = {
    "request_terminal_execution": {
        "description": (
            "Requests execution of a bash command on the host Mac. Requires explicit "
            "human approval. Use only when the blueprint cannot be satisfied by code alone."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The bash command to run."},
                "explanation": {"type": "string", "description": "One-sentence summary shown to the human approver."},
            },
            "required": ["command", "explanation"],
        },
    },
    "patch_file": {
        "description": (
            "Surgically patches the running server source by exact-string replacement. "
            "Each search_text must match the current source EXACTLY, including whitespace."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "patches": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "search_text": {"type": "string", "description": "Exact existing code to replace."},
                            "replace_text": {"type": "string", "description": "New code to insert in its place."},
                        },
                        "required": ["search_text", "replace_text"],
                    },
                },
            },
            "required": ["patches"],
        },
    },
}


def get_engineer_tools(mode: str) -> list:
    names = ["request_terminal_execution"]
    if mode == "Developer":
        names.append("patch_file")
    return [
        {
            "type": "function",
            "function": {"name": n, "description": _ENGINEER_SCHEMAS[n]["description"], "parameters": _ENGINEER_SCHEMAS[n]["parameters"]},
        }
        for n in names
    ]


QA_TEST_TOOL = {
    "type": "function",
    "function": {
        "name": "run_script_test",
        "description": (
            "Runs the Engineer's draft script in the sandbox with real command-line "
            "arguments and returns its stdout/stderr. For CLI scripts, ALWAYS test at "
            "least one realistic example input (e.g. from the blueprint) before submitting "
            "your verdict — a usage/argparse message alone proves nothing about correctness."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "arguments": {"type": "string", "description": "Command-line arguments to pass to the script, e.g. 'M6x1.0'."},
            },
            "required": ["arguments"],
        },
    },
}

QA_VERDICT_TOOL = {
    "type": "function",
    "function": {
        "name": "submit_verdict",
        "description": "Submits the final QA verdict on the Engineer's code. You MUST call this exactly once per review.",
        "parameters": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["APPROVE", "FAIL", "DEPLOY"],
                    "description": "APPROVE saves the code as a skill/file. FAIL sends it back to the Engineer. DEPLOY (Developer mode only) overwrites production source after human approval.",
                },
                "save_path": {"type": "string", "description": "Where to save the approved code, e.g. skills/tap_drill.py (APPROVE only)."},
                "feedback": {"type": "string", "description": "Detailed list of required fixes (FAIL only)."},
                "target_file": {"type": "string", "description": "Production file to overwrite (DEPLOY only)."},
                "summary": {"type": "string", "description": "One-sentence summary of the change (DEPLOY only)."},
            },
            "required": ["status"],
        },
    },
}
