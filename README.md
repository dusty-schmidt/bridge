# Bridge

Bridge is a standalone Python toolkit for launching coding agents with the profile of the directory where the session starts. It provides inherited workspace settings, trusted agent/session identity, SQLite MCP memory, and versioned activity reporting to the existing NATS fleet bus.

Bridge keeps no source package inside a workspace. A workspace contains only the human-authored `.bridge/profile.yaml` (plus any instruction files it references). A launched session keeps the resolved configuration it started with, so `cd` during the session cannot silently change its memory scope or profile. A child session uses its parent's saved profile and receives a new agent ID with the parent ID recorded.

## Install

Install this repository once on the machine where the agent client runs. With `uv`:

```sh
uv tool install --editable /home/ds/bridge
```

Or with pip in a dedicated virtual environment:

```sh
python -m venv ~/.venvs/bridge
~/.venvs/bridge/bin/pip install --editable /home/ds/bridge
```

Claude Code must already be installed and authenticated. Open a workspace and launch:

```sh
cd ~/work/my-project
bridge config show
bridge launch
```

Pass Claude Code arguments after `--`:

```sh
bridge launch --model opus -- --permission-mode plan
```

The launcher starts Claude Code in the original directory and injects the effective standing instructions from a private session file using Claude Code's `--append-system-prompt-file` option. It supplies the session-specific MCP configuration with `--mcp-config --strict-mcp-config`. The profile's `instructions.files` may point at `AGENTS.md`; Bridge reads and passes that content to Claude Code, whether or not the client itself auto-loads that filename.

## Workspace profiles

Each profile is `.bridge/profile.yaml`. Any directory can declare a root with `root: true`; discovery walks upward from the launch directory and stops at the nearest declared root. Profiles between that root and the launch directory are merged from parent to child. A directory with no `.bridge/profile.yaml` anywhere in its ancestry uses the built-in defaults. A directory that has a profile but no ancestor declares `root: true` is a configuration error and `bridge` exits with a message naming the undeclared profile — it never silently inherits a tree it wasn't told to.

Merge rules:

- Mappings merge recursively by key; child values win.
- Scalars replace parent values.
- Lists replace the entire parent list; they are never concatenated. This includes instruction file lists and MCP server arrays.
- YAML `null` removes that key from the effective mapping.
- `root` is only a discovery marker and is not an agent setting.
- `${ENV_NAME}` is resolved from the launching process environment when a session is prepared. An unset variable makes launch fail. `bridge config show` keeps references redacted and never resolves a secret for display.

Example root profile:

```yaml
root: true

instructions:
  files:
    - AGENTS.md
  text: |
    Keep changes focused. Explain assumptions before changing data formats.

mcp:
  servers:
    issue-tracker:
      type: stdio
      command: npx
      args: ["-y", "@example/issue-tracker-mcp"]
      env:
        ISSUE_TRACKER_TOKEN: "${ISSUE_TRACKER_TOKEN}"

memory:
  backend: sqlite
  scope: root_tree
  top_k: 5
  mcp: true

nats:
  url: nats://100.115.32.6:4222
  stream: BRIDGE
  prefix: bridge

agent:
  client: claude
  model: sonnet
  app: claude-code
  role: developer
  defaults:
    permission_mode: manual
```

A child directory can override only the values it needs:

```yaml
# child/.bridge/profile.yaml
instructions:
  files: [AGENTS.md]  # replaces the parent's file list
agent:
  model: opus
```

Commands:

```sh
bridge config show [-C DIRECTORY]
bridge launch [--model MODEL] [--role ROLE] [--task TASK] [-- APP_ARGS...]
bridge child [--parent AGENT_ID] [--model MODEL] [--task TASK] [-- APP_ARGS...]
```

`bridge child` defaults to the current `BRIDGE_AGENT_ID`, inherits its task and saved profile, and marks the new agent as a subagent through its parent ID. Use it in a shell started by the launched agent, or pass `--parent`. Claude Code's native Task subagents are not separately instrumented by Bridge; their activity remains attributed to the Claude Code session that owns them.

Typical session commands:

```sh
bridge launch
bridge child --task review-follow-up -- --permission-mode plan
bridge status "Reviewing the failing integration case"
```

## Memory MCP

The bundled stdio MCP server exposes `remember`, `recall`, and `forget`. Its stable interface calls a replaceable `MemoryBackend`; SQLite and simple term-overlap text retrieval are the initial implementation. `memory.backend` currently accepts `sqlite`; `memory.top_k` sets recall's default limit (1–25); `memory.scope` accepts `root_tree` or `workspace`. Candidates always remain in the originating workspace. Approval shares within the root tree under `root_tree`; under `workspace`, approved entries remain visible only in the originating workspace. A memory stores content, creation/review timestamps, source reference, task, workspace/tree scope, session, originating agent, tags, and review state.

