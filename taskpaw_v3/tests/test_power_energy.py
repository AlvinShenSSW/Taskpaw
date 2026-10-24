"""PWR-2 (#288): per-film energy in the media workflow and the 预计耗电 prediction.

The dispatcher reads the film's power counter at claim and at resolve for
every attempt (PW04/PW05) and keeps the delta in the attempt evidence; the
board sums the current revision's attempts per stage; the 待处理 list predicts
per machine × resolution × tier from the median Wh per film-hour (PW13).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from taskpaw_v3.core import power
from taskpaw_v3.core.config import PowerSettings
from taskpaw_v3.core.gpu_lease import GpuLease
from taskpaw_v3.tests.test_workflow_local_dispatch import (
    add_film,
    plan_film,
    runtime,
    stage,
)
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import board
from taskpaw_v3.workflow import energy as en
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.executors import ExecOutcome, SyntheticExecutor
from taskpaw_v3.workflow.local_dispatch import LocalDispatcher


class Clock:
    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now


class Ticking(SyntheticExecutor):
    """A stage that lets the sampler take `ticks` 5 s samples, then ends."""

    def __init__(self, stage_, rig, *, ticks=3, fail=None, uses_gpu=True):
        super().__init__(stage_, uses_gpu=uses_gpu)
        self.rig, self.ticks, self.fail = rig, ticks, fail
        self.seen: list[dict] = []

    def run(self, context):
        for _ in range(self.ticks):
            self.rig.tick()
        self.seen.append(dict(self.rig.dispatcher.status().current or {}))
        if self.fail:
            failure, self.fail = self.fail, None  # fails once, then succeeds
            return ExecOutcome.failed(failure, "boom")
        return ExecOutcome.succeeded()


class Rig:
    """A real PowerSampler (200 W GPU, idle CPU, 30 W other = 240 W) whose
    owner is the dispatcher's running film, ticked by hand."""

    def __init__(self, rt, *, sampler=True):
        self.mono, self.wall = Clock(), Clock(1_800_000_000.0)
        self.rt = rt
        self.enabled = True  # `power.enabled`, read on every tick (applied live)
        self.sampler = (
            power.PowerSampler(
                lambda: PowerSettings(
                    enabled=self.enabled, tdp_w=65, idle_w=10, other_w=30
                ),
                activity=power.activity_source(
                    lambda _iid: None, lambda: self.dispatcher
                ),
                gpu=lambda: 200.0,
                cpu=lambda: 0.0,
                clock=self.mono,
                wall=self.wall,
                platform="win32",
            )
            if sampler
            else None
        )
        self.dispatcher = LocalDispatcher(
            rt, lease=GpuLease(), poll=0.05, power=lambda: self.sampler
        )
        self.tick()  # the first sample only opens the window

    def tick(self) -> None:
        self.mono.now += 5
        self.wall.now += 5
        if self.sampler is not None:
            self.sampler.tick()

    def stall(self, seconds: float) -> None:
        """Time passes without a sample (a stalled thread, a sleeping machine)."""
        self.mono.now += seconds
        self.wall.now += seconds


@pytest.fixture
def rt(tmp_path):
    value = runtime(tmp_path / "wf")
    try:
        yield value
    finally:
        value.close()


def attempts(rt, film_id, name):
    return rt.store.list_attempts(rt.operator, stage(rt, film_id, name).job_id)


def board_row(rt, film_id):
    (film,) = rt.store.film_board(rt.operator, film_id=film_id)
    return film


# ── the dispatcher keeps every attempt's energy (PW04/PW05) ─────────────────
def test_each_resolved_attempt_keeps_its_energy_in_the_evidence(rt):
    rig = Rig(rt)
    rt.executors["restore"] = Ticking("restore", rig)
    film = add_film(rt)
    assert rig.dispatcher.run_once() is True
    (attempt,) = attempts(rt, film, "restore")
    energy = attempt.evidence[-1].summary.energy
    assert energy is not None
    # three 5 s windows at 240 W
    assert energy.wh == pytest.approx(1.0)
    assert energy.gpu_wh == pytest.approx(200 * 15 / 3600, abs=1e-3)
    assert energy.seconds == pytest.approx(15.0)
    assert energy.avg_w == pytest.approx(240.0)
    assert energy.peak_w == pytest.approx(240.0)


