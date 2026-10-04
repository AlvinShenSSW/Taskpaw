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
    from taskpaw_v3.integrations import activity_writer as aw

    monkeypatch.setattr(aw, "_producer_identity", lambda: None, raising=False)
    from types import SimpleNamespace

    from taskpaw_v3.monitors import session_activity as sa

    monkeypatch.setattr(
        sa.psutil,
        "Process",
        lambda pid: SimpleNamespace(create_time=lambda: 1.0, open_files=lambda: []),
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _snapshot(monkeypatch, {})


def _snapshot(monkeypatch, present):
    monkeypatch.setattr(
        da,
        "scan_activity",
        lambda patterns: {
            tool: {
                "present": value,
                "roots": [{"pid": 10, "created": 1.0, "host": "other"}]
                if value
                else [],
                "cpus": {},
                "complete": True,
            }
            for tool, value in present.items()
        },
    )


_fixture_scopes = {}


def _write(tmp_path, tool, state, ts):
    # Runtime fixtures now represent documented, independent scopes. Read-only
    # legacy tuple tests still read the same core projection. An idle is a real
    # matching SessionEnd/Interrupt, never an invented ordered legacy idle.
    from taskpaw_v3.integrations import activity_writer as aw

    path = tmp_path / f"agent-activity-{tool}.json"
    session, previous_state = _fixture_scopes.get(str(path), ("fixture-0", None))
    if state in ("busy", "waiting") and previous_state == "idle":
        session += "-next"
    _fixture_scopes[str(path)] = (session, state)
    event = {
        "busy": "UserPromptSubmit",
        "waiting": "PermissionRequest",
        "idle": "Interrupt" if tool == "codex" else "SessionEnd",
    }.get(state)
    if event is None:
        path.write_text(
            json.dumps({"tool": tool, "state": state, "ts": ts}), encoding="utf-8"
        )
        return
    raw = json.dumps(
        {
            "hook_event_name": event,
            "session_id": session,
            "turn_id" if tool == "codex" else "prompt_id": session + "-turn",
        }
    )
    fact = aw.hook_fact(raw, tool, ts, (10, 1.0))
    assert fact is not None
    assert aw.publish_hook(path, fact, state, session, ts)


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


@pytest.mark.parametrize("failure", ["error", "limited", "process_error", "all"])
@pytest.mark.parametrize("recovered", ["idle", "busy", "waiting"])
def test_suppressed_idle_recovers_without_duplicate_events(
    tmp_path, monkeypatch, failure, recovered
):
    _snapshot(monkeypatch, {"claude": True})
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
            # Without a fresh hook, failed process evidence must still defer idle.
            (tmp_path / "agent-activity-claude.json").unlink()
            if failure == "all":

                def unavailable(*a):
                    raise RuntimeError("unavailable")

                monkeypatch.setattr(da, "scan_activity", unavailable)
            else:
                sample = (
                    {"limited": True}
                    if failure == "limited"
                    else {"errors": ["denied"]}
                )
                monkeypatch.setattr(da, "scan_activity", lambda *a: {"claude": sample})
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


@pytest.mark.parametrize("failure", ["limited", "error", "all"])
def test_process_failure_does_not_suppress_fresh_hook_idle(
    tmp_path, monkeypatch, failure
):
    _snapshot(monkeypatch, {"claude": True})
    cfg = DevActivityConfig(
        name="ai", state_dir=str(tmp_path), tools=["claude", "codex"]
    )
    inst = DevActivityPlugin().create("ai", cfg)
    events = []
    emit = lambda *a, **k: events.append(a)  # noqa: E731
    try:
        _write(tmp_path, "claude", "busy", time.time())
        inst.check(emit)
        if failure == "all":

            def unavailable(*a):
                raise RuntimeError("unavailable")

            monkeypatch.setattr(da, "scan_activity", unavailable)
        else:
            sample = (
                {"limited": True} if failure == "limited" else {"errors": ["denied"]}
            )
            monkeypatch.setattr(da, "scan_activity", lambda *a: {"claude": sample})
        for state in ("idle", "idle", "busy", "busy"):
            _write(tmp_path, "claude", state, time.time())
            st = inst.check(emit)
            assert st.metrics["tools"][0]["source"] == "hook"
            assert st.metrics["ai_state"] == state
            assert st.metrics["probe_errors"] or st.metrics["probe_limited"]
            if failure != "limited":
                assert st.state == "degraded"
            expected = ["ai: AI idle"]
            if state == "busy":
                expected.append("ai: AI busy")
            assert [e[1] for e in events] == expected
            assert not inst._idle_pending
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
    # The fixture producer is PID10, not all roots of this tool. In the mixed
    # root case its actual attributed host remains vscode; PID11 is uncovered.
    bound_host = "vscode" if host == "mixed" else host
    bound_state = "busy" if bound_host == "vscode" else expected
    assert ai["source"] == "hook" and ai["host"] == bound_host
    assert ai["vscode_state"] == bound_state
    assert vs["state"] == (bound_state or "idle") and not vs["observed"]
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
        lambda snapshot, *a: (
            {}
            if not snapshot["claude"]["roots"]
            else {
                "claude": {
                    "state": "busy",
                    "age_s": 7,
                    "host": "vscode",
                    "vscode_state": "busy",
                    "errors": [],
                    "limited": False,
                }
            }
        ),
    )
    events = []
    st = inst.check(lambda *a, **k: events.append(a))
    assert st.metrics["tools"][0]["source"] == "session"
    _write(tmp_path, "claude", "idle", time.time())
    assert inst.check(lambda *a, **k: None).metrics["tools"][0]["source"] == "hook"
    (tmp_path / "agent-activity-claude.json").unlink()
    from taskpaw_v3.integrations import activity_writer as aw

    aw.sidecar_path(tmp_path / "agent-activity-claude.json").unlink()
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


@pytest.mark.parametrize("cap", ["queue", "depth"])
def test_persistent_session_cycle_limit_allows_cpu_idle_busy_events(
    tmp_path, monkeypatch, cap
):
    from taskpaw_v3.monitors import session_activity as sa

    root = tmp_path / "sessions"
    root.mkdir()
    if cap == "queue":
        for n in range(300):
            (root / str(n)).mkdir()
    else:
        root.joinpath(*[str(n) for n in range(10)]).mkdir(parents=True)
    monkeypatch.setattr(sa, "WINDOWS", True)  # No native open-file inspection.
    clock = [1.0]
    total_cpu = [0.0]
    monkeypatch.setattr(da.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        da,
        "scan_activity",
        lambda *a: {
            "claude": {
                "present": True,
                "roots": [{"pid": 10, "created": 1, "host": "other"}],
                "cpus": {(10, 1): (total_cpu[0], (10, 1))},
                "complete": True,
            }
        },
    )
    inst = DevActivityPlugin().create(
        "ai",
        DevActivityConfig(
            name="ai",
            state_dir=str(tmp_path),
            tools=["claude"],
            session_roots={"claude": [str(root)]},
        ),
    )
    events = []
    try:
        inst.check(lambda *a, **k: None)  # CPU baseline.
        for total, state in [(60.0, "busy"), (60.0, "idle"), (120.0, "busy")]:
            clock[0] += 60
            total_cpu[0] = total
            st = inst.check(lambda *a, **k: events.append(a))
            assert st.metrics["ai_state"] == state
            assert st.metrics["tools"][0]["source"] == "cpu"
            assert st.metrics["probe_limited"]
            assert not st.metrics["probe_errors"]
            assert inst._sessions.cycle_limited["claude"]
            assert not inst._idle_pending
        assert [e[1] for e in events] == ["ai: AI busy", "ai: AI idle", "ai: AI busy"]
    finally:
        inst.stop()


def test_session_limit_does_not_suppress_fresh_hook_idle(tmp_path, monkeypatch):
    _snapshot(monkeypatch, {"claude": True})
    inst = DevActivityPlugin().create(
        "ai", DevActivityConfig(name="ai", state_dir=str(tmp_path), tools=["claude"])
    )
    monkeypatch.setattr(
        inst._sessions, "sample", lambda *a: {"claude": {"limited": True}}
    )
    events = []
    try:
        for state in ("busy", "idle", "busy"):
            _write(tmp_path, "claude", state, time.time())
            st = inst.check(lambda *a, **k: events.append(a))
            assert st.metrics["tools"][0]["source"] == "hook"
            assert st.metrics["probe_limited"]
        assert [e[1] for e in events] == ["ai: AI idle", "ai: AI busy"]
    finally:
        inst.stop()


@pytest.mark.parametrize("active", ["busy", "waiting"])
def test_persistent_hook_error_preserves_next_active_event(
    tmp_path, monkeypatch, active
):
    (tmp_path / "agent-activity-codex.json").write_text("invalid JSON")
    _snapshot(monkeypatch, {"claude": True})
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
        assert [e[1] for e in events] == ["ai: AI idle", f"ai: {title}"]
    finally:
        inst.stop()


