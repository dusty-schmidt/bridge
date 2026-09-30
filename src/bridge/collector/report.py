"""Reports over the collector store: call counts and errors by dimension.

Dimensions the spec asks for: model, agent application (``app``), task, tool —
plus ``agent`` as a bonus. A "call" is any event that carries a tool name and
an outcome, excluding ``tool.start`` (start/complete pairs count once, on the
completion side).
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable

DIMS = ("model", "app", "task", "tool", "agent")
_DIM_COL = {
    "model": "model", "app": "app", "task": "task_id", "tool": "tool", "agent": "agent_id",
}
_CALL_FILTER = "type='tool.complete' AND tool IS NOT NULL AND outcome IS NOT NULL"


def query_dimension(conn: sqlite3.Connection, dim: str, since: str | None = None) -> list[dict[str, Any]]:
    if dim not in _DIM_COL:
        raise ValueError(f"unknown dimension {dim!r}; one of {DIMS}")
    col = _DIM_COL[dim]
    where = _CALL_FILTER
    params: list[Any] = []
    if since:
        where += " AND ts >= ?"
        params.append(since)
    rows = conn.execute(
        f"""SELECT COALESCE({col}, '(none)') AS key,
                   COUNT(*)                AS calls,
                   SUM(CASE WHEN outcome='error' THEN 1 ELSE 0 END) AS errors,
                   CAST(AVG(duration_ms) AS INTEGER) AS avg_ms
            FROM events WHERE {where}
            GROUP BY key ORDER BY calls DESC""",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def report(conn: sqlite3.Connection, by: Iterable[str] = ("model", "app", "task", "tool"),
           since: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"since": since or "beginning"}
    for dim in by:
        out[dim] = query_dimension(conn, dim, since)
    return out


def format_report(data: dict[str, Any], by: Iterable[str] = ("model", "app", "task", "tool")) -> str:
    lines = [f"bridge report — calls and errors since {data['since']}", ""]
    for dim in by:
        rows = data.get(dim) or []
        lines.append(f"by {dim}:")
        if not rows:
            lines.append("  (no calls)")
        for r in rows:
            lines.append(
                f"  {r['key']:<28} calls={r['calls']:<6} errors={r['errors']:<5} avg_ms={r['avg_ms']}"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
