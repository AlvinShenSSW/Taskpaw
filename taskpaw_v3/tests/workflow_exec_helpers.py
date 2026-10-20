"""Shared setup of the T04 executor tests: runtime, films with real work copies,
fake tools and a direct (dispatcher-free) stage run."""

from __future__ import annotations

import copy
import hashlib
import json
import queue
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

from taskpaw_v3.core.gpu_lease import GpuLease
from taskpaw_v3.monitors.plugins import jasna
from taskpaw_v3.monitors.subs.srt import Cue
from taskpaw_v3.monitors.subs.translate import TranslateRequest, TranslateResult
from taskpaw_v3.tests.test_workflow_films import STAGES, fixture, input_manifest
from taskpaw_v3.tests.test_workflow_local_dispatch import GPU_8GB
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.exec_asr import AsrExecutor
from taskpaw_v3.workflow.exec_restore import RestoreExecutor
from taskpaw_v3.workflow.exec_translate import TranslateExecutor
from taskpaw_v3.workflow.executors import ExecOutcome, JobContext
from taskpaw_v3.workflow.local_dispatch import LocalDispatcher
from taskpaw_v3.workflow.local_runtime import LocalRuntime
from taskpaw_v3.workflow.owned_process import OwnedJob

FAKE = Path(__file__).with_name("workflow_fake_tools.py")
ZH_SRT = "".join(
    f"{i}\n00:00:0{i},000 --> 00:00:0{i},900\n这是一个中文字幕的测试句子\n\n"
    for i in (1, 2, 3)
)
JA_SRT = "".join(
    f"{i}\n00:00:0{i},000 --> 00:00:0{i},900\nこれは日本語の字幕のテストです\n\n"
    for i in (1, 2, 3)
)


def fake(tool: str) -> list[str]:
    return [sys.executable, str(FAKE), tool]


def video(width: int = 1920, height: int = 1080, duration: float = 120.0) -> bytes:
    return json.dumps({"width": width, "height": height, "duration": duration}).encode()


@dataclass
class Tools:
    """Fake tool installs under `root`; `jasna`/`whisper` modes are JSON configs."""

    root: Path

    def __post_init__(self) -> None:
        self.jasna_dir = self.root / "jasna"
        self.jasna_exe = self.jasna_dir / "jasna.exe"
        self.whisper_exe = self.root / "whisperjav" / "whisperjav.exe"
        weights = self.jasna_dir / "model_weights" / "ltx-restore"
        weights.mkdir(parents=True, exist_ok=True)
        for fast in (True, False):
            for name in jasna.ltx_missing_files(None, False, fast):
                (weights / name).write_bytes(b"weights")
        self.whisper_exe.parent.mkdir(parents=True, exist_ok=True)
        self.set_jasna()
        self.set_whisper()

    def set_jasna(self, **config: object) -> None:
        self.jasna_exe.write_text(json.dumps(config), encoding="utf-8")

    def set_whisper(self, **config: object) -> None:
        self.whisper_exe.write_text(json.dumps(config), encoding="utf-8")

    def runs(self, exe: Path) -> list[list[str]]:
        path = Path(f"{exe}.runs")
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text("utf-8").splitlines()]

    def restore(self, rt: LocalRuntime, **kw: object) -> RestoreExecutor:
        kw.setdefault("ffprobe", fake("ffprobe"))
        kw.setdefault("gpus", lambda: None)
        return RestoreExecutor(
            rt, str(self.jasna_exe), launch_prefix=fake("jasna"), **kw
        )

    def asr(self, rt: LocalRuntime) -> AsrExecutor:
        return AsrExecutor(rt, str(self.whisper_exe), launch_prefix=fake("whisperjav"))


class FakeTranslator:
    """The `Translator` interface; `mode` decides every film's outcome."""

    instances: list[FakeTranslator] = []

    def __init__(self, run, checkpoint_dir: Path, mode: str = "translated") -> None:
        self.run, self.checkpoint_dir, self.mode = run, checkpoint_dir, mode
        self.results: queue.Queue = queue.Queue()
        self.requests: list[TranslateRequest] = []
        self.started = self.cancelled = self.joined = False
        self.submitted = threading.Event()
        self.discarded: list[str] = []
        FakeTranslator.instances.append(self)

    def start(self) -> None:
        self.started = True

    def submit(self, req: TranslateRequest) -> None:
        self.requests.append(req)
        self.submitted.set()
        if self.mode == "hang":
            return
        zh: tuple[Cue, ...] = ()
        if self.mode == "translated":
            zh = tuple(
                Cue(c.index, c.start_ms, c.end_ms, f"中文{c.index}") for c in req.cues
            )
        self.results.put(
            TranslateResult(
                req.run,
                req.job_id,
                self.mode,
                zh,
                "" if self.mode == "translated" else f"{self.mode} detail",
                kept_ja=1 if self.mode == "translated" else 0,
                checkpoint_key=f"key-{req.job_id}",
            )
        )

    def cancel(self) -> None:
        self.cancelled = True

    def join(self, timeout: float) -> None:
        self.joined = True

    def discard_checkpoint(self, key: str) -> None:
        self.discarded.append(key)


def translator(mode: str = "translated"):
    def factory(run, checkpoint_dir):
        return FakeTranslator(run, checkpoint_dir, mode)

    return factory


def translate(rt: LocalRuntime, mode: str = "translated", **kw) -> TranslateExecutor:
    kw.setdefault("chain_fn", lambda: ("provider",))
    return TranslateExecutor(rt, translator_factory=translator(mode), poll=0.02, **kw)


