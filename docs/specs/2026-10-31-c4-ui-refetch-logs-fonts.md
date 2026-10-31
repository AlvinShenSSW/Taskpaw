# C4: UI refetch policy, Logs tab rendering, self-hosted fonts

Date: 2026-10-31. Status: plan (not implemented). Issue: #379, under EPIC #375 (audit items 7, 8, 9).
Base: origin/main 3ddf070 (TaskPaw 4.0.2). UI: `taskpaw_v3/ui` (React 19, TanStack Query 5.101.1 per
`package-lock.json`, MUI 6, Vite 6, Vitest 4).

## Spec review

Three independent UI costs, one PR.

1. **Global polling (item 7).**
   - `ui/src/main.tsx:11` sets `refetchInterval: 5000` as the QueryClient default. Every
     `useQuery` without its own interval therefore polls forever.
   - TanStack Query's interval ignores `staleTime`. In `@tanstack/query-core` 5.101.1,
     `queryObserver.ts` `#updateRefetchInterval` calls `#executeFetch()` on every tick
     without consulting staleness, so even `staleTime: Infinity` queries are re-fetched.
     This was verified in the installed copy of the same version, read only.
   - The inventory below finds **67 query call sites**:
     - 30 are live and already declare their own interval;
     - 37 are static and inherit the 5 s default.
   - The static set includes:
     - poster, cover and thumbnail images: every mounted image is re-downloaded as a data
       URL every 5 s;
     - one film-detail request per row on 待确认 (`ReviewLine`);
     - the import preview, which re-reads the import source.
   - The core need is to turn the default off. No live query loses its cadence.
2. **Logs tab (item 8).** `components/TaskLog.tsx:118-150` polls with a raw
   `setInterval`.
   - An idle tick does `setBusy(true)` → `api.logs(after)` → `readDays()` →
     `setDays(newArray)` → `setBusy(false)`. That is three commits.
   - Each commit re-renders `TaskLogRows`, which is not memoized. It re-sorts with
     `mergeLogEntries` and re-runs `logDetails`, `renderLogSentence` and `logSafeText`
     on every row.
   - Polling continues while the window is hidden.
   - On 今天, `entries` only grows during the day, through poll merges and "加载更早".
   - The core need:
     - rows render only when rows change;
     - no poll while hidden;
     - a bounded in-memory and rendered window, with the existing "加载更早 / Load
       earlier" control to reach older rows.
3. **Fonts (item 9).** `ui/index.html:7-10` preconnects to Google and loads one
   render-blocking stylesheet.
   - Fetched on 2026-10-31, it is **470,956 bytes with 467 `@font-face` rules**. 404 of
     them are Noto Sans SC: 101 unicode-range slices × 4 weights, and the 400 weight
     alone is 4,500,544 bytes of woff2 across its slices.
   - The Tauri CSP (`src-tauri/tauri.conf.json:15`) also hard-codes
     `style-src … https://fonts.googleapis.com; font-src https://fonts.gstatic.com`. A
     self-hosted font would be **blocked by today's CSP** unless `font-src` gains
     `'self'`.
   - The core need: Fira Sans and Fira Code served from the bundle, no Google request,
     and CJK falling back to the system font.

**Ambiguity, decided here and listed for the operator under Operator decisions.**
- The bounded window size.
- Whether 归档历史 stays timer-free.
- Which Latin subsets to ship.
- Dropping Noto Sans SC. The issue already allows this.

## Acceptance criteria

1. **Refetch policy.**
   - 1.1 The QueryClient has no default `refetchInterval`.
   - 1.2 Every live query in the inventory below keeps its current explicit cadence.
   - 1.3 Every static query refreshes only on mount, on window refocus (TanStack
     defaults) and on mutation invalidation or `setQueryData`.
   - 1.4 On the agent console, an idle 15 s window issues:
     - exactly one request each to `/control/setup` and `/control/plugins`: the initial
       ones;
     - one request every 5 s each to `/control/status` and `/control/config`.
2. **Logs tab.**
   - 2.1 An idle poll tick (no new rows, days unchanged) re-renders no log row.
   - 2.2 A tick that adds one row renders only that row.
   - 2.3 No `/control/logs` request is made while `document.hidden` is true. Polling
     resumes on the next tick after the page becomes visible.
   - 2.4 On 今天, the list holds at most `LOG_WINDOW` (200) rows.
     - When a poll pushes it past that, the oldest rows are dropped and the
       "加载更早 / Load earlier" button appears.
     - The button re-fetches the dropped rows from the server with `before=<oldest kept
       id>`.
     - After the first "Load earlier" the session stops trimming
       (`limit = Infinity`) until it restarts (filter change, boot restart, 今天
       re-entry), so rows the operator explicitly loaded are never trimmed back by a
       later poll (C4-D2).
     - A trim never leaves a hole across midnight. When the oldest row that would be
       kept belongs to a later day than the session's `day`, the session restarts on
       the new 今天 instead of trimming. "Load earlier" therefore never skips
       trimmed new-day rows (C4-D1).
   - 2.5 Task-log files keep their 30-day / 50 MB retention. No server change is made.
3. **Fonts.**
   - 3.1 `index.html` contains no `http(s)://` reference.
   - 3.2 Fira Sans 400/500/600/700 and Fira Code (variable, 400–700) are served from
     `ui/public/fonts/`, with their OFL licences beside them.
   - 3.3 The Tauri CSP allows `font-src 'self'` and names no Google host.
   - 3.4 Latin text looks the same. CJK falls back to the system font, which is
     Microsoft YaHei on Windows.
