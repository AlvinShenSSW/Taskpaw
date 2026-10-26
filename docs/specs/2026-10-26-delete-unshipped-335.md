# #335 — Delete unshipped code: V2 flat scripts, the unmounted workflow HTTP layer, the CVE scanner

Date: 2026-10-26 · Base: `cb9aca4` (main, 3.10.6) · Branch: `afk/delete-unshipped-335`
Planner: Claude (driver). Implementer: Claude Opus 5.5. Gate: Codex astra xhigh.

## Spec review

Issue #335 lists code that nothing ships, imports or runs. Verified on
`origin/main` by the 2026-10-26 audit and re-checked by the planner:

- **V2 flat scripts.** `taskpaw.py`, `taskpaw_hub.py`, `macsubs.py`,
  `TaskPaw.spec`, `build.bat`, `build_hub.sh`. `scripts/build.py` builds only
  `taskpaw_v3/packaging/taskpaw-backend.spec`; `release.yml` and `ci.yml`
  never name them; `pyproject.toml` excludes them from ruff, mypy and
  coverage (lines ~54-92). `taskpaw_v3/**` production code never imports them.
- **Top-level `tests/`.** `tests/test_event_delivery.py`, `tests/test_smoke.py`
  and `tests/test_dependency_compatibility.py` exist for the V2 scripts (the
  audit read: imports `taskpaw` / `taskpaw_hub`, `py_compile` of the scripts,
  `ast` parse of `taskpaw.py`); `tests/test_coverage_config.py` and
  `tests/test_mac_monitor_compat.py` reference `taskpaw_v3` and stay.
  `tests/test_dependency_scan.py` belongs to the scanner below.
- **Unmounted workflow HTTP layer.** `taskpaw_v3/workflow/api.py`
  (`create_worker_app` requiring HTTPS, `_WorkflowBoundary`, pairing route,
  `create_local_app`). No shipped module imports it (`grep -rn "workflow.api\|workflow import api"`
  over `taskpaw_v3` excluding tests: nothing); the real worker API is
  `agent/server/worker_api.py`. `tests/test_workflow_api.py` tests only it.
  `TlsBinding` **stays** (used by `workflow/local_runtime.py:256` and two
  test modules). `WorkerStore.get_authority_binding` (`worker_store.py:799`)
  has `api.py:326` as its only caller and is deleted with it (critic C3).
  `transport_required` is a string literal inside `api.py`, nothing to keep.
  Every other name `api.py` imports has another consumer (critic C3).
- **CVE scanner.** `scripts/dependency_scan.py`, `tests/test_dependency_scan.py`,
  `.github/workflows/dependency-scan.yml`, `docs/guides/dependency-security.md`
  (guide for the scanner only — check its content; delete if it documents
  nothing else).
- **Docs.** `DEPLOYMENT_GUIDE.md` is V2-only; `README.md`, `AGENTS.md`,
  `CLAUDE.md`, `scripts/setup-agent.ps1` (comment) and `docs/guides/deployment.md`
  mention the V2 scripts.

Core need: remove dead weight (about 5.7k lines of scripts, 2.8k of scanner and
tests, 0.5k of unmounted API) without touching anything that ships.

## Acceptance criteria

- AC1 The files listed under "V2 flat scripts" and the three V2-only top-level
  tests are deleted. `taskpaw_v3/tests/test_outbox_migration.py:30` AST-parses
  `taskpaw_hub.py` to extract the real `delivery_outbox` DDL (critic C11):
  that string is inlined as a constant in the test (the V2 DDL frozen at
  tag `v3.10.6`) so the test keeps its meaning without the script, and the
  then-unused `import ast` is dropped (critic C13); `tests/test_coverage_config.py` and
  `tests/test_mac_monitor_compat.py` stay. Before deleting each test file the
  implementer confirms by reading it that it covers nothing in `taskpaw_v3`;
  a file that does is kept and reported.
- AC2 `taskpaw_v3/workflow/api.py` and `taskpaw_v3/tests/test_workflow_api.py`
  are deleted, together with `WorkerStore.get_authority_binding` (its only
  caller) and the then-unused `TlsBinding` import at `worker_store.py:29`
  (ruff F401; critic C9). The `TlsBinding` model itself stays. The implementer
  re-greps each name `api.py` imports; a symbol with another consumer stays.
