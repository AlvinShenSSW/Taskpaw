"""Mount the controller's workflow into the agent process (T03-P1 §3.9, C4, C7).

Started after the agent announced readiness, in a background thread; any failure
is recorded in `status()` and never affects the agent's other features. The
workflow shares the agent's process-wide `gpu_lease` with manual GPU tasks.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from taskpaw_v3.core.config import WorkflowSettings
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.monitors.plugins.jasna_download_session import DownloadSession

from . import intake_models as im
from . import models as m
from .auth import STAGE_ROLES
from .exec_common import MISSING_LTX
from .executors import StageExecutor
from .filmpaw_client import FilmpawClient
from .finalize import Finalizer
from .fleet_live import FleetLive
from .intake_protocol import ProbeArtifact
from .library_scan import LibraryScanner
from .library_store import LibraryStore
from .local_dispatch import LocalDispatcher, ReturnStep
from .local_runtime import LocalRuntime, intake_profiles
from .lookup import MetadataLookup
from .source_watch import SourceWatch
from .timing import TaskLogSink, WorkflowLog
from .transfer import Transfers, live_attempts

ExecutorFactory = Callable[[LocalRuntime], Sequence[StageExecutor]]
LookupFactory = Callable[[LocalRuntime], MetadataLookup]

log = logging.getLogger("taskpaw.workflow.host")

COMPUTE_STAGES: tuple[m.Stage, ...] = ("restore", "asr", "translate")
DOWNLOAD_TICK_SECONDS = 1.0  # how often a running model download is advanced
DOWNLOAD_STOP_SECONDS = 10.0
_DOWNLOAD_BUSY = ("approved", "downloading")
MANDATORY_STAGES: tuple[m.Stage, ...] = (
    "intake",
    "return",
    "scrape",
    "archive",
    "cleanup_input",
    "cleanup_workcopy",
)


# H2 R1: what a download leaves beside the video (cover, advert, shortcut). They
# are attachments: never staged for Miko nor archived, deleted with the input.
# The single list for the intake and the film policy (the store validates both
# are equal); every plain image grouping may emit as an attachment is in it.
DOWNLOAD_EXTRAS: tuple[str, ...] = (
    ".txt",
    ".url",
    ".html",
    ".htm",
    ".mht",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".lnk",
    ".torrent",
)


def film_policy(extensions: tuple[str, ...]) -> m.FilmPolicy:
    return m.FilmPolicy(
        policy_revision=1,
        required_stages=(MANDATORY_STAGES[0], *COMPUTE_STAGES, *MANDATORY_STAGES[1:]),
        subtitle_requirement="chinese",
        required_image_roles=("poster",),
        folder_cover_required=False,
        allowed_attachment_extensions=extensions,
    )


def probe_artifact(path: str | None) -> ProbeArtifact | None:
    found = path or shutil.which("ffprobe")
    if not found or not Path(found).is_file():
        return None
    artifact = Path(found).resolve()
    sha = hashlib.sha256()
    with artifact.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            sha.update(chunk)
    digest = sha.hexdigest()
    return ProbeArtifact(
        executable=str(artifact),
        sha256=digest,
        size_bytes=artifact.stat().st_size,
        version="local",
    )


def _same_root(old: im.IntakeRootPut, new: im.IntakeRootPut) -> bool:
    def key(value: im.IntakeRootPut) -> dict[str, Any]:
        data = value.model_dump(
            mode="json",
            exclude={"expected_version", "native_identity", "work_native_identity"},
        )
        for profile in data["profiles"]:
            profile.pop("approval")
        for name in ("native_identity", "work_native_identity"):
            native = getattr(value, name)
            data[name] = (native.scheme, native.volume_or_export_id, native.object_id)
        return data

    return key(old) == key(new)


def configure_intake(
    rt: LocalRuntime, inbox: Path, policy: im.IntakePolicy | None = None
) -> None:
    """Point the store's single intake root at the configured INBOX."""
    from .intake_files import inspect_root

    ident = rt.identity
    rt.work_dir.mkdir(parents=True, exist_ok=True)
    try:
        current: im.IntakeRootPut | None = rt.store.get_intake_configuration(
            rt.operator, ident.root_id
        )
    except m.WorkflowError as exc:
        if str(exc) != "not_found":
            raise
        current = None
    policy = policy or im.IntakePolicy(
        policy_revision=1, allowed_attachment_extensions=DOWNLOAD_EXTRAS
    )
    wanted = im.IntakeRootPut(
        expected_version=0 if current is None else current.expected_version + 1,
        root_id=ident.root_id,
        native_identity=inspect_root(inbox, ident.root_id),
        private_path=str(inbox),
        work_root_id=ident.work_root_id,
        work_native_identity=inspect_root(rt.work_dir, ident.work_root_id),
        private_work_path=str(rt.work_dir),
        component_id=ident.intake_component_id,
        policy=policy,
        film_policy=film_policy(policy.allowed_attachment_extensions),
        profiles=intake_profiles(rt.evidence("intake_profile_approval")),
        enabled=True,
    )
    if current is None or not _same_root(current, wanted):
        rt.store.configure_intake_root(rt.operator, rt.meta(), wanted)


