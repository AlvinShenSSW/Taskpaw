"""`SubsJob`: ASR attempts, outcome mapping, identity, publishing (#177, subs/job.py)."""

from __future__ import annotations

import errno
import json
import logging
import os
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from taskpaw_v3.monitors.subs import job as job_mod
from taskpaw_v3.monitors.subs.job import (
    ASR_TAIL_CHARS,
    ASR_TAIL_LINES,
    JobOutcome,
    PublishResult,
    SubsJob,
    source_identity,
)
from taskpaw_v3.monitors.subs.srt import Cue, load, parse
from taskpaw_v3.monitors.subs.whisperjav import attempt_dir, build_argv

SRT = "1\n00:00:00,000 --> 00:00:01,000\nはい\n"


class FakeAsrChild:
    """A `ChildProcess` stand-in for the ASR child: records calls, exits when
    told, and optionally writes WhisperJAV outputs into its `--output-dir`."""

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.pid = 4242
        self.rc: Optional[int] = None
        self.calls: list[str] = []
        self.out_dir = Path(argv[argv.index("--output-dir") + 1])

    def poll(self) -> Optional[int]:
        return self.rc

    def tail(self, lines: int = 10, max_chars: int = 800) -> str:
        return "TAIL"

    def terminate_tree(self, timeout: float = 5.0) -> None:
        self.calls.append("terminate_tree")
        if self.rc is None:
            self.rc = 1

    def join_readers(self, timeout: float = 2.0) -> None:
        self.calls.append("join_readers")

    def finish(self, rc: int, state: str = "done", text: Optional[str] = SRT) -> None:
        files = []
        if state:
            out = None
            if text is not None:
                p = self.out_dir / "m_restored.ja.whisperjav.srt"
                p.write_text(text, encoding="utf-8")
                out = str(p)
            files.append({"path": "m", "state": state, "output": out, "detail": ""})
            (self.out_dir / "whisperjav_run.json").write_text(
                json.dumps({"files": files}), encoding="utf-8"
            )
        self.rc = rc


class Spawner:
    def __init__(self, fail: Optional[BaseException] = None) -> None:
        self.children: list[FakeAsrChild] = []
        self.fail = fail

    def __call__(self, argv: list[str]) -> FakeAsrChild:
        if self.fail is not None:
            raise self.fail
        c = FakeAsrChild(argv)
        self.children.append(c)
        return c


def _job(tmp_path: Path, **kw) -> SubsJob:
    out = tmp_path / "out"
    out.mkdir(exist_ok=True)
    media = out / "m_restored.mp4"
    if not media.exists():
        media.write_bytes(b"video")
    return SubsJob(
        run=("inst", 7),
        job_id="m.mp4",
        media=media,
        relpath="m.mp4",
        ja_target=out / "m_restored.ja.srt",
        zh_target=out / "m_restored.srt",
        staging_root=out / ".avsubs",
        exe="C:/WJ/whisperjav.exe",
        engine="anime-whisper",
        extra="--sensitivity aggressive",
        **kw,
    )


def test_source_identity(tmp_path):
    p = tmp_path / "f"
    p.write_bytes(b"abc")
    st = os.stat(p)
    assert source_identity(p) == (3, st.st_mtime_ns)
    with pytest.raises(OSError):
        source_identity(tmp_path / "missing")


def test_start_asr_fresh_attempt_dir_and_argv(tmp_path):
    job = _job(tmp_path)
    sp = Spawner()
    d1 = attempt_dir(job.staging_root, job.relpath, 1)
    d1.mkdir(parents=True)
    (d1 / "stale.srt").write_text("old", encoding="utf-8")
    assert job.start_asr(sp) is None
    assert job.attempt == 1
    assert d1.is_dir() and not (d1 / "stale.srt").exists()  # previous contents gone
    assert (job.staging_root / "tmp").is_dir()
    c = sp.children[0]
    assert job.child is c
    assert c.argv == build_argv(
        job.exe, job.media, d1, job.staging_root / "tmp", job.engine, job.extra
    )
    assert c.argv[1] == str(job.media)
    assert job.identity == source_identity(job.media)
    assert job.started_at > 0


