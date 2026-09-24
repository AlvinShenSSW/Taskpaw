"""#179 cross-plugin GPU hand-off: a real `AvsubsInstance` and a real
`JasnaInstance` share ONE process-wide lease on a frozen fake clock.

Both run on their own suites' fakes (WhisperJAV children through
`AV.ChildProcess` / `J.ChildProcess`, translators through `AV.Translator` /
`J.Translator`, `jasna.exe` through the patched `subprocess.Popen`), so nothing
real is executed. Every step is an explicit `check()`: no reservation expires
and no waiter goes stale unless a test moves the clock.
"""

from __future__ import annotations

import pytest
from test_avsubs import _setup as _av_setup
from test_jasna_subs import _setup as _j_setup

from taskpaw_v3.core import gpu_lease


@pytest.fixture
def clock():
    t = [1000.0]
    gpu_lease._reset_for_tests(clock=lambda: t[0])
    return t


def _pair(tmp_path, monkeypatch, *, av_full=(), **jkw):
    """(avsubs harness, jasna harness). avsubs is「AV」, Jasna is「JASNA」."""
    av = _av_setup(tmp_path / "av", monkeypatch, full=av_full)
    jn = _j_setup(tmp_path / "jn", monkeypatch, **jkw)
    return av, jn


def _finish_restore(jn, i: int = -1, rc: int = 0) -> None:
    jn.launcher.procs[i]._rc = rc


# ── (a) avsubs holds → Jasna's restore and subs-only launches wait ─────────
@pytest.mark.parametrize("work", ["restore", "subs_only"])
def test_avsubs_holding_makes_both_jasna_launch_kinds_wait(
    tmp_path, monkeypatch, clock, work
):
    jkw = {"pending": ["p.mp4"]} if work == "restore" else {"restored": ["e.mp4"]}
    av, jn = _pair(tmp_path, monkeypatch, av_full=["a.mp4"], rcs=[None], **jkw)
    av.inst.start(av.emit)
    assert gpu_lease.holder() == av.inst._run
    jn.inst.start(jn.emit)
    assert jn.launcher.n == 0 and jn.spawner.argvs == []
    assert jn.inst._gpu_waiting and jn.inst._run in gpu_lease.waiters()
    if work == "subs_only":
        assert [p.name for p in jn.inst._subs_only] == ["e.mp4"]
    st = jn.inst.check(jn.emit)
    assert st.detail.startswith("waiting for GPU (held by AV)")
    assert jn.launcher.n == 0 and jn.spawner.argvs == []
    assert gpu_lease.holder() == av.inst._run
    assert jn.evs == [] and av.evs == []  # waiting is never an event
    av.inst.stop(timeout=1)
    jn.inst.stop(timeout=1)


# ── (b) avsubs finishes its file → Jasna's turn, avsubs's retry refused ────
def test_avsubs_finishing_a_file_hands_the_gpu_to_waiting_jasna(
    tmp_path, monkeypatch, clock
):
    av, jn = _pair(
        tmp_path, monkeypatch, av_full=["a.mp4", "b.mp4"], pending=["p.mp4"], rcs=[None]
    )
    av.inst.start(av.emit)
    jn.inst.start(jn.emit)
    assert jn.inst._gpu_waiting
    av.spawner.last.finish(0, state="empty", text="")  # a: no speech, no LLM
    st = av.inst.check(av.emit)  # a's GPU work ends → released → b refused
    assert av.inst._settled["a.mp4"] == ("completed", "no speech")
    assert len(av.spawner.argvs) == 1  # b did not jump Jasna's turn
    assert gpu_lease.holder() is None
    assert gpu_lease.reserved_for() == jn.inst._run
    assert st.state == "idle" and st.metrics["phase"] == "waiting_gpu"
    assert st.detail == "waiting for GPU (held by JASNA) · 1/2 done"
    st = av.inst.check(av.emit)  # still inside the reservation: refused again
    assert len(av.spawner.argvs) == 1 and av.inst._waiting_gpu
    jn.inst.check(jn.emit)  # Jasna's next check takes its turn
    assert jn.launcher.n == 1 and gpu_lease.holder() == jn.inst._run
    st = av.inst.check(av.emit)
    assert st.metrics["phase"] == "waiting_gpu"
    assert st.detail == "waiting for GPU (held by JASNA) · 1/2 done"
    assert len(av.spawner.argvs) == 1
    assert [e for e in av.evs if e[0] == "done"] == []
    av.inst.stop(timeout=1)
    jn.inst.stop(timeout=1)


# ── (c) Jasna finishes one file's restore + ASR → avsubs's turn ────────────
def test_jasna_finishing_restore_and_asr_hands_the_gpu_to_waiting_avsubs(
    tmp_path, monkeypatch, clock
):
    av, jn = _pair(
        tmp_path,
        monkeypatch,
        av_full=["a.mp4"],
        pending=["p.mp4", "q.mp4"],
        rcs=[None, None],
    )
    jn.inst.start(jn.emit)
    assert gpu_lease.holder() == jn.inst._run and jn.launcher.n == 1
    av.inst.start(av.emit)
    assert av.inst._waiting_gpu and av.spawner.argvs == []

    _finish_restore(jn, 0)
    jn.inst.check(jn.emit)  # p restored → p's ASR under the SAME hold
    assert len(jn.spawner.argvs) == 1
    assert gpu_lease.holder() == jn.inst._run
    st = av.inst.check(av.emit)  # the file-scoped hold is not given mid-file
    assert av.spawner.argvs == []
    assert st.detail == "waiting for GPU (held by JASNA) · 0/1 done"

    jn.spawner.last.finish(0)
    st = jn.inst.check(jn.emit)  # p's GPU work ends → released → q refused
    assert jn.launcher.n == 1  # Jasna's next file did not jump the queue
    assert jn.inst._gpu_waiting
    assert gpu_lease.reserved_for() == av.inst._run
    assert "waiting for GPU (held by AV)" in st.detail
    av.inst.check(av.emit)  # avsubs's next check takes its turn
    assert len(av.spawner.argvs) == 1
    assert gpu_lease.holder() == av.inst._run
    st = jn.inst.check(jn.emit)
    assert jn.launcher.n == 1 and "waiting for GPU (held by AV)" in st.detail
    av.inst.stop(timeout=1)
    jn.inst.stop(timeout=1)


