# Hub upstream isolation

The Hub polls each Agent sequentially. Each `/status` or `/events` request runs in
one bounded, cancellable HTTP helper, not an Agent/Hub runtime. Source installs use
`python -m taskpaw_v3.hub.server.upstream_worker`; the shipped sidecar uses the
fixed `upstream-http` dispatch. Bearer/settings travel through stdin, never argv.
The helper starts no listener, config, database, monitor or readiness flow. It
refuses redirects, preserving the polling Bearer boundary.

## Limits and retry

Limits are inclusive, fixed implementation constants, not new configuration keys.

| Surface | Limit |
|---|---|
| Status entity | 256 KiB |
| Events entity | 2 MiB |
| HTTP framing metadata | 64 KiB total; 8 KiB per status/header/chunk/trailer line |
| Streaming body read | At most 16 KiB and remaining allowance plus one byte |
| Request/result IPC | 32 KiB / 4 MiB, UTF-8 JSON |
| JSON depth/numeric token | 32 / 256 characters |
| Status/events envelope nodes | 20,000 / 200,000 |
| Events per complete response | 10,000 |
| One event | 64 KiB canonical UTF-8; 4,096 nodes |
| Event message/title | 32 KiB UTF-8 each |
| Other known event strings | 4 KiB UTF-8 each |
| Historical candidate admission | 128 rows and 2 MiB globally per initialization/poll tick |

Unsupported content encodings are refused; the helper does not decompress input.
Status must be an object with finite numbers and encodable strings. `monitors`
retains both object and legacy list shapes; monitor entries and `metrics` must be
objects. Present known string fields must be strings. Safe additive JSON fields
remain supported. Duplicate status keys, nonfinite values, invalid Unicode,
malformed framing, whole-response size/depth/count/node overflow or invalid cursor
proof reject the response. No prefix is committed or acknowledged.

The existing `http_timeout` is a monotonic **total helper request budget**, covering
input delivery, DNS, connect, headers, body, validation and reply. Socket timeouts
alone cannot bound a drip-fed response or blocking DNS. The parent owns this
budget independently of blocking raw pipe writer/reader threads. Cancellation
closes its own keeper descriptor, terminates/reaps its owned child as necessary,
and joins both non-daemon I/O owners within a shared one-second cleanup grace.
It never flushes or synchronously closes another thread's active buffered writer.

A physically blocked creation, OS I/O or kill cannot be promised an absolute kernel
deadline. Time spent in process creation is charged when it returns. A retained
process/thread/pipe produces `helper_cleanup_failed`, forbids replacement helpers
and prevents successful service stop; later bounded cleanup can retry. It is not
successful native cleanup. Frozen runtime wrappers require actual platform
verification, including the owned interpreter rather than just Popen reaping.

Failed requests retry at the existing poll cadence. An oversized/malformed whole
envelope requires upstream repair; the Hub does not repeatedly acknowledge a
partial prefix. Logs contain only server ID, phase and fixed reason, without raw
upstream bodies, URL/header secrets or exception text. OpenClaw's existing UTC
outbox retry/quarantine and redirect policies are unchanged.

## Last-good status and historical rows

A live valid status is committed to `status_log` and a separate per-server canonical
last-good cache before publication. Failure preserves that cache and last-good
receipt time, marks the source offline, and updates a fixed error/attempt time.
Healthy sources remain usable. Hub `/status` adds `status_health` with `state`,
`error_code`, `attempted_at`, `last_good_at`, `age_seconds`; API reads copy already
admitted objects without parsing upstream JSON. `status.md` preserves its normal
ONLINE/OFFLINE grammar and adds a separate `Upstream:` error/age line.

Runtime age uses monotonic elapsed time; restart age uses canonical UTC receipt
metadata. A clock rollback gives null age and `clock_changed`. Historical local
timestamps have no trustworthy timezone/DST offset: legacy last-seen text can
remain, but age is unknown and a legacy sample alone is not fresh/online.

Recovery scans older status rows in bounded round-robin turns with a persisted
continuation, including rows beyond the first allowance. Oversized candidates do
not enter Python whole; SQL returns length and a budget-admitted bounded BLOB
prefix. This bounds Python candidate return/parsing, **not SQLite VM allocation or
disk I/O**: SQLite CAST/length/substr can materialize the original TEXT internally.
Early cursor identity seeding shares the initial historical allowance and leaves
unreliable/unexamined identities unverified. Pending recovery rows are protected
from history pruning; the independent last-good cache survives ordinary pruning.
Corrupted caches are diagnosed/revalidated before use. Recovery can temporarily
show `recovering`, then retain a validated historical sample with an explicit
historical error/unknown age until a new valid observation succeeds.

## Complete event disposition and evidence

After current status/cursor admission, a complete in-limit proof-bearing response
is classified item by item. Missing, boolean, coerced-string, nonpositive,
out-of-range or unoffered IDs, unsafe bodies and duplicate-key items are quarantined
without losing valid neighbors. Valid unique IDs are delivered in increasing order;
the first valid variant wins. Exact/conflicting duplicates and out-of-order input
also produce receipts. Sparse legacy fields keep their existing defaults; unsafe
present fields are not coerced into strings.

One SQLite transaction persists accepted events, enabled/active OpenClaw outbox
rows, malformed-item receipts, permanent consumed floor and durable ack. Only
then is the in-memory ack advanced to the proof's `offered_highwater`. Rollback
changes none of those, and the next request retries with the previous durable ack.
A poison-only/poison-last response can therefore finish with evidence rather than
replay forever. Existing event/outbox deduplication remains in force.

This relies on the Agent's complete queue response and **monotonic delivered ID**
contract: it cannot introduce a new lower ID after a prior highwater was confirmed.
Reserved gaps are allowed; contiguous IDs are not assumed. A future lower ID is a
producer violation and cannot be recovered by this ack protocol. This does not
add a disk-backed Agent queue or exactly-once OpenClaw delivery.

Receipts contain only fixed reason, ordinal, canonical byte length, SHA-256
fingerprint, safe ID or null, cursor scope, UTC times and saturated repeat count.
They never retain raw body/message/token. Retention is seven days, 256 rows per
server and 4,096 globally. Eviction preserves per-server count/time/reason summary;
the consumed floor is permanent and participates in offline cursor adoption even
after receipts/history are pruned. Disabling retains evidence; removing a server
cascades its new metadata. Hub `event_channel.quarantine` reports bounded retained/
evicted counts and last reason after polling. Metadata corruption pauses the
channel under the existing verified offline recovery rules, never resets its ID.

See [event cursor recovery](event-cursor-recovery.md) for operator adoption steps.
Native smoke tests execute only owned random-loopback HTTP fixtures and the fixed
helper command. Windows source/frozen preread backpressure and actual shipped
helper clean cancellation are separate required evidence; a retained-failure test
passing cannot close native clean acceptance. No real TaskPaw/config/database or
normal packaged runtime is needed by these tests.
