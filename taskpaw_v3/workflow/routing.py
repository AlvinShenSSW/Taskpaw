"""P2-1 (#292): apply one scheduler pass to the store.

The scheduler is pure; this is its only writer. Withdrawals go first so a
withdrawn film can be routed again in the same pass. Every write is a CAS'd
operator command: a decision that lost its race (the film was routed, claimed
or re-pinned meanwhile, the worker drained) is skipped and decided again by
the next pass. The route decisions count the slots and staging the pass's
withdrawals free, so after a lost withdrawal none of them is applied: the pass
decides once more from a fresh snapshot, and after a second loss leaves the
routing to the next pass (CX4). The pass's own thread and triggers come with
remote execution (P2-3): `RoutingThread`.
"""

from __future__ import annotations

import logging
import threading
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from . import models as m
from .auth import AuthProof
from .executors import ComputeStage, StageExecutor, needs_gpu
from .scheduler import RouteDecision, SchedulePass, Withdrawal, WorkerLive, schedule

if TYPE_CHECKING:
    from .store import WorkflowStore

log = logging.getLogger("taskpaw.workflow.routing")

_GPU_STAGES: tuple[ComputeStage, ...] = ("restore", "asr")
# Codes of a decision overtaken between the snapshot and its command.
_RACED = frozenset(
    {
        "cas_conflict",
        "route_exists",
        "route_claimed",
        "precondition_failed",
        "not_found",
        "source_not_admissible",
    }
)


@dataclass(frozen=True)
class PassOutcome:
    planned: SchedulePass
    routed: tuple[RouteDecision, ...]
    withdrawn: tuple[Withdrawal, ...]
    # P2-3 §11: (film, code) of each decision a tolerated race overtook
    overtaken: tuple[tuple[str, str], ...] = ()


def run_pass(
    store: WorkflowStore,
    operator: AuthProof,
    meta: Callable[[], m.CommandMeta],
    *,
    controller_id: str,
    live: Mapping[str, WorkerLive],
    now: datetime,
    executors: Mapping[ComputeStage, StageExecutor],
    controller_free_bytes: int | None = None,
    retry: bool = True,
    remote_enabled: bool = False,
    live_since: datetime | None = None,
) -> PassOutcome:
    """`executors`: the controller's own, so the pass decides which stages
    need a GPU worker by the dispatcher's rule (IR3). `remote_enabled`: the
    `workflow.remote_workers` setting (P2-2 IR1)."""
    snapshot = store.routing_snapshot(operator)
    planned = schedule(
        snapshot,
        live=live,
        now=now,
        controller_id=controller_id,
        controller_free_bytes=controller_free_bytes,
        gpu_stages=gpu_stages(store, operator, snapshot, executors),
        remote_enabled=remote_enabled,
        live_since=live_since,
    )
    withdrawn = []
    lost = False
    for item in planned.withdrawals:
        try:
            store.withdraw_route(
                operator,
                meta(),
                m.RouteWithdraw(
                    route_id=item.route_id,
                    expected_version=item.version,
                    reason=item.reason,
                ),
            )
        except m.WorkflowError as exc:
            if exc.code not in _RACED:
                raise
            log.info(
                "route withdrawal of %s overtaken (code=%s)", item.film_id, exc.code
            )
            lost = True
            continue
        withdrawn.append(item)
    if lost:  # CX4: the route decisions counted its slot and staging as freed
        if not retry:
            return PassOutcome(planned, (), tuple(withdrawn))
        again = run_pass(
            store,
            operator,
            meta,
            controller_id=controller_id,
            live=live,
            now=now,
            executors=executors,
            controller_free_bytes=controller_free_bytes,
            retry=False,
            remote_enabled=remote_enabled,
            live_since=live_since,
        )
        return PassOutcome(
            again.planned,
            again.routed,
            (*withdrawn, *again.withdrawn),
            again.overtaken,
        )
    routed, overtaken = [], []
    for decision in planned.routes:
        try:
            store.route_film(
                operator,
                meta(),
                m.RouteOpen(
                    route_id=str(uuid.uuid4()),
                    film_id=decision.film_id,
                    input_revision=decision.input_revision,
                    worker_id=decision.worker_id,
                    kind=decision.kind,
                    expected_pin_version=decision.pin_version,
                    reason=decision.reason,
                ),
            )
        except m.WorkflowError as exc:
            if exc.code not in _RACED:
                raise
            log.info("route of %s overtaken (code=%s)", decision.film_id, exc.code)
            overtaken.append((decision.film_id, exc.code))
            continue
        routed.append(decision)
    return PassOutcome(planned, tuple(routed), tuple(withdrawn), tuple(overtaken))


def gpu_stages(
    store: WorkflowStore,
    operator: AuthProof,
    snapshot: m.RoutingSnapshot,
    executors: Mapping[ComputeStage, StageExecutor],
) -> dict[tuple[str, int], frozenset[str]]:
    """Per film revision, its restore and ASR that need a GPU worker:
    `needs_gpu`, the controller's dispatcher's own rule, over that revision's
    input manifest (IR3); an open route's older revision too (CX5)."""
    result: dict[tuple[str, int], frozenset[str]] = {}
    for film in (*snapshot.older_revisions, *snapshot.films):
        manifest = store.get_input_manifest(operator, film.film_id, film.input_revision)
        result[film.film_id, film.input_revision] = frozenset(
            stage for stage in _GPU_STAGES if needs_gpu(executors.get(stage), manifest)
        )
    return result


Wait = Callable[[threading.Event, float], bool]


def _wait(event: threading.Event, timeout: float) -> bool:
    return event.wait(timeout)


class RoutingThread:
    """P2-3 R13: the controller's routing pass on its own thread — at start,
    on every `wake()` (plan confirm, any resolve, a FleetLive change) and
    every `interval` seconds. Wakes during a pass coalesce into one more pass;
    `stop()` joins the thread. `wait` is the injectable timer (tests)."""

    def __init__(
        self,
        run: Callable[[], object],
        *,
        interval: float = 30.0,
        wait: Wait = _wait,
        name: str = "workflow-routing",
    ) -> None:
        self._run, self.interval, self._wait_for = run, interval, wait
        self._name = name
        self._wake, self._stop = threading.Event(), threading.Event()
        self._thread: threading.Thread | None = None
        self.passes = 0
        self._passed = threading.Condition()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, timeout: float = 30.0) -> bool:
        self._stop.set()
        self._wake.set()
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def wait_passes(self, count: int, timeout: float = 10.0) -> bool:
        """Until `count` passes ran (tests synchronize on passes, not time)."""
        with self._passed:
            return self._passed.wait_for(lambda: self.passes >= count, timeout)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._wake.clear()
            try:
                self._run()
            except Exception:  # a failed pass is retried by the next trigger
                log.exception("workflow routing pass failed")
            with self._passed:
                self.passes += 1
                self._passed.notify_all()
            if self._stop.is_set():
                return
            self._wait_for(self._wake, self.interval)
