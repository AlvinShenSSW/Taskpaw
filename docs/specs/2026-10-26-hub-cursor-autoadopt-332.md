# #332 — Hub: adopt the agent's event cursor automatically instead of pausing the channel forever

Date: 2026-10-26 · Base: `cb9aca4` (main, 3.10.6) · Branch: `afk/hub-cursor-autoadopt-332`
Planner: Claude (driver). Implementer: Claude Opus 5.5. Gate: Codex astra xhigh.
Design v3 (after critic rounds 1-3; see Debate record).

## Spec review

`taskpaw_v3/hub/server/poller.py`, `_admit_cursor` raises `StateError` for
several binding situations; `fetch_events` catches it, calls
`_channel(sid, reason)` (state `paused`, hint "use verified offline cursor
adoption") and returns `[]`. Nothing clears the pause except the offline CLI
`taskpaw-backend hub-cursor ... adopt-event-cursor --state-report <json>` run with
the server disabled. `/status` keeps `online=True` (the agent answers `/status`;
only `event_channel` shows `paused`). The reasons:

| reason | when | today |
|---|---|---|
| `cursor_adoption_required` | binding `fresh` or `unverified` while the Hub already holds events / outbox rows / consumed highwater for that server (`event_floor != -1`); typical after the Hub schema upgrade that created the `event_cursors` table with `unverified` rows | paused |
| `cursor_store_invalid` | `last_event_ids` config unparsable (`read_acks`), a `bound` row with no ack entry, or a deleted cursor row while `upstream_consumed` holds a row for the server (`event_floor` raises before admission) | paused |
| `state_identity_changed` | the agent's `server_id`/`stream_id` differ from the bound identity (agent state re-initialised) | paused |
| `cursor_floor_regressed` | the same stream offers a resume floor / highwater below what the Hub already received (agent restored from an older state) | paused |

Operator decision (issue #332): on a single-operator LAN nobody forges event
streams; a paused channel is just lost notifications. The Hub adopts the offered
cursor itself wherever that can neither skip an event the agent still holds nor
make a stored id collide, and keeps `paused` otherwise.

Two facts from the agent side decide the rules (`taskpaw_v3/core/protocol.py`,
`EventQueue`): the queue is in memory; at boot `resume_floor = next_id - 1` and
`offered_highwater` starts there and grows with every offered event; the agent
accepts `/events?ack=N` only for `N <= max(resume_floor, offered_highwater)`
(`_payload_locked`), else 409. So an adoption that commits `ack = floor` is
safe exactly when the agent has offered everything up to the Hub's floor:
`cursor["offered_highwater"] >= floor`. For a **different** stream the stricter
`cursor["resume_floor"] >= floor` is required: the in-memory queue of a new
stream may still hold ids `<= floor` that `ack = floor` would skip, unless the
stream's own durable floor is already past the Hub's floor (the agent rebooted
after passing it).

## Acceptance criteria

- AC1 `fresh` / `unverified` binding with `event_floor != -1`: adopt in place —
  `acks[sid] = floor`, bind to the offered identity, continue to fetch events
  with `ack=floor` — when `cursor["offered_highwater"] >= floor`; otherwise
  keep `paused` with `cursor_adoption_required` (the agent has not offered what
  the Hub holds, so an ack at the floor would be refused). A `fresh` binding
  with `floor == -1` keeps today's first-poll path (`acks[sid] = -1`).
- AC2 `bound` binding whose ack entry is missing (`sid not in acks`): the
  identity and regression checks of a bound row run as today; if they pass,
  adopt with `acks[sid] = floor` under the same `offered_highwater >= floor`
  guard, else `cursor_adoption_required`.
- AC3 Unparsable `last_event_ids`: `_load_acks` leaves `last_event_ids == {}`
  and sets `_ack_store_invalid`; every server is then admitted through AC2
  (ack missing) and the first successful commit passes
  `diagnostic=<raw config value>` so the raw value is preserved as a
  `last_event_ids.fault-<hex>` config row (`commit_event_cursor` already does
  this), then clears `_ack_store_invalid`. The fault copy is written once.
- AC4 (v3) Identity change on a `bound` row stays `paused` with reason
  `state_identity_changed`; only the hint changes to
  "重新登记该 Agent（事件流已重新开始）". Rebinding a bound row to a new stream is
  self-defeating: `upstream_consumed` keeps the old `stream_id`, and
  `event_floor` raises `cursor_store_invalid` on the very next call when the
  binding's stream differs from it (store.py 1447-1457), which would turn the
  adoption into a permanent `cursor_store_invalid` (critic C8, probed). The
  CLI refuses the same case (`__main__.py` ~166), so re-registering the agent
  stays the documented path. An `unverified` row whose recorded pre-upgrade
  identity differs from the agent's current identity adopts only when
  `cursor["resume_floor"] >= floor` (the re-initialised stream is already
  past the Hub's floor), else it stays `paused` with `state_identity_changed`
  (critic C9); no consumed row can exist for such a row yet, so the C8
  trap does not apply.
- AC5 `cursor_floor_regressed` stays `paused` exactly as today.
- AC6 `/status` and the `event_channel` field keep their shape; an adopted
  channel reports `ready`. One `log.warning("Event cursor adopted server=%s
  floor=%s")` per adoption with `floor != -1`.
- AC7 Tests (`taskpaw_v3/tests/test_event_cursor.py`):
  - `test_hub_uncertain_cursor_sends_zero_events`: the `floor` case stays
    paused (`cursor_floor_regressed`); `missing-binding` stays paused
    (`cursor_store_invalid`, raised by `event_floor` because
    `upstream_consumed` holds a row — the row was deleted by hand, the CLI
    path remains); `missing-ack` adopts (events requested, channel `ready`,
    `read_acks()[sid] == floor`); the `stream` and `server` cases keep
    asserting `paused` (now with the new hint text), as does the existing
    identity test near line 455 (unchanged stored bodies); a new test covers
    the `unverified` row with a differing recorded identity: adopted when the
    agent's `resume_floor >= floor` (a second `trusted_queue` with `start_id`
    above the floor), paused with `state_identity_changed` otherwise.
  - `test_malformed_ack_store_pauses_without_repair` becomes
    `..._is_repaired_with_a_fault_copy`: after one `poll_once`, `read_acks()`
    is valid, exactly one `last_event_ids.fault-*` row equals the raw value,
    the channel is `ready`, events were requested.
  - A new test for AC1's guard: `unverified` row, floor above the agent's
    `offered_highwater` → `cursor_adoption_required`, nothing committed.
  - Full suite, lint, mypy green.
- AC8 `docs/guides/event-cursor-recovery.md` states which cases adopt
  automatically; the CLI remains for `cursor_floor_regressed`, for a deleted
  cursor row with consumed history, and for a stream that restarted below the
  floor (where the agent must pass the floor *and* reboot before the next poll
  can bind, because the CLI binding carries `boot_id=None` and the next poll
  requires `resume_floor >= floor`). CHANGELOG entry, no version bump.

## Frozen issue contract

- Invariants kept: `commit_event_cursor`, `event_floor`, `read_acks`,
  `get_event_cursor` and the `event_cursors` schema unchanged; the offline CLI
  unchanged; dedup `UNIQUE(server_id, event_id)` is never bypassed; adoption
  never commits an ack the agent would refuse (`offered_highwater >= floor`
  guard) and never binds a different stream below the Hub's floor
  (`resume_floor >= floor`); `admit_result()` guards stay where they are.
- Allowed user-visible change: event channels recover on their own after a Hub
  upgrade (including an agent re-initialised since the pre-upgrade snapshot,
  when its stream is already past the Hub's floor), a lost ack entry, or a
  corrupt ack config; the hint text for the identity case changes. An identity
  change on a bound row still requires re-registering the agent.
- Non-goals: changing the agent side, the cursor protocol, `/events` query
  parameters, the CLI, or the schema; any new table or config key; the
  deleted-cursor-row case.
- Causal boundary: `_load_acks`, `_admit_cursor`, `_channel` (hint text) in
  `poller.py`; `test_event_cursor.py`; the guide; CHANGELOG.

## Assumptions

- A1 `event_floor` already includes stored events, outbox dedupe keys and the
  consumed highwater, so `acks[sid] = floor` cannot re-deliver anything the Hub
  holds (read: store.py 1428-1459; the critic probed the consumed-row raise).
- A2 `commit_event_cursor` with `diagnostic` writes the fault copy in the same
  transaction (read: store.py 1461-1498).
- A3 `_load_acks` runs only in `Poller.__init__` and `_persist_acks` has no
  callers (critic C5), so the raw corrupt value survives until the first
  adopting commit.

## Approach

```python
# _load_acks: unchanged except the message
except StateError:
    self._ack_store_invalid = True
    log.error("Event cursor config invalid; it is replaced at the next poll")
    return {}

# _admit_cursor, from `binding = ...` on
binding = self.store.get_event_cursor(sid)
identity = {k: cursor[k] for k in ("server_id", "stream_id")}
floor = self.store.event_floor(sid, self.last_event_ids)
acks = dict(self.last_event_ids)
adopt = False
if binding["state"] == "bound":
    if binding["identity"] != identity:
        raise StateError("state_identity_changed")      # as today (v3)
    if binding["boot_id"] != cursor["boot_id"]:
        if cursor["resume_floor"] < floor:
            raise StateError("cursor_floor_regressed")
    elif (
        binding["resume_floor"] != cursor["resume_floor"]
        or cursor["offered_highwater"] < floor
    ):
        raise StateError("cursor_floor_regressed")
    if sid not in acks:
        adopt = True
elif floor == -1:
    acks[sid] = -1
else:
    if (
        binding["identity"] not in (None, identity)
        and cursor["resume_floor"] < floor
    ):
        raise StateError("state_identity_changed")      # unverified, re-initialised stream (C9)
    adopt = True
if adopt:
    if cursor["offered_highwater"] < floor:
        raise StateError("cursor_adoption_required")
    acks[sid] = floor
    if floor != -1:
        log.warning("Event cursor adopted server=%s floor=%s", sid, floor)
new_binding = {... as today ...}
if adopt or binding != new_binding:
    with self._acks_lock:
        if not self._transport.admit_result():
            raise StateError("helper_cancelled")
        self.store.commit_event_cursor(
            sid, new_binding, acks,
            diagnostic=self.store.get_config("last_event_ids")
            if self._ack_store_invalid else None,
        )
        self.last_event_ids = acks
        self._ack_store_invalid = False
return cursor
```

The `_ack_store_invalid` early `raise StateError("cursor_store_invalid")` at
the top of `_admit_cursor` is removed (the flag now only selects the diagnostic
copy). `_channel`: hint per reason — `state_identity_changed` →
"重新登记该 Agent（事件流已重新开始）"; others keep today's text.

Alternatives rejected: treating an identity change like a new server by
clearing the server's events (deletes history); adopting without the
`offered_highwater` guard (would commit an ack the agent refuses and land in a
permanent `cursor_floor_regressed`, critic C4).

## Files to change

| path | change | reason |
|---|---|---|
| `taskpaw_v3/hub/server/poller.py` | `_load_acks` message, `_admit_cursor`, `_channel` hint | AC1-AC6 |
| `taskpaw_v3/tests/test_event_cursor.py` | update/split/add the tests named in AC7 | AC7 |
| `docs/guides/event-cursor-recovery.md` | automatic cases vs CLI cases | AC8 |
| `CHANGELOG.md` | one entry, no version bump | AC8 |

## Execution surface

Writes: the files above at the repository root of the issue branch.
Executes: `uv run pytest taskpaw_v3/tests/test_event_cursor.py taskpaw_v3/tests/test_hub.py`,
then the full suite, ruff, mypy; basetemp `<LOCALAPPDATA>/taskpaw-pytest/afk-332`.

## Key implementation notes

- The tests use `trusted_queue(tmp_path, start_id)` / `actual_agent` fixtures
  that run a real agent event queue; `queue.cursor_snapshot()` gives a real
  offered cursor. A re-initialised agent for the identity test is a second
  `trusted_queue` with a new state file and a `start_id` above the Hub's floor
  (so `resume_floor >= floor`); a faked `stream_id` on the old queue cannot
  pass `fetch_events` (the agent rejects the mismatched `cursor_stream`,
  critic C3).
- `acks` stays a copy of `self.last_event_ids`; when `_ack_store_invalid` it
  is `{}` already (critic C7), nothing else to reset.
- The `floor == -1` path for `unverified` rows (no history) binds through
  `binding != new_binding` as the `fresh` path does today.

## Risk assessment

| risk | likelihood | impact | mitigation |
|---|---|---|---|
| A new stream that passed the floor without rebooting | n/a | stays paused (`state_identity_changed`) until the agent reboots or is re-registered | hint text; guide |
| Repairing a corrupt ack map for a server with no stored events | low | that server restarts at -1 and re-receives its retained queue once; dedup by id | accepted |

## Out of scope

Agent-side lineage, the CLI, schema, the Mac Hub UI, the deleted-cursor-row case.

## Test plan

RED: the AC7 test changes fail on `cb9aca4`. GREEN: targeted, then full suite + lint + mypy.

## Debate record

- v1 → v2 (2026-10-26, critic round 1): C1 (P1) the identity rule was stated as
  `next_event_id > floor` in AC4/AC7 and as `resume_floor >= floor` in the
  snippet; v2 keeps the snippet rule everywhere and corrects the expectation
  for the line-455 test (stays paused). C2 (P1) a deleted cursor row with a
  consumed row raises `cursor_store_invalid` inside `event_floor` before
  admission; v2 drops `missing-binding` from the adopted set and documents it.
  C3 (P2) the `stream`/`server` fixture fakes cannot converge because the real
  agent rejects the mismatched `cursor_stream`; v2 tests with a genuinely
  re-initialised agent. C4 (P2) adoption had no range guard; v2 adopts only
  when `offered_highwater >= floor` and runs the bound-row checks even when the
  ack is missing. C5 (minor) repair sequencing verified. C6 (minor) guide
  wording about the CLI's `boot_id=None` binding added to AC8. C7 (minor)
  `acks = {}` dropped, warning gated on `floor != -1`.
- v2 → v3 (critic round 2): C1, C2, C3, C4, C7 revalidated resolved. C8 (P1,
  probed): rebinding a bound row to a new stream trips `event_floor`'s
  consumed-row check and becomes a permanent `cursor_store_invalid`; v3 drops
  the bound-row identity adoption (AC4) and keeps the hint change. C9 (minor):
  `unverified` rows with a differing recorded identity now require
  `resume_floor >= floor`. Checks (a)–(e): no finding.

## Handoff notes

Commit `fix(hub): adopt event cursors automatically where no replay is possible (#332) [skip ci]`.
