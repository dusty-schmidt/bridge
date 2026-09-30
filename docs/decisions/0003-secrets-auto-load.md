# 3. Auto-load secrets from a single mode-600 file

Status: accepted

## Context

Profiles resolve `${ENV_VAR}` from the process environment, but nothing populated that environment — every secret needed a manual `export` before `bridge` ran.

## Decision

`bridge.secrets` loads `~/.bridge/secrets.env` (`KEY=VALUE`, `#` comments) into the process environment at the top of `cli.main()`, never overriding a variable already exported.

## Consequences

No `export` needed for routine use. A variable set in the calling shell still wins, so CI/tests are unaffected.
