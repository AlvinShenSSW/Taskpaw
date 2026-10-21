"""U1 P1': the plan gate in the store, its v3 migration and the U04 backfill."""

from __future__ import annotations

import sqlite3

import pytest

from taskpaw_v3.tests.test_workflow_films import STAGES, input_manifest
from taskpaw_v3.tests.test_workflow_local_dispatch import (
    add_film,
    dispatcher,
    runtime,
    stage,
)
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import schema
from taskpaw_v3.workflow.auth import require_role
from taskpaw_v3.workflow.local_runtime import LocalRuntime


@pytest.fixture
def rt(tmp_path):
    value = runtime(tmp_path / "wf")
    try:
        yield value
    finally:
        value.close()


def confirm(rt: LocalRuntime, film_id: str, revision: int = 1, tier="standard"):
    return rt.store.confirm_plan(
        rt.operator,
        rt.meta(),
        m.FilmPlanConfirm(
            film_id=film_id,
            input_revision=revision,
            ltx_model=tier,
            machine=rt.identity.worker_id,
        ),
    )


def pending_restores(rt: LocalRuntime) -> list[m.PendingComputeJob]:
    return list(rt.store.list_jobs(rt.operator, "pending", ("restore",)))


def new_revision(rt: LocalRuntime, film_id: str, revision: int = 2) -> None:
    """A regroup or new files: the intake publishes another input revision."""
    _, auth = rt.grant_intake_adapter()
    film = rt.store.get_film(rt.operator, film_id).film
    rt.store.put_input_manifest(
        auth,
        rt.meta(),
        m.InputManifestPut(
            film_id=film_id,
            expected_version=film.version,
            manifest=input_manifest(film_id, revision=revision),
        ),
    )


# ── the gate ─────────────────────────────────────────────────────────────
def test_an_unplanned_film_is_listed_as_unplanned_and_never_claimed(rt):
    film = add_film(rt, plan=False)
    assert pending_restores(rt) == []
    (unplanned,) = rt.store.list_unplanned(rt.operator)
    assert (unplanned.film_id, unplanned.input_revision) == (film, 1)
    d = dispatcher(rt)
    assert d.run_once() is False
    assert stage(rt, film, "restore").status == "pending"


def test_claim_rechecks_the_plan_next_to_the_source(rt):
    film = add_film(rt, plan=False)
    job = stage(rt, film, "restore")
    tools = rt.executors["restore"].tools()
    worker = rt.worker()
    with pytest.raises(m.WorkflowError, match="plan_required"):
        rt.store.claim(
            rt.worker_auth,
            rt.meta(),
            m.ClaimRequest(
                job_id=job.job_id,
                expected_version=job.version,
                attempt_id=identity(),
                operation_id=identity(),
                worker_id=worker.worker_id,
                boot_id=rt.boot_id,
                profile_revision=tools.profile_revision,
                config_digest=tools.config_digest,
                resource_id=worker.physical_binding.resource_id,
                tool_versions=tools,
            ),
        )


def test_confirm_lets_the_dispatcher_claim_with_the_planned_tier(rt):
    film = add_film(rt, plan=False)
    result = confirm(rt, film, tier="high")
    assert result.status == "planned"
    (job,) = pending_restores(rt)
    assert (job.film_id, job.ltx_model) == (film, "high")
    assert rt.store.list_unplanned(rt.operator) == ()
    (plan,) = rt.store.list_plans(rt.operator)
    assert (plan.film_id, plan.input_revision, plan.ltx_model, plan.machine) == (
        film,
        1,
        "high",
        rt.identity.worker_id,
    )
    d = dispatcher(rt)
    assert d.run_once() is True
    assert stage(rt, film, "restore").status == "succeeded"


def test_a_film_needing_no_gpu_is_planned_without_a_tier(rt):
    film = add_film(rt, plan=False)
    confirm(rt, film, tier=None)
    (job,) = pending_restores(rt)
    assert job.ltx_model is None


def test_a_stale_revision_is_refused(rt):
    film = add_film(rt, plan=False)
    new_revision(rt, film)
    with pytest.raises(m.WorkflowError, match="cas_conflict"):
        confirm(rt, film, revision=1)
    confirm(rt, film, revision=2)


def test_a_confirmed_plan_cannot_be_changed(rt):
    film = add_film(rt, plan=False)
    confirm(rt, film)
    with pytest.raises(m.WorkflowError, match="plan_exists"):
        confirm(rt, film, tier="high")
    (plan,) = rt.store.list_plans(rt.operator)
    assert plan.ltx_model == "standard"


def test_only_a_pending_restore_can_be_planned(rt):
    film = add_film(rt, plan=False)
    job = stage(rt, film, "restore")
    with rt.store._lock:  # a restore that is no longer pending (e.g. blocked)
        rt.store._conn.execute(
            "UPDATE stages SET status='blocked' WHERE job_id=?", (job.job_id,)
        )
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        confirm(rt, film)


