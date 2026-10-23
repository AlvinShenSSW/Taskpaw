# P2-OD7 (#298): ASR on the controller, GPU workers restore only (frozen contract)

Date 2026-10-23 · Base `afk/claude-p2-271` 6d65b7d · Parent: [phase-2 design](2026-10-22-media-workflow-phase2-multi-machine.md) §14 OD7 (replaces OD1/OD6 for ASR). P2-1…P2-4 stay binding except where this contract changes them. Home LAN, single user, smallest complete change. **No schema change.** `remote_workers: false` (phase 1) behaves exactly as before. UI preview confirmed by the operator (#298 issue link).

## 1. Requirements

| # | Requirement | Source |
|---|---|---|
| R1 | **Carried first.** **CX27:** `RowExtra.lost` becomes `{id, name}`; rows gain additive `lost_worker{id,name}` from `scheduler.lost_films()`; the observer emits `device_lost:<lost_worker.id>`, so an assignment left on an older revision still notifies once. **CX28:** `preview`'s fallback for a not-yet-routable film applies the controller-disk gate through one helper `_disk_full` shared with `_route_films`. **CX33:** the reassign preview keeps the film's `confirmed_at` and its route's queue position; a kept route's estimate counts only the routes ahead of it on that worker (`scheduler.queued_finish`). **CX34:** test that 「本机」 shows for ASR and 「待派发」 only while restore is unfinished. | P2-4 §9 |
| R2 | **Only restore is routed.** `scheduler._ROUTED = ("restore",)`, `routing._GPU_STAGES = ("restore",)`, `local_dispatch.REMOTE_STAGES = {"restore"}`. In remote mode the controller skips only a restore that needs the GPU; it claims ASR locally (`needs_gpu` → `gpu_lease`) like translate. A skipped restore never becomes a `gpu_lease` waiter. | OD7 |
| R3 | **Controller ASR shares the process-wide `gpu_lease`** with manual Jasna/avsubs tasks. Manual task holds → ASR waits 「等待 GPU：<holder>」; ASR holds → manual task waits 「影片工作流：<name>」. Fair both ways (reservation). | OD7 |
| R4 | **The route closes once restore succeeds.** `film_store._GPU_STAGES = ("restore",)` for auto/pinned routes (完成 when restore finished, 失败 when failed/blocked). Local routes unchanged (restore, asr, translate). | OD7, P2-1 R6 |
| R5 | **Store enforcement.** An `asr` claim needs the open route to be the controller's own (`kind='local'`), else 409 `route_required`. Already-claimed assignments untouched. | OD7 |
| R6 | **Eligibility** drops the ASR rule and `WorkerLive.asr_ready`; worker checks are restore-only; < 16 GB is eligible for nothing (「显存不足，不参与修复」). `can_reassign`, `_pin`, `set_pin` "unfinished" checks look at restore only; `reassign_options` → `[]` when no stage is routed. | OD7, OD5 |
| R7 | **Preview.** A film with no routed stage left has no device option; 自动 reads `NO_GPU` = 「无需修复，本机完成」; the worker-ASR fallback is removed. | CX28, P2-4 R5 |
| R8 | **`AsrExecutor.max_resolution` → UHD** (WhisperJAV reads audio only), so a 4K film restored on a worker is transcribed on the 8 GB controller; the controller re-records its approved profiles at restart (as P2-3 R4). | OD7 |
| R9 | **Workers build only the restore executor** (`worker_executors`); no WhisperJAV needed; `Probe.asr_ready` removed; no `asr_ready` sent; `RemoteDispatcher.STAGES = ("restore",)`. The settle/finish/delivery path for an ASR attempt stays for in-flight work. | OD7 |
| R10 | **设备.** `approve(restore)` loses `asr`; `precondition_failed` when no restore profile. Rows drop `asr_ready`. Synthetic eligibility checks `("restore",)`. In remote mode the controller row lists `识别、翻译` (识别 only when an ASR executor exists). | OD7 |
| R11 | **Settings.** 主控 always shows WhisperJAV; Jasna only while 允许 GPU 工作机接入 is off; note 「开启后修复交给 GPU 工作机，识别仍在本机」. GPU 工作机 drops WhisperJAV (not shown/validated; existing yaml value kept). Remote mode, no ASR executor → ASR waits 「本机未配置 WhisperJAV」. | OD7, P2-4 R13 |
| R12 | **Upgrade migration** per §3. | OD7 |

## 2. Payloads (additive unless noted)

- Heartbeat `asr_ready` optional, default False, **ignored** (kept: `extra="forbid"` would reject an older worker). `CurrentLive.stage` keeps `"asr"`.
- Envelope unchanged (`stages.asr` informational).
- **Removed:** 设备 row `asr_ready`; approve body `asr`; `Probe.asr_ready`; `WorkerLive.asr_ready`.
- **Added:** row `lost_worker`.

## 3. In-flight state at upgrade

1. **ASR claimed on a worker:** not cancelled; the worker (either build) settles, uploads, finishes it (finish/`validate_finish_refs`/delivery keep ASR support). On resolve its route closes (restore finished → 完成). A failure/interruption becomes a pending ASR retry that only the controller can claim (R5).
2. **ASR routed but unclaimed** (restore finished, worker route open): the pass's step 1 withdraws every unclaimed auto/pinned route whose routed stages are empty, reason 「识别转本机」; single-machine mode uses the existing 转本机 takeover; the worker gets the film in `release`.
3. **Old-build worker claiming ASR** → 409 `route_required`; it drops the attempt file (P2-3 4xx rule).
4. **Re-核验:** the dropped ASR profile changes the worker's probe → 待核验; one-click 核验 approves restore only; until then no new routes; in-flight work finishes (`record_capabilities` isolates nothing).
5. **Pins** remain and apply to restore only.

## 4. UI

设备: no 识别 chip/switch; notice 「启用后修复交给这台 GPU 工作机；识别、翻译和入库仍在主控」. `workerModel.ts` pipeline 下载 → 修复 → 回传. 改派 copy drops 「（识别）」. ASR rows show 本机 (`live: "local"` / `unroutedMachine`). WorkflowSettings per R11.

## 5. Test plan

- Remote e2e: restore on worker → upload → **controller ASR** (`attempts(film,"asr")[0].worker_id == controller`) → translate → return → scrape → 待确认.
- Crash matrix 1–10 rerun with restore as the remote stage (7/8 then wait for controller ASR).
- Migration: claimed worker ASR finishes on the worker and its route closes; unclaimed restored route withdrawn 「识别转本机」 and released; old-worker ASR claim → 409; after 核验 the worker gets restore routes.
- Scheduler property tests: no decision for a restored film; never a route to the controller; no claimed route withdrawn; route-less ASR-only films absent from decisions; preview 自动 = pass.
- GPU lease contention (injected clock): manual task holds → ASR waits naming the holder → release → ASR runs → manual task waits for ASR; a remote-mode restore never queues on the lease.
- CX27 one event for an older-revision lost assignment; CX28 disk gate in the fallback; CX33 confirmed_at/position preserved; CX34 vitest.
- Settings field sets per role; a worker yaml keeps `whisperjav_exe_path`.
- Five-worker simulation restore-only. Phase-1 suites untouched and green.
- Timing-sensitive tests use events/injected clocks; disk figures injected.

## 6. Out of scope

ASR from the work copy in parallel with remote restore; 4K (P2-5); zero-config (#299); removing the worker's ASR finish code (after one release); ASR time in 预计.

## 7. Risks

1. Every worker needs one re-核验 after upgrade (release note; 设备 shows 待核验).
2. The 5060 becomes the ASR bottleneck behind manual tasks (visible 「等待 GPU：…」).
3. Mixed versions: heartbeat compatible; store guard R5 is the authority.
4. 4K ASR on 8 GB relies on WhisperJAV being audio-only — verify on the real machine.
5. 预计 covers restore + transfer only.

## 8. Commits

0. `docs(spec)`: this contract. 1. `fix(workflow,ui)`: CX27/28/33/34 + tests. 2. `feat(workflow)`: OD7 core R2–R8 + R12 pass withdrawal + property tests. 3. `feat(workflow)`: worker restore-only R9/R10 + heartbeat compatibility. 4. `feat(agent)`: settings R11. 5. `test`: e2e, crash matrix, migration, lease contention, simulation. 6. `feat(ui)`: §4.

### Code facts behind the contract
`film_store.py` `_ROUTE_STAGES`/`_GPU_STAGES` decide route close; `scheduler.py` `_ROUTED` + `asr_ready` rule; preview fallback skipping the disk gate (CX28); `operator_actions.py` `stages or ("asr",)`; `local_dispatch.py` `REMOTE_STAGES` + OD1 gate, `_ensure_route` already takes over an unclaimed worker route (转本机); `fleet_live.Heartbeat` requires `asr_ready` under `extra="forbid"`; `record_capabilities` sets `pending_validation` and isolates no assignment; `local_gpu.max_resolution` is FULL_HD under 16 GB (would block 4K ASR); `remote_dispatch` delivery doesn't need the executor.
