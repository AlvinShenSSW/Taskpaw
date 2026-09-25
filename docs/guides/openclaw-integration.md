# OpenClaw integration — reading TaskPaw fleet status

The V3 Hub writes two OpenClaw-facing artifacts into its data dir (default
`~/.taskpaw-hub/`) every poll, so an external agent can read fleet status **without
an API**:

| File | What it is | Best for |
| ---- | ---------- | -------- |
| `hub.db` | SQLite. `status_log(server_id, timestamp, reachable, status_json)` — the raw per-server `/status` JSON, one row per poll. | **Programmatic reads — structured, every field, exact values.** Use this. |
| `status.md` | Human-readable Markdown snapshot, overwritten each poll. | Quick eyeballing; simple regex scrapers. |

> **Prefer `hub.db`.** `status.md` is a flattened view; the DB has every metric as a
> typed field. Both come from the same poll — they never disagree, but the DB is
> richer and doesn't need parsing heuristics.

## Reading `hub.db` (recommended)

```python
import sqlite3, json, os

con = sqlite3.connect(os.path.expanduser("~/.taskpaw-hub/hub.db"))
con.row_factory = sqlite3.Row

def num(d, k):                       # accept only finite numbers (drop NaN / "n/a")
    v = d.get(k)
    return v if isinstance(v, (int, float)) and v == v else None

rows = con.execute("""
    SELECT s.name AS server, s.online, l.status_json
    FROM servers s
    LEFT JOIN status_log l ON l.id = (
        SELECT id FROM status_log WHERE server_id = s.id ORDER BY timestamp DESC LIMIT 1)
""").fetchall()

for r in rows:
    data = json.loads(r["status_json"] or "{}")   # {machine, os, server_id, monitors}
    for name, mon in data.get("monitors", {}).items():
        met, tid, state = mon.get("metrics") or {}, mon.get("type_id"), mon.get("state")

        # host: type_id == "host_metrics", or (older agents) a disk_pct in metrics
        if tid == "host_metrics" or num(met, "disk_pct") is not None:
            cpu           = num(met, "cpu_pct")
            mem_used_mb   = num(met, "mem_used_mb")    # /1024 = GB (see version note)
            mem_total_mb  = num(met, "mem_total_mb")
            mem_pct       = num(met, "mem_pct")        # always present
            gpu           = num(met, "gpu_pct")
            vram_used_mb  = num(met, "gpu_mem_used_mb")
            vram_total_mb = num(met, "gpu_mem_total_mb")

        # lada / jasna / avsubs: that type_id, or a queue_total in metrics
        if tid in ("lada", "jasna", "avsubs") or num(met, "queue_total") is not None:
            done, total = num(met, "queue_completed"), num(met, "queue_total")
            left, cur   = num(met, "queue_remaining"), met.get("current_file")
            running     = state == "running"          # ← judge by state, NOT enabled
            # Per-task progress — CAPTURE MODE ONLY (lada_capture_progress: true);
            # all None with capture off. Select whichever you need:
            pct         = num(met, "percent")          # 0..100, current file
            eta         = met.get("eta")               # str "MM:SS" / "H:MM:SS"
            elapsed     = met.get("elapsed")           # str, same format
            done_frames = num(met, "processed_frames")
            left_frames = num(met, "remaining_frames")
            fps         = num(met, "fps")
            # jasna (#177): `phase` is on every managed Jasna; subs_* only with「AV 翻译」ticked:
            phase       = met.get("phase")             # "restore" | "subs" | "translate"
            subs_done   = num(met, "subs_completed")
            subs_total  = num(met, "subs_total")
            subs_failed = num(met, "subs_failed")
            translating = num(met, "subs_translating") # files queued/in flight
            # avsubs (#179): `phase` is "asr" | "translate" | "waiting_gpu" (absent
            # when idle); queue_skipped counts its skipped files
            skipped     = num(met, "queue_skipped")
            # #189 (3.7.0) per-film pipeline — jasna with「AV 翻译」ticked, and avsubs:
            film        = met.get("film")               # str: the film `steps` describes
            steps       = met.get("steps")              # list of {key, state, …} (below)
            films       = met.get("films")              # ≤ 12 rows, plan order
            films_more  = num(met, "films_more")        # films not listed in `films`
            model       = met.get("model")              # "grok-4.3 · api.x.ai", only while translating
            restored    = num(met, "queue_restored")    # jasna: films restored so far
            pre_done    = num(met, "queue_pre_done")    # avsubs: had their .srt at scan

        # comfyui: type_id == "comfyui", or BOTH running and pending present
        if tid == "comfyui" or (num(met, "running") is not None and num(met, "pending") is not None):
            comfy_running, comfy_pending = num(met, "running"), num(met, "pending")
```

