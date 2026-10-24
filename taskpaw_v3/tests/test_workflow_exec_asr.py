"""AsrExecutor: subtitle decision matrix and a fake WhisperJAV child (T04-P1 E2)."""

import errno
import threading
import time
from pathlib import Path

import pytest

from taskpaw_v3.monitors.subs import srt
from taskpaw_v3.tests.workflow_exec_helpers import (
    JA_SRT,
    ZH_SRT,
    File,
    Tools,
    add_film,
    attempts,
    dispatcher,
    manifest,
    run_direct,
    runtime,
    stage,
    video,
)
from taskpaw_v3.workflow import exec_common
from taskpaw_v3.workflow.exec_asr import AsrExecutor
from taskpaw_v3.workflow.executors import failed_kind
from taskpaw_v3.workflow.stage_results import check_file, load_stage_result

# A restored name: the restore stage reuses it at once, without a child.
MOVIE = "movie-破解.mp4"


@pytest.fixture
def tools(tmp_path):
    return Tools(tmp_path / "tools")


@pytest.fixture
def rt(tmp_path, tools):
    value = runtime(tmp_path / "wf", tools)
    try:
        yield value
    finally:
        value.close()


def film_with(rt, *subs, name=MOVIE):
    """A film with one main video and `subs` = (path, text-or-bytes, language)."""
    files = [File(name, video())]
    for path, data, language in subs:
        raw = data.encode("utf-8") if isinstance(data, str) else data
        files.append(File(path, raw, "source_subtitle", language, name))
    film = add_film(rt, files)
    assert dispatcher(rt).run_once() is True  # restore
    assert stage(rt, film, "restore").status == "succeeded"
    return film


def run_asr(rt, film):
    d = dispatcher(rt)
    assert d.run_once() is True
    return stage(rt, film, "asr")


def subtitle(rt, film):
    (only,) = load_stage_result(rt, film, "asr").videos
    assert only.restore is None
    return only.subtitle


def test_existing_chinese_srt_is_reused_without_whisperjav(rt, tools):
    film = film_with(rt, ("movie.zh.srt", ZH_SRT, "zh"))
    assert rt.executors["asr"].needs_gpu(manifest(rt, film)) is False
    assert run_asr(rt, film).status == "succeeded"
    facts = subtitle(rt, film)
    assert facts.decision == "reused_zh" and facts.ja is None
    copy = next(
        c
        for c in manifest(rt, film).readiness.workcopy.items
        if c.relative_path.endswith("movie.zh.srt")
    )
    assert facts.zh.path == copy.relative_path and facts.zh.sha256 == copy.sha256
    assert tools.runs(tools.whisper_exe) == []


@pytest.mark.parametrize(
    "data",
    [
        ZH_SRT.encode("gb18030"),
        ZH_SRT.encode("utf-16"),
        "".join(  # bilingual: a Chinese line and a Japanese line per cue
            f"{i}\n00:00:0{i},000 --> 00:00:0{i},900\n这是中文字幕\nこれは日本語です\n\n"
            for i in (1, 2, 3, 4)
        ),
    ],
    ids=["gb18030", "utf16", "bilingual"],
)
def test_chinese_in_other_encodings_and_bilingual_is_reused(rt, tools, data):
    film = film_with(rt, ("movie.chs.srt", data, "chs"))
    assert run_asr(rt, film).status == "succeeded"
    assert subtitle(rt, film).decision == "reused_zh"
    assert tools.runs(tools.whisper_exe) == []


def test_existing_japanese_srt_is_the_translation_source(rt, tools):
    film = film_with(rt, ("movie.ja.srt", JA_SRT, "ja"))
    assert rt.executors["asr"].needs_gpu(manifest(rt, film)) is False
    assert run_asr(rt, film).status == "succeeded"
    facts = subtitle(rt, film)
    assert facts.decision == "source_ja" and facts.zh is None
    assert facts.ja.path.endswith("movie.ja.srt")
    assert tools.runs(tools.whisper_exe) == []


