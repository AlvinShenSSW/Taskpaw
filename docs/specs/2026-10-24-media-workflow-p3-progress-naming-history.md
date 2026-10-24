# P3 — progress, 无码 naming, retained history, 成品目录, lost sources (#300–#304)

This is the frozen issue contract for Taskpaw #300, #301, #302, #303 and #304. They are part of EPIC #271.

The operator approved the UI on 2026-10-24 in the preview canvas, on the page 「识别进度与命名」. The relevant boards are:

- Drawer-ASR
- Naming
- Drawer-Archive
- Drawer-History
- Settings-Output
- Drawer-SourceMissing

MDCX304 #299 (M03) is the Miko half of #303.

Binding decisions:

- OD1–OD7, and Windows-only for the controller and workers.
- Home LAN, single user. No over-design.
- Build the UI from the V3 design baseline and the approved boards, and reuse the existing components (ProgressFrame, ConsoleFold, the RestoreLivePanel patterns).

## 1. Code facts (branch afk/claude-p3-300, based on afk/claude-p2-271)

### ASR (#300)

- `workflow/exec_asr.py:250-256` calls `context.progress(str)` for every WhisperJAV line. It sends no dict.
- `monitors/subs/progress.py:201-328` contains `AsrProgress`, a tested parser for qwen/decoupled runs:
  - phases 1–8, scene i/N;
  - weighted percent, capped at 99;
  - ETA in phase 5;
  - `snapshot()` returns phase, phase_n, scene, scenes, percent, eta_s and elapsed_s.
- The fixture is `tests/test_subs_progress.py RECORDED_RUN`.
- `Progress` already accepts a dict (`executors.py:77-80`).
- `board.py:317-347` adds started_at, console and params only for restore or worker runs.
- `mediaModel.progressText` (`:616`) drops string progress.
- `StageBlock` (`parts.tsx:265`) shows 「此阶段不提供影片总体百分比」 for ASR.

### Archive (#301)

- The archive step is `finalize.py` `Finalizer`. `_poll` (`:454-457`) reads only `status` and shows 「归档中」.
- Filmpaw's `GET /api/workflow/archive/{operation_id}` already returns the public task: phase, progress_bytes, total_bytes, verified_bytes, completed_count/total_count and per-item progress_bytes. It saves every 0.2 s (Filmpaw transfer.py).
- The final package is at `work_dir/<output_relative_path>` (`finalize.py:93`).
- Cleanup deletes the package items (`:87`, `:607`) and the film's private dirs (`_reclaim_film` `:800-815`).

### History (#302)

- `board.py` `_poster` / `poster_path` / jacket / NFO facts read the final package's files (`:852-887`, `:933-939`). There is no fallback once they are deleted.
- The store keeps stages, attempts, plan/params, receipts, cleanup results, task-log rows and `film_timing`.
- `read_store.film_board` shows only the newest 20 finished films (`done_limit=20`, `:409`, `:446-456`). A paging spec already exists: 2026-09-26-198 film-list-paging.

### Naming (#303)

- `monitors/plugins/jasna.py:278-283` and `output_path_for`/`staging_path_for` (`:394`) produce `<stem>-破解.mp4`. `exec_restore.py:759`, `:803` use them.
- Reuse detection is `exec_restore.py:102-123` (`_RESTORED_SUFFIXES`, `is_restored_name`, `needs_restore`).
- Subtitles follow the restored stem (`exec_common.output_stems`, `exec_asr.py:274`, `exec_translate.py:204`, `return_step.py:146-161`).
- In `scrape_step.py:208-236`, each video carries `{file, volume, restored, zh_subtitle}`. `actual_marks` is at `:453-465`.
- The Miko fake is `tests/workflow_fake_mdcx.py`.
- Every workflow restore is LTX (`exec_restore.MODEL="ltx"`). The exception is `reused` inputs.

### Lost source (#304)

- No film discard action exists today.
- `intake_store.settle_abandoned_intake` exists (`:981`). Check whether it covers part of this.

