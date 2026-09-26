"""Pure progress observation for the subtitle steps (#189).

Nothing here does I/O, starts a thread or raises: every input is data the
plugins already hold (the ASR child's captured tail, the marks at their
existing counter points, the live facts they derive at status time).

- `AsrProgress` — WhisperJAV qwen/decoupled pipeline progress from the ASR
  child's captured output (AC1). Patterns are anchored on the pipeline's own
  bracketed tag (`[QwenPipeline PID n] Phase k:`, `[DecoupledPipeline]
  Generating scene i/N`, …), so the balanced pipeline's `Scene 1/1 (…)` line
  or a filename containing "Phase 8" never moves it (D9). Nothing is parsed
  until a pipeline tag was seen; without one the snapshot is elapsed-only.
- `FilmTracker` + `LiveFacts` — per-film step outcomes (AC3/D3/D4/D12).
- `parse_eta` — the restore capture's `eta` string in seconds (N8).

FilmTracker API (what the plugins call)
---------------------------------------
Build one tracker per run: `FilmTracker(JASNA_STEPS)` or
`FilmTracker(AVSUBS_STEPS)`. The steps are `restore` → `asr` → `translate`.

Marks — only at the plugins' existing counter points, each right after its
counter / `_settled` update and before any `emit`:

- `add(film, initial)` — once per film, in QUEUE ORDER (the order of `add`
  calls is the plan order the rows are sorted by), before any settle can
  happen. `initial` maps a step to `pending | done | failed | skipped`
  (missing → pending). A film whose subtitle steps are all terminal at `add`
  (Jasna kind none, planning failure) has NO subtitle job.
- `start(film, step, now)` — started stamp; for `translate` this is the
  `translator.submit(...)` (queued) fact. Repeats keep the first stamp.
- `activate(film, step, now)` — first time the step is derived active.
  `observe()` calls it for every live-active pair.
- `finish(film, step, state, now, *, code=None)` — sticky terminal
  `done|failed|skipped`;
  `finish(translate, done)` records the job outcome `completed`.
- `settle_subs(film, outcome, now, *, code=None, kept_ja=0, models=(),
  translate_s=None)` — the job settled `completed|failed|skipped`:
  the first non-terminal SUBTITLE step gets the outcome (`done` for
  completed), later subtitle steps `skipped` (`done` for completed); the job
  outcome is recorded. A restore is never touched (N1).

Every mark is total (M1): an unknown film or step, a bad state or a garbage
`now` is ignored (a bad `now` only drops the stamp); terminal states are
sticky; `start`/`activate` on a terminal step do nothing.

Status time — the plugin derives `LiveFacts`; `view` retains the validated
facts under the tracker lock for subsequent page reads:

- `active`  — step → the film live in it now (restore: `_process` live and
  `_current` is the film; asr: the job's child is live; translate:
  `Translator.progress()["job_id"]`);
- `waiting` — `(film, step)` of the GPU wait (the queue head for the next
  GPU step while `_gpu_waiting`), else None;
- `holder`  — `gpu_lease.blocking_label()`, `""` when the lease is reserved
  for this run (N5); the tracker caps it at `NAME_CHARS`;
- `numbers` — step → the live numbers of that step's ACTIVE film, merged into
  its `steps` entry and used for the row's `percent`/`eta_s` (restore:
  `percent`, `eta_s` via `parse_eta`; asr: the `SubsJob.progress(now)`
  snapshot (`phase`, `phase_n`, `scene`, `scenes`, `percent`, `eta_s`,
  `elapsed_s`); translate: `Translator.progress()` fields).

Then `view(live, now)` → `{"film", "steps", "films", "films_more"}` (or `{}`
without films) to merge into the metrics; it calls `observe(live, now)`
(activations + the N6 `waited_s` stamp) first. `progress_view(...)` is the
one builder both plugins call: it derives `active`/`numbers` from the ASR job
and the translator (plus Jasna's restore capture), calls `view` and adds the
translation's `model`. The pieces are public too:
`observe`, `focus(live)`, `steps(film, live, now)`, `rows(live, focus)`,
`row_status(film, live)`, `statuses(live)` (every film, for the row ↔ count
mapping), `record(film)` and `restore_done(film)`. Every returned object is a
fresh copy. Every `now` must be a `time.monotonic()` reading.

Local paging (#198):

- `set_extras(rows)` — retains untracked `(name, status)` rows once per run;
  they follow the tracked films in the page list.
- `page(page, size)` — returns a page in plan order plus run, total and focus
  metadata, using the last `view`'s validated live facts (empty before the
  first view). It reads under the tracker lock, never calls `observe` or a
  live source, and does not change stamps. Terminal marks may be newer than
  the retained live facts.
- `read_film_page(tracker, page, size)` — the plugins' error boundary around
  `page`: returns `None` on an internal error and logs once per tracker.

This run's films (#200):

- Marks retain the first `code` and, at the first subtitle settlement,
  `kept_ja`, `models` (bounded, nonempty labels, aggregated line counts;
  at most `MAX_FILM_MODELS`, descending by count) and `translate_s` (the
  translator's duration, including resumed work in this run).
- The terminal hook runs under the lock after all mark facts are assigned.
  It stamps `finished_at` once using `FilmTracker(..., wall_clock=time.time)`;
  step stamps still use monotonic time. `fail_restore(film, now, code)`
  atomically marks restore failed and settles subtitles skipped.
- `run_films(filter, page, size)` reads retained facts without observation or
  clock reads, returning this run's tracked films, counts, totals and paging.
  `read_run_films(tracker, filter, page, size)` is the plugin error boundary:
  None on failure, logged once per tracker.
- Effective terminal outcomes are derived at read time: a failed restore
  wins; otherwise use `code` (a stale `restore_failed` becomes `skipped:other`),
  then completed → `partial` when `kept_ja` > 0, else `translated`, failed →
  `failed`, or `skipped:other`. No fallback is stored by the terminal hook.

Derived state of a step: a stored terminal state wins; else `active` (the
live film of that step), `waiting_gpu` (`live.waiting`), `queued` (translate
started, i.e. submitted) or `pending`. Row status (R1), first match wins:
restore failed → failed; restore not terminal → its derived state; the job's
recorded outcome (completed → done); no job with the restore done → done; the
first non-terminal step's derived state. Duration: `ended_at − (activated_at
or started_at)`, translate only from `activated_at`; a row's `duration_s` is
the sum of its steps' durations once the row is terminal.

The tracker holds a lock of its own so a mark from `stop()` on another thread
can never race a status read; it never blocks on anything else.
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from itertools import islice
from typing import TYPE_CHECKING, Any, Callable, Mapping, Optional

from taskpaw_v3.monitors.subs.util import (
    MAX_FILM_MODELS,
    MODEL_LABEL_CHARS,
    bounded,
    step_numbers,
)

if TYPE_CHECKING:  # annotations only: job.py imports this module
    from taskpaw_v3.monitors.subs.job import SubsJob
    from taskpaw_v3.monitors.subs.translate import Translator

# ── AsrProgress (AC1) ─────────────────────────────────────────────────────
PHASES = 8
# Phase weights of a ~2 h film (A4, unverified split); they sum to 1.0.
WEIGHTS: dict[int, float] = {
    1: 0.03,
    2: 0.04,
    3: 0.01,
    4: 0.12,
    5: 0.75,
    6: 0.01,
    7: 0.02,
    8: 0.02,
}

_GATE = re.compile(r"\[(?:Qwen|Decoupled)Pipeline")
# k = 1..8 (qwen = anime-whisper/qwen3; decoupled mode logs the same 8 phases)
_PHASE = re.compile(r"\[(?:Qwen|Decoupled)Pipeline PID \d+\] Phase (\d):")
_GEN = re.compile(r"\[DecoupledPipeline\] Generating scene (\d+)/(\d+)")
# Only when an aligner is configured (anime-whisper: aligner=none).
_ALIGN = re.compile(r"\[DecoupledPipeline\] Aligning scene (\d+)/(\d+)")
_GEN_DONE = re.compile(r"\[DecoupledPipeline\] Steps 2-4: Complete")
_P5_MARK = re.compile(
    r"\[DecoupledPipeline\]|\[QwenPipeline\] Phase 5 assembly summary"
)
_FINAL = re.compile(
    r"\[(?:Qwen|Decoupled)Pipeline PID \d+\] Phase 8: \d+ subtitles "
    r"(?:in final output|passed through)"
)

_GEN_SHARE = 0.9  # generation's share of Phase 5; alignment the other 0.1
_MAX_PERCENT = 99  # 100 only once the job settles
_ETA_SAMPLES = 50
_ETA_MIN_ADVANCES = 2
_ETA_MIN_SPAN_S = 30.0
_ETA_AFTER_PHASE5_S = 60  # allowance for Phases 6–8
_MAX_DIGITS = 9  # a longer number is malformed (and int() of it could raise)


def _count(text: str) -> Optional[int]:
    return int(text) if len(text) <= _MAX_DIGITS else None


def _scene(m: Optional["re.Match[str]"]) -> Optional[tuple[int, int]]:
    """(i, N) of a scene counter, or None when malformed (i < 1, i > N,
    N < 1, absurdly long)."""
    if m is None:
        return None
    i, n = _count(m.group(1)), _count(m.group(2))
    if i is None or n is None or n < 1 or not 1 <= i <= n:
        return None
    return i, n


def _clock(now: object) -> Optional[float]:
    """A usable timestamp, or None (bool, NaN, inf and non-numbers are not)."""
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        return None
    t = float(now)
    return t if math.isfinite(t) else None


class AsrProgress:
    """WhisperJAV progress of ONE ASR attempt, fed the child's captured tail
    on every poll. Every update is a max, so a repeated (overlapping) tail is
    idempotent and the percent never goes backwards. Not thread-safe: feed and
    read it from the thread that polls the job."""

    def __init__(self, started_at: float) -> None:
        self._started_at = _clock(started_at)
        self._gate = False
        self._phase: Optional[int] = None
        self._n_gen: Optional[int] = None
        self._gen_i = 0
        self._align_i = 0
        self._gen_done = False
        self._final = False
        self._samples: list[tuple[float, int]] = []
        self._best: Optional[int] = None

    def feed_text(self, text: str, now: float) -> None:
        """Parse every line of `text` (any mix of old and new lines). A
        non-string is ignored; never raises."""
        if not isinstance(text, str):
            return
        gen_before = self._gen_i
        for line in text.splitlines():
            if not self._gate:
                if _GATE.search(line) is None:
                    continue
                self._gate = True
            self._parse(line)
        t = _clock(now)
        if self._gen_i > gen_before and t is not None:
            # One sample per poll that advanced: a burst is one advance.
            self._samples.append((t, self._gen_i))
            del self._samples[:-_ETA_SAMPLES]
        pct = self._percent()
        if pct is not None and (self._best is None or pct > self._best):
            self._best = pct

    def _parse(self, line: str) -> None:
        m = _PHASE.search(line)
        if m is not None:
            k = int(m.group(1))
            if 1 <= k <= PHASES:
                self._raise_phase(k)
        gen = _scene(_GEN.search(line))
        align = _scene(_ALIGN.search(line))
        done = _GEN_DONE.search(line) is not None
        if gen or align or done or _P5_MARK.search(line) is not None:
            self._raise_phase(5)  # C5: a later-phase marker implies ≥ 5
        if gen is not None:
            i, n = gen
            if self._n_gen is None:
                self._n_gen = n  # the first GEN line fixes the pass
            if n == self._n_gen:  # a step-down re-pass has another N (D9)
                self._gen_i = max(self._gen_i, i)
        if align is not None and align[1] == self._n_gen:
            self._align_i = max(self._align_i, align[0])
        if done:
            self._gen_done = True
        if _FINAL.search(line) is not None:
            self._final = True

    def _raise_phase(self, k: int) -> None:
        if self._phase is None or k > self._phase:
            self._phase = k

    def _phase5_fraction(self) -> float:
        n = self._n_gen
        if self._gen_done:
            if n and self._align_i:
                return _GEN_SHARE + (1 - _GEN_SHARE) * (self._align_i - 1) / n
            return _GEN_SHARE
        if n and self._gen_i:
            return _GEN_SHARE * (self._gen_i - 1) / n  # C3: logged before it runs
        return 0.0

    def _percent(self) -> Optional[int]:
        k = self._phase
        if k is None:
            return None
        if self._final:
            return _MAX_PERCENT
        f = self._phase5_fraction() if k == 5 else 0.0
        raw = 100 * (sum(WEIGHTS[j] for j in range(1, k)) + WEIGHTS[k] * f)
        return max(0, min(_MAX_PERCENT, round(raw)))

    def _eta_s(self) -> Optional[int]:
        """D10: only while Phase 5 generates, after ≥ 2 advances spanning
        ≥ 30 s; the remaining scenes at the observed rate + 60 s for 6–8."""
        n = self._n_gen
        if self._phase != 5 or self._gen_done or n is None:
            return None
        if len(self._samples) < _ETA_MIN_ADVANCES + 1:
            return None
        (t0, i0), (t1, i1) = self._samples[0], self._samples[-1]
        if t1 - t0 < _ETA_MIN_SPAN_S or i1 <= i0:
            return None
        rate = (i1 - i0) / (t1 - t0)
        return math.ceil((n - self._gen_i + 1) / rate + _ETA_AFTER_PHASE5_S)

    def snapshot(self, now: float) -> dict[str, Any]:
        """A fresh dict: `phase` (1..8), `phase_n` (8), `scene`, `scenes`,
        `percent` (0..99, monotonic), `eta_s`, `elapsed_s`. Before any
        pipeline tag (another engine) everything but `elapsed_s` is None."""
        t = _clock(now)
        elapsed: Optional[int] = None
        if t is not None and self._started_at is not None:
            elapsed = max(0, int(t - self._started_at))
        if not self._gate:
            return {
                "phase": None,
                "phase_n": None,
                "scene": None,
                "scenes": None,
                "percent": None,
                "eta_s": None,
                "elapsed_s": elapsed,
            }
        return {
            "phase": self._phase,
            "phase_n": PHASES,
            "scene": self._gen_i or None,
            "scenes": self._n_gen,
            "percent": self._best,
            "eta_s": self._eta_s(),
            "elapsed_s": elapsed,
        }


# ── parse_eta (N8) ────────────────────────────────────────────────────────
_ETA_MS = re.compile(r"(\d{1,2}):([0-5]\d)")
_ETA_HMS = re.compile(r"(\d{1,3}):([0-5]\d):([0-5]\d)")


def parse_eta(text: object) -> Optional[int]:
    """The capture's `eta` (`M:SS`, `MM:SS` or `H:MM:SS`) in seconds; anything
    else (`--`, `""`, a non-string) → None."""
    if not isinstance(text, str):
        return None
    s = text.strip()
    m = _ETA_MS.fullmatch(s)
    if m is not None:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = _ETA_HMS.fullmatch(s)
    if m is not None:
        return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
    return None


# ── FilmTracker (AC3) ─────────────────────────────────────────────────────
RESTORE = "restore"
ASR = "asr"
TRANSLATE = "translate"
JASNA_STEPS = (RESTORE, ASR, TRANSLATE)
AVSUBS_STEPS = (ASR, TRANSLATE)

MAX_ROWS = 12  # D4/N4 hard cap of `films`
FINISHED_ROWS = 3  # the most recently finished films kept in `films`
NAME_CHARS = 200

_TERMINAL = frozenset({"done", "failed", "skipped"})
_OUTCOME_STATE = {"completed": "done", "failed": "failed", "skipped": "skipped"}


@dataclass(frozen=True)
class LiveFacts:
    """What the plugin derives at status time (validated and cached by view).

    `active`: step → the film live in it now; `waiting`: `(film, step)` of
    the GPU wait; `holder`: who holds the GPU (`""` when reserved for this
    run; capped at `NAME_CHARS`); `numbers`: step → live numbers of that
    step's active film."""

    active: Mapping[str, str] = field(default_factory=dict)
    waiting: Optional[tuple[str, str]] = None
    holder: str = ""
    numbers: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)


