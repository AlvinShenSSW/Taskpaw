# #210 — Hub task views for Jasna / AV 翻译; version 3.9.7

Date: 2026-09-30. Planner handoff, design v2. Run: `2026-09-30-issue-210`.
Target: `/Users/alvinshen/Documents/Workspace/Taskpaw-issue-210`, branch
`issue-210-hub-task-adapt`; inspected HEAD and local `origin/main` both
`30d24c2e4215905586152ce494995a6be67aa1f1`. All source citations below refer to that
revision, unless explicitly marked otherwise. Paths are repository-relative.
Proposed APIs, names and behaviors below are implementation decisions, not claims
that they already exist.

## Revision log

- **v2 — DR-1 (P1), resolved:** Independently confirmed the header-only offline
  rule in `design-system/taskpaw-v3/pages/hub-dashboard.md:49` and online-only
  monitor rendering in `taskpaw_v3/ui/src/views/HubDashboard.tsx:223–230,265–288`;
  disabled servers are forced offline by `taskpaw_v3/hub/server/app.py:216–224`.
  Removed the offline/disabled compact-film exception. Per the driver's issue
  clarification, fallback plus a short note applies to failed on-demand reads
  on online cards; offline/disabled machines remain header-only with fetching
  components unmounted and no film requests. Recorded A1/A4 as driver-resolved.

## Spec review

Bring the Hub fleet's Jasna and AV 翻译 observations up to the agent console's
stepper and film-list experience, make mixed agent/Hub versions visible, and stop
structured or null metrics leaking into generic tiles. This is read-only
observation across the existing LAN trust boundary.

Requirement source: issue #210, supplied fallback
`/Users/alvinshen/Documents/Workspace/Taskpaw/.afk/runs/2026-09-30-issue-210/codex/issue-210.md:40–70`.
`gh issue view 210 --repo AlvinShenSSW/Taskpaw`, its `--comments` variant, and
`gh pr list --repo AlvinShenSSW/Taskpaw --state open --limit 20` all failed with
`error connecting to api.github.com`. The snapshot includes the title/body but
does not establish current labels, comments or open-PR state. No online claim is
inferred from those failures. For v2, the driver confirms issue #210 has 0
comments, labels `bug`/`enhancement`/`v3`, and 0 open PRs as of 2026-09-30 (A1).
The supplied planner contract at
`/Users/alvinshen/.claude/plugins/cache/afk/afk-skills/1.3.1/skills/afk-spec-planner/SKILL.md`
and its environment/output/continuity references were read. This is a child
planning stage, not a claim or resume of the driver's run directory.

Repository findings and corrections to the issue audit:

| Finding | Inspected evidence | Consequence |
|---|---|---|
| Generic metrics stringify every unknown value, but only suppress pipeline-owned keys after a successful parse. | `taskpaw_v3/ui/src/components/MonitorMetrics.tsx:96–112`; `taskpaw_v3/ui/src/components/pipelineProgress.helpers.ts:93–121,145–174` | Guard scalar types independently of pipeline success; always reserve `steps`, `films`, `films_more`. |
| The Hub already uses the shared stepper, through MonitorMetrics, but passes no task name. | `taskpaw_v3/ui/src/views/HubDashboard.tsx:268–283`; `taskpaw_v3/ui/src/components/MonitorMetrics.tsx:118`; `taskpaw_v3/ui/src/components/PipelineProgress.tsx:384–416` | Reuse that renderer; add actual Hub integration tests and a Hub film source. |
| Current list components call the agent control API directly; their keys contain only task name and cursor. | `taskpaw_v3/ui/src/components/PagedFilmList.tsx:10–58`; `taskpaw_v3/ui/src/components/RunFilmsCard.tsx:70–115`; `taskpaw_v3/ui/src/api.ts:226–229` | Thread an explicit source through components and isolate Hub caches by server id. |
| Both control endpoints already validate and clamp requests and inject providers; they do not need new tracker logic. | `taskpaw_v3/agent/server/app.py:96–131,153–182`; `taskpaw_v3/agent/server/launcher.py:218–226`; `taskpaw_v3/monitors/supervisor.py:211–237` | Expose only those reads on the network app and reuse the same supervisor providers. |
| `/ping` includes version; neither network default status nor the production status provider does. | `taskpaw_v3/agent/server/app.py:63–85`; `taskpaw_v3/agent/server/launcher.py:190–209` | Add version at the network status boundary, also to the shared production provider. No extra `/ping` polling. |
| V3 network/control/Hub defaults are 5680/5681/5690, not the issue audit's V2 `:5678`. | `taskpaw_v3/ui/src/api.ts:166–170`; `docs/constitution.md:49–50` | Use registered agent `ip` and `port`, never a hard-coded or control port. |
| The Hub has one effective polling token, not a per-server token column. | `taskpaw_v3/hub/server/app.py:81–84`; `taskpaw_v3/hub/server/poller.py:78–83,227–229`; `taskpaw_v3/hub/server/store.py:312–319` | Reuse the live SQLite/config token resolver; no credential schema change. |
| Offline snapshots are retained by the backend, but the UI hides all offline monitors. | `taskpaw_v3/hub/server/poller.py:398–405`; `taskpaw_v3/hub/server/app.py:212–225`; `taskpaw_v3/ui/src/views/HubDashboard.tsx:223–230,265–288` | Preserve header-only offline/disabled rendering; compact fallback applies only to failed film reads on online cards. |
| Single-film compact lists are deliberately hidden today. | `taskpaw_v3/ui/src/components/FilmList.tsx:59–65`; `taskpaw_v3/ui/src/components/PagedFilmList.tsx:60` | Add an opt-in single-row display for Hub fallbacks, which may have no live header. Keep normal agent behavior. |
| The selector displays raw type ids; schema localization is for config fields, not type names. | `taskpaw_v3/ui/src/components/MonitorSelector.tsx:25–47`; `taskpaw_v3/ui/src/schemaI18n.ts:262–302` | Add short type-name i18n keys; do not import form schemas or change the selector. |
| `status.md` extracts only monitors from status and already renders the target types. | `taskpaw_v3/hub/server/status_md.py:164–170,253–288,317–337` | Leave its production file untouched and pin full output bytes with/without version. |

Style follows the concrete contracts and regression boundaries of #189/#198/#200
(`docs/specs/2026-09-25-189-progress-redesign-design.md:20–72`,
`docs/specs/2026-09-26-198-film-list-paging-design.md:62–126`,
`docs/specs/2026-09-26-200-jasna-run-films-design.md:137–194`). Their historical
“Hub unchanged” limits are superseded only by #210's explicitly requested views.

## Acceptance criteria

The eight issue acceptance bullets map one-to-one below; scope bullet 6 (zh/en)
is additionally pinned by AC9. Test identifiers are specified in Test plan.

- [ ] **AC1 — safe tiles (issue acceptance 1):** Both task types on actual Hub
  cards, and the shared agent renderer, never stringify arrays, objects, null,
  undefined, NaN or infinities into generic tiles. Unknown strings (including
  empty strings), finite numbers (including zero) and booleans retain scalar
  behavior. `steps`, `films`, `films_more` never become tiles, even if malformed
  scalar values or every step is rejected. Tests T1/T5.
- [ ] **AC2 — stepper parity (bullet 2):** Valid Jasna restore/asr/translate and
  avsubs asr/translate metrics render the existing stepper on the Hub, with stage
  labels, active progress and model. Gauges and queue rendering retain their
  meanings; a rejected pipeline does not fabricate steps. Tests T1/T5.
