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
    rows = [
        {
            "name": "PinkPig",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "PinkPig-host": {
                            "state": "ok",
                            "metrics": {
                                "cpu_pct": 45.0,
                                "mem_pct": 51.0,
                                "mem_used_mb": 8393,
                                "mem_total_mb": 16384,
                                "gpu_pct": 78,
                                "gpu_mem_used_mb": 12595,
                                "gpu_mem_total_mb": 24576,
                            },
                        },
                        "LADA": {
                            "state": "running",
                            "metrics": {
                                "queue_completed": 5,
                                "queue_total": 10,
                                "queue_remaining": 5,
                                "current_file": "video_q3.mp4",
                            },
                        },
                        "ComfyUI": {
                            "state": "running",
                            "metrics": {"running": 2, "pending": 100},
                        },
                    }
                }
            ),
        }
    ]
    md = render_status_md(rows, "2026-07-01 16:00:00")
    assert "- PinkPig-host: CPU 45% | RAM 8.2/16.0GB | GPU 78% | VRAM 12.3/24.0GB" in md
    assert "- LADA: 5/10 done (5 left) | video_q3.mp4 |" in md
    assert "- ComfyUI: 2 running, 100 pending" in md


def test_v3_no_metrics_falls_back_to_state():
    # A monitor with no numeric metrics still renders its state (no crash / no "").
    rows = [
        {
            "name": "box",
            "reachable": 1,
            "status_json": json.dumps(
                {"monitors": {"heartbeat": {"state": "ok", "metrics": {}}}}
            ),
        }
    ]
    assert "- heartbeat: ok" in render_status_md(rows, "t")


def test_v3_host_metrics_missing_mem_fields_falls_back_to_state():
    # A host_metrics monitor identified by type_id but with empty/partial metrics
    # (startup stub, disabled stub) must NOT KeyError on the RAM fields — it renders
    # its state instead, so status.md keeps updating (Codex + Kimi).
    rows = [
        {
            "name": "box",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "box-host": {
                            "state": "unknown",
                            "type_id": "host_metrics",
                            "metrics": {},
                        }
                    }
                }
            ),
        }
    ]
    assert "- box-host: unknown" in render_status_md(rows, "t")
    # partial metrics: CPU present, RAM absent → CPU renders, no crash, no RAM seg.
    rows2 = [
        {
            "name": "box",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "box-host": {
                            "state": "ok",
                            "type_id": "host_metrics",
                            "metrics": {"cpu_pct": 20.0},
                        }
                    }
                }
            ),
        }
    ]
    md = render_status_md(rows2, "t")
    assert "- box-host: CPU 20%" in md and "RAM" not in md


def test_folder_pending_not_rendered_as_comfyui_queue():
    # A folder monitor emits metrics={"pending": N} while files stabilize; type_id
    # keeps it from being classified as a ComfyUI queue (Codex).
    rows = [
        {
            "name": "box",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "dl": {
                            "state": "ok",
                            "type_id": "folder",
                            "metrics": {"pending": 3},
                        }
                    }
                }
            ),
        }
    ]
    md = render_status_md(rows, "t")
    assert "- dl: ok" in md and "running" not in md


def test_v3_dict_disabled_monitor_renders_disabled():
    # V3 dict snapshot with enabled:False → "disabled", not its stale state (Kimi).
    rows = [
        {
            "name": "box",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "off": {
                            "state": "stopped",
                            "type_id": "process",
                            "enabled": False,
                        }
                    }
                }
            ),
        }
    ]
    assert "- off: disabled" in render_status_md(rows, "t")


def test_current_file_newline_cannot_inject_lines():
    # A filename with newlines must not break the line-oriented status.md or inject
    # a fake monitor/server line (Kimi).
    evil = "real.mp4\n## FakeServer: ONLINE\n- fake: pwned"
    rows = [
        {
            "name": "b",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "LADA": {
                            "state": "running",
                            "type_id": "lada",
                            "metrics": {
                                "queue_total": 2,
                                "queue_completed": 1,
                                "current_file": evil,
                            },
                        }
                    }
                }
            ),
        }
    ]
    md = render_status_md(rows, "t")
    # no injected server-header / monitor line — the evil content is flattened into
    # the single LADA line (newlines → spaces), not new lines.
    assert not any(ln.strip().startswith("## FakeServer") for ln in md.splitlines())
    lada_lines = [ln for ln in md.splitlines() if ln.startswith("- LADA:")]
    assert len(lada_lines) == 1 and "1/2 done" in lada_lines[0]


