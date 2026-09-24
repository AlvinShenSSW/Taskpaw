"""#179 Jasna × the process-wide GPU lease (AC7, C3/M12, C5/D7, C11).

The lease is the real `core.gpu_lease` on a fake clock (reset per test by the
autouse fixture; re-reset here with `clock=`), and "the other GPU task" is a
bare `gpu_lease.try_acquire(OTHER, …, label="Other")`. Jasna itself runs on the
`test_jasna` / `test_jasna_subs` fakes: nothing real is executed.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest
from test_jasna import _probe
from test_jasna_subs import _done, _keyed, _setup

from taskpaw_v3.core import gpu_lease
from taskpaw_v3.monitors.plugins import jasna as J
from taskpaw_v3.monitors.plugins.jasna import JasnaInstance

OTHER = ("other", 1)


@pytest.fixture
def clock():
    """A frozen lease clock: no reservation expires and no waiter goes stale
    unless a test moves `clock[0]`."""
    t = [1000.0]
    gpu_lease._reset_for_tests(clock=lambda: t[0])
    return t


def _other_try() -> bool:
    return gpu_lease.try_acquire(OTHER, 1.0, label="Other")


def _release_spy(monkeypatch) -> list:
    calls: list = []
    real = gpu_lease.release

    def spy(run):
        calls.append(run)
        return real(run)

    monkeypatch.setattr(gpu_lease, "release", spy)
    return calls


def _withdraw_spy(monkeypatch) -> list:
    calls: list = []
    real = gpu_lease.withdraw

    def spy(run):
        calls.append(run)
        real(run)

    monkeypatch.setattr(gpu_lease, "withdraw", spy)
    return calls


def _finish_restore(r, i: int = -1, rc: int = 0) -> None:
    r.launcher.procs[i]._rc = rc


# ── lease before the probe (D5) / refused → wait ──────────────────────────
def test_refused_restore_waits_before_the_probe_and_launches_once_free(
    tmp_path, monkeypatch, clock
):
    probe = _probe()
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"], probe=probe)
    assert _other_try()
    r.inst.start(r.emit)
    assert r.launcher.n == 0
    assert probe.calls == []  # D5: the probe never runs while refused
    assert [p.name for p in r.inst._pending] == ["a.mp4", "b.mp4"]
    assert r.inst._run in gpu_lease.waiters()
    st = r.inst.check(r.emit)
    assert st.state == "idle"
    assert st.detail == "waiting for GPU (held by Other) · 0/2 done · subs 0/2"
    assert st.metrics["phase"] == "restore"
    assert "current_file" not in st.metrics
    assert r.evs == []  # waiting is not an event
    assert r.launcher.n == 0 and probe.calls == []

    assert gpu_lease.release(OTHER)  # reserved for the waiting Jasna run
    assert gpu_lease.reserved_for() == r.inst._run
    st = r.inst.check(r.emit)
    assert r.launcher.n == 1 and probe.calls == ["a.mp4"]
    assert gpu_lease.holder() == r.inst._run
    assert st.state == "running" and st.metrics["current_file"] == "a.mp4"
    assert not r.inst._gpu_waiting
    r.inst.stop(timeout=1)
    assert gpu_lease.holder() is None


def test_subs_only_asr_acquires_and_waits_when_refused(tmp_path, monkeypatch, clock):
    r = _setup(tmp_path, monkeypatch, restored=["e.mp4"])
    assert _other_try()
    r.inst.start(r.emit)
    assert r.spawner.argvs == []
    assert [p.name for p in r.inst._subs_only] == ["e.mp4"]  # still first in line
    assert "e.mp4" not in r.inst._settled
    st = r.inst.check(r.emit)
    assert st.state == "idle" and st.detail.startswith(
        "waiting for GPU (held by Other)"
    )
    assert r.evs == [] and r.spawner.argvs == []
    assert gpu_lease.release(OTHER)
    st = r.inst.check(r.emit)
    assert len(r.spawner.argvs) == 1  # the subs-only ASR acquired the lease
    assert gpu_lease.holder() == r.inst._run
    assert st.state == "running" and st.metrics["phase"] == "subs"
    r.spawner.last.finish(0)
    r.inst.check(r.emit)
    assert gpu_lease.holder() is None  # the file's GPU work is over
    r.inst.stop(timeout=1)


# ── the file-scoped hold (C5/D7) ──────────────────────────────────────────
def test_the_hold_spans_restore_and_asr_and_the_waiter_gets_the_next_file(
    tmp_path, monkeypatch, clock
):
    releases = _release_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"], rcs=[None, None])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    assert gpu_lease.holder() == inst._run
    assert not _other_try()  # the other task starts waiting

    _finish_restore(r, 0)
    inst.check(emit)  # a restored → a's ASR, with the SAME hold
    assert len(r.spawner.argvs) == 1
    assert releases == []  # no release between restore and ASR
    assert gpu_lease.holder() == inst._run
    assert not _other_try()

    r.spawner.last.finish(0)
    st = inst.check(emit)  # a's GPU work ends → released; the waiter's turn
    assert releases == [inst._run]
    assert r.launcher.n == 1  # b did not jump the queue
    assert inst._gpu_waiting
    assert st.state == "running"  # a's translation is pending
    assert st.metrics["phase"] == "translate"
    assert st.detail == (
        "waiting for GPU (held by Other) · translating 1 · 1/2 done · subs 0/2"
    )
    assert not _keyed(r.evs, "j1:launch")
    assert _other_try()  # the reserved waiter takes it
    st = inst.check(emit)
    assert r.launcher.n == 1 and "held by Other" in st.detail

    assert gpu_lease.release(OTHER)
    inst.check(emit)
    assert r.launcher.n == 2 and r.launcher.inputs()[-1] == "b.mp4"
    assert gpu_lease.holder() == inst._run
    inst.stop(timeout=1)
    assert gpu_lease.holder() is None


@pytest.mark.parametrize("av", [True, False])
def test_a_failed_restore_keeps_the_hold_for_its_same_file_retry(
    tmp_path, monkeypatch, clock, av
):
    releases = _release_spy(monkeypatch)
    r = _setup(
        tmp_path,
        monkeypatch,
        pending=["a.mp4", "b.mp4"],
        rcs=[None, None, None],
        av_translate=av,
    )
    inst, emit = r.inst, r.emit
    inst.start(emit)
    assert not _other_try()
    _finish_restore(r, 0, rc=1)
    inst.check(emit)  # a failed → requeued → retried under the same hold
    assert r.launcher.inputs() == ["a.mp4", "a.mp4"]
    assert releases == []
    assert gpu_lease.holder() == inst._run
    assert not _other_try()
    _finish_restore(r, 1, rc=1)
    inst.check(emit)  # a's final failure: its GPU work is over
    assert releases == [inst._run]
    assert r.launcher.n == 2 and inst._gpu_waiting
    assert _other_try()
    inst.stop(timeout=1)


def test_subs_disabled_during_a_restore_keeps_the_hold_until_it_ends(
    tmp_path, monkeypatch, clock
):
    releases = _release_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"], rcs=[None, None])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    with inst._launch_lock:
        inst._disable_subs("test", emit)
    inst._run_deferred()
    inst.check(emit)
    assert gpu_lease.holder() == inst._run and releases == []
    assert not _other_try()
    _finish_restore(r, 0)
    inst.check(emit)  # a restored; no subtitles → the hold is given
    assert r.spawner.argvs == []
    assert releases == [inst._run]
    assert r.launcher.n == 1 and inst._gpu_waiting  # Other's turn first
    inst.stop(timeout=1)


# ── Stop and the carried hand-over (M4, D2) ───────────────────────────────
def test_stop_racing_the_carried_hand_over_to_the_asr(tmp_path, monkeypatch, clock):
    releases = _release_spy(monkeypatch)
    withdraws = _withdraw_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"], rcs=[None])
    inst, emit = r.inst, r.emit
    seen: dict = {}
    real = JasnaInstance._start_subs

    def racing(self, video, emit_):
        seen["carried"] = self._carried
        seen["holder"] = self._gpu_holder
        self.stop(timeout=1)  # Stop lands between the exit and the hand-over
        seen["lease_after_stop"] = gpu_lease.holder()
        return real(self, video, emit_)

    monkeypatch.setattr(JasnaInstance, "_start_subs", racing)
    inst.start(emit)
    _finish_restore(r, 0)
    inst.check(emit)
    assert seen["carried"] is not None and seen["holder"] is seen["carried"]
    # M4: stop() itself gave the carried hold — not the later hand-over
    assert seen["lease_after_stop"] is None
    assert releases == [inst._run]  # … and only once
    assert inst._run in withdraws
    assert gpu_lease.holder() is None and inst._gpu_holder is None
    assert r.spawner.argvs == [] and r.launcher.n == 1
    assert inst._carried is None
    assert _other_try()


def test_stop_racing_the_carried_hand_over_to_a_retry(tmp_path, monkeypatch, clock):
    releases = _release_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"], rcs=[None])
    inst, emit = r.inst, r.emit
    real = JasnaInstance._launch_next

    def racing(self, emit_):
        if self._carried is not None:
            self.stop(timeout=1)
        return real(self, emit_)

    monkeypatch.setattr(JasnaInstance, "_launch_next", racing)
    inst.start(emit)
    _finish_restore(r, 0, rc=1)  # → requeued, hold carried to the retry
    inst.check(emit)
    assert r.launcher.n == 1  # the retry never launched
    assert releases == [inst._run]
    assert gpu_lease.holder() is None and inst._carried is None
    assert not _done(r.evs)


def test_stop_during_the_probe_after_the_transfer_releases_once(
    tmp_path, monkeypatch, clock
):
    releases = _release_spy(monkeypatch)
    holder: dict = {}

    def probe(video, ffprobe):
        inst = holder.get("inst")
        if inst is not None and Path(video).name == "b.mp4":
            inst.stop(timeout=1)
        return (1920, 1080)

    r = _setup(
        tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"], rcs=[None], probe=probe
    )
    holder["inst"] = r.inst
    r.inst.start(r.emit)
    _finish_restore(r, 0)
    r.inst.check(r.emit)  # a → ASR → (a's ASR still live, so b waits) …
    r.spawner.last.finish(0)
    r.inst.check(r.emit)  # … a's work ends; b is taken, stop lands in its probe
    assert r.launcher.n == 1
    assert releases == [r.inst._run, r.inst._run]  # a's hold, then b's
    assert gpu_lease.holder() is None and r.inst._gpu_holder is None


def test_stop_while_waiting_withdraws(tmp_path, monkeypatch, clock):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    assert _other_try()
    r.inst.start(r.emit)
    assert r.inst._run in gpu_lease.waiters()
    r.inst.stop(timeout=1)
    assert r.inst._run not in gpu_lease.waiters()
    assert gpu_lease.holder() == OTHER  # never touches another run's hold
    assert gpu_lease.release(OTHER)
    assert gpu_lease.reserved_for() is None  # no ghost reservation for Jasna
    assert _other_try()


def test_a_refused_try_while_stopping_withdraws_at_once(tmp_path, monkeypatch, clock):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    assert _other_try()
    r.inst._stopping.set()
    assert r.inst._gpu_acquire() is False
    assert r.inst._run not in gpu_lease.waiters()  # D6


# ── withdraw at the run's ends ────────────────────────────────────────────
def test_done_withdraws(tmp_path, monkeypatch, clock):
    withdraws = _withdraw_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"], av_translate=False)
    r.inst.start(r.emit)
    withdraws.clear()
    r.inst.check(r.emit)
    assert len(_done(r.evs)) == 1
    assert withdraws == [r.inst._run]
    assert gpu_lease.holder() is None


def test_abort_withdraws(tmp_path, monkeypatch, clock):
    withdraws = _withdraw_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4", "c.mp4"], rcs=[1] * 6)
    r.inst.start(r.emit)
    withdraws.clear()
    for _ in range(6):
        st = r.inst.check(r.emit)
    assert st.state == "degraded"
    assert r.inst._run in withdraws
    assert gpu_lease.holder() is None
    assert _other_try()


def test_launch_error_withdraws_and_releases(tmp_path, monkeypatch, clock):
    withdraws = _withdraw_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])

    def popen(argv, creationflags=0, **kw):
        raise OSError("blocked")

    monkeypatch.setattr(J.subprocess, "Popen", popen)
    r.inst.start(r.emit)
    assert r.inst._launch_error is not None
    assert r.inst._run in withdraws
    assert gpu_lease.holder() is None


def test_a_retry_that_finds_nothing_needing_the_gpu_withdraws(
    tmp_path, monkeypatch, clock
):
    withdraws = _withdraw_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, restored=["e.mp4"])
    assert _other_try()
    r.inst.start(r.emit)
    assert r.inst._gpu_waiting
    with r.inst._launch_lock:  # the waiting work goes away meanwhile
        r.inst._disable_subs("test", r.emit)
    r.inst._run_deferred()
    withdraws.clear()
    r.inst.check(r.emit)
    assert not r.inst._gpu_waiting
    assert r.inst._run in withdraws  # D12
    assert r.inst._run not in gpu_lease.waiters()
    assert r.spawner.argvs == []


def test_a_retry_that_finds_nothing_withdraws_while_a_translation_is_pending(
    tmp_path, monkeypatch, clock
):
    # D12 on its own: `done` cannot fire (a translation is still queued), so
    # only the retry's own withdraw can drop the wait.
    r = _setup(tmp_path, monkeypatch, restored=["d.mp4", "e.mp4"], ja=["d.mp4"])
    inst, emit = r.inst, r.emit
    assert _other_try()
    inst.start(emit)  # d → translation submitted; e's ASR refused → waiting
    assert [q.job_id for q in r.translators[0].submitted] == ["d.mp4"]
    assert inst._gpu_waiting and inst._run in gpu_lease.waiters()
    with inst._launch_lock:  # e no longer needs the GPU
        inst._settle("e.mp4", "skipped", "unstable", emit)
    st = inst.check(emit)
    assert not inst._gpu_waiting
    assert not _done(r.evs) and st.metrics["subs_translating"] == 1
    assert inst._run not in gpu_lease.waiters()
    assert gpu_lease.release(OTHER)
    assert gpu_lease.reserved_for() is None  # no reservation for a ghost
    assert r.spawner.argvs == []
    inst.stop(timeout=1)


# ── S5: waiting flag and detail ───────────────────────────────────────────
def test_a_successful_take_clears_the_waiting_flag(tmp_path, monkeypatch, clock):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"], restored=["e.mp4"])
    inst, emit = r.inst, r.emit
    assert _other_try()
    inst.start(emit)
    assert inst._gpu_waiting
    assert gpu_lease.release(OTHER)
    inst._launch_next(emit)  # any dispatching path, not only the retry
    assert r.launcher.n == 1
    assert not inst._gpu_waiting

    inst._pending = []  # the subs-only path clears it too
    inst._gpu_waiting = True
    _finish_restore(r, 0)
    inst._process = None
    inst._gpu_give(inst._restore_hold)
    inst._start_subs(inst._subs_only.pop(0), emit)
    assert len(r.spawner.argvs) == 1
    assert not inst._gpu_waiting
    inst.stop(timeout=1)


def test_waiting_detail_never_names_this_run_when_the_lease_is_reserved_for_it(
    tmp_path, monkeypatch, clock
):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"], av_translate=False)
    assert _other_try()
    r.inst.start(r.emit)
    st = r.inst._build_status("idle")
    assert st.detail == "waiting for GPU (held by Other) · 0/1 done"
    assert gpu_lease.release(OTHER)  # free, reserved for the waiting Jasna run
    assert gpu_lease.reserved_for() == r.inst._run
    st = r.inst._build_status("idle")
    assert st.detail == "waiting for GPU · 0/1 done"
    assert "JASNA" not in st.detail
    r.inst.stop(timeout=1)


# ── exception safety / restart ────────────────────────────────────────────
def test_a_probe_that_raises_gives_the_hold(tmp_path, monkeypatch, clock):
    def probe(video, ffprobe):
        raise RuntimeError("probe blew up")

    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"], probe=probe)
    with pytest.raises(RuntimeError):
        r.inst.start(r.emit)
    assert gpu_lease.holder() is None  # N5
    assert r.inst._gpu_holder is None
    assert r.launcher.n == 0


def test_restart_releases_and_withdraws_the_old_runs_lease(
    tmp_path, monkeypatch, clock
):
    # N7: a hold / wait of the OLD run is cleared before `_run` changes.
    r = _setup(tmp_path, monkeypatch, av_translate=False)
    r.inst.start(r.emit)  # nothing to process: no child, no translator
    old = r.inst._run
    assert r.inst._gpu_take(object())
    assert gpu_lease.holder() == old
    r.inst.start(r.emit)
    assert r.inst._run != old
    assert gpu_lease.holder() is None and r.inst._gpu_holder is None

    # an old run's wait registration goes too
    (r.inp / "a.mp4").write_bytes(b"video")
    assert _other_try()
    r.inst.start(r.emit)
    waiting = r.inst._run
    assert waiting in gpu_lease.waiters()
    r.inst.start(r.emit)
    assert waiting not in gpu_lease.waiters()
    assert r.inst._run in gpu_lease.waiters()  # the new run waits in its place
    r.inst.stop(timeout=1)
    assert gpu_lease.waiters() == []


# ── C11: a WhisperJAV process survives the kill ──────────────────────────
def test_a_survivor_alerts_once_still_releases_and_keeps_the_guard(
    tmp_path, monkeypatch, clock, caplog
):
    releases = _release_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"], rcs=[0, None])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    inst.check(emit)  # a restored → a's ASR (live)
    child = r.spawner.last
    kills: list[float] = []

    def survive(timeout: float = 5.0) -> bool:
        kills.append(timeout)
        return False

    child.terminate_tree = survive  # type: ignore[method-assign]
    with inst._launch_lock:
        inst._disable_subs("test", emit)
    with caplog.at_level(logging.ERROR, logger="taskpaw.monitors.jasna"):
        inst._run_deferred()
    assert len(kills) == 1
    assert len(_keyed(r.evs, "j1:subs-survivor")) == 1
    assert any("survived" in rec.getMessage() for rec in caplog.records)
    assert releases == [inst._run]  # C11: the lease is released anyway
    assert inst._subs_job is not None  # the live-child guard is kept …
    inst.check(emit)
    assert r.launcher.n == 1  # … so no restore starts next to it
    child.rc = 1  # it finally exits
    inst.check(emit)
    assert inst._subs_job is None
    assert kills == [5.0]  # IR5: reaped by poll_asr, no tree kill on the reap
    assert "join_readers" in child.calls
    assert r.launcher.n == 2  # b goes on
    assert len(_keyed(r.evs, "j1:subs-survivor")) == 1
    inst.stop(timeout=1)


def test_a_survivor_at_stop_is_logged_and_the_lease_released(
    tmp_path, monkeypatch, clock, caplog
):
    r = _setup(tmp_path, monkeypatch, restored=["e.mp4"])
    r.inst.start(r.emit)
    child = r.spawner.last
    child.terminate_tree = lambda timeout=5.0: False  # type: ignore[method-assign]
    with caplog.at_level(logging.ERROR, logger="taskpaw.monitors.jasna"):
        r.inst.stop(timeout=1)
    assert any("5151" in rec.getMessage() for rec in caplog.records)
    assert gpu_lease.holder() is None


# ── C3/M12: the publish-temp sweep is age-gated ───────────────────────────
def test_the_srt_temp_sweep_is_age_gated(tmp_path, monkeypatch, clock):
    r = _setup(tmp_path, monkeypatch, av_translate=True)
    fresh = r.out / "a_restored.srt.7.tmp"
    old = r.out / "b_restored.ja.srt.3.tmp"
    for p in (fresh, old):
        p.write_text("x", encoding="utf-8")
    stamp = time.time() - 11 * 60
    os.utime(old, (stamp, stamp))
    junk = r.out / ".avsubs" / "tmp" / "audio.wav"
    junk.parent.mkdir(parents=True, exist_ok=True)
    junk.write_bytes(b"x")
    r.inst.start(r.emit)
    assert fresh.exists()  # may belong to a task still publishing
    assert not old.exists()
    assert not junk.exists()  # the .avsubs/tmp rmtree is unchanged
    r.inst.stop(timeout=1)
