# #265 — Jasna「4K 档：使用 LTX」 + LTX settings that suit an RTX 4090 24 GB box; version 3.9.15

Date: 2026-10-16 (design v4, FROZEN — round 4 CLEAN. History: round 1: D2 (P1) resolved, D1/D3–D9 folded in v2 (revalidated in round 2); round 2: E1–E3 (P2) + E4–E6 (minor) folded in v3 (revalidated in round 3); round 3: F1/F2 wording fixed in v4)
Issue: #265, a follow-up to #263 (3.9.14). Owner: 「4K也可以选择使用这个LTX, 按照4090 24G 128G的配置去调配置」; 「afk codex astra单外门。好了版本号增加0.0.1发版」.
Driver: `/afk` — Claude Opus 5.5 drives and implements; single outer gate Codex gpt-6-astra (high). Merge when AFK merge-ready, release v3.9.15 with the Windows EXE.

## Spec review

3.9.14 lets the Jasna task use LTX for the 1080p tier only (`ltx_1080p`), because 4K LTX cannot finish on the owner's RTX 5060 8 GB. The owner has a second box, an RTX 4090 24 GB with 128 GB RAM, where 4K LTX should fit. Two things are needed:

1. A 4K-tier LTX tickbox, the per-tier pattern the plugin already uses for unet-4x.
2. LTX options that are right on BOTH boxes. 3.9.14 defaults `ltx_fast` to on because the 5060 cannot load the standard model. Jasna's docs mark fast mode "RTX 50-series only" and "Not available" on the 4090; what Jasna does when given the flag there is unverified (D5). Either way TaskPaw must not ask for it on the 4090, because the AC4 model-file check and the status text need the effective variant. The 4090 can also afford `--ltx-large-canvas`, which needs about 12 GB of free VRAM.

Core need: one more tickbox, 「4K 档：使用 LTX」. With it ticked, the 4K-tier files launch with LTX as well. Fast mode applies only where the GPU supports it. The README documents the settings recommended for the 4090 box. Nothing changes for a task that ticks neither LTX box.

### Verified facts

- 3.9.14 code (`taskpaw_v3/monitors/plugins/jasna.py`):
  - `ltx_launch(cfg, tier)` is `cfg.ltx_1080p and tier == "1080p"`.
  - `vr_profile(cfg)` is `cfg.vr_8k and not cfg.ltx_1080p`.
  - `build_argv` emits the LTX flags for an LTX launch.
  - `_launch_locked` forces the tier tickbox off for an LTX launch (no unet-4x, no degrade).
  - The validator owns every `--ltx-*` option and, while `ltx_1080p` is ticked, rejects the LTX-incompatible flags (`_LTX_CONDITIONAL_FLAGS`, `_LTX_SPLIT_FLAGS`).
  - `_LTX_LOCK = {"vr_8k": False, "unet4x_1080p": False}` drives the wizard's lock through `ui_schema()["ltx_1080p"]["ui:options"]["taskpawLock"]`.
- Jasna 0.11.1 help, read on the 5060:
  - `--ltx-fast`: "RTX 50-series (Blackwell) only; needs the fast model file".
  - `--ltx-large-canvas`: "restore large mosaics at 768 px instead of 512 px. More detail, about 3x slower on those; needs 12 GB of free VRAM (ignored otherwise)".
  - Official docs: the RTX 4090 runs standard LTX at about 7 min per minute of 1080p with mosaics throughout; fast mode is "Not available" on the 4090.