def test_monitor_and_state_names_cannot_inject_lines():
    # Newlines in a monitor name or state must not inject fake ## / - lines (Kimi).
    rows = [
        {
            "name": "srv",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "evil\n## Fake: ONLINE\n- x: ok": {
                            "state": "weird\n- y: pwned",
                            "metrics": {},
                        },
                    }
                }
            ),
        }
    ]
    md = render_status_md(rows, "t")
    assert not any(ln.strip().startswith("## Fake") for ln in md.splitlines())
    # exactly one server header (srv) and no injected monitor line "- y:"/"- x:"
    assert sum(1 for ln in md.splitlines() if ln.startswith("## ")) == 1
    assert not any(
        ln.startswith("- y:") or ln.startswith("- x:") for ln in md.splitlines()
    )


def test_server_name_newline_cannot_inject():
    rows = [
        {
            "name": "box\n## Fake: ONLINE",
            "reachable": 0,
            "last_seen": "2026-07-01 09:00:00",
        }
    ]
    md = render_status_md(rows, "t")
    assert not any(ln.strip().startswith("## Fake") for ln in md.splitlines())


def test_lada_nonstring_current_file_ignored():
    # A non-string current_file must not be interpolated verbatim (Kimi).
    rows = [
        {
            "name": "box",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "LADA": {
                            "state": "running",
                            "type_id": "lada",
                            "metrics": {
                                "queue_total": 4,
                                "queue_completed": 1,
                                "current_file": 123,
                            },
                        }
                    }
                }
            ),
        }
    ]
    md = render_status_md(rows, "t")
    assert "1/4 done (3 left)" in md and "123" not in md


def test_nan_metric_not_rendered():
    # A non-finite metric (e.g. nvidia-smi "nan" gpu_pct) must not render "GPU nan%".
    rows = [
        {
            "name": "box",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "box-host": {
                            "state": "ok",
                            "type_id": "host_metrics",
                            "metrics": {
                                "cpu_pct": 10.0,
                                "mem_used_mb": 8000,
                                "mem_total_mb": 16000,
                                "gpu_pct": float("nan"),
                            },
                        }
                    }
                }
            ),
        }
    ]
    md = render_status_md(rows, "t")
    assert "CPU 10%" in md and "nan" not in md.lower() and "GPU" not in md


def test_comfyui_down_shows_state_not_empty_queue():
    # A typed ComfyUI monitor that's down (state error, empty metrics) must show its
    # state, not "0 running, 0 pending" — else the outage is hidden (Codex + Kimi).
    rows = [
        {
            "name": "box",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "ComfyUI": {
                            "state": "error",
                            "type_id": "comfyui",
                            "metrics": {},
                        }
                    }
                }
            ),
        }
    ]
    md = render_status_md(rows, "t")
    assert "- ComfyUI: error" in md and "running" not in md


def test_malformed_queue_metrics_do_not_crash():
    # A valid queue_total with a NaN/"abc" queue_completed/remaining (or one of
    # running/pending non-finite) must not raise and stop status.md (Codex + Kimi).
    rows = [
        {
            "name": "b",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "LADA": {
                            "state": "running",
                            "type_id": "lada",
                            "metrics": {
                                "queue_total": 10,
                                "queue_completed": float("nan"),
                                "queue_remaining": "abc",
                            },
                        },
                        "ComfyUI": {
                            "state": "running",
                            "type_id": "comfyui",
                            "metrics": {"running": 2, "pending": float("nan")},
                        },
                    }
                }
            ),
        }
    ]
    md = render_status_md(rows, "t")  # must not raise
    assert "0/10 done (10 left)" in md and "2 running, 0 pending" in md


def test_degraded_keeps_metrics():
    # `degraded` is an active-alert state (not an outage) → metrics still render,
    # for host and task plugins alike.
    host = [
        {
            "name": "b",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "b-host": {
                            "state": "degraded",
                            "type_id": "host_metrics",
                            "metrics": {
                                "cpu_pct": 95.0,
                                "mem_used_mb": 8000,
                                "mem_total_mb": 16000,
                            },
                        }
                    }
                }
            ),
        }
    ]
    assert "CPU 95%" in render_status_md(host, "t")
    lada = [
        {
            "name": "b",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "LADA": {
                            "state": "degraded",
                            "type_id": "lada",
                            "metrics": {"queue_total": 4, "queue_completed": 1},
                        }
                    }
                }
            ),
        }
    ]
    assert "1/4 done (3 left)" in render_status_md(lada, "t")


def test_host_error_shows_state_but_degraded_keeps_metrics():
    # A host_metrics monitor in a hard-bad state (error/unreachable) surfaces the
    # state, not stale CPU/RAM; but "degraded" (its normal threshold alert) keeps
    # rendering metrics (Kimi).
    def row(state):
        return [
            {
                "name": "b",
                "reachable": 1,
                "status_json": json.dumps(
                    {
                        "monitors": {
                            "b-host": {
                                "state": state,
                                "type_id": "host_metrics",
                                "metrics": {
                                    "cpu_pct": 95.0,
                                    "mem_used_mb": 8000,
                                    "mem_total_mb": 16000,
                                },
                            }
                        }
                    }
                ),
            }
        ]

    assert "- b-host: error" in render_status_md(row("error"), "t")
    assert "CPU" not in render_status_md(row("error"), "t")
    # degraded keeps the metrics
    assert "CPU 95%" in render_status_md(row("degraded"), "t")