def test_a_failed_attempt_counts_and_the_stage_sums_every_attempt(rt):
    rig = Rig(rt)
    rt.executors["restore"] = Ticking("restore", rig, fail="transient")
    film = add_film(rt)
    assert rig.dispatcher.run_once() is True  # fails, is requeued (C6)
    assert rig.dispatcher.run_once() is True  # succeeds
    failed, succeeded = attempts(rt, film, "restore")
    assert failed.status == "failed" and succeeded.status == "succeeded"
    assert failed.evidence[-1].summary.energy is not None
    restore = next(s for s in board_row(rt, film).stages if s.stage == "restore")
    assert len(restore.attempt_energy) == 2
    value = en.film_energy(board_row(rt, film))
    assert value is not None
    assert value["stages"]["restore"]["wh"] == pytest.approx(2.0)
    assert value["stages"]["restore"]["attempts"] == 2
    assert value["stages"]["restore"]["seconds"] == pytest.approx(30.0)
    assert value["partial"] is False
    assert value["total_wh"] == pytest.approx(2.0)


def test_without_a_sampler_the_evidence_keeps_its_old_shape(rt):
    rig = Rig(rt, sampler=False)
    rt.executors["restore"] = Ticking("restore", rig)
    film = add_film(rt)
    assert rig.dispatcher.run_once() is True
    (attempt,) = attempts(rt, film, "restore")
    assert attempt.evidence[-1].summary.energy is None
    assert "energy" not in attempt.evidence[-1].summary.model_dump(mode="json")
    assert en.film_energy(board_row(rt, film)) is None
    assert board.film_row(board_row(rt, film), {})["energy"] is None


def test_an_attempt_without_a_value_makes_the_film_partial(rt):
    rig = Rig(rt)
    rt.executors["restore"] = Ticking("restore", rig, fail="transient")
    film = add_film(rt)
    sampler, rig.sampler = rig.sampler, None  # e.g. cut off by a crash
    assert rig.dispatcher.run_once() is True
    rig.sampler = sampler
    rig.tick()  # the long silence is a gap; this sample reopens the window
    assert rig.dispatcher.run_once() is True
    restore = next(s for s in board_row(rt, film).stages if s.stage == "restore")
    assert restore.attempt_energy[0] is None and restore.attempt_energy[1]
    value = en.film_energy(board_row(rt, film))
    assert value["partial"] is True
    assert value["stages"]["restore"]["partial"] is True
    assert value["stages"]["restore"]["wh"] == pytest.approx(1.0)


# ── an unsampled interval is missing energy, never a measured zero (IR02) ──
class Scripted(Ticking):
    """A stage that runs `script(rig)` instead of plain ticks."""

    def __init__(self, stage_, rig, script, **kw):
        super().__init__(stage_, rig, **kw)
        self.script = script

    def run(self, context):
        self.script(self.rig)
        return ExecOutcome.succeeded()


def _restore_energy(rt, rig, script):
    rt.executors["restore"] = Scripted("restore", rig, script)
    film = add_film(rt)
    assert rig.dispatcher.run_once() is True
    (attempt,) = attempts(rt, film, "restore")
    return film, attempt.evidence[-1].summary.energy


def test_with_sampling_disabled_an_attempt_has_no_energy(rt):
    rig = Rig(rt)
    rig.enabled = False

    def script(r):
        for _ in range(3):
            r.tick()

    film, energy = _restore_energy(rt, rig, script)
    assert energy is None  # not 0 Wh
    assert en.film_energy(board_row(rt, film)) is None


