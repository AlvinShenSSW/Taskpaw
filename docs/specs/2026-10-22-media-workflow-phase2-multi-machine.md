# EPIC #271 phase 2: multi-machine media workflow (design draft)

Date 2026-10-22 · Status: v1 draft for debate, then child issues · Base: main 3b399e2 (phase 1) + #286 (Mac Hub monitor) + PWR-1..3 (power).
Binding inputs: EPIC #271 「当前规划」, the 2026-10-22 phase-2 comment, the #286 decision, T03 #274, T04 #275, T05 #276, constitution.

## 1. Goals and non-goals

**Goals**
- Run restore (Jasna LTX) and ASR (WhisperJAV) for any confirmed film on any enrolled GPU worker on the home LAN. Translate, return, Miko scrape (with the 已修复 + 中文字幕 facts), 确认入库, archive and cleanup stay on the controller, exactly as in phase 1.
- Use TaskPaw's own HTTP for transfer, no SMB. The source goes to the worker's local NVMe staging (resumable, hash-checked). The next 1–2 routed films are prefetched, with a cap of about 500 GB. Outputs are uploaded into the controller's work dir, verified, and only then is the worker's staging cleaned.
- Remote work looks exactly like local work in 媒体处理中心: the same step line, `LtxProgress`, 本片参数, 用时, live W and per-film energy.
- Device choice in 待处理: 「自动」 by default (earliest predicted finish among eligible machines, lower energy as the tie-break), or a pinned machine.
- The 设备 page goes from 「后续」 to real: list, add/pair, approve, health, eligibility, 停领/恢复, revoke, retire.
- A worker shows its own 「媒体处理」 monitor.
- Never double-run a stage. Never treat a lost reply or a lost heartbeat as proof that a process stopped.

**Non-goals (home LAN, single user)**
- No TLS, no multi-user permissions, no automatic credential rotation. FB-01 decided that the LAN is trusted.
- No cross-machine resume of a half-finished LTX run, no concurrent GPU stages on one card, no WAN.
- No Windows Hub migration in this phase (section 8).
- No change to the Mac Hub's role (#286). No controller polling of `/events`.
- No new installer. Workers use the same Windows TaskPaw build.

## 2. Topology and trust