- [ ] **AC3 — versions (bullet 3):** Every successful new-agent network `/status`
  includes `version = taskpaw_v3.__version__`, with and without an injected
  provider. Hub headers display a present nonempty string as `v<version>`.
  A labelled update-Hub warning appears iff two valid versions compare agent >
  `__APP_VERSION__`. Equal, older, missing and malformed inputs never warn.
  Prerelease/build precedence and multi-digit components are covered. Tests T2/T3/T6.
- [ ] **AC4 — task identity (bullet 4):** Each typed Hub monitor row shows its
  ServiceIcon and localized short type name, including Jasna and AV 翻译. Unknown
  string types retain their raw id with the generic icon; missing/invalid ids
  show neither invented type nor broken label. Tests T5/T9.
- [ ] **AC5 — network reads and Hub proxy (bullet 5):** Two GET-only network
  routes mirror the control reads' payloads, query validation and clamps, with
  the `/status` Bearer posture. Missing/wrong credentials return the existing
  401 envelope and cannot call providers. Hub routes authenticate the caller,
  look up a registered server, use the effective stored polling token, and
  return the upstream payload or the documented error. Unknown, disabled,
  offline and old agents have explicit outcomes; timeout/transport/auth/bad-body
  failures do not become 500s. Polling never requests film lists. Tests T2/T3/T4.
- [ ] **AC6 — full lists and fallback (bullet 6):** Jasna AV-on has 本轮影片 with
  done/open/all and paging; avsubs has 影片 with paging/focus-following, including
  an extras-only `queue_pre_done` snapshot without steps. Reuse existing list
  components, size 10, keepPreviousData and cursor recovery. Failed on-demand
  reads on online cards (including 503 agent_offline races, 404, 409, 502 and
  504) show compact fallback plus a short note, or retain a prior good page with
  a stale note under D4's transient-error rules. No snapshot rows yields an
  honest unavailable note, not a fabricated empty-success result. Offline and
  disabled machines remain header-only; fetching components unmount and issue
  no film requests. Two servers with identically named tasks cannot share data
  or cleanup. Tests T5/T7/T8.
- [ ] **AC7 — compatibility bytes (bullet 7):** Existing status.md fixtures keep
  their exact bytes, including when status gains a top-level version. Event
  payloads/ids/acks, status metric shapes and V2 stay unchanged. Tests T3/T4/T10.
- [ ] **AC8 — checks/version (bullet 8):** Version 3.9.7 in all six V3 version
  files, Chinese CHANGELOG entry, Hub design-system note. Required Python/UI
  checks pass. No runtime dependency is added. Tests T11 and listed commands.
- [ ] **AC9 — localization/accessibility (scope 6):** New labels and all fallback
  states have zh/en text; warning and stale state are never color-only. Existing
  list filters/pagers remain keyboard-operable; new header content wraps without
  horizontal overflow at 375 px. Tests T5/T9 and manual layout check.

## Frozen issue contract

**Product boundary:** AC1–AC9 only. Allowed visible changes are safe metric tiles
on both shared consumers, Hub version/skew and type labels, Hub film lists and
their loading/stale/unavailable feedback on online cards.
No changes to task execution, queue accounting, translation, scheduling or
recorded film facts. Reuse the already side-effect-free readers
(`taskpaw_v3/monitors/subs/progress.py:522–551,633–642`; read-boundary tests
`taskpaw_v3/tests/test_avsubs.py:3372–3387` and
`taskpaw_v3/tests/test_jasna_subs.py:3569–3587`).

**Engineering invariants:** constitution §2/§3/§5 remain blocking
(`docs/constitution.md:25–52,63–69`). New network routes expose only GET reads;
the control app is not mounted, forwarded or opened to the LAN. Existing
`/status` gains only a top-level version; existing `/events`, monitor metrics,
status.md and OpenClaw semantics are preserved. No film data is added to
poll-loop requests, persisted snapshots or event queues. No new thread,
background prefetch, database table or setting. Secrets never appear in UI,
error bodies, query strings or logs. No V2 edits.

**Smallest causal boundary:** the two agent app factories and launcher wiring;
Hub request handlers plus a dedicated read-only HTTP helper; the shared metrics,
pipeline and film components; Hub presentation, API client, i18n and pure
helpers; focused tests and the six-file release metadata. The poller, store,
trackers, plugins, schemaI18n and status_md remain read-only dependencies.

The earlier architecture's literal three-route network list
(`docs/specs/2026-06-27-taskpaw-v3-design.md:118–123`) is extended by this issue's
two authorized observation routes; its loopback-control and LAN-auth boundaries
are not relaxed. The offline-header-only design rule
(`design-system/taskpaw-v3/pages/hub-dashboard.md:43–54`) remains unchanged,
including for disabled machines. Repository evidence may correct this
contract; review preferences do not expand it.

## Assumptions

| ID | Assumption / unverified external claim | Risk and handling |
|---|---|---|
| A1 | Resolved by driver: issue #210 has 0 comments, labels `bug`/`enhancement`/`v3`, and 0 open PRs as of 2026-09-30. | Driver-supplied verification resolves the initial failed GitHub reads; the supplied issue body remains the operative request. |
| A2 | The reported installed Hub is old, or its agent sends unknown step states. Neither deployment was inspected. | Root cause of that particular machine remains unproven. The fixes/test matrix cover both cases; no promise to patch an already-installed old binary. |
| A3 | Resolved by driver (DR-1): “too old or offline” fallback means an online Hub card whose on-demand film-list read fails, including a proxy 503 agent_offline race, 404, 409, 502 or 504. | Preserve D4 failure feedback on online cards. Offline/disabled machines retain header-only rendering regardless of retained snapshots; fetching components unmount and issue no film requests. No design-system exception. |
| A4 | Resolved by driver: no `v3.9.7` tag on origin. | Local HEAD is 3.9.6 (`taskpaw_v3/__init__.py:9`). Use 3.9.7 for this plan, not an unsolicited minor-version feature release. |
| A5 | LAN film-page reads fit the existing 5-second socket timeout and the proposed 1 MiB response cap. No production fleet latency/size measurements were run. | Slow or oversized responses use labelled fallback; 50-row bounds and short existing row fields make this a conservative chosen cap. Socket timeout is not a claimed total wall-clock deadline against a continuously trickling peer. |
| A6 | Proposed urllib redirect suppression and request closing, FastAPI route validation, React Query source-key isolation and npm/uv/build commands will behave as specified in the target environment. No runtime experiment was performed in this plan-only stage. | Treat these as implementation obligations verified by tests/build, not existing successful results. Mocked request tests plus optional loopback-only HTTP integration should prove transport details before signoff. |
| A7 | On transient failures after a good response, retaining that page with a stale note meets the requested agent parity; definite unavailable states use compact fallback even if a previous full page exists. | Removes ambiguity between “keep last-good” and compact fallback after a failed read on an online card. Exact precedence is frozen in the state table below. |

No unresolved product question blocks this plan. A1/A3/A4 are resolved by the
driver; A2/A5/A6/A7 remain reviewable assumptions, not claims of additional
operator approval.

## Approach

1. Add scalar-only generic tiles and reserve the three structural keys.
2. Stamp agent status and expose the existing film readers under read-only
   network paths. Inject both providers from the existing supervisor.