def test_sampling_turned_off_mid_attempt_leaves_no_value(rt):
    rig = Rig(rt)

    def off_until_the_end(r):
        r.tick(), r.tick()
        r.enabled = False
        for _ in range(12):
            r.tick()

    _, energy = _restore_energy(rt, rig, off_until_the_end)
    assert energy is None


def test_sampling_off_for_a_while_then_on_again_leaves_no_value(rt):
    rig = Rig(rt)

    def off_then_on(r):
        r.tick(), r.tick()
        r.enabled = False
        for _ in range(12):  # 60 s not sampled
            r.tick()
        r.enabled = True
        for _ in range(3):
            r.tick()

    _, energy = _restore_energy(rt, rig, off_then_on)
    assert energy is None


def test_a_long_gap_between_samples_leaves_no_value(rt):
    rig = Rig(rt)

    def gap(r):
        r.tick(), r.tick()
        r.stall(120)  # no sample for two minutes: the next window is dropped
        for _ in range(3):
            r.tick()

    _, energy = _restore_energy(rt, rig, gap)
    assert energy is None


def test_a_short_scheduling_jitter_still_counts(rt):
    rig = Rig(rt)

    def jitter(r):
        r.tick(), r.tick()
        r.stall(8)  # one dropped window (13 s) is within the tolerance
        for _ in range(3):
            r.tick()

    _, energy = _restore_energy(rt, rig, jitter)
    assert energy is not None and energy.wh > 0


def test_an_unsampled_attempt_makes_the_film_partial_and_leaves_history(rt):
    rig = Rig(rt)
    calls = []

    def first_gaps(r):
        calls.append(1)
        r.tick()
        if len(calls) == 1:
            r.stall(120)
        r.tick(), r.tick()

    class FailOnce(Scripted):
        def run(self, context):
            self.script(self.rig)
            if len(calls) == 1:
                return ExecOutcome.failed("transient", "boom")
            return ExecOutcome.succeeded()

    rt.executors["restore"] = FailOnce("restore", rig, first_gaps)
    film = add_film(rt)
    assert rig.dispatcher.run_once() is True
    assert rig.dispatcher.run_once() is True
    restore = next(s for s in board_row(rt, film).stages if s.stage == "restore")
    assert restore.attempt_energy[0] is None and restore.attempt_energy[1]
    value = en.film_energy(board_row(rt, film))
    assert value["partial"] is True
    assert en.history([board_row(rt, film)]) == []


def test_once_resolved_the_attempt_is_no_longer_running(rt, monkeypatch):
    """IR03: right after the resolution is stored, the board must not add the
    attempt's energy a second time as the running stage's."""
    rig = Rig(rt)
    rt.executors["restore"] = Ticking("restore", rig)
    film = add_film(rt)
    seen = []
    drop = rig.dispatcher._drop_other_workdirs

    def during_cleanup(running):
        current = rig.dispatcher.status().current
        seen.append((current, board.film_row(board_row(rt, film), {}, current=current)))
        drop(running)

    monkeypatch.setattr(rig.dispatcher, "_drop_other_workdirs", during_cleanup)
    assert rig.dispatcher.run_once() is True
    ((current, row),) = seen
    assert current is None
    assert row["energy"]["total_wh"] == pytest.approx(1.0)


def test_prediction_history_is_not_capped_by_the_board(rt):
    """Codex: the board keeps only the newest 20 finished films; the history
    behind 预计耗电 keeps the newest 10 per machine × resolution × tier."""
    from taskpaw_v3.workflow.planning import plan_list

    rig = Rig(rt)
    for name in ("restore", "asr", "translate"):
        rt.executors[name] = Ticking(name, rig)

    def finished(tier):
        film_id = add_film(rt, plan=False)
        plan_film(rt, film_id, tier=tier)
        while rig.dispatcher.run_once():
            pass
        assert stage(rt, film_id, "translate").status == "succeeded"
        return film_id

    standard = [finished("standard") for _ in range(5)]
    for _ in range(20):
        finished("high")
    # cleaned up: the films count as fully finished on the board (D13)
    rt.store._conn.execute(
        "UPDATE stages SET status='succeeded' "
        "WHERE stage IN ('cleanup_input','cleanup_workcopy')"
    )
    shown = {f.film_id for f in rt.store.film_board(rt.operator)}
    assert not shown & set(standard)  # the display board dropped them
    rates = {r["tier"]: r for r in en.rates(rt.store.energy_history(rt.operator))}
    assert rates["standard"]["films"] == 5 and rates["standard"]["wh_per_hour"]
    assert rates["high"]["films"] == 10  # the newest 10 per bucket
    add_film(rt, plan=False)  # a 待处理 entry
    (entry,) = plan_list(rt)["plans"]
    assert entry["prediction"]["standard"] is not None
    assert entry["prediction"]["high"] is not None


