# #198 — AV 翻译 「影片」 list: every film, 10 per page; version 3.9.1

Date: 2026-09-26 (design v3, FROZEN — debate rounds 1–3: D198-1…D198-7, N198-1…N198-5 folded in; N198-6/7 deferred/accepted; round 3 CLEAN; wording V3-1/V3-2 applied)
Issue: #198. Owner request: "translate这个任务 history的整个列表我都要能分页看到 一页10条 现在多了后面就看不见了".
Driver: `/afk` — Claude leads (design, debate, orchestration); implementation by Codex gpt-6-astra (effort high);
outer gate Claude; final gate DeepSeek flash. Merge when AFK merge-ready, then release 3.9.1 (operator instruction).

## Spec review

The per-film list under an AV 翻译 (avsubs) task — and a Jasna task with AV 翻译 on — shows at most 12 rows:
`FilmTracker._rows` (`monitors/subs/progress.py`, #189 D4/N4) picks focus + active + waiting + queued + the last 3
finished + pending up to `MAX_ROWS = 12` and folds the rest into `films_more`, which the UI renders as
「还有 N 部」. The tracker itself holds every film of the run (one entry per planned film, no cap); only the read
is capped. Nothing serves the full list.

The capped block lives inside the task's status metrics, which also go to the Hub (`/status` every poll, stored in
the Hub's 24 h `status_log`), `status.md`, and the OpenClaw guide contract ("at most 12 rows"). Uncapping it would
grow every poll and every stored Hub row with the library size — so the full list gets its own local endpoint.

"History" matters (D198-1): an avsubs scan treats a video that already has subtitles as done before the run
(`plan_tree` → `done += 1`), so after every agent restart (every release) the films translated in earlier runs
leave the tracker. Listing only tracked films would hide exactly the history the owner asks for; the avsubs page
therefore also lists those films and the name-collision losers the scan skipped.

## Frozen issue contract

- [ ] AC1 **Tracker page read** (`FilmTracker`, `monitors/subs/progress.py`):
  - `view(live, now)` additionally stores the validated live facts it used (`self._last_live`, under the tracker
    lock). `set_extras(rows)` stores the run's untracked films ONCE per run on the tracker itself (a tuple of
    `(name, status)`, set by the plugin right after planning; a new tracker per Start starts empty) — so a reader
    that holds one tracker can never mix one run's tracked films with another run's extras (N198-2).
    New read `page(page, size) -> dict`, under the tracker lock and WITHOUT `observe()` or any live source (the ASR
    progress parser is not thread-safe; the page has no side effects on stamps):
    - Rows, in order: every tracked film in **plan order** (`_Film.order`) in today's row shape
      (`name, steps, status, percent, eta_s, duration_s`), then the extras in the order stored. An extra row is
      `{"name", "steps": {}, "status": <extra status>, "percent": null, "eta_s": null, "duration_s": null}`.
    - The page window is computed first and rows are built ONLY for it (N198-5: slice-first 0.002 ms vs 9 ms for
      building 20 000 rows); computing statuses for `focus` is the only whole-list work.
    - Live numbers come from `_last_live`; before the first `view()` the page uses empty live facts. Terminal states
      may be newer than the last check (marks are immediate); live parts are as of the last check (A3).
    - Result `{"run", "total", "size", "page", "pages", "focus", "focus_page", "films"}`. `run` = a random token fixed
      per tracker (a Start builds a new tracker → a new token). `focus` = the `_focus` rule's film, `bounded(…, 200)`
      like row names, or `null`; `focus_page = order // size + 1` (tracked films only) or `null`.
    - Totality (`bool` is not an int here): `page` not an int or `< 1` → 1 when explicitly bad; `page is None` → `focus_page`, else 1; beyond the
      last page → the last page; `size` not an int → 10, else clamped to 1–50; `pages = max(1, ceil(total / size))`;
      `total = 0` → `films: []`, `page = pages = 1`. Rows are fresh objects; names bounded to 200 chars.
