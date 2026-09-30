"""Reusable opt-in wrapper for tool calls owned by another integration.

Bridge automatically covers its MCP memory tools and Bridge launcher/status
events. Other tools are covered only when their host integration calls
``instrument_call``; Claude Code's native tools and native Task subagents are
not intercepted by this wrapper.
"""

from __future__ import annotations

import time
from typing import Any, Callable, TypeVar

from .events import call_error, make_event
from .reporter import Reporter
from ..util import new_id

T = TypeVar("T")


def instrument_call(
    reporter: Reporter,
    identity: dict[str, Any],
    profile_version: str,
    tool: str,
    function: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Emit correlated start/completion events without recording args/results."""
    call_id = new_id("call")
    started = time.monotonic()
    reporter.emit(make_event("tool.start", identity, profile_version, call_id=call_id,
                             tool=tool, outcome="started"))
    try:
        result = function(*args, **kwargs)
    except Exception as exc:
        reporter.emit(make_event(
            "tool.complete", identity, profile_version, call_id=call_id, tool=tool,
            outcome="error", duration_ms=int((time.monotonic() - started) * 1000),
            error=call_error(type(exc).__name__, "tool call failed"),
        ))
        raise
    reporter.emit(make_event(
        "tool.complete", identity, profile_version, call_id=call_id, tool=tool,
        outcome="ok", duration_ms=int((time.monotonic() - started) * 1000),
    ))
    return result