def test_a_restarted_sampler_gives_no_value_rather_than_a_wrong_one(rt):
    rig = Rig(rt)

    class Restarting(Ticking):
        def run(self, context):
            # the agent's sampler is replaced mid-stage: the claim's reading
            # belongs to another sampler, so no delta is invented
            rig.sampler = power.PowerSampler(
                lambda: PowerSettings(), gpu=lambda: 1.0, cpu=lambda: 0.0
            )
            return ExecOutcome.succeeded()

    rt.executors["restore"] = Restarting("restore", rig)
    film = add_film(rt)
    assert rig.dispatcher.run_once() is True
    (attempt,) = attempts(rt, film, "restore")
    assert attempt.evidence[-1].summary.energy is None


def test_the_running_stage_reports_its_energy_and_only_a_gpu_stage_its_power(rt):
    rig = Rig(rt)
    restore = Ticking("restore", rig)
    translate = Ticking("translate", rig, uses_gpu=False)
    rt.executors["restore"], rt.executors["translate"] = restore, translate
    rt.executors["asr"] = Ticking("asr", rig)
    film = add_film(rt)
    for _ in range(3):
        assert rig.dispatcher.run_once() is True
    (during,) = restore.seen
    assert during["uses_gpu"] is True
    assert during["energy"]["wh"] == pytest.approx(1.0)
    assert during["power"]["w"] == pytest.approx(240.0)
    (cpu_only,) = translate.seen
    assert cpu_only["uses_gpu"] is False
    assert cpu_only["power"] is None  # PW12: live W only on the GPU owner
    assert cpu_only["energy"]["wh"] == pytest.approx(1.0)
    value = board.film_row(board_row(rt, film), {})["energy"]
    assert set(value["stages"]) == {"restore", "asr", "translate"}
    assert value["total_wh"] == pytest.approx(3.0)
    assert value["stages"]["restore"]["machine"] == en.LOCAL


def test_the_board_carries_the_probe_duration(rt):
    film = add_film(rt)
    assert board_row(rt, film).duration_ms == 120000
    assert board.film_row(board_row(rt, film), {})["duration_ms"] == 120000


# ── pure: film energy, live power, prediction ──────────────────────────────
T0 = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
W1, W2 = identity(), identity()  # two machines (worker ids)


def energy(wh: float, seconds: float = 3600.0, peak: float | None = 300.0):
    return m.AttemptEnergy(
        wh=wh,
        gpu_wh=wh * 0.7,
        cpu_wh=wh * 0.2,
        other_wh=wh * 0.1,
        seconds=seconds,
        avg_w=wh * 3600 / seconds,
        peak_w=peak,
    )


def film(
    *,
    spent: dict[str, tuple] | None = None,
    statuses: dict[str, str] | None = None,
    tier: str | None = "standard",
    width: int = 3840,
    height: int = 2160,
    hours: float = 2.0,
    done_at: float = 0.0,
    machine: str = W1,
) -> m.BoardFilm:
    spent = spent or {}
    statuses = statuses or {}
    return m.BoardFilm(
        film_id=identity(),
        display_name="Film",
        created_at=T0 - timedelta(days=30),
        final_name=None,
        stages=tuple(
            m.BoardStage(
                stage=name,
                status=statuses.get(name, "succeeded" if name in spent else "pending"),
                ended_at=T0 + timedelta(hours=done_at) if name == "translate" else None,
                attempt_energy=spent.get(name, ()),
            )
            for name in board.STAGE_ORDER
        ),
        approval_active=False,
        archive_open=False,
        archive_error=None,
        planned=True,
        plan_ltx_model=tier,
        plan_machine=machine,
        max_width=width,
        max_height=height,
        duration_ms=int(hours * 3_600_000),
    )