@pytest.mark.parametrize("failure", ["hook", "process_limited", "session_limited"])
def test_unrelated_tool_failure_does_not_suppress_fresh_hook_idle(
    tmp_path, monkeypatch, failure
):
    _snapshot(monkeypatch, {"claude": True})
    cfg = DevActivityConfig(
        name="ai", state_dir=str(tmp_path), tools=["claude", "codex"]
    )
    inst = DevActivityPlugin().create("ai", cfg)
    events = []
    try:
        _write(tmp_path, "claude", "busy", time.time())
        inst.check(lambda *a, **k: events.append(a))
        if failure == "hook":
            (tmp_path / "agent-activity-codex.json").write_text("invalid JSON")
        elif failure == "process_limited":
            monkeypatch.setattr(
                da, "scan_activity", lambda *a: {"codex": {"limited": True}}
            )
        else:
            monkeypatch.setattr(
                inst._sessions, "sample", lambda *a: {"codex": {"limited": True}}
            )
        _write(tmp_path, "claude", "idle", time.time())
        inst.check(lambda *a, **k: events.append(a))
        assert [e[1] for e in events] == ["ai: AI idle"]
    finally:
        inst.stop()


def test_i216_cpu_positive_beats_quiet_session(tmp_path, monkeypatch):
    cfg = DevActivityConfig(name="ai", state_dir=str(tmp_path), tools=["claude"])
    inst, _, _ = _check_obs(cfg, monkeypatch, {"claude": True}, {"claude": 180.0})
    monkeypatch.setattr(
        inst._sessions,
        "sample",
        lambda *a: {
            "claude": {
                "state": "idle",
                "age_s": 120,
                "host": "other",
                "vscode_state": None,
                "errors": [],
                "limited": False,
                "complete": True,
            }
        },
    )
    try:
        for _ in range(4):
            st = inst.check(lambda *a, **k: pytest.fail("spurious off/on"))
            assert st.metrics["tools"][0]["state"] == "busy"
            assert st.metrics["tools"][0]["source"] == "cpu"
    finally:
        inst.stop()


def test_i216_stop_timeout_has_late_single_owner_cleanup(tmp_path, monkeypatch):
    import threading

    cfg = DevActivityConfig(name="ai", state_dir=str(tmp_path), tools=["claude"])
    inst = DevActivityPlugin().create("ai", cfg)
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    cleanup = []
    statuses = []

    def blocked(*a):
        entered.set()
        assert release.wait(2)
        return {}

    monkeypatch.setattr(da, "scan_activity", blocked)
    monkeypatch.setattr(
        inst._sessions, "close", lambda: cleanup.append(threading.get_ident())
    )
    worker = threading.Thread(
        target=lambda: statuses.append(
            inst.check(lambda *a, **k: pytest.fail("late emit"))
        )
    )
    stopper = threading.Thread(target=lambda: (inst.stop(0.01), returned.set()))
    worker.start()
    try:
        assert entered.wait(1)
        stopper.start()
        assert returned.wait(0.25), (
            "stop API waited for native probe past supplied budget"
        )
        assert not cleanup, "native resources still belong to the blocked check"
    finally:
        release.set()
        worker.join(2)
        if stopper.ident is not None:
            stopper.join(2)
    assert not worker.is_alive() and not stopper.is_alive()
    assert len(cleanup) == 1
    assert statuses[0].state == "stopped"
    assert inst.check(lambda *a, **k: pytest.fail("reopened")).state == "stopped"


def test_i216_vscode_override_is_rejected():
    with pytest.raises(ValueError, match="VS Code"):
        DevActivityConfig(name="ai", process_patterns={"vscode": "code"})


@pytest.mark.parametrize("layer", ["scandir", "next", "stat", "open_files", "facts"])
@pytest.mark.parametrize("late_error", [False, True])
def test_i216_late_native_owner_closes_without_state_commit(
    tmp_path, monkeypatch, layer, late_error
):
    import threading
    from types import SimpleNamespace

    from taskpaw_v3.monitors import session_activity as sa

    root = tmp_path / "sessions"
    root.mkdir()
    file = root / "fixture.jsonl"
    file.write_text("SENTINEL PRIVATE CONTENT")
    _snapshot(monkeypatch, {"claude": True})
    cfg = DevActivityConfig(
        name="ai",
        tools=["claude"],
        state_dir=str(tmp_path),
        session_roots={"claude": [str(root)]},
    )
    inst = DevActivityPlugin().create("ai", cfg)
    entered, release = threading.Event(), threading.Event()
    closed, results = [], []

    def blocked():
        entered.set()
        assert release.wait(2)
        if late_error:
            if layer == "facts":
                raise da.ActivityStoreError("activity sidecar unavailable")
            raise OSError("PRIVATE NATIVE DETAIL")

    class Entry:
        path = str(file)

        def stat(self, **kwargs):
            if layer == "stat":
                blocked()
            return file.stat(**kwargs)

    class Cursor:
        used = False

        def __next__(self):
            if self.used:
                raise StopIteration
            self.used = True
            if layer == "next":
                blocked()
            return Entry()

        def close(self):
            closed.append(threading.get_ident())

    def scandir(path):
        if layer == "scandir":
            blocked()
        return Cursor()

    monkeypatch.setattr(sa.os, "scandir", scandir)
    monkeypatch.setattr(sa, "WINDOWS", layer != "open_files")
    if layer == "open_files":
        monkeypatch.setattr(
            sa.psutil,
            "Process",
            lambda pid: SimpleNamespace(
                create_time=lambda: 1.0,
                open_files=lambda: (blocked(), [SimpleNamespace(path=str(file))])[1],
            ),
        )
    elif layer == "facts":
        original = da.read_facts

        def read(path, tool):
            blocked()
            return original(path, tool)

        monkeypatch.setattr(da, "read_facts", read)
    worker = threading.Thread(
        target=lambda: results.append(
            inst.check(lambda *a, **k: pytest.fail("late native emit"))
        )
    )
    worker.start()
    try:
        assert entered.wait(1)
        start = time.monotonic()
        inst.stop(0.01)
        inst.stop(0)
        assert time.monotonic() - start < 0.25
        assert not inst._cleanup_complete
    finally:
        release.set()
        worker.join(2)
    assert not worker.is_alive()
    assert results[0].state == "stopped"
    assert not inst._samples and inst._prev_class is None
    assert inst._cleanup_complete and not inst._sessions.cursors
    expected = 0 if layer == "facts" or (layer == "scandir" and late_error) else 1
    assert len(closed) == expected
    assert inst.check(lambda *a, **k: pytest.fail("reopened")).state == "stopped"
    inst.stop(0)
    assert len(closed) == expected


def test_i216_concurrent_checks_have_one_probe_and_cleanup_owner(tmp_path, monkeypatch):
    import threading

    inst = DevActivityPlugin().create(
        "ai",
        DevActivityConfig(
            name="ai",
            tools=["claude"],
            state_dir=str(tmp_path),
        ),
    )
    entered, release = threading.Event(), threading.Event()
    calls, cleanup, results = [], [], []

    def scan(*args):
        calls.append(threading.get_ident())
        entered.set()
        assert release.wait(2)
        return {}

    monkeypatch.setattr(da, "scan_activity", scan)
    monkeypatch.setattr(
        inst._sessions, "close", lambda: cleanup.append(threading.get_ident())
    )
    workers = [
        threading.Thread(
            target=lambda: results.append(
                inst.check(lambda *a, **k: pytest.fail("late emit"))
            )
        )
        for _ in range(2)
    ]
    workers[0].start()
    try:
        assert entered.wait(1)
        workers[1].start()
        inst.stop(0)
    finally:
        release.set()
        for worker in workers:
            if worker.ident is not None:
                worker.join(2)
    assert all(not worker.is_alive() for worker in workers)
    assert len(calls) == len(cleanup) == 1
    assert cleanup[0] == calls[0]
    assert len(results) == 2 and all(st.state == "stopped" for st in results)


@pytest.mark.parametrize("event", ["Stop", "SubagentStop"])
@pytest.mark.parametrize("stop_first", [False, True])
@pytest.mark.parametrize("stop_active", [None, False, True])
def test_i216_stop_attempt_is_unknown_after_restart(
    tmp_path, monkeypatch, event, stop_first, stop_active
):
    import io

    from taskpaw_v3.integrations import activity_writer as aw

    path = tmp_path / "agent-activity-claude.json"
    start = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "fake-session",
        "prompt_id": "fake-prompt",
    }
    stop = {**start, "hook_event_name": event}
    if stop_active is not None:
        stop["stop_hook_active"] = stop_active
    if event == "SubagentStop":
        start.update(hook_event_name="SubagentStart", agent_id="fake-child")
        stop["agent_id"] = "fake-child"
    for payload in [stop, start] if stop_first else [start, stop]:
        monkeypatch.setattr(aw.sys, "stdin", io.StringIO(json.dumps(payload)))
        assert aw.main(["--tool", "claude", "--path", str(path)]) == 0
    for _ in range(2):
        inst = DevActivityPlugin().create(
            "ai",
            DevActivityConfig(
                name="ai", tools=["claude"], state_dir=str(tmp_path), observe=False
            ),
        )
        try:
            st = inst.check(lambda *a, **k: pytest.fail("unknown emitted completion"))
            assert st.metrics["tools"][0]["state"] is None
        finally:
            inst.stop()


