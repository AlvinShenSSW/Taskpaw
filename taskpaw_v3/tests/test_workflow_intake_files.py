"""Owned native fixtures exercise actual Windows object/share semantics."""

import hashlib
import os
import sys
from uuid import uuid4

import pytest

from taskpaw_v3.workflow import intake_files as files
from taskpaw_v3.workflow.intake_models import IntakePolicy

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native intake")


@pytest.fixture
def package(tmp_path):
    root = tmp_path / "input"
    directory = root / "Actor" / "Film"
    directory.mkdir(parents=True)
    (directory / "movie.mp4").write_bytes(b"synthetic bytes\x00" * 100)
    (directory / "movie.zh.srt").write_bytes(b"synthetic subtitle")
    root_id = str(uuid4())
    identity = files.inspect_root(root, root_id)
    return root, directory, root_id, identity


def held(package, **kwargs):
    root, _, root_id, identity = package
    return files.HeldPackage(
        root,
        "Actor/Film",
        root_id=root_id,
        expected_root=identity,
        policy=IntakePolicy(policy_revision=1, min_free_bytes=0),
        **kwargs,
    )


def test_empty_directory_object_and_ancestor_are_pinned_before_enumeration(tmp_path):
    root = tmp_path / "empty-root"
    child = root / "empty-package"
    child.mkdir(parents=True)
    handles = files._Holds(files.WindowsFiles(), str(uuid4()))
    try:
        handles.chain(child)
        with pytest.raises(PermissionError):
            child.rename(root / "replacement-name")
        with pytest.raises(PermissionError):
            root.rename(tmp_path / "root-replacement")
    finally:
        handles.close()
    child.rename(root / "replacement-name")


def test_holds_source_objects_and_all_path_ancestors_until_checked_close(package):
    _, directory, _, _ = package
    payload = (directory / "movie.mp4").read_bytes()
    with held(package) as source:
        assert set(source.entries) == {"movie.mp4", "movie.zh.srt"}
        entry = source.entries["movie.mp4"]
        assert source.sha256("movie.mp4") == hashlib.sha256(payload).hexdigest()
        assert entry.size_bytes == len(payload)
        assert entry.identity.scheme == "windows_file_id"
        with pytest.raises(PermissionError):
            (directory / "movie.mp4").write_bytes(b"must not modify")
        with pytest.raises(PermissionError):
            (directory / "movie.mp4").unlink()
        with pytest.raises(PermissionError):
            directory.rename(directory.with_name("Moved"))
        with pytest.raises(PermissionError):
            directory.parent.rename(directory.parent.with_name("MovedActor"))
        source.recheck()
    assert source.closed
    assert (directory / "movie.mp4").read_bytes() == payload
    directory.rename(directory.with_name("Moved"))


def test_existing_write_handle_blocks_snapshot_even_if_writer_is_paused(package):
    _, directory, _, _ = package
    with (directory / "movie.mp4").open("r+b"):
        with pytest.raises(files.IntakeFileError, match="writer_busy"):
            with held(package):
                pytest.fail("must not admit")
    with held(package) as source:
        source.recheck()


@pytest.mark.parametrize("where", ["Actor", "Actor/Film", "Actor/Film/CD1"])
def test_reparse_ancestor_or_member_never_reads_external_directory(tmp_path, where):
    import _winapi

    root, external = tmp_path / "input", tmp_path / "outside"
    root.mkdir()
    external.mkdir()
    (external / "secret.mp4").write_bytes(b"owned canary")
    link = root.joinpath(*where.split("/"))
    link.parent.mkdir(parents=True, exist_ok=True)
    _winapi.CreateJunction(str(external), str(link))
    root_id = str(uuid4())
    with pytest.raises(files.IntakeFileError, match="reparse_point"):
        with files.HeldPackage(
            root,
            "Actor/Film",
            root_id=root_id,
            expected_root=files.inspect_root(root, root_id),
            policy=IntakePolicy(policy_revision=1),
        ):
            pytest.fail("must not follow junction")
    assert (external / "secret.mp4").read_bytes() == b"owned canary"


def test_hardlinks_keep_same_object_identity_and_each_source_reference(package):
    _, directory, _, _ = package
    os.link(directory / "movie.mp4", directory / "second.mp4")
    with held(package) as source:
        first, second = source.entries["movie.mp4"], source.entries["second.mp4"]
        assert first.identity.object_id == second.identity.object_id
        assert first.link_count == second.link_count == 2
        assert source.sha256("movie.mp4") == source.sha256("second.mp4")


def test_late_member_including_hidden_temporary_invalidates_full_membership(package):
    _, directory, _, _ = package
    with held(package) as source:
        (directory / ".late.partial").write_bytes(b"new source bytes")
        with pytest.raises(files.IntakeFileError, match="source_changed"):
            source.recheck()


