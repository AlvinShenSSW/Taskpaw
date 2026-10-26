# #334 — Agent: prune `workflow-events.json` markers of films the observer no longer walks

Date: 2026-10-26 · Base: `cb9aca4` (main, 3.10.6) · Branch: `afk/workflow-events-prune-334`
Planner: Claude (driver). Implementer: Claude Opus 5.5. Gate: Codex astra xhigh.

## Spec review

`taskpaw_v3/agent/server/workflow_events.py` (`WorkflowEvents`) dedupes
workflow notifications with `_seen`, a set of keys `[film_id, revision, kind]`
persisted as `workflow-events.json` (`atomic_json`, fsync). `emit` adds a key and
rewrites the whole sorted set once per **new** key (a repeated key returns before
any write, so the issue's "every emit" is really "every new key"). Nothing ever
removes a key, so the file grows by roughly ten keys per film forever.

`observe` (every 5 s) walks the board: films in the `done` group whose two
cleanup stages ended outside the last two days are skipped (`continue`) and are
never emitted again; films removed from the board are not walked at all. Their
keys are dead weight.

Core need: bound the file without changing dedup for films that can still emit.

## Acceptance criteria

- AC1 After each successful `observe` pass **in which the marker file is
  loaded** (`_loaded` is True; it becomes True on the first `emit`, and a
  corrupt file keeps it False so nothing is ever wiped), every `_seen` key
  whose `film_id` was not walked in that pass (skipped as old-done, or absent
  from the board) is removed; the file is rewritten once per pass only when
  something was removed. Accepted (critic C1, minor): a film intaken,
  dispatched and failed between the pass's board read and its prune can lose
  its fresh key once and notify twice; the window is one pass on a home board.
- AC2 Dedup for walked films is unchanged: a repeated emit for a live film is
  still suppressed across restarts (existing
  `test_identity_revision_kind_dedupe_survives_agent_restart` keeps passing).
- AC3 A pass that fails (`except Exception` → `_clear()`) or runs while the host
  is not `running` prunes nothing.
- AC4 Tests: a key for an old-done film and a key for a removed film disappear
  after one pass; a key for a live film stays; no write happens on a pass with
  nothing to prune (count `atomic_json` calls or compare mtime). Full suite,
  lint, mypy green.
- AC5 CHANGELOG entry, no version bump (agent-side fix; a mixed fleet is
  unaffected because the file is per machine).

## Frozen issue contract

- Invariants kept: key format and file format (`{"version": 1, "seen": [...]}`)
  unchanged; `emit`'s write-on-new-key and "paused until saved" behaviour
  unchanged; no new config, thread or file.
- Allowed user-visible change: a film that returns to the board after being
  done for more than two days (or after removal and re-intake under the same
  film id and revision) may notify once more. Accepted.
- Non-goals: changing the two-day window, batching `emit` writes, touching the
  Hub side.
- Causal boundary: `WorkflowEvents.observe` (one prune block at the end of the
  successful path) and, if needed, a tiny helper; tests; CHANGELOG.

## Assumptions

- A1 `json.loads(key)[0]` recovers the film id from every key (`emit` builds
  keys with `json.dumps([film_id, revision, kind])`; `_ready` validates only
  that entries are strings). A key that fails to parse is removed too (it can
  never match an `emit` key). Read, not executed.
- A2 `observe` holds no `_lock` today; the prune takes `self._lock` for the set
  mutation and the save, like `emit`.

## Approach

At the end of the `try` block in `observe`, after the loop and before the
summary is stored:

```python
walked = {film.film_id for film, row in zip(films, rows) if <not skipped>}
with self._lock:
    if self._loaded:
        stale = {k for k in self._seen if _film_of(k) not in walked}
        if stale:
            self._seen -= stale
            self._dirty = True
            self._save()        # OSError/ValueError: keep dirty, emit's _ready flushes later
```

Track `walked` by adding the film id in the loop body right before
`self._observe_film(...)` (the `continue` for old-done films skips that line).
`_film_of(key)` returns the first element of `json.loads(key)` or `None` on any
error.

Alternative rejected: pruning inside `emit` (would run on every new key and
needs the board, which `emit` does not have).

## Files to change

| path | change | reason |
|---|---|---|
| `taskpaw_v3/agent/server/workflow_events.py` | prune block in `observe`, `_film_of` helper | AC1-AC3 |
| `taskpaw_v3/tests/test_workflow_events.py` | prune tests using the existing `observer` fixture and film fixtures | AC4 |
| `CHANGELOG.md` | one entry | AC5 |

## Execution surface

Writes: the files above at the repository root of the issue branch. Executes:
`uv run pytest taskpaw_v3/tests/test_workflow_events.py`, then the full suite,
ruff, mypy; basetemp `<LOCALAPPDATA>/taskpaw-pytest/afk-334`.

## Key implementation notes

- The existing tests build boards with helper fixtures whose cleanup ends are
  older than the two-day window on purpose (see the fixture docstring near line
  32 of the test file); reuse them for the "old-done film" case.
- `_save` may raise `OSError`; keep the key removal in memory and `_dirty` set so
  the next successful `_ready` flushes it (same as `emit`).

## Risk assessment

| risk | likelihood | impact | mitigation |
|---|---|---|---|
| A film observed as old-done, pruned, then its cleanup re-run → one repeat "completed" notification | low | one duplicate line | accepted in the contract |

## Out of scope

Anything on the Hub; the summary window; `emit` batching.

## Test plan

RED: the AC4 tests fail on `cb9aca4`. GREEN: targeted file, then full suite + lint + mypy.

## Debate record

- Round 1 (critic): C1 (minor) in-pass race accepted and recorded in AC1;
  C2, C3, C4, C6, C7 no finding; C5 (minor) `_loaded` guard made explicit in
  AC1 (wording only). No design decision changed; clean round, debate closed.

## Handoff notes

- Keep the prune outside the `_observe_film` loop and outside any `_lock`
  hold that spans `emit` (non-reentrant lock; critic C4).
- Tests: seed keys through `emit` first (this also loads the file), use the
  `later` fixture + `board_film({...})` for the old-done film and
  `film(planned=False)` for the live one, wrap `events.atomic_json` to assert
  zero writes on a second pass (pattern at test lines ~483-490).

Commit `fix(agent): prune workflow event markers of films no longer observed (#334) [skip ci]`.
