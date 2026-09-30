"""Session-scoped memory: the instrumented front end used by MCP and the CLI.

Loads identity/scope from the **trusted session file** written by
``bridge launch`` (a file only the local user can write), never from tool
arguments — an agent cannot claim someone else's workspace or tree.

Every operation emits one telemetry event (``memory.remember`` /
``memory.recall`` / ``memory.forget`` / ``memory.review``) carrying the
memory id, counts and duration — never the memory's content. If the bus is
down the event lands in the outbox; the memory operation itself never blocks.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .. import paths
from ..telemetry import Receipt, Reporter, call_error, make_event
from ..util import new_id
from .backend import MemoryBackend, MemoryRecord, Scope, SqliteMemory


class SessionError(RuntimeError):
    pass


def _load_session(path: str | Path) -> dict[str, Any]:
    if not path:
        raise SessionError(
            "BRIDGE_SESSION is unset; memory is scoped by bridge launch — start the agent with `bridge launch`"
        )
    p = Path(path)
    if not p.is_file():
        raise SessionError(
            f"BRIDGE_SESSION does not point at a session file ({path}); memory is "
            "scoped by bridge launch — start the agent with `bridge launch`"
        )
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise SessionError(f"cannot read session file {p}: {e}") from e
    return data


def scope_from_identity(identity: dict[str, Any], reviewer: str | None = None,
                        memory_cfg: dict[str, Any] | None = None) -> Scope:
    memory_cfg = memory_cfg or {}
    return Scope(
        workspace_id=identity.get("workspace_id", ""),
        tree_id=identity.get("tree_id", ""),
        agent_id=identity.get("agent_id", ""),
        session_id=identity.get("session_id", ""),
        task_id=identity.get("task_id"),
        reviewer=reviewer or (identity.get("agent_id") or "cli"),
        share_scope=memory_cfg.get("scope", "root_tree"),
    )


class SessionMemory:
    """Memory plus its telemetry, bound to one session's identity."""

    def __init__(
        self,
        identity: dict[str, Any],
        profile_version: str,
        nats_cfg: dict[str, Any],
        *,
        backend: MemoryBackend | None = None,
        reporter: Reporter | None = None,
        scope: Scope | None = None,
        memory_cfg: dict[str, Any] | None = None,
    ) -> None:
        self.identity = identity
        self.profile_version = profile_version
        self.memory_cfg = memory_cfg or {"backend": "sqlite", "scope": "root_tree", "top_k": 5}
        backend_name = self.memory_cfg.get("backend", "sqlite")
        if backend is None and backend_name != "sqlite":
            raise SessionError(f"unsupported memory backend {backend_name!r}; this build provides sqlite")
        self.scope = scope or scope_from_identity(identity, memory_cfg=self.memory_cfg)
        if self.scope.share_scope not in ("workspace", "root_tree"):
            raise SessionError("memory.scope must be 'workspace' or 'root_tree'")
        try:
            self.default_top_k = max(1, min(25, int(self.memory_cfg.get("top_k", 5))))
        except (TypeError, ValueError) as e:
            raise SessionError("memory.top_k must be an integer") from e
        self.backend = backend if backend is not None else SqliteMemory()
        self.reporter = reporter or Reporter(
            nats_cfg.get("url", ""),
            stream=nats_cfg.get("stream", "BRIDGE"),
            wildcard=f"{nats_cfg.get('prefix', 'bridge')}.>",
        )

    # -- loaders --------------------------------------------------------------

    @classmethod
    def from_session_file(cls, path: str | Path | None = None) -> "SessionMemory":
        raw = path or os.environ.get(paths.ENV_SESSION)
        data = _load_session(raw)
        return cls(
            identity=data["identity"],
            profile_version=data.get("profile_version", ""),
            nats_cfg=data.get("nats", {}),
            memory_cfg=data.get("memory", {}),
        )

    # -- instrumented operations ---------------------------------------------

    def remember(self, content: str, tags: list[str] | None = None,
                 task_id: str | None = None, source_ref: str | None = None) -> MemoryRecord:
        return self._op(
            "remember", "memory.remember",
            lambda: self.backend.remember(content, self.scope, tags=tags, task_id=task_id,
                                           source_ref=source_ref),
            data=lambda rec: {"memory_id": rec.id, "chars": len(rec.content), "state": rec.state},
        )

    def recall(self, query: str, limit: int | None = None) -> list[MemoryRecord]:
        effective_limit = self.default_top_k if limit is None else max(1, min(25, int(limit)))
        return self._op(
            "recall", "memory.recall",
            lambda: self.backend.recall(query, self.scope, limit=effective_limit),
            data=lambda recs: {"hits": len(recs), "memory_ids": [r.id for r in recs]},
        )

    def forget(self, memory_id: str, force: bool = False) -> bool:
        return self._op(
            "forget", "memory.forget",
            lambda: self.backend.forget(memory_id, self.scope, force=force),
            data=lambda ok: {"memory_id": memory_id, "deleted": bool(ok)},
        )

    def review(self, memory_id: str, action: str, force: bool = False) -> MemoryRecord:
        return self._op(
            f"review:{action}", "memory.review",
            lambda: self.backend.review(memory_id, action, self.scope, force=force),
            data=lambda rec: {"memory_id": rec.id, "state": rec.state},
        )

    # -- plumbing -------------------------------------------------------------

    def _op(self, tool: str, event_type: str, fn, data=lambda r: {}):
        t0 = time.monotonic()
        call_id = new_id("call")
        self._emit("tool.start", call_id, tool, "started", t0,
                   error=None, data={"operation": event_type})
        try:
            result = fn()
        except Exception as e:  # noqa: BLE001 — report, then re-raise to the caller
            self._emit("tool.complete", call_id, tool, "error", t0,
                       error=call_error(type(e).__name__, str(e)), data=None)
            raise
        self._emit("tool.complete", call_id, tool, "ok", t0, error=None,
                   data={"operation": event_type, **data(result)})
        return result

    def _emit(self, event_type: str, call_id: str, tool: str, outcome: str,
              t0: float, *, error: dict | None, data: dict | None) -> Receipt:
        ev = make_event(
            event_type,
            self.identity,
            self.profile_version,
            call_id=call_id,
            tool=tool,
            outcome=outcome,
            duration_ms=int((time.monotonic() - t0) * 1000),
            error=error,
            data=data,
        )
        try:
            return self.reporter.emit(ev)
        except Exception:  # noqa: BLE001 — telemetry must never break memory
            return Receipt("dropped", ev.get("event_id", ""), "reporter raised")