@pytest.mark.parametrize(
    "subs",
    [
        (),
        (("movie.en.srt", "1\n00:00:01,000 --> 00:00:02,000\nHello there\n\n", "en"),),
        (("movie.zh.ass", ZH_SRT, "zh"),),  # not SRT: kept as an attachment only
        (("movie.ja.srt", ZH_SRT, "ja"),),  # tagged Japanese, content is not
    ],
    ids=["none", "english", "ass", "ja-tag-not-ja"],
)
def test_otherwise_whisperjav_generates_japanese(rt, tools, subs):
    film = film_with(rt, *subs)
    assert rt.executors["asr"].needs_gpu(manifest(rt, film)) is True
    job = run_asr(rt, film)
    assert job.status == "succeeded"
    facts = subtitle(rt, film)
    assert facts.decision == "generated_ja" and facts.zh is None
    assert facts.ja.path == f"{job.attempt_id}/movie-破解.ja.srt"
    path = check_file(rt.work_dir, facts.ja)
    cues = srt.load(path)
    assert [c.text for c in cues][0] == "こんにちは、元気ですか"
    (argv,) = tools.runs(tools.whisper_exe)
    assert argv[argv.index("--language") + 1] == "japanese"
    source = rt.work_dir / next(
        c.relative_path
        for c in manifest(rt, film).readiness.workcopy.items
        if c.relative_path.endswith(MOVIE)
    )
    assert argv[1] == str(source)
    assert argv[argv.index("--output-dir") + 1].startswith(
        str(rt.work_dir / job.attempt_id)
    )


@pytest.mark.parametrize("mode", ["empty", "no_cues"])
def test_no_speech_writes_no_subtitle_file(rt, tools, mode):
    tools.set_whisper(mode=mode)
    film = film_with(rt)
    job = run_asr(rt, film)
    assert job.status == "succeeded"
    facts = subtitle(rt, film)
    assert (facts.decision, facts.ja, facts.zh) == ("no_speech", None, None)
    assert not list((rt.work_dir / job.attempt_id).glob("*.srt"))


@pytest.mark.parametrize(
    "data, reason",
    [
        (JA_SRT, "不是中文"),
        ("1\n00:00:01,000 --> 00:00:02,000\n你好\n\n", "太少"),
        (b"\xff\xfe\x00\xd8garbage\x81", "解码"),
        ("not an srt", "SRT"),
    ],
    ids=["japanese", "too-short", "undecodable", "not-srt"],
)
def test_chinese_tag_that_is_not_chinese_needs_attention(rt, tools, data, reason):
    film = film_with(rt, ("movie.zh.srt", data, "zh"))
    assert rt.executors["asr"].needs_gpu(manifest(rt, film)) is False
    job = run_asr(rt, film)
    assert job.status == "failed"
    (attempt,) = attempts(rt, film, "asr")
    assert attempt.evidence[0].kind == failed_kind("needs_attention")
    assert reason in attempt.evidence[0].summary.note
    assert "movie.zh.srt" in attempt.evidence[0].summary.note
    assert tools.runs(tools.whisper_exe) == []


def test_a_reusable_chinese_srt_wins_over_a_mislabelled_one(rt, tools):
    film = film_with(
        rt, ("movie.a.srt", JA_SRT, "zh"), ("movie.b.srt", ZH_SRT, "zh-Hans")
    )
    assert run_asr(rt, film).status == "succeeded"
    assert subtitle(rt, film).zh.path.endswith("movie.b.srt")


def test_whisperjav_failure_is_transient(rt, tools):
    tools.set_whisper(mode="crash")
    film = film_with(rt)
    run_asr(rt, film)
    first = attempts(rt, film, "asr")[0]
    assert first.evidence[0].kind == failed_kind("transient")
    assert "WhisperJAV" in first.evidence[0].summary.note


def _held(*args, **kwargs):
    # A sharing violation on open: errno 13, no winerror (H1-2).
    raise PermissionError(errno.EACCES, "Permission denied")


def test_a_held_work_copy_is_transient(rt, tools, monkeypatch):
    film = film_with(rt, ("movie.zh.srt", ZH_SRT, "zh"))
    monkeypatch.setattr(exec_common, "file_ref", _held)
    run_asr(rt, film)
    first = attempts(rt, film, "asr")[0]
    assert first.evidence[0].kind == failed_kind("transient")


def test_a_held_subtitle_is_transient(rt, tools, monkeypatch):
    film = film_with(rt, ("movie.zh.srt", ZH_SRT, "zh"))
    read_bytes = Path.read_bytes

    def read(path):
        return _held() if path.suffix == ".srt" else read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    run_asr(rt, film)
    first = attempts(rt, film, "asr")[0]
    assert first.evidence[0].kind == failed_kind("transient")
    assert "movie.zh.srt" in first.evidence[0].summary.note


