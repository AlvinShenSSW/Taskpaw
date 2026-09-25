# #189 — AV 翻译 progress redesign: per-film 修复 → 识别 → 翻译 stepper, stage progress, model shown, queue counts fully-done films; version 3.7.0

Date: 2026-09-25 (design v5, FROZEN — debate rounds 1–5: D1–D14, N1–N8, M1–M4, R1–R2, W1; round 5 CLEAN)
Issue: #189. Mockup (owner-approved; owner addition "show the model while translating"):
https://claude.ai/artifact/8xnvaM7SqSwqVoH2HZJVgL — boards ② 修复中, ③ 识别中, ④ 翻译中,
⑤ 独立任务等待 GPU, ⑥ 批量队列, ⑦ Hub status.md.
Driver: `/afk` (Opus 5.5 leads; Opus pilots; Codex 外门 gpt-6-astra high; Kimi 终审). Merge: leave-open.
Upstream: #177 / #179 designs, #187 (naming, v3.6.0).

## Spec review

Owner's SDAB-312 run (v3.5.1): restore 57 min, transcription ~10 min, translation ~3 min. When the
restore ended the UI showed `1/1 已完成` and the per-file bar vanished; the last 13 minutes showed one
detail line and raw `subs_*` tiles. The owner wants the mockup: each film shows its steps (Jasna 3,
avsubs 2) with the active step's own bar and tiles; a film is 完成 only when its subtitles are
settled; a batch view lists films in different stages at once; translation shows the model; the Hub
line keeps its head format. Progress is **read-only observation** — nothing about what runs, when, or
how it settles changes.

## Acceptance criteria

- [ ] AC1 **WhisperJAV progress parser** (`subs/progress.py`, pure): from the ASR child's captured
  output → `{phase, phase_n: 8, scene, scenes, percent, eta_s, elapsed_s}`; anchored patterns;
  phase inferred from later-phase markers (D2); monotonic; never raises; non-qwen engines → elapsed
  only (D9).
- [ ] AC2 **Translator progress**: `Translator.progress()` → the in-flight request's counters and the
  **model label** `<model> · <api host>`; `None` when idle **and after `cancel()`** (D5); fresh dict;
  never the key or userinfo.
- [ ] AC3 **Film tracker**: start stamps + sticky terminal outcomes recorded at the plugins' existing
  counter points; `active / queued / waiting_gpu` **derived at status time** from live facts (D3);
  rows hard-capped (D4); fresh objects (D12).
- [ ] AC4 **Metrics** (additive, one documented semantic change): `film`, `steps`, `films`,
  `films_more`, `model` (while translating); Jasna `queue_restored`; avsubs `queue_pre_done`; with
  AV 翻译 on Jasna's `queue_completed` = **fully done** and `queue_remaining` = total − completed −
  failed (D1); the Jasna detail's "X/Y done" follows the same numbers (D6).
- [ ] AC5 **UI** `PipelineProgress` per the mockup, replacing exactly the now-processing banner, the
  queue bar and the fps/ETA tiles when `steps` is present (D13); zh + en.
- [ ] AC6 **Hub status.md**: exact lines per stage (fixtures below, D7); lada line byte-identical.
- [ ] AC7 Version 3.6.0 → 3.7.0; CHANGELOG; openclaw guide (new keys + both semantic notes, avsubs
  line format).
- [ ] AC8 Tests per the plan; `uv run pytest`, ruff, mypy, UI lint, vitest green.

## Frozen issue contract

**In scope:** AC1–AC8. **User-visible changes allowed:** the new view; Jasna `queue_completed` /
`queue_remaining` / detail "X/Y done" meaning with AV 翻译 on; the status.md suffix for jasna and
avsubs; version 3.7.0.

**Invariants:** constitution §2/§4/§5; zero change to scheduling, settlement, GPU lease, publishing;
wire shape only gains keys except the documented Jasna queue semantics; lada line byte-identical;
AV 翻译 off → Jasna emits no new keys and its existing tests stay unchanged; no key/userinfo in any
metric/detail/log; metrics bounded (≤ 12 rows, strings ≤ 200 chars, model label ≤ 80); no new threads;
`check()` never raises.