def test_start_asr_second_attempt_new_dir(tmp_path):
    job = _job(tmp_path)
    sp = Spawner()
    assert job.start_asr(sp) is None
    sp.children[0].finish(1, state="")
    assert job.poll_asr() == JobOutcome("failed", (), "exit code 1: TAIL")
    assert job.start_asr(sp) is None
    assert job.attempt == 2
    d2 = attempt_dir(job.staging_root, job.relpath, 2)
    assert sp.children[1].out_dir == d2


def test_start_asr_identity_change_before_start_is_unstable(tmp_path):
    job = _job(tmp_path, identity=(1, 1))
    sp = Spawner()
    assert job.start_asr(sp) == "unstable"
    assert sp.children == [] and job.child is None and job.attempt == 0


def test_start_asr_media_deleted_is_unstable_never_raises(tmp_path):
    job = _job(tmp_path)
    job.media.unlink()
    sp = Spawner()
    assert job.start_asr(sp) == "unstable"
    assert sp.children == [] and job.child is None


def test_start_asr_spawn_error_text(tmp_path):
    job = _job(tmp_path)
    sp = Spawner(fail=FileNotFoundError(2, "No such file"))
    err = job.start_asr(sp)
    assert err is not None and err.startswith("launch: FileNotFoundError: ")
    assert job.child is None


def test_start_asr_bad_extra_is_error_text_not_raise(tmp_path):
    job = _job(tmp_path)
    job.extra = '--note "unbalanced'
    err = job.start_asr(Spawner())
    assert err is not None and err.startswith("launch: ValueError")


def test_poll_asr_none_while_running_then_succeeded(tmp_path):
    job = _job(tmp_path)
    sp = Spawner()
    job.start_asr(sp)
    assert job.poll_asr() is None
    sp.children[0].finish(0)
    o = job.poll_asr()
    assert o == JobOutcome("succeeded", (Cue(1, 0, 1000, "はい"),), "")
    assert job.child is None
    assert "join_readers" in sp.children[0].calls
    assert job.poll_asr() is None  # reaped


@pytest.mark.parametrize(
    "rc,state,text,kind",
    [
        (0, "empty", "", "no_speech"),
        (0, "done", "", "no_speech"),
        (0, "failed", SRT, "failed"),
        (0, "done", None, "failed"),
        (3, "done", SRT, "failed"),
        (0, "", None, "failed"),  # no manifest
    ],
)
def test_poll_asr_maps_outcomes(tmp_path, rc, state, text, kind):
    job = _job(tmp_path)
    sp = Spawner()
    job.start_asr(sp)
    sp.children[0].finish(rc, state=state, text=text)
    o = job.poll_asr()
    assert o is not None and o.kind == kind


def test_poll_asr_media_changed_during_asr_is_unstable(tmp_path):
    job = _job(tmp_path)
    sp = Spawner()
    job.start_asr(sp)
    job.media.write_bytes(b"a different, longer video")
    sp.children[0].finish(0)
    o = job.poll_asr()
    assert o is not None and o.kind == "unstable"
    assert job.child is None


def test_poll_asr_media_removed_during_asr_is_unstable(tmp_path):
    job = _job(tmp_path)
    sp = Spawner()
    job.start_asr(sp)
    job.media.unlink()
    sp.children[0].finish(0)
    o = job.poll_asr()
    assert o is not None and o.kind == "unstable"


def _no_tmp_left(folder: Path) -> bool:
    return not list(folder.glob("*.tmp"))