3. Add Hub GET proxy handlers, separated from Poller. Resolve authentication,
   availability and agent address at request time, then perform one bounded read.
4. Extend the existing list components with an explicit optional data source;
   preserve local defaults and isolate every Hub query by server and task.
5. Connect Hub monitor identity/version rendering and list selection; document
   offline and failure states; add regression tests before release metadata.

This reuses the existing tracker and UI instead of copying a second stepper or
list. Uncapping `/status` or putting film reads in Poller would change the
per-cycle cost/storage boundary (`taskpaw_v3/hub/server/poller.py:378–408`). A
browser-to-agent request would bypass the Hub's token owner and target the
network app, which is intentionally separate from the UI control API
(`taskpaw_v3/ui/src/api.ts:166–188`; `taskpaw_v3/agent/server/app.py:108–115`).
Do not infer endpoint support from the reported version: attempt the route on
demand and handle 404; older agents may omit version entirely.

## Files to change

This table authorizes a future implementation surface, not writes in this
planning turn. Only this design document is written by the planner.

| Path | Change type | Reason / existing anchor |
|---|---|---|
| `taskpaw_v3/agent/server/app.py` | Modify | Add status version, share film-route registration/validation between control and network, scoped auth; existing boundaries at `:53–131,153–182`. |
| `taskpaw_v3/agent/server/launcher.py` | Modify | Stamp production status and wire network providers alongside control providers (`:190–226`). |
| `taskpaw_v3/hub/server/app.py` | Modify | Add two authenticated proxy handlers, scoped validation/error conversion (`:187–252`). |
| `taskpaw_v3/hub/server/film_proxy.py` | New | One GET transport helper and typed sanitized errors; avoids changing Poller (`poller.py:227–268,356–408`). |
| `taskpaw_v3/ui/src/api.ts` | Modify | Optional AgentStatus.version; Hub film methods and typed errors using Hub config (`:35–40,172–188,226–239`). |
| `taskpaw_v3/ui/src/views/HubDashboard.tsx` | Modify | Header version, typed monitor labels, Hub source; preserve offline/disabled header-only rendering (`:223–288`). |
| `taskpaw_v3/ui/src/views/hubDashboard.helpers.ts` | New | Total semver comparison and film-kind selection, kept out of component exports. |
| `taskpaw_v3/ui/src/components/MonitorMetrics.tsx` | Modify | Scalar guard and source/type plumbing; independent fallback when no pipeline (`:64–118,164–165`). |
| `taskpaw_v3/ui/src/components/PipelineProgress.tsx` | Modify | Forward source and explicit Hub task type into one existing list slot (`:384–416`). |
| `taskpaw_v3/ui/src/components/pipelineProgress.helpers.ts` | Modify | Extract tolerant compact-row reader independent of valid steps (`:124–163`); export FilmFallback. |
| `taskpaw_v3/ui/src/components/PagedFilmList.tsx` | Modify | Source-aware fetch/keys/cleanup and Hub failure states (`:10–60`). |
| `taskpaw_v3/ui/src/components/RunFilmsCard.tsx` | Modify | Same source contract and Hub states, preserve filter/cursor recovery (`:70–117`). |
| `taskpaw_v3/ui/src/components/filmSource.helpers.ts` | New | Shared FilmSource, key-prefix and known-error-to-i18n mapping; avoid duplicated role decisions. |
| `taskpaw_v3/ui/src/components/FilmListFeedback.tsx` | New | Small reusable labelled notice/compact fallback/empty-snapshot rendering for both list components on online Hub rows. |
| `taskpaw_v3/ui/src/components/FilmList.tsx` | Modify | Optional `showSingle=false`; Hub compact fallback and no-pipeline paged views opt in (`:59–65`). |
| `taskpaw_v3/ui/src/i18n.ts` | Modify | New Hub feedback/version and short task-type strings in both locales (`:190–212,505–527`). |
| `taskpaw_v3/tests/test_agent.py`, `test_launcher.py`, `test_security.py`, `test_hub.py`, `test_status_md.py` | Extend | T2/T3/T4/T10; anchors in Test plan. |
| `taskpaw_v3/tests/test_hub_films.py` | New | Proxy transport and route contract tests T4. |
| `taskpaw_v3/ui/src/test/hubdashboard.test.tsx`, `pipelineprogress.test.tsx`, `pagedfilmlist.test.tsx`, `runfilmscard.test.tsx`, `i18n.test.ts` | Extend | T1/T5/T7/T8/T9; existing suites referenced below. |
| `taskpaw_v3/ui/src/test/hubdashboard.helpers.test.ts` | New | Pure comparator and selection cases T6. |
| `taskpaw_v3/__init__.py`, `taskpaw_v3/src-tauri/tauri.conf.json`, `taskpaw_v3/src-tauri/Cargo.toml`, `taskpaw_v3/src-tauri/Cargo.lock`, `taskpaw_v3/ui/package.json`, `taskpaw_v3/ui/package-lock.json` | Version only | 3.9.7, all six per `taskpaw_v3/tests/test_version.py:57–86`. |
| `CHANGELOG.md` | Document | Chinese 3.9.7 entry, including update-Hub remedy and fallback behavior; style `:7–20`. |
| `design-system/taskpaw-v3/pages/hub-dashboard.md` | Document | Document header version, task type and list presentation; preserve header-only offline/disabled rendering and layout/accessibility `:43–77`. |

`runFilmsCard.helpers.ts` and `pagedFilmList.helpers.ts` remain unchanged response
validators. ServiceIcon and MonitorSelector remain unchanged; use their existing
exports. Do not edit old #189/#198/#200 designs, schemaI18n, production
status_md, Poller, HubStore, plugin/tracker code, pyproject.toml or uv.lock.

## Execution surface

**Planner writes:** exactly
`docs/specs/2026-09-30-210-hub-task-adapt-design.md`. No code, test-generated
files, run-state files, commits, pushes, PRs, deployment or release activity.
The provided run directory and shared `.afk/config.md` are read-only.

**Implementer writes after driver handoff:** only Files to change and disposable
check/build outputs below. Read-only participants in the data path are
Supervisor (`taskpaw_v3/monitors/supervisor.py:211–237`), plugin readers
(`taskpaw_v3/monitors/plugins/jasna.py:3500–3510`,
`taskpaw_v3/monitors/plugins/avsubs.py:1818–1820`), FilmTracker, HubStore,
Poller token/snapshot/address utilities, core auth, and existing UI validators.
Reading or executing these does not authorize edits to them.

| Generator / exact command | Working directory; inputs/configuration | Outputs and expected side effects (future verification, not run here) |
|---|---|---|
| `uv sync --group dev --frozen` | Repo root; `pyproject.toml`, `uv.lock`, compatible Python | `.venv/` and uv cache; downloads if dependencies missing. Must not rewrite the lock. |
| `uv run pytest` | Repo root; tests and dev environment | Terminal results; `.pytest_cache/`, Python `__pycache__/`, test temporary fixtures. No production config/DB. Test discovery is `pyproject.toml:56–58`. |
| `uv lock --check` | Repo root; manifest/lock | Validation output; no intended tracked writes. |
| `uv run ruff check .` / `uv run ruff format --check .` / `uv run mypy` | Repo root; tool config `pyproject.toml:66–85` | Terminal diagnostics, `.ruff_cache/` / `.mypy_cache/`; no formatter writes. |
| `npm ci` | `taskpaw_v3/ui`; existing package and lock | `node_modules/`, npm cache; dependency fetch if missing. No package update authorized. |
| `npm run lint` / `npm test` | `taskpaw_v3/ui`; package scripts, Vite test config | Diagnostics and test result output; temporary tool caches. Commands are defined at `taskpaw_v3/ui/package.json:7–12`. |
| `npm run build` | `taskpaw_v3/ui`; `tsconfig.json`, `vite.config.ts`, source and package version | `dist/` assets/maps, possible compiler build-info cache; no backend launch. `__APP_VERSION__` injection and output directory are `taskpaw_v3/ui/vite.config.ts:9–23`; TS noEmit is `taskpaw_v3/ui/tsconfig.json:12`. |

