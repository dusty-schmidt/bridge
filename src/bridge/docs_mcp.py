"""The MCP docs server: one ``document`` tool over stdio.

Run: ``python -m bridge.docs_mcp`` (normally spawned by the launcher through
the generated ``mcp.json``, only when the profile declares ``docs.routes``).
"""

from __future__ import annotations

import os
import sys
from typing import Any

from . import paths
from .docs import DocsError, SessionDocs

try:
    from mcp.server import MCPServer
except ImportError:  # pragma: no cover - dependency always installed, but fail loudly
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[assignment]

server = MCPServer(
    name="bridge-docs",
    instructions=(
        "Write a message to this workspace's documentation. `kind` selects the "
        "destination from this workspace's configured routes (a file or a "
        "command) — you cannot pick an arbitrary path."
    ),
)

_DOCS: SessionDocs | None = None


def _docs() -> SessionDocs:
    global _DOCS
    if _DOCS is None:
        _DOCS = SessionDocs.from_session_file(os.environ.get(paths.ENV_SESSION))
    return _DOCS


@server.tool()
def document(kind: str, message: str) -> dict[str, Any]:
    """Write ``message`` to the destination configured for ``kind``."""
    try:
        return _docs().document(kind, message)
    except DocsError as e:
        return {"ok": False, "error": "DocsError", "message": str(e)}


def main() -> int:
    try:
        _docs()  # fail fast with a clear message instead of at first tool call
    except DocsError as e:
        print(f"bridge-docs: {e}", file=sys.stderr)
        return 2
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