def runtime(
    path: Path, tools: Tools, *, mode: str = "translated", **kw
) -> LocalRuntime:
    rt = LocalRuntime(path, [], gpu=lambda: GPU_8GB, machine=lambda: "synthetic")
    rt.executors = {
        "restore": tools.restore(rt, **kw),
        "asr": tools.asr(rt),
        "translate": translate(rt, mode),
    }
    rt.open()
    rt.ensure_worker()
    rt.start_boot()
    return rt


@dataclass
class File:
    path: str
    data: bytes
    role: str = "main_video"
    language: str | None = None
    video: str | None = None  # relative path of the subtitle's main video
    volume: int | None = None
    duration_ms: int = 120_000


def add_film(
    rt: LocalRuntime, files: list[File], *, attachments: tuple[str, ...] = ()
) -> str:
    """A film whose work copies really exist under `rt.work_dir`.

    `attachments`: the attachment suffixes its policy allows (H2 R1)."""
    film_id = identity()
    template = fixture("input-manifest-v1.json")
    item0 = template["items"][0]
    copy0 = template["readiness"]["workcopy"]["items"][0]
    probe0 = template["readiness"]["media_probes"][0]
    ids = {f.path: identity() for f in files}
    items, copies, probes = [], [], []
    for f in files:
        digest = hashlib.sha256(f.data).hexdigest()
        rel = f"copies/{film_id}/{f.path}"
        target = rt.work_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(f.data)
        item = copy.deepcopy(item0)
        item.update(
            item_id=ids[f.path],
            role=f.role,
            relative_path=f.path,
            size_bytes=len(f.data),
            sha256=digest,
            native_identity=None,
            volume_ordinal=f.volume if f.role == "main_video" else None,
            video_item_id=ids[f.video] if f.video else None,
            language=f.language,
        )
        items.append(item)
        workcopy = copy.deepcopy(copy0)
        workcopy.update(
            source_item_id=ids[f.path],
            relative_path=rel,
            size_bytes=len(f.data),
            sha256=digest,
            native_identity=None,
        )
        copies.append(workcopy)
        if f.role == "main_video":
            facts = json.loads(f.data) if f.data.startswith(b"{") else {}
            probe = copy.deepcopy(probe0)
            probe.update(
                item_id=ids[f.path],
                width=facts.get("width", 1920),
                height=facts.get("height", 1080),
                duration_ms=f.duration_ms,
            )
            probes.append(probe)

    def change(data: dict) -> None:
        data["items"] = items
        data["readiness"]["workcopy"]["items"] = copies
        data["readiness"]["media_probes"] = probes

    _, auth = rt.grant_intake_adapter()
    rt.store.create_film(
        auth,
        rt.meta(),
        m.FilmCreate(
            film_id=film_id,
            intake_session_id=identity(),
            display_name="Film",
            policy=m.FilmPolicy(
                policy_revision=1,
                required_stages=STAGES,
                subtitle_requirement="chinese",
                required_image_roles=("poster",),
                folder_cover_required=False,
                allowed_attachment_extensions=attachments,
            ),
        ),
    )
    rt.store.put_input_manifest(
        auth,
        rt.meta(),
        m.InputManifestPut(
            film_id=film_id,
            expected_version=1,
            manifest=input_manifest(film_id, change=change),
        ),
    )
    return film_id


def stage(rt: LocalRuntime, film_id: str, name: str) -> m.StageRecord:
    return next(
        s for s in rt.store.get_film(rt.operator, film_id).stages if s.stage == name
    )


def attempts(rt: LocalRuntime, film_id: str, name: str) -> list[m.AttemptRecord]:
    return list(rt.store.list_attempts(rt.operator, stage(rt, film_id, name).job_id))


def dispatcher(rt: LocalRuntime) -> LocalDispatcher:
    return LocalDispatcher(rt, lease=GpuLease(), poll=0.05)


def run_stages(rt: LocalRuntime, *names: str) -> None:
    """Run the dispatcher once per named stage, asserting each one ran."""
    d = dispatcher(rt)
    for _ in names:
        assert d.run_once() is True


def manifest(rt: LocalRuntime, film_id: str) -> m.InputManifest:
    return rt.store.get_input_manifest(rt.operator, film_id, 1)


@dataclass
class Direct:
    outcome: ExecOutcome
    workdir: Path
    progress: list[str]
    processes: OwnedJob
    ltx: list[dict]  # the structured LTX state beside each text update (H3 M3)


def run_direct(
    rt: LocalRuntime,
    film_id: str,
    name: str,
    executor=None,
    *,
    cancel: threading.Event | None = None,
) -> Direct:
    """Run one pending stage's executor without the dispatcher."""
    job = next(
        j
        for j in rt.store.list_jobs(rt.operator, "pending", (name,))
        if j.film_id == film_id
    )
    executor = executor or rt.executors[name]
    workdir = rt.work_dir / identity()
    workdir.mkdir(parents=True)
    progress: list[str] = []
    states: list[dict] = []
    processes = OwnedJob()

    def report(text: str, ltx: dict | None = None) -> None:
        progress.append(text)
        if ltx is not None:
            states.append(ltx)

    context = JobContext(
        job,
        manifest(rt, film_id),
        workdir,
        cancel or threading.Event(),
        processes,
        report,
    )
    try:
        outcome = executor.run(context)
    finally:
        processes.terminate()
        processes.settle(10)
    return Direct(outcome, workdir, progress, processes, states)
