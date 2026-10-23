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
| R5 | **待处理 machine select (remote mode).** The first option is 「自动（推荐）」 with its predicted finish and energy. Each worker follows with its own finish, or greyed out with its eligibility reason. The options come from a new pure `scheduler.preview()` and are given per tier. A film whose pending restore and ASR need no GPU (every volume reused) has no device option; 自动 reads 「无需 GPU，本机完成」 (`auto = {waiting: null, text}`, §9 CX2). With remote workers off the select is unchanged. | V1, gap e |
| R6 | **Confirming a plan.** `PlanItem.machine` may be null (null = 自动). In remote mode only null or a verified, non-revoked worker is accepted. | C2, IR9 |
| R7 | **改派** (`set_pin`) is offered for an unclaimed planned film whose restore or ASR is unfinished. Choosing 自动 for a film whose route sits on a 失联 (`lost`) worker also withdraws that route; a worker still in the start grace (`waiting`) keeps it (§9 IR7). The store checks "no unresolved compute assignment" and withdraws the route in the pin's own transaction (`PinSet.withdraw_route_id`), so a racing claim answers 409 `route_claimed` 「已开始处理」 and leaves the pin and the route unchanged (§9 CX4). | C2, IR8, gap d |
| R8 | **判定已停止 needs the C4 two-channel check.** Both channels must have been dead for at least 300 s. **(a) Heartbeat:** no heartbeat on 5682; `FleetLive.forget` keeps a tombstone of the last heartbeat time; with no entry at all, the controller's start time counts. **(b) Status:** the 5680 `/status` read was continuously either `unreachable`/`timeout` or answered `workflow_worker.busy == false`, the cache was checked within the last 10 s (§9 IR9), and the channel answered at least once since the controller started — one that never connected may be a wrong address, refused 「状态通道从未连通」 (§9 IR2). Any HTTP answer (401/404/invalid), or an answer without the `busy` field, means the machine is up, so the request is refused. The IR4 guard (no renewal in the last 5 min) still applies. **After a successful attest:** `retry_if_due` (a cancelled stage is a free retry), then wake the scheduler and the dispatcher so the film is routed again. | C4, D4, gap c |
| R9 | **Worker `/status.workflow_worker` gains `busy`:** one rule, `remote_dispatch.worker_busy`, read by `/status`, the settings guard and keep-awake alike — true while the dispatcher holds the current run, an unsettled Job, an attempt to deliver or a held child (§9 IR1), or while the attempts folder holds any entry, parsable or not (only the `refused/` archive excepted), or cannot be read (any `OSError` but `FileNotFoundError`) (§9 CX1/CX10/CX13). With no dispatcher (no controller reached since a restart) the folder decides alone. Recovery never drops an unreadable attempt file: it is logged and kept, and the monitor notice reads 「有无法读取的任务记录」 until the operator resolves it (§9 CX13). | C4 |
| R10 | **Energy per stage shows its machine.** `BoardStage.worker_ids` is added, and each `energy.stages[k]` gains `devices[{id,name}]`. | issue |
| R11 | **Enrolled workers become power peers automatically** (address:port, deduplicated, a configured peer wins), tagged `source: "workflow"`, only while `remote_workers` is on (§9 IR11). A peer source that raises keeps the last good list and its quiet time (§9 IR10). The controller still only GETs `/status` and `/power/history`, and never reads `/events`. | PWR-3, gap f |
| R12 | **The Mac shows which machine runs a film.** The observer summary gains `currents[≤4]{film, stage, progress, machine}`, and the Hub's status.md prints the machine name. The network app stays GET-only. | #286 C/D, gap g |
| R13 | **A 「媒体工作流」 section in 设置** so agent.yaml never has to be hand-edited. A role select (不参与 / 主控 / GPU 工作机) writes `enabled` and `role`. **主控:** INBOX 目录, 工作目录 (`data_dir`), ffprobe (required unless PATH has one; the GET's `ffprobe_detected` names it, §9 IR6), 允许 GPU 工作机接入 (`remote_workers`, note 「开启后本机不跑修复和识别」), worker API port; an optional fold holds Miko (`mdcx_*`) and the Filmpaw data directory. **GPU 工作机:** Jasna and WhisperJAV paths, LTX 显卡预设 (`auto` or a `preset_catalogue` id), 暂存目录, 暂存上限 (GB), then the existing 加入. The tool fields also show for a 主控 that does not accept GPU workers. | operator 2026-10-23 |
| R14 | **Validation.** Paths absolute, no `..`, not UNC; INBOX and tool files exist; staging via `staging.validate_dir`; port in range and distinct from `bind_port`/`control_port`; `ltx_gpu` from the catalogue; a worker needs a staging directory; for every role, `data_dir` cannot leave a directory (the saved one or the running one) that holds a workflow store (「工作目录已有媒体数据」) or a worker's pairing, `worker-identity.json` (「工作目录已有工作机配对」) (§9 CX3/IR5); an empty ffprobe is fine when PATH has one (§9 IR6); while a retained workflow store (the running or the new data directory) pairs a non-retired worker, every save, whatever its role, must keep the worker API port those pairings were made on — recorded beside the store in `pairing-port.json` with every pairing code; a store paired before that record falls back to the running port (「已有工作机配对在此端口，改端口需先让它们重新配对」, §9 IR4/CX11/CX14). Errors per field, in Chinese, never echoing a secret. | operator |
| R15 | **Save through the existing config writer:** `AgentAdmin.update_config({"workflow": …})`; `workflow` becomes an editable, not-live field with a partial merge (as `power`). `save_yaml` gains `backup=True` (writes `agent.yaml.bak` with the previous bytes, tmp then replace) and keeps unknown top-level keys. The file is never printed or logged; the task log records field names only. | operator |
| R16 | **Apply and refuse unsafe switches.** Every change takes effect after a restart (「已保存，重启 TaskPaw 后生效」) via the existing `restart_required` mechanism; the banner stays while saved and running differ (no in-process swap: 5682 is claimed at startup, provider closures, FileLease and keep-awake are per process). Leaving 主控 or turning off 允许 GPU 工作机接入 is refused with 409 `workflow_busy` while any compute assignment is unresolved, any route is open or any archive operation is open: 「还有 N 部影片在处理中（M 部在 GPU 工作机上），请先暂停派发并等待完成」. A worker leaving its role, or moving its staging or data directory (§9 IR3), is refused while it has routes, a claim or held children. A save racing another answers 409 `operation_busy` 「正在保存其他设置，请稍后再试」 (§9 IR12). The file is left untouched on refusal. | operator |

## 2. API changes

**Control routes (loopback 5681).** The first two answer 409 while remote workers are off.
- `POST /control/workflow/films/{id}/pin {input_revision, expected_version, worker_id|null}` — 409 `route_claimed`, `cas_conflict`, `precondition_failed`.
- `POST /control/workflow/films/{id}/attest-stopped {assignment_id, expected_version}` — 409 `still_alive` (detail names the failing channel), `recently_renewed`, `stale_attempt`, `cas_conflict`.
- `GET /control/workflow/settings` → `{saved, running, restart_required, busy{films, remote}, presets, ffprobe_detected}` (workflow fields only; never a token or key).
- `PUT /control/workflow/settings {role: off|controller|worker, …}` → `{ok, restart_required}`; 422 `{fields}`, 409 `workflow_busy` or `operation_busy`.

**Shapes.** `actions.stop` = `{assignment_id, expected_version, stage, worker, allowed, reason, heartbeat_silent_s, status_quiet_s}`. Plans gain `remote` and per film `routing[tier] = {auto{machine, finish_at, energy_wh, text} | {waiting, text?}, devices[{id, name, gpu, vram_mib, finish_at, energy_wh, reason}]}`; in remote mode `recommendation.machine` is null. Film detail gains `reassign_options`. 设备 rows gain `assignments[{assignment_id, version, film_id, film_name, stage, state, stop}]` and `route_depth`.

## 3. Store and scheduler

No schema change (`PinSet` gains the optional `withdraw_route_id`, §9 CX4); `read_store` adds each attempt's worker id. `scheduler` gains `_pass_state()`, `preview()`, `lost_films()` (what `schedule()` decides does not change). `FleetLive` gains tombstones and `silent_for()`. `PowerFleet` tracks per worker `workflow_busy`, `quiet_since`, whether it ever answered, `quiet()`.

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

## 9. Review dispositions

Codex (CX) and internal review (IR) of commits 2–8, base `d1cc5a3`. Every admitted finding is fixed with a failing test first; the rules above carry the changed wording. OD7 (ASR on the controller) is the next child and is not part of this round.

| # | Finding | Disposition | Rule | Test |
|---|---|---|---|---|
| CX1 | A restarted worker with surviving children and no controller reachable answered `busy: false` (no dispatcher yet, `_held` filled only by a boot reply). | **Fixed.** Without a dispatcher, any attempt file under the staging folder makes `busy` true (an unreadable folder too); recovery or the boot settles them. | R9 | `test_a_restarted_worker_with_attempt_files_is_busy_before_any_controller` |
| CX2 | A film with pending restore/ASR that needs no GPU (reuse only) made `preview` index `stages[0]` and crash `GET plans`. | **Fixed.** No device option, `Preview.reason` = 「无需 GPU，本机完成」, `auto = {waiting: null, text}`; `Rates.energy_wh` returns None for no stage. `api.ts` gains `text?`. | R5, §2 | `test_preview_of_a_film_that_needs_no_gpu_names_no_device` |
| CX3 + IR5 | The `data_dir` guard ran only for a non-off role and only against the saved value; a worker's paired directory was not protected. | **Fixed.** The guard runs for every role, against the saved and the running directory, and also holds a directory with `worker-identity.json` (or its pending file): 「工作目录已有工作机配对」. | R14 | `test_the_data_dir_guard_applies_to_every_role`, `…_compares_with_the_running_directory`, `test_a_paired_workers_data_dir_cannot_move` |
| CX4 + IR8 | 改派 checked the claim outside the pin's transaction; a claim between the read and the write changed the pin and then failed the withdrawal. | **Fixed.** `set_pin` refuses `route_claimed` whenever the film revision has an unresolved compute assignment, in its own transaction; the 失联 withdrawal is the same step (`PinSet.withdraw_route_id`). | R7 | `test_a_claim_racing_the_reassign_answers_route_claimed[auto/same/other]` |
| IR1 | `RemoteDispatcher.busy()` ignored attempts to deliver and settled-but-undelivered files. | **Fixed.** `busy()` is `claimed()`. | R9 | `test_the_worker_dispatcher_is_busy_while_a_run_or_a_held_child_lives` |
| IR2 | A `/status` channel that never answered since the controller started counted as dead (a wrong address looks like that). | **Fixed.** `PowerFleet` keeps `answered` per peer (`StatusQuiet.answered`); never answered → refused 「状态通道从未连通」. | R8 | `test_a_status_channel_that_never_answered_is_not_dead`, `test_the_status_channel_remembers_whether_it_ever_answered` |
| IR3 | A busy worker could move its staging or data directory. | **Fixed.** Refused 409 `workflow_busy` like leaving the role. | R16 | `test_a_busy_worker_cannot_move_its_staging_or_data_dir` |
| IR4 | The worker API port could change under paired workers. | **Fixed.** 422 on `worker_api_port` 「已有工作机配对在此端口，改端口需先让它们重新配对」 while enrolled, non-retired workers exist; allowed with none. | R14 | `test_the_worker_port_cannot_change_under_paired_workers`, `…_can_change_with_no_paired_worker` |
| IR6 | ffprobe was required even when PATH has one (the pipeline already auto-detects). | **Fixed.** Empty is accepted when `shutil.which("ffprobe")` finds it; the GET reports `ffprobe_detected`. | R13, R14 | `test_an_empty_ffprobe_is_fine_when_it_is_on_the_path` |
| IR7 | 自动 withdrew a route of a worker still in the post-start grace. | **Fixed.** Only a `lost` worker's route is withdrawn. | R7 | `test_auto_keeps_a_route_of_a_worker_still_in_its_start_grace` |
| IR9 | `FRESH_SECONDS` 30 s was looser than the 5 s cache needs. | **Fixed.** 10 s. | R8 | `test_the_status_read_must_be_ten_seconds_fresh` |
| IR10 | A raising peer source emptied the peer list and so dropped `quiet_since`. | **Fixed.** The last good list is kept (logged). | R11 | `test_a_peer_list_that_fails_keeps_the_last_good_one` |
| IR11 | Automatic power peers were added with `remote_workers` off. | **Fixed.** Only while `remote_workers` is on; phase 1 unchanged. | R11 | `test_enrolled_workers_become_peers_but_revoked_and_retired_go` |
| IR12 | `ValueError("operation_busy")` from `update_config` became a field error. | **Fixed.** 409 `operation_busy` 「正在保存其他设置，请稍后再试」. | R16 | `test_a_concurrent_save_answers_409_not_a_field_error` |
| IR13 | `GET plans` runs `preview` per film and tier (a pass's state each time). | **Noted, not changed.** A home LAN holds tens of unplanned films and five workers; the cost is milliseconds per film. Revisit only if 待处理 is measured slow. | — | — |
| Test quality | The simulation's two channels always agreed, so a one-channel regression could pass. | **Fixed.** A split variant (heartbeat dead, `/status` still busy) must never attest; plus a deterministic split case and a per-channel unit test. | R8 | `test_split_channels_never_allow_an_attest`, `test_a_split_machine_is_refused_and_a_dead_one_allowed`, `test_one_dead_channel_never_allows_an_attest` |

Codex round 2 (CX5–CX9), base `052bee2`. Each is fixed with a failing test first.

| # | Finding | Disposition | Rule | Test |
|---|---|---|---|---|
| CX5 | Remote mode sends `recommendation.machine = null` (自动), but the current `PendingPage.tsx` required a truthy machine and its select had no 自动 option, so every remote film's confirm was disabled. | **Fixed (minimal UI).** A null machine is 「自动（推荐）」, the select's first option; `ready` and `complete()` accept null and confirm sends `machine: null`; the single-machine mode is unchanged. The full 待处理 select design still ships in UI commit 9. | R5 | `remote mode: a null recommended machine is 自动（推荐）…` (vitest) |
| CX6 | The settings busy guard read `routes` and `dispatcher.claimed()` only, so a worker restarted while the controller is unreachable (no dispatcher, no routes, unsettled attempt files or surviving children) read idle. | **Fixed.** The guard uses the host's disk-aware `summary()` (routes or `busy`, CX1) for leaving the role and for moving the staging or data directory. | R16 | `test_a_restarted_worker_with_unsettled_attempts_is_busy_without_a_controller` |
| CX7 | 判定已停止 required the film's latest input revision, so an unresolved assignment left on an older revision was refused `stale_attempt` forever. | **Fixed.** The exact unresolved assignment and its attempt are validated against their own stage; the store settles the assignment, frees the GPU owner and closes that revision's route. Only a current-revision stage is retried (`retry_if_due`); an obsolete revision is never revived. | R8 | `test_an_unresolved_assignment_on_an_older_revision_can_be_attested` |
| CX8 | A paired worker's staging directory lives in `worker-identity.json`, so saving a new one to agent.yaml changed nothing. | **Fixed.** 422 on `staging_dir` 「已配对的工作机改暂存目录需要重新加入」 when the saved or running data directory holds a pairing (the directory is chosen at 加入); an unpaired worker may still edit it. | R14 | `test_a_paired_workers_staging_dir_cannot_change_but_an_unpaired_one_can` |
| CX9 | Paths were compared with a blanket `casefold()`, merging two directories on a case-sensitive filesystem. | **Fixed.** `os.path.samefile` when both exist, else `os.path.normcase(os.path.abspath(…))` (folds case only on Windows). | R14 | `test_a_data_dir_differing_only_by_case_follows_the_filesystem`, `test_same_path_uses_the_filesystem_when_both_exist` |

Codex round 3 (CX10–CX12), base `8089869`. Each is fixed with a failing test first.

| # | Finding | Disposition | Rule | Test |
|---|---|---|---|---|
| CX10 | `attempts_on_disk` used `Path.glob`, which swallows `PermissionError`, so an unreadable attempts folder reported `busy: false`. | **Fixed.** `os.scandir`; only `FileNotFoundError` (no folder) is idle, any other `OSError` is busy. | R9 | `test_an_unreadable_attempts_dir_is_busy_and_a_missing_one_is_idle` |
| CX11 | Changing `worker_api_port` while the running role was off skipped the paired-workers guard (disable, restart, re-enable with a new port orphaned the enrolled workers). | **Fixed.** When the running role is not 主控 the guard reads the retained store (`workflow.db` in the running and the new data directory, read-only): any non-retired worker besides the controller's own refuses the port change; an unreadable store counts as paired. A running 主控 still asks the host. | R14 | `test_the_worker_port_stays_guarded_after_a_disable_and_restart`, `test_the_port_is_free_to_change_when_the_retained_store_pairs_nobody` |
| CX12 | The observer summary took `currents[:4]` after sorting by machine, so one worker's prefetch entries could displace other machines. | **Fixed.** One entry per machine (its running stage over its prefetch), then the limit of four. | R12 | `test_one_current_per_machine_prefetch_never_displaces_another` |

Codex round 4 (CX13–CX14), base `f5cf171`. Both are the second finding in their area, so each is fixed as a class with one rule, failing tests first.

| # | Finding | Disposition | Rule | Test |
|---|---|---|---|---|
| CX13 | With a dispatcher present, `busy` came from `claimed()` → `_states()`, which silently dropped attempt files that fail to parse and used `Path.glob` (an unreadable folder read as empty), so 判定已停止 could pass with unsettled work on disk. | **Fixed (one rule).** `worker_busy(staging_root, dispatcher)` is the only busy function: the dispatcher's in-memory work (`holds_work()`: run, unsettled Job, delivery, held child) or `attempts_on_disk` — conservative, any entry but `refused/`, any read error but a missing folder. `/status` (`WorkerHost.summary`), the settings guard (through `summary()`) and keep-awake (`claimed()`) all call it; `RemoteDispatcher.busy()` is gone. Recovery logs an unreadable file, keeps it and sets the notice 「有无法读取的任务记录」. | R9 | `test_a_corrupt_attempt_file_keeps_a_dispatcher_busy_and_recovery_keeps_it`, `test_an_unreadable_attempts_dir_keeps_a_dispatcher_busy`, `test_status_and_the_settings_guard_read_one_busy_rule` |
| CX14 | The port guard compared with the running port and ran only for a 主控 save, so an off-role save with a new port reset the baseline (the next 主控 save adopted it). | **Fixed (one rule).** The baseline is the port the retained pairings were made on: `devices._issue` writes `pairing-port.json` beside `workflow.db` before every pairing code; `pairing_ports` reads it for each named data directory whose store pairs a non-retired worker (an unrecorded store falls back to the running port; an unreadable store counts as paired). Any save, whatever its role, with a different `worker_api_port` is refused; the live-host count (`paired_workers`) is gone — the running host's store is on disk too. | R14 | `test_an_off_role_save_cannot_move_the_port_away_from_the_pairings`, `test_a_controller_save_is_refused_when_the_running_port_left_the_pairings`, `test_with_no_pairings_any_role_may_change_the_port` |
