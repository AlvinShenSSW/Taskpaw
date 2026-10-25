# Agent current task layout (#322)

## Scope and baseline

Implement issue #322 on main `ae193cb`. The operator requested AFK delivery and
a patch increment per issue, overriding the issue's original no-bump constraint:
this change is V3 3.10.3. Default merge policy is leave-open; no deployment.

Read-only checks of the running controller and all four enrolled remote Agents
reported 3.10.2, with fresh heartbeats and reconciliation complete. A reused
version cannot establish the exact repair-build UI bundle; no version mismatch
was observed. The source already shares `StatusBadge`, `StatusProgress` and
`RestoreLivePanel` with the controller. The remaining gap is layout and detail:
connection, equally weighted film rows, current computation and transfers occupy
four separate cards. The operator must connect these themselves.

## Presentation

Use the existing Agent console design tokens and page overrides. The UIUX search
supports the existing compact dark operations design; retain project typography
and components rather than applying its generic landing-page recommendations.
The preview is `design-system/taskpaw-v3/preview/agent-status-322.html`.

1. A compact connection strip preserves pause/resume, heartbeat, reconciliation,
   controller address and credential information. Connection failures, paused
   dispatch/claiming and rejected-result notices stay explicit.
   Controller connectivity is separate from the local attempt phase: a reconnect
   must not hide or relabel still-running local computation as stopped.
   Failed pause/resume requests show an error and retain server-reported state.
2. A primary current-film card owns the film name, truthful shared phase badge,
   download/restore/return step line, current-phase progress, elapsed time and
   matching active transfer. Prefer `current.film_id` over queue position, then
   position zero when downloading/waiting before computation starts. If the
   current attempt has no matching film envelope, retain its ID and information
   rather than attributing it to a queued film.
   Match a transfer by both film identity and phase direction (fetching/down,
   uploading/up); never show leftover transfer figures during finishing/settling.
3. Running restoration embeds the controller's existing detailed panel. Legacy
   ASR remains visible with its own phase, progress and elapsed time. Upload,
   finishing and settling suppress stale computation details and percentages.
   Finishing means awaiting controller acknowledgement, never archive success.
   A matching current attempt's upload/finishing/settling phase takes precedence
   even when its film envelope still says staged/fetching/running. Guard partial
   older payloads without a step or timestamps; show identity and known phase
   with unavailable details. Queue position alone never proves computation.
4. Prefetch/queued films are compact secondary rows with their own transfer
   details; independent prefetch must not replace the current-film focus.
5. Staging capacity and GPU hold remain a compact resource summary.

Use the existing DTO and poll interval. Missing/non-finite elapsed, transfer
counts or rates display unavailable; they are never fabricated zero/NaN. No
attempt to infer whole-film completion from removal of a worker queue entry.
No new controller actions, backend mutations, ports or runtime dependencies.

## Verification and adversarial cases

Write failing UI regressions before implementation for primary/secondary
hierarchy, current-envelope mismatch, missing current/older payload, download,
staged/queued, restoration, legacy ASR, upload, finishing, settling and unknown
metrics. Assert upload/stop wins over stale LTX data; paused/reconnecting and
rejected-result states remain visible; pause remains the sole workflow control.
Retain existing shared component and Agent-role regressions. Check preview and
actual fixture UI at narrow and desktop sizes. Synthetic previews are distinct
from live remote Agent verification, which needs operator deployment.

Run frontend lint/tests/build; version consistency; full canonical Python suite,
lock check, Ruff and Mypy; CI across supported hosts. Bump all six V3 version
sources together, keeping V2 frozen. Perform constitution/CTO self-review then
Claude outer gate and Kimi final gate on the committed branch. Report any
unavailable reviewer explicitly.

## Design debate

Round one found three gaps: preview conflated controller disconnection with local
task phase; a stale film envelope could outrank current attempt settlement;
partial older current payloads could reach detailed components without a step.
The design now separates connection/phase, gives matching attempt terminal
phases precedence, and requires safe partial-payload rendering. Add targeted
regressions for all three. Round two verified those changes and the film/direction
transfer match plus pause-failure feedback. No remaining design blockers; proceed
to failing regressions before product implementation.

## Implementation evidence

The first targeted run failed all ten new regressions while the 27 existing/shared
tests passed. After implementation all 38 targeted tests passed. The complete UI
suite passed all 1,081 tests in 42 files, including the primary downloading film
before a processing attempt exists. ESLint and the
production TypeScript/Vite build passed, retaining the existing two Fast Refresh
warnings and large-bundle advisory. Native browser fixture verification is
recorded separately from the earlier read-only live-version probes. The actual
App/AgentConsole/WorkerMonitor fixture passed 69 checks across 11 states and
375/768/1024/1440 widths, plus 16 Hub/browser-switcher header checks. No horizontal
overflow or JavaScript errors remained. Fixtures intercepted all product API calls.

Full Python validation found one new retired-label regression (fixed and retested)
and 17 existing native credential failures caused by the host's system Temp ACL.
The unchanged baseline reproduced the latter. A newly created private test base
outside Temp passed the production guard, and all 17 cases then passed without
changing security code or existing ACLs. Full-run and focused closure evidence are
kept distinct in `docs/afk-ledger.md`; clean CI remains the final full-suite gate.

The native 375 px fixture exposed an existing shared application-header overflow:
the API badge compressed into separate words and the version label escaped the
viewport while the worker panels fit. A small toolbar wrapping fix preserves the
brand, badge and version widths. This shared CSS also affects the Hub/controller
header and browser role switcher; all role controls remain present. Existing App,
control-credential and connection regressions plus native narrow/desktop checks
cover the repair.
