"""RestoreExecutor: Jasna LTX per main video with a fake Jasna child (T04-P1 E1, E8)."""

import json
import shutil
import subprocess
import threading
import time

import pytest

from taskpaw_v3.monitors.plugins import jasna
from taskpaw_v3.tests.workflow_exec_helpers import (
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
from taskpaw_v3.workflow.exec_common import MISSING_LTX
from taskpaw_v3.workflow.exec_restore import (
    RestoreExecutor,
    is_restored_name,
    parse_probe,
    progress_text,
)
from taskpaw_v3.workflow.executors import failed_kind
from taskpaw_v3.workflow.stage_results import StageResult, check_file, load_stage_result


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


def ledger(run):
    return StageResult.model_validate_json(
        (run.workdir / "stage-result.json").read_bytes()
    )


def single(rt, name="movie.mkv", data=None, **kw):
    return add_film(rt, [File(name, data or video(), **kw)])


def test_restores_each_video_and_records_verified_facts(rt, tools):
    film = single(rt)
    source = rt.work_dir / manifest(rt, film).readiness.workcopy.items[0].relative_path
    before = source.read_bytes()
    assert dispatcher(rt).run_once() is True
    assert stage(rt, film, "restore").status == "succeeded"
    result = load_stage_result(rt, film, "restore")
    (only,) = result.videos
    facts = only.restore
    attempt = stage(rt, film, "restore").attempt_id
    assert facts.decision == "applied"
    assert facts.file.path == f"{attempt}/movie-破解.mp4"
    assert (facts.width, facts.height, facts.duration_ms) == (1920, 1080, 120000)
    assert (facts.video_streams, facts.audio_streams) == (1, 1)
    check_file(rt.work_dir, facts.file)  # size and sha256 match the published file
    assert source.read_bytes() == before  # the work copy is never written
    workdir = rt.work_dir / attempt
    assert not list(workdir.glob("*.tmp.mp4"))
    (argv,) = tools.runs(tools.jasna_exe)
    assert argv[argv.index("--input") + 1] == str(source)
    assert argv[argv.index("--output") + 1] == str(workdir / "movie-破解.tmp.mp4")
    assert argv[argv.index("--restoration-model-name") + 1] == "ltx"
    assert "--ltx-fast" in argv  # unknown GPU keeps the preset's fast mode
    assert argv[argv.index("--working-directory") + 1] == str(workdir / ".jasna-work")
    assert argv[argv.index("--max-clip-size") + 1] == "90"  # 5060-8 standard preset


def test_cr_separated_ltx_progress_reaches_the_status(rt):
    film = single(rt)
    run = run_direct(rt, film, "restore")
    assert run.outcome.ok
    assert "修复 · 阶段 2/4 encode · 52% · 剩余 12:12" in run.progress
    assert "修复 · 阶段 1/4 scan · 100% · 剩余 12:30" in run.progress


def test_restore_reports_the_parsed_ltx_state_beside_the_text(rt):
    """H3 M3/D13: the state the Jasna monitor's LtxProgress reads, plus the volume."""
    film = add_film(
        rt,
        [
            File("cd1.mkv", video(), volume=1),
            File("cd2.mkv", video(), volume=2),
        ],
    )
    run = run_direct(rt, film, "restore")
    assert run.outcome.ok
    # the short text is unchanged (the list row and the other stages keep it)
    assert "修复 第 1/2 卷 · 阶段 2/4 encode · 52% · 剩余 12:12" in run.progress
    encode = [s for s in run.ltx if s["ltx_progress"]["stage_name"] == "encode"]
    first, second = encode
    assert (first["current_file"], second["current_file"]) == ("第 1/2 卷", "第 2/2 卷")
    assert first["ltx_stage"] == "2/4 encode"
    assert (first["percent"], first["fps"], first["eta"]) == (52, 5.62, "12:12")
    assert first["ltx_progress"] == {
        "mode": "restore",
        "stage_index": 2,
        "stage_total": 4,
        "stage_name": "encode",
        "raw_line": "LTX 2/4 encode:  52.00%|█████▏    | 156/300 "
        "[00:27<00:25,  5.620frame/s, run 12:12 left]",
        "percent": 52.0,
        "percent_text": "52.00",
        "current": "156",
        "total": "300",
        "elapsed": "00:27",
        "eta": "00:25",
        "rate_value": "5.620",
        "rate_unit": "frame/s",
        "run_eta": "12:12",
    }
    json.dumps(run.ltx)  # it travels through the status as JSON


def test_a_single_volume_names_its_video_in_the_ltx_state(rt):
    run = run_direct(rt, single(rt, "ABF-394.mp4"), "restore")
    assert run.ltx and {s["current_file"] for s in run.ltx} == {"ABF-394.mp4"}


def test_progress_text_shows_volume_stage_percent_and_eta():
    state = {"ltx_stage": "3/4 denoise", "percent": 7, "eta": "04:57"}
    assert progress_text(" 第 2/3 卷", state) == (
        "修复 第 2/3 卷 · 阶段 3/4 denoise · 7% · 剩余 04:57"
    )
    assert progress_text("", {}) == "修复"


def test_failed_ltx_needs_attention_and_is_never_relaunched(rt, tools):
    tools.set_jasna(mode="fail")
    film = single(rt)
    d = dispatcher(rt)
    assert d.run_once() is True
    assert d.run_once() is False
    job = stage(rt, film, "restore")
    assert job.status == "failed"
    (attempt,) = attempts(rt, film, "restore")
    assert attempt.evidence[0].kind == failed_kind("needs_attention")
    assert "退出码 3" in attempt.evidence[0].summary.note
    assert len(tools.runs(tools.jasna_exe)) == 1


@pytest.mark.parametrize("missing", ["exe", "models", "ffprobe"])
def test_missing_tool_or_models_launch_nothing(rt, tools, missing):
    if missing == "exe":
        tools.jasna_exe.unlink()
    elif missing == "models":
        shutil.rmtree(tools.jasna_dir / "model_weights")
    executor = (
        RestoreExecutor(rt, str(tools.jasna_exe), ffprobe=[], gpus=lambda: None)
        if missing == "ffprobe"
        else rt.executors["restore"]
    )
    film = single(rt)
    run = run_direct(rt, film, "restore", executor)
    assert run.outcome.error_class == "needs_attention"
    expected = {
        "exe": "找不到 Jasna",
        "models": "缺少 LTX 模型文件",
        "ffprobe": "ffprobe",
    }
    assert expected[missing] in run.outcome.message
    assert run.processes.children == []


def test_missing_models_are_cached_for_the_download_offer(rt, tools):
    """H3 D10: the host offers the download from the last preflight, rechecked on
    disk only — reading the offer never queries the GPU (nvidia-smi)."""
    queries = []

    def gpus():
        queries.append(1)
        return None

    executor = tools.restore(rt, gpus=gpus)
    assert executor.missing_models() is None
    weights = tools.jasna_dir / "model_weights" / "ltx-restore"
    shutil.rmtree(weights.parent)
    run = run_direct(rt, single(rt), "restore", executor)
    assert run.outcome.message.startswith(MISSING_LTX)
    asked = len(queries)
    missing = jasna.ltx_missing_files(None, False, True)
    assert executor.missing_models() == (weights, missing, "ltx fast auto-split 12%")
    weights.mkdir(parents=True)
    (weights / missing[0]).write_bytes(b"weights")
    assert executor.missing_models() == (
        weights,
        missing[1:],
        "ltx fast auto-split 12%",
    )
    for name in missing[1:]:
        (weights / name).write_bytes(b"weights")
    assert executor.missing_models() is None
    assert len(queries) == asked
    (weights / missing[0]).unlink()
    assert executor.missing_models() is None  # only a preflight makes a new offer
    run_direct(rt, single(rt, "b.mkv"), "restore", executor)
    assert executor.missing_models() == (
        weights,
        missing[:1],
        "ltx fast auto-split 12%",
    )
    (weights / missing[0]).write_bytes(b"weights")
    assert run_direct(rt, single(rt, "c.mkv"), "restore", executor).outcome.ok
    assert executor.missing_models() is None


def test_models_checked_for_the_variant_the_gpu_allows(rt, tools):
    # A pre-Blackwell card drops --ltx-fast, so the standard weights are required.
    weights = tools.jasna_dir / "model_weights" / "ltx-restore"
    from taskpaw_v3.monitors.plugins import jasna

    standard_only = set(jasna.ltx_missing_files(None, False, False)) - set(
        jasna.ltx_missing_files(None, False, True)
    )
    for name in standard_only:
        (weights / name).unlink()
    old = tools.restore(rt, gpus=lambda: [(8.9, 24564)])
    run = run_direct(rt, single(rt), "restore", old)
    assert run.outcome.error_class == "needs_attention"
    assert run.processes.children == []
    new = tools.restore(rt, gpus=lambda: [(12.0, 8151)])
    assert run_direct(rt, single(rt, "other.mkv"), "restore", new).outcome.ok


def test_exit_zero_without_output_needs_attention(rt, tools):
    tools.set_jasna(mode="no_output")
    run = run_direct(rt, single(rt), "restore")
    assert run.outcome.error_class == "needs_attention"
    assert "没有生成" in run.outcome.message


@pytest.mark.parametrize(
    "duration, ok", [(121.5, True), (118.1, True), (117.0, False), (100.0, False)]
)
def test_output_duration_must_match_the_input(rt, tools, duration, ok):
    # 120 s input: the slack is max(2 s, 2 % = 2.4 s).
    tools.set_jasna(duration=duration)
    run = run_direct(rt, single(rt), "restore")
    assert run.outcome.ok is ok
    if not ok:
        assert run.outcome.error_class == "needs_attention"
        assert "时长不符" in run.outcome.message


def test_long_input_uses_the_two_percent_slack(rt, tools):
    tools.set_jasna(duration=3540.0)  # 60 s short of an hour: 1.7 %
    hour = single(rt, data=video(duration=3600.0), duration_ms=3_600_000)
    assert run_direct(rt, hour, "restore").outcome.ok


def test_restored_name_is_reused_without_a_child_or_the_gpu(rt, tools):
    film = single(rt, "movie-破解.mp4")
    executor = rt.executors["restore"]
    assert executor.needs_gpu(manifest(rt, film)) is False
    d = dispatcher(rt)
    assert d.run_once() is True
    assert d.lease.holder() is None
    (only,) = load_stage_result(rt, film, "restore").videos
    assert only.restore.decision == "reused"
    copy = manifest(rt, film).readiness.workcopy.items[0]
    assert only.restore.file.path == copy.relative_path
    assert only.restore.file.sha256 == copy.sha256
    assert (only.restore.width, only.restore.duration_ms) == (1920, 120000)
    assert tools.runs(tools.jasna_exe) == []


def test_mixed_volumes_restore_in_order_and_reuse_the_restored_one(rt, tools):
    film = add_film(
        rt,
        [
            File("cd2.mkv", video(duration=60.0), volume=2, duration_ms=60_000),
            File("cd1_restored.mp4", video(), volume=1),
        ],
    )
    assert rt.executors["restore"].needs_gpu(manifest(rt, film)) is True
    run = run_direct(rt, film, "restore")
    assert run.outcome.ok and run.outcome.result_digest
    first, second = ledger(run).videos
    assert (first.volume_ordinal, first.restore.decision) == (1, "reused")
    assert first.restore.file.path.endswith("cd1_restored.mp4")
    assert (second.volume_ordinal, second.restore.decision) == (2, "applied")
    assert second.restore.file.path.endswith("cd2-破解.mp4")
    assert len(tools.runs(tools.jasna_exe)) == 1
    assert any("第 2/2 卷" in text for text in run.progress)


def test_is_restored_name_follows_the_plugin_suffixes():
    assert is_restored_name("ABC-123-破解.mp4")
    assert is_restored_name("abc_RESTORED.MP4")
    assert not is_restored_name("abc-破解.mkv")
    assert not is_restored_name("abc.mp4")


def test_stop_ends_jasna_and_reports_cancelled(rt, tools):
    tools.set_jasna(mode="sleep")
    film = single(rt)
    cancel = threading.Event()
    box = {}
    worker = threading.Thread(
        target=lambda: box.update(run=run_direct(rt, film, "restore", cancel=cancel))
    )
    worker.start()
    deadline = time.monotonic() + 20
    while not tools.runs(tools.jasna_exe):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    cancel.set()
    worker.join(30)
    assert not worker.is_alive()
    assert box["run"].outcome.error_class == "cancelled"
    assert box["run"].processes.active() == 0


def test_parse_probe_ignores_cover_art_and_reads_the_format_duration():
    text = """{"streams": [
      {"index": 0, "codec_type": "video", "width": 1280, "height": 720,
       "disposition": {"attached_pic": 0}},
      {"index": 1, "codec_type": "audio"}, {"index": 2, "codec_type": "audio"},
      {"index": 3, "codec_type": "video", "width": 600, "height": 600,
       "disposition": {"attached_pic": 1}}],
      "format": {"duration": "61.5"}}"""
    facts = parse_probe(text)
    assert (facts.width, facts.height, facts.duration_ms) == (1280, 720, 61500)
    assert (facts.video_streams, facts.audio_streams) == (1, 2)
    assert parse_probe("not json") is None


def test_tools_profile_is_stable_and_follows_the_settings(rt, tools):
    first = rt.executors["restore"].tools()
    assert (first.profile, first.profile_revision) == ("workflow-restore-ltx", 1)
    assert first.model == "ltx"
    same = tools.restore(rt).tools()
    assert same.config_digest == first.config_digest
    other_gpu = RestoreExecutor(rt, str(tools.jasna_exe), "5060ti-16").tools()
    other_exe = RestoreExecutor(rt, str(tools.root / "jasna2.exe")).tools()
    assert (
        len({first.config_digest, other_gpu.config_digest, other_exe.config_digest})
        == 3
    )


FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")


@pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="needs ffmpeg and ffprobe")
def test_real_ffprobe_reads_a_real_restored_mp4(rt, tools, tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        [
            FFMPEG,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=10:duration=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=2",
            "-shortest",
            "-pix_fmt",
            "yuv420p",
            str(clip),
        ],
        check=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
    )
    film = single(rt, data=clip.read_bytes(), duration_ms=2000)
    executor = tools.restore(rt, ffprobe=FFPROBE)
    run = run_direct(rt, film, "restore", executor)
    assert run.outcome.ok, run.outcome.message
    (only,) = ledger(run).videos
    facts = only.restore
    assert (facts.width, facts.height, facts.video_streams) == (320, 240, 1)
    assert facts.audio_streams == 1 and abs(facts.duration_ms - 2000) <= 200
