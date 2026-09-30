# 4. Per-session git branch, auto-commit, no gate

Status: accepted

## Context

Wanted every top-level `bridge launch` to work isolated from the branch you started on, without losing uncommitted work at session end.

## Decision

`bridge launch` (not `bridge child`) checks out `bridge/<agent_id>` off the current branch. Session end auto-commits any uncommitted changes to that branch, no confirmation gate. Never pushes, never merges. Opt out per profile with `git.auto_branch: false`.

## Consequences

Zero risk to the branch you were on — commits land only on a branch Bridge created and owns, fully reversible by deleting it. Merging or pushing that branch anywhere stays a manual, separate action.
