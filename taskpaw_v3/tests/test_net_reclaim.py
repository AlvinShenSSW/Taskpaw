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


class _Laddr:
    def __init__(self, ip, port):
        self.ip = ip
        self.port = port


class _Conn:
    def __init__(self, port, pid=None, status="LISTEN", ip="0.0.0.0"):
        self.status = status
        self.laddr = _Laddr(ip, port)
        self.pid = pid


class _FakeProc:
    def __init__(self, pid, name, cmdline, log, conns=None, wait_exc=None):
        self.pid = pid
        self._name = name
        self._cmd = cmdline
        self._log = log
        self._conns = conns or []  # this process's own LISTEN sockets
        self._wait_exc = wait_exc  # exception class to raise from wait(), or None

    def name(self):
        return self._name

    def cmdline(self):
        return self._cmd

    def connections(self, kind="inet"):
        # psutil 5.9.x per-process API (no Process.net_connections yet)
        return self._conns

    def terminate(self):
        self._log.append(("terminate", self.pid))

    def kill(self):
        self._log.append(("kill", self.pid))

    def wait(self, timeout=None):
        if self._wait_exc is not None:
            raise self._wait_exc()
        return 0


class _FakePsutil:
    CONN_LISTEN = "LISTEN"

    class NoSuchProcess(Exception): ...

    class AccessDenied(Exception): ...

    class TimeoutExpired(Exception): ...

    class Error(Exception): ...

    def __init__(self, procs):
        self._procs = {p.pid: p for p in procs}

    def process_iter(self, attrs=None):
        return list(self._procs.values())

    def Process(self, pid):
        if pid not in self._procs:
            raise self.NoSuchProcess()
        return self._procs[pid]


def _install(monkeypatch, port, pid, name, cmdline, ip="0.0.0.0"):
    """One fake process listening on `port` — the common single-holder setup."""
    log: list = []
    proc = _FakeProc(pid, name, cmdline, log, conns=[_Conn(port, pid, ip=ip)])
    monkeypatch.setattr(net, "psutil", _FakePsutil([proc]))
    return log


def _fake_ports_free(monkeypatch, occupied=()):
    """Make net.port_available report only `occupied` ports as taken (foreign)."""
    occ = set(occupied)
    monkeypatch.setattr(net, "port_available", lambda host, port: port not in occ)


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
        monkeypatch,
        port,
        77,
        "python3",
        ["python3", "/x/taskpaw_v3/packaging/backend_main.py", "hub"],
    )
    assert net.reclaim_port_from_stale_instance(
        "127.0.0.1", port, role="hub", what="hub API"
    )
    assert ("terminate", 77) in log


def test_foreign_generic_backend_main_not_matched(monkeypatch):
    # A DIFFERENT project's backend_main.py must NOT be treated as ours (Codex 外门).
    port = _free_port()
    log = _install(
        monkeypatch,
        port,
        78,
        "python3",
        ["python3", "/other/app/backend_main.py", "hub"],
    )
    assert not net.reclaim_port_from_stale_instance(
        "127.0.0.1", port, role="hub", what="hub API"
    )
    assert log == []


def test_from_source_module_form_matched(monkeypatch):
    port = _free_port()
    log = _install(
        monkeypatch,
        port,
        79,
        "python3",
        ["python3", "-m", "taskpaw_v3.packaging.backend_main", "agent"],
    )
    assert net.reclaim_port_from_stale_instance(
        "127.0.0.1", port, role="agent", what="agent API"
    )
    assert ("terminate", 79) in log


def test_module_entrypoint_agent_matched(monkeypatch):
    # Documented headless launch `python -m taskpaw_v3.agent` (deployment.md): process
    # name is `python`, role is implicit in the module (Codex 外门).
    port = _free_port()
    log = _install(
        monkeypatch, port, 91, "python3", ["python3", "-m", "taskpaw_v3.agent"]
    )
    assert net.reclaim_port_from_stale_instance(
        "127.0.0.1", port, role="agent", what="agent API"
    )
    assert ("terminate", 91) in log


def test_module_entrypoint_hub_run_matched(monkeypatch):
    # `python -m taskpaw_v3.hub run` — the `run` subcommand doesn't change the role.
    port = _free_port()
    log = _install(
        monkeypatch, port, 92, "python3", ["python3", "-m", "taskpaw_v3.hub", "run"]
    )
    assert net.reclaim_port_from_stale_instance(
        "127.0.0.1", port, role="hub", what="hub API"
    )
    assert ("terminate", 92) in log


def test_module_entrypoint_service_path_form_matched(monkeypatch):
    # `python -m taskpaw_v3.agent.server.service` (2026-06-27 spec) → agent.
    port = _free_port()
    log = _install(
        monkeypatch,
        port,
        93,
        "python3",
        ["python3", "-m", "taskpaw_v3.agent.server.service"],
    )
    assert net._is_our_backend(net.psutil.Process(93), "agent") is True
    assert net.reclaim_port_from_stale_instance(
        "127.0.0.1", port, role="agent", what="agent API"
    )
    assert ("terminate", 93) in log