def test_publish_ja_and_zh_via_generation_tmp_and_a_refusing_move(
    tmp_path, monkeypatch
):
    # #191 (AC4): the tmp is moved into place WITHOUT replacing — `os.rename`
    # on Windows, `os.link` elsewhere — never `os.replace`.
    job = _job(tmp_path)
    cues = [Cue(5, 0, 1000, "はい"), Cue(9, 1000, 2000, "いいえ")]
    seen: list[tuple[str, str]] = []
    move = "rename" if job_mod._RENAME_REFUSES else "link"
    real_move = getattr(os, move)

    def spy(src, dst):
        seen.append((str(src), str(dst)))
        return real_move(src, dst)

    def no_replace(src, dst):
        raise AssertionError("os.replace must not publish")

    monkeypatch.setattr(os, move, spy)
    monkeypatch.setattr(os, "replace", no_replace)
    assert job.publish_ja(cues) == PublishResult("ok")
    assert job.publish_zh([Cue(1, 0, 1000, "是")]).ok
    assert seen == [
        (str(job.ja_target) + ".7.tmp", str(job.ja_target)),
        (str(job.zh_target) + ".7.tmp", str(job.zh_target)),
    ]
    assert parse(job.ja_target.read_text(encoding="utf-8"))[1].text == "いいえ"
    assert load(job.zh_target) == [Cue(1, 0, 1000, "是")]
    assert _no_tmp_left(job.ja_target.parent)


def test_publish_error_is_text_and_tmp_cleaned(tmp_path, monkeypatch):
    # #191: a directory in the way is now `exists` (never replaced); an error
    # is a failed write or move.
    job = _job(tmp_path)
    job.ja_target = tmp_path / "gone" / "m_restored.ja.srt"  # no such folder
    res = job.publish_ja([Cue(1, 0, 1, "x")])
    assert res.kind == "error" and not res.ok
    assert res.detail.startswith("publish m_restored.ja.srt: FileNotFoundError")
    move = "rename" if job_mod._RENAME_REFUSES else "link"

    def denied(src, dst):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(os, move, denied)
    res = job.publish_zh([Cue(1, 0, 1, "x")])
    assert res.kind == "error" and "PermissionError" in res.detail
    assert job.zh_target.name in res.detail
    assert not job.zh_target.exists()
    assert _no_tmp_left(job.zh_target.parent)


def test_publish_empty_writes_two_zero_byte_files(tmp_path):
    job = _job(tmp_path)
    assert job.publish_empty().ok
    assert job.ja_target.read_bytes() == b"" and job.zh_target.read_bytes() == b""
    assert _no_tmp_left(job.ja_target.parent)


def test_load_ja_round_trip(tmp_path):
    job = _job(tmp_path)
    cues = [Cue(1, 0, 1000, "はい\nそう"), Cue(2, 1500, 2500, "ね")]
    job.publish_ja(cues)
    assert job.load_ja() == cues


# ── #187: the .ja.srt checkpoint goes once the zh is published ────────────
def test_discard_ja_removes_only_the_transcript_and_tolerates_a_missing_one(
    tmp_path,
):
    job = _job(tmp_path)
    assert job.publish_ja([Cue(1, 0, 1000, "はい")]).ok
    assert job.publish_zh([Cue(1, 0, 1000, "是")]).ok
    assert job.discard_ja() is None
    assert not job.ja_target.exists()
    assert job.zh_target.exists() and job.media.exists()
    assert job.discard_ja() is None  # already gone: missing_ok


def test_discard_ja_failure_is_error_text_and_never_raises(tmp_path):
    job = _job(tmp_path)
    job.ja_target.mkdir()  # a directory in the way: unlink fails (OSError)
    (job.ja_target / "keep").write_text("x", encoding="utf-8")
    err = job.discard_ja()
    assert err is not None and job.ja_target.name in err
    assert job.ja_target.is_dir()


# ── #191 (AC4): publishing never overwrites ────────────────────────────────
LIBRARY = "1\n00:00:00,000 --> 00:00:01,000\n店主的字幕\n".encode("utf-8")


