# #267 Jasna presets, inline LTX progress and model download

## Scope and source

Operator 2026-10-18: review Jasna 0.11.2 performance, create an issue and run AFK,
Codex implements, Kimi K3 is the single external gate. Release 3.9.16 and place
the Windows installer on the Desktop. Later instructions specify exactly TWO
selectors: model (plain, unet-4x, LTX) and GPU (5060 8 GB, 5060 Ti 16 GB,
5070 Ti 16 GB, 4090 24 GB, 5090 32 GB). Fill BOTH 1080p/4K for mixed batches;
8K VR remains a separate switch; values remain manually editable. LTX progress
and first-download confirmation belong inside TaskPaw.

Sources verified at Jasna v0.11.2: docs/en/{models,tuning,cli}.md,
jasna/main.py and jasna/ltx/model_files.py. The Windows release enables reduced
LTX overlap by default (~13% faster on long videos). Large canvas costs ~3x on
large mosaics; Best costs ~1.5x. Clip size/temporal overlap tune BasicVSR++, not
LTX's own windows. The CLI explicitly refuses a missing-model download when
stdin.isatty() is false: piping y is NOT an integration. The public encrypted
model files, sizes and SHA256 from model_files.py match Hugging Face repository
revision 3ac0e9734ac40e3b75714bcf942357ef119e2ea1 (verified via its metadata API).
Jasna still owns license activation/decryption; TaskPaw only downloads the public
encrypted files described in Jasna's manual-download instructions.

## Frozen acceptance contract

### AC1 Reference presets

Backend Jasna ui_schema supplies a versioned preset catalogue, the single source
for the UI patches. It has 3 models x 5 GPUs and standard/VR patches.

| GPU | BasicVSR++ clip 1080p / 4K | LTX fast | LTX 1080p / 4K |
| --- | --- | --- | --- |
| 5060 8 GB | 90 / 45 | on | on / off (4K uses unet-4x, explicitly warned) |
| 5060 Ti 16 GB | 180 / 90 | on | on / on |
| 5070 Ti 16 GB | 180 / 90 | on | on / on |
| 4090 24 GB | 180 / 180 | off | on / on |
| 5090 32 GB | 180 / 180 | on | on / on |

Plain disables both unet and LTX tiers. Unet enables both unet tiers and disables
LTX. LTX applies the table, split12, best off, large-canvas off; these are speed
starting points, not measured optimal settings. All use overlap8. Presets do
not change input/output/executable paths, extra args, subtitles, secrets, codec,
CQ or frame rate. They do not infer the actual installed GPU. Existing GPU
checks at Start still gate fast and canvas. Selecting LTX clears VR explicitly.

VR patches are conservative: same 1080p clip, 4K clip30, overlap8, 4K unet off,
LTX off, VR detector. The UI states that VR does not use LTX or the preset's 4K
unet. Introduce vr_8k_custom=False for backwards compatibility: old configs
retain the fixed VR launch profile; preset application sets it True and the
numeric overlap/clip and unet fields are then obeyed and editable. VR detector
and sbs remain fixed. A visible advanced checkbox lets old tasks choose manual
VR tuning too. New validation accounts for the effective VR clip/overlap.

The Configure page shows two labelled selectors above the schema form. Nothing
is applied until both are selected; opening/editing or rerendering never applies
presets. Changing either selection applies only the patch to the latest unsaved
form data. A reset/apply-again button reapplies the selected pair. After that,
manual edits survive all unrelated changes, review/back, and save. Selector
choices are dialog-local references, not an authoritative model/GPU identity.
The existing per-tier fields remain available for manual mixed configurations.
Changing the independent VR switch after a preset is selected applies the
matching standard/VR patch once; it never runs on every render. Older catalogs
without preset metadata keep the current form.

### AC2 Jasna 0.11.2 overlap control

Add ltx_overlap: auto/on/off, default auto. Auto emits nothing, preserving older
Jasna and inheriting 0.11.2 Windows defaults. Explicit on/off requires 0.11.2,
emits the matching --experimental-ltx-less-overlap flag ONLY for LTX, and is
owned/rejected in extra args (including abbreviations). Status labels name only
explicit requests; auto does not claim a probed effective value. Presets use
auto. Existing best/canvas choices remain unchanged unless a preset is applied.

### AC3 Inline LTX progress

Every LTX film runs captured with CREATE_NO_WINDOW and stdin=DEVNULL, independent
of the BasicVSR++ capture preference. BasicVSR++ keeps that preference. Remove
the old missing-model console fallback. Render the existing LTX stage metric as
four labelled steps (scan, encode, denoise, compose), current-stage percentage,
stage FPS when available, whole-run ETA when reported. Do not turn a stage's
100% into whole-film completion or invent an overall weighted percentage.
Works with and without AV subtitles and in read-only Hub metrics. No LTX card
for malformed/absent stage data or a subsequent BasicVSR++ film. Progress resets
between stages/files; old non-LTX dashboards remain compatible.

### AC4 First model download in TaskPaw

At the first pending LTX film with missing model files, create a per-run random
request ID and a local prompt with variant, exact missing basenames, total bytes
and destination. Keep the film queued; do not launch Jasna or increment failure
counts. Release the GPU while waiting/downloading. Start/Stop and reconfigure
invalidate the old request. Model files appearing externally can be recognized
on an explicit retry/new Start; no blind polling of user approval.