def whole(wh_restore: float, **kw) -> m.BoardFilm:
    """A complete run: restore, asr and translate each with one attempt."""
    return film(
        spent={
            "restore": (energy(wh_restore),),
            "asr": (energy(40.0),),
            "translate": (energy(10.0),),
        },
        **kw,
    )


def test_film_energy_sums_stages_with_their_average_and_peak():
    value = en.film_energy(
        film(
            spent={
                "restore": (energy(500.0, 7200, 290.0), energy(100.0, 1800, 310.0)),
                "translate": (energy(10.0, 600, None),),
            }
        )
    )
    restore = value["stages"]["restore"]
    assert restore["wh"] == pytest.approx(600.0)
    assert restore["seconds"] == pytest.approx(9000.0)
    assert restore["avg_w"] == pytest.approx(240.0)
    assert restore["peak_w"] == pytest.approx(310.0)
    assert value["stages"]["translate"]["peak_w"] is None
    assert value["total_wh"] == pytest.approx(610.0)
    assert value["partial"] is False and value["estimated"] is True


def test_film_energy_is_none_until_an_attempt_has_a_value():
    assert en.film_energy(film()) is None
    assert en.film_energy(film(spent={"restore": (None,)})) is None


def test_the_running_attempt_adds_its_energy_so_far():
    row = film(spent={"restore": (energy(100.0),)}, statuses={"restore": "running"})
    current = {
        "film_id": row.film_id,
        "stage": "restore",
        "uses_gpu": True,
        "energy": {
            "wh": 50.0,
            "gpu_wh": 40.0,
            "cpu_wh": 5.0,
            "other_wh": 5.0,
            "seconds": 600.0,
            "avg_w": 300.0,
            "peak_w": 320.0,
        },
        "power": {"w": 286.0, "gpu_w": 210.0, "cpu_w": 46.0, "other_w": 30.0},
    }
    value = en.film_energy(row, current)
    assert value["stages"]["restore"]["wh"] == pytest.approx(150.0)
    assert value["stages"]["restore"]["running"] is True
    assert value["running"] is True
    # someone else's stage is not this film's
    assert en.film_energy(row, {**current, "film_id": "other"})["running"] is False
    # PW12: live W on the GPU-owning row only
    assert board.live_power(row, current) == current["power"]
    assert board.live_power(row, {**current, "uses_gpu": False}) is None
    assert board.live_power(film(), current) is None


def test_an_active_stage_without_a_live_reading_stays_running_and_partial():
    """Codex r5: sampling off or a gap while a later stage runs must not turn
    the film into a finished, complete energy record."""
    row = film(spent={"restore": (energy(100.0),)}, statuses={"scrape": "running"})
    current = {"film_id": row.film_id, "stage": "scrape", "uses_gpu": False}
    value = en.film_energy(row, current)  # no `energy` in current: unknown
    assert value["running"] is True and value["partial"] is True
    assert value["stages"]["scrape"]["running"] is True
    assert value["stages"]["scrape"]["partial"] is True
    assert value["total_wh"] == pytest.approx(100.0)
    # the same film once nothing runs any more is complete again
    assert en.film_energy(row)["running"] is False


def test_rates_take_the_median_of_the_newest_ten_films_per_group():
    films = [whole(100.0 * (i + 1), done_at=i) for i in range(12)]  # 2 h films
    (rate,) = en.rates(films)
    assert (rate["machine"], rate["resolution"], rate["tier"]) == (W1, "4K", "standard")
    assert rate["films"] == 10
    # the newest ten: restore 300..1200 Wh, + 50 Wh, over 2 h → median of
    # (350..1250)/2 = (775 + 825) / 2 / 2
    assert rate["wh_per_hour"] == pytest.approx((775 + 825) / 2 / 2)


