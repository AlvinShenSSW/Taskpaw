"""#299 R4–R6, R9, R16: 网络与访问 — the LAN switch and address, the bind
probe, the address self-heal, the token, the Windows firewall helper (every
ShellExecute and netsh call is FAKED) and the pairing messages. Every file is a
tmp yaml; no test reads this host's NICs or firewall."""

from __future__ import annotations

import logging
import re
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from taskpaw_v3.agent import firewall
from taskpaw_v3.agent.server import network_routes
from taskpaw_v3.agent.server.admin import MonitorAdmin
from taskpaw_v3.agent.server.app import create_control_app
from taskpaw_v3.core import lan
from taskpaw_v3.core.config import AgentConfig, load_yaml
from taskpaw_v3.monitors.registry import PluginRegistry
from taskpaw_v3.packaging import backend_main

TOKEN = "control-token"
SAVED_TOKEN = "tp_SavedTokenSavedTokenSaved1234"
NICS = {
    "以太网": ["192.168.1.20", "fe80::1"],
    "Loopback": ["127.0.0.1", "::1"],
    "vEthernet (WSL)": ["169.254.3.4", "172.20.0.1"],
    "VirtualBox Host-Only Network": ["192.168.56.1"],
    "Tailscale": ["100.101.1.2"],
    "Wi-Fi 2": ["10.0.0.7"],
    "WAN": ["8.8.8.8"],
}


@pytest.fixture(autouse=True)
def fake_host(monkeypatch):
    """This host's NICs, routing and firewall never decide a test."""
    monkeypatch.setattr(lan, "_interfaces", lambda: NICS)
    monkeypatch.setattr(lan, "_route_source", lambda: "192.168.1.20")
    monkeypatch.setattr(
        firewall,
        "status",
        lambda ports, **_: {
            "platform": "windows",
            "rule": "absent",
            "profile": "private",
        },
    )

    def never(*a, **k):
        raise AssertionError("no elevation in tests")

    monkeypatch.setattr(firewall, "_shell_execute_runas", never)
    monkeypatch.setattr(firewall, "_run_netsh", never)
    monkeypatch.setattr(firewall, "_run_powershell", never)


class World:
    def __init__(self, tmp_path: Path, probe=None, changed_from=None, **extra):
        self.path = tmp_path / "agent.yaml"
        data = {"server_id": "s", "machine": "m", "host_metrics": False, **extra}
        self.path.write_text(yaml.safe_dump(data), encoding="utf-8")
        self.config = load_yaml(AgentConfig, self.path)
        self.probes: list[tuple[str, int]] = []

        def recorded(host, port):
            self.probes.append((host, port))
            return probe(host, port) if probe else None

        self.admin = MonitorAdmin(
            self.config, None, PluginRegistry(), self.path, bind_probe=recorded
        )
        app = create_control_app(
            self.config,
            admin=self.admin,
            control_token=TOKEN,
            control_active=lambda: True,
            address_changed_from=changed_from,
        )
        self.client = TestClient(app, base_url="http://127.0.0.1")
        self.h = {"Authorization": f"Bearer {TOKEN}"}

    def patch(self, body):
        return self.client.patch("/control/config", json=body, headers=self.h)

    def network(self):
        return self.client.get("/control/network", headers=self.h).json()

    def saved(self) -> dict:
        return yaml.safe_load(self.path.read_text(encoding="utf-8"))


# ── addresses (R4) ──────────────────────────────────────────────────────
def test_only_private_ipv4_addresses_are_listed_primary_first():
    found = lan.local_addresses()
    assert [a.view() for a in found] == [
        {"ip": "192.168.1.20", "nic": "以太网", "primary": True},
        {"ip": "10.0.0.7", "nic": "Wi-Fi 2", "primary": False},
    ]
    assert [a.routed for a in found] == [True, False]
    other = lan.local_addresses(route_source=lambda: None)
    assert other[0].primary and other[0].ip == "192.168.1.20"
    assert not any(a.routed for a in other)  # no default route: nothing routed
    assert lan.local_addresses(interfaces=lambda: {}) == []

    def broken():
        raise OSError("psutil")

    assert lan.local_addresses(interfaces=broken) == []


