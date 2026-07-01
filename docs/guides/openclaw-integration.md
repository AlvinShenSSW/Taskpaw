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

        # lada: type_id == "lada", or a queue_total in metrics
        if tid == "lada" or num(met, "queue_total") is not None:
            done, total = num(met, "queue_completed"), num(met, "queue_total")
            left, cur   = num(met, "queue_remaining"), met.get("current_file")
            running     = state == "running"          # ← judge by state, NOT enabled

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
| Lada done / total / left | `queue_completed` / `queue_total` / `queue_remaining` | lada | |
| Lada current task | `current_file` | lada | string |
| ComfyUI running / pending | `running` / `pending` | comfyui | |
| **Running?** | top-level **`state`** (`running`/`idle`/`ok`/`error`/`stopped`) | any | **use this, not `enabled`** |

Top-level of each `status_json`: `machine` (display name), `os`, `server_id`.

## Three rules that bite

1. **Judge "is it running" from `state` + `metrics`, never from `enabled`.** `enabled`
   is *config intent*; a monitor can be actively running (live `state`/`metrics`) while
   its config still says `enabled: false`. Reading `enabled` will show a busy worker as
   "disabled".
2. **Filter every number** — a metric can be `NaN` or the string `"n/a"` (e.g. GPU on
   macOS). Use the `num()` helper above.
3. **Identify a monitor by `type_id` first**, falling back to a metric signature only
   for pre-`type_id` agents (`disk_pct` ⇒ host; `queue_total` ⇒ lada; both
   `running`+`pending` ⇒ comfyui).

## `status.md` format (the secondary source)

```
# TaskPaw Hub Status

Last updated: YYYY-MM-DD HH:MM:SS

## PinkPig: ONLINE
- PinkPig-host: CPU 45% | RAM 8.2/16.0GB | GPU 78% | VRAM 12.3/24.0GB
- LADA: 5/10 done (5 left) | clip.mp4 |
- ComfyUI: 2 running, 100 pending
## SkyPig: OFFLINE (last seen 09:15:30)
```

A monitor renders as `- <name>: disabled` only when it is genuinely not running (a
configured-but-unstarted stub). All names/values are sanitized (control chars → space,
capped) so a filename can't inject fake lines.

## Version note — RAM in GB

`mem_used_mb` / `mem_total_mb` (absolute RAM) were added in the desktop build that
introduced this guide. **Older agents report only `mem_pct` (percentage).** If those
fields are `None`, that agent predates the change — upgrade it, or fall back to
`mem_pct`. All other fields (CPU, GPU, VRAM, queue) are available on all V3 agents.
