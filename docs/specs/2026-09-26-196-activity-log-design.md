# #196 — Task log 「日志」: a persistent, readable timeline of what each task and process did; version 3.9.0

Date: 2026-09-26 (design v4, FROZEN — debate rounds 1–4: L1–L20, W1–W5, N1–N4; round 4 CLEAN)
Issue: #196. Owner decisions: the page is named 「日志」; entries are kept 30 days; viewing is local only (no Hub).
Driver: `/afk` — Claude leads (design, debate, orchestration); implementation by Codex gpt-6-astra (effort high);
outer gate Claude; final gate DeepSeek flash. Merge: leave-open.

## Spec review

The owner wants to know, per task and per process, what happened in plain words — "Lada 开始转码 xx", "完成",
"报错", "开始用 grok 翻译", "中途出问题切到 deepseek". Today nothing answers that: the console Events tab shows
the last 500 events kept in memory (lost on restart), throttled at 60/min per task, mostly alerts, oldest-first,
time without a date; every event of every level is forwarded to OpenClaw; the backend log is technical English.

## Acceptance criteria

- [ ] AC1 **Store** (`core/tasklog.py`, new — "activity" already means dev-agent activity in this repo, L12):
  `TaskLog.record(task, kind, *, task_type, film=None, severity="info", pid=None, proc=None, data=None) -> None`
  - thread-safe, NEVER raises; each record is one JSON line (`ensure_ascii=True`, `allow_nan=False` after
    non-finite floats become null — L18c) appended to `<data dir>/logs/tasklog-<YYYYMMDD>.jsonl`; the file is opened
    in append mode and closed per record (no long-lived handle; verified < 0.3 ms p99, 0 torn lines with 8 writer
    threads — critic E1/E2); no fsync.
  - **One critical section (L15):** id assignment, the append (with its single immediate retry) and the ring insert
    all happen under ONE lock, so ids reach readers in order (a delayed writer can no longer let `after` polling skip
    an entry — critic E9). Only the failure callback runs outside it. The record's day is monotonic within a process
    (`max(last day, today)`), so a clock step back across midnight cannot file a record below the cursor.
  - **Torn line (L18a):** at start, if today's file does not end with `\n` (a crash mid-write), the first append is
    prefixed with `\n`, so the torn line stays one skipped line and the new record is not glued onto it.
  - **Sanitising (L3):** every string in the record — task, film, data keys and values, recursively — is made
    JSON/UTF-8 safe (lone surrogates → `backslashreplace`), so no record can make the API fail.
  - **Write failure (L5):** retried once; if it still fails the record stays memory-only and is counted; under
    the lock the store only sets a flag — after releasing it, an injected `on_first_failure(message)` callback
    is called once per process run, inside its own try/except (a failing callback never makes `record()` raise).
    `run_agent` wires it to a direct `queue.add(level="alert", …)` (NOT through the supervisor, so it is not
    mirrored back — no recursion/deadlock); a failure that happens before the queue exists is latched and delivered
    when the callback is wired.
  - Process-wide holder `set_task_log/get_task_log` published by `run_agent` from its config folder (never
    `default_config_path()`); no data dir → memory-only. An in-memory ring holds the last 2 000 records.
  - **Start order (L19):** the store is built (max-n scan, torn-line check, cap rebuild), the previous session is
    reconciled and `agent.started` is recorded ONLY after the stale-instance reclaim and both port claims have
    succeeded (a relaunch overlaps the old process until the reclaim kills it; a launch that fails a port claim
    must leave no line), and before `build_supervisor` / `supervisor.start`. Nothing records before that point.
    `agent.stopping` is recorded at the top of shutdown, before the supervisor stops (W5): its shutdown callback is
    registered AFTER the "agent-servers" one (`GracefulShutdown` runs callbacks newest-first), so it runs first (N3).
  - The Hub's own Supervisor is unaffected: the store is injected into the agent's supervisor only (L12).
- [ ] AC2 **Record shape and ids (L1).** `{"v":1, "id":"<YYYYMMDD>-<n>", "ts":"<local ISO with offset>",
  "task", "task_type", "kind", "severity":"info|warn|error", "film"?, "pid"?, "proc"?, "data"?}`.
  - `n` is a per-day counter: at start the store scans today's file for the **largest** `n` on any parseable line
    (a trailing line without `\n` is ignored) and continues from it; a failed write still consumes its `n`
    (memory-only records never share an id with a file line in the same process; after a restart they are gone).
  - Ids are compared as `(day, int n)`, never as strings.
  - **Boot id (L18b):** every API response carries `boot` (a random id per agent process); the UI reloads its view
    when it changes, so an id re-used after a restart (the last record of the previous run was memory-only) is never
    mistaken for one it has already shown.
  - **Never logged:** argv, `*_extra_args`, custom commands, API keys, userinfo, ports, file contents, subtitle
    text. `operator.update` records changed field NAMES only (L12). Child output tails appear only in failure
    records, bounded to 800 chars (existing `bounded`); a tail is the child's own output and is exempt from the
    "no argv" test (L12).
