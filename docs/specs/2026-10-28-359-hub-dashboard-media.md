# Hub dashboard: media-center work per machine (#359)

## Evidence

On a 3.11.3 fleet the operator's Mac Hub 机群 tab shows no media-center work,
and none of the GPU Agents' Jasna/LTX progress.

- `HubDashboard.tsx` renders `snapshot.monitors` only. The Hub's `/status`
  already carries each Agent's whole parsed `/status` as `snapshot`. That
  includes the controller's `workflow` summary (`counts`, `dispatch`,
  `currents`, `workers`, #351/#355) and a worker's own `workflow_worker`. The
  UI reads neither.
- On a media machine the legacy `LADA` / `Jasna` / `Translate` monitors are
  disabled, so the row reads "Jasna stopped" on a machine that is running
  Jasna. #355 fixed this for `status.md` only; the GUI was a non-goal there.
- The summary's `ltx` keeps `stage_index/stage_total/stage_name/percent/mode`.
  It drops the bounded tqdm tokens that `ltx_progress` already carries
  (`percent_text`, `current/total`, `rate_value/rate_unit`, `elapsed/eta`,
  `run_eta`), which are what make "Jasna detailed progress" useful.

## Frozen contract

D1. Agent summary (`workflow_events._current`): `ltx` also copies `percent_text`,
`current`, `total`, `rate_value`, `rate_unit`, `elapsed`, `eta`, `run_eta`
when present. `raw_line` and everything else stay out. This applies to
`current`, `currents[]` and `workers[].current`. It adds no new keys
elsewhere.

D2. Hub UI source selection (`hubMedia.helpers.ts`): every server whose
snapshot has a `workflow` key is a candidate; the source is the first
*current* candidate. A GPU worker also carries the key (`null`, next to its
`workflow_worker`, `launcher.py`), so when none is current the first candidate
without a `workflow_worker` key (null counts) gives the unavailable reason, and a fleet of workers
alone has no source (internal review P1-1). A candidate is *current* only when
the Hub response is current and `hubServerHealth` is ok/degraded (online,
enabled, sample `ok`), `workflow` is an object carrying counts, a current film
or currents, and `sample_age_seconds`, when present, is a finite 0..15.
Otherwise it is unavailable with a labelled reason (stale / offline / not
running / sample expired) and never produces live claims.

D3. Machine index: server keys are `server.name` and `snapshot.machine`,
trimmed, case-insensitive. From a current source, `currents[]` (≤16) are
indexed by `machine` (`""`/`本机` → the controller's own keys) and `workers[]`
(≤16) by `name`.

D4. The 机群 tab already sits beside the read-only 媒体处理 tab (#286), so no
separate card is added. Each online `MachineRow` gets a flush 媒体处理 block
from the index: its tasks (or the worker row's own current
when the bounded `currents` left it out — a lost node's retained heartbeat is
not live work; a lost/waiting worker row hides even indexed tasks, review P1-2 + Codex r2) with film · stage chip · compact LTX progress (stage
x/4 name, percent bar, frames, rate, stage elapsed/ETA, run ETA via
`readLtxProgress`) or the progress text; else idle, or the lost/waiting state
from its worker row; and its node line (state, heartbeat age when not online,
paused; GPU util/VRAM/power, prefetch and transfers only while online, GPU
values range-checked as in status.md).

D5. The controller's row adds dispatch, group counts and every GPU node from
`workers` (with that node's own current task) and omission notes, so a node
the Hub does not poll is still visible; a controller without `workers` lists
its remote running tasks instead. An unavailable source shows only its reason
on the controller's row. A machine with no index entry but its own
`workflow_worker` shows the self-report (service state, connected, current
stage/phase; a non-busy `running` with no current reads idle). On a media
machine (indexed, or reporting `workflow`/`workflow_worker`) a `lada`/`jasna`/
`avsubs` monitor with `enabled === false` gets the caption
旧版独立监控未使用 · 媒体任务见「媒体处理」. Offline rows stay header-only.

D6. Compatibility: 3.11.1–3.11.3 summaries (no `workers`, or a 5-key `ltx`)
render with what they have; malformed values are ignored, never thrown. React
renders all text as text. Nothing new is polled: no new requests, routes,
config, or Hub server changes. status.md is unchanged.

D7. Docs: the design page `hub-dashboard.md` gains the 媒体处理中心 rule;
`openclaw-integration.md` lists the added `ltx` tokens; CHANGELOG; release
3.11.3 → 3.11.4.

## Non-goals

Mutating controls from the Hub; status.md; showing offline rows' media lines;
deploying the controller or the Hub.

## Tests

RED first:
- Python: D1 tokens appear in `currents[].ltx` and `workers[].current.ltx`, and
  `raw_line` does not.
- Vitest (helpers):
  - source freshness (offline, stale health, expired sample, malformed);
  - case-insensitive index with `本机`;
  - worker fallback for an omitted current.
- Vitest (dashboard):
  - a GPU row shows its LTX stage/percent/frames/ETAs and its node line;
  - the controller row lists dispatch and the GPU nodes, including an unpolled one;
  - the legacy Jasna caption;
  - a stale source makes no live claims;
  - the self-report row;
  - a 3.11.3-shaped summary still shows the progress text.
