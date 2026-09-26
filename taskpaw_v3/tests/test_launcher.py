"""agent launcher build_queue (#115).

ensure_port_free / port_available / claim_port are covered in test_agent.py;
run_agent is integration (binds real sockets). This covers build_queue: the
EventQueue wiring + the persisted monotonic id contract (constitution §3)."""

from __future__ import annotations

import pytest

from taskpaw_v3.agent.server.launcher import build_queue
from taskpaw_v3.core.config import AgentConfig


def _cfg() -> AgentConfig:
    return AgentConfig(server_id="s1", machine="box1")


def test_build_queue_without_state_is_in_memory():
    q = build_queue(_cfg(), None)
    assert q.machine == "box1"
    # An in-memory queue still issues monotonically increasing ids.
    e1 = q.add("mon", "m", level="info")
    e2 = q.add("mon", "m", level="info")
    assert e2["id"] > e1["id"]


def test_build_queue_persists_and_resumes_id_across_restart(tmp_path):
    state = tmp_path / "next_id.json"
    q1 = build_queue(_cfg(), state)
    first = q1.add("mon", "m", level="info")["id"]

    # A fresh queue from the same state file must NOT reissue an already-used id —
    # the persisted counter resumes past it (no duplicate ids after a restart).
    q2 = build_queue(_cfg(), state)
    nxt = q2.add("mon", "m", level="info")["id"]
    assert nxt > first


# ── LLM settings holder initialised at boot (#178 AC5) ─────────────────────
# Hermetic (D7): non-default ports, and run_agent never reaches the real stale-
# port reclaim or a real socket bind.
def _llm_cfg() -> AgentConfig:
    return AgentConfig(
        server_id="s1",
        machine="box1",
        bind_port=15680,
        control_port=15681,
        host_metrics=False,
        llm_model="cfg/model",
        llm_api_key="sk-BOOTKEY-2e2e",
    )


class _Sentinel(Exception):
    pass


@pytest.mark.parametrize("fail_at", ["reclaim", "network", "control"])
def test_tasklog_failed_claim_creates_no_store_or_line(tmp_path, monkeypatch, fail_at):
    from unittest.mock import Mock

    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.core.tasklog import get_task_log

    sock = Mock()

    def reclaim(*a, **kw):
        assert not (tmp_path / "logs").exists()
        if fail_at == "reclaim":
            raise launcher.PortInUseError("occupied")

    def claim(host, port, label):
        assert not (tmp_path / "logs").exists()
        if fail_at in label:
            raise launcher.PortInUseError("occupied")
        return sock

    monkeypatch.setattr(launcher, "reclaim_ports_from_stale_instance", reclaim)
    monkeypatch.setattr(launcher, "claim_port", claim)
    with pytest.raises(launcher.PortInUseError):
        launcher.run_agent(_llm_cfg(), config_path=tmp_path / "agent.yaml", block=False)
    assert not (tmp_path / "logs").exists()
    assert get_task_log().query()["entries"] == []
    if fail_at == "control":
        sock.close.assert_called_once()


