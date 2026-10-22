# Issue #286: Mac Hub workflow monitoring

## Spec review and frozen contract

Issue #286 A–E makes the Mac Hub a read-only view of the Windows Agent's media
workflow and forwards its operator notifications to OpenClaw. Phase 1 has one
workflow controller and one event consumer. Reuse the existing workflow and UI;
no generalized controller, permission or transport framework is needed.

Acceptance:

- A: five GET-only Agent network resources (`/workflow/films`, film detail,
  plans, poster, cover), existing api_token authorization, disabled workflow 404,
  identical enabled-workflow JSON to the control reads, bounded bodies and the
  existing board image path helpers. Enumerate every network route as GET-only.
- B: existing EventQueue wire shape, monitor `media-workflow`, durable dedupe by
  film ID/input revision/event kind; needs-planning, needs-confirmation,
  stage failure/attention, existing translated summary and archive outcomes.
  Existing TaskLog calls and records remain unchanged.
- C: equivalent bounded authenticated Hub GET proxy and a `媒体处理` tab using
  the existing MediaWorkspace with `readOnly` default false. Hide every mutation
  and guard callbacks; make only GET requests to the selected Hub source.
  Preserve overview, recommendations, queue, confirmation status, exceptions,
  details/steps, LTX progress, parameters, timing and cover lightbox. Show exactly
  `只读监控 · 操作请在主控电脑上进行` without slogan headings.
- D: status.md contains `## 媒体处理`, group counts and the running film's progress.
  Compute 完成今日 using the Hub's local date. Workflow notification text starts
  `TaskPaw 媒体 | `; other events remain unchanged.
