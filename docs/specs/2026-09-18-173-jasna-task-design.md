# #173 — Jasna task type (replaces Lada): per-file best-quality strategy, per-resolution unet-4x, version 3.2.0

Date: 2026-09-18 (design v5 after adversarial debate rounds 1–4)
Issue: #173
Driver: `/afk` (Claude Fable leads, Opus subagents implement → internal review → Codex 外门 (astra) → Kimi 终审).
Merge policy: leave-open (owner merges).

## Spec review

The owner wants Jasna (https://github.com/Kruk2/jasna, v0.10.0, installed at
`C:\Jasna\jasna.exe`) to replace Lada as the managed video-restore workload. The
new `jasna` task type keeps the **layout** of the `lada` plugin (managed folder-in →
folder-out, operator-clicked Start, queue counts, GPU metrics, optional captured
progress) but is designed around Jasna's CLI and the best-quality strategy worked
out on 2026-09-18:

- one `jasna.exe` process **per video** (resume / skip-done / retry / per-file
  settings), not one folder-mode batch;
- a **resolution tier** per file ("1080p" vs "4K") selecting clip size and whether
  the supporter-only `unet-4x` secondary upscaler is used — two tickboxes,
  **1080p ticked by default, 4K unticked by default** (8 GB VRAM owner GPU);
- V3 app version **3.1.0 → 3.2.0**.

## Acceptance criteria

1. `jasna` plugin (`type_id="jasna"`, category `task`, display "Jasna (video
   restore)") registered in `default_registry`; appears in the wizard catalog with
   path pickers; zh+en field labels; a ServiceIcon glyph; About blurb mentions Jasna.
2. Config exposes `unet4x_1080p` (default **true**) and `unet4x_4k` (default
   **false**) booleans, tier clip sizes (`clip_size_1080p=90`, `clip_size_4k=60`),
   `temporal_overlap=8`, `codec=hevc`, `cq=24`, `detection_model=rfdetr-v6`, plus the
   lada-shaped fields (`jasna_exe_path`, `process_name`, `jasna_input_folder`,
   `jasna_output_folder`, `jasna_extra_args`, `jasna_gpu_monitor`,
   `jasna_capture_progress=false`). Managed mode requires input+output folders;
   cross-field validation mirrors Jasna's own (`2*temporal_overlap < clip size`).
3. Managed mode processes the input folder **one file per process**, sequentially,
   skipping files whose **final** output `<stem>_restored.mp4` already exists,
   choosing the tier from the file's resolution (ffprobe; pixel-count rule below),
   building an exact argv (pure function, tested). Jasna writes to a staging name
   and the plugin renames to the final name only on exit 0, so a killed/crashed
   run never leaves a "done" marker.