4. **Checks.**
   - Vitest covers 1.1, 1.4 and 2.1–2.4.
   - pytest covers 3.1–3.3.
   - `npm run lint`, `npm test` and `npm run build` (tsc + vite) are clean.
   - `uv run pytest` is green.

## Frozen issue contract

- **Acceptance:** AC 1–4 above.
- **Product invariants:**
  - Live data keeps refreshing at its current cadence: power, progress, devices, the
    workflow board, films, film detail, the overview, task and agent status, monitor
    operations, logs, worker status and Hub status. Operator: 「更新的数据比如功耗，进度这些还是要刷新的」.
  - Static data refreshes after the user's own mutations.
  - The Logs page keeps its design (`design-system/taskpaw-v3/pages/logs.md`):
    - a 5 s poll while mounted;
    - a quiet "正在更新 / Updating…" status line for background requests;
    - "Load earlier" disabled while a request is active;
    - newest first, with date headers;
    - export of the whole selected day, up to 20,000 rows. Export is unaffected by the
      window.
- **Engineering invariants:**
  - No new npm dependency and no lockfile change.
  - No backend change.
  - The CSP only gets narrower.
  - Live queries are not edited, because they already declare their intervals.
- **Allowed user-visible changes:**
  - Static pages no longer silently refresh every 5 s while left open.
  - Poster and cover images are no longer re-downloaded.
  - A long 今天 session shows at most 200 rows until "Load earlier".
  - CJK glyphs render in the system CJK font instead of Noto Sans SC.
  - The first paint no longer waits on Google.
- **Non-goals:**
  - Code splitting (EPIC item 10, declined).
  - List virtualization.
  - Reducing `logDays` server cost.
  - Changing any live query's interval.
  - Pausing React Query polls differently. They already pause while hidden, because
    `refetchIntervalInBackground` defaults to false.
  - Bundling Noto Sans SC.
- **Smallest causal boundary:**
  - `ui/src/main.tsx` plus one new `ui/src/queryClient.ts`;
  - `ui/src/components/TaskLog.tsx`;
  - one `invalidateQueries(["network"])` line in `ui/src/views/setup/DoneStep.tsx`
    (C4-D6);
  - `ui/index.html`, `ui/public/fonts/*` and the CSP string in
    `src-tauri/tauri.conf.json`;
  - four monospace font-stack strings;
  - the typography note in `design-system/taskpaw-v3/MASTER.md`;
  - tests.

## Assumptions

- **A1 — window refocus refetch reaches static queries in the Tauri WebView2.**
  - TanStack's `focusManager` listens to `visibilitychange`
    (`query-core/src/focusManager.ts:22-27`, read).
  - Not verified: that WebView2 fires `visibilitychange` when the Tauri window is
    minimised and restored. If it does not, static data still refreshes on mount and
    on every section or tab switch, because each Settings section, media page and
    dialog mounts its own query. Risk: low.
- **A2 — the self-hosted Fira Code file is a variable font with a `wght` axis.**
  - Google serves the same URL for weights 400, 500, 600 and 700
    (`…/firacode/v27/uU9NCBsR6Z2vfE9aq3bh3dSD.woff2` for latin), which strongly implies
    a variable font.
  - The `fvar` table was not inspected.
  - The implementer verifies with fontTools in a temp dir; see the handoff notes. If it
    is static, ship static 400/500/600/700 files from the same CSS API instead.
- **A3 — Chromium on Windows picks a poor CJK fallback for a generic `monospace` stack.**
  - The expected result is NSimSun or SimSun. This was not observed.
  - Mitigation: name `"Microsoft YaHei"` explicitly in the four monospace stacks that
    lack a system CJK family.
- **A4 — the OFL notice requirement is met.**
  - The method: ship each family's `OFL.txt` in `public/fonts/`, which Vite copies to
    `dist/fonts/`.
  - The source URLs returned HTTP 200 on 2026-10-31.
  - The woff2 `name` tables also carry the copyright and licence URL. That was not
    inspected.
- **A5 — the woff2 URLs and sizes are stable.**
  - They come from the Google CSS API response of 2026-10-31. Only HEAD
    `Content-Length` was fetched; the files were not downloaded.
  - Versioned paths (`v27`, `v18`) are stable in practice.
  - The implementer records the sizes after downloading and compares them with the
    table below.
- **A6 — static pages do not rely on server-side changes appearing within 5 s.**
  - Checked query by query; see the inventory notes. The one judgement call is 归档历史
    (Operator decision 2).
  - A restart rebuilds the Tauri main window (`src-tauri/src/restart_supervisor.rs:8`),
    so the post-restart state never depended on polling.
- **A7 — `ui/node_modules` is absent in the worktree.** Verified. The implementer runs
  `npm ci` there, which uses the existing lockfile and changes nothing in the repo,
  before `npm test`, `lint` or `build`.

## Approach

### Item 7 — default off; static queries rely on invalidation

**Change.**
- Add `ui/src/queryClient.ts` exporting `createQueryClient()`. It returns
  `new QueryClient({ defaultOptions: { queries: { retry: <today's predicate> } } })`
  with no `refetchInterval`, and carries a one-line comment stating the policy.
- `main.tsx` uses it.
- No live query needs editing: all 30 already declare an interval, listed below.

**Why a module rather than editing `main.tsx` alone.** Tests build their own
QueryClient (`agentconsole.test.tsx:35` and 25 others), so today the default is
untestable. A factory lets one test assert the real production defaults. It is
5 lines.

**Alternatives rejected.**
- *Keep the default and add `refetchInterval: false` to the 37 static sites.* That
  touches 20 files, and every future query silently polls again.
- *A per-query `staleTime` regime.* It does not stop interval polling, as the
  query-core read above shows.

