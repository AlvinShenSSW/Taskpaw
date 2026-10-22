# PWR: power estimation — per machine, per film, fleet tab

- **Parent:** EPIC #271.
- **Operator, 2026-10-22:**
  - The total wall energy is measured by a Xiaomi smart plug in front of the power strip, which is out of scope for TaskPaw.
  - TaskPaw provides a rough **estimate**: per-film energy and the instantaneous power distribution.
  - Two surfaces: power data in 媒体处理中心, and a separate 「功耗」 tab for **all** machines.
  - The UI/UX previews are approved: `design-system/taskpaw-v3/preview/media-workflow-v3-power.html` and `power-v3.html`, with three screenshots reviewed.
  - "本机测试版也加上这块的功能" (add this to the local test build too).
- **Principle P0:** a single-user home tool. Estimates are labelled 估算 / 约 everywhere; no billing claims; no electricity price.

## Estimation model

- **Machine power** P = GPU + CPU + 其他. PSU efficiency is optional and defaults to 「不计」, because a baseline calibrated against the plug already includes PSU loss.
  - **GPU:** measured with `nvidia-smi --query-gpu=power.draw` (sum over GPUs). A machine without an NVIDIA GPU has 0.
  - **CPU:** estimated as `idle + (tdp − idle) × utilisation` (psutil). `tdp` and `idle` are per-machine settings with sensible defaults (desktop 65/10 W, Mac mini 20/3 W). 「实测」 appears in the source selector as a disabled future option; there is no kernel-driver dependency in P0.
  - **其他:** a per-machine baseline in W (defaults: desktop 35, Mac 5).
- **Calibration helper.** The operator types the plug's total W at a moment. TaskPaw scales every online machine's 其他 baseline so that Σ estimates = plug reading, distributing the difference evenly over the machines; it shows before and after, and the operator confirms.
- **Energy.** Each machine integrates its own P with the trapezoid rule over 5 s samples. It keeps minute buckets for 48 h, hourly for 90 days and daily for ever, in a small SQLite under its data dir. Counters survive restarts; a gap is not invented (a dashed segment in the chart).
- **Activity attribution.** Each sample is tagged with what the machine is doing:
  - jasna_restore: the Jasna monitor or a workflow restore running;
  - asr;
  - translate;
  - idle (online, nothing running);
  - other.

  Precedence is GPU-owning activity first.

## PWR-1: per-machine sampling (agent and Hub self-monitor; Windows + macOS)

- **Where.** Extend the existing `host_metrics` monitor: add `power.draw` to its nvidia-smi query, and add CPU utilisation (already read). A 5 s sampler thread integrates and persists. On macOS, GPU is n/a; CPU and baseline only.
- **Exposed.**
  - `/status` → `power {w, gpu_w, cpu_w, other_w, estimated: true, today_wh, week_wh, month_wh, activity}`.
  - A read-only network route `GET /power/history?range=24h|7d|30d` with series per activity (bounded size), behind api_token.
  - Control (loopback) routes to read and update the per-machine estimation settings.
- **Settings** in agent.yaml under `power:`: enabled (default true), tdp_w, idle_w, other_w, psu_efficiency (null), cpu_source ("estimate" | "none").

## PWR-2: media workflow energy (媒体处理中心)

- **Per-film energy.** For each stage attempt that succeeded, energy is the machine's integrated energy over [attempt start, end]. GPU stages own the machine, so all of it is attributed. Non-GPU stages (translate, return, scrape, archive) get the machine's energy × (stage share). P0: the stage's own process CPU share is hard, so translate, return and scrape get only the CPU-utilisation delta over the idle baseline, labelled 「按占用分摊」.
  - Stored with the stage evidence (`summary.energy`), like `summary.timing`, so it survives cleanup.
- **Prediction for 待处理.** Per machine and tier, the median of the last N films' kWh per film-hour, times the film's duration, giving 「预计耗电：标准 ~x 度 · 良好 ~y · 高 ~z（按本机历史）」. With fewer than 3 films for a tier, the line shows 「历史不足」 for that tier.
- **UI** as the approved preview:
  - 概览 stat 「集群功耗」: live W, GPU/CPU/其他 bar, 今日约 x 度.
  - 正在处理 rows show live W.
  - The running 修复 step shows 「本机 286 W（GPU 210 · CPU 46 · 其他 30）· 估算」.
  - The drawer gets a 「能耗」 block (total, per stage with 用时, average and peak W, machine).
  - Queue foot: 「全程 x · 约 y 度」.
  - 待处理 shows the prediction.
  - No slogan headings (decision 7).

## PWR-3: 「功耗」 tab (all machines)

