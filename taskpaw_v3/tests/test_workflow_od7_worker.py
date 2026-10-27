"""#298 (P2-OD7) R9/R10: GPU workers build restore only; 设备 approves restore.

A worker builds only the restore executor (no WhisperJAV), proposes and
advertises restore only and still sends `asr_ready: false` (an older controller
requires it); the new controller ignores the value. 核验 approves restore profiles
only, refuses a worker without one, and the 设备 rows carry no `asr_ready`.
In remote mode the controller row lists 识别、翻译 (识别 only with an ASR
executor)."""

from __future__ import annotations

import dataclasses
import inspect
from datetime import datetime, timedelta, timezone

import pytest

from taskpaw_v3.core.config import WorkflowSettings
from taskpaw_v3.tests.test_workflow_devices import BASE, _row, control
from taskpaw_v3.tests.test_workflow_devices import api as api  # noqa: F401
from taskpaw_v3.tests.test_workflow_devices import clock as clock  # noqa: F401
from taskpaw_v3.tests.test_workflow_devices import ctl as ctl  # noqa: F401
from taskpaw_v3.tests.test_workflow_devices import host as host  # noqa: F401
from taskpaw_v3.tests.test_workflow_devices import rt as rt  # noqa: F401
from taskpaw_v3.tests.worker_api_helpers import (
    VERSION,
    beat,
    enrol,
    join,
    probe,
    version,
)
from taskpaw_v3.workflow import devices, remote_dispatch, remote_worker
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.fleet_live import FleetLive, Heartbeat


# ── R9: the worker restores only ─────────────────────────────────────────
def test_r9_a_worker_builds_only_the_restore_executor(tmp_path):
    jasna = tmp_path / "jasna" / "jasna.exe"
    whisper = tmp_path / "whisperjav" / "whisperjav.exe"
    for exe in (jasna, whisper):
        exe.parent.mkdir(parents=True)
        exe.write_text("", encoding="utf-8")
    settings = WorkflowSettings(
        enabled=True,
        role="worker",
        jasna_exe_path=str(jasna),
        whisperjav_exe_path=str(whisper),  # an existing yaml value: ignored
    )
    from taskpaw_v3.tests.test_workflow_local_dispatch import GPU_16GB

    built = remote_worker.worker_executors(settings, None, GPU_16GB)  # type: ignore[arg-type]
    assert [e.stage for e in built] == ["restore"]


def test_r9_the_probe_and_the_dispatcher_know_no_asr_readiness():
    assert [f.name for f in dataclasses.fields(remote_worker.Probe)] == [
        "body",
        "tiers_ready",
    ]
    assert remote_dispatch.STAGES == ("restore",)
    assert (
        "asr_ready"
        not in inspect.signature(remote_dispatch.RemoteDispatcher.observe).parameters
    )


def test_r9_a_beat_without_asr_ready_and_one_with_it_are_both_accepted():
    start = datetime.now(timezone.utc)
    woken: list[int] = []
    fleet = FleetLive(start, VERSION, on_change=lambda: woken.append(1))
    new = beat("00000000-0000-4000-8000-000000000001")
    assert "asr_ready" not in new
    fleet.observe("w", Heartbeat.model_validate(new), start)
    old = Heartbeat.model_validate({**new, "asr_ready": True})  # an older build
    assert old.asr_ready is True
    fleet.observe("w", old, start + timedelta(seconds=5))
    fleet.observe(
        "w",
        Heartbeat.model_validate({**new, "asr_ready": False}),
        start + timedelta(seconds=10),
    )
    assert len(woken) == 1  # ignored: never a routing change
    assert Heartbeat.model_validate(new).asr_ready is False  # the default


