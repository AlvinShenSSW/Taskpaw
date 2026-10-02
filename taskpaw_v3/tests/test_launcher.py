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
    from taskpaw_v3.core.state import initialize_state

    initialize_state(state, _cfg().server_id)
    q1 = build_queue(_cfg(), state)
    first = q1.add("mon", "m", level="info")["id"]

    # A fresh queue from the same state file must NOT reissue an already-used id —
    # the persisted counter resumes past it (no duplicate ids after a restart).
    q1.close()
    q2 = build_queue(_cfg(), state)
    nxt = q2.add("mon", "m", level="info")["id"]
    assert nxt > first
    q2.close()


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
    monkeypatch.setattr(launcher, "announce_ready", lambda *a, **k: None)
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

        def run_films(self, instance_id, filter, page, size):
            return None

    class _FakeServer:
        started = True

        def __init__(self, config):
            self.should_exit = False

        def run(self, sockets=None):
            return None

    supervisor = _FakeSupervisor()
    control_kwargs = {}
    network_kwargs = {}
    create_network_app = app.create_network_app

    def capture_network_app(*args, **kwargs):
        network_kwargs.update(kwargs)
        return create_network_app(*args, **kwargs)

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
    monkeypatch.setattr(app, "create_network_app", capture_network_app)
    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "announce_ready", lambda *a, **k: None)
    shutdown = GracefulShutdown()
    # Keep pytest's own SIGINT/SIGTERM handlers.
    monkeypatch.setattr(shutdown, "install_signal_handlers", lambda: None)
    try:
        launcher.run_agent(_llm_cfg(), shutdown=shutdown, block=False)
        assert control_kwargs.get("films_provider") == supervisor.film_page
        assert control_kwargs.get("run_films_provider") == supervisor.run_films
        from taskpaw_v3 import __version__

        assert network_kwargs.get("films_provider") == supervisor.film_page
        assert network_kwargs.get("run_films_provider") == supervisor.run_films
        assert control_kwargs["status_provider"]()["version"] == __version__
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


