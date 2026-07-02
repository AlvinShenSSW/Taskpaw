"""dev_activity monitor (#154): process presence + state-file busy/idle aggregation."""

from __future__ import annotations

import json
import time

from taskpaw_v3.monitors.plugins import dev_activity as da
from taskpaw_v3.monitors.plugins.dev_activity import (
    DevActivityConfig,
    DevActivityPlugin,
    aggregate,
    read_tool_state,
)
from taskpaw_v3.monitors.registry import default_registry


def _write(tmp_path, tool, state, ts):
    (tmp_path / f"agent-activity-{tool}.json").write_text(
        json.dumps({"tool": tool, "state": state, "ts": ts}), encoding="utf-8"
    )


def test_registered_in_default_registry():
    reg = default_registry()
    assert reg.has("dev_activity")
    assert reg.get("dev_activity").type_id == "dev_activity"


def test_read_tool_state_fresh_stale_missing(tmp_path):
    now = 1_000_000.0
    _write(tmp_path, "claude", "busy", now - 10)
    state, age = read_tool_state(str(tmp_path), "claude", 300, now)
    assert state == "busy" and 9 <= age <= 11

    _write(tmp_path, "codex", "idle", now - 999)  # older than freshness → unknown
    state, age = read_tool_state(str(tmp_path), "codex", 300, now)
    assert state is None and age is not None  # stale, but age reported

    state, age = read_tool_state(str(tmp_path), "kimi", 300, now)  # missing
    assert state is None and age is None


def test_read_tool_state_falls_back_to_shared_default_file(tmp_path):
    # A legacy/default hook writes ~/.taskpaw/agent-activity.json (no --path), tagged
    # with its own tool. The monitor must still read it (Codex 外门).
    now = 2_000_000.0
    (tmp_path / "agent-activity.json").write_text(
        json.dumps({"tool": "claude", "state": "busy", "ts": now - 5}), encoding="utf-8"
    )
    assert read_tool_state(str(tmp_path), "claude", 300, now)[0] == "busy"
    # ...but only for the matching tool.
    assert read_tool_state(str(tmp_path), "codex", 300, now) == (None, None)
    # A per-tool file takes precedence over the shared default.
    _write(tmp_path, "claude", "idle", now)
    assert read_tool_state(str(tmp_path), "claude", 300, now)[0] == "idle"


def test_read_tool_state_ignores_malformed(tmp_path):
    (tmp_path / "agent-activity-x.json").write_text("not json", encoding="utf-8")
    assert read_tool_state(str(tmp_path), "x", 300, time.time()) == (None, None)


def test_aggregate_most_busy_wins():
    def t(tool, state=None, present=False):
        return {"tool": tool, "state": state, "present": present, "age_s": None}

    assert aggregate([t("claude", "busy"), t("codex", "idle")]) == ("busy", ["claude"])
    assert aggregate([t("claude", "waiting"), t("codex", present=True)]) == (
        "waiting",
        [],
    )
    assert aggregate([t("claude", "idle"), t("codex", present=True)]) == ("idle", [])
    assert aggregate([t("claude", present=True)]) == ("present_only", [])
    assert aggregate([t("claude"), t("codex")]) == ("none", [])


def test_vscode_alone_is_not_ai_present(tmp_path, monkeypatch):
    # #154 / Codex 外门: VS Code open but no AI CLI/state → "none", not present_only.
    cfg = DevActivityConfig(
        name="ai", state_dir=str(tmp_path), tools=["claude", "vscode"]
    )
    _, st, _ = _check(cfg, monkeypatch, {"claude": False, "vscode": True})
    assert st.metrics["ai_state"] == "none"
    # vscode is still shown as present (context), it just doesn't drive the headline.
    vs = next(x for x in st.metrics["tools"] if x["tool"] == "vscode")
    assert vs["present"] is True and vs["ai"] is False


def test_invalid_process_pattern_rejected_at_config_time():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DevActivityConfig(name="ai", process_patterns={"claude": "("})  # bad regex


def _check(cfg, monkeypatch, present):
    monkeypatch.setattr(da, "_detect_present", lambda patterns: present)
    inst = DevActivityPlugin().create("ai", cfg)
    events = []
    st = inst.check(lambda *a, **k: events.append((a, k)))
    return inst, st, events


