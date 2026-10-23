"""#299 §3, R11, R14, R15: 立即重启 (exit 75) on the agent and the Hub, and
the Hub's settings stored as overrides (hub.yaml never rewritten). Every
store and yaml is a tmp fixture; OpenClaw is a fake sender; nothing restarts
for real (the stop callback is recorded)."""

from __future__ import annotations

import logging
import re
import socket
import urllib.error
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from taskpaw_v3.agent.server import service
from taskpaw_v3.agent.server.admin import MonitorAdmin
from taskpaw_v3.agent.server.app import create_control_app
from taskpaw_v3.core import gpu_lease, lan, restart
from taskpaw_v3.core.config import AgentConfig, HubConfig, load_yaml
from taskpaw_v3.hub.server import settings as hub_settings
from taskpaw_v3.hub.server.app import create_hub_app, create_hub_control_app
from taskpaw_v3.hub.server.store import HubStore
from taskpaw_v3.monitors.registry import PluginRegistry

TOKEN = "control-token"
BOOT = "0123456789abcdef0123456789abcdef"
OC_TOKEN = "ocw_secret-never-shown-12345678"
POLL_TOKEN = "tp_PollingTokenPollingToken7Q2f"


@pytest.fixture(autouse=True)
def clean_restart(monkeypatch):
    restart._reset_for_tests()
    gpu_lease._reset_for_tests()  # a granted restart closes the process lease
    monkeypatch.setattr(lan, "_interfaces", lambda: {"eth": ["192.168.1.30"]})
    monkeypatch.setattr(lan, "_route_source", lambda: "192.168.1.30")
    yield
    restart._reset_for_tests()
    gpu_lease._reset_for_tests()


# ── the agent ───────────────────────────────────────────────────────────
class Agent:
    def __init__(self, tmp_path: Path, worker=None, available=True):
        self.path = tmp_path / "agent.yaml"
        self.path.write_text(
            yaml.safe_dump({"server_id": "s", "machine": "m", "host_metrics": False}),
            encoding="utf-8",
        )
        self.config = load_yaml(AgentConfig, self.path)
        self.admin = MonitorAdmin(
            self.config, None, PluginRegistry(), self.path, bind_probe=lambda h, p: None
        )
        self.restarts = 0

        def on_restart():
            self.restarts += 1

        app = create_control_app(
            self.config,
            admin=self.admin,
            control_token=TOKEN,
            control_active=lambda: True,
            worker_provider=lambda: worker,
            boot_id=BOOT,
            on_restart=on_restart if available else None,
        )
        self.client = TestClient(app, base_url="http://127.0.0.1")
        self.h = {"Authorization": f"Bearer {TOKEN}"}

    def view(self):
        return self.client.get("/control/restart", headers=self.h).json()

    def post(self, boot=BOOT):
        return self.client.post(
            "/control/restart", json={"boot_id": boot}, headers=self.h
        )


def test_the_restart_view_names_the_pending_fields(tmp_path):
    agent = Agent(tmp_path)
    assert agent.view() == {
        "required": False,
        "fields": [],
        "blocked": None,
        "available": True,
    }
    agent.admin.update_config({"machine": "renamed", "llm_model": "x"})
    view = agent.view()
    assert view["required"] is True and view["fields"] == ["machine"]
    inbox = tmp_path / "INBOX"
    inbox.mkdir()
    agent.client.put(
        "/control/workflow/settings",
        json={"role": "off", "inbox_root": str(inbox)},
        headers=agent.h,
    )
    assert "machine" in agent.view()["fields"]


def test_an_idle_agent_restarts_after_the_reply(tmp_path):
    agent = Agent(tmp_path)
    res = agent.post()
    assert res.status_code == 202 and res.json() == {"restarting": True}
    assert agent.restarts == 1  # BackgroundTask ran after the response


