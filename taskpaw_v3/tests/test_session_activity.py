"""T2/T7: synthetic metadata only; never inspect the operator's home/processes."""

import importlib
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskpaw_v3.monitors.plugins.dev_activity import DevActivityConfig


@pytest.fixture
def probe(tmp_path, monkeypatch):
    sa = importlib.import_module("taskpaw_v3.monitors.session_activity")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    roots = {t: [str(tmp_path / t)] for t in ("claude", "codex", "kimi")}
    for paths in roots.values():
        Path(paths[0]).mkdir()
    cfg = DevActivityConfig(name="ai", session_roots=roots)
    handles = []

    class Proc:
        def create_time(self):
            return 1.0

        def open_files(self):
            return [SimpleNamespace(path=str(p)) for p in handles]

    monkeypatch.setattr(sa.psutil, "Process", lambda pid: Proc())
    p = sa.SessionActivity(cfg)
    yield sa, p, handles, tmp_path
    p.close()


def live(tool="claude", count=1):
    return {
        tool: {
            "present": True,
            "complete": True,
            "roots": [
                {"pid": n + 10, "created": 1.0, "host": "vscode"} for n in range(count)
            ],
        }
    }


def session(home, tool, age, name=None):
    p = (
        home
        / tool
        / (name or ("rollout-test.jsonl" if tool == "codex" else "test.jsonl"))
    )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("SENTINEL PRIVATE CONTENT")
    os.utime(p, (1000 - age, 1000 - age))
    return p


@pytest.mark.parametrize(
    "age,state",
    [
        (7, "busy"),
        (30, "busy"),
        (30.1, "idle"),
        (300, "idle"),
        (301, None),
        (-4, "busy"),
        (-6, None),
    ],
)
def test_session_mtime_boundaries_and_privacy(probe, monkeypatch, age, state):
    sa, p, _, home = probe
    f = session(home, "claude", age)
    f.chmod(0)
    monkeypatch.setattr(
        Path, "read_text", lambda *a, **k: pytest.fail("session content read")
    )
    monkeypatch.setattr(
        Path, "read_bytes", lambda *a, **k: pytest.fail("session content read")
    )
    monkeypatch.setattr(sa, "WINDOWS", True)
    assert p.sample(live(), set(), 1000)["claude"]["state"] == state


def test_open_old_rollout_outside_cache_and_no_stale_positive(probe):
    _, p, handles, home = probe
    old = session(home, "codex", 196, "rollout-old.jsonl")
    for n in range(70):
        session(home, "codex", 100, f"rollout-{n}.jsonl")
    handles.append(old)
    result = p.sample(live("codex"), set(), 1000)["codex"]
    assert result["state"] == "busy" and result["vscode_state"] == "busy"
    handles.clear()
    assert p.sample(live("codex"), set(), 1000)["codex"]["state"] == "idle"


def test_live_root_required_and_hook_short_circuit(probe, monkeypatch):
    sa, p, _, home = probe
    session(home, "claude", 7)
    monkeypatch.setattr(sa.os, "scandir", lambda *a: pytest.fail("discovery bypassed"))
    monkeypatch.setattr(
        sa.psutil, "Process", lambda *a: pytest.fail("handles bypassed")
    )
    assert p.sample({}, set(), 1000) == {}
    assert p.sample(live(), {"claude"}, 1000) == {}


def test_windows_never_calls_handles(probe, monkeypatch):
    sa, p, _, home = probe
    monkeypatch.setattr(sa, "WINDOWS", True)
    monkeypatch.setattr(sa.psutil, "Process", lambda *a: pytest.fail("Windows handles"))
    session(home, "codex", 196)
    assert p.sample(live("codex"), set(), 1000)["codex"]["state"] == "idle"


@pytest.mark.parametrize("denied", [True, False])
def test_denied_handles_not_idle_but_positive_mtime_survives(
    probe, monkeypatch, denied
):
    sa, p, _, home = probe
    f = session(home, "claude", 100)

    class Proc:
        def create_time(self):
            return 1.0

        def open_files(self):
            if denied:
                raise sa.psutil.AccessDenied(10)
            raise OSError("PRIVATE PATH")

    monkeypatch.setattr(sa.psutil, "Process", lambda pid: Proc())
    out = p.sample(live(), set(), 1000)["claude"]
    assert out["state"] is None and out["errors"]
    assert "PRIVATE" not in str(out)
    os.utime(f, (993, 993))
    assert p.sample(live(), set(), 1000)["claude"]["state"] == "busy"


def test_handle_caps_round_robin_and_pid_reuse(probe, monkeypatch):
    sa, p, _, home = probe
    old = session(home, "claude", 100)
    calls = []

    class Proc:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return 2.0 if self.pid == 10 else 1.0

        def open_files(self):
            calls.append(self.pid)
            return [SimpleNamespace(path=str(home / "irrelevant"))] * 256 + [
                SimpleNamespace(path=str(old))
            ]

    monkeypatch.setattr(sa.psutil, "Process", Proc)
    out = p.sample(live(count=20), set(), 1000)["claude"]
    assert len(calls) <= 16 and out["limited"] and out["state"] is None
    first = set(calls)
    p.sample(live(count=20), set(), 1000)
    assert set(calls) > first


def test_symlink_nonregular_deleted_and_empty_roots(probe):
    _, p, _, home = probe
    target = session(home, "claude", 7)
    (home / "claude" / "link.jsonl").symlink_to(target)
    (home / "claude" / "dir.jsonl").mkdir()
    assert p.sample(live(), set(), 1000)["claude"]["state"] == "busy"
    target.unlink()
    assert p.sample(live(), set(), 1000)["claude"]["state"] is None
    sa = importlib.import_module("taskpaw_v3.monitors.session_activity")
    q = sa.SessionActivity(DevActivityConfig(name="ai", session_roots={"claude": []}))
    assert q.sample(live(), set(), 1000)["claude"]["state"] is None
    q.close()


