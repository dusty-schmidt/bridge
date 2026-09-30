"""Where bridge keeps machine-local state.

Everything here lives under one directory (``~/.bridge`` by default,
overridable with ``BRIDGE_STATE_DIR``) so it can be inspected, backed up or
nuked in one place. Per-workspace configuration never lives here — that stays
in the workspace's ``.bridge/`` directory, because bridge is picked up by
directories, not installed into them.
"""

from __future__ import annotations

import os
from pathlib import Path

PROFILE_DIR = ".bridge"
PROFILE_FILE = "profile.yaml"

SESSION_FILE = "session.json"  # inside sessions/<agent_id>/, mode 0600
MCP_FILE = "mcp.json"  # inside sessions/<agent_id>/
INSTRUCTIONS_FILE = "instructions.md"  # inside sessions/<agent_id>/

ENV_STATE_DIR = "BRIDGE_STATE_DIR"
ENV_SESSION = "BRIDGE_SESSION"  # path to the session file (client + MCP env)
ENV_AGENT = "BRIDGE_AGENT_ID"
ENV_NATS_URL = "BRIDGE_NATS_URL"
ENV_TELEMETRY = "BRIDGE_TELEMETRY"  # "off" disables publishing (tests, outages)


def state_dir() -> Path:
    raw = os.environ.get(ENV_STATE_DIR)
    base = Path(raw).expanduser() if raw else Path.home() / ".bridge"
    return base


def sessions_dir() -> Path:
    return state_dir() / "sessions"


def session_dir(agent_id: str) -> Path:
    return sessions_dir() / agent_id


def memory_db() -> Path:
    return state_dir() / "memory.db"


def agents_db() -> Path:
    return state_dir() / "agents.db"


def outbox_db() -> Path:
    return state_dir() / "outbox.db"


def collector_db() -> Path:
    return state_dir() / "collector.db"


def secrets_file() -> Path:
    return state_dir() / "secrets.env"


def ensure_state() -> Path:
    d = state_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    sessions_dir().mkdir(parents=True, exist_ok=True, mode=0o700)
    d.chmod(0o700)
    sessions_dir().chmod(0o700)
    return d


def connect_sqlite(path: Path):
    """Open a sqlite connection with sane defaults for concurrent CLI use."""
    import sqlite3

    state = state_dir().resolve()
    try:
        path.resolve().relative_to(state)
        ensure_state()
    except ValueError:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    conn = sqlite3.connect(str(path), timeout=10)
    Path(path).chmod(0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