def test_localhost_matches_numeric_loopback_listener(monkeypatch):
    # A stale backend bound to `localhost` is reported by psutil as 127.0.0.1; a new
    # instance also configured for `localhost` must still reclaim it (Codex 外门).
    port = _free_port()
    log: list = []
    proc = _FakeProc(
        80,
        "taskpaw-backend",
        ["/x/taskpaw-backend", "hub"],
        log,
        conns=[_Conn(port, 80, ip="127.0.0.1")],
    )
    monkeypatch.setattr(net, "psutil", _FakePsutil([proc]))
    assert net._addr_conflicts("localhost", "127.0.0.1") is True
    assert net.reclaim_port_from_stale_instance(
        "localhost", port, role="hub", what="hub API"
    )
    assert ("terminate", 80) in log


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
        conns=[_Conn(port, 7)],
        wait_exc=_FakePsutil.TimeoutExpired,
    )
    monkeypatch.setattr(net, "psutil", _FakePsutil([proc]))
    # Must not raise; the port wasn't freed → returns False (claim_port fails loud).
    assert (
        net.reclaim_port_from_stale_instance(
            "127.0.0.1", port, role="agent", what="agent API"
        )
        is False
    )
    assert ("terminate", 7) in log and ("kill", 7) in log


def test_no_role_arg_counts_as_agent(monkeypatch):
    # A backend launched with no role arg defaults to agent (backend_main); the
    # agent must reclaim it, the hub must NOT (Codex 外门).
    port = _free_port()
    log = _install(monkeypatch, port, 88, "taskpaw-backend", ["/x/taskpaw-backend"])
    assert net._is_our_backend(net.psutil.Process(88), "agent") is True
    assert net._is_our_backend(net.psutil.Process(88), "hub") is False
    assert net.reclaim_port_from_stale_instance(
        "127.0.0.1", port, role="agent", what="agent API"
    )
    assert ("terminate", 88) in log


def test_multiport_reclaims_when_both_ours(monkeypatch):
    # Both agent ports held by our own stale backend (one pid on both) → reclaim both.
    p1, p2 = _free_port(), _free_port()
    log: list = []
    proc = _FakeProc(
        300,
        "taskpaw-backend",
        ["/x/taskpaw-backend", "agent"],
        log,
        conns=[_Conn(p1, 300), _Conn(p2, 300)],
    )
    monkeypatch.setattr(net, "psutil", _FakePsutil([proc]))
    _fake_ports_free(monkeypatch)  # both bindable after the kill
    assert net.reclaim_ports_from_stale_instance(
        [
            ("127.0.0.1", p1, "agent network API"),
            ("127.0.0.1", p2, "agent control API"),
        ],
        role="agent",
    )
    assert ("terminate", 300) in log


def test_multiport_foreign_on_one_port_reclaims_nothing(monkeypatch):
    # Old agent on the control port (p2), a FOREIGN service on the network port (p1).
    # We can't see the foreign socket without root, but the port is unbindable, so it's
    # classified foreign → we must NOT kill the old agent (Codex 外门). Reclaim nothing.
    p1, p2 = _free_port(), _free_port()
    log: list = []
    ours = _FakeProc(
        301,
        "taskpaw-backend",
        ["/x/taskpaw-backend", "agent"],
        log,
        conns=[_Conn(p2, 301)],
    )
    monkeypatch.setattr(net, "psutil", _FakePsutil([ours]))
    _fake_ports_free(monkeypatch, occupied=[p1])  # p1 held by a foreign process
    assert not net.reclaim_ports_from_stale_instance(
        [
            ("127.0.0.1", p1, "agent network API"),
            ("127.0.0.1", p2, "agent control API"),
        ],
        role="agent",
    )
    assert log == []  # the old agent was left running, nothing terminated


def test_addr_conflicts_predicate():
    # Wildcard on either side, or same address → conflict; different addr / family → not.
    assert net._addr_conflicts("192.168.1.5", "0.0.0.0") is True  # wildcard listener
    assert net._addr_conflicts("0.0.0.0", "127.0.0.1") is True  # wildcard bind
    assert net._addr_conflicts("192.168.1.5", "192.168.1.5") is True
    assert net._addr_conflicts("192.168.1.5", "127.0.0.1") is False  # different addr
    assert net._addr_conflicts("127.0.0.1", "::1") is False  # different family


def test_find_stale_backends_filters_by_bind_address(monkeypatch):
    # Our backend LISTENs on 127.0.0.1; a new instance wanting 192.168.1.5 must not
    # consider it a conflict, but one wanting 127.0.0.1 must (Codex 外门).
    port = _free_port()
    log: list = []
    proc = _FakeProc(
        410,
        "taskpaw-backend",
        ["/x/taskpaw-backend", "agent"],
        log,
        conns=[_Conn(port, 410, ip="127.0.0.1")],
    )
    monkeypatch.setattr(net, "psutil", _FakePsutil([proc]))
    assert list(net._find_stale_backends("192.168.1.5", port, "agent")) == []
    assert [p.pid for p in net._find_stale_backends("127.0.0.1", port, "agent")] == [
        410
    ]


def test_no_psutil_is_noop(monkeypatch):
    monkeypatch.setattr(net, "psutil", None)
    assert not net.reclaim_port_from_stale_instance(
        "127.0.0.1", 5680, role="agent", what="agent API"
    )
    assert not net.reclaim_ports_from_stale_instance(
        [("127.0.0.1", 5680, "agent API")], role="agent"
    )


def test_is_our_backend_matches_name_and_role(monkeypatch):
    monkeypatch.setattr(net, "psutil", _FakePsutil([]))
    log: list = []
    ours = _FakeProc(1, "taskpaw-backend", ["/x/taskpaw-backend", "agent"], log)
    foreign = _FakeProc(2, "node", ["node", "server.js", "agent"], log)
    assert net._is_our_backend(ours, "agent") is True
    assert net._is_our_backend(ours, "hub") is False
    assert net._is_our_backend(foreign, "agent") is False
