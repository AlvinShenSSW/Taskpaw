"""Real owned copies plus synthetic probe facts; not native ffprobe execution."""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from taskpaw_v3.tests import test_workflow_intake_files as fixtures
from taskpaw_v3.tests.test_workflow_intake_files import held
from taskpaw_v3.tests.test_workflow_media_facts import parse
from taskpaw_v3.tests.test_workflow_media_facts import source as probe_source
from taskpaw_v3.workflow import intake_build as build
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow import intake_protocol as wire
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.intake_copy import PrivateCopy
from taskpaw_v3.workflow.intake_files import IntakeFileError, inspect_root
from taskpaw_v3.workflow.intake_snapshot import validate_snapshot
from taskpaw_v3.workflow.manifest import group_keys, group_package, observation_digest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows native source/copy"
)
package = fixtures.package


def uid():
    return str(uuid4())


def work_request(source, tmp_path):
    stamp, work_id = datetime.now(timezone.utc), uid()
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    members = tuple(
        im.ObservedMember(
            member_id=uid(),
            relative_path=relative,
            entry_kind="directory" if v.directory else "file",
            native_identity=v.identity,
            size_bytes=v.size_bytes,
            mtime_ns=v.mtime_ns,
            change_token=str(v.change_filetime),
            temporary=v.temporary,
        )
        for relative, v in source.entries.items()
    )
    grouping = group_package(
        members,
        film_ids=tuple(
            im.FilmIdentity(group_key=key, film_id=uid()) for key in group_keys(members)
        ),
        parent_context="Actor",
        grouping_revision=1,
    )
    assert grouping.status == "ready", grouping.reasons
    observation = im.IntakeObservation(
        root_id=source.root_id,
        root_config_revision=1,
        package_id=uid(),
        expected_package_version=1,
        package_native_identity=source.package_identity,
        relative_path="Actor/Film",
        parent_context="Actor",
        observed_at=stamp,
        coverage="complete",
        trigger="rescan",
        members=members,
        observation_digest=observation_digest(
            members, parent_context="Actor", grouping=grouping
        ),
        grouping=grouping,
        writer_state="clear",
    )
    root = im.IntakeRootPut(
        expected_version=0,
        root_id=source.root_id,
        native_identity=source.expected_root,
        private_path=str(source.root),
        work_root_id=work_id,
        work_native_identity=inspect_root(work, work_id),
        private_work_path=str(work),
        component_id=uid(),
        policy=source.policy,
        film_policy=m.FilmPolicy(
            policy_revision=1,
            required_stages=(
                "intake",
                "return",
                "scrape",
                "archive",
                "cleanup_input",
                "cleanup_workcopy",
            ),
            subtitle_requirement="none",
            required_image_roles=("poster",),
            folder_cover_required=False,
        ),
        enabled=True,
    )
    admission = im.IntakeSnapshotAdmission(
        expected_version=1,
        package_id=observation.package_id,
        source_generation=1,
        root_config_revision=1,
        grouping_revision=1,
        observation_digest=observation.observation_digest,
        operation_id=uid(),
        snapshot_id=uid(),
        owner_boot_id=uid(),
        policy_revision=1,
    )
    return wire.IntakeWorkRequest(
        owner_id=uid(),
        producer_id=uid(),
        admission=admission,
        root=root,
        observation=observation,
        stable_started_at=stamp - timedelta(seconds=60),
        input_revisions=tuple(
            wire.FilmInputRevision(film_id=g.film_id, input_revision=1)
            for g in grouping.film_groups
        ),
        ffprobe=wire.ProbeArtifact(
            executable="C:/synthetic/ffprobe.exe",
            sha256="0" * 64,
            size_bytes=1,
            version="fixture-7",
        ),
    )


def copied(source, request):
    return PrivateCopy(
        source,
        Path(request.root.private_work_path),
        root_id=request.root.work_root_id,
        expected_root=request.root.work_native_identity,
        operation_id=request.operation_id,
    )


@pytest.mark.parametrize("multiple", [False, True])
def test_complete_package_builds_exact_per_film_manifest_and_linked_snapshot_references(
    package, tmp_path, multiple
):
    _, directory, _, _ = package
    (directory / "movie.mp4").rename(directory / "AAA-001.mp4")
    (directory / "movie.zh.srt").rename(directory / "AAA-001.zh.srt")
    if multiple:
        (directory / "BBB-002.mp4").write_bytes(b"second synthetic media")
        (directory / "folder.jpg").write_bytes(b"synthetic common image")
    with held(package) as source:
        request = work_request(source, tmp_path)
        with copied(source, request) as target:
            target.copy_members()
            facts = {member: parse(probe_source()) for member in request.video_ids}
            snapshot = build.build_snapshot(
                request, source=source, copy=target, facts=facts
            )
            assert len(snapshot.manifests) == (2 if multiple else 1)
            assert snapshot.eligibility.decision == "profile_required"
            assert all(
                v.linked and v.linked_change_token
                for v in snapshot.members
                if v.entry_kind == "file"
            )
            assert snapshot.workcopy_verification.kind == "linked_snapshot_sha256"
            validate_snapshot(
                snapshot,
                request.admission,
                request.observation,
                request.root,
                producer_id=request.producer_id,
                now=datetime.now(timezone.utc),
            )
            for manifest in snapshot.manifests:
                for item in manifest.readiness.workcopy.items:
                    assert item.relative_path.startswith(
                        snapshot.relative_work_path + "/"
                    )
                    assert item.native_identity.root_id == request.root.work_root_id
            target.publish(snapshot.model_dump_json().encode())
            assert (
                im.PackageSnapshot.model_validate_json(
                    (target.operation_path / "completion.json").read_bytes()
                )
                == snapshot
            )
            assert all(
                (Path(request.root.private_work_path) / item.relative_path).is_file()
                for manifest in snapshot.manifests
                for item in manifest.readiness.workcopy.items
            )


def test_missing_probe_does_not_omit_a_video_from_the_snapshot(package, tmp_path):
    with held(package) as source:
        request = work_request(source, tmp_path)
        with copied(source, request) as target:
            target.copy_members()
            with pytest.raises(IntakeFileError, match="probe_coverage"):
                build.build_snapshot(request, source=source, copy=target, facts={})


def test_unknown_raw_fact_survives_complete_copy_without_becoming_eligible(
    package, tmp_path
):
    with held(package) as source:
        request = work_request(source, tmp_path)
        with copied(source, request) as target:
            target.copy_members()
            raw = parse(probe_source(width=None, height=None))
            snapshot = build.build_snapshot(
                request, source=source, copy=target, facts={request.video_ids[0]: raw}
            )
            assert snapshot.media_probes[0].raw_facts == raw
            assert snapshot.eligibility.decision == "blocked"
            assert snapshot.media_probes[0].width is None


def test_discovery_identity_mismatch_rejects_copy_evidence(package, tmp_path):
    with held(package) as source:
        request = work_request(source, tmp_path)
        with copied(source, request) as target:
            target.copy_members()
            old = request.observation.members[0]
            changed = old.model_copy(update={"size_bytes": old.size_bytes + 1})
            bad = request.model_copy(
                update={
                    "observation": request.observation.model_copy(
                        update={"members": (changed, *request.observation.members[1:])}
                    )
                }
            )
            with pytest.raises(IntakeFileError, match="source_changed"):
                build.build_snapshot(
                    bad,
                    source=source,
                    copy=target,
                    facts={
                        member: parse(probe_source()) for member in request.video_ids
                    },
                )
