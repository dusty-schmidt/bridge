"""The MCP memory server: ``remember`` / ``recall`` / ``forget`` over stdio.

Three tools, deliberately few. Review actions (approve / flag / quarantine) are
**not** exposed here — agents do not grade their own memories; that is the
operator's job through ``bridge memory review``.

Identity and scope come from the trusted session file the launcher wrote
(``BRIDGE_SESSION``); tool arguments cannot change who is asking. The server
refuses to serve without it.

Run:  ``python -m bridge.memory.mcp_server``  (normally spawned by the
launcher through the generated ``mcp.json``).
"""

from __future__ import annotations

import os
import sys
from typing import Any

from .. import paths
from .session import SessionError, SessionMemory

try:
    from mcp.server import MCPServer
except ImportError:  # pragma: no cover - dependency always installed, but fail loudly
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[assignment]

server = MCPServer(
    name="bridge-memory",
    instructions=(
        "Bridge workspace memory. Remember creates a candidate memory visible to "
        "this workspace only; recall searches what this workspace may see "
        "(candidates/flagged = this workspace, approved = the whole root tree); "
        "forget deletes. Quarantined memories never appear in recall. Content is "
        "scoped by bridge launch, not by you."
    ),
)

_MEM: SessionMemory | None = None


def _memory() -> SessionMemory:
    global _MEM
    if _MEM is None:
        _MEM = SessionMemory.from_session_file(os.environ.get(paths.ENV_SESSION))
    return _MEM


def _fail(e: Exception) -> dict[str, Any]:
    return {"ok": False, "error": type(e).__name__, "message": str(e)}


@server.tool()
def remember(content: str, tags: list[str] | None = None,
             source_ref: str | None = None) -> dict[str, Any]:
    """Store a memory as a candidate in this workspace's scope.

    Candidates stay private to the originating workspace until an operator
    approves them, which promotes them to the whole root tree.
    """
    try:
        rec = _memory().remember(content, tags=tags, source_ref=source_ref)
    except Exception as e:  # noqa: BLE001 — surfaced as structured tool output
        return _fail(e)
    return {
        "ok": True,
        "id": rec.id,
        "state": rec.state,
        "visible_to": "this workspace until approved",
        "created_utc": rec.created_utc,
        "task_id": rec.task_id,
        "source_ref": rec.source_ref,
    }


@server.tool()
def recall(query: str, limit: int | None = None) -> dict[str, Any]:
    """Search memory visible to this session (never quarantined entries)."""
    try:
        hits = _memory().recall(query, limit=limit)
    except Exception as e:  # noqa: BLE001
        return _fail(e)
    return {
        "ok": True,
        "hits": [h.to_dict() for h in hits],
        "note": "candidates/flagged are workspace-local; approved are tree-wide",
    }


@server.tool()
def forget(memory_id: str) -> dict[str, Any]:
    """Delete a memory. Only memories of this workspace can be forgotten."""
    try:
        ok = _memory().forget(memory_id)
    except Exception as e:  # noqa: BLE001
        return _fail(e)
    return {"ok": True, "deleted": ok, "id": memory_id}


def main() -> int:
    try:
        _memory()  # fail fast with a clear message instead of at first tool call
    except SessionError as e:
        print(f"bridge-memory: {e}", file=sys.stderr)
        return 2
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
