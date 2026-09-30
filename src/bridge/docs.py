"""Documentation routing: one MCP tool, a profile-declared routing table.

An agent calls ``document(kind, message)``. ``kind`` is looked up in this
workspace's effective ``docs.routes`` (from the trusted session file, not
from the tool call — an agent cannot invent a destination). A route writes
to a file (``append`` or ``replace``) or runs a command with ``message``
substituted in as one argument (never shell-interpolated). Every call emits
one telemetry event with kind, target and outcome — never the message
content.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import time
from pathlib import Path
from typing import Any

from . import paths
from .telemetry import Reporter, call_error, make_event
from .util import new_id


class DocsError(RuntimeError):
    pass


def _load_session(path: str | Path | None) -> dict[str, Any]:
    if not path:
        raise DocsError(
            "BRIDGE_SESSION is unset; docs routing is scoped by bridge launch — "
            "start the agent with `bridge launch`"
        )
    p = Path(path)
    if not p.is_file():
        raise DocsError(f"BRIDGE_SESSION does not point at a session file ({path})")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise DocsError(f"cannot read session file {p}: {e}") from e


class SessionDocs:
    def __init__(self, identity: dict[str, Any], profile_version: str,
                 nats_cfg: dict[str, Any], routes: dict[str, Any],
                 *, start_dir: str | Path, reporter: Reporter | None = None) -> None:
        self.identity = identity
        self.profile_version = profile_version
        self.routes = routes
        self.start_dir = Path(start_dir)
        self.reporter = reporter or Reporter(
            nats_cfg.get("url", ""), stream=nats_cfg.get("stream", "BRIDGE"),
            wildcard=f"{nats_cfg.get('prefix', 'bridge')}.>",
        )

    @classmethod
    def from_session_file(cls, path: str | Path | None = None) -> "SessionDocs":
        raw = path or os.environ.get(paths.ENV_SESSION)
        data = _load_session(raw)
        docs_cfg = data.get("docs") or {}
        return cls(
            identity=data["identity"], profile_version=data.get("profile_version", ""),
            nats_cfg=data.get("nats", {}), routes=docs_cfg.get("routes") or {},
            start_dir=data["identity"]["start_dir"],
        )

    def document(self, kind: str, message: str) -> dict[str, Any]:
        t0 = time.monotonic()
        call_id = new_id("call")
        self._emit("tool.start", call_id, "document", "started", t0, error=None, data={"kind": kind})
        try:
            result = self._write(kind, message)
        except Exception as e:  # noqa: BLE001 — surfaced to the caller, reported, then re-raised
            self._emit("tool.complete", call_id, "document", "error", t0,
                       error=call_error(type(e).__name__, str(e)), data={"kind": kind})
            raise
        self._emit("tool.complete", call_id, "document", "ok", t0, error=None,
                   data={"kind": kind, "target": result["target_kind"]})
        return result

    def _write(self, kind: str, message: str) -> dict[str, Any]:
        route = self.routes.get(kind)
        if route is None:
            known = ", ".join(sorted(self.routes)) or "(none configured)"
            raise DocsError(f"unknown docs kind {kind!r}; known kinds: {known}")
        if "command" in route:
            return self._run_command(route["command"], message)
        if "path" in route:
            return self._write_file(route["path"], route.get("mode", "append"), message)
        raise DocsError(f"docs route {kind!r} has neither 'path' nor 'command'")

    def _write_file(self, path: str, mode: str, message: str) -> dict[str, Any]:
        if mode not in ("append", "replace"):
            raise DocsError(f"docs route mode must be 'append' or 'replace', got {mode!r}")
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.start_dir / p
        p.parent.mkdir(parents=True, exist_ok=True)
        text = message if message.endswith("\n") else message + "\n"
        if mode == "append":
            with p.open("a", encoding="utf-8") as f:
                f.write(text)
        else:
            p.write_text(text, encoding="utf-8")
        return {"ok": True, "target_kind": "file", "path": str(p), "mode": mode}

    def _run_command(self, template: str, message: str) -> dict[str, Any]:
        # Split the template first, then substitute the placeholder as one
        # argv element — the message is never re-parsed as shell syntax.
        parts = shlex.split(template)
        if "{message}" not in parts:
            raise DocsError(f"docs route command must contain a literal {{message}} token: {template!r}")
        cmd = [message if part == "{message}" else part for part in parts]
        proc = subprocess.run(cmd, cwd=self.start_dir, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            raise DocsError(f"command exited {proc.returncode}: {proc.stderr.strip()[:300]}")
        return {"ok": True, "target_kind": "command", "command": cmd[0], "rc": proc.returncode}

    def _emit(self, event_type: str, call_id: str, tool: str, outcome: str,
              t0: float, *, error: dict | None, data: dict | None) -> None:
        ev = make_event(
            event_type, self.identity, self.profile_version, call_id=call_id, tool=tool,
            outcome=outcome, duration_ms=int((time.monotonic() - t0) * 1000),
            error=error, data=data,
        )
        try:
            self.reporter.emit(ev)
        except Exception:  # noqa: BLE001 — telemetry must never break a doc write
            pass
