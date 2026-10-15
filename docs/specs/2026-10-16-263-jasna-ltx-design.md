# #263 — Jasna「1080p 档：使用 LTX」 tickbox (Jasna 0.11.1); 「8K VR」 greyed out while it is ticked; version 3.9.14

Date: 2026-10-16 (design v5, FROZEN — round 5 CLEAN. History: round 1: C1–C4 (P1) + refuted C6 claim resolved in v2 and revalidated in round 2; C5, C8 deferred; C11 withdrawn. Round 2: N1 (P1) and the C1-fix corrections N4/N5 resolved here; N6/N7/N9 wording/evidence; N2/N3/N8 + C10 rest deferred minor. v4 — round 3 found no P1/P2; its wording items S1–S3, the N6 ambiguity, the reopened N7 numbers and the N1 nit are fixed in v4 (revalidated in round 4). v5 — round 4 M1 (minor): one alert text valid in every case)
Issue: #263. Owner, in order: 「可以选择LTX 然后LTX打开的时候vr会灰掉」; 「尽量这个设备可以能跑的参数」; 「LTX和4k 1080P影片处理区别大吗」; 「4K可以用超分的把 4x」; 「那就在ltx tickbox边上注明就1080p会开启 其他的还是按照原来的」.
Driver: `/afk` — Claude Opus 5.5 drives and implements; single outer gate Codex gpt-6-astra (high) (owner 「codex astra单门」). Owner 「完成后版本号增加0.0.1，发布新的exe」: merge when AFK merge-ready, release v3.9.14 with the Windows EXE.

## Spec review

Jasna 0.11.1 adds a second, supporter-only primary restoration model, **LTX** (a diffusion model: more detail than BasicVSR++, much slower, NVIDIA only). The V3 Jasna task always launches BasicVSR++.

Core need: **one tickbox**「1080p 档：使用 LTX」 on the Jasna form, labelled so the operator sees it applies to the 1080p tier only. Ticked → the task's 1080p-tier files launch with LTX (with the LTX options that run on the owner's RTX 5060 8 GB); every 4K-tier file and every other setting launch exactly as before — the 4K tier keeps BasicVSR++ with its own unet-4x 「超分」 tickbox. While ticked, the settings LTX cannot use show greyed and are ignored at launch: 「8K VR」 (owner rule; LTX cannot restore SBS VR) and 「1080p 档：使用 unet-4x」 (Jasna rejects secondary restoration with LTX). They keep their saved values and apply again when the tickbox is cleared.

Why 1080p only: on this card LTX cannot finish a 4K file — every 4K LTX run ended with `Cannot fit a DiffVAE decode tile under the memory budget` in the last stage (device matrix), while 4K BasicVSR++ + unet-4x runs.

### Verified facts (Jasna 0.11.1 at C:\Jasna, RTX 5060 8 GB, 32 GB RAM, 2026-10-16; executed, logs in the run directory)

- `jasna.exe --help`: `--restoration-model-name {basicvsrpp,ltx,ltx-undistilled}` (default basicvsrpp); `--ltx-fast/--no-ltx-fast` ("RTX 50-series (Blackwell) only; needs the fast model file"); `--ltx-auto-split PERCENT` ("restore only mosaics at least PERCENT of the video height with LTX and smaller ones with BasicVSR++ (12 is a good start) … keeping the video's own codec … a video that can't be split is restored whole with LTX"); `--ltx-seed`, `--ltx-large-canvas` ("needs 12 GB of free VRAM (ignored otherwise)"), `--ltx-trial` ("Output is not restored"). argparse abbreviations still allowed.
- Backend-foreign options are rejected before any GPU work, exit 2 (`_reject_options_of_other_restoration_backend`): `--ltx-fast` / `--ltx-auto-split` without an LTX model → `jasna: error: --ltx-fast needs --restoration-model-name ltx or ltx-undistilled`; LTX with `--secondary-restoration unet-4x` (with or without auto-split) → `--restoration-model-name ltx does not support --secondary-restoration`; LTX with `--denoise low` → `… does not support --denoise`. The check compares against the parser default: LTX with `--secondary-restoration none` is accepted.
- LTX accepts `--max-clip-size 45`, `--temporal-overlap 8`, `--codec hevc --cq 24`, `--detection-model rfdetr-v6`, `--vr-mode sbs` at parse time.
- LTX on an SBS VR file (trial, `--vr-mode sbs`) → `Error processing test_clip1_1080p.mp4: LTX does not support VR180 side-by-side video; restore it with BasicVSR++`, exit 1.
- Missing model: the CLI prints `LTX distilled model not found in C:\Jasna\model_weights\ltx-restore:` + the file list, then asks `Download it (14.9 GB)? [y/N]:` with `input()`; stdin at EOF → `EOFError`, exit 1. File lists printed per variant:

  | Variant | Model file in `model_weights\ltx-restore` |
  |---|---|
  | `ltx` | `ltx-restore-alpha1-distill8-int8.safetensors.enc` |
  | `ltx` + `--ltx-fast` | `ltx-restore-alpha1-distill8-nvfp4.safetensors.enc` |
  | `ltx-undistilled` | `ltx-restore-alpha1-int8.safetensors.enc` |
  | `ltx-undistilled` + `--ltx-fast` | `ltx-restore-alpha1-nvfp4.safetensors.enc` |
  | every variant | `vae.safetensors.enc`, `vae-decoder.safetensors.enc` |

