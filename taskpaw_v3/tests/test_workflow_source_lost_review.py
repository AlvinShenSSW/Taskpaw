"""#304 review fixes (Codex CX*, independent review IR*): a removed film is
refused by route and claim, the scheduler's own pass checks sources, a
re-dropped package is a fresh film, a network hiccup is unknown not lost, the
board and the store agree on `can_remove`, and previews honour the hold.
Fakes and tmp dirs only."""

# ruff: noqa: F401, F811  (pytest fixtures imported from the sibling test modules)

from __future__ import annotations

import shutil
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskpaw_v3.tests import test_workflow_intake_files as fixtures
from taskpaw_v3.tests.test_workflow_approvals import (
    archive,
    archive_receipt,
    cleanup_command,
    h,
    prepared,
)
from taskpaw_v3.tests.test_workflow_films import film
from taskpaw_v3.tests.test_workflow_intake_discovery import config_for, scan
from taskpaw_v3.tests.test_workflow_intake_service import service
from taskpaw_v3.tests.test_workflow_intake_store import drop, intake
from taskpaw_v3.tests.test_workflow_local_dispatch import dispatcher, stage
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
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import operator_actions as ops
from taskpaw_v3.workflow.executors import SyntheticExecutor
from taskpaw_v3.workflow.fleet_live import FleetLive, Heartbeat
from taskpaw_v3.workflow.intake_watch import DirtyBatch
from taskpaw_v3.workflow.local_host import LocalWorkflowHost
from taskpaw_v3.workflow.lookup import MetadataLookup
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
    assert (
        ops.removal_refusal(world.rt, film, claimed=False, routed=False) is None
    )  # board
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


# CX3-CX6: outer-gate regressions
@pytest.mark.parametrize("reingest", [False, True])
def test_removed_source_reference_does_not_block_input_cleanup(h, reingest):
    removed_id, _, _, _ = film(h)
    if not reingest:
        _, final, _, approval = prepared(h)  # already shares the source
    h.store.remove_film(
        h.operator,
        h.meta(),
        m.FilmRemove(
            film_id=removed_id,
            reason="Delete task",
            expected_version=h.store.get_film(h.operator, removed_id).film.version,
        ),
    )
    if reingest:
        _, final, _, approval = prepared(h)  # same source ingested after removal
    admitted = archive(h, approval)
    receipt = archive_receipt(h, admitted, final, approval)
    h.store.accept_archive_receipt(admitted[1], h.meta(), receipt)
    _, _, cleaner, _ = h.component("cleanup_adapter")
    result = h.store.begin_cleanup(
        h.operator,
        h.meta(),
        cleanup_command(h, approval, receipt, "input", cleaner.principal_id),
    )
    assert result.status == "admitted"


