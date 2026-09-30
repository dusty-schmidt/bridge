# 1. Undeclared root fails loudly

Status: accepted

## Context

A directory with a `.bridge/profile.yaml` but no ancestor declaring `root: true` used to fall back silently to the topmost profile found, noted only in `bridge config show`.

## Decision

`profile.chain_for` raises `ProfileError` instead, naming the directory and telling the fix.

## Consequences

A workspace can no longer silently inherit the wrong tree's config. No profile anywhere is unaffected (built-in defaults, no error).
