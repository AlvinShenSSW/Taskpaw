# TaskPaw V3 #1 Event Delivery Implementation

## Clear-On-Ack

The agent keeps the existing `/events` endpoint and response envelope:

```json
{"events": [...]}
```

`ack` is an optional integer query parameter.

- `GET /events` with no `ack`: legacy behavior. Return all queued events and clear
  the queue immediately.
- `GET /events?ack=N`: first trim queued events with `id <= N`, then return events
  with `id > N` without clearing them.
- Hub polls with `ack=<last_event_ids[server.id]>`, where `last_event_ids`
  represents the Hub's durably persisted ack point for that server. Fetching
  events does not advance this value.
- If an older agent returns 404 for `/events?ack=N`, the upgraded Hub falls back to
  `/events`.

Compatibility matrix:

| Agent | Hub | Behavior |
|---|---|---|
| upgraded | upgraded | clear-on-ack; repeated polls with the same ack replay unacked events |
| upgraded | old | no `ack`, so legacy clear-on-read still works |
| old | upgraded | Hub falls back to `/events`; legacy clear-on-read still works |
| old | old | unchanged legacy behavior |

## Additive Event Fields

`add_event(machine, monitor, message, level=None, title=None, data=None)` preserves
the existing event fields: `id`, `time`, `machine`, `monitor`, `message`.

Optional fields are included only when provided:

- `level`: one of `info`, `warn`, `alert`, `done`
- `title`: string title for richer consumers
- `data`: dict payload for structured details

Old consumers continue to ignore unknown fields.

## Hub OpenClaw Outbox

Hub-internal SQLite table: `delivery_outbox`.

The outbox is the source of truth for OpenClaw event delivery. For every fetched
event, the Hub first stores the event history row, then inserts a `pending`
outbox row, and only after both writes commit does it advance and persist
`last_event_ids` for that agent. The poll loop drains due outbox rows at the
start of a cycle and again after newly fetched events are enqueued, so normal
delivery is still prompt but no longer depends on a direct in-loop send.

This is intentionally at-least-once. If the Hub crashes after storing/enqueuing
an event but before persisting the ack point, the next poll re-fetches that event
and may store/enqueue/deliver a duplicate. That rare duplicate is acceptable; the
ordering prevents the worse failure mode where an event is acked to the agent
before it is durable in the Hub and is then lost completely.

Columns:

- `id`
- `server_name`
- `payload_json`
- `kind`: `event` or `summary`
- `delivery_state`: `pending`, `failed`, or `dead_letter`
- `attempts`
- `last_error`
- `next_attempt_at`
- `created_at`
- `dead_letter_alerted`: durable guard for the one local alert

State machine:

```text
live send fails -> failed
pending/failed due -> retry
retry succeeds -> delete row
retry fails -> failed with attempts+1 and exponential backoff
attempts >= 10 or age > 24h -> dead_letter
dead_letter older than 7 days -> prune
```

Dead-letter policy:

- 10 failed attempts or 24h row age marks the row `dead_letter`.
- The Hub emits exactly one local high-priority alert via its local log channel.
- Dead-letter alerts are not sent through OpenClaw.

## Deferred Overflow Spill

The agent keeps `MAX_EVENTS_QUEUE` as an OOM backstop. Under a sustained
Hub-down or never-ack condition, once more than 10,000 unacked events accumulate,
the oldest unacked events are dropped and a loud error is logged. Durable
overflow spill-to-disk is intentionally deferred to the V3 backend work (#15).
