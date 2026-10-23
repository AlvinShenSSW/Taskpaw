"""#298 (P2-OD7) R11: 设置 · 媒体工作流 per role, and the controller without
WhisperJAV.

主控 always uses WhisperJAV (ASR runs there); its Jasna is used only while
允许 GPU 工作机接入 is off, so a stale Jasna path never blocks a remote-mode
save. A GPU 工作机 uses no WhisperJAV: its path is neither validated nor
dropped from agent.yaml. In remote mode a controller without an ASR executor
says why the ASR waits: 「本机未配置 WhisperJAV」."""

from __future__ import annotations

import pytest

from taskpaw_v3.tests.test_workflow_local_dispatch import (
    add_film,
    runtime,
    stage,
)
from taskpaw_v3.tests.test_workflow_local_dispatch import (
    dispatcher as local_dispatcher,
)
from taskpaw_v3.tests.test_workflow_settings import World, _tools
from taskpaw_v3.tests.test_workflow_settings import (
    no_ffprobe_on_path as no_ffprobe_on_path,  # noqa: F401
)
from taskpaw_v3.tests.workflow_route_helpers import (
    claim_remote,
    enrol_remote,
    resolve_remote,
    route,
)
from taskpaw_v3.workflow.local_dispatch import NO_ASR


@pytest.fixture
def tools(tmp_path):
    return _tools(tmp_path)


def test_a_worker_save_ignores_and_keeps_its_whisperjav_path(tmp_path, tools):
    stale = str(tmp_path / "gone" / "whisperjav.exe")
    world = World(tmp_path, enabled=True, role="worker", whisperjav_exe_path=stale)
    response = world.put(
        {
            "role": "worker",
            "staging_dir": str(tmp_path / "staging"),
            "jasna_exe_path": tools["jasna_exe_path"],
        }
    )
    assert response.status_code == 200, response.text
    assert world.saved()["workflow"]["whisperjav_exe_path"] == stale


def test_a_remote_controller_save_ignores_and_keeps_its_jasna_path(tmp_path, tools):
    stale = str(tmp_path / "gone" / "jasna.exe")
    world = World(tmp_path, enabled=True, **{**tools, "jasna_exe_path": stale})
    response = world.put({"role": "controller", "remote_workers": True})
    assert response.status_code == 200, response.text
    assert world.saved()["workflow"]["jasna_exe_path"] == stale
    single = world.put({"role": "controller", "remote_workers": False})
    assert single.status_code == 422
    assert "不存在" in single.json()["fields"]["jasna_exe_path"]


def test_the_controller_always_checks_its_whisperjav_path(tmp_path, tools):
    world = World(tmp_path)
    for remote in (True, False):
        response = world.put(
            {
                "role": "controller",
                **tools,
                "remote_workers": remote,
                "whisperjav_exe_path": str(tmp_path / "x.exe"),
            }
        )
        assert response.status_code == 422
        assert "不存在" in response.json()["fields"]["whisperjav_exe_path"]


def test_a_remote_controller_without_whisperjav_says_why_asr_waits(tmp_path):
    rt = runtime(tmp_path / "wf")
    try:
        w = enrol_remote(rt, "5060 Ti-1")
        film_id = add_film(rt)
        route(rt, film_id, w.worker_id)
        resolve_remote(rt, w, claim_remote(rt, w, film_id, "restore"))
        rt.executors.pop("asr")
        d = local_dispatcher(rt, remote_enabled=True)
        assert d.run_once() is False
        assert d.status().waiting == {film_id: NO_ASR}
        assert NO_ASR == "本机未配置 WhisperJAV"
        assert stage(rt, film_id, "asr").status == "pending"
    finally:
        rt.close()
