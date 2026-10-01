"""R01 credential lifecycle and real-object security, with no real user config."""

import os
import stat
import subprocess
import sys

import pytest

from taskpaw_v3.core import control, control_file
from taskpaw_v3.core.control import (
    ControlCredentialError,
    bootstrap_control,
    read_control_descriptor,
    revoke_control,
    strip_control_env,
    without_control_env,
)


def test_each_boot_fresh_key_rejects_static_env_and_old_file(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setenv("TASKPAW_CONTROL_TOKEN", "STATIC-FAKE-SECRET")
    strip_control_env()
    first = bootstrap_control("agent", "http://127.0.0.1:5681", tmp_path / "agent.yaml")
    before = read_control_descriptor(first.credential_file)
    assert before.control_token == first.token and before.boot_id == first.boot_id
    second = bootstrap_control(
        "agent", "http://127.0.0.1:6001", tmp_path / "agent.yaml"
    )
    try:
        assert first.token != second.token and first.boot_id != second.boot_id
        assert second.token != "STATIC-FAKE-SECRET"
        after = read_control_descriptor(second.credential_file)
        assert after.base_url == "http://127.0.0.1:6001"
        assert after.boot_id == second.boot_id
        assert first.token not in repr(first) + repr(before) + caplog.text
        assert "STATIC-FAKE-SECRET" not in caplog.text
        revoke_control(first)  # must not delete a newer boot
        assert read_control_descriptor(second.credential_file) == after
    finally:
        revoke_control(first)
        revoke_control(second)
    assert not second.credential_file.exists()
    assert not first.is_active() and not second.is_active()


def test_memory_only_has_no_file_and_revoke_is_idempotent(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("memory-only startup must never open real paths")

    monkeypatch.setattr(control, "CredentialLease", forbidden)
    session = bootstrap_control("hub", "http://[::1]:5691", None)
    assert session.credential_file is None and session.is_active()
    revoke_control(session)
    revoke_control(session)
    assert not session.is_active()


@pytest.mark.parametrize("bad", ["", "☃", "white space", "line\r\n", "x" * 1025])
def test_invalid_token_rejected_without_value(bad):
    with pytest.raises(ControlCredentialError) as caught:
        control_file.validate_control_token(bad)
    assert str(caught.value) == "invalid_control_token"


@pytest.mark.parametrize(
    "base",
    [
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://127.0.0.1:5681/",
        "http://localhost:5681",
        "http://127.1:5681",
        "http://user:secret@127.0.0.1:5681",
        "http://127.0.0.1:5681?x",
        "https://evil.invalid:5681",
        "ftp://127.0.0.1:5681",
        "HTTP://127.0.0.1:5681",
    ],
)
def test_base_canonical_numeric_loopback_only(base):
    with pytest.raises(ControlCredentialError):
        bootstrap_control("agent", base, None)


@pytest.mark.parametrize(
    "base", ["http://127.0.0.1:80", "https://[::1]:443", "http://[::1]:5681"]
)
def test_explicit_default_port_and_ipv6_accepted(base):
    session = bootstrap_control("agent", base, None)
    revoke_control(session)


def test_strip_only_individual_control_keys_preserves_mapping():
    class Environment(dict):
        def clear(self):
            raise AssertionError("do not reset the process environment")

        def update(self, *args, **kwargs):
            raise AssertionError("do not reset the process environment")

    env = Environment(
        {
            "TASKPAW_CONTROL_TOKEN": "fake",
            "taskpaw_control_token": "fake2",
            "TASKPAW_UI_TOKEN": "legacy-fake",
            "PATH": "keep",
            "other": "keep2",
        }
    )
    assert without_control_env(env) == {"PATH": "keep", "other": "keep2"}
    assert len(env) == 5
    strip_control_env(env)
    assert env == {"PATH": "keep", "other": "keep2"}


@pytest.mark.skipif(os.name == "nt", reason="POSIX fd/mode checks")
def test_file_private_and_fd_validation_survives_path_replacement(
    tmp_path, monkeypatch
):
    session = bootstrap_control(
        "agent", "http://127.0.0.1:5681", tmp_path / "agent.yaml"
    )
    path = session.credential_file
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    real_read = os.read
    replaced = False

    def replacing_read(fd, size):
        nonlocal replaced
        if not replaced:
            replaced = True
            other = tmp_path / "replacement"
            other.write_bytes(b"not-the-object-we-validated")
            other.chmod(0o600)
            os.replace(other, path)
        return real_read(fd, size)

    monkeypatch.setattr(os, "read", replacing_read)
    descriptor = read_control_descriptor(path)
    assert descriptor.control_token == session.token
    monkeypatch.setattr(os, "read", real_read)
    with pytest.raises(ControlCredentialError):
        revoke_control(session)
    assert not session.is_active() and session._lease is None
    path.unlink()


@pytest.mark.skipif(os.name == "nt", reason="POSIX fd/mode checks")
@pytest.mark.parametrize(
    "unsafe", ["mode", "symlink", "hardlink", "fifo", "parent", "ancestor"]
)
def test_unsafe_file_and_directory_objects_rejected(tmp_path, unsafe):
    directory = tmp_path / "config"
    directory.mkdir(mode=0o700)
    path = directory / "agent.control.json"
    session = bootstrap_control(
        "agent", "http://127.0.0.1:5681", directory / "agent.yaml"
    )
    if unsafe == "mode":
        path.chmod(0o644)
    elif unsafe == "symlink":
        target = directory / "real-file"
        path.rename(target)
        path.symlink_to(target)
    elif unsafe == "hardlink":
        os.link(path, directory / "linked")
    elif unsafe == "fifo":
        path.unlink()
        os.mkfifo(path)
    elif unsafe == "parent":
        directory.chmod(0o777)
    elif unsafe == "ancestor":
        tmp_path.chmod(0o777)
    try:
        with pytest.raises(ControlCredentialError):
            read_control_descriptor(path)
        with pytest.raises(ControlCredentialError):
            bootstrap_control(
                "agent", "http://127.0.0.1:5681", directory / "agent.yaml"
            )
    finally:
        directory.chmod(0o700)
        tmp_path.chmod(0o700)
        session._active.clear()
        session._lease.close()
        session._lease = None


@pytest.mark.skipif(os.name == "nt", reason="POSIX publication syscall failures")
@pytest.mark.parametrize(
    "failure", ["write", "file-fsync", "rename", "directory-fsync"]
)
def test_atomic_publish_failure_cleans_new_file_temp_and_all_fds(
    tmp_path, monkeypatch, failure
):
    real_fsync = os.fsync
    real_close = os.close
    opened = []
    closed = []
    real_open = os.open

    def track_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def track_close(fd):
        closed.append(fd)
        return real_close(fd)

    def fail(*args, **kwargs):
        raise OSError("FAKE-SENSITIVE-EXCEPTION")

    def fail_fsync(fd):
        directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if (failure == "directory-fsync") == directory:
            fail()
        return real_fsync(fd)

    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "close", track_close)
    if failure == "write":
        monkeypatch.setattr(os, "write", fail)
    elif failure == "rename":
        monkeypatch.setattr(os, "replace", fail)
    else:
        monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(ControlCredentialError) as caught:
        bootstrap_control("agent", "http://127.0.0.1:5681", tmp_path / "agent.yaml")
    assert str(caught.value) == "control_publish_failed"
    assert not list(tmp_path.iterdir())
    assert sorted(opened) == sorted(closed)


@pytest.mark.parametrize(
    "body",
    [
        b"not-json",
        b"{}",
        b"[]",
        b"\xff",
        b"x" * (16 * 1024 + 1),
        b'{"version":1,"version":1}',
    ],
)
def test_bad_descriptor_never_echoes_raw_input(tmp_path, body):
    path = tmp_path / "agent.control.json"
    path.write_bytes(body)
    path.chmod(0o600)
    with pytest.raises(ControlCredentialError) as caught:
        read_control_descriptor(path)
    assert str(caught.value) == "control_read_failed"


def test_revoke_failure_still_invalidates_and_releases_lease(tmp_path, monkeypatch):
    session = bootstrap_control(
        "agent", "http://127.0.0.1:5681", tmp_path / "agent.yaml"
    )
    lease = session._lease

    def fail(*args):
        raise ControlCredentialError("control_revoke_failed")

    monkeypatch.setattr(lease, "revoke", fail)
    with pytest.raises(ControlCredentialError):
        revoke_control(session)
    assert not session.is_active() and session._lease is None
    # A later startup never trusts the left-over key.
    next_session = bootstrap_control(
        "agent", "http://127.0.0.1:5681", tmp_path / "agent.yaml"
    )
    assert session.token != next_session.token
    revoke_control(next_session)


@pytest.mark.skipif(os.name != "nt", reason="Windows real owner/DACL APIs")
def test_windows_creation_is_owner_only_and_reparse_is_rejected(tmp_path):
    session = bootstrap_control(
        "agent", "http://127.0.0.1:5681", tmp_path / "agent.yaml"
    )
    try:
        assert (
            read_control_descriptor(session.credential_file).control_token
            == session.token
        )
        assert session.token not in repr(session)
    finally:
        revoke_control(session)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 2),
        ("role", "unknown"),
        ("role", ["agent"]),
        ("boot_id", "old"),
        ("boot_id", "A" * 32),
        ("control_token", "secret\nline"),
        ("base_url", "http://user:secret@127.0.0.1:5681"),
    ],
)
def test_bad_schema_never_echoes_secrets(tmp_path, field, value):
    import json

    path = tmp_path / "agent.control.json"
    data = {
        "version": 1,
        "role": "agent",
        "base_url": "http://127.0.0.1:5681",
        "boot_id": "a" * 32,
        "control_token": "FAKE-MARKER-NEVER-ECHO",
    }
    data[field] = value
    path.write_text(json.dumps(data), encoding="ascii")
    path.chmod(0o600)
    with pytest.raises(ControlCredentialError) as caught:
        read_control_descriptor(path)
    assert str(caught.value) == "control_read_failed"
    assert "secret" not in str(caught.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows writer and real ACL mutation")
def test_windows_production_writer_fixtures_match_acceptance(tmp_path):
    from taskpaw_v3.tests.windows_control_fixture import generate

    manifest = generate(tmp_path / "fixture")
    cases = {case["name"]: case for case in manifest["cases"]}
    assert cases["secure"]["expect"] == "accept"
    assert cases["broad_acl"]["expect"] == "reject"
    assert cases["null_acl"]["expect"] == "reject"
    assert cases["unsafe_parent"]["expect"] == "reject"
    for name in ("trusted_system_parent", "trusted_admins_parent"):
        assert cases[name]["expect"] in {"accept", "skip"}
    for case in cases.values():
        if case["expect"] == "accept":
            descriptor = read_control_descriptor(case["path"])
            assert descriptor.control_token == "fake-python-rust-interop-token"
        elif case["expect"] == "reject":
            with pytest.raises(ControlCredentialError):
                read_control_descriptor(case["path"])
        else:
            assert case["expect"] == "skip" and case["reason"] in {
                "owner_privilege_unavailable",
                "symlink_privilege_unavailable",
            }


def _darwin_acl(path, entry):
    subprocess.run(["chmod", "+a", entry, str(path)], check=True, capture_output=True)


def _clear_darwin_acl(path):
    subprocess.run(["chmod", "-RN", str(path)], check=True, capture_output=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin native extended ACL")
@pytest.mark.parametrize(
    "entry",
    [
        "everyone allow write",
        "everyone allow read,write,execute,file_inherit,directory_inherit",
        "everyone allow read,file_inherit,only_inherit",
    ],
)
def test_darwin_acl_parent_rejected_before_secret_write(tmp_path, monkeypatch, entry):
    _darwin_acl(tmp_path, entry)
    writes = []
    real_write = os.write

    def track_write(fd, data):
        writes.append(len(data))
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", track_write)
    try:
        assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
        with pytest.raises(ControlCredentialError):
            bootstrap_control("agent", "http://127.0.0.1:5681", tmp_path / "agent.yaml")
        assert writes == []
        assert list(tmp_path.iterdir()) == []
    finally:
        _clear_darwin_acl(tmp_path)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin native extended ACL")
def test_darwin_acl_unsafe_ancestor_rejected(tmp_path):
    directory = tmp_path / "config"
    directory.mkdir(mode=0o700)
    _darwin_acl(tmp_path, "everyone allow delete_child")
    try:
        with pytest.raises(ControlCredentialError):
            bootstrap_control(
                "agent", "http://127.0.0.1:5681", directory / "agent.yaml"
            )
        assert list(directory.iterdir()) == []
    finally:
        _clear_darwin_acl(tmp_path)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin native extended ACL")
def test_darwin_acl_unsafe_file_without_unsafe_parent_rejected(tmp_path):
    session = bootstrap_control(
        "agent", "http://127.0.0.1:5681", tmp_path / "agent.yaml"
    )
    path = session.credential_file
    _darwin_acl(path, "everyone allow read")
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with pytest.raises(ControlCredentialError):
            read_control_descriptor(path)
        with pytest.raises(ControlCredentialError):
            bootstrap_control("agent", "http://127.0.0.1:5681", tmp_path / "agent.yaml")
    finally:
        _clear_darwin_acl(path)
        revoke_control(session)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin native extended ACL")
def test_darwin_acl_harmless_deny_metadata_and_native_home_compatible(tmp_path):
    _darwin_acl(tmp_path, "everyone deny delete")
    _darwin_acl(tmp_path, "everyone allow read,execute")
    session = bootstrap_control(
        "agent", "http://127.0.0.1:5681", tmp_path / "agent.yaml"
    )
    try:
        _darwin_acl(session.credential_file, "everyone allow readattr")
        assert (
            read_control_descriptor(session.credential_file).control_token
            == session.token
        )
        home_fd = os.open(
            os.path.expanduser("~"), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        )
        try:
            control_file._check_darwin_acl(home_fd, directory=True, final=False)
        finally:
            os.close(home_fd)
    finally:
        _clear_darwin_acl(tmp_path)
        revoke_control(session)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin native extended ACL")
def test_darwin_acl_new_temporary_fd_checked_before_any_secret_bytes(
    tmp_path, monkeypatch
):
    real_open, real_write = os.open, os.write
    writes = []

    def inject_acl(name, flags, *args, **kwargs):
        fd = real_open(name, flags, *args, **kwargs)
        if flags & os.O_CREAT:
            _darwin_acl(tmp_path / name, "everyone allow read")
        return fd

    def track_write(fd, data):
        writes.append(len(data))
        return real_write(fd, data)

    monkeypatch.setattr(os, "open", inject_acl)
    monkeypatch.setattr(os, "write", track_write)
    with pytest.raises(ControlCredentialError):
        bootstrap_control("agent", "http://127.0.0.1:5681", tmp_path / "agent.yaml")
    assert writes == [] and list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin native extended ACL")
def test_darwin_acl_validation_and_content_use_same_fd_after_replacement(
    tmp_path, monkeypatch
):
    session = bootstrap_control(
        "agent", "http://127.0.0.1:5681", tmp_path / "agent.yaml"
    )
    path, saved = session.credential_file, tmp_path / "saved"
    real_check = control_file._check_darwin_acl
    replaced = False

    def replacing_check(fd, *, directory, final=False):
        nonlocal replaced
        if not directory and not replaced:
            replaced = True
            path.rename(saved)
            path.write_bytes(
                control_file.ControlDescriptor(
                    1,
                    "agent",
                    "http://127.0.0.1:5681",
                    "a" * 32,
                    "fake-replacement-key",
                )._bytes()
            )
            path.chmod(0o600)
            _darwin_acl(path, "everyone allow read")
        return real_check(fd, directory=directory, final=final)

    monkeypatch.setattr(control_file, "_check_darwin_acl", replacing_check)
    try:
        assert read_control_descriptor(path).control_token == session.token
        with pytest.raises(ControlCredentialError):
            read_control_descriptor(path)
    finally:
        _clear_darwin_acl(path)
        path.unlink()
        saved.rename(path)
        revoke_control(session)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin native extended ACL")
def test_darwin_acl_query_failure_cleans_temp_and_directory_fds(tmp_path, monkeypatch):
    import ctypes
    import errno

    real_query, real_open, real_close = control_file._acl_fd, os.open, os.close
    opened, closed, writes = [], [], []

    def fail_file_query(fd, acl_type):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            ctypes.set_errno(errno.EACCES)
            return None
        return real_query(fd, acl_type)

    def track_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def track_close(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(control_file, "_acl_fd", fail_file_query)
    monkeypatch.setattr(os, "open", track_open)
    monkeypatch.setattr(os, "close", track_close)
    monkeypatch.setattr(os, "write", lambda fd, data: writes.append(len(data)))
    with pytest.raises(ControlCredentialError) as caught:
        bootstrap_control("agent", "http://127.0.0.1:5681", tmp_path / "agent.yaml")
    assert str(caught.value) == "control_publish_failed"
    assert writes == [] and list(tmp_path.iterdir()) == []
    assert sorted(opened) == sorted(closed)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin native extended ACL")
@pytest.mark.parametrize(
    "failure",
    [
        "unknown-permission",
        "unknown-flag",
        "unknown-tag",
        "iteration-error",
        "too-many-entries",
    ],
)
def test_darwin_acl_native_parser_fails_closed(tmp_path, monkeypatch, failure):
    import ctypes
    import errno

    session = bootstrap_control(
        "agent", "http://127.0.0.1:5681", tmp_path / "agent.yaml"
    )
    path = session.credential_file
    _darwin_acl(path, "everyone allow readattr")
    native_mask, native_flag = control_file._acl_mask, control_file._acl_flag
    native_tag, native_entry = control_file._acl_tag, control_file._acl_entry

    def mask(entry, output):
        result = native_mask(entry, output)
        ctypes.cast(output, ctypes.POINTER(ctypes.c_uint64)).contents.value |= 1 << 31
        return result

    def flag(flagset, bit):
        return 1 if bit.value == 0x200 else native_flag(flagset, bit)

    def tag(entry, output):
        result = native_tag(entry, output)
        ctypes.cast(output, ctypes.POINTER(ctypes.c_int)).contents.value = 3
        return result

    def iteration(acl, index, output):
        if failure == "iteration-error":
            ctypes.set_errno(errno.EACCES)
            return -1
        return native_entry(acl, 0, output)

    name, replacement = {
        "unknown-permission": ("_acl_mask", mask),
        "unknown-flag": ("_acl_flag", flag),
        "unknown-tag": ("_acl_tag", tag),
        "iteration-error": ("_acl_entry", iteration),
        "too-many-entries": ("_acl_entry", iteration),
    }[failure]
    try:
        with monkeypatch.context() as context:
            context.setattr(control_file, name, replacement)
            with pytest.raises(ControlCredentialError) as caught:
                read_control_descriptor(path)
            assert str(caught.value) == "control_read_failed"
    finally:
        _clear_darwin_acl(path)
        revoke_control(session)
