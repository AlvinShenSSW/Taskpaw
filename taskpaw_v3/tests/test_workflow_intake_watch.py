"""Real local directory notifications are hints with a bounded, joined owner."""

import struct
import sys
import time
from uuid import uuid4

import pytest

from taskpaw_v3.workflow import intake_watch as watch
from taskpaw_v3.workflow.intake_files import inspect_root


def frame(name, *, action=1, next_offset=0):
    encoded = name.encode("utf-16-le")
    return struct.pack("<III", next_offset, action, len(encoded)) + encoded


def test_notification_decoder_retains_old_and_new_names_and_rejects_bad_offsets():
    first = frame("Actor\\Film\\old.mp4", action=4)
    padding = b"\0" * (-len(first) % 4)
    first = struct.pack("<I", len(first) + len(padding)) + first[4:] + padding
    assert watch.decode_notifications(
        first + frame("Actor\\Film\\new.mp4", action=5)
    ) == ("Actor/Film/old.mp4", "Actor/Film/new.mp4")
    for invalid in (
        frame("../outside"),
        frame("file", action=0),
        frame("file", next_offset=4),
        b"\0" * 3,
    ):
        with pytest.raises(ValueError):
            watch.decode_notifications(invalid)


def test_dirty_hint_limit_marks_full_rescan_without_unbounded_names():
    hints = watch.DirtyHints(limit=2)
    hints.add(("Actor/A", "Actor/A", "Actor/B"))
    hints.add(("Actor/C",))
    result = hints.drain()
    assert result.full_rescan and len(result.paths) <= 2
    assert not hints.drain().paths
    hints.add((), overflow=True)
    assert hints.drain().full_rescan


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native notifications")
def test_actual_change_and_cancelled_idle_read_close_without_directory_mutation(
    tmp_path,
):
    root = tmp_path / "input"
    film = root / "Actor" / "Film"
    film.mkdir(parents=True)
    root_id = str(uuid4())
    owner = watch.DirectoryWatcher(root, expected_root=inspect_root(root, root_id))
    owner.start()
    try:
        assert owner.drain().full_rescan  # Initial scan is always required.
        (film / "new.mp4").write_bytes(b"synthetic changed member")
        deadline = time.monotonic() + 3
        paths = set()
        while time.monotonic() < deadline and "Actor/Film/new.mp4" not in paths:
            paths.update(owner.drain().paths)
            time.sleep(0.01)
        assert "Actor/Film/new.mp4" in paths
        before = time.monotonic()
        assert owner.stop(timeout=2)
        assert time.monotonic() - before < 2
        assert owner.closed and not owner.alive and owner.failure is None
        assert (film / "new.mp4").read_bytes() == b"synthetic changed member"
        assert owner.stop(timeout=0.01)
    finally:
        owner.stop(timeout=3)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native notifications")
def test_watcher_rejects_replaced_identity_and_never_creates_missing_root(tmp_path):
    root = tmp_path / "input"
    root.mkdir()
    expected = inspect_root(root, str(uuid4()))
    root.rename(tmp_path / "old")
    owner = watch.DirectoryWatcher(root, expected_root=expected)
    with pytest.raises(OSError):
        owner.start()
    assert not root.exists()
    root.mkdir()
    owner = watch.DirectoryWatcher(root, expected_root=expected)
    with pytest.raises(OSError, match="identity_changed"):
        owner.start()
    assert owner.closed and not owner.alive


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native notifications")
def test_native_close_failure_keeps_owner_unknown_after_later_handle_close(
    tmp_path, monkeypatch
):
    root = tmp_path / "input"
    root.mkdir()
    owner = watch.DirectoryWatcher(root, expected_root=inspect_root(root, str(uuid4())))
    owner.start()
    original = owner._api.close
    failing_handle = owner._watch

    def fail_one(handle):
        if handle == failing_handle:
            raise OSError("injected native close failure")
        original(handle)

    monkeypatch.setattr(owner._api, "close", fail_one)
    try:
        assert not owner.stop(timeout=2)
        assert owner.failure == "watcher_close_failed" and not owner.closed
        assert owner.drain().full_rescan
    finally:
        monkeypatch.setattr(owner._api, "close", original)
        owner._close_handles()
    assert owner.closed and not owner.alive
    assert not owner.stop(timeout=0.1)  # Later absence cannot erase failed proof.
