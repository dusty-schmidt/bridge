# 2. Flat CLI, no `agent` namespace, no lifecycle verbs

Status: accepted

## Context

A parallel brainstorm session designed a two-family taxonomy: substrate lifecycle verbs (`up`/`down`/`restart`/`build`) at top level, agent actions namespaced under `agent`. The implementation shipped flat: `launch`/`child`/`config`/`memory`/`outbox`/`collector`.

## Decision

Keep the flat taxonomy. Do not add the `agent` namespace or the lifecycle verbs.

## Consequences

The proposed lifecycle verbs describe features that don't exist (no service manager; collector is still foreground) — adding the namespace now would rename nothing into existing. Revisit only when those features are actually built.