# ── (d) avsubs 3-strike abort with a live ASR child ────────────────────────
def test_avsubs_abort_keeps_the_lease_until_its_kill_then_jasna_acquires(
    tmp_path, monkeypatch, clock
):
    av, jn = _pair(
        tmp_path,
        monkeypatch,
        av_full=["a.mp4", "b.mp4", "c.mp4", "d.mp4"],
        pending=["p.mp4"],
        rcs=[None],
    )
    av.inst.start(av.emit)
    for _ in range(3):  # a, b, c transcribed (Jasna not started yet)
        av.spawner.last.finish(0)
        av.inst.check(av.emit)
    live = av.spawner.last  # d's ASR, running
    assert live.rc is None and gpu_lease.holder() == av.inst._run
    jn.inst.start(jn.emit)
    assert jn.inst._gpu_waiting and jn.launcher.n == 0

    seen: dict = {}
    real_kill = live.terminate_tree

    def kill(timeout: float = 5.0):
        # The last moment before the tree dies: the lease is still avsubs's,
        # and a Jasna check right now cannot take it.
        seen["holder"] = gpu_lease.holder()
        jn.inst.check(jn.emit)
        seen["launches"] = jn.launcher.n
        return real_kill(timeout)

    live.terminate_tree = kill  # type: ignore[method-assign]
    for rel in ("a.mp4", "b.mp4", "c.mp4"):
        av.translators[0].answer(rel, ok=False)
    jn.inst.check(jn.emit)  # before avsubs settles anything: still refused
    assert jn.launcher.n == 0
    st = av.inst.check(av.emit)  # 3rd failure → abort → kill → release
    assert st.state == "degraded"
    assert seen == {"holder": av.inst._run, "launches": 0}
    assert gpu_lease.holder() is None
    assert gpu_lease.reserved_for() == jn.inst._run
    jn.inst.check(jn.emit)  # Jasna's next check acquires
    assert jn.launcher.n == 1 and gpu_lease.holder() == jn.inst._run
    assert av.inst.check(av.emit).state == "degraded"
    assert len(av.spawner.argvs) == 4
    av.inst.stop(timeout=1)
    jn.inst.stop(timeout=1)


# ── (e) Jasna disabling subtitles mid-restore keeps the lease ──────────────
def test_jasna_disabling_subs_mid_restore_keeps_the_lease_until_it_ends(
    tmp_path, monkeypatch, clock
):
    av, jn = _pair(
        tmp_path,
        monkeypatch,
        av_full=["a.mp4"],
        pending=["p.mp4", "q.mp4"],
        rcs=[None, None],
    )
    jn.inst.start(jn.emit)
    av.inst.start(av.emit)
    assert av.inst._waiting_gpu
    with jn.inst._launch_lock:
        jn.inst._disable_subs("test", jn.emit)
    jn.inst._run_deferred()
    jn.inst.check(jn.emit)
    assert gpu_lease.holder() == jn.inst._run  # the restore still runs
    st = av.inst.check(av.emit)
    assert av.spawner.argvs == []
    assert st.detail == "waiting for GPU (held by JASNA) · 0/1 done"

    _finish_restore(jn, 0)
    jn.inst.check(jn.emit)  # p restored; no subtitles → the hold is given
    assert jn.spawner.argvs == [] and jn.launcher.n == 1
    assert jn.inst._gpu_waiting
    assert gpu_lease.reserved_for() == av.inst._run
    av.inst.check(av.emit)
    assert len(av.spawner.argvs) == 1 and gpu_lease.holder() == av.inst._run
    av.inst.stop(timeout=1)
    jn.inst.stop(timeout=1)


# ── (f) stopping either side frees the GPU for the other's next check ──────
@pytest.mark.parametrize("stopper", ["avsubs", "jasna"])
def test_stopping_either_side_lets_the_other_acquire_on_its_next_check(
    tmp_path, monkeypatch, clock, stopper
):
    av, jn = _pair(
        tmp_path, monkeypatch, av_full=["a.mp4"], pending=["p.mp4"], rcs=[None]
    )
    if stopper == "avsubs":
        holder, waiter = av, jn
        av.inst.start(av.emit)
        jn.inst.start(jn.emit)
    else:
        holder, waiter = jn, av
        jn.inst.start(jn.emit)
        av.inst.start(av.emit)
    assert gpu_lease.holder() == holder.inst._run
    waiter.inst.check(waiter.emit)
    assert gpu_lease.holder() == holder.inst._run  # still waiting
    holder.inst.stop(timeout=1)
    assert gpu_lease.holder() is None
    assert holder.inst._run not in gpu_lease.waiters()
    waiter.inst.check(waiter.emit)
    assert gpu_lease.holder() == waiter.inst._run
    if stopper == "avsubs":
        assert jn.launcher.n == 1
    else:
        assert len(av.spawner.argvs) == 1
    waiter.inst.stop(timeout=1)
    assert gpu_lease.holder() is None and gpu_lease.waiters() == []