def test_task_error_state_with_stale_metrics_shows_state():
    # Plugins emit metrics even in error states; a bad state must surface as the
    # state, not a stale queue sample, so the outage isn't masked (Kimi).
    rows = [
        {
            "name": "box",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "LADA": {
                            "state": "error",
                            "type_id": "lada",
                            "metrics": {"queue_total": 10, "queue_completed": 3},
                        },
                        "ComfyUI": {
                            "state": "error",
                            "type_id": "comfyui",
                            "metrics": {"running": 0, "pending": 5},
                        },
                    }
                }
            ),
        }
    ]
    md = render_status_md(rows, "t")
    assert "- LADA: error" in md and "done" not in md
    assert "- ComfyUI: error" in md and "pending" not in md


def test_host_without_type_id_still_renders_metrics():
    # Older V3 agents omit type_id on the host monitor; detect host by its
    # exclusive disk_pct signature so CPU/GPU/VRAM still render (real fleet data).
    rows = [
        {
            "name": "BGP",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "BGP-host": {
                            "state": "ok",
                            "metrics": {  # NO type_id
                                "cpu_pct": 21.8,
                                "mem_pct": 62.9,
                                "disk_pct": 35.3,
                                "gpu_pct": 33.0,
                                "gpu_mem_used_mb": 2955,
                                "gpu_mem_total_mb": 8151,
                            },
                        }
                    }
                }
            ),
        }
    ]
    md = render_status_md(rows, "t")
    assert "CPU 22%" in md and "GPU 33%" in md and "VRAM 2.9/8.0GB" in md
    assert "- BGP-host: ok" not in md  # not the bare state


def test_running_monitor_with_stale_disabled_flag_shows_data_not_disabled():
    # A LADA that's actually running (live state + queue metrics) but whose config
    # `enabled` flag is false must show its queue, NOT "disabled" — status.md has to
    # match the DB/UI live view (the "LADA shows disabled while running" bug).
    rows = [
        {
            "name": "BlackGoldPig",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "LADA": {
                            "state": "running",
                            "type_id": "lada",
                            "enabled": False,
                            "metrics": {
                                "queue_total": 12,
                                "queue_completed": 0,
                                "queue_remaining": 12,
                                "current_file": "SSNI-456-C.wmv",
                            },
                        }
                    }
                }
            ),
        }
    ]
    md = render_status_md(rows, "t")
    assert "- LADA: 0/12 done (12 left)" in md and "disabled" not in md
    # a genuinely not-running stub (state stopped, no metrics) still shows disabled
    stub = [
        {
            "name": "b",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "LADA": {
                            "state": "stopped",
                            "type_id": "lada",
                            "enabled": False,
                            "metrics": {},
                        }
                    }
                }
            ),
        }
    ]
    assert "- LADA: disabled" in render_status_md(stub, "t")


def test_v2_list_shape_renders_disabled_monitor():
    # V2 agents report a list with `enabled: False` for stopped monitors → status.md
    # shows them as "disabled" rather than their (stale) state.
    rows = [
        {
            "name": "box",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": [
                        {"name": "live", "state": "ok"},
                        {"name": "off", "state": "ok", "enabled": False},
                    ]
                }
            ),
        }
    ]
    md = render_status_md(rows, "now")
    assert "- live: ok" in md
    assert "- off: disabled" in md


def test_offline_with_unparseable_last_seen_falls_back_to_raw():
    # A last_seen that isn't the "%Y-%m-%d %H:%M:%S" shape is echoed verbatim rather
    # than dropped or crashing.
    rows = [
        {"name": "box", "reachable": 0, "last_seen": "just now", "status_json": None}
    ]
    md = render_status_md(rows, "now")
    assert "## box: OFFLINE (last seen just now)" in md


def test_offline_without_last_seen_omits_the_parenthetical():
    rows = [{"name": "box", "reachable": 0, "last_seen": None, "status_json": None}]
    md = render_status_md(rows, "now")
    assert "## box: OFFLINE" in md
    assert "last seen" not in md


# ── #161: lada per-task progress in status.md ─────────────────────────────────
def _lada_row(metrics: dict, state: str = "running") -> list[dict]:
    return [
        {
            "name": "box",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "LADA": {"state": state, "type_id": "lada", "metrics": metrics}
                    }
                }
            ),
        }
    ]


def _lada_line(md: str) -> str:
    return next(ln for ln in md.splitlines() if ln.startswith("- LADA:"))