# R01 lifecycle failures use isolated mocked sockets/runtime; real wire smoke
# below uses only tmp_path files and ephemeral numeric-loopback ports.
@pytest.mark.parametrize("role", ["agent", "hub"])
@pytest.mark.parametrize(
    "failure", ["second_claim", "bootstrap", "app", "runtime_start", "second_thread"]
)
def test_control_startup_rollback_all_phases(
    role, failure, tmp_path, monkeypatch, capsys
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    import uvicorn

    from taskpaw_v3.agent.server import app as agent_app
    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.core import control, net
    from taskpaw_v3.core.config import HubConfig
    from taskpaw_v3.core.lifecycle import GracefulShutdown
    from taskpaw_v3.core.tasklog import get_task_log
    from taskpaw_v3.hub.server import app as hub_app
    from taskpaw_v3.hub.server.store import HubStore
    from taskpaw_v3.monitors import runtime

    order = []
    sockets = [Mock(), Mock()]
    claimed = []
    sessions = []
    original_bootstrap = control.bootstrap_control
    original_revoke = control.revoke_control

    def claim(*args):
        if len(claimed) == 1 and failure == "second_claim":
            raise OSError("second socket failed")
        sock = sockets[len(claimed)]
        claimed.append(sock)
        return sock

    def bootstrap(*args):
        assert len(claimed) == 2
        assert not (tmp_path / "logs").exists()
        assert get_task_log().query()["entries"] == []
        if failure == "bootstrap":
            raise control.ControlCredentialError("Control credentials unavailable")
        session = original_bootstrap(args[0], args[1], None)
        sessions.append(session)
        order.append("published")
        return session

    def revoke(session):
        original_revoke(session)
        order.append("inactive")

    class Thread:
        count = 0

        def __init__(self, *a, **kw):
            self.ident = None
            Thread.count += 1
            self.n = Thread.count

        def start(self):
            if failure == "second_thread" and self.n == 2:
                raise RuntimeError("thread start failed")
            self.ident = self.n

        def join(self, timeout=None):
            order.append("joined")

        def is_alive(self):
            return False

    def start():
        order.append("runtime_start")
        if failure == "runtime_start":
            raise RuntimeError("runtime start failed")

    def stop():
        assert not sessions[0].is_active()
        order.append("runtime_stop")
        return True

    monkeypatch.setattr(launcher, "claim_port", claim)
    monkeypatch.setattr(net, "claim_port", claim)
    monkeypatch.setattr(launcher, "bootstrap_control", bootstrap)
    monkeypatch.setattr(control, "bootstrap_control", bootstrap)
    monkeypatch.setattr(launcher, "revoke_control", revoke)
    monkeypatch.setattr(control, "revoke_control", revoke)
    monkeypatch.setattr(
        launcher, "reclaim_ports_from_stale_instance", lambda *a, **k: None
    )
    monkeypatch.setattr(net, "reclaim_ports_from_stale_instance", lambda *a, **k: None)
    monkeypatch.setattr(launcher.threading, "Thread", Thread)
    monkeypatch.setattr(
        uvicorn, "Server", lambda *a, **kw: SimpleNamespace(should_exit=False)
    )
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **kw: None)
    sup = Mock()
    sup.start.side_effect = start
    sup.stop.side_effect = stop
    monkeypatch.setattr(runtime, "build_supervisor", lambda *a, **kw: sup)
    monkeypatch.setattr(hub_app.HubService, "start", lambda self: start())
    monkeypatch.setattr(hub_app.HubService, "stop", lambda self: stop())
    if failure == "app":
        factory = agent_app if role == "agent" else hub_app
        key = "create_control_app" if role == "agent" else "create_hub_control_app"
        monkeypatch.setattr(factory, key, Mock(side_effect=RuntimeError("app failed")))
    shutdown = GracefulShutdown()
    monkeypatch.setattr(shutdown, "install_signal_handlers", lambda: None)
    store = HubStore(tmp_path / "hub.db")
    try:
        with pytest.raises((OSError, RuntimeError, control.ControlCredentialError)):
            if role == "agent":
                launcher.run_agent(
                    _llm_cfg(),
                    shutdown=shutdown,
                    config_path=tmp_path / "agent.yaml",
                    block=False,
                )
            else:
                hub_app.run_hub(
                    HubConfig(self_monitor=False, bind_port=15690, control_port=15691),
                    store,
                    shutdown=shutdown,
                    config_path=tmp_path / "hub.yaml",
                    block=False,
                )
        for sock in claimed:
            sock.close.assert_called_once()
        assert "taskpaw_ready" not in capsys.readouterr().out
        if sessions:
            assert not sessions[0].is_active()
        if failure in ("second_claim", "bootstrap"):
            assert not (tmp_path / "logs").exists()
            assert "runtime_start" not in order
        if "runtime_stop" in order:
            assert order.index("inactive") < order.index("runtime_stop")
    finally:
        store.close()


@pytest.mark.parametrize("role", ["agent", "hub"])
def test_control_revoke_failure_still_stops_runtime_and_sockets(
    role, tmp_path, monkeypatch, caplog
):
    from unittest.mock import Mock

    import uvicorn

    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.core import control, net
    from taskpaw_v3.core.config import HubConfig
    from taskpaw_v3.core.lifecycle import GracefulShutdown
    from taskpaw_v3.hub.server import app as hub_app
    from taskpaw_v3.hub.server.store import HubStore
    from taskpaw_v3.monitors import runtime

    sockets = [Mock(), Mock()]
    claims = iter(sockets)
    sessions = []
    original = control.bootstrap_control

    def bootstrap(role, base, path):
        session = original(role, base, None)
        sessions.append(session)
        return session

    def revoke(session):
        session._active.clear()
        raise control.ControlCredentialError("fixed credential failure")

    monkeypatch.setattr(launcher, "bootstrap_control", bootstrap)
    monkeypatch.setattr(control, "bootstrap_control", bootstrap)
    monkeypatch.setattr(launcher, "revoke_control", revoke)
    monkeypatch.setattr(control, "revoke_control", revoke)
    monkeypatch.setattr(launcher, "claim_port", lambda *a: next(claims))
    monkeypatch.setattr(net, "claim_port", lambda *a: next(claims))
    monkeypatch.setattr(
        launcher, "reclaim_ports_from_stale_instance", lambda *a, **kw: None
    )
    monkeypatch.setattr(net, "reclaim_ports_from_stale_instance", lambda *a, **kw: None)
    monkeypatch.setattr(uvicorn, "Server", lambda *a, **kw: Mock())
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **kw: None)
    monkeypatch.setattr(launcher, "announce_ready", lambda *a, **kw: None)
    monkeypatch.setattr(net, "announce_ready", lambda *a, **kw: None)
    sup = Mock()
    sup.stop.side_effect = lambda: not sessions[0].is_active()
    monkeypatch.setattr(runtime, "build_supervisor", lambda *a, **kw: sup)
    stopped = []
    monkeypatch.setattr(hub_app.HubService, "start", lambda self: None)

    def stop(self):
        assert not sessions[0].is_active()
        stopped.append(True)
        return True

    monkeypatch.setattr(hub_app.HubService, "stop", stop)
    store = HubStore(tmp_path / "hub.db")
    shutdown = GracefulShutdown()
    monkeypatch.setattr(shutdown, "install_signal_handlers", lambda: None)
    if role == "agent":
        launcher.run_agent(_llm_cfg(), shutdown=shutdown, block=False)
    else:
        hub_app.run_hub(
            HubConfig(self_monitor=False), store, shutdown=shutdown, block=False
        )
    shutdown.shutdown()
    assert not sessions[0].is_active()
    for sock in sockets:
        sock.close.assert_called_once()
    assert "Could not revoke" in caplog.text
    assert sessions[0].token not in caplog.text
    if role == "agent":
        sup.stop.assert_called_once()
    else:
        assert stopped == [True]
    store.close()


