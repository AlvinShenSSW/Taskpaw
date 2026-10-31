# C5: subtitle monitors stop on an unreachable library instead of skipping the whole queue

Issue: AlvinShenSSW/Taskpaw #380 (EPIC #375, audit item 5). Base: `origin/main` 3ddf070 (TaskPaw 4.0.2).
Implemented on branch `afk/perf-c5-380` (revision 3 adds the debate round-2 doc notes; no behaviour change).

Revision 2 follows debate round 1. It addresses C5-D1 (a result queued before the stop), C5-D2 (publish
failures against the dead share are in scope) and C5-D3 (two risk rows). The changes are listed at the end
under **Revision log**.

## Spec review

The standalone 「AV 翻译 (subtitles)」 monitor (`taskpaw_v3/monitors/plugins/avsubs.py`) lists each film's
folder again right before its ASR and right before its translation (#191 AC5). `list_names`
(`taskpaw_v3/monitors/subs/existing.py`) returns `None` on any error. `skip_reason(None, …)` then gives
`SUBTITLE_UNREADABLE`, and `_skip_existing` settles the film `skipped ("subtitle state unreadable")`, with one
alert per run.

When the whole share drops, every remaining film fails its re-check within milliseconds. The queue drains as
"skipped", and `_maybe_done` emits `done`. Evidence from the read-only tasklog `tasklog-20261019.jsonl`:

- 08:06:53.786: the in-flight ASR film settled `unstable`, because its source vanished.
- 08:06:53.794 to 08:06:58: 1,166 films were skipped `subtitle state unreadable`, about 4 ms each.
- 08:06:58.706: `done` was emitted: "Queue: 2077/3245 done, 2 failed, 1166 skipped".

A second path to the same bulk skip exists. Translations already queued or in flight finish and publish their
`.zh.srt` onto the dead share. Each failed publish settles the film `failed` and adds to the streak. After
three, `_abort` (avsubs) or `_disable_subs` (Jasna) bulk-settles every remaining film
`skipped ("cancelled")`. Two cases make this likely:

- translate-only films, which are all submitted at Start without a re-check (avsubs `_start_translate_only`
  ~763–789);
- full films whose translations queue up while the next ASR runs.

**Core need (operator intent):** a NAS drop never bulk-skips the queue. When the library is gone, the run
stops with a visible error, every unfinished film stays unsettled, and the next Start re-plans them. A single
unreadable folder on a reachable library is still skipped as today.

**What the code already settles:**

- **Start always re-plans from disk.** avsubs runs `_reset()` and then `plan_tree`; Jasna runs
  `_reset_subs()`, `plan_queue` and `plan_subs`. No per-film settlement survives a run, so queue retention
  only requires not settling and stopping the run.
- **"Start" on a live instance is Stop then Start.** The UI's Start button only shows for a `stopped`
  lifecycle (`taskpaw_v3/ui/src/views/AgentConsole.tsx` ~238 and ~287). An instance left in `error` shows
  Stop. That is the existing flow after every launch error, abort or `done`. No 继续 control exists on these
  monitors.
- **`_launch_error` is already the run's terminal error state.** It gates `_advance` (no launches),
  `_maybe_done` (no `done`) and `check()` (`state="error"` with the detail) in both plugins. Jasna already sets
  it mid-run (`_launch_next` ~2754–2769).
- **`stop()` is the complete, idempotent teardown in both plugins:**
  - it cancels the translator first, outside the lock;
  - it kills or reaps the ASR child and, in Jasna, the restore child;
  - it releases and withdraws the GPU lease;
  - it joins the threads;
  - it sets `_stopping`, which every advance, poll and `_settle_results` path already honours
    (avsubs `_settle_results` ~1655–1656, Jasna ~4198–4200);
  - it is called again later by Start (idempotent restart) or by the operator's Stop.
- **In Jasna, the re-checked folder is the configured output folder.** `plan_subs` builds every media path as
  `Path(output_folder) / name`, so `job.media.parent` is `jasna_output_folder`, and every `.zh.srt` target sits
  in it as well.

## Acceptance criteria

1. avsubs, at both re-check sites (`_start_asr` before the ASR, `_poll_asr_locked` before the translation):
   when the film's folder listing returns `None`, the monitor probes `avsubs_root_folder` once.
2. **Root unreachable.** The run stops:
   - `state="error"`, with detail `片库不可访问: <root>`;
   - one alert, dedupe key `<iid>:library-unreachable`;
   - one `task.error` row with `reason="library_unreachable"`;
   - the run is then torn down by `stop()`, deferred until after the lock is released: the translator is
     cancelled, any live child is killed, and the GPU lease is released and withdrawn.

   The triggering film and every remaining film stay unsettled. There is no `subtitle state unreadable` skip,
   no `subs.skipped` or `subs.failed` row, no `avsubs-unreadable` alert, no streak increment and no `done`.
3. **Translator results queued before the stop are never published or settled after it** (C5-D1).
4. **The translator-result `.zh.srt` publish failure** (C5-D2): when `publish_zh` returns `kind == "error"`
   (an `OSError` other than the refused-overwrite `exists` case), probe the root once. If it is unreachable,
   take the AC2 stop path: the film stays unsettled, the streak is not touched, there is no per-film alert,
   and the checkpoint is kept. If the root is reachable, the existing `failed` settlement, alert and streak
   behaviour is unchanged.
5. After the share returns, Stop → Start re-plans and processes the remaining films. A film with a published
   `.ja.srt` is planned `translate_only`, and its translation resumes from its checkpoint.
6. **Root reachable** at a re-check (only that folder is unreadable). Behaviour is unchanged: the film is
   skipped `subtitle state unreadable`, with one alert per run, and the walk continues.
7. Jasna applies AC1–AC6, probing `jasna_output_folder`, at four sites:
   - `_start_subs`, translate-only branch;
   - `_start_subs`, full branch;
   - `_poll_subs_locked`;
   - the `_settle_results` publish-failure branch.

   On an unreachable root the whole Jasna run stops in `error`, and its restore queue is left as it is.
8. Tests cover:
   - the unreachable-root stop at each site;
   - queue retention via Stop → Start;
   - the unchanged single-folder skip;
   - a result queued before the stop that is not settled;
   - the translate-only publish failure on an unreachable root (stop, nothing settled) and on a reachable
     root (unchanged failed/streak behaviour).

   Each is covered in both plugins.
9. `library_unreachable` has en and zh-CN labels (`taskpaw_v3/ui/src/i18n.ts`) and is listed in the UI
   reason-code test.

## Frozen issue contract

- **Acceptance criteria:** AC1–AC9 above. They restate issue #380 AC1–5 together with the operator intent
  admitted in debate (C5-D1, C5-D2).
- **Invariants that must hold:**
  - A planned job reaches at most one terminal state, only under `_launch_lock` through `_settle`. Unsettled
    jobs at a stop are already legal: `stop()` and `_launch_error` leave them unsettled, and `_maybe_done`
    never fires.
  - Blocking side effects run after the lock is released, via `_deferred` / `_run_deferred`. The teardown is
    `self.stop`, appended to `_deferred`.
  - Only `_dispatch()` advances the queue.
  - The GPU lease is released on every end path. Each site's existing release (avsubs `release=True` →
    `_gpu_rel()`; Jasna `release_gpu=True` → `_gpu_give`) still runs, and the deferred `stop()` then
    releases and withdraws exactly once more. That second release is a no-op by design.
  - Listing and probe helpers never raise (F14).
  - A publish never overwrites (#191). After the stop, the monitor writes nothing to the library. One
    exception: `stop()`'s existing "child exited 0 unpolled → publish its own output" rules, the avsubs and
    Jasna `_stop_asr` `.ja.srt` and the Jasna `_publish_current` restore. Those are unchanged.
  - A stopped, aborted or errored run never emits `done`.
  - **S1 is unchanged.** Jasna's existing rule (`_check_managed` ~2953–2955, test
    `test_translation_settles_after_a_restore_launch_error`) is that a mid-run restore-launch error does not
    strand finished translations. That rule stays as it is, because this design does not add `_launch_error`
    to the `_settle_results` guard. See Approach §4.
