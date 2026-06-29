"""status_md edge cases not covered by test_openclaw_compat.py (#115).

The main V2-format / ONLINE-OFFLINE / list-shape / atomic-write paths are already
covered there; this adds the disabled-monitor branch and the last_seen fallback."""

from __future__ import annotations

import json

from taskpaw_v3.hub.server.status_md import render_status_md


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