## 2. Requirements

### #300 — ASR live progress (board Drawer-ASR)

**A1.** `exec_asr` feeds each line to `AsrProgress` and publishes a dict on `context.progress`:
- `{phase, phase_n, phase_label, scene, scenes, percent, eta_s, elapsed_s, console, phases:[{n, label, state, seconds}]}`
- Throttle it the way restore does (`CONSOLE_INTERVAL_S`).
- Phase labels:
  1. 提取音频
  2. 划分场景
  3. 语音增强
  4. 语音分段
  5. 识别语音
  6. 生成场景字幕
  7. 合并字幕
  8. 过滤与清理
- A pipeline without the qwen/decoupled tag publishes only elapsed and console.

**A2.** The board gives the running asr step `started_at`, `console` and the progress dict, for local controller runs. A worker-run ASR, if one ever exists, uses the same shape.

**A3.** UI:
- An `AsrLivePanel` in the film drawer, matching the board:
  - heartbeat 已用 and 「第 k/8 阶段 · <label>」;
  - percent and bar;
  - 预计剩余;
  - tiles 场景 / 已用 / 预计剩余 / 设备;
  - the 8-phase list with times;
  - the 最新输出 fold.
- The step chip shows 「识别 · NN%」.
- The overview row shows 「识别 · NN% · 场景 a/b · 约 N 分钟」.
- Without phases, show only the heartbeat and the console, with no fake percent.
- Remove the 「此阶段不提供影片总体百分比」 fallback whenever progress exists.

### #301 — 成品目录, archive progress, open folder (boards Drawer-Archive, Settings-Output)

**B1.** A new controller workflow setting `output_dir`, labelled 成品目录:
- Local fixed disk only; a network path is refused with the existing directory validation.
- The default is `<data_dir>/成品`.
- It is offered with 选择 / 打开 / 创建此目录 in 设置 · 媒体工作流 (controller section) and in the wizard's controller step, like INBOX.
- Changing it while films are waiting for 确认 or 归档 is refused with the existing pause/drain guard pattern.

**B2.** The approved final package lives at `<output_dir>/<final folder name>`, one folder per film. The folder name is the Miko folder name, e.g. `JUR-706-无码-C-4K 标题 演员`.
- Build it there directly (preferred), or move it there when it is produced, on the same volume.
- If output_dir is on a different volume from the work dir: copy, verify, then delete the source using the existing exact-delete helpers (C6).
- The archive manifest, Filmpaw source and cleanup read from the new location.
- Cleanup deletes it only after archive verification, as today.
- C10 still holds: the film's private work dirs are reclaimed.
- A name collision in output_dir gets a ` (2)` suffix and must never overwrite.

**B3.** Archive progress. `_poll` maps the Filmpaw task onto the archive step's progress dict:
- `{phase: copying|verifying, copied_bytes, verified_bytes, total_bytes, files_done, files_total, items:[{name, bytes, total}], target, started_at}`
- The UI computes speed and ETA from successive samples on the client.
- The drawer panel matches Drawer-Archive. The chip and overview show 「归档 · NN%」.

**B4.** Open folder. A control API, loopback only and UI-only, `POST /control/workflow/films/{id}/open-folder` with `{which: "output"|"archive"}`, opens the film's **recorded** folder in Explorer:
- the output_dir package while the film is waiting for 确认/归档;
- the archived NAS folder from the Filmpaw receipt after archive.
- No arbitrary path is accepted.
- It returns 409 「目录已不存在」 when the folder is gone.
- The drawer shows 打开成品目录 or 打开归档目录 accordingly. History films included.
- The archived folder path is persisted with the archive receipt. Add it if it is missing.

### #302 — retained history (board Drawer-History)

**C1.** Before the workcopy cleanup deletes the package, copy into `<data_dir>/retained/<film_id>/`:
- the poster;
- the thumb, if present;
- a small JSON summary from the NFO: title, actors, marks, final folder name, archive path.

