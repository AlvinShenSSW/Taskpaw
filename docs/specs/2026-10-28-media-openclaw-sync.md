# Media workflow status for OpenClaw (#351)

## Contract and evidence

The operator needs the Mac Hub/OpenClaw to see the media center's Jasna fleet
queue and blackgoldpig controller state. Main already carries
`WorkflowEvents.snapshot()` through Agent `/status.workflow`, Hub `upstream_status`
and `status_log`, and `status.md`. The GUI's `/workflow/films` proxy is a separate
on-demand read. We extend the existing summary, not the polling protocol.

The observer already reads the film board, merged local/remote currents, routes
and dispatcher. Its four-machine summary silently omits larger fleets; it has
no unfinished-film preview or dispatch state. Board `active` includes waiting
films, so the Hub must not call that count the number actually running. The
OpenClaw guide currently reads only legacy `monitors`; the root guide is obsolete.

Accepted behavior:

- Preserve `counts`, `current`, `currents`, `completed_at`, monitors, event shape,
  SQLite schema and ack/outbox ordering. Old/current-only summaries remain readable.
- Add a bounded projection of existing unfinished board rows: film identity/name,
  group, stage, stage status, waiting/attention note, assigned machine, queue
  position and route liveness where available. No paths, tokens, action controls,
  covers, full console logs or execution parameters are copied.
- Expose at most 32 unfinished rows, with `films_total` and `films_more`; retain
  board order (not a claim of one global scheduling order). Per-machine route
  positions remain zero-based in JSON and display one-based. Running details use
  existing `currents`, increased to at most 16 machines, with `currents_total` and
  `currents_more`. Each machine's running work still wins over prefetch.
- Add `dispatch` (`state`, `paused`, `reason`), `sampled_at` (UTC diagnostic) and
  `sample_age_seconds` (source-measured age). Bound newly copied text to 400 chars.
  Do not infer actual execution from counts.active, assigned routes or dispatch
  idle; keep row status and current evidence distinct.
- Hub Markdown identifies the source/controller using existing outer machine and
  version fields. It shows scheduling state, unfinished rows and current task
  progress, including omissions. Label active counts as queued/in-progress rather
  than a running total. Missing/null/invalid workflow is unavailable, not zero.
- Source observer stale/error/stop continues returning null; source age uses
  monotonic and wall elapsed time so sleep cannot keep an old sample fresh. Hub
  unreachable/stale status suppresses live media claims. Source timestamps are
  diagnostic; age checks use source duration plus existing Hub sample age, never
  assume machine clocks match. Legacy missing freshness fields are explicitly
  described as unavailable rather than invented.
- The current guide includes both workflow and legacy monitors after the existing
  Hub freshness guard, and the root guide points to it. Explain correct configured
  Agent port, Bearer, read-only Hub access and separate controller/Hub upgrades.

## Approach and execution surface

| Files | Allowed change |
| --- | --- |
| `taskpaw_v3/agent/server/workflow_events.py` | Summary-only projection and source sample freshness; no event or scheduling semantic changes |
| `taskpaw_v3/hub/server/status_md.py` | Defensive compatible rendering of new and old summaries |
| `taskpaw_v3/tests/test_workflow_events.py`, `test_workflow_observer_remote.py`, `test_status_md.py`, new focused integration tests | Behavioral RED→GREEN and persistence/read compatibility |
| `docs/guides/openclaw-integration.md`, `OPENCLAW_INTEGRATION.md` | Correct read contract and entry point |

Poller, store, board, scheduler, launcher, GUI and service management are read-only
dependencies unless a demonstrated in-scope causal defect requires a plan revision.
No new dependencies, tables, worker threads, network calls or configuration knobs.
No application version allocation. Mac cannot execute, approve, archive or clean.
The existing Hub remains the only Agent `/events` consumer; no progress events
are added. Event-body crash persistence (#234) remains unchanged.

Generated outputs are test logs from `uv run pytest`, lock/Ruff/format/mypy and
frontend lint/test/build, retained as local run evidence; test databases and fake
Agent/OpenClaw peers are temporary. No installed application or service is modified.

## Validation and risks

RED tests first: one local plus five remote currents, prefetch exclusion, waiting
film assignment/position/note and paused dispatcher; 17+ machines and 33+ rows
show explicit omissions; no finished rows in preview; no control/secret fields.
Then source expiry/error/stop/restart, clock rollback/sleep, independent snapshots,
empty and malformed summaries, old summaries/current-only fallback, multiline
text sanitization and disabled/unreachable Hub records. Integration passes the
actual observer summary through a controlled Agent, Hub poller and temporary
SQLite into Markdown and a read-only database consumer, with OpenClaw disabled;
verify no duplicate events/ack changes from the added snapshot fields.

Run focused regressions then the repository's complete Python/UI suites, lock,
Ruff/format, mypy and frontend build. Internal review precedes the operator's
single Kimi K3 external gate; no automatic merge/deployment.

Main source is 3.11.1; independent installed builds need not match versions.
The new queue fields require updating the controller Agent and Hub renderer when
deployed; existing summaries continue working during mixed-version operation.
Readonly LAN evidence shows blackgoldpig `/ping` at configured port 5678 reports
3.11.1; 5680 was unreachable and protected status returned 401 without credentials.
The destination Mac's actual configuration and OpenClaw reader are unverified.
Therefore this change fixes demonstrated summary/read-contract gaps, not a claim
that the field issue has only one cause. Remote worker heartbeat authority and
existing queue durability are reused, not redesigned.
