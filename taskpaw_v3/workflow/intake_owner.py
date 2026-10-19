"""One fixed intake helper, with a bounded census independent of Hub policy.

The returned object owns partial setup too. Callers retain it until a persisted
exit proof releases their slot. Child messages never substitute for HANDLEs.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import BinaryIO, Literal, cast

from taskpaw_v3.hub.server.upstream_worker import _WindowsAPI, _WindowsProcess

from . import intake_models as im
from . import intake_protocol as p
from .models import WorkflowError

_Failure = Literal[
    "open_failed",
    "identity_failed",
    "wait_failed",
    "close_failed",
    "query_failed",
    "budget_exceeded",
    "io_failed",
    "timeout",
    "ack_failed",
]


class _CreationAPI(_WindowsAPI):
    """Distinguish native FALSE from a lost result after successful creation."""

    def __init__(self) -> None:
        super().__init__()
        self.creation_attempted = False
        self.creation_failed = False
        self.creation_succeeded = False

    def create_process(
        self, argv: list[str], env: dict, handles: list[int]
    ) -> tuple[int, int, int, int]:
        self.creation_attempted = True
        try:
            result = super().create_process(argv, env, handles)
        except OSError:
            # _winapi returns no PROCESS_INFORMATION when CreateProcess fails.
            self.creation_failed = True
            raise
        self.creation_succeeded = True
        return result


def _new_process() -> _WindowsProcess:
    return _WindowsProcess(_CreationAPI())


def _helper_argv() -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "workflow-intake"]
    return [sys.executable, "-m", "taskpaw_v3.workflow.intake_worker"]


@dataclass
class _HeldBirth:
    birth: im.IntakeProcessBirth
    handle: int
    launcher: bool = False
    waited: bool = False
    closed: bool = False


class OwnedIntakeSnapshot:
    def __init__(self, request: p.IntakeWorkRequest) -> None:
        self.request = request
        self._condition = threading.Condition(threading.RLock())
        self._settle_lock = threading.Lock()
        self._proc: _WindowsProcess | None = None
        self._held: list[_HeldBirth] = []
        self._unidentified: list[int] = []
        self._failures: list[_Failure] = []
        self._total = self._active = 0
        self._resumed = self._stopping = self._terminal = self._eof = False
        self._no_process = self._closed = False
        self._owner_version = self._sequence = 0
        self._pending: im.ProbeBirthNotice | None = None
        self._notice_queued: im.ProbeBirthNotice | None = None
        self._notice_received_at: float | None = None
        self._probe: _HeldBirth | None = None
        self._probe_until: float | None = None
        self._events: deque[p.WorkerEvent] = deque()
        self._progress: p.ProgressEvent | None = None
        self._progress_counts: dict[str, int] = {}
        self._outbound: deque[p.ParentCommand] = deque()
        self._threads: list[threading.Thread] = []
        self._budget = p.TransportBudget()
        self._started = self._last_progress = time.monotonic()

    def _fault(self, code: _Failure) -> None:
        with self._condition:
            if code not in self._failures:
                self._failures.append(code)
            self._condition.notify_all()

    def _start(self) -> None:
        try:
            self._proc = _new_process()
            self._proc.start(_helper_argv(), os.environ.copy())
            proc = self._proc
            if proc.process is None or proc.job is None or not proc.assigned:
                raise OSError("unowned_helper")
            pid, created = proc.api.identity(proc.process, proc.job)
            if pid != proc.pid:
                raise OSError("helper_identity")
            birth = im.IntakeProcessBirth(
                pid=pid, creation_filetime_100ns=created, role="launcher"
            )
            self._held.append(_HeldBirth(birth, proc.process, launcher=True))
            self._total = 1  # The verified creation HANDLE is itself a lower bound.
            self._refresh()
        except (OSError, ValueError, RuntimeError, TypeError):
            failed_proc = self._proc
            api = failed_proc.api if failed_proc is not None else None
            self._no_process = failed_proc is None or (
                failed_proc.process is None
                and isinstance(api, _CreationAPI)
                and (not api.creation_attempted or api.creation_failed)
            )
            if not self._no_process:
                self._fault("identity_failed")
            self._events.append(
                p.FailureEvent(
                    operation_id=self.request.operation_id, reason="owner_unknown"
                )
            )

    def _refresh(self) -> None:
        proc = self._proc
        if proc is None or proc.job is None:
            return
        try:
            total, active = proc.api.accounting(proc.job)
            if (
                type(total) is not int
                or type(active) is not int
                or not self._total <= total <= 4 + len(self.request.video_ids)
                or not 0 <= active <= min(total, 5)
                or total < len(self._held)
            ):
                self._fault("budget_exceeded")
                return
            self._total, self._active = total, active
        except OSError:
            self._fault("query_failed")

    def owner_observation(self) -> im.IntakeOwner:
        with self._condition:
            self._refresh()
            # Do not census arbitrary PIDs here: a suspended probe's notice may
            # still be in transit. Persisted birth roles must never be rewritten.
            return im.IntakeOwner(
                owner_boot_id=self.request.admission.owner_boot_id,
                owner_id=self.request.owner_id,
                births=tuple(v.birth for v in self._held),
                observed_total=self._total,
                observed_active=self._active,
                identity_uncertain=bool(self._failures),
                failure_codes=tuple(self._failures),
                observed_at=datetime.now(timezone.utc),
            )

    def resume_helper(self, *, persisted_owner_version: int) -> None:
        with self._condition:
            self._admit_time()
            if (
                type(persisted_owner_version) is not int
                or persisted_owner_version < 1
                or self._resumed
                or self._stopping
                or self._failures
                or not self._held
            ):
                raise WorkflowError("intake_protocol")
            proc = self._proc
            assert proc is not None
            self._owner_version = persisted_owner_version
            self._outbound.append(
                p.WorkRequestEvent(
                    operation_id=self.request.operation_id, request=self.request
                )
            )
            try:
                proc.resume()
                self._resumed = True
                for target, name in (
                    (self._write, "intake-writer"),
                    (self._read, "intake-reader"),
                ):
                    thread = threading.Thread(target=target, name=name, daemon=False)
                    self._threads.append(thread)
                    thread.start()
            except (OSError, RuntimeError):
                self._fault("io_failed")
                self.request_stop()
                raise WorkflowError("intake_protocol") from None

    def _write(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdin is not None
        try:
            while True:
                with self._condition:
                    while (
                        not self._outbound
                        and not self._stopping
                        and not self._terminal
                        and not self._eof
                    ):
                        self._condition.wait()
                    if self._stopping or self._terminal or self._eof:
                        return
                    # Admission may have preceded a delayed writer schedule.
                    # Do not emit an old request/ACK after its native time bound.
                    self._admit_time()
                    command = self._outbound.popleft()
                p.write_frame(cast(BinaryIO, proc.stdin), command, budget=self._budget)
        except (OSError, ValueError, WorkflowError):
            if not self._stopping:
                self._fault("io_failed")
                self.request_stop()
        finally:
            try:
                proc.stdin.close()
            except OSError:
                self._fault("close_failed")

    def _read(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                event = p.read_frame(cast(BinaryIO, proc.stdout), budget=self._budget)
                if event is None:
                    with self._condition:
                        self._eof = True
                        self._condition.notify_all()
                    return
                if not isinstance(
                    event,
                    (p.ProgressEvent, p.ProbeEvent, p.ResultEvent, p.FailureEvent),
                ):
                    raise WorkflowError("intake_protocol")
                self._receive(event)
        except (OSError, ValueError, WorkflowError):
            if not self._stopping:
                self._fault("io_failed")
                self.request_stop()
        finally:
            try:
                proc.stdout.close()
            except OSError:
                self._fault("close_failed")

    def _receive(self, event: p.WorkerEvent) -> None:
        with self._condition:
            if self._stopping:
                return
            if event.operation_id != self.request.operation_id or self._terminal:
                raise WorkflowError("intake_protocol")
            if isinstance(event, p.ProgressEvent):
                self._admit_time()
                if self._probe_until is not None:
                    # A byte frame cannot replace native probe completion.
                    raise WorkflowError("intake_protocol")
                if event.member_id not in {
                    m.member_id for m in self.request.observation.members
                }:
                    raise WorkflowError("intake_protocol")
                previous = self._progress_counts.get(event.phase, 0)
                if event.bytes_completed < previous:
                    raise WorkflowError("intake_protocol")
                if event.bytes_completed > previous:
                    self._last_progress = time.monotonic()
                self._progress_counts[event.phase] = event.bytes_completed
                self._progress = (
                    event  # One latest cumulative observation, no pipe backpressure.
                )
            else:
                if len(self._events) >= p.QUEUE_ITEMS:
                    raise WorkflowError("intake_limit")
                if isinstance(event, p.ProbeEvent):
                    if self._notice_queued is not None or self._pending is not None:
                        raise WorkflowError("intake_protocol")
                    self._notice_queued = event.notice
                    self._notice_received_at = time.monotonic()
                else:
                    if self._pending is not None or self._notice_queued is not None:
                        raise WorkflowError("intake_protocol")
                    if isinstance(event, p.ResultEvent) and (
                        self._sequence != len(self.request.video_ids)
                        or event.snapshot.admission != self.request.admission
                    ):
                        raise WorkflowError("intake_protocol")
                    self._terminal = True
                    if (
                        isinstance(event, p.FailureEvent)
                        and event.reason == "owner_unknown"
                    ):
                        # Child native failures are negative ownership facts,
                        # never erased by a later empty parent Job. Keep the
                        # exact terminal reason and a bounded existing code.
                        self._fault("io_failed")
                self._events.append(event)
            self._condition.notify_all()

    def next_event(self, *, deadline: float) -> p.WorkerEvent:
        with self._condition:
            while True:
                terminal = bool(self._events) and isinstance(
                    self._events[0], (p.ResultEvent, p.FailureEvent)
                )
                try:
                    end = self._admit_time(deadline=deadline)
                except WorkflowError:
                    if terminal:
                        return self._events.popleft()  # Preserve an occurred fact.
                    raise
                if self._events:
                    return self._events.popleft()
                if self._failures:
                    raise WorkflowError("unknown_owner")
                if self._progress is not None:
                    event, self._progress = self._progress, None
                    return event
                if self._eof or self._stopping:
                    raise WorkflowError("intake_protocol")
                remaining = end - time.monotonic()
                if remaining <= 0:
                    self._fault("timeout")
                    self.request_stop()
                    raise WorkflowError("intake_timeout")
                self._condition.wait(remaining)

    def _advance_probe_phase(self) -> None:
        """Only an actual signaled, waited birth ends the final probe phase."""
        held, proc = self._probe, self._proc
        if held is None or proc is None or held.closed:
            return
        try:
            if not held.waited and proc.api.poll(held.handle) is not None:
                proc.api.wait(held.handle, 0)
                held.waited = True
            if (
                held.waited
                and self._sequence == len(self.request.video_ids)
                and self._pending is None
            ):
                self._probe = None
                self._probe_until = None
                self._last_progress = time.monotonic()
        except (OSError, subprocess.TimeoutExpired):
            self._fault("wait_failed")

    def _admit_time(self, *, deadline: float = float("inf")) -> float:
        """Hard overall bound never resets; byte-idle applies outside probes."""
        now = time.monotonic()
        overall = self._started + self.request.root.policy.operation_deadline_seconds
        # Check the active phase before native completion can reset byte-idle.
        phase_end = (
            self._probe_until
            if self._probe_until is not None
            else self._last_progress + self.request.root.policy.idle_deadline_seconds
        )
        if now >= min(deadline, overall, phase_end):
            self._fault("timeout")
            self.request_stop()
            raise WorkflowError("intake_timeout")
        self._advance_probe_phase()
        if self._failures:
            raise WorkflowError("unknown_owner")
        phase_end = (
            self._probe_until
            if self._probe_until is not None
            else self._last_progress + self.request.root.policy.idle_deadline_seconds
        )
        return min(deadline, overall, phase_end)

    def _open(self, pid: int, expected: im.IntakeProcessBirth | None = None) -> None:
        proc = self._proc
        assert proc is not None and proc.job is not None
        if len(self._held) + len(self._unidentified) >= 4 + len(self.request.video_ids):
            self._fault("budget_exceeded")
            return
        try:
            handle = proc.api.open_process(pid)
        except OSError:
            self._fault("open_failed")
            return
        self._unidentified.append(handle)  # Own the native handle before validation.
        try:
            actual_pid, created = proc.api.identity(handle, proc.job)
            if actual_pid != pid:
                raise ValueError("pid")
            if expected is not None:
                birth = expected
                if expected.creation_filetime_100ns != created:
                    raise ValueError("birth")
            else:
                birth = im.IntakeProcessBirth(
                    pid=pid, creation_filetime_100ns=created, role="member"
                )
            previous = next(
                (
                    v
                    for v in self._held
                    if (v.birth.pid, v.birth.creation_filetime_100ns) == (pid, created)
                ),
                None,
            )
            if previous is not None:
                if expected is not None and previous.birth != expected:
                    raise ValueError("role")
                proc.api.close(handle)
            else:
                if len(self._held) + 1 > self._total:
                    self._fault("budget_exceeded")
                    return
                self._held.append(_HeldBirth(birth, handle))
            self._unidentified.remove(handle)
        except (OSError, ValueError):
            self._fault("identity_failed")

    def _census(self) -> None:
        proc = self._proc
        if proc is None or proc.job is None or not proc.assigned:
            return
        try:
            self._refresh()
            pids = proc.api.members(proc.job)
            if (
                len(pids) > 5
                or len(set(pids)) != len(pids)
                or any(
                    type(pid) is not int or not 1 <= pid <= 2**32 - 1 for pid in pids
                )
            ):
                self._fault("budget_exceeded")
                return
            for pid in pids:
                if not any(
                    v.birth.pid == pid
                    and not v.closed
                    and proc.api.poll(v.handle) is None
                    for v in self._held
                ):
                    self._open(pid)
            self._refresh()
        except OSError:
            self._fault("query_failed")

    def observe_probe(self, notice: im.ProbeBirthNotice) -> im.IntakeOwner:
        with self._condition:
            if self._stopping:
                raise WorkflowError("intake_protocol")
            self._admit_time()
            if (
                not self._resumed
                or self._failures
                or self._pending is not None
                or notice.operation_id != self.request.operation_id
                or notice.sequence != self._sequence + 1
                or notice.sequence > len(self.request.video_ids)
                or notice.item_id != self.request.video_ids[notice.sequence - 1]
                or (self._notice_queued is not None and self._notice_queued != notice)
            ):
                self._fault("ack_failed")
                raise WorkflowError("intake_protocol")
            proc = self._proc
            assert proc is not None
            try:
                if any(
                    v.birth.role == "probe" and proc.api.poll(v.handle) is None
                    for v in self._held
                ):
                    raise OSError("concurrent_probe")
            except OSError:
                self._fault("wait_failed")
            self._refresh()
            if not self._failures:
                self._open(notice.birth.pid, notice.birth)
                self._census()
            if self._failures:
                raise WorkflowError("unknown_owner")
            self._probe = next(
                value for value in self._held if value.birth == notice.birth
            )
            self._probe_until = (
                self._notice_received_at
                if self._notice_received_at is not None
                else time.monotonic()
            ) + self.request.root.policy.probe.deadline_seconds
            self._notice_received_at = None
            self._pending, self._notice_queued = notice, None
            self._admit_time()
            return self.owner_observation()

    def ack_probe(self, ack: im.ProbeResumeAck) -> None:
        with self._condition:
            if self._stopping:
                raise WorkflowError("intake_protocol")
            self._admit_time()
            notice = im.ProbeBirthNotice.model_validate(
                ack.model_dump(exclude={"owner_version"})
            )
            if (
                self._failures
                or self._pending != notice
                or ack.owner_version <= self._owner_version
                or len(self._outbound) >= p.QUEUE_ITEMS
            ):
                self._fault("ack_failed")
                raise WorkflowError("intake_protocol")
            self._outbound.append(
                p.AckCommand(operation_id=self.request.operation_id, ack=ack)
            )
            self._owner_version, self._sequence = ack.owner_version, ack.sequence
            self._pending = None
            self._condition.notify_all()

    def request_stop(self) -> None:
        with self._condition:
            self._stopping = True
            self._outbound.clear()
            self._condition.notify_all()
            if self._proc is not None and not self._closed:
                self._census()
                try:
                    self._proc.terminate_owned()
                except OSError:
                    self._fault("wait_failed")

    def settle(self, *, deadline: float) -> im.IntakeExitProof:
        with self._settle_lock:
            self.request_stop()
            proc = self._proc
            if proc is not None and not self._closed:
                for held in self._held:
                    if not held.waited:
                        try:
                            proc.api.wait(
                                held.handle, max(0, deadline - time.monotonic())
                            )
                            held.waited = True
                        except (OSError, subprocess.TimeoutExpired):
                            self._fault("wait_failed")
                # If launcher validation failed, still wait its original handle.
                if proc.process is not None and not any(v.launcher for v in self._held):
                    try:
                        proc.api.wait(proc.process, max(0, deadline - time.monotonic()))
                    except (OSError, subprocess.TimeoutExpired):
                        self._fault("wait_failed")
                self._refresh()
            for thread in self._threads:
                if thread.ident is not None:
                    thread.join(max(0, deadline - time.monotonic()))
            io_joined = all(not thread.is_alive() for thread in self._threads)
            if not io_joined:
                self._fault("io_failed")
            empty = self._active == 0
            if (
                proc is not None
                and not self._closed
                and empty
                and io_joined
                and all(v.waited for v in self._held)
            ):
                for stream in (proc.stdin, proc.stdout):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            self._fault("close_failed")
                for held in self._held:
                    if not held.launcher and not held.closed:
                        try:
                            proc.api.close(held.handle)
                            held.closed = True
                        except OSError:
                            self._fault("close_failed")
                for handle in self._unidentified[:]:
                    try:
                        proc.api.close(handle)
                        self._unidentified.remove(handle)
                    except OSError:
                        self._fault("close_failed")
                try:
                    proc.close()
                except OSError:
                    self._fault("close_failed")
                for held in self._held:
                    if held.launcher and proc.process is None:
                        held.closed = True
                self._closed = (
                    proc.process is None
                    and proc.thread is None
                    and proc.job is None
                    and not proc.handles
                    and not proc.fds
                    and not self._unidentified
                    and all(v.closed for v in self._held)
                    and all(
                        stream is None or stream.closed
                        for stream in (proc.stdin, proc.stdout)
                    )
                )
            if proc is None:
                self._closed = True
            good = not self._failures and empty and io_joined and self._closed
            state: Literal["no_process_created", "stopped", "unknown"] = (
                "no_process_created"
                if good and self._no_process
                else "stopped"
                if good and self._held
                else "unknown"
            )
            return im.IntakeExitProof(
                operation_id=self.request.operation_id,
                owner_boot_id=self.request.admission.owner_boot_id,
                owner_id=self.request.owner_id,
                source_generation=self.request.admission.source_generation,
                state=state,
                births=tuple(
                    im.IntakeBirthExit(
                        birth=v.birth,
                        signaled=v.waited,
                        waited=v.waited,
                        handle_closed=v.closed,
                    )
                    for v in self._held
                ),
                job_active_zero=empty,
                io_joined=io_joined,
                owned_handles_closed=self._closed,
                failure_codes=tuple(self._failures),
                observed_at=datetime.now(timezone.utc),
            )


def start_snapshot_suspended(request: p.IntakeWorkRequest) -> OwnedIntakeSnapshot:
    if sys.platform != "win32":
        raise WorkflowError("intake_platform")
    request = p.IntakeWorkRequest.model_validate(request)
    # Validate the initial serialized bound before native resource acquisition.
    p.encode_frame(
        p.WorkRequestEvent(operation_id=request.operation_id, request=request)
    )
    result = OwnedIntakeSnapshot(request)
    result._start()
    return result
