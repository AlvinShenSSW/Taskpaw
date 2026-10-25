"""Explicit local controller intake. Importing this module starts nothing."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import intake_discovery_models as dm
from . import intake_models as im
from . import models as m
from .auth import AuthProof
from .intake_discovery import DiscoveryUnresolved, observe_package
from .intake_files import _key
from .intake_protocol import ProbeArtifact
from .intake_runner import SnapshotRunner
from .intake_watch import DirectoryWatcher, DirtyBatch
from .store import WorkflowStore


@dataclass(frozen=True)
class IntakeServiceStatus:
    root_id: str
    running: bool
    scanning: bool
    reason: str
    operation_id: str | None
    retained_unknown: bool


@dataclass(frozen=True)
class _Stable:
    signature: tuple[str, int, int]
    since: float


class IntakeService:
    """One scanner and at most one snapshot owner, both outside request loops.

    All configuration and credentials are supplied by trusted local assembly.
    This object neither opens a store nor mounts Hub/Agent or supplies defaults.
    A blocking native discovery call remains owned by its unjoined scanner;
    timeout never turns that thread into an exit proof.
    """

    def __init__(
        self,
        store: WorkflowStore,
        auth: AuthProof,
        *,
        root_id: str,
        meta: Callable[[], m.CommandMeta],
        owner_boot_id: str,
        ffprobe: ProbeArtifact | None,
        controller: bool,
        enabled: bool,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if controller is not True or enabled is not True:
            raise m.WorkflowError("controller_required")
        self.store, self.auth, self.root_id = store, auth, root_id
        self.meta, self.owner_boot_id = meta, owner_boot_id
        self.clock, self.monotonic = clock, monotonic
        self.config = store.get_intake_configuration(auth, root_id)
        if not self.config.enabled:
            raise m.WorkflowError("intake_disabled")
        self._runner = (
            SnapshotRunner(
                store,
                auth,
                meta=meta,
                owner_boot_id=owner_boot_id,
                ffprobe=ffprobe,
                clock=self.clock,
            )
            if ffprobe
            else None
        )
        self._watcher: DirectoryWatcher | None = None
        self._thread: threading.Thread | None = None
        self._worker: threading.Thread | None = None
        self._stopper: threading.Thread | None = None
        self._stop_ok = False
        self._stop = threading.Event()
        self._scan_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._scan_done = threading.Event()
        self._scan_done.set()
        self._stable: dict[str, _Stable] = {}
        self._linked_tokens: dict[tuple[str, str], str] = {}
        self._pending_owners: list[object] = []
        self._reason = "observed"
        self._scanning = False
        self._last_scan = -math.inf
        self._started = False

    @property
    def progress(self) -> dict[str, Any] | None:
        """The snapshot copy in progress, if any (H2 G1); a lock-free hint."""
        return self._runner.progress if self._runner else None

    def status(self) -> IntakeServiceStatus:
        with self._state_lock:
            alive = bool(self._thread and self._thread.is_alive())
            worker_alive = bool(self._worker and self._worker.is_alive())
            stopper_alive = bool(self._stopper and self._stopper.is_alive())
            operation = self._runner.active_operation_id if self._runner else None
            unknown = bool(self._pending_owners or operation and not worker_alive)
            if self._stop.is_set():
                unknown |= (
                    alive
                    or worker_alive
                    or stopper_alive
                    or self._scanning
                    or self._stopper is not None
                    and not self._stop_ok
                    or bool(self._watcher and not self._watcher.closed)
                )
            return IntakeServiceStatus(
                self.root_id,
                alive or worker_alive or stopper_alive or self._scanning,
                self._scanning,
                self._reason,
                operation,
                unknown,
            )

    def _pages(self) -> Iterator[im.IntakeView]:
        cursor = None
        while True:
            page = self.store.list_intakes(
                self.auth,
                im.IntakeListQuery(
                    root_id=self.root_id, after_package_id=cursor, limit=200
                ),
                current_only=True,
            )
            yield from page.items
            if page.next_package_id is None:
                return
            cursor = page.next_package_id

    def _begin_scan(self, trigger: str) -> dm.IntakeRootScanState:
        old = self.store.get_intake_root_scan(self.auth, self.root_id)
        command = dm.IntakeRootScanBegin.model_validate(
            dict(
                root_id=self.root_id,
                root_config_revision=self.config.expected_version + 1,
                expected_version=old.version if old else 0,
                scan_id=str(uuid4()),
                observed_at=self.clock(),
                trigger=trigger,
            )
        )
        self.store.begin_intake_root_scan(self.auth, self.meta(), command)
        value = self.store.get_intake_root_scan(self.auth, self.root_id)
        assert value is not None
        return value

    def _deleted(self, previous: im.IntakeView | None) -> dict[str, Any]:
        """Discovery's view of films the operator deleted (#304 IR3): their ids
        are not reused, and a package that was missing and is back is a new drop."""
        if previous is None:
            return {}
        return {
            "removed": self.store.removed_intake_films(self.auth, previous.package_id),
            "reappeared": previous.state == "missing",
        }

    def _notice(self, item: im.IntakeView, *, missing: bool, trigger: str) -> None:
        command = dm.IntakeSourceNotice.model_validate(
            dict(
                expected_version=item.version,
                package_id=item.package_id,
                root_config_revision=item.root_config_revision,
                source_generation=item.source_generation,
                observed_at=self.clock(),
                kind="missing" if missing else "dirty",
                trigger=trigger,
            )
        )
        self.store.record_intake_source_notice(self.auth, self.meta(), command)
        with self._state_lock:
            self._stable.pop(item.package_id, None)

    def _dirty(self, batch: DirtyBatch) -> None:
        for item in self._pages():
            path = item.relative_path.casefold()
            if batch.full_rescan or any(
                v.casefold() == path
                or v.casefold().startswith(path + "/")
                or path.startswith(v.casefold() + "/")
                for v in batch.paths
            ):
                if self._owns_snapshot(item):
                    continue
                snapshot = self.store.get_intake_snapshot(self.auth, item.package_id)
                if snapshot and any(member.linked for member in snapshot.members):
                    previous = self.store.get_intake_observation(
                        self.auth, item.package_id
                    )
                    observed = self._observe(
                        item.relative_path, item, snapshot=snapshot
                    )
                    if (
                        observed.coverage == "complete"
                        and observed.writer_state == "clear"
                        and observed.observation_digest == previous.observation_digest
                        and observed.package_native_identity is not None
                        and previous.package_native_identity is not None
                        and _key(observed.package_native_identity)
                        == _key(previous.package_native_identity)
                    ):
                        continue
                self._notice(
                    item,
                    missing=False,
                    trigger="overflow" if batch.full_rescan else "notification",
                )

    def _owns_snapshot(self, item: im.IntakeView) -> bool:
        return bool(
            item.operation_id
            and item.state != "unknown_owner"
            and self._runner
            and self._runner.active_operation_id == item.operation_id
            and self._worker
            and self._worker.is_alive()
        )

    def _observe(
        self,
        relative_path: str,
        previous: im.IntakeView | None,
        *,
        snapshot: im.PackageSnapshot | None = None,
    ) -> im.IntakeObservation:
        return observe_package(
            self.config,
            relative_path,
            previous=self.store.get_intake_observation(self.auth, previous.package_id)
            if previous
            else None,
            package_version=previous.version if previous else 0,
            observed_at=self.clock(),
            trigger="rescan",
            snapshot=snapshot
            or (
                self.store.get_intake_snapshot(self.auth, previous.package_id)
                if previous
                else None
            ),
            linked_tokens=self._linked_tokens,
            **self._deleted(previous),
        )

    def _mark_seen(self, view: im.IntakeView, scan: dm.IntakeRootScanState) -> None:
        self.store.mark_intake_scan_seen(
            self.auth,
            self.meta(),
            dm.IntakeScanSeen(
                expected_version=view.version,
                package_id=view.package_id,
                root_id=self.root_id,
                root_config_revision=scan.root_config_revision,
                scan_id=scan.scan_id,
            ),
        )

    def _remember(self, observation: im.IntakeObservation, view: im.IntakeView) -> None:
        with self._state_lock:
            old = self._stable.get(view.package_id)
            signature = (
                observation.observation_digest,
                view.source_generation,
                view.root_config_revision,
            )
            if (
                view.state != "waiting_stability"
                or observation.coverage != "complete"
                or observation.writer_state != "clear"
            ):
                self._stable.pop(view.package_id, None)
            elif old and old.signature != signature:
                # Let other packages take this bounded slot before a repeatedly
                # changing input can reserve it again on the following scan.
                self._stable.pop(view.package_id, None)
            elif (
                old is None
                and len(self._stable) < self.config.policy.max_dirty_packages
            ):
                self._stable[view.package_id] = _Stable(signature, self.monotonic())

    def scan_once(
        self, *, trigger: str = "rescan", batch: DirtyBatch | None = None
    ) -> bool:
        """One bounded public scan, also available to explicit local orchestration."""
        from . import intake_walk

        with self._state_lock:
            if self._stop.is_set() or not self._scan_lock.acquire(blocking=False):
                return False
            # Direct public scans have the same owned lifetime as the scanner
            # thread. Register before stop can observe a quiescent service.
            self._scanning = True
            self._scan_done.clear()
        scan = None
        complete = False
        reason: im.Reason = "partial_observation"
        try:
            current = self.store.get_intake_configuration(self.auth, self.root_id)
            if current != self.config:
                raise m.WorkflowError("binding_changed")
            scan = self._begin_scan(trigger)
            if batch and (batch.paths or batch.full_rescan):
                self._dirty(batch)
            walk = intake_walk.walk_root(
                self.config, deadline=time.monotonic() + 30, cancelled=self._stop.is_set
            )
            for candidate in walk.candidates:
                if self._stop.is_set():
                    break
                previous = self.store.find_intake(
                    self.auth,
                    dm.IntakeLookup(
                        root_id=self.root_id,
                        relative_path=candidate.relative_path,
                        native_identity=candidate.native_identity,
                    ),
                )
                if previous and self._owns_snapshot(previous):
                    self._mark_seen(previous, scan)
                    continue
                observation = self._observe(candidate.relative_path, previous)
                recorded = self.store.observe_intake(
                    self.auth, self.meta(), observation
                )
                if recorded.entity_id != observation.package_id:
                    # Another discovery already owns the physical directory.
                    # Retain its established film/member identities on reread.
                    previous = self.store.get_intake(self.auth, recorded.entity_id)
                    observation = self._observe(candidate.relative_path, previous)
                    recorded = self.store.observe_intake(
                        self.auth, self.meta(), observation
                    )
                view = self.store.get_intake(self.auth, recorded.entity_id)
                self._mark_seen(view, scan)
                self._remember(observation, view)
            complete = walk.coverage == "complete" and not self._stop.is_set()
            if self._watcher:
                later = self._watcher.drain()
                if later.paths or later.full_rescan:
                    self._dirty(later)
                    complete = False
            if complete:
                cursor = None
                while True:
                    page = self.store.list_intake_unseen(
                        self.auth, self.root_id, scan.scan_id, after_package_id=cursor
                    )
                    for item in page.items:
                        if item.state != "missing":
                            self._notice(item, missing=True, trigger="complete_rescan")
                    if page.next_package_id is None:
                        break
                    cursor = page.next_package_id
                reason = "observed"
            elif "limit_exceeded" in walk.reasons:
                reason = "limit_exceeded"
        except DiscoveryUnresolved as exc:
            self._pending_owners.append(exc.pending_owner)
            reason = "owner_unknown"
            self._stop.set()
        except intake_walk.WalkUnresolved as exc:
            self._pending_owners.append(exc.pending_owner)
            reason = "owner_unknown"
            self._stop.set()
        except (OSError, ValueError, RuntimeError):
            complete = False
            reason = "partial_observation"
        finally:
            if scan is not None:
                try:
                    self.store.finish_intake_root_scan(
                        self.auth,
                        self.meta(),
                        dm.IntakeRootScanFinish(
                            expected_version=scan.version,
                            root_id=self.root_id,
                            root_config_revision=scan.root_config_revision,
                            scan_id=scan.scan_id,
                            observed_at=self.clock(),
                            complete=complete,
                            reason=reason,
                        ),
                    )
                except (OSError, ValueError, RuntimeError):
                    complete = False
                    reason = "recovery_required"
            with self._state_lock:
                self._scanning = False
                self._reason = reason
                self._last_scan = self.monotonic()
                self._scan_lock.release()
                self._scan_done.set()
        return complete

    def ready_packages(self) -> tuple[str, ...]:
        scan = self.store.get_intake_root_scan(self.auth, self.root_id)
        if self._stop.is_set() or scan is None or scan.required:
            return ()
        now = self.monotonic()
        with self._state_lock:
            candidates = tuple(self._stable.items())
        ready = []
        for key, value in candidates:
            view = self.store.get_intake(self.auth, key)
            observation = self.store.get_intake_observation(self.auth, key)
            signature = (
                observation.observation_digest,
                view.source_generation,
                view.root_config_revision,
            )
            if view.state != "waiting_stability" or signature != value.signature:
                with self._state_lock:
                    if self._stable.get(key) == value:
                        self._stable.pop(key, None)
            elif now - value.since >= self.config.policy.stable_seconds:
                ready.append(key)
        return tuple(ready)

    def run_ready_once(self) -> im.IntakeOperationView | None:
        if self._runner is not None and self._runner.has_pending_settlement:
            return self._runner.retry_settlement()
        candidates = self.ready_packages()
        if not candidates:
            return None
        if self._runner is None:
            self._reason = "tool_missing"
            return None
        if self._runner.active_operation_id is not None:
            return None
        try:
            return self._runner.run(candidates[0])
        finally:
            with self._state_lock:
                self._stable.pop(candidates[0], None)

    def _snapshot(self) -> None:
        try:
            self.run_ready_once()
        except (OSError, ValueError, RuntimeError):
            self._reason = "recovery_required"

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                assert self._watcher is not None
                batch = self._watcher.drain()
                due = (
                    self.monotonic() - self._last_scan
                    >= self.config.policy.rescan_seconds
                )
                if batch.paths or batch.full_rescan or due:
                    self.scan_once(
                        trigger="overflow"
                        if batch.full_rescan
                        else "notification"
                        if batch.paths
                        else "rescan",
                        batch=batch,
                    )
                if (
                    self._runner
                    and (self._worker is None or not self._worker.is_alive())
                    and (
                        self._runner.has_pending_settlement
                        or self.ready_packages()
                        and self._runner.active_operation_id is None
                    )
                ):
                    self._worker = threading.Thread(
                        target=self._snapshot,
                        name="taskpaw-intake-snapshot",
                        daemon=False,
                    )
                    self._worker.start()
            except (OSError, ValueError, RuntimeError):
                self._reason = "recovery_required"
                self._stop.set()
            self._stop.wait(0.25)

    def start(self) -> None:
        if self._started or self._stop.is_set():
            raise m.WorkflowError("service_already_started")
        self._started = True
        self._begin_scan("startup")
        self._watcher = DirectoryWatcher(
            Path(self.config.private_path),
            expected_root=self.config.native_identity,
            limit=self.config.policy.max_dirty_packages,
        )
        try:
            self._watcher.start()
            self._thread = threading.Thread(
                target=self._loop, name="taskpaw-intake-discovery", daemon=False
            )
            self._thread.start()
        except (OSError, ValueError, RuntimeError):
            self._reason = "owner_unknown"
            self._stop.set()
            raise

    def _request_stop(self) -> None:
        """Durable control can wait on a store/native lock; retain its own thread."""
        fence_ok = False
        for retry in (False, True):
            try:
                self._begin_scan("reconnect")
                fence_ok = True
            except m.WorkflowError as exc:
                # The fence raced the scan in flight (stop() already refuses
                # new ones) and that scan's begin/finish moved the root-scan
                # version first. Once it is done nothing else writes the row:
                # fence again. A scan that never returns keeps this thread,
                # and so the stop's unknown, retained.
                if exc.code == "cas_conflict" and not retry:
                    self._scan_done.wait()
                    continue
                self._reason = "recovery_required"
            except (OSError, ValueError, RuntimeError):
                self._reason = "recovery_required"
            break
        try:
            # A disabled/revised root may refuse the rescan fence. The existing
            # admitted native owner still needs its own durable cancel and stop.
            if self._runner:
                self._runner.stop()
            self._stop_ok = fence_ok
        except (OSError, ValueError, RuntimeError):
            self._reason = "recovery_required"

    def stop(self, *, timeout: float = 30) -> bool:
        if not math.isfinite(timeout) or not 0 <= timeout <= 30:
            raise ValueError("invalid_stop_timeout")
        end = time.monotonic() + timeout
        with self._state_lock:
            self._stop.set()
            if self._stopper is None:
                self._stopper = threading.Thread(
                    target=self._request_stop, name="taskpaw-intake-stop", daemon=False
                )
                self._stopper.start()
        okay = True
        if self._watcher:
            okay &= self._watcher.stop(timeout=max(0, end - time.monotonic()))
        okay &= self._scan_done.wait(max(0, end - time.monotonic()))
        for thread in (self._stopper, self._thread, self._worker):
            if thread is not None:
                thread.join(max(0, end - time.monotonic()))
                okay &= not thread.is_alive()
        return okay and not self.status().retained_unknown