#### Query inventory (67 call sites; `ui/src/…`)

**Live: unchanged, explicit cadence kept (30)**

| Site | Key / data | Cadence |
|---|---|---|
| `views/AgentConsole.tsx:44` | `agentStatus`: monitor status, progress | 5 s |
| `views/AgentConsole.tsx:48` | `agentConfig`: carries `monitor_operations` (pending start/stop, server-side) | 5 s |
| `views/AgentConsole.tsx:190` | `taskLogs/inline`: latest 8 log rows | 5 s |
| `views/AgentConsole.tsx:347` | `agentConfig` (edit dialog only) | 5 s |
| `components/media/LocalPage.tsx:14` | `agentStatus` | 5 s |
| `components/media/DevicesPage.tsx:92`, `OverviewPage.tsx:40`, `QueuePage.tsx:37` | `workflowDevices` | 5 s |
| `components/media/QueuePage.tsx:59` | `mediaHistory`: the queue's done pages, which grow as films finish | 5 s |
| `components/media/FilmDrawer.tsx:46` | film detail | 5 s |
| `components/media/ReviewDialog.tsx:92` | film detail | 5 s |
| `components/media/ReviewDialog.tsx:100, :171, :191` | target preflights | 30 s until resolved, then off |
| `components/media/workspace.ts:53, :59` | board films, plans | 5 s |
| `components/media/workspace.ts:66` | preflight targets (≤10) | 30 s |
| `components/media/libraryModel.ts:41` | library scan status | 1.5 s busy / 30 s idle |
| `components/power/PowerPage.tsx:52, :53, :57` | power 24 h / ranged / films | 5 s / 60 s / 30 s |
| `components/PagedFilmList.tsx:30`, `RunFilmsCard.tsx:93` | per-monitor film progress | 5 s |
| `components/WorkerJoin.tsx:24`, `views/setup/WorkerJoinStep.tsx:15` | worker join state | 5 s |
| `components/WorkerMonitor.tsx:95` | worker 媒体处理 monitor | 2 s |
| `components/HubSettings.tsx:187` | `hubStatus` (controller card) | 15 s |
| `views/HubDashboard.tsx:69` | `hubStatus` | 5 s |
| `views/HubDashboard.tsx:81` | Hub events (events tab only) | 5 s |
| `views/HubDashboard.tsx:226` | per-server media probes | 5 s |

The Logs tab's own raw interval is handled in item 8.

**Static: today 5 s via the default, after the change on mount, refocus and invalidation (37)**

| Site | Key / data | Refreshed after user action by |
|---|---|---|
| `views/AgentConsole.tsx:45` | `agentPlugins`: the schema catalog | Nothing changes it within a build. |
| `views/SetupWizard.tsx:57` (`AgentHome`) | `setupState` | `SetupWizard.tsx:28`, `setup/DoneStep.tsx:67,72`, `setup/WorkerFieldsStep.tsx:66` |
| `views/Settings.tsx:28`, `components/WorkflowSettings.tsx:92`, `setup/ControllerStep.tsx:47`, `setup/DoneStep.tsx:37`, `setup/RoleStep.tsx:30`, `setup/WorkerFieldsStep.tsx:16`, `setup/WorkerJoinStep.tsx:16` | `workflowSettings` | `WorkflowSettings.tsx:105`, `ControllerStep.tsx:104`, `WorkerFieldsStep.tsx:63` |
| `views/Settings.tsx:135`, `setup/DoneStep.tsx:38` | `restartState` | `Settings.tsx:155,195`, `NetworkAccess.tsx:115`. The workflow section shows its own restart banner from the save response and `restart_required`. `PendingRestart` lives in another section and refetches on mount. |
| `views/Settings.tsx:145,189,252,387`, `components/power/PowerPage.tsx:58` | `agentConfig` (no interval) | `Settings.tsx:154,194,289,403`, `NetworkAccess.tsx:116`, `PowerParams.tsx:86`, `AgentConsole.tsx:66`. The same key is also kept at 5 s by `AgentConsole.tsx:48`, which is mounted around every agent tab. |
| `components/NetworkAccess.tsx:39`, `setup/DoneStep.tsx:36` | `network` | `NetworkAccess.tsx:114,126` |
| `components/HubSettings.tsx:50` | `hubSettings` | `HubSettings.tsx:86,197,208`. Static is also safer here: `HubForm` is keyed on the saved values, so a poll could remount it mid-edit. |
| `components/setupModel.ts:12` | `workflowDetect` (staleTime 60 s) | `setQueryData` on re-detect (`setupModel.ts:15`) |
| `components/setupModel.ts:52` | `dirFacts` (staleTime 15 s) | `ControllerStep.tsx:81` |
| `setup/DoneStep.tsx:39` | `workflowWorker` (worker role summary) | `setQueryData` by `WorkerJoinStep.tsx:22` and `WorkerJoin.tsx:29` |
| `components/media/libraryModel.ts:24, :28` | libraries, rules | `libraryModel.ts:53` (a scan settled, detected by the live scan-status query), `:63`, `:73`; `LibraryRules.tsx:47-52` |
| `components/media/LibraryActors.tsx:43, :46, :125` | performers list, filter counts, performer detail | `libraryModel.ts:53,63`; `LibraryActors.tsx:128-129` |
| `components/media/LibraryHistory.tsx:65` | 归档历史 list | No invalidation. It grows when a finalize archive completes, which is server-side and asynchronous after 确认入库. It refreshes on mount, refocus, and filter or page change. **Operator decision 2.** |
| `components/media/LibraryHistory.tsx:123` | one receipt | Changes (`pending` flips when source cleanup finishes); refreshes on reopen, since the drawer remounts per receipt (C4-D3). |
| `components/media/LibraryImport.tsx:29` | import preview | One-shot. Today it re-reads the import source every 5 s. |
| `components/media/LibraryParts.tsx:22`, `components/media/parts.tsx:75, :121` | thumbnail, poster, cover data URLs (`staleTime: Infinity`) | Keys change with the source. Today they are re-downloaded every 5 s. |
| `components/media/ReviewPage.tsx:78` (`ReviewLine`) | detail per ready film (staleTime 30 s) | Today one request per row every 5 s. The file count of a ready film is fixed. `ReviewDialog` invalidates `mediaFilmKey` on confirm. |
| `components/media/ExplainDialog.tsx:22` | film detail, used for the title and target path only | Both are fixed by the approved binding. The dialog states that results appear in the film detail, which `FilmDrawer` polls live. |
| `components/media/ReviewDialog.tsx:122, :451` | performer search | Typing changes the key. |