4. Retry/degrade per file (works with capture **off**, i.e. without reading any
   output): a failure of a launch that used unet-4x is retried **without** unet-4x;
   if that succeeds, one alert is emitted ("worked without unet-4x: supporter key
   not activated in Jasna's GUI, or not enough VRAM") and unet-4x stays off **for
   that tier** for the rest of this run (a 4K OOM must not strip unet-4x from later
   1080p files); a failure of a plain launch is retried once, then alerted and
   the file is skipped. Three consecutive failed files abort the batch with one alert.
5. Status parity with lada: `queue_completed/total/remaining` (+ `queue_failed`),
   `current_file`, capture-mode `percent/elapsed/processed_frames/eta/remaining_frames/fps`,
   cpu/mem/gpu metrics; `status.md` renders a `jasna` snapshot exactly like `lada`;
   openclaw guide lists the fields for both. `detail` shows the tier and measured
   `WxH` of the current file.
6. Events only: launch error (alert, deduped), per-file failure after retries
   (alert), unet-4x degraded (alert once per tier per run), batch aborted (alert once), batch
   complete (done, with `Queue: X/Y done`). Passive mode: running→gone transition
   emits done. An empty/drained input folder → `idle` with detail, **no** event.
7. Owner rules: managed Jasna never auto-starts at boot (`manual_start()` true when
   `jasna_exe_path` set); `jasna_capture_progress` default false; no `shell=True`;
   child terminated in `stop()` (launch, exit handling and stop share one
   `_launch_lock`, so `stop()` never races a launch or a rename; if the lock cannot
   be taken within the caller's timeout, `stop()` only terminates the child and
   leaves the staging file to the exit branch); `start()` never raises. After a 3-strike abort the state is `degraded` (metrics still render,
   visibly distinct from a paused batch).
8. Version 3.2.0 in all six places (`taskpaw_v3/__init__.py`, `tauri.conf.json`,
   `Cargo.toml`, `Cargo.lock`, `ui/package.json`, `ui/package-lock.json`) with a
   test asserting they agree.
9. Tests + docs per the test plan; `uv run pytest`, ruff, mypy, UI lint/tests green.

## Frozen issue contract

**In scope:** items 1–9 above. Allowed user-visible changes: a new monitor type in
the wizard; new metric rows in Hub/status.md for jasna monitors; version label
3.2.0; About text.

**Invariants:** constitution §2 (no `shell=True`; no secrets in argv/logs/config;
atomic publish of the output file via staging name + `os.replace`), §4 (no silent
except, clean shutdown, no orphans), §6 (reviewer ≠ implementer); owner rules
(manual start, capture default off); the agent→hub wire shape is unchanged
(metrics is an open dict).

**Frozen contract corrections (repository / upstream evidence):**

| # | Issue text | Evidence | Correction |
|---|-----------|----------|------------|
| C1 | optional `license_email`/`license_key` secret fields passed as CLI flags | constitution §2 "No secrets in argv or logs"; Jasna's CLI accepts the license only via argv (`jasna/main.py:834-836` is the sole `set_license` caller; `license_store` itself is a closed submodule, not inspected), so §2's preferred env path is not available through the CLI | **Dropped.** The operator activates once in Jasna's GUI (header supporter chip); Jasna persists the key under `%APPDATA%\jasna`. The plugin detects the unlicensed case by outcome (AC 4). **Smoke prerequisite:** activate first. |
| C2 | "five version files" | `src-tauri/Cargo.lock:2956-2957` pins `taskpaw 3.1.0` | **six** files |
| C3 | unprefixed field names (`input_folder`, `extra_args`, …) | lada uses `lada_*` prefixes; `schemaI18n` keys per type_id; config edit forms | `jasna_*` prefixed names (parity) |
| C4 | "ffprobe if found … else a minimal MP4/MKV header reader" | Jasna itself hard-requires `ffprobe` (`jasna/os_utils.py:check_required_executables`, called before any work in `main.py`) — without it every launch exits 1 | **No header reader.** ffprobe with Jasna's own lookup order; not found → 1080p tier + detail note + one alert (Jasna will fail anyway). |
| C5 | "4K tier: height > 1080 or width > 1920" | 1920×1200 / 2560×1080 sources would lose unet-4x silently | **Pixel-count rule:** `w*h > 1920*1080*1.5` (≈3.1 MP; 2560×1440 and up) → "4K" tier |

**Non-goals:** changing/removing `lada`; migrating lada configs; Jasna SD 1.5
image mode, streaming, segments, VR; dedicated TVAI/RTX-SR fields (an operator may
still put `--secondary-restoration tvai|rtx-super-res` in `jasna_extra_args`: it is
appended last, so argparse last-wins makes it override the tickboxes — documented in
the field description); a license-entry UI; recursive folder scans; rescanning the
input folder while running (lada parity); fixing the existing `services.lada` i18n
blurb.

**Causal boundary:** `taskpaw_v3/monitors/plugins/jasna.py` (new),
`registry.py` (+1 line), `hub/server/status_md.py` (lada block also matches
`jasna`), UI i18n/icon/About, docs, version files, tests.

## Assumptions (unverified claims are listed as such)

| # | Claim | Basis | Risk if wrong |
|---|-------|-------|---------------|
| A1 | `jasna.exe --input FILE --output FILE ...` processes one video and exits 0 on success, non-zero on failure. | Source `jasna/main.py` (single-file path `sys.exit(1)`; uncaught exception → exit 1); installed 0.10.0 `--help` verified 2026-09-18. Not run end-to-end here. | Retry logic misfires; owner smoke. |
| A2 | Progress lines `Processing video:  NN%|…|Processed: M:SS (Nf) | Remaining: M:SS (Nf) | Speed: N.Nfps` via tqdm on **stderr** with `\r`. | Source `jasna/progressbar.py`; lada's regexes verified to match by the critic. Reader uses `stderr=STDOUT`. | Capture mode only. |
| A3 | Unlicensed unet-4x → `RuntimeError("unet-4x is a supporter feature. Enter your license to enable it.")` → non-zero exit. | Source `jasna/engine_compiler.py:145-147`. | Degrade is outcome-based (AC 4), so a wrong message only weakens the alert text. |
| A4 | HEVC in `.mp4` is a valid Jasna output. | GUI default `{original}_restored.mp4` + codec hevc (`gui/models.py:168,184`). | Per-file failure in smoke. |
| A5 | First run compiles TensorRT engines (15–60 min) into `model_weights/*.engine`. | README, release notes ("copy .engine files"). | Only the "compiling" detail text. |
| A6 | `ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 FILE` prints `W,H`. | **Verified** 2026-09-18 with ffprobe 8.0.1 on synthetic mp4/mkv/webm/mov (1080p, 4K hvc1, 1088, 720). | Falls back to 1080p tier. |
| A7 | 8 GB defaults (clip 90 / 60, overlap 8) are safe on an RTX 5060. | Extrapolated from the author's 5090 tuning table; not measured. | Editable; Jasna spills to RAM. |
| A8 | Per-process startup overhead (preflights, torch import, TensorRT engine deserialisation) is small relative to the owner's typical files (30 min – 3 h videos). | Not measured. `main.py` re-runs preflights + `build_restoration_session` per process. | A folder of many short clips pays the overhead N times. Owner smoke measures two short clips; if overhead is large (>60 s), a follow-up may add a "short files → folder mode" path. Per-file mode is still required for skip/resume/tiering. |

## Approach

**Why per-file, not folder mode.** Jasna's folder mode never skips existing
outputs, aborts the whole batch on any non-colourspace exception, and cannot vary
flags per file. Driving one process per video gives resume/skip/retry and per-tier
flags; the cost is A8. Alternative considered: folder mode +
`--post-export-video-command` — rejected because it cannot skip or re-tier.

**Why tier by probing with ffprobe only.** C4/C5. ffprobe is a hard prerequisite
of Jasna, present on the owner's machine, and its CSV output is verified.

**Why outcome-based degrade.** C1 + capture-off default: the plugin cannot read
Jasna's output in the default configuration, so it must not depend on it. "Retry
without unet-4x" is the retry policy; success of the plain retry is the signal.

**Reuse from `lada.py`:** `process_alive`, `_cpu_mem`, `parse_progress_line`
(the `%`/Processed/Remaining/Speed regexes are identical; no folder header exists
in single-file mode). Reuse, don't duplicate.

### Module layout (`taskpaw_v3/monitors/plugins/jasna.py`)

Pure helpers (unit-tested without a GPU):

- `JASNA_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm"}`.
- `output_path_for(output_folder, video) -> Path` → `<output_folder>/<stem>_restored.mp4`;
  `staging_path_for(...)` → `<output_folder>/<stem>_restored.tmp.mp4` (keeps the
  `.mp4` suffix so Jasna picks the mp4 muxer).
- `tier_for(width, height) -> Literal["1080p", "4k"]`: `"4k"` iff `width * height > 1920 * 1080 * 1.5`.
- `find_ffprobe(exe_dir) -> Optional[str]`: `<exe_dir>/tools/ffprobe(.exe)`,
  then `shutil.which("ffprobe")`, then `<exe_dir>/ffprobe(.exe)` (a complete mirror
  of Jasna's `find_executable` order for ffprobe; its "common locations" table only
  lists `nvidia-smi`).
- `probe_resolution(video, ffprobe) -> Optional[tuple[int, int]]`: A6 command,
  `timeout=5` (inside the supervisor's 5 s stop / 10 s reconfigure budgets; a local
  ffprobe answers in well under 1 s), `CREATE_NO_WINDOW`; first line `W,H` ints > 0;
  any failure → `None`.
- `plan_queue(input_folder, output_folder) -> tuple[list[Path], int, list[tuple[Path, Path]]]`:
  sorted non-recursive scan of `JASNA_VIDEO_EXTENSIONS` (no name-based exclusion:
  the validator guarantees input ≠ output folder, so the plugin's own outputs can
  never be scanned, and a source library may legitimately contain `*_restored.mp4`);
  `done` = files whose final output exists; `pending`; and `collisions` = later files whose
  output key (casefolded final path) collides with an earlier file's (excluded from
  `pending`, reported once as an alert). `_total = done + len(pending) + len(collisions)`
  and `_failed` is seeded with `len(collisions)`.