def test_is_local_knows_loopback_and_interfaces_and_says_unknown():
    assert lan.is_local("127.0.0.1") and lan.is_local("192.168.1.20")
    assert lan.is_local("192.168.1.99") is False

    def broken():
        raise OSError

    assert lan.is_local("192.168.1.99", interfaces=broken) is None


# ── self-heal (R5) ──────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "lan_on,host,expected",
    [
        (False, "192.168.1.99", None),  # LAN off: never moved
        (True, "127.0.0.1", None),
        (True, "0.0.0.0", None),  # wildcard: the guard refuses it
        (True, "8.8.4.4", None),  # public: the guard refuses it
        (True, "192.168.1.20", None),  # still ours
        (True, "192.168.1.99", "192.168.1.20"),  # DHCP moved us
    ],
)
def test_healed_bind_host(lan_on, host, expected):
    assert lan.healed_bind_host(lan_on, host) == expected


@pytest.mark.parametrize(
    "nic,ip",
    [
        ("vEthernet (WSL)", "172.20.0.1"),
        ("vEthernet (Default Switch)", "172.28.16.1"),
        ("Hyper-V Virtual Ethernet Adapter", "172.16.5.1"),
        ("VirtualBox Host-Only Network", "192.168.56.1"),
        ("VMware Network Adapter VMnet8", "192.168.200.1"),
        ("Tailscale", "100.101.1.2"),
        ("ZeroTier One [8056c2e21c]", "10.147.17.5"),
        ("以太网 3", "100.64.0.9"),  # the shared range, whatever the name
    ],
)
def test_virtual_and_vpn_adapters_are_never_lan_addresses(nic, ip):
    """#299 review IR1: never listed, never routed, never a heal target."""
    table = {nic: [ip]}
    assert lan.local_addresses(interfaces=lambda: table, route_source=lambda: ip) == []
    assert (
        lan.healed_bind_host(
            True,
            "192.168.1.99",
            addresses=lambda: lan.local_addresses(lambda: table, lambda: ip),
            local=lambda h: False,
        )
        is None
    )


@pytest.mark.parametrize("source", [None, "192.168.56.1", "10.0.0.7"])
def test_a_heal_needs_the_default_routes_lan_address(monkeypatch, source):
    """#299 review IR1: before Wi-Fi/DHCP is up (no default route, or the
    route goes out a virtual adapter) nothing moves — not even to another
    real address that is not the routed one."""
    monkeypatch.setattr(lan, "_route_source", lambda: source)
    # CX11: a routed address elsewhere yields to the saved address's subnet
    expected = "192.168.1.20" if source == "10.0.0.7" else None
    assert lan.healed_bind_host(True, "192.168.1.99") == expected


def test_no_routed_lan_address_keeps_the_saved_bind_unsaved(
    tmp_path, monkeypatch, caplog
):
    """#299 review IR1: kept for this run, logged once, never written."""
    monkeypatch.setattr(lan, "_route_source", lambda: None)
    path = tmp_path / "agent.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "server_id": "s",
                "machine": "m",
                "bind_host": "192.168.1.99",
                "api_token": SAVED_TOKEN,
            }
        ),
        encoding="utf-8",
    )
    before = path.read_bytes()
    config = load_yaml(AgentConfig, path)
    with caplog.at_level(logging.WARNING, logger="taskpaw.agent.network"):
        assert network_routes.heal_on_startup(config, path) is None
    assert config.bind_host == "192.168.1.99"
    assert path.read_bytes() == before
    assert not (tmp_path / "agent.yaml.bak").exists()
    warned = [r for r in caplog.records if "kept, not saved" in r.getMessage()]
    assert len(warned) == 1 and SAVED_TOKEN not in caplog.text


@pytest.mark.parametrize("wg_first", [True, False])
def test_a_heal_prefers_the_saved_addresss_subnet_over_a_vpn_route(wg_first):
    """#299 Codex r5 CX11: a full-tunnel VPN (WireGuard 10.8.0.2) can be the
    default route and its name misses the blacklist; the heal stays on the
    home subnet of the address that went away."""
    table = {"WireGuard": ["10.8.0.2"], "Ethernet": ["192.168.1.35"]}
    if not wg_first:
        table = dict(reversed(list(table.items())))
    found = lan.local_addresses(lambda: table, lambda: "10.8.0.2")
    assert [a.ip for a in found if a.routed] == ["10.8.0.2"]
    assert (
        lan.healed_bind_host(
            True, "192.168.1.20", addresses=lambda: found, local=lambda h: False
        )
        == "192.168.1.35"
    )
    # no home-subnet candidate: the existing routed choice stands
    only_vpn = lan.local_addresses(
        lambda: {"WireGuard": ["10.8.0.2"]}, lambda: "10.8.0.2"
    )
    assert (
        lan.healed_bind_host(
            True, "192.168.1.20", addresses=lambda: only_vpn, local=lambda h: False
        )
        == "10.8.0.2"
    )


