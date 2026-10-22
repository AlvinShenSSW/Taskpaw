"""PWR-1 (#287): power settings, routes and launcher wiring.

The network route is read-only (GET), bounded and behind api_token; the
console reads the same data from a loopback control GET. Settings live under
`power:` in agent.yaml and apply live (PW07).
"""

from __future__ import annotations

import socket
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from taskpaw_v3.agent.server.admin import MonitorAdmin
from taskpaw_v3.agent.server.app import create_control_app, create_network_app
from taskpaw_v3.core import power
from taskpaw_v3.core.config import AgentConfig, PowerSettings, load_yaml
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.tests.test_admin import _agent_config, _registry

TOKEN = "net-read-token"
CONTROL = "ctl-token"
HIST = {
    "range": "24h",
    "step_s": 900,
    "estimated": True,
    "t": [0],
    "covered_s": [0],
    "avg_w": [None],
}


def _net(history=None, token=TOKEN):
    cfg = AgentConfig(server_id="s", machine="m", api_token=token)
    calls = []

    def provider(rng):
        calls.append(rng)
        return {**HIST, "range": rng}

    app = create_network_app(
        cfg,
        EventQueue("m"),
        power_history=provider if history is None else history,
    )
    return TestClient(app), calls


# ── network route ───────────────────────────────────────────────────────────
def test_power_history_requires_the_api_token():
    client, calls = _net()
    assert client.get("/power/history").status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert client.get("/power/history?range=7d", headers=bad).status_code == 401
    # an invalid range from an unauthenticated caller is still a 401
    assert client.get("/power/history?range=zz").status_code == 401
    assert calls == []


def test_power_history_returns_the_bounded_series():
    client, calls = _net()
    auth = {"Authorization": f"Bearer {TOKEN}"}
    for rng in ("24h", "7d", "30d"):
        r = client.get(f"/power/history?range={rng}", headers=auth)
        assert r.status_code == 200 and r.json()["range"] == rng
    assert client.get("/power/history", headers=auth).json()["range"] == "24h"
    assert calls == ["24h", "7d", "30d", "24h"]