def test_tasklog_launcher_order_and_direct_failure_alert(tmp_path, monkeypatch):
    from unittest.mock import Mock

    import uvicorn

    from taskpaw_v3 import __version__
    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.core.lifecycle import GracefulShutdown
    from taskpaw_v3.core.protocol import EventQueue
    from taskpaw_v3.core.tasklog import TaskLog, get_task_log
    from taskpaw_v3.monitors import runtime

    calls = []
    previous = TaskLog(tmp_path)
    previous.record("", "agent.started", task_type="agent")
    previous.record("a", "restore.started", task_type="jasna", film="old")
    queue = EventQueue("m")
    shutdown = GracefulShutdown()
    monkeypatch.setattr(shutdown, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(
        launcher,
        "reclaim_ports_from_stale_instance",
        lambda *a, **kw: calls.append("reclaim"),
    )
    monkeypatch.setattr(
        launcher, "claim_port", lambda *a, **kw: (calls.append("claim"), Mock())[1]
    )

    def build(*a, **kw):
        assert calls == ["reclaim", "claim", "claim"]
        rows = get_task_log().query()["entries"]
        assert rows[0]["kind"] == "agent.started"
        assert rows[0]["data"]["version"] == __version__
        assert rows[0]["data"]["previous_exit"] == "unclean"
        assert rows[1]["data"]["reconstructed"] is True
        sup = Mock()
        sup.stop.side_effect = lambda: calls.append(
            get_task_log().query()["entries"][0]["kind"]
        )
        return sup

    monkeypatch.setattr(runtime, "build_supervisor", build)
    monkeypatch.setattr(uvicorn, "Server", lambda *a, **kw: Mock())
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **kw: None)
    monkeypatch.setattr(launcher, "announce_ready", lambda *a: None)
    launcher.run_agent(
        _llm_cfg(),
        queue=queue,
        shutdown=shutdown,
        config_path=tmp_path / "agent.yaml",
        block=False,
    )
    store = get_task_log()

    def fail(row):
        raise OSError("locked")

    monkeypatch.setattr(store, "_append", fail)
    store.record("a", "task.done", task_type="jasna")
    store.record("a", "task.done", task_type="jasna")
    alerts = queue.recent()
    assert len(alerts) == 1 and alerts[0]["level"] == "alert"
    assert not any(r["kind"] == "event.mirrored" for r in store.query()["entries"])
    shutdown.shutdown()
    assert calls[-1] == "agent.stopping"


def test_run_agent_sets_llm_holder_before_port_reclaim(monkeypatch):
    # T-L1: the holder is initialised BEFORE the stale-port reclaim — hence before
    # every socket claim and the supervisor.
    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.core.llm import get_llm_settings

    seen = {}

    def reclaim(*a, **k):
        seen["settings"] = get_llm_settings()
        raise _Sentinel

    def no_claim(*a, **k):
        raise AssertionError("must not claim a port")

    monkeypatch.setattr(launcher, "reclaim_ports_from_stale_instance", reclaim)
    monkeypatch.setattr(launcher, "claim_port", no_claim)
    with pytest.raises(_Sentinel):
        launcher.run_agent(_llm_cfg(), block=False)
    s = seen["settings"]
    assert (s.model, s.api_key, s.key_source) == (
        "cfg/model",
        "sk-BOOTKEY-2e2e",
        "config",
    )
    assert get_llm_settings() == s


# ── #192: the provider chain, the failover switch and the data dir (C5) ─────
def _chain_boot_cfg() -> AgentConfig:
    return AgentConfig(
        server_id="s1",
        machine="box1",
        bind_port=15680,
        control_port=15681,
        host_metrics=False,
        llm_model="cfg/model",
        llm_api_key="sk-BOOTKEY-2e2e",
        llm_fallback1_api_base="https://api.deepseek.com/v1",
        llm_fallback1_model="deepseek-chat",
        llm_fallback1_api_key="sk-FB1BOOT-3f3f",
        llm_failover=False,
    )


def _observe_at_reclaim(monkeypatch) -> dict:
    """Stop run_agent at the stale-port reclaim (before any socket claim and
    the supervisor) and record what the holders publish by then."""
    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.core.datadir import get_data_dir
    from taskpaw_v3.core.llm import get_llm_chain, get_llm_failover

    seen: dict = {}

    def reclaim(*a, **k):
        seen["chain"] = get_llm_chain()
        seen["failover"] = get_llm_failover()
        seen["data_dir"] = get_data_dir()
        raise _Sentinel

    def no_claim(*a, **k):
        raise AssertionError("must not claim a port")

    monkeypatch.setattr(launcher, "reclaim_ports_from_stale_instance", reclaim)
    monkeypatch.setattr(launcher, "claim_port", no_claim)
    return seen


def _no_default_config_path(monkeypatch) -> None:
    # C5: the data dir comes ONLY from run_agent's config_path — never from
    # default_config_path() (it would be the real %APPDATA% in a test).
    from taskpaw_v3.agent.server import service

    def boom():
        raise AssertionError("default_config_path() must not be used")

    monkeypatch.setattr(service, "default_config_path", boom)


