"""Parent ownership tests. Fake APIs test policy, not Windows ownership proof."""

import io
import sys
import time

import pytest

from taskpaw_v3.tests.test_workflow_intake_protocol import work_request
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow import intake_owner as owner
from taskpaw_v3.workflow import intake_protocol as p
from taskpaw_v3.workflow.models import WorkflowError


class FakeAPI:
    def __init__(self):
        self.total, self.active = 1, 1
        self.identities = {10: (100, 1000)}
        self.current = [100]
        self.closed = []
        self.signaled = set()
        self.fault = None

    def accounting(self, job):
        if self.fault == "query":
            raise OSError("injected")
        return self.total, self.active

    def members(self, job):
        return self.current[:]

    def open_process(self, pid):
        if self.fault == "open":
            raise OSError("injected")
        return next(h for h, identity in self.identities.items() if identity[0] == pid)

    def identity(self, handle, job):
        if self.fault == "identity":
            raise OSError("injected")
        return self.identities[handle]

    def poll(self, handle):
        return 0 if handle in self.signaled else None

    def wait(self, handle, timeout):
        if self.fault == "wait":
            raise OSError("injected")
        self.signaled.add(handle)
        return 0

    def close(self, handle):
        if self.fault == "close":
            raise OSError("injected")
        self.closed.append(handle)


class FakeProcess:
    def __init__(self):
        self.api = FakeAPI()
        self.process, self.thread, self.job, self.pid = 10, 11, 12, 100
        self.assigned = True
        self.stdin, self.stdout = io.BytesIO(), io.BytesIO()
        self.resumed = False
        self.handles, self.fds = set(), set()

    def start(self, argv, env):
        self.argv = argv

    def resume(self):
        self.resumed = True
        self.thread = None

    def terminate_owned(self):
        self.api.active = 0
        self.api.current = []
        self.api.signaled.update(self.api.identities)

    def poll(self):
        return self.api.poll(self.process)

    def close(self):
        for key in ("thread", "process", "job"):
            handle = getattr(self, key)
            if handle is not None:
                self.api.close(handle)
                setattr(self, key, None)


def fake_owner(monkeypatch, count=1):
    proc = FakeProcess()
    monkeypatch.setattr(owner, "_new_process", lambda: proc)
    obj = owner.start_snapshot_suspended(work_request(count))
    return obj, proc


def notice(obj, proc, sequence=1):
    handle, pid = 20 + sequence, 200 + sequence
    proc.api.identities[handle] = (pid, 2000 + sequence)
    proc.api.total, proc.api.active = sequence + 1, 2
    proc.api.current = [100, pid]
    birth = im.IntakeProcessBirth(
        pid=pid,
        creation_filetime_100ns=2000 + sequence,
        role="probe",
        item_id=obj.request.video_ids[sequence - 1],
        sequence=sequence,
    )
    return im.ProbeBirthNotice(
        operation_id=obj.request.operation_id,
        item_id=birth.item_id,
        sequence=sequence,
        birth=birth,
    )


def ready(obj):
    # Policy unit tests do not start pipe threads. Native tests use public resume.
    obj._resumed = True
    obj._owner_version = 1


def test_suspend_birth_budget_and_exact_ack(monkeypatch):
    obj, proc = fake_owner(monkeypatch)
    assert not proc.resumed
    assert obj.owner_observation().births[0].role == "launcher"
    ready(obj)
    event = notice(obj, proc)
    observed = obj.observe_probe(event)
    assert observed.births[-1] == event.birth
    assert not proc.resumed
    with pytest.raises(WorkflowError, match="intake_protocol"):
        obj.ack_probe(im.ProbeResumeAck(**event.model_dump(), owner_version=1))
    assert obj.owner_observation().identity_uncertain
    assert obj.settle(deadline=time.monotonic() + 1).state == "unknown"