def test_i216_independent_busy_survives_other_session_stop(tmp_path, monkeypatch):
    import io

    from taskpaw_v3.integrations import activity_writer as aw

    path = tmp_path / "agent-activity-claude.json"
    for sid, event in [("A", "UserPromptSubmit"), ("B", "Stop")]:
        monkeypatch.setattr(
            aw.sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": event,
                        "session_id": sid,
                        "prompt_id": "fake-" + sid,
                    }
                )
            ),
        )
        assert aw.main(["--tool", "claude", "--path", str(path)]) == 0
    inst = DevActivityPlugin().create(
        "ai",
        DevActivityConfig(
            name="ai", tools=["claude"], state_dir=str(tmp_path), observe=False
        ),
    )
    try:
        assert inst.check(lambda *a, **k: None).metrics["ai_state"] == "busy"
    finally:
        inst.stop()


@pytest.mark.parametrize(
    "writer_freshness", [None, 60.0], ids=["CLI-default300", "helper-explicit60"]
)
def test_i216_default_cli300_and_helper60_reclamation(
    tmp_path, monkeypatch, writer_freshness
):
    import io

    from taskpaw_v3.integrations import activity_writer as aw

    clock = [1000.0]
    monkeypatch.setattr(aw.time, "time", lambda: clock[0])
    path = tmp_path / "agent-activity-codex.json"
    cfg = DevActivityConfig(
        name="ai",
        tools=["codex"],
        state_dir=str(tmp_path),
        observe=False,
        freshness_seconds=60,
    )
    inst = DevActivityPlugin().create("ai", cfg)
    events = []

    def send(session, event, unit=None):
        payload = {
            "session_id": session,
            "turn_id": "turn-" + session,
            "hook_event_name": event,
        }
        if unit is not None:
            payload["tool_use_id"] = "call-" + str(unit)
        monkeypatch.setattr(aw.sys, "stdin", io.StringIO(json.dumps(payload)))
        if writer_freshness is None:
            assert aw.main(["--tool", "codex", "--path", str(path)]) == 0
        else:
            fact = aw.hook_fact(json.dumps(payload), "codex", clock[0])
            assert fact is not None
            assert aw.publish_hook(
                path,
                fact,
                "idle" if event == "Interrupt" else "busy",
                session,
                clock[0],
                freshness=writer_freshness,
            )

    try:
        send("A", "UserPromptSubmit")
        assert (
            inst.check(lambda *a, **k: events.append(a)).metrics["ai_state"] == "busy"
        )
        clock[0] = (
            1301.0 if writer_freshness is None else 1061.0
        )  # Distinct CLI300s/helper60s acceptance.
        for i in range(2047):
            send("B", "PostToolUse", i)
        clock[0] += 1
        send("B", "Interrupt")
        status = inst.check(lambda *a, **k: events.append(a))
        assert status.metrics["tools"][0]["state"] is None
        assert not events, "B final cannot declare silent unbound A complete"
        stored = aw.read_facts(path, "codex")
        assert len(stored["facts"]) == 2048 and len(stored["summaries"]) == 1
        for now in (1400.0, 90000.0):
            clock[0] = now
            send("B", "SessionStart")  # normal retention, no manual DB surgery
            restarted = DevActivityPlugin().create("restart", cfg)
            try:
                status = restarted.check(
                    lambda *a, **k: pytest.fail("restart completion")
                )
                assert status.metrics["tools"][0]["state"] is None
                assert len(aw.read_facts(path, "codex")["summaries"]) == 1
            finally:
                restarted.stop()
        send("A", "Interrupt")
        assert not aw.read_facts(path, "codex")["summaries"]
    finally:
        inst.stop()


def test_i216_uncertain_A_survives_other_tool_busy_then_final(tmp_path, monkeypatch):
    import io

    from taskpaw_v3.integrations import activity_writer as aw

    cfg = DevActivityConfig(
        name="ai", tools=["claude", "codex"], observe=False, state_dir=str(tmp_path)
    )
    inst = DevActivityPlugin().create("ai", cfg)
    events = []
    try:
        for tool, event in [
            ("claude", "UserPromptSubmit"),
            ("claude", "Stop"),
            ("codex", "UserPromptSubmit"),
            ("codex", "Interrupt"),
        ]:
            raw = {
                "hook_event_name": event,
                "session_id": "fake-" + tool,
                "prompt_id" if tool == "claude" else "turn_id": "fake-turn",
            }
            monkeypatch.setattr(aw.sys, "stdin", io.StringIO(json.dumps(raw)))
            assert (
                aw.main(
                    [
                        "--tool",
                        tool,
                        "--path",
                        str(tmp_path / f"agent-activity-{tool}.json"),
                    ]
                )
                == 0
            )
            status = inst.check(lambda *a, **k: events.append(a))
        assert status.metrics["tools"][0]["state"] is None
        assert status.metrics["ai_state"] != "idle"
        assert not any(e[1] == "ai: AI idle" for e in events)
    finally:
        inst.stop()


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize(
    "case,expected",
    [
        ("other_session_final", "busy"),
        ("other_turn_final", "busy"),
        ("child_stop", "busy"),
        ("matching_interrupt", "idle"),
        ("unbound_session_end", "busy"),
        ("bound_session_end", "idle"),
        ("other_incarnation_end", "busy"),
        ("missing_turn", None),
        ("unpaired_permission", None),
        ("presence_only", None),
    ],
)
def test_i216_independent_scope_and_finality_oracles(tmp_path, case, expected, reverse):
    from taskpaw_v3.integrations import activity_writer as aw

    rows = [{"hook_event_name": "UserPromptSubmit", "session_id": "A", "turn_id": "T"}]
    producers = [(10, 1.0)]
    bound = set()
    if case == "other_session_final":
        rows.append({"hook_event_name": "Interrupt", "session_id": "B", "turn_id": "T"})
    elif case == "other_turn_final":
        rows.append(
            {"hook_event_name": "Interrupt", "session_id": "A", "turn_id": "older"}
        )
    elif case == "child_stop":
        rows.append(
            {
                "hook_event_name": "SubagentStop",
                "session_id": "A",
                "turn_id": "T",
                "agent_id": "child",
            }
        )
    elif case == "matching_interrupt":
        rows.append({"hook_event_name": "Interrupt", "session_id": "A", "turn_id": "T"})
    elif case.endswith("session_end") or case == "other_incarnation_end":
        rows.append({"hook_event_name": "SessionEnd", "session_id": "A"})
        if case == "unbound_session_end":
            producers = [None, None]
        elif case == "other_incarnation_end":
            producers.append((10, 2.0))
    elif case == "missing_turn":
        rows[0].pop("turn_id")
    elif case == "unpaired_permission":
        rows[0].update(hook_event_name="PostToolUse", tool_use_id="fake-call")
        rows.append(
            {"hook_event_name": "PermissionRequest", "session_id": "A", "turn_id": "T"}
        )
    elif case == "presence_only":
        rows = [{"hook_event_name": "SessionStart", "session_id": "A"}]
    path = tmp_path / "agent-activity-codex.json"
    facts = [
        aw.hook_fact(
            json.dumps(row), "codex", 1000, producers[min(i, len(producers) - 1)]
        )
        for i, row in enumerate(rows)
    ]
    for fact in reversed(facts) if reverse else facts:
        assert fact is not None
        aw.publish_fact(path, fact)
    out = da.read_hook_activity(
        str(tmp_path),
        "codex",
        300,
        1001,
        {"complete": True, "roots": [{"pid": 10, "created": 1.0, "host": "other"}]},
        bound,
        [],
    )
    assert out["state"] == expected
    if case in (
        "child_stop",
        "unbound_session_end",
        "other_incarnation_end",
        "missing_turn",
        "unpaired_permission",
    ):
        assert out["unknown"]
    else:
        assert not out["unknown"]


