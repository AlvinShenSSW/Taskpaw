"""#304 review fixes (Codex CX*, independent review IR*): a removed film is
refused by route and claim, the scheduler's own pass checks sources, a
re-dropped package is a fresh film, a network hiccup is unknown not lost, the
board and the store agree on `can_remove`, and previews honour the hold.
Fakes and tmp dirs only."""

# ruff: noqa: F401, F811  (pytest fixtures imported from the sibling test modules)

from __future__ import annotations

import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskpaw_v3.tests import test_workflow_intake_files as fixtures
from taskpaw_v3.tests.test_workflow_intake_discovery import config_for, scan
from taskpaw_v3.tests.test_workflow_intake_store import drop, intake
from taskpaw_v3.tests.test_workflow_local_dispatch import dispatcher
from taskpaw_v3.tests.test_workflow_source_lost import api, remove, world
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.worker_api_helpers import VERSION, beat
from taskpaw_v3.tests.workflow_route_helpers import (
    claim_remote,
    enrol_remote,
    job,
    route,
)
from taskpaw_v3.workflow import board, planning
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import operator_actions as ops
from taskpaw_v3.workflow.fleet_live import FleetLive, Heartbeat
from taskpaw_v3.workflow.local_host import LocalWorkflowHost
from taskpaw_v3.workflow.scheduler import SOURCE_CHANGED

package = fixtures.package


def code_of(call) -> str:
    with pytest.raises(m.WorkflowError) as info:
        call()
    return info.value.code


# ── CX1 / IR2: a removed film is neither routed nor claimed ─────────────
def test_a_removed_film_is_neither_routed_nor_claimed(world):
    rt = world.rt
    remote = enrol_remote(rt, "5060 Ti-1")
    film_id = world.film()
    remove(world, film_id)  # the dispatcher / pass read the film before this
    assert code_of(lambda: route(rt, film_id, remote.worker_id)) == (
        "source_not_admissible"
    )
    assert code_of(lambda: claim_remote(rt, remote, film_id, "restore")) == (
        "source_not_admissible"
    )
    assert rt.store.list_routes(rt.operator) == ()
    assert job(rt, film_id, "restore").status == "pending"


def test_the_dispatcher_cannot_open_a_local_route_or_claim_a_removed_film(world):
    rt = world.rt
    film_id = world.film()
    (stale,) = [
        j
        for j in rt.store.list_jobs(rt.operator, "pending", ("restore",))
        if j.film_id == film_id
    ]
    remove(world, film_id)
    d = dispatcher(rt)
    assert d._ensure_route(stale) is False
    assert rt.store.list_routes(rt.operator) == ()
    assert d.run_once() is False
    assert not (rt.work_dir / "attempts").exists() or not any(
        (rt.work_dir / "attempts").iterdir()
    )


# ── CX2 / IR1: the scheduler's pass checks sources itself ───────────────
def host_of(world, remote_fleet):
    """`_route_pass` on a host double: no dispatcher, nothing paused or busy."""
    rt = world.rt
    inputs = {
        "live": remote_fleet.snapshot(),
        "now": rt.clock.now(),
        "controller_id": rt.identity.worker_id,
        "controller_free_bytes": 10**15,
        "live_since": remote_fleet.started_at,
    }
    return SimpleNamespace(
        runtime=rt,
        pass_inputs=lambda: inputs,
        sources=world.watch,
        dispatcher=None,
        last_pass=None,
    )


def fleet_with_worker(world):
    rt = world.rt
    remote = enrol_remote(rt, "5070 Ti")
    fleet = FleetLive(rt.clock.now() - timedelta(hours=1), VERSION)
    fleet.observe(
        remote.worker_id,
        Heartbeat.model_validate(beat(identity(), **{})),
        rt.clock.now(),
    )
    return remote, fleet


def test_the_route_pass_checks_the_sources_of_unstarted_films_itself(world):
    remote, fleet = fleet_with_worker(world)
    gone, here = world.film("gone"), world.film("here")
    world.lose("gone")
    assert world.watch.ids() == frozenset()  # nothing has looked yet
    LocalWorkflowHost._route_pass(host_of(world, fleet))  # type: ignore[arg-type]
    routed = {r.film_id for r in world.rt.store.list_routes(world.rt.operator)}
    assert routed == {here}
    assert world.watch.ids() == frozenset({gone})


# ── IR3: a re-dropped package is a fresh, visible film ──────────────────
@pytest.mark.skipif(sys.platform != "win32", reason="Native source discovery")
def test_a_removed_film_id_is_not_reused_when_the_package_returns(package, tmp_path):
    config = config_for(package, tmp_path)
    first = scan(config)
    (old,) = first.grouping.film_groups
    from taskpaw_v3.workflow import intake_discovery as discovery

    def observe(**kw):
        return discovery.observe_package(
            config,
            "Actor/Film",
            previous=first,
            package_version=1,
            observed_at=datetime.now(timezone.utc),
            trigger="rescan",
            **kw,
        )

    kept = observe()  # present and unchanged: nothing to re-create
    assert kept.grouping.film_groups[0].film_id == old.film_id
    kept_removed = observe(removed=frozenset({old.film_id}))
    assert kept_removed.grouping.film_groups[0].film_id == old.film_id
    back = observe(removed=frozenset({old.film_id}), reappeared=True)
    (fresh,) = back.grouping.film_groups
    assert fresh.film_id != old.film_id
    assert fresh.group_key == old.group_key
    # a change to a still-present package regroups under a new id as well
    (package[1] / "extra.zh.srt").write_bytes(b"more subtitle")
    changed = observe(removed=frozenset({old.film_id}))
    assert all(g.film_id != old.film_id for g in changed.grouping.film_groups)