Use normal file edits for the six version fields; the two lockfiles receive only
their root application version edits, not dependency regeneration. Thus no
`npm version`, `cargo update`, or new generator script is needed. Existing checks
validate synchronization (`taskpaw_v3/tests/test_version.py:28–86`).

Optional manual smoke uses an already-authorized local test Hub/agent with
temporary configuration/database, or mocked browser fixtures. Do not stop,
restart or deploy the user's live tasks to verify this issue. Packaging and
remote CI are driver-owned later stages; CI's broader jobs exist at
`.github/workflows/ci.yml:88–132`, but this plan does not authorize their local
installation/build side effects or publication. No test success is claimed here.

## Key implementation notes

### D1. Agent version and film API

At the network `/status` response boundary return a new shallow dictionary with
the provider's existing fields and authoritative `version: __version__`. Do not
mutate a provider-owned dictionary, and do not trust a provider-supplied version
over the package version. Add the same field to the default response and the
launcher's production `_status_provider`; control status receives it additively
through that provider. Do not require it from older agents. Existing provider
and default branches are `taskpaw_v3/agent/server/app.py:68–85`; production
composition is `taskpaw_v3/agent/server/launcher.py:190–209`.

Extend `create_network_app` with trailing optional keyword provider arguments
`films_provider` and `run_films_provider`, using the same signatures/defaults as
the control app (`taskpaw_v3/agent/server/app.py:103–106`). Wire
`supervisor.film_page` and `supervisor.run_films` in `run_agent`. Both surfaces
read the same retained data; no HTTP request to the loopback control service.

| Route | Query contract | Success / absence |
|---|---|---|
| Agent `GET /monitors/films` | required nonblank `name`; optional integer `page >= 1`, omitted means focus; integer `size=10`, clamp 1–50 | Same object as `/control/monitors/films`; None → 404 `{"detail":"no film list"}`. |
| Agent `GET /monitors/run-films` | required `name` matching `\S`; `filter=done` in done/open/all; integer `page=1 >= 1`; integer `size=10`, clamp 1–50 | Same object as `/control/monitors/run-films`; same 404. |
| Hub `GET /servers/{sid}/monitors/films` | integer server id; same film query as above | Agent JSON object unchanged on success. |
| Hub `GET /servers/{sid}/monitors/run-films` | integer server id; same run query as above | Agent JSON object unchanged on success. |

These paths mirror the existing `/control/monitors/*` resources without implying
control authority, and nest Hub reads under its existing server-id resource
(`taskpaw_v3/hub/server/app.py:338–375`). Keep names in encoded query parameters:
names can contain `/`, spaces, `&`, `#`, `?` and Unicode; never use them as URL
path segments. Existing control rationale: `taskpaw_v3/agent/server/app.py:299–302`.

Create a private `_register_film_routes(app, prefix, films_provider,
run_films_provider, authorize=None)` in agent/server/app.py to register only
these two GETs for prefixes `/control/monitors` and `/monitors`; preserve the
existing parameter declarations and blank-name error distinction. A companion
scoped validation-handler registration should preserve the control logs/default
handlers. For new network film paths only, check authorization before returning
validation errors, so invalid parameters cannot bypass a configured Bearer gate.
Valid requests check the same authorization in the route before any provider
call. This can use the existing `_auth` closure and `_unauthorized()` response;
no global auth refactor or network CORS change. Tests must cover malformed-query
401 as well as normal-query 401. Existing 401 is
`{"error":"unauthorized"}` plus `WWW-Authenticate: Bearer realm="TaskPaw"`
(`taskpaw_v3/agent/server/app.py:44–50`); blank/whitespace token semantics come
from `taskpaw_v3/core/auth.py:14–23`.

Authorized validation errors retain control behavior: missing name or malformed
numeric/filter parameter → 400 `{"detail":"invalid film list parameters"}`;
blank name on `films` → 400 `{"detail":"name must not be blank"}`; run-films
blank-name validation uses the first envelope. Size 0/negative is clamped, not
rejected. Page beyond the final page is passed to the provider to clamp. Unknown
extra query keys remain ignored, matching the existing signatures
(`taskpaw_v3/agent/server/app.py:117–131,153–182`). No POST/PATCH/DELETE variants.

Wire payloads remain exactly the existing reader contracts:

- Film page: `run,total,size,page,pages,focus,focus_page,films`; each row
  `name,steps,status,percent,eta_s,duration_s`, with steps a key/state object.
  Extra rows can have status pre_done/collision and empty steps. UI conversion
  stays in `taskpaw_v3/ui/src/components/pagedFilmList.helpers.ts:3–52`.
- Run page: `run,filter,total,size,page,pages,focus,counts,totals,films`;
  `counts={done,open,all}`; totals translated/partial/has_subs/untranslated/failed/
  restore_failed. Rows contain `name,restore,restored_before,asr,translate,percent,
  outcome,kept_ja,models,duration_s,finished_at`; models are `[label,lines]` pairs.
  Keep validation and unknown-outcome tolerance in
  `taskpaw_v3/ui/src/components/runFilmsCard.helpers.ts:9–72,75–101`.

### D2. Hub request path, errors and transport

Authenticate with Hub `config.api_token` first, before server lookup or network
work; reuse the existing Hub 401 envelope
(`taskpaw_v3/hub/server/app.py:39–45,194–207`). Route-specific validation failures
also check this gate before 400. Other Hub routes keep their current handlers.
Resolve `store.get_server(sid)` then disabled state, then
`service.poller.snapshot_statuses().get(sid)`; absent/false online means offline.
Do not use a caller-supplied URL/port/token. Do not probe offline/disabled agents.
Availability is a request-time observation, not a lock held throughout network
I/O; a later disable cannot recall an already-issued read.

New `film_proxy.py` exports `fetch_agent_film_page(server, resource, params,
headers, timeout=5.0) -> dict` and `FilmProxyError(status_code, code, detail)`.
`resource` is a Literal of `films`/`run-films`, never arbitrary input. Handlers
pass `service.poller._auth_headers()` and `service.poller.http_timeout`, reusing
the live token source rather than freezing it at Hub startup. These inspected
members are `taskpaw_v3/hub/server/poller.py:65–83,227–238`; the config fallback
is `taskpaw_v3/hub/server/app.py:81–84`. Build the destination using imported
`_agent_base_url(server["ip"], server["port"])` (IPv6 handling at
`taskpaw_v3/hub/server/poller.py:30–33`) and `urllib.parse.urlencode` on allowlisted
parameters. Omit page entirely for focus-following. Clamp size before forwarding.