def _ephemeral_pair(host):
    from taskpaw_v3.core.net import claim_port

    first = claim_port(host, 0, "test read")
    try:
        second = claim_port(host, 0, "test control")
        try:
            return first.getsockname()[1], second.getsockname()[1]
        finally:
            second.close()
    finally:
        first.close()


@pytest.mark.parametrize("role", ["agent", "hub"])
@pytest.mark.parametrize(
    "when", ["already_stopped", "before_start", "during_start", "during_start_thread"]
)
def test_startup_cancellation_does_not_restart_or_leak_resources(
    role, when, tmp_path, monkeypatch, capsys, caplog
):
    """A synchronous signal can return into start(), which then acquires more.

    Real isolated children/poller threads and bound sockets must be reclaimed;
    API servers/readiness must never start after the cancellation.
    """
    import logging
    import subprocess
    import sys
    import threading
    import time

    import uvicorn

    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.core import control, net
    from taskpaw_v3.core.config import HubConfig
    from taskpaw_v3.core.lifecycle import GracefulShutdown
    from taskpaw_v3.hub.server import app as hub_app
    from taskpaw_v3.hub.server.store import HubStore
    from taskpaw_v3.monitors import runtime

    read_port, control_port = _ephemeral_pair("127.0.0.1")
    shutdown = GracefulShutdown()
    caplog.set_level(logging.INFO, logger="taskpaw.lifecycle")
    sessions, order, api_runs = [], [], []
    children, pollers = [], []
    stoppers, store_closes = [], []
    poller_stop = threading.Event()
    original_bootstrap = control.bootstrap_control

    def bootstrap(*args):
        session = original_bootstrap(args[0], args[1], None)
        sessions.append(session)
        return session

    def assert_deferred_cleanup():
        assert shutdown.is_stopping
        assert not sessions[0].is_active()
        assert not shutdown.stopped.is_set()
        assert "Graceful shutdown complete" not in caplog.text

    def stop_before_start():
        shutdown.shutdown()
        assert_deferred_cleanup()

    def start(*args):
        order.append("start")
        if when == "during_start":
            shutdown.shutdown()
            assert_deferred_cleanup()
        elif when == "during_start_thread":
            stopper = threading.Thread(
                target=shutdown.shutdown, name="test-cancel-caller"
            )
            stoppers.append(stopper)
            stopper.start()
            deadline = time.monotonic() + 3
            while not shutdown.is_stopping and time.monotonic() < deadline:
                time.sleep(0.001)
            assert shutdown.is_stopping
            while sessions[0].is_active() and time.monotonic() < deadline:
                time.sleep(0.001)
            assert_deferred_cleanup()
        assert not store_closes  # Hub must not close the DB while start can use it.
        # Deliberately acquire after cancellation, like a partially completed
        # monitor/service start. A boolean checked only before start cannot fix it.
        if role == "agent":
            children.append(
                subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
            )
        else:
            thread = threading.Thread(
                target=poller_stop.wait, name="test-cancel-poller"
            )
            pollers.append(thread)
            thread.start()
        order.append("acquired")

    def stop(*args):
        assert not sessions[0].is_active()
        order.append("stop")
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=3)
        poller_stop.set()
        for thread in pollers:
            thread.join(timeout=3)
        return True

    class Runtime:
        def snapshot(self):
            return {}

        def film_page(self, *args, **kwargs):
            return None

        def run_films(self, *args, **kwargs):
            return None

        def start(self):
            start()

        def stop(self):
            return stop()

    class Server:
        started = False
        should_exit = False

        def __init__(self, *args, **kwargs):
            pass

        def run(self, sockets):
            api_runs.append(True)

    monkeypatch.setattr(launcher, "bootstrap_control", bootstrap)
    monkeypatch.setattr(control, "bootstrap_control", bootstrap)
    monkeypatch.setattr(
        launcher, "reclaim_ports_from_stale_instance", lambda *a, **k: None
    )
    monkeypatch.setattr(net, "reclaim_ports_from_stale_instance", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "build_supervisor", lambda *a, **k: Runtime())
    monkeypatch.setattr(hub_app.HubService, "start", start)
    monkeypatch.setattr(hub_app.HubService, "stop", stop)
    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(
        shutdown,
        "install_signal_handlers",
        stop_before_start if when == "before_start" else lambda: None,
    )
    store = HubStore(tmp_path / "hub.db")
    original_close = store.close

    def close_store():
        store_closes.append(True)
        original_close()

    monkeypatch.setattr(store, "close", close_store)
    if when == "already_stopped":
        shutdown.shutdown()
    cfgargs = dict(bind_port=read_port, control_port=control_port)
    try:
        with pytest.raises(RuntimeError, match="startup"):
            if role == "agent":
                launcher.run_agent(
                    AgentConfig(
                        server_id="s", machine="m", host_metrics=False, **cfgargs
                    ),
                    shutdown=shutdown,
                    block=False,
                )
            else:
                hub_app.run_hub(
                    HubConfig(self_monitor=False, **cfgargs),
                    store,
                    shutdown=shutdown,
                    block=False,
                )
        for stopper in stoppers:
            stopper.join(timeout=3)
            assert not stopper.is_alive()
        assert shutdown.stopped.is_set()
        assert caplog.text.count("Graceful shutdown complete") == 1
        assert not sessions[0].is_active()
        assert not api_runs
        assert all(child.poll() is not None for child in children)
        assert all(not thread.is_alive() for thread in pollers)
        if when in {"already_stopped", "before_start"}:
            assert "start" not in order
        else:
            assert order.index("acquired") < order.index("stop")
        assert "taskpaw_ready" not in capsys.readouterr().out
        assert net.port_available("127.0.0.1", read_port)
        assert net.port_available("127.0.0.1", control_port)
        shutdown.shutdown()  # remains idempotent after the failed startup
        if role == "hub":
            assert store_closes == [True]
        assert order.count("stop") == (0 if when == "already_stopped" else 1)
    finally:
        stop()
        for stopper in stoppers:
            stopper.join(timeout=3)
        store.close()