- [ ] AC2 **Plugins**: `MonitorInstance.film_page(page, size) -> dict | None` — base returns `None`. Each plugin reads
  `self._tracker` ONCE into a local. Never raises (an internal error returns `None` and is logged ONCE per tracker,
  not on every 5 s poll — V3-2).
  - **avsubs**: `plan_tree`'s `TreePlan` additionally carries (as new trailing fields with defaults, so existing
    positional constructions keep working) the relpaths of the videos it counted as already done (`done_names`, in
    scan order) and of the collision losers (`collision_names`) — both taken from the scan's own `relpath` string
    (POSIX `a/b.mp4`, exactly the tracked names' form; NOT `Path.relative_to`, which renders `\` on Windows). Right
    after planning, `start()` calls `tracker.set_extras([(n, "collision") …] + [(n, "pre_done") …])` — also on the
    "nothing to subtitle" path where no film is tracked. `film_page` = `tracker.page(page, size)`. Planning,
    counting and queue semantics are unchanged (only names are retained).
  - **Jasna**: returns its tracker's `page(...)` (no extras — its planner keeps only a count of already-done films;
    W1 of #189) when AV 翻译 is on, else `None`.
- [ ] AC3 **Supervisor**: `Supervisor.film_page(instance_id, page, size) -> dict | None` looks the managed instance up
  under `_lock`, releases the lock, then calls `instance.film_page(...)` (the `reconfigure` pattern); unknown /
  stopped (unregistered) → `None`. The Hub's own supervisor never calls it.
- [ ] AC4 **Control API**: `GET /control/monitors/films?name=<task>&page=<n>&size=<n>` on the loopback **control
  app only** (not the network app; the Hub cannot reach it). Wired through a new `create_control_app(...,
  films_provider=...)` kwarg (the `events_provider` / `status_provider` injection pattern); the launcher passes
  `supervisor.film_page` (the supervisor exists before the control app is created).
  - `name` required, non-blank, query param (names may contain `/`); `page` optional int ≥ 1; `size` optional int,
    default 10, clamped to 1–50.
  - Missing/blank `name`, non-integer `page`/`size`, or `page < 1` → HTTP 400 `{"detail": …}`: `page` is declared
    `Query(ge=1)` and the control app's request-validation handler maps this path's validation errors to 400
    (FastAPI would otherwise answer 422); a whitespace-only `name` is rejected explicitly in the handler body.
  - `None` from the provider (unknown, stopped, no film list) → 404 `{"detail": "no film list"}`. Read-only.
- [ ] AC5 **UI** — the agent console's task detail (`AgentConsole.tsx` → `MonitorMetrics` → `PipelineProgress` →
  the 「影片」 list). `MonitorMetrics`/`PipelineProgress` get an optional `taskName`; only the agent console passes
  it. With a name, a separate `PagedFilmList` component (the only place `useQuery` lives; keyed by the task name so
  switching tasks resets it) renders the list; without one (Hub, existing tests) the list is today's capped one.
  - **Mounting (N198-1):** with a name, `PagedFilmList` mounts where the capped list is today; AND, when no pipeline
    renders (a fully subtitled library tracks no film, so the status has no `steps`) but the metrics are
    avsubs-shaped (`queue_pre_done` is a number), `MonitorMetrics` mounts it on its own under the queue block —
    with no fallback rows, hidden until a page answers with `total ≥ 2`. A 404 keeps it hidden.
  - **Query policy (D198-2, N198-3):** `useQuery(["films", name, page])` → `api.films(name, page, 10)` every 5 s
    while mounted, `placeholderData: keepPreviousData`, `gcTime: 0` (no minutes-old cache entry — e.g. a previous
    run's page — flashes on 回到当前影片 / re-select / run reset). "In flight" = `isPlaceholderData` (a page change
    awaiting its answer) — a background poll never disables the pager. Prev/next compute from the server-clamped
    `data.page`. The component keeps the **last good response** for its (mounted) task: a failed page change or
    background poll keeps showing it with the pager enabled. A response whose body is not a valid page (shape check)
    counts as a failure. When a page CHANGE fails, the requested page is set back to the last good page (or back to
    following, if that is where it came from), so 「上一页」/「下一页」 always act on what is shown and never look
    tappable while doing nothing (V3-1). Fallback to the capped status rows (+ 「还有 N 部」) ONLY while this task has no good page
    yet (first load, or every request so far failed), or when the last good page has `total === 0` while the status
    still has rows (a watchdog restart before its rescan, D198-7).
  - **Paging:** 10 rows per page; 「上一页」 / 「下一页」 (disabled at the ends and while in flight) and
    「第 x / y 页 · 共 N 部」. The list opens on `focus_page` and **follows the focus** (auto-advances) until the user
    pages; after a manual page change it stays, and 「回到当前影片」 (shown only then) returns to following. A new
    `run` resets to following. The paged view shows no 「还有 N 部」.
  - Rows: the focus row is highlighted by `data.focus` (same source as the rows). Page rows are read by a NEW page-row
    reader that accepts `StepState | "pre_done" | "collision"` (today's `readPipeline`/`readRow` stay unchanged, so
    the Hub path is identical — N198-4); extra rows show their label — `pipeline.row.pre_done` 「已有字幕」 /
    "Already had subtitles", `pipeline.row.collision` 「同名冲突，未处理」 / "Name collision, skipped" — and no step
    chips. `total ≤ 10` → no pager; `total < 2` → hidden, as today.
  - Layout: the pager row wraps (`flexWrap`) — no horizontal scroll at 375 px; buttons keyboard-operable with
    visible focus and ≥ 40 px targets (MASTER.md, pages/agent-console.md). zh + en strings.
  - The Hub dashboard keeps today's capped list + 「还有 N 部」 unchanged.
