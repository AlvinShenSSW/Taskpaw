"""Owned local NTFS fixtures; no operator media or process/service startup."""

# ruff: noqa: F811

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from taskpaw_v3.tests.test_workflow_intake_discovery import config_for
from taskpaw_v3.tests.test_workflow_intake_files import package  # noqa: F401
from taskpaw_v3.workflow import intake_files as files
from taskpaw_v3.workflow import intake_walk as walk

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="native NTFS handles")


def run(config, **kwargs):
    return walk.walk_root(config, deadline=time.monotonic() + 20, **kwargs)


def paths(result):
    return tuple(value.relative_path for value in result.candidates)


def test_actor_films_empty_and_multi_video_are_one_package_each(package, tmp_path):
    root, directory, *_ = package
    config = config_for(package, tmp_path)
    (directory / "second.mp4").write_bytes(b"synthetic")
    (root / "Actor" / "Empty Film").mkdir()
    result = run(config)
    assert result.coverage == "complete" and result.reasons == ()
    assert paths(result) == ("Actor/Empty Film", "Actor/Film")
    assert result.root_config_revision == config.expected_version + 1
    assert files._key(result.root_native_identity) == files._key(config.native_identity)
    assert result.observed_from <= result.observed_until
    assert all(
        item.native_identity.root_id == config.root_id for item in result.candidates
    )
    assert all(
        not item.enumerated
        for item in result.directories
        if item.relative_path in paths(result)
    )


@pytest.mark.parametrize("layout", ["direct", "multipart"])
def test_first_level_film_context_is_not_split(package, tmp_path, layout):
    root, *_ = package
    film = root / "Standalone Film"
    film.mkdir()
    if layout == "direct":
        (film / "One.mp4").write_bytes(b"synthetic one")
        (film / "Two.mkv").write_bytes(b"synthetic two")
    else:
        for name in ("CD1", "CD2"):
            (film / name).mkdir()
            (film / name / "movie.mp4").write_bytes(b"synthetic")
    result = run(config_for(package, tmp_path))
    assert result.coverage == "complete"
    assert paths(result) == ("Actor/Film", "Standalone Film")


def test_actor_attachment_remains_context_not_shared_film_assignment(package, tmp_path):
    root, *_ = package
    (root / "Actor" / "folder.jpg").write_bytes(b"synthetic image")
    result = run(config_for(package, tmp_path))
    assert result.coverage == "complete"
    assert paths(result) == ("Actor/Film",)
    actor = next(item for item in result.directories if item.relative_path == "Actor")
    assert actor.file_names == ("folder.jpg",)


def test_mixed_direct_video_and_child_film_is_partial_without_overlap(
    package, tmp_path
):
    root, *_ = package
    (root / "Actor" / "loose.mp4").write_bytes(b"synthetic")
    result = run(config_for(package, tmp_path))
    assert result.coverage == "partial" and "ambiguous_layout" in result.reasons
    assert paths(result) == ()


def test_root_loose_file_is_visible_and_never_complete(package, tmp_path):
    root, *_ = package
    (root / "loose.mp4").write_bytes(b"synthetic")
    result = run(config_for(package, tmp_path))
    assert result.coverage == "partial" and "root_package_required" in result.reasons
    assert paths(result) == ("Actor/Film",)
    assert result.directories[0].relative_path is None
    assert result.directories[0].file_names == ("loose.mp4",)


def test_walker_never_opens_reads_or_hashes_files(package, tmp_path, monkeypatch):
    config = config_for(package, tmp_path)
    opened = []
    original = files.WindowsFiles.open

    def open_directory(self, path, *, directory):
        assert directory
        opened.append(path)
        return original(self, path, directory=directory)

    def forbidden(*args, **kwargs):
        pytest.fail("walker attempted a media content/stream read")

    monkeypatch.setattr(files.WindowsFiles, "open", open_directory)
    monkeypatch.setattr(files.WindowsFiles, "read", forbidden)
    monkeypatch.setattr(files.WindowsFiles, "streams", forbidden)
    assert run(config).coverage == "complete"
    assert opened


