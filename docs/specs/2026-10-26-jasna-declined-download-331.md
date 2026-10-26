# #331 — Jasna: a declined LTX model download fails the LTX films and lets the queue continue

Date: 2026-10-26 · Base: `cb9aca4` (main, 3.10.6) · Branch: `afk/jasna-declined-download-331`
Planner: Claude (driver). Implementer: Claude Opus 5.5. Gate: Codex astra xhigh.

## Spec review

In the Jasna monitor (`taskpaw_v3/monitors/plugins/jasna.py`, `JasnaInstance`), a
film on an LTX tier whose model files are missing makes `_launch_locked` call
`self._download.request(...)` and return `False`; the film stays at
`_pending[0]`. The operator's decline (`respond_prompt(request_id, False)`) sets
the shared `DownloadSession.status["state"] = "paused"`. `_launch_locked` refuses
every launch while `self._download.status` is non-empty, so **no film launches
again**, including films on non-LTX tiers; `_maybe_done` never fires because
`_pending` is not empty; the task shows "LTX model download: paused" until Stop.

The `paused` state itself is shared with the media workflow
(`taskpaw_v3/workflow/local_host.py`, `ltx_download` / `respond_ltx_download`),
where it is correct: there each missing-model film is parked in attention by
`exec_restore` and the offer may be resumed later. So `DownloadSession` does not
change; only the Jasna monitor's reaction to a decline does.

Core need: a decline must not freeze the queue. Decision (issue, operator): the
film that needed the model is settled as failed with reason `ltx_model_missing`,
every later LTX film of this run with missing files fails the same way without a
second prompt, and the queue continues. No resume-later state in the monitor.

## Acceptance criteria

- AC1 After a decline, `self._download.status` is empty (the session is stopped
  with `stop(0)`, which only clears the request because no child exists),
  `_ltx_declined` is set for the run, and `_advance_requested` is set so the
  next `check()` dispatches (design v2, critic C1: nothing else re-dispatches
  after a decline).
- AC2 On the next check the declined film is removed from `_pending`, counted in
  `_failed`, marked in the tracker when the film has a subtitle job
  (`_mark_restore_failed`; with `av_translate` off the tracker holds no entry,
  critic C2), logged `restore.skipped` with `data={"reason":
  "ltx_model_missing"}`, its subtitle job (if `av_translate`) settled
  `skipped`/`restore_failed`; the GPU hold is given back (returning `False` from
  `_launch_locked` already does that); `_advance_requested` is set again so the
  `_dispatch` loop walks on to the next film **on the same check**.
- AC3 Any further LTX film of the same run with missing model files is failed the
  same way on that same check, with no new download request and no second
  prompt; the first non-LTX film launches on that check (or `done` fires when
  nothing is left). The GPU lease is taken once per film attempt as for any
  launch, so a GPU held by another task defers the skip like a launch.
- AC4 `_maybe_done` fires once the remaining films finish (declined films count
  as failed in `task.done`).
- AC5 One alert per run, emitted by `_skip_ltx_declined` on the first skipped
  film and guarded by `_ltx_declined_alerted` (reset in `start()`, the same
  pattern as `_ltx_model_alerted`; `respond_prompt` runs on the API thread and
  has no emitter, so the alert cannot be raised at the decline itself — design
  v4). Dedupe key `<id>:ltx:declined`, copy: the LTX films of this run are
  skipped (`ltx_model_missing`); start the task again to be offered the
  download. `_alert_ltx_model`'s copy no longer says the film "stays queued
  until the download finishes" unconditionally.
- AC6 `_ltx_declined` resets on `start()`. A new Start with files still missing
  offers the download again (existing behaviour).
- AC7 `test_missing_model_waits_without_consuming_films_or_gpu` is updated to the
  new contract; new tests cover AC2-AC6 (AC5: exactly one `j1:ltx:declined`
  alert after the first check; two in total after restart, second decline and
  check). Full suite, lint, mypy green.

## Frozen issue contract