def test_missing_whisperjav_launches_nothing(rt, tools):
    film = film_with(rt)
    executor = AsrExecutor(rt, str(tools.root / "missing.exe"))
    run = run_direct(rt, film, "asr", executor)
    assert run.outcome.error_class == "needs_attention"
    assert "WhisperJAV" in run.outcome.message
    assert run.processes.children == []


def test_each_volume_decides_its_own_source(rt, tools):
    files = [
        File("cd1-破解.mp4", video(), volume=1),
        File("cd2-破解.mp4", video(), volume=2),
        File("cd1.zh.srt", ZH_SRT.encode(), "source_subtitle", "zh", "cd1-破解.mp4"),
    ]
    film = add_film(rt, files)
    assert dispatcher(rt).run_once() is True
    job = run_asr(rt, film)
    first, second = load_stage_result(rt, film, "asr").videos
    assert (first.volume_ordinal, first.subtitle.decision) == (1, "reused_zh")
    assert (second.volume_ordinal, second.subtitle.decision) == (2, "generated_ja")
    assert second.subtitle.ja.path == f"{job.attempt_id}/cd2-破解.ja.srt"
    assert len(tools.runs(tools.whisper_exe)) == 1


def test_generated_subtitle_follows_the_restored_name(rt, tools):
    film = add_film(rt, [File("movie.mkv", video())])
    assert dispatcher(rt).run_once() is True  # fake Jasna restores
    job = run_asr(rt, film)
    assert subtitle(rt, film).ja.path == f"{job.attempt_id}/movie-无码.ja.srt"


def test_stop_ends_whisperjav_and_reports_cancelled(rt, tools):
    tools.set_whisper(mode="sleep")
    film = film_with(rt)
    cancel = threading.Event()
    box = {}
    worker = threading.Thread(
        target=lambda: box.update(run=run_direct(rt, film, "asr", cancel=cancel))
    )
    worker.start()
    deadline = time.monotonic() + 20
    while not tools.runs(tools.whisper_exe):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    cancel.set()
    worker.join(30)
    assert not worker.is_alive()
    assert box["run"].outcome.error_class == "cancelled"


def test_tools_profile_is_stable_and_follows_the_settings(rt, tools):
    first = rt.executors["asr"].tools()
    assert (first.profile, first.profile_revision) == ("workflow-asr", 1)
    assert tools.asr(rt).tools().config_digest == first.config_digest
    other = AsrExecutor(rt, str(tools.whisper_exe), engine="large-v3").tools()
    assert other.config_digest != first.config_digest


# #300: replay captured output; no WhisperJAV process or GPU.
def fake_asr_lines(monkeypatch, lines, interval=2.0, after_lines=lambda: 0):
    from types import SimpleNamespace

    from taskpaw_v3.workflow import exec_asr
    from taskpaw_v3.workflow.owned_process import OwnedJob

    clock = [0.0]
    monkeypatch.setattr(exec_asr, "monotonic", lambda: clock[0], raising=False)
    monkeypatch.setattr(
        exec_asr.whisperjav,
        "read_outcome",
        lambda *args: SimpleNamespace(kind="no_speech"),
    )

    def launch(self, argv, *, on_line, **kwargs):
        def wait(cancel):
            for i, line in enumerate(lines):
                clock[0] += interval
                on_line("stdout" if i % 2 else "stderr", line)
            return after_lines()

        return SimpleNamespace(wait=wait)

    monkeypatch.setattr(OwnedJob, "launch", launch)


