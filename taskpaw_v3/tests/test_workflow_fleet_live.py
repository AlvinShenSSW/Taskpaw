"""P2-2 (#293): config keys, live worker state, restart grace and the worker read.

The heartbeat state is in memory only (C5); the controller's clock decides
online, 失联 (60 s, OD2) and 等待心跳 (the restart grace, IR12/C14).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from taskpaw_v3.core.config import AgentConfig, WorkflowSettings
from taskpaw_v3.tests.test_workflow_local_dispatch import runtime
from taskpaw_v3.tests.test_workflow_scheduler import film, fleet, open_route, run
from taskpaw_v3.tests.workflow_route_helpers import enrol_remote
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import scheduler as s
from taskpaw_v3.workflow.fleet_live import PROTOCOL, FleetLive, Heartbeat

START = datetime(2026, 10, 23, 8, 0, tzinfo=timezone.utc)
VERSION = "9.9.9"


def beat(**kw) -> Heartbeat:
    values: dict = dict(
        boot_id="00000000-0000-4000-8000-000000000001",
        version=VERSION,
        protocol=PROTOCOL,
        paused=False,
        gpu={
            "name": "NVIDIA GeForce RTX 5060 Ti",
            "vram_mib": 16311,
            "used_mib": 900,
            "util": 3.0,
            "power_w": 21.5,
            "hold": "none",
        },
        disk={"free": 900 * 10**9, "staging_used": 40 * 10**9, "cap": 500 * 10**9},
        tiers_ready=["standard", "good"],
        asr_ready=True,
        staged=[],
        current=None,
        transfers=[],
    )
    values.update(kw)
    return Heartbeat.model_validate(values)


# ── R1: config keys ─────────────────────────────────────────────────────
def test_defaults_keep_a_single_machine_controller():
    settings = WorkflowSettings()
    assert settings.role == "controller"
    assert settings.remote_workers is False
    assert settings.worker_api_port == 5682
    assert settings.staging_dir is None and settings.staging_cap_gb == 500


@pytest.mark.parametrize(
    "bad", [{"role": "hub"}, {"worker_api_port": 0}, {"worker_api_port": 70000}]
)
def test_invalid_workflow_keys_are_refused(bad):
    with pytest.raises(ValidationError):
        AgentConfig(server_id="s", machine="m", workflow=bad)


# ── R7–R10: live state on the controller's clock ────────────────────────
def test_online_until_sixty_seconds_without_a_heartbeat():
    fleet_live = FleetLive(START, VERSION)
    fleet_live.observe("w1", beat(), START + timedelta(seconds=90))
    assert fleet_live.state("w1", START + timedelta(seconds=149)) == "online"
    assert fleet_live.state("w1", START + timedelta(seconds=150)) == "lost"
    fleet_live.observe("w1", beat(), START + timedelta(seconds=200))  # it returns
    assert fleet_live.state("w1", START + timedelta(seconds=201)) == "online"


def test_restart_grace_waits_then_turns_lost():
    fleet_live = FleetLive(START, VERSION)
    assert fleet_live.state("w1", START + timedelta(seconds=59)) == "waiting"
    assert fleet_live.state("w1", START + timedelta(seconds=60)) == "lost"
    assert fleet_live.snapshot() == {}


def test_snapshot_feeds_the_scheduler():
    fleet_live = FleetLive(START, VERSION)
    now = START + timedelta(seconds=10)
    fleet_live.observe("w1", beat(paused=True), now)
    live = fleet_live.snapshot()["w1"]
    assert live.last_seen == now and live.paused is True
    assert live.tiers_ready == frozenset({"standard", "good"})
    assert live.version_ok is True and not hasattr(live, "asr_ready")  # OD7
    assert (live.free_bytes, live.staging_cap_bytes) == (900 * 10**9, 500 * 10**9)
    entry = fleet_live.entry("w1")
    assert entry is not None and entry.beat.gpu.power_w == 21.5


@pytest.mark.parametrize("change", [{"version": "1.0.0"}, {"protocol": PROTOCOL + 1}])
def test_a_version_or_protocol_mismatch_is_not_version_ok(change):
    fleet_live = FleetLive(START, VERSION)
    fleet_live.observe("w1", beat(**change), START)
    assert fleet_live.snapshot()["w1"].version_ok is False


@pytest.mark.parametrize(
    "change",
    [
        {"protocol": "1"},
        {"tiers_ready": ["ultra"]},
        {"current": {"film": "x"}},
        {"staged": [{"film": "not-a-uuid", "rev": 1}]},
        {"gpu": {"hold": "everything"}},
        {"extra": 1},
    ],
)
def test_a_malformed_heartbeat_is_refused(change):
    with pytest.raises(ValidationError):
        beat(**change)


# ── R9 in the scheduler: no 设备失联 inside the grace window ────────────
def test_grace_suppresses_lost_attention_for_unheard_workers_only():
    ws = fleet()[:2]
    queued, other = film(0), film(1)
    routes = [open_route(queued, ws[0]), open_route(other, ws[1])]
    now = START + timedelta(seconds=30)
    stale = {ws[1].worker_id: s.WorkerLive(last_seen=now - timedelta(seconds=61))}
    within = run(
        [queued, other], ws, routes=routes, now=now, live=stale, live_since=START
    )
    # ws[0] has no entry yet: 等待心跳, no attention; ws[1] was heard and went quiet
    assert within.attention == {other.film_id: "设备失联"}
    later = START + timedelta(seconds=61)
    after = run(
        [queued, other], ws, routes=routes, now=later, live={}, live_since=START
    )
    assert after.attention == {queued.film_id: "设备失联", other.film_id: "设备失联"}
    unchanged = run([queued, other], ws, routes=routes, now=now, live={})
    assert set(unchanged.attention) == {queued.film_id, other.film_id}


# ── the worker's own read ───────────────────────────────────────────────
@pytest.fixture
def rt(tmp_path):
    value = runtime(tmp_path / "wf")
    try:
        yield value
    finally:
        value.close()


def test_authenticated_worker_reads_only_its_own_record(rt):
    remote = enrol_remote(rt, "5060 Ti-1")
    me = rt.store.authenticated_worker(remote.auth)
    assert me.worker.worker_id == remote.worker_id
    assert me.worker.boot_id == remote.boot_id and me.boot_reconciled is True
    with pytest.raises(m.WorkflowError) as operator:
        rt.store.authenticated_worker(rt.operator)
    assert operator.value.code == "forbidden"
    _, intake = rt.grant_intake_adapter()
    with pytest.raises(m.WorkflowError) as component:
        rt.store.authenticated_worker(intake)
    assert component.value.code == "forbidden"


def test_a_revoked_worker_is_unauthorized(rt):
    remote = enrol_remote(rt, "5060 Ti-1")
    worker = rt.store.get_worker(rt.operator, remote.worker_id).worker
    rt.store.revoke_worker(
        rt.operator,
        rt.meta(),
        m.WorkerRevocation(
            worker_id=remote.worker_id, expected_version=worker.version, reason="测试"
        ),
    )
    with pytest.raises(m.WorkflowError) as exc:
        rt.store.authenticated_worker(remote.auth)
    assert exc.value.code == "unauthorized"


# ── HardwareProbe.profiles is additive (risk 3) ─────────────────────────
def test_a_probe_without_profiles_keeps_its_stored_bytes(rt):
    worker = rt.worker()
    probe = worker.capability.probed
    raw = m.canonical_json(probe)
    assert "profiles" not in raw  # a v4 row has none and must re-validate
    again = m.HardwareProbe.model_validate_json(raw)
    assert again.profiles == () and m.canonical_json(again) == raw
    approved = worker.capability.approved.profiles
    with_profiles = probe.model_copy(update={"profiles": approved})
    loaded = m.HardwareProbe.model_validate_json(m.canonical_json(with_profiles))
    assert loaded.profiles == approved