A typed, Bearer-gated loopback-only POST /control/monitors/respond accepts name,
request_id and approved boolean. It delegates through Supervisor to an optional
MonitorInstance.respond_prompt method (default refuses). The handler only records
the response, never downloads or runs a child under server/registry locks.
Stale/duplicate/stopped/unsupported requests return conflict; arbitrary commands,
URLs and filenames are never accepted. No corresponding LAN or Hub write API.

Approve starts a terminable taskpaw backend jasna-download worker on the next
monitor check. Only six pinned encrypted basenames are allowed; URLs come from
the official Hugging Face repository (pinned revision) with the documented
hf-mirror fallback. Read request JSON on stdin (destination + allowed filenames),
then watch stdin EOF for parent death like llm-worker. Download via stdlib TLS,
bounded socket timeouts; progress JSON is throttled and consumed via existing
ChildProcess. Hash and size check each unique temporary file before atomic
publish to the exact missing target; never expose a partial final model or
overwrite an already present model. Existing file means reuse, not redownload.
Partial files stay private and are removed on a normal failure; abrupt-kill
remnants are never treated as installed. A new attempt uses a unique partial,
so concurrent downloads never truncate each other. Disk/network/hash errors
surface in the task UI and require a new explicit retry confirmation; no
infinite download retries. A rejected prompt pauses this run without consuming
films or declaring done, and Start reoffers it. Stop terminates/joins the worker
within the existing stop budget, preserving ownership if it survives.

The local task panel shows Download and continue / Not now, bytes/progress while
downloading, and errors/retry. No download action on Hub. Successful download
rechecks required files and resumes the SAME film; no restored output is
published by the downloader. License activation remains Jasna's existing
setup, and any activation failure is shown as Jasna's actual failure.

### AC5 Documentation, checks and release

README covers the four operator machines plus 5090, the speed/quality tradeoff,
8GB limitation (0.11.1 measurements, not a claimed 0.11.2 result), SSD model/work
storage, no sysmem fallback, no automatic FPS reduction, and short representative
benchmarks. Remove unsupported 4K whole-film time estimates. ~13% does not turn
40 hours into a few hours; no promised speedup without machine measurements.
Changelog and all six V3 version sources advance 3.9.15 -> 3.9.16. Publish only
after checks/review; no live install, task restart or deployment.

## Causal boundary and allowed writes

- taskpaw_v3/monitors/plugins/jasna.py; a focused Jasna preset/download module:
  configuration, catalogue, launch/download state, captured progress.
- taskpaw_v3/monitors/base.py and supervisor.py: optional prompt response route.
- taskpaw_v3/agent/server/{app,launcher}.py: local typed response API and wiring.
- taskpaw_v3/packaging/backend_main.py + packaging spec if needed: fixed downloader role.
- taskpaw_v3/ui/src: MonitorWizard, Jasna preset/progress/download components,
  AgentConsole, MonitorMetrics, api, i18n/schemaI18n; tests for changed behavior.
- Focused backend tests, README, CHANGELOG, design, six version files.
- Generated outputs: npm run build -> UI dist; uv run python scripts/build.py
  with frozen build/v3 deps -> sidecar and NSIS/MSI, then hash-verified EXE copy.
  Full pytest, ruff/mypy, frontend lint/test/build, lockfile check, CI read/repair.

## Risks, assumptions and non-goals

16GB and 5090 numeric presets are conservative reference values, unbenchmarked;
the UI/doc say so. The working 5060 is busy; no real GPU benchmark or large model
download occurs during development. Download tests use tiny fixtures and a
local HTTP server/fake transport; production endpoints remain fixed. Packaging
must execute the fixed worker role in the actual EXE. Unknown future model
bundles need a code/manifest update, not arbitrary URLs. No Jasna replacement,
license handling, automatic FPS loss, live fleet editing, V2 changes or unrelated
supervisor refactor. No new runtime dependency.

## Validation and handoff

Tests-first: all 15 preset pairs/VR variants validate; mixed-tier argv; manual VR
and legacy VR; overlap flag isolation and ownership; edit/add form preset apply,
manual changes, back/save and older catalog; LTX capture for both tiers, progress
transitions. Download state: accept, decline, duplicate/stale response, Stop,
failed/retry/success, queue/failure counts, GPU lease release, no false publication;
API auth, LAN absence, strict body and supervisor stopped race. Worker checks:
truncated/wrong hash, fallback, partial cleanup, existing/concurrent target, parent
EOF, native packaged role. UI prompt approval/rejection/error and download data;
stage card both AV modes and invalid data. Full suites and independent Kimi K3
review on final revision, remote CI, release hash verification.

## Subsequent operator evidence and recovery clarification

The operator confirms both 40h reports had large canvas selected. Research found
upstream issue #502 reporting 0.11.1 WDDM paging on a 20GB card, resolved by
turning canvas off; this is not a controlled quality comparison or a diagnosis
of this fleet. Existing TaskPaw 8GB gating means selected is not necessarily
passed. Keep the speed-first preset explicit and editable, and avoid promised
quality equivalence. A declined prompt exposes Download later with a fresh ID
in the same panel; this makes recovery possible while supervisor lifecycle is
still running, addressing the design critic's checkpoint. Start also reoffers.

Subsequent operator steering: community samples reportedly improve static shots
with large canvas, but show little difference during motion. This is attributed
feedback, not an independently verified universal quality result. The operator
requests OFF by default: apply to both new JasnaConfig/schema defaults and all
reference presets. Preserve explicitly saved true values and manual overrides.
Tests must cover the new default and explicit true behavior.