def retry_failed_stage(rt: LocalRuntime, film_id: str, stage: str) -> None:
    """重试 (T06-P1 D05): only a failed or blocked local stage of the current revision."""
    job = next(
        (s for s in rt.store.get_film(rt.operator, film_id).stages if s.stage == stage),
        None,
    )
    if (
        job is None
        or stage not in STAGE_ROLES
        or job.status not in ("failed", "blocked")
    ):
        raise m.WorkflowError("precondition_failed")
    rt.store.retry_stage(
        rt.operator,
        rt.meta(),
        m.StageRetry(
            job_id=job.job_id, expected_version=job.version, reason="operator"
        ),
    )


class WorkflowHostLike(Protocol):
    """What the 媒体入库 control routes need from a host (T06-P1 D14).

    U1: the 待处理 routes go through `runtime` (plans, the lookup cache on
    `runtime.lookup`) and wake or pause `dispatcher`.
    """

    state: str
    reason: str
    runtime: LocalRuntime | None
    finalizer: Finalizer | None
    dispatcher: Any

    def status(self) -> dict[str, Any]: ...

    def confirm(
        self,
        film_id: str,
        performer_id: str | None = None,
        target_dir: str | None = None,
    ) -> str: ...

    def retry(self, film_id: str, stage: str) -> None: ...

    def respond_ltx_download(self, request_id: str, approved: bool) -> bool: ...

    def stop_ltx_download(self) -> None: ...


def retry_missing_models(rt: LocalRuntime) -> list[str]:
    """H3 D01: requeue every restore that failed only for missing LTX models,
    except a film whose tier still lacks files (U1 Codex r2 F1): its retry
    would only stop on them again."""
    executor = rt.executors.get("restore")
    unresolved = getattr(executor, "unresolved_tiers", None)
    waiting = unresolved() if callable(unresolved) else set()
    preset = getattr(getattr(executor, "cfg", None), "ltx_model", None)
    retried = []
    for film in rt.store.film_board(rt.operator):
        restore = next((s for s in film.stages if s.stage == "restore"), None)
        if (
            restore is None
            or restore.status != "failed"
            or not (restore.note or "").startswith(MISSING_LTX)
            or (film.plan_ltx_model or preset) in waiting
        ):
            continue
        try:
            retry_failed_stage(rt, film.film_id, "restore")
        except m.WorkflowError as exc:  # changed meanwhile: the operator retries
            log.warning("model retry skipped %s: %s", film.film_id, exc)
            continue
        retried.append(film.film_id)
    return retried