Use a module-local urllib opener with automatic redirects disabled; 3xx is an
upstream failure, never permission to send the token to another location. Make
one GET with the bounded socket timeout, no retries/fallback to `/control/*`.
Read at most 1 MiB + 1 bytes, reject overflow, close success and HTTP-error
responses, decode UTF-8 and parse JSON. Success must be HTTP 200 with a JSON
object; other success statuses, malformed JSON, non-object JSON and non-finite
JSON constants are invalid. Preserve all keys of valid objects; detailed page
validation remains in the existing UI readers. Do not cache/persist the response
or modify poller online/ack state. Catch specific HTTP/URL/timeout/OS/decoding/JSON
errors; unexpected programmer errors must not disappear behind `except: pass`.

All new Hub proxy failures (except its own existing 401) use
`{"error":"<code>","detail":"<fixed English detail>"}`. Details are diagnostics;
the UI localizes by code and never renders raw upstream text.

| Condition (in precedence order) | HTTP / code | Fixed detail |
|---|---|---|
| Authorized invalid sid or film parameters | 400 / `invalid_parameters` | Invalid film list parameters. |
| Server id not registered | 404 / `unknown_server` | Agent is not registered. |
| Registered but disabled | 409 / `agent_disabled` | Agent is disabled. |
| No online snapshot, or connection/DNS/OS transport failure | 503 / `agent_offline` | Agent is offline or unreachable. |
| Agent returns 404 | 404 / `film_list_unavailable` | Film list unavailable; the agent may need an update or the task may have no list. |
| Agent returns 401 or 403 | 502 / `agent_auth_failed` | Agent authentication failed; check the polling token. |
| Socket timeout, including timeout wrapped in URLError | 504 / `agent_timeout` | Agent film request timed out. |
| Malformed/oversized/non-object/non-finite successful body | 502 / `invalid_agent_response` | Agent returned an invalid film list response. |
| Other upstream status, including redirects | 502 / `agent_request_failed` | Agent film request failed. |

An old route's 404 and a new agent's “no film list” 404 cannot reliably be
distinguished from status/version alone; do not claim every 404 proves an old
agent. The combined code/detail deliberately covers both. Never forward
upstream bodies, headers, URLs, exception strings or authorization to the
client. Safe logs may contain server id, resource, fixed code and exception
class only. Invalid input and preflight rejection issue zero upstream requests.

### D3. UI source and lifecycle contract

Add `AgentStatus.version?: string` in api.ts; runtime display still checks the
actual value type. Add `api.hubFilms(serverId,name,page?,size=10)` and
`api.hubRunFilms(serverId,name,filter,page,size=10)`, returning `Promise<unknown>`
through the Hub config/auth path. A private Hub-film GET helper parses only the
known error envelope into exported `FilmRequestError` with `status` and `code`;
unknown/malformed failures use `agent_request_failed`. Do not change all REST
error behavior or use a regex against today's error strings
(`taskpaw_v3/ui/src/api.ts:182–188`).

New `filmSource.helpers.ts` defines
`FilmSource = { kind: "agent" } | { kind: "hub"; serverId: number }` and
`filmQueryPrefix(resource, name, source)`. Both list components accept
`{ name: string; fallback?: FilmFallback; source?: FilmSource }`, defaulting to
agent. `FilmFallback` is `Pick<Pipeline,"film"|"films"|"filmsMore">`, exported
from pipelineProgress.helpers.ts. The helper `readFilmFallback(metrics)` reuses
the existing tolerant row reader independently of steps; readPipeline delegates
its row reading to it, preserving accepted step semantics. Unknown row states
are ignored as now; valid row names still display without fabricated states
(`taskpaw_v3/ui/src/components/pipelineProgress.helpers.ts:124–163`).
PagedFilmList additionally accepts `showSingle?: boolean` (default false),
forwarded to FilmList and used to bypass its own total < 2 return only when
total is 1. MonitorMetrics passes true only for a Hub source without a valid
pipeline. RunFilmsCard needs no such prop because it already renders individual
rows (`taskpaw_v3/ui/src/components/RunFilmsCard.tsx:135–142`).

`MonitorMetrics` adds `filmSource?: FilmSource` and `taskType?: string` to its
existing metrics/taskName props. `PipelineProgress` adds the same two optional
props. Agent call sites need no change. Hub passes taskName, taskType and
`{kind:"hub",serverId:s.id}` only for eligible tasks; never enable an implicit
agent source on a Hub monitor. No taskName remains a non-fetching compact path.

| Component | Local key (unchanged) | Hub key |
|---|---|---|
| PagedFilmList | `["films",name,page]` | `["hubFilms",serverId,name,page]` |
| RunFilmsCard | `["runFilms",name,filter,page]` | `["hubRunFilms",serverId,name,filter,page]` |

Every removeQueries/setQueryData call uses the same full source/task prefix,
including cleanup and recovery seeds; do not update only queryKey. Memoize the
prefix using primitive identity, not the freshly allocated source object. Use a
component key serialized from `[source.kind,serverId-or-null,name,taskType]` so
local lastGood/cursors reset across identity changes. Existing cleanup/recovery
sites are `taskpaw_v3/ui/src/components/PagedFilmList.tsx:24–54` and
`taskpaw_v3/ui/src/components/RunFilmsCard.tsx:87–111`.

Retain 5-second refetch while mounted, keepPreviousData, gcTime=0, returned-page
normalization, focus-following/reset and run/filter reset rules. Set retry=false
for Hub film queries so definite unavailable results produce immediate feedback
without bursts; next mounted interval can recover. Preserve local retry policy.
Unmount on departure from Fleet, removal or offline/disabled transition; no
background Hub film queries remain. Offline/disabled rendering stays header-only,
with no monitor list, compact fallback or film query, even with a retained
snapshot. Reconnect mounts fresh defaults (Jasna done/page 1; avsubs following). A pending old response cannot populate a different task/server.

Retain a separate per-mounted-task failure reason for Hub feedback. A recovery
`setQueryData` seed is not a successful HTTP read: it must not clear the stale
notice or the definite-unavailable compact mode. Clear that reason only after
a freshly fetched, validated page succeeds, or when the source/task unmounts.
Update it from the query function's actual success/failure path (with current
request identity guarding late completions), not merely from query.data being
present. Existing lastGood/cursor effects still own cursor recovery. Test this
with the recovery fetch deliberately left pending after a 404 and after a 504.

### D4. Mount selection and safe fallback

In `hubDashboard.helpers.ts`, define `hubFilmKind(typeId,metrics)`:

- Exact `typeId === "avsubs"` → avsubs, including no-steps/extras-only metrics.
- Exact `typeId === "jasna"` → jasna only with AV evidence: an array-valued
  `steps` or `films`, or finite `subs_total`/`queue_restored`. These counters are
  emitted inside AV-on gates (`taskpaw_v3/monitors/plugins/jasna.py:3452–3479`).
  This permits malformed-step fallback and empty tracked runs, without assuming
  every Jasna task enables translation.
- Other/missing type → no Hub endpoint fetch. Keep any parseable compact
  pipeline; do not guess the service from its monitor name.

Within a valid pipeline, an explicit eligible Hub task type chooses the list;
do not let rejection of a future `restore` state misclassify a typed Jasna task
as avsubs. Existing agent/no-type selection keeps its restore-step inference
(`taskpaw_v3/ui/src/components/PipelineProgress.tsx:413–416`). When no pipeline
parses, eligible Hub tasks still mount their correct list with independently
parsed compact fallback. Empty metrics on a typed avsubs row must not be blocked
by MonitorMetrics' early empty-object return or MachineRow's current nonempty
metrics check (`MonitorMetrics.tsx:76`; `HubDashboard.tsx:281–283`). There is only
one film-list slot per monitor, never both lists or duplicate compact rows.