- New entries are `candidate` and visible in their originating workspace only.
- `flagged` entries remain visible to the originating workspace while awaiting review.
- `approved` entries are shared with all workspaces under the same declared root tree.
- `quarantined` entries are excluded from ordinary recall and remain available to operator review.
- Forget is workspace-local for agent tools; the operator CLI can remove any memory.

Operator review commands:

```sh
bridge memory list [--state candidate] [--state flagged] [--state approved] [--state quarantined]
bridge memory review MEMORY_ID approve
bridge memory review MEMORY_ID flag
bridge memory review MEMORY_ID quarantine
bridge memory forget MEMORY_ID
```

Memory calls are instrumented automatically. Telemetry records operation, IDs/counts, outcome and duration, never memory contents or recall queries.

## NATS activity and collector

Bridge reuses the existing NATS server; it does not start another server. Its defaults target the fleet-bus address (`nats://100.115.32.6:4222`), `BRIDGE` JetStream stream, and `bridge.>` subject namespace. Set `nats.url` in the profile or `BRIDGE_NATS_URL` in the environment. Bridge does not change fleet-bus server configuration.

Events are versioned JSON envelopes with event ID, UTC timestamp, event type, call ID, workspace/session/agent/parent/task identity, role, model, application, profile version, tool, outcome, duration, and structured errors. A tool start and completion share a call ID. Explicit `bridge status "short user-facing progress"` summaries are allowed; Bridge never asks for private internal reasoning. Secret-looking fields are redacted, and full tool arguments/results are excluded.

**Coverage:** Bridge lifecycle and explicit status events, all bundled memory MCP operations, and calls passed through the reusable `bridge.telemetry.wrapper.instrument_call` function are covered. Claude Code native tools, third-party MCP server calls, and native Task subagents are not intercepted automatically. Other tool hosts must call the wrapper themselves.

Run the collector as a foreground process (manage it with your preferred user service manager if desired):

```sh
bridge collector run
bridge collector report
bridge collector report --by model app task tool agent
bridge collector report --since 2026-09-30T00:00:00Z --json
```

The collector persists each event in `~/.bridge/collector.db`, keyed by event ID, and reports tool call counts, errors, and average duration by model, application, task, tool, or agent. It acknowledges JetStream messages only after SQLite commit. Replayed messages are harmless because the event ID is a unique key.

Delivery is at least once: the publisher waits for a JetStream publish acknowledgement, which means the stream has accepted the event durably. If publishing fails or times out, Bridge records it in a local SQLite outbox and starts background retries with exponential backoff; later reporting activity also retries, and `bridge outbox flush` forces an immediate attempt. `bridge outbox status` shows queued events. A local outbox receipt is not a durable NATS receipt. NATS's message ID header reduces duplicate stream writes within the server's duplicate window; collector event-ID deduplication provides exactly-once database effect even if an event is replayed or republished outside that window. Memory operations do not depend on NATS availability.

```sh
bridge outbox status
bridge outbox flush
```

Machine-local state lives under `~/.bridge/` (override with `BRIDGE_STATE_DIR`). Session files, generated MCP configuration, and instruction snapshots are private files. Workspace profiles should contain environment-variable references rather than secret values.

### Secrets

`bridge` auto-loads `~/.bridge/secrets.env` (`KEY=VALUE` per line, `#` comments allowed) into its process environment on every invocation, before resolving any `${ENV_NAME}` reference — no `export` required. A variable already present in the environment always wins over the file. Create it once:

```sh
touch ~/.bridge/secrets.env && chmod 600 ~/.bridge/secrets.env
```

The file is outside any Git repository by construction (it lives under `BRIDGE_STATE_DIR`, not a workspace), so there is nothing to gitignore.

## Verification and known gaps

The focused tests cover profile inheritance/list replacement, per-workspace memory isolation, approval and quarantine visibility, parent/child profile and identity attribution, outbox recovery, event deduplication, and telemetry redaction.

- Claude Code is the only fully integrated client. Its launcher uses `--append-system-prompt-file`, `--mcp-config`, `--strict-mcp-config`, and `--model`; these flags were checked against the installed Claude Code CLI 2.1.283. Claude Code native tools and native subagents do not emit per-call Bridge events; see coverage above.
- Agent Zero has no dedicated launcher adapter yet. It can use the bundled MCP server if configured with the trusted `BRIDGE_SESSION` and `BRIDGE_STATE_DIR` environment, but its tool calls are not reported unless its integration invokes the wrapper.
- The collector is a foreground command; service installation is deployment-specific.
- Memory uses simple lexical retrieval and manual review. There is no automated memory judgment or cross-machine memory replication.
- The fleet-bus listener is currently unauthenticated on the private tailnet. Keep profile credentials in environment variables and do not send secrets in status summaries.

## References

- [Claude Code CLI reference](https://docs.anthropic.com/en/docs/claude-code/cli-usage)
- [Claude Code MCP configuration](https://docs.anthropic.com/en/docs/claude-code/mcp)
- [Official MCP Python SDK](https://py.sdk.modelcontextprotocol.io/)
- [NATS JetStream](https://docs.nats.io/learn/jetstream/)
