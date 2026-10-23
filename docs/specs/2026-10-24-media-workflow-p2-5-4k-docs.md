# P2-5 — 4K restore on 16 GB workers, deployment guide, phase-2 real-machine checklist (#296)

Part of EPIC #271, phase 2. This document is the frozen issue contract.

Design inputs:
- `docs/specs/2026-10-22-media-workflow-phase2-multi-machine.md` §13 and §14 OD1–OD7.
- `2026-10-23-media-workflow-p2-od7-asr-on-controller.md`.
- `2026-10-23-zero-config-setup-299.md`; its §12 CX14 is carried here.

Binding operator decisions:
- **OD5:** a GPU under 16 GB never restores.
- **OD7:** workers restore only, and the controller (5060 8 GB) runs ASR and everything after it.
- **OD4:** a tool or capability change needs 核验.
- **Windows-only (2026-10-24):** the controller and the workers run on Windows only; the Mac runs the Hub only.

Fleet: four workers, 3× 5060 Ti 16 GB and 1× 5070 Ti 16 GB.

## 1. Code facts this contract builds on

- **The worker caps every profile at 1080p.** `RestoreExecutor.max_resolution` (`workflow/exec_restore.py:505`) returns `min(max_resolution(vram), FULL_HD)`, and `probe_body` (`remote_worker.py:241`) proposes that cap as each profile's max.
- **The tier is a constant.** `exec_restore.TIER = "1080p"` (`:80`) is passed to `jasna.build_argv`, written into the tools settings (`:498`) and into the launch params (`:346`).
- **The presets already enable 4K LTX on 16 GB.** `jasna_presets.py:22` sets `ltx_4k` on for every preset except 5060-8, but the setting is never reached today.
- **Routing already knows each film's size and refuses 4K.** `route_store._probes` fills `RoutingFilm.max_width` and `max_height` from the intake ffprobe. `scheduler.eligibility` (`:296`) refuses a film that does not fit the approved profile, with the reason 「显存不足 4K」. The claim rechecks every probe against the approved max (`worker_store.py:617`).
- **The estimate is already keyed by resolution class.** `Rates.estimate` uses `(gpu_model, class)`, where `resolution_class` is "4K" when either side exceeds 1920×1080.
- **Nothing called a "verified profile" exists.** The verified profile in the issue text *is* the `ApprovedCapability` stored by 核验并启用 (`devices.approve`).
- **`docs/guides/deployment.md`** covers ports 5680, 5681, 5690 and 5691 and the yaml-based setup. It has no 5682, no GPU-worker role and no zero-config flow.
- **CX14** (`src-tauri/src/main.rs:965-985`): `release_old()` takes `Backend` without holding `LIFECYCLE`.

## 2. Requirements

**R1 — the worker advertises its true restore max.**
- `RestoreExecutor.max_resolution(vram)` returns `max_resolution(vram)`: 3840×2160 at 15360 MiB and above, 1920×1080 below.
- The phase-1 1080p cap is removed. The controller never restores (OD7), so the cap has no remaining user.
- The ASR and translate executors are unchanged.

**R2 — the tier is chosen per film.**
- The restore run picks the Jasna tier for each main video from its probed width and height, using the existing jasna tier rule (`jasna.py:430`, >1.5× 1080p pixels → "4k").
- It passes that tier to `build_argv`, so 4K files get the preset's 4K settings (`ltx_4k`, `unet4x_4k`, 4K clip size).
- It records the tier per video in the launch params and record, alongside the existing 本片参数.
- The tools-level `settings["tier"]` constant goes. The config digest must still change when the 4K settings change, so it keeps the preset's 4K fields.
- An unknown size (no probe) runs as "1080p", as today.

**R3 — 4K only through a verified (approved) profile.**
- A worker that was approved before R1 keeps a 1080p approved profile. It keeps taking 1080p films and does not take 4K films until the operator runs 核验并启用 again.
- The re-核验 must be needed and visible:
  - When the proposed profile max exceeds the approved max, the device shows the existing "needs 核验" state, the same path as a config-digest change (CX9).
  - The 待处理 reason for a 4K film on a ≥16 GB worker whose approved profile is 1080p reads **「4K 需重新核验」**, not 「显存不足 4K」.
- 「显存不足 4K」 stays for a worker that truly cannot take 4K.
- No new UI is added; only the reason text and the existing needs-核验 state are used.
- **OD5 is unchanged:** a worker under 16 GB is refused by `RESTORE_MIN_VRAM_MIB` before any profile check, and `approve()` still refuses it.

**R4 — eligibility tests.** A table-driven test over `scheduler.eligibility` and the claim recheck covers:
- 16 GB approved at 4K: a 4K film and a 1080p film are both eligible.
- 16 GB approved at 1080p: a 4K film gets 「4K 需重新核验」 and a 1080p film is eligible.
- 8 GB: 「显存不足，不参与修复」 whatever its profile.
- The claim side refuses a 4K film for a worker whose approved max is 1080p, even if the route says otherwise.
- `probe_body` on a 16 GB fake proposes 3840×2160; on an 8 GB fake it proposes 1920×1080.
- The executor builds 4K argv for a 4K probe and 1080p argv for a 1080p probe. Use a fake Jasna; there is no GPU in tests.

