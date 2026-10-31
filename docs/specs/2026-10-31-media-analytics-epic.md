# EPIC — 媒体处理中心 · 数据分析 (media analytics page)

Date: 2026-10-31 · Base: `51eca25` (main, after EPIC #375) · Branch: `afk/analytics-epic`
Planner: Claude (tech-lead plan only; no product code).
Revision 2 (after debate rounds 1–2): see §17 Revision log.
Approved UI: [media-analytics-v1.html](../../design-system/taskpaw-v3/preview/media-analytics-v1.html)
(operator approved 2026-10-31). Design rules: [MASTER.md](../../design-system/taskpaw-v3/MASTER.md),
[pages/media-workflow.md](../../design-system/taskpaw-v3/pages/media-workflow.md),
[constitution](../constitution.md).

Language: this document and all code, comments and commits are English; every
operator-facing string is Chinese, as in the preview.

## 1. Spec review

The operator wants a new page **数据分析** in the 媒体处理中心 rail that reports what the
media pipeline has *done*: films archived per period (by resolution), each GPU
machine's output, restore times and energy, the controller's post-processing (ASR,
translation, scraping, archiving), stage duration distributions, estimated energy,
device availability (online / 停领 / offline), failures and warnings, and what the
incoming films look like (INBOX disk, input size, length, actors archived into, marks).
Filters: time range (24h / 7d / 30d / 90d / 全部), machine (each GPU worker, grouped by
GPU model, plus 本机), resolution, and 对比上一周期. Every chart has a tooltip and a
table view.

Core need: a fast, honest, year-scale history view built from data the controller
already stores, plus the two facts it does not store yet (device availability and
translated line counts), which must start accruing as soon as possible because history
cannot be reconstructed later.

The preview is intent, not markup: its demo data generator, scenario bar and fake
"自动刷新 · 每 30 秒" ticker are not product. Where the preview shows something the
system does not record, this document says so and picks a safe default (§3).

## 2. Operator decisions (binding)

| # | Decision |
|---|---|
| OD1 | **No overall progress vs total and no ETA anywhere.** The page reports only what happened. The backlog is operator-throttled (INBOX drive 1 TB); the library is 5000+ films over ~a year. |
| OD2 | Queries stay fast at year scale (≈5000+ archived films, many attempts). |
| OD3 | Fleet: 本机 controller (RTX 5060 8 GB, does not restore now) + GPU workers (5060 Ti 16 GB ×N, one 5070 Ti 16 GB; more may join; machines can leave or be in repair). Machine identity is by **name**, never by colour. A machine that joins mid-period shows 「新加入」. |
| OD4 | Energy values are estimates and are labelled 「估算」. |
| OD5 | Settings live in the UI, not YAML; home LAN, single user; no over-engineering; no new dependency unless unavoidable — **no chart library**, hand-rolled SVG as in the preview. |
| OD6 | Live refresh (EPIC #375): React Query has no global `refetchInterval`; analytics queries declare an explicit, modest interval or refresh on focus, with a justification. |
| OD7 | Dark theme only; tokens from `ui/src/theme.ts` / `mediaStyles.ts`; the validated categorical palette and 4K = blue / 1080P = orange from the preview. |

## 3. Defaults chosen for decisions the operator has not made

Each is the **recommended safe default**; the operator may overturn any of them
without changing the architecture.

| # | Open question | Default (recommended) | Why |
|---|---|---|---|
| D1 | Where to store device-availability history | A separate small SQLite file **`analytics.db`** beside `workflow.db` in the workflow data dir. `workflow.db` stays at schema v4. | `workflow.db` validates an exact DDL allowlist (`schema.py` `validate_schema`); any new table means `SCHEMA_VERSION` 5, a pre-migration backup of a ~1 GB file and a one-way upgrade older builds refuse. A sidecar file has none of that, and an older build simply ignores it. Precedent: `library.db`, `power.db`. |
| D2 | Where aggregation happens | **Backend sends per-film fact rows; the browser aggregates** (bucketing, filters, stats), as the preview's JS does. The backend caches immutable facts per archive receipt and reads only new receipts. | Filters stay instant; one aggregation implementation (TypeScript) instead of Python + TS; measured cost and payload fit the budget (§5). |
| D3 | Refresh policy | `refetchInterval: 60_000` on the one analytics query, plus TanStack's default refetch when the document becomes visible again; label 「每分钟自动刷新」 (the preview's "每 30 秒" was a demo ticker). | Archives land ~16/day (measured); device states change a few times a day. 60 s keeps 「最后刷新」 honest at one cheap incremental read. TanStack pauses the interval only while the **document is hidden** (its `focusManager` listens to `visibilitychange`, `refetchIntervalInBackground` false — C4 #375 §A1); a visible but unfocused window keeps polling once a minute, which is acceptable at this cost. |
| D4 | Duration definitions | 修复 = the succeeded restore attempt's `ended_at − started_at`; 排队等待 = restore's first attempt start − plan confirmation (the board's existing `queue_wait`, `timing.film_timing`; none for backfilled plans); 全程 = archive receipt `accepted_at` − film `created_at` (入库 → 归档, as the preview labels it). ASR / 翻译 / 刮削 / 归档 = each succeeded attempt's duration. | Same numbers the board already shows; 全程 matches the preview text. |
| D5 | Attempts that end without a film or machine fault | Typed by the attempt's **last evidence kind**: `exec_interrupted` → warning 「运行中断」; `exec_failed:cancelled` (停止并重新排队, 判定已停止, a local stop) → warning 「已停止」; `exec_failed:transient` → warning 「翻译暂停」 when the stage is translate and the note starts 「翻译暂停」, else warning 「临时失败（自动重试）」. None counts in 失败率. | Restarts, operator stops and automatic retries are not failures of the film or the machine; `interrupted`, `cancelled` and `machine` are the dispatcher's free retries (`local_dispatch._FREE_RETRY`), `transient` retries automatically. `machine` stays a failure (a machine fault the operator must fix). |
| D6 | 「主控 CPU 高占用」 warnings | **Excluded**, and the preview's 「隐藏主控 CPU 高占用」 switch is dropped. | They are `host_metrics` monitor alerts owned by the Hub's 事件 tab (`monitors/plugins/host_metrics.py`), stored in the Hub's monitoring database, not workflow facts; the preview hid them by default anyway. |
| D7 | 翻译服务中断 strip source | The agent's task log (`translate.provider_down` / `translate.provider_up` / `translate.paused` of task `workflow-translate`), which keeps **at most 30 days and at most 50 MB** (`core/tasklog.py` prune, `MAX_BYTES`); the covered start is the oldest day actually scanned, and longer ranges show 「翻译服务中断记录从 MM-DD 起」 over the uncovered part. Read by kind per day, closed days cached (§6.4). | No durable source exists; adding one means a hook inside `monitors/subs/translate.py`, which the stand-alone subtitle monitor shares. Revisit only if the operator wants year-scale outage history. |
| D8 | Translation line totals and model share | Line totals come from a new `item_count` on the translate success evidence (C1), counted from the rollout date; the 「deepseek-flash 100%」 model-share chip is **omitted**. | Neither is stored today (only the task log has them, 30 days). `item_count` is an existing optional `EvidenceSummary` field, so no schema change; model share would need a new evidence field that strict older builds cannot read back. |
| D9 | INBOX card projection 「约 1 天后可再放入约 N 部」 | **Dropped.** Kept: 已用 / 待处理 / 其他 / **可用** (free space is labelled 「可用」, never 「剩余」), 平均每天腾出 (last 7 days, historical), 现在还可放入约 N 部 (估算, from free space and the 7-day average size). | OD1 forbids forecasts; "now" capacity is a current fact divided by a historical average, labelled 估算. 「可用」 keeps the page free of 「剩余」, which the OD1 guard test bans. |
| D10 | Hub read-only workspace | 数据分析 shows the existing `ControllerOnly` notice there, like 片库与规则 and 设置. | The analytics route is controller-local; no Hub proxy. |
| D11 | GPU model of a machine | The worker's **latest probed capability** (`gpu_model` + `vram_mib`, e.g. "RTX 5060 Ti 16GB"); history after a GPU swap is grouped under the new model. 本机 appears in the GPU table only when it restored a film in the selected range (it did 6 restores on 2026-10-20…22). | Simple; one source of truth already shown on 设备. |
| D12 | 失败率 | failures ÷ (failures + archived films) in the range, as the preview computes it; "failures" = `severity: fail` only (D5 warnings excluded, but a transient attempt whose retries are exhausted counts as a failure, §7.3). | Matches the approved tile without counting restarts, stops and automatic retries that recovered. |
| D13 | Device history retention | Keep all rows (~13k rows / ~1.5 MB per year, §5). | Year-scale is small; no prune job. |
| D14 | 「新加入」 | `first_seen` = MIN(`worker_boots.observed_at`) over the worker's boots. With `effective_start = max(range start, coverage.archives)` and `length = now − effective_start` (for 全部 the range start is `coverage.archives`, so `effective_start = coverage.archives`), a machine is 「新加入」 iff `first_seen > effective_start + 10% × length`. Otherwise it is a normal machine. 新加入 machines are excluded **only** from the same-model average tick; 各机型实测产能 keeps them because it is already normalised per online (or present) day. | Cheap (59 rows today). The 10 % margin stops machines that were present from the start of the data looking new. `observed_at` is rewritten when the same boot re-posts (`worker_store.py` record_boot `UPDATE worker_boots SET observed_at=…`), so MIN can drift later by at most the first boot's lifetime — acceptable against a 10 % margin. |
| D16 | Failure burst banner (preview's red notice) | A burst = ≥ 3 `machine_fault` failures on one machine within any 1 h window in the range; its rows are always highlighted in the event list. The red notice (machine, count, time span) shows only when the burst ends within the last 24 h or any of its rows has outcome `needs_action`. | Keeps the approved banner with a testable rule, without a red alarm for an old, already-recovered burst in a 90 d / 全部 view. |
| D15 | Filter state | Kept in page state only (not persisted). | Single user; no storage surface to test. |

## 4. Evidence (read-only)

All database facts below come from the operator's live `workflow.db` opened strictly as
`sqlite3.connect('file:…/workflow.db?mode=ro', uri=True)` on 2026-10-31, and from a
synthetic copy scaled ×45 built in a scratch directory from those rows (ids suffixed).
No server was started; no other data was touched.

| Fact | Value |
|---|---|
| `workflow.db` | 1.06 GB + 9.4 MB WAL, `user_version` 4. Size is the intake audit backlog that #376 C1 prunes (`audit` 845,848 rows, of which 782k `observe_intake` / `mark_intake_scan_seen`). |
| Films | 150 rows, 134 live (`removed_at` null). |
| Attempts | 932; first `started_at` 2026-10-20T17:39Z. Restore: 112 succeeded, 34 failed, 4 running. Every other stage: no failures except 1 scrape. |
| Archive receipts | 111; first `accepted_at` **2026-10-24T01:59Z** (the 数据记录起点 for archived-film metrics). ≈16 archives/day over the last week. |
| Attempt energy | `attempts.body_json.evidence[-1].summary.energy` (`wh`, `gpu_wh`, `cpu_wh`, `other_wh`, `seconds`, `avg_w`) on restore, asr, translate, return and scrape attempts since **2026-10-22**; archive and cleanup attempts carry none. 21 of 34 failed restores carry energy. |
| Restore timing | `summary.timing` (`wall`, `ltx`, `ltx_stages`, …) on every succeeded restore since 2026-10-22. |
| Probes | `manifests(kind='input').body_json.readiness.media_probes[]` (`width`, `height`, `duration_ms`) on all 152 input manifests; live heights: 52 × 2160, 59 × 1080. |
| Marks | `manifests(kind='final').body_json.postprocess.actual_marks` since 2026-10-24 (`chinese_subtitles`, `uncensored`, `4k`, `restored`). |
| Failure classes | Failed evidence kind keeps the class: `exec_failed:needs_attention` 23, `exec_interrupted` 11 (`executors.py` D18). `error_code` is null on all. Notes: 15 × "[WinError 4551]", 4 × "Jasna LTX 失败（退出码 1）", 2 × "找不到 ffprobe", 2 × "缺少 LTX 模型文件", 11 × "上一次运行被中断", 1 scrape "白名单以外的文件". |
| Subtitle source | ASR success note is `ASR_DONE` = 「字幕来源：<decision> <n>，…」 over the fixed `SubtitleFacts.decision` set `reused_zh` / `source_ja` / `generated_ja` / `no_speech` (`exec_asr.py` 232-235, `stage_results.py` 63). Live: 100 × `generated_ja 1`, 10 × `reused_zh 1`, 1 × `no_speech 1`, 1 × `generated_ja 2，no_speech 1`. Translate notes: 101 「翻译完成」, 11 「无需翻译」 — so 无需翻译 is mostly 已有中文字幕, not 无语音. |
| Last evidence kinds beyond failures | `exec_failed:cancelled` from 停止并重新排队 (`route_store.py` 311), 判定已停止 (`operator_actions.py` 230) and a local stop (`local_dispatch.py` 205); `exec_failed:transient` from translate pauses/failures (`exec_translate.py` 240-243) and `PermissionError` (`executors.classify_exception`). None in the live DB yet. |
| Global pause reaches workers | The heartbeat reply carries the controller's `paused` (`agent/server/worker_api.py` 528) and the worker stops claiming (`remote_dispatch.py` 513). Claims also need a compatible protocol (`FleetLive.claim_admission_error`). |
| Machines | `workers`: 6 rows (本机 + 5 GPU workers), all verified/enabled. `capabilities(kind='probed')` holds `gpu_model` ("NVIDIA GeForce RTX 5060 Ti" …) and `vram_mib`. No registration timestamp on `workers`; `worker_boots.observed_at` (59 rows) gives first presence. |
| Stage placement | asr and translate attempts carry the 本机 worker id; return / scrape / archive have `worker_id` null (controller components); restore carries the GPU worker. |
| Archive targets | Receipt `target.native_identity.scheme`: 93 `export_object_id`, 11 `library_performer`, 6 `manual_directory`, 1 `library_category`; `relative_path` is the film folder only (depth 1), so the actor folder is the target's parent (`library_history.py` builds `target_path` the same way). `library.db`: 511 performers, 12 `archive_tasks`, 0 second targets. |
| Heartbeats / 停领 | In memory only (`fleet_live.py`: "A heartbeat goes into `FleetLive` and nothing else"). 失联 after 60 s (`scheduler.OFFLINE_SECONDS`). 停领 is `workers.admission_state='draining'` (manual, or automatic after a machine fault, `remote_attempts._hold`), or the worker's own 暂停领取 (`Heartbeat.paused`, persisted on the worker). The controller-wide 暂停新领取 is `LocalDispatcher.paused`. |
| Other histories | Task log: daily files, ≤ **30 days and ≤ 50 MB** (`core/tasklog.py` prune, `MAX_BYTES`); `TaskLog.query` returns at most 500 rows and, with `task=`, walks at most 30 days under the log's lock (499, 515-545). Hub `status_log`: successful polls only, 7 days. `power.db`: per machine, 15-min buckets, 35 days, local machine only. None is a year-scale source. |
| Audit as a source | Rejected: a cold scan for `register_worker`/`update_worker` took **5.2 s** on the live file; `update_worker` holds only the 6 registrations although admission changed since. |
| Fact query, live | 111 archived films: 6 ms warm (32 ms cold); 709 attempts of their revisions: 6 ms. |
| Fact query, ×45 synthetic | 4,995 archived films / 41,940 attempts / 620 MB file: film facts **326 ms**, all attempt facts **386 ms** (≈0.71 s full read, under the store lock). Components: failed attempts all-time with JSON 60 ms; probe `json_each` 157 ms; input bytes 60 ms; final marks 50 ms; receipts 52 ms; receipts newer than a rowid high-water mark **0.0 ms**. |
| Payload | Verbose prototype 970 B/film (4.8 MB at 5,000). Planned shape (§7) with real 36-char worker ids and `[worker, wh, seconds]` energy entries ≈ **510 B/film** → ≈ 2.6 MB at 5,000 films for 全部; ≈ 115 KB for 7 d + previous period. |

Evidence commands (all read-only or scratch-only):
`git log`, `rg`/`grep`/`sed` over `taskpaw_v3/`; Python with `-I` against the
read-only URI above: table row counts and body sizes; `audit` kinds and ranges;
attempt evidence shapes per stage; failure note/kind distribution; manifest and
receipt shapes; capability probe fields; coverage start dates; prototype fact SQL with
timings; a scratch `scaled.db` built ×45 from those rows and timed with the same SQL;
`library.db` opened read-only for counts.

## 5. Data-source map

"Available" = in storage today; "derivable" = computed from stored rows; "new" = needs
the recording added by C1. Coverage start is the earliest live row.

| Panel / metric | Source | Status | Coverage from |
|---|---|---|---|
| 完成影片 (count, 4K/1080P split), 产量趋势 buckets | archive `receipts.accepted_at` + input probes max width/height → `board.resolution` | derivable | 2026-10-24 |
| 平均全程时长 | receipt `accepted_at` − `films.created_at` | derivable | 2026-10-24 |
| 平均修复时长 / per resolution / min / max | succeeded restore attempt `started_at`/`ended_at` | derivable | 2026-10-20 (attempts); used via archived films |
| 修复机器 (per film) | succeeded restore attempt `worker_id` | available | 2026-10-20 |
| 平均功率 per machine | Σ restore `energy.wh` × 3600 ÷ Σ `energy.seconds` per worker over every restore attempt with energy (failed included), so a machine with only failed attempts in range still has a value | derivable | 2026-10-22 |
| 用电 (估算): per bucket, per resolution, per machine, top 5 films | attempt `energy.wh` (restore → its worker; asr/translate/return/scrape → 本机) | derivable; films before 10-22 or with a crashed attempt are 「部分」 | 2026-10-22 |
| 修复效率 | restore seconds ÷ probe duration | derivable | 2026-10-20 |
| 各机型实测产能 | films per machine per online day; online time from device spans where covered, else calendar days present | derivable + new | spans from C1 rollout |
| GPU model grouping | latest `capabilities(kind='probed')` `gpu_model`, `vram_mib` | available | current |
| 「新加入」 | MIN `worker_boots.observed_at` per worker, D14 rule | available | 2026-10-20 |
| ASR count, avg, max | succeeded asr attempts | derivable | 2026-10-20 |
| 翻译 films translated in total | translate success note "翻译完成：N 卷" vs "无需翻译" | derivable | 2026-10-20 |
| 「N 片已有中文字幕」 / 「N 片无语音」 | ASR success note decision counts (`reused_zh`, `no_speech`; a film counts once per decision present) | derivable | 2026-10-20 |
| 翻译 line totals | translate success `summary.item_count` | **new (C1)** | C1 rollout |
| 刮削 / 归档 durations | succeeded scrape / archive attempts | derivable | 2026-10-24 |
| 归档去向 (主片库 / 分类 / 新演员 / 手动 / 第二目标) | receipt target scheme (`board.TARGET_KINDS`) + `library.db` `archive_tasks` second target | derivable | 2026-10-24 (second target since #361) |
| 翻译服务中断 strip | task log `translate.provider_down/up/paused` | available, ≤ 30 days / ≤ 50 MB rolling | oldest task-log day scanned |
| 耗时分布 (排队, 修复 4K/1080P, 全程, ASR, 翻译, 刮削, 归档; min/p25/median/p75/max/avg) | as above | derivable | 2026-10-24 |
| 一部片的时间都花在哪 | per-stage averages of the above | derivable | 2026-10-24 |
| 设备在线 lanes, 在线率, 离线/停领合计 | device spans | **new (C1)** | C1 rollout |
| GPU 在线 now (KPI, chips) | `FleetLive` state + `workers.admission_state` + `Heartbeat.paused` (same derivation as the recorder) | available (live) | — |
| 失败 by type / machine, event list | failed attempts: last evidence kind + note classifier (§7.3); outcome from the job's later attempts | derivable | 2026-10-20 |
| 警告: 设备离线 | offline device spans | new (C1) | C1 rollout |
| 警告: 运行中断 / 已停止 / 翻译暂停 / 临时失败 | `exec_interrupted`, `exec_failed:cancelled`, `exec_failed:transient` attempts (D5) | derivable | 2026-10-20 |
| 警告: 翻译服务中断 | task log | available, ≤ 30 days | oldest day scanned |
| INBOX meter: 已用 / 可用 / total | `shutil.disk_usage(inbox_root)` at request time | available (live) | — |
| INBOX 待处理影片占用 | Σ input manifest item bytes of live films whose `cleanup_input` stage has not succeeded | derivable (live) | — |
| 平均每天腾出, 平均每部 | Σ / avg main-video bytes of films archived in the last 7 days | derivable | — |
| 输入大小 distribution | Σ `main_video` `manifest_items.size_bytes` of the input revision | derivable | 2026-10-24 |
| 片长 distribution | Σ probe `duration_ms` | derivable | 2026-10-24 |
| 归档最多的演员 | actor folder of performer / new-actor targets (parent of the target path; label from `library.db` performer when matched, else folder name) | derivable | 2026-10-24 |
| 标识占比 | final `postprocess.actual_marks` | derivable | 2026-10-24 |
| 主控 CPU 高占用 | Hub monitor events | **excluded (D6)** | — |
| Model share chip | — | **omitted (D8)** | — |

## 6. Approach

### 6.1 Storage: `analytics.db` (D1), workflow.db untouched

`workflow.db` keeps schema version 4: no table, index or column changes, so
`schema.py` migration discipline is not engaged. The new file follows the
`library_store.py` pattern: one connection, one lock, version read before DDL, the
local-path rule (`schema._path` — UNC/network paths refused), WAL with
`synchronous=NORMAL` (losing the last tick on power loss is acceptable for a history
log).

```sql
-- analytics.db, PRAGMA application_id = 0x54504153 ('TPAS'), PRAGMA user_version = 1
CREATE TABLE device_spans (
    span_id INTEGER PRIMARY KEY AUTOINCREMENT,
    worker_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('online','paused','offline')),
    reason TEXT NOT NULL DEFAULT '' CHECK(reason IN ('','admission','worker','global','version')),
    started_at TEXT NOT NULL,      -- UTC ISO, controller clock (rt.clock.now())
    last_seen_at TEXT NOT NULL,    -- the latest tick that confirmed this state
    closed INTEGER NOT NULL DEFAULT 0 CHECK(closed IN (0,1))
);
CREATE UNIQUE INDEX one_open_span_per_worker ON device_spans(worker_id) WHERE closed=0;
CREATE INDEX device_spans_seen ON device_spans(last_seen_at);
```

Open rules: empty file → create the above and set both pragmas in one transaction;
`application_id` + `user_version` = 1 with the exact DDL → use; anything else (other
id, newer version, damaged) → the recorder stays off with a stable reason, the file is
never modified, and the analytics response reports `devices.available=false` with that
reason. A future version bump follows the same "known older version → one transaction
upgrade" rule as `library_store.py`.

Row estimate: one row per state change plus one per lane per controller restart.
Seven lanes × ~5 rows/day ≈ 35 rows/day ≈ **13k rows/year ≈ 1.5 MB**. The per-tick
`last_seen_at` refresh is one small transaction every 30 s.

### 6.2 Device state recorder

`DeviceHistory` (new `workflow/device_history.py`) runs one daemon thread on the
controller (`settings.role == "controller"`), started by `LocalWorkflowHost._boot` once
the runtime, dispatcher and (when remote workers are on) `FleetLive` exist, and stopped
and joined in `stop()` before the runtime closes (constitution §4).

Tick: every 30 s on a `time.monotonic()` schedule. `now = rt.clock.now()` (the same
clock `FleetLive` measures with). One `list_workers` read under the store lock, then one
`analytics.db` transaction.

Lane state derivation (a pure function shared with the analytics route's "state now"):

| Lane | Condition (first match) | state / reason |
|---|---|---|
| 本机 (controller worker id) | dispatcher `paused` | `paused` / `global` |
| 本机 | otherwise (the recorder is running, so the controller is) | `online` |
| GPU worker | revoked, retired, not verified, or remote workers off | not recorded; an open span is closed |
| GPU worker | `FleetLive.state` = `waiting` (first 60 s after controller start) | no write (unknown) |
| GPU worker | `FleetLive.state` = `lost` | `offline` |
| GPU worker | `admission_state` in (`draining`, `disabled`) | `paused` / `admission` |
| GPU worker | heartbeat `paused` (the worker's own 暂停领取) | `paused` / `worker` |
| GPU worker | dispatcher `paused` (controller-wide 暂停新领取; the heartbeat reply carries it to every worker, `worker_api.py` 528 → `remote_dispatch.py` 513) | `paused` / `global` |
| GPU worker | `FleetLive.version_ok(beat)` false (incompatible workflow protocol; claims refused) | `paused` / `version` |
| GPU worker | otherwise | `online` |

The `reason` CHECK therefore allows `'' | admission | worker | global | version`.

Write rule per lane: an open span with the same state and reason → `last_seen_at=now`;
different → close it (`closed=1`, `last_seen_at=now`) and insert a new span starting
`now`. At recorder start every open span is closed **without** moving `last_seen_at`, so
the time the controller was not running reads as a gap. The same applies when a tick
finds that more than 3 × 30 s passed since the previous tick by **either** clock — the
`time.monotonic()` delta or the `rt.clock.now()` wall delta (the process was
suspended, the machine slept, the thread stalled, or the wall clock jumped): open spans are closed without
moving `last_seen_at` and new spans start at `now`, so the stall shows as a gap rather
than as continuous state. Precision: one tick (30 s) plus the 60 s 失联 threshold;
documented in the panel's tooltip.

Honest gaps in the UI (§8.6): before the first span (早于数据记录起点), between a closed
span's `last_seen_at` and the next span of that lane (主控未运行或未收到心跳，无记录),
and the `waiting` window. 在线率 = (online + paused) ÷ recorded time; the unrecorded
time is shown separately, never counted as online or offline.

Failure handling: any `Exception` raised inside a tick (database, store read, an
unexpected state) is caught at the tick boundary, logged with its type (rate-limited to
one line per exception type per hour) and the next tick retries; nothing propagates
into the host and the thread never dies of it. The recorder never blocks the dispatcher beyond its single `list_workers` read.

### 6.3 Translated line counts

`TranslateExecutor._run` already holds every translated volume's cues. Its success
outcome carries `item_count` = Σ cues written to the `.zh.srt` files (null for 「无需翻译」);
`ExecOutcome` gains an optional `item_count`, `LocalRuntime.evidence` passes it to
`EvidenceSummary.item_count` (an existing optional field, so the stored shape and every
reader are unchanged). Only the controller's local dispatcher runs translate (measured:
all translate attempts carry the 本机 worker), so no worker protocol changes.

### 6.4 Analytics read (backend)

New module `workflow/analytics.py`, new store reads in `read_store.py`, new route file
`agent/server/analytics_routes.py`.

- **Per-receipt fact cache.** `AnalyticsFacts` lives on the workflow host. A fact is the
  film as archived by one accepted archive receipt: the receipt's final manifest names
  the input revision, whose stages and attempts give every number in §7. Once the
  receipt exists those rows no longer change, so a fact is computed once and kept in
  memory keyed by the receipt's rowid. Each request first reads receipts with rowid
  above the high-water mark (0.0 ms when nothing is new) in **batches of ≤ 200
  receipts per locked read**, releasing the store lock between batches; the cold build
  after a restart is ~25 batches at 5,000 films (≈ 30 ms each, measured total 0.71 s).
  One cache lock (separate from the store lock) wraps the whole incremental update:
  a concurrent request waits for the update in progress and then reads the cache, so
  no batch is ever read twice.
  A film removed later still counts: the archive happened. A film re-archived on a new
  revision yields a second fact (a second archive happened).
- **Failures** are read per request (they mutate in place from `running` to `failed`,
  so a rowid watermark does not apply): `status='failed'` attempts of
  restore / asr / translate / return / scrape / archive with `ended_at ≥ since`; JSON is
  extracted only for matching rows (60 ms all-time at 5,000 films). Typing reads the
  attempt's **last** evidence kind (`evidence[-1].kind`: `exec_failed:<class>`,
  `exec_interrupted`) and then the note (§7.3); outcome from the job's later attempts
  and current stage status.
- **Subtitle source** per fact: the ASR success note parsed against the fixed
  decision set (`ASR_DONE` format); an unparseable note yields empty counts.
- **Device spans** from `DeviceHistory.spans(since)`; **machines** from `list_workers`
  + latest probed capability + MIN boot time; **INBOX** from `disk_usage` + one store
  read.
- **Outages** from the task log, never through `TaskLog.query(task=…)` (500-row cap,
  30-day walk under one lock). A new small `TaskLog` read returns rows of given kinds
  from one day file, starting at a byte offset and returning the offset reached. The
  file is append-only, so it is read **outside** `TaskLog._lock`; a torn last line is
  not consumed (the offset stops before it) and is read again next time, the same
  tolerance `_read_day` applies. The lock is taken only briefly to copy matching rows
  of that day still held in the in-memory ring (not yet flushed). `analytics.py`
  keeps closed days (before today) in memory after their first scan, and for today
  keeps the offset and reads only the bytes appended since the last request; a day
  pruned from disk drops out of the cache, a changed day name (midnight) closes the
  previous day. `coverage.outages` = the oldest day actually scanned. Expected cost
  (estimate, live log not read): ≤ 50 MB of JSONL parsed once after a restart, none of
  it under the lock; then only the new bytes of today per request.
- Archive target kind and actor folder come from the receipt target; the second target
  and the engine's `target_path` come from `library.db` `task_receipts()` (as
  `library_history.py` does). Without the library (it failed to start) the fact still
  builds with `second=false` and the actor taken from `export_identity`.

### 6.5 Why not the alternatives

- *Server-side buckets per filter.* Every filter click would refetch; bucket/compare/
  quantile logic would live in Python and be re-rendered in TS; more endpoints or
  params to test. Rejected for D2.
- *Reuse `film_board` / `energy_history`.* They read only live, current-revision films
  with full stage objects and validate a pydantic model per film (board-sized, not
  year-sized), and hold the lock for the whole read. The fact read reuses their SQL
  idioms (`_probe_sizes`, `_attempt_energies`) but returns plain rows.
- *A v5 table in `workflow.db`.* See D1.
- *Audit table as history.* 5.2 s cold scan; admission changes are not audited
  consistently (§4).

## 7. API contract

`GET /control/workflow/analytics?since=<ISO 8601 with offset>` — loopback control API,
Bearer-gated like every `/control/*` route, **controller only**.

| Status | When | Body |
|---|---|---|
| 200 | success | below |
| 409 | `settings.role != "controller"` | `{"detail": "数据分析只在主控上"}` |
| 422 | `since` not ISO 8601 or without an offset | FastAPI validation body |
| 503 | workflow host off / not running (`workflow_reads.require_host`) | `{"detail": <reason>}` |
| 503 | store error (`workflow_reads._mapped`) | `{"detail": "媒体流水线暂不可用：<code>"}` |

The 200 body is returned as `JSONResponse(body)` (the convention in
`agent/server/app.py`): the route builds plain dicts/lists/str/int/float only, a
returned `Response` skips FastAPI's `jsonable_encoder` walk over ~5,000 rows, and
`JSONResponse` keeps `allow_nan=False`, so a NaN/inf that slipped into a number fails
loudly instead of producing invalid JSON.

`since` omitted = all history. The UI sends `since = range start − range length` (so
对比上一周期 needs no refetch), floored to the bucket start so the query key is stable.

```jsonc
{
  "server_time": "2026-10-31T07:00:00Z",
  "since": "2026-10-17T15:00:00Z",          // echo, null for all history
  "coverage": {                               // earliest record of each source; null = none yet
    "archives": "2026-10-24T01:59:01Z",
    "energy":   "2026-10-22T10:23:10Z",
    "cues":     null,                         // first translate success with item_count
    "devices":  null,                         // first device span
    "outages":  "2026-10-01T00:00:00Z"        // start of the oldest task-log day actually scanned
  },
  "machines": [{
    "id": "<worker_id>", "name": "Snowleopard", "kind": "gpu",       // "controller" for 本机
    "model": "RTX 5070 Ti 16GB",            // from the latest probe; null if never probed
    "first_seen": "2026-10-24T16:27:40Z",   // earliest worker_boots.observed_at
    "retired": false,
    "state_now": "online",                  // online | paused | offline | unknown | off
    "state_reason": "",                     // '' | admission | worker | global | version
    "state_since": "2026-10-31T06:10:00Z"   // open span start, null without one
  }],
  "films": [{                                // one per archive receipt with accepted_at >= since
    "code": "ABC-123",                       // films.display_name
    "archived_at": "…Z", "created_at": "…Z",
    "res": "4K",                             // "4K" | "1080p" | null
    "duration_s": 7260, "input_bytes": 12345678901,
    "worker": "<worker_id>|null",            // succeeded restore's worker; null = no restore
    "queue_s": 53000, "restore_s": 14000,    // null when not applicable
    "asr_s": 540, "translate_s": 52, "translated": true, "cues": 1834,
    "subtitles": {"generated_ja": 1},        // ASR decision counts: reused_zh | source_ja | generated_ja | no_speech; {} if unknown
    "scrape_s": 178, "archive_s": 40, "lead_s": 120000,
    "restore_wh": [["<worker_id>", 1234.5, 14100.0]], // [worker, Σwh, Σenergy seconds] per worker, failed attempts included
    "controller_wh": 61.2, "energy_partial": false,
    "target": "performer",                   // performer | category | new_actor | manual
    "actor": "Example Actor",                // null unless performer / new_actor
    "second": false,
    "marks": ["chinese_subtitles", "4k"]
  }],
  "failures": [{
    "at": "…Z", "code": "ABC-123", "stage": "restore",
    "machine": "<worker_id>",                // controller id for worker-less stages
    "severity": "fail",                      // fail | warn (§7.3; warn never counts in 失败率)
    "type": "machine_fault",                 // §7.3
    "exhausted": false,                      // true: transient type whose retries are used up (§7.3)
    "note": "…",                             // ≤ 200 chars of the attempt note
    "outcome": "reassigned"                  // retried_ok | reassigned | retrying | needs_action
  }],
  "devices": {
    "available": true, "reason": "",
    "spans": [{"machine": "<worker_id>", "state": "offline", "reason": "",
               "start": "…Z", "end": "…Z", "open": false}]
  },
  "outages": [{"start": "…Z", "end": "…Z|null", "paused": false}],
  "inbox": {                                 // null without an inbox root
    "total_bytes": 1000000000000, "free_bytes": 0, "used_bytes": 0,
    "pending_bytes": 0, "pending_films": 0,
    "freed_bytes_per_day_7d": 0, "avg_input_bytes_7d": 0
  }
}
```

### 7.1 Field rules

- Times are UTC ISO with `Z` (`board.iso`). The UI buckets in the controller's local
  time (constitution §3: one authority; the page runs on the controller).
- `films[*]` facts come from the receipt's input revision (§6.4); durations are whole
  seconds rounded from attempt times; an absent stage (e.g. restore of an
  already-restored input) is null, never 0.
- `energy_partial` = some resolved attempt of restore / asr / translate / return /
  scrape lacked energy (same rule as `energy.film_energy`).
- `restore_wh` sums, per worker, `energy.wh` and `energy.seconds` of every restore
  attempt with energy; the UI's 平均功率 for a machine = Σwh × 3600 ÷ Σseconds over all
  its entries in range (a machine with only failed attempts still gets a value).
- `subtitles` counts the ASR success note's decisions; the UI shows 「N 片已有中文字幕」
  (films with `reused_zh` > 0) and 「N 片无语音」 (films with `no_speech` > 0) — 「无需翻译」
  is never presented as 无语音.
- Worker ids stay full ids in every row (no index indirection): it keeps the shape
  self-describing; the cost is in the ≈ 510 B/film estimate.
- `state_now` uses the recorder's derivation (§6.2); `unknown` = `waiting`; `off` =
  remote workers disabled.
- `inbox.used_bytes − pending_bytes` is 「其他文件」 (clamped ≥ 0); the label says it
  includes everything else on that disk.

### 7.2 Outcome of a failure

For the failed attempt's job: the stage later `succeeded` → `retried_ok` if the
succeeded attempt ran on the same machine (or both worker-less), else `reassigned`;
the stage is `failed`/`blocked`/`unknown` and this is its latest attempt →
`needs_action`; otherwise (`pending`/`running`) → `retrying`. UI labels: 已重试成功 /
已自动改派 / 重试中 / 需要处理.

### 7.3 Failure types (ordered; first match)

Matched against the attempt's **last** evidence kind (`evidence[-1].kind`) first, then
its note.

| type | label | severity | match |
|---|---|---|---|
| `interrupted` | 运行中断 | warn | kind `exec_interrupted` |
| `stopped` | 已停止 | warn | kind `exec_failed:cancelled` |
| `translate_paused` | 翻译暂停 | warn | kind `exec_failed:transient`, stage `translate`, note starts 「翻译暂停」 |
| `transient` | 临时失败（自动重试） | warn | kind `exec_failed:transient` (any other) |

Exhausted retries: `should_retry` (`local_dispatch.py` 213-229) retries a transient
failure only while at most `MAX_AUTO_RETRIES` = 2 transient attempts of the job have
failed; after that the stage stays failed. So for the two transient types, when the
attempt's §7.2 outcome is `needs_action`, severity becomes **fail** and the label
「临时失败（重试已用尽）」 (translate pause: 「翻译暂停（重试已用尽）」); the `type` code keeps
its value and the response adds `"exhausted": true`. With any other outcome
(`retrying`, `retried_ok`, `reassigned`) they stay warnings. `exhausted` marks only
the job's latest attempt: earlier transient attempts of that failed job have outcome
`needs_action` but stay warnings (§10).
| `machine_fault` | 应用程序控制拦截 | fail | kind `exec_failed:machine`, or note contains `WinError 4551` |
| `missing_model` | 缺少模型文件 | fail | note contains `缺少 LTX 模型文件` |
| `missing_ffprobe` | 缺少 ffprobe | fail | note contains `ffprobe` |
| `ltx_failed` | LTX 失败 | fail | note contains `LTX 失败` |
| `scrape_whitelist` | 刮削白名单 | fail | stage `scrape` and note contains `白名单` |
| `other` | 其他失败 | fail | anything else (the note is shown) |

Client-side warnings added to the event list: 设备离线 (each offline span), 翻译服务中断
(each outage).

## 8. UI structure

### 8.1 Navigation

- `workspace.ts`: `PageKey` gains `"analytics"`.
- `MediaWorkspace.tsx` `PAGES`: `{ key: "analytics", label: "数据分析", icon: InsightsIcon }`
  after 片库与规则, before Mac 信息桥 (`@mui/icons-material` already ships it). Body:
  `readOnly ? <ControllerOnly title="数据分析" /> : <AnalyticsPage />`, routed beside
  `libraries`/`settings` (it reads its own route and handles 503 itself, so it does not
  wait on `useMediaData`).

### 8.2 Files (all under `taskpaw_v3/ui/src/components/media/analytics/`)

| File | Role |
|---|---|
| `AnalyticsPage.tsx` | Title, 「数据自 MM-DD 起」, 最后刷新 + 「每分钟自动刷新」, sticky filter bar, coverage notice, sections in preview order. |
| `analyticsModel.ts` | Pure, typed port of the preview's `rangeDef` / `compute` / `bucketize` / `stats` / `machineRows` / `modelGroups` / `uptime` / slices; no React. |
| `analyticsFormat.ts` | `fH`, `fMin`, `fKwh`, `fPct`, `fSpan`, `fGB`, local-time labels (as the preview). |
| `charts.tsx` | Primitives: `ColumnChart` (stacked, prev-period line, uncovered band, max label), `HBarChart`, `RangeChart` (min–max hairline, p25–p75 band, avg dot), `ShareBar`, `LaneChart`, `StripChart`, `Sparkline`; `useChartWidth` (ResizeObserver, debounced 120 ms); roving `tabindex` + arrow/Home/End keys; one shared `ChartTooltip` (pointer and focus, Escape hides). |
| `AnalyticsParts.tsx` | `AnalyticsPanel` (title, badges, 图表/表格 toggle), `DataTable`, `KpiTile` (value, delta, note, sparkline), `EstimateChip` 「估算」, `MachineStateChip` (在线 / 停领 / 离线 / 新加入 / 已退休, text + icon, never colour alone). |
| `sections/*.tsx` | `KpiRow`, `ProductionSection`, `MachinesSection`, `ControllerSection`, `DurationsSection`, `EnergySection`, `UptimeSection`, `AlertsSection`, `SourcesSection`. |
| `analyticsStyles.ts` | CSS scoped under `.mw`, values from `mediaTokens` plus the preview's chart tokens (`--c1 #3987e5` 4K, `--c2 #d95926` 1080P, `--c3…--c6`, `--c-prev`, `--c-queue`, `--c-total`, `--st-*`, `--lane-on`, `--grid`, `--axis`, `--uncov`, `--tt-bg`). |

`api.ts`: `workflowAnalytics(since?: string)` using the detail-preserving GET pattern
(as `libraryGet`) so 409/503 details reach the page; types `WorkflowAnalytics`,
`AnalyticsMachine`, `AnalyticsFilm`, `AnalyticsFailure`, `AnalyticsSpan`,
`AnalyticsOutage`, `AnalyticsInbox`.

`PowerChart.tsx` is not refactored; `useChartWidth` is a separate small hook.

### 8.3 State, filters, refresh

- Page state: `range` (default `7d`, as the preview), `machine` (`all` | worker id |
  controller id), `res` (`all` | `4K` | `1080p`), `compare`, per-panel view, machine-table
  sort, alert list page. Changing range refetches (new `since`); machine / res / compare
  recompute locally (`useMemo` over the cached response).
- Query: key `["mediaAnalytics", since]`, `refetchInterval: 60_000` (D3, with a one-line
  comment citing OD6), `placeholderData: keepPreviousData` (the previous render stays
  visible, dimmed, while a new range loads), default refetch-on-focus.
- Machine select: 「全部 GPU 机器」, one `optgroup` per GPU model ("RTX 5060 Ti 16GB × 4"),
  then 「主控」 → 本机. Retired machines with data in range are listed with 「（已退休）」.
- Clicking a machine-table row filters to that machine; 「查看全部 GPU 机器」 resets.

### 8.4 Panels (preview order)

1. **KPI row** (6 tiles): 完成影片 · 平均全程时长 · 平均修复时长 (本机 filter: 「本机不做修复」
   unless it restored in range) · 总用电 「估算」 · 失败率 · GPU 在线 N / M (可领取 K 台;
   spark from spans when covered). Each with Δ vs previous period when `compare` and the
   previous period is covered, else the preview's flat-state texts.
2. **产量趋势**: stacked columns by resolution per bucket, previous-period line, max label,
   uncovered band, tooltip, table.
3. **各 GPU 机器**: model-grouped sortable table (机器 · 状态 · 修复影片 with 4K/1080P bar
   and same-model average tick · 平均修复 4K · 平均修复 1080P · 最短/最长 · 平均功率 ·
   用电 · 失败 · 修复效率); 各机型实测产能 table; small multiples with one shared y-axis.
4. **本机（主控）处理**: ASR / 翻译 (films translated in total, 「共 N 万行字幕」 with
   「自 MM-DD 起统计行数」 while `coverage.cues` is inside the range, and two chips from
   `subtitles`: 「N 片已有中文字幕」 and 「N 片无语音」) / 刮削 / 归档 tiles; 归档去向 bars;
   翻译服务中断 strip with the uncovered band before `coverage.outages` (D7).
5. **耗时分布**: range strips in hours (排队, 修复 4K, 修复 1080P, 全程) and minutes (ASR,
   翻译, 刮削, 归档); time-share bar; table with min / p25 / median / p75 / max / avg.
6. **用电** 「估算」: per-bucket columns (+ previous), 每片平均 / 最多 / 最少, by resolution,
   by machine (+ 本机后处理), top-5 films table; 「部分」 note when `energy_partial` films
   are in range.
7. **设备在线**: lane timeline per machine (在线 / 停领 / 离线 / 无记录), 在线率 text, table of
   spans; amber notice while `coverage.devices` is null or inside the range.
8. **异常与报警**: burst banner per D16 (only for a burst within the last 24 h or with a
   `needs_action` row); by type (fail red / warn amber,
   icon + text; D5 warnings listed as warnings), failures per machine (fail only),
   newest-first event list (时间 · 机器 · 类型 · 影片 · 处理结果 · 说明; burst rows
   highlighted), 14 rows then 「显示更多」.
9. **片源与片库**: INBOX card (meter 已用 / 待处理 / 其他 / 可用 with 「当前 · 不随时段筛选」,
   text 「已用 X GB · 可用 Y GB（共 Z TB）」, 待处理影片占用, 平均每天腾出, 现在还可放入约 N 部
   「估算」 — no projection, D9); 输入大小
   and 片长 histograms; 归档最多的演员（前 8）; 标识占比.

No element anywhere shows a total-progress percentage, remaining count against a total,
or a completion time (OD1).

### 8.5 Accessibility and interaction (from the preview, kept)

Chart groups have `role="img"` + `aria-label`; one focusable item per chart, arrow keys
move, the tooltip follows focus; every chart has the table view; text on every state;
44 px targets for toggles; `prefers-reduced-motion` removes the fade; an `aria-live`
line announces the recomputed summary after a filter change. Layout follows the
preview's breakpoints (6 → 3 → 2 KPI columns; grids to one column ≤ 1080 px; filter bar
not sticky ≤ 900 px).

### 8.6 Empty and uncovered states

| Situation | Shown |
|---|---|
| Workflow off / failed / starting (503) | `Notice`: 「媒体流水线未运行：<reason>」 |
| 409 (a worker install) | 「数据分析只在主控上」 |
| No archive yet | 「还没有归档完成的影片」 per panel; INBOX card still shown |
| Range starts before `coverage.archives` | amber notice 「所选时段早于数据记录起点（MM-DD），只统计 N 天」 and a grey 「无记录」 band in every time chart |
| Device history missing / partial | amber notice in 设备在线; lanes grey before the first span and in gaps (tooltip 「主控未运行或未收到心跳，无记录」) |
| Device history unavailable (`available=false`) | 「设备记录不可用：<reason>」 |
| Range starts before `coverage.outages` | grey band and 「翻译服务中断记录从 MM-DD 起」 (date from `coverage.outages`, not a fixed day count) |
| Previous period not covered | 「上一周期没有完整记录，暂时无法对比」 (as preview) |
| Machine is 新加入 (D14 rule) | 「新加入」 chip, lane starts at `first_seen`, excluded from the same-model average tick only (kept in 各机型实测产能) |

## 9. Performance budgets

| Item | Budget | Basis |
|---|---|---|
| Analytics request, warm, 5,000 archived films / ~42k attempts | ≤ 150 ms server time | incremental receipts 0 ms + failures ≤ 60 ms + spans/inbox small |
| Cold fact build after restart | ≤ 1.5 s total; ≤ 50 ms per locked batch (≤ 200 receipts) | measured 0.71 s for 4,995 films in one read |
| Payload | 7 d (+ previous) ≤ 150 KB; 全部 at 5,000 films ≤ 3 MB | ≈ 510 B/film (≈ 2.6 MB at 5,000); `JSONResponse`, no `jsonable_encoder` (§7) |
| Client aggregation (`compute`) for 5,000 films | ≤ 50 ms | plain arrays, one pass per section |
| Recorder tick | one `list_workers` read + one small transaction; no work when nothing changed except `last_seen_at` | §6.2 |
| `analytics.db` growth | ≈ 1.5 MB / year | §6.1 |

If a later year pushes 全部 past the payload budget, the follow-up is server-side
monthly buckets for 全部 only (out of scope now).

## 10. Test plan

Backend (pytest, temp data dirs only — never the operator's live data directory):

- `test_workflow_device_history.py` (new): create/open/refuse (wrong id, newer
  version → off, file unchanged); derivation table row by row; tick writes (same state
  extends, change closes+opens, revoked/retired close); GPU lane `global` (dispatcher
  paused) and `version` (protocol mismatch) rows; restart closes open spans without
  moving `last_seen_at`; a tick more than 90 s after the previous one does the same,
  tripped separately by a fake monotonic delta and by a fake wall-clock (`rt.clock`)
  jump, and not tripped at 89 s on both; `waiting` writes nothing; any exception in a tick (DB error,
  store error, a raising stub) is logged once per type per hour and the next tick
  retries; host stop joins the thread and closes the DB; `spans(since)` overlap and
  ordering; `coverage_start`.
- `test_workflow_exec_translate.py`: success evidence `item_count` = cue total; 无需翻译
  leaves null; stored evidence still validates.
- `test_workflow_analytics.py` (new): facts over a `World`-style runtime with archived
  films (resolution, durations, queue wait rules, energy per worker incl. failed
  attempts, partial energy, target kinds, actor label, marks); a removed film still
  counts; a new receipt appears on the next call without recomputing old facts;
  batching (≤ 200 receipts per locked read); two concurrent requests on a cold cache
  read each batch once (cache lock); `restore_wh` `[worker, wh, seconds]` incl. a
  machine with only failed attempts; `subtitles` from ASR notes (reused_zh, no_speech,
  mixed, unparseable → {}); failure type table by last evidence kind (interrupted,
  cancelled, transient translate pause, transient other, machine, note types, other);
  **three transient attempts on one job**: the first two are `warn` (outcome
  `retrying` while pending, then their outcome once the job settles) and the third,
  with the stage left failed, is `fail` with `exhausted: true` and outcome
  `needs_action` (same for a translate 「翻译暂停」 job); outcome rules; `since`
  filtering; machines (model label from probe, MIN first_seen, retired); inbox pending
  bytes; outages from a temp task-log folder (kind filter; closed days cached and not
  re-read; today read from the saved byte offset, only appended bytes parsed; a torn
  last line left unconsumed and picked up after it is completed; unflushed ring rows of
  today included; midnight rollover; pruned day dropped; `coverage.outages` = oldest
  scanned day; the day file is read without holding `TaskLog._lock`); the 200 body is a
  `JSONResponse` and a NaN in a number raises instead of emitting invalid JSON.
- `test_workflow_analytics_routes.py` (new) or added to `test_workflow_library_routes.py`
  style: 409 on worker role; 503 not running; 422 bad/naive `since`; 200 shape.
- `test_workflow_ui_contract.py`: add the analytics response to
  `ui/src/test/fixtures/workflow-contract.json` so UI tests read the real shape
  (regenerate with `TASKPAW_WRITE_UI_CONTRACT=1`).
- Manual performance smoke (not CI): build a ×45 synthetic database from a temp test
  store and time a cold and a warm request against §9.

UI (vitest, `taskpaw_v3/ui/src/test/`):

- `analyticsmodel.test.ts`: range edges per range kind (local time), bucket index,
  previous-period alignment, uncovered fraction, quantiles, machine rows, model groups,
  per-online-day throughput with/without span coverage, uptime with gaps, 「新加入」 at
  the D14 boundary (just inside / just outside 10 %, a range starting before
  `coverage.archives`, and 全部 with `effective_start = coverage.archives`); a 新加入
  machine is left out of the same-model average tick but kept in 各机型实测产能;
  平均功率 from `[wh, seconds]`; 失败率 excluding warnings but including exhausted
  transients; D16 burst detection (2 vs 3 in 1 h, 3 spread over > 1 h, two machines)
  and banner gating (burst ending within 24 h → banner; older burst all recovered → no
  banner, rows still highlighted; older burst with a `needs_action` row → banner).
- `analyticspage.test.tsx`: renders from the contract fixture; KPI values; range change
  issues a new `since`; machine/res/compare recompute without a refetch; chart/table
  toggle; machine row click filters; 503 / 409 notices; empty state; uncovered notice;
  `readOnly` workspace shows `ControllerOnly`; the query declares
  `refetchInterval: 60000`.
- `analyticscharts.test.tsx`: each primitive renders bars/segments for given data, a
  tooltip on focus with the expected rows, arrow-key roving, Escape hides.
- **OD1 guard** (rendered page, full fixture, every panel in chart and table view):
  no percentage of progress toward a total (e.g. 「已完成 37%」, 「进度」), no remaining
  count (「剩余 N 部」 or any 「剩余」), none of 「预计」「ETA」「完成度」, and no completion
  date or time (「预计完成」「将于 … 完成」). It explicitly does **not** ban the
  「GPU 在线 N / M」 device-count tile or per-panel 「共 N 部」 totals of what happened —
  the test asserts that tile is present.
- Existing `mediaworkspace.test.tsx` / `mediaworkspace_readonly.test.tsx`: rail gains
  数据分析 in the right place.

Canonical checks for every child (bash; `LOCALAPPDATA`, never `%TEMP%`):

```bash
uv run --locked pytest --basetemp="$LOCALAPPDATA/taskpaw-pytest/<child>" -p no:cacheprovider
uv run ruff check . && uv run ruff format --check . && uv run mypy
cd taskpaw_v3/ui && npm test && npm run lint && npm run build
```

Clean the basetemp directory at the end of the round.

## 11. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Cold fact build holds the store lock and delays dispatch/heartbeat writes | Medium (first request after restart) | Low | ≤ 200 receipts per locked read, lock released between batches; budget test in §9 |
| Device history starts late, so 在线 panels are empty for weeks | Certain at first | Low | C1 ships first and alone; honest 「记录从 MM-DD 开始」 notice; throughput falls back to calendar days with a note |
| Failure notes are free text; a new message lands in 其他失败 | Medium | Low | Note shown verbatim; classifier is one ordered table with tests |
| `analytics.db` unwritable/damaged | Low | Low | Recorder off with reason; workflow unaffected; page shows the reason |
| Payload grows past budget over multiple years | Low (≥ 5k films) | Medium | `since` limits all but 全部; follow-up server buckets for 全部 |
| Probe-based GPU model wrong after a GPU swap | Low | Low | D11 documented; table shows the name first |
| Task-log outage parsing breaks if event kinds change | Low | Low | Kind names are constants in `monitors/subs/translate.py`; test against them |
| First outage scan after restart parses up to 50 MB of task log | Medium (once per restart) | Low | Day files read outside the task-log lock; closed days cached; today read incrementally from a byte offset; cost unmeasured on the live log (not read), C2 records it |
| ASR note format changes and subtitle chips go blank | Low | Low | Parser bound to the `ASR_DONE` constant and the decision literal set; unparseable → {} and the chips hide; tested |
| A stalled controller (sleep, suspend) reported as continuous state | Low | Low | > 90 s between ticks closes spans as a gap (§6.2) |
| Hub version skew | Low | None | Route is controller-local; workers and Hub unaffected; no protocol change |
| Reviewer pressure to add server buckets / persistence of filters | — | Scope | Frozen contracts below; D2/D15 recorded |

## 12. Out of scope

- Any progress-vs-total, backlog forecast, ETA, or 「约 1 天后可再放入」 (OD1, D9).
- Hub-side (read-only) analytics and any Hub proxy route (D10).
- Durable translation-outage history and model share (D7, D8).
- 主控 CPU 高占用 and other monitor alerts (D6).
- Backfilling device history from logs or audit.
- Export (CSV), per-film drill-down from charts, persisted filters, configurable alert
  thresholds, electricity price.
- Changes to `workflow.db` schema, the heartbeat protocol, worker code, or the existing
  pages other than the rail entry.
- Release/version bump and packaging (the operator's release procedure).

## 13. Child issues (frozen contracts), in dependency order

```
C1 recording ──► C2 API ──► C3 page shell + KPI + 产量趋势 ──► C4 machines / controller / durations
                                                         └──► C5 energy / uptime / alerts / sources
```

C1 has no dependency and should merge first so history accrues. C4 and C5 may run in
parallel after C3.

### C1 — Start recording device availability and translated line counts

**Scope.** Persist each machine's online / 停领 / offline spans on the controller and
record the translated cue count on translate successes. No API, no UI.

Acceptance criteria:
1. `workflow/device_history.py` opens `analytics.db` in the workflow data dir (local
   path rule as `library.db`); a new file gets exactly the §6.1 DDL,
   `application_id 0x54504153` and `user_version 1` in one transaction.
2. Opening a file with another application id, a newer or unknown version, or
   mismatched DDL leaves the file byte-identical, keeps the recorder off and exposes a
   stable reason string.
3. On a controller (`settings.role == "controller"`), the workflow host starts the
   recorder after the runtime and dispatcher (and `FleetLive`, when remote workers are
   on) exist, with or without remote workers; on a worker install it never starts.
4. The recorder ticks every 30 s on a monotonic schedule and derives each lane exactly
   per the §6.2 table, first match wins (including: `waiting` → no write;
   revoked/retired/unverified/remote off → not recorded and any open span closed;
   GPU lanes `paused`/`global` while the dispatcher is paused and `paused`/`version`
   while `FleetLive.version_ok` is false). Reasons are limited to
   `'' | admission | worker | global | version`.
5. Per tick, in one transaction: an unchanged state+reason extends `last_seen_at`; a
   change closes the old span at `now` and inserts a new one starting `now`; at most one
   open span per worker (unique partial index).
6. At recorder start, and on any tick that runs more than 3 × 30 s after the previous
   tick by **either** the `time.monotonic()` delta or the `rt.clock.now()` wall delta,
   every open span is closed without changing its `last_seen_at` before the tick's
   states are written.
7. Any `Exception` raised during a tick is caught at the tick boundary, logged with its
   type (at most one line per exception type per hour) and the next tick retries; it
   never raises into the host or the dispatcher and the thread keeps running.
8. `LocalWorkflowHost.stop()` stops and joins the recorder thread (≤ 5 s) and closes
   the connection before the runtime closes; a stop with the recorder off is a no-op.
9. `DeviceHistory.spans(since)` returns spans overlapping `[since, now]` oldest first
   with `open` flags; `coverage_start()` returns the earliest `started_at` or None.
10. A translate success writes `summary.item_count` = total cues written to `.zh.srt`
    across translated volumes; 「无需翻译」 leaves it null. `EvidenceSummary`, worker
    protocol and `workflow.db` schema (v4) are unchanged.
11. CHANGELOG entry in Chinese (operator-facing: history starts recording now).

Non-goals: API/UI; backfill; changing `FleetLive`, heartbeat shape, admission logic,
`workflow.db`; pruning.
Files likely touched: `taskpaw_v3/workflow/device_history.py` (new),
`taskpaw_v3/workflow/local_host.py`, `taskpaw_v3/workflow/exec_translate.py`,
`taskpaw_v3/workflow/executors.py`, `taskpaw_v3/workflow/local_runtime.py`
(`evidence(..., item_count=)`), `taskpaw_v3/workflow/local_dispatch.py` (pass it at the
evidence site), `taskpaw_v3/tests/test_workflow_device_history.py` (new),
`taskpaw_v3/tests/test_workflow_exec_translate.py`,
`taskpaw_v3/tests/test_workflow_local_host.py`, `CHANGELOG.md`.
Dependencies: none.
Canonical checks: §10 (`<child>` = `analytics-c1`).

### C2 — Analytics read API

**Scope.** `GET /control/workflow/analytics` per §7 with the per-receipt fact cache.

Acceptance criteria:
1. Route registered on the loopback control API from a new
   `agent/server/analytics_routes.py` (registered in `agent/server/app.py` beside the
   library routes); Bearer-gated like the other control routes.
2. Worker role → 409 `数据分析只在主控上`; host off/not running → 503 with the host
   reason; store `WorkflowError` → 503 via `_mapped`; `since` invalid or without offset
   → 422.
3. Response matches §7 field-for-field (names, types, null rules); times UTC `Z`.
4. Film facts follow §7.1 and D4 exactly, one per accepted archive receipt with
   `accepted_at ≥ since` (all receipts without `since`), including films removed later.
5. Facts are cached per receipt rowid; a request reads only receipts above the
   high-water mark, in locked batches of ≤ 200; one cache lock wraps the whole
   incremental update, so concurrent requests wait and no batch is read twice; a second
   identical request performs no per-film fact read.
6. Failures: every failed attempt of restore/asr/translate/return/scrape/archive with
   `ended_at ≥ since`, typed by §7.3 on its **last** evidence kind then its note (first
   match: interrupted, cancelled → 已停止, transient translate pause → 翻译暂停, other
   transient → 临时失败, machine, note types, other), with `severity` per §7.3 and §7.2
   outcome; a transient-typed attempt whose outcome is `needs_action` (retries used up,
   `should_retry` false after `MAX_AUTO_RETRIES`) has `severity: fail`,
   `exhausted: true` — only the job's latest attempt; its earlier transient attempts
   are `needs_action` but stay `warn` (§10); every other failure has
   `exhausted: false`; worker-less stages report the controller's machine id.
7. `machines`: every enrolled worker plus 本机 and any retired worker referenced by a
   returned fact/failure/span; `model` from the latest probed capability formatted
   "RTX 5060 Ti 16GB"; `first_seen` = MIN `worker_boots.observed_at`; `state_now`
   via the C1 derivation.
8. `devices` from C1 (`available=false` + reason when the recorder is off); `outages`
   from the task log events named in D7, paired start/end, within `since`, read by kind
   through a new small `TaskLog` method that reads one day file from a byte offset
   **outside** `TaskLog._lock` (append-only file; a torn last line is not consumed) and
   takes the lock only to copy that day's unflushed ring rows; never
   `TaskLog.query(task=…)`. Closed days are cached in memory; today is read
   incrementally from the saved offset (only appended bytes per request);
   `coverage.outages` = start of the oldest day actually scanned.
8a. Each film fact carries `subtitles` (ASR success note decision counts, `{}` when
   unparseable) and `restore_wh` entries `[worker_id, Σwh, Σenergy seconds]` over every
   restore attempt with energy, failed ones included.
8b. The 200 body is returned as `JSONResponse(body)` (§7): no `jsonable_encoder` walk,
   `allow_nan=False` kept.
9. `inbox` per §7 (null without an inbox root); `freed_bytes_per_day_7d` and
   `avg_input_bytes_7d` from films archived in the last 7 days.
10. The UI contract fixture includes an analytics response produced by
    `test_workflow_ui_contract.py`.
11. Measured on a ≥ 5,000-archived-film synthetic store: warm request ≤ 150 ms, cold
    build ≤ 1.5 s with no locked read > 50 ms (recorded in the PR body, not a CI test).
12. No change to existing route responses; CHANGELOG entry.

Non-goals: UI; server-side bucketing; Hub proxy; CSV; writing anything.
Files likely touched: `taskpaw_v3/workflow/analytics.py` (new),
`taskpaw_v3/workflow/read_store.py` (operator-only reads: receipt facts batch,
failures since, first boots, inbox pending), `taskpaw_v3/workflow/local_host.py` (hold
the cache), `taskpaw_v3/core/tasklog.py` (one small per-day, by-kind read method),
`taskpaw_v3/tests/test_tasklog*.py` (that method),
`taskpaw_v3/agent/server/analytics_routes.py` (new),
`taskpaw_v3/agent/server/app.py`, `taskpaw_v3/tests/test_workflow_analytics.py` (new),
`taskpaw_v3/tests/test_workflow_analytics_routes.py` (new),
`taskpaw_v3/tests/test_workflow_ui_contract.py`,
`taskpaw_v3/ui/src/test/fixtures/workflow-contract.json`, `CHANGELOG.md`.
Dependencies: C1.
Canonical checks: §10 (`analytics-c2`).

### C3 — 数据分析 page shell, filters, KPI row, 产量趋势, chart primitives

**Scope.** The rail entry and page frame with the core model and the first two
sections; every later section plugs into it.

Acceptance criteria:
1. 数据分析 appears in the rail after 片库与规则 (desktop rail and narrow nav dialog);
   the Hub read-only workspace shows `ControllerOnly` for it.
2. `api.workflowAnalytics(since?)` with detail-preserving errors and the §7 types.
3. One query `["mediaAnalytics", since]` with `refetchInterval: 60000`,
   `keepPreviousData`, default focus refetch; header shows 最后刷新 (local time) and
   「每分钟自动刷新」.
4. Filter bar per §8.3 (range seg, grouped machine select, resolution seg, compare
   switch disabled for 全部); range change refetches with a floored `since`; other
   filters recompute locally.
5. `analyticsModel.ts` implements range/bucket/compute/stats/slices as §8.2 with unit
   tests; buckets in local time.
6. KPI row (6 tiles) per §8.4 item 1, including deltas, sparklines and the 本机 and
   uncovered variants; 「估算」 on 总用电; 失败率 counts `severity: fail` only, which
   includes exhausted transients (D12, §7.3).
7. 产量趋势 per §8.4 item 2 with tooltip, keyboard roving and table view.
8. Shared primitives used here — `AnalyticsPanel`, `DataTable`, `KpiTile`,
   `ColumnChart`, `Sparkline`, `ChartTooltip`, `useChartWidth` — with tests.
9. Empty/uncovered/503/409 states per §8.6 for the parts in this child.
10. The OD1 guard test per §10 (explicit banned phrases; asserts the 「GPU 在线 N / M」
    tile is present) passes; no new npm dependency; styles only from `mediaTokens` and
    the preview's chart tokens.
11. 全部 uses `effective_start = coverage.archives` and `length = now − coverage.archives`
    for bucketing, uncovered handling and the D14 新加入 test.

Non-goals: the other seven sections; persistence; Hub support.
Files likely touched: `taskpaw_v3/ui/src/components/media/workspace.ts`,
`taskpaw_v3/ui/src/components/media/MediaWorkspace.tsx`, `taskpaw_v3/ui/src/api.ts`,
`taskpaw_v3/ui/src/components/media/analytics/{AnalyticsPage,AnalyticsParts,charts}.tsx`,
`.../analytics/{analyticsModel,analyticsFormat,analyticsStyles}.ts`,
`.../analytics/sections/{KpiRow,ProductionSection}.tsx`,
`taskpaw_v3/ui/src/test/{analyticsmodel,analyticspage,analyticscharts}.test.ts(x)`,
`taskpaw_v3/ui/src/test/mediaworkspace*.test.tsx`, `CHANGELOG.md`.
Dependencies: C2.
Canonical checks: §10 (`analytics-c3`; UI checks are the gate, pytest unchanged).

### C4 — 各 GPU 机器, 本机（主控）处理, 耗时分布

Acceptance criteria:
1. 各 GPU 机器 per §8.4 item 3: model groups from `machines.model`, sortable columns
   (aria-sort), 4K/1080P bar with same-model average tick (excluding only machines that
   are 新加入 under the D14 rule), state chips with notes, row click filters; 平均功率 =
   Σwh × 3600 ÷ Σseconds from `restore_wh` (machines with only failed attempts in range
   included); 失败 counts `severity: fail` only; 各机型实测产能 includes every machine
   (新加入 too), normalised by online days when device spans cover the range, else
   calendar days present with 「未扣除离线时间」; small multiples share one y-axis; table
   view lists every number.
2. 本机 shown in the GPU table only when it restored in range (D11); with the 本机
   filter the panel shows the preview's notice.
3. 本机（主控）处理 per §8.4 item 4: ASR / 翻译 (translated count, line total with
   coverage note, chips 「N 片已有中文字幕」 from `subtitles.reused_zh` and 「N 片无语音」
   from `subtitles.no_speech`; 「无需翻译」 is never shown as 无语音) / 刮削 / 归档 tiles;
   归档去向 bars (主片库, 分类目录, 新建演员, 手动目录, 第二目标); 翻译服务中断 strip with the
   uncovered band before `coverage.outages` and the text 「翻译服务中断记录从 MM-DD 起」
   (D7); a GPU-machine filter shows the preview's 「翻译服务属于本机」 text instead of
   the strip.
4. 耗时分布 per §8.4 item 5 (hours and minutes strips, share bar, full stats table).
5. Primitives added with tests: `HBarChart`, `RangeChart`, `ShareBar`, `StripChart`.
6. No model-share chip (D8); the §10 OD1 guard still passes.

Non-goals: 用电, 设备在线, 异常与报警, 片源与片库; backend changes.
Files likely touched: `.../analytics/sections/{MachinesSection,ControllerSection,DurationsSection}.tsx`,
`.../analytics/charts.tsx`, `.../analytics/analyticsModel.ts`, related tests,
`CHANGELOG.md`.
Dependencies: C3.
Canonical checks: §10 (`analytics-c4`).

### C5 — 用电, 设备在线, 异常与报警, 片源与片库

Acceptance criteria:
1. 用电 per §8.4 item 6, 「估算」 badge, per-machine attribution from `restore_wh`
   `[worker, wh, seconds]` entries (failed attempts on other machines counted there),
   本机后处理 from `controller_wh`, top-5 table, 「部分」 note.
2. 设备在线 per §8.4 item 7: one lane per machine in range (plus 本机), states with text
   and icon (停领 tooltips name the reason: 主控停领 / 工作机暂停领取 / 主控暂停新领取 /
   版本不兼容), gaps as 「无记录」, lanes of 新加入 machines (D14) start at `first_seen`;
   在线率 = (online + paused) ÷ recorded time, unrecorded time listed separately;
   notices per §8.6.
3. 异常与报警 per §8.4 item 8: by type (fail/warn per §7.3, D5/D6), failures per machine
   (fail only), merged event list (failures and warnings + offline spans + outages)
   newest first with outcome labels (§7.2) and the 「（重试已用尽）」 labels for exhausted
   transients (§7.3); no CPU toggle; a burst (≥ 3 `machine_fault` failures on one
   machine within any 1 h window in the range) always highlights its rows, and shows a
   red notice naming the machine, count and span only when the burst ends within the
   last 24 h or any of its rows has outcome `needs_action` (D16).
4. 片源与片库 per §8.4 item 9: INBOX card from `inbox` (meter 已用 / 待处理 / 其他 / 可用;
   free space labelled 「可用」, never 「剩余」; no projection, D9), histograms, top 8
   actors, marks share (labels 中文字幕 / 无码 / 4K / 修复 from the mark codes).
5. Primitive added with tests: `LaneChart`; INBOX meter has `role="meter"` with
   values.
6. The §10 OD1 guard still passes (INBOX 「可用」 text included).

Non-goals: backend changes; durable outage history; CPU alerts.
Files likely touched: `.../analytics/sections/{EnergySection,UptimeSection,AlertsSection,SourcesSection}.tsx`,
`.../analytics/charts.tsx`, `.../analytics/analyticsModel.ts`, related tests,
`CHANGELOG.md`.
Dependencies: C3 (C2's contract fixture).
Canonical checks: §10 (`analytics-c5`).

## 14. Assumptions (not verified by running)

- TanStack Query pauses `refetchInterval` only while the document is hidden
  (`focusManager` on `visibilitychange`, `refetchIntervalInBackground` false) and
  refetches when it becomes visible; a visible but unfocused window keeps its interval —
  taken from EPIC #375 C4 §A1 (read there, not re-run here).
- Task-log day files average ≤ 50 MB / 30 days; the per-day parse cost was not
  measured because the live log lives under the agent config directory, which this
  stage did not read.
- `@mui/icons-material` 6.x exports `Insights` — not imported here; fall back to
  `BarChart` if not.
- The task log's workflow translator events are recorded under task name
  `workflow-translate` (`exec_translate.default_translator`) and kinds
  `translate.provider_down` / `translate.provider_up` / `translate.paused`
  (`monitors/subs/translate.py`); pairing behaviour across restarts was not observed on
  live data (no outage in the inspected period).
- `shutil.disk_usage` on the INBOX root reflects the 1 TB drive the operator means
  (INBOX and the work directory may share that volume; then 「其他文件」 includes work
  copies) — not checked on the machine.
- The ×45 synthetic database reproduces the live row shapes, not real index locality;
  real timings at 5,000 films may differ; C2 AC11 re-measures.
- Browser-side aggregation cost (≤ 50 ms for 5,000 films) is an estimate from the
  preview's algorithm, not measured.

## 15. Execution surface

- Allowed writes per child: only the files listed in that child (plus generated
  fixture `workflow-contract.json` in C2 via
  `TASKPAW_WRITE_UI_CONTRACT=1 uv run --locked pytest taskpaw_v3/tests/test_workflow_ui_contract.py`).
- Read/executed, never modified: `workflow/schema.py` (v4 stays), `fleet_live.py`,
  `scheduler.py`, `devices.py`, `monitors/subs/translate.py`, `core/tasklog.py`
  (except C2's one added read method),
  `library_store.py`, `PowerChart.tsx`.
- Runtime side effects (shipped): a new `analytics.db` (+ WAL) in the workflow data dir
  on controllers; one recorder thread; one new GET route. No network access, no NAS
  access, no new process.
- Tests and manual checks use temp data dirs; never the operator's live workflow data
  directory or `%APPDATA%` configs.

## 16. Handoff notes

- Read the approved preview's JS (`compute`, `rangeDef`, `machineRows`, `uptime`,
  section functions) before writing `analyticsModel.ts`; port behaviour, not markup, and
  drop the demo scenario bar, the 30 s ticker, the CPU toggle and the INBOX projection.
- Do not print or commit real film codes or performer names in tests or fixtures; use
  invented examples.
- The store lock is shared with dispatch: every new read must be a single bounded
  query set per call (batches for the cold build).
- Keep each child's diff to its contract; a reviewer's wish beyond it goes to the
  operator, not into the PR.

## 17. Revision log

Revision 1 (debate round 1). I checked each item against the code or the read-only
live DB before changing anything. All 13 hold; R1-D2 is applied slightly more widely
(see its row).

| ID | Verified against | Change |
|---|---|---|
| R1-D1 | `exec_translate.py` 142 (`_TRANSLATED` = source_ja, generated_ja); `stage_results.py` 63; `exec_asr.py` 232-235 (`ASR_DONE` decision counts); live: the 11 「无需翻译」 films are 10 × `reused_zh 1` and 1 × `no_speech 1` (the one mixed `generated_ja 2，no_speech 1` film was translated) | New per-film `subtitles` decision counts parsed from the ASR success note (§6.4, §7, §7.1); §5 row; §8.4 item 4 shows 「N 片已有中文字幕」 and 「N 片无语音」; C2 AC8a; C4 AC3; tests |
| R1-D2 | `local_dispatch.py` 153-154 `_FREE_RETRY`, 205; `route_store.py` 311; `operator_actions.py` 230; `exec_translate.py` 240-243; `executors.classify_exception` (PermissionError → transient) | §7.3 now types by the **last** evidence kind first: `cancelled` → warn 已停止; translate `transient` with 「翻译暂停」 note → warn 翻译暂停. **Wider than asked:** every other `transient` (translate 「翻译失败：…」, PermissionError on any stage, a worker-reported transient) → warn 临时失败（自动重试）, since all of them are automatic retries. D5, D12, §5, §10, C2 AC6, C3 AC6, C4 AC1, C5 AC3 |
| R1-D3 | `AttemptEnergy.seconds` (`models.py` 335); live restore energy carries `seconds` | `restore_wh` entries are `[worker, Σwh, Σseconds]`; 平均功率 = Σwh·3600/Σseconds per machine, failed-only machines included (§5, §7, §7.1, C2 AC8a, C4 AC1, C5 AC1) |
| R1-D4 | `core/tasklog.py` 24 (`MAX_BYTES` 50 MB), 415-437 (30 d + size prune), 499 (limit ≤ 500), 515-545 (30-day walk under `_lock`) | Outages are read by kind through a new per-day `TaskLog` read, one lock acquisition per day; closed days cached, only today re-read; `coverage.outages` = oldest day scanned; band text uses that date (D7, §5, §6.4, §8.6, C2 AC8, C4 AC3). Cost stated as an estimate (live log not read). `core/tasklog.py` added to C2 files and to the §15 exception |
| R1-D5 | `agent/server/worker_api.py` 528 (reply `paused`); `remote_dispatch.py` 513; `FleetLive.version_ok` / `claim_admission_error` | §6.2 table gains GPU `paused/global` and `paused/version`; DDL reason CHECK and §7 `state_reason` extended; C1 AC4; C5 AC2 tooltips; tests |
| R1-D6 | `worker_store.py` 470-485 (record_boot rewrites `observed_at` for a re-posted boot) | D14 rule: `first_seen` = MIN over boots; 新加入 iff `first_seen > max(range start, coverage.archives) + 10% × range length`; drift noted as acceptable; §5, §8.6, C2 AC7, C4 AC1, C5 AC2, model tests at the boundary |
| R1-D7 | The OD1 guard bans 「剩余」; the preview's INBOX text used 「剩余」 for free space | Free space is 「可用」 everywhere (D9, §5, §8.4 item 9, C5 AC4); guard kept unchanged |
| R1-D8 | FastAPI `jsonable_encoder` walks dict responses; recomputed row size with 36-char ids and 3-element energy entries | Pre-encoded JSON `Response` (§7, C2 AC8b, test); payload re-estimated at ≈ 510 B/film, ≈ 2.6 MB at 5,000 (§4, §9); worker ids kept as full ids (§7.1) to keep the shape simple; budget stays ≤ 3 MB and warm ≤ 150 ms |
| R1-D9 | EPIC #375 C4 §A1 (`focusManager` on `visibilitychange`) | D3 and §14 now say the interval pauses only while the document is hidden |
| R1-D10 | — (robustness) | Recorder catches `Exception` at the tick boundary, rate-limited log (§6.2, C1 AC7, tests) |
| R1-D11 | — (sleep/suspend honesty) | A tick > 3 × 30 s after the previous one closes open spans without moving `last_seen_at` (§6.2, C1 AC6, test with a fake monotonic clock, §11 risk) |
| R1-D12 | Preview's spike banner (4551 burst) | D16: ≥ 3 `machine_fault` failures on one machine within 1 h → red notice and highlighted rows (§8.4 item 8, C5 AC3, model tests) |
| R1-D13 | — (concurrency) | One cache lock around the incremental update; concurrent requests wait, no double batch (§6.4, C2 AC5, test) |

New risks recorded in §11: the first outage scan after a restart (≤ 50 MB of task log,
cost unmeasured), the ASR note format as a parse dependency, and a stalled controller
(mitigated by R1-D11).

Revision 2 (debate round 2). I checked each item against the code first. None is
refuted.

| ID | Verified against | Change |
|---|---|---|
| R2-D1 | `local_dispatch.py` 59 (`MAX_AUTO_RETRIES = 2`), 213-229 (`should_retry`: a transient retries only while ≤ 2 transient attempts have failed; the third leaves the stage failed) | §7.3: a transient-typed attempt whose §7.2 outcome is `needs_action` → `severity: fail`, label 「临时失败（重试已用尽）」 / 「翻译暂停（重试已用尽）」, new field `exhausted: true` in §7; it stays a warning for `retrying` / `retried_ok` / `reassigned`. D12, C2 AC6, C3 AC6, C5 AC3; test with three transient attempts on one job (plain and translate pause) |
| R2-D2 | `core/tasklog.py` 152-177 (`_read_day` treats a parseable tail as complete and skips bad lines); the day file is append-only | §6.4 and C2 AC8: the per-day kind read runs **outside** `TaskLog._lock` from a byte offset; a torn last line is not consumed; the lock is taken only to copy that day's unflushed ring rows; today is read incrementally (only appended bytes); midnight rollover closes the day. Tests in §10 |
| R2-D3 | `agent/server/app.py` 23, 53-74 use `JSONResponse` | §7 and C2 AC8b: `JSONResponse(body)` (skips `jsonable_encoder`, keeps `allow_nan=False`); §9 row and test (a NaN raises) |
| R2-D4 | — (definition gap) | D14: for 全部, `effective_start = coverage.archives`, `length = now − effective_start`; 新加入 excluded only from the same-model average tick, kept in 各机型实测产能 (already normalised). §8.6, C3 AC11, C4 AC1, model tests |
| R2-D5 | — (test precision) | §10 OD1 guard names the banned phrases (progress %, 「剩余 N 部」 / any 「剩余」, 预计 / ETA / 完成度, completion date or time) and asserts the 「GPU 在线 N / M」 tile is present; C3 AC10, C4 AC6, C5 AC6 refer to it |
| R2-D6 | — (alarm fatigue in long ranges) | D16: rows of a burst are always highlighted; the red notice only when the burst ends within the last 24 h or has a `needs_action` row. §8.4 item 8, C5 AC3, gating tests |
| R2-D7 | `rt.clock.now()` is the clock spans are stamped with (§6.2) | Gap rule trips when the monotonic delta **or** the wall delta exceeds 90 s. §6.2, C1 AC6, tests for each clock and the 89 s non-trip |
