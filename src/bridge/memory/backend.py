"""Memory storage: SQLite backend, scopes, review states.

States and visibility (ordinary recall):

===============  =========================================================
state            who can recall it
===============  =========================================================
``candidate``    the originating workspace only (its tree sees it after
                 approval — candidates stay local until then)
``flagged``      originating workspace only (waiting on review)
``approved``     every workspace in the same **root tree** (promotion is
                 per tree, not machine-wide — that was the design call)
``quarantined``  nobody; excluded from ordinary recall entirely (visible
                 only through review listings)
===============  =========================================================

Every memory carries timestamps, the originating agent, its task, and its
review status. Scope comes from the trusted session file written by
``bridge launch`` — tool callers cannot claim a different identity.
"""

from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .. import paths
from ..util import new_id, utcnow
from .retrieval import Retriever, default_retriever

STATES = ("candidate", "flagged", "approved", "quarantined")
ACTIONS = ("approve", "flag", "quarantine")

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id            TEXT PRIMARY KEY,
    content       TEXT NOT NULL,
    chars         INTEGER NOT NULL,
    workspace_id  TEXT NOT NULL,
    tree_id       TEXT NOT NULL,
    state         TEXT NOT NULL DEFAULT 'candidate',
    origin_agent  TEXT,
    session_id    TEXT,
    task_id       TEXT,
    source_ref    TEXT,
    tags          TEXT DEFAULT '[]',
    created_utc   TEXT NOT NULL,
    reviewed_utc  TEXT,
    reviewed_by   TEXT
);
CREATE INDEX IF NOT EXISTS idx_mem_ws ON memories(workspace_id);
CREATE INDEX IF NOT EXISTS idx_mem_tree ON memories(tree_id);
CREATE INDEX IF NOT EXISTS idx_mem_state ON memories(state);
"""


@dataclass
class Scope:
    """Trusted caller scope, injected from session configuration."""

    workspace_id: str
    tree_id: str
    agent_id: str = ""
    session_id: str = ""
    task_id: str | None = None
    reviewer: str = "cli"
    share_scope: str = "root_tree"


@dataclass
class MemoryRecord:
    id: str
    content: str
    state: str
    workspace_id: str
    tree_id: str
    origin_agent: str
    task_id: str | None
    source_ref: str | None
    tags: list[str]
    created_utc: str
    reviewed_utc: str | None = None
    score: float = 0.0

    @classmethod
    def from_row(cls, row: sqlite3.Row, score: float = 0.0) -> "MemoryRecord":
        return cls(
            id=row["id"], content=row["content"], state=row["state"],
            workspace_id=row["workspace_id"], tree_id=row["tree_id"],
            origin_agent=row["origin_agent"] or "", task_id=row["task_id"],
            source_ref=row["source_ref"] if "source_ref" in row.keys() else None,
            tags=json.loads(row["tags"] or "[]"), created_utc=row["created_utc"],
            reviewed_utc=row["reviewed_utc"], score=score,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "content": self.content, "state": self.state,
            "origin_agent": self.origin_agent, "task_id": self.task_id,
            "source_ref": self.source_ref,
            "tags": self.tags, "created_utc": self.created_utc,
            "reviewed_utc": self.reviewed_utc, "score": self.score,
        }


class MemoryError(RuntimeError):
    pass


class MemoryBackend(ABC):
    """Replaceable storage interface: swap SQLite for anything else."""

    @abstractmethod
    def remember(self, content: str, scope: Scope, *, tags: list[str] | None = None,
                 task_id: str | None = None, source_ref: str | None = None) -> MemoryRecord: ...

    @abstractmethod
    def recall(self, query: str, scope: Scope, *, limit: int = 5) -> list[MemoryRecord]: ...

    @abstractmethod
    def forget(self, memory_id: str, scope: Scope, *, force: bool = False) -> bool: ...

    @abstractmethod
    def review(self, memory_id: str, action: str, scope: Scope, *,
               force: bool = False) -> MemoryRecord: ...

    @abstractmethod
    def get(self, memory_id: str) -> MemoryRecord | None: ...

    @abstractmethod
    def list(self, scope: Scope, *, states: tuple[str, ...] | None = None,
             limit: int = 50, all_trees: bool = False) -> list[MemoryRecord]: ...


def visibility_sql(scope: Scope, all_trees: bool = False) -> tuple[str, list[Any]]:
    """SQL fragment implementing the visibility table above."""
    if all_trees:  # operator/CLI review view
        return "1=1", []
    if scope.share_scope == "workspace":
        shared = "(state = 'approved' AND workspace_id = ?)"
        shared_param = scope.workspace_id
    else:
        shared = "(state = 'approved' AND tree_id = ?)"
        shared_param = scope.tree_id
    return (f"state != 'quarantined' AND (workspace_id = ? OR {shared})",
            [scope.workspace_id, shared_param])


class SqliteMemory(MemoryBackend):
    def __init__(self, db_path=None, retriever: Retriever | None = None) -> None:
        self.path = db_path or paths.memory_db()
        self.retriever = retriever or default_retriever()
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
            if "source_ref" not in columns:
                conn.execute("ALTER TABLE memories ADD COLUMN source_ref TEXT")
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        return paths.connect_sqlite(self.path)

    # -- writes ---------------------------------------------------------------

    def remember(self, content: str, scope: Scope, *, tags: list[str] | None = None,
                 task_id: str | None = None, source_ref: str | None = None) -> MemoryRecord:
        content = (content or "").strip()
        if not content:
            raise MemoryError("empty memory")
        rec = MemoryRecord(
            id=new_id("mem"), content=content, state="candidate",
            workspace_id=scope.workspace_id, tree_id=scope.tree_id,
            origin_agent=scope.agent_id, task_id=task_id if task_id is not None else scope.task_id,
            source_ref=source_ref,
            tags=list(tags or []), created_utc=utcnow(),
        )
        conn = self.connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO memories (id, content, chars, workspace_id, tree_id, state,"
                    " origin_agent, session_id, task_id, source_ref, tags, created_utc)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (rec.id, rec.content, len(content), rec.workspace_id, rec.tree_id,
                     rec.state, rec.origin_agent, scope.session_id, rec.task_id, rec.source_ref,
                     json.dumps(rec.tags), rec.created_utc),
                )
        finally:
            conn.close()
        return rec

    def review(self, memory_id: str, action: str, scope: Scope, *, force: bool = False) -> MemoryRecord:
        if action not in ACTIONS:
            raise MemoryError(f"unknown review action {action!r}; one of {ACTIONS}")
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if row is None:
                raise MemoryError(f"no memory {memory_id}")
            if not force and row["workspace_id"] != scope.workspace_id:
                raise MemoryError("reviewing another workspace requires operator review")
            state = {"approve": "approved", "flag": "flagged", "quarantine": "quarantined"}[action]
            with conn:
                conn.execute(
                    "UPDATE memories SET state=?, reviewed_utc=?, reviewed_by=? WHERE id=?",
                    (state, utcnow(), scope.reviewer, memory_id),
                )
            row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            return MemoryRecord.from_row(row)
        finally:
            conn.close()

    def forget(self, memory_id: str, scope: Scope, *, force: bool = False) -> bool:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            if row is None:
                return False
            if not force and row["workspace_id"] != scope.workspace_id:
                raise MemoryError(
                    "refusing: this memory belongs to another workspace (operator may "
                    "forget it with --force)"
                )
            with conn:
                conn.execute("DELETE FROM memories WHERE id=?", (memory_id,))
            return True
        finally:
            conn.close()

    # -- reads ----------------------------------------------------------------

    def get(self, memory_id: str) -> MemoryRecord | None:
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
            return MemoryRecord.from_row(row) if row else None
        finally:
            conn.close()

    def recall(self, query: str, scope: Scope, *, limit: int = 5) -> list[MemoryRecord]:
        sql, params = visibility_sql(scope)
        conn = self.connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM memories WHERE {sql} ORDER BY created_utc DESC LIMIT 500",
                params,
            ).fetchall()
        finally:
            conn.close()
        as_dicts = [dict(r) for r in rows]
        ranked = self.retriever.rank(as_dicts, query, limit)
        return [MemoryRecord.from_row(_rebuild(r), s) for r, s in ranked]

    def list(self, scope: Scope, *, states: tuple[str, ...] | None = None,
             limit: int = 50, all_trees: bool = False) -> list[MemoryRecord]:
        sql, params = visibility_sql(scope, all_trees=all_trees)
        clauses = [sql]
        if states:
            marks = ",".join("?" * len(states))
            clauses.append(f"state IN ({marks})")
            params = list(params) + list(states)
        conn = self.connect()
        try:
            rows = conn.execute(
                f"SELECT * FROM memories WHERE {' AND '.join(clauses)}"
                " ORDER BY created_utc DESC LIMIT ?",
                [*params, limit],
            ).fetchall()
        finally:
            conn.close()
        return [MemoryRecord.from_row(r) for r in rows]


def _visible_to(row: sqlite3.Row, scope: Scope) -> bool:
    if row["state"] == "quarantined":
        return False
    shared_scope = (row["workspace_id"] == scope.workspace_id if scope.share_scope == "workspace"
                    else row["tree_id"] == scope.tree_id)
    return row["workspace_id"] == scope.workspace_id or (
        row["state"] == "approved" and shared_scope
    )


def _rebuild(d: dict[str, Any]) -> sqlite3.Row:
    """Ranker works on plain dicts; convert back for MemoryRecord.from_row."""
    class _R(dict):
        def keys(self):  # sqlite3.Row interface
            return dict.keys(self)

        def __getitem__(self, k):
            return dict.__getitem__(self, k)

    return _R(d)  # type: ignore[return-value]
