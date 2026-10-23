# P2-4 (#295): 待处理 自动/pin, board, 判定已停止 and the 媒体工作流 settings (frozen contract)

Date 2026-10-23 · Base `afk/claude-p2-271` 695933a · Parent: the [phase-2 design](2026-10-22-media-workflow-phase2-multi-machine.md) (§13 and §14 OD1–OD6 override the earlier sections). P2-1, P2-2 and P2-3 stay binding. Home LAN, single user, no over-design. OD1: in remote mode the controller never runs a GPU stage, and 本机 is never a choice.

**UI gate (operator, 2026-10-23):** all UI in this contract (commit 9) waits for the operator's confirmation of the UI/UX preview. Backend commits 1–8 proceed.

## 1. Requirements

| # | Requirement | Source |
|---|---|---|
| R1 | **The routing pass's reasons reach the board.** The host keeps the last pass's `waiting` and `attention`. One helper, `reads.board_inputs(host, rt)`, feeds the film list, the film detail and the #286 observer. Precedence for a film's note: transfer note, then the pass's `waiting`, then 「已派往 X」 for an open unclaimed route (「· 预取」 when it is second in line), then the dispatcher's `waiting`. A film held by the controller-disk gate (「主控磁盘空间不足」) never shows 「等待 GPU 设备」. | gap a, §6.3, V1 |
| R2 | **Additive row fields:** `route{machine{id,name}, kind, reason, routed_at, position, live, silent_s}`, `pin{worker_id, name, version}`, and `actions{can_reassign, stop}`. | V1, gap g |
| R3 | **「设备失联」 is an attention state.** The pass and the board share one helper, `scheduler.lost_films()`. Such a film sits in group `attention` with `attention_kind: "device_lost"` and the note 「设备失联：X · 后台重连中」. Nothing moves, and the state clears when heartbeats return. | OD2, V2, gap b |
| R4 | **The Mac Hub is notified.** The #286 observer sends `workflow.film.device_lost:<worker_id>` (warn, deduplicated per film, revision and worker). In remote mode the 待处理 notice reads 「· 自动」. | #286 B |
| R5 | **待处理 machine select (remote mode).** The first option is 「自动（推荐）」 with its predicted finish and energy. Each worker follows with its own finish, or greyed out with its eligibility reason. The options come from a new pure `scheduler.preview()` and are given per tier. With remote workers off the select is unchanged. | V1, gap e |
| R6 | **Confirming a plan.** `PlanItem.machine` may be null (null = 自动). In remote mode only null or a verified, non-revoked worker is accepted. | C2, IR9 |
| R7 | **改派** (`set_pin`) is offered for an unclaimed planned film whose restore or ASR is unfinished. Choosing 自动 for a film whose route sits on a worker that is not online also withdraws that route. If a claim races the request, the answer is 409 「已开始处理」. | C2, IR8, gap d |
| R8 | **判定已停止 needs the C4 two-channel check.** Both channels must have been dead for at least 300 s. **(a) Heartbeat:** no heartbeat on 5682; `FleetLive.forget` keeps a tombstone of the last heartbeat time; with no entry at all, the controller's start time counts. **(b) Status:** the 5680 `/status` read was continuously either `unreachable`/`timeout` or answered `workflow_worker.busy == false`, and the cache was checked within the last 30 s. Any HTTP answer (401/404/invalid), or an answer without the `busy` field, means the machine is up, so the request is refused. The IR4 guard (no renewal in the last 5 min) still applies. **After a successful attest:** `retry_if_due` (a cancelled stage is a free retry), then wake the scheduler and the dispatcher so the film is routed again. | C4, D4, gap c |
| R9 | **Worker `/status.workflow_worker` gains `busy`:** true while the current run, an unsettled Job or a held child process is alive. | C4 |
| R10 | **Energy per stage shows its machine.** `BoardStage.worker_ids` is added, and each `energy.stages[k]` gains `devices[{id,name}]`. | issue |
| R11 | **Enrolled workers become power peers automatically** (address:port, deduplicated, a configured peer wins), tagged `source: "workflow"`. The controller still only GETs `/status` and `/power/history`, and never reads `/events`. | PWR-3, gap f |
| R12 | **The Mac shows which machine runs a film.** The observer summary gains `currents[≤4]{film, stage, progress, machine}`, and the Hub's status.md prints the machine name. The network app stays GET-only. | #286 C/D, gap g |
| R13 | **A 「媒体工作流」 section in 设置** so agent.yaml never has to be hand-edited. A role select (不参与 / 主控 / GPU 工作机) writes `enabled` and `role`. **主控:** INBOX 目录, 工作目录 (`data_dir`), ffprobe (required), 允许 GPU 工作机接入 (`remote_workers`, note 「开启后本机不跑修复和识别」), worker API port; an optional fold holds Miko (`mdcx_*`) and the Filmpaw data directory. **GPU 工作机:** Jasna and WhisperJAV paths, LTX 显卡预设 (`auto` or a `preset_catalogue` id), 暂存目录, 暂存上限 (GB), then the existing 加入. The tool fields also show for a 主控 that does not accept GPU workers. | operator 2026-10-23 |
| R14 | **Validation.** Paths absolute, no `..`, not UNC; INBOX and tool files exist; staging via `staging.validate_dir`; port in range and distinct from `bind_port`/`control_port`; `ltx_gpu` from the catalogue; a worker needs a staging directory; `data_dir` cannot change once the old directory holds a workflow store (「工作目录已有媒体数据」). Errors per field, in Chinese, never echoing a secret. | operator |
| R15 | **Save through the existing config writer:** `AgentAdmin.update_config({"workflow": …})`; `workflow` becomes an editable, not-live field with a partial merge (as `power`). `save_yaml` gains `backup=True` (writes `agent.yaml.bak` with the previous bytes, tmp then replace) and keeps unknown top-level keys. The file is never printed or logged; the task log records field names only. | operator |
| R16 | **Apply and refuse unsafe switches.** Every change takes effect after a restart (「已保存，重启 TaskPaw 后生效」) via the existing `restart_required` mechanism; the banner stays while saved and running differ (no in-process swap: 5682 is claimed at startup, provider closures, FileLease and keep-awake are per process). Leaving 主控 or turning off 允许 GPU 工作机接入 is refused with 409 `workflow_busy` while any compute assignment is unresolved, any route is open or any archive operation is open: 「还有 N 部影片在处理中（M 部在 GPU 工作机上），请先暂停派发并等待完成」. A worker leaving its role is refused while it has routes, a claim or held children. The file is left untouched on refusal. | operator |