@pytest.mark.parametrize(
    "scan,expected",
    [
        ({"complete": True, "roots": []}, "idle"),
        ({"complete": False, "roots": []}, None),
        ({"complete": True, "errors": ["denied"], "roots": []}, None),
        (
            {"complete": True, "roots": [{"pid": 10, "created": 2.0, "host": "other"}]},
            "idle",
        ),
    ],
)
def test_i216_only_previously_bound_complete_exit_resolves(tmp_path, scan, expected):
    from taskpaw_v3.integrations import activity_writer as aw

    fact = aw.hook_fact(
        json.dumps(
            {"hook_event_name": "UserPromptSubmit", "session_id": "A", "turn_id": "T"}
        ),
        "codex",
        1000,
        (10, 1.0),
    )
    assert fact is not None
    aw.publish_fact(tmp_path / "agent-activity-codex.json", fact)
    bound = set()
    assert (
        da.read_hook_activity(
            str(tmp_path),
            "codex",
            300,
            1001,
            {"complete": True, "roots": [{"pid": 10, "created": 1.0, "host": "other"}]},
            bound,
            [],
        )["state"]
        == "busy"
    )
    assert (
        da.read_hook_activity(str(tmp_path), "codex", 300, 2000, scan, bound, [])[
            "state"
        ]
        == expected
    )
    # A fresh reader cannot manufacture the lost binding history after restart.
    assert (
        da.read_hook_activity(str(tmp_path), "codex", 300, 2000, scan, set(), [])[
            "state"
        ]
        is None
    )


def test_i216_legacy_idle_cannot_prove_whole_tool_completion(tmp_path):
    path = tmp_path / "agent-activity-codex.json"
    path.write_text(
        json.dumps({"tool": "codex", "state": "idle", "ts": 1000, "session": "legacy"})
    )
    out = da.read_hook_activity(str(tmp_path), "codex", 300, 1001, {}, set(), [])
    assert out["state"] is None and out["unknown"] and out["watermark"] == 1000


@pytest.mark.parametrize(
    "uncovered_cpu,partial", [(180.0, False), (180.0, True), (0.0, False), (1.0, False)]
)
def test_i216_sr001_waiting_root_cannot_suppress_uncovered_cpu(
    tmp_path, monkeypatch, uncovered_cpu, partial
):
    from taskpaw_v3.integrations import activity_writer as aw

    clock, counter = [1000.0, 10.0], [0.0]
    monkeypatch.setattr(da.time, "time", lambda: clock[0])
    monkeypatch.setattr(da.time, "monotonic", lambda: clock[1])
    fact = aw.hook_fact(
        json.dumps(
            {"hook_event_name": "PermissionRequest", "session_id": "A", "turn_id": "T"}
        ),
        "codex",
        1000,
        (10, 1.0),
    )
    assert fact is not None
    aw.publish_fact(tmp_path / "agent-activity-codex.json", fact)

    def scan(*args):
        return {
            "codex": {
                "present": True,
                "complete": not partial,
                "errors": ["denied"] if partial else [],
                "roots": [
                    {"pid": 10, "created": 1.0, "host": "other"},
                    {"pid": 20, "created": 2.0, "host": "vscode"},
                ],
                "cpus": {
                    (10, 1.0): (counter[0] * 1.8, (10, 1.0)),
                    (20, 2.0): (counter[0] * uncovered_cpu / 100, (20, 2.0)),
                },
            },
            "vscode": {"present": True, "roots": [], "cpus": {}},
        }

    monkeypatch.setattr(da, "scan_activity", scan)
    inst = DevActivityPlugin().create(
        "ai",
        DevActivityConfig(
            name="ai",
            tools=["codex", "vscode"],
            state_dir=str(tmp_path),
            session_activity=False,
        ),
    )
    events = []
    try:
        assert (
            inst.check(lambda *a, **k: events.append(a)).metrics["ai_state"]
            == "waiting"
        )
        for n in range(1, 4):
            clock[:], counter[0] = [1000.0 + n, 10.0 + n], float(n)
            metrics = inst.check(lambda *a, **k: events.append(a)).metrics
            row = metrics["tools"][0]
            if uncovered_cpu >= 8:
                assert metrics["ai_state"] == "busy" and metrics["busy_tools"] == [
                    "codex"
                ]
                assert row["source"] == "cpu" and row["observed"] and row["cpu"] == 180
                assert row["host"] == "vscode" and row["vscode_state"] == "busy"
                assert metrics["tools"][1]["state"] == "busy"
            else:
                assert metrics["ai_state"] == "waiting" and row["source"] == "hook"
        assert not any("AI idle" in event[1] for event in events)
        assert len(events) == (1 if uncovered_cpu >= 8 else 0)
        assert metrics["duty"]["ratio"] == (0.75 if uncovered_cpu >= 8 else 0)
    finally:
        inst.stop(0)


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("restart", [False, True])
def test_i216_sr002_unavailable_store_defers_other_final(
    tmp_path, monkeypatch, shared, restart
):
    from taskpaw_v3.integrations import activity_writer as aw

    monkeypatch.setattr(da.time, "time", lambda: 1001.0)
    _snapshot(monkeypatch, {"claude": True})
    path = tmp_path / (
        "agent-activity.json" if shared else "agent-activity-claude.json"
    )
    fact = aw.hook_fact(
        json.dumps(
            {"hook_event_name": "UserPromptSubmit", "session_id": "A", "prompt_id": "T"}
        ),
        "claude",
        1000,
        (10, 1.0),
    )
    assert fact is not None
    assert aw.publish_hook(path, fact, "busy", "A", 1000)
    original = aw.sidecar_path(path).read_bytes()
    cfg = DevActivityConfig(
        name="ai", tools=["claude", "codex"], state_dir=str(tmp_path), observe=False
    )
    inst = DevActivityPlugin().create("ai", cfg)
    events = []
    try:
        assert (
            inst.check(lambda *a, **k: events.append(a)).metrics["ai_state"] == "busy"
        )
        aw.sidecar_path(path).write_bytes(b"owned-corrupt-sqlite")
        path.unlink()
        if restart:
            inst.stop(0)
            inst = DevActivityPlugin().create("ai", cfg)
        _write(tmp_path, "codex", "busy", 1000)
        assert (
            inst.check(lambda *a, **k: events.append(a)).metrics["ai_state"] == "busy"
        )
        _write(tmp_path, "codex", "idle", 1000)
        status = inst.check(lambda *a, **k: events.append(a))
        assert status.metrics["ai_state"] != "idle"
        assert not any("AI idle" in event[1] for event in events)
        assert status.metrics["probe_errors"] == [
            {"tool": tool, "layer": "hook", "code": "unavailable"}
            for tool in (["claude", "codex"] if shared else ["claude"])
        ]
        assert "owned-corrupt" not in json.dumps(status.metrics)
        # Restore only the owned fixture's actual prior bytes, then deliver a
        # documented bound final. This is not automatic product DB rebuilding.
        aw.sidecar_path(path).write_bytes(original)
        final = aw.hook_fact(
            json.dumps({"hook_event_name": "SessionEnd", "session_id": "A"}),
            "claude",
            1000,
            (10, 1.0),
        )
        assert final is not None
        assert aw.publish_hook(path, final, "idle", "A", 1000)
        assert (
            inst.check(lambda *a, **k: events.append(a)).metrics["ai_state"] == "idle"
        )
    finally:
        inst.stop(0)


@pytest.mark.parametrize("bad_legacy", [False, True])
def test_i216_sr002_missing_rich_store_is_not_a_barrier(
    tmp_path, monkeypatch, bad_legacy
):
    monkeypatch.setattr(da.time, "time", lambda: 1001.0)
    if bad_legacy:
        (tmp_path / "agent-activity-claude.json").write_text("not-json")
    inst = DevActivityPlugin().create(
        "ai",
        DevActivityConfig(
            name="ai", tools=["claude", "codex"], observe=False, state_dir=str(tmp_path)
        ),
    )
    try:
        _write(tmp_path, "codex", "busy", 1000)
        inst.check(lambda *a, **k: None)
        _write(tmp_path, "codex", "idle", 1000)
        assert inst.check(lambda *a, **k: None).metrics["ai_state"] == "idle"
        assert not (
            tmp_path / "agent-activity-claude.json.activity-v2.sqlite3"
        ).exists()
    finally:
        inst.stop(0)


