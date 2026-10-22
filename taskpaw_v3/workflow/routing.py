"""P2-1 (#292): apply one scheduler pass to the store.

The scheduler is pure; this is its only writer. Withdrawals go first so a
withdrawn film can be routed again in the same pass. Every write is a CAS'd
operator command: a decision that lost its race (the film was routed, claimed
or re-pinned meanwhile, the worker drained) is skipped and decided again by
the next pass. The pass's own thread and triggers come with remote execution
(P2-3).
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from . import models as m
from .auth import AuthProof
from .scheduler import RouteDecision, SchedulePass, Withdrawal, WorkerLive, schedule

if TYPE_CHECKING:
    from .store import WorkflowStore

log = logging.getLogger("taskpaw.workflow.routing")

# Codes of a decision overtaken between the snapshot and its command.
_RACED = frozenset(
    {
        "cas_conflict",
        "route_exists",
        "route_claimed",
        "precondition_failed",
        "not_found",
    }
)
UNPIN_REASON = "本机不参与 GPU 阶段，改为自动"  # OD1


@dataclass(frozen=True)
class PassOutcome:
    planned: SchedulePass
    routed: tuple[RouteDecision, ...]
    withdrawn: tuple[Withdrawal, ...]


def run_pass(
    store: WorkflowStore,
    operator: AuthProof,
    meta: Callable[[], m.CommandMeta],
    *,
    controller_id: str,
    live: Mapping[str, WorkerLive],
    now: datetime,
    controller_free_bytes: int | None = None,
    gpu_stages: Mapping[str, frozenset[str]] | None = None,
) -> PassOutcome:
    planned = schedule(
        store.routing_snapshot(operator),
        live=live,
        now=now,
        controller_id=controller_id,
        controller_free_bytes=controller_free_bytes,
        gpu_stages=gpu_stages,
    )
    withdrawn = []
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
            log.info("route withdrawal of %s overtaken: %s", item.film_id, exc)
            continue
        withdrawn.append(item)
    routed = []
    for decision in planned.routes:
        try:
            pin_version = decision.pin_version
            if decision.unpin:
                store.set_pin(
                    operator,
                    meta(),
                    m.PinSet(
                        film_id=decision.film_id,
                        input_revision=decision.input_revision,
                        expected_version=pin_version,
                        worker_id=None,
                        reason=UNPIN_REASON,
                    ),
                )
                pin_version += 1
            store.route_film(
                operator,
                meta(),
                m.RouteOpen(
                    route_id=str(uuid.uuid4()),
                    film_id=decision.film_id,
                    input_revision=decision.input_revision,
                    worker_id=decision.worker_id,
                    kind=decision.kind,
                    expected_pin_version=pin_version,
                    reason=decision.reason,
                ),
            )
        except m.WorkflowError as exc:
            if exc.code not in _RACED:
                raise
            log.info("route of %s overtaken: %s", decision.film_id, exc)
            continue
        routed.append(decision)
    return PassOutcome(planned, tuple(routed), tuple(withdrawn))