def _http_status(url, token=None, *, method="GET", body=None):
    import http.client
    import json
    import time
    from urllib.parse import urlsplit

    endpoint = urlsplit(url)
    deadline = time.monotonic() + 5
    while True:
        connection = http.client.HTTPConnection(
            endpoint.hostname, endpoint.port, timeout=0.5
        )
        try:
            headers = {"Authorization": "Bearer " + token} if token else {}
            data = json.dumps(body).encode() if body is not None else None
            if data is not None:
                headers["Content-Type"] = "application/json"
            connection.request(
                method,
                endpoint.path + ("?" + endpoint.query if endpoint.query else ""),
                body=data,
                headers=headers,
            )
            response = connection.getresponse()
            response.read()
            return response.status
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.02)
        finally:
            # Close the client before stopping the listener: server-side
            # TIME_WAIT from Connection: close is not a leaked listening socket.
            connection.close()


@pytest.mark.parametrize("role", ["agent", "hub"])
@pytest.mark.parametrize("host", ["127.0.0.1", "::1"])
def test_control_real_dual_listener_descriptor_restart_and_cleanup(
    role, host, tmp_path, monkeypatch, capsys, caplog
):
    import json

    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.core import net
    from taskpaw_v3.core.config import HubConfig, save_yaml
    from taskpaw_v3.core.control import read_control_descriptor
    from taskpaw_v3.hub.server import app as hub_app
    from taskpaw_v3.hub.server.store import HubStore

    try:
        read_port, control_port = _ephemeral_pair(host)
    except OSError:
        if host == "::1":
            pytest.skip("IPv6 loopback unavailable")
        raise
    monkeypatch.setattr(
        launcher, "reclaim_ports_from_stale_instance", lambda *a, **kw: None
    )
    monkeypatch.setattr(net, "reclaim_ports_from_stale_instance", lambda *a, **kw: None)
    config_path = tmp_path / (role + ".yaml")
    descriptor_path = tmp_path / (role + ".control.json")
    previous = None
    for _ in range(2):
        monkeypatch.setenv("TASKPAW_CONTROL_TOKEN", "fake-static-env-marker")
        kwargs = dict(
            bind_host=host,
            bind_port=read_port,
            control_host=host,
            control_port=control_port,
            api_token="fake-read",
        )
        cfg = (
            AgentConfig(server_id="s", machine="m", host_metrics=False, **kwargs)
            if role == "agent"
            else HubConfig(
                self_monitor=False,
                write_status_md=False,
                data_dir=str(tmp_path),
                **kwargs,
            )
        )
        save_yaml(cfg, config_path)
        store = HubStore(tmp_path / "hub.db") if role == "hub" else None
        shutdown = (
            launcher.run_agent(cfg, config_path=config_path, block=False)
            if role == "agent"
            else hub_app.run_hub(cfg, store, config_path=config_path, block=False)
        )
        try:
            descriptor = read_control_descriptor(descriptor_path)
            expected_base = net.loopback_url(host, control_port)
            ready = next(
                json.loads(line)
                for line in capsys.readouterr().out.splitlines()
                if "taskpaw_ready" in line
            )
            assert ready == {
                "taskpaw_ready": True,
                "role": role,
                "base_url": expected_base,
                "control_credential_file": str(descriptor_path),
                "boot_id": descriptor.boot_id,
            }
            assert descriptor.base_url == expected_base
            assert descriptor.control_token != "fake-static-env-marker"
            prefix = "/control" if role == "agent" else ""
            assert (
                _http_status(
                    expected_base + prefix + "/status", descriptor.control_token
                )
                == 200
            )
            assert _http_status(expected_base + prefix + "/status", "fake-read") == 401
            assert (
                _http_status(net.loopback_url(host, read_port) + "/status", "fake-read")
                == 200
            )
            if previous is not None:
                assert descriptor.control_token != previous.control_token
                assert descriptor.boot_id != previous.boot_id
                assert (
                    _http_status(
                        expected_base + prefix + "/status", previous.control_token
                    )
                    == 401
                )
            previous = descriptor
            assert descriptor.control_token not in caplog.text
            assert descriptor.control_token not in config_path.read_text()
        finally:
            shutdown.shutdown()
        assert not descriptor_path.exists()
        assert net.port_available(host, read_port)
        assert net.port_available(host, control_port)