def test_i216_sr003_actual_main_2048_refusal_survives_B_and_restart(
    tmp_path, monkeypatch, capsys
):
    import io

    from taskpaw_v3.integrations import activity_writer as aw

    path = tmp_path / "agent-activity-codex.json"
    raw_B = {"hook_event_name": "Interrupt", "session_id": "B", "turn_id": "B"}
    for n in range(2048):
        fact = aw.hook_fact(json.dumps(raw_B), "codex", 1000, (n + 1, 1.0))
        assert fact is not None
        aw.publish_fact(path, fact)
    before = aw.read_facts(path, "codex")
    clock = [1001.0]
    monkeypatch.setattr(aw.time, "time", lambda: clock[0])
    cfg = DevActivityConfig(
        name="ai",
        tools=["codex"],
        state_dir=str(tmp_path),
        observe=False,
        freshness_seconds=60,
    )

    def send(raw, producer):
        monkeypatch.setattr(aw, "_producer_identity", lambda: producer)
        monkeypatch.setattr(aw.sys, "stdin", io.StringIO(json.dumps(raw)))
        return aw.main(["--tool", "codex", "--path", str(path)])

    assert (
        send(
            {"hook_event_name": "UserPromptSubmit", "session_id": "A", "turn_id": "A"},
            (9001, 1.0),
        )
        == 1
    )
    assert json.loads(path.read_text())["fact_committed"] is False
    assert aw.read_facts(path, "codex")["facts"] == before["facts"]
    assert "activity writer: fact write failed" in capsys.readouterr().err
    for now in (1002.0, 2000.0, 90000.0):
        clock[0] = now
        assert send(raw_B, (1, 1.0)) == 0
        inst = DevActivityPlugin().create("ai", cfg)
        try:
            metrics = inst.check(
                lambda *a, **k: pytest.fail("refused A completion")
            ).metrics
            assert (
                metrics["ai_state"] != "idle" and metrics["tools"][0]["state"] is None
            )
            assert len(aw.read_facts(path, "codex")["summaries"]) == 1
        finally:
            inst.stop()
    clock[0] = 90001.0
    assert (
        send(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "B",
                "turn_id": "new-B",
            },
            (1, 1.0),
        )
        == 0
    )
    inst = DevActivityPlugin().create("ai", cfg)
    events = []
    try:
        assert (
            inst.check(lambda *a, **k: events.append(a)).metrics["ai_state"] == "busy"
        )
        assert (
            send(
                {"hook_event_name": "Interrupt", "session_id": "B", "turn_id": "new-B"},
                (1, 1.0),
            )
            == 0
        )
        assert (
            inst.check(lambda *a, **k: events.append(a)).metrics["ai_state"] != "idle"
        )
        assert not any("AI idle" in event[1] for event in events)
    finally:
        inst.stop()


@pytest.mark.parametrize("summary_first", [False, True])
@pytest.mark.parametrize("current_event", ["SessionEnd", "PostToolUse", "Stop"])
def test_i216_x1_actual_main_confirmed_end_survives_reclamation(
    tmp_path, monkeypatch, summary_first, current_event
):
    """A proved closed session must not return as unknown after writer cleanup."""
    import io

    from taskpaw_v3.integrations import activity_writer as aw

    clock = [1000.0]
    monkeypatch.setattr(aw.time, "time", lambda: clock[0])
    monkeypatch.setattr(aw, "_producer_identity", lambda: (10, 1.0))
    monkeypatch.setattr(aw, "_FACT_CAP", 4)
    _snapshot(monkeypatch, {"codex": True})
    path = tmp_path / "agent-activity-codex.json"
    cfg = DevActivityConfig(
        name="ai", tools=["codex"], state_dir=str(tmp_path), observe=False
    )
    inst = DevActivityPlugin().create("ai", cfg)

    def send(event, session="A", unit=None):
        raw = {
            "hook_event_name": event,
            "session_id": session,
            "turn_id": "turn-" + session,
        }
        if unit is not None:
            raw["tool_use_id"] = unit
        monkeypatch.setattr(aw.sys, "stdin", io.StringIO(json.dumps(raw)))
        assert aw.main(["--tool", "codex", "--path", str(path)]) == 0

    try:
        send("UserPromptSubmit")
        assert inst.check(lambda *a, **k: None).metrics["ai_state"] == "busy"
        if summary_first:
            send("PostToolUse", "B", "one")
            send("PostToolUse", "B", "two")
            clock[0] = 1301.0
            send("Interrupt", "B")
        clock[0] += 1
        send("SessionEnd")
        if summary_first:
            assert aw.read_facts(path, "codex")["summaries"]
        assert inst.check(lambda *a, **k: None).metrics["ai_state"] == "idle"
        if current_event != "SessionEnd":
            clock[0] += 1
            send(
                current_event,
                unit="delayed" if current_event == "PostToolUse" else None,
            )
            assert inst.check(lambda *a, **k: None).metrics["ai_state"] == "idle"
        clock[0] = 90000.0
        send("Interrupt", "B")  # Actual rich publisher drives normal >24h cleanup.
        assert not aw.read_facts(path, "codex")["summaries"]
        inst.stop(0)
        restarted = DevActivityPlugin().create("restart", cfg)
        try:
            status = restarted.check(lambda *a, **k: pytest.fail("restart event"))
            assert status.metrics["ai_state"] == "idle"
            assert status.metrics["tools"][0]["state"] == "idle"
        finally:
            restarted.stop(0)
    finally:
        inst.stop(0)


def _x1_main(
    monkeypatch,
    path,
    event,
    ts,
    *,
    session="A",
    producer=(10, 1.0),
    unit=None,
    turn=None,
):
    import io

    from taskpaw_v3.integrations import activity_writer as aw

    raw = {
        "hook_event_name": event,
        "session_id": session,
        "turn_id": turn or "turn-" + session,
    }
    if unit is not None:
        raw["tool_use_id"] = unit
    monkeypatch.setattr(aw.sys, "stdin", io.StringIO(json.dumps(raw)))
    monkeypatch.setattr(aw.time, "time", lambda: ts)
    monkeypatch.setattr(aw, "_producer_identity", lambda: producer)
    assert aw.main(["--tool", "codex", "--path", str(path)]) == 0


@pytest.mark.parametrize("current_event", ["SessionEnd", "PostToolUse", "Stop"])
def test_i216_d01_current_projection_resolves_after_witness_expiry(
    tmp_path, monkeypatch, current_event
):
    from taskpaw_v3.integrations import activity_writer as aw

    path = tmp_path / "agent-activity-codex.json"
    _snapshot(monkeypatch, {"codex": True})
    cfg = DevActivityConfig(
        name="ai", tools=["codex"], state_dir=str(tmp_path), observe=False
    )
    inst = DevActivityPlugin().create("ai", cfg)
    try:
        _x1_main(monkeypatch, path, "UserPromptSubmit", 1000)
        assert inst.check(lambda *a, **k: None).metrics["ai_state"] == "busy"
        _x1_main(monkeypatch, path, "SessionEnd", 1001)
        assert inst.check(lambda *a, **k: None).metrics["ai_state"] == "idle"
        if current_event != "SessionEnd":
            _x1_main(monkeypatch, path, current_event, 1002, unit="delayed")
        before_json = path.read_bytes()
        # This existing fact-only API must reclaim without inventing a JSON slot.
        cleanup = aw.hook_fact(
            json.dumps({"hook_event_name": "SessionStart", "session_id": "B"}),
            "codex",
            90000,
            (20, 2.0),
        )
        assert cleanup is not None
        aw.publish_fact(path, cleanup)
        assert path.read_bytes() == before_json
        store = aw.read_facts(path, "codex")
        assert not any(r["session"] == aw._hash("A") for r in store["facts"])
        assert (
            not store["summaries"] and store["projection_link"]["resolved"] is not None
        )
        assert aw._projection_matches(json.loads(path.read_text()), store)
        monkeypatch.setattr(aw.time, "time", lambda: 90000)
        inst.stop(0)
        inst = DevActivityPlugin().create("restart", cfg)
        assert (
            inst.check(lambda *a, **k: pytest.fail("restart event")).metrics["ai_state"]
            == "idle"
        )
        # The receipt cannot close a new continuable callback beyond the horizon.
        _x1_main(monkeypatch, path, "Stop", 90001, unit="beyond-horizon")
        status = inst.check(lambda *a, **k: pytest.fail("false completion"))
        assert status.metrics["tools"][0]["state"] is None
        assert status.metrics["ai_state"] != "idle"
        assert aw.read_facts(path, "codex")["projection_link"]["resolved"] is None
    finally:
        inst.stop(0)


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("summary_first", [False, True])
def test_i216_d02_two_store_confirmation_then_independent_reclaim(
    tmp_path, monkeypatch, reverse, summary_first
):
    from taskpaw_v3.integrations import activity_writer as aw

    paths = (tmp_path / "agent-activity-codex.json", tmp_path / "agent-activity.json")
    target, source = paths if not reverse else paths[::-1]
    monkeypatch.setattr(aw, "_FACT_CAP", 4)
    _snapshot(monkeypatch, {"codex": True})
    cfg = DevActivityConfig(
        name="ai", tools=["codex"], state_dir=str(tmp_path), observe=False
    )
    inst = DevActivityPlugin().create("ai", cfg)
    try:
        _x1_main(monkeypatch, target, "Stop", 1000)
        if summary_first:
            _x1_main(monkeypatch, target, "PostToolUse", 1000, session="B", unit="one")
            _x1_main(monkeypatch, target, "PostToolUse", 1000, session="B", unit="two")
            _x1_main(monkeypatch, target, "Interrupt", 1301, session="B")
            _x1_main(
                monkeypatch,
                target,
                "SessionStart",
                1302,
                session="neutral",
                producer=(20, 2.0),
            )
            assert aw.read_facts(target, "codex")["summaries"]
            _x1_main(monkeypatch, target, "Stop", 1303)
        final_ts = 1304 if summary_first else 1001
        _x1_main(monkeypatch, source, "SessionEnd", final_ts)
        assert inst.check(lambda *a, **k: None).metrics["ai_state"] == "idle"
        copies = []
        for path in paths:
            store = aw.read_facts(path, "codex")
            assert not store["summaries"]
            copies.append(
                [r for r in store["facts"] if r["kind"] == "verified_session_end"]
            )
        assert (
            copies[0] == copies[1]
            and len(copies[0]) == 1
            and copies[0][0]["ts"] == final_ts
        )
        for index, path in enumerate(paths):
            cleanup = aw.hook_fact(
                json.dumps(
                    {"hook_event_name": "SessionStart", "session_id": "neutral"}
                ),
                "codex",
                90000,
                (20, 2.0),
            )
            assert cleanup is not None
            aw.publish_fact(path, cleanup)
            monkeypatch.setattr(aw.time, "time", lambda: 90000)
            inst.stop(0)
            inst = DevActivityPlugin().create("restart-" + str(index), cfg)
            assert inst.check(lambda *a, **k: None).metrics["ai_state"] == "idle"
            assert not aw.read_facts(path, "codex")["summaries"]
    finally:
        inst.stop(0)