### Item 8 — memoized rows, quiet idle ticks, hidden pause, bounded window

All changes are inside `components/TaskLog.tsx`.

**Memoization.**
- Wrap `LogRow` in `memo`. Its props are `entry` and `compact`.
  - Entries keep their object identity across merges: `mergeLogEntries` re-uses the
    objects through a Map, and poll pages only add new ids.
  - React Query's structural sharing keeps the inline `logs.data.entries` stable as
    well.
- Wrap `TaskLogRows` in `memo`.
  - Compute `rows` with `useMemo(() => mergeLogEntries(entries), [entries])`.
  - Replace the default parameter `entries = []`, which is a new array on every render,
    with a module constant `NO_ENTRIES`.
- The card shell (filters and status line) may still re-render for `busy`. That is the
  quiet status line the logs page design requires. The rows do not re-render.

**No-op state updates.**
- `readDays` calls `setDays` only when the list changed. Compare a cheap key held on
  the session, e.g. `days.map(d => d.day + ":" + d.count).join(",")`.
- `setToday` needs no guard: React already bails out on an identical primitive
  (C4-D8).
- Already true and kept: `setEntries` is called only when a tick found newer rows.
- `setError(false)` only when it was true is optional. React bails out on an identical
  primitive.

**Hidden pause.**
- `poll()` returns at once when `document.hidden`, before any state update or request.
- No `visibilitychange` listener is added. The next 5 s tick resumes, which is the
  smallest change.

**Bounded window.**
- Add `const LOG_WINDOW = 200` and two session fields: `rows: LogEntry[]`, the session's
  source of truth, and `limit: number`, starting at `LOG_WINDOW`.
- `load(older)`:
  - `current.rows = older ? mergeLogEntries(current.rows, page) : page`;
  - if `older`, `current.limit = Infinity` (C4-D2: explicitly loaded rows are never
    trimmed again in this session);
  - `setEntries(current.rows)`.
- `poll` merges into `current.rows`.
  - If the result is longer than `current.limit`, compute the newest `current.limit`
    rows.
    - If the oldest of those rows has `id.slice(0, 8)` later than `current.day`, and
      the client day has also moved on (`localLogDay() > current.day`), call
      the existing `restart()`. If the client clock has not moved past `current.day`
      (for example, a clock stepped back), trim normally instead, so the restart
      cannot loop (C4-D10). That starts a fresh 今天 session, which loads the new
      day's newest page, and nothing is trimmed (C4-D1). The server's `before` reads
      only `day`'s file (`core/tasklog.py:515-516`), so a trim cursor in a later day
      would hide those rows from "Load earlier".
    - Otherwise keep them, set `current.before` to the id of the oldest kept row, and
      `setBefore` to the same.
  - Then `setEntries(current.rows)`.
- The trimming lives on the session object, not inside a `setEntries` updater, because
  it also moves the `before` cursor. An updater must stay pure.
- The existing "Load earlier" button then works unchanged. The server's `before` is
  exclusive and id-based (`core/tasklog.py:536-550`), so `before=<oldest kept id>`
  returns exactly the trimmed rows, and anything older.
- Only poll merges trim. A historical day, which never gets poll rows, grows only by
  explicit "Load earlier".

**Periodic tidying (「需要定期整理」), what else grows.**
- Server: the ring buffer is bounded (`RING_SIZE = 2000`, `core/tasklog.py:26`). Files
  are pruned to 30 days or 50 MB (keeping at least 6 days), at start-up
  (`tasklog.py:137`) and at each day roll-over (`tasklog.py:386-390`).
- Client: the only unbounded state was `entries`, now bounded. `taskNames` is derived
  from it. The inline recent-logs card requests `limit: 8`.
- Nothing else needs tidying.

**Design compliance.**
- `pages/logs.md` keeps holding:
  - "Load earlier" disabled while busy;
  - a quiet status line for background requests;
  - newest first, with date headers;
  - export still pages the whole day independently.
- Add one bullet to `pages/logs.md`: "Today keeps the newest 200 rows; older rows
  return through Load earlier."

**Alternatives rejected.**
- Virtualizing the list adds a dependency and is more than the issue asks.
- A separate client-side "show earlier" next to the server "Load earlier" would give two
  confusing buttons.
- Moving TaskLog onto `useQuery` is a large rewrite of a carefully serialized
  session/cursor model with extensive tests (`src/test/tasklog.test.tsx`, 504 lines).

### Item 9 — vendor Fira woff2 into `ui/public/fonts/`, drop Noto Sans SC

**Recommendation: vendor the files, not `@fontsource`.**
- Vendoring needs no `npm install` and no lockfile change, and it ships only the
  subsets used.
