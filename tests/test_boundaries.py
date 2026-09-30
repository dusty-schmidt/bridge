from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time

from bridge import gitops, identity, secrets
from bridge.collector import store
from bridge.collector.report import query_dimension
from bridge.cli import main
from bridge.docs import DocsError, SessionDocs
from bridge.launcher import launch, plan
from bridge.memory.backend import Scope, SqliteMemory
from bridge.memory.session import SessionError, SessionMemory, _load_session
from bridge.profile import ProfileError, effective, redacted_view
from bridge.telemetry import Reporter, make_event
from bridge.telemetry import outbox
from bridge.telemetry.wrapper import instrument_call


def _init_repo(path):
    run = lambda *args: subprocess.run(args, cwd=path, check=True, capture_output=True)
    run("git", "init", "-q", "-b", "main")
    run("git", "config", "user.email", "test@example.com")
    run("git", "config", "user.name", "Test")
    (path / "README.md").write_text("hi\n", encoding="utf-8")
    run("git", "add", "-A")
    run("git", "commit", "-q", "-m", "init")


def profile(directory, content):
    path = directory / ".bridge" / "profile.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_profile_merges_maps_and_replaces_lists(tmp_path):
    root = tmp_path / "tree"
    child = root / "nested"
    child.mkdir(parents=True)
    profile(root, "root: true\ninstructions:\n  files: [ROOT.md]\nmcp:\n  servers:\n    alpha: {command: one}\n")
    profile(child, "instructions:\n  files: [CHILD.md]\nmcp:\n  servers:\n    alpha: null\n    beta: {command: two}\n")

    eff = effective(child)

    assert eff.chain.declared_root == root
    assert eff.data["instructions"]["files"] == ["CHILD.md"]
    assert eff.mcp_servers == {"beta": {"command": "two"}}


def test_profile_without_declared_root_fails_loudly(tmp_path):
    workspace = tmp_path / "tree"
    profile(workspace, "instructions:\n  text: no root declared\n")

    try:
        effective(workspace)
    except ProfileError as exc:
        assert "root: true" in str(exc)
    else:
        raise AssertionError("undeclared root should raise ProfileError, not silently fall back")


def test_no_profile_anywhere_uses_defaults_without_error(tmp_path):
    workspace = tmp_path / "bare"
    workspace.mkdir()

    eff = effective(workspace)

    assert eff.chain.declared_root is None
    assert eff.chain.notes == ["no .bridge/profile.yaml found; built-in defaults apply"]


def test_memory_is_workspace_local_until_approved_and_quarantine_is_hidden(tmp_path):
    db = tmp_path / "memory.sqlite"
    backend = SqliteMemory(db_path=db)
    a = Scope("workspace-a", "tree", agent_id="agent-a", session_id="s-a")
    b = Scope("workspace-b", "tree", agent_id="agent-b", session_id="s-b")
    other_tree = Scope("workspace-c", "tree-c", agent_id="agent-c", session_id="s-c")
    candidate = backend.remember("private fact", a, task_id="task-1", source_ref="src/file.py")
    assert candidate.task_id == "task-1"
    assert candidate.source_ref == "src/file.py"

    assert [m.id for m in backend.recall("private", a)] == [candidate.id]
    assert backend.recall("private", b) == []
    assert backend.recall("private", other_tree) == []
    backend.review(candidate.id, "approve", a)
    assert [m.id for m in backend.recall("private", b)] == [candidate.id]
    assert backend.recall("private", other_tree) == []
    backend.review(candidate.id, "quarantine", a)
    assert backend.recall("private", a) == []
    assert backend.recall("private", b) == []
    assert backend.list(a, states=("quarantined",), all_trees=True)[0].id == candidate.id


