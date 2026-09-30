# Claude Code session: g0b tidy-up + ~/.GOB decommission, 2026-09-29 → 09-30

Session ran from `~/g0b`. It did implementation and ops work, not design. The detailed docs live in
g0b under `.g0b/DEV/DECOMMISSION/` (`lessons.md`, `preservation.md`, `plan.md`). This file is the
index and the handoff.

## 1. g0b (all pushed; main at e973ebf)
- Pushed the backlog. Fixed `gob stage` so list/run skip `done/` (they now require `-f` and `-x`).
- Gitignored `.g0b/EXAMPLES/*/`, which holds the upstream reference clones.
- Discarded the foreign README (owner's call). Landed the docs reorg: ARCHITECTURE, BRIEF,
  DECISIONS, VISION and IMPORTS moved to `.g0b/DEV/DOCS/`, and README stayed at the root (e9a78de).
- Decommission docs landed as 2f4d562, 8e6b3e1, e79ebc0 and e973ebf.

## 2. Preservation of the GOB build + MAIN/strategist data (verified)
- Stored in `~/gob-preserve/`, written by `preserve.sh` (copy-only, safe to re-run). `MANIFEST` has sha256 lines.
  - `src/gob.bundle`: the git bundle.
  - `images/gob-agent+base.tar.zst`: 2.6G, including the pinned base.
  - `data/{main,strategist,aux,_archive,shared}`.
  - `units/`: copies of retired units.
- Is GOB lighter than stock Agent Zero? Yes at runtime: 43–143 MiB idle against 96 for stock, and CPU-only torch.
  Barely on disk: 10.7 GB against 11.7–12 GB. No in code: 260k Python lines against 159k.
- Rebuild recipe and import formats are in `preservation.md`. Memory is a FAISS + LangChain pickle:
  export it to JSONL and re-embed. aux holds 3 chats found nowhere else. `_archive` holds the only copy of
  the pre-cutover memory areas and of strategist's old A0 state.

## 3. Services retired (disabled, not deleted; undo lines in `~/bridge/decommission.log`)
- Group A: lab-acronyms, lab-chatter, lab-journal, lab-today, lab-heartbeats-mirror, lab-untracked.
- Group B: gob-fallback-notify, gob-usage-tracker, gateway-dash.
- Group D: lab-logs (Vector), lab-logs-prune, lab-trouble.
- Removed as dead: gob-incident-notify.
- Kept at the owner's choice, until bridge has replacements:
  - The tablet voice feed: lab-mood, lab-world, lab-wounds, lab-brain-events, lab-docker-events,
    fleet-bus-spectator.
  - Agent wakes: lab-alerts, fleet-bus-waker.
- Kept because they're load-bearing: gob-api, gob-portal, lab-shared-push, ob-topics/ob-graph, qdrant (Zoo Code).
- Gate after every group: `~/bridge/bin/health`, which checks what each service answers rather than unit state.

## 4. Moved into ~/bridge
- **NATS**: runs from `~/bridge/services/nats/` as `nats.service`. fleet-bus-nats is disabled. 7 clients reconnected,
  and JetStream kept 2 streams / 10,412 msgs.
- **Open Brain**: code, config and pgdata now live in `~/bridge/services/openbrain/`. The owner ran the cutover
  (brain_events 31904, thoughts 401, archive 8). Rollback is `~/bridge/stage/done/ob-cutover.sh --rollback`.
  `ob-postgres-old` is kept, stopped.

## 5. Agent Zero fixes (gob-main, backups beside every edited file)
- **Codex/ChatGPT 502**: the cause was a blank `codex_version` in `_oauth/config.json`. It's now set to `0.155.1`.
- **Model choice simplified**: `_model_config/presets.yaml` now has one preset per chat model, named after the model.
  Utility and embedding are always local. Root complaint: A0 lets ~6 layers pick the model. The rule going forward
  is to pick the model in one place, in the app (see `lessons.md`).
- **"Falling back to utility model" / Open Brain errors**: the OB key was stale in 4 places: each agent's `.env`,
  each agent's `settings.json` MCP header, and `~/.claude.json`. All were updated for main, strategist and research;
  MAIN loads 11 OB tools again. OB answers a bad key with **HTTP 200** and a JSON-RPC "Unauthorized" body,
  so check the body, not the status code.
- A stray stale-key request at 11:40:18Z came from another Claude Code session that had read `~/.claude.json`
  before the update. It's harmless and goes away once that session restarts.
- **Owner rule for re-hosting**: pass secrets as env vars (`OB1_KEY`) from one mode-600 env file
  (`~/bridge/secrets/agents.env`). Never paste the literal value into configs.

## 6. Still open
- OpenRouter has no credits, so glm-5.3 through the gateway returns 402 (owner: top up or drop it).
- MAIN has 7 errored scheduler tasks, and they haven't been investigated.
- The remote `vector-remote` shippers still run on other hosts. Each buffers up to 512MB and then drops; stop them per host.
- `docker rm ob-postgres-old` once OB has proven stable for a few days.
- Phase 3, still to move: gateway (+db), registry, qdrant, restic-rest, uptime-kuma, projects-sync.
- Phase 4: repoint the symlinks and Claude config away from `~/.GOB`.
- Phase 5: the fleet (owner). Phase 6: registry GC. Phase 7: rotate keys, archive, remove.
- The `~/.GOB` commit 0722bc78 (gitignore of `ELEVEN_LABS_KEY.md`) is local only, not pushed.