- Invariants kept: `DownloadSession` API and the workflow's use of `paused`
  unchanged; GPU lease accounting unchanged (fail happens inside
  `_launch_locked` under the hold, which `_launch_next` releases on `False`);
  `_consecutive_failures` / 3-strike abort NOT touched by a declined film (it is
  a skip, not a Jasna failure); no new config, no new state beyond two booleans
  (`_ltx_declined`, `_ltx_declined_alerted`), both reset in `start()`.
- Allowed user-visible change: declined LTX films are reported failed with
  `ltx_model_missing`; the rest of the queue runs.
- Non-goals: resuming a declined download inside the same run; changing the
  workflow's download flow; changing the approve path or `tick()`.
- Causal boundary: `JasnaInstance.respond_prompt`, `_launch_locked` (the
  `missing` branch), the download-tick/dispatch lines in `check()` (the managed
  check path), `start()` flag resets, one new helper, the two alert copies,
  tests, CHANGELOG.

## Assumptions

- A1 `DownloadSession.stop(0)` with `child is None` clears `status`, releases
  the destination claim and returns True (read: lines 232-247).
- A2 `_launch_next` gives the GPU hold back whenever `_launch_locked` returns
  False without a live child (comment N5 at `_launch_next`, read not executed).
- A3 `task.done` consumers accept `failed` counting declined films (they are
  just counts).

## Approach

```python
# respond_prompt
with self._launch_lock:
    if self._stopping.is_set() or not self._started:
        return False
    if not self._download.respond(request_id, approved):
        return False
    if not approved:
        self._download.stop(0)      # no child yet: clears the request only
        self._ltx_declined = True
        self._advance_requested = True   # check() dispatches on its next frame
    return True

# check(): the existing download-tick line becomes
with self._launch_lock:
    resumed = not self._stopping.is_set() and self._download.tick()
if resumed:
    self._advance_requested = True
self._dispatch(emit)          # a no-op unless an advance was requested

# _launch_locked, replacing the `if missing:` body
if missing:
    if self._ltx_declined:
        self._skip_ltx_declined(emit, missing)
    else:
        self._download.request(...)
        self._alert_ltx_model(emit, missing)
    return False
```

`_skip_ltx_declined` (new, ~12 lines): pop `_pending[0]`, bump `_failed`,
`_mark_restore_failed(name)`, task-log `restore.skipped` with reason
`ltx_model_missing`, settle subs `skipped`/`restore_failed` when `av_translate`
and the name is in `_jobs`, the once-per-run alert guarded by
`_ltx_declined_alerted`, and set
`self._advance_requested = True` so `_dispatch` continues with the next film
after `_launch_next` has given the hold back (the D29 loop pattern; `_dispatch`
runs outside `_launch_lock`, `_advance` → `_launch_next` → `_launch_locked`).

The `_advance_requested` flag is a plain bool already written by settlement
paths and read by `_dispatch`; a decline arriving mid-frame is picked up by
the next `check()` at the latest.

Alternative rejected: keeping `paused` and skipping only LTX films while paused
(keeps a second state machine alive in the monitor for a resume nobody asked for).

## Files to change

| path | change | reason |
|---|---|---|
| `taskpaw_v3/monitors/plugins/jasna.py` | `respond_prompt`, `_launch_locked`, `start()` reset, new `_skip_ltx_declined`, alert copy | AC1-AC6 |
| `taskpaw_v3/tests/test_jasna.py` | update the decline test; add decline-continues / second-LTX-film / done tests | AC7 |
| `CHANGELOG.md` | one entry under the current heading, no version bump | — |

## Execution surface

Writes: the files above in the repository root.
Executes: `uv run pytest taskpaw_v3/tests/test_jasna.py -k "missing_model or declin"`,
then full suite, ruff, mypy, with `--basetemp <LOCALAPPDATA>/taskpaw-pytest/afk-331`.

## Key implementation notes

