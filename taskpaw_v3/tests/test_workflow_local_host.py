"""The workflow is off by default and, once enabled, runs the loop end to end (T03-P1 C7)."""

import json
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import PurePosixPath
from types import SimpleNamespace
from uuid import uuid4

import pytest

from taskpaw_v3.core.config import AgentConfig, WorkflowSettings
from taskpaw_v3.workflow import intake_discovery as discovery
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow import local_host
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.executors import SyntheticExecutor
from taskpaw_v3.workflow.local_host import (
    DOWNLOAD_EXTRAS,
    LocalWorkflowHost,
    start_local_workflow,
)
from taskpaw_v3.workflow.local_runtime import LocalRuntime
from taskpaw_v3.workflow.manifest import (
    _IMAGE,
    group_keys,
    group_package,
    observation_digest,
)

# The two real-machine shapes (H2 finding, 2026-10-21): an unscraped download and
# a Miko-scraped film folder with the download's own cover left beside it.
RAW = ("4K688.com@ABF-393.mp4", "ABF-393.jpg", "新建文本文档.txt")
SCRAPED = (
    "ABF-393.mp4",
    "ABF-393.nfo",
    "ABF-393-poster.jpg",
    "ABF-393-thumb.jpg",
    "ABF-393-fanart.jpg",
    "ABF-393.jpg",
)
FAST = dict(policy_revision=1, stable_seconds=1, rescan_seconds=1, min_free_bytes=0)


def test_disabled_by_default_and_creates_nothing(tmp_path):
    config = AgentConfig(server_id="a", machine="m")
    assert config.workflow.enabled is False
    assert start_local_workflow(config.workflow, tmp_path) is None
    assert not (tmp_path / "workflow").exists()


def test_enabled_without_an_inbox_does_not_start(tmp_path):
    assert start_local_workflow(WorkflowSettings(enabled=True), tmp_path) is None
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


# ── raw downloads (H2 R1, R3) ─────────────────────────────────────────────
def _members(paths):
    root = str(uuid4())
    return tuple(
        im.ObservedMember(
            member_id=str(uuid4()),
            relative_path=path,
            entry_kind="file",
            native_identity=m.NativeIdentity(
                scheme="windows_file_id",
                root_id=root,
                volume_or_export_id="v",
                object_id=f"object-{n}",
                observed_at=datetime.now(timezone.utc),
                evidence_id=str(uuid4()),
            ),
            size_bytes=1,
            mtime_ns=1,
        )
        for n, path in enumerate(paths)
    )


def _grouped(members, parent, extensions):
    return group_package(
        members,
        film_ids=tuple(
            im.FilmIdentity(group_key=key, film_id=str(uuid4()))
            for key in group_keys(members)
        ),
        parent_context=parent,
        grouping_revision=1,
        allowed_attachment_extensions=extensions,
    )


def test_every_plain_image_is_a_download_extra():
    """Grouping makes any unnamed image an attachment, so the film must allow it."""
    assert _IMAGE <= set(DOWNLOAD_EXTRAS)
    assert local_host.film_policy(DOWNLOAD_EXTRAS).allowed_attachment_extensions == (
        DOWNLOAD_EXTRAS
    )
    # the store/manifest model defaults stay empty
    assert im.IntakePolicy(policy_revision=1).allowed_attachment_extensions == ()


@pytest.mark.parametrize(
    ("paths", "parent"), [(RAW, None), (SCRAPED, "河合あすな")], ids=["raw", "scraped"]
)
def test_real_download_packages_group_ready_with_the_extras(paths, parent):
    members = _members(paths)
    grouping = _grouped(members, parent, DOWNLOAD_EXTRAS)
    assert (grouping.status, grouping.reasons) == ("ready", ())
    (film,) = grouping.film_groups
    assert {v.member_id for v in film.members} == {v.member_id for v in members}
    roles = {v.member_id: v.role for v in film.members}
    attachments = {
        v.relative_path for v in members if roles[v.member_id] == "attachment"
    }
    expected = (
        {"ABF-393.jpg", "新建文本文档.txt"} if parent is None else {"ABF-393.jpg"}
    )
    assert attachments == expected
    # what film_store._check_attachments accepts for a film made under this policy
    allowed = local_host.film_policy(DOWNLOAD_EXTRAS).allowed_attachment_extensions
    assert all(PurePosixPath(path).suffix.casefold() in allowed for path in attachments)