def test_an_unknown_interface_table_never_heals():
    assert lan.healed_bind_host(True, "192.168.1.99", local=lambda h: None) is None
    assert lan.healed_bind_host(True, "192.168.1.99", addresses=lambda: []) is None


def test_heal_on_startup_saves_with_a_backup_and_the_notice_shows(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "server_id": "s",
                "machine": "m",
                "bind_host": "192.168.1.99",
                "api_token": SAVED_TOKEN,
                "future_key": 1,
            }
        ),
        encoding="utf-8",
    )
    before = path.read_bytes()
    config = load_yaml(AgentConfig, path)
    assert config.lan_access  # derived from a pre-#299 LAN bind
    old = network_routes.heal_on_startup(config, path)
    assert old == "192.168.1.99" and config.bind_host == "192.168.1.20"
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["bind_host"] == "192.168.1.20" and saved["future_key"] == 1
    assert (tmp_path / "agent.yaml.bak").read_bytes() == before
    assert network_routes.heal_on_startup(config, None) is None
    (tmp_path / "w").mkdir()
    world = World(
        tmp_path / "w",
        changed_from=old,
        bind_host=config.bind_host,
        api_token=SAVED_TOKEN,
    )
    view = world.network()
    assert view["address_changed_from"] == "192.168.1.99"
    assert view["bind_host"] == "192.168.1.20" and view["lan_access"] is True


# ── the LAN switch, the address and the bind probe (R4, R16) ────────────
def test_lan_on_saves_the_address_and_token_and_needs_a_restart(tmp_path):
    world = World(tmp_path)
    res = world.patch(
        {"lan_access": True, "bind_host": "192.168.1.20", "api_token": SAVED_TOKEN}
    )
    assert res.status_code == 200 and res.json()["restart_required"] is True
    saved = world.saved()
    assert saved["lan_access"] is True and saved["bind_host"] == "192.168.1.20"
    # R16: a network save keeps the previous file as agent.yaml.bak
    assert (tmp_path / "agent.yaml.bak").is_file()
    assert world.probes == [("192.168.1.20", 5680)]
    # the masked round trip keeps the token
    view = world.client.get("/control/config", headers=world.h).json()
    assert view["api_token"] == "***" and view["lan_access"] is True
    echo = {k: view[k] for k in ("lan_access", "bind_host", "bind_port", "api_token")}
    assert world.patch(echo).status_code == 200
    assert world.saved()["api_token"] == SAVED_TOKEN


def test_lan_off_means_loopback(tmp_path):
    world = World(tmp_path, bind_host="192.168.1.20", api_token=SAVED_TOKEN)
    assert (
        world.patch({"lan_access": False, "bind_host": "192.168.1.20"}).status_code
        == 200
    )
    assert world.saved()["bind_host"] == "127.0.0.1"
    assert world.saved()["lan_access"] is False


@pytest.mark.parametrize(
    "body,fields",
    [
        (
            {"lan_access": True, "bind_host": "127.0.0.1", "api_token": SAVED_TOKEN},
            {"bind_host": "请选择本机地址"},
        ),
        (
            {"lan_access": True, "bind_host": "192.168.1.20"},
            {"api_token": "开启局域网访问需要访问令牌"},
        ),
        (
            {"lan_access": True, "bind_host": "0.0.0.0", "api_token": SAVED_TOKEN},
            {"bind_host": "不能监听所有网卡，请选择一个本机地址"},
        ),
        (
            {"lan_access": True, "bind_host": "8.8.8.8", "api_token": SAVED_TOKEN},
            {"bind_host": "不能使用公网地址"},
        ),
        ({"lan_access": "yes"}, {"lan_access": "格式不正确"}),
    ],
)
def test_network_refusals_are_per_field_and_write_nothing(tmp_path, body, fields):
    world = World(tmp_path)
    before = world.path.read_bytes()
    res = world.patch(body)
    assert res.status_code == 422 and res.json()["fields"] == fields
    assert world.path.read_bytes() == before
    assert SAVED_TOKEN not in res.text


