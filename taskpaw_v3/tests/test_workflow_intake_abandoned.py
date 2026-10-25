"""An intake operation left by an exited controller boot can be settled (T03-P1 D01)."""

import json
import sqlite3
import sys
import threading
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from taskpaw_v3.tests import test_workflow_intake_store as fixtures
from taskpaw_v3.tests.test_workflow_intake_operations import prepare
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.auth import authenticate, digest_secret
from taskpaw_v3.workflow.intake_reclaim import (
    intake_orphans,
    reclaim_intake,
    remove_work_tree,
)
from taskpaw_v3.workflow.local_runtime import LocalRuntime
from taskpaw_v3.workflow.store import WorkflowStore

intake = fixtures.intake


def next_boot_adapter(h, grant):
    """The controller's next boot: same component, new principal and boot."""
    secret = SecretStr("synthetic-next-boot-intake-credential")
    value = m.ComponentGrant(
        component_id=grant.component_id,
        principal_id=identity(),
        credential_id=identity(),
        boot_id=identity(),
        generation=1,
        role="intake_adapter",
        secret_digest=digest_secret(secret),
        expires_at=h.clock.now() + timedelta(days=1),
    )
    h.store.grant_component(h.operator, h.meta(), value)
    return value, authenticate(h.store, value.credential_id, secret)


def abandoned(intake):
    """Admit a snapshot, then 'crash' the controller by reopening the store."""
    h, grant, adapter, root = intake
    observed, command = prepare(intake)
    h.store.begin_intake_snapshot(adapter, h.meta(), command)
    h.store.close()
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    return observed, command


def settle(h, op, boot_id):
    return h.store.settle_abandoned_intake(
        h.operator,
        h.meta(),
        im.IntakeAbandonedSettlement(
            expected_version=op.version,
            operation_id=op.operation_id,
            current_owner_boot_id=boot_id,
        ),
    )


def test_previous_boot_operation_is_cancelled_and_intake_resumes(intake):
    h, grant, _, root = intake
    observed, command = abandoned(intake)
    nxt, adapter = next_boot_adapter(h, grant)
    (op,) = h.store.list_unresolved_intake_operations(h.operator)
    assert (op.operation_id, op.owner_boot_id) == (command.operation_id, grant.boot_id)

    assert settle(h, op, nxt.boot_id).status == "cancelled"
    assert h.store.list_unresolved_intake_operations(h.operator) == ()
    view = h.store.get_intake(adapter, observed.package_id)
    assert view.state == "waiting_stability" and view.operation_id is None

    # A fresh stability window and snapshot under the new boot are admitted.
    seen = h.store.observe_intake(
        adapter,
        h.meta(),
        observed.model_copy(
            update={
                "expected_package_version": view.version,
                "observed_at": h.clock.now(),
            }
        ),
    )
    h.clock.value += timedelta(seconds=root.policy.stable_seconds)
    stable = h.store.observe_intake(
        adapter,
        h.meta(),
        observed.model_copy(
            update={
                "expected_package_version": seen.version,
                "observed_at": h.clock.now(),
                "trigger": "rescan",
            }
        ),
    )
    again = command.model_copy(
        update={
            "expected_version": stable.version,
            "operation_id": identity(),
            "snapshot_id": identity(),
            "owner_boot_id": nxt.boot_id,
        }
    )
    assert h.store.begin_intake_snapshot(adapter, h.meta(), again).status == "admitted"


def test_settlement_is_idempotent_and_never_reopens(intake):
    h, grant, _, _ = intake
    abandoned(intake)
    nxt, _ = next_boot_adapter(h, grant)
    (op,) = h.store.list_unresolved_intake_operations(h.operator)
    meta = h.meta()
    command = im.IntakeAbandonedSettlement(
        expected_version=op.version,
        operation_id=op.operation_id,
        current_owner_boot_id=nxt.boot_id,
    )
    first = h.store.settle_abandoned_intake(h.operator, meta, command)
    assert h.store.settle_abandoned_intake(h.operator, meta, command) == first
    later = h.store.get_intake_operation(h.operator, op.operation_id)
    assert later.state == "cancelled" and not later.publication_allowed


