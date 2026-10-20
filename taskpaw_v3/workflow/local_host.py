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
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from taskpaw_v3.core.config import WorkflowSettings

from . import intake_models as im
from . import models as m
from .executors import StageExecutor
from .intake_protocol import ProbeArtifact
from .local_dispatch import LocalDispatcher, ReturnStep
from .local_runtime import LocalRuntime, intake_profiles

ExecutorFactory = Callable[[LocalRuntime], Sequence[StageExecutor]]

log = logging.getLogger("taskpaw.workflow.host")

COMPUTE_STAGES: tuple[m.Stage, ...] = ("restore", "asr", "translate")
MANDATORY_STAGES: tuple[m.Stage, ...] = (
    "intake",
    "return",
    "scrape",
    "archive",
    "cleanup_input",
    "cleanup_workcopy",
)


def film_policy() -> m.FilmPolicy:
    return m.FilmPolicy(
        policy_revision=1,
        required_stages=(MANDATORY_STAGES[0], *COMPUTE_STAGES, *MANDATORY_STAGES[1:]),
        subtitle_requirement="chinese",
        required_image_roles=("poster",),
        folder_cover_required=False,
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
    wanted = im.IntakeRootPut(
        expected_version=0 if current is None else current.expected_version + 1,
        root_id=ident.root_id,
        native_identity=inspect_root(inbox, ident.root_id),
        private_path=str(inbox),
        work_root_id=ident.work_root_id,
        work_native_identity=inspect_root(rt.work_dir, ident.work_root_id),
        private_work_path=str(rt.work_dir),
        component_id=ident.intake_component_id,
        policy=policy or im.IntakePolicy(policy_revision=1),
        film_policy=film_policy(),
        profiles=intake_profiles(rt.evidence("intake_profile_approval")),
        enabled=True,
    )
    if current is None or not _same_root(current, wanted):
        rt.store.configure_intake_root(rt.operator, rt.meta(), wanted)


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
    ) -> None:
        self.settings, self.data_dir, self.executors = settings, data_dir, executors
        self.intake_policy = intake_policy
        self.executor_factory, self.return_step = executor_factory, return_step
        self.scrape_step = scrape_step
        self.state, self.reason = "starting", ""
        self.runtime: LocalRuntime | None = None
        self.intake: Any = None
        self.dispatcher: LocalDispatcher | None = None
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._boot, name="workflow-host", daemon=True
        )
        self._thread.start()

    def _boot(self) -> None:
        try:
            rt = self.runtime = LocalRuntime(self.data_dir, self.executors)
            if self.executor_factory is not None:  # executors that read the ledger
                rt.executors = {e.stage: e for e in self.executor_factory(rt)}
            rt.open()
            rt.ensure_worker()
            rt.start_boot()
            _, intake_auth = rt.grant_intake_adapter()
            rt.settle_intake()
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
            self.dispatcher = LocalDispatcher(
                rt, return_step=self.return_step, scrape_step=self.scrape_step
            )
            if self._stopping.is_set():
                return
            self.dispatcher.start()
            self.state, self.reason = "running", rt.reason
        except Exception as exc:  # never fatal to the agent (C4)
            log.exception("media workflow failed to start")
            self.state, self.reason = "failed", str(exc) or type(exc).__name__

    def stop(self, timeout: float = 60.0) -> None:
        """Stop everything; keep the store open unless every user stopped (R5).

        Closing waits on the store lock that a stuck intake stopper may hold,
        which would hang the agent's shutdown. Process exit still releases the
        store lock and closes the kill-on-close Jobs.
        """
        self._stopping.set()
        confirmed = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            confirmed &= not self._thread.is_alive()
        if self.dispatcher is not None:
            confirmed &= self.dispatcher.stop(timeout=timeout)
        if self.intake is not None:
            try:
                confirmed &= bool(self.intake.stop(timeout=min(timeout, 30.0)))
            except Exception:
                log.exception("media workflow intake stop failed")
                confirmed = False
        if not confirmed:
            log.error("media workflow stop unconfirmed; store left open")
            self.state = "stop_unconfirmed"
            return
        if self.runtime is not None:
            self.runtime.close()
        self.state = "stopped"

    def status(self) -> dict[str, Any]:
        value: dict[str, Any] = {"state": self.state, "reason": self.reason}
        if self.dispatcher is not None:
            dispatch = self.dispatcher.status()
            value["dispatch"] = {
                "state": dispatch.state,
                "reason": dispatch.reason,
                "current": dispatch.current,
                "waiting": dispatch.waiting,
            }
        return value


def start_local_workflow(
    settings: WorkflowSettings,
    config_dir: Path | None,
    executors: Sequence[StageExecutor] = (),
) -> LocalWorkflowHost | None:
    """None unless the operator enabled the workflow and named its INBOX (C7)."""
    if not settings.enabled or not settings.inbox_root:
        return None
    if settings.data_dir:
        data_dir = Path(settings.data_dir)
    elif config_dir is not None:
        data_dir = config_dir / "workflow"
    else:
        return None
    if executors:
        host = LocalWorkflowHost(settings, data_dir, executors)
    else:  # the shipped pipeline (T04 E9): tools from agent.yaml, local return
        from .pipeline import default_executors, default_scrape_step
        from .return_step import LocalReturnStep

        host = LocalWorkflowHost(
            settings,
            data_dir,
            executor_factory=default_executors(settings),
            return_step=LocalReturnStep(),
            scrape_step=default_scrape_step(settings),
        )
    host.start()
    return host
