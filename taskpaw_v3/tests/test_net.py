"""core/net.py URL + readiness-handshake helpers (#115).

claim_port / port_available / ensure_port_free are already covered in test_agent.py;
this fills the gaps: loopback_url normalization/bracketing and announce_ready's
stdout contract (the line the Tauri shell parses, #48)."""

from __future__ import annotations

import json

from taskpaw_v3.core.net import announce_ready, loopback_url


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
    assert len(out) == 1                      # exactly one line on stdout
    obj = json.loads(out[0])
    assert obj == {"taskpaw_ready": True, "role": "agent",
                   "base_url": "http://127.0.0.1:5681"}


def test_announce_ready_role_passthrough(capsys):
    announce_ready("hub", "http://[::1]:5690")
    obj = json.loads(capsys.readouterr().out.strip())
    assert obj["role"] == "hub" and obj["base_url"] == "http://[::1]:5690"
