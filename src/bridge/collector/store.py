"""Collector storage: one row per event, deduplicated by ``event_id``.

The stream delivers at-least-once (reconnects replay, outbox retries
republish), so the store must be idempotent: ``INSERT OR IGNORE`` keyed on the
event id gives exactly-once *effect* no matter how often a message arrives.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .. import paths
from ..util import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    event_id         TEXT PRIMARY KEY,
    ts               TEXT NOT NULL,
    type             TEXT NOT NULL,
    subject          TEXT,
    v                INTEGER,
    agent_id         TEXT,
    parent_agent_id  TEXT,
    workspace_id     TEXT,
    tree_id          TEXT,
    session_id       TEXT,
    task_id          TEXT,
    role             TEXT,
    model            TEXT,
    app              TEXT,
    profile_version  TEXT,
    call_id          TEXT,
    tool             TEXT,
    outcome          TEXT,
    duration_ms      INTEGER,
    error_kind       TEXT,
    summary          TEXT,
    received_utc     TEXT NOT NULL,
    payload          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ev_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_ev_tool ON events(tool);
CREATE INDEX IF NOT EXISTS idx_ev_agent ON events(agent_id);
CREATE INDEX IF NOT EXISTS idx_ev_task ON events(task_id);
CREATE INDEX IF NOT EXISTS idx_ev_model ON events(model);
CREATE INDEX IF NOT EXISTS idx_ev_app ON events(app);
"""


def connect(db_path=None) -> sqlite3.Connection:
    conn = paths.connect_sqlite(db_path or paths.collector_db())
    conn.executescript(SCHEMA)
    return conn


def flatten(event: dict[str, Any]) -> dict[str, Any]:
    ident = event.get("identity") or {}
    err = event.get("error") or {}
    return {
        "event_id": event.get("event_id"),
        "ts": event.get("ts") or utcnow(),
        "type": event.get("type") or "",
        "subject": event.get("subject"),
        "v": event.get("v"),
        "agent_id": ident.get("agent_id"),
        "parent_agent_id": ident.get("parent_agent_id"),
        "workspace_id": ident.get("workspace_id"),
        "tree_id": ident.get("tree_id"),
        "session_id": ident.get("session_id"),
        "task_id": ident.get("task_id"),
        "role": ident.get("role"),
        "model": ident.get("model"),
        "app": ident.get("app"),
        "profile_version": event.get("profile_version"),
        "call_id": event.get("call_id"),
        "tool": event.get("tool"),
        "outcome": event.get("outcome"),
        "duration_ms": event.get("duration_ms"),
        "error_kind": err.get("kind") if isinstance(err, dict) else None,
        "summary": event.get("summary"),
        "received_utc": utcnow(),
        "payload": json.dumps(event, default=str),
    }


def store_event(conn: sqlite3.Connection, event: dict[str, Any]) -> bool:
    """Store one event. Returns False when it was already there (duplicate)."""
    if not event.get("event_id"):
        raise ValueError("event without event_id")
    row = flatten(event)
    cols = ", ".join(row)
    marks = ", ".join("?" * len(row))
    cur = conn.execute(
        f"INSERT OR IGNORE INTO events ({cols}) VALUES ({marks})", list(row.values())
    )
    return cur.rowcount == 1


def recent(conn: sqlite3.Connection, limit: int = 20, type_prefix: str | None = None) -> list[dict[str, Any]]:
    if type_prefix:
        rows = conn.execute(
            "SELECT * FROM events WHERE type LIKE ? ORDER BY ts DESC LIMIT ?",
            (f"{type_prefix}%", limit),
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    row = conn.execute(
        "SELECT COUNT(*) total,"
        " SUM(CASE WHEN outcome='error' THEN 1 ELSE 0 END) errors,"
        " SUM(CASE WHEN tool IS NOT NULL AND type = 'tool.complete' THEN 1 ELSE 0 END) calls"
        " FROM events"
    ).fetchone()
    return {"total": row["total"] or 0, "errors": row["errors"] or 0, "calls": row["calls"] or 0}

