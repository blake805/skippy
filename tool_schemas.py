# tool_schemas.py
#
# OpenAI-format function schemas for native tool calling via mlx_lm.server.
# These replace the old "output ONLY this exact JSON" text protocol: the
# schemas are sent in the `tools` field of the chat completion request and
# the model responds with structured `tool_calls`.
#
# The workspace tools (read, search, patch) live here alongside the research and
# context half of the toolset. Terminal execution arrives with the agent runtime.

import skippy_device
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
                        "not rediscovered from scratch next session. "
                        "weakness: something that should be fixed in our own code, which "
                        "also needs 'severity' and becomes a work item a later coding "
                        "session will see."
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
                        "Where you saw this, in whatever form it takes: the command you ran "
                        "and the part of its output that shows it, an offset, a symbol name, "
                        "or a file and byte range in an artifact you read — a flash dump or a "
                        "decoded capture is evidence too. Required for everything except a "
                        "'question'. 'The header is 32 bytes' is worthless in six months; "
                        "'otool -h reports sizeofcmds 0x20' can be rechecked."
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
                "severity": {
                    "type": "string",
                    "enum": list(skippy_re.SEVERITY),
                    "description": (
                        "Required for kind 'weakness'. How urgently this should be fixed in "
                        "our own code — not a CVSS score. This is a different judgment from "
                        "'confidence': severity is how much it matters if it is real, "
                        "confidence is how sure you are that it is. A speculative critical "
                        "needs confirming before it needs fixing, and both numbers travel "
                        "with the work item so whoever picks it up can tell which."
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
    "resolve_work_item": {
        "description": (
            "Closes out a weakness that an earlier reverse-engineering session found in "
            "one of our products. Call it when your change actually addresses the item "
            "your opening message listed — not when you have merely looked at it. Until "
            "you do, it keeps arriving at the top of every future session on these "
            "repos. If you conclude the weakness does not apply, resolve it anyway and "
            "say why: 'this does not apply because...' is a useful record, and leaving "
            "it open means someone re-investigates it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "item_id": {
                    "type": "string",
                    "description": "The work item id from your opening message, e.g. '3'.",
                },
                "how": {
                    "type": "string",
                    "description": (
                        "What you changed that addresses it, or why it does not apply. A "
                        "later session sees the item closed and has no other way to tell "
                        "whether it was fixed, mitigated elsewhere, or ruled out."
                    ),
                },
            },
            "required": ["item_id", "how"],
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
    "git_status": {
        "description": (
            "Current branch, ahead/behind counts, and every changed file in a "
            "workspace repository. Read-only. Call this before committing so you "
            "know exactly what would be included."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": (
                        "Which repository, by workspace-root name or any path inside "
                        "it. Required when there is more than one repo."
                    ),
                },
            },
            "required": [],
        },
    },
    "git_diff": {
        "description": (
            "Unified diff of uncommitted changes in a workspace repository. "
            "Read-only. Untracked files are named but not diffed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Which repository. Required with several repos."},
                "path": {"type": "string", "description": "Limit the diff to one file or directory."},
                "staged": {"type": "boolean", "description": "Show what is staged for commit instead of the working tree."},
            },
            "required": [],
        },
    },
    "git_branch": {
        "description": (
            "Lists branches, creates one, or switches to one. Creating a branch "
            "changes no files. Switching to an existing branch rewrites the working "
            "tree, so it is refused while there are uncommitted changes — commit "
            "first, then switch."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Which repository. Required with several repos."},
                "name": {"type": "string", "description": "Branch to create or switch to. Omit it to list branches."},
                "create": {"type": "boolean", "description": "Create `name` at HEAD and switch to it."},
            },
            "required": [],
        },
    },
    "git_commit": {
        "description": (
            "Stages changes and commits them. The staged diff goes to the human for "
            "approval first, exactly like a code edit — a deny leaves the tree as it "
            "was. Commit when a coherent piece of work is done and verified, with a "
            "message that says why, not just what."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {
                    "type": "string",
                    "description": "The commit message. One line of why; wrap details after a blank line if needed.",
                },
                "repo": {"type": "string", "description": "Which repository. Required with several repos."},
                "paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Files to include. Omit to commit every change in the repo, "
                        "including new files."
                    ),
                },
            },
            "required": ["message"],
        },
    },
    "list_devices": {
        "description": (
            "Lists serial ports and USB devices attached to a host. Call this before "
            "opening anything — handles come from what this returns. host='studio' is "
            "the machine running Skippy; host='macbook' (or any other) reaches a "
            "connected device-bridge client on that machine."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "host": {
                    "type": "string",
                    "description": "Where the hardware is plugged in. Defaults to 'studio'.",
                },
                "kinds": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["serial", "usb"]},
                    "description": "Subset to list. Defaults to both.",
                },
            },
            "required": [],
        },
    },
    "serial_open": {
        "description": (
            "Opens a serial port returned by list_devices and returns a handle for "
            "serial_io / serial_close. Does not write anything to the device."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "port": {"type": "string", "description": "Device path, e.g. /dev/cu.usbserial-10."},
                "baud": {"type": "integer", "description": "Baud rate. Defaults to 115200."},
                "host": {"type": "string", "description": "Host that has the port. Defaults to 'studio'."},
                "timeout": {"type": "number", "description": "Per-read timeout in seconds."},
                "bytesize": {"type": "integer", "description": "Data bits, usually 8."},
                "parity": {"type": "string", "description": "N, E, O, M, or S. Defaults to N."},
                "stopbits": {"type": "number", "description": "1, 1.5, or 2. Defaults to 1."},
            },
            "required": ["port"],
        },
    },
    "serial_io": {
        "description": (
            "Bounded write and/or read on an open serial handle. A non-empty write "
            "requires human approval before any bytes leave; pure reads do not. "
            "Payloads are hex by default (use write_encoding/encoding for base64 or utf8). "
            "Use read_bytes, read_until_idle, or capture_seconds — not all three."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "handle": {"type": "string", "description": "Handle from serial_open."},
                "write": {"type": "string", "description": "Bytes to send, encoded per write_encoding."},
                "write_encoding": {
                    "type": "string",
                    "enum": ["hex", "base64", "utf8"],
                    "description": "How to decode `write`. Defaults to hex.",
                },
                "read_bytes": {"type": "integer", "description": "Read exactly this many bytes (capped)."},
                "read_until_idle": {
                    "type": "number",
                    "description": "Keep reading until this many seconds of silence after the last byte.",
                },
                "capture_seconds": {
                    "type": "number",
                    "description": "Capture everything heard for this many seconds (max 30).",
                },
                "encoding": {
                    "type": "string",
                    "enum": ["hex", "base64", "utf8"],
                    "description": "How to return the response body. Defaults to hex.",
                },
            },
            "required": ["handle"],
        },
    },
    "serial_close": {
        "description": "Closes a serial handle from serial_open.",
        "parameters": {
            "type": "object",
            "properties": {
                "handle": {"type": "string", "description": "Handle from serial_open."},
            },
            "required": ["handle"],
        },
    },
    "usb_transfer": {
        "description": (
            "Bulk or interrupt USB transfer to a device identified by vid/pid. "
            "direction='out' requires human approval; direction='in' does not. "
            "Data is hex by default."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "vid": {"type": "string", "description": "Vendor ID, e.g. '0x1234' or '1234'."},
                "pid": {"type": "string", "description": "Product ID."},
                "direction": {
                    "type": "string",
                    "enum": ["in", "out"],
                    "description": "Transfer direction. out requires human approval.",
                },
                "endpoint": {"type": "integer", "description": "Endpoint address, e.g. 0x81."},
                "data": {"type": "string", "description": "OUT payload, encoded per data_encoding."},
                "data_encoding": {
                    "type": "string",
                    "enum": ["hex", "base64", "utf8"],
                    "description": "How to decode `data`. Defaults to hex.",
                },
                "length": {"type": "integer", "description": "IN length to request."},
                "host": {
                    "type": "string",
                    "description": "Where the USB device is attached. Defaults to 'studio'.",
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": "Transfer timeout in milliseconds.",
                },
            },
            "required": ["vid", "pid", "direction"],
        },
    },
    "usb_control": {
        "description": (
            "USB control transfer. A host-to-device data stage requires human approval; "
            "device-to-host reads do not."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "vid": {"type": "string", "description": "Vendor ID."},
                "pid": {"type": "string", "description": "Product ID."},
                "request_type": {"type": "integer", "description": "bmRequestType."},
                "request": {"type": "integer", "description": "bRequest."},
                "value": {"type": "integer", "description": "wValue."},
                "index": {"type": "integer", "description": "wIndex."},
                "data": {"type": "string", "description": "Optional data-stage payload."},
                "data_encoding": {
                    "type": "string",
                    "enum": ["hex", "base64", "utf8"],
                    "description": "How to decode `data`. Defaults to hex.",
                },
                "length": {"type": "integer", "description": "IN data-stage length."},
                "host": {
                    "type": "string",
                    "description": "Where the USB device is attached. Defaults to 'studio'.",
                },
                "timeout_ms": {
                    "type": "integer",
                    "description": "Transfer timeout in milliseconds.",
                },
            },
            "required": ["vid", "pid", "request_type", "request"],
        },
    },
    "net_connect": {
        "description": (
            "Opens a TCP or UDP session to an address and returns a handle for net_io. "
            "Connecting itself is not a write approval; sending bytes later is."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "address": {"type": "string", "description": "Hostname or IP."},
                "port": {"type": "integer", "description": "TCP/UDP port number."},
                "proto": {
                    "type": "string",
                    "enum": ["tcp", "udp"],
                    "description": "Defaults to tcp.",
                },
                "host": {
                    "type": "string",
                    "description": "Which machine initiates the connection. Defaults to 'studio'.",
                },
                "timeout": {
                    "type": "number",
                    "description": "Connect timeout in seconds.",
                },
            },
            "required": ["address", "port"],
        },
    },
    "net_io": {
        "description": (
            "Send and/or receive on a net_connect handle. A non-empty write requires "
            "human approval; pure reads do not. Payloads default to hex."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "handle": {"type": "string", "description": "Handle from net_connect."},
                "write": {
                    "type": "string",
                    "description": "Bytes to send, encoded per write_encoding.",
                },
                "write_encoding": {
                    "type": "string",
                    "enum": ["hex", "base64", "utf8"],
                    "description": "How to decode `write`. Defaults to hex.",
                },
                "read_bytes": {
                    "type": "integer",
                    "description": "How many bytes to try to receive (capped).",
                },
                "encoding": {
                    "type": "string",
                    "enum": ["hex", "base64", "utf8"],
                    "description": "How to return the response body. Defaults to hex.",
                },
            },
            "required": ["handle"],
        },
    },
    "net_scan": {
        "description": (
            "TCP connect-scan a short list of ports on an address (max 256). "
            "Read-only — no approval needed. ports is a comma list like '22,80,8000-8010'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "address": {"type": "string", "description": "Hostname or IP to scan."},
                "ports": {
                    "type": "string",
                    "description": "Comma-separated ports and ranges, e.g. '22,80,8000-8010'.",
                },
                "host": {
                    "type": "string",
                    "description": "Which machine runs the scan. Defaults to 'studio'.",
                },
                "timeout": {
                    "type": "number",
                    "description": "Per-port timeout in seconds.",
                },
            },
            "required": ["address"],
        },
    },
}