- On the 5060 (trial, this run), `--ltx-large-canvas` logs `LTX: 4.2 GiB free VRAM, restoring large mosaics at 512 px` and continues. The flag is ignored, not an error. But it is NOT harmless there: in a back-to-back pair on an idle GPU (`jasna-lc2`), the plain fast run finished in 721 s with 20/48 blocks resident, while the same run with `--ltx-large-canvas` logged `6.6 GiB free VRAM, restoring large mosaics at 512 px`, kept only 9/48 blocks resident and then FAILED in compose with `Cannot fit a DiffVAE decode tile under the memory budget` (usable 1.23 GB) — on the 1080p clip. Jasna plans memory for the large canvas even when it falls back to 512 px. Hence AC2: TaskPaw passes the flag only when every GPU is known to have at least 15360 MiB in total (the 16 GB class).
- `nvidia-smi --query-gpu=name,compute_cap,memory.total --format=csv,noheader` on the 5060 gives `NVIDIA GeForce RTX 5060, 12.0, 8151 MiB`. The RTX 4090 is Ada Lovelace with compute capability 8.9; the Blackwell parts are 10.x (data centre) and 12.x (RTX 50). This is NVIDIA's published table, not executed on a 4090.
- `host_metrics.read_gpu()` already calls `nvidia-smi` with a 5 s timeout and `CREATE_NO_WINDOW`, and returns None when unavailable. That is the pattern to reuse.
- 4K LTX on the 5060 failed in the compose stage twice, with 1.28 GB and 1.32 GB usable for the DiffVAE decoder (#263 device matrix), after 638 s and 626 s on a 10 s clip — compose runs after the whole video's denoise, so on a full film that failure comes hours in.
- Jasna's own int8 kernel table keys RTX 50 as compute capability (12,0)/(12,1) (`python_source/jasna/ltx/kernels.py`, read by the critic); data-centre Blackwell is 10.x. For the owner's boxes (12.0 vs 8.9) any threshold between 9 and 12 gives the same result.
- Sources: Jasna docs — models.md (4090 ~7 min per minute of 1080p, fast "Not available"; large canvas "needs about 12 GB free VRAM"), tuning.md ("Above about 180 the time doesn't get better, only VRAM goes up"), segments.md (auto-split = "LTX only for big mosaics", the same feature as the Segment Editor's LTX ranges), models.md again (small mosaics with BasicVSR++ "can look blurry" on 4K; "16 GB cards for Segment Editor ranges" with large canvas) at https://github.com/Kruk2/jasna/tree/main/docs/en.

## Frozen issue contract

### AC1 Fields (backend)

| Field | Type / default | Meaning |
|---|---|---|
| `ltx_4k` | `bool`, default `False`, title 「4K 档：使用 LTX」 / "4K tier: restore with LTX" | The 4K-tier files launch with LTX. The description says it needs a big GPU: 8 GB failed in tests, a 24 GB RTX 4090 fits. |
| `ltx_large_canvas` | `bool`, default `True`, title 「LTX 大马赛克高清」 / "LTX large canvas" | `--ltx-large-canvas`: large mosaics at 768 px, more detail on close-ups and 4K, about 3x slower on those. Passed only when every GPU has at least 16 GB of VRAM (AC2) — on an 8 GB card Jasna's large canvas made even a 1080p run fail (verified), so TaskPaw never passes it there. Only with LTX. Default on: the owner asked for 4090-tuned settings, and on smaller cards it is never passed. |

Wording changes on existing fields:
- `ltx_fast`: "applied only on RTX 50-series GPUs — TaskPaw checks the GPU at Start and launches without it when it finds none (e.g. an RTX 4090)"; the shipped claim "Jasna fails on other GPUs" is replaced (unverified, D5). The "Only used with 1080p LTX" clauses of `ltx_fast` / `ltx_auto_split` / `ltx_best` become "Only used with LTX"; the `jasna_extra_args` description's "while 1080p LTX is ticked" becomes "while either LTX tickbox is ticked"; the validator text "(the 4K tier's unet-4x is its tickbox)" becomes "(unet-4x is set with its tickboxes)"; zh mirrors of all of these (D9).
- `ltx_1080p`: keeps its leading sentence and adds "(the 4K tier has its own LTX tickbox)".
- `unet4x_4k` and `vr_8k` (en + zh): add "not used while 4K LTX is ticked" and "not used while either LTX tickbox is ticked".

`ui:order`: `… whisperjav_extra_args, ltx_1080p, ltx_4k, ltx_fast, ltx_auto_split, ltx_best, ltx_large_canvas, vr_8k, clip_size_1080p …`.

Changed pins (named):
- `test_jasna.py` own-field count 24 → 26.
- The #208/#263 neighbour pin becomes `[ltx_large_canvas, vr_8k, clip_size_1080p]`.
- The #263 block-order assertion now lists the six LTX fields.

### AC2 Launch (backend)

- `uses_ltx(cfg)` is `cfg.ltx_1080p or cfg.ltx_4k`.
- `ltx_launch(cfg, tier)` is `(tier == "1080p" and cfg.ltx_1080p) or (tier == "4k" and cfg.ltx_4k)`.
- `vr_profile(cfg)` is `cfg.vr_8k and not uses_ltx(cfg)`.
- An LTX launch emits:
  - the tier's own clip (`clip_size_4k` on the 4K tier) and `--temporal-overlap`;
  - `--secondary-restoration none`, `--codec`, `--cq`;
  - `--detection-model`, with the 4K `rfdetr-v6-large` auto-upgrade as for any 4K launch;
  - then `--restoration-model-name`, `--ltx-fast` (only when `cfg.ltx_fast` and the GPU supports it), `--ltx-large-canvas` (when ticked), `--ltx-auto-split N`, `--working-directory`, and finally the extra args.
  - `build_argv` gains the optional last parameter `ltx_fast_ok: bool = True`.
- A 4K LTX launch reads the 4K tickbox as False, so there is no unet-4x and no degrade path (a failure follows the no-retry rule below). A 1080p launch while only `ltx_4k` is ticked is today's BasicVSR++ launch.
- **Fast-mode support check.**
  - At `start()` of a managed task with `uses_ltx(cfg)`, `query_gpus()` runs once: `nvidia-smi --query-gpu=compute_cap,memory.total --format=csv,noheader,nounits` with a 5 s timeout and `CREATE_NO_WINDOW`, parsed into `[(compute_cap, mem_total_mb)]`. Any failure, empty output or parse error gives `None`.
  - `_ltx_fast_ok` is False only when the query is known and EVERY reported GPU has compute capability below 10.0 (D3: on a mixed box Jasna may run on the Blackwell card, e.g. via `--device cuda:1`). Unknown (`None`) keeps the operator's choice.
  - `_ltx_canvas_ok` is True only when the query is known and EVERY reported GPU has `memory.total` ≥ 15360 MiB — the 16 GB class (D1, tightened by the `jasna-lc2` failure and E1: Jasna's own overhead was ~1.4 GiB at plan time on the idle 8 GB card, so a 12 GB card would always land in the harmful fall-back-to-512-px case; Jasna's docs name 16 GB cards for its LTX ranges). Unknown or a mixed box → not passed (fail safe). When `cfg.ltx_large_canvas` is ticked but not passed, one `log.info` line says why (E5). The flag and the `large canvas` label follow the effective value.
  - The query uses `Popen` + `communicate(timeout=5)`; on timeout it kills the child and returns None without reading again (D8: `subprocess.run` would `communicate()` without a bound after the kill on Windows). It runs in `start()`, never under `_launch_lock`.
  - When `cfg.ltx_fast` and not `_ltx_fast_ok`, one `log.info` line is written. The detail line and the task-log `mode` show no "fast". The AC4 model check uses the effective variant.
  - In `__init__` and in `start()`: `_ltx_fast_ok = True`, `_ltx_canvas_ok = False` (its safe value — a status built before any query never claims large canvas, E2), the cached query `None`.
- **No plain retry for an LTX launch** (D2, P1): a failed LTX launch is not relaunched; the file counts as failed (never published, tried again at the next Start) and the 3-consecutive-failure abort still applies. The LTX failures observed are repeatable (OOM at load, the DiffVAE budget at compose, EOF at the download prompt — the compose budget depends on free VRAM, which other programs change, so "repeatable", not "deterministic", E4), and a 4K LTX failure can come hours into a film, so an immediate relaunch would likely repeat it; the next Start is the retry. A per-launch flag `_current_ltx` (set in `_launch_locked`, read by the failure path) tells an LTX launch from a BasicVSR++ one. BasicVSR++ launches keep their plain retry. This replaces #263's C8 deferral, whose premise ("4K never launches with LTX") no longer holds.
- **4K LTX on a small card.** At the first 4K LTX launch of a run (D7: tiers are known only at launch), when the query cached at Start is known and EVERY GPU has `mem_total_mb ≤ 8192` (D3), one alert is emitted (dedupe `<instance>:ltx:4k-vram`): "4K LTX: this GPU has <N> MB of VRAM. In tests on an 8 GB card a 4K LTX run fails in its last stage (DiffVAE decode), so each 4K file will likely fail — untick「4K 档：使用 LTX」on this machine." The launch still happens.
- Status and log:
  - The `_tier_suffix` of an LTX launch is ` [<tier> <dims>, LTX[ best][ fast][, large canvas][, auto-split N%]]`.
  - The `restore.started` `mode` is `ltx[ best][ fast][ large-canvas][ auto-split N%]`, using the effective fast and large canvas.
  - The AC4 model alert says "the other LTX files" instead of "the other 1080p files".

### AC3 Validation (backend)

- The LTX-conditional rejections (`_LTX_CONDITIONAL_FLAGS`, and `_LTX_SPLIT_FLAGS` with auto-split) apply while EITHER LTX tickbox is ticked. The error text says "with LTX ticked".
- `--ltx-large-canvas` and `--no-ltx-large-canvas` stay always-rejected; they are now owned by the field.
- Saving `vr_8k=True` together with either LTX tickbox stays valid.

### AC4 The form (frontend)

- The backend's `ui_schema` carries `taskpawLock` for each tickbox: `ltx_1080p → {"vr_8k": false, "unet4x_1080p": false}` and `ltx_4k → {"vr_8k": false, "unet4x_4k": false}`, built from the constants `_LTX_1080P_LOCK` and `_LTX_4K_LOCK`.
- While at least one LTX tickbox is ticked, the wizard locks the union of the ticked boxes' fields, using the #208 lock unchanged. The hint sits after `ltx_4k`, or after `ltx_1080p` when the catalog has no `ltx_4k`.
- There are three hint texts: 1080p only (the 3.9.14 text), 4K only, and both. There are three matching review-row values.
- The 8K VR lock applies only while no LTX tickbox is ticked.
- `schemaI18n.ts` gets the zh title and description for `ltx_4k` and `ltx_large_canvas`, and the clause updates from AC1.

### AC5 Docs and version

- README 「Jasna LTX」 section:
  - the 4K tickbox and what it greys;
  - fast mode is auto-skipped off RTX 50;
  - large canvas;
  - the 8 GB 4K warning;
  - a table **"RTX 4090 24 GB / 128 GB 推荐设置"**: both LTX tickboxes on, fast (auto-skipped), large canvas on, auto-split 12, best optional (about 1.5x slower), BasicVSR++ clip sizes 180 / 180 for the small-mosaic parts (Jasna: larger is steadier and faster up to ~180), plus the time estimate for a 2-hour 4K film labelled as an estimate and including large canvas's "about 3x slower on big mosaics" (D6); the trade of auto-split at 4K (small mosaics then use BasicVSR++ without unet-4x, which Jasna says can look blurry on 4K; auto-split 0 = all-LTX quality choice); that 128 GB RAM is plenty but is no Jasna setting; Jasna's NVIDIA "Prefer No Sysmem Fallback" hint; that a failed LTX launch is not relaunched (the next Start tries the film again) — the README Jasna table row's "retry + degrade" gains "(BasicVSR++ launches)" (E4); that large canvas needs every NVIDIA GPU of the box to have ≥ 16 GB (a second, smaller card disables it, E5);
  - recommended settings for the 5060 box: 1080p LTX only, fast on.
- CHANGELOG `## V3 3.9.15 — Jasna「4K 档：使用 LTX」（#265）`.
- Version 3.9.14 → 3.9.15 in the six files.

### Invariants

- A task with neither LTX tickbox launches exactly as before. A task with only `ltx_1080p` launches as in 3.9.14, except that `--ltx-fast` is dropped when every GPU is known non-Blackwell, `--ltx-large-canvas` is added when ticked (default on) and every GPU is known to have ≥ 16 GB, and a failed LTX launch is no longer retried. On the owner's 5060 a 3.9.14 LTX task therefore keeps its argv on upgrade (known 8 GB → no large canvas).
- `build_argv` stays pure. Stored values are never rewritten. No `shell=True`. nvidia-smi runs only at Start of a managed LTX task, bounded at 5 s, and never under `_launch_lock`.
- Tests never call the real `nvidia-smi` (D4): an autouse fixture in `test_jasna.py` patches `J.query_gpus` to return None, and a tripwire fails any test that runs `nvidia-smi` through `subprocess` from the jasna module.

### Non-goals

- No automatic tickbox defaults per machine. The owner ticks 4K LTX on the 4090 box; the README lists the recommended values.
- R1 from #263 (an unprobeable file falls back to the 1080p tier) stays deferred.
- No per-tier LTX options (fast / best / auto-split / large canvas are task-wide).

## Assumptions

- A1: Compute capability ≥ 10.0 is exactly what Jasna's fast mode accepts. This is not executed on non-Blackwell hardware.
  - Wrong-side outcomes: a non-Blackwell card that Jasna accepts would lose fast mode — on an 8 GB card that is NOT "slower but works": the standard model OOMs at load (#263 matrix), so such a card would fail its LTX launches; on ≥ 16 GB cards it is slower but works. A Blackwell part Jasna rejects would keep the flag and fail with Jasna's own error. On a mixed box where Jasna runs on the non-Blackwell card, `--ltx-fast` is kept and Jasna's behaviour there is unverified (E5). The 5060 box is single-GPU (verified with nvidia-smi); the 4090 box is assumed single-GPU — the owner smoke's `large canvas` check confirms it there. Both sit far from the threshold (12.0 / 8.9).
- A2: 4K LTX fits a 24 GB RTX 4090, with large canvas. Jasna's docs give no VRAM figure for 4K LTX (their 8 GB minimum failed at 4K here) and say large canvas needs about 12 GB free; this rests on the 3x VRAM margin over the failing 8 GB card only (D5). Unverified — the owner's 4090 smoke test decides; if it fails, untick large canvas first, then 4K LTX.
- A3: The 4090 time estimates are extrapolated from Jasna's published figure (and large canvas's 3x on big mosaics) and are not measured.
- A4: #263 R1 still applies and widens: with only `ltx_1080p` ticked and ffprobe failing, a 4K file is launched as 1080p-tier LTX (deferred).

## Test plan

- **Backend**:
  - `ltx_launch` / `uses_ltx` / `vr_profile` truth tables.
  - Exact argv of a 4K LTX launch (`clip_size_4k`, the large-detector upgrade, `--ltx-large-canvas`, no `--ltx-fast` when `ltx_fast_ok` is False).
  - A 1080p launch with only `ltx_4k` is the BasicVSR++ argv.
  - Instance mixed queue with both tickboxes: both LTX, unet off on both, no degrade.
  - `query_gpus` parsing: a fake `subprocess.Popen` gives Blackwell, Ada, mixed (fast kept), error, timeout (killed, None) and garbage outputs.
  - Large canvas effective rule: 8 GB known → no flag/label; 12 GB (12282 MiB) → no flag; 16 GB (16376 MiB) and 24 GB → flag; mixed 8 + 24 GB → no flag; unknown → no flag.
  - A failed LTX launch is not relaunched (1080p and 4K); a failed BasicVSR++ launch still gets its plain retry.
  - The autouse no-nvidia-smi fixture and its tripwire.
  - Start with an Ada GPU: no `--ltx-fast`, the model check uses the non-fast file, the mode says no fast.
  - The 8 GB 4K alert fires once at the first 4K LTX launch; none for a 1080p-only queue, on 24 GB, on a mixed 8 + 24 GB box, or when unknown.
  - Validator conditionals under `ltx_4k` alone.
  - `_tier_suffix` / mode strings, `ui_schema` locks, field count and order pins.
  - Existing #263 test changes, all named (E3): `test_mixed_queue_ltx_on_1080p_basicvsrpp_on_4k` (inputs `[a, a, b]` → `[a, b]`, the failed LTX file now counts `_failed == 1`, two modes); the `_missing_model_run` tests (`a, a's retry, b` → the declined LTX launch is not retried: `a, b, c` with three files); `test_ltx_conditional_flags` / `test_fmp4_and_segments_only_clash_with_auto_split` (match "with LTX ticked"); `test_ui_schema_carries_the_ltx_lock_from_the_constants` (`_LTX_LOCK` → `_LTX_1080P_LOCK`); `test_ltx_defaults_and_schema` (block order); `schemai18n.test.tsx` (the vr_8k clause now names either LTX tickbox). With `query_gpus` patched to None, large canvas is never passed, so no #263 argv/mode/detail expectation changes for it. The parsing tests keep a handle to the real `query_gpus` taken before the autouse patch.
- **Frontend**:
  - 4K LTX alone locks `vr_8k` + `unet4x_4k` and keeps `unet4x_1080p` editable.
  - Both tickboxes lock all three.
  - The hint and review texts per combination.
  - Edit mode with both saved.
  - An older catalog without `ltx_4k` still works.
- Full suites on the final commit. **Owner smoke on the 4090**: first Jasna's own 10 s 4K sample `C:\Jasna\assets\test_clip1_2160p.mp4` in a test task (if A2 fails it costs minutes, not hours, E4); then tick both LTX boxes, Start a 4K film, see `LTX, large canvas, auto-split 12%` without `fast`, and that it finishes.

## Key implementation notes

- `query_gpus`: `Popen(..., stdout=PIPE, stderr=DEVNULL, stdin=DEVNULL, creationflags=CREATE_NO_WINDOW)` then `communicate(timeout=5)`; on `TimeoutExpired` call `kill()` and return None WITHOUT another `communicate`/`wait` beyond `wait(1)` — and never as a `with Popen(...)` block, whose exit waits without a bound (E6: 20.05 s vs 1.01 s against a 20 s child).
- The autouse fixture saves the real `J.query_gpus` for the parsing tests before patching it.
