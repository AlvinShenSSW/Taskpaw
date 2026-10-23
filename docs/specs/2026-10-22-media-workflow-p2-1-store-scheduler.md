# P2-1 (#292): store v4, routes and scheduler core — frozen contract

Date 2026-10-22 · Status: frozen for implementation, amended by the review dispositions of §8 · Base: `afk/claude-p2-271` at 7ee9700 (3.9.19 RC).
Parent design: [phase 2](2026-10-22-media-workflow-phase2-multi-machine.md); its §13 (debate
dispositions) and §14 (operator decisions OD1–OD6) override its earlier sections, and this
contract follows them. Home LAN, single user: the smallest complete design.

P2-1 delivers the ledger, the claim gate, the pure scheduler and the controller's executor
wiring. It ships **no** worker API, HTTP route, heartbeat, transfer or UI. With no remote
worker enrolled, a phase-1 single-machine controller behaves exactly as before.

## 1. Requirements

| # | Requirement | Source |
|---|---|---|
| R1 | Schema v4: verified backup, then one transaction from v1/v2/v3. `film_plans.machine` becomes nullable (NULL = 自动); plans stay immutable. | #292, §6.1, C2 |
| R2 | A mutable pin row per plan (`film_pins`), CAS'd on its version. `confirm_plan` writes it; `set_pin` (改派) changes it and withdraws an unclaimed open route that no longer matches. | C2, §6.3 |
| R3 | `film_routes` with states `{open, closed}`; at most one open route per (film, input revision), enforced by a unique partial index. "Claimed" is derived from unresolved assignments, never stored. | C9 |
| R4 | `route_film` and `withdraw_route` are operator commands with CAS (pin version; route version). A claimed route (unresolved compute assignment) is never withdrawn. | §6.1, C9 |
| R5 | `claim` of every compute stage (restore, asr, translate) requires an open route of that film revision to the claiming worker (`route_required`). The controller opens its own routes implicitly (kind `local`) right before it claims, so phase 1 keeps working. | C10 |
| R6 | Routes close by themselves: a new input revision closes the old revision's unclaimed open route (a claimed one closes when its assignment resolves, IR6); a finished route closes in the resolving transaction (a `local` route once restore, asr and translate are all finished; any other route once restore and asr are); a route one of whose stages failed or blocked closes too, so its retry is routed again (IR2). | C9 |
| R7 | `attest_stopped` (operator, 判定已停止) resolves an unresolved compute assignment and its resource owner as failed `cancelled` with operator attestation evidence, without owner-stopped evidence, and closes the film's open route to that worker. | §3.8, D4 |
| R8 | The controller's executors are built from the fixed compute stage set; translate is built whenever it is a local stage, independent of `whisperjav_exe_path`. | C1 |
| R9 | **OD1 correction:** when `workflow.remote_workers` is on (remote mode), the controller claims no stage that needs the GPU, whatever the verified set is (a worker re-pairing, pending validation or revoked does not hand the GPU stage back); stages that need no GPU stay local (C12). With `remote_workers` off, every stage runs locally exactly as in phase 1, and the scheduler pass decides nothing (IR1). | OD1, C12 |
| R10 | `taskpaw_v3/workflow/scheduler.py` is pure over a store snapshot plus live worker state and a clock value. Eligibility, prediction, ordering and re-assignment follow §2 below. | §4, S3–S6, OD2, OD5 |
| R11 | The chosen route's human-readable reason (the device finish times it beat) is persisted on the route row. | V1 |
| R12 | A film routed to a worker that is 失联 yields an attention reason 「设备失联」; nothing moves automatically. | OD2, V2 |

## 2. Schema v4

`SCHEMA_VERSION = 4`, `SUPPORTED_VERSIONS = (1, 2, 3, 4)`. v1–v3 DDL stays frozen. v4 replaces the
`film_plans` table and adds two tables and one index (DDL verbatim, also the reopen allowlist):