**Corrections from repository evidence:**

| # | Issue / v1 text | Evidence | Correction |
|---|---|---|---|
| C1 | "step k/6 and scene [i/N]" | anime-whisper = qwen pipeline: `[QwenPipeline PID n] Phase k: …` (k 1…8) and `[DecoupledPipeline] Generating scene i/N (…)` at INFO on stdout (source + recorded run `scratchpad/clips/run_anime.log`); `k/6` is the balanced pipeline's display step | qwen phases/scenes; other engines elapsed only |
| C2 | (silent) feeding | `ChildProcess` keeps a 40-line tail; `_default_spawn(argv)` is a D10 test seam | `SubsJob.progress(now)` parses `child.tail(lines=40, max_chars=16000)` per poll into a per-attempt parser |
| C3 | "Generating scene 1/1 logged after generation" (v1) | orchestrator.py:511–514 logs it BEFORE `generate_batch`; the recorded 37 s was model load (D11) | scene fraction = (i − 1)/N |
| C4 | "only the metric queue_completed changes" (v1) | `queue_remaining` and `_detail` read the same counters (D1, D6) | fully-done `queue_completed`, `queue_remaining = total − completed − failed`, detail follows; `_done`, the abort detail and the `done` event text stay restore-based (all equal once every job has settled) |
| C5 | v1 A3 "40 lines catch the progress lines" | critic simulation: the single "Phase 5" line is pushed out by ~N framer lines in 44–72% of poll alignments (D2) | phase inferred from any later-phase marker; A3 removed |

**Non-goals:** changing processing; Lada; push channels; other monitors; 401/403 translation error
handling (separate owner topic, OUT-OF-SCOPE).

**Causal boundary:** `monitors/subs/progress.py` (new), `subs/job.py`, `subs/translate.py`,
`subs/__init__.py`, `plugins/jasna.py`, `plugins/avsubs.py`, `hub/server/status_md.py`,
`ui/src/components/PipelineProgress.tsx` (new), `ui/src/components/MonitorMetrics.tsx`,
`ui/src/i18n.ts`, tests, six version files, CHANGELOG, `docs/guides/openclaw-integration.md`, this
doc.

## Assumptions

| # | Claim | Basis | Risk if wrong |
|---|---|---|---|
| A1 | qwen/decoupled log formats as in C1 and the patterns above (incl. `Aligning scene i/N`, `Step-down: …` re-pass) | verified: orchestrator.py 154–1061, decoupled_pipeline.py 431–799, recorded run | parser → elapsed only |
| A2 | the lines reach our pipe for long films | verified for a 41 s clip; `StreamHandler(sys.stdout)`, `--log-level` INFO default; worker re-applies the level | elapsed only |
| A4 | phase weights approximate a 2 h film | unverified split | uneven bar; never backwards; never 100 before settlement |

