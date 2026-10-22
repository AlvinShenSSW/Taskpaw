"""Bounded Windows projection retries retain the standalone writer contract."""

from __future__ import annotations

import ctypes
import errno
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from taskpaw_v3.integrations import activity_writer as aw


def denied(code: int | None) -> OSError:
    error = PermissionError(errno.EACCES, "PRIVATE_SENTINEL")
    if code is not None:
        error.winerror = code
    return error


def retry_clock(monkeypatch):
    now = [0.0]
    waits = []

    def sleep(delay):
        waits.append(delay)
        now[0] += delay

    monkeypatch.setattr(aw.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(aw.time, "sleep", sleep)
    return now, waits


@pytest.mark.parametrize("code", [5, 32, 33])
def test_transient_replace_reuses_one_closed_synced_tmp(tmp_path, monkeypatch, code):
    path = tmp_path / "state.json"
    old = b'{"state":"old"}'
    path.write_bytes(old)
    original_replace, original_fsync = aw.os.replace, aw.os.fsync
    attempts, synced = [], []
    _, waits = retry_clock(monkeypatch)
    monkeypatch.setattr(aw.sys, "platform", "win32")

    def fsync(fd):
        synced.append(fd)
        original_fsync(fd)

    def replace(source, destination):
        assert destination == path
        assert path.read_bytes() == old
        assert len(synced) == 1
        # Reading a flushed/closed complete temporary JSON is possible on every
        # attempt; its exact bytes and name must not change between retries.
        attempts.append((source, Path(source).read_bytes()))
        if len(attempts) <= 2:
            raise denied(code)
        original_replace(source, destination)

    monkeypatch.setattr(aw.os, "fsync", fsync)
    monkeypatch.setattr(aw.os, "replace", replace)
    assert aw.write_activity(str(path), "codex", "busy", "fake", 1234.0) == path
    assert len(attempts) == 3 and attempts[0] == attempts[1] == attempts[2]
    assert len(waits) == 2 and 0 < sum(waits) <= 0.2
    assert json.loads(path.read_text()) == {
        "tool": "codex",
        "state": "busy",
        "session": "fake",
        "ts": 1234.0,
    }
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("code", [5, 32, 33])
def test_persistent_replace_refusal_is_bounded_and_keeps_target(
    tmp_path, monkeypatch, code
):
    path = tmp_path / "state.json"
    old = b'{"state":"old"}'
    path.write_bytes(old)
    error = denied(code)
    calls = []
    _, waits = retry_clock(monkeypatch)
    monkeypatch.setattr(aw.sys, "platform", "win32")

    def replace(source, destination):
        assert path.read_bytes() == old and destination == path
        calls.append(source)
        raise error

    monkeypatch.setattr(aw.os, "replace", replace)
    with pytest.raises(OSError) as raised:
        aw.write_activity(str(path), "codex", "busy", ts=1234.0)
    assert raised.value is error
    assert 1 < len(calls) <= 6 and len(set(calls)) == 1
    assert 0 < sum(waits) <= 0.2
    assert path.read_bytes() == old and not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    "platform,code",
    [("linux", 5), ("darwin", 32), ("win32", None), ("win32", 112), ("win32", 2)],
)
def test_unrelated_errors_are_not_retried(tmp_path, monkeypatch, platform, code):
    error, calls = denied(code), []
    _, waits = retry_clock(monkeypatch)
    monkeypatch.setattr(aw.sys, "platform", platform)

    def replace(*args):
        calls.append(args)
        raise error

    monkeypatch.setattr(aw.os, "replace", replace)
    with pytest.raises(OSError) as raised:
        aw.write_activity(str(tmp_path / "state.json"), "codex", "busy")
    assert raised.value is error and len(calls) == 1 and not waits
    assert not list(tmp_path.glob(".*.tmp"))


def test_elapsed_deadline_prevents_another_replace(tmp_path, monkeypatch):
    now, waits = retry_clock(monkeypatch)
    monkeypatch.setattr(aw.sys, "platform", "win32")
    calls = []

    def replace(*args):
        calls.append(args)
        now[0] += 0.195
        raise denied(5)

    monkeypatch.setattr(aw.os, "replace", replace)
    with pytest.raises(OSError):
        aw.write_activity(str(tmp_path / "state.json"), "codex", "busy")
    assert len(calls) == 1 and len(waits) == 1
    assert 0 < waits[0] <= 0.0051 and now[0] <= 0.2