```sql
CREATE TABLE film_plans (
    film_id TEXT NOT NULL REFERENCES films(film_id) ON DELETE RESTRICT,
    manifest_kind TEXT NOT NULL DEFAULT 'input' CHECK(manifest_kind='input'),
    input_revision INTEGER NOT NULL CHECK(input_revision>0),
    ltx_model TEXT CHECK(ltx_model IN ('standard','good','high')),
    machine TEXT REFERENCES workers(worker_id) ON DELETE RESTRICT,
    confirmed_at TEXT NOT NULL,
    backfilled INTEGER NOT NULL DEFAULT 0 CHECK(backfilled IN (0,1)),
    actors TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(film_id,input_revision),
    FOREIGN KEY(film_id,manifest_kind,input_revision)
        REFERENCES manifests(film_id,kind,revision) ON DELETE RESTRICT
)
CREATE TABLE film_pins (
    film_id TEXT NOT NULL,
    input_revision INTEGER NOT NULL,
    worker_id TEXT REFERENCES workers(worker_id) ON DELETE RESTRICT,
    updated_at TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
    PRIMARY KEY(film_id,input_revision),
    FOREIGN KEY(film_id,input_revision)
        REFERENCES film_plans(film_id,input_revision) ON DELETE RESTRICT
)
CREATE TABLE film_routes (
    route_id TEXT PRIMARY KEY NOT NULL,
    film_id TEXT NOT NULL REFERENCES films(film_id) ON DELETE RESTRICT,
    input_revision INTEGER NOT NULL CHECK(input_revision>0),
    worker_id TEXT NOT NULL REFERENCES workers(worker_id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK(kind IN ('auto','pinned','local')),
    state TEXT NOT NULL CHECK(state IN ('open','closed')),
    reason TEXT NOT NULL,
    routed_at TEXT NOT NULL,
    closed_at TEXT,
    close_reason TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
    body_json TEXT NOT NULL CHECK(json_valid(body_json)),
    CHECK((state='closed')=(closed_at IS NOT NULL AND close_reason IS NOT NULL))
)
CREATE UNIQUE INDEX one_open_route_per_film
    ON film_routes(film_id,input_revision) WHERE state='open'
```