Always reserve `steps`, `films`, `films_more` in KNOWN. After KNOWN and existing
pipeline suppression, a generic tile is admitted only for string, boolean or
finite number; no coercion of any other value. Do not blanket-hide `model`,
`phase` or all `subs_*` when pipeline parsing fails: legitimate scalar metrics
retain today's fallback. Existing test expecting a raw `steps` tile must change
(`taskpaw_v3/ui/src/test/pipelineprogress.test.tsx:549–553`).

New `FilmListFeedback` props:
`{ fallback?: FilmFallback; noteKeys: readonly string[]; compact?: boolean }`, with compact
default true. It renders localized text, then compact FilmList with showSingle
true when requested; zero rows adds `hub.films.noSnapshot`. With compact=false
it renders only the notes above retained full data. Use an explicit finite union
of the `hub.films.*` keys below in the implementation in place of an unrestricted
string element type. This is a small presentation
component, not a new data-fetching layer. Local agent rendering does not gain
Hub feedback or single-row changes.

Film feedback below applies only while the Fleet card remains online and
enabled. Offline/disabled header-only rendering takes precedence over every
request state, including late results and retained snapshots.

| Hub state | Required rendering and recovery |
|---|---|
| Initial request pending | Compact snapshot if any, `hub.films.loading` (role=status); loading feedback is immediate, not a blank section. |
| Valid nonempty page | Existing component's list/pager/filter; no unavailable note. Preserve existing normal singleton suppression for avsubs when a valid pipeline already shows the film. For no-pipeline Hub avsubs, allow its sole film to display (pass showSingle to FilmList). |
| Valid empty run/page, no snapshot films | RunFilmsCard's existing empty state; avsubs uses `hub.films.empty`. This is an actual successful empty result. |
| Valid total/counts.all zero while status still has films | Existing restart compact fallback, plus `hub.films.resyncing`. Do not reinterpret an empty done filter with counts.all > 0 as unavailable. |
| 404 unavailable/unknown server, 409 disabled, or 503 offline | Compact snapshot plus reason note, even after a good full page. Never silently show a stale full page as current. Keep mounted retry cadence if still online according to fleet status. |
| 401 Hub auth, 502 auth/request/bad response, 504 timeout, JS/network/malformed-page error; no prior good page | Compact snapshot plus mapped reason note. No raw exception/UI JSON. |
| Same transient errors after a good page | Retain lastGood and restore the failed page/filter cursor as today; show reason plus `hub.films.stale`. Controls remain usable after transition failure, polling does not disable them. |
| Fleet status is or changes offline/disabled | Keep only the machine header, regardless of retained monitor/film rows. Fetching components are absent or unmount; no monitor list, compact fallback or HTTP film request. |
| No last-known monitor snapshot | Machine header only; no invented task card or film request. Offline/disabled rendering is identical with or without a retained snapshot. |
| Removed server/task or Fleet tab left | Unmount and clean only that source/task's queries; late results never reappear in another row. |

A proxy error does not change Fleet availability: an online card can receive
503 `agent_offline` or 409 `agent_disabled` before the next Fleet status update,
so it keeps the compact fallback and reason note under the table above. Once
Fleet reports offline/disabled, the existing header-only rule applies, including
on initial render. Retained `s.snapshot` rows do not create an exception
(`taskpaw_v3/ui/src/views/HubDashboard.tsx:223–230,265–288`;
`taskpaw_v3/hub/server/app.py:212–225`).

### D5. Versions, identity and text

Add pure `compareSemver(left: unknown,right: unknown): -1 | 0 | 1 | null` in
`ui/src/views/hubDashboard.helpers.ts`; it serves only the Hub warning, so do
not create a global version library or dependency. Define the accepted grammar
and ordering explicitly: three dot-separated ASCII nonnegative integers with
no leading zeros except zero, optional `-` prerelease dot identifiers and `+`
build dot identifiers; identifiers contain ASCII alphanumeric/hyphen and cannot
be empty; numeric prerelease identifiers have no leading zeros. No implicit `v`
prefix, whitespace trim, two-component or arbitrary suffix coercion. Compare
core numbers numerically, then prerelease (release greater than prerelease;
numeric identifiers below nonnumeric; numeric by value, nonnumeric by ASCII
lexical order; equal prefix shorter sequence lower). Ignore build metadata for
precedence. Use decimal-string length/lexical comparison to avoid unsafe-number
rounding. Invalid input on either side returns null. Tests include the standard
alpha → alpha.1 → alpha.beta → beta → beta.2 → beta.11 → rc.1 → release sequence.
This defines the implementation contract without relying on an unverified
third-party comparator.

Hub header prints `v` plus any nonempty string version (escaped React text,
wrapping). Malformed strings remain visible for diagnostics but never warn;
non-string/empty/missing values show nothing. Warn only when comparison against
the existing build-defined `__APP_VERSION__` returns 1, not string comparison or
Hub backend version (`taskpaw_v3/ui/vite.config.ts:9–15`). Use a wrapping labelled
MUI warning Alert once per machine, not per task, including both versions and
the update-Hub remedy. No raw version is a React key or endpoint capability gate.

Use `ServiceIcon id={typeId}` and a text Chip beside the task name. Its Jasna and
avsubs shapes and generic fallback already exist
(`taskpaw_v3/ui/src/components/ServiceIcon.tsx:9–26,50–58`). Add `monitorType.*`
keys, not `services.*` descriptions (those are long explanatory text at
`taskpaw_v3/ui/src/i18n.ts:177–188`). Known labels:

| Key suffix | zh | en |
|---|---|---|
| jasna | Jasna | Jasna |
| avsubs | AV 翻译 | AV translate |
| lada / comfyui | Lada / ComfyUI | Lada / ComfyUI |
| process | 进程 | Process |
| heartbeat | 心跳 | Heartbeat |
| tcp_check | TCP 检查 | TCP check |
| host_metrics | 主机指标 | Host metrics |
| folder | 文件夹 | Folder |
| custom_cmd | 自定义命令 | Custom command |
| state_file | 状态文件 | State file |
| dev_activity | 开发活动 | Dev activity |

The registered plugin set is `taskpaw_v3/monitors/registry.py:53–65`; the actual
folder id is `taskpaw_v3/monitors/plugins/folder.py:132`. Use defaultValue=raw id
for unknown string types; no new network catalog request. Existing generic icon
coverage for other types is acceptable; no unrelated icon redesign.

