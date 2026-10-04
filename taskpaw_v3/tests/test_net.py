"""core/net.py URL + readiness-handshake helpers (#115).

claim_port / port_available / ensure_port_free are already covered in test_agent.py;
this fills the gaps: loopback_url normalization/bracketing and announce_ready's
stdout contract (the line the Tauri shell parses, #48)."""

from __future__ import annotations

import json
import socket
import sys
import time

import pytest

from taskpaw_v3.core.net import (
    PortInUseError,
    announce_ready,
    claim_port,
    loopback_url,
    port_available,
)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS exclusive TIME_WAIT retry")
@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_immediate_restart_after_server_active_close(host):
    if host == "::1" and not socket.has_ipv6:
        pytest.skip("IPv6 unavailable")
    server = claim_port(host, 0, "owned restart fixture")
    address = server.getsockname()[:2]
    try:
        with socket.create_connection(address, timeout=2) as client:
            connection, _ = server.accept()
            connection.close()
            assert client.recv(1) == b""  # Server FIN precedes client close.
    finally:
        server.close()
    assert not port_available(*address)
    with claim_port(
        *address, "restarted fixture", deadline=time.monotonic() + 45
    ) as restarted:
        assert restarted.getsockname()[:2] == address


@pytest.mark.parametrize("reuse", [False, True])
@pytest.mark.parametrize("host", ["127.0.0.1", "0.0.0.0"])
def test_real_listener_survives_probe_and_claim_refusal(host, reuse):
    with socket.socket() as foreign:
        if reuse:
            foreign.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        foreign.bind((host, 0))
        foreign.listen()
        # Winsock permits same-user wildcard-to-specific binding; exercise a
        # same-address conflict there, and the BSD wildcard hazard on POSIX.
        probe_host = host if sys.platform == "win32" else "127.0.0.1"
        address = (probe_host, foreign.getsockname()[1])
        assert not port_available(*address)
        with pytest.raises(PortInUseError):
            claim_port(*address, "owned refusal fixture", deadline=time.monotonic() + 2)
        with socket.create_connection(("127.0.0.1", address[1]), timeout=2):
            connection, _ = foreign.accept()
            connection.close()


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


@pytest.mark.parametrize("mode", ["expires", "success", "unknown", "address_invalid"])
def test_port_retry_is_bounded_and_closes_failed_sockets(monkeypatch, mode):
    import errno

    from taskpaw_v3.core import net

    clock = [0.0]
    sockets = []
    attempts = []

    class Socket:
        def __init__(self, *args):
            self.closed = False
            sockets.append(self)

        def bind(self, address):
            attempts.append(address)
            if mode == "success" and clock[0] >= 0.2:
                return
            code = (
                errno.EADDRNOTAVAIL if mode == "address_invalid" else errno.EADDRINUSE
            )
            raise OSError(code, "owned fixture")

        def listen(self, backlog):
            pass

        def settimeout(self, timeout):
            assert 0 < timeout <= 0.2

        def connect_ex(self, address):
            return errno.EACCES if mode == "unknown" else errno.ECONNREFUSED

        def close(self):
            self.closed = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

    monkeypatch.setattr(net.socket, "socket", Socket)
    monkeypatch.setattr(net.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(
        net.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay)
    )
    if mode == "success":
        with net.claim_port("127.0.0.1", 9999, "fixture", deadline=0.3):
            assert clock[0] == 0.2 and len(attempts) == 2
    else:
        with pytest.raises(PortInUseError):
            net.claim_port("127.0.0.1", 9999, "fixture", deadline=0.3)
        assert clock[0] == (0.3 if mode == "expires" else 0)
        assert len(attempts) == (3 if mode == "expires" else 1)
    assert all(sock.closed for sock in sockets)
