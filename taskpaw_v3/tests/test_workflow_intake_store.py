"""Public intake commands persist observations before allowing a private snapshot."""

from datetime import timedelta

import pytest

from taskpaw_v3.tests.test_workflow_store import Harness, identity
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.manifest import observation_digest
from taskpaw_v3.workflow.store import WorkflowStore


def native(h, root_id, object_id):
    return m.NativeIdentity(
        scheme="windows_file_id",
        root_id=root_id,
        volume_or_export_id="synthetic-volume",
        object_id=object_id,
        observed_at=h.clock.now(),
        evidence_id=identity(),
    )


@pytest.fixture
def intake(tmp_path):
    h = Harness(tmp_path / "intake.db")
    grant, _, adapter, _ = h.component("intake_adapter")
    root_id, work_root_id = identity(), identity()
    film_policy = m.FilmPolicy(
        policy_revision=1,
        required_stages=(
            "intake",
            "restore",
            "asr",
            "translate",
            "return",
            "scrape",
            "archive",
            "cleanup_input",
            "cleanup_workcopy",
        ),
        subtitle_requirement="chinese",
        required_image_roles=("poster",),
        folder_cover_required=False,
    )
    root = im.IntakeRootPut(
        expected_version=0,
        root_id=root_id,
        native_identity=native(h, root_id, "input-root"),
        private_path="C:/synthetic/input",
        work_root_id=work_root_id,
        work_native_identity=native(h, work_root_id, "work-root"),
        private_work_path="C:/synthetic/work",
        component_id=grant.component_id,
        policy=im.IntakePolicy(policy_revision=1),
        film_policy=film_policy,
        profiles=(),
        enabled=True,
    )
    try:
        yield h, grant, adapter, root
    finally:
        h.store.close()


def observation(h, root, **changes):
    member_id, film_id = identity(), identity()
    members = (
        im.ObservedMember(
            member_id=member_id,
            relative_path="MOV-001.mp4",
            entry_kind="file",
            native_identity=native(h, root.root_id, "input-video"),
            size_bytes=100,
            mtime_ns=123,
        ),
    )
    grouping = im.PackageGrouping(
        grouping_revision=1,
        film_groups=(
            im.FilmGroup(
                group_key="mov-001",
                film_id=film_id,
                video_member_ids=(member_id,),
                members=(
                    im.GroupedMember(
                        member_id=member_id, role="main_video", volume_ordinal=1
                    ),
                ),
            ),
        ),
        parent_context="Performer",
        status="ready",
        reasons=(),
    )
    data = dict(
        root_id=root.root_id,
        root_config_revision=1,
        package_id=identity(),
        expected_package_version=0,
        package_native_identity=native(h, root.root_id, "package"),
        relative_path="Performer/MOV-001",
        parent_context="Performer",
        observed_at=h.clock.now(),
        coverage="complete",
        trigger="startup",
        members=members,
        grouping=grouping,
        writer_state="clear",
        observation_digest=observation_digest(
            members, parent_context="Performer", grouping=grouping
        ),
    )
    data.update(changes)
    return im.IntakeObservation(**data)


def test_root_and_first_observation_are_durable_and_idempotent(intake):
    h, _, adapter, root = intake
    setup_meta = h.meta()
    first = h.store.configure_intake_root(h.operator, setup_meta, root)
    assert first.version == 1
    assert h.store.configure_intake_root(h.operator, setup_meta, root) == first
    observed = observation(h, root)
    meta = h.meta()
    recorded = h.store.observe_intake(adapter, meta, observed)
    assert h.store.observe_intake(adapter, meta, observed) == recorded
    view = h.store.get_intake(h.operator, observed.package_id)
    assert view.source_generation == 1 and view.state == "waiting_stability"
    assert view.grouping == observed.grouping and view.operation_id is None
    h.store.close()
    h.store = WorkflowStore.open(h.path, clock=h.clock)
    view = view.model_copy(update={"version": view.version + 1})
    assert h.store.get_intake(h.operator, observed.package_id) == view
    assert h.store.list_intakes(adapter, im.IntakeListQuery()).items == (view,)


def test_observation_requires_the_configured_component_and_revision(intake):
    h, _, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    other = h.component("intake_adapter")[2]
    observed = observation(h, root)
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.observe_intake(other, h.meta(), observed)
    with pytest.raises(m.WorkflowError, match="binding_changed"):
        h.store.observe_intake(
            adapter, h.meta(), observed.model_copy(update={"root_config_revision": 2})
        )
    h.store.observe_intake(adapter, h.meta(), observed)
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.get_intake(other, observed.package_id)
    assert not h.store.list_intakes(other, im.IntakeListQuery()).items