## 2. API changes

**Control routes (loopback 5681).** The first two answer 409 while remote workers are off.
- `POST /control/workflow/films/{id}/pin {input_revision, expected_version, worker_id|null}` — 409 `route_claimed`, `cas_conflict`, `precondition_failed`.
- `POST /control/workflow/films/{id}/attest-stopped {assignment_id, expected_version}` — 409 `still_alive` (detail names the failing channel), `recently_renewed`, `stale_attempt`, `cas_conflict`.
- `GET /control/workflow/settings` → `{saved, running, restart_required, busy{films, remote}, presets}` (workflow fields only; never a token or key).
- `PUT /control/workflow/settings {role: off|controller|worker, …}` → `{ok, restart_required}`; 422 `{fields}`, 409 `workflow_busy`.

**Shapes.** `actions.stop` = `{assignment_id, expected_version, stage, worker, allowed, reason, heartbeat_silent_s, status_quiet_s}`. Plans gain `remote` and per film `routing[tier] = {auto{machine, finish_at, energy_wh, text} | {waiting}, devices[{id, name, gpu, vram_mib, finish_at, energy_wh, reason}]}`; in remote mode `recommendation.machine` is null. Film detail gains `reassign_options`. 设备 rows gain `assignments[{assignment_id, version, film_id, film_name, stage, state, stop}]` and `route_depth`.

## 3. Store and scheduler

No schema change; `read_store` adds each attempt's worker id. `scheduler` gains `_pass_state()`, `preview()`, `lost_films()` (what `schedule()` decides does not change). `FleetLive` gains tombstones and `silent_for()`. `PowerFleet` tracks per worker `workflow_busy`, `quiet_since`, `quiet()`.

## 4. UI (commit 9 — gated on the confirmed UI/UX preview)

V3 style, Chinese copy, one compact title row, no slogans, single column at 390 px, 44 px controls, existing components.
- 待处理 native select (disabled options like 「5060 Ti-2 · 缺少高画质模型」; reason line 「预计 21:40 在 5070 Ti 完成 · 约 0.9 度」 or 「等待设备：…」).
- Queue/overview/exceptions: machine from the route (「待派发」/「本机」); 设备失联 films offer 处理 → drawer.
- Drawer: 设备 line (name, state, reason); 改派 dialog; 判定已停止 disabled-with-reason or dialog (both channel ages, free-retry notice; buttons 取消 / danger 「我确认该机器已关机或任务已停止」); energy rows with devices.
- 设备: unresolved assignments with 判定已停止; the agent.yaml notice points to 设置 · 媒体工作流.
- 功耗: automatic peers tagged 「工作机 · 自动加入」.
- Mac read-only: machine names and 设备失联 visible; mutations hidden.
- 设置 · 媒体工作流: role select + fields; states loading, three roles, field errors, saving, refused (busy), restart banner; 加入 only while the running role is GPU 工作机.