def test_outage_queues_then_flushes_once_and_collector_deduplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    outcomes = [RuntimeError("offline"), None]
    delivered = []

    def publish(subject, payload):
        outcome = outcomes.pop(0)
        if outcome:
            raise outcome
        delivered.append(payload)

    reporter = Reporter("nats://example", publisher=publish, retry_interval=0,
                        background_retry=False)
    event = make_event("tool.complete", {"agent_id": "agent-a", "model": "sonnet"}, "v1",
                       call_id="call-1", tool="shell", outcome="ok")
    first = reporter.emit(event)
    assert first.status == "outbox"
    assert outbox.count() == 1

    sent, failed = reporter.flush()
    assert (sent, failed) == (1, 0)
    assert outbox.count() == 0
    assert json.loads(delivered[0]) == event

    conn = store.connect(tmp_path / "collector.sqlite")
    try:
        payload = json.loads(delivered[0])
        assert store.store_event(conn, payload) is True
        assert store.store_event(conn, payload) is False
        conn.commit()
        assert store.counts(conn)["calls"] == 1
    finally:
        conn.close()


def test_child_uses_parent_profile_snapshot_and_records_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile(workspace, "root: true\ninstructions:\n  text: original guidance\nagent:\n  model: sonnet\n  defaults:\n    permission_mode: plan\n")
    (workspace / "AGENTS.md").write_text("workspace rules", encoding="utf-8")
    parent = plan(workspace)
    identity.record(parent.identity)
    saved = json.loads(parent.session_file.read_text(encoding="utf-8"))
    profile(workspace, "root: true\ninstructions:\n  text: changed guidance\nagent:\n  model: opus\n")

    child = plan(parent=parent.identity.agent_id)

    assert child.identity.parent_agent_id == parent.identity.agent_id
    assert child.identity.is_subagent is True
    assert child.identity.role == "subagent"
    assert child.identity.model == "sonnet"
    assert child.identity.app == parent.identity.app
    assert child.identity.task_id == parent.identity.task_id
    assert child.profile.version == saved["profile_version"]
    assert "original guidance" in child.instructions_file.read_text(encoding="utf-8")
    assert "changed guidance" not in child.instructions_file.read_text(encoding="utf-8")
    assert "--mcp-config" in child.command
    assert "--strict-mcp-config" in child.command
    assert "--append-system-prompt-file" in child.command
    assert child.command[child.command.index("--model") + 1] == "sonnet"
    assert child.command[child.command.index("--permission-mode") + 1] == "plan"