def test_asr_live_progress_replays_recorded_run(rt, monkeypatch):
    from taskpaw_v3.monitors.subs.progress import AsrProgress
    from taskpaw_v3.tests.test_subs_progress import RECORDED_RUN

    film = film_with(rt)
    lines = RECORDED_RUN.splitlines()
    fake_asr_lines(monkeypatch, lines)
    run = run_direct(rt, film, "asr")
    assert run.outcome.ok
    assert run.ltx, "ASR must publish structured progress through the callback"
    assert len(run.ltx) == len(lines) + 1
    parser = AsrProgress(0)
    for i, line in enumerate(lines, 1):
        parser.feed_text(line, i * 2.0)
        expected = parser.snapshot(i * 2.0)
        actual = run.ltx[i]
        assert actual["console"] == line.strip()[:300]
        if expected["phase"] is not None:
            assert {key: actual[key] for key in expected} == expected
            assert (
                actual["phase_label"]
                == actual["phases"][expected["phase"] - 1]["label"]
            )
    gen = next(s for s in run.ltx if "Generating scene 1/1" in s["console"])
    assert [p["label"] for p in gen["phases"]] == [
        "提取音频",
        "划分场景",
        "语音增强",
        "语音分段",
        "识别语音",
        "生成场景字幕",
        "合并字幕",
        "过滤与清理",
    ]
    assert [p["state"] for p in gen["phases"]] == ["done"] * 4 + ["current"] + [
        "pending"
    ] * 3
    starts = [
        next(i * 2 for i, line in enumerate(lines, 1) if f"Phase {n}:" in line)
        for n in range(1, 9)
    ]
    assert gen["phases"][0]["seconds"] == starts[1] - starts[0]
    assert gen["phases"][4]["seconds"] == gen["elapsed_s"] - starts[4]
    assert gen["phases"][5]["seconds"] is None
    assert run.ltx[-1]["percent"] == 99


def test_asr_live_progress_throttles_but_keeps_every_line(rt, monkeypatch):
    from taskpaw_v3.tests.test_subs_progress import RECORDED_RUN

    film = film_with(rt)
    lines = RECORDED_RUN.splitlines()
    fake_asr_lines(monkeypatch, lines, interval=0.001)
    run = run_direct(rt, film, "asr")
    assert 1 < len(run.ltx) <= 3
    assert run.ltx[-1]["percent"] == 99
    assert run.ltx[-1]["scene"] == 1
    assert run.ltx[-1]["console"] == lines[-1]


def test_asr_live_progress_untagged_has_only_elapsed_and_console(rt, monkeypatch):
    film = film_with(rt)
    fake_asr_lines(monkeypatch, ["Loading balanced pipeline", "Scene 1/1 (100%)"])
    run = run_direct(rt, film, "asr")
    assert run.outcome.ok
    assert run.ltx[-1] == {"elapsed_s": 4, "console": "Scene 1/1 (100%)"}
    assert all(set(s) == {"elapsed_s", "console"} for s in run.ltx)


def test_asr_live_progress_eta_uses_scene_advances(rt, monkeypatch):
    film = film_with(rt)
    fake_asr_lines(
        monkeypatch,
        [
            "[DecoupledPipeline] Generating scene 1/100",
            "[DecoupledPipeline] Generating scene 2/100",
            "[DecoupledPipeline] Generating scene 3/100",
        ],
        interval=20,
    )
    run = run_direct(rt, film, "asr")
    assert run.ltx[-1]["eta_s"] == 2020


@pytest.mark.parametrize("exit_code", [0, None])
def test_asr_live_progress_flushes_while_quiet_and_stops_with_child(
    rt, monkeypatch, exit_code
):
    from taskpaw_v3.workflow import board, exec_asr

    film = film_with(rt)
    d = dispatcher(rt)
    observed = []

    def while_quiet():
        deadline = time.monotonic() + 3
        while d.status().current["ltx"]["console"] != "last note":
            assert time.monotonic() < deadline, (
                "the quiet child's last line was not flushed"
            )
            time.sleep(0.01)
        current = d.status().current
        value = rt.store.film_board(rt.operator)[0]
        row = board.film_row(value, {}, current=current)
        step = next(s for s in row["steps"] if s["key"] == "asr")
        assert step["progress"]["console"] == step["console"] == "last note"
        assert step["started_at"] == current["started_at"]
        observed.append(step)
        return exit_code

    fake_asr_lines(monkeypatch, ["first note", "last note"], after_lines=while_quiet)
    monkeypatch.setattr(exec_asr, "monotonic", time.monotonic)
    monkeypatch.setattr(exec_asr, "CONSOLE_INTERVAL_S", 0.05)
    assert d.run_once()
    assert stage(rt, film, "asr").status == (
        "succeeded" if exit_code == 0 else "pending"
    )
    assert observed
    if exit_code is None:
        assert attempts(rt, film, "asr")[0].evidence[0].kind == failed_kind("cancelled")
    assert not any(t.name == "asr-console-flush" for t in threading.enumerate())
