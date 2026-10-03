# V2 outbox timestamp migration (R03 / #220)

V2 stored `delivery_outbox.created_at` and `next_attempt_at` as naive Hub-local
wall times. It did not record the Hub timezone, offset, DST fold, or changes of
machine/timezone. Those missing facts cannot be recovered from the DB or current
host timezone. V3 never guesses UTC or today's local offset for these rows.

## Version 1 and quarantine

The first V3 startup classifies the existing queue in one transaction. Aware
ISO timestamps preserve their instant and microseconds, normalized to
`YYYY-MM-DDTHH:MM:SS.ffffff+00:00`. Naive timestamps require an explicit source
zone through the offline tool. Without one they retain both original timestamps
and enter `outbox_quarantine` with `source_timezone_required`.

Both timestamps must validate before either is changed. JSON must be an object;
attempts must be a nonnegative SQLite integer; kind/state must use the existing
values. Malformed rows are retained with fixed reason codes (`invalid_time`,
`invalid_attempts`, `invalid_payload`, `invalid_kind`, `invalid_state`). DST
round-trips through the source zone determine the instant: zero valid candidates
is `nonexistent_time`, two is `ambiguous_time`, one permits conversion. The tool
never chooses a fold or shifts a missing wall time.

`outbox_migrations` records version, apply time, declared source, and backup file.
Version 1 means classification completed, including quarantines. A later normal
startup/apply neither rewrites rows nor releases quarantines or creates another
backup/run. Unknown newer versions fail closed. Existing IDs, payload bytes,
attempts, kind, state, last_error, dedupe keys, and dead-letter alert flags remain
unchanged. No guessed V2 dedupe keys are introduced.

Quarantined rows are excluded from delivery and automatic dead-letter pruning;
they have no TTL. A new quarantine logs its ID, fixed reason and field once after
commit, without payload/config contents or an OpenClaw notification. Explicit
server removal retains its existing deletion semantics and removes the related
quarantine metadata. Before automatic pruning deletes an expired dead letter,
it validates the complete row and quarantines malformed candidates unchanged.
Quarantine and deletion commit together; a failed prune rolls both back. Valid
dead letters retain the existing seven-day policy.

## Offline preview and apply

Stop all V2/V3 Hub processes before applying or manually repairing data. Use a
copy to rehearse these commands. Replace `hub.db` with the configured Hub DB;
these commands do not discover or modify other databases.

```bash
python -m taskpaw_v3.hub.server.outbox_migration --db hub.db
python -m taskpaw_v3.hub.server.outbox_migration --db hub.db --legacy-zone UTC
# Apply only after verifying the original Hub really used this source zone:
python -m taskpaw_v3.hub.server.outbox_migration --db hub.db --legacy-zone UTC --apply
```

Without `--apply`, the connection is read-only: no schema/data changes, database
creation or backup. Output contains version/source, changed flag, normalized IDs,
new quarantine IDs/reasons/fields, unresolved rows, and released IDs. It never
contains queue payloads or secrets. Invalid source input fails before writes.

`--legacy-zone` accepts `UTC`, an explicitly declared fixed offset such as
`+05:45`, or an IANA zone name. A fixed offset declares that the entire relevant
legacy interval used that offset; it is not a substitute for historical DST.
Windows may have no IANA timezone data. Unavailable data returns
`timezone_data_unavailable`, with no fallback or new runtime dependency. A
trusted TZif file can be supplied with `--legacy-tzfile source.tzif` instead; its
SHA256 is recorded. Do not supply both flags.

If the old Hub changed timezone or its source is unknown, leave those rows
quarantined until trustworthy records resolve them. A single source declaration
cannot reconstruct per-row historical timezone changes. Aware rows use their
own offsets even when a legacy source is supplied.

A normal apply on an already-classified DB does not release quarantines. Explicit
recovery rechecks only currently quarantined rows:

```bash
python -m taskpaw_v3.hub.server.outbox_migration --db hub.db --legacy-zone UTC --retry-quarantined
python -m taskpaw_v3.hub.server.outbox_migration --db hub.db --legacy-zone UTC --retry-quarantined --apply
```

For ambiguous/nonexistent times or genuinely bad fields, the operator must first
establish the correct values from trusted evidence and repair the offline DB
(e.g. both timestamps with explicit offsets). There is no built-in SQL editor or
automatic repair. **Make a complete private snapshot before any manual edit if
you need its previous values.** The automatic apply/retry backup is taken at the
start of that operation, after any earlier manual edits; it cannot recover those
pre-edit values. Still-invalid rows remain isolated. A successful retry normalizes
times and removes the matching quarantine inside one transaction. A no-op retry
adds no run/backup and does not recreate previously delivered rows.

## Backup, failure and rollback

Before an existing DB's first schema/data upgrade or an actual recovery batch,
the writer obtains `BEGIN IMMEDIATE` and creates a complete SQLite backup from a
separate read-only connection. This includes committed WAL data; merely copying
`hub.db` can omit it. The exclusive temporary file is checked with
`PRAGMA quick_check`, closed, fsynced, then atomically published to a unique
`hub.db.outbox-v1-<id>.bak` in the same directory. POSIX backup mode is 0600;
Windows uses the DB directory's existing privacy boundary. Protect the directory
and snapshots: they contain config/payload data, and must not enter git or logs.

Backup progress has a five-second deadline with batched pages and bounded busy
waits. It does not promise to interrupt native filesystem calls that hang.
Permission, space, timeout, check, flush or rename failures abort migration,
remove temporary files, roll back and close connections. No version, partial
schema or half-converted rows are committed. A completed backup remains available
if a later schema/data/commit step fails. The failure is visible at startup/CLI;
the program does not silently bypass backup. Resolve the underlying failure
before retrying. Large/real databases and platform directory protection require
operator validation.

To restore, stop every Hub and confirm all DB connections are closed. Preserve
the current DB and any WAL/SHM sidecars separately for diagnosis. Restore the
complete backup to the DB path and remove that path's stale WAL/SHM before opening
it. Rehearse restoration on a copy and compare tables. For a V2 downgrade, use a
pre-conversion backup: V2 must not consume the normalized aware queue.

Restoration before any outbound send has no new delivery side effects. After
sends, restoring a snapshot may replay notifications; reconcile externally before
resuming. Delivery remains at-least-once, without an exactly-once promise.

## Polling and compatibility

Each poll cycle samples all enabled Agents before one bounded outbox drain.
Query failures and individual parse/time/send/state-update failures are contained,
so healthy Agents and later healthy notification rows continue. Runtime malformed
rows are quarantined, never deleted as a parse-error response. Failed quarantine
persistence leaves the row intact and reports a fixed local processing failure.

OpenClaw disabled or token-empty means no drain/new enqueue; sampling, history and
ack persistence continue. Backlog attempts/timestamps remain intact. Reenable
resumes only valid, non-quarantined rows using their original age and attempts:
attempt cap 10 and age greater than 24 hours still dead-letter once. Turning the
switch on never implicitly releases a quarantine or resets the age clock.

This does not introduce a delivery worker. The existing ten-row batch and HTTP
timeout remain; notification I/O can still delay the next cycle. `status_log`
SQLite localtime, status.md, last_seen, event timestamps, wire protocol, ack and
dedupe contracts are unchanged. V2 sources are unmodified.

Portable tests use the real V2 DDL and synthetic TZif bytes, so DST cases require
neither the machine's timezone nor optional IANA data. The existing CI matrix runs
Linux Python 3.10/3.12, Windows 3.12 and macOS 3.12.