def test_lada_per_task_progress_renders_after_queue():
    # #161: percent / ETA / fps appended after the queue+file segment, additively.
    md = render_status_md(
        _lada_row(
            {
                "queue_completed": 5,
                "queue_total": 10,
                "queue_remaining": 5,
                "current_file": "clip.mp4",
                "percent": 47,
                "eta": "30:47",
                "fps": 112.3,
            }
        ),
        "t",
    )
    line = _lada_line(md)
    # Legacy substrings stay byte-identical (V2 scrapers must keep matching).
    assert "5/10 done (5 left) | clip.mp4 |" in line
    assert "47%" in line
    assert "ETA 30:47" in line
    assert "112" in line  # fps


def test_lada_progress_renders_without_queue_counts():
    # #161 (Codex 外门): a managed Lada that supplies I/O via lada_extra_args has
    # capture-mode progress but no queue_total (_queue_counts reads the folder
    # fields). Progress must still render — not fall back to bare "running".
    md = render_status_md(
        _lada_row(
            {"current_file": "clip.mp4", "percent": 47, "eta": "30:47", "fps": 112.3}
        ),
        "t",
    )
    line = _lada_line(md)
    assert "| clip.mp4 |" in line
    assert "47%" in line and "ETA 30:47" in line and "112fps" in line
    assert "done" not in line and line != "- LADA: running"


def test_lada_capture_off_renders_queue_only_byte_identical():
    # Default (capture off): no per-task fields → the line is exactly today's.
    md = render_status_md(
        _lada_row(
            {
                "queue_completed": 5,
                "queue_total": 10,
                "queue_remaining": 5,
                "current_file": "clip.mp4",
            }
        ),
        "t",
    )
    assert _lada_line(md) == "- LADA: 5/10 done (5 left) | clip.mp4 |"


def test_lada_nonfinite_percent_and_fps_omitted_queue_survives():
    md = render_status_md(
        _lada_row(
            {
                "queue_total": 10,
                "queue_completed": 5,
                "current_file": "clip.mp4",
                "percent": float("nan"),
                "fps": "n/a",
                "eta": "30:47",
            }
        ),
        "t",
    )
    line = _lada_line(md)
    assert "5/10 done (5 left) | clip.mp4 |" in line
    assert (
        "nan" not in line
        and "n/a" not in line
        and "%" not in line
        and "fps" not in line
    )
    assert "ETA 30:47" in line  # a valid field still renders


def test_lada_out_of_range_percent_dropped():
    md = render_status_md(
        _lada_row({"queue_total": 4, "queue_completed": 1, "percent": 150}), "t"
    )
    line = _lada_line(md)
    assert "1/4 done (3 left)" in line
    assert "150%" not in line and "%" not in line


def test_lada_eta_control_chars_cannot_inject_lines():
    md = render_status_md(
        _lada_row(
            {"queue_total": 2, "queue_completed": 1, "eta": "30:47\n## HACKED: ONLINE"}
        ),
        "t",
    )
    # _inline() collapses the newline to a space, so the payload stays INLINE on
    # the single LADA line — it never becomes its own injected "## …" server line.
    lada_lines = [ln for ln in md.splitlines() if ln.startswith("- LADA:")]
    assert len(lada_lines) == 1
    assert not any(ln.startswith("##") and "HACKED" in ln for ln in md.splitlines())


def test_lada_error_state_hides_stale_progress():
    # A hard-bad state must surface the state, not stale per-task progress.
    md = render_status_md(
        _lada_row(
            {"queue_total": 10, "queue_completed": 3, "percent": 47, "fps": 100.0},
            state="error",
        ),
        "t",
    )
    line = _lada_line(md)
    assert "error" in line
    assert "47%" not in line and "done" not in line


# ── #173: a `jasna` snapshot renders through the same block as lada ───────────
def _jasna_row(metrics: dict, state: str = "running") -> list[dict]:
    return [
        {
            "name": "box",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "JASNA": {
                            "state": state,
                            "type_id": "jasna",
                            "metrics": metrics,
                        }
                    }
                }
            ),
        }
    ]


def test_jasna_renders_queue_and_progress_like_lada():
    md = render_status_md(
        _jasna_row(
            {
                "queue_completed": 5,
                "queue_total": 10,
                "queue_remaining": 4,
                "queue_failed": 1,
                "current_file": "clip.mp4",
                "percent": 47,
                "eta": "30:47",
                "fps": 112.3,
            }
        ),
        "t",
    )
    line = next(ln for ln in md.splitlines() if ln.startswith("- JASNA:"))
    assert "5/10 done (4 left) | clip.mp4 |" in line
    assert "47%" in line and "ETA 30:47" in line and "112fps" in line


def test_jasna_degraded_still_renders_metrics():
    # A 3-strike abort leaves the monitor `degraded` — an active-alert state, not
    # an outage, so the queue counts must still reach OpenClaw (#173).
    md = render_status_md(
        _jasna_row(
            {"queue_completed": 2, "queue_total": 6, "queue_remaining": 1},
            state="degraded",
        ),
        "t",
    )
    line = next(ln for ln in md.splitlines() if ln.startswith("- JASNA:"))
    assert "2/6 done (1 left)" in line


