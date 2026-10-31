"""Unchanged intake rescans write nothing; old intake-adapter history is pruned (#376)."""

from datetime import timedelta

import pytest

from taskpaw_v3.tests import test_workflow_intake_store as fixtures
from taskpaw_v3.tests.test_workflow_intake_publication import snapshot_fixture
from taskpaw_v3.tests.test_workflow_intake_root_scan import begin, finish
from taskpaw_v3.workflow import intake_discovery_models as dm
from taskpaw_v3.workflow import intake_store
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.manifest import observation_digest
from taskpaw_v3.workflow.store_core import timestamp

intake = fixtures.intake
IGNORED = {"observed_at", "last_complete_scan_at", "observation_json", "version"}


def counts(h):
    return tuple(
        h.store._conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("audit", "request_results")
    )


def row(h, package_id):
    return dict(
        h.store._conn.execute(
            "SELECT * FROM intake_packages WHERE package_id=?", (package_id,)
        ).fetchone()
    )


def rescan(h, adapter, observed):
    h.clock.value += timedelta(seconds=30)
    view = h.store.get_intake(adapter, observed.package_id)
    return observed.model_copy(
        update={
            "expected_package_version": view.version,
            "observed_at": h.clock.now(),
            "trigger": "rescan",
        }
    )


def force(h, package_id, **values):
    columns = ",".join(f"{key}=?" for key in values)
    h.store._conn.execute(
        f"UPDATE intake_packages SET {columns} WHERE package_id=?",
        (*values.values(), package_id),
    )


def observed_package(intake):
    h, _, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    observed = fixtures.observation(h, root)
    h.store.observe_intake(adapter, h.meta(), observed)
    return h, adapter, observed


def restored(intake, monkeypatch):
    h, adapter, _, observed, command = snapshot_fixture(intake, eligible=True)
    h.store.finish_intake_snapshot(adapter, h.meta(), command)
    assert h.store.get_intake(adapter, observed.package_id).state == (
        "operational_ready"
    )
    return h, adapter, observed


def publication_blocked(intake, monkeypatch):
    h, adapter, observed = observed_package(intake)
    force(h, observed.package_id, state="blocked", reason="publication_blocked")
    return h, adapter, observed


def unknown_owner(intake, monkeypatch):
    h, adapter, observed = observed_package(intake)
    force(h, observed.package_id, state="unknown_owner", reason="recovery_required")
    return h, adapter, observed


def archived_dirty(intake, monkeypatch):
    h, adapter, observed = observed_package(intake)
    force(
        h,
        observed.package_id,
        state="needs_attention",
        reason="recovery_required",
        dirty=1,
    )
    monkeypatch.setattr(h.store, "_intake_archived", lambda package_id: True)
    return h, adapter, observed


def test_unchanged_operational_ready_rescan_is_detected_without_writing(
    intake, monkeypatch
):
    h, adapter, observed = restored(intake, monkeypatch)
    h.store.observe_intake(adapter, h.meta(), rescan(h, adapter, observed))
    before = counts(h)
    assert h.store.intake_observation_unchanged(adapter, rescan(h, adapter, observed))
    assert counts(h) == before


@pytest.mark.parametrize(
    "reach", [restored, publication_blocked, unknown_owner, archived_dirty]
)
def test_skippable_state_differs_from_a_real_rescan_only_in_ignored_fields(
    intake, monkeypatch, reach
):
    h, adapter, observed = reach(intake, monkeypatch)
    h.store.observe_intake(adapter, h.meta(), rescan(h, adapter, observed))  # fixpoint
    command = rescan(h, adapter, observed)
    assert h.store.intake_observation_unchanged(adapter, command)
    before = row(h, observed.package_id)
    h.store.observe_intake(adapter, h.meta(), command)
    after = row(h, observed.package_id)
    assert {key for key in before if before[key] != after[key]} == IGNORED


def test_waiting_stability_is_never_skipped(intake):
    h, adapter, observed = observed_package(intake)
    command = rescan(h, adapter, observed)
    assert not h.store.intake_observation_unchanged(adapter, command)
    h.store.observe_intake(adapter, h.meta(), command)
    assert row(h, observed.package_id)["last_complete_scan_at"] == timestamp(
        command.observed_at
    )


