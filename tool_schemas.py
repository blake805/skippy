# tool_schemas.py
#
# OpenAI-format function schemas for native tool calling via mlx_lm.server.
# These replace the old "output ONLY this exact JSON" text protocol: the
# schemas are sent in the `tools` field of the chat completion request and
# the model responds with structured `tool_calls`.
#
# The workspace tools (read, search, patch) live here alongside the research and
# context half of the toolset. Terminal execution arrives with the agent runtime.

import skippy_re

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
    "run_command": {
        "description": (
            "Runs a test runner, linter, type checker or build tool in the workspace and "
            "returns its output. Use this to check that a change actually works — it is the "
            "only way to find out, since reading your own edit back only confirms what you "
            "wrote. Commands run directly, not through a shell, so pipes and && do not "
            "work; run one program per call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": (
                        "The command, e.g. 'python -m pytest -q' or 'ruff check .'. Only test "
                        "runners, linters, builds and read-only git are permitted."
                    ),
                },
                "cwd": {
                    "type": "string",
                    "description": (
                        "Directory to run in, relative to the workspace root. Required when "
                        "there is more than one workspace root."
                    ),
                },
                "timeout": {
                    "type": "number",
                    "description": "Seconds before the command is killed. Defaults to 300.",
                },
            },
            "required": ["command"],
        },
    },
    "record_decision": {
        "description": (
            "Records a choice you made and the reasoning behind it, for whoever works on "
            "this project next — most likely you, in a session that remembers none of "
            "this. Worth calling when you picked one approach over another, discovered a "
            "constraint that is not obvious from reading the code, or found that an "
            "approach does not work. A dead end is the most valuable thing to record, "
            "because nothing in the repository shows what was already ruled out. Do not "
            "use it to restate what your diff already says."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "One line: what was decided. 'Retries belong in the transport, not per-call'.",
                },
                "body": {
                    "type": "string",
                    "description": (
                        "The reasoning: what you chose, what you rejected, and why. This is "
                        "the part that stops the decision being quietly undone later."
                    ),
                },
                "affects": {
                    "type": "string",
                    "description": (
                        "Comma-separated paths this decision is about. Used to warn a later "
                        "session that the decision may be stale if those files are gone."
                    ),
                },
                "supersedes": {
                    "type": "string",
                    "description": (
                        "Id(s) of decisions this replaces, comma-separated. The old one is "
                        "kept and marked rather than deleted."
                    ),
                },
            },
            "required": ["title", "body"],
        },
    },
    "recall_project": {
        "description": (
            "Searches earlier sessions and decisions for this project. The highlights are "
            "already in your opening message, so use this when you need something older or "
            "more specific than that — whether an approach has been tried before, or why "
            "some part of the code is the way it is. Call with no query for an overview."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What you are looking for. Specific words work; a whole sentence of "
                        "common words does not."
                    ),
                },
            },
            "required": [],
        },
    },
    "note_finding": {
        "description": (
            "Records one thing you have established about the target. In a "
            "reverse-engineering session nothing is written to the target, so these notes "
            "are the entire product of the work — anything not recorded here is lost when "
            "the session ends. Write a finding as soon as you establish it rather than "
            "saving them all for the end. Every finding needs evidence: where you saw it, "
            "so it can be rechecked later. If a later finding contradicts an earlier one, "
            "record the new one with 'supersedes' rather than pretending the first never "
            "happened."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": sorted(skippy_re.KINDS),
                    "description": (
                        "structure: layout, headers, offsets, field meanings. "
                        "behavior: what a routine or component does. "
                        "constant: magic numbers, keys, tables. "
                        "symbol: names, mangling, imports, exports. "
                        "hypothesis: a theory you have a reason for but have not confirmed. "
                        "question: something you do not understand yet, recorded so it is "
                        "not rediscovered from scratch next session."
                    ),
                },
                "title": {
                    "type": "string",
                    "description": "One line, specific. 'Field at 0x10 is a CRC32', not 'header info'.",
                },
                "body": {
                    "type": "string",
                    "description": (
                        "What you found, in enough detail to be useful to someone who has "
                        "not seen the target. For a hypothesis, say what would confirm or "
                        "refute it."
                    ),
                },
                "evidence": {
                    "type": "string",
                    "description": (
                        "Where you saw this: an offset, a symbol name, the command you ran "
                        "and the part of its output that shows it. Required for everything "
                        "except a 'question'. 'The header is 32 bytes' is worthless in six "
                        "months; 'otool -h reports sizeofcmds 0x20' can be rechecked."
                    ),
                },
                "confidence": {
                    "type": "string",
                    "enum": list(skippy_re.CONFIDENCE),
                    "description": (
                        "confirmed: you verified it. likely: strong inference, not verified. "
                        "speculative: a guess. Be honest here — a guess recorded as a fact "
                        "gets cited as one by everything that follows."
                    ),
                },
                "location": {
                    "type": "string",
                    "description": "Where in the target, if it has a place: an offset, address, symbol or file.",
                },
                "supersedes": {
                    "type": "string",
                    "description": (
                        "Id(s) of findings this replaces, comma-separated. The old finding is "
                        "kept and marked, not deleted: being wrong and then right is normal, "
                        "and the correction is itself worth recording."
                    ),
                },
            },
            "required": ["kind", "title", "body", "confidence"],
        },
    },
    "read_notes": {
        "description": (
            "Reads back what you have already established about this target, including "
            "findings from earlier sessions. Call this at the start of an investigation "
            "before re-deriving anything, and again later if you have lost track — the "
            "conversation gets compacted as it grows, but the notes do not."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "finding_id": {
                    "type": "string",
                    "description": "A single finding to read in full, e.g. '3'. Omit for the summary.",
                },
                "kind": {
                    "type": "string",
                    "enum": sorted(skippy_re.KINDS),
                    "description": "Read every finding of one kind. Omit for the summary of all of them.",
                },
            },
            "required": [],
        },
    },
    "apply_patch": {
        "description": (
            "Edits, creates and deletes files. This is the only way to change anything, "
            "and it is all-or-nothing: every edit is validated first, and if any one of "
            "them is bad, NOTHING is written. So put every edit of a coherent change in "
            "one call — a rename touching five files is one call, not five. Several edits "
            "to the same file are applied in order, each seeing the previous result. "
            "Search text must match the file byte-for-byte, including indentation and "
            "blank lines, so read the file first rather than guessing. Pass dry_run to "
            "see the diff without writing."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "edits": {
                    "type": "array",
                    "description": "The edits to apply together, in order.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "File path, relative to the workspace root."},
                            "action": {
                                "type": "string",
                                "enum": ["edit", "create", "delete"],
                                "description": "Defaults to 'edit'.",
                            },
                            "search": {
                                "type": "string",
                                "description": (
                                    "edit only. Exact text to find. Include enough surrounding "
                                    "context to make it unique, or the edit is rejected as ambiguous."
                                ),
                            },
                            "replace": {
                                "type": "string",
                                "description": "edit only. Replacement text. Use \"\" to delete the found text.",
                            },
                            "replace_all": {
                                "type": "boolean",
                                "description": "edit only. Replace every occurrence instead of requiring a unique match.",
                            },
                            "occurrence": {
                                "type": "integer",
                                "description": "edit only. Replace just the Nth occurrence, 1-based.",
                            },
                            "content": {"type": "string", "description": "create only. Full contents of the new file."},
                            "overwrite": {
                                "type": "boolean",
                                "description": "create only. Required to replace a file that already exists.",
                            },
                        },
                        "required": ["path"],
                    },
                },
                "dry_run": {
                    "type": "boolean",
                    "description": "Return the diff without writing anything. Useful for checking a large change first.",
                },
            },
            "required": ["edits"],
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
WRITE_TOOLS = ("apply_patch",)
EXEC_TOOLS = ("run_command",)
NOTES_TOOLS = ("note_finding", "read_notes")
# Offered in both modes: continuing prior work is not specific to either.
MEMORY_TOOLS = ("record_decision", "recall_project")


def filesystem_tools() -> list:
    """The read-only workspace tools, wrapped for the `tools` request field."""
    return [_wrap(name) for name in FILESYSTEM_TOOLS]


def workspace_tools() -> list:
    """Read, write and verify. What an agent needs to actually finish a coding task."""
    return [
        _wrap(name)
        for name in FILESYSTEM_TOOLS + WRITE_TOOLS + EXEC_TOOLS + MEMORY_TOOLS
    ]


def re_tools() -> list:
    """Read, inspect and record. No apply_patch: an RE session does not change the
    artifact, and the notes are the deliverable rather than a diff."""
    return [
        _wrap(name)
        for name in FILESYSTEM_TOOLS + EXEC_TOOLS + NOTES_TOOLS + MEMORY_TOOLS
    ]


def research_tools() -> list:
    """Every schema here, wrapped for the `tools` field of a completion request."""
    return [_wrap(name) for name in _SCHEMAS]