# A raw flash dump has no header saying what it is, and an ESP32 image is exactly that.
# Shared between both tools because getting it right on one and not the other would mean
# a function that disassembles and then refuses to decompile.
_ARCH_PARAM = {
    "type": "string",
    "description": (
        "Only for a raw dump with no recognisable container, where nothing declares the "
        "architecture: 'xtensa' for ESP32, or arm, x86, mips, riscv. Leave this out for "
        "an ELF, Mach-O or PE — those say what they are, and a value that disagrees with "
        "the file is refused rather than obeyed."
    ),
}

_BITS_PARAM = {
    "type": "integer",
    "description": "Word size for a raw dump: 8, 16, 32 or 64. Ignored for a real container.",
}

# The other half of the carving chain: having pulled executables out of a firmware
# image, the point is to read them.
_FILE_PARAM = {
    "type": "string",
    "description": (
        "Optional. Read a file extracted from the target instead of the target itself — "
        "a path as list_extracted shows it, relative to the extraction directory. Leave "
        "this out to read the target."
    ),
}

_SCHEMAS["disassemble_function"] = {
    "description": (
        "Disassemble one function of the target artifact, with rizin's analysis of its "
        "arguments, locals and control flow. Covers x86-64, ARM, AArch64, MIPS, RISC-V "
        "and Xtensa (ESP32). Prefer this over `objdump` through run_command: it returns "
        "the one function you asked about instead of a region, and it handles "
        "architectures the system objdump cannot read at all."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": (
                    "The function to read: a symbol name as it appears in the binary, or "
                    "a hex address like 0x100003f40. Leading underscores and `sym.` "
                    "prefixes are handled for you. Use list_symbols if you do not know "
                    "the name; for a stripped target, find an address with run_command "
                    "first and pass that."
                ),
            },
            "arch": _ARCH_PARAM,
            "bits": _BITS_PARAM,
            "file": _FILE_PARAM,
        },
        "required": ["symbol"],
    },
}