def test_a_gpu_task_or_a_busy_worker_blocks_the_restart(tmp_path):
    agent = Agent(tmp_path)
    assert gpu_lease.try_acquire(("jasna", 1), 1.0, "修复 ABC-123")
    res = agent.post()
    assert res.status_code == 409 and res.json()["code"] == "restart_busy"
    assert res.json()["detail"] == "暂不能重启：本机正在运行：修复 ABC-123"
    assert agent.view()["blocked"] == "本机正在运行：修复 ABC-123"
    gpu_lease.release(("jasna", 1))

    class Busy:
        def summary(self):
            return {"busy": True}

    (tmp_path / "w").mkdir()
    worker = Agent(tmp_path / "w", worker=Busy())
    res = worker.post()
    assert res.status_code == 409 and "主控派来的影片" in res.json()["detail"]
    assert agent.restarts == 0 and worker.restarts == 0


def test_a_granted_restart_refuses_saves_gpu_admissions_and_a_second_restart(
    tmp_path,
):
    """#299 review IR5: the flag is set under the admission lock, so nothing
    new starts between the 202 and the exit."""
    agent = Agent(tmp_path)
    assert agent.post().status_code == 202
    restarting = {"detail": "正在重启", "code": "restarting"}
    second = agent.post()
    assert second.status_code == 409 and second.json() == restarting
    res = agent.client.patch("/control/config", json={"machine": "x"}, headers=agent.h)
    assert res.status_code == 409 and res.json() == restarting
    res = agent.client.put(
        "/control/workflow/settings", json={"role": "off"}, headers=agent.h
    )
    assert res.status_code == 409 and res.json() == restarting
    assert not gpu_lease.try_acquire(("jasna", 1), 1.0, "x")
    assert gpu_lease.holder() is None and gpu_lease.waiters() == []
    # a monitor add/remove/edit reserves first: refused (409 busy) too
    assert agent.admin._reserve("late", "add", {}, 0.0) is None
    assert agent.restarts == 1
    assert yaml.safe_load(agent.path.read_text(encoding="utf-8"))["machine"] == "m"


def test_a_lease_held_when_the_restart_is_asked_keeps_it_open(tmp_path):
    """The busy check and the close are one step: a holder refuses the restart
    and the lease stays usable."""
    agent = Agent(tmp_path)
    assert gpu_lease.try_acquire(("jasna", 1), 1.0, "x")
    assert agent.post().status_code == 409
    gpu_lease.release(("jasna", 1))
    assert gpu_lease.try_acquire(("avsubs", 1), 1.0, "y")
    gpu_lease.release(("avsubs", 1))


def test_a_concurrent_save_blocks_the_restart(tmp_path):
    agent = Agent(tmp_path)
    agent.admin._mutation.acquire()
    try:
        res = agent.post()
    finally:
        agent.admin._mutation.release()
    assert res.status_code == 409 and res.json()["code"] == "operation_busy"
    assert agent.restarts == 0


def test_a_stale_window_or_no_shell_cannot_restart(tmp_path):
    agent = Agent(tmp_path)
    assert agent.post(boot="f" * 32).json()["code"] == "stale_boot"
    assert (
        agent.client.post("/control/restart", content=b"x", headers=agent.h).status_code
        == 409
    )
    (tmp_path / "n").mkdir()
    none = Agent(tmp_path / "n", available=False)
    assert none.post().json()["code"] == "unsupported"
    assert none.view()["available"] is False
    assert agent.restarts == 0


def test_the_service_exits_75_after_a_planned_restart(tmp_path, monkeypatch):
    path = tmp_path / "agent.yaml"
    path.write_text(
        yaml.safe_dump({"server_id": "s", "machine": "m"}), encoding="utf-8"
    )
    monkeypatch.setattr(service, "default_config_path", lambda: path)
    stopped = []

    def run(config, **kwargs):
        restart.request(lambda: stopped.append(True))

    monkeypatch.setattr(service, "run_agent", run)
    assert service.main() == restart.EXIT_CODE == 75
    restart._reset_for_tests()
    monkeypatch.setattr(service, "run_agent", lambda config, **kw: None)
    assert service.main() == 0


def test_request_marks_then_stops_on_its_own_thread():
    import threading

    done = threading.Event()
    restart.request(done.set)
    assert done.wait(5) and restart.requested()