def test_the_intake_board_shows_a_package_whose_only_film_was_removed(intake):
    h, _, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    package_id = drop(
        h.store,
        adapter,
        h.meta,
        root,
        "Performer/MOV-002",
        ("MOV-002.mp4",),
        now=h.clock.now(),
    )
    film_id = identity()
    h.store.create_film(
        adapter,
        h.meta(),
        m.FilmCreate(
            film_id=film_id,
            intake_session_id=identity(),
            display_name="MOV-002",
            policy=root.film_policy,
        ),
    )
    conn = h.store._conn
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO intake_films(film_id,input_revision,package_id,snapshot_id,"
        "source_generation,grouping_revision,created_at) VALUES(?,1,?,?,1,1,'t')",
        (film_id, package_id, identity()),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    assert h.store.list_intake_board(h.operator) == ()  # a live film hides it
    conn.execute(
        "UPDATE films SET body_json=json_set(body_json,'$.removed_at',"
        "'2026-10-24T00:00:00Z') WHERE film_id=?",
        (film_id,),
    )
    conn.commit()
    (shown,) = h.store.list_intake_board(h.operator)  # a removed one does not
    assert shown.relative_path == "Performer/MOV-002"


# ── IR4: a network hiccup is unknown, not 片源丢失 ──────────────────────
def test_a_root_that_vanishes_during_the_check_is_unknown_not_lost(world, monkeypatch):
    film_id = world.film()
    world.watch.check(film_id, force=True)
    real = Path.exists

    def flaky(self, *a, **kw):
        shutil.rmtree(world.inbox, ignore_errors=True)  # the share drops mid-check
        return False

    monkeypatch.setattr(Path, "exists", flaky)
    assert world.watch.check(film_id, force=True) is None
    monkeypatch.setattr(Path, "exists", real)
    assert world.watch.ids() == frozenset()
    assert world.watch.unknown(film_id) is True


def test_an_unknown_check_keeps_a_lost_film_lost(world):
    film_id = world.film()
    world.lose()
    assert world.watch.check(film_id, force=True) is not None
    shutil.rmtree(world.inbox)
    assert world.watch.check(film_id, force=True) is not None
    assert world.watch.unknown(film_id) is True
    world.inbox.mkdir()
    world.watch.check(film_id, force=True)
    assert world.watch.unknown(film_id) is False


def test_recheck_on_an_unreachable_inbox_says_unreachable(world, api):
    film_id = world.film()
    world.lose()
    world.watch.check(film_id, force=True)
    shutil.rmtree(world.inbox)
    body = api.post(f"/control/workflow/films/{film_id}/recheck-source").json()
    assert body["ok"] is True
    assert body["unreachable"] is True
    assert body["source_missing"] is not None  # still shown as it was
    world.inbox.mkdir()
    body = api.post(f"/control/workflow/films/{film_id}/recheck-source").json()
    assert body["unreachable"] is False


# ── IR5: the board and the store agree on can_remove ────────────────────
def test_an_approval_of_a_stale_context_does_not_block_delete_on_either_side(world):
    rt = world.rt
    film_id = world.film()
    conn = rt.store._conn
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO approvals(approval_id,film_id,context_revision,context_hash,"
        "binding_hash,operator_principal_id,epoch,issued_at,expires_at,body_json) "
        "VALUES(?,?,7,'h','b',?,1,'t','t','{}')",
        (identity(), film_id, rt.store.get_film(rt.operator, film_id).film.film_id),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    film = board.board_film(rt, film_id)
    assert ops.removal_refusal(film, claimed=False, routed=False) is None  # board
    assert remove(world, film_id)["ok"] is True  # the store agrees


def test_the_board_counts_any_open_assignment_as_claimed(world, api):
    remote = enrol_remote(world.rt, "5060 Ti-1")
    film_id = world.film()
    route(world.rt, film_id, remote.worker_id)
    claim_remote(world.rt, remote, film_id, "restore")
    claimed, routed = ops.busy_films(world.rt)
    assert film_id in claimed and film_id in routed
    assert api.row(film_id)["can_remove"] is False


# ── IR6: previews honour the hold ───────────────────────────────────────
def test_reassign_options_and_plans_do_not_offer_a_held_film(world):
    remote, fleet = fleet_with_worker(world)
    film_id = world.film()

    def read(**kw):
        return {
            o["name"]: o for o in ops.reassign_options(world.rt, fleet, film_id, **kw)
        }

    free = read()
    assert free["自动"]["machine"] is not None
    assert free["5070 Ti"]["finish_at"] is not None
    held = read(hold=frozenset({film_id}))  # 片源丢失: the pass would not route it
    assert held["自动"]["machine"] is None and held["自动"]["finish_at"] is None
    assert held["5070 Ti"]["finish_at"] is None
    unplanned = world.film("fresh", plan=False)
    inputs = {
        "live": fleet.snapshot(),
        "now": world.rt.clock.now(),
        "controller_id": world.rt.identity.worker_id,
        "controller_free_bytes": 10**15,
        "live_since": fleet.started_at,
    }
    auto = planning.plan_list(world.rt, remote=inputs)["plans"][0]["routing"]
    assert auto["standard"]["auto"]["machine"] is not None
    held = planning.plan_list(world.rt, remote=inputs, hold=frozenset({unplanned}))
    assert held["plans"][0]["routing"]["standard"]["auto"] == {
        "waiting": SOURCE_CHANGED
    }