def test_r9_the_worker_beat_is_accepted_by_the_pre_od7_controller(tmp_path):
    """IR P2-1: retain the old controller's ability to read the heartbeat.
    Its historical version gate is independent of the new compatibility policy.
    The pre-OD7 `Heartbeat` required `asr_ready` under
    `extra="forbid"`; the new beat keeps sending `false`, the new controller
    ignores it."""
    host = remote_worker.WorkerHost(
        WorkflowSettings(enabled=True, role="worker"),
        tmp_path,
        gpu_live=lambda: {"hold": "none"},
        staging_free=lambda _p: 10**12,
    )

    class Staging:
        cap = 500 * 10**9

        def free(self):
            return 10**12

        def used(self):
            return 0

    class PreOd7Heartbeat(Heartbeat):  # asr_ready required, extra=forbid
        asr_ready: bool

    body = host._beat(Staging())  # type: ignore[arg-type]
    assert body["asr_ready"] is False
    body = body | {"gpu": {**body["gpu"], "name": "x"}}
    PreOd7Heartbeat.model_validate(body)
    without = {k: v for k, v in body.items() if k != "asr_ready"}
    with pytest.raises(ValueError):
        PreOd7Heartbeat.model_validate(without)  # why it must be sent
    # the new controller ignores the value: never a routing change
    start = datetime.now(timezone.utc)
    woken: list[int] = []
    fleet = FleetLive(start, VERSION, on_change=lambda: woken.append(1))
    fleet.observe("w", Heartbeat.model_validate(body), start)
    fleet.observe(
        "w",
        Heartbeat.model_validate(body | {"asr_ready": True}),
        start + timedelta(seconds=5),
    )
    assert len(woken) == 1


# ── R10: 设备 approves restore only ──────────────────────────────────────
def test_r10_approval_takes_the_restore_profile_only(api, rt):  # noqa: F811
    joined = join(api, rt)
    api.post(
        "/worker/v1/capabilities", json=probe(joined.worker_id), headers=joined.headers
    )
    assert "asr" not in inspect.signature(devices.approve).parameters
    devices.approve(rt, joined.worker_id, version(rt, joined.worker_id), restore=True)
    worker = rt.store.get_worker(rt.operator, joined.worker_id).worker
    assert [p.allowed_stages for p in worker.capability.approved.profiles] == [
        ("restore",)
    ]


def test_r10_a_worker_without_a_restore_profile_cannot_be_approved(api, rt):  # noqa: F811
    small = join(api, rt, "5060")
    api.post(
        "/worker/v1/capabilities",
        json=probe(small.worker_id, vram=8151),
        headers=small.headers,
    )
    for restore in (True, False):
        with pytest.raises(m.WorkflowError) as refused:
            devices.approve(
                rt, small.worker_id, version(rt, small.worker_id), restore=restore
            )
        assert refused.value.code == "precondition_failed"
    from taskpaw_v3.tests.worker_api_helpers import profiles

    asr_only = join(api, rt, "asr-only")
    api.post(
        "/worker/v1/capabilities",
        json=probe(asr_only.worker_id, profiles=profiles(("asr",))),
        headers=asr_only.headers,
    )
    with pytest.raises(m.WorkflowError) as refused:
        devices.approve(
            rt, asr_only.worker_id, version(rt, asr_only.worker_id), restore=True
        )
    assert refused.value.code == "precondition_failed"


def test_r10_the_approve_body_has_no_asr(ctl, api, rt):  # noqa: F811
    joined = join(api, rt)
    api.post(
        "/worker/v1/capabilities", json=probe(joined.worker_id), headers=joined.headers
    )
    row = _row(ctl, joined.worker_id)
    old = ctl.post(
        f"{BASE}/{joined.worker_id}/approve",
        json={"expected_version": row["version"], "restore": True, "asr": True},
    )
    assert old.status_code == 422
    ok = ctl.post(
        f"{BASE}/{joined.worker_id}/approve",
        json={"expected_version": row["version"], "restore": True},
    )
    assert ok.status_code == 200
    row = _row(ctl, joined.worker_id)
    assert "asr_ready" not in row
    assert {p["stage"] for p in row["approved"]["profiles"]} == {"restore"}


