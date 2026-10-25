# Mac monitoring reliability and Agent compatibility

## Frozen contract

Operator scope, 2026-10-25: a single-user home LAN; Windows executes and controls,
Mac monitors. Complete #223 and the explicitly narrowed parts of #232/#233.
PR #290 already implements the read-only media view and is not reimplemented.
The subsequent request adds compatibility and an upgrade package for the Mac
Agent currently running 3.9.12. Preserve its existing monitoring configuration.
Publication is a reviewed PR; no automatic merge or Hub deployment. The operator
subsequently authorized replacing and restarting this Mac's installed Agent,
preserving its configuration and event identity.

Acceptance criteria:

1. Persist a whole-Agent outage after three consecutive failed status samples
   and at least 120 seconds of observed failure. First discovery has the same
   grace. A short failure produces no alert. One sustained incident produces
   one offline event and one subsequent recovery. Restart preserves the phase;
   time with the Hub stopped does not itself establish failure. Disabling or
   replacing a device resets observation without manufacturing recovery.
2. Preserve authentication, validation and network error reasons. Cached status
   older than `max(2 * poll_interval, 60)` seconds is not current health. Preserve
   existing status fields and successful `status_log` rows; add freshness evidence.
3. Sampling never waits for OpenClaw. One independent worker owns at most one
   notification HTTP operation. Reuse the existing cancellable helper transport,
   bounded request/reply pipes, process cleanup and no-redirect policy. Limit
   response bodies to 64 KiB and metadata through the existing bounded reader.
   The nominal five-second send budget includes the helper cleanup allowance.
   Stop signals cancellation, joins all owners, and does not close SQLite early.
4. Preserve outbox retries, dead letters and durable-event-before-ack ordering.
   Local events and availability phase changes commit atomically with any outbox
   entry. A delivery success receipt and row deletion also commit atomically.
   Uncertain success remains retryable; OpenClaw can receive duplicates.
5. Expose pending count, oldest pending age, last successful delivery, last
   failure and dead-letter/quarantine counts through additive read APIs and
   `status.md`. Disabled/missing-token states differ from an empty working queue.
   Hub self-monitor events persist even when OpenClaw is inactive, without
   creating undeliverable new queue entries. Local history is bounded.
6. Verify the installed Mac Agent's read protocol against the new Hub and the
   controller's existing read interfaces, using isolated state and fake peers.
   Fix only demonstrated Mac compatibility gaps. Produce a Mac Agent candidate
   package from the reviewed source without taking Windows version numbers.

Security invariants: LAN Bearer; loopback-only control; no token-bearing redirect;
bounded validated responses; atomic file replacement; clean process shutdown.
Mac Hub is the sole consumer of Agent `/events`; compatibility tests must not
consume a live queue. No Agent payload persistence/exactly-once promise (#234).

## Approach and causal boundary

Add a small durable availability table and a separate local `hub_events` table.
The latter keeps Hub-generated identities distinct from Agent event IDs. Existing
Agent event storage, cursor admission and acknowledgement ordering stay intact.
Use Hub UTC for persisted times and monotonic time for observed failure durations.
Cache age uses the greater of UTC and monotonic elapsed time so Mac sleep cannot
keep an old sample healthy; a future UTC timestamp fails closed as clock_changed.
Across restart retain accrued failure time, but resume measuring at a fresh sample.

Retain serial Agent polling and its existing request bounds. Extract only
notification scheduling into one stoppable thread with one existing Transport.
No futures backlog, general scheduler, message broker, fleet concurrency project,
dead-letter replay UI, or operations dashboard is introduced. Existing outbox
rows pause while disabled and resume when enabled; new inactive events stay local.

The existing integration guide incorrectly uses `servers.online`, which does not
exist, and treats the latest successful `status_log` row as current health. Replace
that example with read-only SQLite access to `upstream_status`, explicit age/error
evaluation and disabled handling. A static file cannot expire itself after Hub
exit: consumers must reject an old `Last updated` or stale per-device timestamp.

## Files and execution surface

| Paths | Action and reason |
| --- | --- |
| `taskpaw_v3/hub/server/availability.py`, `store.py`, `poller.py` | Durable transitions, local history, notification receipts and cache freshness |
| `taskpaw_v3/hub/server/delivery_worker.py`, `openclaw.py`, `upstream_worker.py` | Isolated bounded notification transport using existing helper lifecycle |
| `taskpaw_v3/hub/server/app.py`, `status_md.py` | Worker ownership, self-monitor sink, additive read evidence |
| `taskpaw_v3/tests/test_hub*.py`, related existing transport/outbox tests | RED→GREEN and regression checks with temporary DBs/fake servers |
| `docs/guides/openclaw-integration.md`, dated audit | Correct read contract and distinguish source/runtime/delivery evidence |
| Mac Agent read protocol/config/build paths | Inspect first; amend this contract with demonstrated gaps before edits |

Generate candidate packages with the repository's `scripts/build.py` using the
existing uv build/v3 dependencies, UI dependencies and Tauri toolchain. Outputs
belong to ignored build directories and a named delivery directory. The separately
authorized Agent replacement uses a verified package, retains rollback evidence
and checks real startup/configuration; Hub deployment remains excluded.
Inspect build flags before execution. Do not touch Windows #322/#323 branches,
Agent status UI, temperature collection, or their 3.10.3/3.10.4 releases.

## Assumptions and risks

- Field inspection found no running Mac Hub/OpenClaw and no Hub DB. The running
  Mac Agent is 3.9.12. This does not prove any Windows deployment version.
- Current source foreground desktop close tears down its backend. No window was
  closed in the field. Always-on operation requires separately deployed service
  configuration; source/GUI presence is not service uptime.
- The existing helper bounds network operations through process cancellation;
  native frozen-package startup/cleanup still needs package verification.
- SQLite writes remain serialized. A failed commit must not advance an incident
  phase or acknowledgement; a network success followed by failed commit may
  repeat. No external idempotency guarantee is assumed.
- Exact Kimi K3 configuration is being verified; no review outcome/model identity
  is claimed until the invocation and result are retained.

## Verification and handoff

Targeted RED→GREEN covers jitter, first failure, sustained failure, recovery,
restart, disable/re-enable, status/auth/JSON/network failures, transactional write
failure, slow/large/redirected OpenClaw responses, cancellation, resource release,
independent repeated samples, outbox restart/dedupe and history without a token.
Then run canonical Python/UI suites, lock/lint/format/types/build checks, independent
internal review and the user-selected Kimi K3 outer gate on the final revision.
Keep current CI and package/field evidence separate. Actual notifications, LAN
outage/sleep and installed-app replacement remain unclaimed until performed.

This contract records an interactive task adopted into AFK after initial RED tests
and isolated implementation had started; it does not retroactively label those
edits as having passed AFK design review.
