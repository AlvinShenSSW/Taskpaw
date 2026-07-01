"""status_md edge cases not covered by test_openclaw_compat.py (#115).

The main V2-format / ONLINE-OFFLINE / list-shape / atomic-write paths are already
covered there; this adds the disabled-monitor branch and the last_seen fallback."""

from __future__ import annotations

import json

from taskpaw_v3.hub.server.status_md import render_status_md


def test_v3_metrics_render_in_v2_format_for_openclaw():
    # V3 keeps state + a structured metrics dict; status.md must re-embed the
    # metrics as V2-format strings so the OpenClaw daily-report regexes (CPU %, RAM
    # used/total GB, GPU %, VRAM, "X/Y done (Z left)", "N running, M pending") match.
    rows = [{
        "name": "PinkPig", "reachable": 1,
        "status_json": json.dumps({"monitors": {
            "PinkPig-host": {"state": "ok", "metrics": {
                "cpu_pct": 45.0, "mem_pct": 51.0, "mem_used_mb": 8393, "mem_total_mb": 16384,
                "gpu_pct": 78, "gpu_mem_used_mb": 12595, "gpu_mem_total_mb": 24576}},
            "LADA": {"state": "running", "metrics": {
                "queue_completed": 5, "queue_total": 10, "queue_remaining": 5,
                "current_file": "video_q3.mp4"}},
            "ComfyUI": {"state": "running", "metrics": {"running": 2, "pending": 100}},
        }}),
    }]
    md = render_status_md(rows, "2026-07-01 16:00:00")
    assert "- PinkPig-host: CPU 45% | RAM 8.2/16.0GB | GPU 78% | VRAM 12.3/24.0GB" in md
    assert "- LADA: 5/10 done (5 left) | video_q3.mp4 |" in md
    assert "- ComfyUI: 2 running, 100 pending" in md


def test_v3_no_metrics_falls_back_to_state():
    # A monitor with no numeric metrics still renders its state (no crash / no "").
    rows = [{"name": "box", "reachable": 1,
             "status_json": json.dumps({"monitors": {"heartbeat": {"state": "ok", "metrics": {}}}})}]
    assert "- heartbeat: ok" in render_status_md(rows, "t")


def test_v3_host_metrics_missing_mem_fields_falls_back_to_state():
    # A host_metrics monitor identified by type_id but with empty/partial metrics
    # (startup stub, disabled stub) must NOT KeyError on the RAM fields — it renders
    # its state instead, so status.md keeps updating (Codex + Kimi).
    rows = [{"name": "box", "reachable": 1,
             "status_json": json.dumps({"monitors": {
                 "box-host": {"state": "unknown", "type_id": "host_metrics", "metrics": {}}}})}]
    assert "- box-host: unknown" in render_status_md(rows, "t")
    # partial metrics: CPU present, RAM absent → CPU renders, no crash, no RAM seg.
    rows2 = [{"name": "box", "reachable": 1,
              "status_json": json.dumps({"monitors": {
                  "box-host": {"state": "ok", "type_id": "host_metrics", "metrics": {"cpu_pct": 20.0}}}})}]
    md = render_status_md(rows2, "t")
    assert "- box-host: CPU 20%" in md and "RAM" not in md


def test_folder_pending_not_rendered_as_comfyui_queue():
    # A folder monitor emits metrics={"pending": N} while files stabilize; type_id
    # keeps it from being classified as a ComfyUI queue (Codex).
    rows = [{"name": "box", "reachable": 1,
             "status_json": json.dumps({"monitors": {
                 "dl": {"state": "ok", "type_id": "folder", "metrics": {"pending": 3}}}})}]
    md = render_status_md(rows, "t")
    assert "- dl: ok" in md and "running" not in md


def test_v3_dict_disabled_monitor_renders_disabled():
    # V3 dict snapshot with enabled:False → "disabled", not its stale state (Kimi).
    rows = [{"name": "box", "reachable": 1,
             "status_json": json.dumps({"monitors": {
                 "off": {"state": "stopped", "type_id": "process", "enabled": False}}})}]
    assert "- off: disabled" in render_status_md(rows, "t")


def test_lada_nonstring_current_file_ignored():
    # A non-string current_file must not be interpolated verbatim (Kimi).
    rows = [{"name": "box", "reachable": 1,
             "status_json": json.dumps({"monitors": {
                 "LADA": {"state": "running", "type_id": "lada",
                          "metrics": {"queue_total": 4, "queue_completed": 1, "current_file": 123}}}})}]
    md = render_status_md(rows, "t")
    assert "1/4 done (3 left)" in md and "123" not in md


def test_v2_list_shape_renders_disabled_monitor():
    # V2 agents report a list with `enabled: False` for stopped monitors → status.md
    # shows them as "disabled" rather than their (stale) state.
    rows = [{
        "name": "box", "reachable": 1,
        "status_json": json.dumps({"monitors": [
            {"name": "live", "state": "ok"},
            {"name": "off", "state": "ok", "enabled": False},
        ]}),
    }]
    md = render_status_md(rows, "now")
    assert "- live: ok" in md
    assert "- off: disabled" in md


def test_offline_with_unparseable_last_seen_falls_back_to_raw():
    # A last_seen that isn't the "%Y-%m-%d %H:%M:%S" shape is echoed verbatim rather
    # than dropped or crashing.
    rows = [{"name": "box", "reachable": 0, "last_seen": "just now", "status_json": None}]
    md = render_status_md(rows, "now")
    assert "## box: OFFLINE (last seen just now)" in md


def test_offline_without_last_seen_omits_the_parenthetical():
    rows = [{"name": "box", "reachable": 0, "last_seen": None, "status_json": None}]
    md = render_status_md(rows, "now")
    assert "## box: OFFLINE" in md
    assert "last seen" not in md
