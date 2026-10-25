"""Real held-directory observations, with persistent identities reused explicitly."""

import os
import sys
from datetime import datetime, timezone

import pytest

from taskpaw_v3.tests import test_workflow_intake_files as fixtures
from taskpaw_v3.tests.test_workflow_intake_build import work_request
from taskpaw_v3.tests.test_workflow_intake_files import held
from taskpaw_v3.workflow import intake_discovery as discovery

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Native source discovery"
)
package = fixtures.package


def test_link_metadata_only_changes_preserve_observation_but_changed_bytes_do_not(
    package, tmp_path, monkeypatch
):
    from taskpaw_v3.tests.test_workflow_intake_build import copied
    from taskpaw_v3.tests.test_workflow_media_facts import parse
    from taskpaw_v3.tests.test_workflow_media_facts import source as probe_source
    from taskpaw_v3.workflow.intake_build import build_snapshot
    from taskpaw_v3.workflow.intake_files import HeldPackage

    with held(package) as source:
        request = work_request(source, tmp_path)
        with copied(source, request) as target:
            target.copy_members()
            snapshot = build_snapshot(
                request,
                source=source,
                copy=target,
                facts={v: parse(probe_source()) for v in request.video_ids},
            )
            target.publish(snapshot.model_dump_json().encode())
    cache = {}
    reads = []
    actual_chunks = HeldPackage.chunks

    def counted(self, relative):
        reads.append(relative)
        yield from actual_chunks(self, relative)

    monkeypatch.setattr(HeldPackage, "chunks", counted)

    def observe():
        return discovery.observe_package(
            request.root,
            request.observation.relative_path,
            previous=request.observation,
            package_version=1,
            observed_at=datetime.now(timezone.utc),
            trigger="rescan",
            snapshot=snapshot,
            linked_tokens=cache,
        )

    assert observe().observation_digest == request.observation.observation_digest
    assert reads == []  # exact stored post-link token needs no second full read
    os.link(target.snapshot_path / "movie.mp4", tmp_path / "returned.mp4")
    assert observe().observation_digest == request.observation.observation_digest
    assert reads == ["movie.mp4"]
    assert observe().observation_digest == request.observation.observation_digest
    assert reads == ["movie.mp4"]  # accepted later token is cached
    original = package[1] / "movie.mp4"
    before = original.stat()
    with original.open("r+b") as stream:
        stream.write(b"changed")
    os.utime(original, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert observe().observation_digest != request.observation.observation_digest


def config_for(package, tmp_path):
    with held(package) as source:
        return work_request(source, tmp_path).root


def scan(config, previous=None, *, relative="Actor/Film", version=0):
    return discovery.observe_package(
        config,
        relative,
        previous=previous,
        package_version=version,
        observed_at=datetime.now(timezone.utc),
        trigger="rescan",
    )


def test_repeat_native_observation_retains_package_members_films_and_digest(
    package, tmp_path
):
    config = config_for(package, tmp_path)
    first = scan(config)
    second = scan(config, first, version=1)
    assert second.package_id == first.package_id
    assert second.observation_digest == first.observation_digest
    assert second.grouping == first.grouping
    assert [v.member_id for v in second.members] == [v.member_id for v in first.members]
    assert second.coverage == "complete" and second.writer_state == "clear"


def test_actual_package_and_video_rename_preserve_canonical_film(package, tmp_path):
    config = config_for(package, tmp_path)
    first = scan(config)
    (package[1] / "movie.mp4").rename(package[1] / "AAA-001.mp4")
    (package[1] / "movie.zh.srt").rename(package[1] / "AAA-001.zh.srt")
    package[1].rename(package[1].with_name("Renamed"))
    second = scan(config, first, version=1, relative="Actor/Renamed")
    assert second.package_id == first.package_id
    assert (
        second.grouping.film_groups[0].film_id == first.grouping.film_groups[0].film_id
    )
    assert second.grouping.grouping_revision == first.grouping.grouping_revision + 1
    assert second.observation_digest != first.observation_digest


def test_same_path_different_directory_creates_new_package_and_film(package, tmp_path):
    config = config_for(package, tmp_path)
    first = scan(config)
    package[1].rename(package[1].with_name("Previous"))
    package[1].mkdir()
    (package[1] / "movie.mp4").write_bytes(b"new object")
    second = scan(config, first, version=1)
    assert (
        second.package_id != first.package_id and second.expected_package_version == 0
    )
    assert (
        second.grouping.film_groups[0].film_id != first.grouping.film_groups[0].film_id
    )


def test_busy_writer_is_partial_and_does_not_claim_empty_complete(package, tmp_path):
    config = config_for(package, tmp_path)
    first = scan(config)
    with (package[1] / "movie.mp4").open("r+b"):
        second = scan(config, first, version=1)
    assert second.coverage == "partial" and second.writer_state == "busy"
    assert second.package_id == first.package_id
    assert second.members == first.members


def test_invalid_nfo_and_temporary_member_are_preserved_with_reason(package, tmp_path):
    config = config_for(package, tmp_path)
    (package[1] / "movie.nfo").write_bytes(
        b'<!DOCTYPE movie SYSTEM "https://invalid.example/x"><movie/>'
    )
    (package[1] / "pending.part").write_bytes(b"incomplete")
    observed = scan(config)
    assert observed.coverage == "complete"
    assert any(
        v.relative_path == "pending.part" and v.temporary for v in observed.members
    )
    assert "invalid_nfo" in observed.grouping.reasons
    assert observed.grouping.status == "needs_attention"


def test_nfo_bytes_above_cap_are_not_read_as_a_valid_hint(package, tmp_path):
    config = config_for(package, tmp_path)
    (package[1] / "movie.nfo").write_bytes(b" " * 262145)
    observed = scan(config)
    assert "invalid_nfo" in observed.grouping.reasons
    assert any(v.relative_path == "movie.nfo" for v in observed.members)


def test_discovery_close_failure_returns_exact_retained_owner(
    package, tmp_path, monkeypatch
):
    from taskpaw_v3.workflow.intake_files import IntakeFileError, WindowsFiles

    config = config_for(package, tmp_path)
    original, failed = WindowsFiles.close, []

    def fail_once(api, handle):
        if not failed:
            failed.append(handle)
            raise IntakeFileError("close_failed")
        return original(api, handle)

    monkeypatch.setattr(WindowsFiles, "close", fail_once)
    with pytest.raises(discovery.DiscoveryUnresolved) as caught:
        scan(config)
    pending = caught.value.pending_owner
    assert not pending.closed
    assert failed[0] in pending._holds.handles
    pending.close()
    assert pending.closed
