# Mac Hub monitor reliability — 2026-10-25

This audit covers the operator-approved home-LAN, single-user Mac Hub work for
[#223](https://github.com/AlvinShenSSW/Taskpaw/issues/223),
[#232](https://github.com/AlvinShenSSW/Taskpaw/issues/232), the necessary part of
[#233](https://github.com/AlvinShenSSW/Taskpaw/issues/233), and the OpenClaw status
read contract. Source, automated checks, installed software, a running service,
and delivered notifications are separate evidence below.

## Baseline and isolation

- Initial checkout: `main`, HEAD `fa926f7`, V3 source version `3.9.18`.
- Refreshed `origin/main`: `ae193cba1e3d533d34a2f55f99917700405e4e81`, V3
  source version `3.10.2`. `git log origin/main..fa926f7 --oneline` is empty;
  there is no unpushed commit on that initial HEAD to carry into this scope.
  The initial working tree had no tracked uncommitted changes.
- Implementation is isolated on `fix/mac-hub-monitor-reliability`, based on
  `ae193cb`. Existing work and the Windows session's branches are preserved.
- The Windows session owns #322 / `3.10.3` and #323 / `3.10.4`. This work does
  not reserve either release version or change the Agent status/temperature UI.
- `origin/main` contains merge `5931640`,
  [PR #290](https://github.com/AlvinShenSSW/Taskpaw/pull/290), implementing the
  read-only Mac media monitor described in
  [the #286 spec](../specs/2026-10-22-hub-workflow-monitor-286.md).
  [#286](https://github.com/AlvinShenSSW/Taskpaw/issues/286) remaining open is
  not evidence that this implementation is absent; it is not reimplemented.
- [The October 2 follow-up](2026-10-02-audit-follow-up.md) is a historical
  snapshot, not the current source or installation inventory. The old AFK run
  was not resumed for this scoped work.

## Design and completion matrix

The Mac remains a read-only monitor for Windows execution/control. It does not
execute media work, approve tasks, archive files, or clean files. The Hub remains
the only consumer and acknowledger of Agent `/events`.

1. Persist whole-Agent availability transitions in the Hub. Debounce transient
   failures, record a sustained outage and its recovery once per incident, and
   carry that state across Hub restart. Disabling a device is an operator choice,
   not a failure. Preserve bounded reason codes for authentication, invalid
   responses, and transport failure.
2. Separate notification delivery from sampling with a small bounded worker.
   Retain SQLite outbox ownership, retry/backoff/dead-letter behavior, and the
   rule that durable event admission precedes acknowledgement advancement.
   Bound in-flight sends, response bytes, total request duration, stop, restart,
   and resource cleanup. No general scheduler or message broker is introduced.
3. Expose only actionable notification health: pending count, oldest pending
   age, last success, failure, and dead-letter state. Local Hub health events are
   retained even while OpenClaw is disabled or its token is absent.
4. Keep existing status fields and add evidence needed to judge them: data age,
   last successful sample, and failure reason. A cached success does not establish
   present health. Document the actual SQLite read contract and file freshness.

| Area | Already complete at baseline | Required change or verification | Field status |
| --- | --- | --- | --- |
| Mac read-only media view | PR #290 is in `ae193cb`; existing scope is preserved | Reuse its read-only routes and ownership boundaries | Not installed/verified as a running Mac Hub |
| #223 device availability | Current failures have status-health evidence | Durable debounced offline/recovery events, restart and disable semantics | No active Hub to exercise |
| #232 delivery isolation | SQLite outbox, retries/dead letters, durable event admission; current `poll_once` calls `drain_outbox` once after serial device polling, with a 10-row batch | Independent bounded send lifecycle and RED→GREEN evidence | No live OpenClaw send attempted |
| #233 notification health | Existing durable delivery rows | Minimal health projection; retain local health events when delivery is inactive | OpenClaw is disabled/unconfigured locally |
| OpenClaw reads | `upstream_status` keeps last accepted status and attempt/error metadata; `status_log` keeps successful history | Freshness-safe SQL and Markdown evidence; compatibility tests | Existing Markdown is stale and configured DB is absent |
| Mac residency | Foreground/headless entrypoint and desktop teardown behavior documented | Keep service installation separate from source delivery | No Hub launchd service found; no deployment authorized by this report |
| #234 event bodies | Persistent Agent sequence/cursor support is distinct from payload storage | No Agent event-body redesign in this scope | Crash-loss and exactly-once claims remain excluded |

At inspection, the integration guide's example selected `s.online`, but the
`servers` schema has only `id`, `name`, `ip`, `port`, and `enabled`; the example
does not run against this schema. `status_log` receives successful samples, not
every failed attempt. Current attempt/error metadata is in `upstream_status`.
Reading the latest successful history row alone cannot establish current health.
The former guide claim that the database and Markdown can never disagree also
needs correction: their writes are separate, and both can become stale if the
Hub stops.

## Read-only Mac inspection

Inspection time: **2026-10-25 18:27 JST** (`2026-10-25T09:27:06Z` for the live
Agent GETs). No secrets were printed. No `/events` endpoint was read, no real
notification was sent, and no service was stopped, started, or deployed.

| Check | Observed evidence | What this establishes |
| --- | --- | --- |
| Process inventory | `ps -axo pid,ppid,lstart,comm` found installed `TaskPaw Agent.app` with PID tree `44771 → 44772 → 44773`, started October 22 at 01:11:51 JST; no Hub process found | An Agent is running, not a Hub |
| Agent version | Installed bundle `Contents/Info.plist` reports `3.9.12`; authenticated GET `/ping` and `/status` both report `3.9.12` | Actual running Agent version is independently verified; source `3.10.2` is not deployed by implication |
| Listening sockets | `lsof -nP -iTCP -sTCP:LISTEN` shows Agent `192.168.1.33:5678` and loopback control `127.0.0.1:5681`; no 5690, 5691, or 18789 listener | Current Agent uses configured port 5678; no Hub or configured local OpenClaw listener is available |
| Agent status | Read-only `/status`: Jellyfin `ok`, host `ok`, VibeCoding `running`; API token is present | These are current Agent observations; no event queue was consumed |
| Hub configuration | Redacted projection: bind `127.0.0.1:5690`, poll interval 60 seconds, `write_status_md=true`, `self_monitor=true`; OpenClaw disabled, token absent, destination origin `http://127.0.0.1:18789` | Existing configuration does not enable notification delivery |
| Service registration | `launchctl list` contains the desktop Agent application entry, not a Hub service. Matching plist inspection in user/system LaunchAgents and LaunchDaemons found no TaskPaw Hub/OpenClaw service entry | No configured Hub residency was found by these checks |
| Status file | Configured data directory's `status.md`: 56 bytes, three lines, no device entries. Embedded text: `Last updated: 2026-07-01 13:01:42`. Filesystem mtime: `2026-07-01T05:01:42.628281Z`, about 116 days before inspection | The file is historical and cannot prove current aggregation. The offset-less embedded timestamp is preserved as text, not reinterpreted as JST |
| Database | No `hub.db` in either configured Hub data directory or config-adjacent legacy location | No deployed Hub database could be inspected for present fleet/outbox state |
| Hub log | Installed desktop Hub log exists, 220 bytes; mtime `2026-07-01T05:01:52.845017Z` | Historical launch evidence only, not an active Hub |

The authenticated Agent GETs used a direct configured address, disabled proxy
discovery, refused redirects, and bounded response reading. Only version and
monitor state projections were printed; credentials were read from local config
without appearing in argv or output.

Normal desktop closure is not an always-on service mechanism. Current
`taskpaw_v3/src-tauri/src/main.rs` handles the close confirmation and terminates
the backend on exit. [Deployment documentation](../guides/deployment.md)
separately describes foreground processes and OS service managers. No existing
window was closed during this inspection, so this report does not claim a
native close-window test or a working background Hub installation. No actual
OpenClaw reader script was identified or executed; interface documentation and
artifact inspection do not establish that OpenClaw currently consumes them.

One inspection command using `uv run` refreshed three development-environment
packages automatically. No tracked source, Hub/Agent config, event queue, or
running service was changed by that command. Subsequent inspection used the
existing interpreter without dependency synchronization.

## Automated acceptance and review record

These checks use temporary databases, fake OpenClaw endpoints, and controlled
Agents. They establish local source behavior, not delivery to the configured
real OpenClaw endpoint. Log names below identify the retained run evidence;
initial working-tree passes are not a final-commit or publication approval.
This is the pre-review checkpoint; final commit-bound checks, external review
and package links are recorded in the delivery PR.

| Scenario | RED evidence | GREEN evidence |
| --- | --- | --- |
| Transient failure versus sustained offline/recovery | `availability-baseline-red.log`: no durable offline event after the grace period; six baseline failures cover this, reason retention and stale cache | `availability-green-0.log`: 22 passed across availability and related status/restart regressions; one incident emits one offline/recovery pair after three failures and 120 observed seconds |
| Authentication, invalid data, network failure classification | Baseline availability reason cases fail; `delivery-compat-red.log` also records generic network/timeout reasons and an accepted-message size rejection | The corresponding availability checks and five `delivery-compat-green.log` cases pass; included in the final targeted transport/settings pass |
| Disabled device, repeat incidents, deduplication, restart | Additional failure-path regression coverage; no separate baseline RED is claimed for every case | Availability tests cover disable/re-enable between samples, disabled in-flight results, endpoint replacement, stopped observations, restart without counting Hub downtime, and atomic state/event/outbox rollback |
| Slow send while status sampling continues | `delivery-service-baseline-red.log`: with one blocked fake notification, two controlled Agents fail to reach a second sample within three seconds | `delivery-service-green-0.log`: four passed in 22.43 seconds; both Agents continue sampling while delivery is blocked, with one in-flight notification |
| Send concurrency, response bytes, total deadline | `delivery-response-validation-red.log`: three failures include truncated 2xx success accepted and unsafe metadata error propagation; `delivery-manual-test-red.log`: ten failures in shared sender/error mapping | Matching response/manual tests turn green. `delivery-final-settings-regression.log`: 75 passed, including 37 transport tests for HTTP/TLS, 64 KiB response limit, bounded metadata, slow drip, hanging DNS, cancellation, one sender shared with manual tests, and a four-second request budget plus cleanup allowance |
| Stop/restart, child/thread/socket cleanup | `delivery-manual-stop-red.log`: stop does not finish a manual sender's owned cleanup | Matching stop case passes in `delivery-manual-stop-green.log`; targeted transport and service passes cover repeated/idempotent start, cancellation, process/pipe/thread cleanup, DB reopen and service restart |
| Event persistence before ack; retry/dead-letter behavior | Existing admission/ack regression assertions retained. Initial integration failures assumed `poll_once` also drained notifications; these are recorded as outdated test coupling, not new delivery guarantees | `hub-integration-green.log`: 154 checks pass after tests call the separate drain phase; durable admission-before-ack, corrupt-row quarantine, retry/dead letters, disabled backlog and success-receipt rollback remain covered. Service tests prove cancellation preserves the queued row across DB reopen/restart |
| Local health event persistence with delivery disabled/no token | Initial session probes failed because local-history/health behavior was absent; no standalone baseline log is claimed for those probes | `test_hub_notification_health.py` passes in the integrated group: disabled or missing-token self-health events survive DB reopen without new undeliverable rows; history retention preserves pending delivery; busy/cancelled admission does not consume retries |
| Freshness, last success, errors, compatible DB/Markdown readers | Baseline cache checks report healthy after expiry and `ok` after stale restart; the prior guide SQL selected nonexistent `servers.online` | Availability and integrated health checks pass: cache expiry is explicit, Markdown includes age/last success/delivery health, and the actual documented SQLite example runs against temporary DBs for healthy, authentication-error and stale states |

- Full backend and shared suite: **not yet green**. The initial `uv run pytest`
  was still running at this checkpoint and had 37 failures in the existing
  redirect suite. `redirect-integration-red.log` reproduces those 37 failures;
  the tests now exercise the owned sender and separate drain, retaining original
  redirect status, no-forward, token, ACK, retry and dead-letter assertions.
  `redirect-integration-green.log` records 37 passes; `redirect-full-green.log`
  records all 258 redirect tests passing. A final full pass is still required,
  regardless of these targeted results.
- Initial lockfile, syntax, repository Ruff lint/format and mypy checks pass
  (181 typed source files). An initial two-error Markdown-age typing check was
  corrected before the recorded repository-wide pass. Final-revision checks
  remain pending.
- Initial frontend checks pass: 1,070 tests in 42 files; ESLint reports only
  two existing `react-refresh` warnings; TypeScript/Vite build completes with
  1,956 modules and the existing chunk-size advisory. These are source/build
  checks, not native desktop package or installed-app validation.
- Initial internal review: **APPROVE WITH COMMENTS**, no supported P1.
  `IR-MAC-01` is retained as **P2 deferred**: settings accepts uppercase HTTP(S)
  schemes, but the helper rejects them before sending. Existing lowercase
  configuration is unaffected; no P2-only repair was made under the retained
  AFK policy. Final immutable review binding and the user-selected Kimi K3
  external gate remain **pending**.
- Commit and PR: **pending**. Default delivery is a PR; no automatic deployment.

## Remaining boundaries

- Native Hub installation, launchd residency, restart/reboot recovery, window
  independence, and actual notification receipt remain unverified until a
  separately authorized deployment and field check.
- #234 remains open in scope terms: persisted event sequence numbers do not
  persist the Agent's in-memory event bodies. This work makes no crash-lossless
  or exactly-once guarantee and does not redesign the Windows Agent queue.
- LAN Bearer authentication, loopback-only control, response validation, and
  refusal to forward credentials across redirects remain required. Mac media
  execution/control remains out of scope.

## Mac Agent upgrade compatibility

The operator subsequently included upgrading this Mac's existing Agent so the
new Windows controller and Mac Hub can read its status. This does not enable
media execution on the Mac or authorize consuming its live event queue.

A second read-only check at **2026-10-25 18:31:38 JST** fetched only `/status`
from the configured running Agent. The current Hub decoder accepted the
3.9.12 payload (2,279 bytes, three monitors), and its durable event cursor
validated. The latest `AgentConfig` loaded the existing configuration without
changing its bytes: monitor definitions, API token, network port 5678 and
loopback control port 5681 were preserved. New defaults leave workflow and
remote workers disabled. Power sampling defaults to enabled; this is an additive
monitoring capability.

The controller's current `PowerFleet` reader was exercised with that response
through a fake fetch function, without contacting a real Windows host. It
returned `no_power` with `answered=true` and `no_busy`; it requested only
`/status` and did not treat the Mac as an idle execution worker. The deployed
3.9.12 response lacks `power` and `energy`, whereas current source adds them and
the authenticated GET `/power/history` endpoint. This is a demonstrated gap in
the installed build's optional power/history support, not a demonstrated break
in the base status protocol. No Agent compatibility source patch was needed.
`core/protocol.py`, `dev_activity.py` and `jellyfin.py` are unchanged between the
3.9.12 source release `b2b6b49` and this branch's `ae193cb` base.

Regression coverage added in `tests/test_mac_monitor_compat.py`:

- A pre-power/pre-workflow YAML fixture round-trips through the current model
  with its monitors, token and non-default ports intact; workflow remains off
  and no workflow host/data directory is created.
- The actual network read routes feed the Hub decoder and controller reader
  with power absent, disabled or active. A Mac power sampler uses injected
  CPU/GPU readers and disposable SQLite state.
- Reads remain authenticated; power history is GET-only; disabled workflow
  reads return 404. The controller requests only `/status` and the three
  supported power-history ranges. A pending fixture event and its cursor are
  unchanged; attempting the event-consumption method fails the test.

Targeted result: **4 passed** with `uv run pytest -q
tests/test_mac_monitor_compat.py`; targeted Ruff lint and formatting pass.
Initial test-authoring failures were import setup and an incorrect fixture
expectation about the controller snapshot shape, not product compatibility
failures. They are not claimed as a product RED→GREEN fix. Existing Starlette
TestClient/httpx deprecation warnings remain unrelated to this change.

Actual registration on the Windows controller, shared Bearer configuration,
and two-machine connectivity are still unverified. No remote-controller address
was inferred. A later registration must honor the Mac's configured port 5678.

For packaging, keep source version `3.10.2` and identify the candidate by commit,
artifact run and checksum; do not take Windows versions `3.10.3` or `3.10.4`.
The shared production Mac is not a disposable runtime-smoke host. Local
`scripts/build.py --skip-tauri` can produce a backend-only artifact; it cannot
establish a verified desktop installer. The existing release workflow supports
manual dispatch on the reviewed branch using fresh native macOS runners and
artifact upload, without creating a release/tag or stopping the local Agent.
Its existing Windows matrix is validation, not a new platform implementation.
Candidate build, native verification, installed-app replacement and actual
notification receipt must each retain their own evidence; this compatibility
check does not establish any of them.