`retained/` sits outside every reclaimed dir (C10), and it is never deleted by cleanup. A copy failure is logged and never blocks cleanup.

**C2.** The board falls back to the retained cover and facts when the package files are gone. Finished films therefore keep their cover, title and actors.

**C3.** History drawer for finished films:
- cover with marks;
- facts;
- archive path;
- 总用时;
- the 修复耗电 total if available;
- a per-step table (步骤 / 设备 / 用时 / 结果) built from the store's `film_timing`, receipts and cleanup results;
- the buttons 本片参数, LTX 用时明细 (existing components) and 打开归档目录 (B4).

**C4.** 已完成 is paged, so every finished film is reachable. Reuse the film-list-paging design (2026-09-26-198) and keep the current page size. The overview count shows the total.

### #303 — LTX is 无码 (board Naming; Miko side is MDCX304 #299)

**D1.** The workflow restore output is `<stem>-无码.mp4`, with staging at `<stem>-无码.tmp.mp4`.
- The standalone Jasna monitor plugin keeps writing `-破解.mp4`; the 无码 rule is workflow-only (the workflow always restores with LTX).
- Reuse detection accepts `-破解.mp4`, `-无码.mp4` and `_restored.mp4`.
- Subtitles follow the stem as today.

**D2.** The scrape request adds a per-video `ltx: bool`. It is true when:
- restore status is `applied` (the workflow is always LTX); or
- the input is `reused` and was named `-无码.mp4`.

A `reused` `-破解`/`_restored` input sends `ltx: false`, so it keeps 破解. Update `actual_marks` and the mark verification so that ltx videos expect 无码, not 破解. Update the fake Miko to match MDCX304 #299: 无码 mosaic, `-无码` names, `<name>-poster/-fanart/-thumb.jpg`, `.zh.srt`.

**D3.** The final package and the archive hold only the Chinese subtitle. A `.ja.srt` is never staged into scrape, never in the package and never archived; it stays an intermediate file and is reclaimed with the work dirs. The board and readers accept `<name>-poster.jpg` as well as `poster.jpg`. Multi-volume films (operator, 2026-10-24) get one image set per volume, each named after its own file, e.g. `KAVR-371-8K-cd1-poster.jpg`, plus a `folder.jpg` copy of the first volume's poster. The board uses the first volume's poster, or `folder.jpg`.

### #304 — lost source (board Drawer-SourceMissing)

**E1.** For a film that has not started a stage, check its INBOX source files (the intake manifest paths) before dispatch and on the scheduler tick. Throttle this to at most one check per film per 60 s.
- When any file is missing, the film enters 片源丢失 with `{paths, found_at}`.
- It is never dispatched, and nothing is deleted.
- The board shows the badge and the drawer alert as on the board.

**E2.** 重新检查 re-runs the check at once. If the files are back, the film returns to its previous waiting state.

**E3.** 删除此任务 is shown for a 片源丢失 film. Offer it also for any film that has no running or claimed stage and is not archived, behind the same confirmation.
- Refuse it while any stage is running or claimed, or while any open worker route/assignment exists.
- On success it removes the film from the queue and board (a terminal `removed` state is acceptable), and reclaims only the film's private work dirs.
- It never touches INBOX, output_dir or NAS.
- It writes a task-log row.
- The confirmation copy is exactly as on the board.

## 3. Out of scope

