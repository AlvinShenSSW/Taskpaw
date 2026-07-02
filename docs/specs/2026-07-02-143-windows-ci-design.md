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

## Windows triage (round 1 — surfaced by the new job)
The new `windows-latest` job immediately caught a real gap (as intended):
`test_shell_script_parses` failed on `scripts/recon/moomoo_probe.sh`. Root cause is
**not** a script bug — on windows-latest `shutil.which("bash")` resolves to the WSL
launcher `C:\Windows\System32\bash.exe`, which with no WSL distro installed prints an
"install a distribution" notice (UTF-16) and exits non-zero for every call, so
`bash -n` never actually parses.

The first fix attempt (a `_functional_bash()` probe running `bash -c "exit 0"`) still
failed on Windows: `shutil.which("bash")` resolves to Git Bash (probe passes), but
`subprocess.run(["bash", ...])` launches a *different* exe via CreateProcess —
`C:\Windows\System32\bash.exe`, the WSL launcher — which with no distro prints the
"install a distribution" notice (UTF-16) and exits non-zero. Probing one bash does
not predict the other.

Final fix (test-robustness in `tests/test_smoke.py` — a test file, not frozen V2):
these are **POSIX (macOS/Linux) setup scripts, never executed on Windows**, so skip
`test_shell_script_parses` on Windows outright (`platform.system() == "Windows"`),
and keep the `_functional_bash()` guard for non-Windows runners. The product/V3 test
suite (433 tests) still runs on windows-latest — that is the coverage #143 wants;
syntax-checking POSIX shell scripts on Windows adds nothing. Verified locally on
macOS: the test still runs and passes (not skipped).

## Constitution gate
- §1 Scope: CI config only; no source touched.
- §5 Testing: suite must pass on the new runner before the PR is green.
- §3 Ports/§4 Reliability: no runtime change.
