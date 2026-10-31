# #376 C1 — workflow.db growth: skip unchanged intake observations, prune adapter rows, reclaim space, scale the migration backup deadline

Date: 2026-10-31 · Base: `3ddf070` (main, 4.0.2) · Branch: `afk/perf-epic` · EPIC #375 (audit items 1, 2)
Planner: Claude (tech-lead plan only; no code).
Revision 2 (after debate round 1): C1-D1 retention 1 h; C1-D2 prune stops between
batches on `_stop`; C1-D3 loop placement and full `sqlite3.Error` mapping; C1-D4 A6
corrected (measured); C1-D5 equivalence text and differential test extended.

## Spec review

The controller's `IntakeService` (`taskpaw_v3/workflow/intake.py`) rescans the INBOX
root every `rescan_seconds` (30 s live). For every candidate package `scan_once`
calls `observe_intake` and `mark_intake_scan_seen`; each is a `_execute` command
(`store_core.py` 357-451): `BEGIN IMMEDIATE`, an `UPDATE intake_packages`, one
`audit` row and one `request_results` row, under `synchronous=FULL`. The rows are
never read back. The live `workflow.db` reached 1.02 GB; startup integrity checks
scale with that size, and the next schema migration's 5 s backup deadline cannot be met.

Core need: stop the per-scan growth at its source, drop the history already written,
give the file back to the disk without an operator step, and let a large DB migrate.
Intake semantics (admission, snapshots, freshness, stability, missing detection) must
not change.

### Evidence gathered (read-only live DB, `file:...workflow.db?mode=ro`, 2026-10-31)

| fact | value |
|---|---|
| file / WAL | 1,024,851,968 B / 9.4 MB; `page_size` 4096, `page_count` 249,988, `freelist_count` 0, `auto_vacuum` 0, `journal_mode` wal, `user_version` 4 |
| `audit` rows | 815,013 (still growing): `mark_intake_scan_seen` 375,994, `observe_intake` 375,963, `begin_intake_root_scan` 27,931, `finish_intake_root_scan` 27,897; every other kind together < 7,000 |
| `request_results` rows | 815,013, rowid 1..815,013; 1:1 with `audit` |
| space by object (`dbstat`) | `request_results` 557 MB, `audit` 257 MB, their three autoindexes 175 MB; next largest table 7.8 MB |
| ordering | `created_at` is non-decreasing in rowid order in both tables (0 inversions); `_observe_time` returns `max(now, high-water)` so this holds by construction |
| adapter rows per day | 1.3k (10-20) → 49k (10-24) → 151k (10-29) |
| `intake_packages` | 25 current `operational_ready/eligible`, 11 current `missing` (+134 retired) |
| SQLite | 3.50.4 (bundled with the uv CPython 3.14 used for packaging) |

### Answers to the questions the driver posed

**Q1 — what `mark_intake_scan_seen` / `last_seen_scan_id` feed.** Exactly one
consumer: `list_intake_unseen` (`intake_store.py` 733-766) selects current packages
whose `last_seen_scan_id` is NULL or differs from the current scan id. `scan_once`
calls it only after a *complete* walk and issues a `missing` source notice for every
returned package that is not already `missing` (`intake.py` 365-376). Nothing else
reads `last_seen_scan_id` (grep over `taskpaw_v3/`). It does **not** feed stability,
`stable_since` or freshness. If an unchanged package were simply not marked, the next
complete scan would report it `missing` (state → `missing`, generation +1,
dependents invalidated) — so "seen" must still be recorded, but it does not need to be
durable: a scan id is a fresh uuid per scan and only `scan_once` marks, so "marked
with this scan id" ≡ "visited in this `scan_once` call".

What an unchanged `observe_intake` feeds: it rewrites `observed_at`,
`last_complete_scan_at`, `observation_json` and bumps `version`; every other column
is recomputed to its old value when the store's own `changed` is false.
- `last_complete_scan_at` is read only by `begin_intake_snapshot` (`intake_store.py`
  903-915) — the freshness bound (`now - last_complete_scan_at <= rescan_seconds`)
  and the stability window (`last_complete_scan_at - stable_since >= stable_seconds`)
  — and only for packages in `waiting_stability`. Every other path that moves a
  package *into* `waiting_stability` either sets `stable_since=NULL` (cancel, grouping
  correction, store open) or happens after an owned snapshot during which the service
  already does not observe the package today (`finish_intake_snapshot`); in both cases
  the next scan observes a `waiting_stability` package, which is never skipped.
- `observed_at` is a lower bound for later observations/notices
  (`observation_time_invalid`); an older value only widens what is accepted, and the
  service's clock is monotonic per run.
- `observation_json` is the `previous` input to `observe_package` (member ids,
  film ids, grouping) and the digest source for `ready_packages`/`_dirty`; an
  unchanged observation has the same digest, members and grouping, so the stored copy
  is equivalent. Admission copies it only from `waiting_stability` rows.
