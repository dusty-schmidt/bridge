# Architecture

Bridge launches an agent client (Claude Code) with directory-scoped config, identity, memory, and activity reporting.

## Flow

profile → identity → launch → (memory MCP, telemetry) → collector

## Components (`src/bridge/`)

- `profile.py` — discovers and merges `.bridge/profile.yaml` up to a declared root.
- `identity.py` — SQLite table of every agent session; parent/child links.
- `launcher.py` — resolves profile + identity into a session, execs the client, owns the git session-branch/auto-commit.
- `gitops.py` — branch/commit primitives used by the launcher.
- `docs.py` / `docs_mcp.py` — routing-table-driven `document(kind, message)` MCP tool.
- `secrets.py` — auto-loads `~/.bridge/secrets.env` into the process environment.
- `paths.py` — machine-local state layout (`~/.bridge/`, override `BRIDGE_STATE_DIR`).
- `util.py` — id generation, redaction, hashing helpers.
- `cli.py` — the `bridge` command.
- `memory/` — SQLite-backed MCP memory server (remember/recall/forget, review lifecycle).
- `telemetry/` — event envelopes, NATS publisher, local outbox for offline retry.
- `collector/` — consumes the NATS stream into a local DB, reports aggregates.

## State

- Per-workspace: `.bridge/profile.yaml` (source of truth for config).
- Per-machine: `~/.bridge/` (sessions, memory, identity, outbox, collector, secrets).
- Shared: fleet-bus NATS server (transport only, not owned by Bridge).