Route `kind`: `auto` (scheduler, film pinned to 自动), `pinned` (scheduler, the film's pin),
`local` (the controller's own implicit route; operator-trusted, no pin check).

### 2.1 Migration

1. Read-only preflight validates the exact v1/v2/v3 format; anything else is refused untouched.
2. Verified backup (`_backup_version`, unchanged): no backup, no upgrade.
3. One `BEGIN IMMEDIATE` transaction:
   - v1: add the intake tables (as today);
   - v1/v2: create the **v4** `film_plans` directly and run the U04 backfill (unchanged SQL);
   - v3: `ALTER TABLE film_plans RENAME TO film_plans_v3`, create the v4 `film_plans`, copy
     every row verbatim, `DROP TABLE film_plans_v3` (the rename keeps the exact v4 DDL text in
     `sqlite_master`, which the reopen allowlist compares);
   - create `film_pins`, `film_routes` and the index;
   - one pin row per plan with `worker_id NULL` (D9 as revised by §13 and OD1: 本机 is never a
     routable GPU worker, so phase-1 pins become 自动; `updated_at = confirmed_at`);
   - `user_version = 4`, full v4 validation, commit.
4. Any failure rolls back: the file keeps its old version and data, the backup stays.

Plan `machine` values are history (the energy history keys on them) and are copied unchanged.

## 3. Store commands and reads

All are typed commands through `_execute` (idempotent replay, audit), operator role. New error
codes: `route_required`, `route_exists`, `route_claimed`.

- **`confirm_plan`** — `FilmPlanConfirm.machine: Id | None`. A given machine must be verified.
  Inserts the plan and its pin (`worker_id = machine`, version 1), except that the controller
  pins 自动 (`NULL`), the one encoding of 自动 the migration writes too (IR9). The store learns
  the controller's worker from its runtime (`WorkflowStore.controller_id`, set on open).
- **`set_pin`** (`PinSet`: film, revision, `expected_version` = pin version, `worker_id | None`,
  reason) — current revision only (`cas_conflict`); a given worker must be verified; the
  controller means 自动 (IR9). If an open route exists and the new pin names another worker, it
  is withdrawn in the same transaction; a claimed one refuses the command (`route_claimed`).
  改派 to 自动 keeps any route (IR8). A kept route's version moves, so a scheduler withdrawal
  decided from a snapshot older than the 改派 loses its CAS (CX2).
- **`route_film`** (`RouteOpen`: `route_id`, film, revision, worker, kind, `expected_pin_version`,
  reason) — current revision (`cas_conflict`); at least one compute stage unfinished
  (`precondition_failed`); worker verified, participating, `enabled` (`precondition_failed`);
  no open route (`route_exists`); no unresolved compute assignment of the revision on another
  worker (`route_claimed`). For `auto`/`pinned`: the film revision is planned, i.e. has a pin
  row (`precondition_failed`, IR13), the pin row's version equals `expected_pin_version`
  (`cas_conflict`), and the pin is NULL for `auto` or equals the worker for `pinned`
  (`precondition_failed`). `local` skips the pin rules.
- **`withdraw_route`** (`RouteWithdraw`: `route_id`, `expected_version`, reason) — open route,
  version CAS; refused while any compute assignment of that film revision is unresolved
  (`route_claimed`).
- **`attest_stopped`** (`StoppedAttestation`: assignment, attempt, operation,
  `expected_version` = assignment version, reason, evidence) — the assignment is unresolved and
  belongs to a worker (component stages are refused); evidence is all passed, produced by the
  operator, and includes the kinds `operator_attested_stopped` and `exec_failed:cancelled`
  (the latter keeps it a free retry, T08-P1a R3). Stage and attempt become `failed` (a proven
  `succeeded`/`failed`/`blocked` stage keeps its status), the operation resolves (`verified`
  for a proven success, else `failed`), the assignment and its resource owner resolve with no
  owner-stopped evidence, and the film's open route to that worker closes (reason 判定已停止).
  Refused (`precondition_failed`) for the controller's own assignment, whose stop the
  controller proves locally (restart settlement), and within 5 min of the assignment's last
  renewal (`renewed_at`), while its worker may still be running it (IR4). The store has no
  heartbeat in P2-1; the UI's two-channel check (C4, P2-4) is the other guard. Like
  `resolve_assignment` it is allowed while the authority is in recovery; the route commands
  are not.
- **`claim`** — after its existing checks, a compute stage requires an open route of the
  revision with `worker_id = command.worker_id` (`route_required`).
- **Route closing** — `_put_input_sql` closes unclaimed open routes of older revisions
  (新版本); a claimed one stays open, holding its worker's slot, until its assignment resolves
  (IR6). `resolve_assignment` and `skip_stage` close an unclaimed route whose revision is no
  longer current (新版本), whose stages are finished (完成), or one of whose stages failed or
  blocked (失败, IR2): a stage waiting for the operator or its retry holds no slot, and the
  retry is routed afresh. OD2 is kept: nothing moves while a route is open; a failure already
  ended the route's work.
- **Reads** — `list_workers`, `film_route(film, revision)` (its open route or None),
  `list_routes(open_only)`, `get_pin`, and `routing_snapshot` (one locked read: current-revision
  films with an unfinished restore/asr or an open route, with plan, pin, stage statuses, claim
  owner and time, largest probe, summed probe duration, source bytes; open routes; workers with
  boot reconciliation, GPU model, VRAM and approved per-stage limits; succeeded restore/asr
  attempts on a worker as rate samples, newest first). `older_revisions` carries the same facts
  for every older revision an open route still holds, so the pass accounts that route with its
  own revision (CX5). A sample's GPU model is the probe of the physical GPU its assignment held
  at claim (`assignments.physical_resource_id` → the probed capability bound to it), never the
  worker's current card; an attempt without a held GPU has no model (CX6). A rate sample is GPU throughput only
  (CX3): a restore needs Jasna's measured wall time and a success note with no reused volume;
  an ASR needs a success note whose every volume ran WhisperJAV (`generated_ja`/`no_speech`)
  and is timed by its attempt span. The evidence counts reused volumes but not their
  durations, so mixed attempts are left out too. The note formats are
  `executors.RESTORE_DONE` / `ASR_DONE`, shared with the executors.

## 4. Controller runtime (executor wiring)

- `pipeline.default_executors` iterates the fixed compute stage set: restore when
  `jasna_exe_path`, asr when `whisperjav_exe_path`, translate always (R8).
- `LocalDispatcher` per admission reads `scheduler.remote_mode`, the same predicate the pass and
  设备 use (IR1/IR5/OD1): `None` when `workflow.remote_workers` is off (single machine), else
  the names of the workers other than its own that are `verified` and not `revoked` (a retired
  one is never verified), possibly none. **OD1 correction:** the first P2-1 predicate was
  "remote configured = the setting and a verified, non-revoked remote exists", which handed
  restore/ASR back to the controller's 8 GB GPU while the only worker was re-pairing or revoked.
  Remote mode is now the setting alone; the verified names only say who can run a stage.
- Whether a stage needs the GPU is `executors.needs_gpu(executor, manifest)` for both the
  dispatcher and the pass (`routing.run_pass` computes `gpu_stages` with it from the
  controller's executors, IR3).
- Before a compute claim the dispatcher ensures an open route to itself:
  - no open route → `route_film(kind=local, reason 本机)`;
  - an open route to itself → claim;
  - an open route to another worker → if the controller will run this stage (translate, a stage
    that needs no GPU, or remote mode is off) and the route is unclaimed, withdraw it
    (reason 转本机) and route to itself; otherwise wait (「已派往 {name}」).
- In remote mode, a restore/asr that needs the GPU waits with
  「等待 GPU 设备：<up to three verified remote names>」 (IR5), or 「等待 GPU 设备：没有可用的 GPU 设备」
  while none is verified (OD1 correction), and is never claimed locally; one that needs no GPU runs locally (C12). Translate is always the
  controller's, whatever its executor's GPU flag.
- With no remote worker every reason, order and outcome of phase 1 is unchanged; the only new
  persisted facts are the controller's own route rows (and their audit rows).

## 5. Scheduler (`scheduler.py`, pure)

**Inputs.** `RoutingSnapshot`; `live: Mapping[worker_id, WorkerLive]` (`last_seen` on the
controller's clock, `paused`, `tiers_ready`, `asr_ready`, `free_bytes`, `staging_cap_bytes`
default 500 GB, `version_ok`); `now`; `controller_id`; `controller_free_bytes` (None = unknown,
gate skipped); `gpu_stages: Mapping[(film_id, input_revision), frozenset]` (stages that need a
GPU; default: both; `run_pass` always passes it, computed by `needs_gpu` from the controller's
executors over each revision's manifest, IR3).
**Output.** `SchedulePass(routes, withdrawals, waiting, attention)`; the routing glue
(`routing.run_pass`) applies withdrawals then routes through the store commands; a CAS loss is
skipped and retried next pass. The route decisions count the slots and staging the withdrawals
free, so after a lost withdrawal none is applied: the pass decides once more from a fresh
snapshot, and after a second loss leaves routing to the next pass (CX4). While `remote_workers(snapshot.workers, controller_id)` is empty
(single machine) the pass returns an empty `SchedulePass` (IR1).

**Constants.** 失联 at 60 s without a heartbeat (S3/OD2); at most 2 open routes per worker
(1 active + 1 prefetch, S4); 1 GB/s transfer (S6); tie window max(600 s, 5 %); restore needs
VRAM ≥ 15360 MiB (16 GB class, OD5); worker staging need(f) = 2.2 × source + 2 GB, cap 500 GB,
free − need ≥ 50 GB; controller reserve 100 GB (C8); 10 newest samples; remaining-time floor 120 s.

**Routed stages** of a film: its unfinished restore and asr that need a GPU. ASR stays on the
restore worker by default because the route spans both stages.

**Candidates.** Planned current-revision films whose first routed stage is `pending` (restore
pending, or restore finished and asr pending), with no unresolved compute assignment and no open
route of that revision other than an unclaimed `local` route of the controller (C12 hand-over).
An open route of an older revision does not block the new one; it still holds its worker's slot
(IR6) and is accounted with its own revision's busy time, staging and output (CX5). Order: 4K first,
then plan confirm time, then film id.

**Eligibility (worker × film)**, first failing rule is the reason: not the controller (OD1,
silently skipped); not retired/revoked; `verified` 「未核验」; participating and not disabled
「未启用」; not draining 「停领」; heartbeat < 60 s 「离线」; not paused 「暂停领取」; version
「版本不一致」; boot reconciled 「等待对账」; restore: VRAM 「显存不足，不参与修复」, profile limit ≥ the
film's largest probe 「显存不足 4K」/「分辨率超出设备上限」, `tiers_ready[tier]` 「缺少<tier>画质模型」;
asr: profile limit, `asr_ready` 「识别未就绪」; worker disk 「空间不足」 (queued + need > cap, or
free − queued − need < 50 GB, where queued is the staging of the worker's open routes and of the
routes already decided in this pass, IR11/CX1). A worker with 2 open routes (of any revision)
is full (「设备队列已满」), which is not ineligibility. The controller-disk gate (Σ 1.2 × source of
open routes with restore unfinished, plus this film, + 100 GB > controller free), once it trips
for a film, stops every later new route of the pass with 「主控磁盘空间不足」 (IR10).

**Pins.** 自动 is stored as NULL only; the store never keeps a pin to the controller (IR9), so
the pass has no unpin step (a stray one waits with 「指定设备不可用」). A pinned film routes only to its pinned worker when eligible with a free
slot; otherwise it waits with 「等待 <name>：<reason>」 and is never moved.

**Prediction (S6).** ratio = wall seconds / film seconds. Per-tier factor = median restore ratio
of the tier ÷ median of `standard` (1.0 without both). Restore base per (GPU model, resolution
class 1080p/4K) = median of the newest 10 samples' ratio ÷ their tier factor; ASR base per GPU
model. No sample → ratio 1.0 (D10: film duration, FIFO onto the least loaded). Film duration
unknown → 1 h. `cost(w, f) = fetch + Σ est + upload` with fetch = source / 1 GB/s while restore is
still pending, upload = 1.2 × source / 1 GB/s while restore is unfinished.
`free_at(w) = now + Σ remaining cost of w's open routes`, a claimed stage's estimate reduced by
its elapsed time (floor 120 s). `finish = free_at + cost`. Energy (§4.2) = the first routed
stage's estimate × the median `avg_w` of that (stage, GPU model); unknown without a sample.
Samples with no wall time or duration are ignored.

**Choice.** Minimum finish (then fewer open routes, name, id). When every candidate within the
tie window of the best has an energy, the lowest energy among them wins instead (then fewer
routes, name, id). Each route updates the pass state (route counts, `free_at`, staging, the
controller output total). A film with no eligible worker waits with 「等待设备：<reasons>」
(「设备队列已满」 when eligible workers are only full, 「没有可用的设备」 when there are none).

**Reason (V1).** 「自动 · 约 2 小时 10 分后在 5070 Ti 完成 · 胜过 5060 Ti-2 约 2 小时 40 分」 (up to
three beaten devices), plus 「 · 无历史，按排队先后」 when no sample was used, plus 「 · 约 0.9 度」
when energy is known; 「指定 <name> · 约 … 后完成」 for a pin. Persisted as the route `reason`.

**Re-assignment.** Only an unclaimed open route is ever withdrawn, and only when: its `auto`
worker became ineligible for the film's routed stages by a rule other than heartbeat age, disk
or a full queue (OD2: 失联 never moves a film; disk only gates new routes); or it is the
controller's unclaimed `local` route of a film that now has a routed GPU stage (C12 hand-over),
withdrawn only when the same pass routes the film (an eligible worker with a free slot exists,
IR1).
A `pinned` route is never withdrawn by the scheduler. Withdrawn films are re-routed in the same
pass. 改派 goes through `set_pin`. Every open route to a 失联 worker (claimed or not) yields
attention 「设备失联」 (OD2/V2).

## 6. Test plan

- **Migration:** v3→v4 converts `film_plans` verbatim and pins every plan to 自动; v2→v4 and
  v1→v4 (fixture) end at v4; exactly one verified backup at the old version; an injected failure
  (rebuild DDL, version, commit) leaves the old version and no v4 table; a modified v4 table is
  refused; new databases are v4.
- **Store:** nullable machine; pin CAS and 改派 (withdraws unclaimed, refuses claimed); route CAS,
  `route_exists`, pin rules per kind, `route_claimed`; claim `route_required` and a withdrawn
  route rejecting the claim; auto-close on finish and on a new revision; `attest_stopped` frees
  the GPU owner, closes the route, keeps a proven success, is a free retry, rejects stale or
  component assignments; idempotent replays.
- **Runtime:** translate executor without WhisperJAV; phase-1 dispatcher suite unchanged; with a
  remote mode on the controller never claims a GPU-needing restore/asr (also with no verified
  worker: it waits 「没有可用的 GPU 设备」) but runs translate and no-GPU stages, and takes over an
  unclaimed foreign route only for those.
- **Scheduler (fake clock, 5 workers: 4 × 16 GB + 1 × 8 GB):** 8 GB never routed restore (OD5)
  but takes ASR-only films; ≤ 1 open route per film; pinned respected and never moved; 4K first;
  energy tie-break inside the window; no-history FIFO onto the least loaded; 失联 yields
  attention and no withdrawal; ineligible auto routes withdrawn and re-routed; controller-disk
  gate; persisted reason. Randomised sequences over the real store (route, claim, finish, crash
  via new boot, withdraw, 改派, attest, retry) never show two unresolved assignments for a job,
  two unresolved owners of a GPU, two open routes for a film, or a claim without the claimer's
  open route. Extended (IR7): the controller's dispatcher runs between passes, films get new
  input revisions, some restores need no GPU, and passes race a claim, a dispatch, a finish or a
  改派 between snapshot and writes; also never more than 2 open routes on a worker, a no-GPU
  restore never routed or run remotely, and a hand-over only for a film routed in that pass.

## 7. Out of scope (later children)

Worker API, pairing UI, heartbeats and `WorkerLive` production (P2-2); transfer, staging, remote
dispatcher, `run_stage`/`ExecRuntime`, the scheduler's own thread and its triggers, retries of
remote failures (P2-3); 待处理 自动/pin select, 改派 and 判定已停止 UI with the C4 two-channel check,
board machine rows, route reasons in `waiting` and the drawer, per-machine energy (P2-4); 4K on
16 GB (P2-5). `local_stages` stays a fixed rule, not a setting.

## 8. Review dispositions (IR = internal review, CX = Codex outer gate)

OD1 correction (P2-3 phase): IR1/IR5's predicate is now `scheduler.remote_mode` (the setting;
the verified set only names who can run a stage); the pass runs in remote mode even with no
verified worker, and the controller's GPU takes no stage then (see R9, §4).

Fixed in P2-1: IR1/IR5 (one `remote_workers` predicate; a single machine decides nothing;
hand-over only when routed), IR2 (a failed/blocked route closes), IR3 (`needs_gpu` shared),
IR4 (no 判定已停止 for the controller or within 5 min of a renewal), IR6 (a claimed
old-revision route stays open), IR7 (extended property suite), IR8/CX2 (改派 to 自动 keeps the
route; a kept route's version moves), IR9 (自动 = NULL), IR10 (the disk gate stops the rest of
the pass), IR11/CX1 (worker disk counts queued staging), IR13 (auto/pinned need a plan), CX3
(rate samples are GPU-only), CX4 (no route decision applied after a lost withdrawal; one fresh
retry), CX5 (open routes accounted per revision), CX6 (samples keep their attempt-time GPU).

### Deferred (no code in P2-1)

- **IR12** — a restart grace for 「设备失联」 (a controller restart sees every worker as 失联
  until its first heartbeat): P2-2, with heartbeats.
- **IR14** — the controller's local route after a refused claim stays open (a later dispatcher
  round claims it, or the C12 hand-over takes it); revisit with the remote dispatcher (P2-3).
- **IR15** — downgrade and backup-restore notes for v4 (a v3 build cannot open v4; restoring the
  pre-upgrade backup loses the routes and pins made since): P2-5 docs.
- **IR16** — over-design guard: do not grow route `kind`, `body_json` or the energy tie-break
  beyond this contract without an operator decision.
- **4K on the 8 GB controller** (found while fixing IR3) — the controller's approved profiles
  cap every stage at its card's resolution, translate and an all-reused restore included, so a
  4K film restored and transcribed on 16 GB workers waits 「没有可接 4K 的设备」 for its translate
  on an 8 GB controller. Decouple the no-GPU stages from the card's limit with 4K on 16 GB (P2-5).
