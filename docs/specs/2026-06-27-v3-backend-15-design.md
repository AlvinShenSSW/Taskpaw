# V3 #15 — Backend minimal loop + clean shutdown (design)

Issue #15 (V3 #2). First real V3 code: a **headless** FastAPI backend for both the
agent and the Hub, the end-to-end poll→store→OpenClaw loop, and the graceful
shutdown primitive that #5's "X = exit" and the headless service mode both reuse.
**No UI in this issue.** Follows the V3 design §3.1, §3.2, §7, §10 #2.

## Scope (this issue)

- `taskpaw_v3/` monorepo backend skeleton (`core/`, `agent/`, `hub/`).
- **Agent** FastAPI app: network-facing read API `/ping /status /events` (Bearer);
  a **loopback-only** control API (`/control/*`) on a separate port. Default
  network port **5680**; refuse to start (clear message) if the port is in use
  (e.g. a V2 agent on 5678 or another v3 instance).
- **Hub** FastAPI app + poller: polls agents (`/events?ack=`), stores to SQLite,
  forwards to OpenClaw through the durable **outbox** (carrying forward #14's
  clear-on-ack + outbox-as-source-of-truth + at-least-once semantics).
- **Graceful shutdown primitive** (`core/lifecycle.py`): one registry that, on
  SIGTERM / shell signal / programmatic stop, runs every registered stop callback
  (stop monitor threads, terminate managed child processes, release ports), idempotently
  and bounded by a timeout. Reused by interactive (#5 X-exit) and service modes.

## Out of scope (later issues)

- Monitor plugins / supervisor → #17. The agent here exposes a **static** status
  (machine info + configured monitor stubs); real monitors land in #17.
- Tauri shell / React UI → #19. Security acceptance pass → #16.

## Layout

```
taskpaw_v3/
├── core/
│   ├── config.py       # pydantic AgentConfig / HubConfig (+ load/save YAML)
│   ├── auth.py         # Bearer check (empty token = disabled, like V2)
│   ├── protocol.py     # Event model + {"events":[...]} envelope, ack semantics
│   └── lifecycle.py    # GracefulShutdown registry (callbacks, signals, children)
├── agent/server/
│   ├── app.py          # FastAPI: /ping /status /events (Bearer) + /control (loopback)
│   ├── launcher.py     # bind, port-in-use check, run uvicorn, wire shutdown
│   └── service.py      # OS-service entrypoint (headless)
├── hub/server/
│   ├── store.py        # SQLite: servers/status_log/events/delivery_outbox
│   ├── poller.py       # poll /events?ack=, store, enqueue outbox, drain
│   ├── openclaw.py     # POST to OpenClaw hooks
│   └── app.py          # FastAPI hub API + starts poller + shutdown
└── tests/
```

## Key contracts

- **Ports:** agent network `5680` (configurable `bind_addr`); control API loopback
  (`control_addr`, default `127.0.0.1:5681`). Port-in-use → exit with a message
  naming the conflict and the V2 migration note.
- **Auth:** Bearer on `/status` + `/events`; `/ping` open; empty token disables
  auth (V2 parity). 401 never drains the event queue.
- **Clear-on-ack / outbox / at-least-once:** identical semantics to V2 #14 —
  store + enqueue before advancing the durable ack; outbox retry + dead-letter.
- **Shutdown:** `GracefulShutdown.shutdown()` is idempotent, runs callbacks
  LIFO, terminates registered child processes, joins threads with a timeout, and
  is invoked on SIGTERM/SIGINT and by the (future) Tauri shell signal.

## Tests

Config round-trip; auth allow/deny + 401-doesn't-drain; `/events?ack=` trim/return
+ legacy no-ack; port-in-use detection; graceful shutdown runs callbacks +
terminates a fake child + is idempotent; hub poller store+enqueue+ack ordering
(at-least-once) reusing the #14 contract.