- `@fontsource/fira-sans` and `@fontsource-variable/fira-code` would add two
  dependencies and a lockfile change. Each weight's CSS also references every subset
  (cyrillic, greek, vietnamese…), so all of them would be emitted into `dist`.
- `ui/public/` already holds a static page that the shell loads
  (`public/hub-waiting.html`, used by `src-tauri/src/hub_connection.rs:161`).
- Vite copies `public/` into `dist/` verbatim, and Tauri bundles `../ui/dist`
  (`tauri.conf.json:7`).

**Weights.** Measured in `ui/src`:
- `fontWeight` and `font-weight` values in use are 400, 500, 600 and 700, plus one 650
  (synthesised from the nearest weight). `<strong>`, `<b>` and `<th>` (44 + 15 + 53
  uses) are bold (700) through CssBaseline and UA defaults.
- No `h1`–`h4` variant is used, so MUI's `fontWeightLight` (300) never renders.
- Therefore drop Fira Sans 300, which Google loads today.

**Subsets.** Ship latin and latin-ext.
- latin-ext covers romanised names with macrons, such as "Ō", in film and actor
  folders.
- Unicode-range means latin-ext is fetched only when such a glyph appears.
- Cyrillic, greek, vietnamese and Fira Code's symbols2 fall back to the next font, as
  they would for any glyph outside the subset.

**Files to vendor.**
- Each file is saved under `taskpaw_v3/ui/public/fonts/`.
- Sizes are the HEAD `Content-Length` values of 2026-10-31.

| File | Source URL | Bytes |
|---|---|---|
| `fira-code-latin.woff2` (variable, wght) | `https://fonts.gstatic.com/s/firacode/v27/uU9NCBsR6Z2vfE9aq3bh3dSD.woff2` | 36,276 |
| `fira-code-latin-ext.woff2` (variable, wght) | `https://fonts.gstatic.com/s/firacode/v27/uU9NCBsR6Z2vfE9aq3bh09SDulI.woff2` | 13,272 |
| `fira-sans-400-latin.woff2` | `https://fonts.gstatic.com/s/firasans/v18/va9E4kDNxMZdWfMOD5Vvl4jL.woff2` | 23,872 |
| `fira-sans-400-latin-ext.woff2` | `https://fonts.gstatic.com/s/firasans/v18/va9E4kDNxMZdWfMOD5VvmYjLeTY.woff2` | 46,400 |
| `fira-sans-500-latin.woff2` | `https://fonts.gstatic.com/s/firasans/v18/va9B4kDNxMZdWfMOD5VnZKveRhf6.woff2` | 24,056 |
| `fira-sans-500-latin-ext.woff2` | `https://fonts.gstatic.com/s/firasans/v18/va9B4kDNxMZdWfMOD5VnZKveSBf6TF0.woff2` | 46,996 |
| `fira-sans-600-latin.woff2` | `https://fonts.gstatic.com/s/firasans/v18/va9B4kDNxMZdWfMOD5VnSKzeRhf6.woff2` | 24,844 |
| `fira-sans-600-latin-ext.woff2` | `https://fonts.gstatic.com/s/firasans/v18/va9B4kDNxMZdWfMOD5VnSKzeSBf6TF0.woff2` | 50,268 |
| `fira-sans-700-latin.woff2` | `https://fonts.gstatic.com/s/firasans/v18/va9B4kDNxMZdWfMOD5VnLK3eRhf6.woff2` | 25,012 |
| `fira-sans-700-latin-ext.woff2` | `https://fonts.gstatic.com/s/firasans/v18/va9B4kDNxMZdWfMOD5VnLK3eSBf6TF0.woff2` | 50,368 |
| `OFL-FiraSans.txt` | `https://raw.githubusercontent.com/google/fonts/main/ofl/firasans/OFL.txt` | (text) |
| `OFL-FiraCode.txt` | `https://raw.githubusercontent.com/google/fonts/main/ofl/firacode/OFL.txt` | (text) |
| `fonts.css` | hand-written, 10 `@font-face` rules | ~2 KB |

**Size impact.**
- The installer and `dist` grow by about 341,364 bytes of woff2 (≈ 333 KiB). The latin
  files alone are 134,060 bytes.
- At runtime, a Chinese UI with Latin digits and IDs typically loads the latin files
  only, on demand.
- Removed from first paint: the 470,956-byte Google stylesheet, its network wait and
  offline timeout, and up to about 18 MB of Noto Sans SC slices fetched over time
  (≈ 4.5 MB per weight × 4).

**`fonts.css` content.**
- One rule per file:
  - `font-family: "Fira Sans"` with `font-weight: 400`, `500`, `600` or `700`, or
    `font-family: "Fira Code"` with `font-weight: 400 700`;
  - `font-style: normal; font-display: swap;`;
  - `src: url("./<file>") format("woff2")`;
  - the unicode-range copied verbatim from the Google CSS.
- latin range: `U+0000-00FF, U+0131, U+0152-0153, U+02BB-02BC, U+02C6, U+02DA, U+02DC,
  U+0304, U+0308, U+0329, U+2000-206F, U+20AC, U+2122, U+2191, U+2193, U+2212, U+2215,
  U+FEFF, U+FFFD`.
- latin-ext range: `U+0100-02BA, U+02BD-02C5, U+02C7-02CC, U+02CE-02D7, U+02DD-02FF,
  U+0304, U+0308, U+0329, U+1D00-1DBF, U+1E00-1E9F, U+1EF2-1EFF, U+2020, U+20A0-20AB,
  U+20AD-20C4, U+2113, U+2C60-2C7F, U+A720-A7FF`.
