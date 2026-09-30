"""Command line interface for profiles, launching, memory review and collection."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from . import identity, paths, secrets
from .collector import store
from .collector.report import DIMS, format_report, report as build_report
from .launcher import LaunchError, _expand_env, launch, plan
from .memory.backend import Scope, SqliteMemory
from .profile import effective, redacted_view
from .telemetry import Reporter, make_event
from .util import redact


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bridge", description="Directory-bound agent workspace toolkit")
    parser.add_argument("--version", action="version", version="bridge 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)

    launch_p = commands.add_parser("launch", help="launch Claude Code with this directory's profile")
    launch_p.add_argument("--directory", "-C", default=None)
    launch_p.add_argument("--client", default=None)
    launch_p.add_argument("--model", default=None)
    launch_p.add_argument("--role", default=None)
    launch_p.add_argument("--task", default=None)
    launch_p.add_argument("--app", default=None)
    launch_p.add_argument("--dry-run", action="store_true")
    launch_p.add_argument("args", nargs=argparse.REMAINDER, help="arguments passed to Claude after --")

    child_p = commands.add_parser("child", help="launch a child agent inheriting this session")
    child_p.add_argument("--parent", default=None, help="parent agent id; defaults to this session")
    child_p.add_argument("--model", default=None)
    child_p.add_argument("--role", default="subagent")
    child_p.add_argument("--task", default=None)
    child_p.add_argument("--dry-run", action="store_true")
    child_p.add_argument("args", nargs=argparse.REMAINDER)

    config_p = commands.add_parser("config", help="inspect effective profile configuration")
    config_sub = config_p.add_subparsers(dest="config_command", required=True)
    show_p = config_sub.add_parser("show")
    show_p.add_argument("--directory", "-C", default=None)

    status_p = commands.add_parser("status", help="set a short public activity summary")
    status_p.add_argument("summary", nargs="+")

    memory_p = commands.add_parser("memory", help="review local memories")
    memory_sub = memory_p.add_subparsers(dest="memory_command", required=True)
    list_p = memory_sub.add_parser("list")
    list_p.add_argument("--state", action="append", choices=("candidate", "flagged", "approved", "quarantined"))
    list_p.add_argument("--limit", type=int, default=50)
    review_p = memory_sub.add_parser("review")
    review_p.add_argument("memory_id")
    review_p.add_argument("action", choices=("approve", "flag", "quarantine"))
    forget_p = memory_sub.add_parser("forget")
    forget_p.add_argument("memory_id")

    outbox_p = commands.add_parser("outbox", help="inspect and retry locally queued telemetry")
    outbox_sub = outbox_p.add_subparsers(dest="outbox_command", required=True)
    outbox_sub.add_parser("status")
    outbox_sub.add_parser("flush")

    ps_p = commands.add_parser("ps", help="list agents with active sessions and their working directory")
    ps_p.add_argument("--all", action="store_true", help="include ended/error sessions too")

    collector_p = commands.add_parser("collector", help="run collector and inspect its report")
    collector_sub = collector_p.add_subparsers(dest="collector_command", required=True)
    run_p = collector_sub.add_parser("run")
    run_p.add_argument("--directory", "-C", default=None)
    report_p = collector_sub.add_parser("report")
    report_p.add_argument("--by", nargs="+", default=["model", "app", "task", "tool"],
                          choices=DIMS)
    report_p.add_argument("--since", default=None, help="inclusive UTC timestamp, ISO-8601")
    report_p.add_argument("--json", action="store_true")

    return parser


def _session_for_agent(agent_id: str) -> dict[str, Any]:
    path = paths.session_dir(agent_id) / paths.SESSION_FILE
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read session snapshot for {agent_id}: {exc}") from exc


def _launch(args: argparse.Namespace) -> int:
    launch_plan = plan(
        args.directory, role=args.role, task=args.task, model=args.model, app=args.app,
        client=args.client, extra_args=args.args[1:] if args.args[:1] == ["--"] else args.args,
    )
    return launch(launch_plan, dry_run=args.dry_run)


def _child(args: argparse.Namespace) -> int:
    parent = args.parent or os.environ.get(paths.ENV_AGENT)
    if not parent and os.environ.get(paths.ENV_SESSION):
        session = json.loads(Path(os.environ[paths.ENV_SESSION]).read_text(encoding="utf-8"))
        parent = session.get("identity", {}).get("agent_id")
    if not parent:
        raise ValueError("bridge child needs a parent; launch Bridge first or pass --parent AGENT_ID")
    if args.dry_run:
        # Planning materializes private config files but does not start Claude.
        child_plan = plan(parent=parent, role=args.role, task=args.task, model=args.model,
                          extra_args=args.args[1:] if args.args[:1] == ["--"] else args.args)
        return launch(child_plan, dry_run=True)
    child_plan = plan(parent=parent, role=args.role, task=args.task, model=args.model,
                      extra_args=args.args[1:] if args.args[:1] == ["--"] else args.args)
    return launch(child_plan)


def _memory(args: argparse.Namespace) -> int:
    backend = SqliteMemory()
    operator = Scope("__operator__", "__operator__", reviewer=os.environ.get("USER", "operator"))
    if args.memory_command == "list":
        states = tuple(args.state) if args.state else None
        rows = backend.list(operator, states=states, limit=max(1, min(args.limit, 500)), all_trees=True)
        for record in rows:
            print(json.dumps(record.to_dict(), ensure_ascii=False))
        return 0
    if args.memory_command == "review":
        rec = backend.review(args.memory_id, args.action, operator, force=True)
        print(f"{rec.id} {rec.state}")
        return 0
    if args.memory_command == "forget":
        deleted = backend.forget(args.memory_id, operator, force=True)
        print("deleted" if deleted else "not found")
        return 0 if deleted else 1
    return 2


def _status(summary_words: list[str]) -> int:
    agent_id = os.environ.get(paths.ENV_AGENT)
    if not agent_id:
        raise ValueError("status requires a Bridge-launched session (BRIDGE_AGENT_ID is unset)")
    summary = redact(" ".join(summary_words)).strip()
    if not summary or len(summary) > 240:
        raise ValueError("status summary must contain 1–240 characters")
    ident = identity.get(agent_id)
    if ident is None:
        raise ValueError(f"unknown agent {agent_id}")
    identity.set_status(agent_id, text=summary)
    session = _session_for_agent(agent_id)
    cfg = session.get("nats", {})
    Reporter(cfg.get("url", ""), stream=cfg.get("stream", "BRIDGE"),
             wildcard=f"{cfg.get('prefix', 'bridge')}.>").emit(
        make_event("agent.status", ident.telemetry, ident.profile_version, summary=summary)
    )
    print(f"status recorded for {agent_id}")
    return 0


def _ps(show_all: bool) -> int:
    sessions = identity.list_sessions()
    if not show_all:
        sessions = [s for s in sessions if s.status == "active"]
    if not sessions:
        print("no active agents")
        return 0
    rows = [
        (s.agent_id, identity.effective_status(s), s.role, s.start_dir)
        for s in sessions
    ]
    w_id = max(len("AGENT"), *(len(r[0]) for r in rows))
    w_status = max(len("STATUS"), *(len(r[1]) for r in rows))
    w_role = max(len("ROLE"), *(len(r[2]) for r in rows))
    print(f"{'AGENT':<{w_id}}  {'STATUS':<{w_status}}  {'ROLE':<{w_role}}  CWD")
    for agent_id, status, role, cwd in rows:
        print(f"{agent_id:<{w_id}}  {status:<{w_status}}  {role:<{w_role}}  {cwd}")
    return 0


def _main(args: argparse.Namespace) -> int:
    if args.command == "launch":
        return _launch(args)
    if args.command == "child":
        return _child(args)
    if args.command == "config":
        eff = effective(args.directory or Path.cwd())
        print(yaml.safe_dump(redacted_view(eff), sort_keys=False, allow_unicode=True), end="")
        return 0
    if args.command == "status":
        return _status(args.summary)
    if args.command == "ps":
        return _ps(args.all)
    if args.command == "memory":
        return _memory(args)
    if args.command == "outbox":
        if args.outbox_command == "status":
            from .telemetry.outbox import count
            print(f"{count()} events waiting for NATS durable receipt")
            return 0
        eff = effective(Path.cwd())
        nats = _expand_env(eff.nats)
        sent, failed = Reporter(nats["url"], stream=nats["stream"],
                                wildcard=f"{nats['prefix']}.>").flush()
        print(f"sent={sent} still_failing={failed}")
        return 0 if failed == 0 else 1
    if args.command == "collector":
        if args.collector_command == "run":
            from .collector.server import run
            eff = effective(args.directory or Path.cwd())
            nats = _expand_env(eff.nats)
            stats = run(nats["url"], stream=nats["stream"],
                        wildcard=f"{nats['prefix']}.>")
            print(json.dumps(stats))
            return 0
        conn = store.connect()
        try:
            data = build_report(conn, args.by, args.since)
            print(json.dumps(data, indent=2) if args.json else format_report(data, args.by))
            return 0
        finally:
            conn.close()
    return 2


def main(argv: list[str] | None = None) -> int:
    secrets.load(os.environ)
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return _main(args)
    except (LaunchError, ValueError, OSError, RuntimeError) as exc:
        print(f"bridge: {redact(str(exc))}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