def _bypass_pre_check(monkeypatch) -> None:
    """Skip the defensive pre-check so the MOVE itself must refuse (C1)."""
    monkeypatch.setattr(job_mod, "_present", lambda path: False)


def test_publish_result_values(tmp_path):
    job = _job(tmp_path)
    ok = job.publish_zh([Cue(1, 0, 1000, "是")])
    assert ok == PublishResult("ok") and ok.ok and ok.detail == ""
    again = job.publish_zh([Cue(1, 0, 1000, "否")])
    assert again.kind == "exists" and not again.ok
    assert again.detail == "m_restored.srt already exists; not replaced"
    assert load(job.zh_target) == [Cue(1, 0, 1000, "是")]
    assert _no_tmp_left(job.zh_target.parent)
    assert not isinstance(ok, str)  # F8: a distinct type, never an error text


@pytest.mark.parametrize("pre_check", [True, False])
@pytest.mark.parametrize("kind", ["file", "read-only", "open", "directory"])
def test_publish_never_overwrites_an_existing_target(
    tmp_path, monkeypatch, kind, pre_check
):
    # The platform's own refusing move (Windows: a real `os.rename`; POSIX:
    # `os.link`), with and without the pre-check in front of it.
    job = _job(tmp_path)
    if not pre_check:
        _bypass_pre_check(monkeypatch)
    target = job.zh_target
    handle = None
    if kind == "directory":
        target.mkdir()
        (target / "keep.txt").write_bytes(LIBRARY)
    else:
        target.write_bytes(LIBRARY)
        if kind == "read-only":
            os.chmod(target, stat.S_IREAD)
        elif kind == "open":
            handle = open(target, "rb")
    try:
        res = job.publish_zh([Cue(1, 0, 1000, "机器翻译")])
    finally:
        if handle is not None:
            handle.close()
        if kind == "read-only":
            os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
    assert res.kind == "exists", res
    if kind == "directory":
        assert (target / "keep.txt").read_bytes() == LIBRARY
    else:
        assert target.read_bytes() == LIBRARY  # byte-identical
    assert _no_tmp_left(target.parent)


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS is case-insensitive")
@pytest.mark.parametrize("pre_check", [True, False])
def test_windows_rename_refuses_a_case_variant_target(tmp_path, monkeypatch, pre_check):
    assert job_mod._RENAME_REFUSES
    job = _job(tmp_path)
    if not pre_check:
        _bypass_pre_check(monkeypatch)
    variant = job.zh_target.with_name(job.zh_target.name.upper())
    variant.write_bytes(LIBRARY)
    res = job.publish_zh([Cue(1, 0, 1000, "机器翻译")])
    assert res.kind == "exists"
    assert [p.name for p in variant.parent.iterdir() if p.suffix == ".SRT"] == [
        variant.name
    ]
    assert variant.read_bytes() == LIBRARY
    assert _no_tmp_left(variant.parent)


@pytest.mark.parametrize("pre_check", [True, False])
def test_the_hard_link_path_publishes_and_refuses(tmp_path, monkeypatch, pre_check):
    # POSIX path (forced here on every platform: NTFS has hard links too, A3):
    # `os.link(tmp, target)` + unlink the tmp; EEXIST refuses.
    monkeypatch.setattr(job_mod, "_RENAME_REFUSES", False)
    if not pre_check:
        _bypass_pre_check(monkeypatch)

    def no_rename(src, dst):
        raise AssertionError("the link path never renames")

    monkeypatch.setattr(os, "rename", no_rename)
    monkeypatch.setattr(os, "replace", no_rename)
    job = _job(tmp_path)
    assert job.publish_ja([Cue(1, 0, 1000, "はい")]).ok
    assert load(job.ja_target) == [Cue(1, 0, 1000, "はい")]
    assert _no_tmp_left(job.ja_target.parent)
    job.zh_target.write_bytes(LIBRARY)
    res = job.publish_zh([Cue(1, 0, 1000, "机器翻译")])
    assert res.kind == "exists"
    assert job.zh_target.read_bytes() == LIBRARY
    assert _no_tmp_left(job.zh_target.parent)