def test_malformed_unhashable_type_id_does_not_crash_rendering():
    # Codex 外门 (#173): a malformed agent may send a list/dict as `type_id`. The
    # lada/jasna discriminator must tolerate it (tuple membership, not a set
    # lookup that raises TypeError) — one bad monitor must never stall status.md.
    rows = [
        {
            "name": "box",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "WEIRD": {
                            "state": "running",
                            "type_id": ["lada"],
                            "metrics": {"queue_completed": 1, "queue_total": 2},
                        },
                        "JASNA": {
                            "state": "running",
                            "type_id": "jasna",
                            "metrics": {"queue_completed": 5, "queue_total": 10},
                        },
                    }
                }
            ),
        }
    ]
    md = render_status_md(rows, "t")
    assert "- JASNA: 5/10 done (5 left)" in md  # the good monitor still renders
    assert "- WEIRD:" in md  # the bad one degrades to a state line, no crash


# ── #177: Jasna「AV 翻译」appends a `subs S/T` part ─────────────────────────────
def _jasna_line(md: str) -> str:
    return next(ln for ln in md.splitlines() if ln.startswith("- JASNA:"))


def test_jasna_subs_part_appended_when_subs_total_present():
    md = render_status_md(
        _jasna_row(
            {
                "queue_completed": 5,
                "queue_total": 10,
                "queue_remaining": 5,
                "current_file": "clip.mp4",
                "subs_total": 5,
                "subs_completed": 3,
                "subs_failed": 0,
            }
        ),
        "t",
    )
    line = _jasna_line(md)
    # the legacy lada substring stays byte-identical, subs follows it
    assert line == "- JASNA: 5/10 done (5 left) | clip.mp4 | subs 3/5"


def test_jasna_subs_part_carries_the_failed_suffix():
    md = render_status_md(
        _jasna_row(
            {
                "queue_completed": 2,
                "queue_total": 4,
                "subs_total": 4,
                "subs_completed": 1,
                "subs_failed": 2,
            }
        ),
        "t",
    )
    assert _jasna_line(md) == "- JASNA: 2/4 done (2 left) | subs 1/4 (2 failed)"


def test_jasna_subs_part_alone_and_malformed_values():
    md = render_status_md(_jasna_row({"subs_total": 3}), "t")
    assert _jasna_line(md) == "- JASNA: subs 0/3"
    md = render_status_md(
        _jasna_row(
            {
                "queue_completed": 1,
                "queue_total": 2,
                "subs_total": float("nan"),
                "subs_completed": 1,
            }
        ),
        "t",
    )
    assert _jasna_line(md) == "- JASNA: 1/2 done (1 left)"
    md = render_status_md(
        _jasna_row({"subs_total": 2, "subs_completed": "x", "subs_failed": "y"}), "t"
    )
    assert _jasna_line(md) == "- JASNA: subs 0/2"


def test_lada_line_byte_identical_without_subs_metrics():
    md = render_status_md(
        _jasna_row(
            {
                "queue_completed": 5,
                "queue_total": 10,
                "queue_remaining": 5,
                "current_file": "clip.mp4",
            }
        ),
        "t",
    )
    assert _jasna_line(md) == "- JASNA: 5/10 done (5 left) | clip.mp4 |"
    md = render_status_md(
        _lada_row(
            {
                "queue_completed": 5,
                "queue_total": 10,
                "queue_remaining": 5,
                "current_file": "clip.mp4",
            }
        ),
        "t",
    )
    assert _lada_line(md) == "- LADA: 5/10 done (5 left) | clip.mp4 |"


def test_jasna_error_state_hides_the_subs_part():
    md = render_status_md(
        _jasna_row({"subs_total": 3, "subs_completed": 1}, state="error"), "t"
    )
    assert "subs" not in _jasna_line(md)


def test_jasna_subs_part_with_capture_progress_renders_each_fragment_once():
    # K-m8: progress fragment and subs fragment together, each exactly once.
    base = {
        "queue_completed": 5,
        "queue_total": 10,
        "queue_remaining": 5,
        "current_file": "clip.mp4",
        "percent": 47,
        "eta": "30:47",
        "fps": 112.3,
    }
    md = render_status_md(
        _jasna_row({**base, "subs_total": 4, "subs_completed": 2}), "t"
    )
    line = _jasna_line(md)
    progress = "47% · ETA 30:47 · 112fps"
    assert line.count(progress) == 1
    assert line.count("subs 2/4") == 1
    assert line == f"- JASNA: 5/10 done (5 left) | clip.mp4 | {progress} | subs 2/4"
    md = render_status_md(_lada_row(base), "t")
    assert _lada_line(md) == f"- LADA: 5/10 done (5 left) | clip.mp4 | {progress}"


