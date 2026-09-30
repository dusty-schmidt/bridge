"""Bridge memory: replaceable backend, scoped sessions, MCP server."""

from .backend import MemoryBackend, MemoryRecord, Scope, SqliteMemory
from .session import SessionMemory, scope_from_identity

__all__ = [
    "MemoryBackend", "MemoryRecord", "Scope", "SqliteMemory",
    "SessionMemory", "scope_from_identity",
]
