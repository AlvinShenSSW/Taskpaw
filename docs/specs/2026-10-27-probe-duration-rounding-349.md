# #349 — intake: ffprobe durations that straddle a half-millisecond are not a conflict

Date: 2026-10-27 · Base: `8b35c43` (main, 3.11.1) · Branch: `afk/probe-duration-rounding`
Planner: Claude (driver). Implementer: Claude Opus 5.5. Gate: Codex astra xhigh.

## Spec review

`parse_ffprobe` (`taskpaw_v3/workflow/media_facts.py` ~400-410) takes the primary
video stream's duration observations (`stream` from `duration`, `ticks` from
`duration_ts × time_base`), rounds each to whole milliseconds half-up
(`_milliseconds`) and sets `duration_conflict` when the rounded values differ;
`duration_ms`/`duration_source` then stay `None`. `RawMediaFacts._relations`
(`raw_media_models.py` ~184-196) re-checks the same rule on every load
(`milliseconds != {duration_ms}` → `duration_projection_mismatch`).

ffprobe prints `duration` with six decimals (microseconds). When the exact tick
duration lies within 0.5 µs below a half-millisecond, the printed value is rounded
up onto the boundary and the two observations round to different milliseconds:

| film | stream | ticks (exact) | ms |
|---|---|---|---|
| MKON-151-4K | 7711.203500 s | 7711.2034999 s | 7711204 / 7711203 |
| GVH-886 | 9799.322500 s | 9799.3224999 s | 9799323 / 9799322 |

`intake_build` then marks the ffprobe evidence `unknown`, `evaluate_eligibility`
blocks (`media_unknown`), and the package sits on 异常 forever. On the controller,
2 of 76 intake snapshots carry `duration_conflict`, exactly these two.

## Decision (revised after Codex r1)

When a stream has both a printed `duration` and `duration_ts`, the two round to
different milliseconds, and their exact values lie within 1 µs (ffprobe's print
precision), the parser drops the printed observation and keeps the exact `ticks`
one. `duration_ms` is then the ticks value (7711203 / 9799322 ms) with source
`ticks`. Everything else is unchanged: a real 1 ms disagreement (10.0005 s vs
10.0015 s) is still a conflict, equal rounded milliseconds still agree, and
`RawMediaFacts._relations` is not touched.

Why not widen the agreement rule (round 1 design): remote workers on 3.11.1
re-validate the controller's raw facts with the old equal-milliseconds rule
(`transfer.envelope()` → `RemoteDispatcher._claim_and_run()`), so widened facts
would be rejected with `duration_projection_mismatch` and the film could not be
dispatched under a controller-only update. Dropping the redundant print-rounded
observation produces facts every released validator accepts.

## Acceptance criteria

- AC1 A stream with `duration` = "7711.203500", `duration_ts` = 77112034999,
  `time_base` = 1/10000000 parses with `duration_ms` 7711203, source `ticks`, only
  the `ticks` observation on the stream, no `duration_conflict`, and round-trips
  through the unchanged `RawMediaFacts` validation (the same rule old workers run).
- AC2 Existing tests unchanged and green; 10.000500 s vs ticks 10.0004989 s
  (1.1 µs apart, across a half-millisecond) is still a conflict.
- AC3 A `MediaProbe` built from the AC1 facts with status `passed` validates, and
  `evaluate_eligibility` with a matching profile returns `eligible`.
- AC4 `uv run pytest`, `ruff check`, `ruff format --check`, `mypy` green.

## Frozen issue contract

- Invariants: the half-up millisecond rounding; `RawMediaFacts` validation
  (shared with released workers); every input accepted today parses to the same
  facts; real conflicts (> 1 µs exact span and different milliseconds) stay
  conflicts.
- Allowed change: a print-rounded `duration` within 1 µs of `duration_ts` that
  rounds differently is dropped in favour of the ticks observation.
- Non-goals: re-probing packages already blocked (the operator touches a member's
  mtime after the controller update so the next scan re-admits it — the
  observation digest includes `mtime_ns`); container (`format`) duration; any UI.
- Causal boundary: `media_facts.py`, `tests/test_workflow_media_facts.py`,
  CHANGELOG.

## Test plan

RED: AC1/AC3 cases in `tests/test_workflow_media_facts.py` (synthetic ffprobe
JSON with the MKON-151-4K numbers). GREEN, then the full suite and lint.

## Debate record

- Round 1 (critic): no P1/P2. Verified the rule is a strict widening (stored
  snapshots with `duration_conflict` still load; `_relations` only checks when
  `duration_ms` is set); 1 µs is safe (stream `duration` and `duration_ts` come from
  the same value, printed with `%f`; other print forms are already
  `invalid_duration`; `format` never enters a stream's observations); no other
  consumer re-derives the rule; the mtime-touch re-admission path traced through
  `intake_store.observe_intake` (changed digest → new source generation → fresh
  probe). P3s applied: the load check keeps source membership and compares only
  milliseconds; the half-up formula lives once, in the shared helper; the first
  observation stays the chosen value.
- Evidence: the new parser on the two real files (ffprobe 7.1.1) gives 9799323 ms
  and 7711204 ms with no `duration_conflict`; the old parser reproduces the
  conflict; all 76 stored `MediaProbe`s on the controller re-validate.
- Internal review (r1 design): APPROVE-WITH-COMMENTS, P3s only (load check source
  leniency, a test just past the 1 µs edge, readability).
- Codex r1 (gpt-6-astra, xhigh): P1 — released workers re-validate raw facts with
  the old rule, so the widened facts break dispatch under a controller-only
  update (reproduced with the base revision's validator). Fixed by the revised
  decision above; the validator is untouched, which also retires the internal
  review's first and third P3; the edge test was added (AC2).
- Evidence (revised): real files give 9799322 / 7711203 ms from `ticks`, no
  conflict.
