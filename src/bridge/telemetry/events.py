"""The versioned event schema emitted by every bridge process.

v1 envelope (JSON, one object per message):

    {
      "v": 1,
      "event_id": "...",          # unique per event, dedupe key end to end
      "ts": "…Z",                 # UTC, emission time
      "subject": "bridge.ev.…",   # NATS subject (also derived from type)
      "type": "tool.complete",    # event type
      "call_id": "…",             # correlates tool.start / tool.complete
      "identity": { workspace_id, tree_id, session_id, agent_id,
                    parent_agent_id, task_id, role, model, app },
      "profile_version": "…",
      "tool": "remember",         # when type concerns a tool call
      "outcome": "ok" | "error",
      "duration_ms": 123,
      "error": {"kind": "…", "message": "…"} | null,
      "summary": "short status" | null      # operator-written, never reasoning
    }

No secrets, no tool payloads, no memory contents: only names, outcomes and
durations (see README "Telemetry coverage"). Every string passes through
redaction before it leaves the process.
"""

from __future__ import annotations

from typing import Any

from ..util import new_id, redact_deep, utcnow

EVENT_VERSION = 1

TYPES = (
    "agent.start",
    "agent.end",
    "agent.status",
    "tool.start",
    "tool.complete",
    "memory.remember",
    "memory.recall",
    "memory.forget",
    "memory.review",
)


def subject_for(event_type: str, prefix: str = "bridge") -> str:
    return f"{prefix}.ev.{event_type}"


def make_event(
    event_type: str,
    identity: dict[str, Any],
    profile_version: str,
    *,
    call_id: str | None = None,
    tool: str | None = None,
    outcome: str | None = None,
    duration_ms: int | None = None,
    error: dict[str, Any] | None = None,
    summary: str | None = None,
    data: dict[str, Any] | None = None,
    prefix: str = "bridge",
) -> dict[str, Any]:
    ev: dict[str, Any] = {
        "v": EVENT_VERSION,
        "event_id": new_id("ev"),
        "ts": utcnow(),
        "type": event_type,
        "subject": subject_for(event_type, prefix),
        "identity": identity,
        "profile_version": profile_version,
        "call_id": call_id,
        "tool": tool,
        "outcome": outcome,
        "duration_ms": duration_ms,
        "error": error,
        "summary": summary,
    }
    if data:
        ev["data"] = data
    return redact_deep(ev)


def call_error(kind: str, message: str | None = None) -> dict[str, Any]:
    """Structured error detail — kind is machine-readable, message is one line."""
    return {"kind": kind, "message": (message or "").strip()[:300]}
