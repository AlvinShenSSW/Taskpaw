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
        # P2-1 C1: translate is the controller's own stage, no tool path needed
        assert [e.stage for e in build(rt)] == ["translate"]
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
        # #303: an LTX restore is 无码, and only the Chinese subtitle is packaged
        assert names == ["AAA-001-无码.mp4", "AAA-001-无码.zh.srt"]
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
        assert settings.ltx_gpu == "auto"
        restore, _translate = default_executors(settings, gpu=card)(rt)  # C1
        assert (restore.gpu_id, restore.preset_auto) == ("5070ti-16", True)
        assert restore.cfg.clip_size_1080p == 180
        same = default_executors(settings, gpu=card)(rt)[0].tools().config_digest
        assert restore.tools().config_digest == same  # stable across rebuilds
        assert len(asked) == 2  # decided at build, never again per job

        none, _ = default_executors(settings, gpu=lambda: None)(rt)
        assert (none.gpu_id, none.preset_auto) == ("5060-8", True)

        explicit = WorkflowSettings(
            jasna_exe_path=str(tools.jasna_exe), ltx_gpu="4090-24"
        )
        named, _ = default_executors(explicit, gpu=card)(rt)
        assert (named.gpu_id, named.preset_auto) == ("4090-24", False)
        assert len(asked) == 2  # an explicit setting never queries the card
    finally:
        rt.close()


def _boot(tmp_path, settings, card):
    """The host's boot order: executors built from the runtime, then enrolment."""
    from taskpaw_v3.workflow.local_runtime import LocalRuntime

    queries = []

    def gpu():
        queries.append(1)
        return card

    rt = LocalRuntime(tmp_path / "wf", [], gpu=gpu, machine=lambda: "synthetic")
    rt.executors = {e.stage: e for e in default_executors(settings)(rt)}
    rt.open()
    rt.ensure_worker()
    return rt, queries


def test_the_boot_queries_the_gpu_once_for_the_preset_and_the_capability(tmp_path):
    from taskpaw_v3.workflow.local_gpu import LocalGpu

    tools = Tools(tmp_path / "tools")
    card = LocalGpu("NVIDIA GeForce RTX 5070 Ti", "GPU-fake", 16303)
    settings = WorkflowSettings(jasna_exe_path=str(tools.jasna_exe))
    rt, queries = _boot(tmp_path, settings, card)
    try:
        assert len(queries) == 1
        assert rt.gpu == card
        assert rt.executors["restore"].gpu_id == "5070ti-16"
    finally:
        rt.close()


@pytest.mark.parametrize(
    ("ltx_gpu", "card", "tail"),
    [
        (
            "auto",
            ("NVIDIA GeForce RTX 5070 Ti", 16303),
            "5070ti-16 (RTX 5070 Ti · 16 GB) — auto from NVIDIA GeForce RTX 5070 Ti, 16 GB",
        ),
        (
            "4090-24",
            ("NVIDIA GeForce RTX 5060", 8151),
            "4090-24 (RTX 4090 · 24 GB) — configured",
        ),
        ("auto", None, "5060-8 (RTX 5060 · 8 GB) — no GPU found, safe default"),
    ],
)
def test_the_boot_logs_which_restore_preset_it_chose_and_why(
    tmp_path, caplog, ltx_gpu, card, tail
):
    import logging

    from taskpaw_v3.workflow.local_gpu import LocalGpu

    tools = Tools(tmp_path / "tools")
    gpu = LocalGpu(card[0], "GPU-fake", card[1]) if card else None
    settings = WorkflowSettings(jasna_exe_path=str(tools.jasna_exe), ltx_gpu=ltx_gpu)
    with caplog.at_level(logging.INFO, logger="taskpaw.workflow.pipeline"):
        rt, _ = _boot(tmp_path, settings, gpu)
    rt.close()
    lines = [
        r.getMessage()
        for r in caplog.records
        if "workflow restore preset" in r.getMessage()
    ]
    assert lines == [f"workflow restore preset: {tail}"]
