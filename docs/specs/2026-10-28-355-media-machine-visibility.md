# Media work per machine for OpenClaw (#355)

## Evidence

After #352 the operator's OpenClaw (SunnyPig, reading `~/.taskpaw-hub/status.md`
as the original root guide's "Option 1" told it to) still could not see any
media-center progress. A read-only capture of BlackGoldPig's `/status`
(Agent 3.11.1, 2026-10-28 13:51 JST) shows the source works: `workflow` carries
counts and four `currents` — local ASR plus Jasna/LTX restores on Blacksilverpig,
Bushpig and DarkbluePig with stage, percent and remaining time.

Feeding that capture through the current `render_status_md` gives:

- every GPU machine's section lists host metrics and the legacy monitors
  `LADA` / `Jasna` / `Translate` (type `lada` / `jasna` / `avsubs`) as
  `disabled`. Media-center work does not run through those monitors, so a reader
  learns "Jasna is disabled" on the machine that is running Jasna;
- the live work appears only in one trailing `## 媒体处理` block under the
  controller, detached from the machines it runs on;
- a GPU machine's own `/status.workflow_worker` is never rendered;
- the LTX line drops the remaining time the source progress carries
  (`剩余 6:33:54`);
- the controller's `FleetLive` already holds each worker's heartbeat (online /
  lost, GPU use, current stage, staged films, transfers), but the summary does
  not carry it, so a worker that is not a registered Hub server (Bushpig) has no
  online/idle state at all;
- #352's `films` / `dispatch` / freshness fields are in no released version
  (no version was allocated), so the controller still runs 3.11.1.

SunnyPig itself was not reachable (SSH key denied); its Hub version, polling and
actual OpenClaw reader remain unverified and are reported to the operator.

## Frozen contract

C1. Controller summary (`workflow_events.py`): in remote mode (the host has a
fleet) add `workers` (≤ 16, name order), `workers_total`, `workers_more`. Each
row: `name`, `state` (`online` / `lost` / `waiting`, controller clock),
`heartbeat_age_s`, `paused`, `admission`, `gpu` (`name`, `util`, `used_mib`,
`vram_mib`, `power_w`, `hold`), `current` (`film` display name, `stage`,
`phase`, `progress`, `ltx` stage fields) or null, `staged` count and
`transfers` (≤ 8: `film`, `dir`, `percent`, `rate`). The controller and
retired / revoked workers are excluded. No ids, attempt ids, addresses, paths,
tokens or actions. Text bounded to 400 chars. Single-machine mode omits the keys.

C2. Hub `status.md`: a fresh, valid controller source indexes its `currents` and
`workers` by machine name (case-insensitive; `本机` is the controller's own
server). The OpenClaw readers named in `status_md.py` (`idle-detector-v2.py`,
`daily-report.py`) parse `- <monitor>: <state>` lines, so each matching server
section gets a monitor-style line in the running-monitor shape
`- 媒体处理中心: running | <film> | <stage · LTX/progress · 剩余 · 已用>`, or
`idle（无运行任务）` / `lost（失联 · 心跳 Ns 前）` from its worker row, plus an
indented `  媒体节点: …` detail (state, pause, GPU, VRAM, power, prefetch,
transfers; GPU only while online). The controller's section gets
`  媒体主控: <dispatch> · 详见「媒体处理」` and its local task line. A server that
only reports `workflow_worker` (no fresh controller row for it) renders
`- 媒体处理中心: <state>（本机自报 · …）`. An unavailable/stale controller source
never produces per-machine live claims.

C3. On a server that takes part in media work (it reports `workflow` or
`workflow_worker`, or is indexed by C2), a `disabled` legacy `lada` / `jasna` /
`avsubs` monitor reads
`disabled（旧版独立监控未使用；媒体任务见「媒体处理」）`.

C4. `## 媒体处理` adds a `GPU 节点 shown/total` list from `workers` with omissions,
and LTX progress keeps the source's `剩余 h:mm:ss`.

C5. Compatibility: 3.11.1 summaries (no `workers` / `films` / `dispatch`) still
render as today plus C2/C3 from `currents`; offline / stale / malformed data keep
the existing unavailable/unknown wording; existing fields, events, SQLite schema
and ack/outbox are unchanged; no new threads, network calls or config keys.

C6. `docs/guides/openclaw-integration.md` documents `workers` and the new
status.md lines; release 3.11.1 → 3.11.2 (version files + CHANGELOG).

## Non-goals

Hub GUI changes; executing/approving/reassigning from the Mac; PR #327 packaging;
deploying the controller or the Hub.

## Tests

RED first: observer `workers` (online with GPU + current, lost, retired excluded,
17 workers bounded, no ids/addresses); status.md with the real 3.11.1 capture
shape (per-machine lines, case-insensitive match, legacy annotation, ETA), a
`workers` summary with an unregistered node, stale/offline controller suppressing
per-machine live lines, `workflow_worker`-only server, multiline injection.