def test_raw_download_needed_attention_under_the_old_empty_list():
    old = _grouped(_members(RAW), None, ())
    assert (old.status, old.reasons) == ("needs_attention", ("unknown_attachment",))


def test_a_changed_root_configuration_regroups_a_stuck_package():
    """D01: an unchanged observation must not keep the grouping of the old policy."""
    members = _members(RAW)
    old = _grouped(members, None, ())
    previous = SimpleNamespace(
        grouping=old,
        parent_context=None,
        root_config_revision=1,
        coverage="complete",
        observation_digest=observation_digest(
            members, parent_context=None, grouping=old
        ),
    )
    source = SimpleNamespace(
        policy=im.IntakePolicy(
            policy_revision=1, allowed_attachment_extensions=DOWNLOAD_EXTRAS
        )
    )
    # the same configuration keeps the stored grouping (an operator correction)
    assert discovery._group(source, members, None, previous, 1) == old
    again = discovery._group(source, members, None, previous, 2)
    assert (again.status, again.reasons) == ("ready", ())
    assert [g.film_id for g in again.film_groups] == [
        g.film_id for g in old.film_groups
    ]
    # H2 IR01: a failed (partial) scan after the reconfigure stored the old
    # grouping under the new revision; the next complete scan must re-group it
    partial = SimpleNamespace(**{**vars(previous), "root_config_revision": 2})
    partial.coverage = "partial"
    regrouped = discovery._group(source, members, None, partial, 2)
    assert (regrouped.status, regrouped.reasons) == ("ready", ())
    assert again.grouping_revision == old.grouping_revision + 1


# ── status (H2 G1) ────────────────────────────────────────────────────────
def test_status_reports_the_running_intake_copy(tmp_path):
    host = LocalWorkflowHost(WorkflowSettings(enabled=True), tmp_path)
    assert host.status()["intake"] is None
    copying = {
        "package_id": str(uuid4()),
        "phase": "copy",
        "bytes_completed": 5,
        "total": 10,
    }
    host.intake = SimpleNamespace(progress=copying)
    assert host.status()["intake"] == copying


