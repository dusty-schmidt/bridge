# Changelog

Format: [Keep a Changelog](https://keepachangelog.com/). Rationale for each decision lives in `docs/decisions/`, not repeated here.

Versioning: [SemVer](https://semver.org/). Release with `scripts/bump_version.py [major|minor|patch]` — it bumps `pyproject.toml`, promotes `[Unreleased]` to a dated section here, commits, and tags.

## [Unreleased]

## [0.0.1] - 2026-09-30

### Added
- Auto-loaded secrets, `~/.bridge/secrets.env` — `docs/decisions/0003`.
- Per-session git branch + auto-commit — `docs/decisions/0004`.
- Docs MCP tool, `document(kind, message)` — `docs/decisions/0005`.
- `docs/` structure (architecture doc + decision records) replacing this file's former name, `WORKLOG.md`.

### Changed
- Undeclared-root profiles fail loudly instead of falling back silently — `docs/decisions/0001`.
- Command taxonomy ratified as shipped (flat, no `agent` namespace) — `docs/decisions/0002`.

### Fixed
- Docs MCP relative paths resolved against the process cwd instead of the workspace; now resolve against the session's `start_dir`.

### Removed
- Stray tracked `AGENT.md.save` artifact at repo root.

## 2026-09-30 — initial build

- Standalone CLI: profile inheritance, session identity, Claude Code launcher, SQLite MCP memory (candidate/approved/quarantine lifecycle), NATS telemetry with offline outbox, collector aggregation.
- 12 tests passing at initial verification; editable install confirmed (`bridge --help`/`--version`).
- Repository boundary: the git root is `/home/ds/bridge`. `services/`, `stage/`, `.remember/`, `NOTES.md`, archive bundles under this root predate Bridge and are unrelated GOB-decommission artifacts, gitignored and left untouched.
