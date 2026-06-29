# #95 Hub Dashboard polish — design (2026-06-29)

Deepen the Hub fleet dashboard per the V3 UI polish preview: fleet health
summary, 5s auto-refresh, drill-down machine cards, and a tile-based self-monitor.
Issue: [#95](https://github.com/AlvinShenSSW/Taskpaw/issues/95). Tracking spec:
[`2026-06-29-v3-ui-polish.md`](2026-06-29-v3-ui-polish.md). Design refs:
`design-system/taskpaw-v3/pages/hub-dashboard.md`, preview Hub section
(`Fleet health` / `.seg-count` / machine cards), changelist §7.

Touches only `taskpaw_v3/ui/` (V2 frozen). Backend #96 (per-agent snapshot) is
already merged, so the per-server `online` / `last_seen` / `snapshot` fields are
available on `/status`.

## Decisions

### 1. Health signal: use #96's `online`/`last_seen`, NOT `acks` (deviation from the issue)

The issue proposes deriving fleet health from `HubStatus.acks[server_id]`
heartbeat freshness "无需新接口". On inspection that's not viable: `acks` is the
**last-event-id cursor** per server (`poller.snapshot_acks()` →
`last_event_ids`), an integer event id, **not a timestamp** — there is no
freshness to measure from it.

Since the issue was written, **#96 landed** and `/status` now returns, per server,
an authoritative `online: bool` and `last_seen` (the poller's reachability +
last-good-poll time, with disabled servers forced `online=False`). That is the
correct, intended health signal. So we derive health from `online` (+ `snapshot`
for degraded), not `acks`. Still "no new interface" — just a better existing one.

### 2. Three health states

Per machine, from its `/status` server entry:
- **offline** — `online === false` (unreachable or disabled).
- **degraded** — `online === true` AND its `snapshot.monitors` has ≥1 monitor in a
  non-ok state (`state === "alert"` / `degraded === true` / `alive === false`).
- **ok** — `online === true` and no degraded monitors.

Summary bar: `N 台 · 正常 X · 降级 Y · 离线 Z`. Counts are conveyed by a labelled
StatusDot + number (never color-only, a11y §1). When a count is 0 it's dimmed.

### 3. `HubStatus` type — declare the #96 fields (resolves deferred #93 P2)

Extend the `servers` element in `api.ts` to
`{ id, name, ip, port, enabled, online, last_seen, snapshot }` where
`snapshot: AgentStatus | null` (the agent's parsed `/status`). This is the [P2]
contract-drift finding deferred from the #93 Kimi 终审 — fixed here because this
is the file/feature that consumes the fields.

### 4. Auto-refresh

Add `refetchInterval: 5000` to the `hubStatus` `useQuery` (the Agent console
already does this; the Hub didn't). Drill-down events reuse the existing
`hubEvents({ server, limit })` filter, fetched only while a card is expanded.

### 5. Drill-down cards

- Card is a `<button>` (keyboard-focusable, `aria-expanded`); click toggles an
  inline detail panel — no route, no modal.
- Hover lift via `transform: translateY(-2px)` + shadow (GPU transform → **no
  layout shift / reflow**, acceptance requirement), 160ms, degrades under
  `prefers-reduced-motion`.
- Card face: StatusDot(health) + name + `ip:port` + last-seen relative time +
  online/offline chip. **No per-machine CPU/MEM mini-bars** (explicitly out of
  scope until a later iteration — the snapshot has the data but the issue scopes
  cards to online/offline + last-seen).
- Expanded detail: the machine's monitors from `snapshot.monitors` (StatusDot +
  name + detail line), plus its recent events via `hubEvents({ server: id })`
  rendered with the existing `EventLog`.

### 6. Self-monitor → tiles

Replace the `JSON.stringify(snap.metrics)` `<pre>` with
`<MonitorMetrics metrics={snap.metrics} />` — the existing metrics dashboard
(gauges + tiles) already handles host_metrics keys (cpu_pct/mem_pct/…). Reuses the
whole component, not just its Tile.

## Test plan (vitest)

1. Health summary counts correct from mock `servers` (`online`/`snapshot`):
   ok/degraded/offline tallied right; status not color-only (dot has aria-label +
   visible number).
2. `hubStatus` query configured with `refetchInterval: 5000`.
3. Self monitor renders metric tiles (not a raw `<pre>` JSON blob).
4. Card click expands the drill-down detail (machine monitors visible).

## Out of scope

Per-machine CPU/MEM mini-bars on the card face; deep drill-down interactions
(history charts, per-monitor actions). Backend untouched.
