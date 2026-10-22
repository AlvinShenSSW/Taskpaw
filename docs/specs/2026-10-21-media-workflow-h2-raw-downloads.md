# H2: accept raw downloads in the INBOX; show stuck packages

- Parent: Taskpaw EPIC #271, phase 1, first real-machine finding (2026-10-21).
- Base: `66d2873` (integration tip of PR #282). Branch `afk/claude-h2-271`.
- Principle P0: single user, home LAN. Use the smallest change that lets an unscraped download go straight in.

## Finding

The operator dropped two freshly downloaded films into `D:\TODO`. Each one was its own folder:

```text
(HD1080P)(Prestige)(ABF-393.67zhf89d)究極のぬるぬるオーガズム 河合あすな/
  4K688.com@ABF-393.mp4      5.6 GB
  ABF-393.jpg                cover from the download site
  新建文本文档.txt            site advert
```

Both packages stopped at intake:

- Stored as `needs_attention / ambiguous_grouping`.
- The grouping itself recorded `unknown_attachment`, for the `.txt` file.
- No film was created, so the 媒体入库 page, which only lists films, showed nothing at all.

Even with the `.txt` removed, the `.jpg` would have failed later. Grouping classifies it as role `attachment`, and `film_store._check_attachments` rejects any attachment whose suffix is not in `FilmPolicy.allowed_attachment_extensions`, which is empty.

## Contract

- **R1 — download extras are attachments.**
  - The default intake policy and the default film policy (local_host `film_policy()`, `configure_intake`) both allow one shared list of common download extras: `.txt .url .html .htm .mht .jpg .jpeg .png .gif .webp .bmp .lnk .torrent`.
  - Attachments never reach Miko and never reach the NAS. The scrape step stages only videos and subtitles, and the final manifest is Miko's output.
  - Attachments are input members, so the input cleanup deletes them with the original. The package folder is then left empty and removed.
  - Other unknown files still stop the package, and the stop is visible (R2).
- **R4 — the page is called 「媒体处理中心」** (operator, 2026-10-21). It replaces 「媒体入库」 everywhere: tab label, view title, i18n zh and en ("Media Center").
- **R5 — the whole lifecycle is observable** (operator: "按照我说的整个生命周期我都可以监察到").
  - Every item is visible from the moment its folder is dropped into the INBOX until its cleanup finishes. Finished films stay in a 已完成 list (most recent first).
  - Each row and the drawer show the lifecycle as one fixed step line: 入库扫描 → 修复 → 识别 → 翻译 → 回传 → 整理（Miko）→ 待确认 → 归档 → 清理 → 完成.
  - Each step shows done / running (with progress when the executor reports it) / waiting (with reason) / failed (with reason) / skipped.
  - The current step is highlighted.
  - A package that is not yet a film is shown at 入库扫描, with its intake state (R2a/R2).
  - 待确认 is the operator's one-click step.
  - 清理 combines cleanup_workcopy and cleanup_input, showing kept files when there are any.
- **R2a — packages in intake are visible (operator, 2026-10-21: "在媒体入库里一样看不到").**
  - Every current INBOX package that is not yet a film appears in 处理中 with a readable state, e.g. 等待文件稳定, 正在复制到工作区, 识别中.
  - This covers states such as receiving, waiting_stability and snapshotting, which the same store read returns.
  - This applies from the moment a folder is dropped in, so the page is never empty while something is happening.
- **R2 — stuck packages are visible.**
  - A store read lists the current INBOX packages that have not become films and are stuck. That means states `needs_attention` and `blocked`, plus `receiving` while a member is temporary.
  - For each package it returns: the folder name, the state, the grouping reasons, and the relative paths of the unassigned members.
  - The 媒体入库 list shows each stuck package in 需处理, with a readable Chinese reason and the offending file names, for example "有无法识别的文件：新建文本文档.txt".
  - Package rows open no drawer. Their only action is the existing "打开文件夹".
- **R3 — packages already stuck re-evaluate.** When the root policy changes, packages already stuck under the old policy must be observed again and must become films without any operator action. The two packages in `D:\TODO` are the acceptance case.

## Tests

- **Grouping:** the observed real package shape (video with a site prefix, `<number>.jpg`, advert `.txt`) groups as ready, with the image and the txt as attachments.
- **Store/e2e:** a package with an extra `.txt` becomes a film, and its input cleanup deletes the txt too.
- **Board:** a stuck package (an `.exe` file, for example) appears in 需处理 with its reason and file name.
- **Reconfigure:** a package stored as stuck under the old empty list is re-observed after the policy change and becomes a film.

## Out of scope

Any other naming heuristics, and Miko's number parsing of `4K688.com@ABF-393`, which is the real-machine check that comes next.

## v2 (debate round 1, D01–D11; overrides the text above)

- **R1 (D02, D03, D10).**
  - `DOWNLOAD_EXTRAS` lives in `local_host` and is the single owner of the list.
  - `configure_intake` uses `IntakePolicy(policy_revision=1, allowed_attachment_extensions=DOWNLOAD_EXTRAS)` as the default, and builds the film policy from the same tuple: `film_policy(extensions)`. The model defaults stay empty, so the store/manifest tests are unchanged.
  - Add a test that `_IMAGE ⊆ DOWNLOAD_EXTRAS`, because a plain image is always an attachment.
  - Live case: `河合あすな/ABF-393…` is already `blocked/publication_blocked` because of `ABF-393.jpg`, and ABF-394 will follow. After the fix and an agent restart, `configure_intake_root` resets them, a new snapshot runs, and both become films.
- **R3 (D01, P1).** `intake_discovery._group` may reuse the previous grouping only when the root configuration is unchanged (`previous.root_config_revision == config.expected_version + 1`); otherwise it re-groups. `previous_keys` are still reused, so film ids stay stable.
- **R2 / R2a (D04, D05, D06).**
  - **Store read.** One new read `list_intake_board(auth)` in intake_store, operator only. It selects `current=1`, `state NOT IN ('missing','operational_ready')`, and `NOT EXISTS intake_films` for the package. It returns: folder path, state, reason, `grouping.reasons`, and the unassigned member paths (taken from the observation's members).
  - **`/control/workflow/films`.** The response gains `packages: [{package_id, name, state, reason, label, group, files}]`. The group is `active` for receiving (incl. temporary_member = download in progress), waiting_stability, snapshotting, waiting_capacity, and blocked/changed. It is `attention` for needs_attention, other blocked reasons, and unknown_owner. Labels follow the D06 table.
  - **Open folder.** New route `POST /control/workflow/packages/{package_id}/open`, current package only, with the folder resolved under the INBOX.
  - **UI.** The UI renders package rows in their group, with the label and file names. "No items" counts both films and packages.
- **R4.** Rename the page to 「媒体处理中心」 / "Media Center".
- **R5. Lifecycle step line, computed in `board.py` from existing data.**
  - **Shape.** Each film row gets `steps: [{key, state, note, progress}]`.
    - `key` ∈ intake, restore, asr, translate, return, scrape, confirm, archive, cleanup, done.
    - `state` ∈ done | running | waiting | failed | skipped | pending.
  - **intake.**
    - Film: done.
    - Package: running, waiting or failed per the package group, with the label as the note.
  - **restore … scrape.**
    - The state comes from the stage status: succeeded → done, skipped → skipped, running → running, failed/blocked → failed, unknown → waiting.
    - The note comes from the stage note or the host waiting reason.
    - Progress comes from the host `current` when it is that film and stage.
  - **confirm.**
    - waiting ("等你确认入库") when can_confirm.
    - done once an approval is active or the archive started or succeeded.
    - waiting with the archive error after a failed archive.
  - **archive.** The state comes from the archive stage status, plus `archive_open` / `archive_error` / the waiting reason.
  - **cleanup.**
    - done when both cleanup stages succeeded.
    - failed/waiting when one is unknown/blocked, with the kept-files note.
    - running otherwise, once the archive has succeeded.
  - **done.** done when both cleanups succeeded.
  - **Packages.** A package row shows the same line, with only intake active.
  - **UI.** A compact step line in each row and a detailed vertical stepper in the drawer, with the current step highlighted.
- **Notes.**
  - D07/D08: `missing` is hidden. Rescans of needs_attention bump generations harmlessly.
  - D09: labels exist for subtitle_language_unknown, invalid_nfo, and desktop.ini/Thumbs.db as unknown_attachment. Making those files acceptable is out of scope.
  - D11: an existing film keeps its stored empty list; the live DB has 0 films.

### v2 addendum (critique of R4/R5)

- **G1:** `host.status()` gains an `intake` field, `{package_id, phase, bytes_completed, total}`, read from the intake runner/owner. The 入库扫描 step shows copy/verify progress.
- **G4:** `BoardStage` gains the stage's own `reason`, taken from `stages.body_json`. Blocked/skipped stages show why.
- **G5:** step states are done | running | waiting | failed | pending (未开始). Stage `unknown` maps to waiting ("等待确认停止") and `blocked` to failed.
- **G3:** a "no work" outcome (reuse, nothing to translate) shows as done with its note. It is never parsed into skipped.
- **G2:** progress for 回传/整理 is optional; they show 进行中.
- **已完成:** shows the 20 most recent finished films.

## Operator decisions (2026-10-21)

- **Target architecture.**
  - The Windows TaskPaw **Hub** is the controller. It runs scheduling, intake, translate, return, Miko scrape, confirm, archive and cleanup, and only the Hub has the 「媒体处理中心」 tab.
  - **Agents** run only the GPU stages (restore/LTX, ASR/WhisperJAV). Each shows a new system monitor 「媒体处理」 listing its claimed tasks, steps and progress. Ordinary agents have no media tab. The Hub machine itself does not run Jasna in the live environment.
  - Sequence: H2 → Hub controller (Windows Hub installer; controller moved into the Hub process; tab in the Hub UI) → Hub↔Agent claim protocol for the GPU stages.
- **Transition.** Until the Hub version ships, the 「媒体处理中心」 tab stays in this machine's Agent console so phase 1 can be validated on the real machine. It is removed when the Hub version lands and is replaced there by the 「媒体处理」 monitor.

## Frozen API additions (H2)

```text
GET /control/workflow/films
  200 {state, reason, current,
       intake: {package_id, phase, bytes_completed, total} | null,
       films:    [FilmRow + steps: [Step]],
       packages: [{package_id, name, state, reason, grouping_reasons: [code], files: [path],
                   group: "active" | "attention", steps: [Step]}]}
  Step = {key, state, note, progress}
    key   ∈ intake | restore | asr | translate | return | scrape | confirm | archive | cleanup | done
    state ∈ done | running | waiting | failed | pending
  The backend sends codes (package state/reason/grouping reasons); the UI owns the zh/en labels.
GET  /control/workflow/films/{film_id}   detail gains steps: [Step]
POST /control/workflow/packages/{package_id}/open   204 | 404 | 501   (current package only, folder under INBOX)
```