@pytest.mark.parametrize(
    "fault",
    [
        "insert",
        "link",
        "summary",
        "delete",
        "commit",
        "lock",
        "cap",
        "schema",
        "identity",
        "stop",
    ],
)
def test_i216_d02_partial_local_commit_no_off_and_retry(tmp_path, monkeypatch, fault):
    import sqlite3
    from contextlib import closing
    from types import SimpleNamespace

    from taskpaw_v3.integrations import activity_writer as aw

    source, target = (
        tmp_path / "agent-activity-codex.json",
        tmp_path / "agent-activity.json",
    )
    _snapshot(monkeypatch, {"codex": True})
    cfg = DevActivityConfig(
        name="ai", tools=["codex"], state_dir=str(tmp_path), observe=False
    )
    inst = DevActivityPlugin().create("ai", cfg)
    events = []
    _x1_main(monkeypatch, target, "Stop", 1000)
    _x1_main(monkeypatch, source, "SessionEnd", 1001)
    target_db = aw.sidecar_path(target)
    row = aw.read_facts(target, "codex")["facts"][0]
    lock = None
    with closing(sqlite3.connect(target_db)) as conn, conn:
        conn.execute(
            "INSERT INTO summaries VALUES(?,?,?,?,?,?,?)",
            tuple(
                row[k] for k in ("tool", "session", "turn", "actor", "pid", "created")
            )
            + (2,),
        )
        clauses = {
            "insert": "BEFORE INSERT ON facts WHEN NEW.kind='verified_session_end'",
            "link": "BEFORE UPDATE OF resolved ON projection_link",
            "summary": "BEFORE DELETE ON summaries",
            "delete": "BEFORE DELETE ON facts",
        }
        if fault in clauses:
            conn.execute(
                f"CREATE TRIGGER injected {clauses[fault]} BEGIN SELECT RAISE(ABORT,'PRIVATE_SENTINEL'); END"
            )
    if fault == "cap":
        _x1_main(monkeypatch, target, "Stop", 1000, unit="another")
        monkeypatch.setattr(aw, "_FACT_CAP", 2)
    if fault == "lock":
        lock = sqlite3.connect(target_db)
        lock.execute("BEGIN IMMEDIATE")
    original = aw._open_store

    def opened(p, **kwargs):
        conn = original(p, **kwargs)
        if not kwargs["writable"] or kwargs.get("create", True):
            return conn

        def commit():
            if Path(p) == target_db and fault == "commit":
                raise sqlite3.OperationalError("PRIVATE_SENTINEL")
            conn.commit()
            if Path(p) == aw.sidecar_path(source):
                if fault == "schema":
                    with closing(sqlite3.connect(target_db)) as other, other:
                        other.execute("PRAGMA user_version=99")
                elif fault == "identity":
                    raw = target_db.read_bytes()
                    old = target_db.stat()
                    # Allocate while the old inode still exists: unlink/recreate
                    # may reuse its identity and miss the intended fault.
                    replacement = tmp_path / "owned-identity-replacement.sqlite3"
                    replacement.write_bytes(raw)
                    replacement.chmod(0o600)
                    allocated = replacement.stat()
                    old_pair = (old.st_dev, old.st_ino)
                    new_pair = (allocated.st_dev, allocated.st_ino)
                    assert new_pair != old_pair
                    replacement.replace(target_db)
                    observed = target_db.stat()
                    assert (observed.st_dev, observed.st_ino) == new_pair != old_pair
                elif fault == "stop":
                    inst._stop_event.set()

        return SimpleNamespace(
            execute=conn.execute,
            rollback=conn.rollback,
            close=conn.close,
            commit=commit,
        )

    def tables():
        with closing(sqlite3.connect(target_db)) as conn:
            return {
                name: conn.execute(f"SELECT * FROM {name} ORDER BY 1,2").fetchall()
                for name in ("facts", "summaries", "tools", "projection_link")
            }

    before = tables()
    monkeypatch.setattr(aw, "_open_store", opened)
    try:
        status = inst.check(lambda *a, **k: events.append(a))
        assert (
            status.state == "stopped"
            if fault == "stop"
            else status.metrics["ai_state"] != "idle"
        )
        assert not events
        committed = aw.read_facts(source, "codex")
        witnesses = [
            r for r in committed["facts"] if r["kind"] == "verified_session_end"
        ]
        assert len(witnesses) == 1 and witnesses[0]["ts"] == 1001
        assert not any(r["kind"] == "session_end" for r in committed["facts"])
        assert tables() == before, (
            "failed target effects roll back; source stays committed"
        )
    finally:
        monkeypatch.setattr(aw, "_open_store", original)
        if lock is not None:
            lock.rollback()
            lock.close()
        with closing(sqlite3.connect(target_db)) as conn, conn:
            conn.execute("DROP TRIGGER IF EXISTS injected")
            if fault == "schema":
                conn.execute(
                    "PRAGMA user_version=2"
                )  # Restore only the injected fixture header.
        monkeypatch.setattr(aw, "_FACT_CAP", 2048)
    try:
        if fault == "stop":
            inst.stop(0)
            inst = DevActivityPlugin().create("retry", cfg)
        monkeypatch.setattr(aw.time, "time", lambda: 1500)
        assert (
            inst.check(lambda *a, **k: events.append(a)).metrics["ai_state"] == "idle"
        )
        target_witness = [
            r
            for r in aw.read_facts(target, "codex")["facts"]
            if r["kind"] == "verified_session_end"
        ]
        assert target_witness == witnesses and target_witness[0]["ts"] == 1001
        assert not aw.read_facts(target, "codex")["summaries"]
    finally:
        inst.stop(0)


@pytest.mark.parametrize("fault", ["insert", "commit"])
def test_i216_d02_source_failure_rolls_back_both_then_later_retry(
    tmp_path, monkeypatch, fault
):
    import sqlite3
    from contextlib import closing
    from types import SimpleNamespace

    from taskpaw_v3.integrations import activity_writer as aw

    source, target = (
        tmp_path / "agent-activity-codex.json",
        tmp_path / "agent-activity.json",
    )
    _snapshot(monkeypatch, {"codex": True})
    _x1_main(monkeypatch, target, "Stop", 1000)
    _x1_main(monkeypatch, source, "SessionEnd", 1001)
    source_db = aw.sidecar_path(source)
    if fault == "insert":
        with closing(sqlite3.connect(source_db)) as conn, conn:
            conn.execute(
                "CREATE TRIGGER injected BEFORE INSERT ON facts WHEN NEW.kind='verified_session_end' BEGIN SELECT RAISE(ABORT,'PRIVATE_SENTINEL'); END"
            )
    before = [aw.read_facts(path, "codex") for path in (source, target)]
    original = aw._open_store

    def opened(p, **kwargs):
        conn = original(p, **kwargs)
        if (
            fault == "commit"
            and kwargs["writable"]
            and not kwargs.get("create", True)
            and Path(p) == source_db
        ):
            return SimpleNamespace(
                execute=conn.execute,
                rollback=conn.rollback,
                close=conn.close,
                commit=lambda: (_ for _ in ()).throw(
                    sqlite3.OperationalError("PRIVATE_SENTINEL")
                ),
            )
        return conn

    monkeypatch.setattr(aw, "_open_store", opened)
    cfg = DevActivityConfig(
        name="ai", tools=["codex"], state_dir=str(tmp_path), observe=False
    )
    inst = DevActivityPlugin().create("ai", cfg)
    try:
        status = inst.check(lambda *a, **k: pytest.fail("failed source completion"))
        assert status.metrics["ai_state"] != "idle"
        assert [aw.read_facts(p, "codex") for p in (source, target)] == before
        monkeypatch.setattr(aw, "_open_store", original)
        with closing(sqlite3.connect(source_db)) as conn, conn:
            conn.execute("DROP TRIGGER IF EXISTS injected")
        monkeypatch.setattr(aw.time, "time", lambda: 1500)
        assert inst.check(lambda *a, **k: None).metrics["ai_state"] == "idle"
        copies = [
            [
                r
                for r in aw.read_facts(p, "codex")["facts"]
                if r["kind"] == "verified_session_end"
            ]
            for p in (source, target)
        ]
        assert copies[0] == copies[1] and copies[0][0]["ts"] == 1001
    finally:
        monkeypatch.setattr(aw, "_open_store", original)
        inst.stop(0)