# ── the Hub: overrides (R15) ────────────────────────────────────────────
@pytest.fixture
def store(tmp_path):
    s = HubStore(tmp_path / "hub.db")
    yield s
    s.close()


def test_effective_config_lays_valid_overrides_over_hub_yaml(store, caplog):
    caplog.set_level(logging.DEBUG)
    yaml_cfg = HubConfig(
        self_monitor=False,
        poll_interval=60,
        openclaw_url="http://127.0.0.1:18789/hooks/wake",
    )
    store.set_config("poll_interval", "30")
    store.set_config("openclaw_url", "http://192.168.1.9:18789/hooks/wake")
    store.set_config("status_log_retention_days", "-5")  # invalid → ignored
    store.set_config("write_status_md", "maybe")  # invalid → ignored
    store.set_config("openclaw_token", OC_TOKEN)
    store.set_config("status_md_path", "")
    effective = hub_settings.effective_hub_config(yaml_cfg, store)
    assert effective.poll_interval == 30
    assert effective.openclaw_url.startswith("http://192.168.1.9")
    assert (
        effective.status_log_retention_days == 7 and effective.write_status_md is True
    )
    assert effective.openclaw_token == OC_TOKEN and effective.status_md_path is None
    assert (
        "status_log_retention_days" in caplog.text and "write_status_md" in caplog.text
    )
    assert (
        "-5" not in caplog.text
        and "maybe" not in caplog.text
        and OC_TOKEN not in caplog.text
    )


def test_a_lan_override_without_a_token_is_dropped_at_startup(store, caplog):
    store.set_config("lan_access", "1")
    store.set_config("bind_host", "192.168.1.30")
    effective = hub_settings.effective_hub_config(HubConfig(self_monitor=False), store)
    assert effective.bind_host == "127.0.0.1" and effective.lan_access is False
    assert "bind_host" in caplog.text


class Hub:
    def __init__(
        self, store: HubStore, tmp_path: Path, yaml_cfg: HubConfig | None = None
    ):
        self.yaml = yaml_cfg or HubConfig(
            self_monitor=False, data_dir=str(tmp_path / "hubdata")
        )
        self.store = store
        self.running = hub_settings.effective_hub_config(self.yaml, store)
        _app, self.service = create_hub_app(self.running, store, yaml_config=self.yaml)
        self.restarts = 0

        def on_restart():
            self.restarts += 1

        app = create_hub_control_app(
            self.running,
            store,
            self.service,
            control_token=TOKEN,
            control_active=lambda: True,
            yaml_config=self.yaml,
            boot_id=BOOT,
            on_restart=on_restart,
        )
        self.client = TestClient(app, headers={"Authorization": f"Bearer {TOKEN}"})

    def get(self):
        return self.client.get("/config/settings").json()

    def put(self, body):
        return self.client.put("/config/settings", json=body)


def test_hub_settings_mask_tokens_and_split_live_from_restart(store, tmp_path):
    hub = Hub(store, tmp_path)
    res = hub.put(
        {
            "openclaw_enabled": True,
            "openclaw_url": "http://127.0.0.1:18789/hooks/wake",
            "openclaw_token": OC_TOKEN,
            "polling_token": POLL_TOKEN,
        }
    )
    assert res.status_code == 200 and res.json()["restart_required"] is False
    # live: the poller reads them at once
    assert hub.service.poller.get_active() is True
    assert hub.service.poller.get_token() == OC_TOKEN
    assert hub.service.poller.get_polling_token() == POLL_TOKEN
    hub.put({"openclaw_url": "http://192.168.1.9:18789/hooks/wake"})
    assert hub.service.poller.get_url() == "http://192.168.1.9:18789/hooks/wake"
    view = hub.get()
    assert (
        view["saved"]["openclaw_token"] == "***"
        and view["saved"]["polling_token"] == "***"
    )
    assert view["saved"]["polling_token_hint"] == "7Q2f"
    assert OC_TOKEN not in str(view) and POLL_TOKEN not in str(view)
    # "***" keeps a token; null clears it
    hub.put({"openclaw_token": "***"})
    assert store.get_config("openclaw_token") == OC_TOKEN
    hub.put({"openclaw_token": None})
    assert store.get_config("openclaw_token") == ""
    # restart keys
    res = hub.put(
        {"poll_interval": 15, "status_log_retention_days": 14, "write_status_md": False}
    )
    assert res.json() == {
        "ok": True,
        "restart_required": True,
        "fields": ["poll_interval", "write_status_md", "status_log_retention_days"],
    }
    assert hub.running.poll_interval == 60  # the running Hub keeps its value