@pytest.mark.parametrize("role", ["agent", "hub"])
def test_stale_key_seen_by_fake_port_holder_never_authorizes_restart(
    role, tmp_path, monkeypatch
):
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.core import net
    from taskpaw_v3.core.config import HubConfig, save_yaml
    from taskpaw_v3.core.control import read_control_descriptor
    from taskpaw_v3.hub.server import app as hub_app
    from taskpaw_v3.hub.server.store import HubStore

    read_port, control_port = _ephemeral_pair("127.0.0.1")
    monkeypatch.setattr(
        launcher, "reclaim_ports_from_stale_instance", lambda *a, **kw: None
    )
    monkeypatch.setattr(net, "reclaim_ports_from_stale_instance", lambda *a, **kw: None)
    common = dict(bind_port=read_port, control_port=control_port, api_token="fake-read")
    cfg = (
        AgentConfig(server_id="s", machine="m", host_metrics=False, **common)
        if role == "agent"
        else HubConfig(
            self_monitor=False, write_status_md=False, data_dir=str(tmp_path), **common
        )
    )
    config = tmp_path / (role + ".yaml")
    descriptor_path = tmp_path / (role + ".control.json")
    save_yaml(cfg, config)
    prefix = "/control" if role == "agent" else ""
    store = HubStore(tmp_path / "hub.db") if role == "hub" else None

    def start():
        if role == "agent":
            return launcher.run_agent(cfg, config_path=config, block=False)
        return hub_app.run_hub(cfg, store, config_path=config, block=False)

    shutdown = start()
    try:
        first = read_control_descriptor(descriptor_path)
        assert (
            _http_status(first.base_url + prefix + "/status", first.control_token)
            == 200
        )
    finally:
        shutdown.shutdown()
    captured = []

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            captured.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    fake = HTTPServer(("127.0.0.1", control_port), Handler)
    thread = threading.Thread(target=fake.serve_forever, daemon=True)
    thread.start()
    try:
        assert (
            _http_status(first.base_url + prefix + "/status", first.control_token)
            == 200
        )
        assert captured == ["Bearer " + first.control_token]
    finally:
        fake.shutdown()
        fake.server_close()
        thread.join(timeout=3)
    if role == "hub":
        store = HubStore(tmp_path / "hub.db")
        store.set_config("polling_token", "initial-poll")
    shutdown = start()

    def snapshot():
        if store is None:
            return config.read_bytes(), cfg.model_dump()
        with store._lock:
            return list(store._conn.iterdump()), cfg.model_dump()

    try:
        second = read_control_descriptor(descriptor_path)
        assert second.control_token != first.control_token
        assert second.boot_id != first.boot_id
        path = second.base_url + prefix + "/config"
        body = (
            {"api_token": "updated-read"}
            if role == "agent"
            else {"polling_token": "updated-poll"}
        )
        before = snapshot()
        assert _http_status(path, first.control_token, method="PATCH", body=body) == 401
        assert snapshot() == before
        assert (
            _http_status(path, second.control_token, method="PATCH", body=body) == 200
        )
        assert snapshot() != before
        if role == "agent":
            assert cfg.api_token == "updated-read"
        else:
            assert store.get_config("polling_token") == "updated-poll"
        assert (
            _http_status(second.base_url + prefix + "/status", first.control_token)
            == 401
        )
        assert (
            _http_status(second.base_url + prefix + "/status", second.control_token)
            == 200
        )
    finally:
        shutdown.shutdown()