- `version` is a CAS token. Fewer bumps only removes conflicts (it may also be the
  cause of #378's self-inflicted `cas_conflict`; not claimed here).

**Q2 — pruning.** Tables: `request_results` and `audit` only. Kinds:
`observe_intake`, `mark_intake_scan_seen`, `begin_intake_root_scan`,
`finish_intake_root_scan`. Retention: rows with `created_at` older than **1 hour**
(revision 2; 1 day would keep the last 24 h of the old build's rows — 125,121 adapter
rows, about 150 MB on the live DB — so launch 2 would only shrink to ~180 MB; the last
hour holds 6,066 rows, about 7 MB, and nothing reads these rows).
`request_results.audit_seq REFERENCES audit(seq) ON DELETE RESTRICT` with
`PRAGMA foreign_keys=ON` (`schema.py` 346, 534), so each batch deletes the
`request_results` rows first, then the `audit` rows they point to, in one
transaction. `audit.seq` is `AUTOINCREMENT`, so deleted audit ids are never reused.
**Measured trap:** `request_results` has no index on `audit_seq`, so with
`foreign_keys=ON` every `DELETE FROM audit` row makes SQLite scan the whole
`request_results` table for referencing children — 20 audit deletes took **15.4 s**
on the synthetic 945 MiB DB (0.77 s per row; the 812k backlog would take a week).
The same batch of 2,000 with `foreign_keys=OFF` took **0.38 s**. Adding the index is
a schema change (and a `SCHEMA_VERSION` bump), so the prune switches FK enforcement
off for its own batch transaction only (see Approach (B)); the child-first delete
order keeps the data FK-consistent, and every store open runs
`PRAGMA foreign_key_check` anyway.
Readers checked: `read_store.py:78` reads only `command_kind='create_film'`;
`WorkerCommands.list_audit` (`worker_store.py` 798) has no production caller (tests
only) and pages by `seq > after`, so gaps are harmless; the idempotency lookup
(`store_core.py` 395) can never hit these rows because `IntakeService` always calls
`self.meta()` with no key and `LocalRuntime.meta` (`local_runtime.py` 288-294)
then draws a fresh `_new_id()` idempotency key — **claim verified**. The agent's
workflow-event observer reads the board, not `audit`; the Hub outbox is a different
database; no diagnostics export copies `workflow.db` (grep for the filename and
`db_path`). Runs in the existing `IntakeService._loop` thread, at most hourly.

**Q3 — reclaiming the 1 GB.** One automatic `VACUUM` at store open when the freelist
is at least half the file and at least 64 MiB, followed by
`PRAGMA wal_checkpoint(TRUNCATE)`. Reasons and measurements under Approach (D).

**Q4 — backup deadline.** `connect_existing` passes a deadline derived from the size
of the database plus its WAL (floor 5 s). The double `integrity_check` per open is an
observation only (see Out of scope / Observations).

**Q5 — tests.** Store tests: `taskpaw_v3/tests/test_workflow_intake_store.py`
(fixture `intake`, helper `observation`), `test_workflow_intake_publication.py`
(`snapshot_fixture` yields an `operational_ready` package after
`finish_intake_snapshot`), `test_workflow_intake_scan_index.py`,
`test_workflow_intake_root_scan.py` (`begin`/`finish` helpers). Service tests (native,
Windows-only): `test_workflow_intake_service.py` (fixture `service`). Migration and
backup: `test_workflow_migrations.py`, `test_workflow_intake_migration.py`. Store open:
`test_workflow_store.py` (`Harness`). New tests are listed in the Test plan.

## Acceptance criteria

- **AC1** A rescan that finds a package unchanged — defined exactly as "the store's
  `observe_intake` would write nothing but `observed_at`, `last_complete_scan_at`,
  `observation_json` and the version bump, and the package is not in
  `waiting_stability`" — issues no `observe_intake` and no `mark_intake_scan_seen`
  for it: no `audit` row, no `request_results` row, no command transaction. A package
  that is not unchanged takes exactly today's path (`observe_intake` + `mark_intake_scan_seen`).
  Interpretation of the issue's "opens no write transaction": the `_observe_time`
  high-water commit that *every* store read already performs (including the reads the
  scan does today for every package, and the new predicate) is not a command
  transaction and is out of scope — see Observations. Literal "no write transaction at
  all" is impossible without changing `_observe_time`.
- **AC1a** A skipped package is still "seen" for that scan: a complete scan never
  reports it `missing`; a package that disappeared is still reported `missing`.
- **AC2** `IntakeService` prunes, at most once per hour (first run right after
  start), `request_results` and `audit` rows of exactly the four intake-adapter
  kinds whose `created_at` is older than 1 hour, in batches with the store lock released
  between batches. Rows of every other kind, and adapter rows younger than 1 hour, are
  untouched. A prune failure (including any `sqlite3.Error`) is logged and never stops
  intake. A stop request (`_stop`) ends the prune after the batch in flight.
- **AC3** On store open, if `freelist_count` ≥ half of `page_count` and the free
  pages total ≥ 64 MiB, the store runs one `VACUUM` and a `wal_checkpoint(TRUNCATE)`
  after schema validation; the file shrinks without operator action (on the first
  open after the backlog has been pruned). A failed VACUUM is logged and the store
  still opens on the unchanged file.
- **AC4** The pre-migration backup deadline in `connect_existing` is
  `max(5.0, (db bytes + wal bytes) / 10 MB/s)`; a migration of a ~1 GB DB is no longer
  bound to 5 s.
- **AC5** Tests cover AC1-AC4 (see Test plan). Existing intake tests stay green
  unchanged except where they assert on removed behaviour (none expected).
  Full suite, ruff, ruff format, mypy green. CHANGELOG entry (Chinese, operator-facing).

## Frozen issue contract

- **Acceptance criteria:** AC1-AC5 above.
- **Product/engineering invariants:**
  - Admission (`begin_intake_snapshot` gates), freshness (`rescan_seconds` bound on
    `last_complete_scan_at`), stability (`stable_since`, `stable_seconds`, the
    service's in-memory `_stable` map), snapshots, grouping, film identity reuse and
    missing detection behave as today.
  - `observe_intake`'s outcome for any call that is still made is byte-for-byte the
    same (helper extraction must be behaviour-identical).
  - No schema change, no `SCHEMA_VERSION` bump (a bump would itself trigger the
    backup this issue is fixing), no new table/index/column, no new thread, no new
    config/setting.
  - `_execute`, the authority high-water (`_observe_time`) and the idempotency
    contract are unchanged.
  - `PRAGMA foreign_keys` is ON whenever the store lock is released (the prune's
    off-window lives entirely inside one lock hold).
  - Never deploys; never touches the operator's live DB from tests or tooling.
- **Allowed user-visible changes:**
  - The first launch after upgrade prunes the backlog in the intake thread (~2-3 min
    measured on a synthetic copy; the next scan of that launch starts that much later,
    once). That launch still pays today's two ~12 s integrity checks at open.
  - The first open after the backlog is pruned (normally the second launch of the new
    build) runs one VACUUM (< 1 s measured for the live-size content); afterwards the
    file is tens of MB — estimated ~50 MB from the live DB: 34 MB of all other objects
    (`dbstat`), ~8 MB for the 6,509 non-adapter `audit`/`request_results` pairs, at most
    ~7 MB for one hour of adapter rows — and the two integrity checks cost well under a
    second together.
  - Intake-adapter command history older than 1 hour is gone (nothing reads it).
  - Unchanged packages' `observed_at`/`last_complete_scan_at`/`version` no longer
    advance every 30 s (not shown in any UI).
  - A concurrent write by the snapshot worker to a package mid-scan no longer turns
    that scan partial via a `mark_intake_scan_seen` CAS conflict *for a skipped
    package* (it never had a mark to conflict with). For packages that are still
    marked, behaviour is unchanged.
- **Explicit non-goals:** changing `_observe_time` (every store read is a small
  write transaction — see Observations); dropping either `validate_schema` pass;
  pruning any other command kind or table; `auto_vacuum`; batching `begin`/`finish`
  root-scan writes; touching the Hub's own SQLite store or its backup
  (`hub/server/store.py`); UI.
- **Smallest causal boundary:** `workflow/intake.py` (`scan_once`, `_loop`, one
  prune helper), `workflow/intake_store.py` (`observe_intake` helper extraction, one
  read predicate, `list_intake_unseen` `seen` argument, one prune method),
  `workflow/schema.py` (backup deadline helper, reclaim helper), `workflow/store_core.py`
  (call the reclaim helper on open), tests, CHANGELOG.

## Assumptions

- **A1** A complete scan never sees the same package id twice in one walk
  (candidates come from distinct directories; `find_intake` resolves one current row).
  The in-memory `unchanged` set relies on "visited in this call" ≡ "marked with this
  scan id". Read, not executed against a pathological tree.
- **A2** `meta.authority_id`/`authority_epoch` always match the store's authority for
  `IntakeService` (meta comes from the same `LocalIdentity` the store was created
  with). The read predicate therefore does not re-check `stale_attempt`; if they ever
  differed, the skipped package simply would not raise where `observe_intake` would.
  Not executed.
- **A3** Steady-state adapter volume after AC1: ~2 rows per scan for begin/finish
  (≈ 240/hour at 30 s) plus rows for genuinely changing packages; at ~1.2 KB per
  `audit`+`request_results` pair (live average incl. index entries) that is well under
  1 MB of retained history and a freelist that the next hour's inserts reuse, so the
  reclaim threshold (≥ 50 % free and ≥ 64 MiB) is not expected to fire in steady
  state. Estimated from live row sizes, not measured over a day.
- **A4** Throughput figures (prune, VACUUM, backup, integrity_check) come from a
  synthetic DB on this machine's `%TEMP%` volume, not from `D:` where the live DB
  lives; they set the order of magnitude, not exact timings. The backup deadline uses
  a deliberately conservative 10 MB/s floor for that reason.
- **A5** `VACUUM` in WAL mode on SQLite 3.50 is atomic (crash leaves the old file
  intact), needs free space of roughly the *live* content size in the temp directory
  plus the same again in the WAL, and fails rather than corrupts on disk-full. This is
  documented SQLite behaviour; the synthetic run confirmed the size effect, not the
  crash behaviour.
- **A6** (corrected in revision 2, C1-D4; measured) Another process may hold a short
  read transaction on `workflow.db` during open — e.g. `workflow_settings._read_store`
  opens a `mode=ro` connection and runs `BEGIN` + selects. That does **not** make
  `VACUUM` fail in WAL mode. It makes `PRAGMA wal_checkpoint(TRUNCATE)` return
  "busy" without raising. In a hermetic test (29 MiB file, 7,142 free pages, a reader
  with an open `BEGIN` on another connection) `VACUUM` succeeded, the checkpoint
  returned `(1, 377, 0)` and the main file stayed 29 MiB with a 1.5 MiB WAL; after the
  reader committed, the next `wal_checkpoint(TRUNCATE)` returned `(0, 0, 0)` and the
  file dropped to 1.5 MiB. So with a concurrent reader the shrink is only deferred to
  the next complete checkpoint (an automatic checkpoint once the WAL passes 1,000
  pages, or the last connection's close). No code change; the checkpoint result need
  not be checked.
- **A8** `PRAGMA foreign_keys=OFF/ON` on the store's connection takes effect between
  transactions (the connection is opened with `isolation_level=None`, so the store
  controls `BEGIN`/`COMMIT` explicitly) and is silently ignored inside one. The
  synthetic run exercised exactly this toggle around each batch on a plain
  connection; the store's own connection was not exercised.
- **A7** The packaged app uses the same bundled SQLite generation as the uv venv
  (3.50.x); nothing here depends on features newer than 3.7 (`VACUUM`,
  `wal_checkpoint(TRUNCATE)`, `freelist_count`).

## Approach

### (A) Skip unchanged observations — store-side predicate, exact by construction

Rejected alternatives:
- *Mirror the store's "unchanged" rules in the service.* The rules depend on `dirty`,
  `stable_since`, the restored-snapshot branch, the archived check and
  `publication_blocked` — none exposed by `IntakeView`; a copy would drift.
- *Let `observe_intake`'s handler detect the no-op and skip the audit insert.* Still
  opens `BEGIN IMMEDIATE`, and `_execute` must return a `CommandResult` with a real
  `audit_id`; it would need a fake id or an Optional return threaded through
  `_execute`'s callers.
- *Keep writing the row but drop audit/request_results for these kinds.* Still one
  `BEGIN IMMEDIATE` + `UPDATE` + fsync per package per scan; fails AC1.

Chosen:
1. In `IntakeCommands.observe_intake`, extract two private helpers out of `apply`
   without changing behaviour:
   - `_observation_target(principal, command, now) -> (root, old)` — the
     preconditions block now at the top of `apply` (root lookup, `_intake_access(...,
     write=True)`, config revision, enabled, old row fetch, CAS, root id, `current`,
     observation time, digest recomputation). Raises exactly as today.
   - `_observation_values(command, root, old) -> (values, changed)` — everything from
     `if command.grouping: self._check_intake_grouping(...)` through building `values`,
     **minus** the `_invalidate_intake_dependents` call. `apply` calls
     `_invalidate_intake_dependents(command.package_id)` itself when `changed`, right
     after the helper and before `_update`/`_insert`. Moving that call later is safe:
     the code between its old and new position reads `intake_snapshots`, `receipts`
     (`_intake_archived`) and the row `old` fetched earlier, none of which the
     invalidation writes (it writes `intake_operations.publication_allowed`,
     `intake_films.source_invalidated_context_revision` and `films`).
   - The identity-retire branch and the `old is None` alias branch stay inline in
     `apply`, between the two helpers.
2. Add a read method `intake_observation_unchanged(auth, command) -> bool` next to it:
   revalidate the model like `_execute` does
   (`im.IntakeObservation.model_validate(command)`); under `self._lock`:
   `principal = self._check_auth(auth, self._observe_time())`; return False if the
   authority's `activation_state != 'active'` (observe_intake is not a recovery
   command, so it would raise); `try` `_observation_target` (any `WorkflowError` →
   False, so the real command raises as today); False if `old is None` or the
   identity-retire condition holds; `try` `_observation_values` (`WorkflowError` →
   False); return `not changed and values["state"] != "waiting_stability" and all
   values[k] == old[k] for k not in {"observed_at", "last_complete_scan_at",
   "observation_json"}`. No `_execute`, no audit, no command transaction (only the
   `_observe_time` high-water commit every store read already does).
3. In `scan_once`, keep a local `unchanged: set[str]`. After
   `observation = self._observe(candidate.relative_path, previous)`:
   ```
   if previous and self.store.intake_observation_unchanged(self.auth, observation):
       unchanged.add(previous.package_id)
       self._remember(observation, previous)
       continue
   ```
   (`_remember` pops the package from `_stable` because its state is not
   `waiting_stability`, exactly as with today's freshly read view.) Owned-snapshot
   packages keep today's `_mark_seen` path; everything else keeps
   `observe_intake` + `get_intake` + `_mark_seen` + `_remember` unchanged.
4. `list_intake_unseen` gains `seen: frozenset[str] = frozenset()`; the SQL and
   pagination stay as they are (rows fetched `limit + 1`, `next_package_id` from the
   raw rows), and the returned `items` exclude package ids in `seen`. The binding
   checks (current scan id, `required`, root config revision) are unchanged.
   `scan_once` passes `seen=frozenset(unchanged)`. Default empty keeps every existing
   caller and test valid.

Why the result is identical: a skipped package is exactly one for which the store
would have rewritten only the three ignored fields (proved by reusing the store's own
computation), and it is in a state whose admission never reads them; its "seen"
fact moves from `last_seen_scan_id` to a per-call set that has the same meaning.

Which states can be skipped (equality with `old` forces `values["state"] ==
old["state"]`, and `waiting_stability` is excluded), traced through
`observe_intake.apply` with `changed` false (computed state `waiting_stability`):
- `operational_ready` / `snapshot_ready` / `snapshotting` (the keep-list keeps state
  and reason), including the **restored-snapshot** branch (active snapshot, no active
  operation: state and reason re-derived from the stored snapshot's eligibility,
  `stable_since` NULL, `dirty` 0) — skipped once that result equals the row.
- `unknown_owner` (the keep-list keeps it; the restored-snapshot branch explicitly
  excludes it; `_owns_snapshot` is False for it, so the scan does observe it today) —
  skipped when the row already holds the recomputed `stable_since`. Its recovery is
  driven by `settle_abandoned_intake`, not by observations.
- `blocked` / `publication_blocked` (`old.reason == 'publication_blocked'` forces
  `blocked`, `stable_since` NULL).
- `needs_attention` / `recovery_required` for an **archived** package with
  `dirty = 1` (the archived branch runs only when `changed or old.dirty`; `dirty` is
  carried over unchanged).
Every other state yields a different `state`, `dirty` or `stable_since` than the row
holds, or a computed `waiting_stability`, and is therefore never skipped.

Why freshness and stability cannot be affected (the `_stable` argument):
`begin_intake_snapshot`'s only production caller is `SnapshotRunner.run`
(`intake_runner.py` 194), reached only from `IntakeService.run_ready_once` with a
package id from `ready_packages()`. `ready_packages()` iterates the in-memory
`_stable` map and keeps a package only if its current view is `waiting_stability`
with an unchanged signature for `stable_seconds`; `_stable` is filled only by
`_remember` for a view in `waiting_stability` with complete coverage and a clear
writer. A skipped package is never in `waiting_stability` (and `_remember` pops it),
and a `waiting_stability` package is never skipped, so every package that can reach
admission still gets its `last_complete_scan_at` refreshed on every scan exactly as
today; the freshness bound at `intake_store.py:913` and the stability window are
evaluated on the same values.

Live effect: the 25 `operational_ready` packages stop writing after at most one more
write each (an `operational_ready` row without a restored snapshot and with
`stable_since` NULL gets `stable_since` set once — after that it is a fixpoint).
Packages the store re-derives as changed on every scan (`needs_attention`,
`blocked` other than `publication_blocked`, `receiving`) keep writing, by design of
the store (generation churn and dependent invalidation are part of today's
semantics); their rows are bounded by the prune.

### (B) Prune — one store method, called from the intake loop

- `IntakeCommands.prune_intake_history(auth, *, before: datetime) -> int` in
  `intake_store.py` deletes **one batch** and returns how many pairs it deleted. Module
  constants `_ADAPTER_KINDS = ("observe_intake", "mark_intake_scan_seen",
  "begin_intake_root_scan", "finish_intake_root_scan")` and `_PRUNE_BATCH = 1000`
  (from the measurement below). The batch loop lives in the service (revision 2,
  C1-D2): that is smaller than threading a `cancelled` callable into the store, and it
  puts the `_stop` check where `_stop` lives.
- One batch, all under one `with self._lock:` hold, the whole body inside
  `try: ... except sqlite3.Error as exc: raise WorkflowError("authority_unavailable")
  from exc` (revision 2, C1-D3 — this covers `_observe_time`, both PRAGMAs, the
  transaction and the rollback; an unmapped `sqlite3.Error` is not an
  `OSError`/`ValueError`/`RuntimeError` and would escape the service's handlers and
  kill the thread):
  1. `principal = self._check_auth(auth, self._observe_time())`; role must be
     `intake_adapter` (else `WorkflowError("forbidden")`);
  2. `PRAGMA foreign_keys=OFF` (outside any transaction — the pragma is a no-op inside
     one);
  3. inner `try`: `BEGIN IMMEDIATE`; `SELECT rowid, audit_seq FROM request_results
     WHERE command_kind IN (...) AND created_at < ? LIMIT ?`; `DELETE FROM
     request_results WHERE rowid=?` (executemany); `DELETE FROM audit WHERE seq=?`
     (executemany); `COMMIT`; on any exception `rollback()` and re-raise; `finally`
     (after commit/rollback, still under the lock) `PRAGMA foreign_keys=ON`;
  4. return the number of rows selected.
- Why FK enforcement is switched off for the batch: see the measured trap under Q2
  (RESTRICT parent check = full scan of `request_results` per deleted audit row).
  Safety: the lock is held for the whole off-window, so no other statement runs with
  FKs off; the batch deletes each `request_results` row before the `audit` row it
  references and deletes nothing else, so it cannot create an orphan; the next open's
  `validate_schema` runs `PRAGMA foreign_key_check` and would refuse an inconsistent
  file. Do **not** add an index instead (schema change → version bump → the very
  backup this issue fixes).
- Deliberately not filtered by principal: each boot gets a fresh `intake_adapter`
  principal (`LocalRuntime.grant_intake_adapter`), so earlier boots' rows would never
  go otherwise.
- Why `request_results` drives the batch: it has no index on `audit_seq`, so deleting
  it by `audit_seq IN (...)` would scan the 557 MB table per batch; selecting its
  rowids and the `audit_seq` they carry gives both delete keys, and `audit.seq` is the
  rowid. Rows are in rowid = time order, so old matches are found at the start of
  the scan; once the backlog is gone the table is small.
- `before` is a timestamp string compared lexicographically with `created_at`;
  both are produced by `timestamp()` (UTC ISO-8601 with `+00:00`), so string order is
  time order. Pass `timestamp(before)`.
- Call site (revision 2, C1-D3): `IntakeService._prune()`, called inside the `_loop`
  body (inside the existing outer `try`), right after the scan block and before the
  snapshot-worker check. `_prune()` itself catches
  `(OSError, ValueError, RuntimeError)` around its whole body and logs a warning, so
  nothing reaches the outer handler (which would stop the service):
  ```
  def _prune(self) -> None:
      if self.monotonic() - self._last_prune < PRUNE_INTERVAL_SECONDS:
          return
      self._last_prune = self.monotonic()
      before = self.clock() - PRUNE_RETENTION      # timedelta(hours=1)
      try:
          while not self._stop.is_set() and self.store.prune_intake_history(
              self.auth, before=before
          ):
              pass
      except (OSError, ValueError, RuntimeError):
          log.warning("intake history prune failed", exc_info=True)
  ```
  `_last_prune` starts at `-math.inf`, so the backlog is pruned right after the first
  loop scan. The cutoff is fixed once per run, so the loop terminates: each batch
  removes rows older than a fixed time and the count reaches 0 (one extra empty batch,
  measured 0.01 s, instead of coupling the service to `_PRUNE_BATCH`).
- Stop behaviour (C1-D2): `stop()` sets `_stop` and joins the non-daemon
  `taskpaw-intake-discovery` thread for at most 30 s; `local_host` logs "stop
  unconfirmed; store left open" if it is still alive. Without the `_stop` check the
  2-3 min backlog prune would hit that on the first graceful stop after upgrade (C2
  makes graceful stop run on Windows). With it, the thread leaves the prune after the
  batch in flight (about 0.2 s) and the loop's `while not self._stop.is_set()` exits.
  A partially pruned backlog simply continues on the next launch.

### (C) Scale the pre-migration backup deadline

- `schema._backup_deadline(path) -> float`: `max(5.0, (size(db) + size(db-wal if
  present)) / 10_000_000)`. `connect_existing` passes it instead of the literal 5.0.
- 10 MB/s is a conservative floor (measured throughput is far higher, see below); the
  deadline only exists to stop a backup that keeps restarting, so generosity is free.
- `schema.backup_database` (tests-only public helper) keeps its explicit parameter;
  optional: default it to the same helper. Not required.

### (D) Reclaim — one-time VACUUM at open when the freelist dominates

Options weighed:
- **`auto_vacuum=INCREMENTAL`** needs a full VACUUM to switch anyway, changes the
  file format permanently and needs a periodic `incremental_vacuum` call — more moving
  parts for a one-off backlog.
- **Documented operator step** — the operator asked for none if it can be safe.
- **VACUUM inside the running app after the backlog prune** — holds the single
  connection's RLock for the whole VACUUM while the UI, dispatcher and runner wait,
  and VACUUM fails if any statement on the connection is still active.
- **Chosen: at open.** In `WorkflowCore._connect`, after
  `schema.configure_connection(conn)` and only when `create` is false, call
  `schema.reclaim_free_pages(conn)`:
  - read `PRAGMA page_count`, `PRAGMA freelist_count`, `PRAGMA page_size`;
  - return unless `freelist * 2 >= page_count and freelist * page_size >= 64 MiB`
    (module constants so tests can lower them);
  - `conn.execute("VACUUM")`, then `conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")`
    (its result is not checked: with a concurrent reader it returns busy and the file
    shrinks at a later checkpoint — A6);
  - on `sqlite3.Error` log a warning and return (the old file is intact — VACUUM is
    one atomic transaction).
- Safety for the real DB: it runs after both `validate_schema` passes
  (integrity and FK checks succeeded on this exact file), under the open's
  `FileLease`, before any thread uses the store, on a single connection with no open
  statements. It rewrites only live content.
- Sequencing on the operator's machine: launch 1 of the new build prunes the backlog in
  the intake thread down to the last hour of adapter rows (file stays 1 GB, now ~95 %
  free pages); launch 2 validates (now ~0.25 s per check — free pages are cheap for
  `integrity_check`, see table) and VACUUMs down to the live size, **tens of MB
  (~50 MB estimated, see Allowed user-visible changes)**. A later steady state never
  meets the threshold (A3).
- Why not prune at open too, so launch 1 already shrinks: it would put ~2-3 min of
  deletes in front of the media workflow on the upgrade launch and add a second prune
  call site; the open-time win on launch 2 comes mostly from the prune anyway.

### Measurements (synthetic, hermetic)

Synthetic DB built in a temp directory with the exact `schema.TABLES`/`INDEXES` DDL,
815,000 `audit` + `request_results` pairs with live-like kinds and row sizes, one row
per second of fake time; WAL, `synchronous=FULL`, `foreign_keys=ON` for the prune;
deleted afterwards.

| step | result |
|---|---|
| build | 945 MiB file |
| `outbox_migration.backup_database` (pages=128, sleep=0.01) incl. `quick_check` | 4.5-4.6 s (≈ 210 MB/s on this volume — already at the 5 s edge; the issue's ≥ 20 s for the live `D:` volume is plausible, not measured) |
| `PRAGMA integrity_check`, full 945 MiB | 18.0 s (live DB: 12.3 s per the audit) |
| prune batch with `foreign_keys=ON`, 20 rows | **15.4 s** (parent-side RESTRICT scan per audit row) |
| prune batch with `foreign_keys=OFF`, 2,000 rows | 0.38 s |
| full backlog prune, 789,793 rows, 395 batches of 2,000 | 131.7 s total; median 0.345 s, max 0.41 s per batch |
| empty prune batch (nothing older than the cutoff) | 0.01 s |
| after prune | `freelist_count` 234,064 of 242,047 pages (96.7 %); WAL 25 MiB |
| `integrity_check` on the pruned but not vacuumed 945 MiB file | **0.24 s** |
| `VACUUM` | 0.35 s → file 26 MiB; WAL 26 MiB until checkpoint |
| `wal_checkpoint(TRUNCATE)` | 0.0 s → WAL 0 B |
| `integrity_check` after VACUUM | 0.12 s |

(The synthetic DB has no `principals` rows, so `foreign_key_check` reports the same
pre-existing `request_results → principals` orphan before and after the prune; the
prune introduced none.)

Consequences for the design:
- Batch size **1,000** (≈ 0.18 s lock hold, extrapolated from 0.345 s per 2,000) keeps
  UI reads responsive during the one-time backlog; the backlog then takes ~2-3 min of
  intake-thread time on launch 1.
- Open time already drops on launch 2 even before VACUUM (free pages are nearly free
  for `integrity_check`); VACUUM itself costs well under a second for the live-size
  content, so doing it at open is cheap.

## Files to change

| path | change | reason |
|---|---|---|
| `taskpaw_v3/workflow/intake_store.py` | extract `_observation_target` / `_observation_values` from `observe_intake.apply` (behaviour-identical); add `intake_observation_unchanged`; add `seen` to `list_intake_unseen`; add `prune_intake_history` + constants | AC1, AC1a, AC2 |
| `taskpaw_v3/workflow/intake.py` | `scan_once` skip path + `unchanged` set passed to `list_intake_unseen`; `_last_prune`, `_prune()` helper (batch loop that checks `_stop`) called from `_loop`; module logger; `PRUNE_INTERVAL_SECONDS` (3600), `PRUNE_RETENTION = timedelta(hours=1)` | AC1, AC1a, AC2 |
| `taskpaw_v3/workflow/schema.py` | `_backup_deadline(path)`; use it in `connect_existing`; `reclaim_free_pages(conn)` + threshold constants; module logger | AC3, AC4 |
| `taskpaw_v3/workflow/store_core.py` | call `schema.reclaim_free_pages(conn)` in `_connect` when `create` is false, after `configure_connection` | AC3 |
| `taskpaw_v3/tests/test_workflow_intake_store.py` (or a new `test_workflow_intake_history.py`) | predicate and prune tests | AC5 |
| `taskpaw_v3/tests/test_workflow_intake_scan_index.py` | `seen` exclusion test | AC5 |
| `taskpaw_v3/tests/test_workflow_intake_service.py` | scan writes nothing for an unchanged package; skipped package not reported missing; vanished one still is; prune cadence | AC5 |
| `taskpaw_v3/tests/test_workflow_migrations.py` / `test_workflow_intake_migration.py` | deadline helper and that `connect_existing` passes it | AC5 |
| `taskpaw_v3/tests/test_workflow_store.py` (or `test_workflow_migrations.py`) | reclaim at open: above threshold shrinks, below does nothing, failure tolerated | AC5 |
| `CHANGELOG.md` | operator-facing Chinese entry under the batch's next-version heading (driver decides the heading; no version bump in this child) | AC5 |

`mark_intake_scan_seen`, `IntakeScanSeen`, its role entry and its
`_RECOVERY_COMMANDS` entry stay: they are still used for changed and owned packages.

## Execution surface

- **Writes:** only the files in the table above, in the worktree.
- **Reads/executes:** `uv run --frozen pytest <targeted files>` then the full suite
  (driver's step), `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy`. Pytest `--basetemp` under `%LOCALAPPDATA%/taskpaw-pytest/afk-376`
  (forward slashes), cleaned at the end of the round.
- **Generated outputs:** none (no generated files, no migrations, no build artefacts).
- **Never:** open the controller's live `workflow.db` or anything in the app data directory
  read-write; any experiment uses `%TEMP%` and is deleted.

## Key implementation notes

- Keep the extraction mechanical: cut/paste the blocks, pass `root`, `old`,
  `command`; `values` must be built exactly as today. Return `changed` from the values
  helper because `apply` needs it for the invalidation call and the predicate needs it
  for the decision. `Mutation(command.package_id, version, values["state"])` replaces
  the old `Mutation(..., state)`.
- The predicate's equality compares SQLite row values with Python values: integers
  (`dirty`, `source_generation`, `root_config_revision`, `grouping_revision`),
  strings (timestamps from `timestamp()`, `canonical_json` output). Both sides come
  from the same producers, so `==` is exact; do not normalise.
- `stable_since` is part of the comparison on purpose: it makes the first rescan after
  a snapshot write once (if it sets `stable_since`), then skip forever.
- The predicate holds `self._lock` for one call; between it and the next candidate
  another thread may write the package. Skipping is then equivalent to the scan having
  observed slightly earlier, which is already possible today.
- `list_intake_unseen`: filter `items` only; compute `next_package_id` from the raw
  `rows` as now, otherwise pagination can stop early when a page is all skipped ids.
- Prune: `executemany` with single-row `DELETE ... WHERE rowid=?` /
  `WHERE seq=?` keeps the SQL static (no dynamic `IN (...)` with thousands of
  placeholders). Do not use `RETURNING`.
- Prune runs on the intake thread, so it never overlaps a scan of the same service;
  other threads interleave between batches because the RLock is released.
- Reclaim: `VACUUM` cannot run inside a transaction; `_connect` calls it outside any
  (`configure_connection` already asserts `not conn.in_transaction`). Run the
  checkpoint after VACUUM; without `TRUNCATE` the WAL file keeps the rewritten size
  until a later checkpoint.
- `IntakeCommands.open` (`intake_store.py` 46-71) runs its recovery `UPDATE`s after
  `super().open` — i.e. after the VACUUM. That is fine; do not move the reclaim into
  `IntakeCommands`.
- Logging: `schema.py` and `intake.py` currently have no logger; add
  a module logger like `local_host.py` (`logging.getLogger("taskpaw.workflow.<module>")`).
  Log messages English, path-free (match the constitution's no-secret logging).

## Risk assessment

| risk | likelihood | impact | mitigation |
|---|---|---|---|
| Extraction changes `observe_intake` behaviour | low | high (intake regressions) | mechanical move; the existing intake store/service/publication/notice suites cover the branches; add the differential test (predicate True ⇒ a real `observe_intake` changes only the ignored fields) |
| A skipped package reported missing | low | high (spurious missing → generation bump, context invalidated) | `seen` exclusion + dedicated service test |
| First-launch backlog prune makes the first intake scan late | certain, once | low | measured ~2-3 min of intake-thread time on the synthetic 945 MiB DB; lock released between batches (≈ 0.2 s holds) |
| VACUUM fails (disk full, temp dir unavailable) | low | none (file unchanged, store opens, retried next open) | log + continue |
| Concurrent reader during the reclaim (e.g. `workflow_settings._read_store`) | low | none: VACUUM succeeds; `wal_checkpoint(TRUNCATE)` returns busy, so the file shrinks at the next complete checkpoint instead (measured, A6) | none needed |
| VACUUM duration at launch 2 | certain, once | low (seconds; measured) | threshold prevents repeats |
| Deadline still too short on a very slow disk | very low | migration refused (`backup_failed`, store not opened) — today's failure mode | 10 MB/s floor is ~20x below the synthetic measurement |
| Prune deletes something a future feature wants (intake history) | low | low | 1-hour window kept; kinds are explicit; other kinds untouched |
| Backlog prune delays a graceful stop | certain before revision 2 | `stop()` joins at most 30 s, then "stop unconfirmed; store left open" | the prune checks `_stop` between batches (about 0.2 s each) |

## Out of scope / Observations (for the operator, not this issue)

- **Every store read is a write transaction.** `_observe_time` (`store_core.py`
  212-233) does `BEGIN IMMEDIATE` + `UPDATE authority SET max_observed_at` +
  commit (fsync under `synchronous=FULL`) on every read, including `find_intake`,
  `get_intake_observation`, `get_intake_snapshot`, `removed_intake_films`. An
  unchanged package therefore still costs ~4 such tiny commits per scan after this fix
  (no DB growth — it rewrites one page). Changing that touches the authority clock
  invariant; it is a separate decision.
- **Double `integrity_check` per open.** `connect_existing` validates on a read-only
  connection and again on the read-write one. After pruning + VACUUM the DB is ~30-50 MB (synthetic: 26 MiB)
  and one check takes ~0.1-0.2 s (synthetic: 0.12 s), so the pair costs ~2x that per launch instead of
  ~25 s. Dropping the second pass would halve it; not in the acceptance criteria.
- A schema migration still runs `validate_schema` four to five times (ro, backup
  verification, rw, twice inside `_migrate`); with a small DB that is acceptable.
- Generation churn for `needs_attention` / `blocked` packages on unchanged content is
  existing store semantics and stays.
- The Hub's `hub/server/store.py` uses the same backup primitive with its own 5 s
  default on a different (small) database; untouched.

## Test plan

RED first (each new test fails on `3ddf070`), then GREEN.

1. **Predicate, store level** (`snapshot_fixture(intake, eligible=True)` →
   `finish_intake_snapshot` → `operational_ready`):
   - after one `observe_intake` rescan (to reach the `stable_since` fixpoint), a
     second identical observation (later `observed_at`, current
     `expected_package_version`) → `intake_observation_unchanged` is True, and the
     `audit`/`request_results` counts are the same before and after the call;
   - differential, parametrised over every skippable state (revision 2, C1-D5):
     restored-snapshot `operational_ready` (the `snapshot_fixture` path),
     `blocked`/`publication_blocked`, `unknown_owner`, and archived
     `needs_attention`/`recovery_required` with `dirty=1`. Reach each from the real
     command sequence where the existing fixtures allow (`snapshot_fixture`;
     `IntakeCommands.open` marks unresolved operations' packages `unknown_owner` on
     reopen; an archive receipt via the publication/archive fixtures), otherwise set the
     row's `state`/`reason`/`dirty`/`stable_since` via `h.store._conn` as
     `test_workflow_intake_store.py` 478-485 does. For each: after at most one
     fixpoint `observe_intake`, `intake_observation_unchanged` is True, and a real
     `observe_intake` with the same observation then changes only `observed_at`,
     `last_complete_scan_at`, `observation_json` and `version` (compare `SELECT *`
     before/after). Also assert the converse for `waiting_stability`: the predicate is
     False and the real call advances `last_complete_scan_at`;
   - False for: a `waiting_stability` package (plain `observation` + first observe);
     a changed member size; a stale `expected_package_version` (and
     `observe_intake` still raises `cas_conflict`); a package made dirty by
     `record_intake_source_notice`; a disabled root; an observation from a principal of
     another component (`forbidden` → False).
2. **`list_intake_unseen(seen=...)`** (`test_workflow_intake_scan_index.py`): with one
   unmarked package, `seen={id}` returns no items; default still returns it; binding
   error unchanged.
3. **Service, native** (`test_workflow_intake_service.py`, `service` fixture): first
   scan; force the row to `operational_ready/eligible`, `dirty=0` via
   `h.store._conn` (pattern used at `test_workflow_intake_store.py` 478-485); scan once
   (allows the one fixpoint write); count `audit` rows by kind; scan again → no new
   `observe_intake` / `mark_intake_scan_seen` rows, exactly one new
   `begin_intake_root_scan` and `finish_intake_root_scan`; package still
   `operational_ready`, not `missing`; then move the package directory out of the root
   and scan → `missing` (skipped packages do not mask disappearance). Existing
   `test_actual_scan_discovers_package_...` and
   `test_locally_owned_snapshot_..._but_is_seen` must stay green.
4. **Prune, store level**: with the `intake` fixture, issue observe / begin / finish /
   mark commands at T, one more observe at T+30 min, advance `h.clock` to T+2 h and
   issue one more observe; monkeypatch `_PRUNE_BATCH` to 2; call
   `prune_intake_history(adapter, before=now - 1 hour)` until it returns 0 → the first
   calls return 2; the rows from T and T+30 min (older than 1 hour) are gone from both
   tables; the T+2 h observe's rows remain; `configure_intake_root` /
   `grant_component` rows remain;
   `PRAGMA foreign_key_check` empty; `PRAGMA foreign_keys` reads 1 afterwards, also
   after a batch forced to fail (make a delete raise `sqlite3.OperationalError`, and
   separately make `_observe_time` raise it) — the error surfaces as
   `WorkflowError("authority_unavailable")`, never a raw `sqlite3.Error`, and nothing
   is deleted from that batch;
   an `observer`/operator principal → `forbidden`.
5. **Prune cadence and stop, service**: with injected `monotonic` and a fake
   `store.prune_intake_history` (monkeypatched on `h.store`): `_prune()` loops while
   the fake returns non-zero and stops at 0; a second `_prune()` within 3600 s does
   not call it; after 3600 s it does, with `before == clock() - 1 hour`. A raising
   prune (`WorkflowError`, `OSError`) is logged and `_stop` stays clear. **Stop
   mid-prune (C1-D2):** a fake that sets `obj._stop` on its first call and always
   returns 1000 is called exactly once — the prune exits after the batch in flight.
   Optionally the same with the real store (`_PRUNE_BATCH` = 2, a wrapper sets `_stop`
   after the first real batch) leaves the remaining old rows in place.
6. **Reclaim at open**: build a store with `Harness`, bulk-insert and then delete
   enough `audit`/`request_results` rows (monkeypatch the 64 MiB constant down), close;
   reopen → file smaller, `freelist_count` 0, store usable, no `-wal` growth left; a
   second reopen does not VACUUM (spy on `reclaim_free_pages`' `VACUUM` path via a
   monkeypatched threshold or by asserting `freelist_count` path returns early); below
   threshold → file size unchanged; a monkeypatched VACUUM raising
   `sqlite3.OperationalError` → open succeeds.
7. **Backup deadline**: `_backup_deadline` = 5.0 for a small file, scales with
   db + wal size (create a sparse/large temp file or monkeypatch `Path.stat`);
   `connect_existing` on a v3 fixture (`test_workflow_intake_migration.py` builds old
   versions) passes the scaled value to `_snapshot_database` (monkeypatch to capture
   `deadline_seconds`, then call through).
8. Regression areas to run targeted before the full suite: all
   `test_workflow_intake_*`, `test_workflow_migrations.py`, `test_workflow_store.py`,
   `test_workflow_od7_migration.py`.

Manual smoke (operator, after release — never by the agent on the live DB): launch the
new build; within minutes `workflow.db` stops growing; after the next launch the file is
tens of MB (~50 MB expected, not ~180 MB) and the media workflow is available within a
few seconds of the window.

## Decisions for the operator (defaults chosen; change only if the operator says so)

1. **Shrink on launch 2, not launch 1.** Default: prune in the running app, VACUUM at
   the next open. Alternative: also prune at open so the upgrade launch shrinks
   immediately, at the cost of ~2-3 min before the media workflow is available once.
2. **Retention 1 hour** for intake-adapter history (driver decision after debate
   round 1, replacing 1 day). Nothing reads it; 0 would also be safe. 1 day would
   have left ~180 MB after launch 2.
3. **FK enforcement off inside the prune batch** instead of a schema change adding an
   index on `request_results(audit_seq)` (which would itself trigger a migration
   backup and needs AC4 first).
4. **Packages the store re-derives as changed on unchanged content**
   (`needs_attention`, `blocked` other than `publication_blocked`, `receiving`) keep
   writing every scan, because their generation churn and dependent invalidation are
   current store semantics. Making them quiet is a semantic change for a separate issue.
5. **Observation, not in scope:** every store read commits the authority high-water
   (`_observe_time`, fsync under `synchronous=FULL`); and `connect_existing` runs
   `integrity_check` twice per open (~0.25 s total after this fix instead of ~25 s).

## Handoff notes

- Do the extraction first as its own commit and run the intake suites before adding
  the predicate; that isolates any behaviour drift.
- Never point a run at the live controller data or the app data directory; all stores in tests
  come from `tmp_path` / `Harness`.
- Commit messages: `perf(workflow): skip unchanged intake observations (#376) [skip ci]`,
  `perf(workflow): prune intake adapter history (#376) [skip ci]`,
  `perf(workflow): reclaim free pages at open, scale migration backup deadline (#376) [skip ci]`.
