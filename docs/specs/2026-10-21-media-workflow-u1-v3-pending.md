# U1: 媒体处理中心 V3 rebuild with the pending-review list

- **Parent:** Taskpaw EPIC #271 phase 1. **Base:** J2, which is H3 plus TaskPaw 3.9.18 merged, with the workflow restore on the new Jasna tiers and the bitrate cap. **Branch:** `afk/claude-u1-271`.
- **Inputs:**
  - The approved design `design-system/taskpaw-v3/pages/media-workflow.md` and `preview/media-workflow-v3.html`, with V3 chosen.
  - The V3 build spec (pages, tokens, labels, data mapping), committed as `docs/specs/2026-10-21-media-center-v3-build-spec.md`.
  - The operator decisions of 2026-10-21 (spec §8).
  - The new operator requirement in this doc.
- **Principle P0:** a single-user home tool. Build what V3 shows; add only what the operator asked for.

## New operator requirement (2026-10-21): 待处理 before the queue

> 片源投入池子后不会立刻执行，而是会进入待处理列表，会显示出片源的信息，图片这些，以及你推荐assign给的机器和画质，我可以调整（比如我特别喜欢的演员我选高画质，普通的我就选标准），我确认完调整完后，才进入正式队列。

(A source dropped into the pool is not executed straight away. It goes onto a pending list that shows the source's info and images, plus the machine and quality you recommend assigning it. I can adjust them — for example, High for performers I really like and Standard for ordinary ones. Only once I have adjusted and confirmed does it enter the formal queue.)

## Contract

### P — the plan gate (backend)

- **P1. Film plan.**
  - Every film that intake creates starts **unplanned**. The dispatcher never claims any stage of an unplanned film.
  - A plan is `{ltx_model: standard|good|high, machine: <agent id>}`, bound to the film's current `input_revision`.
  - A changed input revision, from a regroup or new files, voids the plan, and the film returns to 待处理.
  - The plan is stored in the workflow store: one row per film, with `recommended` and `chosen` values, `confirmed_at` and the operator.
  - Confirming uses CAS on `input_revision`; a stale revision gets 409.
- **P2. Recommendation** (pure, deterministic, explained):
  - **Tier:**
    - When the operator has confirmed a plan for any of this film's NFO actors, use the most recent tier chosen for that actor. The reason reads 「{actor} 上次选择 {tier}」.
    - Otherwise use the Jasna default for the workflow, `ltx_model` from the restore config. The reason reads 「默认」.
    - A film whose inputs are all already restored (`needs_gpu` false) shows 「无需修复」 and has no tier.
  - **Machine:**
    - Phase 1 has one GPU agent, the controller itself. The recommendation is 本机, with the reason 「{GPU} {VRAM}」.
    - A 4K film on a device under 16 GB keeps that recommendation but shows the existing 4K VRAM warning.
    - The multi-machine phase replaces this rule; the field and its reason stay.
- **P3. Routes:**
  - `GET /control/workflow/plans` lists unplanned films. Each entry has:
    - its row data: cover, title, number, actors, resolution, duration, source size and file count;
    - the recommendation with reasons;
    - the choices: tiers, machines.
  - `POST /control/workflow/films/{id}/plan {input_revision, ltx_model, machine}` confirms one film.
  - `POST /control/workflow/plans/confirm-recommended {films: [{film_id, input_revision}]}` confirms each film as recommended. This is the batch button; each film has its own CAS.
  - After either confirm, the dispatcher is woken.
- **P4. Restore uses the plan.**
  - The restore executor takes `ltx_model` from the film's confirmed plan. This is the J2 per-film hook `_cfg_for(context)`.
  - The tier is part of the stage's tool identity, so a changed tier is a new restore.
  - Model preflight and the download offer use the planned tier.

### V — V3 rebuild (frontend; spec §1–§8 unchanged unless noted)

- **V1.** Build the shell, tokens, status vocabulary and pages per the V3 build spec:
  - 概览, 影片队列, 待确认, 异常, 本机;
  - the later pages, marked 「后续」;
  - the drawer;
  - the 图文确认 dialog.

  Use the H3 data: LtxProgress and LtxDownload in the drawer, the input metadata, and the film names.
- **V2. 待处理 page** (new; built in the V3 visual language from the 待确认 review cards).
  - **Rail:** the item 「待处理」 (MUI icon `PlaylistAddCheck`) comes right after 概览. Its count badge works like the 待确认 badge.
  - **Heading:**
    - eyebrow `PLANNING / 待处理`
    - h1 「先安排，再开工。」
    - p 「新收件的影片停在这里。看封面与信息，按推荐或自行选择画质和设备；确认后才进入影片队列。」
  - **Toolbar:**
    - pills: 全部 / 需修复 / 无需修复;
    - chip 「{n} 部影片」;
    - ghost button 「全部按推荐确认」, which confirms every listed film whose recommendation is complete and shows a toast 「已确认 {n} 部，进入影片队列」.
  - **Card** (2 columns at ≥1200px, 1 at ≤900px):
    - cover 88×117;
    - title, resolution chip, 「{number} · {actors}」;
    - 「{duration} · {size} · {n} 个文件」;
    - the plan block:
      - 画质: a 3-option segmented control, 标准 / 良好 / 高, with the recommended one tagged 「推荐」 and the reason under it in 11px muted text;
      - 设备: a select with its reason.
    - Foot: ghost 「查看详情」 (opens the drawer) and primary 「确认入队」.
    - A changed revision shows the amber notice 「收件内容已更新，请重新查看后确认」.
  - **Empty state:** 「没有待处理影片」 / 「新投入 INBOX 的影片会先出现在这里。」
- **V3. The lifecycle step line** gains 「待处理」 after 「入库扫描」:
  入库扫描 → 待处理 → 修复 → 识别 → 翻译 → 回传 → 整理 → 待确认 → 归档 → 清理 → 完成.
  - The overview flow strip gains a 「待处理」 button between 收件 and 修复 / 字幕, with an amber count.
  - The overview stat 「等待您的确认」 counts 待处理 + 待确认, with the sub-line 「{a} 部待安排 · {b} 部待入库」.
- **V4. The drawer** shows the plan in Facts: 画质 / 设备, or 「待安排」.
- **V5. Preview first.** Add the 待处理 page to a copy of the V3 prototype, `design-system/taskpaw-v3/preview/media-workflow-v3-pending.html`, before the React page is built. The operator can review it, and the React build follows it.

## Tests

- **P:**
  - An unplanned film is never claimed.
  - Confirm → claimed. A stale revision gets 409. A regroup voids the plan.
  - Recommendation: actor history → that tier; none → default; a restored input → 无需修复.
  - Batch confirm.
  - Restore argv uses the planned tier, and the model preflight checks that tier's files.
- **V:**
  - Rail order and badges.
  - 待处理 card rendering: recommendation tag, reason, change, confirm → leaves the list.
  - Batch confirm.
  - The step line contains 待处理.
  - Each V3 page renders from fixture data with the spec's labels.
  - No 演示/FIXTURE text.

## Out of scope

- Estimated restore time per tier. This is a candidate follow-up: from the machine's own past restores scaled by the tier factor.
- Favourite-performer lists. The history-based recommendation covers it.
- Multi-machine dispatch.

## v2 (debate U01–U12; overrides the text above)

- **L — metadata preview before planning (new; fixes U06).**
  - Raw INBOX downloads carry no NFO or cover, and Miko runs last, so without this the 待处理 card has nothing to show and the actor rule can never fire. The operator explicitly asked for the info and images on the pending list.
  - **M02 (MDCX304):** a headless `python -m mdcx.cmd.lookup_one --request req.json --result res.json` (`lookup_one/1`).
    - **Input:** the number, parsed by MDCX's own number parser from the main video's file name.
    - **Behaviour:** it uses the operator's MDCX profile (sites and cookies) through the same `CrawlerProvider` that `postprocess_one` uses. It reads only, and writes only the result JSON and `poster.jpg` into a given out dir.
    - **Output:** `{number, title, actors[], studio, release, runtime, poster}`; failures report `needs_attention` or `transient`.
  - **Taskpaw:** after intake has created a film, the controller runs `lookup_one` once per input revision, with a bounded timeout and no GPU.
    - It caches the result under the work dir.
    - The 待处理 card and the drawer show it ahead of the input metadata (precedence: final > lookup > input).
    - A failed lookup shows 「未查到资料」 and the card stays plannable.
    - Retries are manual: a 「重新查询」 button on the card.
- **P1' (U01, U02, U03).**
  - **Gate restore only.** The `read_store.list_jobs` restore query joins `film_plans` on `(film_id, input_revision)`, and `claim`'s apply re-checks it, next to `_require_source_admissible`. All later stages already depend on restore.
  - **Table:** `film_plans(film_id, input_revision, ltx_model NULL, machine, confirmed_at)`, keyed by `(film_id, input_revision)`.
    - A new revision has no row, so the film is back in 待处理; no void write is needed.
    - A confirmed plan cannot be changed.
  - **Schema:** v3 migration (`SCHEMA_VERSION=3`, a frozen v2 DDL copy, a backup step) and a `require_role` mapping for the new command.
- **P2' (U02, U06, U08).** Recommendation.
  - **Tier**, first match wins:
    1. the same film's previous revision's choice, reason 「上次选择 {tier}」;
    2. the most recent tier chosen for any of the lookup actors (exact name match after NFKC and whitespace trim; several actors → the highest tier among them), reason 「{actor} 上次选择 {tier}」;
    3. the restore default, reason 「默认」.
  - **Machine:** 本机 (local worker_id), with its GPU and VRAM as the reason. Phase 1 restore stops at Full HD on any card, so a 4K film gets the reason 「本机修复输出上限 1080p」. It is not blocked.
  - A `needs_gpu=false` film gets `ltx_model` null, with 「无需修复」.
- **P3' (U09, U11, U12).** Routes:
  - `GET /control/workflow/plans` → unplanned films. Each entry has:
    - the row data: `poster_source` / `has_poster` through `/films/{id}/poster`;
    - resolution from the largest probe;
    - duration as the sum over the main videos;
    - size as the sum over the main videos, plus the item count;
    - the lookup result and the recommendation with reasons.
  - `POST /control/workflow/plans {plans: [{film_id, input_revision, ltx_model, machine}]}` → a result for each item. It serves the single confirm and the batch confirm, because the UI sends what it showed.
  - The plan route is distinct from the existing archive confirm, with distinct labels and toasts.
  - Every successful POST runs `dispatcher.wake()`.
- **P4' (U05, U10).** Tier into restore.
  - The `list_jobs` join carries `ltx_model` into `PendingComputeJob`.
  - `tools(job)` and `_run` use `cfg.model_copy(update={"ltx_model": …})`, read once.
  - The tier lives only in settings, so it changes `config_digest`. `model` stays "ltx", so the single approved capability profile still matches the claim.
  - The model-download offer stores `(weights, ltx_model, fast)`. `recall_missing` infers the tier from file names.
  - A `needs_gpu=false` film uses the default cfg.
- **U04. Existing films (operator decision IR02, overrides the first draft's "already have an attempt"):**
  - The migration backfills a default-tier plan only for films whose current-revision restore has status `succeeded`, i.e. films already past restore.
  - Every other restore stays unplanned: never claimed, interrupted, failed or retrying, even with attempts. Once pending it is in 待处理, where the operator picks the tier, and nothing claims it before. On the operator's machine that is ABF-393 (interrupted) and ABF-394 (never claimed); this is intended.
  - Backfilled rows carry `film_plans.backfilled = 1`. They are no operator choice, so the recommendation history leaves them out (IR03).
- **V' (U07).**
  - `film_board` joins the plan. `_place` puts a film with a pending restore and no plan into a 「待处理」 group.
  - Rows carry `plan {ltx_model, machine}`, and the step line contains 待处理.
  - The rail badge and the overview stat count from `/films`.
- **Cut:**
  - the stored `recommended` and `operator` columns (audit already records them);
  - the `confirm-recommended` server route;
  - the sentence 「改画质即新修复」.
- **V2 heading (operator decision 7, overrides V2):** no eyebrow, h1 slogan or paragraph. A compact title row 「待处理」 with 「全部按推荐确认」 on the right; the toolbar sits below it.

### v2.1 — lookup profile isolation (M02 IR01)

- `lookup_one` runs with `MDCX_CONFIG_DIR` set to a fresh per-run directory under the workflow work dir. It is built exactly as the M01 scrape attempt builds its config dir: the operator's MDCX profile JSON (`mdcx_profile`) is copied in as `config.json`, with no `MDCx.config` mark.
- MDCX may create a mark and `userdata/` inside that directory. That is harmless, because the directory is discarded after the run.
- `lookup_one` refuses a mark that points outside its config dir (M02 fix), so the live profile `%LOCALAPPDATA%/com.mdcx.desktop` is never read through a mark and never written.
- The run passes `file_name` = the main video's file name. Names without a number are refused by M02, and the card shows 「文件名中没有番号」 with 「重新查询」 disabled.

## U1b (operator feedback 2026-10-21)

After installing the U1 build the operator wrote: 「封面被裁切了，可以用poster吗 另外点击能lightbox放大吗」 (the cover is cropped; can it use the poster, and can a click enlarge it in a lightbox?).

- **Cause.** Downloaded packages carry `<num>-poster.jpg`, `-thumb.jpg`, `-fanart.jpg` and `<num>.jpg`, and every one of them is the whole landscape jacket (2184×1477 or 840×568). The front cover, which is the poster, is the jacket's right part. When the Miko lookup succeeds it returns a portrait `poster.jpg` (421×600). ABF-393's lookup ran past the 90 s metadata budget, so its card fell back to the input image. The portrait box (`object-fit: cover`, centred) then showed the middle of the jacket.
- **Poster display.** One shared `Cover` serves every place: 待处理 cards, queue cards, review cards, overview rows and peeks, the drawer hero and the 图文确认 dialog. A portrait image renders as before. A landscape image (`naturalWidth > naturalHeight`) renders with `object-fit: cover; object-position: right center`, so the front cover shows. The box sizes are unchanged: 61×82, 88×117, 116×154 and 108 px.
- **Lightbox.** A cover with an image is a button (「查看大图」). It works with Enter and Space and shows a focus ring. It opens a dialog with the full, uncropped image, up to 92vw × 88vh (`object-fit: contain`) over a dark backdrop. The dialog shows the film's full jacket (`/films/{id}/cover`) when `has_cover` is true; otherwise it shows the poster, and it also falls back to the poster when the jacket fails to load. Esc, a backdrop click or 「关闭大图」 close it, and focus returns to the cover. Inside the drawer or the 图文确认 dialog, Esc closes only the lightbox.
- **Lookup budget.** MDCX304 8a3f519 gives `lookup_one` its own 270 s budget: 240 s for metadata and 30 s for the poster. Taskpaw's wall-clock kill moves from 150 s to **300 s**, so a slow site ends with lookup_one's own 「资料查询超时」 result instead of a killed process.
- **Codex r6 P2s.** The 异常 page polls the target preflights, still only for the top ten films, because it lists films whose target is offline, unwritable or taken. 待处理 hides a confirmed film by `(film_id, input_revision)`, so a newer revision that has not been planned shows again, flagged as changed.

## U1e (operator feedback 2026-10-22): how long a film took

> 四个阶段的合计用时要有一个地方显示。日志里最后也要体现一部片从开始到翻译结束的全部时间。

(Show the combined time of the four LTX stages somewhere. The log must also end with a film's total time, from the start to the end of translation.)

- **What the code showed.**
  - The TaskPaw 「日志」 tab reads the agent's persistent task log (`TaskLog`, `logs/tasklog-YYYYMMDD.jsonl`), not the in-memory event queue. The queue feeds the Hub and OpenClaw, and monitor events reach the log only as `event.mirrored` copies. The workflow writes neither today.
  - A stage result lives in `work/<attempt_id>/`. `cleanup_workcopy` reclaims that directory, so a timing kept only there would vanish once the film is done.
- **E1. LTX timing** (restore executor).
  - A pure `PhaseTimer` reads each parsed Jasna state (`parse_jasna_progress_line`). It measures, on the monotonic clock, how long Taskpaw saw each phase as the current one:
    - `scan`: the auto split's Mosaic scan (扫描马赛克);
    - `basicvsr`: BasicVSR++ spans;
    - each restore-mode LTX stage by name (`scan`, `encode`, `denoise`, `compose`);
    - `speed_test`: every LTX speed-test line (测速), whatever its stage.
  - A phase's time runs from its first line until another phase begins, a state change names none of them (a new file header or the split note), or the child exits.
  - `ltx` is the four stages plus the speed test. `ltx_runs` counts LTX runs: a restore-mode stage starts a new run unless it follows a lower stage of the same run. The auto split launches one run per span. `wall` is the time the Jasna children ran.
  - It is accumulated over every run and every volume of the film, as `RestoreTiming` (seconds, one decimal).
  - Each applied video's `RestoreFacts.timing` keeps its own copy. The field is optional, so older stage results still load.
  - The film total goes into the restore's success evidence as `summary.timing`. It is omitted when absent, so older evidence is unchanged, and it survives the work-dir reclaim.
  - While the step runs, it carries `timing`: the film so far, with the current phase counted up to now.
- **E2. Film timing** (board, from the store).
  - Each stage's duration is its current succeeded attempt's `ended_at − started_at`.
  - The start is the earliest of those attempts among restore, asr and translate, which is normally the restore attempt (the real processing start). The end is the translate attempt's `ended_at`.
  - Queue wait runs from the plan's `confirmed_at` to the restore attempt's start. It is null for a backfilled plan, or when the result is negative.
  - Rows and the detail carry `timing`: `{stages: {stage: seconds}, restore_breakdown: RestoreTiming | null, total_restore_to_translate, queue_wait}`.
- **E3. UI.**
  - **Drawer.**
    - A compact 「LTX 合计用时」 card shows the total plus the four stage times and 测速 when one ran. It reuses the shared progress parts (`ProgressFrame`, `DATA`). It shows live on the running 修复 step and from `restore_breakdown` once restore has finished.
    - A 「用时」 block lists 修复 (with 扫描马赛克 / BasicVSR++ / LTX), 识别, 翻译, 回传, 整理, then 「修复→翻译合计」, with 「排队等待」 on its own row.
    - The Jasna monitor's own LtxProgress is unchanged.
  - **Step line.** Every finished step that has a duration shows it under its label, small and muted.
  - **Queue card.** Once translate has finished, the foot shows 「全程 h:mm」.
- **E4. Log.**
  - The launcher hands its `TaskLog` to the workflow host and then to the dispatcher. After each resolved attempt, the dispatcher writes a task-log row (task 「媒体处理中心」, task_type `workflow`, `film` = the shown name):
    - `workflow.stage.succeeded` `{stage, duration}`;
    - `workflow.stage.failed` (severity warn) `{stage, duration, detail}`.
  - When translate succeeds, it also writes `workflow.film.translated` `{run, restore, ltx, asr, translate, text}`, where `text` reads 「{film} 修复→翻译完成，全程 {h:mm:ss}（修复 {..}，其中 LTX {..}；识别 {..}；翻译 {..}）」.
  - The task log is persistent, so the summary is too. It is not copied into the film record.
  - The 日志 tab gains these kinds and fields; the times show as clocks.
  - Nothing goes to the Hub event queue, so no OpenClaw notice is sent per stage.

## API appendix (backend as built, U1)

All routes are on the loopback control API under `/control/workflow`, with the existing bearer token. JSON field names are final; the frontend aligns to this section. Labels, toasts and colours stay in the UI.

The UI tests load what these routes return for a realistic runtime: `taskpaw_v3/tests/test_workflow_ui_contract.py` writes `ui/src/test/fixtures/workflow-contract.json` and fails when the committed copy differs. After an intended change, rerun it with `TASKPAW_WRITE_UI_CONTRACT=1` and fix the UI.

### `GET /films` (extended)

- Every film row also has:
  - `number`, `title`, `actors[]`: precedence final NFO > lookup > input NFO. `name` uses the same order: final folder > lookup number > single-film package folder > input NFO number > video stem.
  - `resolution`: `"4K" | "1080p" | null`, from the largest input probe.
  - `has_poster`, `poster_source`: the cover, by the same precedence as the detail's (final > lookup > input), so the queue and review cards show it.
  - `plan`: `{ltx_model: "standard"|"good"|"high"|null, machine: <worker id>}` or `null` while unplanned.
  - `receipt_id`: the newest archive receipt id, or `null`.
  - `revisions`: `{input: {revision, hash} | null, final: {revision, hash} | null}`.
  - `group` gains `"planning"` (待处理): restore pending and no plan for the current input revision.
  - `steps`: the line is `intake, plan, restore, asr, translate, return, scrape, confirm, archive, cleanup, done`. The `plan` step is `waiting` with note `待安排` while unplanned, and `done` with the tier label (`标准` / `良好` / `高`) or `无需修复` once planned. Package rows carry the same keys.
- Top-level additions:
  - `counts`: `{planning, active, attention, confirm, archiving, done, packages_attention, waiting_for_you}`. `waiting_for_you` is `planning + confirm`, for the overview stat 「等待您的确认」.
  - `dispatch`: `{state, reason, paused}` or `null` without a host. `state` is one of `idle`, `running`, `paused`, `stopping_unconfirmed`.
  - `gpu`: `{name, vram_mib, max_output}` or `null`. `max_output` (`"1080p"` / `"4K"`) is the restore executor's own limit (phase 1: `1080p` on every card), never the card's VRAM class; the UI shows 「修复输出上限 …」 from it.
  - A 409 from `POST /films/{id}/confirm` is `{detail}`, plus `code` (`cas_conflict` = the film's revision changed, `precondition_failed`) when the store named one. Offline, permission and duplicate refusals carry no `code`; the UI shows their `detail`.

### `GET /films/{id}` (extended)

It has the row additions above (`resolution`, `plan`, `receipt_id`, `revisions`) and `lookup` (the block below, or `null` when Miko is not configured). The cover (`has_poster`, `poster_source`, `/films/{id}/poster`) follows final > lookup > input. A lookup cover's `poster_source` starts with `lookup-`.

### `GET /plans`

```json
{
  "plans": [{
    "film_id": "…", "input_revision": 1, "revision_changed": false,
    "name": "ABF-394", "title": "…", "number": "ABF-394", "actors": ["…"],
    "has_poster": true, "poster_source": "lookup-…",
    "resolution": "1080p", "width": 1920, "height": 1080,
    "duration_ms": 7200000, "size_bytes": 4294967296,
    "video_count": 1, "item_count": 3,
    "needs_gpu": true, "created_at": "2026-10-21T00:00:00Z",
    "lookup": {"status": "queued|running|succeeded|failed", "number": "…", "title": "…",
               "actors": [], "studio": "…", "release": "…", "runtime_min": 120,
               "has_poster": true, "error_class": "transient|needs_attention|null",
               "message": "未查到资料 | 文件名中没有番号 | …", "can_requery": true},
    "recommendation": {"ltx_model": "high", "ltx_reason": "三上悠亜 上次选择 高",
                       "machine": "<worker id>", "machine_reason": "NVIDIA GeForce RTX 5060 8 GB"}
  }],
  "default_tier": "standard",
  "choices": {
    "tiers": [{"value": "standard", "label": "标准"}, {"value": "good", "label": "良好"}, {"value": "high", "label": "高"}],
    "machines": [{"id": "<worker id>", "name": "本机", "gpu": "…", "vram_mib": 8151, "reason": "…"}]
  }
}
```

- Entries come oldest film first. `lookup` is `null` when Miko is not configured.
- `revision_changed` is true for an input revision above 1. Show the amber notice 「收件内容已更新，请重新查看后确认」.
- `duration_ms` and `size_bytes` are summed over the main videos. `item_count` counts every input file.
- Recommendation reasons:
  - `ltx_reason` is one of `上次选择 {tier}`, `{actor} 上次选择 {tier}`, `默认` or `无需修复`. A `needs_gpu: false` film has `ltx_model: null`.
  - `machine_reason` is the card's name and VRAM, `本机修复输出上限 1080p` for a 4K film, or `本机 GPU 未就绪`.
- `choices` is listed once at the top level, not per entry.

### `POST /plans`

- Body: `{"plans": [{film_id, input_revision, ltx_model, machine}]}`, with 1–500 items. `ltx_model` is `"standard"|"good"|"high"|null`.
  - A bad body is a 422.
  - The same route serves 确认入队 (one item) and 全部按推荐确认 (every shown item).
- Response: `{"results": [{film_id, ok, error, message}]}`, one result per item in order.
  - Each item is its own CAS. Any success wakes the dispatcher.
  - For a film with `needs_gpu: false`, the tier is stored as `null` whatever was sent.
- `error` codes, with the `message` shown for each:

| `error` | `message` |
|---|---|
| `cas_conflict` | 收件内容已更新，请重新查看后确认 (a revision older or newer than the film's current one) |
| `plan_exists` | 已确认，无需重复确认 |
| `precondition_failed` | 当前状态不允许安排 |
| `not_found` | 未知影片 |
| `invalid_machine` | 设备不可用 |
| `tier_required` | 请选择画质 (null tier for a film that needs restoring) |

### `POST /films/{id}/lookup` (重新查询)

- Response: `{"ok": true}`. The card's `lookup.status` then reads `queued` until the worker reruns it.
- Errors:
  - 503: Miko is not configured.
  - 404: unknown film.
  - 409: a lookup is running, the name has no number (`can_requery: false`), or the film already has a final (its NFO names it).
- A lookup cut short by an app stop caches nothing; the film reads `queued` again after the restart.
- The cache `<work>/lookup/<film>-r<N>/` is reclaimed with the film's other work dirs after `cleanup_workcopy`.

### Recommendation history

- The history is every plan the operator confirmed. Backfilled plans (`film_plans.backfilled = 1`, below) are left out.
- A past film's actors are the snapshot stored with its plan (`film_plans.actors`, below), so archiving and cleaning the film (which removes its lookup cache) does not remove it from the history.
- Only a plan with an empty snapshot falls back to the card's precedence for the plan's input revision: final NFO > lookup > input NFO.

### Schema v3 migration (U04)

- `film_plans` has `backfilled INTEGER NOT NULL DEFAULT 0 CHECK(backfilled IN (0,1))`. An operator's confirm writes 0.
- `film_plans` has `actors TEXT NOT NULL DEFAULT '[]'`: a JSON list of the film's actors (NFKC-normalised, trimmed, no blanks or repeats) as `film_actors` read them when the operator confirmed the plan; `[]` when none were known. The upgrade's backfilled rows hold `[]`. Schema v3 is unreleased, so the column is part of the v3 DDL itself; the frozen v2 copy is untouched.
- The v2 → v3 upgrade inserts one `standard` plan with `backfilled = 1` for each film whose current-revision restore is `succeeded`. Its machine is that restore's last attempt worker.
- No other film gets a plan. Pending, interrupted, failed or retrying restores, even with attempts, wait in 待处理 for the operator's tier.

### `POST /pause`

- Body: `{"paused": bool}`. Response: `{"paused": bool}`.
- While paused, nothing new is claimed; in-flight work and the finalizer continue. The flag is held in memory, so a restart resumes dispatch.
- 503 without a dispatcher.

### `GET /films/{id}/target?performer_id=`

```json
{"film_dir": "ABC-123", "performer_id": "…|null", "target_path": "\\nas\…\ABC-123|null",
 "absent": true, "error_code": null, "message": null,
 "candidates": {"query": ["NFO actor"], "items": [{"id", "name", "source_label", "unc_path", "has_thumb", "aliases"}]}}
```

- This is read-only. `candidates` comes from searching Filmpaw for each NFO actor (final > lookup > input), merged by id. The dialog pre-selects only when exactly one candidate comes back.
- Without `performer_id`, only `candidates` is filled.
- `error_code` is one of:
  - `offline`: Filmpaw or its share is unreachable; `candidates` is then `null`.
  - `permission`: Filmpaw answered 403.
  - `missing`: the performer record is gone (404).
  - `duplicate`: the folder already exists; `absent` is false.
  - `null`: none of the above.
- 409: the film has no final package yet. 404: unknown film.

### U1b: the full jacket and the lookup timeout

- `has_cover`, `cover_source` are on every `GET /films` row, on `GET /films/{id}` and on every `GET /plans` entry. `cover_source` is the jacket's cache key (`final-…` / `input-…` + 16 hex of its sha256), like `poster_source`. Both are `false` / `null` when the film has no landscape image.
- `GET /films/{id}/cover` serves the full jacket: the largest landscape image (wider than tall, by pixel area) of the first tier that has one. The tiers are the final's `fanart`, then the final's `thumb`, then the input's `fanart`, `thumb` and image `attachment`. The size comes from the file header (JPEG, PNG, GIF, BMP, WebP). Input files are read only from their intake work copy (`workcopy_path`), never from the INBOX; final files are read from the final package. The guard is the poster route's: bearer token, control API only, 503 without a running host. It returns 404 「没有完整封面」 when there is no jacket, and 404 for an unknown film.
- The lookup's portrait `poster.jpg` is not a jacket. For a film that has only that, the lightbox shows the poster.
- The `lookup_one` hard timeout (`lookup.TIMEOUT_SECONDS`) is 300 s. It sits above MDCX's own 270 s budget (`MDCX_BUDGET_SECONDS`).
