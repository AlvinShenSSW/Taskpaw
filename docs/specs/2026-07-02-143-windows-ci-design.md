# Design: #143 Windows CI test job

Date: 2026-07-02 · Issue: #143 · Branch: `ci/143-windows-job`

The CI `test` job runs on `ubuntu-latest` only, but Windows is the **primary agent
target** (V2 `taskpaw.py`, V3 agent installers #126). Nothing runs the suite on
Windows, so path-separator / encoding / process-handling regressions reach the
fleet uncaught.

## Decision
Add a single **cheap** `windows-latest` entry to the existing `test` job's matrix
(not a new job), so it reuses the same uv-based steps:
```yaml
matrix:
  os: [ubuntu-latest]
  python-version: ["3.10", "3.12"]
  include:
    - os: windows-latest
      python-version: "3.12"
```
- `runs-on: ${{ matrix.os }}`; job name gains `· ${{ matrix.os }}` so the two
  py3.12 jobs (ubuntu vs windows) are distinguishable.
- **One** Windows entry on the latest supported Python — enough to catch OS-specific
  bugs without tripling CI minutes. Full os×python fan-out would be wasteful.
- Steps are unchanged and already cross-platform (`uv lock --check`, `uv sync
  --group dev`, `uv run pytest`) — uv provides the interpreter on Windows too.
- **Packaging (#126) stays separate** — this is the test suite only.

## Verification
This is pure CI config; there is no app-behaviour unit to add (constitution §5
targets behavioural change). The verification *is* the new job going green on
`windows-latest`. If it surfaces real Windows-specific test failures:
- a genuine V3 bug → fix it (in scope: making the new job green);
- a genuinely V2 (frozen) issue we cannot touch → `skipif`/`xfail` that test on
  Windows with a comment + flag it to the operator, rather than widen the V2 change.
Any such triage will be recorded here and in the PR before the job is declared green.

## Constitution gate
- §1 Scope: CI config only; no source touched.
- §5 Testing: suite must pass on the new runner before the PR is green.
- §3 Ports/§4 Reliability: no runtime change.
