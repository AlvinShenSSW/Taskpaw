# #208 — Jasna 「8K VR」 tickbox: one tick applies the 8K VR profile at launch, untick restores the operator's own settings; version 3.9.6

Date: 2026-09-30 (design v4, FROZEN — round 4 CLEAN; round 1: C1–C13 resolved in v2 (revalidated by name in round 2); round 2: N1–N6 resolved in v3 (revalidated in round 3); round 3: R1–R2 (P2, refuted mechanics of the N1 fix) + R3–R4 (minor) resolved in v4)
Issue: #208. Owner: "更新jasna的配置，可以勾上8KVR，勾上后所有配置都根据8K VR的配置来，保存即可，如果取消勾选则回到之前的".
Driver: `/afk` — Claude (Fable) leads; implementation Claude Opus (subagent); outer gate Codex gpt-6-astra (high); final gate Kimi (config `gates: codex > kimi`, afk-skills 1.3.0).
Merge when AFK merge-ready, then release 3.9.6.

## Spec review

Today a Jasna task has two resolution tiers (1080p / 4K by pixel count, `tier_for`) and the operator sets the detector, the 4K clip size, the overlap and the unet-4x tickboxes by hand. An 8K VR (SBS VR180) film needs a different setup from a 2D film: Jasna 0.10.0's bundled VR180 detector `rfdetr-vr-v1`, a forced `--vr-mode sbs`, a smaller clip, and — on the owner's 8 GB GPU — no unet-4x. Switching a task between 2D and VR today means retyping 4 fields and retyping the old values back afterwards; the owner did it by hand on 2026-09-29 and found it easy to get half of it wrong.

Core need: **one tickbox** on the Jasna form. Ticked → every launch uses the 8K VR profile. Unticked → the launches use the operator's own fields again. The operator's own values are never overwritten by the tick.

