# 5. Docs MCP: one generic tool, profile-declared routes

Status: accepted

## Context

Wanted to replace scattered "write X to file Y" instructions in AGENTS.md/CLAUDE.md files with one tool call, without hardcoding every doc type into Bridge itself.

## Decision

One MCP tool, `document(kind, message)`. `docs.routes.<kind>` in the profile maps a kind to a `path` (append/replace) or a `command` (message substituted as one argv token, never shell-interpolated). Unknown `kind` fails loudly, listing known kinds. Relative paths/command cwd resolve against the workspace's `start_dir`, never the running process's cwd.

## Consequences

Adding a doc type is a config edit, not new code. Migrating real AGENTS.md instructions into routes tables is separate follow-up work, not covered here.

Caught during manual verification: the workspace-relative resolution above was originally missing — a relative `path` resolved against the process cwd and wrote into this repo's own `NOTES.md` once before the fix landed. Regression test: `test_docs_relative_path_resolves_against_workspace_not_process_cwd`.
