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

## 2026-09-30 follow-up: secrets auto-load

Reconciled a gap flagged in `REFS/2026-09-30-claude-brainstorm-session.md` between the brainstorm
design and what had shipped: profiles resolved `${ENV_VAR}` from the process environment, but
nothing populated that environment, so every secret still needed a manual `export`. Added
`bridge.secrets` (`~/.bridge/secrets.env`, mode 600, `KEY=VALUE`, comments with `#`) auto-loaded at
the top of `cli.main()`, never overriding a variable already exported. Covered by three new tests in
`tests/test_boundaries.py`; `.venv/bin/pytest -q` — 15 passed. Documented in `README.md` under
"Secrets".

## 2026-09-30 follow-up: resolved the two open design/build discrepancies

Both remaining items flagged in `REFS/2026-09-30-claude-brainstorm-session.md` decided (owner had no
standing preference, asked for a decision):

- **Root fallback → fail loudly.** `profile.chain_for` now raises `ProfileError` when a directory has
  a `.bridge/profile.yaml` but no ancestor declares `root: true`, instead of silently using the
  topmost profile found with just a note. Reasoning: a workspace inheriting the wrong tree's config
  with no error is worse than a one-line failure naming the fix. Two new tests
  (`test_profile_without_declared_root_fails_loudly`, `test_no_profile_anywhere_uses_defaults_without_error`);
  README's profile-discovery paragraph updated. `.venv/bin/pytest -q` — 17 passed.
- **Command taxonomy → keep Codex's flat CLI, no `agent` namespace, no lifecycle verbs.** The
  brainstorm session's proposed `bridge up/down/restart/build` describe features that don't exist yet
  (no service manager; the collector is still a foreground command) — adding that namespace now would
  be a rename with nothing behind it. Ratifying the shipped, tested, documented flat taxonomy
  (`bridge launch/child/config/memory/outbox/collector`) is the call that avoids building ahead of
  need. No code change; this is the taxonomy decision recorded.

## Repository boundary

The Git root is `/home/ds/bridge`. Pre-existing `services/`, `stage/`, `.remember/`, `NOTES.md`, archive bundles, and runtime artifacts are ignored and were left untouched; no `.GOB` repository files were changed.
