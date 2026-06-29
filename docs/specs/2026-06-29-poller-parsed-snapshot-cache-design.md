# #107 Cache the parsed agent snapshot in the poller — design (2026-06-29)

Stop re-parsing each agent's `status_json` on every `/status` request. Issue:
[#107](https://github.com/AlvinShenSSW/Taskpaw/issues/107) (raised by the #105
Kimi 终审). Backend-only, perf.

## Problem

`Poller.snapshot_statuses()` (`hub/server/poller.py`, added in #96) runs
`json.loads(snap["status_json"])` for **every server on every `/status` call**.
The JSON was already validated when fetched. Under #95's 5s dashboard
auto-refresh this re-parses the whole fleet every 5 seconds for no benefit.

The poller already keeps an in-memory `_status_snapshot[server_id] =
{reachable, status_json, last_seen}` written under `_snap_lock` on each poll.

## Design

Parse once, at write time; cache the dict; read it directly.

- Add a `parsed_status` field to each `_status_snapshot` entry: the agent's
  `/status` parsed to a `dict` (or `None` if absent/unparseable).
- A small `_parse_status(raw)` static helper centralizes the parse + the
  "dict-only, else None" rule (identical to today's inline logic).
- Write sites:
  - `_poll_server` (reachable) → `parsed_status = _parse_status(status_json)`.
  - `_poll_server` (unreachable) → carry forward `prev.get("parsed_status")`,
    alongside the existing carry-forward of `status_json` / `last_seen`.
  - `_seed_snapshot` (restart) → `parsed_status = _parse_status(row[...])` so the
    first `/status` before any poll is also parse-free.
- `snapshot_statuses()` returns `snap.get("parsed_status")` directly — **no
  `json.loads` on the request path**.

`status_json` (raw) stays in the snapshot: `status_snapshot()` →
`write_status_md` still consumes the raw string for the OpenClaw-compat
`status.md`. We add a parsed copy; we don't replace the raw.

Thread-safety is unchanged: parsing happens inside the existing `_snap_lock`
write critical section (poller thread); the API thread still only copies the dict
under the lock.

## Test plan (`uv run pytest`)

1. After a poll, `snapshot_statuses()` returns the parsed dict, and it no longer
   calls `json.loads` on read — assert by monkeypatching `poller.json.loads` to
   raise/count: a `snapshot_statuses()` call after the poll must not invoke it
   (parse happened at write time).
2. Unreachable-after-good poll still returns the last-known parsed snapshot
   (carry-forward), with `online=False`.
3. Existing `/status` contract + `test_poller_keeps_latest_status_snapshot` stay
   green (regression).

## Out of scope

Caching the raw→parsed for `status.md` (it needs the raw string); any change to
the `/status` wire shape.
