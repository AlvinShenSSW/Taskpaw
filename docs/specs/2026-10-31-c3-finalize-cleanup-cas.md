# C3: finalize cleanup reuses a stale film snapshot (self-inflicted `cas_conflict`)

Date: 2026-10-31. Status: plan (not implemented). Issue: #378, under EPIC #375 (audit item 6).
Base: origin/main 3ddf070 (TaskPaw 4.0.2).

## Spec review

When a film's archive has succeeded, `Finalizer._cleanups`
(`taskpaw_v3/workflow/finalize.py:805-826`) runs two cleanup scopes in one
call: `workcopy` first, then `input`. Both scopes use the `wf` snapshot read once
in `_advance` (`finalize.py:544-547`).

1. The workcopy scope admits its operation through `_admit` → `store.begin_cleanup`
   (`finalize.py:1003-1028`).
2. `begin_cleanup` ends with `self._set_film(film)`
   (`taskpaw_v3/workflow/approval_store.py:921`), which bumps the film's version
   (`taskpaw_v3/workflow/film_store.py:273-284`).
3. The input scope then calls `_admit` with `expected_version=wf.version`, which is
   now stale. `begin_cleanup` checks it first (`approval_store.py:798-799`) and
   raises `WorkflowError("cas_conflict")`.
4. `run_once` (`finalize.py:528-533`) logs `finalize <id>: cas_conflict, retrying`.
   It drops that film's `worked` flag for the tick, and the input scope is admitted
   one finalize tick later.

`record_cleanup_result` does not touch the film row. It updates the operation, the
attempt and the stage (`approval_store.py:932-1027`, `_external_fact` at `:449-472`).
So `begin_cleanup` is the only bump between the two scopes.

**Evidence.**
- The issue counts 109 such lines for 109 archived films.
- On the controller, the current `taskpaw-backend-agent.log` holds 107 lines
  `cas_conflict, retrying`. The count is consistent with the issue once log rotation is
  allowed for. This was a read-only count.
- A hermetic experiment reproduces it. It ran a scratch test against this tree, with a
  temp basetemp:
  - on the baseline, the cleanup tick logs exactly one `cas_conflict, retrying`, and
    `cleanup_input` is still `pending` when `cleanup_workcopy` turns `succeeded`;
  - when `_cleanups` re-reads `wf` after the workcopy scope, both scopes succeed in the
    same tick with no conflict line;
  - an injected genuine conflict on the input admission is still logged once and
    retried on the next tick.

**Impact.** Finalize runs only between dispatch ticks
(`taskpaw_v3/workflow/local_dispatch.py:442-473`). Input cleanup, together with the
reclaim of the INBOX folder and readonly snapshot that hangs off it, therefore slips
by up to one whole local stage. ASR takes about 6 min.

**Core need.** The input scope must judge and CAS against the film as it is after the
workcopy admission, so that only a write by someone else can fail the CAS.

No ambiguity needs the operator.

## Acceptance criteria

1. Archiving a film admits both cleanup scopes in the same `run_once` tick.
   - No `cas_conflict` is logged for that film, provided the package does not hold
     the input scope back (`_package_archived`).
2. A film-version change that lands between the input scope's read and its
   `begin_cleanup` still raises `cas_conflict`. It is logged as
   `finalize <id>: cas_conflict, retrying`, keeps the previous waiting text, and is
   retried next tick. This is today's handling, unchanged.
3. If the approval changed after the workcopy scope, the input scope is not admitted
   that tick. Instead the waiting text `清理等待：审批已变化` is recorded, the same guard
   that `_cleanups` already applies on entry.
4. A regression test in `taskpaw_v3/tests/test_workflow_finalize.py` fails on 3ddf070
   and passes after the fix (AC 1). A second test covers AC 2.