@pytest.mark.parametrize("rng", ["1y", "", "24H", "24h&x", "x" * 300])
def test_power_history_rejects_other_ranges(rng):
    client, calls = _net()
    r = client.get(
        "/power/history",
        params={"range": rng},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert r.status_code == 400 and r.json() == {"error": "invalid range"}
    assert calls == []


def test_power_history_404_without_a_sampler():
    cfg = AgentConfig(server_id="s", machine="m")
    client = TestClient(create_network_app(cfg, EventQueue("m")))
    assert client.get("/power/history").status_code == 404


def test_power_history_is_get_only():
    client, _ = _net()
    auth = {"Authorization": f"Bearer {TOKEN}"}
    for method in ("post", "put", "patch", "delete"):
        assert (
            getattr(client, method)("/power/history", headers=auth).status_code == 405
        )


def test_network_app_stays_all_get():
    cfg = AgentConfig(server_id="s", machine="m", api_token=TOKEN)
    app = create_network_app(cfg, EventQueue("m"), power_history=lambda rng: dict(HIST))
    methods = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            methods |= set(route.methods)
    assert methods <= {"GET", "HEAD"}
    assert any(
        isinstance(r, APIRoute) and r.path == "/power/history" for r in app.routes
    )


def test_status_carries_the_power_field_from_the_provider():
    cfg = AgentConfig(server_id="s", machine="m")
    live = {"w": 80.0, "estimated": True}
    client = TestClient(
        create_network_app(
            cfg, EventQueue("m"), status_provider=lambda: {"power": live}
        )
    )
    assert client.get("/status").json()["power"] == live


# ── control route ───────────────────────────────────────────────────────────
def _control(view=None):
    cfg = AgentConfig(server_id="s", machine="m")
    calls = []

    def provider(rng):
        calls.append(rng)
        return {"power": {"w": 1.0}, "history": {**HIST, "range": rng}}

    app = create_control_app(
        cfg,
        control_token=CONTROL,
        control_active=lambda: True,
        power_view=provider if view is None else view,
    )
    return TestClient(app), calls


def test_control_power_reads_live_and_history():
    client, calls = _control()
    auth = {"Authorization": f"Bearer {CONTROL}"}
    r = client.get("/control/power?range=7d", headers=auth)
    assert r.status_code == 200
    assert r.json() == {
        "power": {"w": 1.0},
        "history": {**HIST, "range": "7d"},
    }
    assert client.get("/control/power?range=nope", headers=auth).status_code == 400
    assert calls == ["7d"]


def test_control_power_requires_the_control_token():
    client, calls = _control()
    assert client.get("/control/power").status_code == 401
    assert calls == []


def test_control_power_404_without_a_sampler():
    cfg = AgentConfig(server_id="s", machine="m")
    client = TestClient(
        create_control_app(cfg, control_token=CONTROL, control_active=lambda: True)
    )
    auth = {"Authorization": f"Bearer {CONTROL}"}
    assert client.get("/control/power", headers=auth).status_code == 404


def test_power_view_combines_live_and_history():
    s = power.PowerSampler(
        lambda: PowerSettings(),
        gpu=lambda: None,
        cpu=lambda: 0.0,
        activity=lambda: ("idle", power.NO_OWNER),
        platform="win32",
    )
    view = power.power_view(s, "30d")
    assert view["power"] is None  # nothing sampled yet
    assert view["history"]["range"] == "30d"
    with pytest.raises(ValueError):
        power.power_view(s, "bad")


# ── settings: _EDITABLE_CONFIG / _LIVE_CONFIG ───────────────────────────────
def test_power_is_an_editable_live_setting():
    assert "power" in MonitorAdmin._EDITABLE_CONFIG
    assert "power" in MonitorAdmin._LIVE_CONFIG
    assert "power" not in MonitorAdmin._NON_LIVE_CONFIG


def test_update_config_power_partial_patch_is_live_and_persisted(tmp_path):
    cfg = _agent_config(power={"tdp_w": 90, "other_w": 40})
    path = tmp_path / "agent.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    res = admin.update_config({"power": {"tdp_w": 125, "psu_efficiency": 0.9}})
    assert res == {"ok": True, "restart_required": False}
    # a partial patch merges over the current power settings
    assert cfg.power == PowerSettings(tdp_w=125, other_w=40, psu_efficiency=0.9)
    assert power.get_power_settings() == cfg.power
    assert load_yaml(AgentConfig, path).power == cfg.power
    view = admin.config_view()
    assert view["power"]["tdp_w"] == 125 and view["power"]["other_w"] == 40


def test_update_config_power_can_turn_sampling_off(tmp_path):
    cfg = _agent_config()
    admin = MonitorAdmin(cfg, None, _registry(), tmp_path / "agent.yaml")
    admin.update_config({"power": {"enabled": False}})
    assert power.get_power_settings().enabled is False
    admin.update_config({"power": {"cpu_source": "none"}})
    s = power.get_power_settings()
    assert s.enabled is False and s.cpu_source == "none"


@pytest.mark.parametrize(
    "bad",
    [
        {"cpu_source": "measured"},
        {"psu_efficiency": 2},
        {"tdp_w": "lots"},
        "not-an-object",
    ],
)
def test_update_config_power_rejects_invalid(tmp_path, bad):
    cfg = _agent_config()
    path = tmp_path / "agent.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    with pytest.raises(ValueError):
        admin.update_config({"power": bad})
    assert not path.exists()
    assert power.get_power_settings() == PowerSettings()


def test_get_control_config_shows_power(tmp_path):
    cfg = _agent_config(power={"other_w": 28})
    admin = MonitorAdmin(cfg, None, _registry(), tmp_path / "a.yaml")
    client = TestClient(
        create_control_app(
            cfg, admin=admin, control_token=CONTROL, control_active=lambda: True
        )
    )
    body = client.get(
        "/control/config", headers={"Authorization": f"Bearer {CONTROL}"}
    ).json()
    assert body["power"]["other_w"] == 28
    assert body["power"]["cpu_source"] == "estimate"


# ── read hooks ──────────────────────────────────────────────────────────────
def test_supervisor_type_of():
    from taskpaw_v3.monitors.plugins.host_metrics import (
        HostMetricsConfig,
        HostMetricsPlugin,
    )
    from taskpaw_v3.monitors.supervisor import Supervisor

    sup = Supervisor(lambda *a, **k: None)
    sup.register(HostMetricsPlugin(), HostMetricsConfig(name="h"))
    try:
        assert sup.type_of("h") == "host_metrics"
        assert sup.type_of("missing") is None
    finally:
        sup.stop()


def test_dispatcher_current_stage_is_a_pure_read():
    from taskpaw_v3.workflow.local_dispatch import LocalDispatcher

    d = LocalDispatcher(SimpleNamespace())  # type: ignore[arg-type]
    assert d.current_stage() is None
    d._running = SimpleNamespace(  # type: ignore[assignment]
        job=SimpleNamespace(film_id="film-1", stage="translate")
    )
    assert d.current_stage() == ("film-1", "translate")
    assert d._running is not None and d.current_stage() == ("film-1", "translate")


# ── launcher wiring (PW01) ──────────────────────────────────────────────────
class _FakeSampler:
    instances: list = []

    def __init__(self, settings, *, activity, data_dir, **kw):
        self.settings, self.activity, self.data_dir = settings, activity, data_dir
        self.events: list[str] = []
        _FakeSampler.instances.append(self)

    def start(self):
        self.events.append("start")

    def stop(self, timeout=10.0):
        self.events.append("stop")
        return True

    def live(self):
        return {"w": 42.0, "estimated": True}

    def history(self, rng):
        return {"range": rng}


class _FakeSupervisor:
    def __init__(self, order):
        self.order = order

    def start(self):
        self.order.append("supervisor.start")

    def stop(self):
        self.order.append("supervisor.stop")

    def snapshot(self):
        return {}

    def film_page(self, *a):
        return None

    def run_films(self, *a):
        return None

    def respond_prompt(self, *a):
        return False

    def type_of(self, iid):
        return {"j": "jasna"}.get(iid)


class _FakeServer:
    started = True

    def __init__(self, config):
        self.should_exit = False

    def run(self, sockets=None):
        return None


def _run_agent(monkeypatch, cfg, tmp_path=None):
    import uvicorn

    import taskpaw_v3.monitors.runtime as runtime
    from taskpaw_v3.agent.server import app, launcher
    from taskpaw_v3.core import control
    from taskpaw_v3.core.lifecycle import GracefulShutdown

    order: list[str] = []
    _FakeSampler.instances = []
    kwargs: dict = {"network": {}, "control": {}}
    net_app, ctl_app = app.create_network_app, app.create_control_app

    def cap_net(*a, **k):
        kwargs["network"].update(k)
        return net_app(*a, **k)

    def cap_ctl(*a, **k):
        kwargs["control"].update(k)
        return ctl_app(*a, **k)

    class OrderedSampler(_FakeSampler):
        def start(self):
            order.append("sampler.start")
            super().start()

        def stop(self, timeout=10.0):
            order.append("sampler.stop")
            return super().stop(timeout)

    monkeypatch.setattr(
        launcher, "reclaim_ports_from_stale_instance", lambda *a, **k: None
    )
    monkeypatch.setattr(launcher, "claim_port", lambda *a, **k: socket.socket())
    monkeypatch.setattr(
        runtime, "build_supervisor", lambda *a, **k: _FakeSupervisor(order)
    )
    monkeypatch.setattr(app, "create_network_app", cap_net)
    monkeypatch.setattr(app, "create_control_app", cap_ctl)
    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "announce_ready", lambda *a, **k: None)
    monkeypatch.setattr(power, "PowerSampler", OrderedSampler)
    # an in-memory session: the real bootstrap needs a directory that passes
    # the credential ACL checks, which a temp dir does not everywhere
    original_bootstrap = control.bootstrap_control

    def bootstrap(role, base_url, _config_path):
        return original_bootstrap(role, base_url, None)

    monkeypatch.setattr(launcher, "bootstrap_control", bootstrap)
    monkeypatch.setattr(control, "bootstrap_control", bootstrap)
    shutdown = GracefulShutdown()
    monkeypatch.setattr(shutdown, "install_signal_handlers", lambda: None)
    config_path = None if tmp_path is None else tmp_path / "agent.yaml"
    launcher.run_agent(cfg, shutdown=shutdown, block=False, config_path=config_path)
    return shutdown, order, kwargs