**R5 — CX14 (carried from #299).**
- `release_old()` runs under `LIFECYCLE`. The quit path therefore either sees the old backend and kills its group itself, or waits until `release_old` has finished killing it.
- The whole release→spawn→install sequence of one respawn is serialized with `begin_exit`.
- There must be no deadlock with `spawn()`, which takes the same lock: take the lock once for the whole respawn, or release it between the steps with the EXITING check repeated.
- Cargo test: `begin_exit` waits for an in-flight `release_old`.

**R6 — deployment guide (operator-facing, Chinese UI terms kept as-is).**
- Add a section 「多机媒体工作流部署」 to `docs/guides/deployment.md`, and mark the yaml steps as the legacy/manual path.
- **Roles:** Windows controller (Hub plus media center plus local ASR), Windows GPU worker (Jasna restore), Mac Hub (read-only monitor and the single `/events` consumer).
- **Ports:** 5680 (agent read API, LAN), 5681 (control, loopback), 5682 (worker API, LAN, controller ↔ worker), 5690 and 5691 (Hub). Each entry says which firewall rule the wizard creates.
- **Zero-config flow per role, with no file editing:**
  - install → first-run wizard (角色, 局域网访问, 家庭令牌 generate-on-controller / paste-on-others);
  - the firewall UAC prompt;
  - pairing the worker from the controller's 设备 page, then 核验并启用;
  - 4K needs re-核验 after this update (R3).
- **Operations:** 失联 (OD2: raise an error, retry in the background, the operator chooses 改派 / 判定已停止); per-film stop is 停止并重新排队 only (OD3); a tool-version change needs 核验 (OD4).
- **Troubleshooting:** 401 means the household token differs; the address changed (self-heal, same subnet); 版本不一致.

**R7 — phase-2 real-machine checklist (the T09 addendum).**
- New file `docs/specs/2026-10-24-media-workflow-phase2-real-machine-checklist.md`, in the same table style as the phase-1 checklist (step / expected / result column left blank).
- Rows:
  - 5G and 10G transfer throughput (a 40 GB source in about 35–70 s, resumable after a cable pull, hash-checked);
  - five machines with a mixed 1080p/4K batch (4K routed only to re-verified 16 GB workers, 自动 picks the earliest finish);
  - a real Jasna crash mid-film, where the Job kills the tree, the film returns to 待处理 / the error is shown, and there is no orphan process;
  - real power numbers per worker against the estimate;
  - 失联 by unplugging a worker (error shown, nothing auto-moved, 改派 and 判定已停止 both work);
  - controller ASR on the 5060 8 GB, including a 4K source (the OD7 deferral);
  - a 4K film on 16 GB with the full LTX detail in the monitor;
  - CX14: on the Mac Hub, quit during 立即重启 leaves no orphan.
- The file links the #299 zero-config checklist instead of repeating its rows.

## 3. Out of scope

- Worker model download (D6), `release_resource`, periodic renew, cross-machine resume and parallel ranges.
- Parallel ASR, removing the worker's ASR finish code, and ASR time in 预计.
- The NTFS ADS race (F02).
- macOS controller or worker paths.
- De-duplicating `RESTORE_MIN_VRAM_MIB` / `FOUR_K_VRAM_MIB`.
- A real-hardware 4K throughput verification (that belongs to R7's checklist).

## 4. Verification

- Targeted tests for R1–R5 must fail first (red), then pass.
- Then ruff, mypy (native/linux/darwin), the UI tests/lint/tsc if the UI changes, cargo test, and the full repo pytest from the repo root.

## 5. Dispositions

| ID | Finding | Disposition |
|---|---|---|
| D1 | R3: a worker approved at 1080p "keeps 1080p until re-核验" and a stale-approval state is shown through a new check. | The existing CX9 path already does this, more strongly: `probe_changed` compares the profiles including `max_width/height`, so the worker posts its 4K probe, `POST /capabilities` stores it with `approved=None`, and the store de-verifies the worker (待核验, disabled) until 核验并启用. The config digest also changes (the `tier` setting is gone). No new check was added; a test pins the max-only change (`test_a_higher_max_alone_drops_an_approved_worker_back_to_validation`, `test_a_higher_proposed_max_alone_needs_a_new_verification`). The eligibility reason 「4K 需重新核验」 covers a verified record whose approved max is still 1080p. |
| D2 | R2: the tier is recorded "in the launch params and record". | Recorded in the launch params (`params.tier`, shown by 本片参数) only. `RestoreFacts` (the stage result) is unchanged, to keep the persisted result schema and its digest fixtures stable; the output's own width/height are already in it. |
| D3 | R3: 「显存不足 4K」 stays for workers that truly cannot take 4K. | Since OD5 refuses every card under 15360 MiB first, a worker reaching the profile check is always 4K-capable by VRAM; the old text stays only as the unreachable-today fallback for a card below `FOUR_K_VRAM_MIB`. |