@pytest.mark.parametrize("code", [errno.EPERM, errno.EOPNOTSUPP])
def test_no_hard_links_fall_back_to_a_checked_replace(
    tmp_path, monkeypatch, caplog, code
):
    monkeypatch.setattr(job_mod, "_RENAME_REFUSES", False)

    def no_links(src, dst):
        raise OSError(code, "no hard links here")

    monkeypatch.setattr(os, "link", no_links)
    job = _job(tmp_path)
    with caplog.at_level(logging.DEBUG, logger="taskpaw.subs.job"):
        assert job.publish_ja([Cue(1, 0, 1000, "はい")]).ok
    assert load(job.ja_target) == [Cue(1, 0, 1000, "はい")]
    assert any("no hard links" in r.getMessage() for r in caplog.records)
    assert _no_tmp_left(job.ja_target.parent)
    # the fallback's own check refuses even when the first one is bypassed
    calls = {"n": 0}
    real = job_mod._present

    def first_misses(path):
        calls["n"] += 1
        return False if calls["n"] == 1 else real(path)

    monkeypatch.setattr(job_mod, "_present", first_misses)
    job.zh_target.write_bytes(LIBRARY)
    res = job.publish_zh([Cue(1, 0, 1000, "机器翻译")])
    assert res.kind == "exists" and calls["n"] == 2
    assert job.zh_target.read_bytes() == LIBRARY
    assert _no_tmp_left(job.zh_target.parent)


def test_any_other_link_error_is_an_error_not_a_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(job_mod, "_RENAME_REFUSES", False)

    def io_error(src, dst):
        raise OSError(errno.EIO, "i/o error")

    monkeypatch.setattr(os, "link", io_error)
    job = _job(tmp_path)
    res = job.publish_zh([Cue(1, 0, 1000, "是")])
    assert res.kind == "error" and "i/o error" in res.detail
    assert not job.zh_target.exists() and _no_tmp_left(job.zh_target.parent)


def test_an_unreadable_target_state_fails_closed(tmp_path, monkeypatch):
    # The pre-check cannot tell whether the target exists → error, not a write.
    def unreadable(path):
        raise PermissionError(13, "denied")

    job = _job(tmp_path)
    monkeypatch.setattr(job_mod.os, "lstat", unreadable)
    res = job.publish_zh([Cue(1, 0, 1000, "是")])
    monkeypatch.undo()
    assert res.kind == "error" and "PermissionError" in res.detail
    assert not job.zh_target.exists() and _no_tmp_left(job.zh_target.parent)


def test_publish_empty_refused_srt_removes_the_empty_ja_it_just_wrote(tmp_path):
    job = _job(tmp_path)
    job.zh_target.write_bytes(LIBRARY)
    res = job.publish_empty()
    assert res.kind == "exists"
    assert not job.ja_target.exists()
    assert job.zh_target.read_bytes() == LIBRARY
    assert job.ja_published  # it did publish (and then took it back)
    assert _no_tmp_left(job.zh_target.parent)


def test_publish_empty_refused_ja_writes_nothing(tmp_path):
    job = _job(tmp_path)
    job.ja_target.write_bytes(LIBRARY)  # a transcript appeared mid-run
    res = job.publish_empty()
    assert res.kind == "exists" and not job.ja_published
    assert job.ja_target.read_bytes() == LIBRARY
    assert not job.zh_target.exists()


