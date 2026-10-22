# P2-3 (#294): remote execution end-to-end (frozen contract)

Date 2026-10-23 · Base `afk/claude-p2-271` d1bd523 · Parent: [phase 2](2026-10-22-media-workflow-phase2-multi-machine.md) (§13 and §14 / OD1–OD6 override the earlier sections). The [P2-1](2026-10-22-media-workflow-p2-1-store-scheduler.md) and [P2-2](2026-10-23-media-workflow-p2-2-worker-api.md) contracts stay binding. Home LAN, single user, no over-design.

P2-3 runs restore and ASR of a routed film on a worker and brings the verified results back. The controller runs no GPU stage (OD1). Translate, return and scrape run unchanged on the controller.

## 1. Requirements

| # | Requirement | Source |
|---|---|---|
| R1 | **First commit.** **CX15:** `Transfers.sweep()` re-checks each folder right before it deletes it, under the attempt lock: the id must still be absent from a fresh `live()` read, no upload may hold it, and no plan may have been written after the snapshot. **CX16:** when a first join loses both the `/pair` reply and the `/self` recovery, `_resume(old=None)` keeps the pending identity as `_pending_check` and starts `_run` (the existing loop with no saved identity). | P2-2 §11 |
| R2 | `run_stage()` is extracted from `LocalDispatcher._execute` (§4). The phase-1 suites stay green unchanged. | §5, risk |
| R3 | `ExecRuntime` protocol (§4). Executors and `exec_common` see only this protocol. | §5 |
| R4 | `TranslateExecutor.max_resolution` returns UHD for every card, because translate is LLM-only. The existing profile compare in `_ensure_capability` re-records the controller's approved profiles at restart; the controller stays verified. | P2-1 deferred note |
| R5 | New endpoints (§2). | §3.1, C3, C5, C7 |
| R6 | **`RemoteDispatcher` claim gate.** The worker claims only the position-0 route, and only when all of these hold: source staged and verified, plan tier ready, `gpu_lease` held, GPU hold ≠ unknown (「GPU 有未知占用」), boot reconciled, not paused. **After the claim:** exactly one renew; no periodic renew (C5). | §3.2, C5 |
| R7 | **Prefetch.** One fetch thread, position 0 first, then position 1 within the disk rule (S4). A download failure never creates an attempt. | S4, §3.6 |
| R8 | **Boot reconcile.** The worker reports its attempt files at boot (§5) and re-posts the same `boot_id` at every beat until `reconciled` (C6). Each new dispatcher (a new session identity, or after a revoke) takes a new `boot_id` (§11 IR1). | §3.8, C6, S7 |
| R9 | **Cancel.** The heartbeat reply's `cancel` holds (a) attempts the operator stopped with 停止并重新排队 (`POST /control/workflow/films/{film}/requeue {attempt_id}`, an in-memory set cleared at resolve), and (b) a reported `current.attempt_id` that is no longer current and unresolved (stale, D4). For (a) the worker cancels its Job, settles and finishes failed `cancelled`. That is a free retry: the route closes (P2-1 IR2) and the pass routes the film again (a pinned film returns to the same worker). For (b) the worker kills the Job and discards the attempt. Once the worker reports the attempt `uploading` or `finishing`, the requeue is refused (409 `already_settled`), and a cancel that reaches a stage already settled ok is ignored: finished GPU work is delivered (§11 CX5). | OD3, D4 |
| R10 | **Staging GC.** Source files are deleted only when the film is named in `release` or missing from the boot reply's `keep`. `work/<a>/` is deleted once its attempt has resolved. Nothing is deleted while an attempt is unknown. A revision being released is never claimable, and the claim gate checks each staged file's size first (§11 IR2). At start, `work/<uuid>/` folders no attempt file names are swept once every attempt file was answered (§11 IR5). **`release` rule:** staged revisions with no open route to this worker and no unresolved assignment of this worker, **and** either (restore and ASR finished, or the revision is no longer current, or the film is routed to another worker). | §5, D2 |
| R11 | **Keep-awake.** `ES_CONTINUOUS\|ES_SYSTEM_REQUIRED` is held while the worker has an open route or an unresolved claim. It is set and cleared only from the long-lived heartbeat thread (the call is per thread), cleared at stop, and injectable for tests. | C11 |
| R12 | **Worker-local 暂停领取.** Loopback `POST /control/workflow/worker/pause {paused}` → heartbeat `paused` → 停领 rules (S8). In-flight runs, uploads and prefetch continue. The choice survives a restart (`worker-state.json`) and never clears the controller's 暂停派发 (§11 IR10). | S8 |
| R13 | **Scheduler thread on the controller** (remote enabled only). It runs `routing.run_pass` on plan confirm, on any resolve (local or finish), on a FleetLive change (entry new/lost/online, paused, readiness, version, staged set) and every 30 s. Wakes are coalesced; stop joins the thread. | §4 |
| R14 | **Energy.** `WorkerHost.dispatcher` exposes `run_id` and `current_stage()`, so the worker's `activity_source` attributes the sampler's energy to the film. A PW04 reading is taken at claim and at settle. Finish carries `energy` from `energy_between`, or null, never 0; it is always null when the worker restarted in between. **Timing:** finish carries the executor's `RestoreTiming`. The controller puts both into the resolving evidence. | PWR-2, §5 |
| R15 | **Remote currents.** `board.film_rows(..., currents: Mapping[film_id, current])` replaces the single `current`; the local current and `FleetLive.currents(now)` are merged. Each row shows `progress`, `ltx` (LtxProgress, 本片参数 `params`, `timing`, `console`), `started_at` = controller now − `elapsed_s` (C14), live energy and W, `machine{id,name}`, and 「下载源文件/回传 x/y GB · rate」 as the step note. The JSON is additive. | §6.3 |
| R16 | **Worker monitor (§6).** `/status.workflow_worker` gives `{state, reason, connected, routes, current{film_id, stage, phase}, paused}` with no secret. | §5, §7 |