def test_fewer_than_three_films_is_not_enough_history():
    films = [whole(300.0, done_at=i) for i in range(2)]
    (rate,) = en.rates(films)
    assert rate["films"] == 2 and rate["wh_per_hour"] is None
    value = en.predict(en.rates(films), W1, "4K", 3_600_000)
    assert value["standard"] is None  # 「历史不足」
    assert value["films"]["standard"] == 2
    assert value["min_films"] == 3


def test_incomplete_partial_or_unknown_length_films_are_left_out():
    films = [
        whole(300.0, done_at=1),
        whole(300.0, done_at=2),
        whole(300.0, done_at=3),
        film(spent={"restore": (energy(900.0),)}),  # translate not finished
        film(  # 「部分」
            spent={"restore": (None, energy(900.0)), "translate": (energy(1.0),)}
        ),
        whole(900.0, hours=0.0),  # no probe duration
    ]
    (rate,) = en.rates(films)
    assert rate["films"] == 3
    assert rate["wh_per_hour"] == pytest.approx(175.0)


def test_prediction_scales_per_tier_resolution_and_machine():
    films = [
        *(whole(300.0, done_at=i) for i in range(3)),  # 4K standard: 175 Wh/h
        *(whole(900.0, tier="high", done_at=i) for i in range(4)),  # 475 Wh/h
        *(whole(60.0, width=1920, height=1080, done_at=i) for i in range(3)),
        *(whole(5000.0, machine=W2, done_at=i) for i in range(3)),
    ]
    table = en.rates(films)
    value = en.predict(table, W1, "4K", int(1.5 * 3_600_000))
    assert value["standard"] == pytest.approx(175.0 * 1.5)
    assert value["high"] == pytest.approx(475.0 * 1.5)
    assert value["good"] is None and value["films"]["good"] == 0
    hd = en.predict(table, W1, "1080p", 3_600_000)
    assert hd["standard"] == pytest.approx(55.0)
    assert en.predict(table, W1, "4K", None)["standard"] is None  # no length


def test_a_film_without_restore_is_its_own_tier():
    films = [whole(0.0, tier=None, done_at=i) for i in range(3)]
    (rate,) = en.rates(films)
    assert rate["tier"] == "none"
    assert en.predict(en.rates(films), W1, "4K", 7_200_000)["none"] == pytest.approx(
        50.0
    )


def test_old_evidence_without_energy_still_loads():
    old = {"note": "x", "item_ids": []}
    summary = m.EvidenceSummary.model_validate(old)
    assert summary.energy is None
    assert "energy" not in summary.model_dump(mode="json")
    with_energy = m.EvidenceSummary(note="x", energy=energy(1.0))
    dumped = with_energy.model_dump(mode="json")
    assert dumped["energy"]["wh"] == 1.0
    assert m.EvidenceSummary.model_validate(dumped) == with_energy


@pytest.mark.parametrize(
    "bad", [{"wh": -1.0}, {"wh": float("nan")}, {"avg_w": float("inf")}]
)
def test_attempt_energy_rejects_impossible_values(bad):
    with pytest.raises(ValueError):
        m.AttemptEnergy.model_validate(bad)


# ── Codex r2: a resolved attempt is never also counted live ────────────────
def test_the_live_figure_is_dropped_once_its_attempt_has_resolved(rt):
    """The route takes `status()` first and reads the records after: an attempt
    that resolves in between must count once (from its evidence)."""
    rig = Rig(rt)
    executor = Ticking("restore", rig)
    rt.executors["restore"] = executor
    film = add_film(rt)
    assert rig.dispatcher.run_once() is True
    (attempt,) = attempts(rt, film, "restore")
    stale = executor.seen[-1]  # the status captured while it was still running
    assert stale["attempt_id"] == attempt.attempt_id
    assert stale["energy"]["wh"] == pytest.approx(1.0)
    row = board_row(rt, film)  # ... then the attempt resolved and was read
    value = en.film_energy(row, stale)
    assert value["stages"]["restore"]["wh"] == pytest.approx(1.0)  # not 2.0
    assert value["stages"]["restore"]["attempts"] == 1
    assert value["stages"]["restore"]["running"] is False
    assert value["total_wh"] == pytest.approx(1.0)
    assert board.film_row(row, {}, current=stale)["energy"][
        "total_wh"
    ] == pytest.approx(1.0)