- [ ] AC3 **Kind catalog** (structured `data`; the UI renders zh + en; plugins never concatenate text):
  - agent: `agent.started` (version), `agent.stopping` — recorded by the launcher. **Unclean exit (L10):** on
    Windows the desktop shell force-kills the backend when the app closes (no signal; `GracefulShutdown`, plugin
    `stop()` and `agent.stopping` never run), so at start the launcher asks the store to reconcile the previous
    session: if the last `agent.started` has no later `agent.stopping`, `agent.started` carries
    `{previous_exit: "unclean", last_ts}` and one reconstructed `task.interrupted {reconstructed: true, film, step}`
    is recorded per activity that was still open at the end of that session (a `restore.started` / `asr.started` /
    `translate.started` with no conclusion for the same task and film). **Closers (L20):** restore — `restore.finished
    / failed / retry / skipped` or `task.interrupted`; ASR — `asr.finished`, `asr.retry`, any `subs.*` for that
    film or `task.interrupted`; translation — `translate.finished / paused`, any `subs.*` for that film or
    `task.interrupted`; and every task-level entry closes all open activities of its task: `task.done`,
    `task.aborted`, a later `task.started`, `subs.skipped_bulk`, `operator.stop / remove / update`. (Lada in capture
    mode records `restore.finished` for its last file at exit 0, so `task.done` is not its only closer.) A closer
    counts only when it comes AFTER the activity's latest start (in the reverse scan, a task-level closer closes
    every earlier start of that task). Every ASR attempt that spawns records its own `asr.started` (new pid), so
    `asr.retry` closes only the failed attempt. In capture mode a non-zero Lada exit records
    `restore.failed {film: current file}`. Reconstructed entries render as inferred (「可能中断」) (N1).
    **Termination (L20):** files are read newest first and lines in reverse until the first `agent.started` or
    `agent.stopping`: `agent.stopping` first → clean; `agent.started` first → unclean; neither found but entries
    exist (the session outlived retention, was pruned, or its `agent.started` write failed) → unclean over the
    scanned range; no files → first run, nothing to reconcile. Bounded by the retained files (critic E11: a 20-day,
    55 MB session scanned in 0.4 s).
  - operator (admin, recorded after the request validates and just BEFORE the live change is applied, L10/N2 — a
    rejected request records nothing): `operator.start|stop|add|remove|update`.
  - task: `task.started` (queued, done, skipped counts), `task.done` (done, failed, skipped, duration, kept_ja,
    paused), `task.aborted` (reason), `task.interrupted` (film, step, elapsed — recorded by the plugin's own
    `stop()`, ONE entry per interrupted activity (L17): the restore or ASR of a **live** child (`poll() is None`),
    and the translation in flight — taken from a snapshot of `translator.progress()` and `queued()` made BEFORE
    `translator.cancel()` (cancel clears both). A restore child that already exited non-zero, unpolled, when Stop
    lands records `restore.failed {exit_code, at_stop: true}` instead (not final — the next Start retries the file,
    W3); an ASR child in the same state records nothing (harmless: the next Start re-plans it). A child that exited
    0 is published and records only `restore.finished`), `task.error` (setup/launch problems:
    missing exe, scan failure, planning failure, ffprobe missing, collisions — the structured twin of those
    alerts), `task.gpu_wait` (holder) and `task.gpu_acquired` (L9c: logged when a GPU child is actually launched
    after a logged wait; a Stop/abort clearing the flag logs nothing).
  - restore (Lada, Jasna): `restore.started` (film, index/total, mode unet-4x|plain, pid, proc),
    `restore.finished` (output name, duration — recorded at the ONE successful `_publish_current`, which covers
    the normal, stopping and `stop()` publish paths, L9b), `restore.failed` (exit code, tail), `restore.retry`
    (next mode), `restore.skipped` (already restored / name collision).
  - ASR: `asr.started` (engine, pid, proc), `asr.finished` (lines, duration — when a transcript with at least one
    cue is published: the `succeeded` outcome, incl. `_stop_asr` when a transcript finished as Stop landed; NOT the
    empty no-speech publish, W1/L16), `asr.retry` (a
    non-final failed attempt). The FINAL ASR failure and the no-speech outcome are carried by `_settle`'s entry
    (`subs.failed {step: "asr"}`, `subs.published {no_speech: true}`), not by separate ASR entries (L16).
  - translation (translator thread, push — L2): `translate.started` recorded at the film's FIRST `_call` with the
    label of the provider that call actually goes to (lines, resumed lines) — or, when a film defers or finishes
    without any `_call` (deferred at once; fully resumed), at that moment with `model: null` (L16);
    **`translate.switched {from, to, reason}` only for availability-driven changes (L14):** `unavailable` (`from`
    is open — its failure kind is kept on the provider as `last_fail_kind`, pure observation; `from` is added to the
    film's `left_open` set), `recovered` (`to` is in the film's `left_open` and now closed — removed from the set),
    `changed` (`from` is no longer in the chain / retired — a Settings change); if both `unavailable` and
    `recovered` hold, `unavailable` wins (W4); otherwise no entry. The switch facts are computed where `film.model`
    is set (under `_count_lock`), and `record()` is called after that lock is released, so file I/O never delays
    `submit()` or the status reads (W5). Routing a refused or transiently failed line to another model is NOT a
    switch (critic E10: a resumed film would otherwise log ~2 switches per refused line); it is covered by
    `translate.refused` (N lines refused by model X — one entry per bisection conclusion, no routing call) and by
    `translate.finished` carrying per-model line counts `{by_model: {label: n}}`; `translate.provider_down`
    (model, reason kind, pause minutes) and `translate.provider_up` (model) at `_open` / `_half_open`;
    `translate.deferred` / `translate.paused` (2 h) / `translate.resumed`; `translate.finished` (lines, by_model,
    kept_ja, duration) recorded in `_finish` AFTER its cancel check (L9d) — its text says the translation finished,
    never that the `.srt` was written (that is `subs.published`).
  - subtitles (plugin, at `_settle` — the ONLY subtitle producer, L9a): `subs.published` (srt name),
    `subs.skipped` (reason), `subs.failed` (detail). **Bulk settlements** (abort, no_exe, `_disable_subs`) write
    ONE `subs.skipped_bulk` {count, reason} instead of one entry per film (L8) — the bulk flag is set inside
    `_disable_subs` / `_abort` / the no_exe loop themselves (they are also reached from `_settle`'s three-failure
    path).
  - mirrored: `event.mirrored` — every event the supervisor actually delivers (or folds) is mirrored with its
    level, title and message (AC4).