- [ ] AC6 **Unchanged contracts**: the status metrics block (`films` ≤ 12 rows + `films_more`), `status.md`, the
  Hub, the OpenClaw guide and every existing #189 row-cap test stay as they are.
- [ ] AC7 **Docs + version**: 3.9.1 in the six version files; CHANGELOG 3.9.1 (Chinese); a short paging note in
  `design-system/taskpaw-v3/pages/agent-console.md`.
- [ ] AC8 **Tests** (no network, no real exes):
  - Tracker `page`: plan order with a NON-alphabetical plan (proves `order`, not a name sort); extras after tracked
    rows; row parity — for the same films, page rows equal the status view's rows; focus on page ≥ 2 → default
    page = focus page; beyond-last / `page < 1` / bad types (incl. `bool`) / size clamps; empty; extras only (no
    tracked film); `set_extras` per tracker; live numbers from the last view; no stamp changes; fresh objects; new
    `run` per tracker; slice-first (a 20 000-extras page builds only its window); 1 000 films.
  - avsubs: `done_names` / `collision_names` from `plan_tree` with NESTED folders use `/` (and unchanged counts);
    `film_page` lists tracked, then collision, then pre-done rows; a fully subtitled library → `film_page` lists
    every pre-done film while the status has no `steps`; reset at Start; a plugin-level spy proving `film_page`
    never calls the ASR job's `progress()` nor the translator. Jasna: `None` with AV off; paging after a status
    build.
  - Supervisor: unknown/stopped → None; raising instance → None; `_lock` not held during the instance call.
  - API: shape, defaults, clamps, each 400 case (incl. the handler path), 404, `/` in names, not on the network app.
  - UI: paged rows (≤ 10), default focus page + following, manual paging + 回到当前影片, `run` reset, task switch
    shows no stale rows, background poll does not disable the pager, pager disabled at ends/in flight, no
    「还有 N 部」 in the paged view, live refresh updates a status, extra-row labels (not blank), page text zh/en,
    `total ≤ 10` no pager, fallback to capped rows on first load / all-failed / total 0 with status rows, last good
    page kept on a failed page change or poll, and the next click after a failed change requests a new page (V3-1),
    malformed body = failure, avsubs metrics without `steps` + a page of
    pre_done rows → the list shows (404 → hidden), Hub/no-name path unchanged.

## Invariants

- Status payloads, the Hub and OpenClaw see exactly what they see today.
- The page read has no side effects (no `observe`, no stamp changes), never touches a live source, and holds only
  the tracker lock for O(total) work (critic probe: 0.25 ms at 1 000 films, 2.6 ms at 10 000).
- Observation only: no change to scheduling, settlement, GPU lease, publishing, translation or avsubs planning
  counts.

## Assumptions

- A1 **"Every film"** = for avsubs: every tracked film of the run, every video its scan found already subtitled, and
  every name-collision loser — the whole library as the task sees it. For Jasna: its tracked films (its planner
  keeps no names of already-done films; listing them would change the Jasna planner — out of scope; its collision
  losers could be listed cheaply but alone add little — N198-6 deferred).
  History across restarts beyond "what the library already has" is the 日志 page (#196).
- A2 Page size is 10 in the UI (owner). The server accepts 1–50 for tests and future use.
- A3 Live parts of a row (`percent`, `eta_s`, active/waiting) are as of the last status check (the poll interval,
  default 10 s); terminal states may be newer.

- A4 A transient `/control/status` failure replaces the whole task hero with the error alert (existing behaviour),
  which unmounts the list and resets a manual page to following (N198-7, accepted).

## Test plan

Tests first in `tests/test_subs_progress.py`, `tests/test_avsubs.py`, `tests/test_jasna_subs.py`,
`tests/test_monitors.py` (supervisor), `tests/test_agent.py` (API), `ui/src/test/pipelineprogress.test.tsx` and
`ui/src/test/agentconsole.test.tsx`, per AC8. Full `uv run pytest`, ruff, format, mypy; UI lint, vitest, tsc.
