"""Agent identity: who each session is, who its parent is, what it is doing.

Identity is assigned by ``bridge launch`` — never by the client — and stored in
``~/.bridge/agents.db``. Sub-agent status is *derived* from parent identity
(a parent with a live child is ``delegating``) because a client's native
sub-agents (e.g. Claude Code's Task tool) are not separately instrumented:
their work shows up under the parent's identity, as documented in the README.
"""

from __future__ import annotations

import sqlite3
from dataclasses import asdict, dataclass, field
from typing import Any

from . import paths
from .util import new_agent_id, new_session_id, short_hash, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    agent_id        TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL,
    workspace_id    TEXT NOT NULL,
    tree_id         TEXT NOT NULL,
    root_dir        TEXT,
    start_dir       TEXT NOT NULL,
    parent_agent_id TEXT,
    task_id         TEXT,
    role            TEXT NOT NULL,
    model           TEXT,
    app             TEXT NOT NULL,
    client          TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    status          TEXT NOT NULL,           -- active | ended | error
    status_text     TEXT DEFAULT '',
    created_utc     TEXT NOT NULL,
    last_seen_utc   TEXT NOT NULL,
    ended_utc       TEXT
);
CREATE TABLE IF NOT EXISTS agent_events (
    seq        INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id   TEXT NOT NULL,
    kind       TEXT NOT NULL,               -- start | status | end | child
    detail     TEXT,
    ts         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_parent ON sessions(parent_agent_id);
"""


@dataclass
class Identity:
    agent_id: str
    session_id: str
    workspace_id: str
    tree_id: str
    root_dir: str | None
    start_dir: str
    parent_agent_id: str | None
    task_id: str | None
    role: str
    model: str
    app: str
    client: str
    profile_version: str
    status: str = "active"
    status_text: str = ""
    created_utc: str = field(default_factory=utcnow)
    last_seen_utc: str = field(default_factory=utcnow)
    ended_utc: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_subagent(self) -> bool:
        """Subagent status is derived solely from the presence of a parent ID."""
        return self.parent_agent_id is not None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Identity":
        return cls(**{k: row[k] for k in row.keys()})

    @property
    def telemetry(self) -> dict[str, Any]:
        """The identity block embedded in every event."""
        return {
            "workspace_id": self.workspace_id,
            "tree_id": self.tree_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "parent_agent_id": self.parent_agent_id,
            "task_id": self.task_id,
            "role": self.role,
            "model": self.model,
            "app": self.app,
        }


def connect() -> sqlite3.Connection:
    conn = paths.connect_sqlite(paths.agents_db())
    conn.executescript(SCHEMA)
    return conn


def new_task_id(parent_task: str | None = None) -> str:
    if parent_task:
        return parent_task  # a child works its parent's task by default
    return "task-" + short_hash(new_session_id(), n=8)


def record(identity: Identity) -> None:
    conn = connect()
    try:
        with conn:
            conn.execute(
                """INSERT OR REPLACE INTO sessions
                   (agent_id, session_id, workspace_id, tree_id, root_dir, start_dir,
                    parent_agent_id, task_id, role, model, app, client, profile_version,
                    status, status_text, created_utc, last_seen_utc, ended_utc)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    identity.agent_id, identity.session_id, identity.workspace_id,
                    identity.tree_id, identity.root_dir, identity.start_dir,
                    identity.parent_agent_id, identity.task_id, identity.role,
                    identity.model, identity.app, identity.client,
                    identity.profile_version, identity.status, identity.status_text,
                    identity.created_utc, identity.last_seen_utc, identity.ended_utc,
                ),
            )
            if identity.parent_agent_id:
                conn.execute(
                    "INSERT INTO agent_events (agent_id, kind, detail, ts) VALUES (?,?,?,?)",
                    (identity.agent_id, "child",
                     f"parent={identity.parent_agent_id}", utcnow()),
                )
            conn.execute(
                "INSERT INTO agent_events (agent_id, kind, detail, ts) VALUES (?,?,?,?)",
                (identity.agent_id, "start", identity.profile_version, utcnow()),
            )
    finally:
        conn.close()


def get(agent_id: str) -> Identity | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM sessions WHERE agent_id=?", (agent_id,)).fetchone()
        return Identity.from_row(row) if row else None
    finally:
        conn.close()


def list_sessions() -> list[Identity]:
    conn = connect()
    try:
        rows = conn.execute("SELECT * FROM sessions ORDER BY created_utc DESC").fetchall()
        return [Identity.from_row(r) for r in rows]
    finally:
        conn.close()


def children(agent_id: str) -> list[Identity]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM sessions WHERE parent_agent_id=? ORDER BY created_utc", (agent_id,)
        ).fetchall()
        return [Identity.from_row(r) for r in rows]
    finally:
        conn.close()


def set_status(agent_id: str, status: str | None = None, text: str | None = None) -> None:
    conn = connect()
    try:
        with conn:
            if status is not None:
                conn.execute(
                    "UPDATE sessions SET status=?, last_seen_utc=?, ended_utc=? WHERE agent_id=?",
                    (status, utcnow(), utcnow() if status in ("ended", "error") else None, agent_id),
                )
            if text is not None:
                conn.execute(
                    "UPDATE sessions SET status_text=?, last_seen_utc=? WHERE agent_id=?",
                    (text, utcnow(), agent_id),
                )
            conn.execute(
                "INSERT INTO agent_events (agent_id, kind, detail, ts) VALUES (?,?,?,?)",
                (agent_id, "status" if status is None else ("end" if status in ("ended", "error") else "start"),
                 text or status or "", utcnow()),
            )
    finally:
        conn.close()


def effective_status(agent: Identity) -> str:
    """Status as derived from parent/child relationships.

    An ``active`` parent with at least one live child is ``delegating`` —
    the honest view, since the children are the ones working.
    """
    if agent.status != "active":
        return agent.status
    if any(c.status == "active" for c in children(agent.agent_id)):
        return "delegating"
    return "active"


def has_live_child(agent_id: str) -> bool:
    return any(c.status == "active" for c in children(agent_id))
