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

## Decision

The observations agree when their rounded milliseconds are equal (today's rule,
unchanged) **or** their exact values span at most 1 µs (ffprobe's print
precision). In the second case `duration_ms` is the first observation's rounded
value and `duration_source` its source — the same choice as today.

Not "rounded values within 1 ms": `test_ticks_same_scope_half_up_and_conflict_retained`
pins a real 1 ms disagreement (10.0005 s vs 10.0015 s) as a conflict, and that
stays. Not "exact span ≤ 1 µs" alone: equal rounded milliseconds with a larger
exact span (10.0005 vs 10.0010 s, also pinned) are accepted today, and stored
snapshots are re-validated on load, so the rule may only widen.

One helper in `raw_media_models.py` holds the rule; `parse_ffprobe` and
`RawMediaFacts._relations` both call it, so the parse and the load-time check
cannot drift.

## Acceptance criteria

- AC1 A stream with `duration` = "7711.203500", `duration_ts` = 77112034999,
  `time_base` = 1/10000000 parses with `duration_ms` 7711204, source `stream`, and
  no `duration_conflict`; the result round-trips through `RawMediaFacts`
  validation.
- AC2 The existing tests stay green unchanged: a 1 ms real difference is still a
  conflict; equal rounded milliseconds still agree; missing durations still give
  `missing_duration`.
- AC3 A `MediaProbe` built from the AC1 facts with status `passed` validates, and
  `evaluate_eligibility` with a matching profile returns `eligible`.
- AC4 `uv run pytest`, `ruff check`, `ruff format --check`, `mypy` green.

## Frozen issue contract

- Invariants: the half-up millisecond rounding; the choice of the first
  observation; every input accepted today stays accepted with the same values;
  real conflicts (> 1 µs exact span and different milliseconds) stay conflicts.
- Allowed change: observations within 1 µs agree.
- Non-goals: re-probing packages already blocked (the operator touches a member's
  mtime after the controller update so the next scan re-admits it — the
  observation digest includes `mtime_ns`); container (`format`) duration; any UI.
- Causal boundary: `media_facts.py`, `raw_media_models.py`,
  `tests/test_workflow_media_facts.py`, CHANGELOG.

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