### Field reference (all under `monitors[name]["metrics"]`)

| Data | Field | Monitor | Notes |
| ---- | ----- | ------- | ----- |
| CPU % | `cpu_pct` | host | always |
| RAM % | `mem_pct` | host | **always** |
| RAM used / total (MB) | `mem_used_mb` / `mem_total_mb` | host | ÷1024 = GB. See version note. |
| GPU % | `gpu_pct` | host | Windows (`"n/a"` on macOS) |
| VRAM used / total (MB) | `gpu_mem_used_mb` / `gpu_mem_total_mb` | host | ÷1024 = GB |
| Queue done / total / left | `queue_completed` / `queue_total` / `queue_remaining` | lada / jasna / avsubs | avsubs: done includes videos that already had subtitles at Start (since 3.7.1 the #191 rules, see the `avsubs` note below). jasna with「AV 翻译」ticked (since 3.7.0): done = **fully** done films — see the note below |
| Queue restored | `queue_restored` | jasna | int; only with「AV 翻译」ticked (3.7.0): films restored so far — the restore count `queue_completed` carried before 3.7.0 |
| Queue already subtitled | `queue_pre_done` | avsubs | int (3.7.0): videos that already had subtitles at Start (included in `queue_completed`) |
| Queue failed | `queue_failed` | jasna / avsubs | int; files given up on after their retries (plus output-name collisions) |
| Queue skipped | `queue_skipped` | avsubs | int; no LLM key (since 3.8.0: no usable model — primary or fallback), source changed during transcription, cancelled (abort), whisperjav.exe missing, (3.7.1) a subtitle / transcript that appeared before it could be written (never overwritten) or a folder that could not be read, or (3.8.0) a translation paused after 2 h without a translation service (continued at the next Start) |
| Current task | `current_file` | lada / jasna / avsubs | string; capture mode or folder-derived. avsubs: the video's path **relative to the library folder** (e.g. `sub/film.mp4`), only while WhisperJAV transcribes it |
| Current-file % | `percent` | lada / jasna | 0..100; **capture mode only** |
| ETA / elapsed | `eta` / `elapsed` | lada / jasna | string `MM:SS`/`H:MM:SS`; **capture mode only** |
| Frames done / left | `processed_frames` / `remaining_frames` | lada / jasna | int; **capture mode only** |
| Speed (fps) | `fps` | lada / jasna | float; **capture mode only** |
| Phase | `phase` | jasna | `restore` (a video is being restored, or idle), `subs` (WhisperJAV is transcribing), `translate` (only translations are running) |
| Subtitles done / total / left | `subs_completed` / `subs_total` / `subs_remaining` | jasna | int; only with「AV 翻译」ticked |
| Subtitles failed / skipped | `subs_failed` / `subs_skipped` | jasna | int; skipped = restore failed, no LLM key (since 3.8.0: no usable model — also when the translator finds none, formerly a failure), source changed, cancelled, whisperjav.exe missing, (3.7.1) a subtitle / transcript that appeared before it could be written (never overwritten) or an output folder that could not be read, or (3.8.0) a translation paused after 2 h without a translation service |
| Translations pending | `subs_translating` | jasna / avsubs | int; files queued in or held by the translator |
| Phase | `phase` | avsubs | `asr` (WhisperJAV is transcribing `current_file`), `translate` (only translations are running), `waiting_gpu` (the GPU is held by another task, e.g. Jasna); **absent** when idle/finished |
| Focus film | `film` | jasna (「AV 翻译」) / avsubs | string ≤ 200 (3.7.0): the film `steps` describes — the one in a GPU child (restore / transcription), else the one waiting for the GPU, else the one translating, else the next still to finish, else the last finished. avsubs: the path relative to the library folder |
| Film steps | `steps` | jasna (「AV 翻译」) / avsubs | list (3.7.0) of `{key, state, …}` for `film`, see "Per-film pipeline" below |
| Film rows | `films` / `films_more` | jasna (「AV 翻译」) / avsubs | list (3.7.0) of at most 12 rows `{name, steps: {key: state}, status, percent, eta_s, duration_s}` in queue order (`percent` / `eta_s` / `duration_s` may be `null`); `films_more` = films not listed |
| Translation model | `model` | jasna (「AV 翻译」) / avsubs | string ≤ 80 (3.7.0): `<model> · <api host>` of the translation in flight — **absent** otherwise; never the key |
| ComfyUI running / pending | `running` / `pending` | comfyui | |
| **Running?** | top-level **`state`** (`running`/`idle`/`ok`/`error`/`stopped`) | any | **use this, not `enabled`** |

Top-level of each `status_json`: `machine` (display name), `os`, `server_id`.

> **Lada per-task progress needs capture mode.** `percent` / `eta` / `elapsed` /
> `processed_frames` / `remaining_frames` / `fps` describe the *current file* and
> are populated only when the agent captures lada-cli's output
> (`lada_capture_progress: true`). With capture off (the default) lada runs in its
> own console window and only the folder-derived fields — `queue_*` and
> `current_file` — are available. They also appear only while the monitor `state`
> is `running`; an idle/finished snapshot omits them. Always guard each with
> `num()` / a `None` check.
>
> **Jasna reports the same keys.** A monitor with `type_id == "jasna"` carries exactly
> the same metric names as lada (plus `queue_failed`), so the same reader code covers
> both — but its `current_file` is always known, even with capture off, because it
> launches one `jasna.exe` per video instead of one batch for the folder.
>
> **Jasna「AV 翻译」(#177, names since 3.6.0 / #187).** Restored films are written as
> `<name>-破解.mp4` (a `<name>_restored.mp4` from 3.5 and earlier still counts as
> restored and is not renamed). With the tickbox on, every restored film also gets
> `<name>-破解.srt` (Simplified Chinese) **next to `<name>-破解.mp4`** in the output
> folder — the subtitle always carries its video's name, so a legacy
> `<name>_restored.mp4` gets `<name>_restored.srt`. The Japanese transcript
> (`<name>-破解.ja.srt`) is deleted once the `.srt` is written and kept only while
> a translation is unfinished. Since 3.7.1 (#191) a film whose restored video already
> has `<name>-破解.srt` (or `.ass` / `.ssa` / `.vtt`), or a `<name>-破解.<tag>.srt` whose
> tag is not Japanese (e.g. `.chs.srt`), gets no subtitle job; the output folder is
> looked at again before each transcription and translation, a subtitle is never
> overwritten, and an output folder that cannot be read skips the film (one alert
> `subtitle state unreadable` per run). The snapshot adds
> the `subs_*` keys above (`phase` itself is present for every managed Jasna, ticked or
> not). `current_file` follows the live child: the
> video being restored in `phase == "restore"`, the restored `.mp4` being
> transcribed in `phase == "subs"`, and **absent** in `phase == "translate"` (no GPU
> child is running then). The batch `done` event text gains
> `| Subs: S/T done, U failed, V skipped` — since 3.8.0 followed by
> `; N lines kept in Japanese` and `; N paused` when those are not 0 (see
> "Resumable translation" below).

> **Jasna queue counts with「AV 翻译」ticked (#189, since 3.7.0).** `queue_completed`
> counts films that are **fully done**: restored AND their subtitle job settled —
> translated, failed or skipped (a film that needs no subtitles counts once it is
> restored). `queue_remaining` = `queue_total − queue_completed − queue_failed`, and
> the detail line's `X/Y done` shows the same numbers. The restore count moved to
> `queue_restored`. The batch `done` event and the abort alert keep the restore
> count (`Queue: X/Y done, F failed`), which equals `queue_completed` once every
> subtitle job has settled. With the tickbox off nothing changes and none of the
> #189 keys appear.

> **Per-film pipeline (#189, since 3.7.0).** Jasna with「AV 翻译」ticked and `avsubs`
> add `film`, `steps`, `films` and `films_more` — always all four together, or none
> (no films planned) — plus `model` while a translation is in flight. Each `steps`
> entry is `{key, state}` plus optional numbers (**absent** when unknown, never
> `null`):
>
> - `key` — `restore` (jasna only), `asr` (WhisperJAV transcription), `translate`,
>   in that order;
> - `state` — `done` / `failed` / `skipped` (terminal), `active`, `queued` (handed to
>   the translator, waiting for its turn), `waiting_gpu`, `pending`;
> - a terminal step: `duration_s`;
> - the active step's numbers — restore: `percent`, `eta_s`, `elapsed_s` (capture mode
>   only); asr: `percent` (0..99), `eta_s`, `elapsed_s`, `phase` (1..8) / `phase_n`
>   (8), `scene` / `scenes` (the WhisperJAV qwen pipelines, e.g. anime-whisper; other
>   engines report `elapsed_s` only); translate: `percent`, `eta_s`, `elapsed_s`,
>   `model`, `batches_done` / `batches_total`, `cues_done` / `cues_total`, and since
>   3.8.0 (#192) `cues_resumed` (lines taken from the checkpoint), `cues_fallback`
>   (lines translated by a fallback model), `cues_kept_ja` (lines kept in Japanese),
>   `paused` (bool: the film waits for a translation service — the step stays
>   `active` and `eta_s` is absent) and `deferred` (how many films wait so);
> - a `waiting_gpu` step: `holder` (the task holding the GPU; `""` while the GPU is
>   free and reserved for this task) and `waited_s`.
>
> A row's `status` is `failed` when its restore failed; the restore step's state while
> the restore is not done (even when its subtitles were already given up, e.g.
> whisperjav.exe missing); then the subtitle job's outcome — translated → `done`,
> `failed`, `skipped`; `done` when there was nothing to subtitle; otherwise the state
> of its first unfinished step.
>
> **Rows ↔ counts.** `films` is capped; over ALL films the counts map as follows.
> avsubs — `queue_completed − queue_pre_done` = films `done`; `queue_skipped` = films
> `skipped`; `queue_failed −` (name collisions) = films `failed`. Jasna —
> `queue_failed −` (name collisions) = films whose `restore` step `failed`;
> `queue_completed −` (films already restored at Start that are not rows (normally those
> that already had their subtitles)) = films whose restore is `done` and whose `status` is
> terminal (`done`, or a subtitle step `failed` / `skipped`: a settled job of any outcome
> counts as done). Name collisions,
> videos that already had subtitles (avsubs) and films already restored with
> subtitles at Start (jasna) are counted but never rows; the steps show which step
> failed.

> **「AV 翻译 (subtitles)」task (`avsubs`, #179).** A library task: for every video
> under its folder (recursively by default) that has no subtitles yet, it writes
> `<name>.srt` (Simplified Chinese) **next to the video**; the intermediate
> `<name>.ja.srt` (Japanese) is deleted once the `.srt` is written (since 3.6.0 /
> #187) and kept only while a translation is unfinished. Since 3.7.1 (#191) a video
> already has subtitles — and is skipped, counted in `queue_pre_done` — when, from
> its folder's listing, (a) `<name>.srt` (or `.ass` / `.ssa` / `.vtt`) exists, (b) a
> `<name>.<tag>.srt` whose tag is not Japanese exists (e.g. `.chs.srt`, `.zh.srt`), or
> (c) its folder holds only this one video and any non-Japanese subtitle file. The
> folder is looked at again before each transcription and translation; a subtitle
> that appeared meanwhile, or a folder that cannot be read, skips the video
> (`queue_skipped`; tried again at the next Start), and a subtitle is never
> overwritten. It reports the same
> `queue_*` / `current_file` keys as lada/jasna (plus
> `queue_skipped`, `phase`, `subs_translating`), so the reader above covers it. It
> shares the GPU with Jasna one file at a time: while the other task holds the GPU
> its `phase` is `waiting_gpu` and its detail reads `waiting for GPU (held by
> <task name>)` — that is not an event. Events:
>
> - `done`: `AV 翻译 complete | Queue: X/Y done, F failed, K skipped | <timestamp>`
>   (never after a Stop or an abort); since 3.8.0 `skipped` is followed by
>   `; N lines kept in Japanese` and `; N paused` when those are not 0;
> - alert `AV 翻译 aborted after 3 consecutive failures | Queue: X/Y done, F failed,
>   K skipped` (the monitor then stays `degraded` until the next Start);
> - alert `a WhisperJAV process may still be running; check Task Manager` (a killed
>   transcription left a process behind);
> - per-file alerts `<task>: subtitles for <relpath> failed`, plus one-per-run
>   alerts for a missing LLM API key, `whisperjav.exe not found` (monitor `error`),
>   subtitle name collisions, unreadable folders/names, and (3.7.1) one
>   `<task>: subtitle state unreadable` when a video's folder could not be read
>   right before its work; since 3.8.0 also the translation alerts below.

> **Resumable translation and fallback models (#192/#190, since 3.8.0).** Both Jasna's
> 「AV 翻译」and `avsubs` translate line by line from a checkpoint under the agent's
> data folder (`subs-checkpoints/`, next to `agent.yaml`; labels only, never a key),
> so a Stop, a crash or an outage loses at most the request in flight and the next
> Start asks only for the lines still open. Settings → LLM API has two optional
> fallback models (`llm_fallback1_api_base` / `_model` / `_api_key`, the same for
> `llm_fallback2_…`; the keys also from `TASKPAW_LLM_FALLBACK1_API_KEY` /
> `TASKPAW_LLM_FALLBACK2_API_KEY`, which win) and the switch
> 「主模型不可用时改用备用模型」(`llm_failover`, default on). A line a model refuses goes
> to the next model; a line every model refuses keeps its Japanese text; while a
> model is unavailable its lines go to the next one (switch on) or wait. A film is
> skipped `no_llm_key` only when no model is usable. Skip reasons and events:
>
> - `translation_paused` — a film that found no translation service for 2 h in total
>   is skipped (`queue_skipped` / `subs_skipped`, never a failure, its transcript
>   and checkpoint kept) and continued at the next Start; ONE alert per run
>   `<task>: translation paused` — `N file(s) paused: …`;
> - alert `<task>: translation model unavailable: <model> · <api host>` — once per
>   model per run, with the reason (key or credit, model or URL not found, rate
>   limit or quota, content policy, a rejected request, unusable output,
>   unreachable) and when it is tried again;
> - alert `<task>: translation checkpoint not saved` — once per run; translation goes
>   on, but a Stop then loses the lines not yet published;
> - the `done` text's `; N lines kept in Japanese` / `; N paused` (above); each film
>   with kept lines is also logged by the agent (a count, never the text).

## Three rules that bite

1. **Judge "is it running" from `state` + `metrics`, never from `enabled`.** `enabled`
   is *config intent*; a monitor can be actively running (live `state`/`metrics`) while
   its config still says `enabled: false`. Reading `enabled` will show a busy worker as
   "disabled".
2. **Filter every number** — a metric can be `NaN` or the string `"n/a"` (e.g. GPU on
   macOS). Use the `num()` helper above.
3. **Identify a monitor by `type_id` first**, falling back to a metric signature only
   for pre-`type_id` agents (`disk_pct` ⇒ host; `queue_total` ⇒ lada/jasna/avsubs; both
   `running`+`pending` ⇒ comfyui).

## `status.md` format (the secondary source)

```
# TaskPaw Hub Status

Last updated: YYYY-MM-DD HH:MM:SS

## PinkPig: ONLINE
- PinkPig-host: CPU 45% | RAM 8.2/16.0GB | GPU 78% | VRAM 12.3/24.0GB
- LADA: 5/10 done (5 left) | clip.mp4 | 47% · ETA 30:47 · 112fps
- JASNA: 2/8 done (5 left) | film-破解.mp4 | 识别 43% · 约剩 6 分 | 修复 3/8 · subs 1/8 (1 failed)
- AV-LIB: 12/40 done (27 left) | sub/film 01.mp4 | 识别 43% · 约剩 6 分
- ComfyUI: 2 running, 100 pending
## SkyPig: OFFLINE (last seen 09:15:30)
```

A Jasna task with「AV 翻译」on appends `修复 R/T · subs S/T` (`R` = `queue_restored`;
` (N failed)` when any subtitle failed) after its queue segment; without subtitle
metrics the line is exactly the lada format. An `avsubs` task renders the lada format
(its `current_file` is the relative path being transcribed).

Since 3.7.0 (#189) both also show the focus film's live stage, after the file and
progress parts and before the counts:

```
- JASNA: 0/1 done (1 left) | SDAB-312.mp4 | 57% · ETA 7:18 · 157fps | 修复 0/1 · subs 0/1
- JASNA: 0/1 done (1 left) | SDAB-312-破解.mp4 | 识别 43% · 约剩 6 分 | 修复 1/1 · subs 0/1
- JASNA: 0/1 done (1 left) | SDAB-312-破解.mp4 | 识别 · 已用 4 分 | 修复 1/1 · subs 0/1
- JASNA: 0/1 done (1 left) | 翻译 56% · 约剩 1 分 · grok-4.3 · api.x.ai | 修复 1/1 · subs 0/1
- JASNA: 0/2 done (2 left) | 等待 GPU（AV） | 修复 0/2 · subs 0/2
- AV: 21/63 done (41 left) | 2024/ABC-123.mp4 | 识别 43% · 约剩 6 分
```

The stage is taken from the focus film's active step, else its step waiting for the
GPU: a restore adds nothing (the `P% · ETA · fps` part covers it); transcription reads
`识别 P% · 约剩 N 分`, `识别 P%` before an estimate exists, or `识别 · 已用 N 分` for an
engine without progress; translation reads `翻译 P% · 约剩 N 分 · <model>`; a GPU wait
reads `等待 GPU（<holder>）`, or `等待 GPU` when the GPU is free and reserved for this
task. Minutes are whole minutes and never 0 (remaining rounded up, elapsed rounded
down). A queued, pending or finished focus step adds nothing.

A monitor renders as `- <name>: disabled` only when it is genuinely not running (a
configured-but-unstarted stub). All names/values are sanitized (control chars → space,
capped) so a filename can't inject fake lines.

## Version note — RAM in GB

`mem_used_mb` / `mem_total_mb` (absolute RAM) were added in the desktop build that
introduced this guide. **Older agents report only `mem_pct` (percentage).** If those
fields are `None`, that agent predates the change — upgrade it, or fall back to
`mem_pct`. All other fields (CPU, GPU, VRAM, queue) are available on all V3 agents.