# ── #179: an `avsubs` snapshot renders through the lada/jasna block ───────────
def _avsubs_row(metrics: dict, state: str = "running") -> list[dict]:
    return [
        {
            "name": "box",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        "AVSUBS": {
                            "state": state,
                            "type_id": "avsubs",
                            "metrics": metrics,
                        }
                    }
                }
            ),
        }
    ]


def _avsubs_line(md: str) -> str:
    return next(ln for ln in md.splitlines() if ln.startswith("- AVSUBS:"))


def test_avsubs_renders_queue_and_current_file_like_jasna():
    metrics = {
        "queue_completed": 3,
        "queue_total": 10,
        "queue_remaining": 6,
        "queue_failed": 1,
        "queue_skipped": 0,
        "current_file": "sub/film 01.mp4",
        "phase": "asr",
        "subs_translating": 2,
        "cpu_pct": 12.0,
        "gpu_pct": 80,
    }
    md = render_status_md(_avsubs_row(metrics), "t")
    assert _avsubs_line(md) == "- AVSUBS: 3/10 done (6 left) | sub/film 01.mp4 |"
    # exactly the line a jasna snapshot with the same keys gets
    md = render_status_md(_jasna_row(metrics), "t")
    assert _jasna_line(md) == "- JASNA: 3/10 done (6 left) | sub/film 01.mp4 |"


def test_avsubs_degraded_keeps_counts_and_error_hides_them():
    counts = {"queue_completed": 2, "queue_total": 6, "queue_remaining": 1}
    md = render_status_md(_avsubs_row(counts, state="degraded"), "t")
    assert _avsubs_line(md) == "- AVSUBS: 2/6 done (1 left)"
    md = render_status_md(_avsubs_row(counts, state="error"), "t")
    assert "done" not in _avsubs_line(md)


def test_avsubs_does_not_change_the_lada_line():
    base = {
        "queue_completed": 5,
        "queue_total": 10,
        "queue_remaining": 5,
        "current_file": "clip.mp4",
    }
    md = render_status_md(_lada_row(base), "t")
    assert _lada_line(md) == "- LADA: 5/10 done (5 left) | clip.mp4 |"


def test_unknown_state_hides_stale_metrics_like_other_outages():
    # #127: `unknown` joins error/stopped/unreachable — a monitor whose state is
    # unknown must surface that, never a stale metric sample (host or queue).
    def row(state, type_id, metrics):
        return [
            {
                "name": "b",
                "reachable": 1,
                "status_json": json.dumps(
                    {
                        "monitors": {
                            "m": {
                                "state": state,
                                "type_id": type_id,
                                "metrics": metrics,
                            }
                        }
                    }
                ),
            }
        ]

    host = {"cpu_pct": 95.0, "mem_used_mb": 8000, "mem_total_mb": 16000}
    queue = {"queue_total": 10, "queue_completed": 4, "queue_remaining": 6}
    out = render_status_md(row("unknown", "host_metrics", host), "t")
    assert "- m: unknown" in out and "95" not in out
    out = render_status_md(row("unknown", "jasna", queue), "t")
    assert "- m: unknown" in out and "4/10" not in out
    # a healthy state still renders the metrics (no regression)
    assert "4/10 done (6 left)" in render_status_md(row("running", "jasna", queue), "t")


# ── #189: the stage fragment + Jasna's `修复 a/b` count (D7) ─────────────────
def _line_of(name: str, type_id: str, metrics: dict, state: str = "running") -> str:
    rows = [
        {
            "name": "box",
            "reachable": 1,
            "status_json": json.dumps(
                {
                    "monitors": {
                        name: {"state": state, "type_id": type_id, "metrics": metrics}
                    }
                }
            ),
        }
    ]
    md = render_status_md(rows, "t")
    return next(ln for ln in md.splitlines() if ln.startswith(f"- {name}:"))


_Q1 = {
    "queue_completed": 0,
    "queue_total": 1,
    "queue_remaining": 1,
    "queue_failed": 0,
    "subs_total": 1,
    "subs_completed": 0,
    "subs_failed": 0,
}
_Q2 = {**_Q1, "queue_total": 2, "queue_remaining": 2, "subs_total": 2}
_PENDING = [
    {"key": "asr", "state": "pending"},
    {"key": "translate", "state": "pending"},
]
_DONE_R = {"key": "restore", "state": "done", "duration_s": 3420}
_TR_PENDING = {"key": "translate", "state": "pending"}
_MODEL = "grok-4.3 · api.x.ai"


def _asr(**nums) -> list:
    return [_DONE_R, {"key": "asr", "state": "active", **nums}, _TR_PENDING]


def _waiting(holder: str) -> list:
    wait = {"key": "restore", "state": "waiting_gpu", "holder": holder}
    return [{**wait, "waited_s": 30}, *_PENDING]