def test_i216_d01_expired_stored_proof_does_not_close_fresh_work(tmp_path, monkeypatch):
    from taskpaw_v3.integrations import activity_writer as aw

    path = tmp_path / "agent-activity-codex.json"
    _snapshot(monkeypatch, {"codex": True})
    cfg = DevActivityConfig(
        name="ai", tools=["codex"], state_dir=str(tmp_path), observe=False
    )
    inst = DevActivityPlugin().create("ai", cfg)
    try:
        _x1_main(monkeypatch, path, "SessionEnd", 1001)
        assert inst.check(lambda *a, **k: None).metrics["ai_state"] == "idle"
        assert any(
            r["kind"] == "verified_session_end" and r["ts"] == 1001
            for r in aw.read_facts(path, "codex")["facts"]
        )
        # No intervening cleanup: fresh work arrives with the expired witness
        # physically retained. The normal publisher must retire, not renew, it.
        _x1_main(monkeypatch, path, "Stop", 90001, unit="fresh")
        stored = aw.read_facts(path, "codex")
        assert not any(r["kind"] == "verified_session_end" for r in stored["facts"])
        assert any(
            r["kind"] == "stop_attempt" and r["ts"] == 90001 for r in stored["facts"]
        )
        assert stored["projection_link"]["resolved"] is None
        status = inst.check(lambda *a, **k: pytest.fail("expired proof completion"))
        assert status.metrics["tools"][0]["state"] is None
        assert status.metrics["ai_state"] != "idle"
    finally:
        inst.stop(0)


def test_i216_c3_s04_expired_interrupt_retires_current_progress_only(
    tmp_path, monkeypatch
):
    from taskpaw_v3.integrations import activity_writer as aw

    path = tmp_path / "agent-activity-codex.json"
    _x1_main(monkeypatch, path, "PostToolUse", 1000, unit="old")
    for session, event, ts in (
        ("A", "Interrupt", 1001),
        ("neutral", "SessionStart", 90000),
    ):
        fact = aw.hook_fact(
            json.dumps(
                {
                    "hook_event_name": event,
                    "session_id": session,
                    "turn_id": "turn-" + session,
                }
            ),
            "codex",
            ts,
            (10, 1.0),
        )
        assert fact is not None
        aw.publish_fact(path, fact)
    stored = aw.read_facts(path, "codex")
    assert not stored["summaries"]
    assert stored["projection_link"]["resolved"]["ts"] == 1001
    assert aw._projection_matches(json.loads(path.read_text()), stored)
    assert (
        da.read_hook_activity(str(tmp_path), "codex", 300, 90000, {}, set(), [])[
            "state"
        ]
        == "idle"
    )
    _x1_main(monkeypatch, path, "Stop", 90001, unit="fresh")
    out = da.read_hook_activity(str(tmp_path), "codex", 300, 90001, {}, set(), [])
    assert out["unknown"] and out["unresolved"] and out["state"] is None
    assert aw.read_facts(path, "codex")["projection_link"]["resolved"] is None


@pytest.mark.parametrize("foreign", [False, True])
@pytest.mark.parametrize("has_final", [False, True])
def test_i216_d02_complete_exit_uses_only_prior_exact_binding(
    tmp_path, monkeypatch, foreign, has_final
):
    from taskpaw_v3.integrations import activity_writer as aw

    source, target = (
        tmp_path / "agent-activity-codex.json",
        tmp_path / "agent-activity.json",
    )
    _snapshot(monkeypatch, {"codex": True})
    _x1_main(monkeypatch, source, "UserPromptSubmit", 1000)
    _x1_main(monkeypatch, target, "Stop", 1001)
    cfg = DevActivityConfig(
        name="ai", tools=["codex"], state_dir=str(tmp_path), observe=False
    )
    inst = DevActivityPlugin().create("ai", cfg)
    try:
        inst.check(lambda *a, **k: None)  # Bind only the real sampled fixture pair.
        if foreign:
            _x1_main(monkeypatch, target, "Stop", 1002, session="B", producer=(20, 2.0))
        _snapshot(monkeypatch, {"codex": False})
        if has_final:
            # Only the prior exact binding may validate this real callback
            # observed after the sampled root has disappeared.
            _x1_main(monkeypatch, source, "SessionEnd", 1003)
        monkeypatch.setattr(aw.time, "time", lambda: 1500)
        status = inst.check(lambda *a, **k: None)
        assert (status.metrics["ai_state"] == "idle") is (not foreign)
        copies = [
            [
                r
                for r in aw.read_facts(path, "codex")["facts"]
                if r["kind"] == "verified_session_end"
            ]
            for path in (source, target)
        ]
        if has_final:
            assert copies[0] == copies[1] and len(copies[0]) == 1
            assert copies[0][0]["ts"] == 1003
        else:
            assert copies == [[], []], (
                "Complete exit alone cannot mint a durable witness"
            )
        restarted = DevActivityPlugin().create("restart", cfg)
        try:
            state = restarted.check(lambda *a, **k: pytest.fail("restart off")).metrics[
                "ai_state"
            ]
            assert (state == "idle") is (has_final and not foreign)
        finally:
            restarted.stop(0)
    finally:
        inst.stop(0)


def test_i216_d02_expired_partial_proof_cannot_resolve_target(tmp_path, monkeypatch):
    import sqlite3
    from types import SimpleNamespace

    from taskpaw_v3.integrations import activity_writer as aw

    source, target = (
        tmp_path / "agent-activity-codex.json",
        tmp_path / "agent-activity.json",
    )
    _snapshot(monkeypatch, {"codex": True})
    cfg = DevActivityConfig(
        name="ai", tools=["codex"], state_dir=str(tmp_path), observe=False
    )
    inst = DevActivityPlugin().create("ai", cfg)
    _x1_main(monkeypatch, target, "Stop", 1000)
    _x1_main(monkeypatch, source, "SessionEnd", 1001)
    original = aw._open_store

    def opened(p, **kwargs):
        conn = original(p, **kwargs)
        if (
            kwargs["writable"]
            and not kwargs.get("create", True)
            and Path(p) == aw.sidecar_path(target)
        ):
            return SimpleNamespace(
                execute=conn.execute,
                rollback=conn.rollback,
                close=conn.close,
                commit=lambda: (_ for _ in ()).throw(
                    sqlite3.OperationalError("synthetic")
                ),
            )
        return conn

    monkeypatch.setattr(aw, "_open_store", opened)
    try:
        assert inst.check(lambda *a, **k: None).metrics["ai_state"] != "idle"
        monkeypatch.setattr(aw, "_open_store", original)
        monkeypatch.setattr(aw.time, "time", lambda: 90000)
        inst.stop(0)
        inst = DevActivityPlugin().create("expired", cfg)
        status = inst.check(lambda *a, **k: pytest.fail("unconverged off"))
        assert status.metrics["tools"][0]["state"] is None
        assert status.metrics["ai_state"] != "idle"
        assert not any(
            r["kind"] == "verified_session_end"
            for r in aw.read_facts(target, "codex")["facts"]
        )
    finally:
        monkeypatch.setattr(aw, "_open_store", original)
        inst.stop(0)


def test_i216_d02_unrelated_scope_and_wrong_incarnation_stay_unresolved(
    tmp_path, monkeypatch
):
    from taskpaw_v3.integrations import activity_writer as aw

    source, target = (
        tmp_path / "agent-activity-codex.json",
        tmp_path / "agent-activity.json",
    )
    _snapshot(monkeypatch, {"codex": True})
    cfg = DevActivityConfig(
        name="ai", tools=["codex"], state_dir=str(tmp_path), observe=False
    )
    inst = DevActivityPlugin().create("ai", cfg)
    try:
        _x1_main(monkeypatch, target, "Stop", 1000, session="B")
        _x1_main(
            monkeypatch,
            target,
            "Stop",
            1000,
            producer=(10, 2.0),
            unit="wrong-incarnation",
        )
        _x1_main(monkeypatch, source, "SessionEnd", 1001)
        status = inst.check(lambda *a, **k: None)
        assert status.metrics["ai_state"] != "idle"
        stored = aw.read_facts(target, "codex")["facts"]
        assert any(r["session"] == aw._hash("B") for r in stored)
        assert any(r["created"] == 2.0 and r["kind"] == "stop_attempt" for r in stored)
        _x1_main(monkeypatch, target, "UserPromptSubmit", 1002, session="positive")
        assert inst.check(lambda *a, **k: None).metrics["ai_state"] == "busy"
    finally:
        inst.stop(0)


