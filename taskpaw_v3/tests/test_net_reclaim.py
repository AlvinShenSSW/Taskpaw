"""Startup port takeover from a stale SAME-APP backend (seamless updates).

Uses a fake psutil so no real processes are touched; asserts we only ever
terminate our own backend (never a foreign service on the port).
"""

from __future__ import annotations

import socket

from taskpaw_v3.core import net


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _FakeProc:
    def __init__(self, pid, name, cmdline, log, wait_exc=None):
        self.pid = pid
        self._name = name
        self._cmd = cmdline
        self._log = log
        self._wait_exc = wait_exc  # exception class to raise from wait(), or None

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmd

    def terminate(self):
        self._log.append(("terminate", self.pid))

    def kill(self):
        self._log.append(("kill", self.pid))

    def wait(self, timeout=None):
        if self._wait_exc is not None:
            raise self._wait_exc()
        return 0


class _Laddr:
    def __init__(self, ip, port):
        self.ip = ip
        self.port = port


class _Conn:
    def __init__(self, port, pid, status="LISTEN"):
        self.status = status
        self.laddr = _Laddr("0.0.0.0", port)
        self.pid = pid


class _FakePsutil:
    CONN_LISTEN = "LISTEN"

    class NoSuchProcess(Exception): ...

    class AccessDenied(Exception): ...

    class TimeoutExpired(Exception): ...

    class Error(Exception): ...

    def __init__(self, conns, procs):
        self._conns = conns
        self._procs = procs

    def net_connections(self, kind="inet"):
        return self._conns

    def Process(self, pid):
        if pid not in self._procs:
            raise self.NoSuchProcess()
        return self._procs[pid]


def _install(monkeypatch, port, pid, name, cmdline):
    log: list = []
    proc = _FakeProc(pid, name, cmdline, log)
    fake = _FakePsutil([_Conn(port, pid)], {pid: proc})
    monkeypatch.setattr(net, "psutil", fake)
    return log


def test_reclaims_stale_same_role_backend(monkeypatch):
    port = _free_port()  # real port is free → the post-kill wait returns at once
    log = _install(
        monkeypatch, port, 4321, "taskpaw-backend", ["/x/taskpaw-backend", "agent"]
    )
    assert net.reclaim_port_from_stale_instance(
        "127.0.0.1", port, role="agent", what="agent API"
    )
    assert ("terminate", 4321) in log


def test_leaves_foreign_process_untouched(monkeypatch):
    port = _free_port()
    log = _install(monkeypatch, port, 999, "nginx", ["nginx", "-g", "daemon off;"])
    assert not net.reclaim_port_from_stale_instance(
        "127.0.0.1", port, role="agent", what="agent API"
    )
    assert log == []  # never touched a non-TaskPaw process


def test_wrong_role_not_reclaimed(monkeypatch):
    port = _free_port()
    log = _install(
        monkeypatch, port, 55, "taskpaw-backend", ["/x/taskpaw-backend", "hub"]
    )
    # An agent starting must not kill a hub backend (different role).
    assert not net.reclaim_port_from_stale_instance(
        "127.0.0.1", port, role="agent", what="agent API"
    )
    assert log == []


def test_from_source_backend_matched(monkeypatch):
    port = _free_port()
    log = _install(
        monkeypatch, port, 77, "python3", ["python3", "/x/backend_main.py", "hub"]
    )
    assert net.reclaim_port_from_stale_instance(
        "127.0.0.1", port, role="hub", what="hub API"
    )
    assert ("terminate", 77) in log


def test_reclaims_target_triple_suffixed_sidecar(monkeypatch):
    # The Tauri shell may launch taskpaw-backend-<triple>[.exe] (backend_command
    # fallback) — must still be recognized as ours (Codex 外门).
    port = _free_port()
    log = _install(
        monkeypatch,
        port,
        4444,
        "taskpaw-backend-aarch64-apple-darwin",
        ["/x/taskpaw-backend-aarch64-apple-darwin", "agent"],
    )
    assert net.reclaim_port_from_stale_instance(
        "127.0.0.1", port, role="agent", what="agent API"
    )
    assert ("terminate", 4444) in log


def test_stuck_process_wait_timeout_does_not_crash(monkeypatch):
    # A process that won't exit even after kill() (wait raises TimeoutExpired) must
    # NOT abort startup — reclaim logs + returns without raising (Codex 外门).
    port = _free_port()
    log: list = []
    proc = _FakeProc(
        7,
        "taskpaw-backend",
        ["/x/taskpaw-backend", "agent"],
        log,
        wait_exc=_FakePsutil.TimeoutExpired,
    )
    fake = _FakePsutil([_Conn(port, 7)], {7: proc})
    monkeypatch.setattr(net, "psutil", fake)
    # Must not raise; the port wasn't freed → returns False (claim_port fails loud).
    assert (
        net.reclaim_port_from_stale_instance(
            "127.0.0.1", port, role="agent", what="agent API"
        )
        is False
    )
    assert ("terminate", 7) in log and ("kill", 7) in log


def test_no_psutil_is_noop(monkeypatch):
    monkeypatch.setattr(net, "psutil", None)
    assert not net.reclaim_port_from_stale_instance(
        "127.0.0.1", 5680, role="agent", what="agent API"
    )


def test_is_our_backend_matches_name_and_role(monkeypatch):
    fake = _FakePsutil([], {})
    monkeypatch.setattr(net, "psutil", fake)
    log: list = []
    ours = _FakeProc(1, "taskpaw-backend", ["/x/taskpaw-backend", "agent"], log)
    foreign = _FakeProc(2, "node", ["node", "server.js", "agent"], log)
    assert net._is_our_backend(ours, "agent") is True
    assert net._is_our_backend(ours, "hub") is False
    assert net._is_our_backend(foreign, "agent") is False