def test_discard_own_ja_only_removes_a_transcript_this_job_published(tmp_path):
    job = _job(tmp_path)
    job.ja_target.write_bytes(LIBRARY)  # the library's own transcript
    assert not job.ja_published
    assert job.discard_own_ja() is None
    assert job.ja_target.read_bytes() == LIBRARY
    assert job.publish_ja([Cue(1, 0, 1000, "はい")]).kind == "exists"
    assert not job.ja_published
    job.ja_target.unlink()
    assert job.publish_ja([Cue(1, 0, 1000, "はい")]).ok and job.ja_published
    assert job.discard_own_ja() is None
    assert not job.ja_target.exists()


def test_terminate_live_and_dead_child(tmp_path):
    job = _job(tmp_path)
    sp = Spawner()
    job.start_asr(sp)
    live = sp.children[0]
    job.terminate(1.0)
    assert live.calls == ["terminate_tree", "join_readers"]
    assert job.child is None
    job.terminate(1.0)  # no child: no-op
    job.start_asr(sp)
    dead = sp.children[1]
    dead.finish(0)
    job.terminate(1.0)  # harmless on an exited, unpolled child
    assert dead.calls == ["terminate_tree", "join_readers"]
    assert job.child is None


def test_start_asr_with_real_childprocess_sees_devnull(tmp_path):
    # The real default spawn: a stand-in "exe" (python) that would block on a
    # tty prompt instead reads EOF from DEVNULL and exits (D15).
    job = _job(tmp_path)
    job.exe = sys.executable
    job.engine = "custom"
    job.extra = ""
    # python treats the media path as a script; make it one that reads stdin.
    job.media.write_text("import sys; sys.stdin.read()", encoding="utf-8")
    job.identity = None
    assert job.start_asr() is None
    assert job.child is not None
    assert job.child.proc.wait(timeout=30) is not None
    o = job.poll_asr()
    assert o is not None and o.kind == "failed"  # no manifest, but no hang


# ── #179: terminate -> bool (C11 / N2 / item c), lingering descendants (m1) ──


class TreeFake(FakeAsrChild):
    """A fake with a configurable `terminate_tree` result and a direct child
    that may survive the kill; `kill_tracked` reports lingering pids."""

    def __init__(self, argv: list[str], result=None, dies: bool = True) -> None:
        super().__init__(argv)
        self.result = result
        self.dies = dies
        self.lingering: list[int] = []

    def terminate_tree(self, timeout: float = 5.0):
        self.calls.append("terminate_tree")
        if self.dies and self.rc is None:
            self.rc = 1
        return self.result

    def kill_tracked(self) -> list[int]:
        self.calls.append("kill_tracked")
        out, self.lingering = self.lingering, []
        return out


def _spawn_one(job: SubsJob, **kw) -> TreeFake:
    made: list[TreeFake] = []

    def spawn(argv: list[str]) -> TreeFake:
        made.append(TreeFake(argv, **kw))
        return made[-1]

    assert job.start_asr(spawn) is None
    return made[0]


def test_terminate_without_a_child_is_true(tmp_path):
    job = _job(tmp_path)
    assert job.terminate(1.0) is True


@pytest.mark.parametrize("result,gone", [(None, True), (True, True), (False, False)])
def test_terminate_returns_tree_result_is_not_false(tmp_path, result, gone):
    # N2: a fake that returns None (the #177 signature) counts as gone.
    job = _job(tmp_path)
    c = _spawn_one(job, result=result)
    assert job.terminate(1.0) is gone
    assert c.calls == ["terminate_tree", "join_readers"]
    assert job.child is None  # the direct child exited: reset as before


def test_terminate_keeps_child_while_the_direct_child_still_runs(tmp_path):
    # (c): the live-child guards must keep blocking new launches, and the
    # normal poll path reaps it later.
    job = _job(tmp_path)
    c = _spawn_one(job, result=False, dies=False)
    assert job.terminate(1.0) is False
    assert job.child is c
    assert "join_readers" not in c.calls  # its readers cannot end yet
    c.finish(0)  # it finally exits
    o = job.poll_asr()
    assert o is not None and o.kind == "succeeded"
    assert job.child is None