def test_a_changed_address_or_port_is_test_bound_first(tmp_path):
    def probe(host, port):
        if host == "192.168.1.77":
            return lan.NOT_LOCAL
        if port == 5999:
            return lan.IN_USE
        return None

    world = World(tmp_path, probe=probe)
    before = world.path.read_bytes()
    res = world.patch(
        {"lan_access": True, "bind_host": "192.168.1.77", "api_token": SAVED_TOKEN}
    )
    assert res.json()["fields"] == {"bind_host": "此地址不属于本机"}
    res = world.patch({"bind_port": 5999})
    assert res.json()["fields"] == {"bind_port": "端口已被占用"}
    res = world.patch({"control_port": 5999})
    assert res.json()["fields"] == {"control_port": "端口已被占用"}
    assert world.path.read_bytes() == before
    # nothing changed → nothing probed; a non-network save never probes
    world.probes.clear()
    assert world.patch({"bind_port": 5680, "machine": "x"}).status_code == 200
    assert world.patch({"llm_model": "m2"}).status_code == 200
    assert world.probes == []


def test_the_real_probe_names_a_foreign_address_and_a_taken_port():
    import socket

    assert lan.probe_bind("127.0.0.1", 0) is None
    with socket.socket() as held:
        held.bind(("127.0.0.1", 0))
        held.listen(1)
        assert lan.probe_bind("127.0.0.1", held.getsockname()[1]) == lan.IN_USE
    assert lan.probe_bind("192.0.2.254", 0) == lan.NOT_LOCAL  # TEST-NET, never ours


def test_gpu_workers_keep_lan_access_on(tmp_path):
    world = World(
        tmp_path,
        bind_host="192.168.1.20",
        api_token=SAVED_TOKEN,
        workflow={"enabled": True, "role": "controller", "remote_workers": True},
    )
    res = world.patch({"lan_access": False})
    assert res.status_code == 422
    assert res.json()["fields"] == {"lan_access": "GPU 工作机接入需要局域网访问"}


def test_gpu_worker_access_or_role_needs_lan_first(tmp_path, monkeypatch):
    from taskpaw_v3.agent.server import workflow_settings as ws

    monkeypatch.setattr(ws, "detect_ffprobe", lambda: None)
    inbox = tmp_path / "INBOX"
    inbox.mkdir()
    world = World(tmp_path)
    put = world.client.put(
        "/control/workflow/settings",
        json={"role": "controller", "inbox_root": str(inbox), "remote_workers": True},
        headers=world.h,
    )
    assert put.status_code == 422
    assert put.json()["fields"]["remote_workers"] == ws.NEEDS_LAN
    put = world.client.put(
        "/control/workflow/settings",
        json={"role": "worker", "staging_dir": str(tmp_path / "staging")},
        headers=world.h,
    )
    assert put.status_code == 422 and put.json()["fields"]["role"] == ws.NEEDS_LAN


def test_a_concurrent_config_save_answers_409(tmp_path):
    world = World(tmp_path)
    world.admin._mutation.acquire()
    try:
        res = world.patch({"machine": "x"})
    finally:
        world.admin._mutation.release()
    assert res.status_code == 409 and res.json()["code"] == "operation_busy"


# ── the token (R4, R16) ─────────────────────────────────────────────────
def test_generate_returns_a_fresh_unsaved_token_never_cached_or_logged(
    tmp_path, caplog
):
    caplog.set_level(logging.DEBUG)
    world = World(tmp_path)
    before = world.path.read_bytes()
    res = world.client.post(
        "/control/network/token", json={"action": "generate"}, headers=world.h
    )
    token = res.json()["token"]
    assert re.fullmatch(r"tp_[0-9A-Za-z]{32}", token)
    assert res.headers["cache-control"] == "no-store"
    again = world.client.post(
        "/control/network/token", json={"action": "generate"}, headers=world.h
    ).json()["token"]
    assert again != token
    assert world.path.read_bytes() == before  # the step's save writes it
    assert token not in caplog.text
    assert world.network()["token_set"] is False