def test_an_unknown_machine_is_refused(rt):
    film = add_film(rt, plan=False)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        rt.store.confirm_plan(
            rt.operator,
            rt.meta(),
            m.FilmPlanConfirm(
                film_id=film,
                input_revision=1,
                ltx_model="standard",
                machine=identity(),
            ),
        )


def test_an_unknown_film_is_not_found(rt):
    with pytest.raises(m.WorkflowError, match="not_found"):
        confirm(rt, identity())


def test_a_new_revision_returns_the_film_to_planning(rt):
    film = add_film(rt, plan=False)
    confirm(rt, film)
    new_revision(rt, film)
    assert pending_restores(rt) == []
    (unplanned,) = rt.store.list_unplanned(rt.operator)
    assert (unplanned.film_id, unplanned.input_revision) == (film, 2)
    # the old revision's plan stays, unchanged (history for the recommendation)
    assert [
        (p.input_revision, p.ltx_model) for p in rt.store.list_plans(rt.operator)
    ] == [(1, "standard")]


def test_only_the_operator_confirms_plans():
    require_role("operator", "confirm_plan")
    for role in ("compute_worker", "intake_adapter", "observer", "scrape_adapter"):
        with pytest.raises(m.WorkflowError, match="forbidden"):
            require_role(role, "confirm_plan")


def test_other_stages_are_not_gated(rt):
    """Later stages depend on restore; only restore needs the plan (U01)."""
    film = add_film(rt, plan=False)
    confirm(rt, film)
    d = dispatcher(rt)
    assert d.run_once() is True  # restore
    (asr,) = rt.store.list_jobs(rt.operator, "pending", ("asr",))
    assert asr.film_id == film and asr.ltx_model is None


# ── v3 migration (U04) ───────────────────────────────────────────────────
def as_v2(path) -> None:
    """Turn a v3 file back into the exact v2 format (drop the plan table)."""
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE film_plans")
        conn.execute("PRAGMA user_version=2")


def test_v2_upgrade_backfills_only_restores_that_already_ran(tmp_path):
    rt = runtime(tmp_path / "wf")
    ran = add_film(rt, plan=False)
    never = add_film(rt, plan=False)
    stale = add_film(rt, plan=False)
    confirm(rt, ran, tier="high")
    confirm(rt, stale)
    d = dispatcher(rt)
    for _ in range(10):
        if stage(rt, stale, "restore").status == "succeeded":
            break
        assert d.run_once() is True
    assert stage(rt, ran, "restore").status == "succeeded"
    new_revision(rt, stale)  # its attempt belongs to revision 1 only
    worker = rt.identity.worker_id
    rt.close()
    as_v2(tmp_path / "wf" / "workflow.db")
    again = runtime(tmp_path / "wf")
    try:
        plans = {
            (p.film_id, p.input_revision): (p.ltx_model, p.machine)
            for p in again.store.list_plans(again.operator)
        }
        assert plans == {
            (ran, 1): (schema.BACKFILL_LTX_MODEL, worker),
        }  # `stale` ran only on revision 1; its current revision 2 never ran
        assert {f.film_id for f in again.store.list_unplanned(again.operator)} == {
            never,
            stale,
        }
    finally:
        again.close()
    backups = list((tmp_path / "wf").glob("*.bak"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    with sqlite3.connect(tmp_path / "wf" / "workflow.db") as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3


def test_v2_upgrade_failure_keeps_the_v2_file(tmp_path, monkeypatch):
    rt = runtime(tmp_path / "wf")
    rt.close()
    path = tmp_path / "wf" / "workflow.db"
    as_v2(path)
    real = schema._backfill_plans

    def broken(conn):
        real(conn)
        raise sqlite3.OperationalError("injected backfill failure")

    monkeypatch.setattr(schema, "_backfill_plans", broken)
    with pytest.raises(schema.SchemaError, match="migration_failed"):
        schema.connect_existing(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name='film_plans'"
        ).fetchone()


def test_a_modified_plan_table_is_rejected(tmp_path):
    rt = runtime(tmp_path / "wf")
    rt.close()
    path = tmp_path / "wf" / "workflow.db"
    with sqlite3.connect(path) as conn:
        conn.execute("DROP TABLE film_plans")
        conn.execute(
            "CREATE TABLE film_plans (film_id TEXT, input_revision INTEGER, "
            "ltx_model TEXT, machine TEXT, confirmed_at TEXT)"
        )
    with pytest.raises(schema.SchemaError, match="invalid_schema"):
        schema.connect_existing(path)


def test_new_databases_are_created_at_v3(rt):
    assert schema.SCHEMA_VERSION == 3
    assert "film_plans" in schema.TABLES
    assert STAGES  # imported policy stages still create every stage


# ── pause (V3 §7.5) ──────────────────────────────────────────────────────
def test_a_paused_dispatcher_claims_nothing_until_resumed(rt):
    film = add_film(rt)
    d = dispatcher(rt)
    d.set_paused(True)
    assert d.run_once() is False
    status = d.status()
    assert (status.state, status.paused) == ("paused", True)
    assert stage(rt, film, "restore").status == "pending"
    d.set_paused(False)
    assert d.run_once() is True
    assert d.status().paused is False
