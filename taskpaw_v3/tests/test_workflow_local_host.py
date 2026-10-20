"""The workflow is off by default and, once enabled, runs the loop end to end (T03-P1 C7)."""

import shutil
import subprocess
import sys
import time

import pytest

from taskpaw_v3.core.config import AgentConfig, WorkflowSettings
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow.executors import SyntheticExecutor
from taskpaw_v3.workflow.local_host import LocalWorkflowHost, start_local_workflow


def test_disabled_by_default_and_creates_nothing(tmp_path):
    config = AgentConfig(server_id="a", machine="m")
    assert config.workflow.enabled is False
    assert start_local_workflow(config.workflow, tmp_path) is None
    assert not (tmp_path / "workflow").exists()


def test_old_agent_yaml_without_workflow_still_loads():
    config = AgentConfig.model_validate({"server_id": "a", "machine": "m"})
    assert config.workflow == WorkflowSettings()


def _wait(predicate, seconds=60.0):
    deadline = time.monotonic() + seconds
    while not predicate():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.1)


def test_start_failure_is_reported_not_raised(tmp_path):
    host = start_local_workflow(
        WorkflowSettings(enabled=True, inbox_root=str(tmp_path / "missing")), tmp_path
    )
    try:
        _wait(lambda: host.state != "starting")
        assert host.state == "failed" and "收件目录不存在" in host.reason
    finally:
        host.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="Native intake")
def test_dropped_film_is_taken_in_and_processed_by_the_local_gpu(tmp_path):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe") or not shutil.which("nvidia-smi"):
        pytest.skip("media tools or NVIDIA GPU not available")
    inbox = tmp_path / "inbox"
    film = inbox / "Actor" / "AAA-001"
    film.mkdir(parents=True)
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
            "0.25",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(film / "AAA-001.mp4"),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )
    execs = [SyntheticExecutor(s) for s in ("restore", "asr", "translate")]
    host = LocalWorkflowHost(
        WorkflowSettings(enabled=True, inbox_root=str(inbox)),
        tmp_path / "data",
        execs,
        intake_policy=im.IntakePolicy(
            policy_revision=1, stable_seconds=1, rescan_seconds=1, min_free_bytes=0
        ),
    )
    host.start()
    try:
        _wait(lambda: host.state != "starting")
        assert host.state == "running", host.reason
        rt = host.runtime
        _wait(lambda: execs[0].runs, seconds=120)
        job_film = rt.store.get_stage(rt.operator, execs[0].runs[0]).film_id
        _wait(
            lambda: (
                {
                    s.stage: s.status
                    for s in rt.store.get_film(rt.operator, job_film).stages
                }["translate"]
                == "succeeded"
            )
        )
        stages = {
            s.stage: s.status for s in rt.store.get_film(rt.operator, job_film).stages
        }
        assert [stages[s] for s in ("intake", "restore", "asr", "translate")] == [
            "succeeded"
        ] * 4
        assert stages["return"] == "pending"  # T04 owns the next stage
    finally:
        host.stop()
    assert host.state == "stopped"
