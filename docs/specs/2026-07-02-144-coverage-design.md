# Design: #144 Coverage measurement (pytest-cov)

Date: 2026-07-02 · Issue: #144 · Branch: `ci/144-coverage`

~436 tests pass but nothing measures what they exercise. Add `pytest-cov` and a
coverage report in CI. **Measurement only — no hard threshold yet** (per the issue).

## Decisions
- **pytest-cov in the dev group** (`pytest-cov>=5.0,<8.0`), so `uv sync --group
  dev` gives CI and contributors the plugin. Lockfile regenerated (`uv lock`).
- **Coverage scoped to `taskpaw_v3` only.** `[tool.coverage.run] source =
  ["taskpaw_v3"]`. The frozen V2 scripts (`taskpaw.py` / `taskpaw_hub.py` /
  `macsubs.py`) are deliberately **out of source** so coverage never pressures V2
  churn — the issue's "exclude frozen V2 from any threshold" and "focus review on
  V3 supervisor/lifecycle". `branch = true` for branch coverage; `omit`
  `taskpaw_v3/tests/*`.
- **No `fail_under`.** Threshold deferred to a later issue once the V3
  supervisor/lifecycle gaps are triaged — adding one now would either be arbitrary
  or immediately red.
- **CI reports, local stays fast.** The CI test step runs `pytest --cov=taskpaw_v3
  --cov-report=term-missing` (both matrix pythons). `addopts` is **not** given
  `--cov`, so a bare `uv run pytest` stays uninstrumented and quick; CI opts in
  explicitly. No coverage upload service wired (out of scope; term report in the
  log is the deliverable).
- **Guard test** (`tests/test_coverage_config.py`): asserts pytest-cov is importable
  and the coverage config stays V3-scoped with the V2 scripts excluded. Uses
  text assertions on pyproject.toml because `tomllib` is 3.11+ and CI runs 3.10.

## Baseline observed (informational, not gated)
Local `pytest --cov=taskpaw_v3`: **TOTAL 85%** (3685 stmts). Lowest areas are the
plugins (`lada.py` 78%, `heartbeat.py` 79%) and parts of `supervisor.py` (90%) —
the lifecycle code the issue calls out. Recorded here to seed the future
threshold/triage issue.

## Test plan
- `uv run pytest --cov=taskpaw_v3 --cov-report=term-missing` → 436 passed, coverage
  table printed.
- `uv lock --check` clean; `uv run pytest` (no cov) still green.
- New guard tests pass on 3.10 and 3.12.

## Kimi 终审 triage (round 1)
Kimi flagged the guard test's string-splitting as fragile. Adopted its fix: parse
pyproject.toml with a real parser (`tomllib` on 3.11+, `tomli` on 3.10 — confirmed
present transitively via `pytest-cov → coverage[toml] → tomli`, marker
`python_full_version < '3.11'` in uv.lock) and assert the actual structure. Added a
third guard (`test_coverage_has_no_hard_threshold_yet`) so a future `fail_under`
must be introduced deliberately. Verified on both 3.13 and 3.10.

## Constitution gate
- §1 Scope: tooling/CI + config only; no V2 touched; V3 code unchanged.
- §5 Testing: the behavioural surface (coverage wiring) is guarded by
  test_coverage_config.py; lockfile authoritative and clean.