- More parse-time results (exit 2 before any GPU work, `jasna-argv2/`): with LTX, `--fmp4` or `--segments` + `--ltx-auto-split` → `--fmp4 cannot be combined with --ltx-auto-split`; `--no-compile-basicvsrpp` → `--compile-basicvsrpp needs --restoration-model-name basicvsrpp`; `--stream` → `ltx does not support --stream`. Accepted with LTX: `--fmp4` without auto-split, `--denoise-step`, `--no-enable-crossfade`, `--rtx-scale`, `--tvai-model`, `--tvai-denoise`, `--retarget-high-fps`, `--vr-mode auto`, `--secondary-restoration=none`, `--restoration-model-path`, `--working-directory`, `--compile-basicvsrpp` (the default). Without LTX: `--ltx-seed` / `--ltx-trial` rejected, `--no-ltx-trial` / `--no-ltx-large-canvas` (defaults) accepted.
- `--ltx-trial` writes the output file anyway (an unrestored copy of the input, 1.3 MB for the 1.2 MB test clip).
- LTX progress (stderr, tqdm, `\r` updates): `LTX 1/4 scan:  45%|████▌     | 136/300 [00:01<00:01, 133.25frame/s]`, then `LTX 2/4 encode:  52%|█████▏    | 156/300 [00:27<00:25,  5.62frame/s, run 12:12 left]` — four stages, the percent restarts each stage, `run … left` is Jasna's estimate for the whole video (present from stage 2 once a speed profile exists).
- Killing the parent `jasna.exe` (taskkill /T) could not terminate a protected child (`Access is denied`), but that child exited on its own within seconds; no `jasna.exe` was left running.
- Device results: see **Device matrix** below.

### Code facts (`taskpaw_v3/monitors/plugins/jasna.py`, main 43062f3)

