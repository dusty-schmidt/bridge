# Bridge work log

## Goal

Ship a standalone Python CLI that launches Claude Code from inherited workspace profiles, provides scoped MCP memory, and reports activity to the existing NATS fleet bus with an offline outbox and deduplicating collector.

## Decisions

- Bridge is its own repository, independent of `.GOB`; existing unrelated service payloads in this directory are out of scope.
- Reuse the existing NATS server, with Bridge's own `BRIDGE` JetStream stream and `bridge.>` subject namespace.
- Profiles declare their own roots with `.bridge/profile.yaml` and `root: true`.
- Memory approval shares only within a declared root tree.
- One Python package and CLI; Claude Code is the first launch integration.
- Parent/child attribution is explicit through `bridge child`; Claude Code's native Task subagents are not separately instrumented.

## Build sequence

1. [x] Pin profile inheritance, memory isolation/quarantine, outbox recovery, collector deduplication, and identity attribution with focused tests.
2. [x] Finish session identity and Claude Code launcher/config generation; add CLI commands for launch, child launch, config, memory review, outbox, and collector reports.
3. [x] Harden MCP tool call telemetry and collector aggregations; ensure no content/secrets enter routine telemetry.
4. [x] Document install, exact commands, sample profile, delivery semantics, and client gaps in `README.md`.
5. [x] Run focused/full tests and CLI smoke checks, inspect repository status, and record remaining gaps here.

## Progress

- [x] Inspected existing package and confirmed initialized standalone Git root at `/home/ds/bridge`.
- [x] Implementation and boundary tests.
- [x] README and verification.

## Verification

- `.venv/bin/pytest -q`: 12 passed.
- `python -m compileall -q src tests`: passed.
- Editable package install into the repository `.venv`: succeeded; `bridge --help` and `bridge --version` work.
- `uv pip check --python .venv/bin/python`: all 36 installed packages compatible.
- `git diff --check`: clean.
- MCP server without a session file exits with the expected actionable message; covered by a boundary test.
- NATS outage/recovery and deduplication are tested with a deterministic publisher and collector SQLite store. The shared live NATS server was not used during tests.

## Repository boundary

The Git root is `/home/ds/bridge`. Pre-existing `services/`, `stage/`, `.remember/`, `NOTES.md`, archive bundles, and runtime artifacts are ignored and were left untouched; no `.GOB` repository files were changed.