def test_a_running_attempt_still_counts_beside_the_resolved_ones():
    done, running = identity(), identity()
    f = film(spent={"restore": (energy(10.0),)})
    f = f.model_copy(
        update={
            "stages": tuple(
                s.model_copy(update={"attempt_ids": (done,)})
                if s.stage == "restore"
                else s
                for s in f.stages
            )
        }
    )
    current = {
        "film_id": f.film_id,
        "stage": "restore",
        "attempt_id": running,
        "energy": energy(4.0).model_dump(mode="json"),
    }
    value = en.film_energy(f, current)
    assert value["stages"]["restore"]["wh"] == pytest.approx(14.0)
    assert value["stages"]["restore"]["attempts"] == 2
    assert value["stages"]["restore"]["running"] is True
    current["attempt_id"] = done  # the same attempt: the evidence wins
    value = en.film_energy(f, current)
    assert value["stages"]["restore"]["wh"] == pytest.approx(10.0)
    assert value["stages"]["restore"]["running"] is False


# ── IR01 payload budget / IR02 the observer's lighter board ────────────────
def test_the_network_films_snapshot_stays_inside_its_payload_budget(monkeypatch):
    """200 unfinished films, every local stage with energy, stay well under
    the 1 MiB network cap (a done row sheds per-stage detail when it must)."""
    import json
    from types import SimpleNamespace

    from taskpaw_v3.agent.server import workflow_reads as reads
    from taskpaw_v3.core.config import WorkflowSettings
    from taskpaw_v3.workflow import planning

    spent = {name: (energy(12.5), energy(3.0)) for name in en.STAGES}
    films = tuple(film(spent=spent) for _ in range(200))
    rt = SimpleNamespace(
        operator=object(),
        gpu=None,
        identity=SimpleNamespace(worker_id="controller"),  # P2-4 board_inputs
        store=SimpleNamespace(
            film_board=lambda *_a, **_k: films,
            list_intake_board=lambda _a: (),
            energy_history=lambda _a: (),
            list_workers=lambda _a: (),
            list_open_assignments=lambda _a: (),  # P3 #304: can_remove reads both
            list_routes=lambda _a: (),
        ),
    )
    monkeypatch.setattr(board, "row_facts", lambda *_a: {})
    monkeypatch.setattr(planning, "max_output", lambda _rt: "4k")
    host = SimpleNamespace(
        state="running", reason="", runtime=rt, status=lambda: {"dispatch": {}}
    )
    body = reads.films(WorkflowSettings(), lambda: host)
    assert all(row["energy"] is not None for row in body["films"])
    size = len(json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode())
    assert size < 768 * 1024, size


def test_film_board_without_energies_skips_the_evidence_scan(rt, monkeypatch):
    film_id = add_film(rt)
    rig = Rig(rt)
    rt.executors["restore"] = Ticking("restore", rig)
    assert rig.dispatcher.run_once() is True
    (full,) = rt.store.film_board(rt.operator, film_id=film_id)
    assert any(s.attempt_energy for s in full.stages)

    def scanned(*_a, **_k):
        raise AssertionError("the evidence scan ran")

    monkeypatch.setattr(type(rt.store), "_attempt_energies", scanned)
    (light,) = rt.store.film_board(rt.operator, film_id=film_id, energies=False)
    assert all(not s.attempt_energy and not s.attempt_ids for s in light.stages)
    assert board.film_row(light, {})["energy"] is None