- **Allowed user-visible changes:**
  - A run that loses its library ends in `error` with `片库不可访问: <root>` and one alert, instead of
    `done` with N skipped, or `aborted` / "AV 翻译 disabled" with N cancelled.
  - In Jasna that also halts the restore queue, and an in-progress LTX model download is cancelled (the
    deferred `stop()` stops the downloader, C5-D4).
  - Task-log rows gain one new reason label.
- **Non-goals:**
  - auto-resume when the share returns;
  - the in-flight ASR film that settles `unstable` when its source vanishes;
  - the `.ja.srt` publish failure at the poll sites;
  - the empty-transcript `.zh.srt` publish inside `_submit_translation`. Rationale for these last items is in
    Approach §5;
  - Jasna planning (`plan_subs` already turns subtitles off when the output folder cannot be listed at
    Start);
  - the media workflow (`taskpaw_v3/workflow/*` calls none of this);
  - any generic reachability framework.
- **Smallest causal boundary:**
  - one never-raising probe in `subs/existing.py`;
  - one `_stop_unreachable` per plugin;
  - the branch at 3 sites in avsubs and 4 sites in Jasna;
  - docstrings;
  - the i18n label and its test;
  - tests.

## Assumptions

- **A1 (measured, partly).** On this machine with Python 3.12.9, against `\\taskpaw-nonexistent-host\x`:
  - The first `os.scandir` took 2.0 s (name resolution) and raised `FileNotFoundError` with `winerror=53`.
  - Every later `scandir`, `stat` and `isdir` call on that host or a sub-path took under 1 ms (the negative
    cache).
  - Unverified: a resolvable host that is powered off or asleep. That is an SMB connect timeout, possibly
    tens of seconds when cold.
  - **Mitigation:** the probe runs only after an operation on the same share has just failed. The live log
    shows those failing in about 4 ms each. In the unreachable case it runs at most once per run.
