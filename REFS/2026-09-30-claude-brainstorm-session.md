# Claude Code brainstorm session — 2026-09-30

Context for merging branches: this is one input among "two parallel claude sessions" + "the-net"
artifacts you're consolidating. This session never wrote implementation code — it was pure
brainstorming (architectural path, per the `superpowers:brainstorming` skill) toward a spec, and it
was overtaken mid-conversation by Codex actually building Bridge (see `WORKLOG.md`, `README.md`,
`src/`, `tests/` — 12 tests passing, CLI installed). Treat this file as design-intent + a list of
things to reconcile against what Codex already shipped, not as an authoritative spec on its own.

## The core realization (keep this — it's the load-bearing one)

You'd been mentally blurring **the substrate** (persists independent of any one agent: profiles,
identity, memory, telemetry) **with the agent** (transient, replaceable — Claude Code, Agent Zero)
because an agent framework was your first experience of a substrate at all. That blurring had
concretely leaked into directory structure: agent code and infra code were at risk of ending up in
the wrong repos.

**Directory boundary, corrected and already written to `~/agent-shared/NOTES.md`:**
- `/home/ds/bridge` = the substrate. Standalone, not owned by any one project.
- `~/.GOB` = an agent/consumer of the substrate, same as any other workspace — not its home.
- The one intentional cross-reference: Bridge's telemetry reuses GOB's existing fleet-bus NATS
  server (`100.115.32.6:4222`) as transport. That's a wire, not shared code.

**The-net / "digital shadow" vision, reconciled:** you described wanting agents' traces to linger
in the substrate the way a coworker's presence lingers in an office after they leave — noticed by
others without intent. Conclusion reached: the memory lifecycle already being designed (candidate →
flagged/approved → quarantined, with provenance: who, when, from what task) **is** that residue
mechanism. The substrate doesn't need to model "personality" or "shadow" explicitly — it just must
never destroy provenance or hard-delete instead of quarantine, so a future personality/shadow layer
built on GOB or elsewhere can still read the trail. No new MVP feature was added for this; it's a
constraint on how existing features get built (don't foreclose it).

**Go vs. Python for the substrate:** considered and rejected. The isolation someone was hoping a
language switch would buy is already provided by process boundaries (MCP server and collector are
separate OS processes talking JSON-RPC/NATS, not imports) — a second language adds cost without
adding isolation. Decision: Python throughout, matches what Codex already built.

## Design decisions reached in this session

- **Root declaration:** self-declared roots (`.bridge/profile.yaml` with `root: true`), not one
  global root and not `$HOME`. Matches Codex's implementation.
  - **Open discrepancy:** Codex's README says an unrooted tree silently falls back to "the topmost
    profile found" with only a note in `bridge config show`. This session's owner did not approve
    that fallback — flagged as a silent-behavior risk (a workspace could inherit the wrong config
    without an explicit error). Worth a decision: fail loudly instead, or keep the fallback but
    make it a hard warning/exit code, not just a note.
- **Memory promotion scope:** "approved" memories share within a declared root tree, not globally
  across all Bridge roots on the machine. Matches Codex's implementation.
- **NATS:** reuse the existing fleet-bus server, new `BRIDGE` JetStream stream, new `bridge.>`
  subject namespace, no second NATS instance. Matches Codex's implementation.
- **Secrets:** never inline in profile YAML; `${ENV_VAR}` interpolation resolved at merge time.
  This session additionally proposed (not yet confirmed against Codex's build): a single
  `chmod 600`, gitignored `secrets.env` that Bridge auto-loads at CLI startup, so no `export`
  ever needs to be typed by hand — motivated by the owner's Termux/phone typing constraint (fat
  fingers, no swipe/word-suggestion in Termux's keyboard, avoid multi-step interactive setup).
  Check whether Codex's build already does this or still expects manual env export.
- **Packaging:** one Python package, one CLI entrypoint, internally-isolated modules
  (profiles/identity/memory/telemetry/collector), not split into separate installable services for
  v1. Matches Codex's implementation (`src/`, single `pyproject.toml`).
- **Command taxonomy — NOT yet reconciled with Codex's build.** This session designed a two-family
  split specifically to reinforce the substrate/agent mental boundary:
  - Substrate lifecycle, top-level, present-tense infra verbs: `bridge up` / `down` / `restart` /
    `status` / `build` (build = package as a container image; explicitly a documented gap, not
    built, per "avoid Docker during development").
  - Agent-facing actions, namespaced under `agent`: `bridge agent launch [--parent --task]`,
    `bridge agent config show`, `bridge agent memory {list,approve,flag,quarantine}`.
  - **Codex's actual shipped CLI is flat**: `bridge config show`, `bridge launch`, `bridge child`,
    `bridge memory ...`, `bridge collector run/report`, `bridge outbox status/flush` — no `agent`
    namespace, and no `up`/`down`/`restart`/`status`/`build` lifecycle verbs at all (collector is
    a foreground process today, no service-manager integration).
  - This is the one design point actively in tension with what's built. Owner's stated reason for
    wanting the split was explicitly to protect their own mental model (substrate as "true top
    tier"), so it's worth deciding deliberately rather than defaulting to whichever version is
    already typed.

## Not yet captured to Open Brain

Open Brain (`mcp__open-brain__*`) was disconnected all session (`-32001 Unauthorized`). Nothing
from this session made it into long-term semantic memory. Once auth is fixed, worth capturing at
least: the substrate/agent blurring realization, the directory boundary correction, and the open
command-taxonomy tension above.
