"""Process-wide GPU lease with fair per-file hand-off (#179, core/gpu_lease.py).

Every behaviour is a pure function of the call sequence and the injected
clock, so these tests drive a `FakeClock` by hand."""

from __future__ import annotations

import logging
import threading

import pytest

from taskpaw_v3.core import gpu_lease
from taskpaw_v3.core.gpu_lease import (
    RESERVE_MIN_S,
    STALE_FACTOR,
    STALE_MIN_S,
    GpuLease,
)

A = ("inst-a", 1)
B = ("inst-b", 1)
C = ("inst-c", 1)


class FakeClock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def lease(clock: FakeClock) -> GpuLease:
    return GpuLease(clock=clock)


def test_constants():
    assert RESERVE_MIN_S == 30.0
    assert STALE_MIN_S == 30.0
    assert STALE_FACTOR == 3.0


def test_acquire_release_holder_and_label(lease):
    assert lease.holder() is None
    assert lease.blocking_label() == ""
    assert lease.try_acquire(A, 5.0, label="Jasna") is True
    assert lease.holder() == A
    assert lease.blocking_label() == "Jasna"
    assert lease.release(A) is True
    assert lease.holder() is None
    assert lease.blocking_label() == ""
    assert lease.reserved_for() is None
    assert lease.waiters() == []


def test_reacquire_by_the_holder_is_idempotent_and_refreshes_label(lease):
    assert lease.try_acquire(A, 5.0, label="old") is True
    assert lease.try_acquire(A, 5.0, label="new") is True
    assert lease.holder() == A
    assert lease.blocking_label() == "new"
    assert lease.waiters() == []
    assert lease.release(A) is True
    assert lease.release(A) is False  # held once, released once


def test_blocking_label_names_the_reserved_waiter_while_free(lease):
    lease.try_acquire(A, 5.0, label="Jasna")
    assert lease.try_acquire(B, 5.0, label="AV subs") is False
    assert lease.blocking_label() == "Jasna"  # the holder
    lease.release(A)
    assert lease.holder() is None
    assert lease.reserved_for() == B
    assert lease.blocking_label() == "AV subs"  # free but reserved (D8)