def test_seventeen_probes_do_not_inherit_hub_lifetime_limit(monkeypatch):
    obj, proc = fake_owner(monkeypatch, 17)
    ready(obj)
    for sequence in range(1, 18):
        event = notice(obj, proc, sequence)
        obj.observe_probe(event)
        obj.ack_probe(
            im.ProbeResumeAck(**event.model_dump(), owner_version=sequence + 1)
        )
        # Consume outbound in lieu of a fake pipe thread.
        obj._outbound.clear()
        proc.api.signaled.add(20 + sequence)
    assert len(obj.owner_observation().births) == 18
    proof = obj.settle(deadline=time.monotonic() + 1)
    assert proof.state == "stopped"
    assert all(v.signaled and v.waited and v.handle_closed for v in proof.births)


@pytest.mark.parametrize(
    "fault,code",
    [
        ("open", "open_failed"),
        ("identity", "identity_failed"),
        ("query", "query_failed"),
        ("wait", "wait_failed"),
        ("close", "close_failed"),
    ],
)
def test_faults_remain_sticky_after_empty_job(monkeypatch, fault, code):
    obj, proc = fake_owner(monkeypatch)
    ready(obj)
    event = notice(obj, proc)
    proc.api.fault = fault
    if fault in {"open", "identity", "query"}:
        with pytest.raises(WorkflowError):
            obj.observe_probe(event)
    proof = obj.settle(deadline=time.monotonic() + 1)
    assert proof.state == "unknown"
    assert code in proof.failure_codes
    proc.api.fault = None
    proof = obj.settle(deadline=time.monotonic() + 1)
    assert proof.state == "unknown" and code in proof.failure_codes


@pytest.mark.parametrize("total,active", [(6, 1), (1, 2), (2, 6), (True, 1), (-1, 0)])
def test_accounting_budget_is_not_clamped_to_success(monkeypatch, total, active):
    obj, proc = fake_owner(monkeypatch)
    proc.api.total, proc.api.active = total, active
    assert obj.owner_observation().identity_uncertain
    assert obj.settle(deadline=time.monotonic() + 1).state == "unknown"


def test_stop_forbids_notice_and_ack(monkeypatch):
    obj, proc = fake_owner(monkeypatch)
    ready(obj)
    event = notice(obj, proc)
    obj.request_stop()
    with pytest.raises(WorkflowError):
        obj.observe_probe(event)
    assert obj.settle(deadline=time.monotonic() + 1).state == "stopped"


def test_progress_mailbox_coalesces_without_unbounded_queue(monkeypatch):
    obj, _ = fake_owner(monkeypatch)
    for value in range(10000):
        obj._receive(
            p.ProgressEvent(
                operation_id=obj.request.operation_id,
                member_id=obj.request.video_ids[0],
                phase="copy",
                bytes_completed=value,
            )
        )
    assert len(obj._events) <= p.QUEUE_ITEMS
    assert obj._progress is not None and obj._progress.bytes_completed == 9999