| New i18n key | zh | en |
|---|---|---|
| `hub.agentVersion` | Agent v{{version}} | Agent v{{version}} |
| `hub.versionSkew` | Hub v{{hub}} 低于 Agent v{{agent}}，可能无法完整显示新任务类型。请更新 Hub。 | Hub v{{hub}} is older than Agent v{{agent}} and may not fully show new task types. Update the Hub. |
| `hub.films.loading` | 正在读取影片列表… | Loading film list… |
| `hub.films.unavailable` | 无法读取完整影片列表；Agent 可能需要更新，或任务暂无列表。 | Full film list unavailable; the agent may need an update or the task may have no list. |
| `hub.films.offline` | Agent 离线，显示上次状态中的影片。 | Agent offline; showing films from its last status. |
| `hub.films.disabled` | Agent 已禁用，显示上次状态中的影片。 | Agent disabled; showing films from its last status. |
| `hub.films.unknown` | Agent 已移除，暂时显示上次状态中的影片。 | Agent removed; temporarily showing films from its last status. |
| `hub.films.authFailed` | 无法验证 Agent，请检查 Hub 的轮询令牌。 | Agent authentication failed. Check the Hub polling token. |
| `hub.films.hubAuthFailed` | 无法验证 Hub 连接，请检查 Hub API 令牌。 | Hub authentication failed. Check the Hub API token. |
| `hub.films.timeout` | 读取影片列表超时，将自动重试。 | Film list request timed out; retrying automatically. |
| `hub.films.failed` | 暂时无法读取影片列表，将自动重试。 | Film list unavailable for now; retrying automatically. |
| `hub.films.stale` | 显示上次成功读取的列表。 | Showing the last successfully loaded list. |
| `hub.films.resyncing` | 正在同步新一轮影片，暂时显示状态中的影片。 | Syncing the new run; showing films from status for now. |
| `hub.films.noSnapshot` | 上次状态中没有影片明细。 | No film details in the last status. |
| `hub.films.empty` | 本轮没有影片。 | No films this run. |

Header visible version text is `v<version>` with `hub.agentVersion` as its
accessible label. Error mapping is by D2 code: unknown/disabled/offline/unavailable/
auth/timeout to corresponding keys; other errors to failed; Hub HTTP 401 to
hubAuthFailed. Reuse existing runFilms/pipeline pager labels. Keep full-width
rows, wrapping header/detail and existing <sm stacked list behavior; do not
introduce nested machine cards or an expand action. These follow
`design-system/taskpaw-v3/pages/hub-dashboard.md:43–51,70–77` and
`taskpaw_v3/ui/src/components/RunFilmsCard.tsx:55–67,135–150`.

### D6. Compatibility and release

Do not add version rendering or film fetching to status.md/OpenClaw. The status
parser already preserves arbitrary top-level keys
(`taskpaw_v3/hub/server/poller.py:165–176,183–204`), so no poller schema change.
`status.md` reads only monitors (`taskpaw_v3/hub/server/status_md.py:253–264`).
No film proxy request updates last_seen, status_log, online or events/acks.

Bump exactly: Python `__version__` (`taskpaw_v3/__init__.py:9`);
`taskpaw_v3/src-tauri/tauri.conf.json:4`; `taskpaw_v3/src-tauri/Cargo.toml:3`;
only the taskpaw package entry in `taskpaw_v3/src-tauri/Cargo.lock:2956–2957`;
`taskpaw_v3/ui/package.json:4`; package-lock top-level
`taskpaw_v3/ui/package-lock.json:3` and packages[""] at `:9`.
Baseline `git show --stat 30d24c2` records
the previous six-file bump; the enforceable synchronization contract is
`taskpaw_v3/tests/test_version.py:57–86`. Do not change the V2 package version
(`pyproject.toml:1–6`, `taskpaw_v3/__init__.py:3–6`).

## Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| New LAN route exposes control or token | Low with tests | High | Fixed GET-only allowlist; reuse auth; no redirect forwarding or raw errors; negative security tests. |
| Same-name tasks on different machines collide | High without key change | High | Source/server/task prefix for fetch, cleanup, recovery and React identity; concurrent two-machine tests. |
| Fallback accidentally vanishes when steps are rejected or a single film remains | Medium | Medium | Independent compact reader, explicit typed mount selection, Hub-only showSingle and noSnapshot feedback. |
| Rejected future restore state sends Jasna to avsubs endpoint | Medium | Medium | Hub type_id is authoritative for list selection; step parsing remains tolerant. |
| Availability transition displays stale live data | Medium | Medium | Offline/disabled unmount and header-only rendering; online proxy failures retain labelled fallback per D4; preflight rejection never mutates poller state. |
| More simultaneous on-demand requests in a large fleet | Medium | Medium | One page/task every 5 seconds only while mounted, no retry bursts, size cap, timeout and no background prefetch. No scale benchmark is claimed (A5). |
| Control validation or agent-local paging regresses during reuse | Medium | Medium | Shared route registration, preserve exact old signatures/envelopes; retain local query keys and existing recovery tests. |
| Comparator gives false warning on 3.10 or prerelease | Medium without tests | Low | Strict total comparator with explicit malformed/prerelease/build/large-number cases. |
| Branch integration conflicts with newer local main | Known overlap | Medium | Local main is `0da9bb0` by read-only git inspection; its stat includes launcher/test_agent/test_launcher. Driver reconciles before implementation; do not rebase or import unrelated restart work in this planner stage. |
| HTTP library/runtime behavior differs from proposed contract | Unknown until tested | Medium | A6; test redirect, wrapped timeout, close and auth-before-validation explicitly. |

## Out of scope

- Hub task logs, FFmpeg diagnostics, task start/stop/config/edit, remote control,
  new network CORS, agent token management or per-server credential schema.
- V2, scheduling, GPU ownership, film tracker membership/order/outcomes,
  translation, subtitle publishing, run history persistence or queue semantics.
- Uncapped status payloads, film fetching in Poller, `/ping` version probing,
  event protocol changes, status.md/OpenClaw content or time-conversion changes.
- Version-based blocking of task views, automatic updates, deployment, release
  publication, branches/commits/pushes/PRs or modifications to the AFK run folder.
- Changing offline/disabled header-only rendering, all REST error handling, all icons,
  schema localization, selector labels, or a generic frontend data-source framework.

## Test plan

All tests use in-process fixtures, fake transport or temporary data. No real
Jasna/WhisperJAV, GPU, LLM provider or production agent is required. This is a
test specification; none of these checks was executed by the planner.