- **Data source in phase 1.** A top-level tab in the Windows Agent console, a transition exactly like 媒体处理中心. It shows the local machine plus **peers** the operator adds in the tab (name, address, api_token, stored in agent.yaml `power.peers`). The local backend fetches peers' `/status` and `/power/history` server-side (read-only, bounded, timeouts). Peers without power support show 「无功耗数据（需升级 TaskPaw）」.
- **Later.** The same component is mounted in the Hub dashboard. Which Hub (Mac monitor or Windows controller) follows the open Mac Hub decision (#286 vs T07).
- **UI** as the approved `power-v3.html`:
  - title row 「功耗」 + 24 小时/7 天/30 天 + 估算参数;
  - plug notice;
  - stats: all machines live, 今日/本周/本月 度, Jasna cluster live, per-film average;
  - machine list (role, activity, live W split, 24 h sparkline, today);
  - stacked power chart (inline SVG, legend toggles);
  - energy by machine × day/week/month and by activity;
  - 每片能耗 list;
  - 估算参数 dialog with the calibration helper and the peer list.

## Tests (each child)

- **Model:** the formula per source and the PSU option.
- **Integration:** trapezoid, gaps and restarts.
- **Buckets:** roll-up and retention.
- **Attribution:** activity precedence.
- **Per-film energy:** from attempts, plus the prediction median and the 「历史不足」 case.
- **Routes:** network GET-only, bounded, auth; peers fetch timeouts and errors.
- **UI:** fixture-driven render of every element in the previews; no slogan headings; responsive at 390 px.
- **Contract fixture:** updated.

## v2 (debate PW01–PW14; overrides the text above)

- **PW01. Where the sampler lives.**
  - It is an agent-level `PowerSampler` (core/power.py) with its own long-lived thread, not part of the host_metrics monitor.
  - The launcher starts it after the port claims and `supervisor.start()` and stops it on shutdown. Only `power.enabled` (default true) gates it.
  - The Hub self-monitor does NOT sample, so a machine is never counted twice.
- **PW02. One GPU query.**
  - A single `gpu_sample()` with a 4 s cache and a lock queries `utilization.gpu,memory.used,memory.total,power.draw`.
  - `power.draw` is parsed on its own, so `[N/A]` gives None power while the rest stays.
  - `read_gpu` delegates to it, so existing GPU% and VRAM consumers are unchanged.
- **PW03. Activity source.** Activity comes from `gpu_lease.holder()`, mapped to restore or asr (workflow or manual Jasna/avsubs/lada). Otherwise the workflow's running non-GPU stage gives translate or other. Otherwise the machine is idle. No heuristics.
- **PW04. One owner per sample.**
  - Each sample belongs to exactly one (activity, owner film_id | manual | none).
  - Per-film energy = the sum of that film's samples, read from a cumulative per-owner counter at claim and at resolve by the dispatcher.
  - The CPU-share apportioning is dropped.
- **PW05. Every attempt counts.**
  - `summary.energy` is written on every resolved attempt, failed ones included. `EvidenceSummary` gets an optional `energy`, dropped when None, like `timing`.
  - Film total = the sum over the current revision's attempts.
  - An attempt cut off by a crash has no value, so the UI shows 「部分」.
- **PW06. Peers.**
  - Peers are `{name, address}` only; requests carry this agent's own `api_token` (the Hub's single polling token already has to match every agent's).
  - Only the fixed paths `/status` and `/power/history` are fetched, with `ProxyHandler({})` and `NoRedirectHandler` (as in jellyfin.py), a 2 s timeout and a 256 KB cap.
  - A background refresh keeps a cache no older than 10 s, and routes return the cache, never waiting on a peer.
  - Public addresses are rejected (`bind_is_global`).
  - A minimal peer list (name + address) is added to the 估算参数 dialog in the approved V3 style. It is a small addition to the preview, so the operator does not have to edit YAML.
- **PW07. Storage.**
  - One table of 15-minute buckets (Wh per activity, plus GPU/CPU/other Wh), kept 35 days, in `power.db` under the data dir (in memory when there is no data dir).
  - Only the sampler thread writes, one INSERT per closed bucket. Routes read an in-memory window. No hourly or daily rollups; aggregates are computed when read.
  - Settings use `_EDITABLE_CONFIG` / `_LIVE_CONFIG` (no new control routes).
  - The calibration helper follows the preview: one chosen machine, `其他基线 = 插座读数 × 效率 − GPU − CPU`, computed in the browser.
- **PW08.** The Hub-dashboard variant is deferred until the Mac decision (#286 vs T07). A Mac running only the Hub has no row in P0, and the tab says so.
- **PW09. CPU sampling.**
  - CPU utilisation via `psutil.cpu_percent(interval=None)` on the sampler's own thread; the first reading is dropped.
  - CPU energy uses rectangles over the window average; GPU energy uses the trapezoid rule.
  - Time deltas are monotonic, and a gap over 15 s is a gap.
  - Defaults: win32 tdp 65 / idle 10 / other 35 W; darwin 20 / 3 / 5 W.
- **PW10.** All "measured CPU" options (powermetrics, LibreHardwareMonitor, RAPL) are shown disabled; everything is 估算.
- **PW11.** Day/week/month boundaries use local time, with the week starting Monday; buckets are stored as UTC epoch.
- **PW12.** Live W is shown only on the GPU-owning 正在处理 row; other rows show —.
- **PW13.** Prediction tiers are resolution × quality (4K/1080p × 标准/良好/高).
- **PW14.** Older builds cannot read `summary.energy` (strict validation). This is accepted and noted.
- **Machine fact.** The controller machine has an RTX 5060 8 GB (power limit 145 W, idle about 45 W). A 4K standard restore is expected well below the 5070 Ti numbers in the previews.
