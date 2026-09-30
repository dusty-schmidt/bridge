"""Durable local outbox: where events go when the bus is unreachable.

Small sqlite table, one row per event, primary key = ``event_id`` (so a retry
can never duplicate what is already stored). The reporter writes here only
when publishing fails; the next emit — or ``bridge outbox flush`` — retries.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .. import paths
from ..util import utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS outbox (
    event_id    TEXT PRIMARY KEY,
    subject     TEXT NOT NULL,
    payload     TEXT NOT NULL,
    created_utc TEXT NOT NULL,
    attempts    INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT
);
"""


def connect() -> sqlite3.Connection:
    conn = paths.connect_sqlite(paths.outbox_db())
    conn.executescript(SCHEMA)
    return conn


def add(event_id: str, subject: str, payload: str, error: str | None = None) -> None:
    conn = connect()
    try:
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO outbox (event_id, subject, payload, created_utc, last_error)"
                " VALUES (?,?,?,?,?)",
                (event_id, subject, payload, utcnow(), (error or "")[:300]),
            )
    finally:
        conn.close()


def pending(limit: int = 500) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT event_id, subject, payload, attempts FROM outbox"
            " ORDER BY created_utc LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count() -> int:
    conn = connect()
    try:
        return conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    finally:
        conn.close()


def mark_failed(event_id: str, error: str) -> None:
    conn = connect()
    try:
        with conn:
            conn.execute(
                "UPDATE outbox SET attempts=attempts+1, last_error=? WHERE event_id=?",
                ((error or "")[:300], event_id),
            )
    finally:
        conn.close()


def remove(event_ids: list[str]) -> None:
    if not event_ids:
        return
    conn = connect()
    try:
        with conn:
            conn.executemany("DELETE FROM outbox WHERE event_id=?", [(i,) for i in event_ids])
    finally:
        conn.close()


def decode(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(row["payload"])