- [ ] AC4 **Producers.**
  - Supervisor: an injected observer (from `build_supervisor`, agent only) mirrors every delivered/folded event as
    `event.mirrored` with the task type from `_Managed`. **Accepted duplication (L4):** for Lada/Jasna/avsubs an
    alert usually has a structured twin; the UI renders mirrored rows as compact secondary 「提醒」 rows (the
    original title only, details on expand) so the readable Chinese entries lead. **Per-day cap (L6/L13):** at
    most 500 mirrored entries per task per day. Append-only deltas, nothing superseded: when the cap is first hit
    one `event.suppressed {task, since_cap: true}` line ("mirrored alerts not logged for the rest of the day"); then
    at most one `event.suppressed {count}` line per hour carrying the count since the previous line, written when a
    suppressed event arrives, at the day rollover (as the OLD day's last line, inside the critical section, before
    the new day's first record — W2) and at `agent.stopping` (an unclean exit loses only the last partial count). At store start each task's mirrored count for today is rebuilt from today's file, so a restart
    does not reset the cap.
  - Launcher: `agent.started` / `agent.stopping`. Admin: `operator.*` before applying.
  - Lada: run-level entries always; per-file `restore.started/finished` ONLY in capture mode (lada's own
    per-file headers) — the folder-count inference is not used (L7).
  - Jasna / avsubs: launch (`_launch_locked` Popen), exit handling (failure/retry/requeue), the single successful
    publish, `_settle`, ASR start/poll, GPU wait/acquire edges, start/done/abort/interrupt, `task.error` at the
    setup/launch alert sites.
  - Translator: push on its own thread (the store is thread-safe; nothing is lost at Stop/done).
  - Durations come from monotonic clocks; `ts` is wall-clock with its UTC offset.
- [ ] AC5 **Retention.** Day files older than 30 days are deleted; if the folder still exceeds 50 MB the oldest days
  go first, but the most recent 7 days are never deleted (L6). A delete that fails because a reader holds the file
  (WinError 32 — critic E3) is retried at the next prune. Pruning runs at store start and at the first record of
  each new local day (no timer thread), AFTER the store lock is released (it never touches today's file, W5). Only
  `tasklog-*.jsonl` names are touched.
- [ ] AC6 **Loopback API** `GET /control/logs?day=YYYYMMDD&task=&severity=&q=&before=<id>&after=<id>&limit=`
  (limit clamped 1–500):
  - `day` given → that day's entries newest first, server-side filters (task exact; severity set; `q` =
    case-insensitive substring of film / model labels / title), `next_before` for paging back within the day.
  - `task` without `day` → that task's newest `limit` entries across days (the inline panel, L11a), searching back
    day by day until filled or 30 days.
  - `after=<id>` → entries newer than that id, **across the midnight boundary** (the rest of the id's day, then
    later days), oldest-first, for live polling (L11b).
  - `days` → available days, newest first, with counts; counts of closed days are cached (L11c) — a day counts as
    closed only once the store has written a record for a LATER day (the rollover delta lands in the old day at the
    first record of the new day, N4).
  - Reads stream the day file line by line, skip a trailing partial line, and merge the in-memory ring (by id)
    for records not in the file. The set of tasks present in each closed day is cached (with the counts), so the
    inline panel's `task` query skips days without that task (L18d). Not on the network (Hub) API.
- [ ] AC7 **UI 「日志」 page** (AgentConsole tab — replaces 「事件」: the log is a superset, every event is mirrored):
  day selector (今天 / 昨天 / the `days` list), filters (task, severity 信息/警告/错误), search (片名/模型),
  newest-first list (time with seconds, severity chip with text, task name + type, the rendered sentence, film),
  compact secondary rows for `event.mirrored`, expandable details (PID + process, exit code, output tail, model,
  counts, original alert text), live updates every 5 s while open (`after`), 「加载更早」 paging, empty state.
  **导出:** the whole filtered day (the UI pages through it, capped at 20 000 rows) as a `.txt` in the UI
  language, saved via an `<a download>` blob (the WebView2 default download flow on Windows — owner smoke checks
  it; L11d). The per-task inline panel shows that task's last 8 entries across days (「最近日志」). Design system:
  agent-console rules + a new `design-system/taskpaw-v3/pages/logs.md`; zh + en.
- [ ] AC8 Version 3.8.0 → 3.9.0; CHANGELOG; README; openclaw guide (the log is local, not forwarded); a design note
  that `emit` is no longer a plugin's only output (documented deviation from the V3 design §"emit 是插件唯一出口").
- [ ] AC9 Tests (fake children/translators; no real exes, network, keys or %APPDATA%): store (append, ids with a
  failed write + restart → no duplicate, `-900` < `-1000`, midnight rollover, prune age/size/7-day floor/locked
  file, write failure → memory + one callback outside the lock, lone surrogates in every field → API 200, never
  raises, memory-only without a data dir); API (filters, paging, `after` across midnight, `task` across days,
  days cache, clamp, not on the network app); producers (supervisor mirror + type + cap/suppressed; admin before
  apply; launcher; Lada capture on/off; Jasna restore start/finish on every publish path incl. Stop, fail/retry,
  GPU edges incl. Stop not logging acquired, task.error, interrupted, done/abort; avsubs ASR + settle + bulk;
  translator started-with-actual-model, switched on failover with reason, refused per bisection, down/up,
  deferred/paused/resumed, finished only when not cancelled, no per-request entries); a planted secret/argv/
  userinfo never appears in any record (tails exempt); UI (page, filters, search, paging, live across midnight,
  export, details, compact mirrored rows, inline panel across days, zh/en); conftest autouse reset of the holder.
  **Round 2:** unclean previous session → `previous_exit: "unclean"` + reconstructed interrupted entries (L10);
  suppressed deltas append-only, cap rebuilt after restart (L13); a resumed film with scattered refused lines logs
  no `switched`, a provider going down logs `unavailable` with its kind, recovery logs `recovered`, a Settings
  removal logs `changed` (L14); a slow writer never makes `after` polling skip (L15); Stop during a translation +
  a restore logs two interrupted entries, a just-exited child logs finished/failed not interrupted (L17); no
  duplicate ASR entries (L16); torn line repaired, boot id change reloads the UI, NaN in data → API 200, inline
  query skips days without the task (L18). **Round 3:** a failed port claim writes no tasklog line; the store is
  built after the reclaim (L19); closers per activity and the termination rule incl. first run, a session without
  its `agent.started`, and a session spanning days (L20); no-speech publishes no `asr.finished` (W1); rollover
  delta lands in the old day (W2); `at_stop` failure (W3); `unavailable` beats `recovered` (W4); `agent.stopping`
  precedes the supervisor stop (W5).

## Frozen issue contract

**In scope:** AC1–AC9. **User-visible changes allowed:** 「日志」 replaces 「事件」; the per-task inline panel shows
log entries; a `logs/` folder in the agent data dir; version 3.9.0.

**Invariants:** constitution §2 (no secrets in logs; control API loopback), §3 (timestamps carry their offset),
§4 (no silent except — a failed write is counted and alerted once; clean shutdown; no new threads), §5 (tests).
Alerts, dedupe, throttling, the Hub and OpenClaw forwarding unchanged (log entries are NOT events). Plugins'
scheduling, settlement, GPU lease and translation behaviour unchanged — logging is observation only (no routing
or probing to compute log fields).

**Corrections from repository evidence:**

| # | Issue text | Evidence | Correction |
|---|---|---|---|
| C1 | "every alert also appears in the log" | supervisor `_emit` is the single delivery path | mirror via an injected supervisor observer |
| C2 | Stop entries | `unregister` pops before `stop()`; emits during Stop are dropped | `operator.stop` by the admin; `task.interrupted` recorded directly by the plugin |
| C3 | "translator `drain_activity()`" | drain is pull-based and skipped at Stop/done/cancel | push on the translator thread |
| C4 | Lada per-file start/finish | boundaries exist only in capture mode; the folder-count heuristic misattributes | per-file only in capture mode |
| C5 | a separate page | every event is mirrored; Events lists oldest-first with no date | 「日志」 replaces 「事件」 |
| C6 | daily rotation | Windows cannot delete an open file | open-append-close per record; delete whole old days, retry locked ones |
| C7 | `<data dir>/activity/activity-*.jsonl` | "activity" names dev-agent activity here | `<data dir>/logs/tasklog-<YYYYMMDD>.jsonl`, module `core/tasklog.py` |
| C8 | `/control/activity?since=&until=` | a 30-day timeline is browsed a day at a time | `/control/logs?day=` + `after` for live + `task` across days |
| C9 | "failover names both models" | the model used is chosen per request in `_call` | `translate.switched {from, to, reason}` in `_call` |

**Non-goals (OUT-OF-SCOPE):** Hub or phone delivery; changing alerts; log shipping; replacing the technical
backend log; the Hub's own Events tab; per-request translator logging.

**Causal boundary:** `core/tasklog.py` (new), `monitors/supervisor.py` (observer hook), `monitors/runtime.py`
(inject), `agent/server/admin.py`, `agent/server/app.py`, `agent/server/launcher.py`, `monitors/plugins/lada.py`,
`jasna.py`, `avsubs.py`, `monitors/subs/translate.py`, `ui/src/views/AgentConsole.tsx`,
`ui/src/components/TaskLog.tsx` (new, + helpers), `ui/src/api.ts`, `ui/src/i18n.ts`,
`design-system/taskpaw-v3/pages/logs.md` (new), `tests/conftest.py`, tests, README, guide, CHANGELOG, V3 design
note, version files, this doc.

## Assumptions

| # | Claim | Basis | Risk if wrong |
|---|---|---|---|
| A1 | structured entries are ~10 per film (~400 B each); mirrored entries capped at 500/task/day | design caps | size cap prunes oldest days, never the last 7 |
| A2 | one short append without fsync costs < 1 ms locally | VERIFIED (critic E1: p99 0.28 ms; 8 threads p99 2.2 ms incl. lock wait) | — |
| A3 | antivirus/indexer locks are transient | common Windows behaviour | a lost line is counted + one alert; the ring keeps it for the session |
| A4 | WebView2 saves an `<a download>` blob to the Downloads folder | wry sets no handler → WebView2 default (critic L11d, unverified at runtime) | owner smoke; fallback: copy-to-clipboard |
| A5 | in capture mode, Lada prints a per-file header for each file and an error line for a failed one | lada.py output parsing; behaviour after a failed file unverified offline | a failed file could read as 完成 when the next header arrives; an error line seen for the current file records `restore.failed` |

## Handoff notes
Implementation by Codex (restricted executor; the driver inspects diffs, reruns checks, commits). Pilot split:
**A** store + holder + supervisor observer + admin/launcher entries + API + conftest (first; its API and record
shape are the contract) → **B** producers (plugins + translator) ∥ **C** UI. Never touch the owner's live agent,
config or library; no network; no real keys/exes; no WhisperJAV/GPU; never open the repo-root `.env`.