def test_temporary_member_is_retained_in_the_complete_observation(package):
    _, directory, _, _ = package
    (directory / ".copy.part").write_bytes(b"not finished")
    with held(package) as source:
        assert ".copy.part" in source.entries
        assert source.entries[".copy.part"].temporary


def test_extra_stream_is_rejected_without_modifying_source(package):
    _, directory, _, _ = package
    stream = str(directory / "movie.mp4") + ":side-data"
    with open(stream, "wb") as writer:
        writer.write(b"owned alternate stream")
    with pytest.raises(files.IntakeFileError, match="alternate_stream") as raised:
        with held(package):
            pytest.fail("must retain and report source")
    assert raised.value.path == "movie.mp4"  # the operator is told which file
    with open(stream, "rb") as reader:
        assert reader.read() == b"owned alternate stream"


def test_windows_metadata_streams_are_not_film_data(package):
    """Explorer's 0-byte Thumbs.db `:encryptable` and the Mark of the Web are accepted."""
    _, directory, _, _ = package
    (directory / "Thumbs.db").write_bytes(b"thumbnail cache")
    open(str(directory / "Thumbs.db") + ":encryptable", "wb").close()
    with open(str(directory / "movie.mp4") + ":Zone.Identifier", "wb") as writer:
        writer.write(b"[ZoneTransfer] ZoneId=3")
    with held(package) as source:
        assert {"Thumbs.db", "movie.mp4"} <= set(source.entries)


def test_a_non_empty_encryptable_stream_is_still_refused(package):
    """Codex 4.0.1 P2: only Explorer's 0-byte marker is metadata."""
    _, directory, _, _ = package
    with open(str(directory / "movie.mp4") + ":encryptable", "wb") as writer:
        writer.write(b"data that was never archived")
    with pytest.raises(files.IntakeFileError, match="alternate_stream"):
        with held(package):
            pytest.fail("must refuse")


@pytest.mark.parametrize(
    "relative",
    ["../outside", "Actor/../Film", "Actor/Film:ads", "Actor/Film.", "Actor/Film "],
)
def test_path_alias_or_escape_is_rejected_before_open(package, relative):
    root, _, root_id, identity = package
    with pytest.raises(files.IntakeFileError, match="invalid_path"):
        with files.HeldPackage(
            root,
            relative,
            root_id=root_id,
            expected_root=identity,
            policy=IntakePolicy(policy_revision=1),
        ):
            pytest.fail("invalid path")


def test_replacement_root_does_not_inherit_registered_identity(package):
    root, _, root_id, original = package
    root.rename(root.with_name("previous"))
    (root / "Actor" / "Film").mkdir(parents=True)
    with pytest.raises(files.IntakeFileError, match="identity_changed"):
        with files.HeldPackage(
            root,
            "Actor/Film",
            root_id=root_id,
            expected_root=original,
            policy=IntakePolicy(policy_revision=1),
        ):
            pytest.fail("must not reuse old root")


def test_member_and_depth_caps_reject_the_whole_package(package):
    root, directory, root_id, identity = package
    with pytest.raises(files.IntakeFileError, match="limit_exceeded"):
        with files.HeldPackage(
            root,
            "Actor/Film",
            root_id=root_id,
            expected_root=identity,
            policy=IntakePolicy(policy_revision=1, max_members=1),
        ):
            pytest.fail("must not truncate member list")
    (directory / "one" / "two").mkdir(parents=True)
    with pytest.raises(files.IntakeFileError, match="limit_exceeded"):
        with files.HeldPackage(
            root,
            "Actor/Film",
            root_id=root_id,
            expected_root=identity,
            policy=IntakePolicy(policy_revision=1, max_depth=1),
        ):
            pytest.fail("must not ignore deep members")


def test_fixed_probe_artifact_is_hashed_and_pinned_before_any_execution(tmp_path):
    tool = tmp_path / "fixed-tool.exe"
    payload = b"owned synthetic executable bytes"
    tool.write_bytes(payload)
    with files.PinnedFile(
        tool, sha256=hashlib.sha256(payload).hexdigest(), size_bytes=len(payload)
    ):
        with pytest.raises(PermissionError):
            tool.write_bytes(b"replacement")
        with pytest.raises(PermissionError):
            tool.rename(tool.with_name("replacement.exe"))
    with pytest.raises(files.IntakeFileError, match="artifact_mismatch"):
        with files.PinnedFile(tool, sha256="0" * 64, size_bytes=len(payload)):
            pytest.fail("must not execute unverified artifact")
    tool.write_bytes(b"released after closure")