- `build_argv(cfg, exe, video, staging_out, tier, unet_enabled, large_detector_available) -> list[str]`:
  ```
  [exe, "--input", video, "--output", staging_out,
   "--max-clip-size", str(clip_for_tier), "--temporal-overlap", str(cfg.temporal_overlap),
   "--secondary-restoration", "unet-4x" if unet_enabled else "none",
   "--codec", cfg.codec, "--cq", str(cfg.cq),
   "--detection-model", "rfdetr-v6-large" if (tier == "4k" and large_detector_available and cfg.detection_model == "rfdetr-v6") else cfg.detection_model,
   *shlex.split(cfg.jasna_extra_args)]
  ```
- `large_detector_available(exe_dir)`: `exe_dir/model_weights/rfdetr-v6-large.onnx` exists.
- `engines_present(exe_dir)`: any `exe_dir/model_weights/*.engine`.
- `is_license_failure(tail) -> bool`: case-insensitive `"supporter feature"` only —
  used **only** to sharpen the degrade alert text in capture mode.

`JasnaConfig(BaseMonitorConfig)` — fields as in AC 2 with descriptions. Validators:
managed (exe set) needs both folders, and they must differ (resolved, casefolded);
`jasna_extra_args` must not contain any flag the plugin owns (`--input --output
--output-pattern --max-clip-size --temporal-overlap --codec --cq --detection-model`,
matched as exact tokens or `--flag=`; `--secondary-restoration` is deliberately
allowed as the documented last-wins override); `cq` 0..63; clip sizes ≥ 8; overlap ≥ 0;
`2 * temporal_overlap < min(clip_size_1080p, clip_size_4k)` (Jasna's own rule);
`codec` is a `Literal`.

`JasnaInstance(MonitorInstance)`:

- state: `_pending: list[Path]`, `_done: int`, `_failed: int`, `_total: int`,
  `_current: Optional[Path]`, `_current_tier`, `_current_dims`, `_current_unet: bool`,
  `_unet_retry_pending: bool` (the next launch of `_current` must run without unet),
  `_plain_retry_used: bool` (per file), `_run_unet_disabled: dict[str, bool]`
  (per tier), `_last_failure_tail: str` (snapshot of the tail at the last rc≠0),
  `_consecutive_failures: int`, `_process`, `_reader`, `_progress`, `_recent_output`
  (deque 20, **cleared per launch**), `_launch_error`, `_batch_done_emitted`,
  `_batch_aborted`, `_compiling`, `_prev_running` (passive), `_stopping` (Event),
  `_ffprobe: Optional[str]`, and **two locks**: `_launch_lock` (`threading.RLock`
  — re-entrant because the exit branch calls `_launch_next` while holding it;
  guards launch, exit handling and stop) and lada's `_lock` (`threading.Lock`;
  guards `_progress` / `_recent_output`, taken by the reader thread per line). Lock
  order is `_launch_lock` → `_lock` only; nothing holding `_lock` ever takes
  `_launch_lock`; `stop()` joins the reader without holding either lock; no
  blocking I/O runs while `_launch_lock` is held except the non-blocking `Popen`
  itself, the `os.replace`, and `stop()`'s own terminate/kill wait, which is
  bounded by the caller's `timeout` (+2 s for the kill reap). All terminate/kill
  paths go through one `_terminate_child(proc, timeout)` helper carrying lada's
  `OSError` guard (the child may already be gone), used by `stop()`, its no-lock
  fallback, and `_launch_next`'s post-`Popen` re-check.
