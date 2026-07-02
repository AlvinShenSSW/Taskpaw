# #161 — Surface all Lada per-task progress metrics to the Hub (openclaw self-select)

Date: 2026-07-02
Issue: #161
Driver: `/afk` (Claude implement → Codex 外门 → Kimi 终审). Merge policy: leave-open.

## Problem

Lada (capture mode) already parses rich per-task progress in
`parse_progress_line` — `percent`, `elapsed`, `processed_frames`, `eta`,
`remaining_frames`, `fps`, `current_file` — and merges them into
`MonitorStatus.metrics` while `running`
([lada.py `_build_status`](../../taskpaw_v3/monitors/plugins/lada.py)). Because
`metrics` is an open `dict[str, Any]` serialized verbatim into `/status`
([supervisor.py:460](../../taskpaw_v3/monitors/supervisor.py)), the Hub already
stores every one of these fields in `hub.db.status_log.status_json`. **The
transport is complete; nothing on the wire needs to change.**

The gap is *discoverability on the consumer side*:

1. `docs/guides/openclaw-integration.md`'s field table documents only the Lada
   `queue_*` fields and `current_file`. An openclaw reader following the doc has
   no way to know `percent` / `eta` / `elapsed` / `processed_frames` /
   `remaining_frames` / `fps` exist — so the data is present in the JSON but
   never selected.
2. `status.md` (the secondary, human/regex-scraper view) renders only
   `X/Y done (Z left) | file |` for Lada — the per-task progress is invisible
   there.

Goal: **formally deliver these fields to the Hub side** — lock the metric
contract with tests, render the essentials in `status.md`, and document every
field so openclaw can self-select. No behavioural change to the agent→hub wire.

## Non-goals

- No new metrics parsing in `lada.py` (all fields already emitted). We only add
  a **contract test** to prevent silent regressions of the field names/types.
- No protocol / hub-store changes. `metrics` already flows verbatim.
- No change to hub.db (it already carries the full blob).

## Design

### 1. Lock the metric contract (test-only, `test_lada.py`)

Add one capture-mode test that feeds a realistic English tqdm progress line and
asserts the running snapshot's `metrics` contains **all** per-task keys with the
right types:

- `percent: int`, `processed_frames: int`, `remaining_frames: int`
- `fps: float`
- `elapsed: str`, `eta: str`, `current_file: str`

This is the guard that keeps the fields available to the Hub/openclaw; it does
not add behaviour.

### 2. Render per-task progress in `status.md` (`status_md.py`)

Extend the existing Lada block in `_status_text` (currently only queue counts +
current file). After the `X/Y done (Z left) [| file |]` segment, append the
per-task essentials as a trailing, additive suffix:

```
- LADA: 5/10 done (5 left) | clip.mp4 | 47% · ETA 30:47 · 112fps
```

Rules (parity with the existing guards):

- **Backward-compatible.** The `X/Y done (Z left)` and `| file |` substrings stay
  byte-identical, so the V2 regex scrapers (idle-detector-v2 / daily-report)
  keep parsing. The per-task suffix is *appended* to the same single `parts`
  entry — never a new pipe-joined part — so no `| |` double-pipe and the
  filename-between-pipes pattern is undisturbed.
- **Each field guarded independently** with `_is_num` (percent, fps) — a `NaN` /
  `"n/a"` / missing field is simply omitted, never `"nan%"`. `eta` is a string:
  render only when it's a non-empty str, and `_inline()`-sanitize it (defence in
  depth — the parser already restricts it to `[\d:]`) so it can't inject lines.
- **`bad_state` guard reused.** The suffix is inside the existing
  `is_lada and not bad_state` block, so an `error`/`stopped` Lada shows its state,
  not stale progress.
- **Only when present.** With capture off (default) none of these fields exist →
  the line is exactly today's `X/Y done (Z left) [| file |]` (no regression).
- **`percent` sanity.** Render only when `0 <= percent <= 100` (an out-of-range
  int from a garbled line is dropped), formatted `{percent:.0f}%`.
- Separator within the suffix is `·` (matches lada.py's own `_detail`), joined
  only from the fields that are present (e.g. `| clip.mp4 | 47%` when fps/eta
  absent).

Fields chosen for `status.md`: `percent`, `eta`, `fps` — the eyeball essentials.
`elapsed` / `processed_frames` / `remaining_frames` stay hub.db-only (documented)
to keep the human line short; openclaw reads the DB for the full set.

**Decoupled from queue counts (Codex 外门 finding).** A managed Lada may supply
I/O via `lada_extra_args` (`--input X --output Y`) instead of the folder fields;
the config validator accepts that, but `_queue_counts()` reads only the folder
fields, so `queue_total` is absent while capture-mode progress is present.
Progress rendering must therefore NOT be nested under `queue_total` — the Lada
line is assembled from three independent, space-joined pieces (queue seg, current
file, progress), each emitted only when its data is present. When all three are
present the output is byte-identical to the queue-first layout; when only progress
is present it still renders (instead of falling back to bare `running`).

### 3. Document every field (`openclaw-integration.md`)

- Extend the reader example's `lada` branch to pull the new fields via the
  existing `num()` / `.get()` helpers.
- Extend the field-reference table with `percent`, `elapsed`, `eta`,
  `processed_frames`, `remaining_frames`, `fps`, each marked **capture-mode
  only** (`lada_capture_progress: true`); note that `queue_*` + `current_file`
  are available without capture.
- Update the `status.md` sample block to show the new Lada line.

## Tests

- `test_lada.py`: metric-contract test (all per-task keys + types present in a
  running capture snapshot). RED first (assert new keys) — expected to pass since
  the fields already exist; it is a *lock*, so also assert the exact key set to
  catch a rename.
- `test_status_md.py`:
  - per-task fields render as `… | file | 47% · ETA 30:47 · 112fps`.
  - capture-off Lada (only `queue_*`) renders byte-identical to today (no suffix).
  - `NaN`/`"n/a"` percent & fps omitted; out-of-range percent dropped; the queue
    segment still renders.
  - `eta` containing control chars is sanitized (no line injection).
  - `bad_state` (error) Lada with stale progress shows state, not progress.

## Risks / mitigations

- **Breaking V2 scrapers.** Mitigated by keeping the existing substrings
  byte-identical and only appending — covered by a regression test asserting the
  legacy `X/Y done (Z left)` / `| file |` still present.
- **Injection via `eta`.** Mitigated by `_inline()` + the parser's `[\d:]`
  restriction; test covers a control-char eta.
- **Field drift** (a future lada.py rename silently dropping a field from the
  Hub). Mitigated by the exact-key-set contract test.