- AC3 The scanner (`scripts/dependency_scan.py`), its test
  (`tests/test_dependency_scan.py`), its workflow
  (`.github/workflows/dependency-scan.yml`) and its exceptions file
  (`docs/security/dependency-exceptions.json`, read only by the scanner —
  critic C5) are deleted. `docs/guides/dependency-security.md` **stays** with
  its scanner sections removed; its "Targeted R15 decisions and exposure" and
  "Rebuild and package boundaries" sections record dependency decisions
  unrelated to the scanner (critic C4); inside those retained sections the
  references to the "dedicated Pillow compatibility test"
  (`tests/test_dependency_compatibility.py`, deleted under AC1) and to
  "scanner severity" are rewritten as plain statements of the decision
  (critic C10). `docs/guides/v01-merged-acceptance-preparation.md` line ~25
  drops the links to the deleted scanner files and line ~85 loses the stale
  "现有独立流程" wording; its links to `dependency-security.md` (lines ~27, 85)
  stay because that guide stays.
- AC3b `tests/test_smoke.py` also carried `test_shell_script_parses` (`bash -n`
  over `scripts/**/*.sh`, today only `scripts/recon/moomoo_probe.sh`, a
  moomoo-era probe). Decision: dropped with the file; nothing shipped depends
  on that script (critic C2).
- AC4 `pyproject.toml` loses every V2 mention: the `[tool.uv]` comment (lines
  ~54-55, reworded; `package = false` stays), the ruff comment and
  `extend-exclude` (~64-73), the mypy header line (~79) and the coverage
  comment (~91-92). `testpaths` keeps `tests` (two files remain there; CI runs
  bare `pytest --cov`).