- `start(emit)`: idempotent reset (lada #59 recipe: `stop()` a prior child, reset
  every per-run field); managed → preflight exe (dir / missing → `_launch_error` +
  alert, dedupe `f"{iid}:launch"`); `_ffprobe = find_ffprobe(exe_dir)` (None →
  alert once "ffprobe not found; all files use the 1080p tier"); `plan_queue`
  (collisions → one alert); sweep `*_restored.tmp.mp4` files in the output folder
  whose source is not in the scan (orphans of an earlier hard stop; best effort);
  no pending → `idle`, detail `"nothing to process (N already restored)"`, no
  event; else `_launch_next(emit)`.
- `_launch_next(emit)`: peek the next pending file and **probe it before taking
  the lock** (`probe_resolution` may block up to 5 s and touches no shared state) —
  skipped when the peeked path equals `_current` and `_current_tier` is already set
  (a relaunch of the same file reuses the probe); then under `_launch_lock`: `if self._stopping.is_set(): return`; pop it; if it
  differs from `_current` reset `_plain_retry_used` and `_unet_retry_pending`;
  remove a stale staging file for it; tier from the probe (`None` → 1080p,
  `_current_dims=None`); `unet = tier tickbox and not _run_unet_disabled[tier] and
  not _unet_retry_pending`; `_current_unet = unet`; argv; `Popen` (capture →
  `PIPE`, `stderr=STDOUT`, byte reader; else `CREATE_NEW_CONSOLE` on Windows);
  `FileNotFoundError`/`PermissionError`/other → `_launch_error` + alert (never
  raise). `_compiling = not engines_present(exe_dir)`. Clear `_progress`,
  `_recent_output`. Re-check `_stopping` after `Popen` and terminate if set.