- E: document the Mac Hub as the only /events consumer next to the poller and in
  deployment.md. A future Windows controller reads /status and /workflow/* only.

Invariants: retain Bearer auth, event IDs/cursors/outbox semantics, atomic state
writes, bounded transport, clean thread shutdown and Agent console behavior.
Allowed product changes are only the five resources, read-only Hub view,
workflow notifications and status section above. No workflow execution,
planning/lookup/timing/store/schema/migration internals change. local_dispatch.py
permits one small event-emission hook only. No V2, release/version, launchd,
auto-update, Windows Hub controller, deployment or live state changes.

## Approach and repository evidence

### Reads and network surface (A)

Extract the existing five reads in `agent/server/workflow_routes.py` into small
shared functions in `agent/server/workflow_reads.py`; control routes call them
without changing output/lifecycle behavior. Add thin wrappers in
`workflow_network.py`, wired through create_network_app's late-bound provider.
The network wrappers check auth first, return 404 when disabled, and use the
same board.poster_path/cover_path helpers. JSON cap: 1 MiB (film_proxy policy);
image cap: 8 MiB, read at most cap + 1 bytes. Oversize local resources return 413.
Do not mount the control router or add target/performer/control resources.
Disable network OpenAPI if necessary so every registered route has GET only.

### Events and summary (B, D)

A small workflow event adapter observes existing read APIs, not workflow writes.
It lives beside the Agent server integration and owns a JSON dedupe sidecar in
the workflow data directory, written with temporary-file + os.replace. Keys
contain film ID, input revision and kind (including stage for stage failures).
Only a successfully enqueued event becomes a seen key; a queue failure remains
retryable. A sidecar error is logged and stops new emission until recovered,
without stopping workflow execution. Never silently reset corrupt markers.

The adapter's observer belongs to the existing LocalWorkflowHost lifecycle,
uses a stoppable periodic wait and joins before the runtime store closes.
A single optional callback in LocalDispatcher's existing resolved-stage logging
path delivers immediate failed-stage / translated events with the job identity;
its exception cannot change the stage result or existing TaskLog behavior.
The observer finds planning, confirmation, attention and archive/cleanup states
from board/read APIs; it also recovers translated completion from persisted
stage state. Reuse timing.summary_text for the existing 全程 wording; do not edit
timing.py. Archive success, pending cleanup and cleanup completion are distinct
kinds so a pending-cleanup result cannot suppress its later completion.

Normal Agent restarts reload the dedupe sidecar; Hub restarts use existing
persisted event cursor/outbox dedupe. This does not claim transactional
exactly-once delivery across a process crash between queue and marker writes:
EventQueue already persists the counter rather than the payload. Replacing that
contract with another outbox is outside this task.

A compact additive `workflow` summary in /status supplies group counts, current
progress and recent completion timestamps to the existing Hub poller; no second
Hub polling service or database schema is added. The observer reads film_board
with done_limit=None for the summary because the ordinary board retains only
20 completed rows. Completion is the latest successful cleanup ended_at; send
recent completion instants spanning the Hub's possible current local day and
let status_md convert them to Hub-local dates. Cached summary starts unavailable,
is cleared on observer failure/stopped host, and never marks stale work running.

The OpenClaw prefix belongs in hub/server/store.py commit_upstream_batch's
existing payload branch for monitor == media-workflow, retaining all cursor,
transaction and outbox behavior. No raw failure detail or token is logged by
new transport code.

### Hub reads and UI (C)

Add five fixed GET routes under /servers/{sid}/workflow/* through the Hub's
existing read-route registration. Follow film_proxy's server validation,
current polling-token lookup, timeout, no redirects, error sanitization and
limit + 1 reads; images allow only the media types the board serves.
No token is sent to the browser and no proxy action mutates Agent state.

When the tab mounts, probe enabled registered Agents through the GET proxy and
use the one successful workflow source. Phase 1 has exactly one; no source
picker or controller registry. Cache keys include source/server identity.
Unavailable/no source is an honest read-only empty/error state.

MediaWorkspace's existing context receives readOnly and the selected source,
with reads directed to Hub proxies. Control-only target/preflight and performer
queries stay disabled. Backend group=confirm displays generic 待确认 rather than
permanent 检查中 or an invented readiness assertion. ReviewDialog never mounts.
Pending choices become static recommendations; pause/resume, enqueue, requery,
confirm, retry, download/stop and folder-opening controls and callbacks are
unavailable. LtxDownload is already display-only without name/onRespond.
LocalPage uses the selected server snapshot; LaterPages omits Agent config reads.
Standalone cover/Agent callers keep their defaults. Reuse the existing V3 CSS,
responsive layout and no-slogan rule (build spec decision 7).

### Single consumer (E)

Existing cursor lineage establishes pairing/boots, not reliable consumer
identity. Document the invariant without adding a heuristic startup warning
that could mislabel a normal restart. Existing cursor mismatch warnings remain.

## Files and execution surface

| Files | Allowed change / reason |
| --- | --- |
| Agent server workflow_reads.py, workflow_network.py, app.py | Shared reads and network wrappers (A) |
| Agent server workflow_events.py, launcher.py; workflow/local_host.py | Event adapter, host lifecycle and compact status wiring (B/D) |
| workflow/local_dispatch.py | Single optional resolved-stage event callback only (B) |
| Hub server workflow_proxy.py, app.py | Fixed workflow read proxies (C) |
| Hub server status_md.py, store.py, poller.py | Summary rendering, notification prefix, consumer comment (D/E) |
| UI api.ts, HubDashboard.tsx, existing media components/context/model | GET source and readOnly rendering (C) |
| Python/UI tests; deployment.md; this design | Contract tests and documentation |

Read-only dependencies: workflow board, plan/planning, lookup, timing,
executors, all stores/schema/migrations; core EventQueue and Hub cursor code.
The source branch is afk/claude-u1b-271. Rebase onto its latest tip before PR
publication/update. The deliverable remains a Draft PR to that branch, never
merged; main/base branch and operator services/data/config are untouched.

Generated outputs: `uv sync --locked --group dev` creates the worktree .venv from
pyproject.toml/uv.lock; `npm ci` creates UI node_modules from the lockfile;
`npx tsc -b` / `npm run build` create ignored build outputs; temporary fake-data
browser verification creates six PNG screenshots (overview, pending, LTX drawer
at 1440 and 390 px). Tests and screenshots use only isolated temporary configs,
data and fake services on loopback ports. No live Windows address/token is used.

## Risk and verification

The material risks are accidental control requests, hidden image path divergence,
missed/repeated notifications, stale summaries, completion-date mismatch and
rebase overlap with the Windows implementer. Pin each with focused tests rather
than additional product architecture.

Tests first: auth/disabled/parity/size/path-safety/all-GET network tests; event
kinds, same-key/new-revision/restart dedupe, queue/state failures and lifecycle;
Hub proxy limits/status/token/redirect/image tests; fetch-level readOnly UI
navigation/drawer/lightbox with no mutation/control requests and Agent regression;
status section/progress/today dates and unchanged other notification text;
Hub restart cursor/outbox notification regression. Record RED then GREEN.

Before external review run the operator's complete verification set: full
`uv run --locked pytest -p no:cacheprovider`, Ruff check/format, mypy native,
linux and win32; UI npm ci/test/lint, tsc -b and build. Capture complete logs and
summary counts. Full final suite repeats only if later content changes invalidate
that evidence; do not rerun unchanged expensive checks for ceremony.

After self/internal checks: Opus 5.5 outer, then Kimi final on the same revision.
Fix demonstrated in-scope P1s; record P2/minor dispositions without broadening
scope. Six-cycle default allowance, no arbitrary additional call/time limits.
Keep Draft as explicitly requested and report remote validation as actually
executed; do not equate a skipped Draft job with CI success. PR description
contains A–E changes, review results, test counts and six screenshots, followed
by a short issue #286 comment linking the PR.

## Assumptions and handoff

All work uses a fake Agent for integration/screenshots: no operator confirmation
of a live Windows address/token was provided. The read-only acceptance is tested
against the same shared snapshot functions; real Windows deployment acceptance
is not claimed. Claude/Kimi binaries and helper dry runs work; remote model
access is unverified until actual review. One-user LAN scale is deliberate.
