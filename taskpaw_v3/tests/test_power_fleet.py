"""PWR-3 (#289): the 功耗 tab's peers (PW06) and the cluster's live power.

Peers are `{name, address}` under agent.yaml `power.peers`; the agent fetches
only `/status` and `/power/history` with its own api_token, bounded (2 s,
256 KB), never through a proxy or a redirect, never from a public address.
A background refresh keeps the cache; routes never wait on a peer. Network
access in these tests goes only to a fake server on 127.0.0.1.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from taskpaw_v3.agent.server.admin import MonitorAdmin
from taskpaw_v3.core import power, power_fleet
from taskpaw_v3.core.config import AgentConfig, PowerPeer, PowerSettings, load_yaml
from taskpaw_v3.core.power_fleet import PeerError, PowerFleet, fetch_json
from taskpaw_v3.tests.test_admin import _agent_config, _registry

TOKEN = "shared-polling-token"
LIVE = {
    "w": 172.0,
    "gpu_w": 118.0,
    "cpu_w": 24.0,
    "other_w": 30.0,
    "estimated": True,
    "today_wh": 1920.0,
    "week_wh": 9630.0,
    "month_wh": 54900.0,
    "activity": "asr",
}


# ── addresses ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "address, expected",
    [
        ("192.168.1.20", ("192.168.1.20", 5680)),
        ("192.168.1.20:5690", ("192.168.1.20", 5690)),
        ("gpu-02.local", ("gpu-02.local", 5680)),
        ("gpu-02:5680", ("gpu-02", 5680)),
        ("[fe80::1]:5680", ("fe80::1", 5680)),
        ("fd00::20", ("fd00::20", 5680)),
        (" 10.0.0.5 ", ("10.0.0.5", 5680)),
    ],
)
def test_peer_addresses_are_host_and_port(address, expected):
    assert power_fleet.parse_address(address) == expected


@pytest.mark.parametrize(
    "address",
    [
        "",
        "http://192.168.1.20:5680",
        "192.168.1.20/status",
        "user@192.168.1.20",
        "192.168.1.20:0",
        "192.168.1.20:99999",
        "192.168.1.20:abc",
        "0.0.0.0",
        "[::]",
        "8.8.8.8",  # public: refused (bind_is_global)
        "[2001:4860:4860::8888]:5680",
        "host name",
        "192.168.1.20?x=1",
    ],
)
def test_bad_or_public_peer_addresses_are_refused(address):
    with pytest.raises(ValueError):
        power_fleet.parse_address(address)


def test_peer_urls_use_only_the_fixed_paths():
    assert power_fleet.peer_url("10.0.0.5", "/status") == "http://10.0.0.5:5680/status"
    assert (
        power_fleet.peer_url("[fd00::20]:5681", "/power/history?range=7d")
        == "http://[fd00::20]:5681/power/history?range=7d"
    )


def test_settings_validate_the_peer_list():
    s = PowerSettings(peers=[{"name": " GPU-02 ", "address": "192.168.1.20"}])
    assert s.peers == [PowerPeer(name="GPU-02", address="192.168.1.20")]
    with pytest.raises(ValueError):
        PowerSettings(peers=[{"name": "x", "address": "8.8.8.8"}])
    with pytest.raises(ValueError):
        PowerSettings(
            peers=[
                {"name": "x", "address": "10.0.0.1"},
                {"name": "x", "address": "10.0.0.2"},
            ]
        )
    with pytest.raises(ValueError):
        PowerSettings(peers=[{"name": "", "address": "10.0.0.1"}])
    with pytest.raises(ValueError):
        PowerSettings(
            peers=[{"name": f"m{i}", "address": f"10.0.0.{i}"} for i in range(17)]
        )


def test_the_peer_list_is_saved_live_from_the_dialog(tmp_path):
    cfg = _agent_config(power={"other_w": 40})
    path = tmp_path / "agent.yaml"
    admin = MonitorAdmin(cfg, None, _registry(), path)
    peers = [{"name": "GPU-02", "address": "192.168.1.20"}]
    assert admin.update_config({"power": {"peers": peers}})["ok"] is True
    assert power.get_power_settings().peers == [PowerPeer(**peers[0])]
    assert power.get_power_settings().other_w == 40  # a partial patch
    assert load_yaml(AgentConfig, path).power.peers == [PowerPeer(**peers[0])]
    with pytest.raises(ValueError):
        admin.update_config({"power": {"peers": [{"name": "w", "address": "1.1.1.1"}]}})
    assert power.get_power_settings().peers == [PowerPeer(**peers[0])]


# ── one bounded GET against a fake peer on 127.0.0.1 ───────────────────────
class FakePeer:
    """A local HTTP server answering per path; records the headers it saw."""

    def __init__(self):
        self.routes: dict[str, tuple[int, bytes, dict]] = {}
        self.seen: list[tuple[str, dict]] = []
        self.delay = 0.0
        peer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                peer.seen.append((self.path, dict(self.headers)))
                if peer.delay:
                    time.sleep(peer.delay)
                code, body, headers = peer.routes.get(self.path, (404, b"{}", {}))
                self.send_response(code)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def json(self, path: str, value, code: int = 200, headers=None):
        self.routes[path] = (code, json.dumps(value).encode(), headers or {})

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def peer():
    value = FakePeer()
    try:
        yield value
    finally:
        value.close()


def test_fetch_sends_this_agents_token_and_reads_json(peer):
    peer.json("/status", {"machine": "GPU-02", "power": LIVE})
    assert fetch_json(peer.url("/status"), TOKEN)["machine"] == "GPU-02"
    ((path, headers),) = peer.seen
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_fetch_ignores_proxy_settings(peer, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    peer.json("/status", {"ok": True})
    assert fetch_json(peer.url("/status"), TOKEN) == {"ok": True}


@pytest.mark.parametrize(
    "code, error",
    [
        (401, "unauthorized"),
        (403, "unauthorized"),
        (404, "not_found"),
        (500, "http_error"),
    ],
)
def test_fetch_maps_http_errors_to_codes(peer, code, error):
    peer.json("/status", {"detail": "secret upstream text"}, code=code)
    with pytest.raises(PeerError) as caught:
        fetch_json(peer.url("/status"), TOKEN)
    assert caught.value.code == error
    assert "secret" not in str(caught.value)


def test_fetch_refuses_a_redirect(peer):
    peer.routes["/status"] = (302, b"", {"Location": "http://8.8.8.8/status"})
    with pytest.raises(PeerError) as caught:
        fetch_json(peer.url("/status"), TOKEN)
    assert caught.value.code == "http_error"
    assert [p for p, _ in peer.seen] == ["/status"]


def test_fetch_caps_the_body_at_256_kb(peer):
    big = b'{"x":"' + b"a" * (256 * 1024) + b'"}'
    peer.routes["/status"] = (200, big, {})
    with pytest.raises(PeerError) as caught:
        fetch_json(peer.url("/status"), TOKEN)
    assert caught.value.code == "too_large"


def test_fetch_gives_up_after_its_timeout(peer):
    peer.json("/status", {"ok": True})
    peer.delay = 1.5
    started = time.monotonic()
    with pytest.raises(PeerError) as caught:
        fetch_json(peer.url("/status"), TOKEN, timeout=0.3)
    assert caught.value.code in ("unreachable", "timeout")
    assert time.monotonic() - started < 1.4


def test_fetch_reports_a_closed_port_and_bad_json(peer):
    peer.routes["/status"] = (200, b"not json", {})
    with pytest.raises(PeerError) as caught:
        fetch_json(peer.url("/status"), TOKEN)
    assert caught.value.code == "invalid"
    closed = peer.port
    peer.close()
    with pytest.raises(PeerError) as caught:
        fetch_json(f"http://127.0.0.1:{closed}/status", TOKEN, timeout=0.5)
    assert caught.value.code == "unreachable"


# ── the cache ──────────────────────────────────────────────────────────────
class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class Peers:
    """`fetch` answers by a part of the URL; a str answer raises PeerError."""

    def __init__(self, answers):
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url, token):
        self.calls.append((url, token))
        for suffix, value in self.answers.items():
            if suffix in url:
                if isinstance(value, str):
                    raise PeerError(value)
                return value
        raise PeerError("not_found")


def fleet(peers, answers, token=TOKEN):
    clock = Clock()
    fetch = Peers(answers)
    value = PowerFleet(
        lambda: [SimpleNamespace(**p) for p in peers],
        lambda: token,
        fetch=fetch,
        clock=clock,
        wall=lambda: 1_800_000_000.0,
    )
    return value, fetch, clock


_ACTS = ("restore", "asr", "translate", "idle", "other")
HISTORY = {
    "range": "24h",
    "step_s": 900,
    "estimated": True,
    "t": [0, 900],
    "covered_s": [900.0, 0.0],
    "avg_w": [100.0, None],
    "wh": [25.0, 0.0],
    "gpu_wh": [15.0, 0.0],
    "cpu_wh": [5.0, 0.0],
    "other_wh": [5.0, 0.0],
    "activity_wh": {k: [5.0, 0.0] for k in _ACTS},
    "totals": {"wh": 25.0, "activity_wh": {k: 5.0 for k in _ACTS}},
}
GPU02 = {"name": "GPU-02", "address": "192.168.1.20"}


def test_a_peer_with_power_is_cached_with_its_history():
    f, fetch, _clock = fleet(
        [GPU02],
        {
            "/status": {"machine": "GPU-02", "version": "3.9.18", "power": LIVE},
            "range=24h": HISTORY,
            "range=7d": {**HISTORY, "range": "7d"},
            "range=30d": {**HISTORY, "range": "30d"},
        },
    )
    assert f.snapshot()[0]["state"] == "pending"  # before the first refresh
    f.refresh_once()
    (row,) = f.snapshot("7d")
    assert row["state"] == "ok" and row["machine"] == "GPU-02"
    assert row["power"]["w"] == 172.0 and row["power"]["activity"] == "asr"
    assert row["history"]["range"] == "7d"
    assert row["stale"] is False and row["age_s"] == 0.0
    assert {token for _, token in fetch.calls} == {TOKEN}
    assert {url.split("5680")[1] for url, _ in fetch.calls} == {
        "/status",
        "/power/history?range=24h",
        "/power/history?range=7d",
        "/power/history?range=30d",
    }


def test_histories_refresh_once_a_minute_and_status_every_cycle():
    f, fetch, clock = fleet([GPU02], {"/status": {"power": LIVE}, "range=": HISTORY})
    f.refresh_once()
    clock.now += 5
    f.refresh_once()
    statuses = [u for u, _ in fetch.calls if u.endswith("/status")]
    histories = [u for u, _ in fetch.calls if "history" in u]
    assert len(statuses) == 2 and len(histories) == 3
    clock.now += 60
    f.refresh_once()
    assert len([u for u, _ in fetch.calls if "history" in u]) == 6


@pytest.mark.parametrize(
    "answers, state",
    [
        ({"/status": {"machine": "old"}}, "no_power"),  # an older TaskPaw
        ({"/status": {"power": None}}, "off"),  # sampling turned off
        ({"/status": {"power": LIVE}, "range=24h": "not_found"}, "no_power"),
        ({"/status": "unreachable"}, "unreachable"),
        ({"/status": "unauthorized"}, "unauthorized"),
        ({"/status": ["not", "an", "object"]}, "invalid"),
    ],
)
def test_peer_states(answers, state):
    f, _fetch, _clock = fleet([GPU02], answers)
    f.refresh_once()
    (row,) = f.snapshot()
    assert row["state"] == state
    if state != "ok":
        assert row["power"] is None


def test_a_public_address_saved_by_hand_is_never_fetched():
    f, fetch, _clock = fleet([{"name": "wan", "address": "8.8.8.8"}], {})
    f.refresh_once()
    assert fetch.calls == []
    assert f.snapshot()[0]["state"] == "invalid_address"


def test_removed_peers_are_dropped():
    peers = [GPU02, {"name": "GPU-03", "address": "192.168.1.30"}]
    f, _fetch, _clock = fleet(peers, {"/status": {"power": LIVE}, "range=": HISTORY})
    f.refresh_once()
    assert len(f.snapshot()) == 2
    del peers[1]
    f.refresh_once()
    assert [row["name"] for row in f.snapshot()] == ["GPU-02"]


def test_a_route_reads_the_cache_and_never_waits_on_a_peer():
    gate = threading.Event()
    entered = threading.Event()

    def slow(url, token):
        entered.set()
        gate.wait(5)
        raise PeerError("unreachable")

    f = PowerFleet(lambda: [SimpleNamespace(**GPU02)], lambda: TOKEN, fetch=slow)
    worker = threading.Thread(target=f.refresh_once)
    worker.start()
    try:
        assert entered.wait(5)
        started = time.monotonic()
        (row,) = f.snapshot()
        assert time.monotonic() - started < 0.5
        assert row["state"] == "pending"
    finally:
        gate.set()
        worker.join(5)


def test_the_refresher_thread_starts_and_stops():
    f = PowerFleet(lambda: [], lambda: TOKEN, fetch=Peers({}), interval=0.05)
    f.start()
    time.sleep(0.1)
    assert f.stop(5) is True


def test_a_peer_is_stale_when_it_stops_answering():
    f, _fetch, clock = fleet([GPU02], {"/status": {"power": LIVE}, "range=": HISTORY})
    f.refresh_once()
    clock.now += power_fleet.STALE_SECONDS + 1
    (row,) = f.snapshot()
    assert row["stale"] is True and row["power"]["w"] == 172.0


# ── the cluster's live power (媒体处理中心 集群功耗) ────────────────────────
class _LocalSampler:
    def live(self):
        return {
            "w": 286.0,
            "gpu_w": 210.0,
            "cpu_w": 46.0,
            "other_w": 30.0,
            "today_wh": 4550.0,
            "estimated": True,
        }


def test_cluster_power_sums_this_machine_and_fresh_peers():
    assert power_fleet.cluster_power() is None  # nothing sampled
    power.set_sampler(_LocalSampler())  # type: ignore[arg-type]
    assert power_fleet.cluster_power()["w"] == 286.0
    f, _fetch, clock = fleet([GPU02], {"/status": {"power": LIVE}, "range=": HISTORY})
    f.refresh_once()
    power_fleet.set_fleet(f)
    value = power_fleet.cluster_power()
    assert value["w"] == pytest.approx(458.0)
    assert value["gpu_w"] == pytest.approx(328.0)
    assert value["today_wh"] == pytest.approx(6470.0)
    assert value["machines"] == 2 and value["estimated"] is True
    clock.now += power_fleet.STALE_SECONDS + 1  # a silent peer is not summed
    assert power_fleet.cluster_power()["machines"] == 1


# ── launcher: the control view carries the peers ────────────────────────────
def test_the_launcher_runs_the_refresher_and_the_view_lists_peers(
    monkeypatch, tmp_path
):
    from taskpaw_v3.tests.test_power_routes import _run_agent

    cfg = AgentConfig(
        server_id="s",
        machine="HOME-PC",
        host_metrics=False,
        api_token=TOKEN,
        power={"peers": [GPU02]},
    )
    seen: list = []

    def fake_fetch(url, token, timeout=2.0, opener=None):
        seen.append((url, token))
        raise PeerError("unreachable")

    monkeypatch.setattr(power_fleet, "fetch_json", fake_fetch)
    shutdown, _order, kwargs = _run_agent(monkeypatch, cfg, tmp_path)
    try:
        fleet_ = power_fleet.get_fleet()
        assert fleet_ is not None
        assert fleet_._own() == ("s", "HOME-PC")  # IR06: knows this agent
        fleet_.refresh_once()
        view = kwargs["control"]["power_view"]("7d")
        assert view["machine"]["name"] == "HOME-PC"
        (row,) = view["peers"]
        assert row["name"] == "GPU-02" and row["state"] == "unreachable"
        assert seen and all(token == TOKEN for _, token in seen)
    finally:
        shutdown.shutdown()
    assert power_fleet.get_fleet() is None


def test_a_peer_that_stops_answering_has_no_current_figure():
    answers = {"/status": {"power": LIVE}, "range=": HISTORY}
    f, _fetch, _clock = fleet([GPU02], answers)
    f.refresh_once()
    answers["/status"] = "unreachable"
    f.refresh_once()
    (row,) = f.snapshot()
    assert row["state"] == "unreachable" and row["power"] is None
    assert row["history"] == HISTORY  # what was read stays history


# ── scheduling and shutdown (Codex PWR-2/3) ─────────────────────────────────
def wait_until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_stop_cancels_queued_peers_and_returns_within_its_budget():
    gate = threading.Event()
    started: list[float] = []
    lock = threading.Lock()

    def slow(url, token):
        with lock:
            started.append(time.monotonic())
        gate.wait(1.0)  # a peer that answers late
        raise PeerError("unreachable")

    peers = [SimpleNamespace(name=f"m{i}", address=f"10.0.0.{i + 1}") for i in range(8)]
    f = PowerFleet(lambda: peers, lambda: TOKEN, fetch=slow, interval=0.05)
    f.start()
    try:
        assert wait_until(lambda: len(started) >= 4)  # every worker is busy
        stopped = time.monotonic()
        assert f.stop(5) is True
        assert time.monotonic() - stopped < 3.0
        assert all(at < stopped for at in started)  # nothing started after stop
        count = len(started)
        time.sleep(0.2)
        assert len(started) == count
    finally:
        gate.set()
    f.refresh_once()  # after stop: a no-op, never an error
    assert len(started) == count


def test_a_slow_history_never_delays_the_status_refresh():
    gate = threading.Event()
    statuses: list[str] = []

    def fetch(url, token):
        if url.endswith("/status"):
            statuses.append(url)
            return {"power": LIVE}
        gate.wait(5)  # every history hangs
        return HISTORY

    clock = Clock()
    f = PowerFleet(
        lambda: [SimpleNamespace(**GPU02)], lambda: TOKEN, fetch=fetch, clock=clock
    )
    try:
        f.refresh_once(wait=False)
        assert wait_until(lambda: len(statuses) == 1)
        clock.now += 5

        def cycle():  # the loop's next cycles, the histories still hanging
            f.refresh_once(wait=False)
            return len(statuses) >= 2

        assert wait_until(cycle)
        assert f.snapshot()[0]["state"] == "ok"
    finally:
        gate.set()
        assert f.stop(5) is True


def test_a_failing_history_is_retried_with_a_backoff():
    f, fetch, clock = fleet(
        [GPU02],
        {"/status": {"power": LIVE}, "range=24h": "unreachable", "range=": HISTORY},
    )
    for _ in range(121):  # ten minutes of 5 s cycles
        f.refresh_once()
        clock.now += 5
    failing = [u for u, _ in fetch.calls if "range=24h" in u]
    healthy = [u for u, _ in fetch.calls if "range=7d" in u]
    statuses = [u for u, _ in fetch.calls if u.endswith("/status")]
    assert len(statuses) == 121
    assert len(healthy) == 11  # once a minute
    # 60 s, then 120 s, then 240 s, then at most 5 min apart: 100, 160, 280, 520
    assert len(failing) == 4
    assert f.snapshot("7d")[0]["history"] == HISTORY


@pytest.mark.parametrize(
    "history",
    [
        {"t": [0, 900], "avg_w": [1.0]},  # unequal lengths
        {"t": list(range(169)), "avg_w": [1.0] * 169},  # more than 168 points
        {"t": [0, 900], "avg_w": [1.0, "2"]},
        {"t": [0, "900"], "avg_w": [1.0, 2.0]},
        {"t": [0, 900], "avg_w": [True, 2.0]},
        {"t": [0, None], "avg_w": [1.0, 2.0]},
        {"t": [0, 900], "avg_w": None},
        {"t": [0, 900], "avg_w": [float("nan"), 1.0]},
        [1, 2],
        # Codex r2: the whole shape, not only t / avg_w — a UI reads totals
        {"t": [0], "avg_w": [100]},
        {k: v for k, v in HISTORY.items() if k != "totals"},
        {k: v for k, v in HISTORY.items() if k != "activity_wh"},
        {k: v for k, v in HISTORY.items() if k != "wh"},
        {**HISTORY, "wh": [25.0]},  # unequal length
        {**HISTORY, "gpu_wh": [15.0, "x"]},
        {**HISTORY, "cpu_wh": None},
        {**HISTORY, "totals": {"wh": 25.0}},  # no activity totals
        {
            **HISTORY,
            "totals": {"wh": "25", "activity_wh": HISTORY["totals"]["activity_wh"]},
        },
        {**HISTORY, "totals": {"wh": 25.0, "activity_wh": {"restore": 5.0}}},
        {
            **HISTORY,
            "totals": {
                "wh": 25.0,
                "activity_wh": {**HISTORY["totals"]["activity_wh"], "asr": None},
            },
        },
        {
            **HISTORY,
            "activity_wh": {k: [5.0, 0.0] for k in _ACTS[:-1]},
        },  # a key missing
        {**HISTORY, "activity_wh": {**HISTORY["activity_wh"], "idle": [5.0]}},
        {**HISTORY, "activity_wh": []},
    ],
)
def test_an_invalid_peer_history_is_treated_as_unavailable(history):
    f, _fetch, _clock = fleet([GPU02], {"/status": {"power": LIVE}, "range=": history})
    f.refresh_once()
    (row,) = f.snapshot()
    assert row["state"] == "ok" and row["history"] is None


def test_a_valid_peer_history_keeps_nulls_as_gaps():
    f, _fetch, _clock = fleet([GPU02], {"/status": {"power": LIVE}, "range=": HISTORY})
    f.refresh_once()
    assert f.snapshot()[0]["history"]["avg_w"] == [100.0, None]


def test_a_peer_that_is_this_agent_is_not_counted_twice():
    clock = Clock()
    fetch = Peers({"/status": {"machine": "HOME-PC", "server_id": "s", "power": LIVE}})
    f = PowerFleet(
        lambda: [SimpleNamespace(**GPU02)],
        lambda: TOKEN,
        fetch=fetch,
        clock=clock,
        own=lambda: ("s", "HOME-PC"),
    )
    f.refresh_once()
    (row,) = f.snapshot()
    assert row["state"] == "self" and row["power"] is None and row["history"] is None
    assert [u for u, _ in fetch.calls if "history" in u] == []
    power_fleet.set_fleet(f)
    assert power_fleet.cluster_power() is None  # nothing local, the peer skipped


def test_another_machine_with_its_own_server_id_is_counted():
    fetch = Peers(
        {
            "/status": {"machine": "GPU-02", "server_id": "g2", "power": LIVE},
            "range=": HISTORY,
        }
    )
    f = PowerFleet(
        lambda: [SimpleNamespace(**GPU02)],
        lambda: TOKEN,
        fetch=fetch,
        clock=Clock(),
        own=lambda: ("s", "HOME-PC"),
    )
    f.refresh_once()
    assert f.snapshot()[0]["state"] == "ok"


# ── a hostname is resolved once and checked (IR04) ──────────────────────────
def test_a_hostname_resolving_to_a_public_address_is_never_contacted(peer, monkeypatch):
    peer.json("/status", {"ok": True})
    monkeypatch.setattr(
        power_fleet,
        "_getaddrinfo",
        lambda host, port, **kw: [(2, 1, 6, "", ("8.8.8.8", port))],
    )
    with pytest.raises(PeerError) as caught:
        fetch_json(f"http://gpu-02.lan:{peer.port}/status", TOKEN)
    assert caught.value.code == "invalid_address"
    assert peer.seen == []


def test_a_hostname_is_fetched_at_the_address_it_resolved_to(peer, monkeypatch):
    peer.json("/status", {"ok": True})
    calls = []

    def resolve(host, port, **kw):
        calls.append(host)
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(power_fleet, "_getaddrinfo", resolve)
    assert fetch_json(f"http://gpu-02.lan:{peer.port}/status", TOKEN) == {"ok": True}
    assert calls == ["gpu-02.lan"]  # one resolution, then that address
    ((_path, headers),) = peer.seen
    assert headers["Host"] == f"gpu-02.lan:{peer.port}"


def test_a_hanging_resolver_is_bounded(monkeypatch):
    release = threading.Event()

    def hang(host, port, **kw):
        release.wait(5)
        return [(2, 1, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(power_fleet, "_getaddrinfo", hang)
    started = time.monotonic()
    try:
        with pytest.raises(PeerError) as caught:
            fetch_json("http://gpu-02.lan:5680/status", TOKEN, timeout=0.3)
        assert caught.value.code == "unreachable"
        assert time.monotonic() - started < 1.5
    finally:
        release.set()


# ── Codex r2: recovery, bounded backoff ─────────────────────────────────────
def test_a_peer_that_gets_the_history_after_a_404_recovers_without_a_restart():
    answers = {"/status": {"power": LIVE}, "range=": "not_found"}
    f, fetch, clock = fleet([GPU02], answers)
    f.refresh_once()
    (row,) = f.snapshot()
    assert row["state"] == "no_power" and row["power"] is None
    clock.now += 5
    f.refresh_once()  # still inside the backoff: no history request, still no_power
    assert f.snapshot()[0]["state"] == "no_power"
    answers["range="] = HISTORY  # the peer was upgraded
    clock.now += 60
    f.refresh_once()  # the probe now succeeds and clears the flag
    clock.now += 5
    f.refresh_once()  # the next status is trusted again
    (row,) = f.snapshot()
    assert row["state"] == "ok" and row["power"]["w"] == 172.0
    assert row["history"] == HISTORY


def test_a_peer_still_without_the_history_is_probed_at_the_backoff_cadence():
    f, fetch, clock = fleet(
        [GPU02], {"/status": {"power": LIVE}, "range=": "not_found"}
    )
    for _ in range(121):  # ten minutes of 5 s cycles
        f.refresh_once()
        clock.now += 5
    probes = [u for u, _ in fetch.calls if "range=24h" in u]
    assert len(probes) == 4  # 60 s, 120 s, 240 s, then at most 5 minutes apart
    assert f.snapshot()[0]["state"] == "no_power"


def test_a_long_failing_history_keeps_the_five_minute_cap():
    f, _fetch, clock = fleet(
        [GPU02], {"/status": {"power": LIVE}, "range=": "unreachable"}
    )
    f.refresh_once()  # creates the entry
    f._update(("GPU-02", "192.168.1.20"), history_fails={"24h": 2000})
    clock.now += 600
    now = clock.now
    f.refresh_once()
    entry = f._cache[("GPU-02", "192.168.1.20")]
    assert entry["history_fails"]["24h"] == 2001
    assert entry["history_due"]["24h"] == now + power_fleet.HISTORY_RETRY_MAX


# ── Codex r3: offline peers' period energy, absolute deadline, duplicates ───
class Wall:
    def __init__(self, now=1_800_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def wall_fleet(peers, answers, wall, **kw):
    clock = Clock()
    fetch = Peers(answers)
    value = PowerFleet(
        lambda: [SimpleNamespace(**p) for p in peers],
        lambda: TOKEN,
        fetch=fetch,
        clock=clock,
        wall=wall,
        **kw,
    )
    return value, fetch, clock


DAY = 86400.0


def test_an_offline_peers_period_energy_is_kept_until_the_period_rolls_over():
    wall = Wall()
    answers = {"/status": {"power": LIVE}, "range=": HISTORY}
    f, _fetch, _clock = wall_fleet([GPU02], answers, wall)
    f.refresh_once()
    (row,) = f.snapshot()
    assert row["energy"] == {
        "today_wh": 1920.0,
        "week_wh": 9630.0,
        "month_wh": 54900.0,
    }
    assert row["energy_stale"] is False
    answers["/status"] = "unreachable"
    f.refresh_once()
    (row,) = f.snapshot()
    assert row["power"] is None  # live W stays excluded
    assert row["energy"]["today_wh"] == 1920.0 and row["energy_stale"] is True
    wall.now += DAY  # the next day: today is over, the week and month are not
    (row,) = f.snapshot()
    assert row["energy"] == {
        "today_wh": None,
        "week_wh": 9630.0,
        "month_wh": 54900.0,
    }
    wall.now += 4 * DAY  # a new week
    assert f.snapshot()[0]["energy"] == {
        "today_wh": None,
        "week_wh": None,
        "month_wh": 54900.0,
    }
    wall.now += 20 * DAY  # a new month
    assert f.snapshot()[0]["energy"] == {
        "today_wh": None,
        "week_wh": None,
        "month_wh": None,
    }


def test_a_silent_or_off_peer_keeps_its_energy_marked_stale():
    wall = Wall()
    answers = {"/status": {"power": LIVE}, "range=": HISTORY}
    f, _fetch, clock = wall_fleet([GPU02], answers, wall)
    f.refresh_once()
    clock.now += power_fleet.STALE_SECONDS + 1  # still "ok", but silent
    (row,) = f.snapshot()
    assert row["stale"] is True and row["energy_stale"] is True
    assert row["energy"]["today_wh"] == 1920.0
    answers["/status"] = {"power": None}  # sampling turned off meanwhile
    f.refresh_once()
    (row,) = f.snapshot()
    assert row["state"] == "off" and row["energy"]["week_wh"] == 9630.0
    assert row["energy_stale"] is True


def test_a_peer_never_seen_or_this_agent_has_no_energy():
    f, _fetch, _clock = fleet([GPU02], {"/status": "unreachable"})
    f.refresh_once()
    assert f.snapshot()[0]["energy"] is None
    own = Peers({"/status": {"machine": "HOME-PC", "server_id": "s", "power": LIVE}})
    g = PowerFleet(
        lambda: [SimpleNamespace(**GPU02)],
        lambda: TOKEN,
        fetch=own,
        clock=Clock(),
        own=lambda: ("s", "HOME-PC"),
    )
    g.refresh_once()
    assert g.snapshot()[0]["energy"] is None


def test_cluster_today_keeps_an_offline_peers_energy_marked_partial():
    power.set_sampler(_LocalSampler())  # type: ignore[arg-type]
    answers = {"/status": {"power": LIVE}, "range=": HISTORY}
    f, _fetch, _clock = wall_fleet([GPU02], answers, Wall())
    f.refresh_once()
    power_fleet.set_fleet(f)
    assert "today_partial" not in power_fleet.cluster_power()
    answers["/status"] = "unreachable"
    f.refresh_once()
    value = power_fleet.cluster_power()
    assert value["w"] == 286.0 and value["machines"] == 1  # live W excluded
    assert value["today_wh"] == pytest.approx(4550.0 + 1920.0)
    assert value["today_partial"] is True


class TricklePeer:
    """A TCP server that answers one request slowly: a header line every
    `gap` seconds (or, with `body`, a complete header then one body byte)."""

    def __init__(self, gap=0.02, body=False, lifetime=4.0):
        self.gap, self.body, self.lifetime = gap, body, lifetime
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.requests = 0
        self.closing = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self.closing:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.requests += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            conn.settimeout(2.0)
            data = b""
            while b"\r\n\r\n" not in data:
                data += conn.recv(4096)
            if self.body:
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 1000\r\n\r\n")
            else:
                conn.sendall(b"HTTP/1.1 200 OK\r\n")
            end = time.monotonic() + self.lifetime
            while time.monotonic() < end and not self.closing:
                conn.sendall(b"a" if self.body else b"X-Pad: a\r\n")
                time.sleep(self.gap)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.closing = True
        self.sock.close()


@pytest.fixture
def trickle():
    made = []

    def make(**kw):
        value = TricklePeer(**kw)
        made.append(value)
        return value

    yield make
    for value in made:
        value.close()


@pytest.mark.parametrize("body", [False, True])
def test_a_trickling_peer_cannot_outlive_the_request_deadline(trickle, body):
    peer_ = trickle(body=body)
    started = time.monotonic()
    with pytest.raises(PeerError) as caught:
        fetch_json(f"http://127.0.0.1:{peer_.port}/status", TOKEN, timeout=0.05)
    assert time.monotonic() - started < 0.4
    assert caught.value.code == "timeout"


def test_stop_does_not_wait_on_a_trickling_peer_beyond_its_budget(trickle):
    peer_ = trickle()
    f = PowerFleet(
        lambda: [SimpleNamespace(name="slow", address=f"127.0.0.1:{peer_.port}")],
        lambda: TOKEN,
        fetch=lambda u, t: fetch_json(u, t, timeout=0.2),
        interval=0.05,
    )
    f.start()
    assert wait_until(lambda: peer_.requests >= 1)
    started = time.monotonic()
    assert f.stop(2.0) is True
    assert time.monotonic() - started < 1.0


def test_two_aliases_of_one_machine_are_counted_once():
    status = {"machine": "GPU-02", "server_id": "g2", "power": LIVE}
    alias = {"name": "GPU-02 (wifi)", "address": "192.168.1.21"}
    f, fetch, _clock = wall_fleet(
        [GPU02, alias], {"/status": status, "range=": HISTORY}, Wall()
    )
    f.refresh_once()
    first, second = f.snapshot()
    assert first["state"] == "ok" and first["duplicate_of"] is None
    assert second["state"] == "duplicate" and second["duplicate_of"] == "GPU-02"
    assert second["power"] is None and second["energy"] is None
    assert second["history"] is None
    power_fleet.set_fleet(f)
    power.set_sampler(_LocalSampler())  # type: ignore[arg-type]
    value = power_fleet.cluster_power()
    assert value["machines"] == 2  # this machine and one GPU-02
    assert value["w"] == pytest.approx(458.0)
    assert value["today_wh"] == pytest.approx(6470.0)


def test_the_first_configured_entry_wins_whatever_answers_first():
    status = {"machine": "GPU-02", "power": LIVE}  # no server_id: the name
    alias = {"name": "alias", "address": "192.168.1.21"}
    for order in ([GPU02, alias], [alias, GPU02]):
        f, _fetch, _clock = wall_fleet(
            order, {"/status": status, "range=": HISTORY}, Wall()
        )
        f.refresh_once()
        rows = f.snapshot()
        assert [r["state"] for r in rows] == ["ok", "duplicate"]
        assert rows[1]["duplicate_of"] == rows[0]["name"]


def test_different_machines_are_not_duplicates():
    other = {"name": "GPU-03", "address": "192.168.1.30"}
    f, _fetch, _clock = fleet(
        [GPU02, other], {"/status": {"power": LIVE}, "range=": HISTORY}
    )
    f.refresh_once()
    # no identity reported at all: never merged by guesswork
    assert [r["state"] for r in f.snapshot()] == ["ok", "ok"]
    answers = Peers(
        {
            "192.168.1.20": {"machine": "SAME", "server_id": "a", "power": LIVE},
            "192.168.1.30": {"machine": "SAME", "server_id": "b", "power": LIVE},
            "range=": HISTORY,
        }
    )
    g = PowerFleet(
        lambda: [SimpleNamespace(**GPU02), SimpleNamespace(**other)],
        lambda: TOKEN,
        fetch=answers,
        clock=Clock(),
    )
    g.refresh_once()
    assert [r["state"] for r in g.snapshot()] == ["ok", "ok"]  # two server ids


def test_history_is_read_from_a_peer_that_reports_no_live_power():
    answers = {"/status": {"machine": "GPU-02", "power": None}, "range=": HISTORY}
    f, fetch, clock = fleet([GPU02], answers)
    f.refresh_once()
    (row,) = f.snapshot("7d")
    assert row["state"] == "off" and row["power"] is None
    assert row["history"] == HISTORY
    assert len([u for u, _ in fetch.calls if "history" in u]) == 3
    clock.now += 60
    f.refresh_once()
    assert len([u for u, _ in fetch.calls if "history" in u]) == 6


def test_an_off_peer_without_the_history_endpoint_backs_off():
    f, fetch, clock = fleet(
        [GPU02], {"/status": {"power": None}, "range=": "not_found"}
    )
    for _ in range(121):  # ten minutes of 5 s cycles
        f.refresh_once()
        clock.now += 5
    probes = [u for u, _ in fetch.calls if "range=24h" in u]
    assert len(probes) == 4  # 60 s, 120 s, 240 s, then at most 5 minutes apart
    assert f.snapshot()[0]["state"] == "off"  # still reports no live power


def test_a_peer_without_power_support_is_not_asked_for_history():
    f, fetch, _clock = fleet([GPU02], {"/status": {"machine": "old"}})
    f.refresh_once()
    assert [u for u, _ in fetch.calls if "history" in u] == []
