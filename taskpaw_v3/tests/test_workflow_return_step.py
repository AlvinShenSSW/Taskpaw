"""Local return package, hand-off manifest and receipt (T04-P1 E4–E6, D12, D17)."""

import hashlib
import json
import shutil

import pytest

from taskpaw_v3.core.gpu_lease import GpuLease
from taskpaw_v3.tests.test_workflow_films import STAGES, input_manifest
from taskpaw_v3.tests.test_workflow_local_dispatch import GPU_8GB, Crash
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.executors import ExecOutcome
from taskpaw_v3.workflow.local_dispatch import LocalDispatcher
from taskpaw_v3.workflow.local_gpu import max_resolution
from taskpaw_v3.workflow.local_runtime import LocalIdentity, LocalRuntime
from taskpaw_v3.workflow.return_step import MANIFEST_NAME, LocalReturnStep, package_dir
from taskpaw_v3.workflow.stage_results import (
    FileRef,
    RestoreFacts,
    StageResult,
    SubtitleFacts,
    VideoResult,
    file_ref,
    load_stage_result,
    main_videos,
    workcopy_path,
    write_stage_result,
)

VIDEO = b"synthetic video bytes\x00" * 64
JA = "1\n00:00:01,000 --> 00:00:02,000\nこんにちは、元気ですか\n\n".encode()
ZH = "1\n00:00:01,000 --> 00:00:02,000\n你好，你好吗\n\n".encode()
WORKCOPY = "intake/op-1/snapshot"


def _tools(stage):
    return m.ToolVersions(
        tool="fake",
        tool_version="1",
        model="fake",
        model_version="1",
        profile=f"fake-{stage}",
        profile_revision=1,
        config_digest="d" * 64,
    )


class _Fake:
    uses_gpu = False

    def __init__(self, rt):
        self.rt = rt

    def tools(self):
        return _tools(self.stage)

    def max_resolution(self, vram):
        return max_resolution(vram)

    def _done(self, context, videos):
        digest = write_stage_result(
            context.workdir,
            StageResult(
                film_id=context.job.film_id,
                input_revision=context.job.input_revision,
                stage=self.stage,
                attempt_id=context.workdir.name,
                videos=tuple(videos),
            ),
        )
        return ExecOutcome.succeeded(result_digest=digest)


class FakeRestore(_Fake):
    stage = "restore"

    def run(self, context):
        videos = []
        for video in main_videos(context.manifest):
            out = context.workdir / "movie-破解.mkv"
            shutil.copyfile(
                workcopy_path(self.rt, context.manifest, video.item_id), out
            )
            ref = file_ref(self.rt.work_dir, out)
            facts = RestoreFacts(
                decision="applied",
                file=ref,
                width=1920,
                height=1080,
                duration_ms=120000,
                video_streams=1,
                audio_streams=1,
            )
            videos.append(
                VideoResult(
                    video_item_id=video.item_id, volume_ordinal=1, restore=facts
                )
            )
        return self._done(context, videos)


class FakeAsr(_Fake):
    stage = "asr"

    def run(self, context):
        (video,) = main_videos(context.manifest)
        (sub,) = [i for i in context.manifest.items if i.role == "source_subtitle"]
        ja = file_ref(
            self.rt.work_dir, workcopy_path(self.rt, context.manifest, sub.item_id)
        )
        facts = SubtitleFacts(decision="source_ja", ja=ja)
        return self._done(
            context,
            [
                VideoResult(
                    video_item_id=video.item_id, volume_ordinal=1, subtitle=facts
                )
            ],
        )


class FakeTranslate(_Fake):
    stage = "translate"

    def run(self, context):
        asr = load_stage_result(self.rt, context.job.film_id, "asr")
        (entry,) = asr.videos
        out = context.workdir / "movie-破解.zh.srt"
        out.write_bytes(ZH)
        facts = SubtitleFacts(
            decision=entry.subtitle.decision,
            ja=entry.subtitle.ja,
            zh=file_ref(self.rt.work_dir, out),
            kept_ja=0,
        )
        return self._done(context, [entry.model_copy(update={"subtitle": facts})])


def make_runtime(path):
    rt = LocalRuntime(
        path, [], gpu=lambda: GPU_8GB, machine=lambda: "synthetic-machine"
    )
    rt.executors = {
        e.stage: e for e in (FakeRestore(rt), FakeAsr(rt), FakeTranslate(rt))
    }
    rt.open()
    rt.ensure_worker()
    rt.start_boot()
    return rt


@pytest.fixture
def rt(tmp_path):
    value = make_runtime(tmp_path / "wf")
    try:
        yield value
    finally:
        value.close()


def add_real_film(rt):
    """A film whose work copy really exists, with matching hashes."""
    files = {"movie.mkv": VIDEO, "movie.ja.srt": JA}
    for name, raw in files.items():
        path = rt.work_dir / WORKCOPY / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)

    def real(data):
        for item in (*data["items"], *data["readiness"]["workcopy"]["items"]):
            raw = files[item["relative_path"]]
            item["size_bytes"], item["sha256"] = (
                len(raw),
                hashlib.sha256(raw).hexdigest(),
            )
        for item in data["readiness"]["workcopy"]["items"]:
            item["relative_path"] = f"{WORKCOPY}/{item['relative_path']}"

    _, auth = rt.grant_intake_adapter()
    film = identity()
    rt.store.create_film(
        auth,
        rt.meta(),
        m.FilmCreate(
            film_id=film,
            intake_session_id=identity(),
            display_name="Film",
            policy=m.FilmPolicy(
                policy_revision=1,
                required_stages=STAGES,
                subtitle_requirement="chinese",
                required_image_roles=("poster",),
                folder_cover_required=False,
            ),
        ),
    )
    rt.store.put_input_manifest(
        auth,
        rt.meta(),
        m.InputManifestPut(
            film_id=film, expected_version=1, manifest=input_manifest(film, change=real)
        ),
    )
    return film