- Header comment: "Fira Sans / Fira Code, SIL OFL 1.1 — see OFL-*.txt; self-hosted
  (#379), no runtime Google request."

**`index.html`.**
- Delete the two `preconnect` links, the Google `<link>` and the #89 comment.
- Add `<link rel="stylesheet" href="/fonts/fonts.css" />`. A local, render-blocking
  stylesheet is about 2 KB and is served from the bundle.

**CSP (`src-tauri/tauri.conf.json:15`).**
- `style-src 'self' 'unsafe-inline'` (drop `https://fonts.googleapis.com`).
- `font-src 'self'` (replace `https://fonts.gstatic.com`).
- Everything else stays byte-identical.
- The public woff2 files are never inlined by Vite, so `data:` is not needed.

**CJK fallback (Noto Sans SC dropped, Operator decision 1).**
- The font stacks keep the `"Noto Sans SC"` name. It is used only if installed locally,
  and it keeps `src/test/theme.test.ts` truthful.
- Add `"Microsoft YaHei","PingFang SC"` right after it in the four monospace stacks that
  have no system CJK family:
  - `src/theme.ts:20` (`MONO`, used by `body2` everywhere);
  - `src/components/progressParts.tsx:12`;
  - `src/components/HubMedia.tsx:16`;
  - `src/components/PasswordWidget.tsx:39`.
- `SANS` (`theme.ts:19`) and `mediaStyles.ts:18-19` already name Microsoft YaHei.
- Update the `theme.ts:17-18` comment.

**Design system.** In `design-system/taskpaw-v3/MASTER.md` §Typography (lines 34-43),
replace the Google Fonts link and `@import` with: self-hosted under
`taskpaw_v3/ui/public/fonts/` (Fira Sans 400–700, Fira Code 400–700; CJK uses the system
font). Agents read MASTER before building pages and would otherwise re-add the Google
link.

## Files to change

| Path | Change | Reason |
|---|---|---|
| `taskpaw_v3/ui/src/queryClient.ts` | new | `createQueryClient()` with no default interval (item 7); testable. |
| `taskpaw_v3/ui/src/main.tsx` | modify | Use `createQueryClient()`; drop the inline QueryClient and the `ControlCredentialError` import. |
| `taskpaw_v3/ui/src/components/TaskLog.tsx` | modify | `memo` rows and list, `useMemo` merge, no-op guards, hidden pause, session `rows`/`limit`, poll trim (item 8). |
| `taskpaw_v3/ui/index.html` | modify | Remove Google preconnects and the stylesheet; link `/fonts/fonts.css`. |
| `taskpaw_v3/ui/public/fonts/` (10 × `.woff2`, `fonts.css`, `OFL-FiraSans.txt`, `OFL-FiraCode.txt`) | new | Self-hosted fonts and licences. |
| `taskpaw_v3/src-tauri/tauri.conf.json` | modify, CSP string only | `font-src 'self'`; drop the Google hosts. |
| `taskpaw_v3/ui/src/theme.ts`, `components/progressParts.tsx`, `components/HubMedia.tsx`, `components/PasswordWidget.tsx` | modify, one string each | Deterministic system CJK fallback in the monospace stacks. |
| `design-system/taskpaw-v3/MASTER.md` | modify, Typography section | Self-hosted fonts; no Google import. |
| `design-system/taskpaw-v3/pages/logs.md` | modify, one bullet | Document the 200-row window. |
| `taskpaw_v3/ui/src/test/refetchpolicy.test.tsx` | new | AC 1.1, 1.4. |
| `taskpaw_v3/ui/src/test/tasklogrender.test.tsx` | new | AC 2.1, 2.2. It needs a module-level `vi.mock`, so it goes in its own file. |
| `taskpaw_v3/ui/src/test/tasklog.test.tsx` | add tests; edit the drain test | AC 2.3, 2.4, midnight (C4-D1). The "drains a full oldest-first page" test (`:415-428`) now expects 200 rows, `20260926-302`…`501`, plus the Load earlier button, and still the `after=20260926-500` request (C4-D9). |
| `taskpaw_v3/ui/src/views/setup/DoneStep.tsx` | modify, one line | Invalidate `["network"]` after `api.networkFirewall()` (C4-D6). |
| `taskpaw_v3/ui/src/test/setupwizard.test.tsx` | add 1 assertion | The DoneStep firewall row refreshes (C4-D6). |
| `taskpaw_v3/ui/src/test/theme.test.ts` | add 1 assertion | The monospace stack names Microsoft YaHei. |
| `taskpaw_v3/tests/test_ui_fonts.py` | new | AC 3.1–3.3: no external refs, local font files exist, CSP. |
| `CHANGELOG.md` | one line, per the EPIC convention | Release notes. |

## Execution surface

- **Writes:** only the files listed above.
- **Read or executed, not modified:** every other `ui/src` file in the inventory,
  `core/tasklog.py`, `src-tauri/src/restart_supervisor.rs` and
  `src-tauri/src/hub_connection.rs`.
- **Downloads.** The implementer fetches the 10 woff2 and 2 OFL files from the URLs in
  the table.
  - Use `curl -fL --max-time 30 -o <file> <url> < /dev/null`, run from a temporary
    directory.
  - Check each size against the table and the woff2 magic `wOF2`.
  - Then copy them into `taskpaw_v3/ui/public/fonts/`.
  - Downloaded files are data only and are never executed.
- **Verification commands:**
  - from `taskpaw_v3/ui`: `npm ci` (no lockfile change), `npm run lint`, `npm test`,
    `npm run build`;
  - from the repo root: `uv run pytest`.
- **Generated output.** `npm run build` writes `taskpaw_v3/ui/dist/`, which is not
  committed. It should contain `dist/fonts/*.woff2`, `fonts.css` and `OFL-*.txt`
  copied from `public/`.
- **Must not:**
  - run `npm install <pkg>` or change `package.json` or `package-lock.json`;
  - touch the installed app or `%APPDATA%\TaskPaw`.

## Key implementation notes

- **TanStack facts this plan relies on**, verified in the `query-core` 5.101.1 source:
  - interval polling ignores `staleTime`;
  - interval ticks are skipped while unfocused unless `refetchIntervalInBackground`;
  - `refetchOnWindowFocus` and `refetchOnMount` default to true, with staleTime 0;
  - a function `refetchInterval` returning `false` or `undefined` means no interval. It
    never falls back to the default.
- **The `agentConfig` key is shared by a 5 s observer** (`AgentConsole.tsx:48`) and by
  interval-less observers in Settings and PowerPage. TanStack runs one interval per
  observer, so the cache stays at 5 s while AgentConsole is mounted. That is intended:
  `monitor_operations` is live.
- **TaskLog ordering inside `poll()`:**
  1. `if (document.hidden) return;`
  2. the day check (`setToday`, unguarded);
  3. the busy and serialization check;
  4. the paged catch-up loop, merging into `current.rows`, then trimming, then one
     `setEntries` per page that adds rows;
  5. `readDays()` with its change guard.
- Keep `current.busy` serialization exactly as it is. "Load earlier" and poll must not
  overlap, because the trim and the older-page merge both write `current.rows` and
  `current.before`.
- **`load(false)` (session start or restart)** resets `current.rows = page` and
  `current.limit = LOG_WINDOW`. The effect creates a fresh session on every filter
  change already.
- **The `TaskLogRows` key** in `TaskLog` (`${revision}-${selection}-…`) stays. It
  remounts on filter change, which is the existing behaviour.
- **Fira Code `font-weight: 400 700` range syntax** is supported by Chromium and
  WebView2, and by Safari for the Mac shell.
- **Tauri dev mode** (`npm run dev`) serves `public/` at `/`, so `/fonts/fonts.css`
  works in dev and in the bundle.

## Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| A static view shows stale data after a server-side change | Low | Low | The inventory checked each one. Refresh on mount and refocus remains. Library page data (归档历史, library and performer counts, receipts) refreshes on visit and refocus (operator decision 2, widened per C4-D3/D4). |
| A transient error on a `retry:false` static query no longer clears itself on the next 5 s tick | Low | Low | The window opens only after the backend is ready. Remount or refocus refetches. Worst case: `/control/setup` failing shows the console instead of the first-run wizard until refocus (C4-D5). Accepted. |
| A live query relied on the default and is missed | Very low | Medium | All 67 call sites enumerated. The 30 live ones all carry explicit intervals, and this change edits none of them. `refetchpolicy.test.tsx` guards the default and two agent-console intervals, not every live site (C4-D7). |
| The CSP blocks the self-hosted fonts | Medium if forgotten | Medium (system fonts shown) | CSP edit in scope; pytest asserts `font-src 'self'`. |
| Fira Code is not variable, so 600/700 are synthesised | Low | Low (faux bold) | The fontTools check in the handoff notes. If static, vendor the 4 static files from the same CSS API. |
| A CJK monospace fallback looks different from Noto Sans SC | Certain (intended) | Low | Microsoft YaHei is named explicitly. The issue AC accepts the system font. |
| Trimming drops rows the operator is reading at the bottom of a long 今天 list | Low | Low | Trimming only happens past 200 rows. "Load earlier" restores them and turns trimming off for the session. Across midnight the session restarts instead of trimming. |
| The client clock is behind the log ids, so a trim keeps a next-day cursor (C4-D12) | Very low | Low | A normal trim is accepted so the restart cannot loop. The trimmed next-day rows are reachable only after the session restarts: 今天 re-entry or the next real midnight restarts it. A code comment at the branch says so. |
| An existing tasklog test assumes `readDays` → `setDays` every tick | Low | Low | `readDays` still requests every tick, so "refreshes only the days list while viewing a past day" still holds. Only the state write is skipped. |

## Out of scope

- Reducing server-side `logDays` work. Today's day summary is recomputed from the file
  on every call (`core/tasklog.py:457-463`, `:445-455`), every 5 s while the Logs tab
  is open. It is a candidate follow-up, not part of #379.
- Code splitting (declined), list virtualization, and any change to live intervals.
- Bundling Noto Sans SC or any CJK font.
- Hub web serving: the backend does not serve the UI (no `StaticFiles` and no CSP in
  Python).

## Test plan

**Vitest** (`taskpaw_v3/ui/src/test/`, jsdom, `setup.ts`; run with `npm test` from
`taskpaw_v3/ui`).

1. `refetchpolicy.test.tsx`
   - `createQueryClient().getDefaultOptions().queries?.refetchInterval` is `undefined`.
   - Its `retry` still refuses `ControlCredentialError`.
   - Render `<AgentHome/>` (from `views/SetupWizard`) inside
     `QueryClientProvider client={createQueryClient()}` and the theme.
     - Use `vi.useFakeTimers()`.
     - Stub `fetch` by path, following `agentconsole.test.tsx:20-31`:
       - `/control/setup` → `{ status: "done" }`;
       - `/control/status` → two monitors;
       - `/control/plugins` → `{ plugins: [], presets: [] }`;
       - `/control/config` → `{}`;
       - others → `{}`.
     - `advanceTimersByTimeAsync(0)`, then `advanceTimersByTimeAsync(15000)`.
     - Count calls per path. Expect:
       - `/control/setup` 1 and `/control/plugins` 1;
       - `/control/status` ≥ 4 and `/control/config` ≥ 4: one initial call plus three
         interval calls.
   - Optionally, mount `Poster` or `LibraryThumb` with a stubbed image fetch and assert
     there is no second request after 15 s.
2. `tasklogrender.test.tsx`
   - `vi.mock("../components/TaskLog.helpers", async (importOriginal) => { const m = await importOriginal(); return { ...m, renderLogSentence: vi.fn(m.renderLogSentence) }; })`.
   - Render `TaskLog` with the fetch stub pattern of `tasklog.test.tsx:215-223`, three
     rows initially, under fake timers.
   - Clear the spy, then advance 5 s with no new rows. Expect `renderLogSentence` to be
     called 0 times (AC 2.1).
   - Make the next `after=` response return one new row and advance 5 s. Expect exactly
     1 call (AC 2.2).
   - Both fail on 3ddf070. Without memo, each idle tick re-renders all rows about 3
     times.
3. `tasklog.test.tsx`, new cases.
   - **Hidden pause (AC 2.3).**
     - `Object.defineProperty(document, "hidden", { configurable: true, get: () => true })`
       after the initial load.
     - Clear the fetcher and advance 10 s. Expect no `/control/logs` call.
     - Restore `hidden = false` and advance 5 s. Expect one `after=` call.
     - Restore the property in `afterEach`.
   - **Window (AC 2.4).**
     - The initial load returns 100 rows, `next_before: null`.
     - The poll returns 150 newer rows.
     - Expect 200 `log-row-*` elements, with the newest present and the oldest 50
       absent.
     - Expect the "Load earlier" button to be visible.
     - Click it. Expect a request with `before=<id of the oldest kept row>`, and after
       it resolves, 250 rows.
     - A further poll that adds one row leaves 251 rows (C4-D2).
   - **Midnight (C4-D1).** System time starts at 23:59:59 on day D, as in the
     existing carry-over test, and moves past midnight before the tick. A poll
     returns more than 200 rows whose ids are on day D+1. Expect a restart onto the
     new day, a fresh `before`-less load for D+1, and no `before=<D+1 id>` request
     for day D.
   - **Clock not advanced (C4-D10).** Same rows, but the system time stays on day D.
     Expect a normal trim and no restart.
   - **Drain (edited, C4-D9).** See Files to change.
4. Other existing `tasklog.test.tsx` cases must stay green unchanged: midnight
   carry-over, debounce, past-day days refresh, boot restart, late response, export.
5. `theme.test.ts`: add `expect(String(theme.typography.body2.fontFamily)).toContain("Microsoft YaHei")`.
6. **Firewall row (C4-D6).** `DoneStep` invalidates `["network"]` after
   `api.networkFirewall()`. Covered by one assertion in `setupwizard.test.tsx`.

**pytest** (`taskpaw_v3/tests/test_ui_fonts.py`; path helpers as in `test_version.py:22-25`).

- `ui/index.html` has no `http://` or `https://` substring, and contains
  `href="/fonts/fonts.css"`.
- Every `url(...)` in `ui/public/fonts/fonts.css` is relative, the file exists, and its
  first 4 bytes are `b"wOF2"`.
- `font-family: "Fira Sans"` and `"Fira Code"` are both declared.
- `OFL-FiraSans.txt` and `OFL-FiraCode.txt` exist and contain `SIL OPEN FONT LICENSE`.
- The `tauri.conf.json` CSP contains neither `googleapis` nor `gstatic`, and its
  `font-src` directive includes `'self'`.

**Manual smoke** (operator, after the build; not run by AFK):
- Launch the agent offline. The window paints without waiting.
- Latin text is in Fira, and Chinese in Microsoft YaHei.
- The 日志 tab updates while visible.
- 功耗 and the media board still tick.

## Operator decisions

1. **Drop Noto Sans SC** (recommended) in favour of the system CJK font, Microsoft YaHei.
   - Bundling it would add about 4.5 MB per weight.
   - Issue #379 AC 3 already allows this.
   - It reverses #89's "in-brand CJK".
**Driver record, 2026-10-31.** Decisions 1–4 were taken at their recommended defaults
and recorded in the run ledger. Decision 1 matches the operator's 「用本地字体」.

2. **Library page data (归档历史, library and performer counts, receipts) refreshes on
   visit and refocus, not on a timer** (recommended; widened per C4-D3/D4). The
   operator's list names history as static. The alternative is to invalidate the
   history key when the board sees a film reach 已入库, which adds coupling between the
   board and the library page.
3. **Window size `LOG_WINDOW = 200`** (two server pages). The value is easy to change.
4. **Subsets: latin + latin-ext** (≈ 333 KiB in the bundle; recommended for romanised
   names with macrons), or latin only (≈ 131 KiB).

## Handoff notes

- Run `npm ci` in `taskpaw_v3/ui` first. `node_modules` is absent in this worktree.
  Never run `npm install <pkg>`.
- Write the two RED tests first: `refetchpolicy.test.tsx` and `tasklogrender.test.tsx`.
- **Variable-font check, in a temp dir:**
  `uv run --no-project --with fonttools python -I -c "from fontTools.ttLib import TTFont; f=TTFont(r'<path>'); print('fvar' in f, [a.axisTag for a in f['fvar'].axes] if 'fvar' in f else None)"`.
  The expected result is `True ['wght']` for both Fira Code files.
- Do not edit the 30 live queries. If a reviewer asks to "make intervals explicit
  everywhere", point to the frozen contract: the change is the default, not the call
  sites.
- Keep the CSP edit to the two directives. The version-consistency test reads
  `tauri.conf.json` `version` only.