@pytest.mark.parametrize("role", ["agent", "hub"])
def test_api_server_startup_failure_never_announces_ready(
    role, tmp_path, monkeypatch, capsys
):
    from unittest.mock import Mock

    import uvicorn

    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.core import net
    from taskpaw_v3.core.config import HubConfig
    from taskpaw_v3.hub.server import app as hub_app
    from taskpaw_v3.hub.server.store import HubStore
    from taskpaw_v3.monitors import runtime

    read_port, control_port = _ephemeral_pair("127.0.0.1")
    monkeypatch.setattr(
        launcher, "reclaim_ports_from_stale_instance", lambda *a, **kw: None
    )
    monkeypatch.setattr(net, "reclaim_ports_from_stale_instance", lambda *a, **kw: None)
    monkeypatch.setattr(runtime, "build_supervisor", lambda *a, **kw: Mock())
    monkeypatch.setattr(hub_app.HubService, "start", lambda self: None)
    monkeypatch.setattr(hub_app.HubService, "stop", lambda self: True)

    class Server:
        started = False
        should_exit = False

        def __init__(self, *a, **kw):
            pass

        def run(self, sockets):
            raise RuntimeError("fake startup failure")

    monkeypatch.setattr(uvicorn, "Server", Server)
    cfgargs = dict(bind_port=read_port, control_port=control_port)
    store = HubStore(tmp_path / "hub.db")
    try:
        with pytest.raises(RuntimeError, match="startup failed"):
            if role == "agent":
                launcher.run_agent(
                    AgentConfig(
                        server_id="s", machine="m", host_metrics=False, **cfgargs
                    ),
                    block=False,
                )
            else:
                hub_app.run_hub(
                    HubConfig(self_monitor=False, **cfgargs), store, block=False
                )
        assert "taskpaw_ready" not in capsys.readouterr().out
        assert net.port_available("127.0.0.1", read_port)
        assert net.port_available("127.0.0.1", control_port)
    finally:
        store.close()