No WhisperJAV/GPU runs in this run (the owner's avsubs task holds the GPU).

## Module design

### `subs/progress.py` (new, pure)

Patterns (anchored; D9):
```
PHASE = r"\[(?:Qwen|Decoupled)Pipeline PID \d+\] Phase (\d):"       # k = 1..8 (qwen = anime-whisper/qwen3; decoupled mode logs the same 8 phases)
GEN   = r"\[DecoupledPipeline\] Generating scene (\d+)/(\d+)"
ALIGN = r"\[DecoupledPipeline\] Aligning scene (\d+)/(\d+)"          # only when an aligner is configured (anime-whisper: aligner=none)
GEN_DONE  = r"\[DecoupledPipeline\] Steps 2-4: Complete"
P5_MARK   = r"\[DecoupledPipeline\]|\[QwenPipeline\] Phase 5 assembly summary"   # ⇒ phase ≥ 5
FINAL     = r"\[(?:Qwen|Decoupled)Pipeline PID \d+\] Phase 8: \d+ subtitles (?:in final output|passed through)"
```
```
WEIGHTS = {1: .03, 2: .04, 3: .01, 4: .12, 5: .75, 6: .01, 7: .02, 8: .02}   # sum 1.0
class AsrProgress:
    def __init__(self, started_at: float) -> None
    def feed_text(self, text: str, now: float) -> None      # all lines; idempotent for repeated tails
    def snapshot(self, now: float) -> dict                  # fresh dict
```
- **Pipeline gate:** nothing is parsed until a `[QwenPipeline` or `[DecoupledPipeline` marker has
  been seen (orchestrator.py and decoupled_pipeline.py, WhisperJAV 1.9.3, verified by source read); without one, `snapshot` = `{"phase": None, "percent": None, "eta_s": None, "elapsed_s": …}`.
- **Phase:** `phase = max(phase, k)` from PHASE; any P5_MARK/GEN/ALIGN/GEN_DONE line ⇒ `phase =
  max(phase, 5)` (C5).
- **Generation pass:** the FIRST GEN line of Phase 5 fixes `N_gen`; later GEN lines count only when
  their N equals `N_gen` (a qwen3 step-down re-pass uses a different N and is ignored, D9);
  `gen_i = max(gen_i, i)`; samples `(now, i)` kept (≤ 50).
- **Alignment:** ALIGN lines with N = `N_gen` set `align_i = max(...)`; `gen_complete` on GEN_DONE.
- **Fraction inside Phase 5:** `f = 0.9 × (gen_i − 1)/N_gen` while generating; after GEN_DONE: `0.9 +
  0.1 × (align_i − 1)/N_gen` when ALIGN lines exist, else `0.9`; PHASE 6+ ⇒ f = 1.
- **Percent:** `round(100 × (Σ WEIGHTS[1..phase−1] + WEIGHTS[phase] × f_phase))`, `f_phase` = f in
  Phase 5, 0 elsewhere; clamp [0, 99] (FINAL ⇒ 99; 100 only when the job settles); monotonic.
- **ETA (D10):** only in Phase 5 before GEN_DONE, and only after ≥ 2 advances of `gen_i` spanning ≥ 30
  s: `rate = (i_last − i_first)/(t_last − t_first)` over the samples; `eta_s = ceil((N_gen − gen_i +
  1)/rate + 60)` (60 s allowance for Phases 6–8); else None.
- Malformed numbers (i < 1, i > N, N < 1) ignored; never raises.

### `subs/job.py`, `subs/translate.py`

- `SubsJob.start_asr` creates `self._asr_progress = AsrProgress(started_at)` per attempt.
  `SubsJob.progress(now) -> dict|None`: None without a live child or when the child has no `tail`;
  else `feed_text(child.tail(lines=40, max_chars=16000), now)` and return the snapshot.
- `Translator`: a `_progress` record set when a request starts (`job_id`, `cues_total`,
  `batches_total = ceil(n/40)`, `started_at`, `model` label from the request's settings), advanced
  when a batch completes (a split batch counts once, when both halves finish; `cues_done` += the
  batch size), cleared in the request's `finally` and by `cancel()`. `progress()` returns a fresh dict
  or None (None whenever `_cancel` is set, D5). Model label: `f"{model} · {host}"` with host =
  `urlsplit(api_base).hostname` (no userinfo, no port), truncated to 80 chars; never the key.
  Computed defensively (N7): inside try/except; a None host or any exception → the model name
  alone; it can never fail the translation.
- **Translate percent/ETA (D10):** `percent = floor(100 × cues_done / cues_total)`; `eta_s =
  ceil(elapsed/cues_done × (cues_total − cues_done))` when `cues_done ≥ 1` and `elapsed ≥ 10 s`.

### Film tracker (in `subs/progress.py`, pure; D3/D4/D12)

```
class FilmTracker:
    def __init__(self, steps: tuple[str, ...]) -> None       # ("restore","asr","translate") | ("asr","translate")
    def add(self, film: str, initial: dict[str, str]) -> None
    def start(self, film, step, now) -> None                   # started stamp (translate: submitted)
    def activate(self, film, step, now) -> None                # first time derived active (keeps first)
    def finish(self, film, step, state, now) -> None           # sticky terminal: done|failed|skipped
    def settle_subs(self, film, outcome, now) -> None          # N1: first non-terminal subtitle step
                                                               # gets outcome, later subtitle steps skipped
    def record(self, film) -> dict                             # fresh copy
```
- **Safety (M1):** every tracker method is total — it never raises (bad input is ignored), and a
  film or step it does not know is ignored. `add` runs for every film **before any settle can
  happen**: Jasna in the job-creation loop of `_setup_subs` (before its no_exe loop and before the
  translator-start `_disable_subs`); kind-none pending films right after `plan_subs` inside
  `_setup_subs` (the kind is only known there; they have no job, so no settle can precede it); on
  the planning-failure path (`_subs_disabled = "planning failed"`, no jobs) every pending film is
  added with asr and translate `skipped` (R2);
  avsubs in the job-creation loop, before its no_exe settle. Each mark sits **immediately after its
  counter / `_settled` update and before any `emit` or `_disable_subs` call**, so a raising emit
  cannot skip it. `_fence_internal`'s direct `_settled[job_id] = …` write (the path taken when
  `_settle` itself raised) also calls `settle_subs(film, "failed")`.
- Terminal states are **sticky** (a later mark never changes them); `start`/`activate` on a terminal
  step are ignored; a repeated `start`/`activate` keeps the first stamp (restore retries keep one
  duration). **Precedence (N1):** a stored terminal state always wins; `active / queued /
  waiting_gpu` are derived only for non-terminal steps.
- `duration_s` = `ended_at − (activated_at or started_at)`; for translate only `activated_at` is used
  (a translation that finished between two polls has no duration; the UI shows ✓ alone).
- **Marks (only at counter points):** Jasna — restore launch → `start(restore)`; `_done += 1` →
  `finish(restore, done)`; `_fail_current` (and the internal-error fence's direct bump) →
  `finish(restore, failed)` + `settle_subs(film, skipped)`. A restore's terminal state is set **only**
  at these two points (N1). ASR start → `start(asr)` (stamp = `job.started_at`); `publish_ja` /
  no_speech → `finish(asr, done)`; `translator.submit(...)` → `start(translate)` (the queued fact,
  N2); `_settle(job, completed)` → `finish(asr, done)` if not yet + `finish(translate, done)`;
  `_settle(job, failed|skipped, …)` → `settle_subs(film, failed|skipped)` — **subtitle steps only**,
  so a pending or running restore is never touched (no_exe at Start, `_disable_subs`), and an ASR
  failure marks asr `failed`, translate `skipped` (N1). avsubs identically without restore
  (`translator.submit` at Start for translate_only films and after each ASR).
- **Initial states:** Jasna full pending → all pending; Jasna pending translate_only → asr `done`
  (existing `.ja.srt`, no duration); Jasna subs_only → restore `done` (pre-existing), asr per kind;
  Jasna kind none (subs already present) → asr and translate `skipped` at `add` (M2), so the film is
  fully done once its restore is; avsubs full → asr pending; avsubs translate_only → asr `done`.
  **Row status (R1) is derived from outcomes, first match wins:** (1) restore `failed` → `failed`;
  (2) restore not terminal (Jasna) → the derived state of the restore step (`active`, `waiting_gpu`
  or `pending`), even if the film's subtitle job was already settled early (no_exe,
  `_disable_subs`); (3) subtitle job settled → its outcome: completed → `done`, failed → `failed`,
  skipped → `skipped`; (4) no job (Jasna kind none or planning failed) with the restore done →
  `done`; (5) otherwise the derived state of the first non-terminal step. The tracker stores the
  job outcome in `settle_subs` (and `finish(translate, done)` records completed). "第 k 步 / 共 n
  步" = the index of the first non-terminal step, or n when none.
  **Row ↔ count mapping (documented in the guide, tested over the tracker, not the capped rows):**
  avsubs — `queue_completed − queue_pre_done` = films `done`; `queue_skipped` = films `skipped`;
  `queue_failed − (name collisions)` = films `failed` (pre-done films and collisions are counted at
  scan and are not tracker films). Jasna — `queue_failed − len(collisions)` = tracker films whose
  restore `failed`; `queue_completed − (plan_queue done − len(plan.subs_only))` = tracker films whose
  restore is done and whose status is terminal (`done`, subtitle `failed` or `skipped` — D1 counts a
  settled job of any outcome); collisions and films already restored with subtitles at Start are
  counted but not tracked (W1); the stepper shows which step failed.
- **Derived at status time (never stored):** restore `active` ⇔ `_process` live and `_current` is
  the film; asr `active` ⇔ `_subs_job.child` live for the film; translate `active` ⇔
  `Translator.progress().job_id` is the film (→ `activate(translate)` at status time); translate
  `queued` ⇔ translate started (submitted) ∧ not terminal ∧ not active (N2 — a translate_only film
  not yet submitted stays `pending`); `waiting_gpu` ⇔ `_gpu_waiting` and the film is the queue head
  for the next GPU step (Jasna: `_pending[0]` → restore, else `_subs_only[0]` → asr; avsubs:
  `_queue[0]` → asr — matches `_advance`, verified round 2). `holder = blocking_label()`, but `""`
  when `reserved_for() == self._run` (N5 — the existing IR9 guard, jasna.py:2651, avsubs.py:1336).
  `waited_s` (N6): a status-time stamp dict keyed by `(film, step)`, set when the derived waiting
  state first appears and dropped when it is no longer derived (the refusal flags toggle every poll,
  so they cannot hold it).
- **Focus film:** GPU child (restore/asr) > waiting_gpu > translating > next pending > last finished.
- **Rows (`films`, N4):** rows are **picked by priority** — focus, active, waiting, queued (nearest
  first in plan order), last 3 finished, next pending — until the **hard cap 12**, then the picked
  rows are **sorted by plan order**; the focus film is always included; the rest are counted in
  `films_more`. Row: `{"name",
  "steps": {step: state}, "status": done|failed|skipped|active|queued|waiting_gpu|pending, "percent",
  "eta_s", "duration_s"}` (fresh dicts; name ≤ 200 chars).

### Plugins — metrics

Added when Jasna is managed with AV 翻译 on, and always on avsubs:
- `film` (≤ 200), `steps`: list of `{"key", "state", "percent"?, "eta_s"?, "elapsed_s"?,
  "duration_s"?, "holder"?, "waited_s"?}` plus per-step numbers (N8): restore — `percent` and
  `eta_s` from the existing capture keys (`eta` string parsed as `M:SS`, `MM:SS` or `H:MM:SS`,
  anything else → None); asr — `phase`, `phase_n`, `scene`, `scenes` from `SubsJob.progress` (the UI
  and status.md format them; no Chinese text in the metrics); translate — `model`, `batches_done`,
  `batches_total`, `cues_done`, `cues_total` from `Translator.progress`. Also `films`, `films_more`,
  and top-level `model` while a translation is active.
- Jasna: `queue_restored = _done`; `queue_completed` = `_done − |{j ∈ _jobs : film restored (subs_only
  at Start, or restored this run) ∧ j ∉ _settled}|`; `queue_remaining = total − queue_completed −
  _failed`. `_detail` / `_waiting_detail` use these metric values (D6). `_aborted_detail` and the
  `done` event keep `_done`.
- avsubs: `queue_pre_done = _pre_done` (the 已有字幕 chip, D8); its queue semantics unchanged.

### Hub status.md (D7) — exact lines

Built on the existing segment logic: head `X/Y done (Z left)`, then `| current_file |`, then the
existing restore progress `p% · ETA · fps`. New **stage** fragment from the focus step (N3): an
active restore → nothing new (the existing progress part covers it); a `waiting_gpu` step of any kind
→ `等待 GPU（<holder>）`, or `等待 GPU` when holder is empty; active asr → `识别 43% · 约剩 6 分`
(ETA minutes = ceil(eta_s/60)), `识别 43%` without an ETA, `识别 · 已用 4 分` without a percent
(floor(elapsed/60), min 1); active translate → `翻译 56% · 约剩 1 分 · grok-4.3 · api.x.ai`, or
`翻译 56% · grok-4.3 · api.x.ai` without an ETA (model `_inline`, ≤ 80); queued/pending/terminal focus
→ nothing.
Joined like the existing subs part (space after a trailing `|`, else ` | `). Then the counts part:
Jasna with `queue_restored` → `修复 a/b · subs S/T` (+ ` (N failed)`); without it the existing `subs
S/T` rendering, unchanged. Fixtures:
```
- JASNA: 0/1 done (1 left) | SDAB-312.mp4 | 57% · ETA 7:18 · 157fps | 修复 0/1 · subs 0/1
- JASNA: 0/1 done (1 left) | SDAB-312.mp4 | 修复 0/1 · subs 0/1                       (capture off)
- JASNA: 0/1 done (1 left) | SDAB-312-破解.mp4 | 识别 43% · 约剩 6 分 | 修复 1/1 · subs 0/1
- JASNA: 0/1 done (1 left) | SDAB-312-破解.mp4 | 识别 · 已用 4 分 | 修复 1/1 · subs 0/1
- JASNA: 0/1 done (1 left) | 翻译 56% · 约剩 1 分 · grok-4.3 · api.x.ai | 修复 1/1 · subs 0/1
- JASNA: 0/2 done (2 left) | 等待 GPU（AV） | 修复 0/2 · subs 0/2
- JASNA: 0/2 done (2 left) | 等待 GPU | 修复 0/2 · subs 0/2                                  (holder "")
- JASNA: 0/1 done (1 left) | SDAB-312-破解.mp4 | 识别 43% | 修复 1/1 · subs 0/1          (no ETA yet)
- AV: 21/63 done (41 left) | 2024/ABC-123.mp4 | 识别 43% · 约剩 6 分
- LADA: … (byte-identical to today)
```
Malformed `steps` (not a list / bad fields) → no stage fragment.

### UI (D8/D13)

`PipelineProgress.tsx` renders when `metrics.steps` is an array: header area (film name + "第 k 步 /
共 n 步"), stepper (done ✓ + duration / active ring + percent + ETA / queued / waiting amber with
`holder` and `waited_s` / failed red / skipped grey / pending number), active-step panel (bar +
tiles: restore fps/ETA/output name; asr 场景 i/N or 阶段 k/8 formatted from `scene`/`scenes`/`phase`/
`phase_n` (zh + en strings), elapsed, ETA; translate model, batches, cues,
elapsed, ETA), queue card (Jasna: fully-done bar + light-green in-progress + 修复/字幕 chips;
avsubs: 完成/翻译中/失败/跳过/排队/已有字幕 chips + red failed segment), batch list from `films` (+
"还有 N 部"). In `MonitorMetrics`, when `steps` is present PipelineProgress **replaces** the
now-processing banner, the queue bar and the fps/ETA tiles; gauges and the VRAM bar stay; the new keys
plus `phase`, `subs_*`, every `queue_*` key (incl. `queue_failed`, `queue_skipped`, `queue_restored`,
`queue_pre_done`) and the capture keys `elapsed`, `processed_frames`, `remaining_frames` are hidden
from tiles **only then** (D13: they are shown by the queue chips and the restore panel); with
no `steps` everything renders exactly as today. Colours from `TINT`/theme; zh + en strings.

## Test plan

- `test_subs_progress.py`: recorded lines fixture (checked-in excerpt of `run_anime.log`) → phases,
  scene, 99 at FINAL; synthetic 276-scene stream with a 276-line framer burst **and no Phase 5 line in
  the tail** → phase 5 inferred, percent moves (D2); ETA gate and formula values; bursts; step-down
  re-pass (different N) ignored (D9); ALIGN sub-phase; balanced "Scene 1/1 (…)" and a filename
  containing "Phase 8" → no effect (D9); (i − 1)/N (C3); repeated tails idempotent; new attempt → new
  parser; garbage/partial lines; never raises. FilmTracker: sticky terminals, start stamp kept across
  retries, marks from settle/fail, initial-state table, window + hard cap 12 with a 30-film
  translate-only backlog (D4), fresh objects.
- `test_subs_job.py` / `test_subs_translate.py`: `progress()` accessors; batch counters incl. split
  halves and retries; `progress()` None after cancel mid-batch (D5); model label with
  `https://u:k@api.x.ai:443/v1` → `api.x.ai` (D14); no key text.
- Jasna/avsubs integration: metrics per stage (restore capture on/off; asr with progress; translate
  with model; waiting_gpu with holder/waited_s; queued translations; batch with one film translating
  while the next restores); fully-done `queue_completed` + `queue_remaining` + detail consistency
  incl. failed/skipped/no_exe/disabled/unstable subs (D1) — `test_jasna_subs.py:590` and `:725` stay
  green; tracker under `_disable_subs`, abort, stop/restart generations, restore requeue, internal-
  error fences; AV 翻译 off → no new keys and existing tests unchanged; `done` event text unchanged.
- `test_status_md.py`: every fixture line above; malformed `steps`; `_inline` of model/holder; lada
  byte-identical.
- UI vitest: each stepper state, model label, holder/waited, queue chips (both kinds), batch rows +
  "还有 N 部", no raw tiles for hidden keys when `steps` present, unchanged rendering without `steps`.
- Round-2 cases: `settle_subs` never touches a pending/running restore (no_exe at Start and
  `_disable_subs` mid-batch → the next film still shows 修复 active, then done; N1); ASR failure →
  asr failed + translate skipped; a Jasna translate_only film is `pending` until
  `translator.submit` and `queued` after (N2); translate `duration_s` from the first derived-active
  poll, absent when never seen active; `waited_s` grows across polls while the refusal flag toggles
  and resets when the wait ends (N6); `holder == ""` when the lease is reserved for this run (N5);
  model label for `api.x.ai/v1` (no scheme), `http://[::1` (malformed) and `https://u:k@h:443/v1`
  never raises and never contains `u`/`k` (N7); `eta` strings `7:18`, `07:18`, `1:02:03`, `--`, `""`
  (N8); 30 queued translate_only films sorting before the ASR film → the focus film is still in
  `films` and rows stay in plan order (N4); status.md fixtures for holder "" and no-ETA (N3).
- Round-3 cases (M1/M2): after no_exe at Start (both plugins) and after a translator-start failure,
  every film's asr and translate are `skipped` and restores still progress; a tracker fed an unknown
  film / step / garbage never raises; a raising `emit` inside `_settle` / `_fail_current` still
  leaves the mark; the `_fence_internal` direct-write path marks subtitle steps `failed`; a kind-none
  film is `done` once restored; row `status` for each mix of step states.
- Round-4 cases (R1/R2): row status for asr done + translate skipped via avsubs abort, no key and
  Jasna `_disable_subs` → `skipped` (not `done`); a Jasna film settled early by no_exe while its
  restore is active → `active`, then `skipped` once restored; the row ↔ count mapping holds after
  mixed runs in both plugins (incl. collisions and pre-done films in avsubs); kind-none films added
  after `plan_subs`; planning failure → every pending film has asr/translate `skipped` and ends
  `done` once restored.
- Every existing test whose assertion changes is listed in the PR with the reason.

## Handoff notes

Pilot split: **A** = `subs/progress.py` (parser + tracker) + job/translate accessors + tests; **B**
(after A) = Jasna + avsubs metrics/tracker marks + status_md + guide + tests; **C** (parallel with A) =
UI coded against the metrics shapes above. Driver: version, CHANGELOG, sweep, commit/PR. Never touch the
owner's live agent/config/library; no WhisperJAV/GPU runs.