| Machine | Role | Runs |
|---|---|---|
| Windows controller (the Agent console in phase 2) | `workflow.role: controller` | Workflow store, INBOX intake, 待处理, scheduler, worker API, translate, return, Miko scrape, 确认入库, Filmpaw archive and cleanup. Its 5060 8 GB does **not** run Jasna: `local_stages: [translate]`. |
| GPU workers (5070 Ti 16 ×1, 5060 Ti 16 ×3; see OD6) | `workflow.role: worker` | Restore and ASR only. Local staging, the 「媒体处理」 monitor, the PowerSampler. No store, no Miko, no Filmpaw, no NAS credentials. |
| Mac mini | Hub (#286) | Read-only monitor and OpenClaw. The **only** consumer of agent `/events`. |
| Other agents | Plain monitoring | Unchanged. Being monitored never makes a machine a worker. |

### 2.1 Where worker→controller calls go (decision)

**Decision: the controller exposes a separate *worker API* on its own LAN port (default 5682), and workers are its HTTP clients.** It is a separate FastAPI app, mounted only when `role: controller` and `remote_workers: true`, and serves only `/worker/v1/*`.

Rejected alternatives:
- **(a) POST routes on the agent network app (5680).** Breaks the GET-only invariant that #286 tests.
- **(b) The controller pushes to workers.** Needs write routes on every worker's network app, and the controller would have to reach into workers.
- **(c) Workers call the controller's loopback control app (5681).** That app is loopback-only by the constitution and holds operator powers.

With the chosen design:
- Every write is a typed command on the caller's own assignments, authenticated by a per-worker credential.
- Commands from the controller to a worker (cancel, release, routes, pause) travel only in **heartbeat responses**. Workers never expose a write route.
- Operator actions (approve, revoke, 判定已停止, pins) stay on the controller's loopback control app.

### 2.2 Credentials, pairing, rotation

- **Credential.** Reuse T01 `principals` / `credentials` (`compute_worker`, `generation`) and `parse_bearer`: `Authorization: Bearer <credential_id>.<secret>`. The worker generates the secret; the controller stores only its digest. Lifetime is long, like the local runtime's.
- **Pairing.** The operator adds a device on 设备. This runs `register_worker` (controller-generated `device_id`) and then `issue_pairing` (ticket, 15 min expiry, single use). The page shows a copyable code `TP1:<controller host>:5682:<pairing_id>:<ticket>`. On the worker's console, the 「加入媒体工作流」 wizard takes the code and calls `POST /worker/v1/pair`. That call runs `authenticate_pairing` and `complete_pairing`; the server fills `worker_id` and `device_id` from the pairing row.
- **Validation.** The worker probes its GPU, VRAM, per-tier LTX model presence, WhisperJAV, free disk and machine/GPU identity, then posts `record_capabilities` (worker role, no `approved`). The 设备 page shows the probe. **核验并启用** records `approved` profiles; the operator can narrow 4K. Then `update_worker` sets `participation=1` and `enabled`.
- **Rotation is 重新配对.** It issues a new ticket; `complete_pairing` takes generation+1 and revokes the old credential. The worker goes back to `pending_validation` until approved again (D5 auto-approves unchanged hardware). No scheduled rotation (FB-01).
- **Revoke and retire.** Use the T01 commands. Revoke isolates the worker's open assignments to `unknown` and returns 401 on its next call. A revoked worker stops its job and waits. Retire requires that no unresolved assignment exists, and it **never** calls `Store.remove_server`. Joining again means a new `worker_id`. The physical-GPU identity hash still blocks a new identity while an old owner is unresolved; T01 already enforces this.

### 2.3 Exposure invariants (tested)

- The worker API binds the agent's LAN `bind_host`. It is refused on a global address (`bind_is_global`), and port 5682 in use fails loudly (constitution §3).
- Only `/worker/v1/*` is mounted. Every route needs a worker credential, except `/pair`, which needs a ticket. No operator credential is ever accepted, and nothing forwards to 5681.
- The agent network app keeps all-GET (the #286 test stays). On a worker, `/workflow/*` returns 404, so the Mac Hub never picks a worker as its workflow source. Workers emit no `media-workflow` events: the controller's observer (#286 B) already covers remote films.
- Bodies are capped: JSON 1 MiB, heartbeat 64 KiB, upload chunk 256 MiB. Paths are allow-listed (section 3.5). Every response carries `X-Taskpaw-Worker-Protocol: 1`; a mismatch returns 426, shown on 设备.

## 3. Protocol

### 3.1 Endpoints (controller, port 5682)

| Method and path | Store effect | Purpose |
|---|---|---|
| `POST /worker/v1/pair` | `complete_pairing` | Ticket → credential |
| `POST /worker/v1/capabilities` | `record_capabilities` (worker) | Probe report |
| `POST /worker/v1/boot` | `record_boot` | New boot, plus a reconcile report of local attempt files (section 3.8) |
| `POST /worker/v1/heartbeat` | `renew` every 10 min | Live state in; routes, cancel, release, pause out |
| `GET /worker/v1/films/{film}/{rev}` | – | Route envelope: input manifest, plan tier, work-copy item list, current ledgers of earlier stages with their digests |
| `GET /worker/v1/films/{film}/{rev}/items/{item_id}` | – | Source bytes, `Range` supported, `X-Sha256`; only for a film routed to this worker |
| `POST /worker/v1/claim` | `claim` | Claim the routed film's next compute stage |
| `POST /worker/v1/attempts/{a}/outputs` | – | Upload plan; returns per-file received offsets; 507 when space is short |
| `PUT /worker/v1/attempts/{a}/files/{path}` | – | One chunk: `Content-Range`, `X-Chunk-Sha256` |
| `POST /worker/v1/attempts/{a}/finish` | `resolve_assignment` (operator) | Outcome, ledger, owner-stopped evidence, timing, energy |
| `GET /worker/v1/attempts/{a}` | – | State query for unknown outcomes |

The HTTP layer turns the Bearer into an `AuthProof` and calls the existing store commands. The store stays the only writer, and the controller remains the single authority (epoch 1, fencing).

### 3.2 Per-film life on a worker

```
route queued → fetching (download, verify) → staged
  → [gpu_lease acquired] claim restore → running (Job, LTX) → settled
  → uploading → finish → controller verifies → resolved(succeeded)
  → claim asr → running → settled → uploading → finish → resolved
  → route done → release → staging deleted
```

- **Claim only after the source is staged and verified,** the tier's models are present and `gpu_lease` is held. Download failures therefore never create an attempt.
- **GPU first.** The worker keeps the process-wide `gpu_lease` (shared with manual Jasna/avsubs tasks, as in local C5). It refuses to claim when the GPU shows unknown use: memory or utilisation above the PW03 threshold with no lease holder. It reports 「GPU 有未知占用」.
- **Uploads hold the GPU (D3).** The GPU owner stays reserved until resolve, so uploads sit between GPU stages: about 1–2 min for a 40 GB restore at 5–10G. A `release_resource` command that frees the GPU once processes have settled is deferred.

### 3.3 Heartbeat, lease, timeouts

**Worker → controller,** every 5 s while it has a route or claim, every 15 s when idle:
- `boot_id`, `state`, `paused`;
- `gpu {name, vram, used, util, power_w, hold: none|workflow|manual(label)|unknown}`;
- `disk {free, staging_used, cap}`, `tiers_ready {standard, good, high}`, `asr_ready`;
- `current`: the dispatcher's `current` dict verbatim (`film_id, stage, progress, elapsed_s, ltx{…, console, params, timing}`);
- `transfers [{film, dir, done, total, rate}]`, `local_owner` for the running attempt, `version`.

The controller turns `elapsed_s` into `started_at` on its own clock, so worker timestamps are never compared.

**Response:** server time, global `paused`, the worker's ordered `routes` (position 0 is active, 1–2 are prefetch), `cancel [attempt_id]`, `release [film/rev, attempt_id]`, `reconcile_needed`.

**Renewal.** The controller calls `renew` itself, with the worker's proof and the reported `local_owner`, when the last renewal is more than 10 min old. The store lease stays 24 h (`LEASE_SECONDS`). Progress is held in memory and never written per tick.

**Timeouts (controller clock):**
- No heartbeat for 30 s: 「心跳延迟」.
- 120 s: 「失联」. No new routes.
- 5 min offline: its *unclaimed* auto routes are withdrawn.

A lease or heartbeat timeout **never** resolves, re-runs or re-routes a claimed stage.

### 3.4 Idempotency and CAS

- **claim.** The worker generates `attempt_id`, `operation_id` and `idempotency_key = claim:<attempt_id>`, and writes the exact request body to its local attempt file *before* sending. A retry sends the same bytes, and `request_results` returns the same result. The store checks the job version (CAS) and the route (section 6.1).
- **finish.** The controller first reads the assignment. If it is already resolved for this attempt, it returns that outcome; this replaces key-based replay, because resolve evidence carries fresh IDs. Otherwise it verifies and resolves. A non-current attempt gets 409 `stale_attempt`, and the worker discards that attempt.
- **Uploads** are offset-CAS: a chunk must start at the current received size, otherwise 409 with the offset.

### 3.5 Transfer

- **Download.** Items are the intake *work copies* (never the user's INBOX original), from `manifest.readiness.workcopy`.
  - The worker writes `<staging>/work/<copy.relative_path>.part`, the same relative layout as the controller's work dir. It resumes with `Range: bytes=<part size>-`.
  - It hashes while writing (re-hashing the existing part when resuming) and checks size and sha256 against the manifest, then renames atomically. A mismatch deletes the part and fetches again.
  - Starlette `FileResponse` (1.3) serves the range in 4 MiB chunks. If T09 shows that one stream cannot fill 5–10G, use two parallel ranges per file.
- **Upload.** Executors write into `<staging>/work/<attempt_id>/…`, as they do locally.
  - Plan: `POST …/outputs` with `[{path, size, sha256}]` from the stage ledger. Paths must sit under `<attempt_id>/`, with no `..` and no absolute or alternate-stream names. The plan is kept in `work/.incoming/<a>/plan.json`.
  - Chunks: each 64–256 MiB chunk carries `X-Chunk-Sha256`. A chunk that fails its hash gets 422 and only that chunk is re-sent.
  - Completion: when the received size reaches the total, the controller hashes the whole file and renames `.incoming/<a>/<path>.part` to `work/<path>` atomically.
- **Finish.** The worker sends the `stage-result.json` bytes. The controller runs `check_file` on every `FileRef`, writes the ledger atomically to `work/<a>/stage-result.json`, and resolves with `payload_digest = sha256(ledger)`.
  - Downstream code is unchanged: translate reads the ASR ledger, and return hard-links the restored video from the same volume.
  - Three whole-file mismatches end in failed `needs_attention`; retry re-runs the stage.

### 3.6 Backpressure and disk

- **Controller limits.** At most 2 outbound source streams and 2 inbound uploads in total, and 1 of each per worker. Beyond that: 503 with `Retry-After`.
  - Streams for an active (position 0) route are admitted before prefetch.
  - Hashing runs off the event loop. Heartbeat handlers never touch disk.
- **Controller disk.** An upload plan needs free space ≥ declared + 100 GB reserve, because Miko and the archive also stage there. Otherwise 507 and 「主控磁盘空间不足」.
  - The stage stays running and the upload retries with backoff. This is not a failure.
- **Worker disk.** `need(film) = source + 1.2 × source + 2 GB`. Prefetch only while `staging_used + need ≤ cap (500 GB)` and `free − need ≥ 50 GB`.
  - The worker reports `disk` so the scheduler sees the same limits.

### 3.7 Prefetch, pause, drain, cancel

- **Prefetch.** One download thread on the worker, priority to position 0, then positions 1 and 2, within budget. A withdrawn route's staging is deleted when `release` arrives.
- **暂停派发** (controller, global, already in phase 1). No new routes or claims; in-flight work and prefetch continue.
- **停领** (device, `admission_state=draining`). Claims are refused. Queued auto routes are withdrawn and re-routed; pinned films wait with 「设备停领」. **恢复** sets `enabled`.
- **Worker-local 暂停领取** (on the worker's own monitor, loopback). It reports `paused`; the scheduler treats the worker like draining but leaves its routes in place.
- **停止并重新排队** (controller, per running film). The attempt ID goes into `cancel`. The worker cancels its Job, settles it and finishes as failed `cancelled`. That is a free retry, and the scheduler routes the film again (a pinned film goes back to the same machine). Stopping just one film has no separate hold (D12).

### 3.8 Recovery and unknown outcomes (consistent with T01 / T08-P1a)

On every start the worker posts `boot`. A new `boot_id` isolates all of its open assignments to `unknown`, as `record_boot` already does. The worker then reports, for each local attempt file, whether its children are alive, whether a ledger exists, and which outputs are present. The controller settles each as in `settle_previous`:

| Situation | Result |
|---|---|
| Claim reply lost | Same-key retry returns the same assignment |
| Finish reply lost | Retry; the controller answers from the assignment's state |
| Worker or agent crash; Job closed; no ledger | Resolved failed `interrupted` (free retry), same route, staged source reused |
| Crash after the ledger was written; outputs intact | Upload, verify, resolve **succeeded** (unknown → succeeded through `resolve_assignment`, no GPU re-run) |
| Child still alive (driver hang) | Stays `unknown`; the boot is not reconciled, so T01 refuses claims. 设备 shows 「上次的任务进程仍在运行」 |
| Controller down mid-run | The worker finishes offline, keeps its outputs, retries upload and finish with backoff. A deadline that passed meanwhile does not matter: resolve does not check it while the attempt is current |
| Controller crash mid-upload | `.part` sizes survive; `outputs` returns the offsets |
| Partition for longer than 2 min | 「失联 · 结果待核对」; the claimed stage stays put; unclaimed auto routes move after 5 min |
| Machine gone for good | Operator **判定已停止** (dialog: 「我确认该机器已关机或任务已停止」). The controller resolves failed `cancelled` with operator evidence `operator_attested_stopped`; this also frees the GPU owner. The film is re-routed. If that worker returns later it gets `stale_attempt`, kills the job and discards it (D4) |
| Credential revoked mid-run | 401: the worker stops its job. The assignments are already `unknown` through revoke, and 判定已停止 settles them |

Error classes reuse T08-P1a:
- **Transient:** download errors before the claim, and upload/network errors after it. These are retried; they are never a GPU failure.
- **Needs attention:** repeated hash mismatch.
- **Cancelled / interrupted:** free retries.
- **Bounded:** `MAX_AUTO_RETRIES` applies unchanged.

## 4. Scheduler (controller, `workflow/scheduler.py`)

It runs on its own thread: on plan confirm, attempt resolve, a heartbeat state change, and every 30 s. Its functions are pure over a snapshot of plans, routes, workers, heartbeats and history, and are tested with a fake clock.

### 4.1 Eligibility (worker × film)

All of the following must hold:
- `verified`, participating, `enabled`, not paused, online (heartbeat under 120 s), boot reconciled, protocol matches;
- an approved profile covers `restore` and `asr`, with `max_width/height ≥` the film's largest probe (8 GB: 1080p; 16 GB: 4K once child P2-6 lifts the phase-1 cap);
- `tiers_ready[plan tier]` and `asr_ready`;
- the disk rule (section 3.6) holds, counting that worker's queued routes.

Each failing rule becomes the reason shown in 待处理 and on 设备: 「显存不足 4K」「缺少高画质模型」「停领」「离线」「空间不足」.

### 4.2 Predicted finish

- **Rate samples.** `wall_restore / film_duration` from each succeeded restore attempt (`summary.timing.wall`, `worker_id`, plan tier, resolution class 1080p/4K), keyed by (worker, tier, resolution class). The estimate is the median of the newest 10.
  - Fallbacks in order: the same GPU model on other workers; the same VRAM class; none (「无历史」).
  - ASR uses the same pattern (ASR attempt wall / duration).
- **Transfer rate.** An EMA of that worker's measured transfers; 500 MB/s by default.
- **Formula.** `finish(w,f) = free_at(w) + fetch(f,w) + restore(w,f) + upload(f,w) + asr(w,f)`.
  - `free_at(w) = now + remaining(current) + Σ est(queued routes on w)`.
  - `remaining = est − elapsed`, floored at 2 min.
  - A manual GPU hold adds 30 min.
  - `fetch` is 0 once the film is staged.
- **Energy.** `est_restore × median W` for (w, tier, resolution) from `summary.energy` (PWR-2). If unknown, no energy tie-break.
- **No history anywhere** (D10): the restore estimate is the film duration on every worker, which in practice means FIFO onto the least-loaded eligible worker. The reason line says 「无历史，按排队先后」.

### 4.3 Assignment pass (greedy, stable)

1. **Candidates.** Confirmed films whose current revision has restore or ASR pending and no open route. 4K films first (so 16 GB machines are never starved by 1080p), then confirm time.
2. **Pinned films.** Route to the pinned worker if it is eligible and has a free slot. Otherwise the film waits with the reason; it is never moved automatically.
3. **Auto films.** Compute `finish` for every eligible worker with fewer than 3 open routes (1 active + 2 prefetch) and pick the minimum.
   - If two finishes are within max(10 min, 5 %), take lower predicted energy, then fewer routes, then name.
   - A 1080p film may take a 16 GB worker only if no unrouted 4K film is waiting.
   - With no eligible worker the film waits: 「等待设备：…」.
4. **Persist.** `route_film` (CAS); the worker learns of it at its next heartbeat.

Routing is late: the 待处理 recommendation (「自动 · 预计 21:40 在 5070 Ti 完成 · 约 0.9 度」) is advisory until the pass routes the film.

### 4.4 Re-assignment rules

- **Unclaimed route** (queued/fetching/staged). It is withdrawn when:
  - the worker becomes ineligible: drained, revoked, offline > 5 min, model or disk missing;
  - the operator changes the film's pin (改派).

  It is never withdrawn to chase a slightly better estimate.
- **Claimed stage** (unresolved assignment). Never moved automatically. Only reconciliation by that worker, or 判定已停止, resolves it.
- **Between restore and ASR.** The route stays (the source is staged there). If that worker becomes ineligible, ASR may be re-routed: the new worker downloads the source, and the envelope carries the restore ledger for subtitle stems.
- **Never double-run.** This is the T01 `one_unresolved_assignment_per_job` and `one_active_owner_per_resource`, plus the new route check at claim and route CAS. A withdrawn route rejects the claim.

## 5. Worker side

- **Module.** `workflow/remote_worker.py`, with `WorkerHost` mirroring `LocalWorkflowHost`: start after the agent is ready, stoppable, joins its threads (constitution §4). It contains:
  - `WorkerClient`: HTTP with `ProxyHandler({})`, no redirects, timeouts;
  - `RemoteDispatcher`;
  - `Staging`;
  - an identity file `worker-identity.json` in the data dir, holding the secret like the local identity file, gitignored.
- **Config** (`agent.yaml`): `workflow.role: worker`, `controller_url` (from the code), `staging_dir` (local NVMe, refused if UNC or a network path), `staging_cap_gb: 500`, and the tool paths as today (`jasna_exe_path`, `whisperjav_exe_path`, `ffprobe_path`, `ltx_gpu`). Executors: restore and ASR only.
- **Shared execution.** Extract the run section of `LocalDispatcher._execute` (OwnedJob kill-on-close Job, progress callback, settle, `_Unsettled` handling) into one `run_stage()` used by both dispatchers.
- **`ExecRuntime` protocol.** `work_dir`, `gpu`, `stage_result(film, stage)`. `LocalRuntime` implements it through the store; `WorkerRuntime` implements it through the envelope's ledgers (digest-checked). `exec_common.output_stems` and `load_stage_result` move behind it. The executors are otherwise unchanged.
- **Staging layout.**
  - `staging/work/<workcopy paths>`: per film-revision, shared by its attempts and retries (D2).
  - `staging/work/<attempt_id>/`: per-attempt outputs.
  - `staging/attempts/<attempt_id>.json`: request body and children, for crash recovery.
  - `staging/routes.json`.
  - GC deletes only what `release` names, or, at boot, whatever the controller's boot reply does not list as live. Nothing is deleted while it is unknown.
- **GPU.** The same `gpu_lease` as manual tasks. A manual Jasna task on a worker simply makes the workflow wait (「GPU 被手动任务占用：…」), and the scheduler sees `hold: manual`.
- **Model downloads.** The H3 offer appears in the worker's own monitor and is approved there (D6). Until then 设备 shows 「缺少<tier>模型」 and that tier is ineligible.
- **「媒体处理」 monitor** (Agent console tab, shown when `role: worker`). It reads the loopback `GET /control/workflow/worker` and shows:
  - the controller connection (地址, 心跳, 凭据状态);
  - the routed and claimed films (cover from the envelope, name, position);
  - the step line 下载 → 修复 → 回传 → 识别 → 回传, with the existing `LtxProgress`, 本片参数 and 用时;
  - transfers with their rates, staging used out of the cap, and the GPU hold;
  - controls: 暂停领取 and 下载模型 only. No approval, archive or NAS controls.

  V3 styling, no slogan headings (decision 7).
- **Power.** The PW01 sampler runs on the worker. The PW04 owner counter per `film_id` is read at claim and at settle. `finish` carries `energy` (EnergyDelta, or null for 「部分」, never 0). The heartbeat carries live W.

## 6. Controller side

### 6.1 Store v4 (verified backup, then one-transaction upgrade, as for v3)

- **`film_plans` rebuilt** with `machine` **nullable** (NULL = 自动). `confirm_plan` checks that the machine is verified only when one is given. Migration converts the plan of every still-pending restore to 自动 and keeps the rest (D9).
- **New `film_routes`:**
  - columns `film_id, input_revision, worker_id → workers, route_seq, state ∈ {queued, active, done, withdrawn}, routed_at, reason, version, body_json`;
  - unique index on `(film_id, input_revision) WHERE state IN ('queued','active')`;
  - commands `route_film` and `withdraw_route` (operator, CAS).
- **`claim`** for restore and ASR additionally requires an open route for this worker (it then sets `active`). Translate needs no route: only the controller's worker has a translate profile. The local dispatcher considers only jobs routed to itself.
- **`attest_stopped`** (operator) resolves an unknown or orphaned assignment as failed `cancelled`, with explicit attestation evidence, and resolves the resource owner with it.
- **`EvidenceSummary.energy`** from PWR-2. `RestoreTiming` comes from the worker's finish payload. No transfer table: upload state lives in `.incoming` and heartbeats.

### 6.2 Controller runtime

- `role: controller` keeps phase-1 behavior.
- New settings:
  - `local_stages` (default `[translate]` once the first remote worker is enrolled; all stages for single-machine);
  - `remote_workers: true`;
  - `worker_api_port: 5682`.
- The worker API is a third uvicorn server claimed up front, like 5680/5681 in `launcher.py`.

### 6.3 Board, drawer, 待处理

- `film_rows(…, currents: dict[film_id, current])` merges the local dispatcher's current with the remote heartbeats. Rows gain `machine {id, name}`, `transfer`, `heartbeat_age`.
- The step line is unchanged. Progress text reads e.g. 「下载源文件 12.4/38.0 GB · 610 MB/s」 → LTX → 「回传 …」.
- The drawer adds a 设备 line and energy per stage with its machine.
- Scheduler reasons merge into `waiting`.
- 待处理's machine select:
  - 「自动（推荐）」 first, with the predicted finish and energy;
  - then each device with its own predicted finish, or greyed with its eligibility reason.
- 改派 on a queued film changes only its pin (a new plan revision is not needed: pins live in the route and the plan's `machine`). This is allowed until the claim.
- The JSON is additive, so #286's read-only Mac view shows remote rows unchanged.

### 6.4 设备 page (from 「后续」 to real)

- **Rows:** name, address, role, enrollment/admission, 心跳 age, GPU/VRAM, an eligibility matrix (1080p/4K × 标准/良好/高: ✓ / 缺模型 / 显存不足), disk free / staging used, current film and stage, route depth, live W, today's 度 (估算).
- **Actions:**
  - 添加设备 (pairing code), 核验并启用 (shows declared vs probed), 停领/恢复, 重新配对, 撤销, 退休 (blocked while owners are unresolved);
  - 判定已停止 per unknown assignment.
- **本机** shows its stages (翻译).
- **Health** comes from heartbeats. Fleet power reuses PWR-3: enrolled workers are automatically peers for `/status` and `/power/history` (GET only, the controller's own api_token). The controller never reads `/events`.

## 7. Mac Hub and events

Unchanged. The Mac Hub reads the controller's `/workflow/*` and stays the only `/events` consumer. Workers answer `/status` (adding a `workflow_worker` summary) but no `/workflow/*`. Notifications for remote films come from the controller's #286 observer, so nothing is notified twice.

## 8. Windows Hub packaging (T05): smallest step

**Phase 2 stays on the Windows Agent console.** The workflow host already lives in the agent process and the store is process-agnostic (one SQLite file under its FileLease), so moving it gains nothing for multi-machine work.

The smallest T05 step is:
1. `workflow.role` plus the worker wizard in the existing Agent console (paste the code, staging dir, tool and model check, explicit enable). Same installer, ordinary agents untouched, no auto-enable on upgrade.
2. A deployment guide and an addendum to the T09 checklist.

The Windows-Hub-hosted controller becomes a later child after phase 2 passes on real machines. That move would be: drain, stop the agent workflow host, start a Hub-mounted host on the same data dir, move port 5682, and re-point workers' `controller_url` through a heartbeat redirect. It is not designed further here.

## 9. Testing strategy

- **Fake worker.** An in-process `WorkerClient` against the real worker API (uvicorn on loopback), with fake executors (`launch_prefix` fake Jasna/WhisperJAV), a fake GPU and a fake sampler. It covers pair → probe → approve → boot → route → download → restore → upload → finish → controller translate/return/scrape (fakes) → 待确认.
- **Five-worker simulation.** Fake clock, scheduler property tests:
  - 8 GB never routed or claimed 4K; pinned films respected; energy tie-break; 4K priority;
  - only unclaimed routes move; at most one open route per film;
  - random crash and partition sequences never produce two unresolved assignments or a second GPU owner.
- **Transfer.** Resume at random offsets (both directions); a corrupted chunk (422, resend one chunk); whole-file mismatch; Range resume; traversal, ADS and absolute paths refused; oversize; 507; concurrent PUT to one file (409); controller restart mid-upload; prefetch budget and cap; priority of active over prefetch; 503 backpressure.
- **Crash matrix.** {worker crash, controller crash, partition, revoke} × {before claim reply, mid-download, mid-run, after settle before upload, mid-upload, after the last chunk before finish, after resolve before reply}. Each cell asserts the end state from section 3.8, no GPU re-run when ledger and outputs exist, and stale-result rejection.
- **Security.** Enumerate the worker API routes: all authenticated, `/pair` ticket-only, no operator credential accepted. The network app stays all-GET; workers' `/workflow/*` returns 404. Global bind refused; port collision fails loudly; no secret in logs or argv.
- **Store.** v3→v4 migration with backup and plan conversion; route CAS; claim route check; `attest_stopped`; idempotent replays.
- **UI.** Fixture-driven tests for 设备, the 待处理 machine select, remote rows and drawer, and the worker monitor; 390 px; no slogans; the read-only Mac view shows remote rows.
- **待实机 (T09).** 5G/10G throughput of one vs two streams, five-machine mixed 1080p/4K, real Jasna crash with Job kill, real power numbers.

## 10. Rollout (child issues, each shippable)

1. **P2-1 Store v4, routes, scheduler core.** Nullable plan machine, `film_routes`, claim route check, `attest_stopped`, pure scheduler with the prediction model.
   *Acceptance:* single-machine phase 1 works unchanged through local routes; the scheduler property suite passes; migration and backup are tested.
2. **P2-2 Worker API skeleton and enrollment.** Port 5682, pair/capabilities/boot/heartbeat, worker identity and wizard, 设备 page v1 (list, add, code, probe, approve, 停领/恢复, revoke, retire, health).
   *Acceptance:* a fake worker pairs, is approved and shows online; no claims yet; security tests pass.
3. **P2-3 Transfer.** Envelope, ranged download, upload plan/chunks/commit, verification, backpressure, disk rules.
   *Acceptance:* the transfer test matrix passes.
4. **P2-4 Remote execution end-to-end.** `run_stage()` and `ExecRuntime` refactor, `RemoteDispatcher`, claim → finish, boot reconciliation, cancel and release through heartbeats, staging GC, prefetch, remote currents in board and drawer, `local_stages`.
   *Acceptance:* fake-worker end-to-end and the crash matrix pass; one real worker on the LAN (待实机).
5. **P2-5 自动 / pinned in 待处理 and live scheduling.** Predicted finish and energy shown, re-route rules, 改派, 判定已停止 UI.
   *Acceptance:* five-worker simulation; the UI fixtures.
6. **P2-6 4K on 16 GB.** Restore `max_resolution` by VRAM with the 4K preset (#265).
   *Acceptance:* 8 GB is never 4K-eligible; 16 GB is approved for 4K only through a verified profile.
7. **P2-7 Worker 「媒体处理」 monitor and power.** Per-film energy from workers, live W, enrolled workers as power peers.
   *Acceptance:* UI fixtures; energy missing (never 0) on a cut-off attempt.
8. **P2-8 Roles, docs, T09 addendum.** Wizard polish, deployment guide (single consumer, ports 5680/5681/5682), real-machine checklist.

Order: 1 → 2 → 3 → 4, then 5, 6 and 7 in parallel, then 8.

## 11. Decisions for the operator

- **D1.** The controller's 5060 does translate only (no ASR, no restore). Alternative: allow ASR there too.
- **D2.** Source staging is per film-revision and shared by restore, ASR and retries, rather than strictly per attempt; outputs stay per attempt. This avoids re-downloading 20–60 GB on a retry.
- **D3.** An upload holds the worker's GPU slot until verified (minutes). Alternative: an extra `release_resource` command.
- **D4.** 判定已停止 (your attestation) is the only way to move a *claimed* film off a vanished machine.
- **D5.** Auto-approve a re-probe when the hardware identity and VRAM are unchanged and only tool versions differ.
- **D6.** Phase 2 approves model downloads on each worker's own console. Alternative later: approve them from 设备 via a heartbeat command.
- **D7.** Phase 2 stays on the Windows Agent console, and the Windows Hub controller moves later.
- **D8.** Plain HTTP with per-worker Bearer on the LAN, long-lived credentials, rotation = 重新配对.
- **D9.** Plans still pending from phase 1 (pinned to 本机) become 自动 at the upgrade.
- **D10.** With no history anywhere, 自动 means FIFO onto the least-loaded eligible worker. The tie window is 10 min or 5 %.
- **D11.** Port 5682 for the worker API.
- **D12.** Per-film stop means 停止并重新排队 only; there is no separate per-film hold.

## 12. Risks

- **Python HTTP throughput at 10G.** Measure in T09; the fallback is two parallel ranges.
- **Executor refactor (`ExecRuntime`).** It touches phase-1 code; the existing executor regression suite gates it.
- **Attestation misuse** (a partitioned but alive worker). Mitigated by the dialog wording, the `stale_attempt` kill on return, and the physical-GPU owner index.
- **Controller disk.** Restored videos for several films land on the controller before archive. 507 backpressure and the 设备 or overview warning cover it.

### Critical files for implementation
- `taskpaw_v3/workflow/schema.py` and `film_store.py` (v4, routes, claim route check, `attest_stopped`)
- `taskpaw_v3/workflow/local_dispatch.py` (`run_stage()` extraction; local route filter)
- `taskpaw_v3/workflow/worker_store.py` (pairing, boot, capability flows the worker API drives)
- `taskpaw_v3/workflow/planning.py` with `plan.py` and `board.py` (自动 / pinned, `currents` map, machine rows)
- `taskpaw_v3/agent/server/launcher.py` (third server on 5682; worker role wiring)

## 13. Debate round 1 — dispositions (v2, 2026-10-22)

Critic findings checked against the u1 tree. All admitted unless noted; the text above is superseded where this section differs.

**Correctness (admitted)**
- C1 (P1) Executors are built from the stage set: translate is appended whenever it is a local stage, independent of `whisperjav_exe_path` (`pipeline.py:69-74`).
- C2 (P1) Plans stay immutable. `film_plans.machine` (nullable) is the operator's choice at confirm; the effective pin lives on a mutable CAS'd row (`film_routes` pending/pin). 改派 = withdraw the open route + set the pin.
- C3 (P1) Finish accepts a `FileRef` that is either uploaded under `<attempt_id>/` or equal (path, sha256, size) to a manifest work-copy item the controller holds (restore `reused`, ASR `reused_zh`/`source_ja`). Only the first kind is uploaded.
- C4 (P1) 判定已停止 is offered only when the worker is dead on both channels for ≥ 5 min: the controller's existing 5680 `/status` read (PWR-3 peer) must also fail or show no live attempt. Otherwise refused with the reason.
- C5 (P2) One `renew` right after claim (records `local_owner`, as `_execute` does); no periodic renewal — nothing sweeps `lease_deadline` and periodic renew races finish resolve.
- C6 (P2) The worker re-posts `boot` (same `boot_id`, idempotent) until the reply says `reconciled`.
- C7 (P2) Upload endpoints refuse attempts that are resolved or not current.
- C8 (P2) The scheduler stops routing when controller free space < Σ expected outputs of open routes + 100 GB reserve; the upload-plan check subtracts bytes planned but not yet received.
- C9 (P2) Routes are `{open, closed}`; "claimed" is derived from unresolved assignments. An open route with no unresolved assignment may be withdrawn. A new input revision closes the old route.
- C10 (P2) The claim route check applies to every compute stage; the controller's own routes are created implicitly.
- C11 (P2) The worker holds `ES_SYSTEM_REQUIRED` (SetThreadExecutionState) while it has an open route or claim.
- C12 (P2) Stages that need no GPU (all-reused restore, reused_zh ASR) run on the controller; no transfer.
- C13 Hash while receiving (sequential offsets; re-hash the prefix on resume); finish compares the recorded sha/size/mtime instead of re-reading.
- C14 After a controller restart, every worker's last-seen starts at boot time.

**Simplifications (admitted)**
- S1 One streaming `PUT` per file from the received offset, `GET` for the offset, whole-file sha, full re-send on mismatch. No per-chunk hashes, no 422 chunk protocol.
- S2 No protocol header / 426. A version mismatch is an eligibility reason 「版本不一致」.
- S3 Two timeout tiers: 60 s 失联 (no new routes), 5 min withdraw unclaimed routes.
- S4 Prefetch depth 1 (a 40 GB fetch ≈ 1 min on 10G vs hours of restore); keeps 自动 binding late. The 1080p-on-16 GB rule is dropped; 4K-first ordering covers it.
- S5 One stream per worker per direction; no global caps or 503 admission.
- S6 Prediction keyed on (GPU model, resolution class) × per-tier factor; fixed 1 GB/s transfer; no manual-hold heuristic.
- S7 Crash matrix by persisted state: {nothing persisted, claimed no ledger, ledger + outputs, resolved reply lost} × {worker restart, controller restart} + partition/attest + revoke (~10 cases).
- S8 Worker-local 暂停领取 uses the 停领 rules.

**Visibility (admitted)**
- V1 The chosen route's reason (the per-device finish times it beat) is persisted and shown in 待处理 / drawer, with the actual routed device.
- V2 A 失联 worker holding a claimed stage is a film attention state (so #286's observer and the Mac Hub surface it).
- V3 Scrape markers come only from controller-verified ledgers (already true: `scrape_step.py:208,453`).

**Controller GPU (C-D1, operator decides).** 本机 becomes an ordinary in-process routable worker (fetch/upload cost 0). The memory-recorded operator decision is that the controller does not run Jasna, so its participation defaults **off**; turning it on lets 自动 use it for 1080p. This removes the `local_stages` auto-flip.

**Decisions changed:** D1 → C-D1 above; D3 kept with C8; D4 kept with C4; D5 dropped (a tool-version change needs one-click 核验); D9 no special migration — pending phase-1 plans pinned to 本机 stay pinned if 本机 participates, else convert to 自动; D10 kept with S6. D2, D6, D7, D8, D11, D12 kept.

**Rollout v2 (5 children):**
1. P2-1 Store v4 + scheduler core: nullable plan machine + mutable pin, `film_routes {open, closed}`, route check on all compute stages, `attest_stopped`, 本机 as a routable worker, executors from the stage set, pure scheduler.
2. P2-2 Worker API: pair, capabilities, boot, heartbeat, envelope, ranged GET, streaming PUT, disk rules; 设备 v1.
3. P2-3 Remote execution end-to-end: `run_stage`/`ExecRuntime`, `RemoteDispatcher`, boot re-post reconciliation, cancel/release, GC, keep-awake, minimal worker monitor, reduced crash matrix.
4. P2-4 待处理 自动/pin + board: remote rows, 改派, 判定已停止 with the `/status` cross-check, route reasons, energy.
5. P2-5 4K on 16 GB + T09 addendum + deployment docs.

## 14. Operator decisions (2026-10-22)

- **OD1 (C-D1).** The controller's 5060 takes no GPU stage: no restore, no ASR. It is the controller and runs translate and other jobs. 本机 is never a routable GPU worker; `local_stages` is fixed to the non-GPU stages (translate, and C12 no-GPU stages). The 本机 routable-worker idea is dropped.
- **OD2 (失联).** A lost worker raises an error for the operator; nothing is moved automatically.
  - At 60 s without a heartbeat the worker is 失联. Every film routed to it, claimed or not, becomes a film attention state 「设备失联」 (visible in 媒体处理中心, surfaced to the Mac Hub by the #286 observer). No new routes go to it.
  - Retries continue in the background: the worker keeps reconnecting and resumes downloads and uploads with backoff. If it comes back, its films continue and the error clears by itself.
  - The operator resolves it by hand: 改派 for an unclaimed film, 判定已停止 (with the C4 two-channel check) for a claimed one.
  - S3's automatic 5-min withdrawal of unclaimed routes is removed.
- **OD3.** Per-film stop is 停止并重新排队 only (D12 kept).
- **OD4.** A tool-version change needs a one-click 核验 on 设备 (D5 dropped).
- **OD5 (8 GB cards).** GPUs with < 16 GB VRAM never take restore (LTX): a fixed eligibility rule (「显存不足，不参与修复」), not a toggle. 8 GB workers take ASR only. 16 GB workers take restore (1080p and 4K) and ASR. The 设备 page keeps per-device 修复/识别 switches only for ≥ 16 GB cards (识别 for all). With no history, 自动 sends restore to 16 GB cards only and ASR to whichever eligible card is free first, so 8 GB cards absorb ASR while 16 GB cards restore.
- **OD6 (fleet, corrected).** The controller is the 5060 8 GB (本机); it runs no GPU stage and also does other jobs (translate etc.). The GPU workers are **3 × 5060 Ti 16 GB + 1 × 5070 Ti 16 GB**; all four take restore (1080p and 4K) and ASR. There are no 8 GB workers, so OD5 only guards against a future one. The §2 topology row "5060 8 ×2" is wrong. Restore and ASR of a film normally run on the same worker (source already staged), so ASR needs no extra transfer.
- **OD7 (operator, 2026-10-23 — supersedes OD1/OD6 for ASR).** GPU workers run **restore (Jasna) only**. The controller's 5060 8 GB runs **ASR (WhisperJAV)** plus every no-GPU stage (translate, return, Miko scrape, confirm, archive, cleanup); it still never runs restore. Rationale: the 5060 is too slow for LTX and otherwise idle; the restored video comes back to the controller anyway. Consequences: ASR is never routed to a worker; workers need no WhisperJAV; the route closes after restore; the controller's ASR shares the process-wide `gpu_lease` with manual tasks on that machine. Later optimisation (not now): start ASR on the controller from the work copy while the worker restores.