def test_fsync_failure_is_not_retried(tmp_path, monkeypatch):
    _, waits = retry_clock(monkeypatch)
    calls = []
    error = denied(5)
    monkeypatch.setattr(aw.os, "fsync", lambda fd: (_ for _ in ()).throw(error))
    monkeypatch.setattr(aw.os, "replace", lambda *args: calls.append(args))
    with pytest.raises(OSError) as raised:
        aw.write_activity(str(tmp_path / "state.json"), "codex", "busy")
    assert raised.value is error and not calls and not waits
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("permanent", [False, True])
def test_rich_projection_retries_preserve_fact_and_nonce_semantics(
    tmp_path, monkeypatch, permanent
):
    path = tmp_path / "state.json"
    original = aw.os.replace
    seed = aw.hook_fact(
        json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "seed",
                "turn_id": "one",
            }
        ),
        "codex",
        1000.0,
    )
    assert aw.publish_hook(path, seed, "busy", "seed", 1000.0)
    before = path.read_bytes()
    fact = aw.hook_fact(
        json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "second",
                "turn_id": "two",
            }
        ),
        "codex",
        1001.0,
    )
    payloads = []
    retry_clock(monkeypatch)
    monkeypatch.setattr(aw.sys, "platform", "win32")

    def replace(source, destination):
        payloads.append(Path(source).read_bytes())
        if permanent or len(payloads) == 1:
            raise denied(32)
        original(source, destination)

    monkeypatch.setattr(aw.os, "replace", replace)
    assert aw.publish_hook(path, fact, "busy", "second", 1001.0) is not permanent
    assert len(payloads) > 1 and len(set(payloads)) == 1
    store = aw.read_facts(path, "codex")
    assert len(store["facts"]) == 2
    projection = json.loads(path.read_text())
    assert aw._projection_matches(projection, store) is not permanent
    if permanent:
        assert path.read_bytes() == before
    else:
        assert projection["ts"] == 1001.0 and projection["fact_id"] == fact["id"]
    assert not list(tmp_path.glob(".*.tmp"))


def test_cli_persistent_replace_failure_remains_sanitized(
    tmp_path, monkeypatch, capsys
):
    retry_clock(monkeypatch)
    monkeypatch.setattr(aw.sys, "platform", "win32")
    monkeypatch.setattr(
        aw.os, "replace", lambda *args: (_ for _ in ()).throw(denied(5))
    )
    path = tmp_path / "state.json"
    assert aw.main(["--tool", "codex", "--state", "idle", "--path", str(path)]) == 1
    captured = capsys.readouterr()
    assert (
        captured.out == "" and captured.err == "activity writer: state write failed\n"
    )
    assert not aw.sidecar_path(path).exists() and not list(tmp_path.glob(".*.tmp"))


def test_copied_writer_retries_without_package_dependency(tmp_path, monkeypatch):
    copied = tmp_path / "standalone-writer.py"
    copied.write_bytes(Path(aw.__file__).read_bytes())
    spec = importlib.util.spec_from_file_location("standalone_activity_writer", copied)
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    original = writer.os.replace
    calls = []
    retry_clock(monkeypatch)
    monkeypatch.setattr(writer.sys, "platform", "win32")

    def replace(*args):
        calls.append(args)
        if len(calls) == 1:
            raise denied(33)
        original(*args)

    monkeypatch.setattr(writer.os, "replace", replace)
    path = tmp_path / "state.json"
    writer.write_activity(str(path), "codex", "busy", ts=1234.0)
    assert len(calls) == 2 and json.loads(path.read_text())["ts"] == 1234.0


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native sharing contract")
@pytest.mark.parametrize("release_on_retry", [False, True])
def test_native_read_handle_denies_replace_until_released(
    tmp_path, monkeypatch, release_on_retry
):
    from ctypes import wintypes

    path = tmp_path / "state.json"
    old = b'{"state":"old"}'
    path.write_bytes(old)
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    api.CreateFileW.restype = wintypes.HANDLE
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    handle = api.CreateFileW(str(path), 0x80000000, 0x1 | 0x2, None, 3, 0, None)
    assert handle not in (None, ctypes.c_void_p(-1).value)
    held = [handle]
    errors = []
    original = aw.os.replace
    now = [0.0]

    def close():
        if held:
            assert api.CloseHandle(held.pop())

    def replace(source, destination):
        try:
            original(source, destination)
        except OSError as error:
            errors.append(error.winerror)
            assert path.read_bytes() == old
            raise

    def sleep(delay):
        now[0] += delay
        if release_on_retry:
            close()  # Deterministic release after a real native refusal.

    monkeypatch.setattr(aw.os, "replace", replace)
    monkeypatch.setattr(aw.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(aw.time, "sleep", sleep)
    try:
        if release_on_retry:
            aw.write_activity(str(path), "codex", "busy", ts=1234.0)
            assert json.loads(path.read_text())["state"] == "busy"
        else:
            with pytest.raises(OSError):
                aw.write_activity(str(path), "codex", "busy", ts=1234.0)
            assert path.read_bytes() == old and 1 < len(errors) <= 6
        assert errors and set(errors) <= {5, 32, 33}
        assert not list(tmp_path.glob(".*.tmp"))
    finally:
        close()