@pytest.mark.parametrize(
    "body,field",
    [
        ({"poll_interval": 20}, "poll_interval"),
        ({"poll_interval": True}, "poll_interval"),
        ({"status_log_retention_days": 3651}, "status_log_retention_days"),
        ({"openclaw_url": "ftp://x"}, "openclaw_url"),
        ({"openclaw_url": "http://u:p@host/x"}, "openclaw_url"),
        ({"openclaw_enabled": "yes"}, "openclaw_enabled"),
        ({"status_md_path": "relative/status.md"}, "status_md_path"),
        ({"status_md_path": "\\\\nas\\status.md"}, "status_md_path"),
        ({"polling_token": "bad\ntoken"}, "polling_token"),
        ({"unknown": 1}, "unknown"),
        ({"lan_access": True}, "api_token"),
    ],
)
def test_hub_settings_refuse_per_field_and_write_nothing(store, tmp_path, body, field):
    hub = Hub(store, tmp_path)
    res = hub.put(body)
    assert res.status_code == 422 and field in res.json()["fields"]
    assert hub_settings.stored_overrides(store) == {}
    assert "bad\ntoken" not in res.text


def test_hub_lan_read_api_switch_picks_the_primary_address(
    store, tmp_path, monkeypatch
):
    probes = []
    monkeypatch.setattr(lan, "probe_bind", lambda h, p: probes.append((h, p)))
    hub = Hub(store, tmp_path)
    res = hub.put({"lan_access": True, "api_token": "tp_HubReadApiTokenHubReadApi0001"})
    assert res.status_code == 200 and "bind_host" in res.json()["fields"]
    assert store.get_config("bind_host") == "192.168.1.30"
    assert probes == [("192.168.1.30", 5690)]
    hub.put({"lan_access": False})
    assert store.get_config("bind_host") == "127.0.0.1"


def test_every_changed_hub_read_address_is_probed_even_loopback(
    store, tmp_path, monkeypatch
):
    """#299 review CX1: switching the read API back to loopback binds
    127.0.0.1:port once before it is saved — a port taken there would leave
    the Hub unable to start."""
    probes = []
    monkeypatch.setattr(
        lan,
        "probe_bind",
        lambda h, p: (
            probes.append((h, p)) or (lan.IN_USE if h == "127.0.0.1" else None)
        ),
    )
    lan_yaml = HubConfig(
        self_monitor=False,
        data_dir=str(tmp_path / "hubdata"),
        lan_access=True,
        bind_host="192.168.1.30",
        api_token="tp_HubReadApiTokenHubReadApi0001",
    )
    hub = Hub(store, tmp_path, lan_yaml)
    res = hub.put({"lan_access": False})
    assert res.status_code == 422 and res.json()["fields"] == {
        "bind_port": "端口已被占用"
    }
    assert probes == [("127.0.0.1", 5690)]
    assert hub_settings.stored_overrides(store) == {}
    probes.clear()
    assert hub.put({"lan_access": True}).status_code == 200  # the running address
    assert probes == []