def test_i216_pure_hook_readers_leave_projection_and_database_unchanged(
    tmp_path, monkeypatch
):
    from taskpaw_v3.integrations import activity_writer as aw

    path = tmp_path / "agent-activity-codex.json"
    _x1_main(monkeypatch, path, "SessionEnd", 1000)
    before = (path.read_bytes(), aw.sidecar_path(path).read_bytes())
    for _ in range(3):
        store = aw.read_facts(path, "codex")
        assert all(r["kind"] != "verified_session_end" for r in store["facts"])
        da.read_hook_activity(
            str(tmp_path),
            "codex",
            300,
            1001,
            {"complete": True, "roots": [{"pid": 10, "created": 1.0, "host": "other"}]},
            set(),
            [],
        )
        assert (path.read_bytes(), aw.sidecar_path(path).read_bytes()) == before
    assert not aw.sidecar_path(tmp_path / "agent-activity.json").exists()


@pytest.mark.parametrize("event", ["SessionEnd", "Stop", "PostToolUse"])
def test_i216_c3_s02_same_current_id_keeps_first_time_after_retirement(
    tmp_path, monkeypatch, event
):
    from taskpaw_v3.integrations import activity_writer as aw

    path = tmp_path / "agent-activity-codex.json"
    _snapshot(monkeypatch, {"codex": True})
    cfg = DevActivityConfig(
        name="ai", tools=["codex"], state_dir=str(tmp_path), observe=False
    )
    inst = DevActivityPlugin().create("ai", cfg)
    try:
        _x1_main(monkeypatch, path, "SessionEnd", 1000)
        assert inst.check(lambda *a, **k: None).metrics["ai_state"] == "idle"
        _x1_main(monkeypatch, path, event, 1001 if event != "SessionEnd" else 1000)
        first = json.loads(path.read_text())
        cleanup = aw.hook_fact(
            json.dumps({"hook_event_name": "SessionStart", "session_id": "neutral"}),
            "codex",
            90000,
            (20, 2.0),
        )
        assert cleanup is not None
        aw.publish_fact(path, cleanup)
        _x1_main(monkeypatch, path, event, 90001)
        current = json.loads(path.read_text())
        assert current["fact_id"] == first["fact_id"]
        assert current["link_nonce"] != first["link_nonce"]
        assert current["fact_ts"] == first["fact_ts"]
        assert current["ts"] == 90001, (
            "attempt time must not rewind to the first receipt"
        )
        assert aw.read_facts(path, "codex")["projection_link"]["resolved"] is None
        status = inst.check(lambda *a, **k: None)
        assert status.metrics["tools"][0]["state"] is None
        assert status.metrics["ai_state"] != "idle"
        assert not any(
            r["kind"] == "verified_session_end"
            for r in aw.read_facts(path, "codex")["facts"]
        )
    finally:
        inst.stop(0)


def test_i216_c3_s02_distinct_session_final_witnesses_keep_local_coverage(
    tmp_path, monkeypatch
):
    from taskpaw_v3.integrations import activity_writer as aw

    path = tmp_path / "agent-activity-codex.json"
    _snapshot(monkeypatch, {"codex": True})
    cfg = DevActivityConfig(
        name="ai", tools=["codex"], state_dir=str(tmp_path), observe=False
    )
    inst = DevActivityPlugin().create("ai", cfg)
    try:
        _x1_main(monkeypatch, path, "SessionEnd", 1000)
        _x1_main(monkeypatch, path, "SessionEnd", 1001, turn="different-final-turn")
        assert inst.check(lambda *a, **k: None).metrics["ai_state"] == "idle"
        witnesses = [
            r
            for r in aw.read_facts(path, "codex")["facts"]
            if r["kind"] == "verified_session_end"
        ]
        assert len(witnesses) == 2 and sorted(r["ts"] for r in witnesses) == [
            1000,
            1001,
        ]
        assert not aw.sidecar_path(tmp_path / "agent-activity.json").exists()
    finally:
        inst.stop(0)


@pytest.mark.parametrize("reclaim", [False, True])
def test_i216_m01_expired_receipt_allows_same_root_cpu(tmp_path, monkeypatch, reclaim):
    from taskpaw_v3.integrations import activity_writer as aw

    path = tmp_path / "agent-activity-codex.json"
    wall, mono = [1000.0], [10.0]
    counters = {(10, 1.0): 0.0}
    monkeypatch.setattr(da.time, "monotonic", lambda: mono[0])
    monkeypatch.setattr(
        da,
        "scan_activity",
        lambda *a: {
            "codex": {
                "present": True,
                "complete": True,
                "roots": [
                    {"pid": p, "created": c, "host": "other"} for p, c in counters
                ],
                "cpus": {pair: (value, pair) for pair, value in counters.items()},
            }
        },
    )
    inst = DevActivityPlugin().create(
        "ai",
        DevActivityConfig(
            name="ai", tools=["codex"], state_dir=str(tmp_path), session_activity=False
        ),
    )
    events = []

    def check():
        monkeypatch.setattr(aw.time, "time", lambda: wall[0])
        return inst.check(lambda *a, **k: events.append(a))

    try:
        _x1_main(monkeypatch, path, "UserPromptSubmit", 1000)
        assert check().metrics["ai_state"] == "busy"
        wall[0], mono[0] = 1001, 11
        _x1_main(monkeypatch, path, "SessionEnd", 1001)
        assert check().metrics["ai_state"] == "idle"
        original = path.read_bytes()
        if reclaim:
            fact = aw.hook_fact(
                json.dumps({"hook_event_name": "SessionStart", "session_id": "B"}),
                "codex",
                90000,
                (20, 2.0),
            )
            assert fact is not None
            aw.publish_fact(path, fact)
            assert not any(
                r["session"] == aw._hash("A")
                for r in aw.read_facts(path, "codex")["facts"]
            )
        wall[0], mono[0] = 90000, 100
        check()  # CPU baseline; no hook publication after the original final.
        wall[0], mono[0], counters[(10, 1.0)] = 90001, 101, 1.8
        status = check()
        assert status.metrics["ai_state"] == "busy"
        assert status.metrics["tools"][0]["source"] == "cpu"
        assert status.metrics["tools"][0]["cpu"] == 180.0
        wall[0], mono[0], counters[(10, 1.0)] = 90002, 102, 3.6
        counters[(20, 2.0)] = 0.0
        assert check().metrics["ai_state"] == "busy"
        wall[0], mono[0], counters[(10, 1.0)] = 90003, 103, 5.4
        counters[(20, 2.0)] = 1.8
        assert check().metrics["ai_state"] == "busy"
        before = len(events)
        wall[0], mono[0], counters[(10, 1.0)] = 90004, 104, 7.2
        del counters[(20, 2.0)]
        assert check().metrics["ai_state"] == "busy"
        assert len(events) == before  # Removing the independent root cannot emit off.
        assert path.read_bytes() == original
        assert aw.read_facts(path, "codex")["projection_link"]["resolved"]["ts"] == 1001
    finally:
        inst.stop(0)


@pytest.mark.parametrize(
    "age,expected", [(1, "idle"), (86400, "idle"), (86401, "busy")]
)
def test_i216_m01_receipt_horizon_for_session_positive(
    tmp_path, monkeypatch, age, expected
):
    from taskpaw_v3.integrations import activity_writer as aw

    path = tmp_path / "agent-activity-codex.json"
    _snapshot(monkeypatch, {"codex": True})
    inst = DevActivityPlugin().create(
        "ai", DevActivityConfig(name="ai", tools=["codex"], state_dir=str(tmp_path))
    )
    try:
        _x1_main(monkeypatch, path, "UserPromptSubmit", 1000)
        assert inst.check(lambda *a, **k: None).metrics["ai_state"] == "busy"
        _x1_main(monkeypatch, path, "SessionEnd", 1001)
        assert inst.check(lambda *a, **k: None).metrics["ai_state"] == "idle"
        monkeypatch.setattr(
            inst._sessions,
            "sample",
            lambda snapshot, *a: (
                {}
                if not snapshot["codex"]["roots"]
                else {
                    "codex": {
                        "state": "busy",
                        "age_s": 1,
                        "host": "other",
                        "vscode_state": None,
                        "errors": [],
                        "limited": False,
                    }
                }
            ),
        )
        monkeypatch.setattr(aw.time, "time", lambda: 1001 + age)
        status = inst.check(lambda *a, **k: None)
        assert status.metrics["ai_state"] == expected
        assert status.metrics["tools"][0]["source"] == (
            "session" if expected == "busy" else "hook"
        )
        assert aw.read_facts(path, "codex")["projection_link"]["resolved"]["ts"] == 1001
    finally:
        inst.stop(0)