## 2. Endpoints (`/worker/v1` on 5682)

All endpoints need a worker Bearer and are scoped to the caller (S-4).

| Route | Body | Effect | Errors / idempotency |
|---|---|---|---|
| POST `/claim` | `{attempt_id, operation_id, job_id, expected_version, boot_id, tool_versions}`. These exact bytes are written to `attempts/<a>.json` before sending. | `claim` with worker auth, meta key `claim:<a>`. The server fills `worker_id` and `resource_id` from the binding, and `profile_revision`/`config_digest` from `tool_versions`. | Same bytes → the stored result (`request_results`). 409 `route_required`, `cas_conflict`, `owner_unresolved`, `precondition_failed`. A 4xx drops the attempt file and frees the lease; a transient error resends the same bytes. |
| POST `/attempts/{a}/renew` | `{owner_id, generation, boot_id, process_identity}` | `renew` with `local_owner` (state running, controller-made `native_owner` evidence) | 409 `stale_attempt`. A failure is logged and never stops the Job. |
| POST `/attempts/{a}/finish` | `{outcome, error_class?, message, ledger?, payload_digest?, timing?, energy?}`, sent only after the Job settled | **(1)** Already resolved for this attempt → 200 with the stored outcome. **(2)** Not current, or another worker's → 409 `stale_attempt`. **(3)** Success: `validate_finish_refs` (committed plan entries, or work-copy refs per C3); sha256(ledger) must equal `payload_digest`; the ledger is written atomically to `work/<a>/stage-result.json`; `resolve_assignment(succeeded)`. **(4)** Failure: resolve failed `failed_kind(error_class)`, then `retry_if_due`. **(5)** Then: owner-stopped evidence made by the controller, plan sweep, E4 workdir drop, `on_resolved` (log, #286 events), scheduler wake. | 422 `invalid_ledger`, `ledger_mismatch`, `unverified_output`, `digest_mismatch`. Idempotent by state. `message` ≤ 2000 characters, cut by the worker; a 400/404/413/422 refusal of a failed finish ends its delivery (§11 IR3). |
| GET `/attempts/{a}` | – | read only | `{state: running\|unknown\|resolved, outcome?, current}`. 404 when absent or another worker's. |