def test_launcher_starts_the_sampler_after_the_supervisor(monkeypatch, tmp_path):
    cfg = AgentConfig(
        server_id="s", machine="m", host_metrics=False, power={"tdp_w": 77}
    )
    shutdown, order, kwargs = _run_agent(monkeypatch, cfg, tmp_path)
    try:
        assert order.index("sampler.start") > order.index("supervisor.start")
        (sampler,) = _FakeSampler.instances
        assert sampler.data_dir == tmp_path
        assert power.get_sampler() is sampler
        assert power.get_power_settings().tdp_w == 77
        assert sampler.settings is power.get_power_settings
        assert sampler.activity() == ("idle", power.NO_OWNER)
        status = kwargs["control"]["status_provider"]()
        assert status["power"] == {"w": 42.0, "estimated": True}
        assert kwargs["network"]["power_history"]("7d") == {"range": "7d"}
        assert kwargs["control"]["power_view"]("30d") == {
            "power": {"w": 42.0, "estimated": True},
            "history": {"range": "30d"},
        }
    finally:
        shutdown.shutdown()
    assert "sampler.stop" in order
    assert order.index("sampler.stop") > order.index("supervisor.stop")
    assert power.get_sampler() is None


def test_launcher_activity_maps_a_manual_lease_holder(monkeypatch):
    from taskpaw_v3.core import gpu_lease

    cfg = AgentConfig(server_id="s", machine="m", host_metrics=False)
    shutdown, _order, _kw = _run_agent(monkeypatch, cfg)
    try:
        (sampler,) = _FakeSampler.instances
        assert sampler.data_dir is None  # no config file: memory only
        assert gpu_lease.try_acquire(("j", 1), 5.0, "manual jasna")
        assert sampler.activity() == ("restore", power.MANUAL)
        gpu_lease.release(("j", 1))
    finally:
        shutdown.shutdown()


def test_launcher_sampler_is_built_even_when_disabled(monkeypatch):
    # enabled is live (PW07): the thread gates each tick on it, so turning it on
    # later needs no restart; while off, /status.power is null.
    cfg = AgentConfig(
        server_id="s", machine="m", host_metrics=False, power={"enabled": False}
    )
    shutdown, order, _kw = _run_agent(monkeypatch, cfg)
    try:
        assert "sampler.start" in order
        assert power.get_power_settings().enabled is False
    finally:
        shutdown.shutdown()
