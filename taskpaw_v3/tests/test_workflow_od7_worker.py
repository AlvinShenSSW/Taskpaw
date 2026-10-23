"""#298 (P2-OD7) R9/R10: GPU workers build restore only; 设备 approves restore.

A worker builds only the restore executor (no WhisperJAV), proposes and
advertises restore only and sends no `asr_ready`; an older worker's beat with
`asr_ready` is still accepted and ignored. 核验 approves restore profiles
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


def test_r9_a_new_worker_beat_has_no_asr_ready_and_an_old_one_is_accepted():
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


def test_r9_the_worker_heartbeat_sends_no_asr_ready(tmp_path):
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

    body = host._beat(Staging())  # type: ignore[arg-type]
    assert "asr_ready" not in body
    Heartbeat.model_validate(body | {"gpu": {**body["gpu"], "name": "x"}})


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