- **Additive changes:** the envelope gains `stages{restore, asr: {job_id, version, status}}`.
- **Heartbeat `current`** becomes typed (`{film_id, stage, attempt_id, phase: running\|settling\|uploading\|finishing, progress, elapsed_s, ltx?, energy?, power?, uses_gpu}`, within 64 KiB: the worker keeps it under 32 KiB and sends only the live watts in `power`, §11 IR11), and `transfers` is filled.
- **Shared code:** `retry_if_due` and the resolution builder move out of `LocalDispatcher` into module functions used by both paths.

## 3. Worker state machine (per film revision)

```
route(open, pos 0|1) → fetching (envelope; ranged .part, transient retries) → staged
 → [gate R6] claim: file{request} → POST → file{assignment_id} → renew ×1
 → running (run_stage; children → file) → settled: file{outcome, digest}
 → uploading (plan + PUT until done; failures skip this) → finish → resolved:
   rm work/<a>/ and the file; ASR: re-fetch envelope (restore ledger) → claim …
 → route closes in the store → release → source deleted
```

- **GPU lease:** held from claim until finish resolves (D3), and from a boot `held` verdict until those children are gone (§11 CX2).
- **Unsettled Job:** stays 「停止未确认」 and keeps the lease; settle is retried every tick before finish (C4), and `stop()` stays unconfirmed until a later call settles it (§11 CX1).
- **Persisted crash points per attempt file:** `request` → `claimed` → `settled` → `finish sent`.

## 4. `run_stage()` and `ExecRuntime`

```python
@dataclass
class StageRun: outcome: ExecOutcome; processes: OwnedJob | None; settled: bool
def run_stage(executor, job, manifest, workdir, cancel, *, on_launch, progress,
              stop_settle, on_processes=None) -> StageRun
```

- **What moves:** the run section of `_execute` minus the renewal. That is `OwnedJob(on_launch)`, the mkdir, `executor.run(JobContext)`, exception → `classify_exception`, cancel → `cancelled`, and `terminate` + `settle`.
- **`LocalDispatcher`** keeps its renew thread around the call, plus `_Unsettled` and `_resolve` unchanged.
- **`ExecRuntime`** (in `executors.py`): `work_dir: Path`, `gpu: LocalGpu | None`, `stage_result(film_id, stage) -> StageResult | None` (None = the film has no such stage; `StageResultError` = not verified).
- **`LocalRuntime.stage_result`** is today's store logic.
- **`WorkerRuntime.stage_result`** reads the envelope ledger text and checks sha256 against `payload_digest`, plus film, revision and attempt.
- **Helpers:** `load_stage_result` raises 「尚未成功」 on None; `output_stems` falls back to input stems on None. Executors only change their `runtime` annotation.
- **Gate:** these suites stay green and untouched: `test_workflow_local_dispatch`, `local_recovery`, `exec_restore/asr/translate`, `pipeline_e2e`, `controller_stages`.

## 5. Crash / recovery matrix

- **Boot body:** `{boot_id, assignment_ids, attempts:[{attempt_id, assignment_id?, children_alive, ledger, outputs_intact, outcome?}]}`.
- **Settlement after `record_boot`**, for each unresolved assignment of the worker:
  - not reported, or children dead with no ledger and no settled outcome → resolve failed `interrupted` + retry;
  - children alive → stays unknown (`held`, 「上次的任务进程仍在运行」);
  - ledger + intact outputs, or a settled outcome → stays unknown, reply `finish`.
- **Reconciled:** `record_boot` runs again with the same boot id and is reconciled once nothing remains unresolved.
- **Reply:** `{reconciled, attempts{a: interrupted|finish|held}, keep:[film/rev]}`.