def test_root_ancestors_and_candidates_are_pinned_until_final_recheck(
    package, tmp_path, monkeypatch
):
    root, directory, *_ = package
    config = config_for(package, tmp_path)
    original = os.scandir
    root_reads = 0
    checked = False

    def scan(path):
        nonlocal root_reads, checked
        if Path(path) == root:
            root_reads += 1
            if root_reads == 2:
                for value in (root, root.parent, directory):
                    with pytest.raises(OSError):
                        value.rename(value.with_name(value.name + "-renamed"))
                checked = True
        return original(path)

    monkeypatch.setattr(walk.os, "scandir", scan)
    assert run(config).coverage == "complete"
    assert checked
    directory.rename(directory.with_name("Renamed"))


def test_same_path_replaced_root_is_not_accepted(package, tmp_path):
    root, *_ = package
    config = config_for(package, tmp_path)
    root.rename(root.with_name("old-input"))
    root.mkdir()
    result = run(config)
    assert result.coverage == "partial" and "identity_changed" in result.reasons
    assert not result.candidates


@pytest.mark.parametrize("where", ["actor", "film"])
def test_junction_never_followed_and_other_packages_retained(
    package, tmp_path, where, monkeypatch
):
    import _winapi

    root, *_ = package
    config = config_for(package, tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.mp4").write_bytes(b"owned canary")
    link = root / "Linked Actor" if where == "actor" else root / "Actor" / "Linked Film"
    _winapi.CreateJunction(str(outside), str(link))
    original = os.scandir

    def no_follow(path):
        assert Path(path) not in (link, outside)
        return original(path)

    monkeypatch.setattr(walk.os, "scandir", no_follow)
    result = run(config)
    assert result.coverage == "partial" and "reparse_point" in result.reasons
    assert paths(result) == ("Actor/Film",)
    assert (outside / "secret.mp4").read_bytes() == b"owned canary"


def test_permission_failure_is_partial_and_does_not_hide_other_actor(
    package, tmp_path, monkeypatch
):
    root, *_ = package
    config = config_for(package, tmp_path)
    bad = root / "Blocked"
    bad.mkdir()
    original = os.scandir

    def scan(path):
        if Path(path) == bad:
            raise PermissionError(13, "owned injected permission fault")
        return original(path)

    monkeypatch.setattr(walk.os, "scandir", scan)
    result = run(config)
    assert result.coverage == "partial" and "permission_denied" in result.reasons
    assert paths(result) == ("Actor/Film",)


def test_cancel_before_io_and_after_native_return_never_complete(
    package, tmp_path, monkeypatch
):
    config = config_for(package, tmp_path)
    result = run(config, cancelled=lambda: True)
    assert result.coverage == "partial" and result.reasons == ("cancelled",)
    assert result.candidates == ()
    original = os.scandir
    stopped = False

    def scan(path):
        nonlocal stopped
        value = original(path)
        stopped = True
        return value

    monkeypatch.setattr(walk.os, "scandir", scan)
    result = run(config, cancelled=lambda: stopped)
    assert result.coverage == "partial" and "cancelled" in result.reasons


def test_native_return_after_deadline_never_reports_complete(
    package, tmp_path, monkeypatch
):
    config = config_for(package, tmp_path)
    original = os.scandir
    now = 0.0

    def scan(path):
        nonlocal now
        value = original(path)
        now = 2.0
        return value

    monkeypatch.setattr(walk.os, "scandir", scan)
    result = walk.walk_root(config, deadline=1.0, clock=lambda: now)
    assert result.coverage == "partial" and "timeout" in result.reasons


def test_late_member_in_final_recheck_prevents_missing_proof(
    package, tmp_path, monkeypatch
):
    root, *_ = package
    config = config_for(package, tmp_path)
    original = os.scandir
    reads = 0

    def scan(path):
        nonlocal reads
        if Path(path) == root:
            reads += 1
            if reads == 2:
                (root / "Late Actor").mkdir()
        return original(path)

    monkeypatch.setattr(walk.os, "scandir", scan)
    result = run(config)
    assert result.coverage == "partial" and "source_changed" in result.reasons


@pytest.mark.parametrize(
    "limits", [walk.WalkLimits(max_directories=2), walk.WalkLimits(max_entries=1)]
)
def test_root_limits_are_explicit_partial_not_truncated_success(
    package, tmp_path, limits
):
    result = run(config_for(package, tmp_path), limits=limits)
    assert result.coverage == "partial" and "limit_exceeded" in result.reasons


@pytest.mark.parametrize(
    "kwargs", [{"max_directories": True}, {"max_entries": 0}, {"max_entries": 65537}]
)
def test_limits_are_strict_and_bounded(kwargs):
    with pytest.raises(ValueError):
        walk.WalkLimits(**kwargs)


@pytest.mark.parametrize("deadline", [True, float("nan"), float("inf")])
def test_invalid_deadline_rejected_before_native_io(package, tmp_path, deadline):
    with pytest.raises(ValueError, match="deadline"):
        walk.walk_root(config_for(package, tmp_path), deadline=deadline)


def test_failed_close_retains_original_owner_and_can_be_retried(
    package, tmp_path, monkeypatch
):
    config = config_for(package, tmp_path)
    original = files.WindowsFiles.close
    failed = []

    def close(self, handle):
        if not failed:
            failed.append(handle)
            raise files.IntakeFileError("close_failed")
        return original(self, handle)

    monkeypatch.setattr(files.WindowsFiles, "close", close)
    with pytest.raises(walk.WalkUnresolved) as raised:
        run(config)
    error = raised.value
    assert not error.pending_owner.closed
    assert error.result.coverage == "partial" and "close_failed" in error.result.reasons
    assert failed[0] in error.pending_owner._holds.handles
    error.pending_owner.close()
    assert error.pending_owner.closed


def test_failed_directory_identity_acquisition_still_spends_handle_budget(
    package, tmp_path, monkeypatch
):
    root, *_ = package
    config = config_for(package, tmp_path)
    (root / "AAA Bad").mkdir()
    original = files.WindowsFiles.entry

    def entry(self, handle, path, relative, root_id):
        if relative == "AAA Bad":
            raise files.IntakeFileError("native_io_failed")
        return original(self, handle, path, relative, root_id)

    monkeypatch.setattr(files.WindowsFiles, "entry", entry)
    result = run(config, limits=walk.WalkLimits(max_directories=2))
    assert "limit_exceeded" in result.reasons
    assert not any(value.relative_path == "Actor" for value in result.directories)


class IteratorFault:
    def __init__(self, iterator, *, item=None, close=None):
        self.iterator, self.item, self.close_hook = iterator, item, close

    def __iter__(self):
        return self

    def __next__(self):
        value = next(self.iterator)
        return self.item(value) if self.item else value

    def close(self):
        if self.close_hook:
            self.close_hook()
        self.iterator.close()


def test_failed_entry_stat_still_spends_total_entry_budget(
    package, tmp_path, monkeypatch
):
    root, *_ = package
    config = config_for(package, tmp_path)
    bad = root / "AAA Bad"
    bad.mkdir()
    (bad / "one.mp4").write_bytes(b"synthetic")
    original = os.scandir

    class FailedStat:
        name = "one.mp4"

        def stat(self, **kwargs):
            raise PermissionError(13, "owned stat fault")

    def scan(path):
        value = original(path)
        return (
            IteratorFault(value, item=lambda _: FailedStat())
            if Path(path) == bad
            else value
        )

    monkeypatch.setattr(walk.os, "scandir", scan)
    result = run(config, limits=walk.WalkLimits(max_entries=3))
    assert "permission_denied" in result.reasons
    assert "limit_exceeded" in result.reasons
    assert result.candidates == ()


def test_unclosed_enumeration_iterator_keeps_exact_owner(
    package, tmp_path, monkeypatch
):
    root, *_ = package
    config = config_for(package, tmp_path)
    original = os.scandir
    fail = True

    def close():
        if fail:
            raise OSError("owned close fault")

    def scan(path):
        value = original(path)
        return IteratorFault(value, close=close) if Path(path) == root else value

    monkeypatch.setattr(walk.os, "scandir", scan)
    with pytest.raises(walk.WalkUnresolved) as raised:
        run(config)
    owner = raised.value.pending_owner
    assert not owner.closed and owner._iterators
    fail = False
    owner.close()
    assert owner.closed


def test_unsupported_filesystem_is_partial(package, tmp_path, monkeypatch):
    config = config_for(package, tmp_path)

    def reject(*args):
        raise files.IntakeFileError("unsupported_filesystem")

    monkeypatch.setattr(files.WindowsFiles, "local_filesystem", reject)
    result = run(config)
    assert result.coverage == "partial" and result.reasons == (
        "unsupported_filesystem",
    )


def test_fixture_result_objects_are_immutable(package, tmp_path):
    result = run(config_for(package, tmp_path))
    with pytest.raises(AttributeError):
        result.coverage = "partial"
