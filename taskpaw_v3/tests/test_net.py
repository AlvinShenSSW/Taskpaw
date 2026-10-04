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
