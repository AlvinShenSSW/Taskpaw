"""core/net.py URL + readiness-handshake helpers (#115).

claim_port / port_available / ensure_port_free are already covered in test_agent.py;
this fills the gaps: loopback_url normalization/bracketing and announce_ready's
stdout contract (the line the Tauri shell parses, #48)."""

from __future__ import annotations

import errno
import json
import os
import socket

import pytest

from taskpaw_v3.core.net import announce_ready, claim_port, loopback_url, port_available


@pytest.mark.skipif(os.name == "nt", reason="POSIX TIME_WAIT reuse")
def test_immediate_restart_after_server_closes_connection():
    # Closing the listening socket alone does NOT reproduce this bug. The server
    # must actively close an accepted connection, leaving its port in TIME_WAIT.
    server = claim_port("127.0.0.1", 0, "test API")
    address = server.getsockname()
    try:
        with socket.create_connection(address, timeout=2) as client:
            connection, _ = server.accept()
            connection.close()
            assert client.recv(1) == b""
    finally:
        server.close()
    assert port_available(*address)
    with claim_port(*address, "restarted API") as restarted:
        assert restarted.getsockname() == address


@pytest.mark.parametrize("reuse", [False, True])
def test_live_listener_still_blocks_probe_and_claim(reuse):
    from taskpaw_v3.core.net import PortInUseError

    with socket.socket() as foreign:
        if reuse:
            foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        foreign.bind(("127.0.0.1", 0))
        foreign.listen()
        address = foreign.getsockname()
        assert not port_available(*address)
        with pytest.raises(PortInUseError):
            claim_port(*address, "test API")
        # A failed probe/claim must leave the existing service reachable.
        with socket.create_connection(address, timeout=2):
            connection, _ = foreign.accept()
            connection.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX wildcard listener protection")
@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize(
    ("family", "wildcard", "specific"),
    [(socket.AF_INET, "0.0.0.0", "127.0.0.1"), (socket.AF_INET6, "::", "::1")],
)
def test_foreign_wildcard_listener_blocks_specific_claim(
    family, wildcard, specific, reuse
):
    from taskpaw_v3.core.net import PortInUseError

    if family == socket.AF_INET6:
        try:
            with socket.socket(family, socket.SOCK_STREAM) as ipv6:
                ipv6.bind((specific, 0))
        except OSError as exc:
            if exc.errno in (
                errno.EAFNOSUPPORT,
                errno.EPROTONOSUPPORT,
                errno.EADDRNOTAVAIL,
            ):
                pytest.skip("IPv6 loopback unavailable")
            raise
    with socket.socket(family, socket.SOCK_STREAM) as foreign:
        if reuse:
            foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        foreign.bind((wildcard, 0))
        foreign.listen(8)
        foreign.settimeout(2)
        address = (specific, foreign.getsockname()[1])
        assert not port_available(*address)
        with pytest.raises(PortInUseError):
            claim_port(*address, "test API")
        # Both bounded probes connected but sent nothing, then closed. Drain them
        # before checking that a subsequent client really reaches the foreign API.
        for _ in range(2):
            connection, _ = foreign.accept()
            with connection:
                connection.settimeout(2)
                assert connection.recv(1) == b""
        with socket.create_connection(address, timeout=2) as client:
            client.sendall(b"x")
            connection, peer = foreign.accept()
            with connection:
                connection.settimeout(2)
                assert peer[1] == client.getsockname()[1]
                assert connection.recv(1) == b"x"


def test_exclusive_claim_does_not_connect(monkeypatch):
    class NoConnectSocket(socket.socket):
        def connect(self, address):
            pytest.fail("an exclusive claim must not probe")

    monkeypatch.setattr(socket, "socket", NoConnectSocket)
    with claim_port("127.0.0.1", 0, "test API") as claimed:
        assert claimed.getsockname()[1] > 0
        if os.name == "posix":
            assert claimed.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0