def test_changed_stale_dirty_disabled_or_foreign_observations_are_not_skipped(
    intake, monkeypatch
):
    h, adapter, observed = restored(intake, monkeypatch)
    h.store.observe_intake(adapter, h.meta(), rescan(h, adapter, observed))
    command = rescan(h, adapter, observed)
    assert h.store.intake_observation_unchanged(adapter, command)
    members = (command.members[0].model_copy(update={"size_bytes": 101}),)
    changed = command.model_copy(
        update={
            "members": members,
            "observation_digest": observation_digest(
                members,
                parent_context=command.parent_context,
                grouping=command.grouping,
            ),
        }
    )
    assert not h.store.intake_observation_unchanged(adapter, changed)
    stale = command.model_copy(
        update={"expected_package_version": command.expected_package_version - 1}
    )
    assert not h.store.intake_observation_unchanged(adapter, stale)
    with pytest.raises(m.WorkflowError, match="cas_conflict"):
        h.store.observe_intake(adapter, h.meta(), stale)
    other = h.component("intake_adapter")[2]
    assert not h.store.intake_observation_unchanged(other, command)
    view = h.store.get_intake(adapter, observed.package_id)
    h.store.record_intake_source_notice(
        adapter,
        h.meta(),
        dm.IntakeSourceNotice(
            expected_version=view.version,
            package_id=view.package_id,
            root_config_revision=view.root_config_revision,
            source_generation=view.source_generation,
            observed_at=h.clock.now(),
            kind="dirty",
            trigger="notification",
        ),
    )
    assert not h.store.intake_observation_unchanged(
        adapter, rescan(h, adapter, observed)
    )


def test_disabled_root_observation_is_not_skipped(intake):
    h, adapter, observed = observed_package(intake)
    _, _, _, root = intake
    force(h, observed.package_id, state="unknown_owner", reason="recovery_required")
    assert h.store.intake_observation_unchanged(adapter, rescan(h, adapter, observed))
    h.store.configure_intake_root(
        h.operator,
        h.meta(),
        root.model_copy(update={"expected_version": 1, "enabled": False}),
    )
    assert not h.store.intake_observation_unchanged(
        adapter, rescan(h, adapter, observed)
    )


# ── prune ────────────────────────────────────────────────────────────────
def adapter_history(intake):
    """Four adapter kinds at T, one observe at T+30 min, one more at T+2 h."""
    h, _, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    observed = fixtures.observation(h, root)
    first = h.store.observe_intake(adapter, h.meta(), observed)
    scan = begin(h, root)
    started = h.store.begin_intake_root_scan(adapter, h.meta(), scan)
    seen = h.store.mark_intake_scan_seen(
        adapter,
        h.meta(),
        dm.IntakeScanSeen(
            package_id=observed.package_id,
            expected_version=first.version,
            root_id=root.root_id,
            root_config_revision=1,
            scan_id=scan.scan_id,
        ),
    )
    h.store.finish_intake_root_scan(adapter, h.meta(), finish(h, scan, started.version))
    for minutes in (30, 90):
        h.clock.value += timedelta(minutes=minutes)
        seen = h.store.observe_intake(
            adapter,
            h.meta(),
            observed.model_copy(
                update={
                    "expected_package_version": seen.version,
                    "observed_at": h.clock.now(),
                }
            ),
        )
    return h, adapter, seen.audit_id


def kinds(h):
    return sorted(
        r[0] for r in h.store._conn.execute("SELECT command_kind FROM request_results")
    ), sorted(r[0] for r in h.store._conn.execute("SELECT command_kind FROM audit"))


def test_prune_deletes_old_adapter_rows_in_batches_and_keeps_everything_else(
    intake, monkeypatch
):
    h, adapter, recent = adapter_history(intake)
    monkeypatch.setattr(intake_store, "_PRUNE_BATCH", 2)
    before = h.clock.now() - timedelta(hours=1)
    batches = []
    while batch := h.store.prune_intake_history(adapter, before=before):
        batches.append(batch)
    assert batches == [2, 2, 1]
    left, audit = kinds(h)
    assert (
        left == audit == ["configure_intake_root", "grant_component", "observe_intake"]
    )
    assert [
        tuple(r)
        for r in h.store._conn.execute(
            "SELECT seq FROM audit WHERE command_kind='observe_intake'"
        )
    ] == [(recent,)]
    assert h.store._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert h.store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_failed_prune_batch_deletes_nothing_and_restores_foreign_keys(
    intake, monkeypatch
):
    import sqlite3

    h, adapter, _ = adapter_history(intake)
    before = h.clock.now() - timedelta(hours=1)
    rows = counts(h)
    h.store._conn.execute(
        "CREATE TEMP TRIGGER refuse_audit_delete BEFORE DELETE ON main.audit "
        "BEGIN SELECT RAISE(ABORT,'synthetic'); END"
    )
    with pytest.raises(m.WorkflowError, match="authority_unavailable"):
        h.store.prune_intake_history(adapter, before=before)
    assert counts(h) == rows
    assert h.store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    h.store._conn.execute("DROP TRIGGER refuse_audit_delete")

    def unavailable():
        raise sqlite3.OperationalError("synthetic")

    with monkeypatch.context() as patcher:
        patcher.setattr(h.store, "_observe_time", unavailable)
        with pytest.raises(m.WorkflowError, match="authority_unavailable"):
            h.store.prune_intake_history(adapter, before=before)
    assert counts(h) == rows
    assert h.store._conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    observer = h.component("observer")[2]
    for principal in (observer, h.operator):
        with pytest.raises(m.WorkflowError, match="forbidden"):
            h.store.prune_intake_history(principal, before=before)
    assert h.store.prune_intake_history(adapter, before=before) == 5