- `build_argv()` (~1085) is pure; argv order `exe --input --output --max-clip-size --temporal-overlap --secondary-restoration --codec --cq --detection-model [--vr-mode sbs] <extra>`. `_launch_locked()` (~1910) resolves `unet` from the tier tickbox (#208: the 4K tickbox reads `_VR8K_UNET4X_4K` under 8K VR), the per-run degrade map and the retry flag; capture mode spawns with `stdout=PIPE, stderr=STDOUT` and **no `stdin`** (inherited from the backend); console mode uses `CREATE_NEW_CONSOLE`.
- `owned_flags_in(extra, vr_8k)` + `_selects()` (abbreviation-aware) back the validator's "TaskPaw owns" rejection; `_VR8K_OWNED_FLAGS` are owned only while `vr_8k`.
- `restore.started` task-log data: `index`, `total`, `mode` (`unet-4x` | `plain`), `profile` (`8k-vr`, only when ticked). `_tier_suffix()` → ` [1080p 1920x1080, unet-4x off]`, ` [4K …, 8K VR, …]`.
- Capture-mode progress uses lada's `parse_progress_line` (percent / elapsed / remaining / fps); `_progress` is merged verbatim into the status metrics (`metrics.update(self._progress)`); the UI renders unknown metric keys as generic tiles (`MonitorMetrics.tsx:115`).
- Start alerts: `vr_detector_available()` (#208 AC5) alerts once when ticked and `rfdetr-vr-v1.onnx` is missing.
- Frontend: the #208 lock (`LockedFields {after, note, fields}` via `LockedFieldsContext`, decorative disabled display + hidden-but-mounted rjsf field, invalid values stay visible) is generic; `MonitorWizard` builds it from `ui_schema.vr_8k["ui:options"].taskpawProfile`.
- Pinned tests: `test_jasna.py:204` (20 own fields), `test_jasna.py:327-333` (`vr_8k` between `whisperjav_extra_args` and `clip_size_1080p`), `test_jasna.py:1757` (`ui:order[:6]`), `test_jasna_subs.py:411-418`, `test_catalog.py:330`.
- Owner's live task (agent.yaml): `jasna_capture_progress: true`, both unet-4x ticked, `clip_size_4k: 45` — it must launch exactly as before after the upgrade (the new tickbox defaults to off).

## Device matrix (RTX 5060 8 GB, 1.3–1.4 GB VRAM held by desktop apps; trial mode = real speed, no restore)

| Run (trial, `--ltx-trial`, real speed) | Input | Result |
|---|---|---|
| `ltx` (standard int8) | 1080p, 300 frames | **CUDA out of memory** while loading the transformer — twice (deterministic) |
| `ltx --ltx-fast` | 1080p, 300 frames | **740 s** with a cold kernel cache (920 kernels compiled in that run; ~74x realtime) — **541–614 s warm** (rows below: ~54–61x realtime); 15–16 of 48 transformer blocks resident, the rest streamed from RAM; peak VRAM 4.3 GB; process RAM ~15 GB. The very first LTX run adds a one-off ~11 min speed-profile pass. |
| `ltx --ltx-fast --ltx-auto-split 12` | 1080p, 300 frames | **614 s**, but the split did not happen: `LTX auto split skipped (The selected ranges have no usable safe video cut points, so they would require re-encoding the entire video); restoring the whole video with LTX`. The 614 vs 740 s difference is the kernel cache (920 kernels compiled in the first run, 0 here), not splitting (C6). Auto-split's saving is unmeasured. |
| `ltx --ltx-fast` | 4K (3840x2160), 300 frames | **failed after 638 s**, in the last (compose) stage: `ValueError: Cannot fit a DiffVAE decode tile under the memory budget: min tile ~80f x 320x320px … usable_bytes=1280921600. Reduce resolution or free GPU memory.` |
| `ltx --ltx-fast --ltx-auto-split 12` | 4K, 300 frames | **failed after 626 s**, same error (usable 1.32 GB); the split was skipped the same way |
| `ltx-undistilled --ltx-fast` | 1080p, 300 frames | **968 s** warm (235 kernels reused): **1.6–1.8x** the warm fast `ltx` (541–614 s), in line with Jasna's "about 1.5x"; 15 denoise steps per window instead of 8, 2.96 vs ~2.1 s per step; 17–18/48 blocks resident; peak VRAM 4.7 GB; RAM ~14 GB; its own one-off speed-profile pass first |
| BasicVSR++, no secondary | 1080p | restore 1.9 s for 300 frames; VRAM 2.7–3.7 GB |
| BasicVSR++ + unet-4x | 1080p | restore 2.7 s + secondary 2.3 s for 300 frames; VRAM 3.3 GB |
| BasicVSR++ + unet-4x, clip 45 | 4K, 300 frames | **6.8 s** processing (24 s wall incl. start-up); VRAM max 6.4 GB |
| TaskPaw's exact LTX argv (clip 90, overlap 8, `--secondary-restoration none`, hevc cq 24, rfdetr-v6, fast, auto-split 12, `--working-directory`) | 1080p, 300 frames | **541 s, exit 0** (C7, `jasna-c7/`); the split was skipped the same way; peak VRAM 4.6 GB. With `--working-directory <wd>` the `.ltx-<random>` scratch folder appeared in `<wd>` only (never next to the output) and was removed when the run ended; the output folder held only the staging `a-破解.tmp.mp4`. |

Reading: LTX's cost follows the number of mosaic windows (15 on the 1080p clip, 16 on the same clip at 4K), not the frame size — but at 4K the frame buffers leave the VAE decoder too little memory on 8 GB, so 4K cannot finish. Killing a running LTX `jasna.exe` (TaskPaw does on Stop, on a save-restart and on quit) leaves its scratch folder `.ltx-<random>` behind — 15 files / 16 MB for the 15-window test clip (C2); runs that end by themselves remove it; `--working-directory` relocates it (verified, last row). Jasna also prints `CUDA Sysmem Fallback Policy: Driver Default (recommended: Prefer No Sysmem Fallback)` on every run.

## Frozen issue contract

### AC1 Fields and defaults (backend)

`JasnaConfig` gets these fields (English `title`/`description` in jasna.py, zh in `schemaI18n.ts`):

| Field | Type / default | Meaning |
|---|---|---|
| `ltx_1080p` | `bool`, default `False`, title 「1080p 档：使用 LTX」/ "1080p tier: restore with LTX" | The 1080p tier launches with LTX. The description starts with "1080p tier only: 4K files and every other setting stay as they are." |
| `ltx_fast` | `bool`, default `True` | `--ltx-fast` (RTX 50 only; Jasna errors on other GPUs). Default on: the standard LTX model does not load on an 8 GB card (device matrix). Only with `ltx_1080p`. |
| `ltx_auto_split` | `int`, `ge=0`, `le=100`, default `12` | `--ltx-auto-split N` when > 0: only mosaics at least N % of the video height use LTX, smaller ones BasicVSR++ (Jasna: "12 is a good start"); 0 = the whole film with LTX. Only with `ltx_1080p`. |
| `ltx_best` | `bool`, default `False` | 「LTX 最佳模型」: `--restoration-model-name ltx-undistilled` instead of `ltx` (Jasna's "LTX (best)": slightly more detail; Jasna says about 1.5x slower, measured 1.6–1.8x on this card; its own model download). Only with `ltx_1080p`. |

`ui:order`: `… whisperjav_extra_args, ltx_1080p, ltx_fast, ltx_auto_split, ltx_best, vr_8k, clip_size_1080p …` — the LTX block sits directly above the 「8K VR」 switch it greys. Changed pins (named; the only existing assertions this design changes): `test_jasna.py:204` own-field count 20 → 24; `test_jasna.py:327-333` now asserts `ltx_1080p` right after `whisperjav_extra_args` and `[ltx_best, vr_8k, clip_size_1080p]` (the #208 C4 intent — 8K VR after the WhisperJAV block, directly before `clip_size_1080p` — is kept).

The LTX options keep their values while `ltx_1080p` is off (never emitted). An existing config without these keys loads with `ltx_1080p` off and launches byte-for-byte as today.

### AC2 Launch (backend)

`ltx_launch(cfg, tier)` = `cfg.ltx_1080p and tier == "1080p"` — whether THIS launch is an LTX launch. `vr_profile(cfg)` = `cfg.vr_8k and not cfg.ltx_1080p` — the effective 8K VR profile (owner rule: VR is greyed whenever the LTX tickbox is on, so it is off for that task's 4K files too).

- A BasicVSR++ launch (tickbox off, or a 4K-tier file): `build_argv` unchanged except that the 8K VR substitutions follow `vr_profile(cfg)` instead of `cfg.vr_8k` (the existing exact-list argv tests stay untouched).
- An LTX launch: `build_argv` emits the same leading flags from the operator's own fields — `--max-clip-size clip_size_1080p`, `--temporal-overlap`, `--secondary-restoration none`, `--codec`, `--cq`, `--detection-model` — then `--restoration-model-name` (`ltx-undistilled` when `ltx_best`, else `ltx`), `--ltx-fast` when ticked, `--ltx-auto-split N` when N > 0, `--working-directory <output folder>/.jasna-work` when that folder is usable (C2: Jasna's LTX scratch folder lands in TaskPaw's own folder; `_launch_locked` prepares it, AC4b; `build_argv` takes it as a new optional last parameter `work_dir`, default `None` = not emitted), then the operator extra args. Never `--vr-mode`.
- `_launch_locked`: for an LTX launch the 1080p tickbox reads False, so `unet` is False, `_current_unet` is False, the retry-without-unet / degrade path cannot fire and `_run_unet_disabled` is never set for that tier; a failed LTX launch takes the plain retry path. A 4K-tier launch reads `unet4x_4k` exactly as today (with its degrade).
- Capture mode spawns with `stdin=subprocess.DEVNULL` (every launch): there is no console to answer Jasna's download prompt, so it must end with `EOFError` (a failed file with the captured tail) instead of waiting forever on an inherited pipe. Console mode is unchanged (the prompt is answerable in Jasna's window).
- Status / log: `_tier_suffix()` for an LTX launch reads ` [1080p 1920x1080, LTX[ best][ fast][, auto-split N%]]` (no unet part, no `8K VR`), and its `8K VR` part keys on `vr_profile(cfg)` for every launch (a 4K launch of an LTX task never says `8K VR`, C10); `restore.started` `mode` is `ltx` + ` best` + ` fast` + ` auto-split N%` for an LTX launch (`unet-4x` / `plain` otherwise); `profile: 8k-vr` only when `vr_profile(cfg)`.
- Start: the #208 rfdetr-vr-v1 alert fires only when `vr_profile(cfg)`.

The stored `vr_8k`, `unet4x_1080p`, `unet4x_4k`, clip sizes, overlap and detector are never rewritten.

### AC3 Validation (backend)

The extra args go to every launch of the task, BasicVSR++ and LTX alike, and Jasna rejects each backend's options on the other (verified). So:

- Always rejected in `jasna_extra_args` (abbreviation-aware via `_selects`, also `--flag=…`): `--restoration-model-name` and every LTX option — `--ltx-fast`, `--no-ltx-fast`, `--ltx-auto-split`, `--ltx-seed`, `--ltx-large-canvas`, `--no-ltx-large-canvas`, `--ltx-trial`, `--no-ltx-trial` (the `--no-` forms are defaults Jasna accepts without LTX; rejected as no-op forms, N6). Error: "jasna_extra_args must not set LTX options (<flags>): the extra args go to every launch and Jasna rejects LTX options on a BasicVSR++ launch — use the LTX fields (and --ltx-trial would publish unrestored copies)". `--restoration-model-name` joins `_OWNED_FLAGS` (existing "TaskPaw owns" text).
- While `ltx_1080p` is ticked, also rejected (verified as rejected by Jasna, owned by the LTX launch, or a harmless no-op form of such a flag): `--secondary-restoration`, `--denoise`, `--no-compile-basicvsrpp`, `--stream` (Jasna rejects them with LTX; `--secondary-restoration none`, the default, is accepted by Jasna but rejected here as a no-op form of the same flag; `--compile-basicvsrpp`, the accepted default, is a different name and stays accepted, N6), `--vr-mode` (owner rule: no VR under LTX; LTX fails on SBS files, C4), `--restoration-model-path` (one path cannot be both the BasicVSR++ weights file and the LTX folder) and `--working-directory` (TaskPaw owns it for LTX launches, C2); and while `ltx_auto_split > 0`, `--fmp4` and `--segments` (Jasna: "cannot be combined with --ltx-auto-split", C3). Error: "with 1080p LTX ticked, jasna_extra_args must not set <flags>: the extra args go to every launch and LTX launches reject them (the 4K tier's unet-4x is its tickbox)".
- The 8K VR rules (overlap vs `clip_size_1080p`, `_VR8K_OWNED_FLAGS`) stay keyed on the stored `vr_8k`, so the stored values stay valid for clearing the LTX tickbox.
- `vr_8k=True` with `ltx_1080p=True` is valid (the greyed box cannot be unticked, so rejecting it would trap the operator).

### AC4 First-run model download (backend)

Every LTX launch checks `ltx_missing_files(exe_dir, best, fast)` (pure, over the variant table; `<exe dir>/model_weights/ltx-restore`). When files are missing:

- **Capture mode on Windows** (C1, N5): the **first** such launch of a run is spawned like a console-mode launch (`CREATE_NEW_CONSOLE`, no pipes, no reader thread, no `stdin` override), so Jasna's `Download it (…)? [y/N]` prompt appears in its own window and can be answered; that film then runs in its window (no captured %/ETA, no failure tail). Every later launch of the run is captured as usual (`stdin=DEVNULL`): a declined prompt, a still-missing model or a renamed model file (A1) therefore costs at most one window per run, and a further missing-model launch fails with Jasna's EOF error in its captured tail. Off Windows there is no console to open: capture launches always keep `stdin=DEVNULL` and fail fast with that tail. Without the fallback the prompt would get EOF, every 1080p file would fail twice and three in a row would abort the batch.
- **Both modes**: **one** alert per run (dedupe `<instance>:ltx:model`), one text for every case (M1): "LTX model not downloaded: <missing files> are not in Jasna's model_weights\ltx-restore folder (about 9 GB with fast mode, 15 GB without). Jasna asks to download it when an LTX launch has a window: answer y there (with capture progress on, TaskPaw opens that window for the first such launch of a run, on Windows only). Until the model is downloaded, the other 1080p files fail — you can also download it once in Jasna's own app (pick LTX there)." The launch still happens.

Checked per LTX launch, not at Start: a folder of 4K files never needs the model.

### AC4b LTX scratch folder (backend, C2)

`<output folder>/.jasna-work` is TaskPaw's. Before each LTX launch `_launch_locked` runs `mkdir(exist_ok=True)` on it; when that fails (e.g. a file of that name, a permission error) the launch goes ahead **without** `--working-directory` (Jasna's default: next to the output) and one warning is logged (N4) — a scratch-folder problem never stops a restore.

On `start()` of a managed Jasna, the sweep runs only when `.jasna-work` is a real directory: `lstat` shows a directory that is neither a symlink nor a Windows junction / symlink reparse point (the avsubs `_is_link_dir` rule, Python 3.10-safe). A missing `.jasna-work` (the normal case for a task that never ran LTX) is skipped silently; a link or a non-directory is left alone and logged once per Start (N1: a junctioned `.jasna-work` must never make TaskPaw delete in its target; Jasna may still write through it — the operator's choice). In a real directory, every direct entry whose own mtime is older than `_ORPHAN_STAGING_AGE` is removed: a directory with `shutil.rmtree` (which refuses a link entry, and never follows nested junctions — verified by the critic, exp4.py), a file with `unlink`; errors are logged, never raised. No child of this instance is live at Start, and another task must not share the output folder (documented since #187). Nothing outside `.jasna-work` is touched. `plan_queue` scans the input folder only and the done check looks for `<name>-破解.mp4` files, so the folder is invisible to both (like `.avsubs`).

### AC5 Capture-mode LTX progress (backend)

A Jasna-local `parse_jasna_progress_line(line, prev)` recognises `LTX <k>/<n> <stage>: <p>%` lines: `percent` = the stage percent, `fps` from `<x>frame/s` (removed when the line has none — the denoise stage reports s/step, compose may report s/frame, C9), `eta` from `run <…> left` when present (kept from `prev` otherwise), and a new metric `ltx_stage` = `"<k>/<n> <stage>"`; `elapsed` / `processed_frames` / `remaining_frames` are dropped (they would describe a stage, not the film). A line starting `Jasna: ` (0.11.1's installation check, `Jasna: checked 14563/19444 files (74.9%)`, printed on every launch) is not progress and returns `prev` (C9: lada's parser would read it as percent 74 and clear the compiling hint). Any other line goes to lada's `parse_progress_line`; when that yields a new percent, `ltx_stage` is dropped. An unrecognised line returns `prev` itself (the reader detects updates by identity). The detail line shows `LTX <k>/<n> <stage>` before the percent. `ltx_stage` is added to the OpenClaw metric table (docs).

### AC6 The form (frontend)

- `ltx_1080p`, `ltx_fast` render as checkboxes, `ltx_auto_split` as a number, in the AC1 order.
- While `ltx_1080p` is ticked (live form data), the #208 lock shows `vr_8k` and `unet4x_1080p` as greyed, unchecked display controls; the real fields stay mounted and hidden, so the stored values are submitted untouched. `unet4x_4k` and everything else stay editable. A full-row caption hint directly under the tickbox: zh 「已勾选：只有 1080p 档的文件用 LTX，4K 档和其他设置都按原来的配置（4K 档继续用 BasicVSR++ 和它的 unet-4x）。LTX 细节更多但慢很多；它不支持 VR 左右并排视频和 unet-4x，所以「8K VR」和「1080p 档：使用 unet-4x」灰显、不使用，保存的原值不变，取消勾选后恢复。第一次用 LTX 时 Jasna 会弹出窗口询问是否下载模型（快速模式约 9 GB，不开快速约 15 GB），输入 y。「仅大马赛克」在影片没有合适切点时会被 Jasna 跳过，整片都用 LTX；小马赛克走 BasicVSR++ 时也不用 unet-4x。运行中的任务保存后会从头重跑当前影片。」 plus an English equivalent.
- The 8K VR lock and hint apply only while `ltx_1080p` is off; the two locks never apply together.
- One backend source (#208 N4): `ui_schema()["ltx_1080p"]["ui:options"]["taskpawLock"]` = `{"vr_8k": false, "unet4x_1080p": false}`, built from backend constants; absent (older agent) → no LTX lock, never a throw.
- Add-mode review step: with `ltx_1080p` a row 「LTX」/ "LTX" valued 「仅 1080p 档（不用 8K VR 和 1080p 档 unet-4x）」/ "1080p tier only (without 8K VR and the 1080p unet-4x)" after the type row; the 8K VR row and its `stored → profile` arrows only while `ltx_1080p` is off.
- i18n: `schemaI18n.ts` zh titles/descriptions for the new fields; the `vr_8k` and `unet4x_1080p` descriptions gain 「勾选「1080p 档：使用 LTX」时不使用」; `wizard.ltxHint`, `wizard.ltxRow`, `wizard.ltxOn` in zh + en.

### AC7 Docs and version

- README: the Jasna table row mentions 「1080p 档：使用 LTX」; a section 「Jasna LTX（1080p 档）」 after 「Jasna「8K VR」」: what the tickbox does, what is greyed and why, the options and their defaults, the device matrix summary (incl. "4K cannot finish on an 8 GB card"), the first-run download, the rejected extra args, and the NVIDIA "CUDA Sysmem Fallback Policy" hint Jasna prints.
- CHANGELOG `## V3 3.9.14 — Jasna「1080p 档：使用 LTX」（#263）`; `docs/guides/openclaw-integration.md` metric row `ltx_stage`.
- Version 3.9.13 → 3.9.14 in the six files.

### Invariants

- Managed Jasna never auto-starts; `manual_start` unchanged. argv stays a list, no `shell=True`. `build_argv` pure; the stored config is never mutated by a launch.
- With the tickbox off every launch's argv is byte-for-byte today's (C12: the Popen kwargs of capture launches gain `stdin=DEVNULL`); the 4K-tier argv under the tickbox is today's whenever 8K VR is off.
- No secrets, no real film codes (`a.mp4` names), no writes to `C:\Jasna` or the live agent.yaml from code or tests.
- Frontend follows `design-system/taskpaw-v3/MASTER.md`; the #208 lock is reused, not forked (`ObjectFieldTemplate` unchanged).

### Non-goals

- No LTX for the 4K tier (does not finish on 8 GB); no `--ltx-seed` / `--ltx-large-canvas` / trial UI.
- No greying of the LTX options while the tickbox is off (their descriptions say "only with 1080p LTX").
- No GPU detection for `ltx_fast` (Jasna's own error explains it on a non-RTX-50 GPU).
- No TaskPaw-driven model download; no change to Lada, the BasicVSR++ degrade rule, or 8K VR while the tickbox is off.
- No per-tier extra args (C5, deferred): while the tickbox is on, the AC3 conditional flags are rejected for the whole task, so a 4K-tier use of e.g. `--secondary-restoration tvai` or `--denoise` cannot be combined with 1080p LTX (the owner's extra args are empty). `--secondary-restoration none` is rejected too although Jasna accepts it (harmless: the tickboxes decide).
- No special retry rule for LTX failures (C8, deferred): the 1080p LTX failures observed are early (OOM at load ~30 s, EOF at the prompt); the late DiffVAE failure is 4K-only, which this design never launches with LTX.
- The add-mode review step still lists `ltx_auto_split` while the tickbox is off, and the 4K-tier / detector descriptions keep their "8K VR overrides this at launch" clause although 8K VR is off under LTX (C10 minor, deferred).
- Deferred minors of round 2: the sweep's age guard uses each entry's own mtime (NTFS does not touch a directory's mtime when files inside it change — only matters for a shared output folder, unsupported, N2); the sweep runs inline in `start()` (local output folders; bounded by one killed run's windows, N3); auto-split's `Mosaic scan:` pre-pass is parsed by lada's rule (percent only, `ltx_stage` dropped — benign, N8).

## Assumptions (unverified, with risk)

- A1 — The model file names are Jasna 0.11.1's. A later Jasna that renames them makes AC4 believe the model is missing: one spurious alert per run and, in capture mode on Windows, the first LTX film of each run in its own window (uncaptured); the launches themselves work. Fix by updating the table.
- A7 — LTX was measured at 1920x1080 (2.07 MP) only. The 1080p tier reaches 1.5 x 1920x1080 (e.g. 2560x1080, 1920x1440 at ~2.8 MP); whether the DiffVAE decoder fits there on 8 GB is unmeasured (4K at 8.3 MP does not). A failure would take the ordinary failed-file path (C8 deferral).
- A2 — The LTX progress format (English stage names, `run … left`) is the CLI's default-locale output on this machine; a localized stage name still matches (`<stage>` is any text up to the colon), a localized `left` loses only the ETA.
- A3 — `--codec`/`--cq` are accepted with `--ltx-auto-split` (verified at parse time) but the auto-split output "keeps the video's own codec" (Jasna help): a non-HEVC source then yields a non-HEVC `-破解.mp4`; the hvc1 retag returns `no-hevc-entry` (pre-existing, safe). A source codec MP4 cannot hold would fail that file (not verified; Jasna's muxer error lands in the failure tail).
- A4 — Stage percents restart at 0 four times; `ltx_stage` renders as a generic metric tile ("ltx stage", untranslated, `MonitorMetrics.tsx:115`) and in the detail line; the per-film stepper shows only the stage percent (C9). An overall percent is not derivable from Jasna's output.
- A5 — `--ltx-auto-split 12` was never exercised: Jasna skipped the split on the test clip for lack of safe cut points (C6). On a real film with regular keyframes it is expected to split (unmeasured; no film files are used by this run); when it cannot, Jasna restores the whole film with LTX — slower, never wrong.
- A6 — The `run … left` format for runs longer than a day was never observed; `parse_eta` reads H:MM:SS, so a longer form only loses the stepper's ETA (the detail line shows the raw string).

## Approach (why)

A per-tier tickbox (the plugin's existing unet-4x pattern) + the #208 context lock. Alternatives rejected: (a) a task-wide model select — the owner asked for a tickbox noted "1080p only", and LTX cannot finish 4K here; (b) rejecting `vr_8k=True` with LTX at validation — the greyed box cannot be unticked; (c) clearing `vr_8k` / `unet4x_1080p` when LTX is ticked — loses the operator's values (#208: never rewrite stored values); (d) answering Jasna's download prompt from TaskPaw (`y` on stdin) — a silent 9 GB download inside a restore launch with no progress UI.

## Files to change

| Path | Change | Reason |
|---|---|---|
| `taskpaw_v3/monitors/plugins/jasna.py` | edit | fields, `_LTX_*` constants, `ltx_launch` / `vr_profile`, `build_argv`, `_launch_locked` (unet rule, stdin, log mode, AC4 console fallback + alert, `.jasna-work`), Start sweep of `.jasna-work`, validator, `ltx_missing_files`, `parse_jasna_progress_line` + reader + detail, `_tier_suffix`, `ui:order` + `ui:options` |
| `taskpaw_v3/tests/test_jasna.py` | edit | AC1–AC5 tests; the two named pins |
| `taskpaw_v3/ui/src/views/MonitorWizard.tsx` | edit | LTX lock + hint, VR lock only while LTX is off, review rows |
| `taskpaw_v3/ui/src/schemaI18n.ts`, `taskpaw_v3/ui/src/i18n.ts` | edit | zh/en strings |
| `taskpaw_v3/ui/src/test/wizard.test.tsx`, `schemai18n.test.tsx` (+ the i18n test) | edit | AC6 tests |
| `README.md`, `CHANGELOG.md`, `docs/guides/openclaw-integration.md` | edit | AC7 |
| six version files | edit | 3.9.14 |

## Execution surface

- Writes: the files above only. Reads/executes: `uv run pytest` (with a short `--basetemp` whose parent exists, to stay under Windows MAX_PATH), `uv run ruff check . && uv run ruff format --check taskpaw_v3 tests scripts && uv run mypy`, `cd taskpaw_v3/ui && npm run lint && npx tsc -b && npx vitest run`. No live agent config, no `C:\Jasna` writes, no film files.

## Key implementation notes

- `build_argv`'s existing callers are unchanged (only the optional last `work_dir=None` is new); branch on `ltx_launch(cfg, tier)` after the shared leading flags so the BasicVSR++ list is literally the old one.
- `ObjectFieldTemplate` needs no change: `MonitorWizard` passes either the LTX lock or the VR lock as `locked`.
- The LTX lock reads the live `ltx_1080p` from `liveFormData` (never rjsf state); `uiSchema`/Form props stay static (#204 trap).
- The AC4 check runs inside `_launch_locked` (emit available, exe dir known); `capture` becomes a per-launch value — `cfg.jasna_capture_progress and not (missing and sys.platform == "win32" and not self._ltx_console_used)`, with `_ltx_console_used` set when the fallback is taken and reset in `start()` — used for the flags, the Popen kwargs (`stdin=DEVNULL` whenever `capture`) and the reader thread; the failure-tail code keeps reading `_recent_output` (empty for an uncaptured launch). The dedupe key keeps the alert to one per run.

## Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Existing tasks change behaviour after upgrade | low | wrong model / argv | default off; unchanged-argv tests |
| Capture-mode launch hangs on the download prompt | possible (the backend's own stdin is unverified, C12) | task stuck forever | `stdin=DEVNULL` in capture mode; on Windows the first missing-model launch of a run opens its own window (AC4) |
| A killed LTX run leaves GBs of scratch in the library | certain on Stop / save-restart (C2) | disk use in the output folder | `--working-directory .jasna-work` + Start sweep (AC4b) |
| LTX far too slow for whole films on 8 GB | certain (measured) | days per film | `ltx_auto_split` 12 default (may be skipped, A5); README numbers |
| An LTX option in extra args breaks every BasicVSR++ launch | medium | every 4K file fails, batch aborts | AC3 rejection |
| A greyed VR box hides a stored tick | medium | surprise when clearing the tickbox | hint: saved values come back |

## Test plan

- Backend (`test_jasna.py`): `build_argv` for an LTX launch (fast / not / auto-split 0 and 12 / `ltx_best` → `ltx-undistilled` / extra args last / stored `vr_8k` ignored, no `--vr-mode`); the 4K-tier launch of an LTX task = the BasicVSR++ argv (with `vr_8k` stored: no VR substitutions); the tickbox off with LTX options stored = today's argv; instance tests (fake Popen) — a mixed 1080p + 4K queue with the tickbox: the 1080p file is an LTX launch with unet off whose failure takes the plain retry (no degrade alert), the 4K file a BasicVSR++ launch with `unet4x_4k`; capture launches pass `stdin=DEVNULL`, console launches do not; an LTX launch with missing model files in capture mode is spawned as a console launch (no pipes) and the next one with the files present is captured; LTX launches carry `--working-directory <out>/.jasna-work` and the folder exists; Start removes an old `.ltx-x` dir and file inside `.jasna-work` but keeps a fresh one and anything outside it; a `.jasna-work` that is a symlink/junction (or a file) is not swept and an LTX launch with a file named `.jasna-work` runs without `--working-directory`; the missing-model console fallback happens once per run (a second missing-model launch is captured with `stdin=DEVNULL`) and never off Windows (patched `sys.platform`); `restore.started` mode/profile; validator matrix (every LTX flag incl. abbreviations and `=` forms always rejected, the LTX-conditional flags only while ticked, `--fmp4`/`--segments` only while ticked with auto-split > 0, `--denoise-step` still accepted, `vr_8k`+LTX accepted, `ltx_auto_split` bounds); AC4 alert once per run / none when the files exist / none for a 4K-only queue / none with the tickbox off; `ltx_missing_files` per variant; the progress parser on the recorded lines (scan without ETA, encode with `run … left`, denoise without frame/s drops fps, `Jasna: checked …` ignored, a BasicVSR++ line after an LTX line drops `ltx_stage`, an unrecognised line returns `prev` itself); `_tier_suffix` for an LTX launch; `ui_schema` lock equals the constants; field count 24; the order pins.
- Frontend (`wizard.test.tsx`): tick `1080p 档：使用 LTX` → the `vr_8k` and `unet4x_1080p` display controls are disabled and unchecked, `unet4x_4k` stays enabled, the hint shows, a typed name survives (no form reset); clear it → the real fields return with their values; edit mode with `vr_8k: true` + `ltx_1080p: true` saved → the LTX lock (not the VR lock) on open, Save submits `vr_8k: true`, `unet4x_1080p: true` untouched; review step LTX row and no 8K VR row; no LTX lock without the backend option. i18n assertions for the new keys.
- Full suites once on the final commit.
- Manual smoke (owner): tick the LTX box, Start a folder with a 1080p and a 4K film; the Jasna window asks once to download the model; the 1080p film's detail line shows `LTX fast, auto-split 12%` and, in capture mode, the LTX stage and ETA; the 4K film runs as before with unet-4x; clear the tickbox and 8K VR / the 1080p unet-4x apply again.

## Handoff notes

- Read `AGENTS.md`, `docs/constitution.md`, `design-system/taskpaw-v3/MASTER.md` before the UI change. Do not touch `lada.py`.
