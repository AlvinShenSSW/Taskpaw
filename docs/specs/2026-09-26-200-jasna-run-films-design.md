# #200 — Jasna 「本轮影片」 card: this run's films with restore / translation result and the models used, filter 已完成 / 未完成 / 全部, 10 per page; version 3.9.3

Date: 2026-09-26 (design v3, FROZEN — debate rounds 1–3: R200-1…R200-16; round 3 CLEAN; wording nits applied)
Issue: #200. Owner-approved mockup: https://claude.ai/artifact/3ZNp4dsm8REHAeXr69zuPB (filter added by the owner: default 已完成, also 未完成 / 全部).
Driver: `/afk` — Claude leads; implementation Codex gpt-6-astra (high); outer gate Claude; final gate DeepSeek flash (afk-skills 1.2.3).
Merge when AFK merge-ready, then release 3.9.3.

## Spec review

On a Jasna task with AV 翻译 on, the owner wants a card in the lower-middle of the task page. It lists every film of the current run and shows for each film:

- the name;
- the restore result;
- the translation result;
- the model(s) that translated it, with line counts;
- the duration;
- the finish time.

A filter offers three views: 已完成 (default, newest first), 未完成 (the current film first, then the queue) and 全部 (unfinished, then finished). The card shows 10 films per page.

What exists today (code map + critic, 2026-09-26):

