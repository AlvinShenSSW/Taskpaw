"""Native end to end: INBOX drop → restore → ASR → translate → return package (T04 E9/E10)."""

import json
import shutil
import subprocess
import sys
import time

import pytest

from taskpaw_v3.core.config import WorkflowSettings
from taskpaw_v3.tests.workflow_exec_helpers import Tools, translate
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow.local_host import LocalWorkflowHost, start_local_workflow
from taskpaw_v3.workflow.pipeline import default_executors
from taskpaw_v3.workflow.return_step import MANIFEST_NAME, LocalReturnStep, package_dir

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Native intake")


def _wait(predicate, seconds=120.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.1)


def test_shipped_pipeline_registers_executors_only_for_configured_tools(tmp_path):
    from taskpaw_v3.tests.workflow_exec_helpers import runtime as make

    tools = Tools(tmp_path / "tools")
    rt = make(tmp_path / "wf", tools)
    try:
        build = default_executors(WorkflowSettings(enabled=True, inbox_root="x"))
        assert build(rt) == []
        only_asr = default_executors(
            WorkflowSettings(whisperjav_exe_path=str(tools.whisper_exe))
        )
        assert [e.stage for e in only_asr(rt)] == ["asr", "translate"]
        both = default_executors(
            WorkflowSettings(
                jasna_exe_path=str(tools.jasna_exe),
                whisperjav_exe_path=str(tools.whisper_exe),
            ),
            gpu=lambda: None,  # never the real nvidia-smi
        )
        assert [e.stage for e in both(rt)] == ["restore", "asr", "translate"]
    finally:
        rt.close()


def test_enabled_agent_config_starts_the_shipped_pipeline(tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(LocalWorkflowHost, "start", lambda self: started.append(self))
    settings = WorkflowSettings(enabled=True, inbox_root=str(tmp_path))
    host = start_local_workflow(settings, tmp_path)
    assert started == [host]
    assert isinstance(host.return_step, LocalReturnStep)
    assert host.executor_factory is not None


def confirm_first_plan(host) -> str:
    """U1: the taken-in film waits in 待处理 until its plan is confirmed."""
    rt = host.runtime
    _wait(lambda: rt.store.list_unplanned(rt.operator), seconds=120)
    (film,) = rt.store.list_unplanned(rt.operator)
    (result,) = host.confirm_plans(
        [
            {
                "film_id": film.film_id,
                "input_revision": film.input_revision,
                "ltx_model": "standard",
                "machine": rt.identity.worker_id,
            }
        ]
    )
    assert result["ok"], result
    return film.film_id


def test_dropped_film_becomes_a_verified_return_package(tmp_path):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe or not shutil.which("nvidia-smi"):
        pytest.skip("media tools or NVIDIA GPU not available")
    inbox = tmp_path / "inbox"
    film_dir = inbox / "Actor" / "AAA-001"
    film_dir.mkdir(parents=True)
    source = film_dir / "AAA-001.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=256x144:rate=24",
            "-t",
            "0.5",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    original = source.read_bytes()
    tools = Tools(tmp_path / "tools")

    def executors(rt):
        return [
            tools.restore(rt, ffprobe=ffprobe),
            tools.asr(rt),
            translate(rt),
        ]

    host = LocalWorkflowHost(
        WorkflowSettings(enabled=True, inbox_root=str(inbox)),
        tmp_path / "data",
        executor_factory=executors,
        return_step=LocalReturnStep(),
        intake_policy=im.IntakePolicy(
            policy_revision=1, stable_seconds=1, rescan_seconds=1, min_free_bytes=0
        ),
    )
    host.start()
    try:
        _wait(lambda: host.state != "starting")
        assert host.state == "running", host.reason
        rt = host.runtime

        def stages():
            jobs = rt.store.list_jobs(
                rt.operator, "succeeded", ("restore", "asr", "translate", "return")
            )
            return {(j.film_id, j.stage) for j in jobs}

        confirm_first_plan(host)
        _wait(lambda: len(stages()) == 4, seconds=180)
        (film,) = {film_id for film_id, _ in stages()}
        package = package_dir(rt, film, 1)
        handoff = json.loads((package / MANIFEST_NAME).read_text(encoding="utf-8"))
        names = sorted(i["relative_path"].rsplit("/", 1)[-1] for i in handoff["items"])
        assert names == [
            "AAA-001-破解.ja.srt",
            "AAA-001-破解.mp4",
            "AAA-001-破解.zh.srt",
        ]
        assert handoff["subtitles"]["status"] == "present"
        assert handoff["restoration"]["status"] == "applied"
        assert tools.runs(tools.jasna_exe) and tools.runs(tools.whisper_exe)
        assert source.read_bytes() == original  # the user's INBOX file is untouched
    finally:
        host.stop()


def test_unset_ltx_gpu_picks_the_preset_once_from_the_local_gpu(tmp_path):
    from taskpaw_v3.tests.workflow_exec_helpers import runtime as make
    from taskpaw_v3.workflow.local_gpu import LocalGpu

    tools = Tools(tmp_path / "tools")
    rt = make(tmp_path / "wf", tools)
    asked = []

    def card():
        asked.append(1)
        return LocalGpu("NVIDIA GeForce RTX 5070 Ti", "GPU-fake", 16303)

    try:
        settings = WorkflowSettings(jasna_exe_path=str(tools.jasna_exe))
        assert settings.ltx_gpu is None
        (restore,) = default_executors(settings, gpu=card)(rt)
        assert (restore.gpu_id, restore.preset_auto) == ("5070ti-16", True)
        assert restore.cfg.clip_size_1080p == 180
        same = default_executors(settings, gpu=card)(rt)[0].tools().config_digest
        assert restore.tools().config_digest == same  # stable across rebuilds
        assert len(asked) == 2  # decided at build, never again per job

        (none,) = default_executors(settings, gpu=lambda: None)(rt)
        assert (none.gpu_id, none.preset_auto) == ("5060-8", True)

        explicit = WorkflowSettings(
            jasna_exe_path=str(tools.jasna_exe), ltx_gpu="4090-24"
        )
        (named,) = default_executors(explicit, gpu=card)(rt)
        assert (named.gpu_id, named.preset_auto) == ("4090-24", False)
        assert len(asked) == 2  # an explicit setting never queries the card
    finally:
        rt.close()
