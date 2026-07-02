"""External CPU probe (#163): process_util.scan_activity + cpu_percents.

Fake psutil — no real processes are inspected.
"""

from __future__ import annotations

import re

import pytest

from taskpaw_v3.monitors import process_util as pu


class _CpuTimes:
    def __init__(self, user, system):
        self.user = user
        self.system = system


class _Proc:
    def __init__(self, pid, ppid, name, cmdline, user=0.0, system=0.0):
        self.info = {
            "pid": pid,
            "ppid": ppid,
            "name": name,
            "cmdline": cmdline,
            "cpu_times": _CpuTimes(user, system),
        }


class _FakePsutil:
    class NoSuchProcess(Exception): ...

    class AccessDenied(Exception): ...

    class ZombieProcess(Exception): ...

    def __init__(self, procs):
        self._procs = procs

    def process_iter(self, fields=None):
        return list(self._procs)


def _pat(**kv):
    return {k: re.compile(v, re.IGNORECASE) for k, v in kv.items()}


def test_scan_activity_sums_subtree_cpu(monkeypatch):
    # claude(10) → bash(11) → rg(12); nginx(99) is foreign.
    procs = [
        _Proc(10, 1, "claude", ["claude"], user=1.0, system=0.5),  # 1.5
        _Proc(11, 10, "bash", ["bash", "-c", "rg foo"], user=2.0, system=0.0),  # 2.0
        _Proc(12, 11, "rg", ["rg", "foo"], user=0.5, system=0.0),  # 0.5
        _Proc(99, 1, "nginx", ["nginx"], user=9.0, system=9.0),
    ]
    monkeypatch.setattr(pu, "psutil", _FakePsutil(procs))
    out = pu.scan_activity(_pat(claude=r"\bclaude\b"))
    assert out["claude"]["present"] is True
    assert out["claude"]["cpu_seconds"] == pytest.approx(4.0)  # 1.5 + 2.0 + 0.5


def test_scan_activity_absent_tool(monkeypatch):
    monkeypatch.setattr(pu, "psutil", _FakePsutil([_Proc(99, 1, "nginx", ["nginx"])]))
    out = pu.scan_activity(_pat(claude=r"\bclaude\b"))
    assert out["claude"] == {"present": False, "cpu_seconds": 0.0}


def test_scan_activity_no_psutil(monkeypatch):
    monkeypatch.setattr(pu, "psutil", None)
    with pytest.raises(RuntimeError):
        pu.scan_activity(_pat(claude=r"\bclaude\b"))


def test_scan_activity_subtree_is_cycle_safe(monkeypatch):
    # A pathological ppid cycle must not hang (bounded walk).
    procs = [
        _Proc(10, 12, "claude", ["claude"], user=1.0),
        _Proc(11, 10, "child", ["child"], user=1.0),
        _Proc(12, 11, "loop", ["loop"], user=1.0),  # 12's parent is 11 → cycle
    ]
    monkeypatch.setattr(pu, "psutil", _FakePsutil(procs))
    out = pu.scan_activity(_pat(claude=r"\bclaude\b"))
    assert out["claude"]["present"] is True
    assert out["claude"]["cpu_seconds"] == pytest.approx(3.0)  # each counted once


def test_cpu_percents_first_sample_is_zero():
    sample = {"claude": {"present": True, "cpu_seconds": 4.0}}
    pct, new_prev = pu.cpu_percents({}, 0.0, sample, 2.0)
    assert pct == {}  # no prior → no percent yet
    assert new_prev == {"claude": 4.0}


def test_cpu_percents_delta():
    prev = {"claude": 4.0}
    sample = {"claude": {"present": True, "cpu_seconds": 6.0}}
    pct, new_prev = pu.cpu_percents(prev, 0.0, sample, 2.0)
    assert pct["claude"] == pytest.approx(100.0)  # 2 cpu-s over 2 s = 100% of a core
    assert new_prev == {"claude": 6.0}


def test_cpu_percents_exited_child_clamps_to_zero():
    prev = {"claude": 10.0}
    sample = {"claude": {"present": True, "cpu_seconds": 8.0}}  # dropped (child exited)
    pct, _ = pu.cpu_percents(prev, 0.0, sample, 2.0)
    assert pct["claude"] == 0.0


def test_cpu_percents_absent_tool_omitted():
    prev = {"claude": 4.0}
    sample = {"claude": {"present": False, "cpu_seconds": 0.0}}
    pct, new_prev = pu.cpu_percents(prev, 0.0, sample, 2.0)
    assert pct == {} and new_prev == {}