def test_discovery_bounded_resumes_and_stop_closes(probe):
    _, p, _, home = probe
    for n in range(600):
        session(home, "claude", 100, f"{n}.jsonl")
    out = p.sample(live(), set(), 1000)["claude"]
    assert out["limited"] and out["state"] is None
    assert len(p.candidates["claude"]) <= 64
    p.close()
    assert not p.cursors


@pytest.mark.parametrize(
    "kwargs",
    [
        {"session_busy_seconds": float("nan")},
        {"session_idle_seconds": 20},
        {"session_scan_interval_seconds": float("inf")},
        {"session_max_files": 257},
        {"session_roots": {"claude": ["/tmp/x", "/tmp/x/y"]}},
        {"session_roots": {"claude": [f"/tmp/{n}" for n in range(9)]}},
    ],
)
def test_session_configuration_rejects_invalid(kwargs):
    with pytest.raises(ValueError):
        DevActivityConfig(name="ai", **kwargs)


def test_lexical_alias_roots_rejected(tmp_path):
    with pytest.raises(ValueError):
        DevActivityConfig(
            name="ai",
            session_roots={"claude": [str(tmp_path / "a"), str(tmp_path / "b/../a")]},
        )


def test_handle_cannot_escape_root_via_dotdot(probe):
    _, p, handles, home = probe
    outside = home / "outside.jsonl"
    outside.write_text("private")
    os.utime(outside, (800, 800))
    handles.append(home / "claude/../outside.jsonl")
    assert p.sample(live(), set(), 1000)["claude"]["state"] is None


def test_denied_directory_degraded_missing_normal(probe, monkeypatch):
    sa, p, _, home = probe
    original = sa.os.scandir

    def denied(path):
        if Path(path) == home / "claude":
            raise PermissionError("PRIVATE")
        return original(path)

    monkeypatch.setattr(sa.os, "scandir", denied)
    out = p.sample(live(), set(), 1000)["claude"]
    assert out["errors"] == ["denied"] and out["state"] is None
    (home / "codex").rmdir()
    assert not p.sample(live("codex"), set(), 1000)["codex"]["errors"]


def test_cursor_resumes_cache_evicts_after_denial(probe, monkeypatch):
    sa, p, _, home = probe
    now = [10.0]
    monkeypatch.setattr(sa.time, "monotonic", lambda: now[0])
    for n in range(600):
        session(home, "claude", 100, f"{n}.jsonl")
    assert p.sample(live(), set(), 1000)["claude"]["limited"]
    assert p.cursors
    assert p.sample(live(), set(), 1000)["claude"]["state"] == "idle"
    assert not p.cursors and len(p.candidates["claude"]) == 64
    original = sa.safe_path

    def denied(path):
        if path.suffix == ".jsonl":
            raise PermissionError("PRIVATE")
        return original(path)

    monkeypatch.setattr(sa, "safe_path", denied)
    now[0] += 601
    assert p.sample(live(), set(), 1601)["claude"]["state"] is None
    assert not p.candidates["claude"]


def test_kimi_default_roots_and_name_filters(tmp_path, monkeypatch):
    sa = importlib.import_module("taskpaw_v3.monitors.session_activity")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(sa, "WINDOWS", True)
    root = tmp_path / ".kimi-code/sessions/a/b/c/d"
    root.mkdir(parents=True)
    f = root / "wire.jsonl"
    f.write_text("private")
    os.utime(f, (993, 993))
    p = sa.SessionActivity(DevActivityConfig(name="ai"))
    try:
        assert p.sample(live("kimi"), set(), 1000)["kimi"]["state"] == "busy"
    finally:
        p.close()


def test_discovery_budget_and_depth_limit(probe, monkeypatch):
    sa, p, _, home = probe
    clock = iter([0, 0.2, 0.4, 0.6, 0.8, 1, 1.2])
    monkeypatch.setattr(sa.time, "monotonic", lambda: next(clock, 2))
    assert p.sample(live(), set(), 1000)["claude"]["limited"]
    monkeypatch.setattr(sa.time, "monotonic", lambda: 3)
    session(home, "claude", 7, "a/b/c/d/e/f/g/h/i/too-deep.jsonl")
    out = p.sample(live(), set(), 1000)["claude"]
    assert out["limited"] and out["state"] is None


def test_handle_identity_rechecked_after_native_call(probe, monkeypatch):
    sa, p, _, home = probe
    f = session(home, "claude", 400)
    calls = [0]

    class Proc:
        def create_time(self):
            calls[0] += 1
            return 1 if calls[0] == 1 else 2

        def open_files(self):
            return [SimpleNamespace(path=str(f))]

    monkeypatch.setattr(sa.psutil, "Process", lambda pid: Proc())
    assert p.sample(live(), set(), 1000)["claude"]["state"] is None


def test_unavailable_live_identity_cannot_use_positive_metadata(probe, monkeypatch):
    sa, p, _, home = probe
    session(home, "claude", 7)
    monkeypatch.setattr(sa, "WINDOWS", True)
    snapshot = live()
    snapshot["claude"]["roots"][0]["created"] = None
    snapshot["claude"]["complete"] = False
    out = p.sample(snapshot, set(), 1000)["claude"]
    assert out["state"] is None and out["errors"]