def test_only_operator_can_configure_a_root_and_disabled_roots_cannot_observe(intake):
    h, _, adapter, root = intake
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.configure_intake_root(adapter, h.meta(), root)
    h.store.configure_intake_root(
        h.operator, h.meta(), root.model_copy(update={"enabled": False})
    )
    with pytest.raises(m.WorkflowError, match="intake_disabled"):
        h.store.observe_intake(adapter, h.meta(), observation(h, root))


def test_changed_members_advance_source_generation_but_unchanged_rescan_does_not(
    intake,
):
    h, _, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    observed = observation(h, root)
    first = h.store.observe_intake(adapter, h.meta(), observed)
    h.clock.value += timedelta(seconds=30)
    rescan = observed.model_copy(
        update={
            "expected_package_version": first.version,
            "observed_at": h.clock.now(),
            "trigger": "rescan",
        }
    )
    second = h.store.observe_intake(adapter, h.meta(), rescan)
    assert h.store.get_intake(adapter, observed.package_id).source_generation == 1
    changed = (observed.members[0].model_copy(update={"size_bytes": 101}),)
    third = rescan.model_copy(
        update={
            "expected_package_version": second.version,
            "members": changed,
            "observation_digest": observation_digest(
                changed, parent_context=rescan.parent_context, grouping=rescan.grouping
            ),
        }
    )
    h.store.observe_intake(adapter, h.meta(), third)
    assert h.store.get_intake(adapter, observed.package_id).source_generation == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("coverage", "unknown"),
        ("writer_state", "busy"),
        ("package_native_identity", None),
    ],
)
def test_incomplete_or_unsafe_observation_invalidates_generation_immediately(
    intake, field, value
):
    h, _, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    observed = observation(h, root)
    result = h.store.observe_intake(adapter, h.meta(), observed)
    changed = observed.model_copy(
        update={field: value, "expected_package_version": result.version}
    )
    h.store.observe_intake(adapter, h.meta(), changed)
    view = h.store.get_intake(h.operator, observed.package_id)
    assert view.source_generation == 2
    assert view.state not in ("operational_ready", "snapshot_ready", "snapshotting")


def test_bad_digest_cas_and_future_time_leave_the_observation_unchanged(intake):
    h, _, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    observed = observation(h, root)
    result = h.store.observe_intake(adapter, h.meta(), observed)
    before = h.store.get_intake(adapter, observed.package_id)
    for changes, reason in (
        ({"expected_package_version": 999}, "cas_conflict"),
        (
            {
                "expected_package_version": result.version,
                "observation_digest": "b" * 64,
            },
            "observation_digest_mismatch",
        ),
        (
            {
                "expected_package_version": result.version,
                "observed_at": h.clock.now() + timedelta(seconds=1),
            },
            "observation_time_invalid",
        ),
    ):
        with pytest.raises(m.WorkflowError, match=reason):
            h.store.observe_intake(
                adapter, h.meta(), observed.model_copy(update=changes)
            )
        assert h.store.get_intake(adapter, observed.package_id) == before


def test_reconfiguring_a_root_invalidates_previous_package_configuration(intake):
    h, _, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    observed = observation(h, root)
    h.store.observe_intake(adapter, h.meta(), observed)
    h.store.configure_intake_root(
        h.operator,
        h.meta(),
        root.model_copy(update={"expected_version": 1, "enabled": False}),
    )
    view = h.store.get_intake(h.operator, observed.package_id)
    assert view.source_generation == 2 and view.state == "blocked"
    assert view.root_config_revision == 2
    with pytest.raises(m.WorkflowError, match="cas_conflict"):
        h.store.configure_intake_root(h.operator, h.meta(), root)


def test_observer_receives_only_summary_and_no_grouping_payload(intake):
    h, _, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    observed = observation(h, root)
    h.store.observe_intake(adapter, h.meta(), observed)
    observer = h.component("observer")[2]
    view = h.store.get_intake(observer, observed.package_id)
    assert (
        view.grouping is None and view.media_probes == () and view.eligibility is None
    )
    assert "C:/synthetic" not in view.model_dump_json()


