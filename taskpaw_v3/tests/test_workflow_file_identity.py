"""C6 exact delete and the shared native identity (F02-P1 §4.2/§4.3)."""

import hashlib
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from taskpaw_v3.workflow import file_identity as fid
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.intake_files import IntakeFileError

WINDOWS = pytest.mark.skipif(sys.platform != "win32", reason="Windows share modes")
PAYLOAD = b"synthetic video bytes\x00" * 64


def _key(identity: m.NativeIdentity) -> tuple[str, str, str, str]:
    return (
        identity.scheme,
        identity.root_id,
        identity.volume_or_export_id,
        identity.object_id,
    )


def _member(path: Path, root_id: str) -> m.CleanupMember:
    data = path.read_bytes()
    return m.CleanupMember(
        item_id=str(uuid4()),
        root_id=root_id,
        relative_path=path.name,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        native_identity=fid.native(path, root_id),
    )


@pytest.fixture
def member(tmp_path):
    path = tmp_path / "movie.mp4"
    path.write_bytes(PAYLOAD)
    return path, _member(path, str(uuid4()))


def test_native_is_stable_and_scheme_matches_platform(member):
    path, value = member
    again = fid.native(path, value.root_id)
    assert _key(again) == _key(value.native_identity)
    assert again.evidence_id != value.native_identity.evidence_id
    if sys.platform == "win32":
        assert again.scheme == "windows_file_id"
        assert len(again.volume_or_export_id) == 16
        assert len(again.object_id) == 32
    else:
        info = os.lstat(path)
        assert again.scheme == "posix_inode"
        assert (again.volume_or_export_id, again.object_id) == (
            str(info.st_dev),
            str(info.st_ino),
        )


@WINDOWS
def test_native_equals_what_intake_records(tmp_path):
    from taskpaw_v3.workflow import intake_files as files
    from taskpaw_v3.workflow.intake_models import IntakePolicy

    root = tmp_path / "input"
    (root / "Film").mkdir(parents=True)
    path = root / "Film" / "movie.mp4"
    path.write_bytes(PAYLOAD)
    root_id = str(uuid4())
    with files.HeldPackage(
        root,
        "Film",
        root_id=root_id,
        expected_root=files.inspect_root(root, root_id),
        policy=IntakePolicy(policy_revision=1, min_free_bytes=0),
    ) as source:
        recorded = source.entries["movie.mp4"].identity
    assert _key(fid.native(path, root_id)) == _key(recorded)


def test_native_rejects_symlinks_and_directories(tmp_path):
    target = tmp_path / "real.bin"
    target.write_bytes(b"x")
    link = tmp_path / "link.bin"
    try:
        link.symlink_to(target)
    except OSError:
        link = None
    if link is not None:
        with pytest.raises(IntakeFileError):
            fid.native(link, str(uuid4()))
    with pytest.raises(IntakeFileError):
        fid.native(tmp_path, str(uuid4()))


def test_delete_exact_removes_the_verified_file(member):
    path, value = member
    assert fid.check(path, value) == "ok"
    assert fid.delete_exact(path, value) == "deleted"
    assert not path.exists()
    assert fid.delete_exact(path, value) == "already_absent"
    assert fid.check(path, value) == "already_absent"


def test_missing_parent_is_already_absent(member, tmp_path):
    _, value = member
    path = tmp_path / "gone" / "movie.mp4"
    assert fid.check(path, value) == "already_absent"
    assert fid.delete_exact(path, value) == "already_absent"


@WINDOWS
def test_held_for_writing_is_blocked_and_file_kept(member):
    path, value = member
    with open(path, "r+b"):
        assert fid.check(path, value) == "blocked_writing"
        assert fid.delete_exact(path, value) == "blocked_writing"
    assert path.read_bytes() == PAYLOAD
    assert fid.delete_exact(path, value) == "deleted"


@WINDOWS
def test_delete_handle_denies_concurrent_writers(member):
    """While C6 holds its handle no writer can open the member."""
    path, value = member
    api = fid._api()
    handle = api.open_exact(path, fid._DELETE | fid._GENERIC_READ)
    try:
        with pytest.raises(PermissionError):
            open(path, "r+b")
    finally:
        api.close(handle)
    assert fid.delete_exact(path, value) == "deleted"


def test_replaced_by_same_name_and_content_is_blocked_identity(member, tmp_path):
    path, value = member
    replacement = tmp_path / "replacement.tmp"
    replacement.write_bytes(PAYLOAD)
    os.replace(replacement, path)
    assert fid.check(path, value) == "blocked_identity"
    assert fid.delete_exact(path, value) == "blocked_identity"
    assert path.read_bytes() == PAYLOAD


def test_directory_in_place_is_blocked_identity(member):
    path, value = member
    path.unlink()
    path.mkdir()
    assert fid.check(path, value) == "blocked_identity"
    assert fid.delete_exact(path, value) == "blocked_identity"
    assert path.is_dir()


def test_same_size_content_change_is_blocked_changed(member):
    path, value = member
    with open(path, "r+b") as handle:
        handle.write(b"Z")
    # check never reads content: identity and size still match (D12).
    assert fid.check(path, value) == "ok"
    assert fid.delete_exact(path, value) == "blocked_changed"
    assert path.read_bytes() == b"Z" + PAYLOAD[1:]


def test_size_change_is_blocked_changed(member):
    path, value = member
    with open(path, "ab") as handle:
        handle.write(b"more")
    assert fid.check(path, value) == "blocked_changed"
    assert fid.delete_exact(path, value) == "blocked_changed"
    assert path.read_bytes() == PAYLOAD + b"more"


def test_empty_file_round_trip(tmp_path):
    path = tmp_path / "empty.srt"
    path.write_bytes(b"")
    value = _member(path, str(uuid4()))
    assert fid.check(path, value) == "ok"
    assert fid.delete_exact(path, value) == "deleted"
    assert not path.exists()