_FIXTURES = [
    (  # restore active, capture on
        {
            **_Q1,
            "queue_restored": 0,
            "current_file": "SDAB-312.mp4",
            "percent": 57,
            "eta": "7:18",
            "fps": 157.0,
            "film": "SDAB-312.mp4",
            "steps": [
                {"key": "restore", "state": "active", "percent": 57, "eta_s": 438},
                *_PENDING,
            ],
        },
        "running",
        "- JASNA: 0/1 done (1 left) | SDAB-312.mp4 | 57% · ETA 7:18 · 157fps"
        " | 修复 0/1 · subs 0/1",
    ),
    (  # restore active, capture off
        {
            **_Q1,
            "queue_restored": 0,
            "current_file": "SDAB-312.mp4",
            "steps": [{"key": "restore", "state": "active"}, *_PENDING],
        },
        "running",
        "- JASNA: 0/1 done (1 left) | SDAB-312.mp4 | 修复 0/1 · subs 0/1",
    ),
    (  # asr with percent + ETA
        {
            **_Q1,
            "queue_restored": 1,
            "current_file": "SDAB-312-破解.mp4",
            "steps": _asr(percent=43, eta_s=360, phase=5, phase_n=8, elapsed_s=500),
        },
        "running",
        "- JASNA: 0/1 done (1 left) | SDAB-312-破解.mp4 | 识别 43% · 约剩 6 分"
        " | 修复 1/1 · subs 0/1",
    ),
    (  # asr without a percent (another engine): elapsed only
        {
            **_Q1,
            "queue_restored": 1,
            "current_file": "SDAB-312-破解.mp4",
            "steps": _asr(elapsed_s=250),
        },
        "running",
        "- JASNA: 0/1 done (1 left) | SDAB-312-破解.mp4 | 识别 · 已用 4 分"
        " | 修复 1/1 · subs 0/1",
    ),
    (  # translate with ETA and model
        {
            **_Q1,
            "queue_restored": 1,
            "model": _MODEL,
            "steps": [
                _DONE_R,
                {"key": "asr", "state": "done"},
                {
                    "key": "translate",
                    "state": "active",
                    "percent": 56,
                    "eta_s": 45,
                    "model": _MODEL,
                },
            ],
        },
        "running",
        "- JASNA: 0/1 done (1 left) | 翻译 56% · 约剩 1 分 · grok-4.3 · api.x.ai"
        " | 修复 1/1 · subs 0/1",
    ),
    (  # waiting for the GPU another task holds
        {**_Q2, "queue_restored": 0, "steps": _waiting("AV")},
        "idle",
        "- JASNA: 0/2 done (2 left) | 等待 GPU（AV） | 修复 0/2 · subs 0/2",
    ),
    (  # waiting, the lease reserved for this run (holder "")
        {**_Q2, "queue_restored": 0, "steps": _waiting("")},
        "idle",
        "- JASNA: 0/2 done (2 left) | 等待 GPU | 修复 0/2 · subs 0/2",
    ),
    (  # asr, no ETA yet
        {
            **_Q1,
            "queue_restored": 1,
            "current_file": "SDAB-312-破解.mp4",
            "steps": _asr(percent=43, elapsed_s=90),
        },
        "running",
        "- JASNA: 0/1 done (1 left) | SDAB-312-破解.mp4 | 识别 43% | 修复 1/1 · subs 0/1",
    ),
]


def test_189_jasna_fixture_lines_are_byte_exact():
    for metrics, state, expected in _FIXTURES:
        assert _line_of("JASNA", "jasna", metrics, state) == expected


def test_189_avsubs_fixture_line_is_byte_exact():
    metrics = {
        "queue_completed": 21,
        "queue_total": 63,
        "queue_remaining": 41,
        "queue_failed": 1,
        "queue_skipped": 0,
        "queue_pre_done": 20,
        "current_file": "2024/ABC-123.mp4",
        "phase": "asr",
        "subs_translating": 0,
        "film": "2024/ABC-123.mp4",
        "steps": [
            {"key": "asr", "state": "active", "percent": 43, "eta_s": 330},
            {"key": "translate", "state": "pending"},
        ],
    }
    assert _line_of("AV", "avsubs", metrics) == (
        "- AV: 21/63 done (41 left) | 2024/ABC-123.mp4 | 识别 43% · 约剩 6 分"
    )


def test_189_minutes_never_read_zero():
    # ETA minutes = max(1, ceil(eta_s / 60)); elapsed = max(1, floor(s / 60)).
    def line(**nums) -> str:
        return _line_of("AV", "avsubs", {"steps": _asr(**nums)[1:]})

    assert line(percent=99, eta_s=0) == "- AV: 识别 99% · 约剩 1 分"
    assert line(percent=99, eta_s=59) == "- AV: 识别 99% · 约剩 1 分"
    assert line(percent=99, eta_s=61) == "- AV: 识别 99% · 约剩 2 分"
    assert line(elapsed_s=0) == "- AV: 识别 · 已用 1 分"
    assert line(elapsed_s=119) == "- AV: 识别 · 已用 1 分"
    assert line() == "- AV: 识别"


