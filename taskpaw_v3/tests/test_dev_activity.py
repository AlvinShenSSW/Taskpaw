"""dev_activity monitor (#154): process presence + state-file busy/idle aggregation."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from taskpaw_v3.monitors.plugins import dev_activity as da
from taskpaw_v3.monitors.plugins.dev_activity import (
    DevActivityConfig,
    DevActivityPlugin,
    aggregate,
    read_tool_state,
)
from taskpaw_v3.monitors.registry import default_registry


@pytest.fixture(autouse=True)
def _no_real_observation(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _snapshot(monkeypatch, {})


def _snapshot(monkeypatch, present):
    monkeypatch.setattr(
        da,
        "scan_activity",
        lambda patterns: {
            tool: {"present": value, "roots": [], "cpus": {}, "complete": True}
            for tool, value in present.items()
        },
    )


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


def test_read_tool_state_rejects_nonfinite_ts(tmp_path):
    # NaN/Infinity ts (json allows them) would make age non-finite → NaN in /status
    # JSON, breaking the browser parse. Must be rejected (Kimi 终审).
    for bad in ("NaN", "Infinity", "-Infinity"):
        (tmp_path / "agent-activity-c.json").write_text(
            f'{{"tool": "c", "state": "busy", "ts": {bad}}}', encoding="utf-8"
        )
        assert read_tool_state(str(tmp_path), "c", 300, time.time()) == (None, None)


def test_empty_process_pattern_rejected_at_config_time():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DevActivityConfig(name="ai", process_patterns={"claude": ""})


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
    _snapshot(monkeypatch, present)
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


def _check_obs(cfg, monkeypatch, present, cpu):
    """A check() where the external CPU probe is stubbed to return `cpu` (a
    {tool: percent} dict), so observation is deterministic (no real timing)."""
    _snapshot(monkeypatch, present)
    inst = DevActivityPlugin().create("ai", cfg)
    monkeypatch.setattr(da, "cpu_percents", lambda *a: (cpu, {}))
    events: list = []
    st = inst.check(lambda *a, **k: events.append((a, k)))
    return inst, st, events


def test_observe_high_cpu_reports_busy(tmp_path, monkeypatch):
    # #163: present + no hook state + subtree CPU ≥ threshold → observed busy.
    cfg = DevActivityConfig(
        name="ai", state_dir=str(tmp_path), tools=["claude"], busy_cpu_percent=8.0
    )
    _, st, _ = _check_obs(cfg, monkeypatch, {"claude": True}, {"claude": 42.0})
    assert st.metrics["ai_state"] == "busy"
    claude = next(t for t in st.metrics["tools"] if t["tool"] == "claude")
    assert claude["state"] == "busy" and claude["observed"] is True
    assert claude["cpu"] == 42.0


def test_observe_low_cpu_reports_idle(tmp_path, monkeypatch):
    # present + no hook state + low CPU → observed idle (not just present_only).
    cfg = DevActivityConfig(
        name="ai", state_dir=str(tmp_path), tools=["claude"], busy_cpu_percent=8.0
    )
    _, st, _ = _check_obs(cfg, monkeypatch, {"claude": True}, {"claude": 0.3})
    assert st.metrics["ai_state"] == "idle"
    claude = next(t for t in st.metrics["tools"] if t["tool"] == "claude")
    assert claude["state"] == "idle" and claude["observed"] is True


def test_hook_state_wins_over_observation(tmp_path, monkeypatch):
    # A fresh hook state must take precedence over the CPU probe (hooks are truth).
    _write(tmp_path, "claude", "idle", time.time())
    cfg = DevActivityConfig(name="ai", state_dir=str(tmp_path), tools=["claude"])
    _, st, _ = _check_obs(cfg, monkeypatch, {"claude": True}, {"claude": 99.0})
    claude = next(t for t in st.metrics["tools"] if t["tool"] == "claude")
    assert claude["state"] == "idle" and claude["observed"] is False


def test_no_cpu_reading_stays_present_only(tmp_path, monkeypatch):
    # First sample (no CPU% yet) → observation unavailable → present_only, not idle.
    cfg = DevActivityConfig(name="ai", state_dir=str(tmp_path), tools=["claude"])
    _, st, _ = _check_obs(cfg, monkeypatch, {"claude": True}, {})  # no cpu for claude
    assert st.metrics["ai_state"] == "present_only"


def test_observed_vscode_busy_does_not_drive_ai_headline(tmp_path, monkeypatch):
    # VS Code observed busy shows in its row but must NOT make the machine "AI busy"
    # (it's a context editor, ai=false) (#163).
    cfg = DevActivityConfig(name="ai", state_dir=str(tmp_path), tools=["vscode"])
    _, st, _ = _check_obs(cfg, monkeypatch, {"vscode": True}, {"vscode": 80.0})
    assert st.metrics["ai_state"] == "none"  # not "busy"
    vs = next(t for t in st.metrics["tools"] if t["tool"] == "vscode")
    assert vs["state"] == "idle" and vs["ai"] is False and vs["observed"] is False
    assert vs["cpu"] is None


def test_observe_disabled_is_presence_only(tmp_path, monkeypatch):
    # observe=False → no CPU probe; present + no state → present_only (old behavior).
    cfg = DevActivityConfig(
        name="ai", state_dir=str(tmp_path), tools=["claude"], observe=False
    )
    inst = DevActivityPlugin().create("ai", cfg)
    _snapshot(monkeypatch, {"claude": True})
    # If observe were on, _observe would be called; assert it is NOT.
    monkeypatch.setattr(
        da, "cpu_percents", lambda *a: (_ for _ in ()).throw(AssertionError("probed"))
    )
    st = inst.check(lambda *a, **k: None)
    assert st.metrics["ai_state"] == "present_only"


def test_check_emits_on_busy_edge_only(tmp_path, monkeypatch):
    cfg = DevActivityConfig(name="ai", state_dir=str(tmp_path), tools=["claude"])
    _snapshot(monkeypatch, {"claude": True})
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
    _snapshot(monkeypatch, {"claude": True})
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


@pytest.mark.parametrize("failure", ["error", "limited"])
@pytest.mark.parametrize("recovered", ["idle", "busy", "waiting"])
def test_suppressed_idle_recovers_without_duplicate_events(
    tmp_path, monkeypatch, failure, recovered
):
    cfg = DevActivityConfig(
        name="ai", state_dir=str(tmp_path), tools=["claude"], observe=False
    )
    inst = DevActivityPlugin().create("ai", cfg)
    events = []
    emit = lambda *a, **k: events.append(a)  # noqa: E731
    try:
        _write(tmp_path, "claude", "idle", time.time())
        inst.check(emit)
        _write(tmp_path, "claude", "busy", time.time())
        inst.check(emit)
        assert [e[1] for e in events] == ["ai: AI busy"]

        if failure == "error":
            (tmp_path / "agent-activity-claude.json").write_text("invalid JSON")
        else:
            _write(tmp_path, "claude", "idle", time.time())
            monkeypatch.setattr(
                da, "scan_activity", lambda *a: {"claude": {"limited": True}}
            )
        for _ in range(2):
            st = inst.check(emit)
            assert st.metrics["probe_errors"] or st.metrics["probe_limited"]
            assert [e[1] for e in events] == ["ai: AI busy"]

        _snapshot(monkeypatch, {})
        _write(tmp_path, "claude", recovered, time.time())
        expected = ["ai: AI busy"]
        if recovered == "idle":
            expected.append("ai: AI idle")
        elif recovered == "waiting":
            expected.append("ai: AI waiting for input")
        for _ in range(2):
            st = inst.check(emit)
            assert st.metrics["ai_state"] == recovered
            assert not st.metrics["probe_errors"] and not st.metrics["probe_limited"]
            assert [e[1] for e in events] == expected
    finally:
        inst.stop()


def test_read_tool_state_rejects_far_future_ts(tmp_path):
    # A far-future timestamp must NOT read as "fresh forever" (Kimi 终审).
    now = 1000.0
    _write(tmp_path, "claude", "busy", now + 10_000)
    assert read_tool_state(str(tmp_path), "claude", 300, now) == (None, None)
    # ...but a few seconds of clock skew is tolerated as just-written.
    _write(tmp_path, "codex", "busy", now + 2)
    assert read_tool_state(str(tmp_path), "codex", 300, now)[0] == "busy"


def test_present_scan_error_degrades_not_crash(tmp_path, monkeypatch):
    # A psutil PermissionError/OSError during enumeration must not abort check();
    # presence degrades to absent and file-based activity is still reported.
    def boom(patterns, search_cmdline=True):
        raise PermissionError("denied")

    monkeypatch.setattr(da, "scan_activity", boom)
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
    _snapshot(monkeypatch, {"claude": True})
    inst = DevActivityPlugin().create("ai", cfg)
    emit = lambda *a, **k: None  # noqa: E731
    _write(tmp_path, "claude", "busy", time.time())
    inst.check(emit)
    inst.check(emit)
    _write(tmp_path, "claude", "idle", time.time())
    st = inst.check(emit)
    # 2 busy of 3 samples → ratio ~0.67
    assert 0.6 <= st.metrics["duty"]["ratio"] <= 0.7


@pytest.mark.parametrize("state", ["bogus", 1, True])
def test_unknown_hook_falls_through(tmp_path, state):
    _write(tmp_path, "claude", state, 1000)
    assert read_tool_state(str(tmp_path), "claude", 300, 1000)[0] is None


def test_wrong_tag_hook_invalid(tmp_path):
    (tmp_path / "agent-activity-claude.json").write_text(
        json.dumps({"tool": "codex", "state": "busy", "ts": 1000})
    )
    assert read_tool_state(str(tmp_path), "claude", 300, 1000)[0] is None


def test_tool_deduplication(tmp_path, monkeypatch):
    _write(tmp_path, "claude", "busy", time.time())
    _, st, _ = _check(
        DevActivityConfig(
            name="ai", state_dir=str(tmp_path), tools=["claude", "claude"]
        ),
        monkeypatch,
        {"claude": True},
    )
    assert st.metrics["busy_tools"] == ["claude"]
    assert len(st.metrics["tools"]) == 1


@pytest.mark.parametrize(
    "host,expected",
    [("vscode", "busy"), ("mixed", None), ("unknown", None), ("other", None)],
)
def test_hook_context_attribution(tmp_path, monkeypatch, host, expected):
    from taskpaw_v3.monitors import process_util as pu

    _write(tmp_path, "claude", "busy", time.time())
    roots = [{"pid": 10, "created": 1, "host": host}]
    if host == "mixed":
        roots = [
            {"pid": 10, "created": 1, "host": "vscode"},
            {"pid": 11, "created": 1, "host": "other"},
        ]
    monkeypatch.setattr(
        da,
        "scan_activity",
        lambda p: {
            "claude": {"present": True, "roots": roots, "cpus": {}, "complete": True},
            "vscode": {"present": True, "roots": [], "cpus": {}, "complete": True},
        },
    )
    monkeypatch.setattr(pu, "WINDOWS", False)
    cfg = DevActivityConfig(
        name="ai", state_dir=str(tmp_path), tools=["claude", "vscode"]
    )
    st = DevActivityPlugin().create("ai", cfg).check(lambda *a, **k: None)
    ai, vs = st.metrics["tools"]
    assert ai["source"] == "hook" and ai["host"] == host
    assert ai["vscode_state"] == expected
    assert vs["state"] == (expected or "idle") and not vs["observed"]
    assert st.metrics["busy_tools"] == ["claude"]


def test_session_precedence_controls_and_failure_transition(tmp_path, monkeypatch):
    cfg = DevActivityConfig(name="ai", state_dir=str(tmp_path), tools=["claude"])
    monkeypatch.setattr(
        da,
        "scan_activity",
        lambda p: {
            "claude": {
                "present": True,
                "roots": [{"pid": 10, "created": 1, "host": "vscode"}],
                "cpus": {},
                "complete": True,
            }
        },
    )
    inst = DevActivityPlugin().create("ai", cfg)
    monkeypatch.setattr(
        inst._sessions,
        "sample",
        lambda *a: {
            "claude": {
                "state": "busy",
                "age_s": 7,
                "host": "vscode",
                "vscode_state": "busy",
                "errors": [],
                "limited": False,
            }
        },
    )
    events = []
    st = inst.check(lambda *a, **k: events.append(a))
    assert st.metrics["tools"][0]["source"] == "session"
    _write(tmp_path, "claude", "idle", time.time())
    assert inst.check(lambda *a, **k: None).metrics["tools"][0]["source"] == "hook"
    (tmp_path / "agent-activity-claude.json").unlink()
    inst.check(lambda *a, **k: None)
    monkeypatch.setattr(
        inst._sessions,
        "sample",
        lambda *a: {"claude": {"state": None, "errors": ["denied"], "limited": False}},
    )
    st = inst.check(lambda *a, **k: events.append(a))
    assert st.state == "degraded" and st.metrics["probe_errors"]
    assert not events
    inst.stop()


def test_new_root_cannot_inherit_idle_cpu(tmp_path, monkeypatch):
    from taskpaw_v3.monitors import process_util as pu

    roots = [{"pid": 10, "created": 1, "host": "other"}]
    sample = {
        "claude": {
            "present": True,
            "complete": True,
            "roots": roots,
            "cpus": {(10, 1): (1, (10, 1))},
        }
    }
    monkeypatch.setattr(da, "scan_activity", lambda *a: sample)
    cfg = DevActivityConfig(
        name="ai", state_dir=str(tmp_path), session_activity=False, tools=["claude"]
    )
    inst = DevActivityPlugin().create("ai", cfg)
    inst.check(lambda *a, **k: None)
    roots.append({"pid": 11, "created": 2, "host": "vscode"})
    sample["claude"]["cpus"][(11, 2)] = (100, (11, 2))
    # Snapshot dictionaries are fresh in production; preserve independent baseline.
    inst._prev_cpu = {"claude": {(10, 1): (1, (10, 1))}}
    st = inst.check(lambda *a, **k: None)
    assert st.metrics["ai_state"] == "present_only"
    assert pu.cpu_percents({}, 0, sample, 1)[0] == {}


@pytest.mark.parametrize("budget", ["entries", "directories"])
def test_busy_off_busy_events_across_discovery_yields(tmp_path, monkeypatch, budget):
    from taskpaw_v3.monitors import session_activity as sa

    root = tmp_path / "sessions"
    root.mkdir()
    for n in range(1600 if budget == "entries" else 200):
        path = root / str(n)
        path.touch() if budget == "entries" else path.mkdir()
    monkeypatch.setattr(sa, "WINDOWS", True)
    monkeypatch.setattr(sa.time, "monotonic", lambda: 1.0)
    monkeypatch.setattr(
        da,
        "scan_activity",
        lambda *a: {
            "claude": {
                "present": True,
                "roots": [{"pid": 10, "created": 1, "host": "other"}],
                "cpus": {},
                "complete": True,
            }
        },
    )
    cpu = [20.0]
    monkeypatch.setattr(da, "cpu_percents", lambda *a: ({"claude": cpu[0]}, {}))
    cfg = DevActivityConfig(
        name="ai",
        state_dir=str(tmp_path),
        tools=["claude"],
        session_roots={"claude": [str(root)]},
    )
    inst = DevActivityPlugin().create("ai", cfg)
    events = []
    try:
        for percent, state in [(20.0, "busy"), (0.0, "idle"), (20.0, "busy")]:
            cpu[0] = percent
            st = inst.check(lambda *a, **k: events.append(a))
            assert st.metrics["ai_state"] == state
            assert not st.metrics["probe_limited"]
            assert inst._sessions.queues  # Discovery still resumes next tick.
        assert [e[1] for e in events] == ["ai: AI idle", "ai: AI busy"]
    finally:
        inst.stop()


@pytest.mark.parametrize("active", ["busy", "waiting"])
def test_persistent_hook_error_preserves_next_active_event(
    tmp_path, monkeypatch, active
):
    (tmp_path / "agent-activity-codex.json").write_text("invalid JSON")
    cfg = DevActivityConfig(
        name="ai", state_dir=str(tmp_path), tools=["claude", "codex"]
    )
    inst = DevActivityPlugin().create("ai", cfg)
    events = []
    try:
        for state in (active, "idle", active, active):
            _write(tmp_path, "claude", state, time.time())
            st = inst.check(lambda *a, **k: events.append(a))
            assert st.state == "degraded" and st.metrics["probe_errors"]
        title = "AI busy" if active == "busy" else "AI waiting for input"
        assert [e[1] for e in events] == [f"ai: {title}"]
    finally:
        inst.stop()
