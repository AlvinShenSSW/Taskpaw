# H3: second real-machine round — model download, metadata before Miko, LTX detail

- Parent: Taskpaw EPIC #271 phase 1. Base: `24a4ec8` (H2). Branch `afk/claude-h3-271`.
- Principle P0: reuse what the Jasna monitor already does, in the same form the operator knows from it.

## Findings (operator, 2026-10-21, build test2)

1. **Missing model.** ABF-394 restore stopped with "缺少 LTX 模型文件：vae.safetensors.enc、vae-decoder.safetensors.enc" (`exec_restore._preflight` raises needs_attention). The operator copied the files in by hand. The workflow should offer the download.
2. **No cover, no NFO.** ABF-393 was pre-scraped by the operator: its input has an NFO, posters and full metadata. The drawer still showed 无封面 and no NFO data, because `board.film_detail` reads the poster and NFO only from the final manifest, which does not exist until Miko runs. The film name showed the grouping key `.|abf-393` (`display_name`).
3. **Progress too coarse.** The Jasna monitor (3.9.17, #269) shows LTX with four stages and detailed data: stage n/4 and name, percent, counters, rate, elapsed/eta, raw line, through `LtxProgress.tsx`. The workflow restore step parses the same lines with `parse_jasna_progress_line` but flattens them into one string (`exec_restore.progress_text`).

## Contract

- **M1 — model download from the Media Center.**
  - When `_preflight` finds LTX files missing (`jasna.ltx_missing_files`), the restore step shows waiting "缺少 LTX 模型" with the file list and total size (`jasna_download.FILES`), plus a 「下载模型」 action.
  - The action downloads with the Jasna monitor's own verified, pinned, out-of-process downloader (`jasna_download` / `DownloadSession`) into the Jasna model directory (the same destination the monitor uses). It runs one download at a time and can be stopped.
  - Download progress (bytes / total) shows on the 修复 step.
  - On success the failed restore stage is retried automatically.
  - Nothing downloads without the operator's click: the files are 8–13 GB, matching the monitor's confirmation.
- **M2 — metadata before Miko.**
  - Until a final manifest exists, the detail takes cover, title, actors and number from the **input**: the input NFO through the hardened `parse_nfo_summary`, and the input poster (role poster, else thumb, else the first image attachment), served from the intake work copy.
  - The film name is the input package's film folder name (the last path segment of the package), else the NFO number. It is never the grouping key.
  - Once the final exists, the final wins, as today.
- **M3 — LTX detail.**
  - The restore executor reports structured progress: the parsed state dict from `parse_jasna_progress_line` together with the volume label, alongside the short text.
  - The dispatcher passes it through `current.progress` as `{text, ltx}`, and the board puts it on the 修复 step.
  - The drawer's 修复 step renders it with the existing `LtxProgress` component: four stages, percent, counters, rate, elapsed/eta and raw line, as in the Jasna monitor.
  - The list row's step line shows the compact text: 阶段 n/4 名称 · % · 剩余 eta.
  - Other stages keep their text progress.

## Tests

- M1: preflight with missing files → step waiting with list and size; the download route starts the downloader (fake) and reports progress; on completion the stage is retried; a second click while one is running is refused.
- M2: pre-scraped input → detail has title, actors, number, poster from the input; name = folder name; once the final exists → final values.
- M3: a fake restore emits LTX lines → `current.progress.ltx` has the stage fields → the UI renders LtxProgress in the drawer step and the compact text in the row.

## Out of scope

WhisperJAV model downloads (no evidence yet); the Hub move (next).

## v2 (debate round 1, D01–D15; overrides the text above)

Scope change: the operator asked for the UI to be rebuilt faithfully to `design-system/taskpaw-v3` (V3). H3 now delivers the **backend** only. The UI work is folded into that rebuild, which will reuse `LtxDownload.tsx` (with an `onRespond` prop) and `LtxProgress.tsx`.

- **M1 (D01, D02, D07, D08, D09, D10, D14).**
  - Missing models leave the restore step **failed** (needs_attention), as today. The step carries a `download` object:
    - It holds `{state, request_id, files, total_bytes, downloaded_bytes, destination, variant, error}` from the session status.
    - Its `destination` is the workflow's own `<jasna exe dir>/model_weights/ltx-restore`.
  - `LocalWorkflowHost` owns exactly one `DownloadSession` behind a lock, plus a daemon thread that ticks it about every second while it has a status. `host.stop()` stops it.
  - **Routes:**
    - `POST /control/workflow/ltx-download {request_id, approved}` uses the session's own request handshake, which already refuses stale or second clicks.
    - `POST /control/workflow/ltx-download/stop`.
  - **Requesting a download:** the session is requested with the missing list cached by `RestoreExecutor` from its last preflight, as `(dest, missing, variant)`. The board never runs nvidia-smi.
  - **After a verified download:** missing files are re-checked, then every film whose restore is `failed` with a note starting `MISSING_LTX = "缺少 LTX 模型文件："` is retried (`retry_failed_stage`, then wake).
  - **One download per destination:** a module-level set of active destinations, guarded by a lock in `jasna_download_session`, stops the monitor and the workflow from downloading into the same directory at once.
  - **Cleanup on stop:** after a stop, the leftover `.{name}.*.part` files of the request are deleted.
  - Sizes come from `jasna_download.FILES`, not from any hard-coded number.
- **M2 (D05, D06, D11).**
  - `parse_nfo_summary` also returns the number (`<num>`).
  - `film_detail`, `poster_path` and `has_poster` fall back to the input before a final exists:
    - the NFO with role nfo;
    - the image: poster, else thumb, else an attachment with an image suffix;
    - all read from the work copy via `stage_results.workcopy_path`.
  - Responses include the poster source, so the UI cache key can use it.
  - **One name helper**, used by `film_row`, `current.film` and the GPU-lease label. `film_board` adds the package `relative_path`, and the helper picks the first that applies:
    1. the final folder name;
    2. the last package segment, when the package has exactly one film;
    3. the NFO number;
    4. the main video stem, original case.
  - The grouping key is never shown.
- **M3 (D04, D12, D13).**
  - `progress` stays a string (`progress_text`, unchanged). `JobContext.progress(text, ltx=None)` adds the parse-state dict for restore.
  - The dispatcher stores it as `current["ltx"]` (`DispatchStatus.current: dict[str, Any]`). The board copies it onto the restore step as `ltx`, and puts the volume label in `current_file`.
  - The film-level bar must not use the stage-local LTX percent (D12).
- **Minor:** D15 (a failed preflight still takes the GPU lease and leaves an empty workdir) is noted and acceptable.