def test_the_live_openclaw_url_falls_back_to_hub_yaml(store, tmp_path):
    """#299 review CX4: the poller's getter validates like startup does — an
    invalid override (a hand edit) means the hub.yaml address, not the raw
    value and not an older override."""
    yaml_cfg = HubConfig(
        self_monitor=False,
        data_dir=str(tmp_path / "hubdata"),
        openclaw_url="http://127.0.0.1:18789/hooks/wake",
    )
    store.set_config("openclaw_url", "http://192.168.1.9:18789/hooks/wake")
    hub = Hub(store, tmp_path, yaml_cfg)
    assert hub.service.poller.get_url() == "http://192.168.1.9:18789/hooks/wake"
    for bad in ("file:///etc/passwd", "", "http://user:pw@x/y", "not a url"):
        store.set_config("openclaw_url", bad)
        assert hub.service.poller.get_url() == "http://127.0.0.1:18789/hooks/wake"
    store.set_config("openclaw_url", "http://192.168.1.10:1/x")
    assert hub.service.poller.get_url() == "http://192.168.1.10:1/x"


def test_the_hub_names_its_controller_explicitly(store, tmp_path):
    """#299 review IR2: 主控连接 is the server the operator chose — never an
    implicit servers[0]; a live setting; a removed server is no controller."""
    first = store.add_server("NAS-agent", "192.168.1.5", 5680)
    second = store.add_server("BlackGoldPig", "192.168.1.20", 5680)
    hub = Hub(store, tmp_path)
    assert hub.get()["controller_server_id"] is None
    res = hub.put({"controller_server_id": second})
    assert res.status_code == 200 and res.json()["restart_required"] is False
    assert hub.get()["controller_server_id"] == second
    for bad in (999, "2", True, 0):
        res = hub.put({"controller_server_id": bad})
        assert res.status_code == 422 and "controller_server_id" in res.json()["fields"]
    assert hub.get()["controller_server_id"] == second
    store.remove_server(second)
    assert hub.get()["controller_server_id"] is None
    hub.put({"controller_server_id": first})
    assert hub.put({"controller_server_id": None}).status_code == 200
    assert hub.get()["controller_server_id"] is None


def test_hub_yaml_is_never_rewritten(store, tmp_path):
    path = tmp_path / "hub.yaml"
    path.write_text("machine: hub\nself_monitor: false\n", encoding="utf-8")
    before = path.read_bytes()
    hub = Hub(store, tmp_path, load_yaml(HubConfig, path))
    hub.put({"poll_interval": 30, "status_md_path": str(tmp_path / "s.md")})
    assert path.read_bytes() == before


def test_status_md_follows_the_saved_path(store, tmp_path):
    target = tmp_path / "openclaw" / "status.md"
    cfg = HubConfig(
        self_monitor=False,
        data_dir=str(tmp_path / "hubdata"),
        status_md_path=str(target),
    )
    _app, svc = create_hub_app(cfg, store)
    svc._refresh_compat_outputs()
    assert target.is_file()
    assert not (tmp_path / "hubdata" / "status.md").exists()


def test_a_concurrent_hub_save_answers_409(store, tmp_path):
    hub = Hub(store, tmp_path)
    lock = hub.client.app.state.hub_settings._lock
    lock.acquire()
    try:
        assert hub.put({"poll_interval": 30}).json()["code"] == "operation_busy"
        res = hub.client.post("/restart", json={"boot_id": BOOT})
        assert res.json()["code"] == "operation_busy"
    finally:
        lock.release()
    assert hub.client.post("/restart", json={"boot_id": BOOT}).status_code == 202
    assert hub.restarts == 1
    res = hub.client.post("/restart", json={"boot_id": "x"})
    assert res.json()["code"] == "stale_boot"


def test_a_granted_hub_restart_refuses_later_saves_and_restarts(store, tmp_path):
    """#299 review IR5 on the Hub: 409 「正在重启」 after the 202."""
    hub = Hub(store, tmp_path)
    assert hub.client.post("/restart", json={"boot_id": BOOT}).status_code == 202
    restarting = {"detail": "正在重启", "code": "restarting"}
    again = hub.client.post("/restart", json={"boot_id": BOOT})
    assert again.status_code == 409 and again.json() == restarting
    res = hub.put({"poll_interval": 15})
    assert res.status_code == 409 and res.json() == restarting
    assert store.get_config("poll_interval", "unset") == "unset"
    assert hub.restarts == 1


