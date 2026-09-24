"""`SubsJob`: ASR attempts, outcome mapping, identity, publishing (#177, subs/job.py)."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

from taskpaw_v3.monitors.subs.job import JobOutcome, SubsJob, source_identity
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


def test_publish_ja_and_zh_via_generation_tmp_and_replace(tmp_path, monkeypatch):
    job = _job(tmp_path)
    cues = [Cue(5, 0, 1000, "はい"), Cue(9, 1000, 2000, "いいえ")]
    seen: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append((str(src), str(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    assert job.publish_ja(cues) is None
    assert job.publish_zh([Cue(1, 0, 1000, "是")]) is None
    assert seen == [
        (str(job.ja_target) + ".7.tmp", str(job.ja_target)),
        (str(job.zh_target) + ".7.tmp", str(job.zh_target)),
    ]
    assert parse(job.ja_target.read_text(encoding="utf-8"))[1].text == "いいえ"
    assert load(job.zh_target) == [Cue(1, 0, 1000, "是")]
    assert _no_tmp_left(job.ja_target.parent)


def test_publish_error_is_text_and_tmp_cleaned(tmp_path):
    job = _job(tmp_path)
    job.ja_target.mkdir()  # a directory in the way: os.replace fails
    err = job.publish_ja([Cue(1, 0, 1, "x")])
    assert err is not None and job.ja_target.name in err
    assert _no_tmp_left(job.ja_target.parent)


def test_publish_empty_writes_two_zero_byte_files(tmp_path):
    job = _job(tmp_path)
    assert job.publish_empty() is None
    assert job.ja_target.read_bytes() == b"" and job.zh_target.read_bytes() == b""
    assert _no_tmp_left(job.ja_target.parent)


def test_load_ja_round_trip(tmp_path):
    job = _job(tmp_path)
    cues = [Cue(1, 0, 1000, "はい\nそう"), Cue(2, 1500, 2500, "ね")]
    job.publish_ja(cues)
    assert job.load_ja() == cues


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