- Tests use `_managed(tmp_path, ltx_1080p=True, ...)`, `_videos(inp, ...)`,
  `_Launcher`, `_patch`, `_events()` and `_tasklog(kind)` helpers already in
  `test_jasna.py` (see `test_missing_model_waits_without_consuming_films_or_gpu`).
  A mixed queue needs one 1080p LTX film and one film on a non-LTX tier: use the
  probe helper `_probe({...})` to give the second film 4K dims with `ltx_4k`
  off, as other tests do.
- The decline path runs under `_launch_lock`; `_settle` is already called under
  that lock elsewhere (`_skip_existing`).
- Do not call `_fail_current` (it bumps `_consecutive_failures` and may abort the
  batch).

## Risk assessment

| risk | likelihood | impact | mitigation |
|---|---|---|---|
| A decline during an in-flight approve race | very low | `respond` already rejects a stale `request_id` | existing test keeps the stale-id assertion |
| `_maybe_done` not firing because `_jobs` has an unsettled entry for the declined film | medium | done never emitted | AC2 settles the subs job; test AC4 with `av_translate=True` and `False` |

## Out of scope

Workflow download flow; resuming inside a run; Jasna's 3-strike policy.

## Test plan

RED: updated decline test + three new tests fail on `cb9aca4`. GREEN: targeted,
then full suite + lint + mypy.

## Debate record

- v1 → v2 (2026-10-26): critic C1 (supported, P1): after a decline nothing
  re-dispatched (`check()` dispatches only when `tick()` resumes; `_gpu_waiting`
  is False). Fixed: decline sets `_advance_requested`, `check()` always calls
  `_dispatch` (no-op unless requested), and `_skip_ltx_declined` re-requests the
  advance so consecutive LTX films fail on one frame. C2 (minor, supported):
  tracker marks exist only with `av_translate` on; AC2 and the tests say so.
  Lenses 1, 2, 3, 4, 5, 7: verified by probes, no finding.
- Round 2 (v2): C1 resolved (probe: one check fails both LTX films, fires
  `done` once, mixed queue launches the non-LTX film on the same check; no
  infinite loop: only the skip branch re-requests and it always pops). C2
  resolved; wording: with `av_translate` on every pending film has a tracker
  row. C3 (minor): the skip helper would alert once per skipped film; v3 moves
  the single alert to the decline branch, no new flag. C4 (nit): a competing
  GPU waiter makes each skip wait a turn, as any launch does; accepted.
  Lenses a, b, c: no finding.
- v3 → v4 (driver, same day): the instance stores no emitter and
  `respond_prompt` has none, so the alert moves back to the skip helper with
  the `_ltx_declined_alerted` guard (the `_ltx_model_alerted` pattern). C3
  stays open until round 3 revalidates it by name.
- Round 3 (v4): C3 resolved (probe: one alert for two skips, two after a
  restart and second decline). Clarifications added without changing any
  decision: AC7 names the alert-count test, the causal boundary names the
  `check()` dispatch lines, the restart note names both flags. Clean round;
  debate closed.

## Handoff notes

- Test expectations (from the critic's probes): after `respond_prompt(id, False)`
  and one `check()`, with `a.mp4` and `b.mp4` both LTX 1080p and the model files
  absent: `launcher.n == 0`, `inst._failed == 2`, `inst._pending == []`, no
  `ltx_download` key in `metrics`, `done` event text "0/2 done, 2 failed",
  `task.done` data `{"done": 0, "failed": 2}`, `_consecutive_failures == 0`,
  `_gpu_holder is None`. A mixed queue (`b.mp4` 4K with `ltx_4k` off) launches
  `b.mp4` on that same check. Assert tracker state only with `av_translate` on.
- The existing test's line asserting `["ltx_download"]["state"] == "paused"`
  goes away; its stale-id and restart assertions stay (`start()` recreates the
  session and must reset `_ltx_declined` and `_ltx_declined_alerted`).

Commit `fix(jasna): a declined LTX download fails the LTX films and continues the queue (#331) [skip ci]`.
