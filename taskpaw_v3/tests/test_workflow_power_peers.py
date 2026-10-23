"""P2-4 (#295) R8b/R11: enrolled workers as automatic power peers, and the
status channel of 判定已停止 (`PowerFleet.quiet`).

The controller still only GETs `/status` and `/power/history`; it never
reads `/events`. A configured peer wins over a worker at the same address.
Every `/status` read is a status-channel observation: unreachable, timeout
or `workflow_worker.busy == false` keep the worker quiet; any other answer
(401, 404, an invalid body, no `busy`) means the machine is up."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from taskpaw_v3.core import power_fleet as pf
from taskpaw_v3.core.config import PowerPeer
from taskpaw_v3.tests.test_workflow_local_dispatch import add_film, runtime
from taskpaw_v3.tests.workflow_route_helpers import (
    claim_remote,
    enrol_remote,
    route,
)
from taskpaw_v3.workflow import devices


class Clock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


class Fetch:
    """Per URL: a dict answer or a PeerError code; records every URL."""

    def __init__(self) -> None:
        self.answers: dict[str, object] = {}
        self.urls: list[str] = []

    def __call__(self, url: str, token: str):
        self.urls.append(url)
        for prefix, answer in self.answers.items():
            if url.startswith(prefix):
                if isinstance(answer, str):
                    raise pf.PeerError(answer)
                if isinstance(answer, pf.PeerError):
                    raise answer
                return answer
        raise pf.PeerError("unreachable")


def fleet_of(peers, fetch, clock):
    return pf.PowerFleet(
        lambda: peers,
        lambda: "token",
        fetch=fetch,
        clock=clock,
        wall=lambda: 1_790_000_000.0,  # a fixed day (never the host clock)
    )


# ── merging (R11) ───────────────────────────────────────────────────────
def test_workflow_peers_merge_behind_the_configured_ones():
    configured = [PowerPeer(name="Mac", address="192.0.2.30:5682")]
    auto = [
        pf.AutoPeer("5060 Ti-1", "192.0.2.30:5682"),  # the same machine: config wins
        pf.AutoPeer("5070 Ti", "192.0.2.31:5680"),
        pf.AutoPeer("5070 Ti again", "192.0.2.31"),  # 5680 is the default port
    ]
    merged = pf.merge_peers(configured, auto)
    assert [(p.name, p.address, p.source) for p in merged] == [
        ("Mac", "192.0.2.30:5682", "config"),
        ("5070 Ti", "192.0.2.31:5680", "workflow"),
    ]


def test_snapshot_rows_carry_their_source():
    clock, fetch = Clock(), Fetch()
    peers = pf.merge_peers(
        [PowerPeer(name="Mac", address="192.0.2.20")],
        [pf.AutoPeer("5070 Ti", "192.0.2.31:5680")],
    )
    fleet = fleet_of(peers, fetch, clock)
    try:
        fleet.refresh_once()
        rows = {row["name"]: row["source"] for row in fleet.snapshot()}
        assert rows == {"Mac": "config", "5070 Ti": "workflow"}
    finally:
        fleet.stop()


def test_the_controller_only_gets_status_and_history(tmp_path):
    clock, fetch = Clock(), Fetch()
    fetch.answers["http://192.0.2.31:5680/status"] = {
        "power": {"w": 100.0},
        "workflow_worker": {"busy": False},
    }
    fleet = fleet_of([pf.AutoPeer("5070 Ti", "192.0.2.31:5680")], fetch, clock)
    try:
        fleet.refresh_once()
        assert fetch.urls
        assert all(
            url.endswith("/status") or "/power/history?range=" in url
            for url in fetch.urls
        )
        assert not any("/events" in url for url in fetch.urls)
    finally:
        fleet.stop()


def test_enrolled_workers_become_peers_but_revoked_and_retired_go(tmp_path):
    rt = runtime(tmp_path / "wf")
    try:
        kept = enrol_remote(rt, "5060 Ti-1")
        busy = enrol_remote(rt, "5060 Ti-2")
        gone = enrol_remote(rt, "old")
        retired = enrol_remote(rt, "older")
        film_id = add_film(rt)
        route(rt, film_id, busy.worker_id)
        claim_remote(rt, busy, film_id, "restore")
        for remote in (busy, gone, retired):
            worker = rt.store.get_worker(rt.operator, remote.worker_id).worker
            devices.revoke(rt, remote.worker_id, worker.version)
        worker = rt.store.get_worker(rt.operator, retired.worker_id).worker
        devices.retire(rt, retired.worker_id, worker.version)
        on = SimpleNamespace(remote_workers=True)
        host = SimpleNamespace(state="running", runtime=rt, settings=on)
        peers = devices.power_peers(host)
        assert {p.name for p in peers} == {"5060 Ti-1", "5060 Ti-2"}
        # a revoked worker still holding work stays: 判定已停止 reads its /status
        assert all(p.address == "192.0.2.30:5682" for p in peers)
        assert kept.worker_id
        assert devices.power_peers(None) == []
        # §9 IR11: only with 允许 GPU 工作机接入 (phase 1 is unchanged)
        off = SimpleNamespace(remote_workers=False)
        assert (
            devices.power_peers(
                SimpleNamespace(state="running", runtime=rt, settings=off)
            )
            == []
        )
        assert (
            devices.power_peers(SimpleNamespace(state="starting", runtime=None)) == []
        )
    finally:
        rt.close()


# ── the status channel (R8b) ────────────────────────────────────────────
URL = "http://192.0.2.31:5680/status"
ADDRESS = "192.0.2.31:5680"


@pytest.fixture
def channel():
    clock, fetch = Clock(), Fetch()
    fleet = fleet_of([pf.AutoPeer("5070 Ti", ADDRESS)], fetch, clock)
    try:
        yield SimpleNamespace(clock=clock, fetch=fetch, fleet=fleet)
    finally:
        fleet.stop()


def read(channel, answer, step=5.0):
    channel.fetch.answers[URL] = answer
    channel.fleet.refresh_once()
    channel.clock.value += step


@pytest.mark.parametrize(
    "answer",
    ["unreachable", "timeout", {"workflow_worker": {"busy": False}}],
)
def test_dead_or_idle_reads_keep_the_worker_quiet(channel, answer):
    for _ in range(61):
        read(channel, answer)
    state = channel.fleet.quiet(ADDRESS)
    assert state.quiet_s == pytest.approx(305.0)
    assert state.checked_s == pytest.approx(5.0)


@pytest.mark.parametrize(
    ("answer", "code"),
    [
        ({"workflow_worker": {"busy": True}}, "busy"),
        ("unauthorized", "unauthorized"),
        ("not_found", "not_found"),
        ("invalid", "invalid"),
        ({"workflow_worker": None}, "no_busy"),
        ({"machine": "w"}, "no_busy"),
        (["not", "a", "dict"], "invalid"),
    ],
)
def test_any_other_answer_means_the_machine_is_up(channel, answer, code):
    for _ in range(10):
        read(channel, "unreachable")
    read(channel, answer)
    state = channel.fleet.quiet(ADDRESS)
    assert state.quiet_s is None and state.state == code


@pytest.mark.parametrize("code", ["timeout", "unreachable"])
def test_an_answered_request_whose_body_failed_is_not_quiet(channel, code):
    """§9 CX19 (Codex r6): a response arrived (status line and headers) but
    the body stalled or broke: the status channel is alive, never quiet."""
    for _ in range(10):
        read(channel, "unreachable")
    channel.fetch.answers[URL] = pf.PeerError(code, answered=True)
    for _ in range(61):
        channel.fleet.refresh_once()
        channel.clock.value += 5.0
    state = channel.fleet.quiet(ADDRESS)
    assert state.quiet_s is None and state.answered is True


def test_quiet_restarts_after_an_answer_or_a_gap(channel):
    for _ in range(10):
        read(channel, "unreachable")
    read(channel, {"workflow_worker": {"busy": True}})
    read(channel, "timeout")
    assert channel.fleet.quiet(ADDRESS).quiet_s == pytest.approx(5.0)
    channel.clock.value += 60  # no read for a minute: not proof of silence
    read(channel, "timeout", step=0.0)
    assert channel.fleet.quiet(ADDRESS).quiet_s == 0.0
    channel.clock.value += 40
    assert channel.fleet.quiet(ADDRESS).checked_s == pytest.approx(40.0)


def test_quiet_finds_the_peer_by_address_and_knows_no_other(channel):
    read(channel, "unreachable")
    assert channel.fleet.quiet("192.0.2.31") is not None  # the default port
    assert channel.fleet.quiet("192.0.2.99:5680") is None
    assert channel.fleet.quiet("not an address") is None


def test_a_peer_list_that_fails_keeps_the_last_good_one(channel, caplog):
    """§9 IR10: a failing peer source (the store busy) keeps the configured
    peers and the status channel's quiet time instead of dropping them."""
    peers = [pf.AutoPeer("5070 Ti", ADDRESS)]
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("store busy")
        return peers

    fleet = pf.PowerFleet(
        flaky,
        lambda: "token",
        fetch=channel.fetch,
        clock=channel.clock,
        wall=lambda: 1_790_000_000.0,
    )
    try:
        channel.fetch.answers[URL] = "unreachable"
        fleet.refresh_once()
        channel.clock.value += 5.0
        fleet.refresh_once()  # the source raises from here on
        channel.clock.value += 5.0
        fleet.refresh_once()
        state = fleet.quiet(ADDRESS)
        assert state is not None and state.quiet_s == pytest.approx(10.0)
        assert [row["name"] for row in fleet.snapshot("24h")] == ["5070 Ti"]
        assert "power peers unavailable" in caplog.text
    finally:
        fleet.stop()


def test_the_status_channel_remembers_whether_it_ever_answered(channel):
    """§9 IR2: reads that only ever failed (unreachable or timeout) since the
    controller started are not a dead machine: `answered` is False until
    the first HTTP answer, and stays True after it."""
    read(channel, "unreachable")
    read(channel, "timeout")
    assert channel.fleet.quiet(ADDRESS).answered is False
    read(channel, {"workflow_worker": {"busy": False}})
    assert channel.fleet.quiet(ADDRESS).answered is True
    read(channel, "unreachable")
    state = channel.fleet.quiet(ADDRESS)
    assert state.answered is True and state.quiet_s is not None
