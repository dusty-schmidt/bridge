"""The launcher: profile + identity -> one command that starts a client.

What ``bridge launch`` does, in order:

1. resolves the inheritance chain from the **launch directory** (or, for a
   child, the parent session's directory) up to the declared root;
2. assigns identity — workspace/session/agent ids, parent, task, role, model,
   app — and freezes it into ``~/.bridge/sessions/<agent_id>/session.json``
   (mode 0600) together with the effective profile. Everything downstream
   (MCP server, status, telemetry) reads *that file*, so browsing other
   directories mid-session cannot silently switch profiles;
3. materialises the workspace's tools for this session: the generated
   ``mcp.json`` (bridge memory + any profile-declared MCP servers) and the
   composed instructions;
4. emits ``agent.start``, runs the client as a subprocess, emits ``agent.end``
   with the exit code.

Client integration: **Claude Code first** (``--mcp-config`` +
``--append-system-prompt``). Other clients still get identity env vars and
any extra args you pass; they just don't receive the generated MCP/system
prompt flags — see README "Known gaps".
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import gitops
from . import identity as ids
from . import paths
from .profile import Chain, EffectiveProfile, effective, ProfileError
from .telemetry import Reporter, call_error, make_event

# One tested integration for this MVP; other clients need a dedicated adapter.
CLIENTS: dict[str, dict[str, Any]] = {
    "claude": {"cmd": ["claude"], "mcp": True, "system_prompt": True, "app": "claude-code"},
}


class LaunchError(RuntimeError):
    pass


@dataclass
class LaunchPlan:
    identity: ids.Identity
    profile: EffectiveProfile
    session_file: Path
    mcp_file: Path
    instructions_file: Path
    command: list[str]
    env: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match) -> str:
            name = match.group(1)
            if name not in os.environ:
                raise LaunchError(f"profile references unset environment variable {name}")
            return os.environ[name]
        return _ENV_REF.sub(replace, value)
    if isinstance(value, list):
        return [_expand_env(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand_env(item) for key, item in value.items()}
    return value


def reporter_for(eff: EffectiveProfile) -> Reporter:
    nats = eff.nats
    return Reporter(nats["url"], stream=nats["stream"], wildcard=f"{nats['prefix']}.>")


def _emit_safely(reporter: Reporter, event: dict[str, Any]) -> None:
    """Activity reporting is best-effort for agent lifecycle as for tool calls."""
    try:
        reporter.emit(event)
    except Exception:  # noqa: BLE001 — telemetry storage failure must not block launch
        pass


# -- planning ----------------------------------------------------------------


def _profile_from_session(session: dict[str, Any]) -> EffectiveProfile:
    ident = session["identity"]
    saved = session["profile_chain"]
    start = Path(ident["start_dir"])
    chain = Chain(
        start_dir=start,
        dirs=[Path(p) for p in saved["dirs"]],
        profiles=saved["profiles"],
        declared_root=Path(saved["declared_root"]) if saved["declared_root"] else None,
        notes=saved.get("notes", []),
    )
    return EffectiveProfile(start, chain, session["profile_data"], session["profile_version"])


def plan(
    directory: str | Path | None = None,
    *,
    parent: str | None = None,
    role: str | None = None,
    task: str | None = None,
    model: str | None = None,
    app: str | None = None,
    client: str | None = None,
    extra_args: list[str] | None = None,
) -> LaunchPlan:
    parent_ident: ids.Identity | None = None
    notes: list[str] = []

    if parent:
        parent_ident = ids.get(parent)
        if parent_ident is None:
            raise LaunchError(f"unknown parent agent {parent!r}")
        session_path = paths.session_dir(parent_ident.agent_id) / paths.SESSION_FILE
        try:
            parent_session = json.loads(session_path.read_text(encoding="utf-8"))
            parent_profile = _profile_from_session(parent_session)
        except (OSError, KeyError, ValueError, TypeError) as exc:
            raise LaunchError(f"cannot load parent profile snapshot at {session_path}: {exc}") from exc
        if directory:
            requested = Path(directory).expanduser().resolve()
            if requested != parent_profile.start_dir:
                raise LaunchError("child launch inherits its parent workspace; omit a different directory")

    # Directory: explicit wins; otherwise a child reuses the parent's workspace.
    if directory:
        start = Path(directory).expanduser().resolve()
    elif parent_ident:
        start = Path(parent_ident.start_dir)
    else:
        start = Path.cwd()

    eff = parent_profile if parent_ident else effective(start)
    ag = eff.agent

    client_name = client or ag.get("client") or "claude"
    if client_name not in CLIENTS:
        raise LaunchError(f"unknown client {client_name!r}; known: {', '.join(CLIENTS)}")
    cspec = CLIENTS[client_name]

    agent_id = ids.new_agent_id()
    if parent_ident:
        role_name = role or "subagent"
        model_name = model or parent_ident.model or ag.get("model") or ""
        app_name = app or parent_ident.app or cspec["app"]
        task_id = task or parent_ident.task_id or ids.new_task_id()
    else:
        role_name = role or ag.get("role") or "worker"
        model_name = model or ag.get("model") or ""
        app_name = app or ag.get("app") or cspec["app"]
        task_id = task or ids.new_task_id()

    ident = ids.Identity(
        agent_id=agent_id,
        session_id=ids.new_session_id(),
        workspace_id=eff.workspace_id,
        tree_id=eff.tree_id,
        root_dir=str(eff.root_dir) if eff.root_dir else None,
        start_dir=str(start),
        parent_agent_id=parent_ident.agent_id if parent_ident else None,
        task_id=task_id,
        role=role_name,
        model=model_name,
        app=app_name,
        client=client_name,
        profile_version=eff.version,
    )

    sdir = paths.session_dir(agent_id)
    paths.ensure_state()
    sdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    sdir.chmod(0o700)
    session_file = sdir / paths.SESSION_FILE
    mcp_file = sdir / paths.MCP_FILE
    instructions_file = sdir / paths.INSTRUCTIONS_FILE

    profile_instructions = (
        parent_session.get("profile_instructions", "") if parent_ident
        else eff.resolved_instruction_text()
    )
    instructions = compose_instructions(ident, eff, profile_instructions)
    instructions_file.write_text(instructions, encoding="utf-8")
    os.chmod(instructions_file, 0o600)

    external_servers = (
        parent_session.get("external_mcp_servers", {}) if parent_ident else eff.mcp_servers
    )
    external_servers = _expand_env(external_servers)
    nats_cfg = _expand_env(eff.nats)
    mcp_cfg = build_mcp_config(ident, eff, external_servers=external_servers)
    mcp_file.write_text(json.dumps(mcp_cfg, indent=2), encoding="utf-8")
    os.chmod(mcp_file, 0o600)

    session = {
        "version": 1,
        "created_utc": ident.created_utc,
        "identity": ident.to_dict(),
        "profile_version": eff.version,
        "profile_data": eff.data,
        "profile_chain": {
            "dirs": [str(d) for d in eff.chain.dirs],
            "declared_root": str(eff.chain.declared_root) if eff.chain.declared_root else None,
            "notes": eff.chain.notes,
            "profiles": eff.chain.profiles,
        },
        "external_mcp_servers": {
            key: value for key, value in external_servers.items()
            if key not in ("bridge-memory", "bridge-docs")
        },
        "chain": [str(d) for d in eff.chain.dirs],
        "root_dir": ident.root_dir,
        "start_dir": ident.start_dir,
        "nats": nats_cfg,
        "memory": eff.memory,
        "docs": eff.docs,
        "instructions_file": str(instructions_file),
        "profile_instructions": profile_instructions,
    }
    session_file.write_text(json.dumps(session, indent=2), encoding="utf-8")
    os.chmod(session_file, 0o600)

    command = build_command(
        cspec, mcp_file, instructions_file, extra_args or [], mcp_cfg,
        model=ident.model, defaults=ag.get("defaults") or {},
    )
    env = {
        paths.ENV_SESSION: str(session_file),
        paths.ENV_AGENT: agent_id,
        paths.ENV_STATE_DIR: str(paths.state_dir()),
        paths.ENV_NATS_URL: nats_cfg["url"],
        "BRIDGE_PROFILE_VERSION": eff.version,
        "BRIDGE_WORKSPACE": ident.workspace_id,
        "BRIDGE_TASK": task_id or "",
    }
    if not mcp_cfg.get("mcpServers"):
        notes.append("no MCP servers configured for this session")

    return LaunchPlan(
        identity=ident, profile=eff, session_file=session_file, mcp_file=mcp_file,
        instructions_file=instructions_file, command=command, env=env, notes=notes,
    )


# -- materialisation ---------------------------------------------------------


def compose_instructions(ident: ids.Identity, eff: EffectiveProfile,
                         profile_instructions: str | None = None) -> str:
    head = [
        "# Bridge session",
        "",
        f"- agent: `{ident.agent_id}`  role: `{ident.role}`"
        + (f"  parent: `{ident.parent_agent_id}`" if ident.parent_agent_id else ""),
        f"- task: `{ident.task_id or '-'}`  model: `{ident.model or '-'}`  app: `{ident.app}`",
        f"- workspace: `{ident.start_dir}`  root: `{ident.root_dir or '-'}`  profile: `{eff.version}`",
        "",
        "Your workspace and memory scope are fixed for this session (set at launch).",
        "Memory MCP tools: `remember` stores a *candidate* (visible to this workspace only"
        " until an operator approves it), `recall` searches what you may see, `forget`"
        " deletes your workspace's entry. Quarantined memories never appear.",
        "Activity (agent lifecycle, memory ops, wrapped tool calls, status lines) is"
        " reported as telemetry events — no private reasoning, no secrets, no payloads.",
    ]
    body = eff.resolved_instruction_text() if profile_instructions is None else profile_instructions
    text = "\n".join(head)
    if body:
        text += "\n\n## Standing instructions\n\n" + body
    return text + "\n"


def build_mcp_config(ident: ids.Identity, eff: EffectiveProfile,
                     external_servers: dict[str, Any] | None = None) -> dict[str, Any]:
    servers: dict[str, Any] = {}
    if eff.memory.get("mcp", True):
        servers["bridge-memory"] = {
            "command": sys.executable,
            "args": ["-m", "bridge.memory.mcp_server"],
            "env": {
                paths.ENV_SESSION: str(paths.session_dir(ident.agent_id) / paths.SESSION_FILE),
                paths.ENV_STATE_DIR: str(paths.state_dir()),
            },
        }
    if eff.docs.get("routes"):
        servers["bridge-docs"] = {
            "command": sys.executable,
            "args": ["-m", "bridge.docs_mcp"],
            "env": {
                paths.ENV_SESSION: str(paths.session_dir(ident.agent_id) / paths.SESSION_FILE),
                paths.ENV_STATE_DIR: str(paths.state_dir()),
            },
        }
    # Workspace-declared MCP connections from the profile chain.
    reserved = {"bridge-memory", "bridge-docs"}
    for name, cfg in (external_servers if external_servers is not None else eff.mcp_servers).items():
        if name in reserved and name in servers:
            raise LaunchError(f"MCP server name {name!r} is reserved by Bridge")
        servers[name] = cfg
    return {"mcpServers": servers}


def build_command(cspec: dict[str, Any], mcp_file: Path, instructions_file: Path,
                  extra_args: list[str], mcp_cfg: dict[str, Any], *,
                  model: str | None = None, defaults: dict[str, Any] | None = None) -> list[str]:
    cmd = list(cspec["cmd"])
    if cspec.get("mcp") and mcp_cfg.get("mcpServers"):
        cmd += ["--mcp-config", str(mcp_file), "--strict-mcp-config"]
    if model:
        cmd += ["--model", model]
    defaults = defaults or {}
    if defaults.get("permission_mode"):
        cmd += ["--permission-mode", str(defaults["permission_mode"])]
    for name, flag in (("allowed_tools", "--allowedTools"),
                       ("disallowed_tools", "--disallowedTools")):
        if defaults.get(name):
            cmd += [flag, *map(str, defaults[name])]
    if cspec.get("system_prompt"):
        cmd += ["--append-system-prompt-file", str(instructions_file)]
    return cmd + list(extra_args)


# -- execution ---------------------------------------------------------------


def launch(plan_obj: LaunchPlan, *, dry_run: bool = False) -> int:
    ident = plan_obj.identity
    eff = plan_obj.profile

    if dry_run:
        print(f"agent_id     {ident.agent_id}")
        print(f"session      {ident.session_id}")
        print(f"workspace    {ident.workspace_id}  ({ident.start_dir})")
        print(f"tree/root    {ident.tree_id}  ({ident.root_dir or '-'})")
        print(f"parent       {ident.parent_agent_id or '-'}   task {ident.task_id}   role {ident.role}")
        print(f"model/app    {ident.model or '-'} / {ident.app}   client {ident.client}")
        print(f"profile      {eff.version}  chain: {' -> '.join(str(d) for d in eff.chain.dirs) or '(defaults)'}")
        print(f"session file {plan_obj.session_file}")
        print(f"client       {plan_obj.command[0]} (profile instructions and private MCP config prepared)")
        for n in plan_obj.notes + eff.chain.notes:
            print(f"note         {n}")
        return 0

    ids.record(ident)
    rep = reporter_for(eff)
    t0 = time.monotonic()
    _emit_safely(rep, make_event(
        "agent.start", ident.telemetry, ident.profile_version,
        tool=f"client:{ident.client}", outcome="ok",
        data={"command": plan_obj.command[0], "parent": ident.parent_agent_id},
    ))

    start_path = Path(ident.start_dir)
    session_branch = (
        not ident.parent_agent_id
        and eff.git.get("auto_branch", True)
        and gitops.is_repo(start_path)
    )
    if session_branch:
        branch = f"bridge/{ident.agent_id}"
        if gitops.create_session_branch(start_path, branch):
            print(f"bridge: checked out {branch}", file=sys.stderr)
        else:
            session_branch = False

    env = dict(os.environ)
    env.update(plan_obj.env)

    try:
        proc = subprocess.run(plan_obj.command, cwd=ident.start_dir, env=env)
        rc = proc.returncode
        outcome, err = ("ok", None) if rc == 0 else ("error", call_error("exit_code", f"rc={rc}"))
    except FileNotFoundError as e:
        rc = 127
        outcome, err = "error", call_error("not_found", str(e))
    except KeyboardInterrupt:
        rc = 130
        outcome, err = "error", call_error("interrupted", "KeyboardInterrupt")

    if session_branch and gitops.has_changes(start_path):
        gitops.commit_all(start_path, f"bridge: {ident.agent_id} session snapshot (task {ident.task_id})")

    ids.set_status(ident.agent_id, "ended" if outcome == "ok" else "error")
    _emit_safely(rep, make_event(
        "agent.end", ident.telemetry, ident.profile_version,
        tool=f"client:{ident.client}", outcome=outcome,
        duration_ms=int((time.monotonic() - t0) * 1000), error=err,
        data={"rc": rc},
    ))
    return rc