def test_r10_an_8gb_row_is_eligible_for_nothing(ctl, api, rt, clock):  # noqa: F811
    small = join(api, rt, "5060")
    api.post(
        "/worker/v1/capabilities",
        json=probe(small.worker_id, vram=8151),
        headers=small.headers,
    )
    row = _row(ctl, small.worker_id)
    assert row["restore_allowed"] is False
    assert row["eligible"] is False


def test_r10_the_controller_row_lists_asr_and_translate_in_remote_mode(host, rt):  # noqa: F811
    assert control(host).get(BASE).json()["controller"]["stages"] == ["识别", "翻译"]
    rt.executors.pop("asr")
    assert control(host).get(BASE).json()["controller"]["stages"] == ["翻译"]
    off = control(host, remote=False).get(BASE).json()["controller"]["stages"]
    assert off == ["修复", "翻译"]


def test_cx35_eligibility_is_any_tier_ready_not_the_standard_tier(ctl, api, rt):  # noqa: F811
    """A worker with only the good/high models is eligible; with no tier
    model it reads 缺少修复模型 (the matrix keeps the per-tier readiness)."""
    joined, boot = enrol(api, rt)

    def row_after(tiers: list[str]):
        api.post(
            "/worker/v1/heartbeat",
            json=beat(boot, tiers_ready=tiers),
            headers=joined.headers,
        )
        return _row(ctl, joined.worker_id)

    only_high = row_after(["good", "high"])
    assert only_high["tiers_ready"] == ["good", "high"]
    assert only_high["eligible"] is True and only_high["eligibility"] == "可接单"
    none = row_after([])
    assert none["tiers_ready"] == []
    assert none["eligible"] is False and none["eligibility"] == "缺少修复模型"


def test_m3_a_previously_approved_card_that_now_probes_8gb_reads_no_restore(
    ctl, api, rt
):  # noqa: F811
    """Approve refuses < 16 GB, so 待核验 would never end: the row says why."""
    joined, _boot = enrol(api, rt)
    api.post(
        "/worker/v1/capabilities",
        json=probe(joined.worker_id, vram=8151),
        headers=joined.headers,
    )
    row = _row(ctl, joined.worker_id)
    assert row["enrollment_state"] == "pending_validation"
    assert row["restore_allowed"] is False and row["eligible"] is False
    assert row["eligibility"] == "显存不足，不参与修复"


# ── a worker with no ffprobe setting finds Jasna's own copy ──────────────
def _jasna_with_tools_ffprobe(tmp_path):
    jasna = tmp_path / "jasna" / "jasna.exe"
    probe = tmp_path / "jasna" / "tools" / "ffprobe.exe"
    probe.parent.mkdir(parents=True)
    jasna.write_text("", encoding="utf-8")
    probe.write_text("", encoding="utf-8")
    return jasna, probe


def test_a_worker_without_ffprobe_setting_uses_jasnas_tools_copy(tmp_path, monkeypatch):
    import shutil

    from taskpaw_v3.tests.test_workflow_local_dispatch import GPU_16GB

    jasna, probe = _jasna_with_tools_ffprobe(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    settings = WorkflowSettings(enabled=True, role="worker", jasna_exe_path=str(jasna))
    (restore,) = remote_worker.worker_executors(settings, None, GPU_16GB)  # type: ignore[arg-type]
    assert restore.ffprobe == [str(probe)]  # type: ignore[attr-defined]


def test_a_saved_ffprobe_setting_still_wins_on_a_worker(tmp_path, monkeypatch):
    import shutil

    from taskpaw_v3.tests.test_workflow_local_dispatch import GPU_16GB

    jasna, _probe = _jasna_with_tools_ffprobe(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    settings = WorkflowSettings(
        enabled=True,
        role="worker",
        jasna_exe_path=str(jasna),
        ffprobe_path=str(tmp_path / "mine" / "ffprobe.exe"),
    )
    (restore,) = remote_worker.worker_executors(settings, None, GPU_16GB)  # type: ignore[arg-type]
    assert restore.ffprobe == [str(tmp_path / "mine" / "ffprobe.exe")]  # type: ignore[attr-defined]