## 5. Test plan

- Board precedence (disk gate beats 「等待 GPU 设备」).
- Lost films: appear/clear, restart grace, one observer event, status.md machine.
- C4 matrix: heartbeat {online, lost < 5 min, ≥ 5 min, revoked tombstone} × status {busy, idle, unreachable, timeout, 401, invalid, stale cache, no `busy`}; IR4; stale version; success re-routes; pinned-to-dead waits until 改派.
- 改派: to a device; to 自动 off a lost worker (withdrawn) or a healthy one (kept); racing claim 409.
- Preview: unchanged state → 自动 equals the next `run_pass` decision.
- Power peers: merged, deduped, revoked/retired dropped, GET only.
- Five-worker simulation (3 × 5060 Ti 16 GB, 1 × 5070 Ti 16 GB, one 8 GB guard; fake clock): random confirms, heartbeat loss/return, 改派, attests, shrinking controller disk; after each step row notes = pass reasons, lost routes = `device_lost` films, attests only under C4, P2-1 invariants hold.
- Settings: round-trip keeps monitors/LLM keys/power peers/unknown key; backup = previous bytes; failed write leaves both files; no secret in logs/responses; validation table; refused busy switches leave the file unchanged; idle switch saves with `restart_required`.
- UI fixtures (commit 9): all pages, both dialogs, settings states, 390 px, no slogans, `mediaworkspace_readonly`.
- Timing-sensitive tests use events or injectable clocks; disk figures are injected.

## 6. Out of scope

4K restore (P2-5), T09, deployment docs, automatic moves on 失联 (OD2), `release_resource`, periodic renewal, model downloads from 设备, cross-machine resume, Mac write actions, in-process role switching, the first-run onboarding wizard (separate zero-config issue after its UI preview).

## 7. Risks

1. Attest on a partition — both channels, HTTP answers never dead, IR4, D4 `stale_attempt`.
2. Preview is advisory (预计).
3. Withdraw-on-自动 for a lost worker is control-layer only.
4. Token mismatch → `/status` 401 → 判定已停止 refused with that reason.
5. 4K greyed on every worker until P2-5.
6. Settings save: `.bak` holds the same secrets (same folder/permissions, never logged); unknown-key preservation covered by round-trip; busy refusal explains how to drain.

## 8. Commits

1. `docs(spec)`: this contract.
2. `feat(agent)`: 媒体工作流 settings backend (editable `workflow`, `save_yaml(backup=True)` keeping unknown keys, validation, busy refusal, GET/PUT settings) + tests.
3. `refactor(workflow)`: `_pass_state`, `lost_films`, `preview`.
4. `feat(workflow)`: last pass on the host, `board_inputs`, row fields, stage worker ids, energy devices.
5. `feat(workflow)`: 待处理 自动/pin + null confirm, 改派, 判定已停止 with C4, FleetLive tombstone, worker `busy`.
6. `feat(core)`: automatic power peers, PowerFleet quiet tracking.
7. `feat(agent,hub)`: observer `device_lost` and currents; machine in status.md.
8. `test`: five-worker simulation, C4 matrix, board precedence.
9. `feat(ui)`: all UI in §4 — **only after the operator confirms the UI/UX preview**.

### Code facts behind the contract
- `_route_pass` (`local_host.py`) throws away `planned.waiting`/`attention`; the board shows only the dispatcher's 「等待 GPU 设备」 (`local_dispatch.py`).
- `FleetLive.currents` skips lost workers; `energy._stage` hard-codes `machine: LOCAL`; 「本机」 is hard-coded in `QueuePage.tsx`, `OverviewPage.tsx`, `EnergyBlock`.
- `PlanItem.machine` is a required string; PowerFleet peers come only from config (`launcher.py`).
- `workflow` is not in `AgentAdmin._EDITABLE_CONFIG`; `save_yaml` dumps the model with no backup; `Settings.tsx` shows `WorkerJoinSection` only for `role == "worker"`; `DevicesPage.tsx` tells the operator to edit agent.yaml; the workflow host starts once after readiness and 5682 is claimed at startup; there is no restart command in the console or the Tauri shell.