_SCHEMAS["decompile"] = {
    "description": (
        "Decompile one function of the target artifact to C, using the Ghidra "
        "decompiler. Much faster to read than disassembly for working out what a "
        "routine does. The output is a reconstruction rather than the original source, "
        "so a finding based on it alone is 'likely' rather than 'confirmed'. On Xtensa "
        "and RISC-V the parameter list specifically is unreliable and the tool says so."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "symbol": {
                "type": "string",
                "description": (
                    "The function to decompile: a symbol name as it appears in the "
                    "binary, or a hex address. Same resolution as disassemble_function."
                ),
            },
            "arch": _ARCH_PARAM,
            "bits": _BITS_PARAM,
            "file": _FILE_PARAM,
        },
        "required": ["symbol"],
    },
}

_SCHEMAS["list_symbols"] = {
    "description": (
        "The named functions in the target artifact, with their addresses. Use this to "
        "find something worth reading before disassembling, and to avoid guessing at "
        "symbol names. Returns nothing useful on a stripped binary, which is itself "
        "worth knowing."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "contains": {
                "type": "string",
                "description": (
                    "Optional filter: only symbols whose name contains this, "
                    "case-insensitively. 'verify', 'crypt', 'update'."
                ),
            },
            "file": _FILE_PARAM,
        },
        "required": [],
    },
}