def progress_view(
    tracker: "FilmTracker",
    now: float,
    asr_job: Optional["SubsJob"],
    translator: Optional["Translator"],
    waiting: Optional[tuple[str, str]] = None,
    holder: str = "",
    restore: Optional[tuple[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    """#189: the per-film stepper of one status poll (`film`, `steps`,
    `films`, `films_more` — all or none) and, while a translation runs, its
    `model` — the one builder Jasna and avsubs share. Derived from what is
    live NOW (D3): `restore` is Jasna's `(film, capture numbers)` while its
    restore child runs; the ASR step is live while `asr_job` has a child
    (numbers: `asr_job.progress(now)`); the translate step while
    `translator.progress(now)` names a request. `waiting`/`holder` are the
    plugin's GPU-wait facts. `now` must be `time.monotonic()`. Read-only."""
    active: dict[str, str] = {}
    numbers: dict[str, dict[str, Any]] = {}
    if restore is not None:
        active[RESTORE] = restore[0]
        numbers[RESTORE] = step_numbers(restore[1])
    if asr_job is not None and asr_job.child is not None:
        active[ASR] = asr_job.job_id
        numbers[ASR] = step_numbers(asr_job.progress(now))
    request = translator.progress(now) if translator is not None else None
    job_id = request.get("job_id") if request is not None else None
    if isinstance(job_id, str):
        active[TRANSLATE] = job_id
        numbers[TRANSLATE] = step_numbers(request)
    view = tracker.view(LiveFacts(active, waiting, holder, numbers), now)
    model = numbers.get(TRANSLATE, {}).get("model")
    if view and model:
        view["model"] = model
    return view


@dataclass(frozen=True)
class _Live:
    """`LiveFacts` validated once: garbage entries dropped."""

    active: dict[str, str]
    waiting: Optional[tuple[str, str]]
    holder: str
    numbers: dict[str, dict[str, Any]]


def _validate(live: object) -> _Live:
    active: dict[str, str] = {}
    waiting: Optional[tuple[str, str]] = None
    holder = ""
    numbers: dict[str, dict[str, Any]] = {}
    if isinstance(live, LiveFacts):
        if isinstance(live.active, Mapping):
            for step, film in live.active.items():
                if isinstance(step, str) and isinstance(film, str):
                    active[step] = film
        w = live.waiting
        if (
            isinstance(w, tuple)
            and len(w) == 2
            and isinstance(w[0], str)
            and isinstance(w[1], str)
        ):
            waiting = (w[0], w[1])
        if isinstance(live.holder, str):
            holder = bounded(live.holder, NAME_CHARS)  # defence in depth
        if isinstance(live.numbers, Mapping):
            for step, nums in live.numbers.items():
                if isinstance(step, str) and isinstance(nums, Mapping):
                    numbers[step] = {
                        k: v
                        for k, v in nums.items()
                        if isinstance(k, str) and k not in ("key", "state")
                    }
    return _Live(active, waiting, holder, numbers)


@dataclass
class _Step:
    state: str = "pending"
    started: bool = False
    started_at: Optional[float] = None
    activated_at: Optional[float] = None
    ended_at: Optional[float] = None


@dataclass
class _Film:
    name: str
    order: int
    steps: dict[str, _Step]
    job: bool
    outcome: Optional[str] = None
    finished_wall: Optional[float] = None
    code: Optional[str] = None
    kept_ja: int = 0
    models: tuple[tuple[str, int], ...] = ()
    translate_s: Optional[float] = None


def _duration(key: str, s: _Step) -> Optional[int]:
    if s.state not in _TERMINAL or s.ended_at is None:
        return None
    begin = s.activated_at
    if begin is None and key != TRANSLATE:
        begin = s.started_at
    if begin is None:
        return None
    return max(0, int(s.ended_at - begin))


class FilmTracker:
    """Per-film step outcomes of one run; see the module docstring. Every
    `now` passed to it (marks, `observe`, `steps`, `view`) must be a
    `time.monotonic()` reading: stamps are subtracted from each other."""

    def __init__(
        self, steps: tuple[str, ...], *, wall_clock: Callable[[], float] = time.time
    ) -> None:
        try:
            given = tuple(steps)
        except TypeError:
            given = ()
        self._keys = tuple(k for k in JASNA_STEPS if k in given)
        self._films: dict[str, _Film] = {}
        self._wait: Optional[tuple[tuple[str, str], float]] = None
        self._lock = threading.RLock()
        self._last_live = _validate(None)
        self._extras: Optional[tuple[tuple[str, str], ...]] = None
        self._run = uuid.uuid4().hex
        self._page_error_logged = False
        self._run_films_error_logged = False
        self._wall_clock = wall_clock

    def set_extras(self, rows: list[tuple[str, str]]) -> None:
        """Retain the scan's untracked films once for this run (#198)."""
        with self._lock:
            if self._extras is None:
                self._extras = tuple(rows)

    def page(self, page: object, size: object) -> dict:
        """Local full-list read using only the last view's live facts (#198).

        Dict insertion order is plan order: add() assigns consecutive orders
        and films are never removed. Only the requested window builds rows.
        No observation or clock read: terminal marks may be newer than live.
        """
        limit = (
            max(1, min(50, size))
            if isinstance(size, int) and not isinstance(size, bool)
            else 10
        )
        with self._lock:
            lv = self._last_live
            extras = self._extras or ()
            tracked = len(self._films)
            total = tracked + len(extras)
            pages = max(1, (total + limit - 1) // limit)
            statuses = {n: self._status(f, lv)[0] for n, f in self._films.items()}
            focus = self._focus(lv, statuses)
            focus_page = (
                self._films[focus].order // limit + 1 if focus is not None else None
            )
            if page is None:
                selected = focus_page or 1
            elif isinstance(page, int) and not isinstance(page, bool) and page >= 1:
                selected = page
            else:
                selected = 1
            selected = min(selected, pages)
            begin, end = (selected - 1) * limit, selected * limit
            rows = [
                self._row(f, lv, *self._status(f, lv))
                for f in islice(
                    self._films.values(), min(begin, tracked), min(end, tracked)
                )
            ]
            rows.extend(
                {
                    "name": bounded(name, NAME_CHARS),
                    "steps": {},
                    "status": status,
                    "percent": None,
                    "eta_s": None,
                    "duration_s": None,
                }
                for name, status in extras[
                    max(0, begin - tracked) : max(0, end - tracked)
                ]
            )
            return {
                "run": self._run,
                "total": total,
                "size": limit,
                "page": selected,
                "pages": pages,
                "focus": bounded(focus, NAME_CHARS) if focus is not None else None,
                "focus_page": focus_page,
                "films": rows,
            }

    @staticmethod
    def _effective_outcome(f: _Film) -> str:
        restore = f.steps.get(RESTORE)
        if restore is not None and restore.state == "failed":
            return "restore_failed"
        if f.code is not None:
            return "skipped:other" if f.code == "restore_failed" else f.code
        if f.outcome == "completed":
            return "partial" if f.kept_ja else "translated"
        if f.outcome == "failed":
            return "failed"
        return "skipped:other"

    def _run_row(self, f: _Film, lv: _Live, status: str, key: Optional[str]) -> dict:
        terminal = status in _TERMINAL
        restore = f.steps.get(RESTORE)
        duration: Optional[float] = None
        if terminal:
            parts = [
                f.translate_s
                if k == TRANSLATE and f.translate_s is not None
                else _duration(k, s)
                for k, s in f.steps.items()
            ]
            known = [d for d in parts if d is not None]
            duration = sum(known) if known else None
        return {
            "name": bounded(f.name, NAME_CHARS),
            "restore": self._derived(f, RESTORE, lv) if restore is not None else None,
            "restored_before": bool(
                restore is not None
                and restore.state == "done"
                and restore.started_at is None
                and restore.activated_at is None
                and restore.ended_at is None
            ),
            "asr": self._derived(f, ASR, lv) if ASR in f.steps else None,
            "translate": self._derived(f, TRANSLATE, lv)
            if TRANSLATE in f.steps
            else None,
            "percent": lv.numbers.get(key, {}).get("percent")
            if status == "active" and key
            else None,
            "outcome": self._effective_outcome(f) if terminal else None,
            "kept_ja": f.kept_ja,
            "models": [[label, lines] for label, lines in f.models],
            "duration_s": duration,
            "finished_at": f.finished_wall,
        }

    def run_films(self, filter: object, page: object, size: object) -> dict:
        """This run only (#200): a side-effect-free read of retained facts."""
        selected_filter = (
            filter
            if isinstance(filter, str) and filter in ("done", "open", "all")
            else "done"
        )
        limit = max(1, min(50, size)) if type(size) is int else 10
        with self._lock:
            lv = self._last_live
            states = {n: self._status(f, lv) for n, f in self._films.items()}
            focus = self._focus(lv, {n: st for n, (st, _) in states.items()})
            done, opened = [], []
            totals = dict.fromkeys(
                (
                    "translated",
                    "partial",
                    "has_subs",
                    "untranslated",
                    "failed",
                    "restore_failed",
                ),
                0,
            )
            for f in self._films.values():
                if states[f.name][0] not in _TERMINAL:
                    opened.append(f)
                    continue
                done.append(f)
                code = self._effective_outcome(f)
                if code in ("has_subs", "skipped:subtitle_exists"):
                    bucket = "has_subs"
                elif code in ("failed", "asr_failed"):
                    bucket = "failed"
                elif code in ("translated", "partial", "restore_failed"):
                    bucket = code
                else:
                    bucket = "untranslated"
                totals[bucket] += 1
            done.sort(key=lambda f: (-self._ended(f), f.order))
            rank = {"active": 0, "waiting_gpu": 1, "queued": 2, "pending": 3}
            opened.sort(
                key=lambda f: (f.name != focus, rank.get(states[f.name][0], 3), f.order)
            )
            counts = {"done": len(done), "open": len(opened), "all": len(states)}
            films = (
                done
                if selected_filter == "done"
                else opened
                if selected_filter == "open"
                else opened + done
            )
            total = len(films)
            pages = max(1, (total + limit - 1) // limit)
            selected = min(page, pages) if type(page) is int and page >= 1 else 1
            return {
                "run": self._run,
                "filter": selected_filter,
                "total": total,
                "size": limit,
                "page": selected,
                "pages": pages,
                "focus": bounded(focus, NAME_CHARS) if focus is not None else None,
                "counts": counts,
                "totals": totals,
                "films": [
                    self._run_row(f, lv, *states[f.name])
                    for f in films[(selected - 1) * limit : selected * limit]
                ],
            }

    def _terminal_hook(self, f: _Film) -> None:
        # Called after ALL facts of a mark, under _lock. Never stores a fallback.
        if f.finished_wall is None and self._status(f, self._last_live)[0] in _TERMINAL:
            f.finished_wall = self._wall_clock()

    # ── marks ────────────────────────────────────────────────────────────
    def add(self, film: str, initial: dict[str, str]) -> None:
        if not isinstance(film, str):
            return
        with self._lock:
            if film in self._films:
                return
            steps = {k: _Step() for k in self._keys}
            if isinstance(initial, Mapping):
                for k, state in initial.items():
                    if k in steps and isinstance(state, str) and state in _TERMINAL:
                        steps[k].state = state
            subs = [s for k, s in steps.items() if k != RESTORE]
            job = any(s.state not in _TERMINAL for s in subs)
            self._films[film] = _Film(film, len(self._films), steps, job)
            self._terminal_hook(self._films[film])

    def _step(self, film: object, step: object) -> Optional[_Step]:
        if not isinstance(film, str) or not isinstance(step, str):
            return None
        f = self._films.get(film)
        return None if f is None else f.steps.get(step)

    def start(self, film: str, step: str, now: float) -> None:
        with self._lock:
            s = self._step(film, step)
            if s is None or s.state in _TERMINAL:
                return
            if not s.started:
                s.started = True
                s.started_at = _clock(now)

    def activate(self, film: str, step: str, now: float) -> None:
        t = _clock(now)
        if t is None:
            return
        with self._lock:
            s = self._step(film, step)
            if s is None or s.state in _TERMINAL or s.activated_at is not None:
                return
            s.activated_at = t
            if not s.started:
                s.started = True
                s.started_at = t

    def finish(
        self,
        film: str,
        step: str,
        state: str,
        now: float,
        *,
        code: Optional[str] = None,
    ) -> None:
        if not isinstance(state, str) or state not in _TERMINAL:
            return
        with self._lock:
            s = self._step(film, step)
            if s is None or s.state in _TERMINAL:
                return
            s.state = state
            s.ended_at = _clock(now)
            f = self._films[film]
            if step == TRANSLATE and state == "done" and f.outcome is None:
                f.outcome = "completed"
            if f.code is None and code is not None:
                f.code = code
            self._terminal_hook(f)

    def settle_subs(
        self,
        film: str,
        outcome: str,
        now: float,
        *,
        code: Optional[str] = None,
        kept_ja: int = 0,
        models: tuple[tuple[str, int], ...] = (),
        translate_s: Optional[float] = None,
    ) -> None:
        if not isinstance(film, str) or not isinstance(outcome, str):
            return
        first = _OUTCOME_STATE.get(outcome)
        if first is None:
            return
        later = "done" if outcome == "completed" else "skipped"
        t = _clock(now)
        with self._lock:
            f = self._films.get(film)
            if f is None:
                return
            state = first
            for k, s in f.steps.items():
                if k == RESTORE or s.state in _TERMINAL:
                    continue
                s.state, s.ended_at = state, t
                state = later
            if f.outcome is None:
                f.outcome = outcome
                f.kept_ja = (
                    max(0, kept_ja)
                    if isinstance(kept_ja, int) and not isinstance(kept_ja, bool)
                    else 0
                )
                by_model: dict[str, int] = {}
                for item in models if isinstance(models, (tuple, list)) else ():
                    if not isinstance(item, (tuple, list)) or len(item) != 2:
                        continue
                    label, lines = item
                    if (
                        not isinstance(label, str)
                        or not isinstance(lines, int)
                        or isinstance(lines, bool)
                        or lines < 0
                    ):
                        continue
                    label = bounded(label, MODEL_LABEL_CHARS)
                    if not label:
                        continue
                    by_model[label] = by_model.get(label, 0) + lines
                f.models = tuple(
                    sorted(by_model.items(), key=lambda item: -item[1])[
                        :MAX_FILM_MODELS
                    ]
                )
                f.translate_s = translate_s
            if f.code is None and code is not None:
                f.code = code
            self._terminal_hook(f)

    def fail_restore(self, film: str, now: float, code: Optional[str]) -> None:
        """One atomic restore failure plus subtitle skip; never exposes half a mark."""
        with self._lock:
            s = self._step(film, RESTORE)
            if s is None:
                return
            if s.state not in _TERMINAL:
                s.state, s.ended_at = "failed", _clock(now)
            # RLock: settle runs the hook only after all subtitle facts are set.
            self.settle_subs(film, "skipped", now, code=code)

    def record(self, film: str) -> Optional[dict[str, Any]]:
        """A fresh copy of the stored record, None for an unknown film."""
        if not isinstance(film, str):
            return None
        with self._lock:
            f = self._films.get(film)
            if f is None:
                return None
            return {
                "name": f.name,
                "job": f.job,
                "outcome": f.outcome,
                "steps": {
                    k: {
                        "state": s.state,
                        "started": s.started,
                        "started_at": s.started_at,
                        "activated_at": s.activated_at,
                        "ended_at": s.ended_at,
                        "duration_s": _duration(k, s),
                    }
                    for k, s in f.steps.items()
                },
            }

    def restore_done(self, film: str) -> bool:
        """Whether `film`'s restore step is `done` (#189 D1). False for an
        unknown film, a non-string or a tracker without a restore step; never
        raises and copies nothing."""
        with self._lock:
            s = self._step(film, RESTORE)
            return s is not None and s.state == "done"

    # ── status time ──────────────────────────────────────────────────────
    def observe(self, live: LiveFacts, now: float) -> None:
        """Status-time side of the live facts: `activate` every live-active
        pair; keep the N6 wait stamp while the same `(film, step)` is derived
        waiting, drop it once it is not."""
        t = _clock(now)
        if t is None:
            return
        lv = _validate(live)
        with self._lock:
            for step, film in lv.active.items():
                self.activate(film, step, t)
            key: Optional[tuple[str, str]] = None
            if lv.waiting is not None:
                f = self._films.get(lv.waiting[0])
                if f is not None and lv.waiting[1] in f.steps:
                    if self._derived(f, lv.waiting[1], lv) == "waiting_gpu":
                        key = lv.waiting
            if key is None:
                self._wait = None
            elif self._wait is None or self._wait[0] != key:
                self._wait = (key, t)

    def _derived(self, f: _Film, key: str, lv: _Live) -> str:
        s = f.steps[key]
        if s.state in _TERMINAL:
            return s.state
        if lv.active.get(key) == f.name:
            return "active"
        if lv.waiting == (f.name, key):
            return "waiting_gpu"
        if key == TRANSLATE and s.started:
            return "queued"
        return "pending"

    def _status(self, f: _Film, lv: _Live) -> tuple[str, Optional[str]]:
        """(row status, the step it came from) — R1, first match wins."""
        restore = f.steps.get(RESTORE)
        if restore is not None:
            if restore.state == "failed":
                return "failed", RESTORE
            if restore.state not in _TERMINAL:
                return self._derived(f, RESTORE, lv), RESTORE
        if f.outcome is not None:
            return _OUTCOME_STATE[f.outcome], None
        if not f.job:
            return "done", None
        for k, s in f.steps.items():
            if k != RESTORE and s.state not in _TERMINAL:
                return self._derived(f, k, lv), k
        # Only reachable through direct finish() marks without a settle.
        states = {s.state for k, s in f.steps.items() if k != RESTORE}
        if "failed" in states:
            return "failed", None
        return ("skipped" if "skipped" in states else "done"), None

    def row_status(self, film: str, live: LiveFacts) -> Optional[str]:
        if not isinstance(film, str):
            return None
        with self._lock:
            f = self._films.get(film)
            return None if f is None else self._status(f, _validate(live))[0]

    def statuses(self, live: LiveFacts) -> dict[str, str]:
        """Every film's row status, in plan order (uncapped)."""
        lv = _validate(live)
        with self._lock:
            return {name: self._status(f, lv)[0] for name, f in self._films.items()}

    @staticmethod
    def _ended(f: _Film) -> float:
        stamps = [s.ended_at for s in f.steps.values() if s.ended_at is not None]
        return max(stamps) if stamps else -math.inf

    def _focus(self, lv: _Live, status: dict[str, str]) -> Optional[str]:
        for step in (RESTORE, ASR):  # the GPU child
            film = lv.active.get(step)
            if film in self._films:
                return film
        if lv.waiting is not None and lv.waiting[0] in self._films:
            return lv.waiting[0]
        film = lv.active.get(TRANSLATE)
        if film in self._films:
            return film
        for name, st in status.items():  # the next film still to finish
            if st not in _TERMINAL:
                return name
        finished = [f for f in self._films.values() if status[f.name] in _TERMINAL]
        if not finished:
            return None
        return max(finished, key=lambda f: (self._ended(f), f.order)).name

    def focus(self, live: LiveFacts) -> Optional[str]:
        """GPU child (restore/asr) > waiting_gpu > translating > next film
        still to finish (plan order) > last finished; None without films."""
        lv = _validate(live)
        with self._lock:
            status = {n: self._status(f, lv)[0] for n, f in self._films.items()}
            return self._focus(lv, status)

    def _steps(self, f: _Film, lv: _Live, t: Optional[float]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for k, s in f.steps.items():
            state = self._derived(f, k, lv)
            item: dict[str, Any] = {"key": k, "state": state}
            if state in _TERMINAL:
                d = _duration(k, s)
                if d is not None:
                    item["duration_s"] = d
            elif state == "active":
                item.update(lv.numbers.get(k, {}))
            elif state == "waiting_gpu":
                item["holder"] = lv.holder
                w = self._wait
                if w is not None and w[0] == (f.name, k) and t is not None:
                    item["waited_s"] = max(0, int(t - w[1]))
            out.append(item)
        return out

    def steps(self, film: str, live: LiveFacts, now: float) -> list[dict[str, Any]]:
        """The stepper list of one film: `{"key", "state"}` + `duration_s`
        (terminal, when known), the step's live numbers (active), `holder` and
        `waited_s` (waiting_gpu; `waited_s` needs a prior `observe`)."""
        if not isinstance(film, str):
            return []
        lv = _validate(live)
        with self._lock:
            f = self._films.get(film)
            return [] if f is None else self._steps(f, lv, _clock(now))

    def _row(self, f: _Film, lv: _Live, status: str, key: Optional[str]) -> dict:
        nums = lv.numbers.get(key, {}) if status == "active" and key else {}
        duration: Optional[int] = None
        if status in _TERMINAL:
            parts = [_duration(k, s) for k, s in f.steps.items()]
            known = [d for d in parts if d is not None]
            duration = sum(known) if known else None
        return {
            "name": bounded(f.name, NAME_CHARS),
            "steps": {k: self._derived(f, k, lv) for k in f.steps},
            "status": status,
            "percent": nums.get("percent"),
            "eta_s": nums.get("eta_s"),
            "duration_s": duration,
        }

    def _rows(self, lv: _Live, focus: object) -> tuple[list[dict[str, Any]], int]:
        status = {n: self._status(f, lv) for n, f in self._films.items()}
        by_state: dict[str, list[str]] = {}
        for name, (st, _key) in status.items():
            by_state.setdefault(st, []).append(name)
        finished = sorted(
            (self._films[n] for n, (st, _k) in status.items() if st in _TERMINAL),
            key=lambda f: (self._ended(f), f.order),
            reverse=True,
        )
        candidates: list[str] = []
        if isinstance(focus, str) and focus in self._films:
            candidates.append(focus)
        candidates += by_state.get("active", [])
        candidates += by_state.get("waiting_gpu", [])
        candidates += by_state.get("queued", [])
        candidates += [f.name for f in finished[:FINISHED_ROWS]]
        candidates += by_state.get("pending", [])
        picked: list[str] = []
        for name in candidates:
            if len(picked) >= MAX_ROWS:
                break
            if name not in picked:
                picked.append(name)
        picked.sort(key=lambda n: self._films[n].order)
        rows = [self._row(self._films[n], lv, *status[n]) for n in picked]
        return rows, len(self._films) - len(rows)

    def rows(
        self, live: LiveFacts, focus: Optional[str]
    ) -> tuple[list[dict[str, Any]], int]:
        """(`films`, `films_more`) — N4: picked by priority (focus, active,
        waiting, queued nearest first, last 3 finished, pending) up to
        `MAX_ROWS`, then sorted by plan order; the rest are counted."""
        lv = _validate(live)
        with self._lock:
            return self._rows(lv, focus)

    def view(self, live: LiveFacts, now: float) -> dict[str, Any]:
        """`observe(live, now)`, then the metrics block for the focus film:
        `{"film", "steps", "films", "films_more"}`; `{}` without films."""
        self.observe(live, now)
        lv = _validate(live)
        with self._lock:
            self._last_live = lv
            status = {n: self._status(f, lv)[0] for n, f in self._films.items()}
            focus = self._focus(lv, status)
            if focus is None:
                return {}
            rows, more = self._rows(lv, focus)
            return {
                "film": bounded(focus, NAME_CHARS),
                "steps": self._steps(self._films[focus], lv, _clock(now)),
                "films": rows,
                "films_more": more,
            }


def read_film_page(tracker: FilmTracker, page: object, size: object) -> Optional[dict]:
    """Plugin boundary: contain errors and log only once per tracker (#198 V3-2)."""
    try:
        return tracker.page(page, size)
    except Exception as exc:
        with tracker._lock:
            if not tracker._page_error_logged:
                tracker._page_error_logged = True
                logging.getLogger(__name__).warning(
                    "Film page unavailable (%s)", type(exc).__name__
                )
        return None


def read_run_films(
    tracker: FilmTracker, filter: object, page: object, size: object
) -> Optional[dict]:
    """#200 plugin boundary, logged once per tracker just like #198."""
    try:
        return tracker.run_films(filter, page, size)
    except Exception as exc:
        with tracker._lock:
            if not tracker._run_films_error_logged:
                tracker._run_films_error_logged = True
                logging.getLogger(__name__).warning(
                    "Run films unavailable (%s)", type(exc).__name__
                )
        return None