- `check(emit)`: managed:
  - `_launch_error` → `error`.
  - no process and batch finished → `idle`; aborted → `degraded` (short-circuit; counters frozen).
  - process alive → `running` (progress; `detail` = `"running: <file> [4K 3840x2160, unet-4x] · 47% · ETA …"`;
    while `_compiling` and no `percent`: prefix `"compiling TensorRT engines (first run, 15–60 min): "`;
    `_compiling` clears when `engines_present()` or a progress line arrives).
  - process exited — the whole branch runs under `_launch_lock` (re-entrant, so
    the trailing `_launch_next` is safe) and is handled **once** (`_process = None`
    at its end):
    - rc 0 → `os.replace(staging, final)` (failure to rename → treat as file failure);
      `_done += 1`; `_consecutive_failures = 0`; if `_unet_retry_pending` (this was
      the plain relaunch after a unet failure) → emit the degrade alert once per
      tier (dedupe `f"{iid}:unet:{tier}"`; text says "supporter key not activated
      in Jasna's GUI" when `is_license_failure(_last_failure_tail)`, else "supporter
      key not activated or not enough VRAM") and set `_run_unet_disabled[tier] = True`;
      clear `_unet_retry_pending`.
    - rc ≠ 0 → snapshot `_last_failure_tail` from `_recent_output` (under `_lock`;
      **only on this branch**, so the failing launch's tail survives the successful
      relaunch); delete staging (best effort); if `_current_unet` → set
      `_unet_retry_pending = True` and re-queue the same file at the front (the
      relaunch runs without unet; `_failed`/`_consecutive_failures` untouched).
      Else (a plain launch failed): **leave `_unet_retry_pending` as is** (it means
      "this file runs without unet from now on"; it is reset only when `_current`
      changes or after the rc 0 degrade handling); if not `_plain_retry_used` → set
      it, re-queue at the front; else `_failed += 1`, `_consecutive_failures += 1`,
      emit alert `f"{name}: {file} failed"` with the bounded `_last_failure_tail`
      (capture) or `"exit code N"`. Worst case per always-failing file: unet →
      plain → plain = **3 launches**, 9 before the 3-strike abort.
    - `_consecutive_failures >= 3` → `_batch_aborted = True`, one alert
      `"batch aborted after 3 consecutive failures"`, state `degraded` from then on.
    - pending left → `_launch_next`; else emit `done` once (`"Jasna processing
      complete | Queue: X/Y done, F failed | <timestamp>"`) → `idle`.
- `stop(timeout)`: set `_stopping`; `_launch_lock.acquire(timeout=max(0.1, timeout))`
  (mirrors `supervisor.stop()`'s timed acquire); if acquired: if the child is still
  alive, `_terminate_child` it and only then delete the current staging file (best
  effort) — an already-exited child is left to the exit branch, so a completed
  rename is never undone; release. If not acquired within the budget: log it and
  `_terminate_child` without the lock (the orphan guarantee wins over tidiness; the
  staging file is left for `start()`'s sweep). Then join the reader (no lock held).
- `_build_status(state)`: metrics per AC 5; `queue_remaining = total - done - failed`;
  `current_file` only while running.
- passive: `process_alive(cfg.process_name)`; running→gone emits done.

`JasnaPlugin(MonitorPlugin)`: `type_id="jasna"`, `display_name="Jasna (video restore)"`,
`category="task"`, `config_version=1`, `ui_schema` order: name, jasna_exe_path,
jasna_input_folder, jasna_output_folder, unet4x_1080p, unet4x_4k, clip_size_1080p,
clip_size_4k, temporal_overlap, codec, cq, detection_model, process_name,
jasna_extra_args, jasna_gpu_monitor, jasna_capture_progress, poll_interval, timeout, `*`;
path pickers via `ui:options.taskpawPath`. `manual_start()` true when exe set.

### Hub / UI / docs

- `status_md.py`: `is_lada = tid in {"lada", "jasna"} or (tid is None and …)`.
- `schemaI18n.ts`: `jasna` block (zh titles/descriptions for every field, incl.
  "1080p 档：使用 unet-4x 二次修复（默认开）" / "4K 档：使用 unet-4x 二次修复（默认关，8 GB 显存放不下）",
  the tier rule, the console-window-per-file note, and that the "compiling" hint
  relies on `model_weights/*.engine`).
- `i18n.ts`: `services.jasna` en/zh (accurate: "Jasna video restore — per-file
  queue, unet-4x by resolution, GPU"); About blurb mentions Jasna in both languages.
- `ServiceIcon.tsx`: `jasna` glyph.
- `docs/guides/openclaw-integration.md`: Monitor column `lada / jasna`; `queue_failed`.
- `CHANGELOG.md`: new top section `## V3 3.2.0 — Jasna 任务类型 (#173)`.
- `README.md` plugin table row.
- Version bump in six files + `taskpaw_v3/tests/test_version.py`.

## Files to change

| Path | Change | Reason |
|------|--------|--------|
| `taskpaw_v3/monitors/plugins/jasna.py` | new | plugin |
| `taskpaw_v3/monitors/registry.py` | edit | register |
| `taskpaw_v3/hub/server/status_md.py` | edit | render jasna like lada |
| `taskpaw_v3/__init__.py`, `src-tauri/tauri.conf.json`, `src-tauri/Cargo.toml`, `src-tauri/Cargo.lock`, `ui/package.json`, `ui/package-lock.json` | edit | 3.2.0 |
| `taskpaw_v3/ui/src/schemaI18n.ts`, `i18n.ts`, `components/ServiceIcon.tsx` | edit | UI |
| `taskpaw_v3/tests/test_jasna.py`, `test_version.py` | new | tests |
| `taskpaw_v3/tests/test_status_md.py`, `test_catalog.py` | edit | jasna rows |
| `taskpaw_v3/ui/src/test/wizard.test.tsx` | edit | tickbox defaults |
| `docs/guides/openclaw-integration.md`, `CHANGELOG.md`, `README.md` | edit | docs |
| `docs/specs/2026-09-18-173-jasna-task-design.md` | new | this doc |

## Execution surface

Writes: the files above only. Reads/executes: `uv run pytest`, `uv run ruff check .`,
`uv run ruff format --check .`, `uv run mypy`, `cd taskpaw_v3/ui && npm run lint && npx vitest run`.
`jasna.exe`/`ffprobe` are never executed by tests (`Popen`/`run` mocked).
`C:\Jasna\env.txt` is never read by code or tests.

## Key implementation notes

- Byte-at-a-time reader and `stderr=STDOUT` exactly as lada (tqdm `\r`).
- `check()` may launch the next process (non-blocking `Popen`) but only under
  `_launch_lock` and only while not `_stopping`; never block/sleep; the resolution
  probe runs before the lock is taken.
- Re-queue at the front keeps ordering deterministic.
- The per-file failure alert includes the bounded output tail (lada's 10 lines /
  800 chars) in capture mode, otherwise the exit code — never argv.
- `CREATE_NEW_CONSOLE` per file opens a console window per video when capture is
  off; documented in the field description.
- `detection_model` auto-upgrade to `rfdetr-v6-large` only for the 4K tier, only
  when the operator left the default, only when the file exists.
- A systemic failure (driver/GPU/ffprobe preflight) costs up to 3 launches per
  file (unet → plain → plain) before the 3-strike abort (9 launches, 3 alerts) —
  bounded; a fast-failure heuristic to skip the unet retry is deferred (N-11).
- `jasna_extra_args` description must say: "`--secondary-restoration` here
  overrides the tickboxes for every file **and disables the automatic unet-4x
  degrade** (the relaunch would carry the same flag)."

- Jasna's `--cq` + `--encoder-settings cq=` conflict is Jasna's own error; the
  `jasna_extra_args` description says so.

## Risk assessment

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| Transient/VRAM failure on a unet launch misread as "unet unsupported" | medium (8 GB, A7) | rest of run **for that tier** without unet, one honest alert | per-tier flag; alert names both causes; operator restarts |
| A8 per-file overhead large on short clips | medium | slower batches of short clips | measured in smoke; follow-up if needed |
| 8 GB defaults too high for some 4K sources | medium | slow (RAM spill), not a crash | editable; unet off for 4K by default |
| Console window per file annoys | low | cosmetic | capture mode option |
| Six-file version drift | low | wrong About/ping | `test_version.py` |

## Out of scope

See non-goals. No automatic Lada→Jasna migration, no Hub schema changes.

## Test plan

`taskpaw_v3/tests/test_jasna.py` (mocked `Popen`/`subprocess.run`, `tmp_path`):

- config: managed needs folders (and distinct ones); passive needs none; owned
  flags in extra args rejected (exact token and `=` forms; `--input-size`-style
  lookalikes and `--secondary-restoration tvai` accepted);
  `2*overlap >= clip` rejected; defaults (`unet4x_1080p` True, `unet4x_4k` False,
  clip 90/60, overlap 8, hevc, cq 24, capture False); json_schema defaults exposed.
- `tier_for`: 1920×1080, 1920×1200, 2560×1080 → 1080p; 2560×1440, 3840×2160 → 4k.
- `build_argv`: exact argv for 1080p±unet, 4k±unet, large detector present/absent,
  explicit detection model respected, extra args appended, staging output path.
- `find_ffprobe` order; `probe_resolution`: mocked run → `(1920,1080)`; nonzero /
  timeout / garbage → `None`.
- `plan_queue`: skip existing final outputs (staging files don't count); sorted;
  a source named `foo_restored.mp4` is still queued; collisions (`a.mp4`+`a.mkv`,
  `A.mp4`+`a.MP4`) excluded, reported, counted in `queue_total` and `queue_failed`.
- lifecycle with FakePopen (rc scripted per launch):
  - file 1 rc 0 → staging renamed to final, file 2 launched;
  - unet launch fails, plain relaunch succeeds → exactly one degrade alert, later
    files **of that tier** launched with `--secondary-restoration none` while the
    other tier keeps unet (default config, capture off); a unet launch that fails
    is relaunched at most once with unet before the plain retry budget applies
    (no unbounded relaunch loop: assert the launch count);
  - plain launch fails once → retried once **with `--secondary-restoration none`
    (assert the retry argv)**; fails again → one failure alert, skipped, exactly 3
    launches for that file; next file's retry budget is fresh (unet on again);
  - three consecutive failed files → one abort alert, `degraded`, no further launches;
  - batch end emits exactly one `done`; **repeated `check()` after batch end keeps
    `queue_completed` stable** and launches nothing;
  - `stop()` while a process is alive terminates it and removes the staging file;
    `stop()` after the child already exited (rc 0 not yet handled) leaves the
    staging file for the exit branch to rename (no spurious failure);
  - launch/stop race, two tests: (a) `_stopping` set from inside FakePopen's
    constructor → the post-`Popen` re-check terminates the child; (b) `stop()`
    called from a second thread, synchronised by an `Event` set in the constructor,
    leaves no child running and does not deadlock (join with timeout);
  - a file completing (rc 0) followed by the next launch inside the same `check()`
    does not deadlock (re-entrant lock) — the sequential lifecycle test covers it;
  - a relaunch of the same file does not probe again (one `subprocess.run` call per
    distinct file); `start()` sweeps an orphaned `*_restored.tmp.mp4` whose source
    is gone and leaves one whose source is pending;
  - `_terminate_child` on an already-reaped child does not raise;
  - `stop(timeout=0.2)` while another thread holds `_launch_lock` returns within
    the budget and still terminates the child;
  - staging file left by a killed run is not counted as done on the next `start()`;
  - restart resets state; launch errors never raise; empty folder → idle, no event.
- capture parsing: `%` line updates `percent/fps/eta`; tail cleared between files;
  degrade alert text mentions the supporter key when the **failing** launch's tail
  contained `supporter feature` (snapshot survives the relaunch's clear); running
  metrics expose all per-task keys (#161-style contract test).
- compiling detail when no `.engine` files; cleared after they appear.
- registry: `jasna` registered; catalog path markers (`test_catalog.py`);
  `status_md`: a `jasna` snapshot renders `X/Y done (Z left) | file | 47% · ETA … · fps`.
- `test_version.py`: six sources agree.
- UI: `wizard.test.tsx` — a jasna-shaped schema renders `unet4x_1080p` checked and
  `unet4x_4k` unchecked by default; zh labels exist for jasna fields.

Manual smoke (owner): **activate the supporter key in Jasna's GUI first**, then
issue #173's smoke; also time two short clips per-file vs folder mode (A8).

## Handoff notes (for the implementing subagents)

- Copy lada's process/reader/stop recipe where it applies; import shared helpers
  from `lada.py` instead of duplicating. Never touch `lada.py` behaviour.
- Keep `extra="forbid"`; every field gets a `description` (rjsf renders it).
- Run `uv run ruff format` on new files; mypy is scoped to `taskpaw_v3/`.
- UI tests run with `npx vitest run` in `taskpaw_v3/ui`.

## Debate record (round 1 → v2)

D-1 fixed (outcome-based degrade, AC 4) · D-2 fixed (`_lock` + `_stopping`) ·
D-3 fixed (staging + `os.replace`) · D-4 fixed (exit handled once, idle short-circuit) ·
D-5 fixed (tail cleared per launch; `supporter feature` only, text-only use) ·
D-6 recorded as A8 with smoke measurement · D-7 fixed (C3) · D-8 fixed (validator) ·
D-9 fixed (per-file retry state) · D-10 fixed (`queue_failed`; one definition) ·
D-11 fixed (3-strike abort) · D-12 fixed (collision detection) · D-13/D-19 moot
(parsers dropped, C4) · D-14 fixed (C1 smoke prerequisite; env channel shown
unavailable) · D-15 fixed (C4) · D-16 fixed · D-17 fixed (C5) · D-18 fixed (cq 0..63,
owned-flag reject list) · D-20 fixed (tests added) · D-21: empty folder no event
(fixed); lada blurb out of scope; no rescan out of scope; compiling note documented.

Round 2 → v3: N-1 fixed (`_unet_retry_pending` declared and in the launch formula) ·
N-2 fixed (`_last_failure_tail`) · N-3 fixed (exit branch under `_launch_lock`; stop
deletes staging only after terminating a live child) · N-4 fixed (two named locks,
lock order) · N-5 fixed (race tests split) · N-6 fixed (`--secondary-restoration`
allowed as documented override) · N-7 fixed (per-tier disable) · N-8 fixed
(`degraded` after abort) · N-9 fixed (distinct folders; own outputs never scanned) ·
N-10 fixed (`_total` defined) · N-11 Deferred (minor; bounded) · N-12 fixed (C1 wording).

Round 3 → v4: P-1 fixed (`_launch_lock` is an `RLock`; probe outside the lock) ·
P-2 fixed (tail snapshot only on rc≠0; N-2 closed) · P-3 fixed (`_unet_retry_pending`
kept through the plain retry; bound is 3/9 again, N-11 bound corrected) · P-4 fixed
(timed acquire in `stop()`, probe before lock) · P-5 fixed · P-6 fixed · P-7 fixed
(exclusion dropped) · P-8 fixed (description sentence). N-11 stays Deferred (minor).

Round 4 → v5 (wording/one-liners; no design change): Q-1 fixed (lock invariant
names `stop()`'s bounded wait) · Q-2 fixed (probe timeout 5 s) · Q-3 fixed (relaunch
reuses the probe) · Q-4 fixed (`_terminate_child` helper) · Q-5 fixed (`start()`
sweeps orphaned staging files) · Q-6 fixed (AC 7 wording).

Round 5: **clean**. R-1/R-2/R-3 (minor) are applied in implementation, not as a design revision: `_launch_next`'s post-`Popen` re-check calls `_terminate_child(proc, timeout=2)` and the lock invariant covers any `_terminate_child` wait; `start()`'s sweep skips staging files modified within the last 60 s (a live encode from another monitor); the no-lock `stop()` fallback leaves the staging file to the exit branch or, if the run is over, to `start()`'s sweep; a 5 s probe fits the 10 s reconfigure budget and at worst leaves the daemon worker unjoined at shutdown, as with lada.