5. `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and
   `uv run mypy` are clean.

## Frozen issue contract

- **Acceptance:** AC 1–5 above.
- **Invariants:**
  - The CAS protects exactly the snapshot on which the input-scope decisions are made:
    the approval guard, the `_package_archived` gate, which operations already exist,
    and `expected_version`.
  - Cleanup order stays workcopy → input.
  - The store-side checks in `begin_cleanup` are untouched: CAS, approval validity,
    binding equality, source admissibility, shared sources and the single cleanup
    adapter.
  - `run_once`'s handling of `cas_conflict` and of other `WorkflowError`s is untouched.
- **Allowed behaviour changes:**
  - The input cleanup starts in the same tick as the workcopy cleanup, instead of one
    tick later.
  - The self-inflicted `cas_conflict` log line disappears.
- **Non-goals:**
  - No store or schema change.
  - No change to `_admit`'s `expected_version` source.
  - No change to reclaim, retention, the package-sibling gate, or the archive half of
    finalize.
  - No retry loop inside a tick.
- **Smallest causal boundary:** `Finalizer._cleanups` in
  `taskpaw_v3/workflow/finalize.py`, plus new tests in
  `taskpaw_v3/tests/test_workflow_finalize.py`.

## Assumptions

- **A1 — the only film-version bump between the two scopes is `begin_cleanup`'s
  `_set_film`.** Verified by reading `approval_store.py:793-930` and `:932-1027`, and
  `film_store.py:273-322`.
  - If a future change adds another film bump on the workcopy path, the re-read still
    covers it, because it happens after the whole workcopy `_cleanup` call.
- **A2 — the extra `film_workflow` read is cheap enough.**
  - It is one indexed read per film, per tick, and only while that film's workcopy
    cleanup is unfinished (`read_store.py:316`).
  - Not measured. It is the same call `_advance` already makes for every open film on
    every tick.
- **A3 — the 107/109 log count is a field symptom of this path only.**
  - This is inferred from the exact 1:1 ratio with archived films and from the
    reproduction.
  - Not every line was correlated with a film id.

## Approach

Re-read the film workflow after the workcopy scope has been handled, before the input
scope is judged and admitted.

Reshape `_cleanups` slightly:

1. Keep the entry guard, the `final` lookup and `worked` as they are.
2. Iterate the two scope names in order. Read each scope's status from the current
   `wf` at the time it is judged, not from a tuple built up front.
3. After the workcopy scope's `self._cleanup(...)` call returns, whatever it returns:
   - re-read `wf = rt.store.film_workflow(rt.operator, wf.film_id)`;
   - re-apply the existing approval guard against the fresh `wf`. If it fails, record
     `清理等待：审批已变化` and `return worked`;
   - use `approval = wf.approval` for the input scope.
4. Do not re-read when the workcopy scope was skipped (its status is `succeeded`).
   Nothing was written in that case.

**Why re-read rather than return after the first scope that did work.** Returning early
removes the log line but keeps the one-tick slip, which is the user-visible impact the
issue names. It also does not meet AC 1 ("starts both scopes"), and it leaves the stale
snapshot one refactor away from biting again.

**Why not read the version inside `_admit` just before `begin_cleanup`.** That would
CAS against a version nobody judged, which defeats the optimistic lock. A genuine
concurrent change would never be detected (AC 2).

**Why re-read whenever the workcopy `_cleanup` was called, and not only when it
returned `True`.** `_cleanup` returns `False` after a successful admission when no member
needed deleting (`finalize.py:887-889`). In that case the version was still bumped.
Re-reading unconditionally after the call is simpler and always correct.

`final` is not re-read. It is keyed by `approval.binding.final_revision`. If the
approval changed, the re-applied guard returns before `final` is used again.

## Files to change

| Path | Change | Reason |
|---|---|---|
| `taskpaw_v3/workflow/finalize.py` | modify `_cleanups` (~lines 805-826) | Re-read `wf` and re-apply the approval guard after the workcopy scope; read scope status lazily. |
| `taskpaw_v3/tests/test_workflow_finalize.py` | add 2 tests | AC 1 regression (RED on 3ddf070), AC 2 genuine conflict still retried. |
| `CHANGELOG.md` | one line under the unreleased / 4.0.3 section, if the EPIC keeps one | Release notes. Follow the EPIC's convention. |

## Execution surface

- **Writes:** only the files listed above.
- **Read or executed, not modified:**
  - `approval_store.py`, `film_store.py` and `read_store.py`: read for the CAS
    semantics;
  - `local_dispatch.py`: read for the tick cadence;
  - the test helpers `test_workflow_return_step.py`, `test_workflow_scrape_step.py` and
    `workflow_fake_library.py`: used through the existing `World` fixture.
- **Commands:**
  - `uv run pytest taskpaw_v3/tests/test_workflow_finalize.py`, then the full
    `uv run pytest`;
  - `uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy`.
  - No generated outputs.
  - Side effects are confined to the pytest basetemp. See the handoff notes on its
    length.

## Key implementation notes

- **The status tuple must not be built from the stale `wf`.** Today `scopes` captures
  `wf.cleanup_input_status` before the loop. After the change, read the input status
  from the refreshed `wf`.
  - Today the value would be the same, because the workcopy path does not touch the
    input stage.
  - Reading it fresh is what keeps the CAS honest: every input decision comes from one
    snapshot.
- **The input scope's `_cleanup` must receive the refreshed `wf`.** It uses
  `wf.cleanup_operations`, `wf.package_path`, `wf.archive_receipt_id` and `wf.version`
  through `_admit`.
- **A genuine `cas_conflict` raised by the workcopy scope is unaffected.** It
  propagates out of `_cleanups` before the re-read, exactly as today.
- **Logger name.** `taskpaw.workflow.finalize` (`finalize.py:34`). Tests capture it with
  `caplog.at_level(logging.INFO, logger="taskpaw.workflow.finalize")`.
- **The `World` fixture wraps `film_workflow`.** It replaces `rt.store.film_workflow`
  with a wrapper that injects `package_path="pkg"` (`test_workflow_finalize.py:57-66`).
  The re-read goes through that wrapper, so the input scope still finds the INBOX
  originals. The prototype confirmed this.
- **Keep it small.** There are only two scopes, so a short explicit sequence is fine:
  workcopy step, re-read and guard, then input step. Do not introduce a generic scope
  framework.

## Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| The re-read masks a concurrent change that should have stopped the input admission | Low | Medium | The approval guard is re-applied on the fresh snapshot. `begin_cleanup` re-validates the approval, binding, source admissibility and sharing server-side. The CAS still covers the read → write window. AC 2 test. |
| The refreshed `wf` lacks fields that the stale one had (test wrapper or `package_path`) | Low | Low | `film_workflow` is the same call `_advance` uses. The prototype passed with the `World` wrapper. |
| The extra read adds lock contention on `workflow.db` | Low | Low | One read per film, only during the ticks in which a workcopy cleanup is pending. |
| Package siblings: the input scope still waits for `_package_archived` | None | — | Unchanged gate. It is evaluated on the fresh `wf`. |

## Out of scope

- Reducing other `cas_conflict` sources: the archive half, operator actions racing
  finalize.
- Running finalize more often than once per dispatch tick.
- Any change to C1, C2 or C4 areas of EPIC #375.

## Test plan

Add both tests to `taskpaw_v3/tests/test_workflow_finalize.py`, using the existing
`world` fixture.

1. **`test_archiving_starts_both_cleanup_scopes_in_one_tick` (AC 1, the regression).**
   - `world.scrape()`; `with world.fake, caplog.at_level(INFO, logger="taskpaw.workflow.finalize")`.
   - `fin = world.finalizer()`, then `fin.confirm(world.film, "actor")`.
   - Call `fin.run_once()` up to 5 times. After each call, record
     `(status("archive"), status("cleanup_workcopy"), status("cleanup_input"))`.
   - Assert `"cas_conflict" not in caplog.text`.
   - Assert that the first recorded tuple whose workcopy status is `succeeded` also has
     input `succeeded`.
   - **Verified RED on 3ddf070.** The tuples read
     `running/pending/pending → succeeded/pending/pending → succeeded/succeeded/pending
     → succeeded/succeeded/succeeded`, and one conflict line is logged.
   - Verified GREEN with the re-read, applied through a monkeypatched `_cleanups` in a
     scratch copy of the test.
2. **`test_a_genuine_conflict_on_input_admission_is_retried_next_tick` (AC 2).**
   - Wrap `world.rt.store.begin_cleanup`. On the first call with
     `command.scope == "input"`, forward
     `command.model_copy(update={"expected_version": command.expected_version - 1})` to
     the real method. That is the store's real CAS refusing a version older than the
     film's.
   - Run up to 5 ticks.
   - Assert `caplog.text.count("cas_conflict, retrying") == 1`.
   - Assert both cleanup stages end `succeeded`.
   - Verified passing against the prototype fix.
3. **Existing coverage to keep green:**
   - `test_one_click_archives_once_and_cleans_both_scopes`;
   - `test_manual_confirm_archives_and_cleans_both_scopes`;
   - `test_input_cleanup_waits_until_every_film_of_the_package_is_archived`, which
     covers the package gate on the re-read path;
   - `test_a_crash_right_after_the_recorded_cleanup_still_reclaims`;
   - `test_cas_conflict_is_retried_quietly`;
   - the reclaim tests;
   - everything in `taskpaw_v3/tests/test_workflow_cleanup_scope.py`.
4. Full `uv run pytest`.

**Manual smoke.** None is required. Optionally, after deployment, the operator can
check that the backend log gains no new `cas_conflict, retrying` line when a film is
archived. This plan does not deploy anything.

## Handoff notes

- **Use a short basetemp.** The scrape step's work tree is deep. A long `--basetemp`,
  such as a nested session scratch directory, overflows Windows MAX_PATH and fails in
  `scrape_step._copy_config` with `FileNotFoundError`. The default pytest basetemp
  under `%TEMP%`, or a short explicit one such as `--basetemp=%TEMP%/c3bt` with forward
  slashes, works. Clean it up after the round.
- **Write the RED test first and watch it fail on the unmodified tree.** It fails with
  the conflict line in `caplog`. Then apply the `_cleanups` change.
- No operator decision is needed.
