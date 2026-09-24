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

        # lada / jasna: type_id == "lada"/"jasna", or a queue_total in metrics
        if tid in ("lada", "jasna") or num(met, "queue_total") is not None:
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
            # jasna「AV 翻译」(#177) — present only when the task has it ticked:
            phase       = met.get("phase")             # "restore" | "subs" | "translate"
            subs_done   = num(met, "subs_completed")
            subs_total  = num(met, "subs_total")
            subs_failed = num(met, "subs_failed")
            translating = num(met, "subs_translating") # files queued/in flight

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
| Queue done / total / left | `queue_completed` / `queue_total` / `queue_remaining` | lada / jasna | |
| Queue failed | `queue_failed` | jasna | int; files given up on after their retries (plus output-name collisions) |
| Current task | `current_file` | lada / jasna | string; capture mode or folder-derived |
| Current-file % | `percent` | lada / jasna | 0..100; **capture mode only** |
| ETA / elapsed | `eta` / `elapsed` | lada / jasna | string `MM:SS`/`H:MM:SS`; **capture mode only** |
| Frames done / left | `processed_frames` / `remaining_frames` | lada / jasna | int; **capture mode only** |
| Speed (fps) | `fps` | lada / jasna | float; **capture mode only** |
| Phase | `phase` | jasna | `restore` (a video is being restored, or idle), `subs` (WhisperJAV is transcribing), `translate` (only translations are running) |
| Subtitles done / total / left | `subs_completed` / `subs_total` / `subs_remaining` | jasna | int; only with「AV 翻译」ticked |
| Subtitles failed / skipped | `subs_failed` / `subs_skipped` | jasna | int; skipped = restore failed, no LLM key, source changed, cancelled, or whisperjav.exe missing |
| Translations pending | `subs_translating` | jasna | int; files queued in or held by the translator |
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
> **Jasna「AV 翻译」(#177).** With the tickbox on, every restored film also gets
> `<name>_restored.ja.srt` (Japanese) and `<name>_restored.srt` (Simplified Chinese)
> **next to `<name>_restored.mp4`** in the output folder, and the snapshot adds
> `phase` plus the `subs_*` keys above. `current_file` follows the live child: the
> video being restored in `phase == "restore"`, the `<name>_restored.mp4` being
> transcribed in `phase == "subs"`, and **absent** in `phase == "translate"` (no GPU
> child is running then). The batch `done` event text gains
> `| Subs: S/T done, U failed, V skipped`.

## Three rules that bite

1. **Judge "is it running" from `state` + `metrics`, never from `enabled`.** `enabled`
   is *config intent*; a monitor can be actively running (live `state`/`metrics`) while
   its config still says `enabled: false`. Reading `enabled` will show a busy worker as
   "disabled".
2. **Filter every number** — a metric can be `NaN` or the string `"n/a"` (e.g. GPU on
   macOS). Use the `num()` helper above.
3. **Identify a monitor by `type_id` first**, falling back to a metric signature only
   for pre-`type_id` agents (`disk_pct` ⇒ host; `queue_total` ⇒ lada/jasna; both
   `running`+`pending` ⇒ comfyui).

## `status.md` format (the secondary source)

```
# TaskPaw Hub Status

Last updated: YYYY-MM-DD HH:MM:SS

## PinkPig: ONLINE
- PinkPig-host: CPU 45% | RAM 8.2/16.0GB | GPU 78% | VRAM 12.3/24.0GB
- LADA: 5/10 done (5 left) | clip.mp4 | 47% · ETA 30:47 · 112fps
- JASNA: 3/8 done (5 left) | film.mp4 | subs 2/3 (1 failed)
- ComfyUI: 2 running, 100 pending
## SkyPig: OFFLINE (last seen 09:15:30)
```

A Jasna task with「AV 翻译」on appends `subs S/T` (and ` (N failed)` when any failed)
after its queue segment; without subtitle metrics the line is exactly the lada format.

A monitor renders as `- <name>: disabled` only when it is genuinely not running (a
configured-but-unstarted stub). All names/values are sanitized (control chars → space,
capped) so a filename can't inject fake lines.

## Version note — RAM in GB

`mem_used_mb` / `mem_total_mb` (absolute RAM) were added in the desktop build that
introduced this guide. **Older agents report only `mem_pct` (percentage).** If those
fields are `None`, that agent predates the change — upgrade it, or fall back to
`mem_pct`. All other fields (CPU, GPU, VRAM, queue) are available on all V3 agents.