- Miko internals (MDCX304 #299).
- Filmpaw changes. Its progress API already exists. If a field is missing, stop and record it instead.
- Parallel ASR.
- Worker-side ASR.
- Deleting archived NAS films.
- Bulk delete.

## 4. Verification

- Each child: targeted tests first, red, then green.
- Backend tests use fakes: no GPU, no real WhisperJAV, Jasna or Filmpaw, and no live data.
- UI: vitest for the panels and actions.
- Then ruff, mypy (native, linux, darwin), npm test/lint/tsc -b/build, cargo test if touched, and the full repo pytest from the repo root.

## 5. Dispositions

| ID | Finding | Disposition |
|---|---|---|
| E-D1 | #304 E1: where the 片源丢失 state lives. `settle_abandoned_intake` (`intake_store.py:981`) only settles an operation left by an exited controller boot; it does not cover a vanished source. The store's claim already refuses an inadmissible package (`source_not_admissible`), which is why ABF-394 waited with no reason. | The state is **in memory** (`source_watch.SourceWatch`, `{paths, found_at}` per film), not a store row: it is derived from the files, so a restart finds it again with the first check, and `found_at` is then the time this controller first saw them missing. The check is a plain existence test of every input-manifest member under `INBOX/<package_path>` (`Path.exists`; an unreachable INBOX root or an `OSError` is unknown, never lost), at most once per film per 60 s, and only for a film with no stage started after intake. |
| E-D2 | #304 E1: where dispatch is stopped. | The dispatcher skips the film's pending jobs (`LocalDispatcher(sources=...)`); the routing pass gets `hold=SourceWatch.ids()` (`run_pass` → `routing_snapshot(hold)`), which marks the film `source_admissible=False`, so it is neither routed nor kept routed (an unclaimed route is withdrawn by the pass, as for any inadmissible source). The check runs from the dispatcher tick over its pending (planned) jobs, so a film still in 待处理 is checked once it is planned. 重新检查 runs it at once and wakes both threads. |
| E-D3 | #304 E3: the terminal `removed` state and the schema. | **No schema migration** (`SCHEMA_VERSION` stays 4): `FilmRecord.removed_at` is an optional field of the film's `body_json` (omitted from the wire while null, like `InputManifest.source_binding`). `ReadCommands` skip a removed film through `json_extract(f.body_json,'$.removed_at') IS NULL` in `film_board`, `list_jobs`, `list_unplanned`, the package counts and `_package_films`, and `RouteCommands._routing_films`. The command is the operator-only `remove_film` (`FilmRemove`, CAS on the film version). |
| E-D4 | #304 E3: the guard. | Checked twice: `operator_actions.removal_refusal` for the friendly refusal (409 codes `film_running`, `film_routed`, `film_archived`; the board's `can_remove` uses the same function), and again inside the store's transaction (`precondition_failed`), so a claim racing the delete wins. Refused for a running/unknown stage, an unresolved assignment, an open route, or any approval, archive or cleanup record. |
| E-D5 | #304 E3: what is reclaimed. | Only `finalize.film_work_dirs` (compute attempt dirs, `return/`, `scrape/`, `lookup/` of the film; the same set `Finalizer._reclaim_film` uses, now shared) and the shared intake snapshot when no other film of its package remains. Every path must lie inside the work dir. INBOX, the output directory and NAS are never opened. A task-log row `workflow.film.removed` (`WorkflowLog.removed`) records it. Dirs that could not be removed are counted in the row and not retried. |
| E-D6 | #304 board contract. | Film rows and details carry `source_missing` (`{paths, found_at}` or null) and `can_remove`; `attention_kind` gains `source_missing` and the row moves to the attention group with the note 「INBOX 里的文件已不存在 · 不再派发」. The #286 events observer does not read them (`board_inputs(..., removal=False)`), so no notification is sent for 片源丢失. Routes: `POST /control/workflow/films/{id}/recheck-source`, `POST /control/workflow/films/{id}/remove`, loopback control API only. |
| CX1 | Codex P1 / IR P2-2: a removed film could still be routed or claimed by a decision read before the delete (hidden running film; its attempt dir created after the reclaim). | `_require_source_admissible` (the claim's and `route_film`'s own check, so also the dispatcher's `_ensure_route`) refuses a film with `removed_at` as `source_not_admissible` inside the mutation transaction; the routing pass already tolerates that code as a race. |
| CX2 | Codex P2 / IR P2-1: the source check ran only on the dispatcher thread, so a paused or busy dispatcher left `_route_pass` with an empty cache. | `_route_pass` first runs `SourceWatch.check_candidates()` (the throttled check of every planned film with restore or ASR pending; a started film is skipped by the check itself) and passes the result as `hold`. No dependence on the dispatcher. |
| IR1 | IR P2-3: a re-dropped package was absorbed into the removed film (discovery reused its film_id, the board hid the package). | Discovery gets `removed` (the package's deleted film ids, `removed_intake_films`): they are never reused for a regrouped package, and a package that was `missing` and is back (`reappeared`) is regrouped even when unchanged, so a new film appears. A present, unchanged package keeps its grouping, so a deleted film stays deleted until the folder changes. `list_intake_board` no longer counts a removed film as holding its package. |
| IR2 | IR P3-1: a network hiccup could read as 片源丢失. | After collecting the missing members `SourceWatch` re-checks the INBOX root; unreachable means unknown: the state before is kept, `unknown(film_id)` is true and 重新检查 returns `unreachable`. A reachable root with a missing package folder is genuinely lost, so the package base is not part of the re-check. |
| IR3 | IR P3-2: the board and the store disagreed on `can_remove` (approval context; what counts as claimed). | One `operator_actions.busy_films` (any open assignment, any open route) feeds both the guarded delete and the board. The store counts an approval only for the current ready context, as the board's `approval_active` does. |
| IR4 | IR P3-3: 改派 and 待处理 previews ignored `hold`. | `reassign_options` and `plan_list` take `hold` (`SourceWatch.ids()`), so a lost film shows `片源变化，等待重新扫描` and no device estimate. |
| IR5 | IR P3-4: UI. | The 概览 panel subtitle reads exactly 「INBOX 里的文件已不存在 · 不再派发」; on `unreachable` 重新检查 toasts 「INBOX 暂时无法访问，稍后再试」; the alert offers 删除此任务 only when `can_remove`. |
| CX3 | Codex P2: a removed film retained source references forever, blocking input cleanup of a surviving or re-ingested film. | `remove_film` releases all of that film's `source_refs` (`retained=0`) in the removal transaction. It never deletes source files. Regression coverage admits input cleanup both for an existing shared film and for a later re-ingest; live shared films still block cleanup. |
| CX4 | Codex P2: a directory notification changed `missing` to `receiving` before discovery, so an unchanged returned package reused a removed film id. | `record_intake_source_notice` preserves an existing `missing` state on dirty hints until discovery observes the package. Service regressions cover periodic scans, directory notifications and overflow scans: the returned package gets a fresh film id and is visible in intake. |
| CX5 | Codex P2: retry reset a failed restore to `pending`, so vanished INBOX members blocked a film that had already started. | `SourceWatch.started` also checks the stage's `first_started_at`, which survives retry. A failed synthetic restore, explicit retry and subsequent source loss still permit dispatch. |
| CX6 | Codex P2: a member existence `OSError` was treated as present and could falsely clear 片源丢失. | Member access failures are logged and raise the existing unknown result. The prior state and `found_at` remain; recheck returns `unreachable` for the existing 「INBOX 暂时无法访问，稍后再试」 UI. Regressions cover both previously present and lost films, then recovery after a successful check. |
| CX7 | Codex P2: concurrent scheduler and dispatcher checks returned a stale source result before the first check completed. | One `SourceWatch._check_lock` serializes the throttled check and result recording; the second caller sees the completed result. An event-gated regression confirms one filesystem check, identical lost-source results and no dispatch. |
| CX8 | Codex P2: removal during a replacement intake allowed later publication and left retained source references on a hidden film. | The shared `film_intake_open` predicate checks the film's package for an unresolved `intake_operations` row. `remove_film` rechecks it inside its transaction and returns coded 409 `film_intaking`, 「正在收录这部片，稍后再删」. The board uses the same predicate; the regression covers direct store refusal, board/API refusal, a bypassed precheck and removal after settlement. |
| CX9 | Codex P2: removing an unplanned film during metadata lookup deleted its work dir, which the result write recreated. | Removal and the board's `can_remove` use the existing `MetadataLookup._running` marker through the shared refusal predicate, returning coded 409 `film_lookup_running`, 「正在查询元数据，稍后再删」. The marker remains set through `_write`. A fake lookup paused during work and before writing proves both refusals; removal after completion reclaims its directory. |
| D-D1 | #303 D1 (superseded by D-CX1: the monitor naming was reverted; only the helper parameters and the reuse detection remain): the Jasna monitor names a file by its tier, but the plan (`plan_subs`) names pending files before the tier is probed. | `output_path_for` / `staging_path_for` take `ltx`. The plan predicts `-无码` when either LTX tickbox is on (`uses_ltx`). `_publish_current` publishes under the launch's real name (`ltx_launch(cfg, tier)`) and `_retarget_subs` moves the pending subtitle job (`media`, `zh_target`, and `ja_target` when it was derived) to it, so a 4K file with only the 1080p tickbox still ends up `-破解` with `-破解.zh.srt`. Done detection, collision keys, the staging sweep and the subtitle temp sweep accept `-破解`, `-无码` and `_restored`. |
| D-D2 | #303 D2: how `ltx` is derived per video. | `_is_ltx(name, status)` in `scrape_step`: a `-无码.mp4` file is LTX (an applied restore, or a reused earlier LTX result); a `-破解.mp4` / `_restored.mp4` file is not; any other name is LTX when the film's restoration is `applied` (the workflow only restores with LTX). `restoration.videos` carries no per-video decision, so the name is the only per-video fact after the return package. |
| D-D3 | #303 D2: marks and mark verification. | `PostprocessEvidence.actual_marks` gains `uncensored`: an LTX video is stamped 无码, so a film with LTX videos gets `uncensored` and one with other restored videos gets `restored` (both for a mixed film, which D12 of MDCX304 says never happens). The FinalManifest validator treats both as a restoration claim. The board wire is unchanged: `marks.restored` is true for either, so the UI needs no change. Verification: each `expected_marks[].mosaic` Miko reports must be `无码` for an `ltx` video and `无码破解` otherwise; a mismatch (e.g. an old Miko that ignores `ltx`) is `needs_attention` and nothing is published. |
| D-D4 | #303 D3: where the Japanese subtitle is dropped. | `return_step` packages only the Chinese subtitle: the generated or kept `.ja.srt` and an input Japanese subtitle item are no longer members of the return package, so nothing stages them and `scrape_step._place_japanese` (which put them next to Miko's output) is removed. `StageResult` and `kept_ja` are unchanged; the `.ja.srt` stays in the attempt dirs and is reclaimed with the work dirs. |
| D-D5 | #303 D3: which poster the board uses. | Miko lists a poster per volume. `scrape_step` records the first volume's `<name>-poster.jpg` as `poster_item_id` (the first listed poster is the fallback); `board._poster_item` serves that item, then any poster, then `folder.jpg`, so finals from before #303 (`poster.jpg`) still read. The Miko lookup CLI's `poster.jpg` (`lookup.POSTER_NAME`) is a separate contract and is unchanged. |
| D-D6 | #303 verification: the fake Miko and multi-volume coverage. | The fake is `taskpaw_v3/tests/workflow_fake_mdcx.py` (not `tests/`). It follows MDCX304 #299: `-无码` / `-破解` / `-C` names, per-volume `.nfo` and `-poster/-fanart/-thumb.jpg`, `folder.jpg`, `.zh.srt`, and a `wrong_mosaic` mode. The test films are single-volume, so the multi-volume file list is pinned by running the fake directly, and the first-volume poster by `board._poster_item` unit tests. |
| D-CX1 | Codex r1 [P2]×2: the monitor's tier-dependent -无码/-破解 name could differ from the planned name, so subtitles were skipped or a .ja.srt was mis-targeted. | Monitor naming reverted to always -破解; the 无码 rule is workflow-only (the workflow is always LTX). Operator: 不要过度工程. |