- AC5 `DEPLOYMENT_GUIDE.md` is replaced by a three-line pointer to
  `docs/guides/deployment.md`. `README.md`, `AGENTS.md`, `CLAUDE.md`,
  `docs/constitution.md` (line 9, the "V2 is frozen" rule becomes "V2 was
  removed in 3.10.x; tag `v3.10.6` holds it"; critic C12),
  `docs/guides/deployment.md`, `scripts/setup-agent.ps1` no longer tell the
  reader to run or build the V2 scripts (one sentence may say V2 was removed
  in 3.10.x). Historical docs under `docs/audits/` and `docs/specs/` are left
  as they are.
- AC6 `uv run python scripts/build.py --skip-tauri` succeeds; the full test
  suite, ruff, ruff format, mypy and `cd taskpaw_v3/ui && npm run lint` are
  green; `git grep` for each deleted basename matches only historical docs
  under `docs/audits/`, `docs/specs/`, `scripts/recon/README.md` and
  provenance comments in code that cite V2 line numbers (critic C14); no
  import, `ast.parse`, `py_compile`, build or CI reference remains.
- AC7 CHANGELOG entry under a new newest heading, no version bump.

## Frozen issue contract

- Invariants kept: nothing under `taskpaw_v3/` that is imported by shipped code
  changes; the packaging spec, CI and release workflows are untouched except
  for the deleted scanner workflow; no behaviour change at runtime.
- Allowed user-visible change: the repository no longer carries the V2
  programs or the scanner.
- Non-goals: `lada`, `dev_activity`, `integrations/activity_*`,
  `presets/moomoo.py`, `scripts/macos_release.py` (used by `scripts/build.py`),
  any live isolation/credential mechanism, any refactor, any UI change.
- Causal boundary: file deletions listed above; `pyproject.toml` exclusions;
  the five doc files; CHANGELOG.

## Assumptions

- A1 `ci.yml` runs `pytest` with `testpaths` from `pyproject.toml`; removing
  V2 tests only shrinks the run (read, not executed).
- A2 `docs/guides/dependency-security.md` documents only the scanner (title
  read; content to be confirmed by the implementer before deletion).

## Approach

Plain `git rm` of the listed files, then the config and doc edits; no code
rewrites. Order: delete, run `scripts/build.py --skip-tauri`, run lint/mypy
(they will point at any remaining reference), run the suite.

## Files to change

| path | change | reason |
|---|---|---|
| `taskpaw.py`, `taskpaw_hub.py`, `macsubs.py`, `TaskPaw.spec`, `build.bat`, `build_hub.sh` | delete | AC1 |
| `tests/test_event_delivery.py`, `tests/test_smoke.py`, `tests/test_dependency_compatibility.py` | delete after reading | AC1 |
| `taskpaw_v3/workflow/api.py`, `taskpaw_v3/tests/test_workflow_api.py` | delete | AC2 |
| `scripts/dependency_scan.py`, `tests/test_dependency_scan.py`, `.github/workflows/dependency-scan.yml`, `docs/security/dependency-exceptions.json` | delete | AC3 |
| `docs/guides/dependency-security.md`, `docs/guides/v01-merged-acceptance-preparation.md` | drop scanner sections / links | AC3 |
| `taskpaw_v3/workflow/worker_store.py` | delete `get_authority_binding` and the `TlsBinding` import | AC2 |
| `taskpaw_v3/tests/test_outbox_migration.py` | inline the V2 `delivery_outbox` DDL constant | AC1 |
| `docs/constitution.md` | V2 rule rewritten | AC5 |
| `pyproject.toml` | drop V2 exclusions | AC4 |
| `DEPLOYMENT_GUIDE.md`, `README.md`, `AGENTS.md`, `CLAUDE.md`, `docs/guides/deployment.md`, `scripts/setup-agent.ps1` | remove V2 instructions | AC5 |
| `CHANGELOG.md` | entry | AC7 |

## Execution surface

Writes: the files above at the repository root of the issue branch. Executes:
`uv run python scripts/build.py --skip-tauri`, `uv run ruff check . && uv run
ruff format --check . && uv run mypy`, `cd taskpaw_v3/ui && npm run lint`, the
full `uv run pytest` (basetemp `<LOCALAPPDATA>/taskpaw-pytest/afk-335`).
`git grep` for each deleted path.

## Key implementation notes

- `pyproject.toml` has prose comments about V2 around the ruff, mypy and
  coverage sections; remove the entries and the comments that refer to the
  deleted files, nothing else.
- `AGENTS.md` lists the V2 scripts in its roster/commands; keep the file's
  structure, drop the rows and the `py_compile` command.
- Mention in `README.md` where V2 lives for the record: the `v3.10.6` tag.

## Risk assessment

| risk | likelihood | impact | mitigation |
|---|---|---|---|
| A top-level test covers V3 behaviour under a V2 name | low | lost coverage | AC1 read-before-delete; suite stays green |
| An `api.py` import kept a symbol alive that mypy now flags unused | low | lint red | AC2 re-grep per imported name |

## Out of scope

Everything in Non-goals; CI tuning; the `scripts/macos_release.py` signing code.

## Test plan

No new tests. Build, lint, mypy, UI lint and the full suite on the final
commit; `git grep` evidence recorded in the handoff.

## Debate record

- v1 → v2 (critic round 1): C1 supported; C2 (minor) the `bash -n` gate in
  `test_smoke.py` is dropped knowingly (AC3b); C3 (P2) `get_authority_binding`
  is dead after the deletion and goes with it, `transport_required` is a
  literal; C4 (P2) `dependency-security.md` keeps two non-scanner sections and
  the acceptance-preparation guide loses its links; C5 (P2) the exceptions
  JSON is deleted with the scanner; C6 (minor) the `[tool.uv]` comment is
  reworded; C7, C8 no finding.
- v2 → v3 (critic round 2): C2, C5, C6 resolved. C9 (P2) the `TlsBinding`
  import in `worker_store.py` becomes unused → dropped. C10 (P2) the retained
  guide sections referenced the deleted Pillow test; rewritten; acceptance
  guide line details corrected. C11 (P1) `test_outbox_migration.py` parses
  `taskpaw_hub.py` → the DDL is inlined. C12 (P2) `docs/constitution.md:9`
  rewritten.
- Round 3 (critic): C9, C10, C11, C12 resolved. C13 (minor) `import ast`
  dropped; C14 (minor) AC6 reworded to allow provenance comments. The
  scanner-only "Exact temporary exceptions" section of the dependency guide
  goes with the other scanner sections. Wording only; clean round, debate
  closed.

## Handoff notes

Commit `chore: delete the V2 flat scripts, the unmounted workflow API and the CVE scanner (#335) [skip ci]`.
