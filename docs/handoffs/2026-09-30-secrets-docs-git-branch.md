# Handoff — 2026-09-30

## What shipped
- Secrets auto-load: `~/.bridge/secrets.env` loaded at CLI startup, env wins over file — `docs/decisions/0003`.
- Undeclared-root profiles fail loudly instead of silently falling back — `docs/decisions/0001`.
- Command taxonomy ratified as shipped, flat, no `agent` namespace — `docs/decisions/0002`.
- Per-session git branch (`bridge/<agent_id>`) + auto-commit at session end, no gate — `docs/decisions/0004`.
- Docs MCP tool `document(kind, message)`, profile-declared routing table (file or shell command targets) — `docs/decisions/0005`.
- Docs restructured to README + Keep-a-Changelog `CHANGELOG.md` + `docs/ARCHITECTURE.md` + `docs/decisions/` ADRs.
- Removed stray tracked `AGENT.md.save`.

## Bug caught and fixed
`SessionDocs._write_file` resolved relative `path` routes against the process's cwd, not the workspace. During manual verification this wrote a demo line into this repo's own `NOTES.md`. Fixed: resolution now goes through the session's `start_dir`. Regression test: `test_docs_relative_path_resolves_against_workspace_not_process_cwd`. Detail in `docs/decisions/0005`.

## Decisions made without re-asking
User told me to stop rabbit-holing on the docs-routing tool design and just decide. Landed on: one generic `document()` tool, profile-declared routes, `path` or `command` targets, fail loud on unknown kind. Also decided root-fallback and command-taxonomy discrepancies unilaterally per explicit "make the call" instruction.

## Left alone, on purpose
Four root-level artifacts predate Bridge and belong to a separate, unfinished GOB-decommission process: `decommission.log`, `bin/health`, `the-net-docs.zip`, `the-net-history.bundle`. Investigated each (tail/head/unzip -l/git bundle verify) — none safe to delete without that process's owner confirming.

## Known gaps / not done
- `bridge launch --dry-run` never calls `ids.record()`, so a dry-run agent_id can't be used as a `bridge child --parent`. Not fixed, just disclosed.
- "Projects directory" isolation (agents on end-user work should have zero visibility into GOB/Bridge infra) — not designed. Leaning toward a scoped filesystem MCP server (Desktop Commander) over bind mounts. Logged in `NOTES.md`.
- "Once stable, start a dev branch to persist in parallel" — distinct from the per-session `bridge/<agent_id>` branches, not built. Logged in `NOTES.md`.

## Style note for future docs in this repo
Keep it to bullets. No rationale essays, no code examples in top-level docs, no directory listings deeper than root — that's an explicit standing preference, not a one-off.