def test_same_boot_unknown_keeps_the_t02_blocking_rule(intake):
    h, grant, adapter, _ = intake
    abandoned(intake)
    (op,) = h.store.list_unresolved_intake_operations(h.operator)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        settle(h, op, grant.boot_id)
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        settle(h, op, identity())  # no intake principal holds that boot
    assert len(h.store.list_unresolved_intake_operations(h.operator)) == 1


def test_only_the_operator_settles_or_lists(intake):
    h, grant, _, _ = intake
    abandoned(intake)
    nxt, adapter = next_boot_adapter(h, grant)
    (op,) = h.store.list_unresolved_intake_operations(h.operator)
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.list_unresolved_intake_operations(adapter)
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.settle_abandoned_intake(
            adapter,
            h.meta(),
            im.IntakeAbandonedSettlement(
                expected_version=op.version,
                operation_id=op.operation_id,
                current_owner_boot_id=nxt.boot_id,
            ),
        )


@pytest.fixture
def orphan_tree(tmp_path):
    # Read-side projection: deliberately includes running AND unknown owners to
    # exercise every keep rule together, without weakening the real store's
    # one-unresolved-owner index. Real store joins are covered by removal/runner tests.
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE intake_operations(operation_id, package_id, state, unresolved);
        CREATE TABLE intake_packages(package_id, active_operation_id);
        CREATE TABLE films(film_id, current_input_revision, body_json);
        CREATE TABLE intake_snapshots(snapshot_id, operation_id, body_json);
        CREATE TABLE intake_films(film_id, input_revision, snapshot_id);
    """)
    work = tmp_path / "work"
    paths = {}
    for name, state in (
        ("orphan", "completed"),
        ("active", "completed"),
        ("running", "running"),
        ("unknown", "unknown"),
        ("live", "completed"),
        ("admitted", "admitted"),
        ("stopping", "stopping"),
    ):
        op = identity()
        conn.execute(
            "INSERT INTO intake_packages VALUES (?,?)",
            (op, op if name == "active" else None),
        )
        conn.execute(
            "INSERT INTO intake_operations VALUES (?,?,?,?)",
            (
                op,
                op,
                state,
                int(state in {"running", "unknown", "admitted", "stopping"}),
            ),
        )
        paths[name] = work / "intake" / op
    paths["unrecognized"] = work / "intake" / identity()
    for path in paths.values():
        for name in ("snapshot", "partial"):
            (path / name).mkdir(parents=True)
            (path / name / "film.bin").write_bytes(b"123")
        (path / "completion.json").write_text("{}")
    loose = work / "intake" / "loose.txt"
    loose.write_text("keep")
    # Even a known terminal operation name must be an actual directory.
    conn.execute("INSERT INTO intake_packages VALUES (?,NULL)", (loose.name,))
    conn.execute(
        "INSERT INTO intake_operations VALUES (?,?,'failed',0)",
        (loose.name, loose.name),
    )
    for name, removed in (("orphan", "removed"), ("live", None)):
        conn.execute(
            "INSERT INTO films VALUES (?,2,?)",
            (name, json.dumps({"removed_at": removed})),
        )
        conn.execute("INSERT INTO intake_films VALUES (?,1,?)", (name, name))
        conn.execute(
            "INSERT INTO intake_snapshots VALUES (?,?,?)",
            (
                name,
                paths[name].name,
                json.dumps(
                    {
                        "relative_work_path": (paths[name] / "snapshot")
                        .relative_to(work)
                        .as_posix()
                    }
                ),
            ),
        )
    conn.commit()
    store = SimpleNamespace(
        _conn=conn,
        _lock=threading.RLock(),
        list_unresolved_intake_operations=lambda auth: (),
    )
    try:
        yield store, work, paths
    finally:
        conn.close()


@pytest.mark.parametrize("state", ["completed", "failed", "cancelled"])
def test_startup_dry_run_and_sweep_keep_every_non_orphan(orphan_tree, state, caplog):
    store, work, paths = orphan_tree
    conn = store._conn
    conn.execute(
        "UPDATE intake_operations SET state=? WHERE operation_id=?",
        (state, paths["orphan"].name),
    )
    conn.commit()
    before = tuple(conn.iterdump())
    conn.execute("PRAGMA query_only=ON")
    assert intake_orphans(conn, work) == [paths["orphan"]]
    assert tuple(conn.iterdump()) == before
    assert all((p / "snapshot" / "film.bin").exists() for p in paths.values())
    caplog.set_level("INFO", logger="taskpaw.workflow.intake_reclaim")
    rt = SimpleNamespace(store=store, work_dir=work, operator=None)
    assert LocalRuntime.settle_intake(rt) == 0  # the once-per-boot startup hook
    for name, path in paths.items():
        assert (path / "snapshot").exists() == (name != "orphan")
        assert (path / "partial").exists() == (name != "orphan")
        assert (path / "completion.json").read_text() == "{}"
    assert (work / "intake" / "loose.txt").read_text() == "keep"
    assert paths["orphan"].name in caplog.text
    assert caplog.text.count("bytes_freed=3") == 2
    assert tuple(conn.iterdump()) == before
    assert intake_orphans(conn, work) == []  # metadata-only dirs are not orphans


def test_snapshot_location_not_operation_foreign_key_controls_retention(orphan_tree):
    store, work, paths = orphan_tree
    # A live historical revision referring into the orphan protects it even if
    # the snapshot's operation_id says something else.
    store._conn.execute(
        "UPDATE intake_snapshots SET body_json=? WHERE snapshot_id='live'",
        (
            json.dumps(
                {
                    "relative_work_path": (paths["orphan"] / "snapshot")
                    .relative_to(work)
                    .as_posix()
                }
            ),
        ),
    )
    selected = intake_orphans(store._conn, work)
    assert paths["orphan"] not in selected


@pytest.mark.skipif(sys.platform != "win32", reason="Windows junction safety")
@pytest.mark.parametrize(
    "placement", ["nested", "snapshot", "operation", "root", "live"]
)
def test_orphan_junction_never_deletes_its_outside_target(
    orphan_tree, tmp_path, placement
):
    import _winapi

    store, work, paths = orphan_tree
    orphan = paths["orphan"]
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.bin").write_bytes(b"never delete")
    if placement == "nested":
        link = orphan / "snapshot" / "outside-link"
    elif placement in {"snapshot", "live"}:
        directory = paths["live"] if placement == "live" else orphan
        link = directory / "snapshot"
        link.rename(directory / "saved-snapshot")
    elif placement == "operation":
        link = orphan
        link.rename(work / "saved-operation")
    else:
        link = work / "intake"
        link.rename(work / "saved-intake")
    _winapi.CreateJunction(str(outside), str(link))
    try:
        selected = intake_orphans(store._conn, work)
        assert (orphan in selected) == (placement in {"nested", "snapshot", "live"})
        assert paths["live"] not in selected
        reclaim_intake(store, work)
        assert (outside / "precious.bin").read_bytes() == b"never delete"
        assert link.exists() == (placement in {"operation", "root", "live"})
    finally:
        if link.exists():
            link.rmdir()  # unlink the junction so pytest never cleans its target


@pytest.mark.parametrize("target", ["root", "outside", "traversal", "file"])
def test_reclamation_rejects_root_and_paths_outside_it(
    orphan_tree, tmp_path, target, caplog
):
    _, work, paths = orphan_tree
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.bin").write_bytes(b"keep")
    root = work / "intake"
    path = {
        "root": root,
        "outside": outside,
        "traversal": root / ".." / ".." / "outside",
        "file": root / "loose.txt",
    }[target]
    remove_work_tree(path, root, operation_id="fixture")
    assert (outside / "keep.bin").read_bytes() == b"keep"
    assert (root / "loose.txt").read_text() == "keep"
    assert (paths["orphan"] / "snapshot" / "film.bin").exists()
    assert "reclaim skipped" in caplog.text


def test_reclamation_failure_is_logged_and_does_not_escape_startup(
    orphan_tree, monkeypatch, caplog
):
    store, work, paths = orphan_tree
    original = Path.unlink

    def locked(path, *args, **kwargs):
        if path == paths["orphan"] / "snapshot" / "film.bin":
            raise PermissionError("synthetic locked file")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", locked)
    LocalRuntime.settle_intake(
        SimpleNamespace(store=store, work_dir=work, operator=None)
    )
    assert (paths["orphan"] / "snapshot" / "film.bin").exists()
    assert not (paths["orphan"] / "partial").exists()
    assert (
        paths["orphan"].name in caplog.text and "synthetic locked file" in caplog.text
    )