def test_run_agent_publishes_chain_failover_and_data_dir_first(monkeypatch, tmp_path):
    from taskpaw_v3.agent.server import launcher

    _no_default_config_path(monkeypatch)
    seen = _observe_at_reclaim(monkeypatch)
    config_path = tmp_path / "cfgdir" / "agent.yaml"
    with pytest.raises(_Sentinel):
        launcher.run_agent(_chain_boot_cfg(), config_path=config_path, block=False)
    assert [(s.model, s.api_key, s.key_source) for s in seen["chain"]] == [
        ("cfg/model", "sk-BOOTKEY-2e2e", "config"),
        ("deepseek-chat", "sk-FB1BOOT-3f3f", "config"),
    ]
    assert seen["failover"] is False
    assert seen["data_dir"] == tmp_path / "cfgdir"
    assert not (tmp_path / "cfgdir").exists()  # publishing creates nothing


def test_run_agent_without_config_path_has_no_data_dir(monkeypatch, tmp_path):
    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.core.datadir import set_data_dir

    _no_default_config_path(monkeypatch)
    set_data_dir(tmp_path / "stale")  # a previous value never survives
    seen = _observe_at_reclaim(monkeypatch)
    with pytest.raises(_Sentinel):
        launcher.run_agent(_chain_boot_cfg(), block=False)
    assert seen["data_dir"] is None
    assert len(seen["chain"]) == 2


def test_run_agent_supervisor_starts_with_llm_settings(monkeypatch):
    # T-L2 (D13): ordering by observation — the supervisor's start() (the first
    # point any monitor can check()) already sees the configured settings.
    import socket

    import uvicorn

    import taskpaw_v3.monitors.runtime as runtime
    from taskpaw_v3.agent.server import app, launcher
    from taskpaw_v3.core.lifecycle import GracefulShutdown
    from taskpaw_v3.core.llm import get_llm_settings

    started = {}

    class _FakeSupervisor:
        def start(self):
            from taskpaw_v3.core.datadir import get_data_dir
            from taskpaw_v3.core.llm import get_llm_chain

            started["settings"] = get_llm_settings()
            started["chain"] = get_llm_chain()
            started["data_dir"] = get_data_dir()

        def stop(self):
            started["stopped"] = True

        def snapshot(self):
            return {}

        def film_page(self, instance_id, page, size):
            return None

    class _FakeServer:
        def __init__(self, config):
            self.should_exit = False

        def run(self, sockets=None):
            return None

    supervisor = _FakeSupervisor()
    control_kwargs = {}
    create_control_app = app.create_control_app

    def capture_control_app(*args, **kwargs):
        control_kwargs.update(kwargs)
        return create_control_app(*args, **kwargs)

    monkeypatch.setattr(
        launcher, "reclaim_ports_from_stale_instance", lambda *a, **k: None
    )
    monkeypatch.setattr(launcher, "claim_port", lambda *a, **k: socket.socket())
    monkeypatch.setattr(runtime, "build_supervisor", lambda *a, **k: supervisor)
    monkeypatch.setattr(app, "create_control_app", capture_control_app)
    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "announce_ready", lambda *a, **k: None)
    shutdown = GracefulShutdown()
    # Keep pytest's own SIGINT/SIGTERM handlers.
    monkeypatch.setattr(shutdown, "install_signal_handlers", lambda: None)
    try:
        launcher.run_agent(_llm_cfg(), shutdown=shutdown, block=False)
        assert control_kwargs.get("films_provider") == supervisor.film_page
        s = started["settings"]
        assert (s.model, s.api_key, s.key_source) == (
            "cfg/model",
            "sk-BOOTKEY-2e2e",
            "config",
        )
        assert started["chain"] == (s,)
        assert started["data_dir"] is None  # no config_path
    finally:
        shutdown.shutdown()
    assert started.get("stopped") is True
