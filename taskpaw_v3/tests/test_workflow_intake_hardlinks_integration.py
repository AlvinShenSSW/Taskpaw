"""Native intake/publication through archive and exact cleanup on owned fixtures.

The intake helper and ffprobe are real. Restore and Miko are existing test
adapters; the archive is the real library engine over a temporary NAS directory.
"""

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskpaw_v3.tests import test_workflow_intake_runner as native
from taskpaw_v3.tests import test_workflow_return_step as ret
from taskpaw_v3.tests import test_workflow_scrape_step as scr
from taskpaw_v3.tests.test_workflow_films import STAGES
from taskpaw_v3.tests.test_workflow_finalize import _files
from taskpaw_v3.tests.test_workflow_local_dispatch import plan_film
from taskpaw_v3.tests.test_workflow_media_facts import profile
from taskpaw_v3.tests.workflow_fake_library import FakeLibrary
from taskpaw_v3.workflow import file_identity
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.finalize import Finalizer
from taskpaw_v3.workflow.intake import IntakeService
from taskpaw_v3.workflow.intake_files import inspect_root
from taskpaw_v3.workflow.intake_watch import DirtyBatch
from taskpaw_v3.workflow.stage_results import (
    RestoreFacts,
    VideoResult,
    file_ref,
    main_videos,
    workcopy_path,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Native NTFS intake")
package, ready = native.package, native.ready


class ReuseNativeVideo(ret.FakeRestore):
    """Retain the actual input bytes so later processing links share their ID."""

    def run(self, context):
        (item,) = main_videos(context.manifest)
        (probe,) = context.manifest.readiness.media_probes
        path = workcopy_path(self.rt, context.manifest, item.item_id)
        facts = RestoreFacts(
            decision="reused",
            file=file_ref(self.rt.work_dir, path),
            width=probe.width,
            height=probe.height,
            duration_ms=probe.duration_ms,
            video_streams=probe.video_streams,
            audio_streams=probe.audio_streams,
        )
        return self._done(
            context,
            [VideoResult(video_item_id=item.item_id, volume_ordinal=1, restore=facts)],
        )


@pytest.fixture
def linked_world(ready, tmp_path):
    h, adapter, observed, runner = ready
    original = h.store.get_intake_configuration(adapter, observed.root_id)
    inbox = Path(original.private_path)
    source = inbox / "Actor" / "Film"
    (source / "movie.zh.srt").write_bytes(ret.ZH)
    rt = ret.make_runtime(tmp_path / "linked-runtime")
    rt.work_dir.mkdir(parents=True, exist_ok=True)
    grant, auth = rt.grant_intake_adapter()
    config = original.model_copy(
        update={
            "root_id": rt.identity.root_id,
            "native_identity": inspect_root(inbox, rt.identity.root_id),
            "work_root_id": rt.identity.work_root_id,
            "work_native_identity": inspect_root(rt.work_dir, rt.identity.work_root_id),
            "private_work_path": str(rt.work_dir),
            "component_id": grant.component_id,
            "profiles": (profile(),),
            "policy": original.policy.model_copy(
                update={"stable_seconds": 1, "rescan_seconds": 1}
            ),
            "film_policy": original.film_policy.model_copy(
                update={"required_stages": STAGES, "subtitle_requirement": "chinese"}
            ),
        }
    )
    rt.store.configure_intake_root(rt.operator, rt.meta(), config)
    service = IntakeService(
        rt.store,
        auth,
        root_id=config.root_id,
        meta=rt.meta,
        owner_boot_id=grant.boot_id,
        ffprobe=runner.ffprobe,
        controller=True,
        enabled=True,
        clock=rt.clock.now,
    )
    try:
        assert service.scan_once()
        time.sleep(1.05)  # One real stability interval, as in service-native tests.
        assert service.scan_once()
        result = service.run_ready_once()
        assert result is not None and result.state == "completed", result
        snapshot = result.result.snapshot
        assert snapshot is not None and snapshot.eligibility.decision == "eligible"
        files = [member for member in snapshot.members if member.entry_kind == "file"]
        assert files
        snapshot_dir = rt.work_dir / snapshot.relative_work_path
        for member in files:
            assert os.path.samefile(
                source / member.relative_path, snapshot_dir / member.relative_path
            )
        assert all(member.linked for member in files)
        (manifest,) = snapshot.manifests
        rt.executors["restore"] = ReuseNativeVideo(rt)
        rt.executors["asr"].decision = "reused_zh"
        plan_film(rt, manifest.film_id)
        yield SimpleNamespace(
            rt=rt,
            service=service,
            auth=auth,
            inbox=inbox,
            source=source,
            snapshot=snapshot,
            snapshot_dir=snapshot_dir,
            film=manifest.film_id,
            package=snapshot.admission.package_id,
            ffprobe=runner.ffprobe.executable,
            tmp=tmp_path,
        )
    finally:
        assert service.stop(timeout=5)
        rt.close()


def assert_ready(world):
    view = world.rt.store.get_intake(world.auth, world.package)
    assert view.state == "operational_ready"
    assert view.source_generation == world.snapshot.admission.source_generation
    film = world.rt.store.get_film(world.rt.operator, world.film).film
    assert film.current_input_revision == 1


def test_native_linked_snapshot_survives_rescan_and_late_processing_notification(
    linked_world,
):
    world = linked_world
    assert_ready(world)
    assert world.service.scan_once()
    assert_ready(world)
    batch = DirtyBatch(("Actor/Film/movie.mp4",), False)
    assert world.service.scan_once(trigger="notification", batch=batch)
    assert_ready(world)

    # Return/scrape add more names after the persisted post-intake change token.
    processing = world.tmp / "later-processing.mp4"
    os.link(world.snapshot_dir / "movie.mp4", processing)
    assert world.service.scan_once(trigger="notification", batch=batch)
    assert_ready(world)
    assert os.path.samefile(world.source / "movie.mp4", processing)


def test_input_name_unlink_keeps_native_snapshot_bytes(linked_world):
    world = linked_world
    source = world.source / "movie.mp4"
    snapshot = world.snapshot_dir / "movie.mp4"
    raw = snapshot.read_bytes()
    member = next(m for m in world.snapshot.members if m.relative_path == "movie.mp4")
    expected = m.CleanupMember(
        item_id=member.member_id,
        root_id=member.source_identity.root_id,
        relative_path=member.relative_path,
        size_bytes=member.size_bytes,
        sha256=member.sha256,
        native_identity=member.source_identity,
    )
    assert file_identity.delete_exact(source, expected) == "deleted"
    assert not source.exists()
    assert snapshot.read_bytes() == raw


def test_in_place_linked_edit_is_invalidated_and_hash_check_keeps_both_names(
    linked_world,
):
    world = linked_world
    source = world.source / "movie.mp4"
    before = source.stat()
    raw = source.read_bytes()
    source.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
    member = next(m for m in world.snapshot.members if m.relative_path == "movie.mp4")
    expected = m.CleanupMember(
        item_id=member.member_id,
        root_id=member.copy_identity.root_id,
        relative_path=member.relative_path,
        size_bytes=member.size_bytes,
        sha256=member.sha256,
        native_identity=member.copy_identity,
    )
    snapshot_file = world.snapshot_dir / member.relative_path
    assert file_identity.delete_exact(snapshot_file, expected) == "blocked_changed"
    assert source.exists() and snapshot_file.exists()
    assert world.service.scan_once(
        trigger="notification", batch=DirtyBatch(("Actor/Film/movie.mp4",), False)
    )
    view = world.rt.store.get_intake(world.auth, world.package)
    assert view.source_generation > world.snapshot.admission.source_generation
    assert view.state != "operational_ready"


def test_native_linked_archive_and_readonly_cleanup_preserve_other_names(
    linked_world, monkeypatch
):
    world = linked_world
    rt = world.rt
    step = scr.scrape_step(world.tmp, definition="144P")
    step.ffprobe = [world.ffprobe]
    dispatcher = scr.dispatcher(rt, step)
    for _ in range(6):
        dispatcher.run_once()
    assert ret.status(rt, world.film, "scrape") == "succeeded", dispatcher.status()
    output = rt.output_dir / "ABC-123"
    archived_files = _files(output)
    final_video = next(output.glob("*.mp4"))
    source_video = world.source / "movie.mp4"
    assert os.path.samefile(source_video, final_video)
    assert world.service.scan_once(
        trigger="notification", batch=DirtyBatch(("Actor/Film/movie.mp4",), False)
    )
    assert_ready(world)
    canary = world.tmp / "unrelated-hardlink.mp4"
    os.link(source_video, canary)
    expected_canary = canary.read_bytes()
    source_video.chmod(0o444)  # Attribute belongs to the shared NTFS file.
    nas = world.tmp / "nas" / "Actor"
    nas.mkdir(parents=True)
    deleted = []
    real_delete = file_identity.delete_exact

    def checked_delete(path, member, cancel=None):
        result = real_delete(path, member, cancel)
        if result == "deleted":
            deleted.append(path)
            assert canary.read_bytes() == expected_canary
            # Each cleanup must leave the other existing hardlink intact.
            if path == final_video:
                assert source_video.exists()
        return result

    monkeypatch.setattr(file_identity, "delete_exact", checked_delete)
    fake = FakeLibrary(world.tmp / "library", {"actor": nas})
    with fake:
        finalizer = Finalizer(rt, world.inbox, fake.archive)
        approval_id = finalizer.confirm(world.film, "actor")
        assert world.service.scan_once(
            trigger="notification", batch=DirtyBatch(("Actor/Film/movie.mp4",), False)
        )
        approval = rt.store.film_workflow(rt.operator, world.film).approval
        assert approval.approval_id == approval_id and not approval.revoked
        for _ in range(4):
            finalizer.run_once()
        assert fake.copies == 1
    fake.close()
    for stage in ("archive", "cleanup_input", "cleanup_workcopy"):
        assert ret.status(rt, world.film, stage) == "succeeded", finalizer.waiting
    assert source_video in deleted and final_video in deleted
    assert _files(nas / "ABC-123") == archived_files
    assert not source_video.exists() and not world.snapshot_dir.exists()
    assert not output.exists()
    assert canary.read_bytes() == expected_canary