def test_reveal_and_the_hint(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    world = World(tmp_path, bind_host="192.168.1.20", api_token=SAVED_TOKEN)
    res = world.client.post(
        "/control/network/token", json={"action": "reveal"}, headers=world.h
    )
    assert res.json() == {"token": SAVED_TOKEN}
    assert res.headers["cache-control"] == "no-store"
    view = world.network()
    assert view["token_set"] and view["api_token_hint"] == "1234"
    assert SAVED_TOKEN not in str(view)
    assert SAVED_TOKEN not in caplog.text
    (tmp_path / "short").mkdir()
    short = World(tmp_path / "short", bind_host="192.168.1.20", api_token="short-token")
    assert short.network()["api_token_hint"] is None
    bad = world.client.post(
        "/control/network/token", json={"action": "x"}, headers=world.h
    )
    assert bad.status_code == 422


def test_the_network_view_lists_addresses_and_firewall(tmp_path):
    view = World(tmp_path).network()
    assert view["addresses"][0] == {
        "ip": "192.168.1.20",
        "nic": "以太网",
        "primary": True,
    }
    assert view["firewall"]["rule"] == "absent"
    assert view["lan_access"] is False and view["bind_port"] == 5680


# ── the firewall helper (R6, §4): ShellExecute and netsh faked ──────────
def test_the_route_accepts_only_its_fixed_argv():
    ran: list[str] = []

    def run(command):
        ran.append(command)
        return 0

    for argv in (
        [],
        ["allow"],
        ["allow", "--ports"],
        ["allow", "--ports", "5680", "x"],
        ["delete", "--ports", "5680"],
        ["allow", "--port", "5680"],
        ["allow", "--ports", "0"],
        ["allow", "--ports", "65536"],
        ["allow", "--ports", "5680,5680"],
        ["allow", "--ports", "5680,5681,5682"],
        ["allow", "--ports", "5680;calc"],
        ["allow", "--ports", "５６８０"],
        ["allow", "--ports", " 5680"],
    ):
        assert firewall.route_main(argv, run=run, frozen=True, platform="win32") == 2
    assert (
        firewall.route_main(
            ["allow", "--ports", "5680"], run=run, frozen=False, platform="win32"
        )
        == 2
    )
    assert (
        firewall.route_main(
            ["allow", "--ports", "5680"], run=run, frozen=True, platform="darwin"
        )
        == 2
    )
    assert ran == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows firewall")
def test_the_route_runs_the_two_fixed_netsh_lines():
    ran: list[str] = []

    def run(command):
        ran.append(command)
        return 1 if "delete" in command else 0  # no old rule: delete fails, fine

    code = firewall.route_main(
        ["allow", "--ports", "5680,5682"],
        run=run,
        frozen=True,
        platform="win32",
        program=r"C:\Program Files\TaskPaw\taskpaw-backend.exe",
        system32=r"C:\Windows\System32",
    )
    assert code == 0
    assert ran == [
        r'"C:\Windows\System32\netsh.exe" advfirewall firewall delete rule name="TaskPaw Agent"',
        r'"C:\Windows\System32\netsh.exe" advfirewall firewall add rule name="TaskPaw Agent" dir=in '
        r"action=allow protocol=TCP localport=5680,5682 profile=private "
        r'program="C:\Program Files\TaskPaw\taskpaw-backend.exe"',
    ]
    assert (
        firewall.route_main(
            ["allow", "--ports", "5680"],
            run=lambda c: 1,
            frozen=True,
            platform="win32",
            program="x",
            system32="y",
        )
        == 1
    )


def test_backend_main_dispatches_the_fixed_route(monkeypatch):
    seen = []
    monkeypatch.setattr(firewall, "route_main", lambda argv: seen.append(argv) or 0)
    assert backend_main.main(["agent-firewall", "allow", "--ports", "5680"]) == 0
    assert seen == [["allow", "--ports", "5680"]]


def test_request_allow_maps_every_outcome():
    calls = []

    def launch(code=None, exc=None):
        def fake(file, params, timeout):
            calls.append((file, params, timeout))
            if exc is not None:
                raise exc
            return code

        return fake

    kw = dict(
        frozen=True, platform="win32", executable=r"C:\TaskPaw\taskpaw-backend.exe"
    )
    assert firewall.request_allow([5682, 5680], launch=launch(0), **kw) == "added"
    assert calls[-1] == (
        r"C:\TaskPaw\taskpaw-backend.exe",
        "agent-firewall allow --ports 5680,5682",
        120.0,
    )
    assert (
        firewall.request_allow([5680], launch=launch(exc=firewall.Declined()), **kw)
        == "declined"
    )
    assert (
        firewall.request_allow([5680], launch=launch(exc=OSError(1223, "x")), **kw)
        == "declined"
    )
    assert (
        firewall.request_allow([5680], launch=launch(exc=OSError(5, "x")), **kw)
        == "failed"
    )
    assert firewall.request_allow([5680], launch=launch(1), **kw) == "failed"
    assert (
        firewall.request_allow([5680], launch=launch(None), **kw) == "failed"
    )  # timeout
    assert (
        firewall.request_allow([5680, 5681, 5682], launch=launch(0), **kw) == "failed"
    )
    n = len(calls)
    assert (
        firewall.request_allow([5680], launch=launch(0), frozen=False, platform="win32")
        == "unsupported"
    )
    assert (
        firewall.request_allow([5680], launch=launch(0), frozen=True, platform="darwin")
        == "unsupported"
    )
    assert len(calls) == n  # nothing launched


def test_request_allow_is_bounded_overall_and_one_at_a_time():
    """#299 review IR3: ShellExecuteExW runs on a worker thread; the request
    answers `timeout` when the whole thing (UAC prompt included) is not done
    in time, and a second request meanwhile answers `pending` (no second
    prompt). Event-driven: the fake launcher waits on a gate."""
    import threading

    gate, entered = threading.Event(), threading.Event()
    calls = []

    def blocked(file, params, timeout):
        calls.append(timeout)
        entered.set()
        gate.wait(10)
        return 0

    kw = dict(frozen=True, platform="win32", executable="taskpaw-backend.exe")
    assert firewall.request_allow([5680], launch=blocked, timeout=0, **kw) == "timeout"
    assert entered.wait(10)
    assert firewall.request_allow([5680], launch=blocked, **kw) == "pending"
    assert calls == [0]  # never a second UAC prompt
    gate.set()
    assert firewall._LAST.wait(10)  # the worker finished and freed the slot
    assert firewall.request_allow([5680], launch=lambda *a: 0, **kw) == "added"


def test_the_uac_prompt_is_owned_by_the_foreground_taskpaw_window():
    """#299 review IR3: the windowless backend hands ShellExecuteExW the
    shell's foreground window, so Windows shows the prompt in front; any
    other app's window (or none) → no owner."""
    family = lambda: {4100, 4000}  # noqa: E731 — the parent shell and its parent
    assert (
        firewall.owner_window(
            foreground=lambda: 0x1234, pid_of=lambda h: 4100, family=family
        )
        == 0x1234
    )
    assert (
        firewall.owner_window(
            foreground=lambda: 0x1234, pid_of=lambda h: 999, family=family
        )
        is None
    )
    assert (
        firewall.owner_window(
            foreground=lambda: 0, pid_of=lambda h: 4100, family=family
        )
        is None
    )

    def broken():
        raise OSError

    assert firewall.owner_window(foreground=broken, family=family) is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows firewall")
def test_status_reads_utf8_and_the_profile_of_the_bind_hosts_nic(monkeypatch):
    """#299 review IR7: the fixed script forces UTF-8 output and asks for
    the connection profile of the adapter that owns bind_host (validated as
    an IPv4 literal — anything else is never put into the script)."""
    monkeypatch.undo()
    firewall._CACHE.clear()
    scripts = []

    def run(argv, timeout):
        scripts.append(argv[4])
        return '{"rule":false,"ports":[],"program":"","profile":"Private"}'

    kw = dict(run=run, platform="win32", frozen=True, executable="b.exe")
    firewall.status([5680], bind_host="192.168.1.20", **kw)
    assert scripts[-1].startswith("[Console]::OutputEncoding=[Text.Encoding]::UTF8;")
    assert "Get-NetIPAddress -IPAddress '192.168.1.20'" in scripts[-1]
    assert "-InterfaceIndex" in scripts[-1]
    firewall.status([5680], bind_host="192.168.1.20", **kw)
    assert len(scripts) == 1  # cached per address
    firewall.status([5680], bind_host="10.0.0.7", **kw)
    assert len(scripts) == 2 and "'10.0.0.7'" in scripts[-1]
    for hostile in ("1.2.3.4';Remove-Item x;'", "127.0.0.1", "fe80::1", "", None):
        firewall._CACHE.clear()
        firewall.status([5680], bind_host=hostile, **kw)
        assert (
            "Get-NetIPAddress" not in scripts[-1] and "Remove-Item" not in scripts[-1]
        )
    firewall._CACHE.clear()


def test_the_firewall_route_asks_for_the_saved_ports(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(
        firewall, "request_allow", lambda ports: seen.append(ports) or "declined"
    )
    world = World(
        tmp_path,
        bind_host="192.168.1.20",
        api_token=SAVED_TOKEN,
        workflow={"enabled": True, "role": "controller", "remote_workers": True},
    )
    res = world.client.post("/control/network/firewall", headers=world.h)
    assert res.json() == {"result": "declined"} and seen == [[5680, 5682]]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows firewall")
def test_status_reads_the_rule_once_per_30_seconds(monkeypatch):
    monkeypatch.undo()  # the real status(), with a fake runner and clock
    firewall._CACHE.clear()
    now = [1000.0]
    runs = []
    out = [
        '{"rule":true,"ports":["5680","5682"],"program":"C:\\\\TaskPaw\\\\b.exe","profile":"Private"}'
    ]

    def run(argv, timeout):
        runs.append((argv, timeout))
        return out[0]

    kw = dict(
        run=run,
        platform="win32",
        frozen=True,
        executable=r"c:\taskpaw\B.exe",
        clock=lambda: now[0],
    )
    assert firewall.status([5680, 5682], **kw) == {
        "platform": "windows",
        "rule": "present",
        "profile": "private",
    }
    assert firewall.status([5680], **kw)["rule"] == "present"
    assert len(runs) == 1 and runs[0][1] == 8.0
    argv = runs[0][0]
    assert argv[0].lower().endswith(r"windowspowershell\v1.0\powershell.exe")
    assert argv[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
    assert "netsh" not in argv[4]
    assert firewall.status([5680, 5690], **kw)["rule"] == "absent"  # a port missing
    assert (
        firewall.status([5680], **{**kw, "executable": r"D:\moved\b.exe"})["rule"]
        == "absent"
    )
    now[0] += 30
    out[0] = '{"rule":false,"ports":[],"program":"","profile":"Public"}'
    assert firewall.status([5680], **kw) == {
        "platform": "windows",
        "rule": "absent",
        "profile": "public",
    }
    now[0] += 30
    out[0] = None
    assert firewall.status([5680], **kw)["rule"] == "unknown"
    assert firewall.status([5680], platform="darwin") == {
        "platform": "macos",
        "rule": "unknown",
        "profile": None,
    }
    firewall._CACHE.clear()


# ── pairing messages (R9) ───────────────────────────────────────────────
def test_pairing_messages_are_local_first_and_never_echo_the_code():
    from taskpaw_v3.workflow.remote_worker import (
        MALFORMED,
        REFUSED_CODE,
        JoinError,
        parse_code,
    )

    for code in ("", "hello", "TP2:1.2.3.4:5682:x:y", "TP1:192.168.1.20:5682"):
        with pytest.raises(JoinError) as caught:
            parse_code(code)
        assert str(caught.value) == MALFORMED
    with pytest.raises(JoinError) as caught:
        parse_code("TP1:8.8.8.8:5682:12345678-1234-5678-1234-567812345678:" + "A" * 40)
    assert "局域网" in str(caught.value) and "A" * 40 not in str(caught.value)
    assert "15 分钟内有效，只能用一次" in REFUSED_CODE


def test_a_controller_401_names_the_refused_code():
    from types import SimpleNamespace

    from taskpaw_v3.workflow.remote_worker import REFUSED_CODE, JoinError, WorkerHost
    from taskpaw_v3.workflow.worker_client import ApiError

    class Client:
        def self_view(self):
            raise ApiError(401, "unauthorized")

    host = SimpleNamespace(state=None, reason=None)
    with pytest.raises(JoinError) as caught:
        WorkerHost._recover(host, Client(), ApiError(401, "unauthorized"))
    assert str(caught.value) == REFUSED_CODE
    with pytest.raises(JoinError) as caught:
        WorkerHost._recover(host, Client(), ApiError(0, "unreachable"))
    assert "无法连接主控" in str(caught.value)
