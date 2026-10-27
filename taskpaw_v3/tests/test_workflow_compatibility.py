"""Application builds may differ; new work requires a verified workflow protocol."""

from __future__ import annotations

import sqlite3

import pytest

from taskpaw_v3.tests.test_worker_attempts import (
    P,
    claim,
    claim_body,
    finish,
    finished,
    owner,
    restore_ledger,
    uploaded,
)
from taskpaw_v3.tests.test_worker_attempts import api as api  # noqa: F401
from taskpaw_v3.tests.test_worker_attempts import clock as clock  # noqa: F401
from taskpaw_v3.tests.test_worker_attempts import host as host  # noqa: F401
from taskpaw_v3.tests.test_worker_attempts import rt as rt  # noqa: F401
from taskpaw_v3.tests.test_worker_attempts import world as world  # noqa: F401
from taskpaw_v3.tests.test_workflow_fleet_live import START
from taskpaw_v3.tests.test_workflow_fleet_live import beat as live_beat
from taskpaw_v3.tests.test_workflow_scheduler import (
    film,
    fleet,
    open_route,
    routed,
    run,
)
from taskpaw_v3.tests.worker_api_helpers import beat, boot_id
from taskpaw_v3.tests.workflow_route_helpers import job
from taskpaw_v3.workflow.fleet_live import PROTOCOL, FleetLive


@pytest.mark.parametrize("worker_version", ["3.10.2", "3.10.4", "4.0.0"])
def test_supported_protocol_allows_new_routes_across_application_versions(
    worker_version,
):
    worker = fleet()[0]
    live = FleetLive(START, "3.10.5")
    live.observe(worker.worker_id, live_beat(version=worker_version), START)
    queued = film(tier="standard")
    result = run([queued], [worker], live=live.snapshot(), now=START)
    assert routed(result)[queued.film_id] == worker.worker_id
    assert live.entry(worker.worker_id).beat.version == worker_version


def test_application_version_change_keeps_an_unclaimed_automatic_route():
    worker = fleet()[0]
    queued = film(tier="standard")
    route = open_route(queued, worker)
    live = FleetLive(START, "3.10.5")
    live.observe(worker.worker_id, live_beat(version="3.10.2"), START)
    result = run([queued], [worker], routes=[route], live=live.snapshot(), now=START)
    assert result.withdrawals == ()


def test_protocol_mismatch_blocks_new_routing_without_cancelling_claimed_work():
    worker = fleet()[0]
    current = film(
        0,
        restore="running",
        claimed_by=worker.worker_id,
        claimed_stage="restore",
        claimed_at=START,
    )
    queued = film(1, pin=worker.worker_id)
    live = FleetLive(START, "3.10.5")
    live.observe(
        worker.worker_id, live_beat(version="3.10.5", protocol=PROTOCOL + 1), START
    )
    result = run(
        [current, queued],
        [worker],
        routes=[open_route(current, worker)],
        live=live.snapshot(),
        now=START,
    )
    assert result.withdrawals == ()
    assert not result.routes
    assert "工作流协议不兼容" in result.waiting[queued.film_id]


def _claim_writes(rt):
    with sqlite3.connect(rt.db_path) as conn:
        return (
            conn.execute("SELECT next_fence FROM authority").fetchone()[0],
            *(
                conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("attempts", "assignments", "audit", "request_results")
            ),
        )


def test_claim_admission_is_not_a_worker_controlled_wire_field(api, rt, world):
    joined, boot, film_id, _ = world
    request = claim_body(rt, film_id, boot)
    before = _claim_writes(rt)
    refused = claim(api, joined, {**request, "admission_error": None})
    assert refused.status_code == 422
    assert _claim_writes(rt) == before


@pytest.mark.parametrize("reported", [None, "other_boot", "other_protocol"])
def test_fresh_claim_requires_protocol_and_current_boot_without_caching_denial(
    api, rt, host, world, reported
):
    joined, boot, film_id, _ = world
    # The fixture may have already observed its normal heartbeat. A restarted
    # controller starts with no live entry and must await a current-boot report.
    host.fleet = FleetLive(rt.clock.now(), "3.10.5")
    if reported is not None:
        body = beat(
            boot_id() if reported == "other_boot" else boot,
            version="3.10.2",
            protocol=PROTOCOL + 1 if reported == "other_protocol" else PROTOCOL,
        )
        assert (
            api.post(f"{P}/heartbeat", json=body, headers=joined.headers).status_code
            == 200
        )
    request = claim_body(rt, film_id, boot)
    before = _claim_writes(rt)
    refused = claim(api, joined, request)
    assert refused.status_code == 409
    assert refused.json() == {
        "code": "workflow_protocol_incompatible"
        if reported == "other_protocol"
        else "workflow_protocol_unverified"
    }
    assert _claim_writes(rt) == before
    assert (
        api.post(
            f"{P}/heartbeat", json=beat(boot, version="3.10.2"), headers=joined.headers
        ).status_code
        == 200
    )
    # Same idempotency key/body after a transient refusal must still succeed.
    accepted = claim(api, joined, request)
    assert accepted.status_code == 200, accepted.text
    assert claim(api, joined, request).json() == accepted.json()
    assert len(rt.store.list_attempts(rt.operator, request["job_id"])) == 1


@pytest.mark.parametrize("later", ["different_version", "other_protocol", "restart"])
def test_owned_claim_replay_renew_upload_and_finish_survive_compatibility_changes(
    api, rt, host, world, later
):
    joined, boot, film_id, _ = world
    assert (
        api.post(
            f"{P}/heartbeat", json=beat(boot, version="3.10.2"), headers=joined.headers
        ).status_code
        == 200
    )
    request = claim_body(rt, film_id, boot)
    accepted = claim(api, joined, request)
    assert accepted.status_code == 200, accepted.text
    if later == "restart":
        host.fleet = FleetLive(rt.clock.now(), "3.10.5")
    else:
        assert (
            api.post(
                f"{P}/heartbeat",
                json=beat(
                    boot,
                    version="4.0.0",
                    protocol=PROTOCOL + 1 if later == "other_protocol" else PROTOCOL,
                ),
                headers=joined.headers,
            ).status_code
            == 200
        )
    assert claim(api, joined, request).json() == accepted.json()
    changed = claim(api, joined, {**request, "operation_id": boot_id()})
    assert changed.status_code == 409
    assert changed.json() == {"code": "idempotency_conflict"}
    attempt = request["attempt_id"]
    renewed = api.post(
        f"{P}/attempts/{attempt}/renew", json=owner(boot), headers=joined.headers
    )
    assert renewed.status_code == 200, renewed.text
    raw = restore_ledger(film_id, attempt, uploaded(api, joined, attempt))
    done = finish(api, joined, attempt, finished(raw))
    assert done.status_code == 200, done.text
    assert done.json() == {"state": "resolved", "outcome": "succeeded"}
    assert job(rt, film_id, "restore").status == "succeeded"
    assert len(rt.store.list_attempts(rt.operator, request["job_id"])) == 1