- **A2 (unverified).** The Windows SMB directory and attribute caches (about 10 s) do not make
  `os.scandir(root)` succeed while the share is down. The root is enumerated only at Start. If this is wrong,
  behaviour degrades to today's for that one film.
- **A3 (from source).** WinError 53/67 surface as `FileNotFoundError` (F13 note in `existing.py`). The probe
  treats any exception as unreachable, so a deleted or permission-denied root also stops the run. That is
  intended.
- **A4.** "Next Start or 继续" is the existing Stop → Start.
- **A5.** In Jasna, an unreachable output folder ends the whole run, restores included. `stop()` kills a live
  restore, whose staging file is in the same dead folder anyway.
- **A6.** The detail and alert text is operator-facing Chinese, as the AC fixes. Code and logs stay English.
- **A7 (from source).** `Translator.cancel()` is idempotent and bounded (about 1 s, `subs/translate.py`
  ~701). A checkpoint is discarded only after a zh publish succeeds (`translator.discard_checkpoint` follows
  `res.ok`), so an unpublished or cancelled translation resumes cheaply next Start.
- **A8 (from source).** Calling `self.stop()` from the monitor's own worker thread, in a `_run_deferred`
  call after the lock is released, is safe:
  - `stop()` takes `_launch_lock` with a timeout;
  - it is idempotent, and `_log_stopped` stops the `task.interrupted` rows from being written twice;
  - `start()` already calls it for an idempotent restart, and `_run_deferred` catches anything it raises
    (Jasna's `RuntimeError("model downloader is still stopping")`).

  The supervisor keeps the instance registered and keeps calling `check()`, which returns the
  `_launch_error` status.

## Approach

### 1. Probe helper (shared)

`root_reachable(root) -> bool` goes in `taskpaw_v3/monitors/subs/existing.py`, next to `list_names`:

- It opens `os.scandir(root)` and closes it immediately. On Windows that is one FindFirstFile, with no full
  enumeration.
- It returns True on success. On ANY exception it logs a warning, like `list_names`, and returns False. It
  never raises (F14).
- It catches `Exception`, not only `OSError`, because three of its call sites are outside the lock and outside
  the K1 fence with the GPU held. That is the same reason `list_names` does. The existing tests pass
  `RuntimeError("bug")` through `os.scandir` there.
- `_missing()` does not fit: it returns False for roots and answers a different question. No other
  reachability helper exists under `taskpaw_v3/monitors`.

### 2. `_stop_unreachable(emit)`, one per plugin, called under `_launch_lock`

1. Set `self._launch_error = f"片库不可访问: {root}"`. Root is `self._cfg.avsubs_root_folder.strip()` for
   avsubs and `self._cfg.jasna_output_folder.strip()` for Jasna.
2. Write `get_task_log().record(iid, "task.error", task_type=…, severity="error",
   data={"reason": "library_unreachable"})`.
3. Emit one alert:
   - title: `f"{cfg.name}: 片库不可访问"`;
   - message: `片库不可访问: <root>。剩余影片保留在队列中，未标记跳过；共享恢复后先「停止」再「启动」即可继续。`;
   - `dedupe_key=f"{iid}:library-unreachable"`.
4. `self._deferred.append(self.stop)`.

It settles nothing. In particular it must not reuse `_abort` or `_disable_subs`, because both bulk-settle
`skipped ("cancelled")`.

**Why a deferred `self.stop()`** (revision 2: this replaces the revision-1 "translator cancel only"):

- **It covers C5-D1 with the existing guard.** `stop()` cancels the translator and sets `_stopping`. Every
  holder calls `_run_deferred()` immediately after releasing the lock, on the same thread, before any further
  result is drained. Both `_settle_results` loops already stop at
  `with self._launch_lock: if self._stopping.is_set(): return`, on the very next result.
  - So results that were already queued are dropped unpublished and unsettled. That includes the ones that
    `Translator.cancel()` leaves ahead of its `CANCELLED` sentinel (`translate.py` ~719–720).
  - Their checkpoints are kept (A7).
- **It covers C5-D2's harder case.** At the `_settle_results` site an ASR child (avsubs or Jasna), or a Jasna
  restore child, may be live for another film.
  - Revision 1 assumed no live child at the call site. That holds at the re-check sites, not here.
  - Without a teardown, `check()` returns on `_launch_error` before ever polling that child. The child would
    run unobserved, holding the GPU lease, until the operator pressed Stop.
  - `stop()` already kills or reaps every child and releases and withdraws the lease (the #40 no-orphan
    guarantee).
- **It reuses the one teardown both plugins already trust.** There is no new cleanup closure.
- **Its edges are the operator Stop's edges** (C5-D5). A child that survives the kill is reaped only by the
  operator's Stop → Start, exactly as after an operator Stop. In Jasna, `_stop_unreachable` overwrites an
  already-set restore-launch-error text; that error's alert was already sent.

### 3. Re-check sites (AC1, AC2, AC6, AC7)

Each site computes `lost = names is None and not root_reachable(root)` before the unchanged `skip_reason`
path. The probe runs where the listing runs:

- outside the lock at avsubs `_start_asr` and at both Jasna `_start_subs` branches;
- under the lock at both poll sites. F15 already accepts the listing there as "the same class of I/O as the
  publish".

When `lost` is true, the site calls `_stop_unreachable(emit)` instead of `_skip_existing(job,
SUBTITLE_UNREADABLE, emit)` and keeps its existing release and terminal bookkeeping.

### 4. Publish-failure site (C5-D2): one per plugin

This is the `publish_zh` failure inside `_settle_results`, the translated branch, at the final `else` (the
`res.kind == "error"` case):

- avsubs `_settle_results` ~1676–1679: `self._settle(job.job_id, "failed", res.detail, emit)` followed by
  `self._alert_job(...)`;
- Jasna `_settle_results` ~4231–4239: `self._settle(job.job_id, "failed", res.detail, emit,
  translate_s=…)` followed by `self._alert_job(...)`.

New shape at both sites: `if not root_reachable(root): self._stop_unreachable(emit)`, otherwise the existing
two calls unchanged. The probe is under the lock, like the publish it follows.

After the stop, the loop's `_run_deferred()` runs `stop()`, and the next iteration's `_stopping` guard
returns, so the remaining results in the batch are not published either.

**The driver's proposed extra guard is deliberately not added.** The proposal was `or self._launch_error is
not None` in both `_settle_results` guards. With the deferred `stop()` it is redundant for this issue, and it
would invert Jasna's existing S1 rule and test (`test_translation_settles_after_a_restore_launch_error`): a
finished translation after an AV-blocked `jasna.exe` launch would stop being published. If the debate still
prefers the guard, it is a one-line change per plugin. That test must then be inverted (zh not written, not
settled, checkpoint not discarded), and the `_check_managed` S1 comment at ~2953–2955 rewritten. The intent
change for Jasna's restore-launch-error path must also be recorded.

### 5. Why the other publish failures stay unchanged

- **The `.ja.srt` publish at the poll sites** (avsubs `_poll_asr_locked` ~1233, Jasna `_poll_subs_locked`
  ~4041–4043). ASR is serial, so at most one film fails this way. The next film's re-check then stops the run
  (§3). Its streak contribution is 1, and translation failures no longer contribute (§4), so the abort
  threshold (3) cannot be reached through it.
- **The empty-transcript `publish_zh([])` in `_submit_translation`** (avsubs ~1617–1624, Jasna ~3790–3797).
  - It runs in two places. One is `_start_translate_only`'s sub-second Start loop. The other is right after a
    re-check that just succeeded: the poll site and Jasna's translate-only branch.
  - A drop exactly inside that window that also hits three empty-transcript films in a row is not a credible
    home-LAN event. No guard is added (operator rule: no defensive code for impossible states).
- **Jasna's translate-only `load_ja` failure after a successful re-check** (`_submit_translation`, the
  counterpart of avsubs `_fail_unreadable_ja`). The share would have to drop between the listing and the read;
  that one film settles `failed`, and the next film's re-check stops the run. Bounded at 1.

### 6. The film that triggered the stop

It is kept unsettled and is not reinserted into the in-memory queue, which is dead after the stop. The disk
is the queue of record.

- avsubs counts it in `queue_remaining` (total − done − failed − skipped).
- A film whose `.ja.srt` was published keeps it and is planned `translate_only` next Start.

### Rejected alternatives

- **Settle the remaining films with a new non-skip state.** Unsettled jobs at a stop are already legal. It
  would need a new terminal code, tracker and UI labels, and a bulk loop, all for state thrown away at the
  next Start.
- **Auto-resume when the share returns.** Not requested, and it adds a timer state machine to two monitors.
- **Probe before every re-check.** It costs a round-trip per film on the happy path.
- **Count N consecutive failures and then stop.** That is a heuristic; the probe answers directly.

## Files to change

| Path | Change | Reason |
|---|---|---|
| `taskpaw_v3/monitors/subs/existing.py` | add `root_reachable(root)`; module docstring sentence on listing helpers | AC1 |
| `taskpaw_v3/monitors/plugins/avsubs.py` | import; add `_stop_unreachable`; branches at `_start_asr` (~1000–1007), `_poll_asr_locked` (~1225–1229) and `_settle_results` publish-error `else` (~1676–1679); docstring (~19–20) | AC1–AC6 |
| `taskpaw_v3/monitors/plugins/jasna.py` | import; add `_stop_unreachable`; branches at `_start_subs` translate-only (~3840–3848), `_start_subs` full (~3868–3877), `_poll_subs_locked` (~4035–4039) and `_settle_results` publish-error `else` (~4231–4239); docstring (~88–90) | AC7 |
| `taskpaw_v3/ui/src/i18n.ts` | `reasons.library_unreachable`: en "Library unreachable" (~130), zh-CN "片库不可访问" (~616) | AC9 |
| `taskpaw_v3/ui/src/test/tasklog.test.tsx` | add `"library_unreachable"` to `reasonCodes` (~63) | AC9 |
| `taskpaw_v3/tests/test_subs_existing.py` | probe tests | AC8 |
| `taskpaw_v3/tests/test_avsubs.py` | update 2 tests, add new ones | AC8 |
| `taskpaw_v3/tests/test_jasna_subs.py` | update 3 tests, add new ones | AC8 |

No version bump. Release and CHANGELOG handling belong to the EPIC.

**Exact new call sites (7):**

| # | Plugin | Function | Line (≈) | Trigger | Probe under lock? |
|---|---|---|---|---|---|
| 1 | avsubs | `_start_asr` | 1000 | `list_names(job.media.parent) is None` | no |
| 2 | avsubs | `_poll_asr_locked` | 1225 | same, after the `.ja.srt` publish | yes |
| 3 | avsubs | `_settle_results` (translated → `publish_zh` → else) | 1676 | `res.kind == "error"` | yes |
| 4 | jasna | `_start_subs` translate-only | 3840 | `list_names(...) is None` | no |
| 5 | jasna | `_start_subs` full (hold held) | 3868 | same | no |
| 6 | jasna | `_poll_subs_locked` | 4035 | same, after the `.ja.srt` publish | yes |
| 7 | jasna | `_settle_results` (translated → `publish_zh` → else) | 4231 | `res.kind == "error"` | yes |

## Execution surface

- **Writes allowed:** only the files in the table.
- **Read or executed, never modified:**
  - `subs/translate.py`, `subs/job.py` (`PublishResult` kinds), `core/gpu_lease.py`;
  - `monitors/supervisor.py`;
  - `agent/server/admin.py` and `ui/src/views/AgentConsole.tsx`;
  - `tests/conftest.py`.
- **Commands:**
  - `uv run pytest taskpaw_v3/tests/test_subs_existing.py taskpaw_v3/tests/test_avsubs.py taskpaw_v3/tests/test_jasna_subs.py`;
  - `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`;
  - from `taskpaw_v3/ui`: `npm test -- tasklog` and `npm run lint`.

  The full suite runs later in the pipeline.
- **No generated outputs.** Never touch the NAS, the installed app, `%APPDATA%\TaskPaw` or
  `D:\TaskpawWork`. Tests use `tmp_path`, a monkeypatched `os.scandir`, and a monkeypatched
  `SubsJob._publish`.

## Key implementation notes

1. **avsubs `_start_asr`.** Compute `lost` after the listing, outside the lock. Under the lock, after the
   existing `stopping or aborted` branch, add `elif lost: self._stop_unreachable(emit); release = True`. Do
   not request an advance. The `finally` runs `_gpu_rel()` and then `_run_deferred()`, which runs `stop()`.
2. **avsubs `_poll_asr_locked`.** Bind `names` and then `skip`. If `skip is None`, submit. Else if
   `names is None and not root_reachable(root)`, call `_stop_unreachable`. Otherwise call `_skip_existing`.
   `terminal` stays true, so `_asr_job` is cleared, `release=True`, and an advance is requested. `_poll_asr`
   then releases, runs `_deferred` (the `stop()`), and dispatches; `_advance` returns because `_stopping` is
   set.
3. **avsubs `check()` after a stop** (corrected in revision 2):
   - Step (1) `_settle_results` returns at its existing `_stopping` guard on the first remaining result. Those
     results, including any queued ahead of the `CANCELLED` sentinel, are never published or settled.
   - Step (3) returns `_build_status("error", self._launch_error)`. Queue metrics are kept, so
     `queue_remaining` shows the unfinished films.
4. **avsubs `_settle_results`.** Only the publish-error `else` changes (§4). After `_stop_unreachable`, the
   loop's existing `self._run_deferred(); self._dispatch(emit)` runs `stop()`, and the next iteration returns
   at the guard.
5. **Jasna translate-only branch.** The hold is given before the listing. Inside the existing
   `if not stopping and _subs_disabled is None:` add the `lost` branch ahead of `skip`. Leave
   `_advance_requested = True`; it is a no-op once stopping.
6. **Jasna full branch.** After the stopping/`_subs_disabled` branch, add `elif lost:
   self._stop_unreachable(emit); release_gpu = True`. The K1 `try` already encloses it, and
   `_gpu_give(job)` follows. `_run_deferred()` at the end of `_start_subs` runs `stop()`.
7. **Jasna `_poll_subs_locked`.** Mirrors note 2. `terminal` stays true: `_subs_job` is cleared,
   `release_gpu=True`, `_phase` is recomputed, and an advance is requested. `_poll_subs` gives the hold, runs
   `_deferred` (the `stop()`), and dispatches as a no-op.
8. **Jasna `_settle_results`.** Mirrors note 4.
9. **Jasna `_check_managed` after the stop:**
   - `_settle_results` returns at its guard;
   - `_download.tick()` is skipped because stopping;
   - `_dispatch` is a no-op;
   - step (2) returns `MonitorStatus(state="error", detail=self._launch_error)`, which carries no metrics,
     like every Jasna launch error.

   `stop()` has already killed any live restore or ASR child.
10. **Root strings.** Use exactly `self._cfg.avsubs_root_folder.strip()` and
    `self._cfg.jasna_output_folder.strip()`. The test spies match `os.fspath(path) == str(folder)`.
11. **No second-call guard.** After `_stop_unreachable`, its deferred `stop()` runs on the same thread before
    any other site can execute: the next `_settle_results` iteration, the next `_advance`, or the next poll.
    Each of those then returns because `_stopping` is set. No re-entrancy check is needed.
12. **Docstrings.** In each plugin module docstring, after "a folder that cannot be read skips it too", add:
    "— unless the library root itself cannot be opened (also checked when a translated `.zh.srt` fails to
    publish): then the run stops in error (片库不可访问) and every unfinished film stays queued for the next
    Start". In `existing.py`, mention `root_reachable` among the listing helpers.

## Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| The probe hangs on a powered-off but resolvable host (cold SMB connect) | low: the redirector just failed a sibling operation (A1) | one check cycle blocks for up to the SMB timeout, once per run | at most one probe per stop; outside the lock at 3 of 7 sites |
| The SMB cache answers the probe while the share is down (A2) | low | that film is handled as today | degrades to current behaviour |
| **(D3a) Jasna re-lists the same root.** The re-checked folder is the output folder, so a short blip that fails both the listing and the probe now stops the whole run instead of skipping one film | low | the operator must Stop → Start; nothing is lost (unsettled, checkpoints kept) | accepted; it is the conservative outcome for a library-safety guard |
| **(D3b) Probe under `_launch_lock`** at sites 2, 3, 6 and 7, a second SMB call after the listing or publish. An operator Stop during it waits for the lock, then after its timeout (5 s default) takes the existing lockless kill path (avsubs D13, Jasna #40 mirror) | low | Stop takes the fallback path; no orphan | accepted; same class as the F15 listing already under the lock |
| A deferred `stop()` blocks the worker's check for up to its 5 s budget (translator join, child terminate) | certain at the stop, once | one slow check cycle | bounded; same cost as an operator Stop |
| A permission-denied or deleted root reads as unreachable | rare | stops instead of skipping | intended (A3) |
| Existing tests that refuse the root listing now see a stop | certain | 2 avsubs and 3 Jasna tests updated | listed below |
| A Jasna error status carries no queue metrics | certain | the progress chips disappear while in error | same as every existing Jasna launch error |

## Out of scope

- Auto-resume when the share returns.
- The in-flight ASR film that settles `unstable` when its source vanishes. That is at most one film per drop.
- The `.ja.srt` publish failure at the poll sites, and the empty-transcript `publish_zh([])` in
  `_submit_translation`. The bounded rationale is in Approach §5. The translator-result `.zh.srt` publish
  failure, the bulk path, is in scope (§4).
- Jasna restore-side handling beyond the run stop. Lada, the media workflow, and Hub or OpenClaw forwarding
  are unaffected.

## Test plan

**Probe tests (`taskpaw_v3/tests/test_subs_existing.py`):**

- `test_root_reachable_opens_one_folder`: True for `tmp_path`, False for a missing sub-path.
- `test_root_reachable_reads_any_failure_as_unreachable`: parametrised `PermissionError(13)`,
  `OSError(53, "net")` and `RuntimeError("bug")` from a monkeypatched `os.scandir`. Returns False and never
  raises. A null-byte path also returns False.
- `test_root_reachable_does_not_enumerate`: `os.scandir` is called once, its iterator is never advanced, and
  it is closed.

**avsubs tests (`taskpaw_v3/tests/test_avsubs.py`).** Harness: `_setup`, `_gpu_turn`, `_refuse_listing`,
`_lease_spy`, `_keyed`, `_done`, `_states`, `_tasklog` and `_FakeTranslator`, which provides `cancel_calls`,
`cancel_lock_free`, `discarded` and `answer`.

- **Update** `test_an_unreadable_folder_before_the_asr_skips_with_one_alert`. Use subfolders
  (`full=["a/a.mp4", "b/b.mp4"]`) and refuse only `r.root/"a"` and `r.root/"b"`. Call `_refuse_listing`
  twice; the wrappers chain. All current assertions still hold. This is AC6.
- **Update** `test_a_full_film_is_rechecked_before_its_translation[unreadable]` the same way.
- **New** `test_an_unreachable_root_before_the_asr_stops_the_run_and_keeps_the_queue`, parametrised over the
  three exceptions.
  - Setup: `_gpu_turn(full=["a.mp4", "b.mp4", "c.mp4"])`. The films sit at the root, so refusing `r.root`
    hits both the listing and the probe. Release the other holder, then call `check()`.
  - Assertions:
    - `_settled == {}` and no spawn;
    - `st.state == "error"`, with `"片库不可访问"` and `str(r.root)` in the detail;
    - `queue_remaining == 3` and `queue_skipped == 0`;
    - one `library-unreachable` alert and no `avsubs-unreadable` alert;
    - no `done`;
    - `gpu_lease.holder() is None`;
    - `r.inst._stopping.is_set()`;
    - `cancel_calls == 1` and `cancel_lock_free == [True]`;
    - one `task.error` row with `reason == "library_unreachable"`;
    - no `subs.skipped` rows;
    - no `.srt` in the root.
  - A second `check()` is silent.
  - Lift the refusal, then Stop → Start. All three are planned, and `a.mp4`'s ASR spawns.
- **New** `test_an_unreachable_root_before_the_translation_stops_and_keeps_the_transcript`.
  - Setup: `full=["a.mp4", "b.mp4"]`, a transcribing. Refuse root, finish a with 0, then call `check()`.
  - Assertions: a unsettled; a's `.ja.srt` exists; nothing submitted; one spawn; error; lease free.
  - Restart: a is planned `translate_only` and submitted at Start; b is `full`.
- **New (D1)** `test_a_translation_result_after_the_unreachable_stop_is_not_published`.
  - Setup: `_setup(full=["a.mp4"], ja=["t.mp4"])`. Start: t is submitted and a is transcribing. Refuse
    `r.root`, finish a with 0, then `check()` → stop.
  - Then call `r.translators[0].answer("t.mp4")` and `check()`.
  - Assertions:
    - `"t.mp4" not in _settled`;
    - `_zh(r, "t.mp4")` does not exist;
    - `discarded == []`;
    - no `subs.published` row.
  - The fake's FIFO puts the answer behind `CANCELLED`, which `_settle_results` merely `continue`s past.
    Without the `_stopping` guard the result would be published, so this is a real regression check for
    results queued before the cancel.
- **New (D2, unreachable)** `test_a_translate_only_publish_failure_on_an_unreachable_root_stops_and_settles_nothing`.
  - Setup: `_setup(ja=["a.mp4", "b.mp4", "c.mp4"], full=["x.mp4"])`. Start: three submitted, x's ASR live.
    Set `r.inst._streak = 2`. Monkeypatch `SubsJob._publish` to return `PublishResult("error",
    "publish a.zh.srt: FileNotFoundError: [WinError 53]")` and refuse `r.root`. Answer all three, then
    `check()`.
  - Assertions:
    - `_settled == {}`;
    - `not _aborted` and `_streak == 2`;
    - error 片库不可访问, with one `library-unreachable` alert;
    - no `avsubs:<job>` failure alert and no `subs.failed` / `subs.skipped_bulk` rows;
    - `discarded == []`;
    - all three `.ja.srt` files still exist;
    - x's fake child was terminated and `gpu_lease.holder() is None`.
  - Lift both patches, then Stop → Start: a, b and c are planned `translate_only` and submitted again; x is
    planned `full`.
- **New (D2, reachable)** `test_a_translate_only_publish_failure_on_a_reachable_root_is_unchanged`.
  - Setup: the same publish patch, with the root readable and `_streak = 2`. Answer a.
  - Assertions: a is `failed` with its alert; `_aborted`; b and c are `("skipped", "cancelled")`, exactly
    today's behaviour; no `library-unreachable` alert; and the probe was called once (spy).
- **Regression:** `test_translate_only_films_submitted_at_start_are_not_relisted` stays green. Add an
  `AV.root_reachable` spy to one happy-path test, asserting it is never called.

**Jasna tests (`taskpaw_v3/tests/test_jasna_subs.py`).** Harness: `_setup`, `_scandir_spy`, `_gpu_spy`,
`_keyed`, `_done`, `_states`, `r.launcher`. Refusing `r.out` is now the unreachable-root case.

- **Update** `test_an_unreadable_folder_before_the_asr_skips_and_releases_the_hold` (same params) into
  `…_unreachable_root_before_the_asr_stops_the_run`.
  - Assertions:
    - `_settled == {}` and no spawn;
    - `gpu == ["acquire", "release"]`;
    - `_launch_error` starts with `"片库不可访问"`;
    - `check()` returns `state="error"`;
    - one `j1:library-unreachable` alert and no `j1:subs-unreadable` alert;
    - no `done`.
- **Update** `test_a_translate_only_film_is_rechecked_before_its_submit[unreadable]`.
  - Assertions: a unsettled; nothing submitted; library `.ja.srt` untouched; `gpu == ["acquire", "release"]`;
    error; no `done`.
- **Update** `test_a_full_film_is_rechecked_before_its_translation[unreadable]`.
  - Assertions: a unsettled; `_ja` exists; `_subs_job is None`; hold given; error; no `done`.
- **New** `test_an_unreachable_root_stops_restores_and_the_next_start_resumes`.
  - Setup: `pending=["a.mp4", "b.mp4"]`. a is restored and its ASR runs. Refuse `r.out`, finish the ASR,
    then `check()`.
  - Assertions: error, and `r.launcher.n == 1` (b never launched).
  - Lift the refusal, then Stop → Start: b is pending, and a is planned `translate_only`.
- **New (D1)** `test_a_translation_result_after_the_unreachable_stop_is_not_published`.
  - Setup: `_setup(restored=["d.mp4", "e.mp4"], ja=["d.mp4"])`. d is submitted and e's ASR is live. Refuse
    `r.out`, finish e, then `check()` → stop.
  - Then `answer("d.mp4")` and `check()`.
  - Assertions: d unsettled; no zh; `discarded == []`.
- **New (D2, unreachable)** `test_a_translate_only_publish_failure_on_an_unreachable_root_stops_and_settles_nothing`.
  - Setup: `restored=["d.mp4", "e.mp4", "f.mp4"]`, `ja=` the same three, all submitted. Set
    `inst._subs_consecutive_failures = 2`, patch `SubsJob._publish` to `error`, refuse `r.out`, answer all,
    then `check()`.
  - Assertions: `_settled == {}`; `_subs_disabled is None`; error 片库不可访问; no `subs-disabled` alert;
    `discarded == []`.
- **New (D2, reachable)** `test_a_translate_only_publish_failure_on_a_reachable_root_is_unchanged`.
  - Setup: as above but with `r.out` readable.
  - Assertions: d is `failed`; `_subs_disabled` is set; the others are `("skipped", "cancelled")`; no
    `library-unreachable` alert.
- **Kept unchanged** (AC6 for Jasna): `test_run_films_real_planning_and_submit_paths[unreadable]`. It patches
  `J.list_names` → `None` with `r.out` readable and asserts `skipped:unreadable`.
- **New** `test_a_transient_unreadable_listing_with_a_reachable_root_skips_as_before`.
  - Setup: patch `J.list_names` → `None` for one call.
  - Assertions: `("skipped", "subtitle state unreadable")`; one `j1:subs-unreadable` alert; the walk
    continues; `_launch_error is None`.
- **Unchanged (S1 kept):** `test_translation_settles_after_a_restore_launch_error`.

**UI test:** `taskpaw_v3/ui/src/test/tasklog.test.tsx`. With `library_unreachable` in `reasonCodes`, the
existing `it.each` asserts both languages render a label.

**Manual smoke:** none. The tests drive the real plugin code. Never reproduce against a NAS.

## Handoff notes

- Five existing tests flip (2 avsubs, 3 Jasna). Update them as above.
- Keep the probe off the happy path. Its only callers are the five `names is None` branches and the two
  `res.kind == "error"` branches.
- `_stop_unreachable` settles nothing and appends `self.stop` to `_deferred`. Never call `stop()` under
  `_launch_lock`, and never use `_abort` or `_disable_subs`.
- Do not add `_launch_error` to the `_settle_results` guards unless the debate overrides Approach §4. If it
  does, invert `test_translation_settles_after_a_restore_launch_error` and rewrite the S1 comment.
- **Operator decisions to confirm:**
  - (a) Stop → Start is the resume path, with no auto-resume.
  - (b) In Jasna, a lost output folder stops restores too, kills a live restore, and cancels an in-progress
    LTX model download.
  - (c) The detail and alert wording.

## Revision log

**Revision 2**, from driver debate round 1:

- **C5-D1.** The fix is the deferred `self.stop()` in `_stop_unreachable`. It replaces the deferred translator
  cancel. The existing `_stopping` guards in both `_settle_results` loops then drop every result queued before
  the cancel. The driver's `_launch_error` guard is not added: it is redundant here, and it would invert
  Jasna's S1 rule and its test. The trade-off, and how to apply it if overridden, is in Approach §4. Note 3 is
  corrected, and a D1 test is added per plugin.
- **C5-D2.** Now in scope. The translator-result `publish_zh` error branch in `_settle_results` probes the root
  and stops instead of settling `failed` (sites 3 and 7). This exposed a live-child case at that site (an ASR
  or Jasna restore child of another film), which the deferred `stop()` also tears down. The false out-of-scope
  claim is replaced by the bounded rationale in §5. D2 tests are added per plugin, for unreachable and
  reachable roots.
- **C5-D3.** Two risk rows added: Jasna's same-root re-listing, and the probe under `_launch_lock` with
  Stop's 5 s lockless fallback.

**Revision 3**, from driver debate round 2 (documentation only, no new behaviour):

- **C5-D4.** Jasna's deferred `stop()` also cancels an in-progress LTX model download. Added to the allowed
  user-visible changes and to operator decision (b).
- **C5-D5.** Approach §2 now states the operator-Stop edges: a child that survives the kill is reaped only by
  Stop → Start, and in Jasna `_stop_unreachable` overwrites an already-alerted restore-launch-error text.
- §5 names Jasna's translate-only `load_ja` failure after a successful re-check as another case bounded at 1.
