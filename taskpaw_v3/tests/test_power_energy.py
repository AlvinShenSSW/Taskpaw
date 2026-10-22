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
from taskpaw_v3.tests.test_workflow_local_dispatch import add_film, runtime, stage
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
        self.sampler = (
            power.PowerSampler(
                lambda: PowerSettings(tdp_w=65, idle_w=10, other_w=30),
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
