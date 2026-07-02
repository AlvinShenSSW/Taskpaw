# Codex Independent Audit Findings

> Audit date: 2026-05-06  
> Scope: Follow-up review of `CODE_AUDIT_REPORT.md`, `CHANGELOG.md`, and current source files.  
> Reviewer: Codex

This file lists findings that appear missing, incomplete, or newly introduced after the KIMI audit/fix pass.

## Summary

Most of the P0/P1/P2 fixes listed in `CHANGELOG.md` are present in source, including atomic writes, `shell=False` for custom commands, Hub foreign keys, rollback handling, monotonic polling, rotating logs, Hub IP/port validation, and dependency upper bounds.

The main remaining risks are around event delivery compatibility, unauthenticated HTTP APIs, MacSubs event ID persistence, and a few smaller validation/documentation gaps.

## Findings

### 1. High: Windows TaskPaw events are currently broken/lost

**Files:**
- `taskpaw.py:185`
- `taskpaw.py:275`
- `taskpaw_hub.py:487`

`taskpaw.py` creates events without an `id` field and returns `/events` as a raw JSON list. `taskpaw_hub.py` expects a response shaped as `{"events": [...]}` and filters events by increasing `id`.

**Impact:** Hub either fails to parse events or filters all of them out. Because `/events` clears the queue immediately, notifications can be permanently lost.

**Suggested fix:**
- Add monotonic `id` values to Windows TaskPaw events.
- Return `{"events": events}` from TaskPaw `/events`.
- Consider making Hub tolerate both raw-list and wrapped formats during migration.

### 2. High: HTTP API auth is still missing, including MacSubs

**Files:**
- `taskpaw.py:305`
- `macsubs.py:98`
- `macsubs.py:169`

KIMI correctly left TaskPaw API auth as pending, but `macsubs.py` has the same issue: it binds to `0.0.0.0`, has no auth, and sends permissive CORS headers.

**Impact:** Any LAN client can read status and drain events. On public/shared Wi-Fi this exposes workflow details and can break notification delivery.

**Suggested fix:**
- Add shared bearer-token auth to `taskpaw.py`, `macsubs.py`, and Hub polling.
- Store the token separately from the OpenClaw token unless intentionally using one shared secret.
- Do not clear events for unauthorized requests.

### 3. High: MacSubs event IDs reset on restart

**Files:**
- `macsubs.py:59`
- `macsubs.py:64`
- `taskpaw_hub.py:630`

MacSubs now adds event IDs, but `_next_event_id` is in memory only. Hub persists `last_event_ids`, so after MacSubs restarts and begins again at `1`, Hub may ignore all new events until the counter exceeds the previously persisted value.

**Impact:** Post-restart MacSubs completion/error events may never reach Hub/OpenClaw.

**Suggested fix:**
- Persist the next event ID in a small state file under the MacSubs base/cache directory, or
- Include a boot/session identifier and update Hub dedupe to use `(session_id, id)`.

### 4. Medium: FolderWatcher 0-byte false completion is still unfixed

**File:** `taskpaw.py:1343`

The audit report mentioned 0-byte files being treated as stable complete files. Current code still records and counts zero-byte files toward `stable_seconds`.

**Impact:** Empty failed downloads or placeholder files can trigger false "file complete" notifications.

**Suggested fix:**
- Skip zero-byte files before updating `file_sizes` and `stable_count`, or make zero-byte notifications an explicit opt-in.

### 5. Medium: MacSubs and Hub default port mismatch remains

**Files:**
- `macsubs.py:6`
- `macsubs.py:42`
- `taskpaw_hub.py:1111`
- `DEPLOYMENT_GUIDE.md`

MacSubs runs on `5679`, while Hub defaults new servers to `5678`. The main deployment docs mostly describe `5678`.

**Impact:** Default setup can make Hub fail to connect to MacSubs unless the user manually knows to enter `5679`.

**Suggested fix:**
- Either standardize MacSubs on `5678`, or
- Add clear MacSubs-specific setup docs and UI hints that MacSubs uses `5679`.

### 6. Medium: MacSubs monitor loop still silently swallows exceptions

**File:** `macsubs.py:475`

The top-level monitor loop catches `Exception` and only `pass`es.

**Impact:** Directory scan failures, path errors, and unexpected processing errors can be hidden while the status keeps returning idle/waiting.

**Suggested fix:**
- Log the exception.
- Add an error event.
- Update `/status` to an error state.

### 7. Low/Medium: Windows agent API port validation is incomplete

**File:** `taskpaw.py:2063`

The TaskPaw UI clamps API port to at least `1`, but does not enforce the upper bound `65535`.

**Impact:** Invalid ports can be saved and API startup then fails.

**Suggested fix:**
- Validate `1 <= api_port <= 65535`.
- Show a UI error instead of silently saving/falling back.

### 8. Low: Config loading is brittle for migrations

**File:** `taskpaw.py:149`

`AppConfig.from_dict()` constructs watchers via `WatcherConfig(**w)`. If a config contains one unknown legacy/future key, config loading fails and the app falls back to a blank default config.

**Impact:** A single stale field can make all monitor configuration appear lost.

**Suggested fix:**
- Filter watcher dicts to known dataclass fields before constructing `WatcherConfig`.
- Log ignored keys.

## Verified Fixes Present

The following KIMI changelog items were checked in source and appear present:

- Atomic `save_config()` write with temp file and `os.replace()`.
- Custom command execution changed to `shlex.split()` plus `shell=False`.
- Hub SQLite `PRAGMA foreign_keys=ON`.
- Hub write operations use rollback on failure.
- Hub polling uses `time.monotonic()`.
- Hub `status.md` write is atomic.
- Rotating log handlers for TaskPaw and Hub.
- Hub IP and port validation.
- Hub event dedupe persistence.
- MacSubs `/events` endpoint no longer hardcodes an empty list.
- Dependency upper bounds in `requirements.txt`.

## Verification Notes

I could not run `python -m py_compile` because the local `python.exe` launcher is inaccessible in this environment. This folder also is not a git repository, so the audit was done against the working files directly.
