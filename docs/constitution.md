# TaskPaw Constitution

Non-negotiable rules every change (human or agent) is checked against. Keep this
short; if a rule needs paragraphs of justification it belongs in a design doc.
The AFK / review gates self-check against this file — a violation is a blocker.

## 1. Scope discipline

- **V2 is frozen.** `taskpaw.py`, `taskpaw_hub.py`, `macsubs.py` accept only
  critical bug fixes until V3 ships (operator decision §9.6 of the V3 design).
  No new features, no refactors-for-taste in V2.
  - **Documented exception — issue #14 (event-delivery optimization).** The
    operator explicitly chose to implement #14's clear-on-ack + Hub→OpenClaw
    outbox + additive event fields on the **existing V2 code** (not the V3
    monorepo), because #14 precedes the V3 backend (#15) in the dependency order
    and §10 #1 scopes it as optimizing the *current* protocol implementation.
    These changes are backward-compatible. V3 inherits them. This is the only
    sanctioned V2 feature work; it does not reopen V2 for anything else.
- **V3 is greenfield** and lives under `taskpaw-v3/` (a monorepo, not yet
  created). New capability goes there, per
  [docs/specs/2026-06-27-taskpaw-v3-design.md](specs/2026-06-27-taskpaw-v3-design.md).
- **Never widen an operator-given scope.** In AFK mode, touch only the issues /
  files handed to you. Out-of-scope work → stop and report, don't do it.

## 2. Security invariants (apply to V2 and V3)

- **No `shell=True`** with any user-influenced input. Use `shlex.split()` +
  `shell=False`.
- **Atomic writes** for any file another process reads (config, `status.md`,
  state): write `*.tmp` then `os.replace()`.
- **No secrets in argv or logs.** Tokens/keys come from config files or env only.
- **Network-facing HTTP requires auth.** An agent's `/status` and `/events` must
  be Bearer-gated when a token is set; a `401` must **not** clear the event
  queue. Local control APIs (start/stop, edit config) bind loopback only and are
  never exposed to the network/Hub.
- **No public/WAN exposure.** LAN + per-agent Bearer is the trust boundary; do
  not bind a reachable port to the internet.

## 3. Cross-machine contracts

- **Event delivery:** monotonic `id` per agent, persisted across restarts; Hub
  dedupes by `last_event_ids`. Do not break this wire shape — V3 only *adds*
  optional fields (`level`/`title`/`data`) and changes queue-trim timing
  (clear-on-ack), never the endpoint or required fields.
- **Ports:** V2 agent `5678`, MacSubs `5679` (being retired), V3 agent `5680`.
  A new component must not silently collide; detect port-in-use and fail loudly.
- **Timezone:** pick one authority (Hub local time) and convert on ingest; never
  compare timestamps from different machines lexically.

## 4. Reliability

- No silent `except: pass`. Catch the specific exception and log it; for monitor
  loops also surface an error state, don't just keep reporting "idle".
- Long-running loops use wallclock/`monotonic()` scheduling, not sleep-counting.
- Clean shutdown is mandatory: stopping must join threads, terminate managed
  child processes (e.g. `lada-cli`), and release ports — no zombies, no orphan
  port holders.

## 5. Testing & change discipline

- Every behavioural change ships with a test. Tests run via
  `uv run pytest` and must pass before a PR is opened.
- The lockfile is authoritative: `uv lock --check` must be clean.
- Never merge red CI or an unresolved review finding. Merge ≠ deploy — the
  operator pulls and restarts.

## 6. Review discipline (AFK)

- The reviewer is always a *different* model than the implementer. Default
  `/afk`: Claude implements → Codex 外门 → Kimi 终审. Never let a model review its
  own work.
- A degraded review (one external reviewer down) is allowed but **must be flagged
  to the operator**, never silently dropped.