_SCHEMAS["extract_artifact"] = {
    "description": (
        "Carve a firmware image or container into its parts, using unblob. Handles 30+ "
        "archive, compression and filesystem formats, recursively, and reports what it "
        "found. Use this when `file`, entropy or a magic number suggests the target is a "
        "container rather than plain code — then read the executables it produces by "
        "passing their paths to list_symbols, disassemble_function and decompile. "
        "Extraction runs inside a container with no network and read-only input, so it "
        "cannot modify the artifact or reach anything else."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Optional. Extract a file that a previous extraction produced, for "
                    "an image nested deeper than the recursion reached. Leave out to "
                    "extract the target itself."
                ),
            },
            "depth": {
                "type": "integer",
                "description": (
                    "How many levels of nested container to unpack. Defaults to 4, which "
                    "handles ordinary firmware; raise it if the parts you want are "
                    "deeper, at the cost of time."
                ),
            },
        },
        "required": [],
    },
}

_SCHEMAS["list_extracted"] = {
    "description": (
        "List what extraction has produced in this pack, including from earlier sessions "
        "on the same target. Use it to find something worth reading after carving, and "
        "to get the paths that the disassembly tools take."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Optional. List one subdirectory instead of everything, for a large "
                    "extracted filesystem."
                ),
            },
        },
        "required": [],
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
# Version control, coding mode only: an RE session does not change the artifact,
# so it has no history to write. Commits gate through the code-approval card.
GIT_TOOLS = ("git_status", "git_diff", "git_branch", "git_commit")
NOTES_TOOLS = ("note_finding", "read_notes")
# RE mode only, and not because coding mode would be harmed by them: they read the
# session's target artifact, and a coding session has a repository rather than a target.
# See ADR 0018 for why these are structured tools instead of an allowlisted `rizin`.
DISASSEMBLY_TOOLS = ("list_symbols", "disassemble_function", "decompile")
# RE mode only, same reason, and one more: extraction writes into the pack's quarantine,
# which only an RE session has. See ADR 0019 for why this runs in a container.
EXTRACTION_TOOLS = ("extract_artifact", "list_extracted")
# Offered in both modes: continuing prior work is not specific to either.
MEMORY_TOOLS = ("record_decision", "recall_project")
# Coding mode only. A weakness is raised in RE mode and discharged by changing code,
# which RE mode cannot do — offering this there would let a session close an item it
# has no way to have fixed.
WORK_ITEM_TOOLS = ("resolve_work_item",)
# Live hardware — RE only. See skippy_device / ADR 0015.
DEVICE_TOOLS = skippy_device.DEVICE_TOOLS


def filesystem_tools() -> list:
    """The read-only workspace tools, wrapped for the `tools` request field."""
    return [_wrap(name) for name in FILESYSTEM_TOOLS]


def workspace_tools() -> list:
    """Read, write and verify. What an agent needs to actually finish a coding task."""
    return [
        _wrap(name)
        for name in FILESYSTEM_TOOLS + WRITE_TOOLS + EXEC_TOOLS + GIT_TOOLS
        + MEMORY_TOOLS + WORK_ITEM_TOOLS
    ]


def re_tools() -> list:
    """Read, inspect, probe live devices, and record. No apply_patch: an RE session
    does not change the artifact, and the notes are the deliverable rather than a
    diff. Device writes are separately gated by human approval."""
    return [
        _wrap(name)
        for name in FILESYSTEM_TOOLS + EXEC_TOOLS + NOTES_TOOLS + DISASSEMBLY_TOOLS
        + EXTRACTION_TOOLS + MEMORY_TOOLS + DEVICE_TOOLS
    ]


def research_tools() -> list:
    """Every schema here, wrapped for the `tools` field of a completion request."""
    return [_wrap(name) for name in _SCHEMAS]