def test_terminate_with_real_child_returns_true_and_resets(tmp_path):
    job = _job(tmp_path)
    job.exe = sys.executable
    job.engine = "custom"
    job.extra = ""
    job.media.write_text("import time; time.sleep(60)", encoding="utf-8")
    job.identity = None
    assert job.start_asr() is None
    assert job.child is not None
    assert job.terminate(5.0) is True
    assert job.child is None


def test_poll_asr_kills_lingering_tracked_processes_and_warns(tmp_path, caplog):
    job = _job(tmp_path)
    c = _spawn_one(job)
    c.lingering = [111, 222]
    c.finish(0)
    caplog.set_level("WARNING", logger="taskpaw.subs.job")
    o = job.poll_asr()
    assert o is not None and o.kind == "succeeded"
    assert "kill_tracked" in c.calls
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert "111" in msg and "222" in msg


def test_poll_asr_quiet_when_nothing_lingers(tmp_path, caplog):
    job = _job(tmp_path)
    c = _spawn_one(job)
    c.finish(0)
    caplog.set_level("WARNING", logger="taskpaw.subs.job")
    assert job.poll_asr() is not None
    assert "kill_tracked" in c.calls
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_poll_asr_with_a_fake_without_kill_tracked_still_works(tmp_path):
    # Older fakes (and the plugins' test doubles) have no `kill_tracked`.
    job = _job(tmp_path)
    sp = Spawner()
    job.start_asr(sp)
    sp.children[0].finish(0)
    o = job.poll_asr()
    assert o is not None and o.kind == "succeeded"


_BASE_PY = getattr(sys, "_base_executable", None) or sys.executable