| ID | Acceptance coverage | Concrete tests / location |
|---|---|---|
| T1 | AC1/AC2 | Extend `ui/src/test/pipelineprogress.test.tsx`: both valid pipelines retain stepper/gauges; null, undefined, nested object, object array, string array, NaN, ±Infinity never tile; unknown scalar string/zero/false still tile; reserved keys stay hidden even as scalar values. Update raw-steps expectation at `:549–553`. Unknown step states/all rejected still preserve readable compact rows on Hub-source rendering. |
| T2 | AC3/AC5 | Extend `taskpaw_v3/tests/test_agent.py`: network status default/provider adds authoritative version without mutating fixture; production launcher provider checked in T3. Parameterize control/network parity using real FilmTracker fixtures at `:225–367`: both resources, names with slash/Unicode/query metacharacters, defaults, page 99 clamp, size -1/0/99, missing/blank name, non-int/fraction/bool-like page/size, page 0/-1, bad filter, no provider/None 404. Add correct/missing/wrong/whitespace configured token cases; unauthorized malformed query calls no provider and leaves events intact. |
| T3 | AC3/AC5/AC7 | Extend launcher injection capture at `taskpaw_v3/tests/test_launcher.py:265–341` to inspect both app factories and invoke their provider. Extend `test_security.py:33–77` for new gated GETs, no CORS, no control paths or mutation methods; existing 401/event tests remain. Extend `test_hub.py:394–431,471–517` with version and nested metrics to prove status pass-through and offline retention without added parsing or wire changes. |
| T4 | AC5/AC7 | New `taskpaw_v3/tests/test_hub_films.py`: successful decoded response equals agent fixture for both routes, including unknown additive keys; exact params and IPv4/IPv6 registered target; omission of page; clamp/filter/400; Hub auth-first (including invalid query), zero requests for unknown/disabled/offline; precise D2 codes for 404, 401/403, timeout and wrapped timeout, transport errors, 3xx, other statuses, invalid UTF-8/JSON/object/non-finite/oversize. Prove response closing, no redirect second request, no upstream body/token in responses/logs, no client token forwarded. Change SQLite polling token between two calls, then test config fallback and empty token. One upstream call/request, no retries. Snapshot/ack/store observations unchanged. Spy on all HTTP destinations during poll_once and `/status`: only existing `/status` and `/events` requests, zero film transport calls; attach to `test_hub.py` poll harness at `:98–148,394–418`. |
| T5 | AC1/AC2/AC4/AC6/AC9 | Extend actual `ui/src/test/hubdashboard.test.tsx` beyond its current four-machine harness (`:12–66`): Jasna and avsubs valid stepper labels/progress/model, icons + localized names in both locales; malformed/no steps never object/null tiles; avsubs no-steps extras-only uses `/servers/{id}/monitors/films`; Jasna including rejected restore state uses run-films; AV-off/other/missing type never fetch. Offline/disabled initial renders and transitions stay header-only with or without last-known film rows; no monitor list, compact fallback, stepper/gauges or film requests. While the card remains online, 404/409/503 and first-read 502/504 show compact + note; no-row failures show the unavailable note. Include singleton fallback and no-row note, no task controls, both list behaviors under actual MachineRow. |
| T6 | AC3 | New `ui/src/test/hubdashboard.helpers.test.ts`: 3.9.7 > 3.9.6, 3.10.0 > 3.9.99, equal/older, missing/non-string/malformed, forbidden prefixes/leading zeros/empty identifiers, full prerelease sequence, build-only equality, numeric identifiers beyond Number.MAX_SAFE_INTEGER. In Hub tests compare against actual __APP_VERSION__, and assert warning text/remedy only for newer, once per machine; malformed string visible but no warning, non-string absent. |
| T7 | AC6 | Extend `ui/src/test/pagedfilmlist.test.tsx`: Hub URLs encode names and send only Hub API auth; 23 films across pages, following/manual/back-to-current, new run/clamp reset, transitions keepPreviousData, last-good plus stale note on 502/504/malformed, failed cursor recovery next click works. Definite 404/503 switches to compact even after success. New no-pipeline singleton/empty states. Preserve local tests at `:250–298`, including local 404-hidden behavior. |
| T8 | AC6 | Extend `ui/src/test/runfilmscard.test.tsx`: Hub done/open/all counts/order/10-row paging, response filter drives UI, new run reset, disabled controls only in transition, transient failure restores cursor and lastGood with note, unavailable compact precedence. Concurrent same-name tasks on server 1/2 plus local source: paging one affects only its URL/key; unmount/cleanup of one preserves other rows; source identity change and delayed old responses do not leak. Repeat source isolation for PagedFilmList. Preserve existing local coverage at `:112–249`. Fake timers prove requests stop off Fleet/offline/disabled/unmounted and resume on reconnect. In the MachineRow harness, offline/disabled transitions remove all film feedback even with cached data or a late response; still-online proxy failures retain D4 feedback and retry cadence. |
| T9 | AC4/AC9 | Extend `ui/src/test/i18n.test.ts` using locale switch pattern `:8–23`: every new key exists in en and zh-CN and has resolved interpolation; no raw key/error leakage; all type labels, warning update remedy, each feedback mapping. Hub integration asserts accessible labels and keyboard filter/pager actions. |
| T10 | AC7 | Extend `taskpaw_v3/tests/test_status_md.py`: for existing Jasna fixture matrix (`:1037–1162`), avsubs (`:1165–1184`), Lada/host and offline/V2-list cases, compare complete `render_status_md(...).encode("utf-8")` with/without top-level version and against the pre-change expected full output, fixed now. Preserve existing expected strings rather than regenerate them from changed code. Keep `test_openclaw_compat.py:303–351` tests unchanged; run them as regression evidence. |
| T11 | AC8 | Existing six-version parity/semver test `taskpaw_v3/tests/test_version.py:57–86`, lock check, all Python checks and UI lint/vitest/typechecked build below. No runtime dependency or V2 diff. |

Retain provider/tracker regression coverage unchanged:
`taskpaw_v3/tests/test_monitors.py:237–297` (unknown/stopped/lock discipline),
`taskpaw_v3/tests/test_subs_progress.py:1541–1574` (legacy key sets),
`taskpaw_v3/tests/test_avsubs.py:3349–3387` (extras-only/reset/no live calls),
`taskpaw_v3/tests/test_jasna_subs.py:3569–3587` (read boundary/AV-off).

Run focused suites during implementation, then once the final changes settle:

1. Repo root: `uv lock --check`, `uv run pytest`, `uv run ruff check .`,
   `uv run ruff format --check .`, `uv run mypy`.
2. `taskpaw_v3/ui`: `npm run lint`, `npm test`, `npm run build`.
3. Review `git diff --check` and the path list; production Poller/status_md/V2
   must have no diff. Version-only files must have no dependency churn.

The configured command set is also recorded read-only at
`/Users/alvinshen/Documents/Workspace/Taskpaw/.afk/config.md:3–6`; source CI
gates are `.github/workflows/ci.yml:26–39,63–86`. Failed/unavailable checks must
be reported as such, never assumed green.

Manual layout smoke after passing tests: use both locales at 375/768/1440 px;
show two same-name tasks on different machines; long version/task/model names;
one newer agent warning; page and filter navigation; offline then reconnect;
404 and transient-error fixtures. Confirm no horizontal scroll, visible focus,
labelled stale/warning states and no task controls. MASTER requests responsive
and focus checks (`design-system/taskpaw-v3/MASTER.md:184–208`); unit jsdom tests
alone do not prove browser layout. No live deployment is necessary.

## Handoff notes

- Deliverable is this file only; no implementation, tests or publication has
  occurred. Initial worktree was clean. Planner has not written the run folder
  or edited the driver's ledger; driver owns evidence retention and next stage.
- Baseline is the supplied issue snapshot plus this Frozen issue contract,
  separate from Git base `30d24c2`. Driver resolved comments/labels/PR state
  (A1) and confirmed no `v3.9.7` tag on origin (A4). Local main has later commit
  `0da9bb0`; read-only `git show --stat` found launcher/test overlap. Do not silently move this branch's base during planning.
- Implement in D1/D2 before D3/D4 so real API fixtures drive UI tests; D5 can
  follow independently. Finish byte regression and all checks before metadata
  signoff. Production poller/status_md edits are a scope warning, not a shortcut.
- Highest review attention: new endpoint auth, credential forwarding/redirects,
  cache cleanup prefixes, typed Jasna list selection on rejected steps, preserved
  offline/disabled header-only rendering, and error precedence after a previous
  successful page.
- A1/A3/A4 are resolved by the driver; A2/A5/A6/A7 remain explicitly open for
  design review. No blocking product question is left unanswered by this plan;
  deployment root cause is unverified.
- Repair-cycle/audit accounting and active driver gate profile are not established
  by this child; unknown consumption is not zero. The shared config was read but
  does not replace run-specific authority. Reviewer independence remains required
  by `docs/constitution.md:71–77`.
- Next step: return this bounded plan to the AFK driver for its design-review /
  implementation handoff. This document grants no commit, push, PR, merge,
  release or deployment permission; those remain outside this planner request.