- **`FilmTracker`** (#189 / #198) holds every film of the run, with per-step states and monotonic stamps.
  - It has no wall clock.
  - `settle_subs` drops the reason.
  - Per-film model counts (`by_model`) and kept-Japanese counts (`kept_ja`) exist only in the translator's task-log record (#196 `translate.finished`).
  - A result reaches Jasna only through the translator's `TranslateResult` queue.
- **Jasna's `_settle` call sites cover a closed set of `(terminal, detail, step)` combinations.**
  - Three paths settle the subtitle steps BEFORE the restore runs: the no_exe bulk skip at Start, the translator failing to start, and three consecutive subtitle failures.
  - Restore failure takes two tracker calls today.
  - `_fence_internal` bypasses `_settle`.
- **#198 already pages the film list for Jasna** (`film_page`, `PagedFilmList`) in plan order. The new card with 全部 covers the same films, so for Jasna it **replaces** that list.
- **Constraints:**
  - The HTTP thread may touch only the tracker (its lock).
  - Status payloads, the Hub and OpenClaw must not change.

## Frozen issue contract

### AC1 Per-film facts, recorded on the worker thread

- **New `_Film` fields:**
  - `finished_wall: float | None`
  - `code: str | None` (see AC3)
  - `kept_ja: int`
  - `models: tuple[tuple[str, int], ...]` (`(label, lines)`; each label is bounded to 80 chars FIRST, then the counts are aggregated per bounded label, then sorted by lines descending and capped at 8 entries, R200-16)
  - `translate_s: float | None`
- **Terminal hook (R200-3b, R200-13).** One generic "just became terminal" hook runs in both `finish()` and `settle_subs()`, under the lock.
  - Each mark first assigns ALL of its own facts (states, code, kept_ja, models, translate_s), then runs the hook.
  - The first time a film's status turns terminal, the hook stamps `finished_wall` from an injectable `wall_clock` (default `time.time`), once. The hook also runs in `add()`, so a film that is terminal at `add` (not reachable in Jasna today; the tracker is shared) also gets `finished_wall`.
  - The hook stores NO fallback code. A code is stored only when a caller supplies one, first wins.
- **Effective outcome, computed at READ (R200-4, R200-13, R200-15).** No terminal row is ever without one:
  1. `restore.state == failed` → `restore_failed` (R200-3a: it wins over any earlier code, e.g. no_exe or cancelled settled at Start);
  2. else the stored code. The only exception is a stored `restore_failed` when the restore did NOT fail, as in `_exit_internal_error` after a successful restore: that becomes `skipped:other` (R200-15);
  3. else by the job outcome: completed → `translated` or `partial` by `kept_ja`; failed → `failed`; skipped, or no job → `skipped:other`. Anything else (e.g. a job with no outcome) → `skipped:other`, so the rule is total.
- **Restore failure is ONE tracker call (R200-4).** A new `fail_restore(film, now, code)` marks restore failed and settles the remaining subtitle steps skipped, atomically, then runs the hook once. `_mark_restore_failed` uses it.
- **Translation results.** `settle_subs(outcome, …, code=None, kept_ja=0, models=(), translate_s=None)` records the translation facts in the same locked call that sets the terminal states.
  - `TranslateResult` gains two trailing, defaulted fields: `by_model: tuple[tuple[str, int], ...] = ()` and `duration_s: float | None = None`.
  - They are computed ONCE in `_finish` (via `dataclasses.replace`) on the translator thread. They reuse the same expression and the same clock read as the task-log `translate.finished` record (R200-10).
  - Only `translated` results carry models. `paused`, `no_key` and `failed` carry none.
  - Jasna's `_settle_results` passes `kept_ja`, `by_model` and `duration_s` through `_settle(...)`.
  - A translation whose publish is refused because a subtitle appeared meanwhile is settled as `skipped:subtitle_exists`, without models.
- **Planning failure (R200-2).** When Jasna adds a film without a job (kind `none`), it passes the no-job code on the restore `finish(..., code=…)`:
  - `has_subs` for a real kind-none film;
  - `skipped:planning_failed` when `_subs_disabled == "planning failed"`.
- `_fence_internal` passes `code="failed"`.

### AC2 Tracker read `run_films(filter, page, size) -> dict`

- **Access.** The read runs under the tracker lock, with no `observe()` and no live source, using `_last_live`. It has no side effects.
- **`filter`** takes `"done"` (terminal), `"open"` (non-terminal) or `"all"`. Anything else is treated as `"done"`.
- **Order:**
  - `done`: newest first by the monotonic terminal stamp (`_ended`), ties by plan order.
  - `open` (R200-5, R200-12):
    - the focus film first, only when its status is NON-terminal (the same `_focus` rule as the status view: GPU child > waiting > translating > …; `_focus` can return a terminal film, e.g. a stale `_last_live` or its last-finished fallback, and such a film never appears in `open`);
    - then by derived rank (active > waiting_gpu > queued > pending);
    - then plan order.
  - `all`: open (as above), then done (as above).
- **Totality and clamps** are as in #198's `page()`: `bool` is not an int; `size` defaults to 10 and is clamped to 1–50; `page < 1` or a bad page gives page 1; a page past the end gives the last page.
- **Result:** `{run, filter, total, size, page, pages, focus, counts: {done, open, all}, totals: {...}, films: [row]}`.
  - `focus` is bounded to 200 chars, or null.
  - `counts.all == counts.done + counts.open`.
- **`totals` covers the done films (R200-1, R200-14).** It is the histogram of the done films' EFFECTIVE `outcome`, i.e. the same value the row shows, never the stored code. Every done film lands in exactly ONE bucket, so `Σtotals == counts.done`:

  | Bucket | Codes |
  |---|---|
  | `translated` | `translated` |
  | `partial` | `partial` |
  | `has_subs` | `has_subs`, `skipped:subtitle_exists` |
  | `untranslated` | `no_speech`, every other `skipped:*` |
  | `failed` | `failed`, `asr_failed` |
  | `restore_failed` | `restore_failed` (derived, AC1) |

- **Row fields:**
  - `name` (bounded to 200)
  - `restore` (derived step state)
  - `restored_before` (the restore step was done at `add` with no stamps: a subs-only film)
  - `asr` and `translate` (derived step states)
  - `percent` (the active step's, from `_last_live`, or null)
  - `outcome` (the effective code, AC3; null while open)
  - `kept_ja`
  - `models: [[label, lines], ...]`
  - `duration_s` (restore + ASR from the tracker, plus translate = `translate_s` when present, else the tracker's (R200-9); null while open)
  - `finished_at` (epoch seconds, or null)
- **Status unchanged (R200-11).** The status view's `films` rows and #198's `page()` rows keep their exact key set. A test pins it.

### AC3 Outcome codes (a closed vocabulary)

| Code | When | zh label | en label |
|---|---|---|---|
| `translated` | completed, not no-speech, `kept_ja == 0` | 翻译完成 | Translated |
| `partial` | completed, `kept_ja > 0` | 部分保留日文 · N 句 | Partly kept in Japanese · N lines |
| `no_speech` | completed as no speech | 未翻译 · 无语音 | Not translated · no speech |
| `has_subs` | kind `none`: already had subtitles, no subtitle work this run | 已有字幕 | Already had subtitles |
| `restore_failed` | restore failed (always wins, AC1) | 未开始（修复失败） | Not started (restore failed) |
| `asr_failed` | a failure whose step is ASR | 识别失败 | Recognition failed |
| `failed` | any other failure (translator, publish, unreadable .ja.srt, internal) | 翻译失败 | Translation failed |
| `skipped:<reason>` | skipped for another reason | 跳过 · \<reason\> | Skipped · \<reason\> |

- **Reasons.** Each reason code maps from Jasna's existing detail strings and has a localized label:

  | Reason | zh label |
  |---|---|
  | `no_llm_key` | 未配置翻译模型 |
  | `translation_paused` | 翻译服务不可用，可续 |
  | `subtitle_exists` | 已有字幕 |
  | `transcript_exists` | 已有日文字幕 |
  | `unreadable` | 字幕状态无法读取 |
  | `unstable` | 文件还在写入 |
  | `cancelled` | 本轮已关闭 AV 翻译 (R200-8: it only comes from `_disable_subs`, never from an operator Stop) |
  | `no_exe` | 未安装 WhisperJAV |
  | `planning_failed` | 无法列出输出目录 (R200-2) |
  | `other` | 其它原因 |

  Only the code travels in the row, never free text.
- **Unknown codes.** The UI renders an unknown code as a generic 「其它」 label. It never rejects the page because of an unknown code (R200-4).

### AC4 Plugin and API

- **`MonitorInstance.run_films(filter, page, size) -> dict | None`**, None in the base.
  - **Jasna** returns its tracker's `run_films(...)` when AV 翻译 is on, else None. It reads `self._tracker` once, never raises, and logs errors once per tracker (the #198 `read_film_page` pattern).
  - **avsubs** returns None.
- **`Supervisor.run_films(instance_id, filter, page, size)`** looks the instance up under `_lock` and calls it outside the lock. An unknown or stopped instance gives None.
- **`GET /control/monitors/run-films?name=&filter=&page=&size=`** is served on the loopback **control app only**.
  - It is wired through a new `create_control_app(run_films_provider=…)`; the launcher passes `supervisor.run_films`.
  - These give 400 `{"detail": …}`, via the request-validation handler path, as in #198: a missing or blank `name`; a non-int `page` or `size`; `page < 1`; a `filter` not in {done, open, all}.
  - When the provider returns None: 404 `{"detail": "no film list"}`.

### AC5 UI: `RunFilmsCard` (agent console, Jasna only)

- **Mount.** It replaces #198's `PagedFilmList` inside `PipelineProgress` when a task name is given and the pipeline is Jasna's (it has a `restore` step).
  - avsubs keeps #198's list; the Hub keeps the capped list.
  - With AV 翻译 off there is no pipeline, so there is no card.
- **Query policy.** Same as #198, on its own root key `["runFilms", name, filter, page]`:
  - 5 s refetch while mounted;
  - `keepPreviousData` and `gcTime: 0`;
  - unobserved `["runFilms", name]` entries are removed when the filter or page changes;
  - the last good response is kept per task;
  - a failed change resets the requested filter and page to the last good ones (V3-1);
  - a malformed body counts as a failure (but an unknown outcome code is not malformed, AC3);
  - the component is keyed by task name.
- **Rendering source (R200-7).** Header, order note, rows and pressed filter all come from `data.filter`, the answered one. The filter buttons are disabled while a filter or page change is in flight (`isPlaceholderData`); a background poll does not disable them.
- **Fallback (R200-7).** While there is no good response yet, or when the last good response has `counts.all == 0` while the status still has rows (the D198-7 case), the card shows #198's plain capped `FilmList`: no filter, header or totals.
- **Filter.** A segmented button group: 「已完成 N」 (default), 「未完成 N」, 「全部 N」, with `aria-pressed` and targets of at least 40 px.
  - Changing the filter goes to page 1.
  - A new `run` goes to page 1 and keeps the filter.
- **Header.** 「本轮影片」 plus the order note:
  - 已完成：最新完成的在前
  - 未完成：当前 → 排队
  - 全部：未完成在前，已完成在后

  The done totals follow: 翻译完成 / 部分保留日文 / 已有字幕 / 未翻译 / 失败 / 修复失败 (Σ equals the 已完成 count).
- **Rows (≥ `sm`).**

  | Column | Content |
  |---|---|
  | 片名 | mono font |
  | 修复 | ✓ 完成 / ✕ 失败 / 修复中 N% / 等待 GPU / 排队 / 本轮前已修复 |
  | 翻译 | a chip with the outcome label (AC3); while open, 识别中 N% / 翻译中 N% / 等待 |
  | 用的模型 | one line per model: the model part before " · " plus 「· N 句」 |
  | 用时 | 「1 小时 12 分」 / 「58 分」 / 「不到 1 分」 / 「—」 |
  | 完成于 | HH:MM if today, else M/D HH:MM, else 「—」 |

  The focus row (`data.focus`) is highlighted. Status is never shown by colour alone.
- **Rows below `sm` (R200-6).** A stacked, labelled layout (FilmList-style flex-wrap): the name, then 「修复：…」 「翻译：…」 「模型：…」 「用时：…」 「完成于：…」. There is no horizontal scroll at 375 px.
  - The full `model · host` label is reachable as text, via an accessible description or the stacked layout's model line; it is not only a `title`.
- **Pager.** As in #198: 上一页 / 下一页 (disabled at the ends and while in flight) and 「第 x / y 页 · 共 N 部」. It wraps.
- **Empty states per filter.** 「本轮还没有完成的影片」 / 「没有未完成的影片」.
- zh + en strings. A design-system note goes in `design-system/taskpaw-v3/pages/agent-console.md`, covering the layout and the stacking below `sm`.

### AC6 Unchanged

- Status metrics (`films` ≤ 12 + `films_more`) and their row key set, `status.md`, the Hub and OpenClaw.
- #198's endpoint and rows, and avsubs' list.
- Scheduling, settlement, GPU lease, publishing and translation behaviour: observation only.

### AC7 Docs and version

3.9.3 in the six version files; CHANGELOG 3.9.3 (Chinese).

### AC8 Tests

No network, no real exes.

- **Tracker:**
  - The terminal hook stamps `finished_wall` from the injected clock once, in both `finish` and `settle_subs`, AFTER the mark's own facts are assigned (a `partial`/`has_subs`/`planning_failed` code is never lost to a fallback).
  - The effective outcome at read: each rule, including no-job films → `skipped:other` and a stored `restore_failed` without a failed restore → `skipped:other` (R200-15).
  - `fail_restore(film, now, code)` is atomic: no intermediate terminal state.
  - A failed restore wins over an earlier skipped code, and the TOTALS agree with the ROWS (no_exe / cancelled + restore fail → counted in 修复失败, R200-14): totals == the histogram of every done row's `outcome` across all pages.
  - A terminal focus never appears in `open`, and `total == counts[filter]` (R200-12, the D1/D2 setups).
  - Models: bound, then aggregate, then cap (two long labels that bound to the same string merge, R200-16).
  - Membership and order for each filter, including focus first in `open`.
  - `Σtotals == counts.done` and `counts.all == done + open` in every scenario.
  - Clamps and bad input; the `run` token; `focus`.
  - No side effects.
  - `restored_before`.
  - `duration_s`, preferring `translate_s`.
  - Model re-bounding and cap.
  - 1 000 films.
  - The exact key sets of status `films` rows and #198 `page()` rows are pinned.
- **Translator:**
  - `TranslateResult.by_model` and `duration_s` equal the task-log `translate.finished` values (same clock read).
  - Only `translated` results carry models.
  - Resumed films with long or unbounded checkpoint labels are re-bounded.
  - Existing positional constructions still work.
- **Jasna:**
  - Each AC3 code from its REAL settle path: translated; partial; no speech; `has_subs`; `planning_failed`; `restore_failed`; `asr_failed`; `failed`; `failed` via `_fence_internal`; each skipped reason.
  - Settle-before-restore then restore fails, i.e. no_exe / cancelled + restore fail → `restore_failed`.
  - Settle-before-restore then restore OK → `finished_at` set.
  - `run_films` is None with AV off.
  - The read never touches live sources.
- **Supervisor and API:** lookup; response shape; filter / page / size validation (400) and 404; `/` in names; not on the network app.
- **UI:**
  - The three filters with counts, default 已完成.
  - Order per filter.
  - Every cell variant in zh and en; the unknown-code label.
  - Models with the host stripped, and the full label reachable as text.
  - Finish-time and duration formats.
  - The pager; a filter change resets to page 1; a run change resets.
  - Render source = `data.filter`; filters disabled only while in flight; a background poll does not disable the pager.
  - Last good response kept, V3-1 recovery, malformed body.
  - Both fallbacks.
  - The stacked layout at 375 px.
  - avsubs and the Hub unchanged.
  - Accessibility: `aria-pressed` and labels.

## Invariants

- The card's data is read only from the tracker, under its lock, with no live source.
- Facts are recorded on the worker thread. No terminal row lacks a code or a `finished_wall`.
- `Σtotals == counts.done` and `counts.all == done + open`.
- Status, Hub, OpenClaw, #198 rows and avsubs are unchanged. Observation only.

## Assumptions

- **A1** 「本轮」 means the current tracker (since this Start).
  - A watchdog re-start or a Stop/Start begins a new run. Films finished before that are replanned as already done and do not appear.
  - Films already restored with subtitles before the run are not tracked (#198 A1). Earlier history is on the 日志 page (#196).
- **A2** The translate duration is the translator's, from dequeue to finish, including any deferral. `duration_s` prefers it.
- **A3** Model labels are shown without the host. The full label stays reachable as text.