| # | Persisted | Restart / event | Expected end state |
|---|---|---|---|
| 1 | request only | worker | GET 404 → file dropped; claimed anew on the staged source |
| 2 | request only | controller | Same-bytes retry → exactly one assignment |
| 3 | claimed, no ledger | worker | unknown → `interrupted`, free retry |
| 4 | claimed, no ledger | controller | Worker runs on; finish retried with backoff → resolved |
| 5 | ledger + outputs | worker | unknown → upload → finish → succeeded, **no GPU re-run** |
| 6 | ledger + outputs, mid-upload | controller | Offsets survive, prefix rebuilt → succeeded, no re-run |
| 7 | resolved, reply lost | worker | Boot reconciles; GET `resolved` → GC |
| 8 | resolved, reply lost | controller | Finish retry → 200 with the stored outcome |
| 9 | partition > 60 s; `attest_stopped` | – | 失联, nothing moves; on return, finish succeeds. After attest: finish 409, `cancel` kills, outputs discarded |
| 10 | revoke mid-run | – | 401 → Job cancelled and settled, loop ends; assignment unknown until attest |

Extra case: a child still alive at boot → `held`; claims are refused (`owner_unresolved`).

## 6. UI

- **Worker 「媒体处理」 tab** (role=worker, V3, 390 px, noun headings). It shows:
  - connection (地址, 心跳, 凭据, 对账);
  - routed and claimed films (name, 当前/预取, state);
  - the step line 下载→修复→回传→识别→回传 with LtxProgress, 本片参数 and 用时;
  - transfers with rate, staging used/cap, GPU hold.

  The only control is 暂停领取/恢复领取. Data comes from the extended `GET /control/workflow/worker`.
- **Controller:**
  - remote progress on board rows and in the drawer, with a 设备 name on the running step;
  - a drawer button 停止并重新排队 (confirm dialog) on a running remote restore or ASR, shown only while its phase is `running` or `settling` (§11 CX5).

## 7. Test plan

- **Fake-worker end-to-end:** real `WorkerHost`/`RemoteDispatcher`/`WorkerClient` against the real 5682 app on loopback, with `launch_prefix` fake Jasna/WhisperJAV, fake GPU, lease and sampler. The controller host uses fake translate/return/scrape. The film must reach 待确认 with digest-checked ledgers, timing, and energy (null when cut).
- **Crash matrix:** cases 1–10 plus the held child. Each asserts its end state, ≤ 1 unresolved assignment per job, ≤ 1 GPU owner, an executor run counter (no re-run in 5/6), and the stale-finish 409.
- **Idempotency:** claim replay; finish by state; both ref kinds; digest mismatch 422.
- **Prefetch:** position 1 fetches while 0 runs; never more than 2 routes; the budget refuses; GC deletes only `release`/non-`keep` films.
- **Cancel:** a requeue becomes a free retry and is re-routed (a pinned film to the same worker); a stale current is cancelled.
- **Keep-awake seam:** on with a route or claim, off after release and at stop, always from one thread.
- **Pause:** no claim; in-flight work completes; the auto route is withdrawn.
- **Scheduler thread:** each trigger wakes a pass (fake clock); stop joins.
- **Other units:** CX15 concurrent create vs sweep; CX16 identity row; translate on a 4K film with the 8 GB controller plus the profile refresh at restart; `WorkerRuntime` digest mismatch.
- **UI fixtures:** worker monitor states, remote row and drawer, requeue dialog, 390 px.
- **Timing-sensitive tests** use events or injectable clocks, never bare wall-clock budgets (lesson from P2-2).

## 8. Out of scope

- **P2-4:** 待处理 自动/pin and predicted finish, 判定已停止 UI (C4), 改派 UI, board/Mac 「设备失联」 rows, route reasons in `waiting`/drawer, automatic power peers.
- **P2-5:** 4K restore on 16 GB, 4K no-GPU stages on the 8 GB controller, T09.
- **Later:** model download on the worker monitor (D6), `release_resource`, periodic renew, cross-machine resume, parallel ranges.