def dispatcher(rt):
    return LocalDispatcher(
        rt, lease=GpuLease(), poll=0.05, return_step=LocalReturnStep()
    )


def status(rt, film, name):
    return next(
        s.status for s in rt.store.get_film(rt.operator, film).stages if s.stage == name
    )


def run_all(d):
    while d.run_once():
        pass


def test_return_builds_a_verified_package_with_the_m01_handoff(rt):
    film = add_real_film(rt)
    run_all(dispatcher(rt))
    assert status(rt, film, "return") == "succeeded"
    package = package_dir(rt, film, 1)
    assert (package / "movie-破解.mkv").read_bytes() == VIDEO
    assert (package / "movie-破解.zh.srt").read_bytes() == ZH
    assert (package / "movie-破解.ja.srt").read_bytes() == JA
    assert not (
        package / "movie.ja.srt"
    ).exists()  # included once, under its final name
    handoff = json.loads((package / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert handoff["film_id"] == film and handoff["input_revision"] == 1
    assert handoff["restoration"]["status"] == "applied"
    assert handoff["restoration"]["videos"][0]["height"] == 1080
    assert handoff["subtitles"]["status"] == "present"
    assert handoff["subtitles"]["language"] == "zh"
    roles = {i["relative_path"]: i["role"] for i in handoff["items"]}
    assert roles == {
        "movie-破解.mkv": "main_video",
        "movie-破解.zh.srt": "generated_subtitle",
        "movie-破解.ja.srt": "source_subtitle",
    }
    assert "return" not in handoff["attempts"]
    # the intake work copy is untouched
    assert (rt.work_dir / WORKCOPY / "movie.mkv").read_bytes() == VIDEO
    assert not any(rt.attempt_dir.glob("*.json"))


def test_crash_before_the_receipt_reuses_the_finished_package(tmp_path, monkeypatch):
    first = make_runtime(tmp_path / "wf")
    film = add_real_film(first)
    d = dispatcher(first)
    for _ in range(3):
        assert d.run_once()

    def crash(*args, **kwargs):
        raise Crash()

    monkeypatch.setattr(first.store, "accept_artifact_receipt", crash)
    with pytest.raises(Crash):
        d.run_once()
    first.close()
    assert (package_dir(first, film, 1) / MANIFEST_NAME).is_file()
    monkeypatch.undo()
    builds = []
    real_build = LocalReturnStep._build
    monkeypatch.setattr(
        LocalReturnStep,
        "_build",
        lambda self, *a: builds.append(1) or real_build(self, *a),
    )
    second = make_runtime(tmp_path / "wf")
    try:
        assert second.reconciled
        assert (
            status(second, film, "return") == "pending"
        )  # the interrupted return was settled
        run_all(dispatcher(second))
        assert status(second, film, "return") == "succeeded"
        assert builds == []  # nothing rebuilt, no GPU stage rerun
        assert status(second, film, "restore") == "succeeded"
    finally:
        second.close()


def test_leftover_partial_directory_is_rebuilt(rt):
    film = add_real_film(rt)
    partial = package_dir(rt, film, 1).with_name(
        package_dir(rt, film, 1).name + ".partial"
    )
    (partial / "junk").mkdir(parents=True)
    run_all(dispatcher(rt))
    assert status(rt, film, "return") == "succeeded"
    assert not partial.exists()


def test_tampered_stage_result_stops_the_return(rt):
    film = add_real_film(rt)
    d = dispatcher(rt)
    for _ in range(3):
        assert d.run_once()
    restore = next(
        s for s in rt.store.get_film(rt.operator, film).stages if s.stage == "restore"
    )
    result = rt.work_dir / restore.attempt_id / "stage-result.json"
    result.write_text(
        result.read_text(encoding="utf-8").replace("1080", "1081"), encoding="utf-8"
    )
    assert d.run_once() is True
    assert status(rt, film, "return") == "failed"


def test_settlement_only_touches_this_runtime_assignments(rt):
    own = m.AssignmentRecord.model_construct(worker_id=rt.identity.worker_id)
    receiver = m.AssignmentRecord.model_construct(worker_id=None)
    ours = m.OpenAssignment.model_construct
    assert rt._ours(ours(assignment=own, stage="restore", owner_component_id=None))
    assert rt._ours(
        ours(
            assignment=receiver,
            stage="return",
            owner_component_id=rt.identity.receiver_component_id,
        )
    )
    assert not rt._ours(
        ours(assignment=receiver, stage="scrape", owner_component_id=identity())
    )
    assert not rt._ours(
        ours(assignment=receiver, stage="return", owner_component_id=identity())
    )


def test_old_identity_file_gets_a_stable_receiver_component(tmp_path):
    path = tmp_path / "local-identity.json"
    from datetime import datetime, timezone

    first = LocalIdentity.load_or_create(path, datetime.now(timezone.utc))
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["receiver_component_id"]
    path.write_text(json.dumps(data), encoding="utf-8")
    again = LocalIdentity.load_or_create(path, datetime.now(timezone.utc))
    assert (
        again.receiver_component_id and again.operator_secret == first.operator_secret
    )
    assert (
        LocalIdentity.load_or_create(
            path, datetime.now(timezone.utc)
        ).receiver_component_id
        == again.receiver_component_id
    )


def test_refs_are_relative_to_the_work_root(rt):
    target = rt.work_dir / "x" / "f.bin"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"abc")
    assert file_ref(rt.work_dir, target) == FileRef(
        path="x/f.bin", sha256=hashlib.sha256(b"abc").hexdigest(), size=3
    )
