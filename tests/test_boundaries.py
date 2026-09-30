from __future__ import annotations

import json
import os
import stat
import threading
import time

from bridge import identity, secrets
from bridge.collector import store
from bridge.collector.report import query_dimension
from bridge.cli import main
from bridge.launcher import plan
from bridge.memory.backend import Scope, SqliteMemory
from bridge.memory.session import SessionError, SessionMemory, _load_session
from bridge.profile import ProfileError, effective, redacted_view
from bridge.telemetry import Reporter, make_event
from bridge.telemetry import outbox
from bridge.telemetry.wrapper import instrument_call


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