def test_poll_asr_real_launcher_exits_leaving_a_tracked_grandchild(tmp_path):
    # m1 with real processes: the "exe" starts a grandchild, lives long enough
    # for a tracking poll, then exits; poll_asr must kill the orphan.
    psutil = pytest.importorskip("psutil")
    pid_file = tmp_path / "gpid.txt"
    job = _job(tmp_path)
    job.exe = _BASE_PY
    job.engine = "custom"
    job.extra = ""
    job.media.write_text(
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],\n"
        "    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,\n"
        "    stderr=subprocess.DEVNULL)\n"
        f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(1.5)\n",
        encoding="utf-8",
    )
    job.identity = None
    assert job.start_asr() is None
    gpid = None
    try:
        deadline = time.monotonic() + 30
        outcome = None
        while outcome is None and time.monotonic() < deadline:
            if gpid is None and pid_file.exists():
                text = pid_file.read_text().strip()
                gpid = int(text) if text else None
            outcome = job.poll_asr()
            time.sleep(0.05)
        assert outcome is not None and outcome.kind == "failed"  # no manifest
        assert gpid is not None
        gone_by = time.monotonic() + 5
        while psutil.pid_exists(gpid) and time.monotonic() < gone_by:
            try:
                if psutil.Process(gpid).status() == psutil.STATUS_ZOMBIE:
                    break
            except psutil.NoSuchProcess:
                break
            time.sleep(0.05)
        try:
            alive = psutil.Process(gpid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            alive = False
        assert not alive, "the orphaned grandchild survived poll_asr"
    finally:
        if gpid is not None:
            try:
                psutil.Process(gpid).kill()
            except psutil.Error:
                pass
        job.terminate(2.0)


# ── #189: live ASR progress (SubsJob.progress, C2) ────────────────────────
_P = "2026-09-25 10:00:00 - whisperjav - INFO - "
_QWEN5 = "\n".join(
    [f"{_P}[QwenPipeline PID 7] Phase {k}: x" for k in range(1, 6)]
    + [f"{_P}[DecoupledPipeline] Generating scene 3/4 (26.1s audio)..."]
)


class TailFake(FakeAsrChild):
    """An ASR child whose captured tail the test sets; records tail() calls."""

    def __init__(self, argv: list[str]) -> None:
        super().__init__(argv)
        self.text = ""
        self.tail_calls: list[tuple[int, int]] = []

    def tail(self, lines: int = 10, max_chars: int = 800) -> str:
        self.tail_calls.append((lines, max_chars))
        return self.text


class TailSpawner:
    def __init__(self) -> None:
        self.children: list[TailFake] = []

    def __call__(self, argv: list[str]) -> TailFake:
        self.children.append(TailFake(argv))
        return self.children[-1]


def _pin_clock(monkeypatch, t: float = 100.0) -> None:
    """Freeze `time.monotonic` as subs/job.py sees it (only its `time`), so
    `started_at + 30` is exactly 30 s later: on the real clock `(t0 + 30) - t0`
    can round to 29.99... and floor to 29 (#189 IR1)."""
    fake = SimpleNamespace(monotonic=lambda: t, time=time.time, sleep=time.sleep)
    monkeypatch.setattr("taskpaw_v3.monitors.subs.job.time", fake)


def test_progress_none_without_a_live_child(tmp_path):
    job = _job(tmp_path)
    assert job.progress(1.0) is None  # never started
    sp = TailSpawner()
    assert job.start_asr(sp) is None
    sp.children[0].finish(0)
    assert job.poll_asr() is not None  # reaped: child is None again
    assert job.progress(job.started_at + 5) is None


def test_progress_feeds_the_child_tail_on_every_poll(tmp_path, monkeypatch):
    assert (ASR_TAIL_LINES, ASR_TAIL_CHARS) == (40, 16000)
    _pin_clock(monkeypatch)
    job = _job(tmp_path)
    sp = TailSpawner()
    assert job.start_asr(sp) is None
    c = sp.children[0]
    t0 = job.started_at
    assert job.progress(t0 + 5) == {
        "phase": None,
        "phase_n": None,
        "scene": None,
        "scenes": None,
        "percent": None,
        "eta_s": None,
        "elapsed_s": 5,
    }
    c.text = _QWEN5
    s = job.progress(t0 + 30)
    assert s is not None
    assert (s["phase"], s["phase_n"], s["scene"], s["scenes"]) == (5, 8, 3, 4)
    assert (s["percent"], s["elapsed_s"]) == (54, 30)  # 0.2 + 0.75 × 0.9 × 2/4
    assert c.tail_calls == [(40, 16000), (40, 16000)]
    c.text = ""  # the tail rolled over: the parser keeps what it saw
    assert job.progress(t0 + 31)["percent"] == 54


def test_progress_new_attempt_gets_a_new_parser(tmp_path, monkeypatch):
    _pin_clock(monkeypatch)
    job = _job(tmp_path)
    sp = TailSpawner()
    assert job.start_asr(sp) is None
    sp.children[0].text = _QWEN5
    assert job.progress(job.started_at + 1)["phase"] == 5
    sp.children[0].finish(1, state="")
    assert job.poll_asr() is not None
    assert job.start_asr(sp) is None
    s = job.progress(job.started_at + 2)
    assert s is not None and (s["phase"], s["percent"], s["elapsed_s"]) == (
        None,
        None,
        2,
    )


def test_progress_none_when_the_child_has_no_tail_or_tail_raises(tmp_path):
    class NoTail:
        pid = 1

    class BadTail:
        pid = 2

        def tail(self, lines: int = 10, max_chars: int = 800) -> str:
            raise RuntimeError("boom")

    job = _job(tmp_path)
    assert job.start_asr(TailSpawner()) is None
    job.child = NoTail()  # type: ignore[assignment]
    assert job.progress(job.started_at + 1) is None
    job.child = BadTail()  # type: ignore[assignment]
    assert job.progress(job.started_at + 1) is None
