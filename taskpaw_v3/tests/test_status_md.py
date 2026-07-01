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


def test_current_file_newline_cannot_inject_lines():
    # A filename with newlines must not break the line-oriented status.md or inject
    # a fake monitor/server line (Kimi).
    evil = "real.mp4\n## FakeServer: ONLINE\n- fake: pwned"
    rows = [{"name": "b", "reachable": 1, "status_json": json.dumps({"monitors": {
        "LADA": {"state": "running", "type_id": "lada",
                 "metrics": {"queue_total": 2, "queue_completed": 1, "current_file": evil}}}})}]
    md = render_status_md(rows, "t")
    # no injected server-header / monitor line — the evil content is flattened into
    # the single LADA line (newlines → spaces), not new lines.
    assert not any(ln.strip().startswith("## FakeServer") for ln in md.splitlines())
    lada_lines = [ln for ln in md.splitlines() if ln.startswith("- LADA:")]
    assert len(lada_lines) == 1 and "1/2 done" in lada_lines[0]


def test_monitor_and_state_names_cannot_inject_lines():
    # Newlines in a monitor name or state must not inject fake ## / - lines (Kimi).
    rows = [{"name": "srv", "reachable": 1, "status_json": json.dumps({"monitors": {
        "evil\n## Fake: ONLINE\n- x: ok": {"state": "weird\n- y: pwned", "metrics": {}},
    }})}]
    md = render_status_md(rows, "t")
    assert not any(ln.strip().startswith("## Fake") for ln in md.splitlines())
    # exactly one server header (srv) and no injected monitor line "- y:"/"- x:"
    assert sum(1 for ln in md.splitlines() if ln.startswith("## ")) == 1
    assert not any(ln.startswith("- y:") or ln.startswith("- x:") for ln in md.splitlines())


def test_server_name_newline_cannot_inject():
    rows = [{"name": "box\n## Fake: ONLINE", "reachable": 0, "last_seen": "2026-07-01 09:00:00"}]
    md = render_status_md(rows, "t")
    assert not any(ln.strip().startswith("## Fake") for ln in md.splitlines())


def test_lada_nonstring_current_file_ignored():
    # A non-string current_file must not be interpolated verbatim (Kimi).
    rows = [{"name": "box", "reachable": 1,
             "status_json": json.dumps({"monitors": {
                 "LADA": {"state": "running", "type_id": "lada",
                          "metrics": {"queue_total": 4, "queue_completed": 1, "current_file": 123}}}})}]
    md = render_status_md(rows, "t")
    assert "1/4 done (3 left)" in md and "123" not in md


def test_nan_metric_not_rendered():
    # A non-finite metric (e.g. nvidia-smi "nan" gpu_pct) must not render "GPU nan%".
    rows = [{"name": "box", "reachable": 1,
             "status_json": json.dumps({"monitors": {
                 "box-host": {"state": "ok", "type_id": "host_metrics",
                              "metrics": {"cpu_pct": 10.0, "mem_used_mb": 8000, "mem_total_mb": 16000,
                                          "gpu_pct": float("nan")}}}})}]
    md = render_status_md(rows, "t")
    assert "CPU 10%" in md and "nan" not in md.lower() and "GPU" not in md


def test_comfyui_down_shows_state_not_empty_queue():
    # A typed ComfyUI monitor that's down (state error, empty metrics) must show its
    # state, not "0 running, 0 pending" — else the outage is hidden (Codex + Kimi).
    rows = [{"name": "box", "reachable": 1,
             "status_json": json.dumps({"monitors": {
                 "ComfyUI": {"state": "error", "type_id": "comfyui", "metrics": {}}}})}]
    md = render_status_md(rows, "t")
    assert "- ComfyUI: error" in md and "running" not in md


def test_malformed_queue_metrics_do_not_crash():
    # A valid queue_total with a NaN/"abc" queue_completed/remaining (or one of
    # running/pending non-finite) must not raise and stop status.md (Codex + Kimi).
    rows = [{"name": "b", "reachable": 1, "status_json": json.dumps({"monitors": {
        "LADA": {"state": "running", "type_id": "lada",
                 "metrics": {"queue_total": 10, "queue_completed": float("nan"), "queue_remaining": "abc"}},
        "ComfyUI": {"state": "running", "type_id": "comfyui",
                    "metrics": {"running": 2, "pending": float("nan")}},
    }})}]
    md = render_status_md(rows, "t")  # must not raise
    assert "0/10 done (10 left)" in md and "2 running, 0 pending" in md


