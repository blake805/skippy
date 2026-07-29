"""Bridge to the Cursor/VS Code extension.

The extension connects to the hub as `client_id=cursor` and answers RPCs. That
gives the agent two things the filesystem cannot: the editor's live diagnostics,
and edits that land in the editor's own undo stack instead of appearing as
mysterious on-disk changes.

Wire protocol. Server sends:

    {"action": "get_diagnostics", "task_id": "<uuid>", "paths": ["..."]}

Extension replies on the same socket, echoing `task_id`:

    {"task_id": "<uuid>", "ok": true, "result": {...}}
    {"task_id": "<uuid>", "ok": false, "error": "..."}

`task_id` is what routes the reply back to the waiting coroutine, so the
extension must always echo it verbatim.
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("skippy_cursor")

CURSOR_CLIENT_ID = "cursor"

# A workspace edit or a task run legitimately takes far longer than a query for
# the open file list, so timeouts are per action rather than one global value.
ACTION_TIMEOUTS: Dict[str, float] = {
    "get_workspace_roots": 10.0,
    "get_open_files": 10.0,
    "get_diagnostics": 30.0,
    "apply_patches": 120.0,
    "create_file": 60.0,
    "run_task": 300.0,
}
DEFAULT_TIMEOUT = 30.0


class CursorBridge:
    def __init__(self, hub, client_id: str = CURSOR_CLIENT_ID):
        self.hub = hub
        self.client_id = client_id

    @property
    def connected(self) -> bool:
        return self.client_id in getattr(self.hub, "active_connections", {})

    async def call(
        self, action: str, payload: Optional[dict] = None, timeout: Optional[float] = None
    ) -> dict:
        if not self.connected:
            return {"ok": False, "error": f"Cursor client '{self.client_id}' is not connected."}

        request = dict(payload or {})
        request["action"] = action
        response = await self.hub.execute_tool_on_client(
            self.client_id,
            request,
            timeout=timeout or ACTION_TIMEOUTS.get(action, DEFAULT_TIMEOUT),
        )

        if not isinstance(response, dict):
            return {"ok": False, "error": f"Malformed reply for '{action}': {response!r}"}
        if response.get("error"):
            return {"ok": False, "error": str(response["error"])}
        if response.get("ok") is False:
            return {"ok": False, "error": str(response.get("error") or "Cursor reported a failure.")}

        result = response.get("result")
        if result is None:
            # Tolerate an extension that answers with a flat payload.
            result = {
                key: value for key, value in response.items() if key not in ("task_id", "ok", "action")
            }
        return {"ok": True, "result": result}

    # -- convenience wrappers --------------------------------------------

    async def workspace_roots(self) -> List[str]:
        response = await self.call("get_workspace_roots")
        if not response["ok"]:
            logger.info("Cursor workspace roots unavailable: %s", response["error"])
            return []
        roots = response["result"].get("roots") or []
        resolved = []
        for root in roots:
            path = root.get("path") if isinstance(root, dict) else root
            if isinstance(path, str) and path:
                resolved.append(path)
        return resolved

    async def open_files(self) -> dict:
        return await self.call("get_open_files")

    async def diagnostics(self, paths: Optional[List[str]] = None) -> dict:
        return await self.call("get_diagnostics", {"paths": list(paths or [])})

    async def apply_patches(self, edits: List[dict]) -> dict:
        return await self.call("apply_patches", {"edits": edits})

    async def create_file(self, path: str, content: str) -> dict:
        return await self.call("create_file", {"path": path, "content": content})

    async def run_task(self, command: str, cwd: Optional[str] = None) -> dict:
        return await self.call("run_task", {"command": command, "cwd": cwd})


def format_diagnostics(result: Any, limit: int = 100) -> str:
    """Render the extension's diagnostics payload into something a model can read."""
    entries = result.get("diagnostics") if isinstance(result, dict) else result
    if not entries:
        return ""
    lines = []
    for entry in list(entries)[:limit]:
        if not isinstance(entry, dict):
            continue
        severity = str(entry.get("severity", "info")).lower()
        location = f"{entry.get('path', '?')}:{entry.get('line', '?')}"
        column = entry.get("col")
        if column not in (None, ""):
            location += f":{column}"
        source = entry.get("source")
        suffix = f"  [{source}]" if source else ""
        lines.append(f"{severity}: {location} {entry.get('message', '')}{suffix}")
    if len(entries) > limit:
        lines.append(f"... [{len(entries) - limit} more]")
    return "\n".join(lines)