def test_only_one_enabled_input_root_is_allowed(intake):
    h, _, _, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    second_id, second_work_id = identity(), identity()
    second = root.model_copy(
        update={
            "root_id": second_id,
            "native_identity": native(h, second_id, "second-input"),
            "private_path": "D:/synthetic/input",
            "work_root_id": second_work_id,
            "work_native_identity": native(h, second_work_id, "second-work"),
            "private_work_path": "D:/synthetic/work",
        }
    )
    with pytest.raises(m.WorkflowError, match="input_root_already_enabled"):
        h.store.configure_intake_root(h.operator, h.meta(), second)
    h.store.configure_intake_root(
        h.operator, h.meta(), second.model_copy(update={"enabled": False})
    )


def test_duplicate_discovery_returns_existing_package_without_creating_another(intake):
    h, _, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    observed = observation(h, root)
    h.store.observe_intake(adapter, h.meta(), observed)
    duplicate = observed.model_copy(update={"package_id": identity()})
    result = h.store.observe_intake(adapter, h.meta(), duplicate)
    assert result.entity_id == observed.package_id
    items = h.store.list_intakes(adapter, im.IntakeListQuery()).items
    assert len(items) == 1
    assert items[0].source_generation == 1


def test_same_path_with_a_different_native_directory_creates_new_package(intake):
    h, _, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    original = observation(h, root)
    h.store.observe_intake(adapter, h.meta(), original)
    replacement = observation(
        h, root, package_native_identity=native(h, root.root_id, "new-directory")
    )
    result = h.store.observe_intake(adapter, h.meta(), replacement)
    assert result.entity_id == replacement.package_id != original.package_id
    old = h.store.get_intake(adapter, original.package_id)
    new = h.store.get_intake(adapter, replacement.package_id)
    assert old.state == "missing" and old.grouping == original.grouping
    assert new.state == "waiting_stability" and new.grouping == replacement.grouping
    assert old.grouping.film_groups[0].film_id != new.grouping.film_groups[0].film_id


def test_proven_directory_rename_retains_canonical_film_identity(intake):
    h, _, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    original = observation(h, root)
    h.store.observe_intake(adapter, h.meta(), original)
    candidate = original.model_copy(
        update={"package_id": identity(), "relative_path": "Performer/Renamed"}
    )
    duplicate = h.store.observe_intake(adapter, h.meta(), candidate)
    assert duplicate.entity_id == original.package_id
    current = h.store.get_intake(adapter, original.package_id)
    updated = candidate.model_copy(
        update={
            "package_id": original.package_id,
            "expected_package_version": current.version,
        }
    )
    h.store.observe_intake(adapter, h.meta(), updated)
    assert (
        h.store.get_intake(adapter, original.package_id).grouping == original.grouping
    )


def test_operator_grouping_correction_requires_new_observation_and_snapshot(intake):
    h, _, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    observed = observation(h, root)
    recorded = h.store.observe_intake(adapter, h.meta(), observed)
    grouping = observed.grouping.model_copy(update={"grouping_revision": 2})
    command = im.IntakeGroupingPut(
        expected_version=recorded.version,
        package_id=observed.package_id,
        source_generation=1,
        grouping=grouping,
    )
    with pytest.raises(m.WorkflowError, match="forbidden"):
        h.store.resolve_intake_grouping(adapter, h.meta(), command)
    meta = h.meta()
    updated = h.store.resolve_intake_grouping(h.operator, meta, command)
    assert h.store.resolve_intake_grouping(h.operator, meta, command) == updated
    view = h.store.get_intake(adapter, observed.package_id)
    assert view.source_generation == 2 and view.grouping == grouping
    assert view.state == "waiting_stability"
    assert not view.media_probes and view.eligibility is None


def test_grouping_correction_cannot_omit_an_observed_member(intake):
    h, _, adapter, root = intake
    h.store.configure_intake_root(h.operator, h.meta(), root)
    observed = observation(h, root)
    recorded = h.store.observe_intake(adapter, h.meta(), observed)
    grouping = im.PackageGrouping(
        grouping_revision=2,
        film_groups=(),
        parent_context=observed.parent_context,
        status="needs_attention",
        reasons=("ambiguous_grouping",),
    )
    with pytest.raises(m.WorkflowError, match="grouping_coverage_mismatch"):
        h.store.resolve_intake_grouping(
            h.operator,
            h.meta(),
            im.IntakeGroupingPut(
                expected_version=recorded.version,
                package_id=observed.package_id,
                source_generation=1,
                grouping=grouping,
            ),
        )