def test_degraded_keeps_metrics():
    # `degraded` is an active-alert state (not an outage) → metrics still render,
    # for host and task plugins alike.
    host = [{"name": "b", "reachable": 1, "status_json": json.dumps({"monitors": {
        "b-host": {"state": "degraded", "type_id": "host_metrics",
                   "metrics": {"cpu_pct": 95.0, "mem_used_mb": 8000, "mem_total_mb": 16000}}}})}]
    assert "CPU 95%" in render_status_md(host, "t")
    lada = [{"name": "b", "reachable": 1, "status_json": json.dumps({"monitors": {
        "LADA": {"state": "degraded", "type_id": "lada",
                 "metrics": {"queue_total": 4, "queue_completed": 1}}}})}]
    assert "1/4 done (3 left)" in render_status_md(lada, "t")


def test_host_error_shows_state_but_degraded_keeps_metrics():
    # A host_metrics monitor in a hard-bad state (error/unreachable) surfaces the
    # state, not stale CPU/RAM; but "degraded" (its normal threshold alert) keeps
    # rendering metrics (Kimi).
    def row(state):
        return [{"name": "b", "reachable": 1, "status_json": json.dumps({"monitors": {
            "b-host": {"state": state, "type_id": "host_metrics",
                       "metrics": {"cpu_pct": 95.0, "mem_used_mb": 8000, "mem_total_mb": 16000}}}})}]
    assert "- b-host: error" in render_status_md(row("error"), "t")
    assert "CPU" not in render_status_md(row("error"), "t")
    # degraded keeps the metrics
    assert "CPU 95%" in render_status_md(row("degraded"), "t")


def test_task_error_state_with_stale_metrics_shows_state():
    # Plugins emit metrics even in error states; a bad state must surface as the
    # state, not a stale queue sample, so the outage isn't masked (Kimi).
    rows = [{"name": "box", "reachable": 1,
             "status_json": json.dumps({"monitors": {
                 "LADA": {"state": "error", "type_id": "lada",
                          "metrics": {"queue_total": 10, "queue_completed": 3}},
                 "ComfyUI": {"state": "error", "type_id": "comfyui",
                             "metrics": {"running": 0, "pending": 5}},
             }})}]
    md = render_status_md(rows, "t")
    assert "- LADA: error" in md and "done" not in md
    assert "- ComfyUI: error" in md and "pending" not in md


def test_host_without_type_id_still_renders_metrics():
    # Older V3 agents omit type_id on the host monitor; detect host by its
    # exclusive disk_pct signature so CPU/GPU/VRAM still render (real fleet data).
    rows = [{"name": "BGP", "reachable": 1, "status_json": json.dumps({"monitors": {
        "BGP-host": {"state": "ok", "metrics": {  # NO type_id
            "cpu_pct": 21.8, "mem_pct": 62.9, "disk_pct": 35.3,
            "gpu_pct": 33.0, "gpu_mem_used_mb": 2955, "gpu_mem_total_mb": 8151}}}})}]
    md = render_status_md(rows, "t")
    assert "CPU 22%" in md and "GPU 33%" in md and "VRAM 2.9/8.0GB" in md
    assert "- BGP-host: ok" not in md  # not the bare state


def test_running_monitor_with_stale_disabled_flag_shows_data_not_disabled():
    # A LADA that's actually running (live state + queue metrics) but whose config
    # `enabled` flag is false must show its queue, NOT "disabled" — status.md has to
    # match the DB/UI live view (the "LADA shows disabled while running" bug).
    rows = [{"name": "BlackGoldPig", "reachable": 1, "status_json": json.dumps({"monitors": {
        "LADA": {"state": "running", "type_id": "lada", "enabled": False,
                 "metrics": {"queue_total": 12, "queue_completed": 0, "queue_remaining": 12,
                             "current_file": "SSNI-456-C.wmv"}}}})}]
    md = render_status_md(rows, "t")
    assert "- LADA: 0/12 done (12 left)" in md and "disabled" not in md
    # a genuinely not-running stub (state stopped, no metrics) still shows disabled
    stub = [{"name": "b", "reachable": 1, "status_json": json.dumps({"monitors": {
        "LADA": {"state": "stopped", "type_id": "lada", "enabled": False, "metrics": {}}}})}]
    assert "- LADA: disabled" in render_status_md(stub, "t")


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
