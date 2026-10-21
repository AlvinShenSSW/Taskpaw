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
- **U04. Existing films:**
  - The migration backfills a default-tier plan only for current-revision restore jobs that already have an attempt.
  - Never-claimed restores land in 待处理. On the operator's machine that is ABF-393 and ABF-394; this is intended.
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