@pytest.mark.skipif(sys.platform != "win32", reason="Native source discovery")
@pytest.mark.parametrize("trigger", ["rescan", "notification", "overflow"])
def test_scan_of_returned_deleted_package_uses_a_fresh_film(service, package, trigger):
    h, adapter, config, scanner, _ = service
    assert scanner.scan_once()
    (original,) = h.store.list_intakes(adapter, im.IntakeListQuery()).items
    old_id = original.grouping.film_groups[0].film_id
    h.store.create_film(
        adapter,
        h.meta(),
        m.FilmCreate(
            film_id=old_id,
            intake_session_id=identity(),
            display_name="movie",
            policy=config.film_policy,
        ),
    )
    # Synthetic publication link; discovery itself uses only the tmp package.
    conn = h.store._conn
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO intake_films(film_id,input_revision,package_id,snapshot_id,"
        "source_generation,grouping_revision,created_at) VALUES(?,1,?,?,1,1,'t')",
        (old_id, original.package_id, identity()),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    h.store.remove_film(
        h.operator,
        h.meta(),
        m.FilmRemove(film_id=old_id, expected_version=1, reason="Delete task"),
    )
    # Moving out and back preserves every member's native identity and mtime.
    parked = package[0].parent / "parked"
    package[1].rename(parked)
    assert scanner.scan_once()
    assert h.store.get_intake(adapter, original.package_id).state == "missing"
    parked.rename(package[1])
    batch = (
        None
        if trigger == "rescan"
        else DirtyBatch((original.relative_path,), trigger == "overflow")
    )
    assert scanner.scan_once(trigger=trigger, batch=batch)
    returned = h.store.get_intake(adapter, original.package_id)
    assert returned.grouping.film_groups[0].film_id != old_id
    assert returned.state == "waiting_stability"
    (visible,) = h.store.list_intake_board(h.operator)
    assert visible.package_id == original.package_id
    assert (package[1] / "movie.mp4").read_bytes() == b"synthetic bytes\x00" * 100


def test_pending_restore_retry_with_history_can_dispatch_after_source_loss(world):
    rt = world.rt
    film_id = world.film()
    rt.executors["restore"] = SyntheticExecutor("restore", "raise SystemExit(77)")
    d = dispatcher(rt, sources=world.watch)
    assert d.run_once() is True
    failed = stage(rt, film_id, "restore")
    assert failed.status == "failed"
    rt.store.retry_stage(
        rt.operator,
        rt.meta(),
        m.StageRetry(
            job_id=failed.job_id, expected_version=failed.version, reason="Retry"
        ),
    )
    assert stage(rt, film_id, "restore").status == "pending"
    restore = next(
        s for s in board.board_film(rt, film_id).stages if s.stage == "restore"
    )
    assert restore.first_started_at is not None
    assert restore.attempt_ids
    world.lose()
    assert world.watch.check(film_id, force=True) is None
    assert world.watch.ids() == frozenset()
    rt.executors["restore"] = SyntheticExecutor("restore")
    assert d.run_once() is True
    assert stage(rt, film_id, "restore").status == "succeeded"


@pytest.mark.parametrize("already_lost", [False, True])
def test_member_oserror_preserves_state_and_recheck_reports_unreachable(
    world, api, monkeypatch, caplog, already_lost
):
    film_id = world.film()
    if already_lost:
        (world.inbox / "movie.mkv").unlink()
    previous = world.watch.check(film_id, force=True)
    previous_wire = api.row(film_id)["source_missing"]
    world.restore()
    real_exists = Path.exists

    def unreadable(path):
        if path == world.inbox / "movie.mkv":
            raise OSError("synthetic member access failure")
        return real_exists(path)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "exists", unreadable)
        assert world.inbox.is_dir()  # only the member is inaccessible
        response = api.post(f"/control/workflow/films/{film_id}/recheck-source")
    assert response.status_code == 200
    assert response.json() == {
        "ok": True,
        "unreachable": True,
        "source_missing": previous_wire,
    }
    assert world.watch.unknown(film_id) is True
    assert world.watch.get(film_id) == previous
    assert "synthetic member access failure" in caplog.text
    assert "movie.mkv" in caplog.text
    # A successful later check is the recovery proof.
    assert api.post(f"/control/workflow/films/{film_id}/recheck-source").json() == {
        "ok": True,
        "unreachable": False,
        "source_missing": None,
    }