def test_check_busy_headline_and_metrics(tmp_path, monkeypatch):
    now = time.time()
    _write(tmp_path, "claude", "busy", now)
    _write(tmp_path, "codex", "idle", now)
    cfg = DevActivityConfig(
        name="ai", state_dir=str(tmp_path), tools=["claude", "codex", "kimi"]
    )
    _, st, _ = _check(cfg, monkeypatch, {"claude": True, "codex": True, "kimi": True})
    assert st.state == "running"
    assert st.metrics["ai_state"] == "busy"
    assert st.metrics["busy_tools"] == ["claude"]
    # kimi has no state file but is present → reported present, state unknown.
    kimi = next(t for t in st.metrics["tools"] if t["tool"] == "kimi")
    assert kimi["present"] is True and kimi["state"] is None


def test_check_present_only_is_not_idle(tmp_path, monkeypatch):
    # #154 core fix: processes up but no state file → "present_only", NOT idle/none.
    cfg = DevActivityConfig(name="ai", state_dir=str(tmp_path), tools=["claude"])
    _, st, _ = _check(cfg, monkeypatch, {"claude": True})
    assert st.metrics["ai_state"] == "present_only"
    assert st.state == "idle"


def test_check_none_when_absent_and_no_files(tmp_path, monkeypatch):
    cfg = DevActivityConfig(name="ai", state_dir=str(tmp_path), tools=["claude"])
    _, st, _ = _check(cfg, monkeypatch, {"claude": False})
    assert st.metrics["ai_state"] == "none" and st.state == "unknown"


def test_check_emits_on_busy_edge_only(tmp_path, monkeypatch):
    cfg = DevActivityConfig(name="ai", state_dir=str(tmp_path), tools=["claude"])
    monkeypatch.setattr(da, "_detect_present", lambda patterns: {"claude": True})
    inst = DevActivityPlugin().create("ai", cfg)
    events: list = []
    emit = lambda *a, **k: events.append(a)  # noqa: E731

    _write(tmp_path, "claude", "idle", time.time())
    inst.check(emit)  # first sample, prev None → no emit
    _write(tmp_path, "claude", "busy", time.time())
    inst.check(emit)  # idle→busy edge → one emit
    inst.check(emit)  # still busy → no new emit
    assert len(events) == 1 and events[0][0] == "info"


def test_busy_to_waiting_emits_waiting_not_idle(tmp_path, monkeypatch):
    # busy→waiting (Claude Notification) must surface "waiting for input", not "idle".
    cfg = DevActivityConfig(name="ai", state_dir=str(tmp_path), tools=["claude"])
    monkeypatch.setattr(da, "_detect_present", lambda patterns: {"claude": True})
    inst = DevActivityPlugin().create("ai", cfg)
    events: list = []
    emit = lambda *a, **k: events.append(a)  # noqa: E731

    _write(tmp_path, "claude", "busy", time.time())
    inst.check(emit)  # prev None → no emit
    _write(tmp_path, "claude", "waiting", time.time())
    st = inst.check(emit)  # busy→waiting edge
    assert st.metrics["ai_state"] == "waiting"
    assert (
        events
        and "waiting" in events[-1][1].lower()
        and "idle" not in events[-1][1].lower()
    )


def test_present_scan_error_degrades_not_crash(tmp_path, monkeypatch):
    # A psutil PermissionError/OSError during enumeration must not abort check();
    # presence degrades to absent and file-based activity is still reported.
    def boom(rx, search_cmdline):
        raise PermissionError("denied")

    monkeypatch.setattr(da, "_scan", boom)
    _write(tmp_path, "claude", "busy", time.time())
    cfg = DevActivityConfig(name="ai", state_dir=str(tmp_path), tools=["claude"])
    inst = DevActivityPlugin().create("ai", cfg)
    st = inst.check(lambda *a, **k: None)
    assert st.metrics["ai_state"] == "busy"  # state file still read
    assert st.metrics["tools"][0]["present"] is False  # presence degraded


def test_duty_ratio_accumulates(tmp_path, monkeypatch):
    cfg = DevActivityConfig(
        name="ai", state_dir=str(tmp_path), tools=["claude"], window_seconds=3600
    )
    monkeypatch.setattr(da, "_detect_present", lambda patterns: {"claude": True})
    inst = DevActivityPlugin().create("ai", cfg)
    emit = lambda *a, **k: None  # noqa: E731
    _write(tmp_path, "claude", "busy", time.time())
    inst.check(emit)
    inst.check(emit)
    _write(tmp_path, "claude", "idle", time.time())
    st = inst.check(emit)
    # 2 busy of 3 samples → ratio ~0.67
    assert 0.6 <= st.metrics["duty"]["ratio"] <= 0.7