def test_config_redacts_secret_values_and_launcher_resolves_env_refs(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("TRACKER_TOKEN", "abc123-secret-value")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile(workspace, "root: true\nnats:\n  url: 'nats://alice:super-secret@localhost:4222'\nmcp:\n  servers:\n    tracker:\n      command: tool\n      env:\n        API_TOKEN: '${TRACKER_TOKEN}'\n")

    eff = effective(workspace)
    view = redacted_view(eff)
    launch_plan = plan(workspace)
    config = json.loads(launch_plan.mcp_file.read_text(encoding="utf-8"))

    assert "abc123-secret-value" not in json.dumps(view)
    assert "super-secret" not in json.dumps(view)
    assert "${TRACKER_TOKEN}" in json.dumps(view)
    assert config["mcpServers"]["tracker"]["env"]["API_TOKEN"] == "abc123-secret-value"
    assert stat.S_IMODE(launch_plan.mcp_file.stat().st_mode) == 0o600


def test_reusable_tool_wrapper_correlates_events_without_payloads(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BRIDGE_TELEMETRY", "on")
    published = []
    reporter = Reporter("nats://unused", publisher=lambda subject, payload: published.append(payload))

    result = instrument_call(
        reporter, {"agent_id": "agent-a", "model": "sonnet"}, "v1", "secret-tool",
        lambda value: value, "private-argument-and-result",
    )

    events = [json.loads(payload) for payload in published]
    assert result == "private-argument-and-result"
    assert [event["type"] for event in events] == ["tool.start", "tool.complete"]
    assert events[0]["call_id"] == events[1]["call_id"]
    assert "private-argument-and-result" not in json.dumps(events)


def test_memory_operations_work_during_bus_outage_and_emit_correlated_events(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    published = []
    reporter = Reporter("nats://unused", publisher=lambda subject, payload: (
        published.append(payload), (_ for _ in ()).throw(OSError("bus offline"))
    )[0], background_retry=False)
    service = SessionMemory(
        {"workspace_id": "w", "tree_id": "t", "agent_id": "a", "session_id": "s"},
        "profile-v1", {"url": "nats://unused"},
        backend=SqliteMemory(db_path=tmp_path / "memory.sqlite"), reporter=reporter,
    )

    rec = service.remember("valuable content", source_ref="notes.md")

    assert rec.state == "candidate"
    assert service.recall("valuable")
    events = [outbox.decode(row) for row in outbox.pending()]
    by_call = {}
    for event in events:
        by_call.setdefault(event["call_id"], set()).add(event["type"])
    assert by_call and all(types == {"tool.start", "tool.complete"} for types in by_call.values())
    assert "valuable content" not in json.dumps(events)


def test_memory_profile_controls_share_scope_and_default_retrieval(tmp_path):
    backend = SqliteMemory(db_path=tmp_path / "memory.sqlite")
    memory = SessionMemory(
        {"workspace_id": "workspace-a", "tree_id": "tree", "agent_id": "agent-a",
         "session_id": "session-a", "task_id": "task-a"},
        "profile-v1", {"url": "nats://unused"}, backend=backend,
        reporter=Reporter("nats://unused", publisher=lambda *_: None, background_retry=False),
        memory_cfg={"backend": "sqlite", "scope": "workspace", "top_k": 2},
    )
    record = memory.remember("scope preference")
    memory.review(record.id, "approve")
    other_workspace = Scope("workspace-b", "tree", share_scope="workspace")

    assert memory.default_top_k == 2
    assert memory.scope.task_id == "task-a"
    assert backend.recall("scope", other_workspace) == []


def test_outbox_retries_in_background_after_bus_recovers(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    attempts = 0
    delivered = threading.Event()

    def publish(subject, payload):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("offline")
        delivered.set()

    reporter = Reporter("nats://unused", publisher=publish, retry_interval=0.01)
    reporter.emit(make_event("tool.complete", {}, "v1", tool="shell", outcome="ok"))

    assert delivered.wait(2)
    deadline = time.monotonic() + 2
    while outbox.count() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert attempts >= 2
    assert outbox.count() == 0


def test_collector_report_counts_only_completed_calls(tmp_path):
    conn = store.connect(tmp_path / "collector.sqlite")
    try:
        start = make_event("tool.start", {"model": "sonnet", "app": "claude-code"}, "v1",
                           call_id="c1", tool="shell", outcome="started")
        complete = make_event("tool.complete", {"model": "sonnet", "app": "claude-code"}, "v1",
                             call_id="c1", tool="shell", outcome="error")
        store.store_event(conn, start)
        store.store_event(conn, complete)
        conn.commit()
        assert query_dimension(conn, "model")[0]["calls"] == 1
        assert query_dimension(conn, "model")[0]["errors"] == 1
    finally:
        conn.close()


def test_memory_server_requires_trusted_session_file(monkeypatch):
    monkeypatch.delenv("BRIDGE_SESSION", raising=False)
    try:
        _load_session(None)
    except SessionError as exc:
        assert "bridge launch" in str(exc)
    else:
        raise AssertionError("missing session file should fail clearly")


def test_secrets_env_loads_without_overriding_already_exported_vars(tmp_path, monkeypatch):
    monkeypatch.delenv("TRACKER_TOKEN", raising=False)
    monkeypatch.delenv("OTHER_KEY", raising=False)
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    secrets_path = secrets.ensure_file()
    secrets_path.write_text(
        "# comment, and a blank line above should be skipped\nTRACKER_TOKEN=from-file\nOTHER_KEY=also-from-file\n",
        encoding="utf-8",
    )

    env: dict[str, str] = {"TRACKER_TOKEN": "already-exported"}
    applied = secrets.load(env)

    assert env["TRACKER_TOKEN"] == "already-exported"
    assert env["OTHER_KEY"] == "also-from-file"
    assert applied == ["OTHER_KEY"]
    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600


def test_secrets_env_missing_file_is_a_silent_no_op(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    assert secrets.load({}) == []


def test_cli_auto_loads_secrets_env_at_startup(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("PRIVATE_KEY", raising=False)
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    secrets.ensure_file().write_text("PRIVATE_KEY=from-secrets-env\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile(workspace, "root: true\n")

    assert main(["config", "show", "-C", str(workspace)]) == 0
    assert os.environ.get("PRIVATE_KEY") == "from-secrets-env"


def test_ps_lists_only_active_agents_with_their_cwd(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    active = identity.Identity(
        agent_id="ag-active", session_id="s1", workspace_id="ws", tree_id="tree",
        root_dir=str(tmp_path), start_dir=str(tmp_path / "workdir"), parent_agent_id=None,
        task_id=None, role="developer", model="sonnet", app="claude-code", client="claude",
        profile_version="v1",
    )
    ended = identity.Identity(
        agent_id="ag-ended", session_id="s2", workspace_id="ws", tree_id="tree",
        root_dir=str(tmp_path), start_dir=str(tmp_path / "other"), parent_agent_id=None,
        task_id=None, role="developer", model="sonnet", app="claude-code", client="claude",
        profile_version="v1", status="ended",
    )
    identity.record(active)
    identity.record(ended)

    assert main(["ps"]) == 0
    output = capsys.readouterr().out
    assert "ag-active" in output
    assert str(tmp_path / "workdir") in output
    assert "ag-ended" not in output


def test_ps_with_no_active_agents_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))

    assert main(["ps"]) == 0
    assert capsys.readouterr().out.strip() == "no active agents"


def test_config_cli_prints_effective_profile_with_secret_references_redacted(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("PRIVATE_KEY", "do-not-print-this")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    profile(workspace, "root: true\nmcp:\n  servers:\n    private:\n      env:\n        ACCESS_TOKEN: '${PRIVATE_KEY}'\n")

    assert main(["config", "show", "-C", str(workspace)]) == 0
    output = capsys.readouterr().out
    assert "${PRIVATE_KEY}" in output
    assert "do-not-print-this" not in output
    assert "backend: sqlite" in output
    assert "model: sonnet" in output


def _docs(routes, start_dir, **overrides):
    return SessionDocs(
        identity={"agent_id": "a"}, profile_version="v1", nats_cfg={}, routes=routes,
        start_dir=start_dir,
        reporter=Reporter("nats://unused", publisher=lambda *_: None, background_retry=False),
        **overrides,
    )


def test_docs_file_route_appends_then_replaces(tmp_path):
    target = tmp_path / "NOTES.md"
    doc = _docs({"note": {"path": str(target), "mode": "append"}}, tmp_path)

    doc.document("note", "first")
    doc.document("note", "second")
    assert target.read_text(encoding="utf-8") == "first\nsecond\n"

    doc.routes["note"]["mode"] = "replace"
    doc.document("note", "only")
    assert target.read_text(encoding="utf-8") == "only\n"


def test_docs_relative_path_resolves_against_workspace_not_process_cwd(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    doc = _docs({"note": {"path": "NOTES.md", "mode": "append"}}, workspace)

    doc.document("note", "scoped to the workspace, not cwd")

    assert (workspace / "NOTES.md").read_text(encoding="utf-8") == "scoped to the workspace, not cwd\n"
    assert not (elsewhere / "NOTES.md").exists()


def test_docs_unknown_kind_lists_known_kinds(tmp_path):
    doc = _docs({"task": {"path": "x", "mode": "append"}}, tmp_path)
    try:
        doc.document("missing", "x")
    except DocsError as e:
        assert "task" in str(e)
    else:
        raise AssertionError("unknown kind should raise DocsError")


def test_docs_command_route_passes_message_as_one_argv_element(tmp_path):
    out_file = tmp_path / "out.txt"
    script = tmp_path / "capture.py"
    script.write_text(
        "import sys, pathlib\npathlib.Path(sys.argv[1]).write_text(sys.argv[2])\n", encoding="utf-8"
    )
    template = f"{sys.executable} {script} {out_file} {{message}}"
    doc = _docs({"note": {"command": template}}, tmp_path)

    tricky = 'hello "world" with spaces'
    result = doc.document("note", tricky)

    assert result["ok"] is True
    assert out_file.read_text(encoding="utf-8") == tricky


def test_launch_includes_bridge_docs_only_when_routes_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    with_routes = tmp_path / "with-routes"
    with_routes.mkdir()
    profile(with_routes, "root: true\ndocs:\n  routes:\n    task:\n      path: TASKS.md\n      mode: append\n")
    without_routes = tmp_path / "without-routes"
    without_routes.mkdir()
    profile(without_routes, "root: true\n")

    with_config = json.loads(plan(with_routes).mcp_file.read_text(encoding="utf-8"))
    without_config = json.loads(plan(without_routes).mcp_file.read_text(encoding="utf-8"))

    assert with_config["mcpServers"]["bridge-docs"]["args"] == ["-m", "bridge.docs_mcp"]
    assert "bridge-docs" not in without_config["mcpServers"]


def test_gitops_branch_and_commit_cycle(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    assert gitops.is_repo(repo)
    assert gitops.current_branch(repo) == "main"
    assert gitops.create_session_branch(repo, "bridge/ag-test")
    assert gitops.current_branch(repo) == "bridge/ag-test"
    assert gitops.has_changes(repo) is False

    (repo / "file.txt").write_text("data", encoding="utf-8")
    assert gitops.has_changes(repo) is True
    assert gitops.commit_all(repo, "bridge: snapshot")
    assert gitops.has_changes(repo) is False


def test_launch_creates_session_branch_and_auto_commits(tmp_path, monkeypatch):
    from bridge import launcher

    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BRIDGE_TELEMETRY", "off")
    monkeypatch.setitem(launcher.CLIENTS, "fake", {
        "cmd": [sys.executable, "-c", "import pathlib; pathlib.Path('touched.txt').write_text('x')"],
        "mcp": False, "system_prompt": False, "app": "fake",
    })
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    profile(repo, "root: true\nagent:\n  client: fake\n")

    launch_plan = plan(repo)
    rc = launch(launch_plan)

    assert rc == 0
    assert gitops.current_branch(repo) == f"bridge/{launch_plan.identity.agent_id}"
    assert gitops.has_changes(repo) is False
    log = subprocess.run(["git", "log", "--oneline", "-1"], cwd=repo,
                         capture_output=True, text=True, check=True).stdout
    assert launch_plan.identity.agent_id in log


def test_launch_skips_session_branch_when_auto_branch_disabled(tmp_path, monkeypatch):
    from bridge import launcher

    monkeypatch.setenv("BRIDGE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("BRIDGE_TELEMETRY", "off")
    monkeypatch.setitem(launcher.CLIENTS, "fake", {
        "cmd": [sys.executable, "-c", "pass"],
        "mcp": False, "system_prompt": False, "app": "fake",
    })
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    profile(repo, "root: true\nagent:\n  client: fake\ngit:\n  auto_branch: false\n")

    launch_plan = plan(repo)
    rc = launch(launch_plan)

    assert rc == 0
    assert gitops.current_branch(repo) == "main"