Verified facts (this machine, 2026-09-30, plus the critic's read of upstream Kruk2/jasna v0.10.0 `jasna/main.py`):

- `C:\Jasna\jasna.exe --help` (0.10.0): `--vr-mode {auto,off,sbs,sbs-fisheye}` (default `auto`, main.py:419-423); `--detection-model` names installed models discovered from `model_weights/` — `rfdetr-v6` and `rfdetr-vr-v1` are bundled (main.py:376-385), `rfdetr-v6-large` and `zelefans-vr-yolo-v2` are optional downloads; RF-DETR weights are loaded from `model_weights/<name>.onnx` and a missing file raises `FileNotFoundError` (main.py:719-731); argparse keeps `allow_abbrev` (main.py:142). `rfdetr-vr-v1.onnx` is present in `C:\Jasna\model_weights`. No exclusion between `--vr-mode` and `--detection-model`.
- Upstream `docs/en/vr180.md`: `--vr-mode auto` already treats an exact 2:1 frame taller than 1080 as SBS; `sbs` forces it for every file.
- `taskpaw_v3/monitors/plugins/jasna.py`: `build_argv()` (~1030) is pure and builds the whole argv from `JasnaConfig` + tier + `unet_enabled` + `large_detector`; `_OWNED_FLAGS` (~185) are rejected in `jasna_extra_args` by prefix (`owned_flags_in` / `_selects`, ~770-805 — a longer flag such as `--detection-model-path` is NOT selected by `--detection-model`); the 4K tier auto-upgrades `rfdetr-v6` → `rfdetr-v6-large` when those weights exist; `_launch_locked` (~1835) derives `unet` from the tier tickbox, the per-run degrade flag and the retry flag, and `_current_unet` additionally excludes an extra-args `--secondary-restoration` override; `_tier_suffix()` (~3464) writes ` [4K 8192x4096, unet-4x]` into the running detail; `restore.started` logs `mode`. Nothing else consumes `build_argv`, `_OWNED_FLAGS` or the tier suffix (git grep: jasna.py and its tests only).
- `admin.py:236-266` `update` → `supervisor.reconfigure` (supervisor.py:163-208) stops the old instance (kills the running jasna.exe) and starts a new run at once — **saving any config edit on a running task restarts the current film from 0** (pre-existing behaviour).
- `taskpaw_v3/ui/src/components/SchemaForm.tsx` + `views/MonitorWizard.tsx`: the plugin's `json_schema`/`ui_schema` drive an rjsf form; the wizard mirrors the live form data through `onChange` (`liveFormData`, seeded at MonitorWizard.tsx:53 for edit and :68 `enterConfig` for add) and renders the #204 FFmpeg reminder through a React **context**, because any change to an rjsf `Form` prop rebuilds the form state from `props.formData` and drops unsaved edits — verified in `@rjsf/core` 5.24.13 `Form.js:52-68` `getSnapshotBeforeUpdate` (`if (!deepEquals(this.props, prevProps)) { nextState = this.getStateFromProps(this.props, this.props.formData, …) }`) and `componentDidUpdate` (84-93). So a **dynamic `uiSchema` (`ui:readonly` toggled by the tick) would revert the tick itself**; the lock must travel by context, like the reminder.
- Critic experiment (jsdom 25 / MUI 6.5 / rjsf 5.24.13, 2026-09-30): an rjsf `Form` with a context-driven `ObjectFieldTemplate` keeps a typed `name` and the tick across the lock; the stored `45` is submitted while locked; untick re-enables the inputs with `45` still there. In jsdom `toBeDisabled()` holds for MUI inputs, but `fireEvent.click`/`fireEvent.change` still fire on a disabled control — tests must assert non-interactivity with `toBeDisabled()`, never with `fireEvent`.
- `ObjectFieldTemplate.tsx` renders each property's `content` in a two-column grid (width rules only, lines 12-24); booleans span the full row.
- `TaskLog.helpers.ts:42-47` `FIELDS` is a **whitelist**: an unknown data key is dropped silently; a shown key needs `logs.fields.<key>` in zh and en (`i18n.ts`).
- The add-mode review step (MonitorWizard.tsx:275-283) lists every non-boolean field; booleans are never shown.
- Pinned orders in tests: `test_jasna.py:204` counts 19 own fields and requires every own field to have a `description`; `test_jasna.py:1513-1520` pins `ui:order[:6]` (`…, jasna_output_folder, unet4x_1080p, unet4x_4k`); `test_catalog.py:323-324` (`av_translate` right after `unet4x_4k`); `test_jasna_subs.py:411-418` (`unet4x_4k`, `av_translate`, the three whisperjav fields, in that order).

Owner evidence 2026-09-30: an 8192x4096 VR film with `rfdetr-vr-v1`, clip 45 and unet-4x ON ran at 0.3 fps on an RTX 5060 8 GB (64 GB RAM) — consistent with VRAM spilling to system memory, which Jasna does not report as a failure, so the outcome-based degrade cannot catch it. #208's own rule for that case is "off if it turns out to spill VRAM" (C1).

## Frozen issue contract

### AC1 The profile and the tick (backend)

`JasnaConfig` gets `vr_8k: bool = False` (title `8K VR`, with an English `description`: "Treat every file of this task as 8K SBS VR: launch with the VR180 detector rfdetr-vr-v1, --vr-mode sbs, 4K-tier clip 30, overlap 8 and the 4K-tier unet-4x off. The fields it overrides keep their saved values and apply again when unticked. 2D films belong in another task. Saving this on a running task restarts the current film."). In `ui:order` it sits **directly before `clip_size_1080p`** (after `whisperjav_extra_args`), so the pinned orders above stay green and the switch sits next to the fields it locks (C4). `test_jasna.py:204` changes 19 → 20 (named here: the only existing assertion this design changes).

The 8K VR profile is a module-level constant in `jasna.py`:

| Launch flag | Profile value | Applies to |
|---|---|---|
| `--detection-model` | `rfdetr-vr-v1` | every tier; the `rfdetr-v6-large` auto-upgrade never fires |
| `--vr-mode` | `sbs` | every tier (appended once, right after `--detection-model`, before the operator extra args) |
| `--temporal-overlap` | `8` | every tier (C3) |
| `--max-clip-size` | `30` | 4K tier only; the 1080p tier keeps `clip_size_1080p` |
| unet-4x, 4K tier | **off** (`--secondary-restoration none`) | 4K tier only; the 1080p tier keeps `unet4x_1080p` (C1) |

`build_argv(cfg, …)` reads `cfg.vr_8k`; when true it substitutes those values for `cfg.detection_model` / `cfg.temporal_overlap` / `cfg.clip_size_4k`, emits `--vr-mode sbs`, and ignores `large_detector`. `_launch_locked` computes `unet` as today but with the 4K-tier tickbox read as False under the tick (`tickbox = False if (tier == "4k" and cfg.vr_8k) else …`), so a 4K-tier launch is never a unet-4x launch: `_current_unet` is False, the retry-without-unet / degrade path cannot fire, and `_run_unet_disabled` is never set for the 4K tier (issue acceptance 4). The 1080p tier is unchanged. Everything else in the argv is unchanged: `--codec`, `--cq` still come from the config. The documented `--secondary-restoration unet-4x` in the extra args stays the escape hatch for a GPU that fits it (argparse last-wins; it disables the degrade as today and applies to every file of the task).

The operator's own field values (`unet4x_4k`, `detection_model`, `clip_size_4k`, `temporal_overlap`, …) are **never rewritten** by the tick: the profile is applied at launch time only. Untick → the next launch uses the stored values again. Nothing is stashed, so there is nothing to restore.

Validation (C3): the existing rule `2*temporal_overlap < min(clip_size_1080p, clip_size_4k)` stays (the stored values must remain valid for untick). Under the tick one more rule: `2*8 < clip_size_1080p` (i.e. `clip_size_1080p > 16`, the 1080p launch's own clip with the profile overlap; the 4K launch is 30/8 and always valid) — error text: "8K VR uses a temporal overlap of 8, so clip_size_1080p must be larger than 16".

Tests (`test_jasna.py`):
- `build_argv` with `vr_8k=True`, 4K tier: `--detection-model rfdetr-vr-v1`, `--vr-mode sbs` right after it, `--max-clip-size 30`, `--temporal-overlap 8`; with `large_detector=True` the detector stays `rfdetr-vr-v1`; operator extra args still come last.
- `build_argv` with `vr_8k=True`, 1080p tier: `rfdetr-vr-v1` + `--vr-mode sbs` + `--temporal-overlap 8`, but `--max-clip-size` = `clip_size_1080p`.
- `build_argv` with `vr_8k=False` is byte-for-byte what it was (the three existing argv tests stay untouched).
- Instance test (fake Popen, existing fixtures): a 4K-tier launch of a ticked task with `unet4x_4k=True` passes `--secondary-restoration none`, logs `mode: plain`, and a failed launch takes the plain retry path — `_run_unet_disabled` stays empty and no degrade alert is emitted; a 1080p-tier launch of the same task still honours `unet4x_1080p`.
- Config: `vr_8k=True` with `clip_size_1080p=16` is rejected; `clip_size_1080p=17` accepted; a config saved with `vr_8k=True, clip_size_4k=45, detection_model="rfdetr-v6", unet4x_4k=True` still carries those values, and with `vr_8k=False` launches with them.

### AC2 Extra args under the tick

While `vr_8k` is ticked, `jasna_extra_args` must not set `--vr-mode` or `--detection-model-path` (C10: the latter would silently swap the pinned detector's weights): both join the owned-flag rejection (exact name, `--flag=…`, and argparse abbreviations via the existing `_selects`) with the existing error text ("jasna_extra_args must not set the flags TaskPaw owns (…)"). With the tick off, both are accepted as today (an operator may force `sbs-fisheye` by hand). `--secondary-restoration` keeps its documented override semantics under the tick. Keep `_OWNED_FLAGS` for the unconditional flags; the conditional pair lives in the validator (`owned_flags_in(extra, vr_8k=…)` or a second tuple `_VR8K_OWNED_FLAGS`).

Help texts (C12): the `jasna_extra_args` description (en in jasna.py, zh in `schemaI18n.ts`) gains "while 8K VR is ticked, `--vr-mode` and `--detection-model-path` are rejected too"; the `detection_model` description gains "ignored while 8K VR is ticked (rfdetr-vr-v1 is used)"; `unet4x_4k`, `clip_size_4k` and `temporal_overlap` gain one clause each ("8K VR overrides this at launch").

Tests: `--vr-mode sbs-fisheye`, `--vr-m off`, `--vr-mode=off`, `--detection-model-path x.onnx` rejected when ticked; accepted when unticked; `--detection-model` still rejected either way. Assert on the "TaskPaw owns" error text, not on an exact owned-flag list: under the tick `--detection-model` is also a prefix of `--detection-model-path` and is reported for both (N9, cosmetic).

### AC3 Status and log

`_tier_suffix()` inserts `8K VR` after the dims when `cfg.vr_8k`: ` [4K 8192x4096, 8K VR, unet-4x off]`. When the extra args carry the documented `--secondary-restoration` override (`secondary_overridden`), the last part reads `secondary via extra args` instead of `unet-4x off` (N3: the override is now the documented unet-4x route for a ticked task, and `_current_unet` is False on that path by design — jasna.py:1878 — so the old text would claim unet-4x is off while it runs). This applies to unticked tasks too (same code path; the existing `test_detail_shows_the_tier_and_unet_state` is unaffected because it sets no extra args). The `restore.started` task-log entry gets `"profile": "8k-vr"` in its `data` only when ticked (absent otherwise). `TaskLog.helpers.ts` `FIELDS` gets `profile`, and `i18n.ts` gets `logs.fields.profile` (zh 「配置」/ en "profile") (C11). The film list / 本轮影片 card and the Hub `status.md` need no change (they never showed the tier).

Tests: the existing `test_detail_shows_the_tier_and_unet_state` gets a ticked sibling and an override sibling (`secondary via extra args`); a `restore.started` record carries `profile` only when ticked; a TaskLog helper test shows the `profile` field.

### AC4 The form (frontend)

The wizard (add and edit) shows the `8K VR` switch like every other boolean. While it is ticked:

- The four locked fields `unet4x_4k`, `detection_model`, `clip_size_4k`, `temporal_overlap` render **greyed, non-interactive, showing the profile values** (C2): a disabled MUI `TextField` with the localized field label, the localized field description as `helperText` (N5: so the C12 "8K VR overrides this at launch" clause is visible exactly when it applies), and the value `rfdetr-vr-v1` / `30` / `8`; and a disabled MUI `Checkbox` + `FormControlLabel` (unchecked; rjsf-mui renders booleans as a Checkbox, N5) with the `unet4x_4k` label and description. The real rjsf field (`el.content`) stays **mounted but hidden** (`display: none`) in the same cell, so rjsf keeps the operator's stored value and submits it untouched. The display control is decorative: MUI `disabled` (its own greyed styling; no `<fieldset>`), `tabIndex -1`, its own `id` (`locked_<name>`, never rjsf's `root_*`).
- **A locked field whose live value is invalid is not hidden** (N1): HTML5 constraint validation is on (rjsf `noHtml5Validate` defaults to false; rjsf-mui passes the schema `minimum` to the native input), and a `display:none` invalid input makes the browser block submit silently ("not focusable") — the critic reproduced it in jsdom (`checkValidity()` false, no `onSubmit`/`onError`, no visible error). So `ObjectFieldTemplate` shows the real field (the browser's own validation message appears on that visible field on submit; rjsf's inline error only after HTML5 validation passes) instead of the display control when `props.errorSchema?.[name]?.__errors?.length` or the live value is present and invalid: `const v = props.formData?.[name]; v !== undefined && !props.registry.schemaUtils.getValidator().isValid(fieldSchema, v, props.registry.rootSchema)` (R1: `isValid` lives on `ValidatorType`, reached through `schemaUtils.getValidator()` — `@rjsf/utils` 5.24.13 types.d.ts:873; `SchemaUtilsType` has no `isValid`. R3: an `undefined` value — a cleared number field — counts as valid for the lock, so the display control shows; the submit omits the key and the backend keeps the stored value on edit / applies the default on add). `props.formData`, `props.errorSchema` and `props.registry.rootSchema` are on `ObjectFieldTemplateProps` (types.d.ts:621/623/334). The hint still shows. Stored values are always valid (pydantic `ge` = JSON-schema `minimum`), so this only affects an unsaved typed value.
- A full-width hint (caption, `text.secondary`, like the FFmpeg reminder's neutral row) directly under the `8K VR` switch: zh 「已勾选 8K VR：这个任务的所有文件都按 SBS VR 处理（2D 影片请放到另一个任务）。启动时固定使用 rfdetr-vr-v1 检测模型、--vr-mode sbs、时序重叠 8、4K 档片段长度 30、4K 档不用 unet-4x。灰显字段里保存的原值不会改变，取消勾选后恢复生效。运行中的任务保存后会从头重跑当前影片。」 and an English equivalent (C8, C9).
- Untick → the four real fields render again with the values they had (nothing was changed); the hint is gone.

Mechanics (the #204 pattern, because a `uiSchema` change would reset the form — see Spec review):

- `SchemaForm` accepts a new optional prop `locked?: { after: string; note: ReactNode; fields: Record<string, { value: string | number | boolean }> }` (R4: the backend profile carries numbers) and provides it through a **context** (`LockedFieldsContext`), next to `ReminderContext`. The rjsf `Form` props stay exactly as they are (schema/uiSchema/widgets/templates/formData unchanged).
- `ObjectFieldTemplate` consumes the context: for a property whose name is in `fields`, the cell renders the display control first (label and description from the localized schema — `props.schema.properties[name].title` / `.description` — boolean → disabled `Checkbox` + `FormControlLabel` with the description in a `FormHelperText` beneath (N5 note: `FormControlLabel` has no `helperText`), else disabled `TextField` with `helperText`) and then `el.content` **always inside the same `Box`**, toggling only that Box's `display` (N2: a stable structure keeps the rjsf input's DOM node across lock/unlock — verified by the critic; a conditional wrapper would remount it); after the property named by `after` it renders `note` in a full-row cell. Without a context value the template is unchanged. The width rule (`spansFull`) is unchanged.
- `MonitorWizard` passes `locked` only for `type_id === "jasna"` and `liveFormData.vr_8k === true` (add and edit: the seeded `liveFormData`), mirroring the FFmpeg reminder condition. The profile values have **one source, the backend** (N4): the plugin's static `ui_schema` carries `"vr_8k": {"ui:options": {"taskpawProfile": {"unet4x_4k": false, "detection_model": "rfdetr-vr-v1", "clip_size_4k": 30, "temporal_overlap": 8}}}` built from the same `_VR8K_*` constants `build_argv` uses (a pytest pins that the ui_schema profile equals the constants); the wizard reads `plugin.ui_schema?.vr_8k?.["ui:options"]?.taskpawProfile` with optional chaining (R4: an older agent's catalog has no `vr_8k`; absent → no lock, no review rows, never a throw) — static per plugin, so it can never change a Form prop mid-edit — and derives `locked.fields` from its keys. The hint and the review rows interpolate their values from that same object (`{{detection}}`, `{{clip}}`, `{{overlap}}` i18n params), so the frontend carries no copy of the profile; the backend `vr_8k` description is f-stringed from the `_VR8K_*` constants. README/CHANGELOG state the values in prose (docs, not code). The wizard test fixture carries the same `ui:options`.
- Add-mode review step (C6, N6): when `formData.vr_8k === true`, a row 「8K VR」 / "8K VR" with value 「已勾选（4K 档不用 unet-4x）」 / "on (4K tier without unet-4x)" is listed first after the type row (booleans are otherwise filtered out, so this row is how the unet override is shown), and the three overridden non-boolean rows show `<stored> → <profile>` only when the values differ (`60 → 30`, `rfdetr-v6 → rfdetr-vr-v1`; an equal pair shows the value once). The arrow and values are language-neutral; only the row label/value need `wizard.*` keys.
- Save is the ordinary submit: rjsf submits the operator's stored values untouched plus `vr_8k`. No extra button.
- i18n: `schemaI18n.ts` `jasna.vr_8k` title/description (zh); the hint and the review-row strings under `wizard.*` in `i18n.ts` (zh + en); `logs.fields.profile`.

Tests (`wizard.test.tsx`, jasna fixture extended with `vr_8k`, `unet4x_4k`, `detection_model`, `clip_size_4k`, `temporal_overlap` and the `ui:order` of the real plugin):
- add mode: type a name, tick `8K VR` → the typed name survives (the #204 N3-1 regression: no form reset), the four display controls are `toBeDisabled()` and show `rfdetr-vr-v1` / `30` / `8` / unchecked, the hint is visible; untick → the four real inputs are enabled again and still show their values (`60`, `rfdetr-v6`, …); the hint is gone. Queries use `getByRole("textbox"|"checkbox", {name})` or `within(cell)` — `getByLabelText` would match both the display control and the hidden real input (N2); the hidden cell is asserted with `not.toBeVisible()`.
- add mode: type `5` into `clip_size_4k` (minimum 8), tick `8K VR` → the real `clip_size_4k` input stays visible and `toBeInvalid()` (no display control for it), the other three are locked; clicking Review does not advance to step 3 and `addMonitor` is not called (R2: HTML5 constraint validation blocks the submit before rjsf validates, so no rjsf inline text appears in jsdom; in a browser the native bubble points at the now-visible field). Never turn `noHtml5Validate` on to make an inline error appear — that changes every form.
- edit mode with `vr_8k: true`, `clip_size_4k: 45`, `unet4x_4k: true` saved: the lock and hint show on open; the display shows `30` and off; Save submits `clip_size_4k: 45`, `unet4x_4k: true`, `vr_8k: true` unchanged.
- add mode review step with the tick on: the 「8K VR」 row and `60 → 30` are shown; with the tick off neither is.
- the hint is not rendered for a non-jasna plugin.
- `schemai18n.test.tsx`: explicit assertions for `jasna.vr_8k` zh title/description; `i18n` test for the hint key and the review-row strings in zh and en (C5).
- backend: `test_jasna.py` pins `ui_schema()["vr_8k"]["ui:options"]["taskpawProfile"]` to the `_VR8K_*` constants (N4).

### AC5 Preflight alert (failure path)

On `start()` of a managed Jasna with `vr_8k` ticked, if `<exe dir>/model_weights/rfdetr-vr-v1.onnx` is not a file and the extra args do not carry `--detection-model-path` (impossible under AC2, so simply: if the file is missing), emit **one** alert (dedupe key `<instance>:vr8k:weights`): "8K VR: rfdetr-vr-v1.onnx is not in Jasna's model_weights folder — Jasna 0.10.0 or newer bundles it. Jasna cannot load the VR detector, so each file will fail until it is installed." The run still starts (a launch failure then takes the ordinary failed-file path; after 3 consecutive failures the existing abort alert fires). No alert when the file exists, when unticked, or in passive mode. Helper: `vr_detector_available(exe_dir)` beside `large_detector_available`.

Tests: alert once when missing; no alert when present; no alert when unticked.

### AC6 Docs and version

- README: the Jasna table row mentions 「8K VR」; a short section after the FFmpeg section: what the tick does, the profile (five values), that the four fields keep their saved values and apply again when unticked, that every file of a ticked task is processed as SBS VR (2D films go in another task), that saving on a running task restarts the current film, the `--secondary-restoration unet-4x` extra-args escape hatch for a bigger GPU (the detail line then says `secondary via extra args`), and that 8K **H.264** sources decode on the CPU (NVDEC caps H.264 at 4K) and should be remuxed to HEVC first.
- CHANGELOG: `## V3 3.9.6 — Jasna「8K VR」一键配置（#208）` in the existing zh style.
- Version 3.9.5 → 3.9.6 in the six files (`taskpaw_v3/__init__.py`, `src-tauri/tauri.conf.json`, `Cargo.toml`, `Cargo.lock`, `ui/package.json`, `ui/package-lock.json`); `test_version.py` pins them.

### Invariants
- Managed Jasna never auto-starts at boot; `manual_start` unchanged. No `shell=True`; argv stays a list.
- `build_argv` stays pure; the operator's stored config is never mutated by a launch.
- No secrets, no real film codes in tests/docs (use `a.mp4`-style names).
- Frontend follows `design-system/taskpaw-v3/MASTER.md` (tokens from `theme.ts`; caption style for the note).
- No existing test assertion changes except `test_jasna.py:204` (19 → 20).

### Non-goals / out of scope
- No per-file VR detection (the tick is task-wide: every file of that task is treated as SBS VR — stated in the hint, the description and the README).
- No `sbs-fisheye` / `auto` choice in the UI (an operator can still pass `--vr-mode` in extra args with the tick off).
- No 8K tier: 8K files stay in the 4K tier by pixel count.
- No change to Lada, to the degrade rule for unticked tasks, or to the 1080p tier.
- No save-time warning for a running task (pre-existing restart behaviour; documented instead).

## Assumptions (unverified claims, with risk)
- A1 — Jasna accepts `--vr-mode sbs` together with `--detection-model rfdetr-vr-v1` in one launch: supported by the upstream argparse source (no exclusion), not executed. Risk: low; a rejection would surface as a launch failure with Jasna's stderr in capture mode.
- A2 — clip 30 / overlap 8 / no unet-4x fits 8 GB for 8K SBS. Unbenchmarked; conservative estimate from the owner's 45-frame run. Risk: performance only; the operator can untick and tune by hand.
- A3 — MUI `disabled` on the decorative display controls is the browser guarantee of non-interactivity (the real field is hidden); jsdom's `toBeDisabled()` is what the tests assert (critic experiment: `fireEvent` bypasses `disabled` in jsdom, so tests never use it for this).

## Approach (why)
Launch-time override + context-driven lock with decorative display controls. Alternatives rejected: (a) writing the profile values into the stored fields on tick and stashing the old ones — the stash would have to live in agent.yaml or the browser, and a save with the tick on would still overwrite the operator's values; (b) dynamic `ui:readonly` — resets the rjsf form (verified above); (c) hiding the fields under the tick — the issue asks for greyed, visible fields; (d) `<fieldset disabled>` around the real field showing the stored value — the issue asks for the profile values, and a remounting wrapper would drop the rjsf input node (critic experiment).

## Files to change

| Path | Change | Reason |
|---|---|---|
| `taskpaw_v3/monitors/plugins/jasna.py` | edit | `vr_8k` field + descriptions, `_VR8K_*` profile constants, `build_argv`, `_launch_locked` unet rule, validator rules (overlap, conditional owned flags), `_tier_suffix` (+ override text), `restore.started` data, `vr_detector_available` + start alert, `ui:order` + `ui:options.taskpawProfile` |
| `taskpaw_v3/tests/test_jasna.py` | edit | AC1/AC2/AC3/AC5 tests; 19 → 20 |
| `taskpaw_v3/ui/src/components/SchemaForm.tsx` | edit | `locked` prop → `LockedFieldsContext` |
| `taskpaw_v3/ui/src/components/ObjectFieldTemplate.tsx` | edit | display controls (Checkbox / TextField + helperText) + always-mounted real field (hidden unless invalid) + note cell |
| `taskpaw_v3/ui/src/views/MonitorWizard.tsx` | edit | read `ui:options.taskpawProfile`, pass `locked` for jasna + `vr_8k`; review-step rows |
| `taskpaw_v3/ui/src/components/TaskLog.helpers.ts` | edit | `profile` in `FIELDS` |
| `taskpaw_v3/ui/src/schemaI18n.ts`, `i18n.ts` | edit | zh/en strings (field, hint, review row, `logs.fields.profile`) |
| `taskpaw_v3/ui/src/test/wizard.test.tsx`, `schemai18n.test.tsx`, the TaskLog helper test | edit | AC3/AC4 tests |
| `README.md`, `CHANGELOG.md` | edit | AC6 |
| six version files | edit | 3.9.6 |

## Execution surface
- Writes: the files above. Reads/executes: `uv run pytest`, `uv run ruff check . && uv run ruff format --check taskpaw_v3 tests scripts && uv run mypy`, `cd taskpaw_v3/ui && npm run lint && npx tsc -b && npx vitest run`. No live agent config, no `C:\Jasna` writes, no film files.
- pytest basetemp: `C:/Users/304/AppData/Local/Temp/t208/pt` (short, MAX_PATH) — `mkdir -p` the parent first.

## Key implementation notes
- `build_argv` order: `… --detection-model <d> [--vr-mode sbs] <extra args>`; the existing argv tests assert exact lists for the unticked case, so append the new flag only when ticked.
- `_tier_suffix` formatting: `[4K 8192x4096, 8K VR, unet-4x off]` — the UI just displays the detail string.
- The display controls are never given to rjsf: they are plain MUI components inside the template cell; the real `el.content` stays mounted in a stable Box (hidden unless its live value is invalid) so no remount, no state loss, no silent HTML5 block.
- Wizard `liveFormData` is reset on service change and seeded on `enterConfig` — the lock condition must read it, never rjsf state.
- The 4K-tier unet rule lives in `_launch_locked` (`tickbox`), not in `build_argv` (which receives `unet_enabled` already resolved) — keep `build_argv`'s signature.

## Risk assessment
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| A form-prop change resets the form (the #204 trap) | medium if done via uiSchema | typed values lost, tick reverts | context-only lock; wizard test types a name then ticks |
| `rfdetr-vr-v1` weights missing on an older Jasna | low | every launch fails | AC5 one alert at start; failed-file path already alerts; abort after 3 |
| A 2D film in a ticked task is processed as SBS and then skipped forever (published marker) | medium (operator error) | wrong output for that film | hint + description + README say "2D films in another task" |
| Saving the tick on a running task restarts the current film | certain (pre-existing) | lost progress | stated in the description, hint and README |
| `--vr-mode` / `--detection-model-path` duplicated by extra args | low | silent override | AC2 rejection under the tick |

## Test plan
- Backend: AC1–AC3, AC5 unit tests in `test_jasna.py` (pure `build_argv`; instance tests via the existing fake-Popen fixtures for the unet rule, the alert and the log record); `test_version.py` for the bump.
- Frontend: `wizard.test.tsx` (AC4, incl. the no-reset regression, the edit-mode save carrying stored values, the review rows); explicit i18n assertions; TaskLog helper `profile`.
- Full suites once on the final commit: `uv run pytest`, lint, `vitest`.
- Manual smoke (owner, post-release): tick 8K VR on the VR task, Start, confirm the detail line shows `8K VR` and the console/argv carries `--vr-mode sbs` and `--secondary-restoration none`; untick, Start, confirm the old clip size is back.

## Handoff notes
- Read `AGENTS.md`, `docs/constitution.md`, `design-system/taskpaw-v3/MASTER.md` and `pages/agent-console.md` before the UI change.
- Do not touch `lada.py`; jasna imports its process recipes from there.
- Keep the diff minimal: no refactor of `build_argv`'s signature beyond reading `cfg.vr_8k`.