@pytest.mark.skipif(sys.platform != "win32", reason="Native root identity")
def test_the_default_intake_policy_allows_the_download_extras(tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    rt = LocalRuntime(tmp_path / "data", [])
    rt.open()
    try:
        rt.ensure_worker()
        rt.start_boot()
        rt.grant_intake_adapter()
        local_host.configure_intake(rt, inbox)
        config = rt.store.get_intake_configuration(rt.operator, rt.identity.root_id)
        assert config.policy.allowed_attachment_extensions == DOWNLOAD_EXTRAS
        assert config.film_policy == local_host.film_policy(DOWNLOAD_EXTRAS)
    finally:
        rt.close()


def _video(ffmpeg, path):
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
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=60,
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Native intake")
def test_stuck_downloads_become_films_once_the_extras_are_allowed(tmp_path):
    """R1 + R3, the live case: both packages stuck under the old list, then films."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not shutil.which("ffprobe"):
        pytest.skip("media tools not available")
    inbox = tmp_path / "inbox"
    raw = "(HD1080P)(Prestige)(ABF-393.67zhf89d)究極のぬるぬるオーガズム 河合あすな"
    scraped = "三佳詩/ABF-394 理不尽不道德NTR 三佳詩"
    for folder in (raw, scraped):
        (inbox / folder).mkdir(parents=True)
    _video(ffmpeg, inbox / raw / "4K688.com@ABF-393.mp4")
    (inbox / raw / "ABF-393.jpg").write_bytes(b"cover from the download site")
    (inbox / raw / "新建文本文档.txt").write_bytes(b"site advert")
    _video(ffmpeg, inbox / scraped / "ABF-394.mp4")
    (inbox / scraped / "ABF-394.nfo").write_text(
        "<movie><num>ABF-394</num></movie>", encoding="utf-8"
    )
    for name in ("poster", "thumb", "fanart"):
        (inbox / scraped / f"ABF-394-{name}.jpg").write_bytes(name.encode())
    (inbox / scraped / "ABF-394.jpg").write_bytes(b"cover from the download site")
    settings = WorkflowSettings(enabled=True, inbox_root=str(inbox))

    def boot(policy):
        host = LocalWorkflowHost(settings, tmp_path / "data", intake_policy=policy)
        host.start()
        _wait(lambda: host.state != "starting")
        assert host.state == "running", host.reason
        return host

    def states(rt):
        page = rt.store.list_intakes(
            rt.operator, im.IntakeListQuery(), current_only=True
        )
        return {v.relative_path: (v.state, v.reason) for v in page.items}

    old = boot(im.IntakePolicy(**FAST))  # the 0.5 default: no extras at all
    try:
        rt = old.runtime
        stuck = {
            raw: ("needs_attention", "ambiguous_grouping"),
            scraped: ("blocked", "publication_blocked"),
        }
        _wait(lambda: states(rt) == stuck, seconds=120)
        assert rt.store.film_board(rt.operator) == ()
        rows = {p.relative_path: p for p in rt.store.list_intake_board(rt.operator)}
        assert rows[raw].grouping_reasons == ("unknown_attachment",)
        assert rows[raw].unassigned_paths == ("新建文本文档.txt",)
        assert rows[scraped].unassigned_paths == ()
    finally:
        old.stop()
    assert old.state == "stopped"
    new = boot(im.IntakePolicy(**FAST, allowed_attachment_extensions=DOWNLOAD_EXTRAS))
    try:
        rt = new.runtime
        _wait(lambda: len(rt.store.film_board(rt.operator)) == 2, seconds=120)
        assert rt.store.list_intake_board(rt.operator) == ()
        inputs = {}
        for film in rt.store.film_board(rt.operator):
            view = rt.store.get_film(rt.operator, film.film_id).film
            manifest = rt.store.get_input_manifest(
                rt.operator, film.film_id, view.current_input_revision
            )
            inputs.update({i.relative_path: i.role for i in manifest.items})
        assert inputs["ABF-393.jpg"] == inputs["新建文本文档.txt"] == "attachment"
        assert inputs["ABF-394.jpg"] == "attachment"
        assert inputs["ABF-394-poster.jpg"] == "poster"
    finally:
        new.stop()


# ── LTX model download from the Media Center (H3 M1) ─────────────────────
@pytest.fixture
def offered(tmp_path, monkeypatch):
    """A running host whose restore lacks its LTX models; fake download workers."""
    from taskpaw_v3.monitors.plugins import jasna_download_session as S
    from taskpaw_v3.tests.test_jasna_download import _Child
    from taskpaw_v3.tests.workflow_exec_helpers import Tools, dispatcher, runtime

    workers: list = []

    def spawn(*a, **kw):
        workers.append(_Child(*a, **kw))
        return workers[-1]

    monkeypatch.setattr(S, "ChildProcess", spawn)
    monkeypatch.setattr(local_host, "DOWNLOAD_TICK_SECONDS", 0.01)
    tools = Tools(tmp_path / "tools")
    weights = tools.jasna_dir / "model_weights" / "ltx-restore"
    shutil.rmtree(weights.parent)
    rt = runtime(tmp_path / "wf", tools)
    host = LocalWorkflowHost(WorkflowSettings(enabled=True), tmp_path / "data")
    host.runtime, host.dispatcher, host.state = rt, dispatcher(rt), "running"
    try:
        yield SimpleNamespace(
            host=host, rt=rt, tools=tools, weights=weights, workers=workers
        )
    finally:
        host.stop(timeout=10)
        assert host.state == "stopped"


def _control(host):
    from fastapi.testclient import TestClient

    from taskpaw_v3.agent.server.app import create_control_app

    return TestClient(
        create_control_app(
            AgentConfig(server_id="s1", machine="dev"),
            control_token="t",
            control_active=lambda: True,
            workflow_provider=lambda: host,
        ),
        headers={"Authorization": "Bearer t"},
    )


def _restore_step(client, film):
    films = client.get("/control/workflow/films").json()["films"]
    row = next(f for f in films if f["film_id"] == film)
    return next(s for s in row["steps"] if s["key"] == "restore")


def test_missing_models_are_offered_downloaded_and_the_restore_retried(offered):
    from taskpaw_v3.monitors.plugins import jasna
    from taskpaw_v3.monitors.plugins.jasna_download import FILES
    from taskpaw_v3.tests.workflow_exec_helpers import File, add_film, stage, video
    from taskpaw_v3.workflow.exec_common import MISSING_LTX
    from taskpaw_v3.workflow.exec_restore import RestoreExecutor

    rt, d, workers = offered.rt, offered.host.dispatcher, offered.workers
    restore = rt.executors["restore"]
    # another film failed restore for another reason: never retried by the download
    rt.executors["restore"] = RestoreExecutor(
        rt, str(offered.tools.jasna_exe), ffprobe=[], gpus=lambda: None
    )
    other = add_film(rt, [File("other.mkv", video())])
    assert d.run_once() is True
    rt.executors["restore"] = restore
    film = add_film(rt, [File("ABF-394.mp4", video())])
    assert d.run_once() is True
    assert d.run_once() is False  # D01: needs attention, never retried on its own
    assert stage(rt, film, "restore").status == "failed"
    client = _control(offered.host)
    step = _restore_step(client, film)
    assert step["state"] == "failed" and step["note"].startswith(MISSING_LTX)
    offer = step["download"]
    missing = jasna.ltx_missing_files(None, False, True)
    assert offer == {
        "state": "awaiting",
        "request_id": offer["request_id"],
        "files": missing,
        "total_bytes": sum(FILES[name][0] for name in missing),
        "downloaded_bytes": 0,
        "destination": str(offered.weights.resolve()),
        "variant": "ltx fast auto-split 12%",
        "error": None,
    }
    assert "download" not in _restore_step(client, other)
    assert workers == []  # nothing downloads without the operator's click
    url = "/control/workflow/ltx-download"
    stale = client.post(url, json={"request_id": "stale", "approved": True})
    assert stale.status_code == 409
    assert (
        client.post(url, json={"request_id": "x", "approved": "yes"}).status_code == 422
    )
    click = {"request_id": offer["request_id"], "approved": True}
    assert client.post(url, json=click).json() == {"ok": True}
    assert client.post(url, json=click).status_code == 409  # a second click
    _wait(lambda: workers, seconds=10)
    (worker,) = workers
    assert json.loads(worker.payload) == {
        "destination": str(offered.weights.resolve()),
        "files": missing,
    }
    worker.sink.put('{"state":"downloading","downloaded_bytes":123}')
    _wait(lambda: _restore_step(client, film)["download"]["downloaded_bytes"] == 123)
    assert _restore_step(client, film)["download"]["state"] == "downloading"
    offered.weights.mkdir(parents=True)
    for name in missing:  # what the verified worker publishes
        (offered.weights / name).write_bytes(b"weights")
    worker.finish()
    _wait(lambda: stage(rt, film, "restore").status == "pending", seconds=10)
    assert stage(rt, other, "restore").status == "failed"
    assert "download" not in _restore_step(client, film)
    assert d.run_once() is True
    assert stage(rt, film, "restore").status == "succeeded"
    assert len(workers) == 1


def test_the_offer_survives_an_agent_restart(tmp_path, monkeypatch):
    """H3 Codex r3: the failed restore is persisted, the executor's cache is not."""
    from taskpaw_v3.monitors.plugins import jasna
    from taskpaw_v3.tests.test_workflow_local_dispatch import GPU_8GB
    from taskpaw_v3.tests.workflow_exec_helpers import (
        File,
        Tools,
        add_film,
        dispatcher,
        runtime,
        stage,
        video,
    )

    tools = Tools(tmp_path / "tools")
    weights = tools.jasna_dir / "model_weights" / "ltx-restore"
    shutil.rmtree(weights.parent)
    data = tmp_path / "data"
    rt = runtime(data, tools)
    film = add_film(rt, [File("ABF-394.mp4", video())])
    assert dispatcher(rt).run_once() is True
    assert stage(rt, film, "restore").status == "failed"
    rt.close()

    def no_gpu_query():
        raise AssertionError("the offer must not query the GPU")

    monkeypatch.setattr(
        local_host,
        "LocalRuntime",
        lambda path, execs: LocalRuntime(
            path, execs, gpu=lambda: GPU_8GB, machine=lambda: "synthetic"
        ),
    )
    host = LocalWorkflowHost(
        WorkflowSettings(enabled=True),
        data,
        executor_factory=lambda r: [tools.restore(r, gpus=no_gpu_query)],
    )
    host.start()
    try:
        _wait(lambda: host.state != "starting")
        assert host.state == "running", host.reason
        client = _control(host)
        offer = _restore_step(client, film)["download"]
        missing = jasna.ltx_missing_files(None, False, True)
        assert offer["state"] == "awaiting" and offer["files"] == missing
        assert offer["variant"] == "ltx fast auto-split 12%"
        weights.mkdir(parents=True)  # copied in by hand meanwhile: withdrawn
        for name in missing:
            (weights / name).write_bytes(b"weights")
        assert "download" not in _restore_step(client, film)
    finally:
        host.stop(timeout=10)
    assert host.state == "stopped"


def test_recall_missing_infers_the_variant_from_the_file_names(tmp_path):
    from taskpaw_v3.tests.workflow_exec_helpers import Tools, runtime

    tools = Tools(tmp_path / "tools")
    shutil.rmtree(tools.jasna_dir / "model_weights")
    rt = runtime(tmp_path / "wf", tools)
    try:
        vae = ["vae.safetensors.enc", "vae-decoder.safetensors.enc"]
        for names, cfg_fast, fast in (
            (["ltx-restore-alpha1-distill8-nvfp4.safetensors.enc"], False, True),
            (["a-nvfp4.enc", "vae.safetensors.enc"], False, True),
            (["ltx-restore-alpha1-distill8-int8.safetensors.enc"], True, False),
            (vae, True, True),
            (vae, False, False),
        ):
            restore = tools.restore(rt, gpus=lambda: pytest.fail("GPU queried"))
            restore.cfg = restore.cfg.model_copy(update={"ltx_fast": cfg_fast})
            restore.recall_missing(names)
            assert restore._models == (restore._weights(), fast), names
        # only VAE missing: the installed model is the one preflight chose
        from taskpaw_v3.monitors.plugins.jasna_download import MODEL_FILES

        weights = restore._weights()
        weights.mkdir(parents=True)
        for installed, cfg_fast in ((False, True), (True, False)):
            for stale in weights.iterdir():
                stale.unlink()
            (weights / MODEL_FILES[(False, installed)]).write_bytes(b"w")
            restore = tools.restore(rt, gpus=lambda: pytest.fail("GPU queried"))
            restore.cfg = restore.cfg.model_copy(update={"ltx_fast": cfg_fast})
            restore.recall_missing(vae)
            assert restore._models == (weights, installed), installed
        restore._models = (tmp_path, True)  # an existing preflight result wins
        restore.recall_missing(["x-int8.enc"])
        assert restore._models == (tmp_path, True)
    finally:
        rt.close()


def test_a_download_can_be_stopped_and_is_offered_again(offered):
    from taskpaw_v3.tests.workflow_exec_helpers import File, add_film, stage, video

    rt, d, workers = offered.rt, offered.host.dispatcher, offered.workers
    film = add_film(rt, [File("ABF-394.mp4", video())])
    assert d.run_once() is True
    client = _control(offered.host)
    offer = _restore_step(client, film)["download"]
    click = {"request_id": offer["request_id"], "approved": True}
    assert client.post("/control/workflow/ltx-download", json=click).status_code == 200
    _wait(lambda: workers, seconds=10)
    stop = client.post("/control/workflow/ltx-download/stop")
    assert stop.json() == {"ok": True}
    again = _restore_step(client, film)["download"]
    assert again["state"] == "awaiting" and again["request_id"] != offer["request_id"]
    click = {"request_id": again["request_id"], "approved": True}
    assert client.post("/control/workflow/ltx-download", json=click).status_code == 200
    _wait(lambda: len(workers) == 2, seconds=10)
    workers[1].survives = True
    stuck = client.post("/control/workflow/ltx-download/stop")
    assert stuck.status_code == 503
    workers[1].survives = False
    assert stage(rt, film, "restore").status == "failed"


def test_stopping_the_host_stops_the_download(offered):
    from taskpaw_v3.tests.workflow_exec_helpers import File, add_film, video

    rt, d, workers = offered.rt, offered.host.dispatcher, offered.workers
    film = add_film(rt, [File("ABF-394.mp4", video())])
    assert d.run_once() is True
    host = offered.host
    offer = host.status()["ltx_download"]
    assert offer["state"] == "awaiting" and offer["files"]
    assert host.respond_ltx_download(offer["request_id"], True)
    _wait(lambda: workers, seconds=10)
    workers[0].survives = True
    host.stop(timeout=1)
    assert host.state == "stop_unconfirmed"  # the worker did not end
    workers[0].survives = False
    host.stop(timeout=1)
    assert host.state == "stopped"
    assert not host.respond_ltx_download(offer["request_id"], True)
    assert film


def test_a_stopped_download_that_ends_later_is_offered_again(offered):
    """H3 IR01: the tick thread reaps a worker whose stop had timed out."""
    from taskpaw_v3.tests.workflow_exec_helpers import File, add_film, video

    rt, d, workers = offered.rt, offered.host.dispatcher, offered.workers
    add_film(rt, [File("ABF-394.mp4", video())])
    assert d.run_once() is True
    host = offered.host
    offer = host.status()["ltx_download"]
    assert host.respond_ltx_download(offer["request_id"], True)
    _wait(lambda: workers, seconds=10)
    workers[0].survives = True
    with pytest.raises(RuntimeError):
        host.stop_ltx_download()
    assert host.status()["ltx_download"] is None  # the worker still holds the folder
    workers[0].rc = 1  # it ends on its own
    _wait(lambda: host.status()["ltx_download"] is not None, seconds=10)
    again = host.status()["ltx_download"]
    assert again["state"] == "awaiting" and again["request_id"] != offer["request_id"]
    assert host.respond_ltx_download(again["request_id"], True)
    _wait(lambda: len(workers) == 2, seconds=10)  # the claim was freed


def test_the_download_ticker_survives_a_failing_tick(offered, monkeypatch):
    """H3 IR02: an unexpected tick error is logged, not fatal to the thread."""
    from taskpaw_v3.tests.workflow_exec_helpers import File, add_film, video

    rt, d, workers = offered.rt, offered.host.dispatcher, offered.workers
    add_film(rt, [File("ABF-394.mp4", video())])
    assert d.run_once() is True
    host = offered.host
    real, calls = host._download.tick, []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        return real()

    monkeypatch.setattr(host._download, "tick", flaky)
    offer = host.status()["ltx_download"]
    assert host.respond_ltx_download(offer["request_id"], True)
    _wait(lambda: workers and len(calls) >= 2, seconds=10)
    assert host._ticker is not None and host._ticker.is_alive()
    workers[0].finish()
    _wait(lambda: host._ticker is None, seconds=10)
    assert host._download.status == {}


def _start_download(offered, worker=True):
    from taskpaw_v3.tests.workflow_exec_helpers import File, add_film, video

    add_film(offered.rt, [File("ABF-394.mp4", video())])
    assert offered.host.dispatcher.run_once() is True
    offer = offered.host.status()["ltx_download"]
    assert offered.host.respond_ltx_download(offer["request_id"], True)
    if worker:
        _wait(lambda: offered.workers, seconds=10)


def test_stop_is_bounded_while_the_ticker_holds_the_download_lock(offered, monkeypatch):
    """H3 Codex r1 F1: a stalled tick must not hang the shutdown."""
    host = offered.host
    entered, release = threading.Event(), threading.Event()
    real = host._download.tick

    def stalled():
        entered.set()
        release.wait(30)
        return real()

    monkeypatch.setattr(host._download, "tick", stalled)
    _start_download(offered, worker=False)
    assert entered.wait(10)
    started = time.monotonic()
    host.stop(timeout=0.05)
    assert time.monotonic() - started < 5
    assert host.state == "stop_unconfirmed"
    assert host.runtime is not None and host.runtime.store is not None
    release.set()
    host.stop(timeout=10)
    assert host.state == "stopped"


def test_stop_is_unconfirmed_while_the_ticker_runs_the_retry_callback(
    offered, monkeypatch
):
    """H3 Codex r1 F2: the callback runs outside the lock but is still ours."""
    host = offered.host
    entered, release = threading.Event(), threading.Event()
    closed = []
    real_close = offered.rt.close
    monkeypatch.setattr(offered.rt, "close", lambda: closed.append(1) or real_close())

    def slow():
        entered.set()
        release.wait(30)

    monkeypatch.setattr(host, "_downloaded", slow)
    _start_download(offered)
    offered.workers[0].finish()
    assert entered.wait(10)
    host.stop(timeout=0.05)
    assert host.state == "stop_unconfirmed" and closed == []
    release.set()
    host.stop(timeout=10)
    assert host.state == "stopped" and closed == [1]