@pytest.mark.skipif(os.name == "nt", reason="POSIX guarded reuse")
@pytest.mark.parametrize("probe_errno", [None, errno.EACCES, errno.EHOSTUNREACH])
def test_uncertain_connect_probe_fails_closed(monkeypatch, probe_errno):
    from taskpaw_v3.core.net import PortInUseError

    opened = []
    attempts = []

    class UncertainSocket(socket.socket):
        def __init__(self, *args):
            super().__init__(*args)
            opened.append(self)

        def connect(self, address):
            attempts.append(address)
            assert self.gettimeout() == 1.0
            if probe_errno is None:
                raise TimeoutError("simulated connect timeout")
            raise OSError(probe_errno, os.strerror(probe_errno))

        def setsockopt(self, *args):
            pytest.fail("an uncertain probe must not authorize reuse")

    with socket.socket() as foreign:
        foreign.bind(("127.0.0.1", 0))
        foreign.listen()
        address = foreign.getsockname()
        monkeypatch.setattr(socket, "socket", UncertainSocket)
        with pytest.raises(PortInUseError) as failure:
            claim_port(*address, "test API")
        assert failure.value.__cause__.errno == errno.EADDRINUSE
        assert not port_available(*address)
    assert attempts == [address, address]
    assert len(opened) == 4  # exclusive socket + probe per claim, never a retry
    assert all(sock.fileno() == -1 for sock in opened)