def synthetic_helper(tmp_path, monkeypatch, *, block=False, probe_body=None):
    """Only this trusted test assembly replaces a private fixed launch builder."""
    canary = tmp_path / "probe-canary.py"
    canary.write_text(
        "import json, pathlib, sys\n"
        'pathlib.Path(sys.argv[1]).write_text("resumed", encoding="utf-8")\n'
        'print(json.dumps({"program_version":{"version":"fixture-1"},'
        '"streams":[{"index":0,"codec_type":"video","codec_name":"h264",'
        '"width":1920,"height":1080,"duration":"1.0","avg_frame_rate":"24/1",'
        '"r_frame_rate":"24/1"}],"format":{"format_name":"mov","duration":"1.0"}}))\n',
        encoding="utf-8",
    )
    if probe_body is not None:
        canary.write_text(probe_body, encoding="utf-8")
    script = tmp_path / "owned-helper.py"
    script.write_text(
        "import sys\n"
        "from taskpaw_v3.workflow import intake_protocol as p, intake_probe as probe, intake_probe_native as n\n"
        "budget=p.TransportBudget()\n"
        "first=p.read_frame(sys.stdin.buffer,budget=budget)\n"
        "request=first.request\n"
        + (
            "sys.stdin.buffer.read(1)\n"
            if block
            else "def fixed(executable, path, *, policy):\n"
            f"    return (sys._base_executable, {str(canary)!r}, path)\n"
            "n.build_ffprobe_argv=fixed\n"
            "def birth(notice):\n"
            "    p.write_frame(sys.stdout.buffer,p.ProbeEvent(operation_id=request.operation_id,notice=notice),budget=budget)\n"
            "def ack(deadline):\n"
            "    return p.read_frame(sys.stdin.buffer,budget=budget).ack\n"
            "for seq, member in enumerate(request.video_ids,1):\n"
            f'    path={str(tmp_path)!r}+"/ran-"+str(seq)\n'
            "    probe.probe_video(request,member_id=member,private_path=path,sequence=seq,on_birth=birth,receive_ack=ack,check_cancelled=lambda:None)\n"
            'p.write_frame(sys.stdout.buffer,p.FailureEvent(operation_id=request.operation_id,reason="probe_failed"),budget=budget)\n'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(owner, "_helper_argv", lambda: [sys.executable, str(script)])
    # Source fixture imports only this checkout; no service/default configuration.
    monkeypatch.setenv(
        "PYTHONPATH", str(__import__("pathlib").Path(__file__).resolve().parents[2])
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows native HANDLE proof")
def test_native_seventeen_probes_and_exact_handles(tmp_path, monkeypatch):
    synthetic_helper(tmp_path, monkeypatch)
    obj = owner.start_snapshot_suspended(work_request(17))
    proof = None
    try:
        initial = obj.owner_observation()
        assert not initial.identity_uncertain and len(initial.births) == 1
        obj.resume_helper(persisted_owner_version=1)
        for seq in range(1, 18):
            event = obj.next_event(deadline=time.monotonic() + 15)
            assert isinstance(event, p.ProbeEvent) and event.notice.sequence == seq
            observed = obj.observe_probe(event.notice)
            assert not observed.identity_uncertain
            assert not (tmp_path / f"ran-{seq}").exists()
            held = obj._held[-1]
            assert obj._proc.api.poll(held.handle) is None
            obj.ack_probe(
                im.ProbeResumeAck(**event.notice.model_dump(), owner_version=seq + 1)
            )
        assert isinstance(
            obj.next_event(deadline=time.monotonic() + 15), p.FailureEvent
        )
    finally:
        proof = obj.settle(deadline=time.monotonic() + 5)
    assert proof.state == "stopped", proof
    assert sum(v.birth.role == "probe" for v in proof.births) == 17
    assert all(v.signaled and v.waited and v.handle_closed for v in proof.births)
    assert obj.owner_observation().observed_total >= 18
    assert all(
        (tmp_path / f"ran-{seq}").read_text() == "resumed" for seq in range(1, 18)
    )
    assert all(not t.is_alive() for t in obj._threads)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows suspended probe")
@pytest.mark.parametrize("wrong_ack", [False, True])
def test_native_no_valid_ack_cannot_run_canary(tmp_path, monkeypatch, wrong_ack):
    synthetic_helper(tmp_path, monkeypatch)
    obj = owner.start_snapshot_suspended(work_request())
    try:
        obj.resume_helper(persisted_owner_version=1)
        event = obj.next_event(deadline=time.monotonic() + 15)
        assert isinstance(event, p.ProbeEvent)
        obj.observe_probe(event.notice)
        if wrong_ack:
            with pytest.raises(WorkflowError):
                obj.ack_probe(
                    im.ProbeResumeAck(**event.notice.model_dump(), owner_version=1)
                )
        assert not (tmp_path / "ran-1").exists()
    finally:
        proof = obj.settle(deadline=time.monotonic() + 5)
    assert proof.state == ("unknown" if wrong_ack else "stopped")
    assert not (tmp_path / "ran-1").exists()
    assert proof.job_active_zero and proof.io_joined and proof.owned_handles_closed


@pytest.mark.skipif(sys.platform != "win32", reason="Windows blocking pipe fixture")
def test_native_blocking_child_deadline_keeps_parent_responsive(tmp_path, monkeypatch):
    synthetic_helper(tmp_path, monkeypatch, block=True)
    obj = owner.start_snapshot_suspended(work_request())
    started = time.monotonic()
    try:
        obj.resume_helper(persisted_owner_version=1)
        with pytest.raises(WorkflowError, match="intake_timeout"):
            obj.next_event(deadline=time.monotonic() + 0.25)
    finally:
        proof = obj.settle(deadline=time.monotonic() + 5)
    assert time.monotonic() - started < 5
    assert proof.state == "unknown" and "timeout" in proof.failure_codes
    assert proof.job_active_zero and proof.io_joined and proof.owned_handles_closed


@pytest.mark.skipif(sys.platform != "win32", reason="Windows partial-start ownership")
@pytest.mark.parametrize(
    "method",
    [
        "create_job",
        "configure_job",
        "pipe",
        "fd",
        "inherit",
        "create_process",
        "assign",
        "identity",
        "close",
    ],
)
def test_native_partial_setup_is_returned_and_can_be_settled(
    tmp_path, monkeypatch, method
):
    synthetic_helper(tmp_path, monkeypatch)
    api = owner._CreationAPI()
    original = getattr(api, method)
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected native setup fault")
        return original(*args, **kwargs)

    monkeypatch.setattr(api, method, fail_once)
    proc = owner._WindowsProcess(api)
    monkeypatch.setattr(owner, "_new_process", lambda: proc)
    obj = owner.start_snapshot_suspended(work_request())
    assert obj is not None and not obj._resumed
    proof = obj.settle(deadline=time.monotonic() + 3)
    assert proof.io_joined and proof.owned_handles_closed
    assert proof.state in {"unknown", "no_process_created"}
    assert not (tmp_path / "ran-1").exists()


def test_accounting_regression_and_overflow_remain_unknown(monkeypatch):
    obj, proc = fake_owner(monkeypatch, 17)
    proc.api.total = 18
    assert obj.owner_observation().observed_total == 18
    proc.api.total = 17
    observation = obj.owner_observation()
    assert observation.observed_total == 18 and observation.identity_uncertain
    proc.api.total = 18
    assert obj.settle(deadline=time.monotonic() + 1).state == "unknown"


@pytest.mark.parametrize("bad", ["pid", "creation", "concurrent", "sequence", "item"])
def test_probe_birth_mismatch_is_not_acknowledged(monkeypatch, bad):
    obj, proc = fake_owner(monkeypatch, 2)
    ready(obj)
    event = notice(obj, proc)
    if bad == "pid":
        proc.api.identities[21] = (999, 2001)
        proc.api.open_process = lambda pid: 21
    elif bad == "creation":
        proc.api.identities[21] = (201, 999)
    elif bad == "sequence":
        event = notice(obj, proc, 2)
    elif bad == "item":
        event = event.model_copy(update={"item_id": obj.request.video_ids[1]})
    elif bad == "concurrent":
        obj.observe_probe(event)
        obj.ack_probe(im.ProbeResumeAck(**event.model_dump(), owner_version=2))
        event = notice(obj, proc, 2)
    with pytest.raises(WorkflowError):
        obj.observe_probe(event)
    assert obj.owner_observation().identity_uncertain
    assert obj.settle(deadline=time.monotonic() + 1).state == "unknown"


def test_full_native_census_budget_4100_4101(monkeypatch):
    obj, proc = fake_owner(monkeypatch, 4096)
    proc.api.total, proc.api.active = 4100, 5
    assert not obj.owner_observation().identity_uncertain
    proc.api.total = 4101
    observed = obj.owner_observation()
    assert observed.observed_total == 4100 and observed.identity_uncertain
    proc.api.total = 4100
    assert obj.settle(deadline=time.monotonic() + 1).state == "unknown"


def test_pipe_owner_must_join_before_handles_are_closed(monkeypatch):
    import threading

    obj, proc = fake_owner(monkeypatch)
    release = threading.Event()
    thread = threading.Thread(target=release.wait, daemon=False)
    obj._threads.append(thread)
    thread.start()
    try:
        proof = obj.settle(deadline=time.monotonic() + 0.01)
        assert proof.state == "unknown" and not proof.io_joined
        assert proc.process is not None and proc.job is not None
    finally:
        release.set()
        thread.join(1)
    proof = obj.settle(deadline=time.monotonic() + 1)
    assert proof.state == "unknown" and proof.io_joined and proof.owned_handles_closed


def test_second_terminal_and_unsolicited_second_notice_rejected(monkeypatch):
    obj, proc = fake_owner(monkeypatch)
    ready(obj)
    event = p.ProbeEvent(
        operation_id=obj.request.operation_id, notice=notice(obj, proc)
    )
    obj._receive(event)
    with pytest.raises(WorkflowError):
        obj._receive(event)
    obj.request_stop()
    obj.settle(deadline=time.monotonic() + 1)
    obj, proc = fake_owner(monkeypatch)
    event = p.FailureEvent(operation_id=obj.request.operation_id, reason="probe_failed")
    obj._receive(event)
    with pytest.raises(WorkflowError):
        obj._receive(event)
    obj.settle(deadline=time.monotonic() + 1)


@pytest.mark.skipif(
    sys.platform != "win32", reason="Windows observed native failure remains sticky"
)
@pytest.mark.parametrize(
    "method", ["open_process", "identity", "accounting", "wait", "close"]
)
def test_native_observed_failure_is_not_erased_by_later_empty_job(
    tmp_path, monkeypatch, method
):
    synthetic_helper(tmp_path, monkeypatch)
    obj = owner.start_snapshot_suspended(work_request())
    try:
        obj.resume_helper(persisted_owner_version=1)
        event = obj.next_event(deadline=time.monotonic() + 15)
        assert isinstance(event, p.ProbeEvent)
        if method in {"wait", "close"}:
            obj.observe_probe(event.notice)
        api = obj._proc.api
        original = getattr(api, method)
        calls = 0

        def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("injected observed fault")
            return original(*args, **kwargs)

        monkeypatch.setattr(api, method, fail_once)
        if method not in {"wait", "close"}:
            with pytest.raises(WorkflowError):
                obj.observe_probe(event.notice)
        proof = obj.settle(deadline=time.monotonic() + 5)
        assert proof.state == "unknown"
    finally:
        proof = obj.settle(deadline=time.monotonic() + 5)
    assert (
        proof.state == "unknown"
        and proof.job_active_zero
        and proof.io_joined
        and proof.owned_handles_closed
    )
    assert not (tmp_path / "ran-1").exists()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows writer pipe owner")
def test_native_blocked_request_writer_joins_after_stop(tmp_path, monkeypatch):
    script = tmp_path / "not-reading.py"
    script.write_text("import time\ntime.sleep(120)\n", encoding="utf-8")
    monkeypatch.setattr(owner, "_helper_argv", lambda: [sys.executable, str(script)])
    obj = owner.start_snapshot_suspended(work_request(100))
    try:
        obj.resume_helper(persisted_owner_version=1)
        with pytest.raises(WorkflowError, match="intake_timeout"):
            obj.next_event(deadline=time.monotonic() + 0.2)
    finally:
        proof = obj.settle(deadline=time.monotonic() + 5)
    assert proof.state == "unknown" and proof.io_joined and proof.owned_handles_closed
    assert all(not thread.is_alive() for thread in obj._threads)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows bounded native output")
@pytest.mark.parametrize(
    "stream,size", [("stdout", 5 * 1024 * 1024), ("stderr", 128 * 1024)]
)
def test_native_probe_output_overflow_never_produces_result(
    tmp_path, monkeypatch, stream, size
):
    synthetic_helper(
        tmp_path,
        monkeypatch,
        probe_body=f'import sys\nsys.{stream}.buffer.write(b"x"*{size})\nsys.{stream}.buffer.flush()\n',
    )
    obj = owner.start_snapshot_suspended(work_request())
    try:
        obj.resume_helper(persisted_owner_version=1)
        event = obj.next_event(deadline=time.monotonic() + 15)
        assert isinstance(event, p.ProbeEvent)
        obj.observe_probe(event.notice)
        obj.ack_probe(im.ProbeResumeAck(**event.notice.model_dump(), owner_version=2))
        with pytest.raises(WorkflowError, match="intake_protocol"):
            obj.next_event(deadline=time.monotonic() + 15)
    finally:
        proof = obj.settle(deadline=time.monotonic() + 5)
    assert proof.state == "stopped" and proof.owned_handles_closed


@pytest.mark.parametrize("action", ["queued_probe", "queued_progress", "resume", "ack"])
def test_cycle3_overall_deadline_precedes_continuation(monkeypatch, action):
    obj, proc = fake_owner(monkeypatch)
    if action != "resume":
        ready(obj)
    if action == "queued_probe":
        obj._receive(
            p.ProbeEvent(
                operation_id=obj.request.operation_id, notice=notice(obj, proc)
            )
        )
    elif action == "queued_progress":
        obj._receive(
            p.ProgressEvent(
                operation_id=obj.request.operation_id,
                member_id=obj.request.video_ids[0],
                phase="copy",
                bytes_completed=1,
            )
        )
    elif action == "ack":
        pending = notice(obj, proc)
        obj.observe_probe(pending)
    obj._started -= obj.request.root.policy.operation_deadline_seconds + 1
    with pytest.raises(WorkflowError, match="intake_timeout"):
        if action == "resume":
            obj.resume_helper(persisted_owner_version=1)
        elif action == "ack":
            obj.ack_probe(im.ProbeResumeAck(**pending.model_dump(), owner_version=2))
        else:
            obj.next_event(deadline=time.monotonic() + 1)
    assert not proc.resumed and obj._stopping
    assert obj.settle(deadline=time.monotonic() + 1).state == "unknown"


def test_cycle3_terminal_fact_survives_timeout_without_granting_continuation(
    monkeypatch,
):
    obj, proc = fake_owner(monkeypatch)
    obj._started -= obj.request.root.policy.operation_deadline_seconds + 1
    event = p.FailureEvent(operation_id=obj.request.operation_id, reason="probe_failed")
    obj._receive(event)
    assert obj.next_event(deadline=time.monotonic() + 1) == event
    assert obj.settle(deadline=time.monotonic() + 1).state == "unknown"


def test_cycle3_child_uncertainty_is_sticky_but_media_failure_is_not(monkeypatch):
    for reason, expected in (("owner_unknown", "unknown"), ("probe_failed", "stopped")):
        obj, _ = fake_owner(monkeypatch)
        event = p.FailureEvent(operation_id=obj.request.operation_id, reason=reason)
        obj._receive(event)
        assert obj.next_event(deadline=time.monotonic() + 1) == event
        assert obj.settle(deadline=time.monotonic() + 1).state == expected
        assert obj.settle(deadline=time.monotonic() + 1).state == expected


def test_cycle3_probe_phase_uses_its_deadline_and_returns_to_byte_idle(monkeypatch):
    obj, proc = fake_owner(monkeypatch)
    ready(obj)
    event = notice(obj, proc)
    obj.observe_probe(event)
    obj.ack_probe(im.ProbeResumeAck(**event.model_dump(), owner_version=2))
    obj._outbound.clear()
    obj._last_progress -= obj.request.root.policy.idle_deadline_seconds + 1
    # Active native probe: the old byte timestamp must not expire its own phase.
    assert obj._admit_time() > time.monotonic()
    with pytest.raises(WorkflowError, match="intake_protocol"):
        obj._receive(
            p.ProgressEvent(
                operation_id=obj.request.operation_id,
                member_id=obj.request.video_ids[0],
                phase="verify",
                bytes_completed=0,
            )
        )
    proc.api.signaled.add(21)
    assert obj._admit_time() > time.monotonic()
    assert obj._probe_until is None
    obj._last_progress -= obj.request.root.policy.idle_deadline_seconds + 1
    with pytest.raises(WorkflowError, match="intake_timeout"):
        obj.next_event(deadline=time.monotonic() + 1)
    assert obj.settle(deadline=time.monotonic() + 1).state == "unknown"


@pytest.mark.skipif(sys.platform != "win32", reason="Actual elapsed operation deadline")
def test_cycle3_actual_queued_probe_after_hard_deadline_never_executes(
    tmp_path, monkeypatch
):
    synthetic_helper(tmp_path, monkeypatch)
    raw = work_request().model_dump()
    raw["root"]["policy"].update(operation_deadline_seconds=2, idle_deadline_seconds=2)
    raw["root"]["policy"]["probe"]["deadline_seconds"] = 2
    obj = owner.start_snapshot_suspended(p.IntakeWorkRequest.model_validate(raw))
    try:
        obj.resume_helper(persisted_owner_version=1)
        time.sleep(max(0, obj._started + 2.05 - time.monotonic()))
        with pytest.raises(WorkflowError, match="intake_timeout"):
            obj.next_event(deadline=time.monotonic() + 1)
        assert not (tmp_path / "ran-1").exists()
    finally:
        proof = obj.settle(deadline=time.monotonic() + 5)
    assert proof.state == "unknown" and proof.io_joined and proof.owned_handles_closed


@pytest.mark.skipif(sys.platform != "win32", reason="Actual probe phase progress")
def test_cycle3_valid_native_probes_can_outlast_copy_idle(tmp_path, monkeypatch):
    synthetic_helper(tmp_path, monkeypatch)
    script = tmp_path / "probe-canary.py"
    script.write_text(
        "import time\ntime.sleep(.30)\n" + script.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    raw = work_request(4).model_dump()
    raw["root"]["policy"].update(operation_deadline_seconds=10, idle_deadline_seconds=1)
    raw["root"]["policy"]["probe"]["deadline_seconds"] = 1
    obj = owner.start_snapshot_suspended(p.IntakeWorkRequest.model_validate(raw))
    try:
        obj.resume_helper(persisted_owner_version=1)
        for sequence in range(1, 5):
            event = obj.next_event(deadline=time.monotonic() + 5)
            assert isinstance(event, p.ProbeEvent)
            obj.observe_probe(event.notice)
            obj.ack_probe(
                im.ProbeResumeAck(
                    **event.notice.model_dump(), owner_version=sequence + 1
                )
            )
        assert isinstance(obj.next_event(deadline=time.monotonic() + 5), p.FailureEvent)
    finally:
        proof = obj.settle(deadline=time.monotonic() + 5)
    assert proof.state == "stopped" and len(list(tmp_path.glob("ran-*"))) == 4


def test_cycle3_late_verify_frame_cannot_erase_expired_probe_deadline(monkeypatch):
    obj, proc = fake_owner(monkeypatch)
    ready(obj)
    notice_value = notice(obj, proc)
    obj.observe_probe(notice_value)
    obj.ack_probe(im.ProbeResumeAck(**notice_value.model_dump(), owner_version=2))
    proc.api.signaled.add(21)
    obj._probe_until = time.monotonic() - 1
    with pytest.raises(WorkflowError, match="intake_timeout"):
        obj._receive(
            p.ProgressEvent(
                operation_id=obj.request.operation_id,
                member_id=obj.request.video_ids[0],
                phase="verify",
                bytes_completed=1,
            )
        )
    assert obj.settle(deadline=time.monotonic() + 1).state == "unknown"


def test_cycle3_ack_wait_has_finite_probe_deadline(monkeypatch):
    obj, proc = fake_owner(monkeypatch)
    ready(obj)
    pending = notice(obj, proc)
    obj.observe_probe(pending)
    obj._probe_until = time.monotonic() - 1
    with pytest.raises(WorkflowError, match="intake_timeout"):
        obj.ack_probe(im.ProbeResumeAck(**pending.model_dump(), owner_version=2))
    assert obj.settle(deadline=time.monotonic() + 1).state == "unknown"


@pytest.mark.skipif(sys.platform != "win32", reason="Native child failure propagation")
@pytest.mark.parametrize(
    "failure",
    [
        "identity",
        "setup_close",
        "resume",
        "query",
        "wait",
        "final_close",
        "nonzero",
        "parse",
    ],
)
def test_cycle3_native_child_fault_survives_worker_mapping(
    tmp_path, monkeypatch, failure
):
    synthetic_helper(tmp_path, monkeypatch)
    script = tmp_path / "owned-helper.py"
    injection = """
from taskpaw_v3.workflow.intake_worker import _reason
def failure_hook(kind, exc, tb):
    p.write_frame(sys.stdout.buffer,p.FailureEvent(operation_id=request.operation_id,reason=_reason(exc)),budget=budget)
sys.excepthook=failure_hook
mode=MODE
original_new=probe._new_probe
def injected_probe():
    obj=original_new()
    names={"identity":"identity","setup_close":"close","resume":"resume","query":"poll","wait":"wait","final_close":"close"}
    if mode not in names:
        return obj
    name=names[mode]
    original=getattr(obj.api,name)
    failed=[False]
    def injected(*args):
        eligible = mode != "final_close" or args[0] == obj.process
        if eligible and not failed[0]:
            failed[0]=True
            raise OSError("injected child native fault")
        return original(*args)
    setattr(obj.api,name,injected)
    return obj
probe._new_probe=injected_probe
""".replace("MODE", repr(failure))
    original = script.read_text(encoding="utf-8")
    split = original.index("for seq, member")
    script.write_text(original[:split] + injection + original[split:], encoding="utf-8")
    if failure in {"nonzero", "parse"}:
        (tmp_path / "probe-canary.py").write_text(
            "import sys\nsys.exit(3)\n" if failure == "nonzero" else "print('{')\n",
            encoding="utf-8",
        )
    obj = owner.start_snapshot_suspended(work_request())
    terminal = None
    try:
        obj.resume_helper(persisted_owner_version=1)
        while True:
            event = obj.next_event(deadline=time.monotonic() + 10)
            if isinstance(event, p.ProbeEvent):
                obj.observe_probe(event.notice)
                obj.ack_probe(
                    im.ProbeResumeAck(**event.notice.model_dump(), owner_version=2)
                )
            else:
                terminal = event
                break
    finally:
        proof = obj.settle(deadline=time.monotonic() + 5)
    native_failure = failure not in {"nonzero", "parse"}
    assert isinstance(terminal, p.FailureEvent)
    assert terminal.reason == ("owner_unknown" if native_failure else "probe_failed")
    assert proof.state == ("unknown" if native_failure else "stopped")
    assert proof.job_active_zero and proof.io_joined and proof.owned_handles_closed
    assert obj.settle(deadline=time.monotonic() + 1).state == proof.state


@pytest.mark.parametrize("kind", ["request", "ack"])
@pytest.mark.parametrize("expired", [True, False])
def test_cycle4_writer_rechecks_deferred_continuation(monkeypatch, kind, expired):
    obj, proc = fake_owner(monkeypatch)
    ready(obj)
    if kind == "request":
        obj._outbound.append(
            p.WorkRequestEvent(
                operation_id=obj.request.operation_id, request=obj.request
            )
        )
    else:
        value = notice(obj, proc)
        obj.observe_probe(value)
        obj.ack_probe(im.ProbeResumeAck(**value.model_dump(), owner_version=2))
    if expired:
        obj._started -= obj.request.root.policy.operation_deadline_seconds + 1
    sent = []

    def send(stream, frame, *, budget):
        sent.append(frame.kind)
        obj._eof = True

    monkeypatch.setattr(p, "write_frame", send)
    obj._write()
    assert sent == ([] if expired else [kind])
    proof = obj.settle(deadline=time.monotonic() + 1)
    assert proof.state == ("unknown" if expired else "stopped")
    assert ("timeout" in proof.failure_codes) == expired
