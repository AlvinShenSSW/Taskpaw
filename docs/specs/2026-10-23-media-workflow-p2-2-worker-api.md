# P2-2 (#293): worker API, enrollment, heartbeat and transfer (frozen contract)

Date 2026-10-22 · Base: `afk/claude-p2-271` after P2-1 (#292) · Parent: [phase 2](2026-10-22-media-workflow-phase2-multi-machine.md). §13/§14 (OD1–OD6) override the earlier sections of that design. Home LAN, single user, smallest complete design.

P2-2 adds the controller's worker API, enrollment, live state, both transfer directions and 设备 v1. It runs **no** remote stage: there is no claim, finish or renew endpoint yet (P2-3). With `remote_workers: false` (the default), nothing changes — also after workers were approved: the single "remote configured" predicate is `remote_workers and <a verified, non-revoked remote exists>`, used by the dispatcher, the routing pass and 设备 alike, so turning the flag off puts restore/ASR back on the controller (IR1).

## 1. Requirements

| # | Requirement | Source |
|---|---|---|
| R1 | Config keys `workflow.role: controller\|worker` (default controller), `remote_workers: false`, `worker_api_port: 5682`. The 5682 app is mounted only when role=controller, enabled and remote_workers. It is a third uvicorn server claimed up front and included in the stale-port reclaim. A port collision fails startup loudly. | §2.1, §6.2 |
| R2 | 添加设备 runs `register_worker`, then `issue_pairing` (15 min, single use), and shows the code `TP1:<host>:<port>:<pairing_id>:<ticket>` (IPv6 host in brackets). `POST /pair` runs `authenticate_pairing` and `complete_pairing`. The server fills in worker_id, device_id, generation = max+1 and a long expiry. | §2.2 |
| R3 | The worker generates `credential_id` and `secret` and writes them to `worker-identity.pending.json` **before** sending. On a lost reply it calls `GET /self` with its Bearer; a 200 means it is paired. Only then does the pending file atomically replace `worker-identity.json`: a failed re-join keeps the working identity and restarts its loop (IR7). Joins are serialized. | §2.2, C6 style |
| R4 | The probe (GPU, VRAM, device and GPU identity, free disk, the **proposed profiles** of its restore/asr executors) is posted with `record_capabilities` (no `approved`), **only when** identity, VRAM or profiles differ from `GET /self`. A worker post de-verifies the worker by store rule. The probe re-runs every 60 s (tier/ASR readiness for the heartbeat, CX4) and at every beat while the GPU cannot be read (IR8); the change check stays in front of every post. | §2.2, OD4 |
| R5 | 核验并启用 (operator) writes a new capability revision that reuses the worker's probe and holds the chosen profiles (修复 only when VRAM ≥ 15360 MiB, OD5), then calls `update_worker(participation, enabled)`; that second write re-reads the version and is skipped when already enabled, so a write in between never leaves a verified worker disabled (IR9). 设备 offers 启用 for a verified, disabled row. A changed tool, config or model profile triggers a new probe, then 待核验, then a one-click 核验. Approve and admission answer 409 while `remote_workers` is off. | OD4, OD5 |
| R6 | `POST /boot` at every process start. The same `boot_id` is re-posted until the reply says `reconciled` (C6) — once per heartbeat: heartbeats run meanwhile and the worker shows 等待对账 (IR6). In P2-2 the worker reports no attempts. | §3.8, C6 |
| R7 | Heartbeat every 5 s while the worker has an open route and every 15 s when idle. It goes into an in-memory `FleetLive` and makes **no store write and no renew** (C5). The reply carries `routes`, global `paused`, `cancel`, `release` and `reconcile_needed`. | §3.3, C5 |
| R8 | 失联 after 60 s with no heartbeat, on the controller's clock. No new routes go to a 失联 worker and nothing moves. The worker keeps retrying with backoff capped at 30 s (the exponent is clamped, CX1), and its state clears by itself when heartbeats return. Any other error (staging unusable, a refused post, a bad heartbeat) is shown as the reason, backed off and retried; only a revoked credential or a stop ends the loop (IR8, IR15). 撤销 drops the worker's live entry at once; with `remote_workers` off every row reads 远程未启用, never 失联 (IR14). | OD2, S3 |
| R9 | Restart grace: for 60 s after the controller starts, a worker that has not been heard from is 「等待心跳」, not 失联 (IR12, C14). | IR12, C14 |
| R10 | If the heartbeat's `version` or `protocol` differs from the controller's, then `version_ok=false`, which shows as 「版本不一致」. There is no 426. | S2 |
| R11 | Envelope: answered only for a film revision with an open route to the caller. | §3.1, D2 |
| R12 | Ranged GET of work copies, which the worker writes to `<staging>/work/<relative_path>.part`. The worker hashes while writing (re-hashing the prefix on resume), checks size and sha against the envelope, then does an atomic rename (an empty work copy gets an empty part, CX5). A mismatch deletes the part. | §3.5, S5 |
| R13 | One streaming PUT per file, starting at the received offset. The controller hashes while receiving and checks the whole-file sha. A match is renamed atomically to `work/<path>` and records `{sha, size, mtime_ns}`. A mismatch resets the file to 0. **Resume (IR3):** a PUT never re-reads a prefix. The hash state of a closed part is kept in memory with its size and mtime_ns; without it (a controller restart) the PUT answers 409 `prefix_unverified` and `GET /outputs` rebuilds it — the worker calls GET/POST outputs with a 30-minute timeout. **Settling (IR2):** GET outputs and an identical plan re-post verify a complete part no stream holds and commit it, or reset it to 0 on a mismatch; a too-long part is reset. A refused rename (Windows sharing violation) answers 503 `commit_pending` with `Retry-After`, keeps the verified state and is retried by the next request. The worker treats only `done: true` as success. A client that disconnects keeps the bytes already received and causes no 500 (IR11). | S1, C13 |
| R14 | Upload routes refuse an attempt that is resolved, not current for its job, or owned by another worker (C7). An attempt in `unknown` that is still current is accepted. | C7, §3.8 |
| R15 | Disk rules (§4). | §3.6, C8 |
| R16 | `validate_finish_refs()` is unit-tested and not wired yet. | C3, C13 |
| R17 | Each worker has at most 1 stream per direction; a second one gets 429 with `Retry-After`. A stream without progress for 120 s (PUT body read, or a work-copy send) is dropped and frees its slot (IR4). After a reset or refusal the worker reads its next step from GET outputs, which also carries a refusal the server sent before reading the body (IR12). | S5 |
| R18 | 设备 v1 and worker join (§5). On role=worker, `/workflow/*` returns 404 and the worker emits no media-workflow events. | §2.3, §6.4 |

## 2. Endpoints (port 5682, `/worker/v1`)

Every route requires `Bearer <credential_id>.<secret>` of a `compute_worker` principal (anything else: 401/403), except `/pair`. Errors carry `{code}` only.

| Method path | Body cap | Store effect | Errors |
|---|---|---|---|
| POST `/pair` `{pairing_id, ticket, credential_id, secret}` | 1 MiB | `complete_pairing` | 401 ticket bad/used/expired; 409 revoked/retired |
| GET `/self` | – | read: own record, boot reconciled | 401 |
| POST `/capabilities` | 1 MiB | `record_capabilities` (worker); server fills `expected_version`, one retry on CAS | 403 `approved` sent; 409 |
| POST `/boot` `{boot_id, assignment_ids:[], local_owners:[]}` | 1 MiB | `record_boot` | 409 |
| POST `/heartbeat` | 64 KiB | none (FleetLive; one read of open routes) | 422 |
| GET `/films/{film}/{rev}` | reply ≤ 1 MiB | read | 404 not routed |
| GET `/films/{film}/{rev}/items/{item_id}` | Range | read; `FileResponse`, `X-Sha256`, `X-Size` | 404, 416, 429 |
| POST `/attempts/{a}/outputs` `[{path,size,sha256}]` | 1 MiB | none; writes `work/.incoming/<a>/plan.json`; identical re-post returns offsets | 409 `stale_attempt`/`plan_conflict`, 422 path, 507 |
| GET `/attempts/{a}/outputs` | – | none; settles parts first (may hash for minutes), then the plan with `received`, `done` and mismatch count per file | 404, 409 |
| PUT `/attempts/{a}/files/{path}` `Content-Range: bytes o-(n-1)/n` | ≤ n−o | none; commit on completion | 409 `offset_mismatch{received}`/`prefix_unverified{received}`/`stale_attempt`, 413 (truncated back to o), 422 `hash_mismatch`, 429, 503 `commit_pending`, 507 |

Heartbeat in: `boot_id, version, protocol, paused, gpu{name, vram_mib, used_mib, util, power_w, hold}, disk{free, staging_used, cap}, tiers_ready, asr_ready, staged[{film, rev}], current:null, transfers[]`.

Heartbeat out: `server_time, paused, routes[{route_id, film, rev, position}], cancel:[]` (filled by P2-3/P2-4), `release` (= staged films that have no open route and no unresolved assignment of this worker), `reconcile_needed`.

Envelope: `film, rev, route{id, version}, ltx_model, manifest, workcopy[{item_id, relative_path, size, sha256}]`, plus `ledgers{stage: {attempt_id, payload_digest, text}}` for each earlier compute stage that succeeded. Downloads are resolved **only** by item_id through the manifest.

## 3. Modules and config

**Controller side:**
- `agent/server/worker_api.py`: app factory with docs and openapi off, a late-bound host provider (503 `controller_starting` until running), a body-cap middleware, and the Bearer → `AuthProof` → `store.authenticated_worker(auth)` step (a new read).
- `workflow/fleet_live.py`: `FleetLive.observe/snapshot/state(now)` with states online, lost and waiting, where `started_at` gives R9.
- `workflow/transfer.py`: plans, parts, commit, disk gate, `validate_finish_refs`.
- `scheduler.schedule(..., live_since)`: a worker with no entry raises no 「设备失联」 before `live_since + 60 s`.
- `models.HardwareProbe.profiles: tuple[ProfileCapability, ...] = ()` (additive).
- Loopback control: `GET/POST /control/workflow/devices`, plus `…/{id}/pairing|approve|admission|revoke|retire`, each with `expected_version`.

**Worker side:**
- `workflow/remote_worker.py`: `WorkerHost` (start after ready, stoppable, joins its threads), `WorkerIdentity`, `parse_code` (refuses wildcard and global hosts), probe, boot loop, heartbeat loop.
- `workflow/worker_client.py`: `ProxyHandler({})`, `NoRedirectHandler`, timeouts, `download_film()`, `upload_file()`, with retry and backoff.
- `workflow/staging.py`: dir validation and the need/cap rules.
- Loopback: `GET /control/workflow/worker` and `POST /control/workflow/worker/join {code, staging_dir}`.

**Worker config:** `workflow.role: worker`, `staging_dir` (local only: UNC, network or relative paths are refused), `staging_cap_gb: 500`, and the existing tool paths. The controller address lives only in the identity file.

## 4. Disk and path rules

- **Controller:** a plan is accepted only if free − Σ(planned − received of all other **live** plans) − this plan's remainder ≥ 100 GB, else 507 「主控磁盘空间不足」. A plan is live while its attempt is unresolved and current for its job (the C7 rule, any worker). The plans and parts of every other attempt — resolved, superseded or unknown to the store — are swept at controller start, on plan creation and on GET outputs (IR5). The free-space source can be injected in tests.
- **Worker:** a download starts only while (staging_used − present) + need ≤ cap and free + present − need ≥ 50 GB, where need = 2.2 × source + 2 GB and present = this film's bytes already staged (they are part of its need, not extra, IR16). Otherwise the heartbeat reports 「空间不足」.
- **Upload paths:** `models.RelativePath` (this rejects `\`, `:`/ADS, `..`, reserved names and non-NFC), must start with `<a>/`, must not be `<a>/stage-result.json`, and the resolved path must lie under `work/<a>`. Within a plan no path (case-folded) may be another's folder, nor its `.part` name's (IR13). A target that already exists must be identical.
- **Finish refs:** each `FileRef` must be either (a) committed under `<a>/` with recorded sha and size and an unchanged size and mtime_ns, or (b) equal in (path, sha, size) to a manifest work-copy item. The ledger's film, revision, stage and attempt must match.

## 5. UI (V3 design system, `pages/media-workflow.md`)

**设备** (the 「后续」 marker goes):
- **本机 row:** shows the stages it runs (翻译) and has no actions.
- **Worker rows:** name, address, enrollment/admission chip, heartbeat (在线 Ns 前 / 失联 / 等待心跳), GPU·VRAM·util·W, disk free and staging used/cap, tier readiness 标准/良好/高 and 识别, version, and the eligibility reason from `scheduler.eligibility` on a synthetic 1080p film.
- **Actions by state:** 添加设备 (name and address, then the code with copy and expiry), 核验并启用 (probe vs last approved with changes highlighted; 修复/识别 switches, 修复 disabled under 16 GB with 「显存不足，不参与修复」), 启用 (verified but disabled, IR9), 停领/恢复, 重新配对, 撤销 (confirm), 退休 (confirm; only when revoked and no owner is unresolved, IR10). With `remote_workers` off only 撤销 and 退休 remain and the heartbeat reads 远程未启用 (IR14).
- **Form and accessibility:** native dialogs, 44 px controls, states shown as text and colour, single column at 390 px, noun headings only (no slogans).

**Worker console:** a 「媒体工作流」 section in 设置 with 加入 (code and staging dir), the connection and credential state, and 重新加入.

## 6. Security invariants (each tested)

- **S-1:** 5682 binds `bind_host`. Wildcard and `bind_is_global` hosts are refused before the claim, and a collision fails loudly.
- **S-2:** Route enumeration equals the §2 table exactly. All routes are under `/worker/v1/`. There is no docs or openapi route, and nothing forwards to 5681.
- **S-3:** Without a Bearer every route returns 401 except `/pair`. Operator and component credentials get 403. A revoked or retired worker gets 401. `/pair` ignores a Bearer.
- **S-4:** Every film and attempt access is scoped to the caller's worker_id.
- **S-5:** The network app stays all-GET (the #286 test). On role=worker, `/workflow/*` returns 404. The control app stays loopback.
- **S-6:** No secret or ticket appears in logs, argv, the task log or error bodies. The access log is off. Identity writes are atomic. `repr` hides secrets.
- **S-7:** Shutdown joins the worker host threads and closes the 5682 socket.

## 7. Test plan

- **Fake worker:** a real `WorkerClient` against the real app on a loopback uvicorn server. It pairs, probes, is approved, boots and shows online on 设备. No claim is possible.
- **Enrollment:** a lost pair reply recovers through `/self`; ticket expired, reused or wrong; 重新配对 bumps the generation and revokes the old credential; a probe change leads to 待核验 and then 核验 (OD4); boot re-posts until reconciled; revoke gives 401; retire is blocked while an owner is unresolved.
- **Live state, fake clock:** online, lost at 60 s, waiting in the grace window (no attention), then lost; version mismatch; heartbeat over 64 KiB gives 413.
- **Transfer matrix:**
  - resume at random offsets in both directions;
  - whole-file mismatch (download re-fetch, upload reset and count);
  - Range 416;
  - traversal, ADS, absolute, backslash and reserved paths refused;
  - oversize 413;
  - 507;
  - second stream 429;
  - a stale or resolved attempt, and another worker's attempt, refused (C7);
  - controller restart mid-upload (the server restarts, offsets survive, the hash is rebuilt);
  - an unrouted film returns 404.
- **Finish-ref unit:** both ref kinds; a changed mtime or size, a foreign attempt or a wrong stage refused.
- **UI:** fixture tests for each row state and dialog, 390 px, no slogans.

## 8. Out of scope

- **P2-3:** claim, finish and renew endpoints, `GET /attempts/{a}`, `RemoteDispatcher`, `run_stage`/`ExecRuntime`, attempt-file boot reconcile, the prefetch and fetch loop, staging GC beyond `release`, the scheduler thread, keep-awake (C11), the worker 「媒体处理」 monitor, `/status` `workflow_worker`, translate without a resolution cap (TranslateExecutor.max_resolution = UHD + profile refresh).
- **P2-4:** producers for `cancel`, board, drawer and Mac rows for 「设备失联」, 改派, 判定已停止 with the C4 check, automatic power peers.
- **P2-5:** 4K narrowing and approval.
- **Not planned:** TLS, scheduled rotation, parallel ranges (T09 fallback).

## 9. Risks

1. **Stalled GPU work before P2-3.** Approving any worker makes the controller stop its own GPU stages (P2-1). Mitigations: `remote_workers` defaults to false, the 核验并启用 dialog says so, and P2-2 ships in the same release as P2-3.
2. **Accidental de-verification.** A worker's own capability post de-verifies it, so posting only on change is tested.
3. **Old stored probes.** The additive `HardwareProbe.profiles` field needs a test that v4 rows re-validate.
4. **Throughput.** 10G throughput of one Python stream (T09), and re-hashing a 40 GB prefix on resume after a controller restart (about 1–2 min, accepted). It happens in GET outputs, which the worker calls with a long timeout, never inside a PUT (IR3).

## 10. Commits

1. `docs(spec)`: this contract.
2. `feat(workflow)`: config keys, `FleetLive`, `schedule(live_since)`, `authenticated_worker`, `HardwareProbe.profiles`.
3. `feat(agent)`: the 5682 app with pair/self/capabilities/boot/heartbeat, launcher wiring, security tests.
4. `feat(workflow)`: transfer (envelope, item GET, outputs and PUT, disk, C7, finish refs) and the matrix tests.
5. `feat(workflow)`: worker side (identity, join, probe, boot, heartbeat, client) and the fake-worker test.
6. `feat(ui)`: 设备 v1 and worker join.

### Contract notes (from reading the code)
- `authenticate_pairing` refuses a consumed pairing, so a lost `/pair` reply cannot be retried; R3 uses `GET /self`.
- A worker's own `record_capabilities` drops it to pending_validation/disabled; R4 posts only on change.
- Operator approval must be a new capability revision; the binding's `capability_revision` must equal it.
- Approved profiles must equal the worker's executor `tools()`; nothing carries them to the controller today, hence `HardwareProbe.profiles`.
- Seeding placeholder heartbeats would make the scheduler withdraw auto routes; `live_since` is the smallest restart-grace fix.

## 11. Review dispositions

Internal review (IR) and Codex (CX) findings on the P2-2 branch. Every item below is fixed; the test names are in `taskpaw_v3/tests/`.

| Id | Finding | Disposition | Test |
|---|---|---|---|
| IR1 | `remote_workers: false` did not roll back an approved worker | Fixed: `scheduler.remote_workers(..., enabled=)` is the single predicate; the dispatcher (`remote_enabled`), `run_pass`/`schedule` and 设备 get the setting; approve/admission answer 409 when off | `test_turning_remote_workers_off_runs_restore_and_asr_locally_again`, `test_with_remote_workers_off_approval_and_admission_are_refused` |
| IR2, CX2 | A complete but uncommitted part was a dead end, and the client called it done | Fixed: GET outputs verifies and commits (or resets) it; a refused rename is 503 `commit_pending`, retried; the client needs `done` | `test_a_complete_uncommitted_part_is_committed_on_get`, `test_a_complete_part_with_a_wrong_hash_is_reset_on_get`, `test_a_failed_commit_is_retried_by_the_next_request`, `test_a_received_but_uncommitted_upload_is_not_done` |
| IR3, CX3 | Resume livelock: the prefix re-hash ran before the body was read | Fixed: in-memory hash state per part (size + mtime_ns checked); after a restart GET outputs rebuilds it under a 30-minute client timeout; a PUT without it is 409 `prefix_unverified` | `test_a_resumed_put_reuses_the_hash_state_of_the_cut_stream`, `test_a_controller_restart_mid_upload_keeps_offsets_and_rebuilds_the_hash`, `test_a_large_prefix_resumes_after_a_real_restart_without_a_put_timeout` |
| IR4 | A dead connection held a stream slot | Fixed: 120 s idle timeout on PUT body reads and work-copy sends | `test_a_stalled_upload_frees_its_slot_and_a_concurrent_put_is_429`, `test_a_stalled_download_frees_its_slot` |
| IR5 | `.incoming` was never cleaned (permanent 507) | Fixed: only live plans count; sweep at start, plan creation and GET outputs | `test_plans_of_settled_attempts_are_swept_and_stop_counting`, `test_the_sweep_runs_at_start`, `test_unit_plans_count_only_live_attempts` |
| IR6 | The boot loop ran without heartbeats while unreconciled | Fixed: one boot post per beat, 等待对账 | `test_heartbeats_run_while_the_boot_waits_for_reconcile` |
| IR7 | Re-join overwrote a working identity before pairing; no lock | Fixed: pending file, atomic replace after success, join lock, old loop resumed | `test_re_join_with_an_expired_code_keeps_the_working_identity`, `test_the_same_code_after_an_unreachable_reply_reuses_the_credential` |
| IR8 | Ordinary errors ended the worker loop; no probe retry | Fixed: shown, backed off, retried; probe retried at each beat | `test_ordinary_errors_never_end_the_loop` |
| IR9 | Approval's two writes could leave verified+disabled | Fixed: idempotent re-read second write; 启用 action | `test_approval_survives_a_write_between_its_two_steps`, UI `offers 启用 for a verified worker left disabled` |
| IR10 | 退休 had no confirmation | Fixed: confirm dialog | UI `drains, re-pairs, revokes after confirmation and retires with the row version` |
| IR11 | A client disconnect mid-PUT gave a 500 and lost buffered bytes | Fixed: own receive loop keeps the bytes, no traceback | `test_a_client_disconnect_keeps_its_bytes_and_logs_no_error` |
| IR12 | A refusal before the body was read reached the client as a reset | Fixed: after a reset the client reads GET outputs, which raises the real code | `test_a_refusal_before_the_body_is_read_surfaces_its_code` |
| IR13 | `.part` suffix collision between a file and a folder | Fixed: plan check includes `<path>.part` | `test_a_part_name_cannot_shadow_a_folder` |
| IR14 | Fleet off showed 失联; a revoked entry lingered | Fixed: heartbeat `off` (远程未启用); `FleetLive.forget` on 撤销 | `test_with_remote_workers_off_approval_and_admission_are_refused`, `test_a_revoked_worker_leaves_the_live_state_at_once`, UI `shows 远程未启用 and only 撤销 …` |
| IR15 | A heartbeat 422 read as 「无法连接主控」 | Fixed: 「主控拒绝了心跳（code）」 | `test_a_refused_heartbeat_shows_its_reason` |
| IR16 | Download admission counted staged bytes as needed | Fixed: present bytes added back to free | `test_already_staged_bytes_are_not_needed_twice` |
| IR17 | Dead code | Fixed: `Transfers.stream`, `Transfers.committed`, `retrying`, `WorkerClient.with_credential` removed (P2-3's finish route reads the plan's `committed` itself) | – |
| IR18 | Missing transfer tests | Added: concurrent PUT, client disconnect, stuck slot, large-prefix resume with a slow hash, real server restart (new app instance, same dirs) | the IR3/IR4/IR11 tests above |
| CX1 | `backoff(1024)` overflowed | Fixed: exponent clamped | `test_backoff_never_overflows` |
| CX4 | Readiness cached for the process lifetime | Fixed: probe every 60 s, change check kept | `test_readiness_is_refreshed_without_reposting_an_unchanged_probe` |
| CX5 | A zero-length work copy failed its rename | Fixed: empty part created | `test_an_empty_work_copy_is_staged` |
| CX6 | A reply cut mid-body (`IncompleteRead`, reset, partial JSON) escaped as a 500 and skipped the `/self` recovery | Fixed: every read or transport failure in `call`, `open` and the upload reply is the transient `unreachable` | `test_a_truncated_reply_is_transient`, `test_a_pair_reply_cut_mid_body_recovers_through_self` |
| CX7 | A download that ended early deleted the valid partial and restarted from 0, ending in a false `hash_mismatch` | Fixed: an early EOF keeps the `.part` and raises the transient `unreachable`; the next call resumes with `Range`; only a complete file that fails verification is reset | `test_a_download_cut_by_an_early_eof_keeps_its_part_and_resumes` |
| CX8 | After a good probe, a later missing GPU reading kept advertising tiers and ASR | Fixed: the reading clears `tiers_ready` and `asr_ready` until a probe returns; 设备 shows the worker ineligible (识别未就绪) | `test_a_missing_gpu_reading_withdraws_readiness` |
| CX9 | The probe projection dropped `tools().config_digest`: a changed executor path or setting left `probe_changed` false and the worker stayed verified (OD4) | Fixed: the probe carries `config_digest` (sha256 over the executors' profile + config digests), `HardwareProbe.config_digest` stores it (absent from older rows), `probe_changed` compares it; an old stored probe without it compares as changed once (one re-核验 after upgrade); the 核验 dialog shows a 配置 row marked 配置变更 | `test_the_config_fingerprint_is_part_of_the_probe_comparison`, `test_a_stored_probe_without_a_fingerprint_is_reposted_once`, `test_the_default_probe_fingerprints_the_executor_settings`, `test_the_approve_dialog_data_carries_the_fingerprints`, UI `shows 配置变更 when only the config fingerprint differs` |
| CX10 | A `stop()` during an in-flight `join()` was followed by `_spawn()` (completed pairing or `_resume()`), restarting the heartbeat thread after shutdown | Fixed: `stop()` is terminal (`_closed`), waits for the in-flight join (bounded by its timeout); `_spawn`/`_resume` and a later `join` refuse after it; a completed pairing still persists its identity | `test_a_shutdown_during_a_join_leaves_no_worker_running[paired,refused]` |
| CX11 | A re-join stopped the working loop, then a failed pending-identity write (disk full, access denied) skipped `_resume()` and escaped as a 500, leaving the worker stopped | Fixed: everything that can fail on disk (loading the old identity, creating the data dir, writing the pending identity) runs before the loop is halted; an `OSError` is a `JoinError` 「无法写入身份文件」 and the running worker is untouched | `test_a_re_join_that_cannot_write_its_identity_leaves_the_worker_running` |
| CX12 | An HTTP error whose body was cut (`IncompleteRead` from `exc.read()`) escaped `_api_error` as a raw exception, bypassing the `/self` recovery and loop restoration | Fixed: `_api_error` catches `HTTPException`/`OSError`/`ValueError` and keeps the status (a cut 503 is transient, a cut 401 still revokes). Audit: `_read_json`, the download body reads and `open()` already map these to `ApiError`; no other read path is outside the contract | `test_an_error_reply_with_a_cut_body_is_still_an_api_error[503,401]` |
| CX11b | After a successful `/pair` (the old credential is gone on the controller) a failed identity save or a failed `os.replace` onto the identity file escaped `join()` as a raw exception | Fixed: the worker runs on the paired identity in memory; `_promote()` (save pending, rename over the identity file) is retried at each heartbeat and the reason reads 「身份文件未保存，重试中」 until it lands. At start, a pending file is checked with `GET /self` in the worker thread: 200 promotes it (a lost-reply or unsaved pairing), 401 drops it and keeps the current identity, unreachable waits with backoff | `test_a_failed_identity_save_after_a_good_pair_keeps_the_worker_running[save,replace]`, `test_a_restart_promotes_a_pending_identity_the_controller_accepts[save,replace]`, `test_a_restart_drops_a_pending_identity_the_controller_refuses` |
| CX13 | A failed re-join to a different, unreachable controller left its pending identity; on restart `_run()` blocked in `_settle_pending()` retrying forever, so the healthy saved identity never started | Fixed: the start-up check is ONE bounded attempt (5 s): 200 promotes, 401 drops; unreachable or any other answer starts the saved identity at once and the beat loop re-checks the pending one every 60 s without blocking (a later 200 promotes it and switches the session to it, a later 401 drops it). With no saved identity it still retries with backoff. Status shows `pending` and the reason 「新的配对待确认」 | `test_an_unreachable_pending_controller_never_blocks_the_saved_identity`, `test_a_later_confirmation_of_the_pending_identity_switches_to_it`, `test_a_later_refusal_of_the_pending_identity_drops_it` |
| CX14 | Re-pair succeeded but promotion failed, so the saved credential was revoked while the valid replacement stayed pending; on restart a transient failure of the pending `/self` fell back to the saved identity, whose 401 stopped the worker for good without the pending one ever being checked again. CX11b, CX13 and CX14 were each a special case of the same gap | Fixed by one rule in `_run()`, replacing `_settle_pending()`/`_recheck_pending()`: a 401/403 refuses only the identity it was sent with, a transient error refuses none, and the loop stops for good (「凭据已撤销，请重新加入」) only when every identity it holds is refused. Until then it retries with backoff: a usable saved identity runs while the pending one is checked whenever due at any wait in the session, even while the saved controller is unreachable (`_pause`); a refused saved identity hands over to the pending one; a 200 promotes it. The same rule covers a re-join whose reply and `/self` were both lost: the old identity resumes and the new one is held as pending | `test_a_worker_stops_only_when_every_identity_it_holds_is_refused[saved×pending, 15 cases]`, `test_a_re_join_whose_reply_was_lost_is_held_until_it_settles` |

### Deferred after the fix allowance (6 cycles used) — admitted, fixed first in P2-3 (#294)

| Id | Finding (Codex r7) | Why deferred | P2-3 action |
|---|---|---|---|
| CX15 | `transfer.sweep()` can delete an upload plan created between its liveness snapshot and the directory scan (`transfer.py:327-330`); later GET/PUT 404, partial bytes lost. | Recoverable: the worker re-posts the plan and re-sends; no stuck state, no corruption. | Recheck liveness and upload activity under the attempt lock right before deletion; concurrent create-vs-sweep test. |
| CX16 | First join: `/pair` succeeds but both its reply and the `/self` recovery are lost; with no previous identity, `remote_worker.py:505-508` returns without entering pending recovery, so the worker stays `unjoined` with a pending file. | Recoverable by the operator issuing a new pairing code; no data loss. | Keep the pending identity and run the same recovery loop when there is no previous identity; covered by the identity table test. |