def test_189_no_stage_for_restore_queued_pending_or_terminal_focus():
    base = {**_Q1, "queue_restored": 1}
    for steps in (
        [{"key": "restore", "state": "active", "percent": 5}, *_PENDING],
        [_DONE_R, {"key": "asr", "state": "done"}, {**_TR_PENDING, "state": "queued"}],
        [{"key": "restore", "state": "pending"}, *_PENDING],
        [
            _DONE_R,
            {"key": "asr", "state": "failed"},
            {**_TR_PENDING, "state": "skipped"},
        ],
    ):
        line = _line_of("JASNA", "jasna", {**base, "steps": steps})
        assert line == "- JASNA: 0/1 done (1 left) | 修复 1/1 · subs 0/1"


def test_189_malformed_steps_render_no_stage_fragment():
    plain = "- JASNA: 0/1 done (1 left) | 修复 1/1 · subs 0/1"
    for steps in (
        "asr",
        {"key": "asr", "state": "active"},
        [1, 2],
        [{"key": "asr"}],
        [{"key": 7, "state": "active"}],
        [{"key": "asr", "state": None}],
        [_DONE_R, "x", {"key": "asr", "state": "active", "percent": 40}],
    ):
        m = {**_Q1, "queue_restored": 1, "steps": steps}
        assert _line_of("JASNA", "jasna", m) == plain, steps
    # a bad number is dropped on its own; the fragment stays
    bad = {**_Q1, "queue_restored": 1}
    bad["steps"] = _asr(percent="43", eta_s=float("nan"), elapsed_s=-5)
    assert _line_of("JASNA", "jasna", bad) == (
        "- JASNA: 0/1 done (1 left) | 识别 | 修复 1/1 · subs 0/1"
    )
    bad["steps"] = _asr(percent=140, eta_s=float("inf"))
    assert _line_of("JASNA", "jasna", bad) == (
        "- JASNA: 0/1 done (1 left) | 识别 | 修复 1/1 · subs 0/1"
    )


def test_189_model_and_holder_are_sanitized_and_capped():
    model = "grok\n- INJECTED: x" + "m" * 200
    steps = [{"key": "translate", "state": "active", "percent": 10, "model": model}]
    line = _line_of("AV", "avsubs", {"steps": steps})
    assert "\n" not in line and "INJECTED: x" in line  # one line, no new entry
    fragment = line.removeprefix("- AV: 翻译 10% · ")
    assert len(fragment) == 80
    # the step's own model wins; the top-level one is the fallback
    steps = [{"key": "translate", "state": "active", "percent": 10}]
    line = _line_of("AV", "avsubs", {"model": _MODEL, "steps": steps})
    assert line == "- AV: 翻译 10% · grok-4.3 · api.x.ai"
    holder = "Other\ttask\n" + "h" * 300
    steps = [{"key": "asr", "state": "waiting_gpu", "holder": holder}]
    line = _line_of("AV", "avsubs", {"steps": steps})
    assert line.startswith("- AV: 等待 GPU（Other task ") and line.endswith("）")
    assert len(line) == len("- AV: 等待 GPU（）") + 200
    steps = [{"key": "asr", "state": "waiting_gpu", "holder": 7}]
    assert _line_of("AV", "avsubs", {"steps": steps}) == "- AV: 等待 GPU"


def test_189_restored_count_carries_the_subs_failed_suffix():
    m = {
        "queue_completed": 1,
        "queue_total": 4,
        "queue_remaining": 2,
        "queue_failed": 1,
        "queue_restored": 2,
        "subs_total": 3,
        "subs_completed": 1,
        "subs_failed": 1,
    }
    assert _line_of("JASNA", "jasna", m) == (
        "- JASNA: 1/4 done (2 left) | 修复 2/4 · subs 1/3 (1 failed)"
    )
    # without queue_restored (AV 翻译 off / older agents) exactly as before
    m.pop("queue_restored")
    assert _line_of("JASNA", "jasna", m) == (
        "- JASNA: 1/4 done (2 left) | subs 1/3 (1 failed)"
    )


def test_189_error_state_hides_the_stage_and_lada_is_unchanged():
    m = {**_Q1, "queue_restored": 1, "steps": _asr(percent=43)}
    assert _line_of("JASNA", "jasna", m, state="error") == "- JASNA: error"
    base = {
        "queue_completed": 5,
        "queue_total": 10,
        "queue_remaining": 5,
        "current_file": "clip.mp4",
        "percent": 47,
        "eta": "30:47",
        "fps": 112.3,
    }
    assert _lada_line(render_status_md(_lada_row(base), "t")) == (
        "- LADA: 5/10 done (5 left) | clip.mp4 | 47% · ETA 30:47 · 112fps"
    )