def recall_missing_models(rt: LocalRuntime) -> None:
    """H3: after a restart, tell the restore executor which models its persisted
    failures lacked, so the 修复 step offers the download again; every film's
    tier is recalled (F1), the session then offers them one at a time."""
    restore = rt.executors.get("restore")
    recall = getattr(restore, "recall_missing", None)
    if not callable(recall):
        return
    for film in rt.store.film_board(rt.operator):
        stage = next((s for s in film.stages if s.stage == "restore"), None)
        note = (stage.note or "") if stage is not None else ""
        if (
            stage is None
            or stage.status != "failed"
            or not note.startswith(MISSING_LTX)
        ):
            continue
        names = [n.strip() for n in note[len(MISSING_LTX) :].split("、")]
        recall([n for n in names if n], film.plan_ltx_model)  # U1: the film's tier


class LocalWorkflowHost:
    def __init__(
        self,
        settings: WorkflowSettings,
        data_dir: Path,
        executors: Sequence[StageExecutor] = (),
        *,
        intake_policy: im.IntakePolicy | None = None,
        executor_factory: ExecutorFactory | None = None,
        return_step: ReturnStep | None = None,
        scrape_step: ReturnStep | None = None,
        off_reasons: dict[str, str] | None = None,
        lookup_factory: LookupFactory | None = None,
        task_log: TaskLogSink | None = None,
        event_queue: EventQueue | None = None,
    ) -> None:
        self.settings, self.data_dir, self.executors = settings, data_dir, executors
        # U1e: the agent's task log (the 日志 tab) gets a row per finished stage
        self.workflow_log = WorkflowLog(task_log)
        from taskpaw_v3.agent.server.workflow_events import WorkflowEvents

        self.events = (
            WorkflowEvents(event_queue, data_dir / "workflow-events.json")
            if event_queue is not None
            else None
        )
        self.lookup_factory = lookup_factory
        self.lookup: MetadataLookup | None = None
        self.intake_policy = intake_policy
        self.executor_factory, self.return_step = executor_factory, return_step
        self.scrape_step, self.off_reasons = scrape_step, off_reasons
        self.state, self.reason = "starting", ""
        self.runtime: LocalRuntime | None = None
        self.intake: Any = None
        self.dispatcher: LocalDispatcher | None = None
        self.finalizer: Finalizer | None = None
        # P3 E1 (#304): films whose INBOX source vanished
        self.sources: SourceWatch | None = None
        # P2-2: the workers' live state, while the worker API is on
        self.fleet: FleetLive | None = None
        self.transfers: Transfers | None = None
        # P2-3 R9: attempts stopped with 停止并重新排队, until they resolve
        self.cancels: set[str] = set()
        self.scheduler: Any = None  # P2-3 R13: the routing pass's thread
        # #362: the controller's NAS library index and its scanner thread
        self.library: LibraryStore | None = None
        self.library_scanner: LibraryScanner | None = None
        self.library_reason = ""
        # P2-4 R1: the last pass's decision; its waiting reasons reach the board
        self.last_pass: Any = None
        # the controller's free disk for the C8 gate (tests inject a figure)
        self.free_bytes: Callable[[], int | None] | None = None
        # C4's /status channel; None: the agent's power peers (P2-4 R8)
        self.status_channel: Callable[[], Any] | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        # H3 M1/D02: one LTX model download; the session needs serialized calls
        self._download = DownloadSession()
        self._download_lock = threading.Lock()
        self._ticker: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._boot, name="workflow-host", daemon=True
        )
        self._thread.start()

    def _boot(self) -> None:
        try:
            rt = self.runtime = LocalRuntime(
                self.data_dir,
                self.executors,
                output_dir=Path(self.settings.output_dir)
                if self.settings.output_dir
                else None,
            )
            if self.executor_factory is not None:  # executors that read the ledger
                rt.executors = {e.stage: e for e in self.executor_factory(rt)}
            rt.open()
            rt.ensure_worker()
            rt.start_boot()
            _, intake_auth = rt.grant_intake_adapter()
            rt.settle_intake()
            try:
                recall_missing_models(rt)
            except Exception:
                log.exception("media workflow model offer recall failed")
            inbox = self.settings.inbox_root
            if inbox:
                if not Path(inbox).is_dir():
                    raise FileNotFoundError(f"收件目录不存在：{inbox}")
                artifact = probe_artifact(self.settings.ffprobe_path)
                if artifact is None:
                    raise FileNotFoundError("找不到 ffprobe")
                configure_intake(rt, Path(inbox), self.intake_policy)
                from .intake import IntakeService

                self.intake = IntakeService(
                    rt.store,
                    intake_auth,
                    root_id=rt.identity.root_id,
                    meta=rt.meta,
                    owner_boot_id=rt.boot_id,
                    ffprobe=artifact,
                    controller=True,
                    enabled=True,
                    clock=rt.clock.now,
                )
                if self._stopping.is_set():
                    return
                self.intake.start()
            data = self.settings.filmpaw_data_dir
            self.finalizer = Finalizer(
                rt,
                Path(inbox) if inbox else None,
                FilmpawClient(Path(data) if data else None),
            )
            self.sources = SourceWatch(
                rt, Path(inbox) if inbox else None, on_change=self._on_sources
            )
            self.dispatcher = LocalDispatcher(
                rt,
                sources=self.sources,
                return_step=self.return_step,
                scrape_step=self.scrape_step,
                off_reasons=self.off_reasons,
                finalizer=self.finalizer,
                workflow_log=self.workflow_log,
                on_resolved=self._local_resolved,
                remote_enabled=self.settings.remote_workers
                and self.settings.role == "controller",
            )
            if self._stopping.is_set():
                return
            self.dispatcher.start()
            if self.lookup_factory is not None:  # U1 L: off the dispatch thread
                self.lookup = rt.lookup = self.lookup_factory(rt)
                self.lookup.start()
            if self.settings.remote_workers and self.settings.role == "controller":
                from taskpaw_v3 import __version__

                from .routing import RoutingThread

                # R9: the restart grace counts from here (IR12, C14)
                self.fleet = FleetLive(
                    rt.clock.now(), __version__, on_change=self._wake_scheduler
                )
                self.transfers = Transfers(rt.work_dir, live=lambda: live_attempts(rt))
                self.transfers.sweep()  # IR5: settled attempts' plans go
                # P2-3 R13: the routing pass's own thread
                self.scheduler = RoutingThread(self._route_pass)
                if self._stopping.is_set():
                    return
                self.scheduler.start()
            if self._stopping.is_set():
                return
            self._start_library()
            self.state, self.reason = "running", rt.reason
            if self.events is not None and not self._stopping.is_set():
                self.events.start(self)
        except Exception as exc:  # never fatal to the agent (C4)
            log.exception("media workflow failed to start")
            self.state, self.reason = "failed", str(exc) or type(exc).__name__

    def _start_library(self) -> None:
        """`library.db` beside `workflow.db`; a failure here leaves the
        workflow running and the 片库 routes answering with the reason."""
        try:
            self.library = LibraryStore.open(self.data_dir / "library.db")
            self.library_scanner = LibraryScanner(self.library)
            self.library_scanner.start()
        except Exception as exc:
            log.exception("library index failed to start")
            if self.library is not None:
                self.library.close()
            self.library, self.library_scanner = None, None
            self.library_reason = f"片库启动失败：{exc}"

    def stop(self, timeout: float = 60.0) -> None:
        """Stop everything; keep the store open unless every user stopped (R5).

        Closing waits on the store lock that a stuck intake stopper may hold,
        which would hang the agent's shutdown. Process exit still releases the
        store lock and closes the kill-on-close Jobs.
        """
        deadline = time.monotonic() + timeout
        self._stopping.set()
        confirmed = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            confirmed &= not self._thread.is_alive()
        ticker = self._ticker
        if ticker is not None:
            ticker.join(timeout=max(0.0, deadline - time.monotonic()))
            # a ticker still inside the retry callback (outside the lock) or a
            # stalled tick owns the runtime: closing under it is unsafe
            confirmed &= not ticker.is_alive()
        # a ticker stalled inside the lock must not hang the whole shutdown; the
        # session and its child stay ours for a later stop() to retry
        if self._download_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            try:
                confirmed &= self._download.stop(min(timeout, DOWNLOAD_STOP_SECONDS))
            finally:
                self._download_lock.release()
        else:
            confirmed = False
        if self.library_scanner is not None:  # a scan in progress finishes first
            confirmed &= self.library_scanner.stop(
                timeout=max(1.0, deadline - time.monotonic())
            )
        if self.scheduler is not None:  # R13: before the dispatcher it wakes
            confirmed &= self.scheduler.stop(timeout=min(timeout, 30.0))
        if self.dispatcher is not None:
            confirmed &= self.dispatcher.stop(timeout=timeout)
        if self.lookup is not None:
            confirmed &= self.lookup.stop(timeout=min(timeout, 30.0))
        if self.intake is not None:
            try:
                confirmed &= bool(self.intake.stop(timeout=min(timeout, 30.0)))
            except Exception:
                log.exception("media workflow intake stop failed")
                confirmed = False
        if self.events is not None:
            # a small floor: a deadline the earlier steps spent must not make the
            # observer's join zero-length (a spurious unconfirmed stop)
            confirmed &= self.events.stop(timeout=max(1.0, deadline - time.monotonic()))
        if not confirmed:
            log.error("media workflow stop unconfirmed; store left open")
            self.state = "stop_unconfirmed"
            return
        if self.runtime is not None:
            self.runtime.close()
        if self.library is not None:
            self.library.close()
            self.library = None
        self.state = "stopped"

    def confirm(
        self,
        film_id: str,
        performer_id: str | None = None,
        target_dir: str | None = None,
    ) -> str:
        """确认入库 (F02-P1 C1); raises ConfirmError with the reason when refused."""
        if self.finalizer is None:
            raise RuntimeError("媒体流水线尚未启动")
        approval_id = self.finalizer.confirm(film_id, performer_id, target_dir)
        if self.dispatcher is not None:
            self.dispatcher.wake()
        return approval_id

    def plans(self) -> dict[str, Any]:
        """待处理 (U1 P3'): the unplanned films and their recommendations."""
        if self.runtime is None:
            raise RuntimeError("媒体流水线尚未启动")
        from .planning import plan_list

        return plan_list(self.runtime)

    def confirm_plans(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """确认入队: one result per item; a confirmed plan wakes the dispatcher."""
        if self.runtime is None:
            raise RuntimeError("媒体流水线尚未启动")
        from .planning import confirm_plans

        results = confirm_plans(self.runtime, items)
        if any(r["ok"] for r in results):
            if self.dispatcher is not None:
                self.dispatcher.wake()
            self._wake_scheduler()  # R13: a confirmed plan
        return results

    # ── routing (P2-3 R13) ──────────────────────────────────────────────
    def _wake_scheduler(self) -> None:
        if self.scheduler is not None:
            self.scheduler.wake()

    def _on_sources(self) -> None:
        """A film entered or left 片源丢失: route again, dispatch again."""
        self._wake_scheduler()
        if self.dispatcher is not None:
            self.dispatcher.wake()

    def _local_resolved(
        self, rt: LocalRuntime, job: m.PendingComputeJob, name: str, outcome: Any
    ) -> None:
        """The controller's own dispatcher resolved a stage: the #286 events,
        then a routing pass (a failed route closes; a finished one frees a
        worker's slot)."""
        if self.events is not None:
            self.events.resolved(rt, job, name, outcome)
        self._wake_scheduler()

    def _controller_free(self, rt: LocalRuntime) -> int | None:
        if self.free_bytes is not None:
            return self.free_bytes()
        from .transfer import disk_free

        try:
            return disk_free(rt.work_dir)
        except OSError:
            return None

    def pass_inputs(self) -> dict[str, Any] | None:
        """The routing pass's live inputs (P2-4: 待处理's preview reads the
        same ones); None while remote workers are off."""
        rt, fleet = self.runtime, self.fleet
        if rt is None or fleet is None:
            return None
        return {
            "live": fleet.snapshot(),
            "now": rt.clock.now(),
            "controller_id": rt.identity.worker_id,
            "controller_free_bytes": self._controller_free(rt),
            "live_since": fleet.started_at,
        }

    def _route_pass(self) -> None:
        """One `routing.run_pass` over the live fleet; a routed or withdrawn
        film wakes the controller's dispatcher (a hand-over or a local
        route)."""
        rt, inputs = self.runtime, self.pass_inputs()
        if rt is None or inputs is None:
            return
        from .routing import run_pass

        # P3 #304 IR1: the pass looks at the sources itself, whether or not
        # the dispatcher is paused or busy with a stage
        hold: frozenset[str] = frozenset()
        if self.sources is not None:
            try:
                hold = self.sources.check_candidates()
            except Exception:
                log.exception("source check before the routing pass failed")
                hold = self.sources.ids()
        outcome = run_pass(
            rt.store,
            rt.operator,
            rt.meta,
            live=inputs["live"],
            now=inputs["now"],
            controller_id=inputs["controller_id"],
            executors=rt.executors,
            controller_free_bytes=inputs["controller_free_bytes"],
            remote_enabled=True,
            live_since=inputs["live_since"],
            hold=hold,
        )
        self.last_pass = outcome.planned
        if (outcome.routed or outcome.withdrawn) and self.dispatcher is not None:
            self.dispatcher.wake()

    def requeue(self, film_id: str, attempt_id: str) -> None:
        """停止并重新排队 (P2-3 R9a); WorkflowError when it is not one."""
        if self.runtime is None:
            raise RuntimeError("媒体流水线尚未启动")
        from .remote_attempts import requeue

        requeue(self.runtime, self.cancels, film_id, attempt_id, fleet=self.fleet)

    def remote_resolved(
        self,
        rt: LocalRuntime,
        job: m.PendingComputeJob,
        name: str,
        outcome: Any,
    ) -> None:
        """A remote finish resolved (P2-3 §2 (5)): the 日志 row and the #286
        events as for a local stage; the worker API then wakes dispatch."""
        from . import board

        try:
            name = board.name_of(rt, job.film_id) or name
        except Exception:
            log.exception("media workflow film name unavailable")
        if outcome.error_class != "cancelled":
            try:
                attempt = next(
                    a
                    for a in rt.store.list_attempts(rt.operator, job.job_id)
                    if a.status in ("succeeded", "failed")
                    and a.ended_at is not None
                    and a.attempt_id
                    == rt.store.get_stage(rt.operator, job.job_id).attempt_id
                )
                ended = attempt.ended_at
                assert ended is not None
                duration = (ended - attempt.started_at).total_seconds()
                self.workflow_log.stage(
                    name,
                    job.stage,
                    ok=outcome.ok,
                    duration=duration,
                    note=outcome.message,
                )
            except Exception:  # the log never changes the outcome
                log.exception("media workflow log row failed")
        if self.events is not None:
            try:
                self.events.resolved(rt, job, name, outcome)
            except Exception:
                log.exception("media workflow event hook failed")

    def retry(self, film_id: str, stage: str) -> None:
        """Requeue a failed local stage (T06-P1 D05); WorkflowError when it is not one."""
        if self.runtime is None or self.dispatcher is None:
            raise RuntimeError("媒体流水线尚未启动")
        retry_failed_stage(self.runtime, film_id, stage)
        self.dispatcher.wake()

    # ── LTX model download (H3 M1, D02, D10) ────────────────────────────
    def _missing_models(self) -> tuple[Path, list[str], str] | None:
        """The restore executor's cached preflight result, rechecked on disk."""
        rt = self.runtime
        restore = rt.executors.get("restore") if rt is not None else None
        check = getattr(restore, "missing_models", None)
        return check() if callable(check) else None

    def ltx_download(self) -> dict[str, Any] | None:
        """The download session for the 修复 step. A restore that stopped on
        missing models is offered (`awaiting`); nothing downloads before the
        operator approves it through `respond_ltx_download`."""
        with self._download_lock:
            session = self._download
            if session.child is None and session.status.get("state") not in (
                _DOWNLOAD_BUSY
            ):
                missing = self._missing_models()
                if missing is None:
                    session.stop(0)  # all there now: withdraw a stale offer
                elif not session.status or session.status["files"] != missing[1]:
                    session.request(*missing)
            return dict(session.status) or None

    def respond_ltx_download(self, request_id: str, approved: bool) -> bool:
        """The operator's 下载模型 click; False for a stale or a second click."""
        with self._download_lock:
            if self._stopping.is_set():
                return False
            if not self._download.respond(request_id, approved):
                return False
            if approved and (self._ticker is None or not self._ticker.is_alive()):
                self._ticker = threading.Thread(
                    target=self._tick_download,
                    name="workflow-ltx-download",
                    daemon=True,
                )
                self._ticker.start()
            return True

    def stop_ltx_download(self) -> None:
        """Stop the download; RuntimeError while its worker is still running."""
        with self._download_lock:
            if not self._download.stop(DOWNLOAD_STOP_SECONDS):
                raise RuntimeError("模型下载进程仍在停止")

    def _tick_download(self) -> None:
        """Advance the download until it ends; then retry the waiting restores."""
        while not self._stopping.is_set():
            with self._download_lock:
                try:
                    done = self._download.tick()
                except Exception:
                    # a dying thread would keep the destination claim held
                    log.exception("media workflow model download tick failed")
                    done = False
                busy = (
                    self._download.child is not None
                    or self._download.status.get("state") in _DOWNLOAD_BUSY
                )
                if not done and not busy:
                    self._ticker = None
                    return
            if done:
                self._downloaded()
            self._stopping.wait(DOWNLOAD_TICK_SECONDS)

    def _downloaded(self) -> None:
        """D01: a verified download retries the restores that lacked the models;
        a film whose tier still lacks files waits (F1): the next read offers it."""
        rt, dispatcher = self.runtime, self.dispatcher
        if rt is None:
            return
        try:
            retry_missing_models(rt)
        except Exception:
            log.exception("media workflow model retry failed")
        if dispatcher is not None:
            dispatcher.wake()

    def status(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "state": self.state,
            "reason": self.reason,
            # the snapshot copy in progress, for the 入库扫描 step (H2 G1)
            "intake": self.intake.progress if self.intake is not None else None,
            "ltx_download": self.ltx_download(),
        }
        if self.dispatcher is not None:
            dispatch = self.dispatcher.status()
            value["dispatch"] = {
                "state": dispatch.state,
                "reason": dispatch.reason,
                "current": dispatch.current,
                "waiting": dispatch.waiting,
                "paused": dispatch.paused,
            }
        return value


def start_local_workflow(
    settings: WorkflowSettings,
    config_dir: Path | None,
    executors: Sequence[StageExecutor] = (),
    *,
    task_log: TaskLogSink | None = None,
    event_queue: EventQueue | None = None,
) -> LocalWorkflowHost | None:
    """None unless the operator enabled the workflow and named its INBOX (C7).
    `task_log`: the agent's task log, for the workflow's 日志 rows (U1e)."""
    if not settings.enabled or not settings.inbox_root:
        return None
    if settings.data_dir:
        data_dir = Path(settings.data_dir)
    elif config_dir is not None:
        data_dir = config_dir / "workflow"
    else:
        return None
    if executors:
        host = LocalWorkflowHost(
            settings, data_dir, executors, task_log=task_log, event_queue=event_queue
        )
    else:  # the shipped pipeline (T04 E9): tools from agent.yaml, local return
        from .pipeline import (
            MIKO_OFF,
            default_executors,
            default_lookup,
            default_scrape_step,
        )
        from .return_step import LocalReturnStep

        host = LocalWorkflowHost(
            settings,
            data_dir,
            executor_factory=default_executors(settings),
            return_step=LocalReturnStep(),
            scrape_step=default_scrape_step(settings),
            off_reasons={"scrape": MIKO_OFF},
            lookup_factory=default_lookup(settings),
            task_log=task_log,
            event_queue=event_queue,
        )
    host.start()
    return host