def test_the_hub_service_exits_75_after_a_planned_restart(tmp_path, monkeypatch):
    from taskpaw_v3.hub.server import service as hub_service

    path = tmp_path / "hub.yaml"
    path.write_text(
        f"self_monitor: false\ndata_dir: {tmp_path.as_posix()}/d\n", encoding="utf-8"
    )

    def run(config, store, **kwargs):
        restart.request(lambda: None)

    monkeypatch.setattr(hub_service, "run_hub", run)
    assert hub_service.run_from_config(path) == 75


# ── OpenClaw 测试发送 (R14) ──────────────────────────────────────────────
def _sender(exc=None):
    sent = []

    def send(url, token, payload, timeout):
        sent.append((url, token, payload))
        if exc is not None:
            raise exc

    return send, sent


def test_openclaw_test_outcomes():
    clock = iter([10.0, 10.4])
    send, sent = _sender()
    ok = hub_settings.openclaw_test(
        "http://127.0.0.1:18789/hooks/wake",
        OC_TOKEN,
        send=send,
        clock=lambda: next(clock),
        now=lambda: datetime(2026, 10, 23, 14, 2),
    )
    assert ok == {"ok": True, "at": "14:02", "elapsed_s": 0.4}
    assert sent[0][1] == OC_TOKEN
    cases = [
        (
            urllib.error.URLError(ConnectionRefusedError()),
            {"ok": False, "error": "unreachable"},
        ),
        (urllib.error.URLError(socket.timeout()), {"ok": False, "error": "timeout"}),
        (TimeoutError(), {"ok": False, "error": "timeout"}),
        (
            urllib.error.HTTPError("u", 401, "x", None, None),
            {"ok": False, "error": "token", "status": 401},
        ),
        (
            urllib.error.HTTPError("u", 502, "x", None, None),
            {"ok": False, "error": "http", "status": 502},
        ),
    ]
    for exc, expected in cases:
        send, _ = _sender(exc)
        assert hub_settings.openclaw_test("http://x/y", "", send=send) == expected


def test_the_openclaw_route_uses_the_form_or_the_saved_token(
    store, tmp_path, monkeypatch
):
    seen = []
    monkeypatch.setattr(
        hub_settings,
        "openclaw_test",
        lambda url, token, **kw: seen.append((url, token)) or {"ok": True},
    )
    hub = Hub(store, tmp_path)
    hub.put({"openclaw_token": OC_TOKEN})
    res = hub.client.post(
        "/config/openclaw/test", json={"url": "http://127.0.0.1:1/x", "token": "***"}
    )
    assert res.json() == {"ok": True} and seen[-1] == ("http://127.0.0.1:1/x", OC_TOKEN)
    hub.client.post(
        "/config/openclaw/test", json={"url": "http://127.0.0.1:1/x", "token": "typed"}
    )
    assert seen[-1][1] == "typed"
    bad = hub.client.post("/config/openclaw/test", json={"url": "file:///etc/passwd"})
    assert bad.status_code == 422 and "openclaw_url" in bad.json()["fields"]
    assert OC_TOKEN not in res.text


def test_the_hub_view_format(store, tmp_path):
    view = Hub(store, tmp_path).get()
    assert set(view) >= {
        "saved",
        "running",
        "restart_required",
        "fields",
        "restart_available",
    }
    assert view["saved"]["status_md_default"].endswith("status.md")
    assert re.fullmatch(r"\d+", str(view["saved"]["poll_interval"]))


def test_a_hub_address_that_cannot_bind_is_refused(store, tmp_path, monkeypatch):
    monkeypatch.setattr(lan, "probe_bind", lambda h, p: lan.NOT_LOCAL)
    hub = Hub(store, tmp_path)
    res = hub.put({"lan_access": True, "api_token": "tp_HubReadApiTokenHubReadApi0001"})
    assert res.status_code == 422
    assert res.json()["fields"] == {"bind_host": "此地址不属于本机"}
    assert hub_settings.stored_overrides(store) == {}
