"""Actual private file copies are independent and checked before publication."""

import hashlib
import sys
from uuid import uuid4

import pytest

from taskpaw_v3.tests import test_workflow_intake_files as fixtures
from taskpaw_v3.tests.test_workflow_intake_files import held
from taskpaw_v3.workflow import intake_copy as copies
from taskpaw_v3.workflow import intake_files as files

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native intake")
package = fixtures.package


def copy(source, tmp_path, **kwargs):
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    root_id = str(uuid4())
    return copies.PrivateCopy(
        source,
        work,
        root_id=root_id,
        expected_root=files.inspect_root(work, root_id),
        operation_id=str(uuid4()),
        **kwargs,
    )


def test_copy_rechecks_bytes_and_publishes_only_complete_independent_files(
    package, tmp_path
):
    _, directory, _, _ = package
    (directory / "CD1").mkdir()
    (directory / "CD1" / "extra.nfo").write_bytes(b"synthetic metadata")
    progress = []
    with held(package) as source:
        with copy(source, tmp_path) as target:
            target.copy_members(on_progress=lambda *v: progress.append(v))
            assert not target.snapshot_path.exists()
            assert set(target.entries) == set(source.entries)
            for relative, item in target.entries.items():
                assert item.source.identity.object_id != item.copy.identity.object_id
                if item.sha256:
                    assert (
                        item.sha256
                        == hashlib.sha256(
                            (directory / relative).read_bytes()
                        ).hexdigest()
                    )
            with pytest.raises(PermissionError):
                target.private_path("movie.mp4").write_bytes(
                    b"cannot corrupt while probing"
                )
            published = target.publish(b'{"synthetic_completion":true}')
            assert published == target.snapshot_path and published.is_dir()
            assert not target.partial_path.exists()
            assert (
                target.operation_path / "completion.json"
            ).read_bytes() == b'{"synthetic_completion":true}'
            assert target.private_path("movie.mp4").parent == published
            with pytest.raises(PermissionError):
                (directory / "movie.mp4").write_bytes(
                    b"source remains held through publish"
                )
        assert target.closed
    assert {v[0] for v in progress} == {"copy", "verify"}
    assert all(v[2] > 0 for v in progress)


def test_late_source_member_prevents_publish_and_preserves_private_partial(
    package, tmp_path
):
    _, directory, _, _ = package
    with held(package) as source:
        with copy(source, tmp_path) as target:
            target.copy_members()
            (directory / "late.srt").write_bytes(b"late source")
            with pytest.raises(files.IntakeFileError, match="source_changed"):
                target.publish(b"{}")
            assert target.partial_path.is_dir() and not target.snapshot_path.exists()
            assert not (target.operation_path / "completion.json").exists()


def test_existing_operation_never_gets_overwritten_or_reused(package, tmp_path):
    with held(package) as source:
        target = copy(source, tmp_path)
        target.operation_path.mkdir(parents=True)
        canary = target.operation_path / "keep"
        canary.write_bytes(b"unresolved previous owner")
        with pytest.raises(files.IntakeFileError, match="operation_path_exists"):
            with target:
                pytest.fail("must not reuse")
        assert canary.read_bytes() == b"unresolved previous owner"


def test_junction_in_private_intake_parent_rejects_before_external_write(
    package, tmp_path
):
    import _winapi

    with held(package) as source:
        target = copy(source, tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        _winapi.CreateJunction(str(outside), str(target.work_root / "intake"))
        with pytest.raises(files.IntakeFileError, match="reparse_point"):
            with target:
                pytest.fail("must not follow private alias")
        assert not tuple(outside.iterdir())


def test_copy_interruption_preserves_source_and_does_not_create_completion(
    package, tmp_path
):
    _, directory, _, _ = package
    original = {path.name: path.read_bytes() for path in directory.iterdir()}
    with held(package) as source:
        with copy(source, tmp_path) as target:

            def interrupt():
                raise files.IntakeFileError("cancelled")

            with pytest.raises(files.IntakeFileError, match="cancelled"):
                target.copy_members(check_cancelled=interrupt)
            with pytest.raises(files.IntakeFileError, match="copy_incomplete"):
                target.publish(b"{}")
    assert original == {path.name: path.read_bytes() for path in directory.iterdir()}
    assert not (target.operation_path / "completion.json").exists()


def test_verification_mismatch_is_not_a_successful_copy(package, tmp_path, monkeypatch):
    with held(package) as source:
        with copy(source, tmp_path) as target:
            native_read = target.api.read
            monkeypatch.setattr(
                target.api,
                "read",
                lambda handle: b"changed" if native_read(handle) else b"",
            )
            with pytest.raises(files.IntakeFileError, match="copy_verification_failed"):
                target.copy_members()
            assert not target.snapshot_path.exists()


def test_capacity_check_precedes_file_creation(package, tmp_path, monkeypatch):
    from collections import namedtuple

    usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(copies.shutil, "disk_usage", lambda _: usage(1, 1, 0))
    with held(package) as source:
        with copy(source, tmp_path) as target:
            with pytest.raises(files.IntakeFileError, match="waiting_capacity"):
                target.copy_members()
            assert not tuple(target.partial_path.iterdir())


def test_only_known_copied_relative_paths_can_be_probed(package, tmp_path):
    with held(package) as source:
        with copy(source, tmp_path) as target:
            with pytest.raises(files.IntakeFileError, match="copy_incomplete"):
                target.private_path("movie.mp4")
            target.copy_members()
            for relative in ("../outside", "movie.mp4:ads", "unlisted.mp4"):
                with pytest.raises(files.IntakeFileError, match="unknown_member"):
                    target.private_path(relative)


def test_atomic_rename_failure_leaves_unpublished_complete_partial(
    package, tmp_path, monkeypatch
):
    with held(package) as source:
        with copy(source, tmp_path) as target:
            target.copy_members()

            def fail_rename(*_):
                raise OSError("injected rename failure")

            monkeypatch.setattr(copies.os, "rename", fail_rename)
            with pytest.raises(OSError, match="injected rename failure"):
                target.publish(b"{}")
            assert target.partial_path.is_dir() and not target.snapshot_path.exists()
            assert not (target.operation_path / "completion.json").exists()


def test_flush_failure_is_not_verified_or_published(package, tmp_path, monkeypatch):
    with held(package) as source:
        with copy(source, tmp_path) as target:

            def fail_flush(*_):
                raise OSError("injected flush failure")

            monkeypatch.setattr(target.api, "flush", fail_flush)
            with pytest.raises(OSError, match="injected flush failure"):
                target.copy_members()
            with pytest.raises(files.IntakeFileError, match="copy_incomplete"):
                target.publish(b"{}")
            assert not target.snapshot_path.exists()