## 9. Risks

1. **Phase-1 regression from the refactor.** Mitigation: the change is type-only, behind the regression gate.
2. **A retry after an interruption can move to another idle worker** (the route closes, IR2) and re-download (about 1 min at 10G). Accepted.
3. **`SetThreadExecutionState` is per thread.** If a short-lived thread set it, it would silently lapse; the test asserts the owning thread.
4. **A worker with a ledger awaiting upload is unreconciled and claims nothing until finish.** This is intended.
5. **Restored outputs fill the controller disk before archive.** P2-2's 507 keeps the stage running and the upload retrying.
6. **P2-2 §8 placed the cancel producers in P2-4.** #294 names OD3, so the minimal producer (route + drawer button) ships here.

## 10. Commits

1. `fix(workflow)`: CX15 + CX16 with their tests.
2. `docs(spec)`: this contract.
3. `refactor(workflow)`: `run_stage`, `ExecRuntime`, translate UHD with profile refresh.
4. `feat(agent)`: claim/renew/finish/GET attempt, envelope `stages`, typed `current`, cancel/release producers, boot settlement.
5. `feat(workflow)`: `RemoteDispatcher`, `WorkerRuntime`, fetch/prefetch, GC, keep-awake, pause, energy, scheduler thread.
6. `test`: fake-worker end-to-end and the crash matrix.
7. `feat(ui)`: currents, requeue, worker monitor, `/status.workflow_worker`.

### Code facts behind the contract
- **Owner evidence on resolve:** `resolve_assignment` requires stopped owner evidence when a GPU resource is held (`film_store._check_local_owner(stopped=True)`). The owner identity must also match the one recorded by the single renew, so the finish handler builds stopped evidence from `assignment.local_owner`.
- **Reconcile rule:** `record_boot` reconciles only when no unresolved assignment remains (`worker_store.py`), which is why boot is re-posted after settlement.
- **Claim key:** the store's idempotency digest covers the whole command, so the claim must carry `job_id`/`expected_version` verbatim. That is why the envelope gains `stages`.
- **No scheduler thread yet:** `routing.run_pass` has no caller today.
- **Energy owner:** `power.activity_source` reads `workflow_host.dispatcher.run_id`/`current_stage()`, so the worker host must expose a `dispatcher`.

## 11. Review dispositions

Codex r1 (CX1–CX6) and the internal review (IR) of `1c6da42`; Codex r2 (CX7–CX9) of `ff27800`. Every finding was fixed; none is deferred. CI-L is the Linux CI failure of the same head. The rules above are updated where a fix changes them.