@pytest.mark.parametrize("wrong", [("inst-b", 1), ("inst-a", 2)])
def test_release_by_wrong_instance_or_generation_refused_warning_after_lock(
    lease, caplog, wrong
):
    lease.try_acquire(A, 5.0, label="x")
    seen_locked: list[bool] = []

    class Spy(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen_locked.append(lease._lock.locked())

    spy = Spy(level=logging.WARNING)
    logger = logging.getLogger("taskpaw.gpu_lease")
    logger.addHandler(spy)
    try:
        caplog.set_level(logging.WARNING, logger="taskpaw.gpu_lease")
        assert lease.release(wrong) is False
    finally:
        logger.removeHandler(spy)
    assert lease.holder() == A
    assert seen_locked == [False]  # logged, and NOT under the lease's lock
    msg = " ".join(r.getMessage() for r in caplog.records)
    assert str(wrong) in msg and str(A) in msg


def test_release_when_free_refused_never_raises(lease):
    assert lease.release(A) is False
    lease.try_acquire(A, 5.0)
    assert lease.release(A) is True
    assert lease.release(A) is False  # double release


def test_waiter_order_is_kept_when_a_waiter_updates(lease, clock):
    lease.try_acquire(A, 5.0)
    assert lease.try_acquire(B, 5.0, label="b") is False
    assert lease.try_acquire(C, 5.0, label="c") is False
    clock.advance(1)
    assert lease.try_acquire(B, 7.0, label="b2") is False  # update keeps position
    assert lease.waiters() == [B, C]
    lease.release(A)
    assert lease.reserved_for() == B
    assert lease.blocking_label() == "b2"


def test_releaser_reacquire_refused_while_another_waits_then_waiter_wins(lease):
    assert lease.try_acquire(A, 5.0, label="a")
    assert lease.try_acquire(B, 5.0, label="b") is False
    assert lease.release(A) is True
    assert lease.try_acquire(A, 5.0, label="a") is False  # B's turn (fair)
    assert lease.holder() is None
    assert lease.waiters() == [B, A]
    assert lease.try_acquire(B, 5.0, label="b") is True
    assert lease.holder() == B
    assert lease.waiters() == [A]
    assert lease.reserved_for() is None  # held: no reservation
    assert lease.release(B) is True
    assert lease.reserved_for() == A
    assert lease.try_acquire(B, 5.0) is False
    assert lease.try_acquire(A, 5.0) is True


def test_third_party_refused_while_reserved(lease):
    lease.try_acquire(A, 5.0)
    lease.try_acquire(B, 5.0)
    lease.release(A)
    assert lease.try_acquire(C, 5.0) is False
    assert lease.try_acquire(B, 5.0) is True


@pytest.mark.parametrize(
    "poll,window",
    [(5.0, RESERVE_MIN_S), (40.0, 80.0)],  # max(30, 2 × waiter poll) — both branches
)
def test_reservation_window_uses_the_waiters_poll_interval(lease, clock, poll, window):
    lease.try_acquire(A, 1.0)
    lease.try_acquire(B, poll)
    lease.release(A)  # the reservation starts now
    clock.advance(window - 0.01)
    assert lease.reserved_for() == B
    assert lease.try_acquire(C, 1.0) is False
    lease.withdraw(C)
    clock.advance(0.02)  # the window has passed
    assert lease.reserved_for() is None
    assert B not in lease.waiters()  # an expired reservation drops its waiter
    assert lease.try_acquire(C, 1.0) is True


def test_expired_reservation_passes_to_the_next_waiter(lease, clock):
    lease.try_acquire(A, 1.0)
    lease.try_acquire(B, 1.0, label="b")
    lease.try_acquire(C, 1.0, label="c")
    lease.release(A)
    assert lease.reserved_for() == B
    clock.advance(RESERVE_MIN_S - 1)
    lease.try_acquire(C, 1.0, label="c")  # C stays fresh; B went quiet
    clock.advance(2)
    assert lease.reserved_for() == C
    assert lease.blocking_label() == "c"
    assert lease.waiters() == [C]
    assert lease.try_acquire(B, 1.0) is False  # B is back, at the end
    assert lease.try_acquire(C, 1.0) is True


def test_expired_reservation_with_no_other_waiter_frees_the_lease(lease, clock):
    lease.try_acquire(A, 1.0)
    lease.try_acquire(B, 1.0)
    lease.release(A)
    clock.advance(RESERVE_MIN_S + 0.01)
    assert lease.reserved_for() is None
    assert lease.waiters() == []
    assert lease.try_acquire(A, 1.0) is True


@pytest.mark.parametrize(
    "poll,limit",
    [(5.0, STALE_MIN_S), (20.0, 60.0)],  # max(30, 3 × poll) — both branches
)
def test_stale_waiter_pruning(lease, clock, poll, limit):
    lease.try_acquire(A, 1.0)
    lease.try_acquire(B, poll)
    clock.advance(limit)  # exactly at the limit: still a waiter
    assert lease.waiters() == [B]
    clock.advance(0.01)
    assert lease.waiters() == []
    lease.release(A)
    assert lease.reserved_for() is None
    assert lease.try_acquire(A, 1.0) is True  # nobody waits any more


def test_withdraw_removes_waiter_and_reservation_and_passes_the_turn(lease):
    lease.try_acquire(A, 1.0)
    lease.try_acquire(B, 1.0, label="b")
    lease.try_acquire(C, 1.0, label="c")
    lease.withdraw(C)  # a plain waiter
    assert lease.waiters() == [B]
    lease.try_acquire(C, 1.0, label="c")
    lease.release(A)
    assert lease.reserved_for() == B
    lease.withdraw(B)  # the reserved waiter: the turn passes to C
    assert lease.waiters() == [C]
    assert lease.reserved_for() == C
    assert lease.blocking_label() == "c"
    lease.withdraw(C)
    assert lease.reserved_for() is None and lease.waiters() == []
    assert lease.try_acquire(A, 1.0) is True


def test_withdraw_never_touches_the_holder(lease):
    lease.try_acquire(A, 1.0)
    lease.withdraw(A)
    assert lease.holder() == A
    lease.withdraw(("nobody", 0))  # unknown run: no-op
    assert lease.holder() == A


def test_no_waiter_means_immediate_reacquire(lease):
    for _ in range(3):
        assert lease.try_acquire(A, 1.0) is True
        assert lease.release(A) is True


def test_bad_poll_interval_never_raises(lease):
    assert lease.try_acquire(A, float("nan")) is True
    assert lease.try_acquire(B, -5.0) is False
    assert lease.try_acquire(C, "x") is False  # type: ignore[arg-type]
    lease.release(A)
    assert lease.reserved_for() == B


def test_module_level_functions_delegate_to_the_process_lease():
    clock = FakeClock()
    gpu_lease._reset_for_tests(clock=clock)
    assert gpu_lease.try_acquire(A, 1.0, label="a") is True
    assert gpu_lease.holder() == A
    assert gpu_lease.try_acquire(B, 1.0, label="b") is False
    assert gpu_lease.waiters() == [B]
    assert gpu_lease.blocking_label() == "a"
    assert gpu_lease.release(A) is True
    assert gpu_lease.reserved_for() == B
    assert gpu_lease.blocking_label() == "b"
    clock.advance(RESERVE_MIN_S + 1)  # the injected clock drives the module lease
    assert gpu_lease.reserved_for() is None
    gpu_lease.try_acquire(B, 1.0)
    gpu_lease.withdraw(B)
    gpu_lease._reset_for_tests()
    assert gpu_lease.holder() is None and gpu_lease.waiters() == []


def test_autouse_fixture_gives_each_test_a_fresh_lease_part1():
    assert gpu_lease.holder() is None
    gpu_lease.try_acquire(("leak", 1), 1.0)
    gpu_lease.try_acquire(("leak-waiter", 1), 1.0)


def test_autouse_fixture_gives_each_test_a_fresh_lease_part2():
    assert gpu_lease.holder() is None
    assert gpu_lease.waiters() == []


def test_eight_thread_hammer_never_sees_two_holders():
    lease = GpuLease()  # real clock; every thread keeps trying, so none goes stale
    rounds = 40
    inside = 0
    peak = 0
    guard = threading.Lock()
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        nonlocal inside, peak
        run = (f"t{i}", 1)
        try:
            barrier.wait()
            for _ in range(rounds):
                while not lease.try_acquire(run, 0.01, label=f"t{i}"):
                    pass
                with guard:
                    inside += 1
                    peak = max(peak, inside)
                assert lease.holder() == run
                with guard:
                    inside -= 1
                assert lease.release(run) is True
        except BaseException as e:  # surfaced in the main thread
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads)
    assert errors == []
    assert peak == 1
    assert lease.holder() is None