# CX7-CX9: concurrent source checks and removal during intake / lookup.
def test_concurrent_source_checks_wait_for_the_completed_result(world, monkeypatch):
    film_id = world.film()
    world.lose()
    entered, release, second_started, second_done = (
        threading.Event() for _ in range(4)
    )
    original = world.watch._missing
    checked = []

    def blocked(film_id):
        checked.append(film_id)
        entered.set()
        assert release.wait(5)
        return original(film_id)

    def second_check():
        second_started.set()
        result = world.watch.check(film_id)
        second_done.set()
        return result

    monkeypatch.setattr(world.watch, "_missing", blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(world.watch.check, film_id)
        try:
            assert entered.wait(5)
            second = pool.submit(second_check)
            assert second_started.wait(5)
            returned_early = second_done.wait(0.2)
        finally:
            release.set()
        one, two = first.result(timeout=5), second.result(timeout=5)
    assert not returned_early
    assert one is not None and two == one
    assert checked == [film_id]
    assert dispatcher(world.rt, sources=world.watch).run_once() is False


def test_remove_refuses_an_open_intake_in_the_transaction_and_board(
    world, api, monkeypatch
):
    rt = world.rt
    film_id = world.film(plan=False)
    other = world.film("other", plan=False)
    package_id, operation_id = identity(), identity()
    # Synthetic replacement operation and an existing publication link.
    conn = rt.store._conn
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute(
        "INSERT INTO intake_films(film_id,input_revision,package_id,snapshot_id,"
        "source_generation,grouping_revision,created_at) VALUES(?,1,?,?,1,1,'t')",
        (film_id, package_id, identity()),
    )
    conn.execute(
        "INSERT INTO intake_operations(operation_id,package_id,owner_principal_id,"
        "root_id,root_config_revision,source_generation,owner_boot_id,state,"
        "unresolved,publication_allowed,version,admission_json,observation_json,"
        "config_json,created_at,updated_at) "
        "VALUES(?,?,?, ?,1,2,?,'running',1,1,1,'{}','{}','{}','t','t')",
        (operation_id, package_id, identity(), identity(), identity()),
    )
    conn.commit()
    conn.execute("PRAGMA foreign_keys=ON")
    command = m.FilmRemove(
        film_id=film_id,
        expected_version=rt.store.get_film(rt.operator, film_id).film.version,
        reason="Delete task",
    )
    with pytest.raises(m.WorkflowError, match="film_intaking"):
        rt.store.remove_film(rt.operator, rt.meta(), command)
    assert api.row(film_id)["can_remove"] is False
    assert api.row(other)["can_remove"] is True
    url = f"/control/workflow/films/{film_id}/remove"
    for bypass_precheck in (False, True):
        with monkeypatch.context() as patch:
            if bypass_precheck:
                patch.setattr(ops, "removal_refusal", lambda *a, **kw: None)
            response = api.post(url)
        assert response.status_code == 409
        assert response.json()["code"] == "film_intaking"
        assert response.json()["detail"] == "正在收录这部片，稍后再删"
    assert rt.store.get_film(rt.operator, film_id).film.removed_at is None
    conn.execute(
        "UPDATE intake_operations SET unresolved=0,state='completed' WHERE operation_id=?",
        (operation_id,),
    )
    conn.commit()
    assert api.row(film_id)["can_remove"] is True
    assert api.post(url).status_code == 200
    assert (
        conn.execute(
            "SELECT 1 FROM source_refs WHERE film_id=? AND retained=1", (film_id,)
        ).fetchone()
        is None
    )


def test_remove_refuses_metadata_lookup_until_its_result_is_written(
    world, api, tmp_path, monkeypatch
):
    rt = world.rt
    film_id = world.film(plan=False)
    other = world.film("other", plan=False)
    lookup = MetadataLookup(
        rt,
        python=sys.executable,
        root=str(tmp_path),
        profile=str(tmp_path / "fake.json"),
    )
    rt.lookup = lookup
    working, finish_work, writing, finish_write = (threading.Event() for _ in range(4))
    cache = lookup.cache_dir / f"{film_id}-r1"

    def fake_lookup(*_):
        cache.mkdir(parents=True)
        (cache / "working.txt").write_text("fake lookup", encoding="utf-8")
        working.set()
        assert finish_work.wait(5)
        return {"status": "failed", "message": "synthetic result"}

    original_write = lookup._write

    def blocked_write(*args):
        writing.set()
        assert finish_write.wait(5)
        original_write(*args)

    monkeypatch.setattr(lookup, "_lookup", fake_lookup)
    monkeypatch.setattr(lookup, "_write", blocked_write)
    url = f"/control/workflow/films/{film_id}/remove"
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(lookup.run, film_id, 1, "movie.mkv")
        try:
            for entered, release in ((working, finish_work), (writing, finish_write)):
                assert entered.wait(5)
                response = api.post(url)
                assert response.status_code == 409
                assert response.json()["code"] == "film_lookup_running"
                assert response.json()["detail"] == "正在查询元数据，稍后再删"
                assert api.row(film_id)["can_remove"] is False
                assert api.row(other)["can_remove"] is True
                assert (cache / "working.txt").is_file()
                release.set()
        finally:
            finish_work.set()
            finish_write.set()
        running.result(timeout=5)
    assert (cache / "lookup.json").is_file()
    assert api.row(film_id)["can_remove"] is True
    assert api.post(url).status_code == 200
    assert not cache.exists()