@pytest.mark.parametrize("multiple", [False, True])
def test_reclaim_slow_probe_does_not_sleep_past_deadline(monkeypatch, multiple):
    from types import SimpleNamespace

    from taskpaw_v3.core import net

    now = [0.0]
    polls = []
    sleeps = []

    def unavailable(host, port):
        polls.append((host, port))
        now[0] += 1.0  # one slow connect probe
        return False

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(net, "psutil", SimpleNamespace())
    monkeypatch.setattr(
        net, "_find_stale_backends", lambda *a: [SimpleNamespace(pid=123)]
    )
    monkeypatch.setattr(net, "_terminate_backend", lambda *a: True)
    monkeypatch.setattr(net, "port_available", unavailable)
    monkeypatch.setattr(net.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(net.time, "sleep", sleep)
    if multiple:
        assert net.reclaim_ports_from_stale_instance(
            [("127.0.0.1", 1234, "read"), ("127.0.0.1", 1235, "control")],
            role="agent",
            wait=0.1,
        )
    else:
        assert net.reclaim_port_from_stale_instance(
            "127.0.0.1", 1234, role="agent", what="read", wait=0.1
        )
    assert polls == [("127.0.0.1", 1234)]
    assert now[0] <= 0.1 + 1.0
    assert not any(sleeps)


@pytest.mark.parametrize(
    ("stage", "expected_errno", "socket_count"),
    [
        ("windows", errno.EADDRINUSE, 1),
        ("initial_bind", errno.EADDRNOTAVAIL, 1),
        ("probe_create", errno.EADDRINUSE, 1),
        ("probe_setup", errno.EADDRINUSE, 2),
        ("probe_success", errno.EADDRINUSE, 2),
        ("probe_timeout", errno.EADDRINUSE, 2),
        ("retry_create", errno.EMFILE, 2),
        ("retry_options", errno.EACCES, 3),
        ("retry_bind", errno.EADDRNOTAVAIL, 3),
        ("retry_listen", errno.EADDRINUSE, 3),
        ("refused", None, 3),
    ],
)
def test_guarded_reuse_error_paths_without_network(
    monkeypatch, stage, expected_errno, socket_count
):
    from types import SimpleNamespace

    from taskpaw_v3.core import net

    opened = []
    connects = []
    address = ("::1", 5680)

    class FakeSocket:
        def __init__(self, family, kind):
            assert (family, kind) == (socket.AF_INET6, socket.SOCK_STREAM)
            self.index = len(opened)
            if stage == "probe_create" and self.index == 1:
                raise OSError(errno.ECONNREFUSED, "probe allocation failed")
            if stage == "retry_create" and self.index == 2:
                raise OSError(errno.EMFILE, "retry allocation failed")
            self.closed = False
            opened.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def close(self):
            self.closed = True

        def bind(self, target):
            assert target == address
            if self.index == 0:
                error = (
                    errno.EADDRNOTAVAIL if stage == "initial_bind" else errno.EADDRINUSE
                )
                raise OSError(error, "exclusive bind failed")
            assert self.index == 2
            if stage == "retry_bind":
                raise OSError(errno.EADDRNOTAVAIL, "retry bind failed")

        def listen(self, backlog):
            assert backlog == 128
            if stage == "retry_listen":
                raise OSError(errno.EADDRINUSE, "retry listen failed")

        def settimeout(self, timeout):
            assert self.index == 1 and timeout == 1.0
            if stage == "probe_setup":
                raise OSError(errno.ECONNREFUSED, "probe setup failed")

        def connect(self, target):
            assert target == address
            connects.append(target)
            if stage == "probe_timeout":
                raise TimeoutError("probe timed out")
            if stage != "probe_success":
                raise OSError(errno.ECONNREFUSED, "no listener")

        def setsockopt(self, *args):
            assert self.index == 2 and opened[0].closed and opened[1].closed
            assert args == (socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if stage == "retry_options":
                raise OSError(errno.EACCES, "retry options failed")

    monkeypatch.setattr(
        net, "os", SimpleNamespace(name="nt" if stage == "windows" else "posix")
    )
    monkeypatch.setattr(net.socket, "socket", FakeSocket)
    if expected_errno is None:
        with claim_port(*address, "test API") as claimed:
            assert claimed is opened[-1] and not claimed.closed
    else:
        error_type = (
            net.PortInUseError
            if expected_errno == errno.EADDRINUSE
            else net.PortBindError
        )
        with pytest.raises(error_type) as failure:
            claim_port(*address, "test API")
        assert failure.value.__cause__.errno == expected_errno
    assert len(opened) == socket_count
    assert all(sock.closed for sock in opened)
    assert len(connects) == (0 if socket_count == 1 or stage == "probe_setup" else 1)


@pytest.mark.parametrize("error", [errno.EADDRNOTAVAIL, errno.EACCES])
def test_bind_errors_preserve_cause_and_close_socket(monkeypatch, error):
    from taskpaw_v3.core import net

    real_socket = socket.socket
    opened = []

    class FailedSocket(real_socket):
        def bind(self, address):
            raise OSError(error, os.strerror(error))

    def make_socket(*args):
        sock = FailedSocket(*args)
        opened.append(sock)
        return sock

    monkeypatch.setattr(net.socket, "socket", make_socket)
    with pytest.raises(RuntimeError) as failure:
        claim_port("192.168.1.184", 5678, "agent network API")
    assert "already in use" not in str(failure.value)
    assert "192.168.1.184:5678" in str(failure.value)
    assert failure.value.__cause__.errno == error
    assert opened[0].fileno() == -1


def test_loopback_url_ipv4_and_plain_host():
    assert loopback_url("127.0.0.1", 5681) == "http://127.0.0.1:5681"
    assert loopback_url("localhost", 5690) == "http://localhost:5690"


def test_loopback_url_wildcard_maps_to_loopback():
    # A wildcard bind is reachable locally via its loopback (the UI is always local).
    assert loopback_url("0.0.0.0", 5680) == "http://127.0.0.1:5680"
    assert loopback_url("", 5680) == "http://127.0.0.1:5680"
    assert loopback_url("::", 5690) == "http://[::1]:5690"
    assert loopback_url("[::]", 5690) == "http://[::1]:5690"


def test_loopback_url_brackets_ipv6_literal():
    # IPv6 literals must be bracketed so the URL is valid (not http://::1:port).
    assert loopback_url("::1", 5681) == "http://[::1]:5681"
    assert loopback_url("fe80::1", 7000) == "http://[fe80::1]:7000"


def test_announce_ready_emits_single_json_handshake_line(capsys):
    announce_ready("agent", "http://127.0.0.1:5681")
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1  # exactly one line on stdout
    obj = json.loads(out[0])
    assert obj == {
        "taskpaw_ready": True,
        "role": "agent",
        "base_url": "http://127.0.0.1:5681",
    }


def test_announce_ready_role_passthrough(capsys):
    announce_ready("hub", "http://[::1]:5690")
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["role"] == "hub" and obj["base_url"] == "http://[::1]:5690"


def test_ready_descriptor_metadata_is_optional_and_contains_no_credential(capsys):
    announce_ready(
        "hub",
        "http://[::1]:5691",
        control_credential_file="config/hub.control.json",
        boot_id="b" * 32,
    )
    ready = json.loads(capsys.readouterr().out)
    assert ready == {
        "taskpaw_ready": True,
        "role": "hub",
        "base_url": "http://[::1]:5691",
        "control_credential_file": "config/hub.control.json",
        "boot_id": "b" * 32,
    }
    assert "control_token" not in ready