| Id | Finding | Disposition | Test |
|---|---|---|---|
| CX1 | `stop()` returned True after the settle timed out, and nothing settled again | `stop()` is False while the Job's processes survive; each later call settles again and records the outcome. The host keeps such a dispatcher, and builds no new one beside it (「停止未确认」, retried with backoff) | `test_cx1_stop_stays_unconfirmed_while_the_children_survive` |
| CX2 | A boot `held` child left the process-wide `gpu_lease` free | `held` reserves the lease (holder 「上次的任务进程仍在运行」); the gate's release keeps it until a boot reply no longer holds the children; `stop()` frees it | `test_cx2_a_held_child_of_an_earlier_boot_reserves_the_gpu` |
| CX3 | A re-join stopped only the heartbeat; the old dispatcher ran on with the new identity | Each dispatcher is bound to its session: credential, controller address, staging folder and boot id. A session on another identity first stops and joins the old dispatcher, then builds a new one. A Job running at 重新配对 is therefore stopped and finished `cancelled`, a free retry | `test_a_worker_re_paired_mid_run_stops_its_job_and_retries` |
| CX4 | No GPU reading at start meant executors with no GPU, never rebuilt | Executors are built only with a GPU reading. A later or changed reading rebinds them between attempts, and the rebind is tried again at each probe until no attempt runs | `test_cx4_the_executors_are_built_once_the_gpu_is_read` |
| CX5 + IR4 | 停止并重新排队 during the upload discarded finished GPU work | Decision: finished GPU work is never discarded. The drawer offers the stop only in `running`/`settling`. The controller refuses it in `uploading`/`finishing` (409 `already_settled`, 「修复已完成，正在回传」). The worker ignores a cancel for an attempt that settled ok, so a cancel during the run still stops it | `test_requeue_is_refused_once_the_worker_settled_its_stage`, `test_cx5_a_cancel_after_a_successful_settle_keeps_the_success`, UI 「offers no requeue once the worker uploads」 |
| CX6 | A settled write that exhausted its retries dropped the attempt from delivery | The outcome stays in memory and is written again before any upload or finish; the delivery waits for it | `test_cx6_a_lost_settled_write_is_retried_before_the_delivery` |
| IR1 | Revoke and re-join in one process stalled at 等待对账 (one boot id per process) | A new boot id comes with every new dispatcher. A boot also delivers every settled local attempt file, whatever its verdict says (finish is idempotent by state; a stale one gets 409 and is discarded). The `_revoked` docstring is corrected | `test_a_revoked_worker_joined_again_in_the_same_process`, `test_ir1_a_settled_attempt_is_delivered_whatever_the_boot_says` |
| IR2 | The staged index could claim complete while the files were gone | A release marks the entry `releasing` (never complete) before it deletes, and kept leftovers stay `releasing`; they are still reported in `staged` so `release` comes again. The fetch marks the entry complete before it clears `_fetching`. The claim gate stats each file against its recorded size, and a mismatch is fetched again | `test_ir2_a_staged_file_gone_is_fetched_again_before_a_claim`, `test_ir2_a_release_in_progress_is_never_claimable` |
| IR3 | A refused (422) failed finish retried forever and held the GPU | Messages are cut to 2000 characters at the source. A 400/404/413/422 refusal of a failed finish is terminal: it is logged, the GPU is freed, the attempt file moves to `attempts/refused/`, and the monitor shows 「主控拒绝了…的失败结果」. The controller's assignment is settled at the next boot (not reported → `interrupted`) | `test_ir3_a_refused_failed_finish_ends_and_frees_the_gpu`, UI 「shows a refused result」 |
| IR5 | A `recover()` 404 left `work/<a>/` behind | A 404 collects the attempt, its folder included. Once every attempt file was answered, `work/<uuid>/` folders that no file names are swept; this is skipped while a tick runs, and copies are never touched | `test_ir5_recover_collects_a_dropped_attempt_and_orphan_work`, `test_ir5_nothing_is_swept_while_an_attempt_is_unknown` |
| IR6 | Shutdown could not interrupt an upload | The stop event reaches the block iterator. The stream ends within one block as a cut, no offset read follows, and the controller keeps the prefix for the next start | `test_ir6_stop_interrupts_an_upload_and_ir8_keeps_the_started_time`, `test_a_stop_ends_an_upload_within_one_block` |
| IR7 | Whole-file sends were 3×3 | The dispatcher no longer retries around the client, whose 3 sends are the cap | `test_ir7_a_mismatching_output_is_sent_whole_three_times_in_all` |
| IR8 | 用时 restarted at the upload | The claim time is kept across phases (a boot finish starts at the boot) | `test_ir6_…_ir8_keeps_the_started_time` |
| IR9 | Crash case 5 finished with no timing | The `RestoreTiming` is read from the ledger's restore facts | `test_ir9_a_ledger_found_at_boot_finishes_with_its_timing`, case 5 |
| IR10 | `set_paused` dropped the controller's paused; the local pause was lost at restart | `set_paused` passes the controller's `paused` through, and the local choice is written to `worker-state.json` and loaded at start | `test_ir10_the_local_pause_keeps_the_controllers_and_survives_a_restart` |
| IR11 | `current` could exceed 64 KiB; `power` carried calendar totals | `current` is kept under 32 KiB: the console line is cut, then 本片参数, then the LTX detail, then the energy go. `power` carries only `w`, `gpu_w`, `cpu_w`, `other_w` | `test_ir11_the_typed_current_fits_the_heartbeat` |
| IR12 | Cases 6 and 7 did not prove their claim | Case 7 now ends the worker before the finish is resent, so the next process's `recover()` collects the attempt (one finish). Case 6 asserts the resumed offset is > 0: its half PUT had never been sent (an unquoted non-ASCII path) | cases 6 and 7 |
| IR13 | Linger-based waits | Fake Jasna `mode="sleep"` with a release file. Each test waits until Jasna runs, interrupts it, asserts the stage is still running, then releases it (case 4, case 9, pause, re-pair, revoke) | those tests |
| CI-L | On Linux CI (py3.10/3.12), every e2e test hung with 「等待 GPU 设备」 and no route | **Root cause: a fixture artefact, not a product bug.** The test controller's routing pass and output plans read the runner's real free disk (`transfer.disk_free`). A GitHub Linux runner has far less than the C8 reserve (`CONTROLLER_RESERVE_BYTES`, 100 GB), so the pass held every remote route with 「主控磁盘空间不足」; macOS and Windows runners have more free disk than that. `explain()` hid this because it scheduled without `controller_free_bytes`, so it showed a decision the real pass never made; no `route_film` was refused. Reproduced on Windows by injecting 20 GB. Fix: the e2e world injects the controller's free disk (pass and `Transfers`). `explain()` now uses the real pass's inputs and shows the last real pass. `routed_film` fails at the first pass that holds the film for disk or whose `route_film` was refused, and names the code. The pass also logs each tolerated race at INFO with `code=…` and returns it in `PassOutcome.overtaken`. The C8 gate itself is correct: a real controller with less than 100 GB free waits the same way, and says so | `test_a_decision_that_never_routes_fails_fast_and_names_why`, `test_a_route_overtaken_by_a_race_is_logged_with_its_code` |
| CX7 | After a restart, a persisted successful outcome whose ledger or outputs were gone skipped validation; the delivery raised a filesystem error forever, the attempt never left the delivery and the worker never claimed again | Before every finish attempt a settled success is checked: the ledger is there, its sha256 equals the settled digest, and every output it names is present at its size. Otherwise the outcome is rewritten as failed `cancelled` 「本地结果丢失」 (a free retry, T08-P1a R3; the settled energy is kept), saved, and finished once. The attempt, its folder and the GPU are freed, and the next route is claimed. This holds for a boot `finish` verdict and for a settled file the boot delivers whatever the reply says (IR1) | `test_cx7_a_recovered_success_that_lost_its_files_fails_once`, `test_cx7_an_intact_recovered_success_is_still_delivered`, `test_cx7_the_worker_claims_again_after_a_lost_result` |
| CX8 | An incomplete indexed download was left out of the heartbeat `staged`, so the controller could never name it in `release`; after a route moved it stayed until boot and ate the staging budget | `staged` reports every indexed revision; one that is neither complete nor releasing carries `incomplete: true` (`StagedFilm.incomplete`, default false; part of the routing facts). The controller's `release_list` rule is unchanged and covers it (kept while routed to this worker or held by an assignment). `_release_film` still leaves a revision that is being fetched; the controller names it again at the next beat | `test_cx8_an_incomplete_download_is_reported_and_releasable`, `test_cx8_an_active_fetch_is_never_released`, `test_release_covers_an_incomplete_download` |
| CX9 | A cancelled (requeue) finish omitted the settled energy | The cancelled finish carries `energy` from the settled record, as the failure branch does: null only when it is unknown, never 0 | `test_cx9_a_cancelled_finish_keeps_the_settled_energy`, `test_a_cancel_stops_the_job_and_finishes_cancelled` |
