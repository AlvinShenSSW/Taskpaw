"""PWR-1 (#287): the per-machine power estimate, its sampler and its storage.

Design: docs/specs/2026-10-22-power-estimation.md, section v2 (PW01-PW11).
Every reader is faked: nvidia-smi output, psutil and both clocks. Nothing here
depends on this machine's GPU.
"""

from __future__ import annotations

import logging
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from taskpaw_v3.core import gpu_sample as gs
from taskpaw_v3.core import power
from taskpaw_v3.core.config import AgentConfig, PowerSettings
from taskpaw_v3.monitors.plugins import host_metrics as hm

CST = timezone(timedelta(hours=8))
# 2026-10-22 (a Thursday) 10:00 local in UTC+8, on a 15-minute boundary.
T0 = datetime(2026, 10, 22, 10, 0, tzinfo=CST).timestamp()


# ── fakes ───────────────────────────────────────────────────────────────────
class Clock:
    def __init__(self, mono: float = 1000.0, wall: float = T0) -> None:
        self.mono, self.wall_t = mono, wall

    def __call__(self) -> float:
        return self.mono

    def wall(self) -> float:
        return self.wall_t

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall_t += seconds


class Readers:
    """Scripted GPU W and CPU % readings; the last value repeats."""

    def __init__(self, gpu=(None,), cpu=(0.0,)) -> None:
        self.gpu, self.cpu = list(gpu), list(cpu)
        self.activity: tuple[str, str] = ("idle", power.NO_OWNER)

    def read_gpu(self):
        return self.gpu.pop(0) if len(self.gpu) > 1 else self.gpu[0]

    def read_cpu(self):
        return self.cpu.pop(0) if len(self.cpu) > 1 else self.cpu[0]

    def read_activity(self):
        return self.activity


def settings(**kw) -> PowerSettings:
    base = dict(tdp_w=65.0, idle_w=10.0, other_w=35.0)
    base.update(kw)
    return PowerSettings(**base)


def sampler(clock, readers, cfg=None, data_dir=None, **kw) -> power.PowerSampler:
    cfg = cfg if cfg is not None else settings()
    holder = {"cfg": cfg}
    s = power.PowerSampler(
        lambda: holder["cfg"],
        activity=readers.read_activity,
        gpu=readers.read_gpu,
        cpu=readers.read_cpu,
        data_dir=data_dir,
        clock=clock,
        wall=clock.wall,
        tz=CST,
        platform="win32",
        **kw,
    )
    s.settings_holder = holder  # type: ignore[attr-defined]
    return s


def run_ticks(s, clock, n, step=5.0):
    for _ in range(n):
        clock.advance(step)
        s.tick()


# ── settings ────────────────────────────────────────────────────────────────
def test_power_settings_defaults_and_agent_config():
    cfg = AgentConfig(server_id="s", machine="m")
    p = cfg.power
    assert p.enabled is True
    assert (p.tdp_w, p.idle_w, p.other_w, p.psu_efficiency) == (None, None, None, None)
    assert p.cpu_source == "estimate"
    assert "power" in cfg.model_dump()


@pytest.mark.parametrize(
    "bad",
    [
        {"psu_efficiency": 0},
        {"psu_efficiency": 1.2},
        {"cpu_source": "measured"},
        {"cpu_source": "powermetrics"},
        {"tdp_w": -1},
        {"idle_w": 80, "tdp_w": 65},
        {"other_w": -5},
    ],
)
def test_power_settings_rejects_invalid(bad):
    with pytest.raises(ValidationError):
        PowerSettings(**bad)


def test_resolve_model_platform_defaults_and_overrides():
    win = power.resolve_model(PowerSettings(), platform="win32")
    assert (win.tdp_w, win.idle_w, win.other_w) == (65.0, 10.0, 35.0)
    mac = power.resolve_model(PowerSettings(), platform="darwin")
    assert (mac.tdp_w, mac.idle_w, mac.other_w) == (20.0, 3.0, 5.0)
    custom = power.resolve_model(
        PowerSettings(tdp_w=125, other_w=30, psu_efficiency=0.9), platform="win32"
    )
    assert (custom.tdp_w, custom.idle_w, custom.other_w) == (125.0, 10.0, 30.0)
    assert custom.psu_efficiency == 0.9
    # an explicit idle above the platform default TDP is clamped, never inverted
    odd = power.resolve_model(PowerSettings(idle_w=30), platform="darwin")
    assert odd.idle_w <= odd.tdp_w


# ── model ───────────────────────────────────────────────────────────────────
def test_estimate_formula_per_source():
    model = power.resolve_model(settings(), platform="win32")
    e = power.estimate(model, 100.0, 50.0)
    assert e.cpu_w == pytest.approx(10 + 55 * 0.5)
    assert e.gpu_w == 100.0 and e.other_w == 35.0
    assert e.w == pytest.approx(100 + 37.5 + 35)
    # no NVIDIA GPU / power [N/A]: GPU contributes 0 and is reported as unknown
    e = power.estimate(model, None, 0.0)
    assert e.gpu_w is None and e.w == pytest.approx(10 + 35)
    # cpu_source "none": no CPU share at all
    none = power.resolve_model(settings(cpu_source="none"), platform="win32")
    e = power.estimate(none, 50.0, 90.0)
    assert e.cpu_w == 0.0 and e.w == pytest.approx(85.0)
    # utilisation is clamped to 0..100
    assert power.estimate(model, None, 250.0).cpu_w == pytest.approx(65.0)


def test_estimate_psu_option_puts_the_loss_in_other():
    model = power.resolve_model(settings(psu_efficiency=0.8), platform="win32")
    e = power.estimate(model, 100.0, 0.0)
    dc = 100 + 10 + 35
    assert e.w == pytest.approx(dc / 0.8)
    assert e.gpu_w == 100.0 and e.cpu_w == pytest.approx(10.0)
    assert e.other_w == pytest.approx(dc / 0.8 - 110)


# ── PW02: one GPU query ─────────────────────────────────────────────────────
def fake_smi(monkeypatch, stdout, returncode=0):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(gs.subprocess, "run", run)
    monkeypatch.setattr(sys, "platform", "win32")
    return calls


def test_gpu_sample_queries_power_and_sums_gpus(monkeypatch):
    calls = fake_smi(monkeypatch, "50, 2048, 8192, 120.50\n70, 1024, 8192, 30.25\n")
    s = gs.gpu_sample()
    assert s == {
        "util_pct": 60.0,
        "mem_used_mb": 3072,
        "mem_total_mb": 16384,
        "power_w": 150.75,
    }
    assert any("power.draw" in part for part in calls[0])
    assert any("utilization.gpu" in part for part in calls[0])


def test_gpu_sample_na_power_keeps_the_rest(monkeypatch):
    fake_smi(monkeypatch, "12, 500, 8151, [N/A]\n")
    s = gs.gpu_sample()
    assert s is not None and s["power_w"] is None
    assert (s["util_pct"], s["mem_used_mb"], s["mem_total_mb"]) == (12.0, 500, 8151)


def test_gpu_sample_sums_only_reported_power(monkeypatch):
    fake_smi(monkeypatch, "10, 1, 2, [Not Supported]\n20, 1, 2, 44.5\n")
    assert gs.gpu_sample()["power_w"] == 44.5


def test_gpu_sample_old_three_column_output_has_no_power(monkeypatch):
    fake_smi(monkeypatch, "50, 2048, 8192\n")
    s = gs.gpu_sample()
    assert s["util_pct"] == 50.0 and s["power_w"] is None


@pytest.mark.parametrize(
    "stdout,code", [("", 0), ("garbage, x, y, 1\n", 0), ("1, 2, 3, 4\n", 9)]
)
def test_gpu_sample_none_when_unusable(monkeypatch, stdout, code):
    fake_smi(monkeypatch, stdout, returncode=code)
    assert gs.gpu_sample() is None


def test_gpu_sample_none_off_windows_without_query(monkeypatch):
    calls = fake_smi(monkeypatch, "1, 2, 3, 4\n")
    monkeypatch.setattr(sys, "platform", "darwin")
    assert gs.gpu_sample() is None
    assert calls == []


def test_gpu_sample_is_cached_for_four_seconds(monkeypatch):
    clock = Clock()
    gs._reset_for_tests(clock=clock)
    calls = fake_smi(monkeypatch, "1, 2, 3, 4\n")
    gs.gpu_sample()
    clock.advance(3.9)
    gs.gpu_sample()
    assert len(calls) == 1
    clock.advance(0.2)
    gs.gpu_sample()
    assert len(calls) == 2


def test_gpu_sample_concurrent_callers_share_one_query(monkeypatch):
    gate = threading.Event()
    calls = []

    def slow_run(cmd, **kw):
        calls.append(cmd)
        gate.wait(2)
        return SimpleNamespace(returncode=0, stdout="1, 2, 3, 4\n")

    monkeypatch.setattr(gs.subprocess, "run", slow_run)
    monkeypatch.setattr(sys, "platform", "win32")
    results = []
    threads = [
        threading.Thread(target=lambda: results.append(gs.gpu_sample()))
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    time.sleep(0.05)
    gate.set()
    for t in threads:
        t.join(5)
    assert len(calls) == 1 and len(results) == 3 and all(results)


def test_read_gpu_delegates_and_keeps_its_contract(monkeypatch):
    calls = fake_smi(monkeypatch, "50, 2048, 8192, 99.9\n")
    g = hm.read_gpu()
    assert g == {"util_pct": 50.0, "mem_used_mb": 2048, "mem_total_mb": 8192}
    assert gs.gpu_sample()["power_w"] == 99.9
    assert len(calls) == 1  # one query serves both consumers


# ── PW09: integration ───────────────────────────────────────────────────────
def test_trapezoid_gpu_rectangle_cpu_and_baseline():
    clock = Clock()
    r = Readers(gpu=[100.0, 200.0, 200.0], cpu=[99.0, 20.0, 40.0])
    s = sampler(clock, r)
    s.tick()  # first: no interval yet; its CPU reading is dropped
    run_ticks(s, clock, 2)
    h = s.history("24h")
    gpu_wh = ((100 + 200) / 2 * 5 + (200 + 200) / 2 * 5) / 3600
    cpu_wh = ((10 + 55 * 0.20) * 5 + (10 + 55 * 0.40) * 5) / 3600
    other_wh = 35 * 10 / 3600
    assert sum(h["gpu_wh"]) == pytest.approx(gpu_wh, abs=1e-3)
    assert sum(h["cpu_wh"]) == pytest.approx(cpu_wh, abs=1e-3)
    assert sum(h["other_wh"]) == pytest.approx(other_wh, abs=1e-3)
    assert h["totals"]["wh"] == pytest.approx(gpu_wh + cpu_wh + other_wh, abs=1e-3)
    assert sum(h["covered_s"]) == pytest.approx(10.0)


def test_psu_efficiency_scales_the_integral():
    clock = Clock()
    r = Readers(gpu=[0.0], cpu=[0.0])
    s = sampler(clock, r, settings(psu_efficiency=0.5))
    s.tick()
    run_ticks(s, clock, 1)
    h = s.history("24h")
    assert h["totals"]["wh"] == pytest.approx((10 + 35) / 0.5 * 5 / 3600, abs=1e-3)


def test_gap_over_fifteen_seconds_is_not_integrated():
    clock = Clock()
    r = Readers(gpu=[100.0], cpu=[0.0])
    s = sampler(clock, r)
    s.tick()
    clock.advance(20)  # a gap: nothing is invented
    s.tick()
    assert s.history("24h")["totals"]["wh"] == 0
    clock.advance(5)
    s.tick()
    h = s.history("24h")
    assert sum(h["covered_s"]) == pytest.approx(5.0)
    assert h["totals"]["wh"] == pytest.approx((100 + 10 + 35) * 5 / 3600, abs=1e-3)


def test_monotonic_backwards_step_is_a_gap():
    clock = Clock()
    r = Readers(gpu=[100.0], cpu=[0.0])
    s = sampler(clock, r)
    s.tick()
    clock.mono -= 3
    s.tick()
    assert s.history("24h")["totals"]["wh"] == 0


def test_disabled_samples_nothing_and_resume_does_not_bridge():
    clock = Clock()
    r = Readers(gpu=[100.0], cpu=[0.0])
    s = sampler(clock, r)
    s.tick()
    run_ticks(s, clock, 1)
    before = s.history("24h")["totals"]["wh"]
    s.settings_holder["cfg"] = settings(enabled=False)
    run_ticks(s, clock, 3)
    assert s.live() is None
    assert s.history("24h")["totals"]["wh"] == before
    s.settings_holder["cfg"] = settings()
    s.tick()  # first tick after the pause: no interval
    assert s.history("24h")["totals"]["wh"] == before
    run_ticks(s, clock, 1)
    assert s.history("24h")["totals"]["wh"] > before


def test_reader_failures_never_stop_sampling():
    clock = Clock()

    def boom():
        raise RuntimeError("nvidia-smi exploded")

    s = power.PowerSampler(
        lambda: settings(),
        activity=boom,
        gpu=boom,
        cpu=boom,
        clock=clock,
        wall=clock.wall,
        tz=CST,
        platform="win32",
    )
    s.tick()
    run_ticks(s, clock, 1)
    live = s.live()
    assert live is not None
    assert live["gpu_w"] is None and live["activity"] == "other"
    assert s.history("24h")["totals"]["wh"] == pytest.approx(45 * 5 / 3600, abs=1e-3)


def test_live_status_field_shape():
    clock = Clock()
    r = Readers(gpu=[120.0], cpu=[0.0, 50.0])
    s = sampler(clock, r)
    assert s.live() is None  # nothing sampled yet
    s.tick()
    r.activity = ("restore", "film-1")
    run_ticks(s, clock, 1)
    live = s.live()
    assert set(live) == {
        "w",
        "gpu_w",
        "cpu_w",
        "other_w",
        "estimated",
        "today_wh",
        "week_wh",
        "month_wh",
        "activity",
    }
    assert live["estimated"] is True
    assert live["activity"] == "restore"
    assert live["gpu_w"] == 120.0
    assert live["cpu_w"] == pytest.approx(37.5)
    assert live["other_w"] == 35.0
    assert live["w"] == pytest.approx(192.5)
    assert live["today_wh"] > 0


def test_live_cpu_is_none_until_the_baseline_has_a_window():
    clock = Clock()
    r = Readers(gpu=[120.0], cpu=[0.0, 50.0])
    s = sampler(clock, r)
    s.tick()  # the first reading has no window behind it
    first = s.live()
    assert first["cpu_w"] is None
    assert first["gpu_w"] == 120.0
    assert first["w"] == pytest.approx(120.0 + first["other_w"])  # CPU omitted
    run_ticks(s, clock, 1)
    assert s.live()["cpu_w"] == pytest.approx(37.5)
    # a pause drops the baseline again
    s.settings_holder["cfg"] = settings(enabled=False)
    run_ticks(s, clock, 1)
    s.settings_holder["cfg"] = settings()
    s.tick()
    assert s.live()["cpu_w"] is None


# ── PW03/PW04: attribution ──────────────────────────────────────────────────
def manual_types(kind):
    return {"jasna-1": "jasna", "subs": "avsubs", "lada-1": "lada"}.get(kind)


WF = ("workflow", 7)


@pytest.mark.parametrize(
    "holder,current,expected",
    [
        (WF, ("f1", "restore"), ("restore", "f1")),
        (WF, ("f1", "asr"), ("asr", "f1")),
        (WF, ("f1", "translate"), ("translate", "f1")),
        (WF, None, ("other", power.NO_OWNER)),
        (("jasna-1", 3), None, ("restore", power.MANUAL)),
        (("subs", 4), None, ("asr", power.MANUAL)),
        # the GPU owner wins over the workflow's running non-GPU stage
        (("jasna-1", 3), ("f2", "translate"), ("restore", power.MANUAL)),
        (("subs", 4), ("f2", "return"), ("asr", power.MANUAL)),
        (("lada-1", 1), None, ("restore", power.MANUAL)),
        (("gone", 1), None, ("other", power.MANUAL)),
        (None, ("f3", "translate"), ("translate", "f3")),
        (None, ("f3", "return"), ("other", "f3")),
        (None, ("f3", "scrape"), ("other", "f3")),
        (None, None, ("idle", power.NO_OWNER)),
    ],
)
def test_classify_precedence(holder, current, expected):
    assert power.classify(holder, WF, current, manual_types) == expected


@pytest.mark.parametrize(
    "util,expected",
    [
        (None, ("idle", power.NO_OWNER)),
        (0.0, ("idle", power.NO_OWNER)),
        (29.9, ("idle", power.NO_OWNER)),
        (30.0, ("other", power.NO_OWNER)),  # an untracked GPU job
        (95.0, ("other", power.NO_OWNER)),
    ],
)
def test_classify_busy_gpu_without_a_holder_is_other(util, expected):
    assert power.classify(None, WF, None, manual_types, gpu_util=util) == expected


def test_classify_gpu_util_never_overrides_a_known_owner():
    assert power.classify(("jasna-1", 1), WF, None, manual_types, gpu_util=99.0) == (
        "restore",
        power.MANUAL,
    )
    assert power.classify(
        None, WF, ("f3", "translate"), manual_types, gpu_util=99.0
    ) == ("translate", "f3")


def test_activity_source_busy_gpu_without_a_lease_is_other():
    util = [None]
    read = power.activity_source(manual_types, lambda: None, lambda: util[0])
    assert read() == ("idle", "none")
    util[0] = 80.0
    assert read() == ("other", "none")

    def boom():
        raise RuntimeError("smi")

    assert power.activity_source(manual_types, lambda: None, boom)() == ("idle", "none")


def test_classify_without_a_workflow():
    assert power.classify(("jasna-1", 1), None, None, manual_types) == (
        "restore",
        power.MANUAL,
    )
    assert power.classify(None, None, None, manual_types) == ("idle", "none")


def test_classify_survives_a_failing_type_lookup():
    def broken(_iid):
        raise RuntimeError("supervisor busy")

    assert power.classify(("x", 1), WF, None, broken) == ("other", power.MANUAL)


def test_activity_source_reads_lease_and_dispatcher():
    from taskpaw_v3.core import gpu_lease

    dispatcher = SimpleNamespace(
        run_id=("workflow", 11), current_stage=lambda: ("film-9", "asr")
    )
    read = power.activity_source(manual_types, lambda: dispatcher)
    assert read() == ("other", "film-9")  # no lease: asr without GPU is "other"
    assert gpu_lease.try_acquire(("workflow", 11), 5.0, "wf")
    assert read() == ("asr", "film-9")
    gpu_lease.release(("workflow", 11))
    assert gpu_lease.try_acquire(("jasna-1", 2), 5.0, "manual")
    assert read() == ("restore", power.MANUAL)
    gpu_lease.release(("jasna-1", 2))
    assert power.activity_source(manual_types, lambda: None)() == ("idle", "none")


def test_activity_energy_per_bucket():
    clock = Clock()
    r = Readers(gpu=[100.0], cpu=[0.0])
    s = sampler(clock, r)
    s.tick()
    r.activity = ("restore", "f1")
    run_ticks(s, clock, 2)
    r.activity = ("idle", power.NO_OWNER)
    run_ticks(s, clock, 1)
    h = s.history("24h")
    per = (100 + 10 + 35) * 5 / 3600
    acts = h["totals"]["activity_wh"]
    assert acts["restore"] == pytest.approx(2 * per, abs=1e-3)
    assert acts["idle"] == pytest.approx(per, abs=1e-3)
    assert acts["asr"] == acts["translate"] == acts["other"] == 0
    assert set(h["activity_wh"]) == set(power.ACTIVITIES)


# ── PW04: per-owner counters ────────────────────────────────────────────────
def test_owner_counter_between_claim_and_resolve():
    clock = Clock()
    r = Readers(gpu=[100.0, 100.0, 300.0, 100.0], cpu=[0.0])
    s = sampler(clock, r)
    s.tick()
    claim = s.reading("f1")
    assert claim.wh == 0 and claim.owner == "f1"
    r.activity = ("restore", "f1")
    run_ticks(s, clock, 3)
    r.activity = ("idle", power.NO_OWNER)
    run_ticks(s, clock, 2)  # other owners' samples never leak in
    resolve = s.reading("f1")
    delta = s.energy_between(claim, resolve)
    assert delta is not None
    gpu = ((100 + 300) / 2 * 5 + (300 + 100) / 2 * 5 + (100 + 100) / 2 * 5) / 3600
    assert delta.gpu_wh == pytest.approx(gpu, abs=1e-4)
    assert delta.cpu_wh == pytest.approx(10 * 15 / 3600, abs=1e-4)
    assert delta.other_wh == pytest.approx(35 * 15 / 3600, abs=1e-4)
    assert delta.wh == pytest.approx(delta.gpu_wh + delta.cpu_wh + delta.other_wh)
    assert delta.seconds == pytest.approx(15.0)
    assert delta.avg_w == pytest.approx(delta.wh * 3600 / 15)
    assert delta.peak_w == pytest.approx(300 + 10 + 35)
    d = delta.to_dict()
    assert set(d) == {
        "wh",
        "gpu_wh",
        "cpu_wh",
        "other_wh",
        "seconds",
        "avg_w",
        "peak_w",
    }


def test_owner_counter_is_cumulative_across_attempts():
    clock = Clock()
    r = Readers(gpu=[50.0], cpu=[0.0])
    s = sampler(clock, r)
    s.tick()
    a = s.reading("f1")
    r.activity = ("restore", "f1")
    run_ticks(s, clock, 2)
    b = s.reading("f1")
    r.activity = ("asr", "f1")
    run_ticks(s, clock, 1)
    c = s.reading("f1")
    assert c.wh > b.wh > a.wh
    first, second = s.energy_between(a, b), s.energy_between(b, c)
    assert first.seconds == pytest.approx(10) and second.seconds == pytest.approx(5)
    assert s.energy_between(a, c).wh == pytest.approx(first.wh + second.wh)


def test_energy_between_rejects_mismatched_readings():
    clock = Clock()
    r = Readers(gpu=[50.0], cpu=[0.0])
    s = sampler(clock, r)
    s.tick()
    a, b = s.reading("f1"), s.reading("f2")
    assert s.energy_between(a, b) is None
    # a reading from another sampler (another process lifetime) is not comparable
    other = sampler(Clock(), Readers())
    assert s.energy_between(other.reading("f1"), s.reading("f1")) is None


def test_manual_and_none_owners_have_counters_too():
    clock = Clock()
    r = Readers(gpu=[80.0], cpu=[0.0])
    s = sampler(clock, r)
    s.tick()
    m0 = s.reading(power.MANUAL)
    r.activity = ("restore", power.MANUAL)
    run_ticks(s, clock, 1)
    assert s.energy_between(m0, s.reading(power.MANUAL)).wh > 0


# ── PW07: storage ───────────────────────────────────────────────────────────
def rows(path):
    con = sqlite3.connect(str(path))
    try:
        return con.execute("SELECT * FROM power_buckets ORDER BY start").fetchall()
    finally:
        con.close()


def test_closed_bucket_is_one_insert_and_open_bucket_stays_in_memory(tmp_path):
    clock = Clock(wall=T0 + 890)  # 10 s before the 10:15 boundary
    r = Readers(gpu=[100.0], cpu=[0.0])
    s = sampler(clock, r, data_dir=tmp_path)
    s.tick()
    run_ticks(s, clock, 1)  # 10:14:55 → still the 10:00 bucket
    assert rows(tmp_path / "power.db") == []
    run_ticks(s, clock, 2)  # crosses 10:15 → the 10:00 bucket closes
    stored = rows(tmp_path / "power.db")
    assert len(stored) == 1 and stored[0][0] == int(T0)
    h = s.history("24h")
    assert sum(1 for c in h["covered_s"] if c) == 2  # closed + open bucket
    s.stop()
    assert len(rows(tmp_path / "power.db")) == 2  # stop flushes the open bucket


def test_counters_survive_a_restart(tmp_path):
    clock = Clock()
    r = Readers(gpu=[100.0], cpu=[0.0])
    s = sampler(clock, r, data_dir=tmp_path)
    s.tick()
    run_ticks(s, clock, 4)
    today = s.live()["today_wh"]
    s.stop()
    clock.advance(60)  # the restart takes a minute: that gap is not invented
    s2 = sampler(clock, r, data_dir=tmp_path)
    s2.tick()
    assert s2.live()["today_wh"] == pytest.approx(today, abs=1e-3)
    run_ticks(s2, clock, 2)
    s2.stop()
    s3 = sampler(clock, r, data_dir=tmp_path)
    s3.tick()
    per = (100 + 10 + 35) * 5 / 3600
    assert s3.live()["today_wh"] == pytest.approx(6 * per, abs=1e-3)
    # the same bucket was flushed twice: additive, never overwritten
    assert len(rows(tmp_path / "power.db")) == 1
    s3.stop()


def test_retention_drops_buckets_older_than_35_days(tmp_path):
    db = tmp_path / "power.db"
    clock = Clock()
    s = sampler(clock, Readers(gpu=[10.0]), data_dir=tmp_path)
    s.tick()
    s.stop()
    con = sqlite3.connect(str(db))
    old = int(T0) - 36 * 86400
    keep = int(T0) - 34 * 86400
    for start in (old, keep):
        con.execute(
            "INSERT INTO power_buckets VALUES (?, 900, 1, 1, 1, 0, 0, 0, 3, 0)",
            (start,),
        )
    con.commit()
    con.close()
    s2 = sampler(clock, Readers(gpu=[10.0]), data_dir=tmp_path)
    s2.tick()
    assert s2.history("30d")["totals"]["wh"] == 0  # 34 days ago is outside 30d
    run_ticks(s2, clock, 1)  # energy in the 10:00 bucket
    assert old in [row[0] for row in rows(db)]
    clock.advance(900)  # a gap, then energy in the next bucket closes 10:00
    s2.tick()
    run_ticks(s2, clock, 1)
    starts = [row[0] for row in rows(db)]
    assert old not in starts and keep in starts
    s2.stop()


def test_no_data_dir_keeps_everything_in_memory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    clock = Clock()
    s = sampler(clock, Readers(gpu=[10.0]))
    s.tick()
    run_ticks(s, clock, 200)  # crosses a bucket boundary
    s.stop()
    assert list(tmp_path.iterdir()) == []


def test_unopenable_database_falls_back_to_memory(tmp_path):
    (tmp_path / "power.db").mkdir()  # a directory where the file should be
    clock = Clock()
    s = sampler(clock, Readers(gpu=[10.0]), data_dir=tmp_path)
    s.tick()
    run_ticks(s, clock, 1)
    assert s.history("24h")["totals"]["wh"] > 0
    s.stop()


def seed_db(tmp_path, entries):
    con = sqlite3.connect(str(tmp_path / "power.db"))
    con.execute(power._SCHEMA)
    for start, wh in entries:
        con.execute(
            "INSERT INTO power_buckets VALUES (?, 900, ?, 0, 0, 0, 0, 0, ?, 0)",
            (int(start), wh, wh),
        )
    con.commit()
    con.close()


def test_history_is_available_while_sampling_is_disabled_at_boot(tmp_path):
    seed_db(tmp_path, [(T0 - 900, 2.0), (T0 - 1800, 3.0)])
    clock = Clock()
    s = sampler(
        clock, Readers(gpu=[10.0]), cfg=settings(enabled=False), data_dir=tmp_path
    )
    s.tick()
    assert s.live() is None
    h = s.history("24h")
    assert h["totals"]["wh"] == pytest.approx(5.0)
    assert power.power_view(s)["history"]["totals"]["wh"] == pytest.approx(5.0)
    s.stop()


def test_disabling_live_flushes_the_open_bucket_and_keeps_history(tmp_path):
    clock = Clock()
    s = sampler(clock, Readers(gpu=[100.0], cpu=[0.0]), data_dir=tmp_path)
    s.tick()
    run_ticks(s, clock, 3)
    assert rows(tmp_path / "power.db") == []  # the open bucket is memory only
    before = s.history("24h")["totals"]["wh"]
    assert before > 0
    s.settings_holder["cfg"] = settings(enabled=False)
    run_ticks(s, clock, 1)
    stored = rows(tmp_path / "power.db")
    assert len(stored) == 1 and stored[0][0] == int(T0)
    assert s.history("24h")["totals"]["wh"] == pytest.approx(before)
    run_ticks(s, clock, 2)  # nothing is written twice
    assert len(rows(tmp_path / "power.db")) == 1
    s.settings_holder["cfg"] = settings()
    s.tick()
    run_ticks(s, clock, 2)  # re-enabled: same bucket, additive
    s.stop()
    assert len(rows(tmp_path / "power.db")) == 1
    assert s.history("24h")["totals"]["wh"] > before


def test_disabled_without_a_database_creates_none(tmp_path):
    s = sampler(
        Clock(), Readers(gpu=[10.0]), cfg=settings(enabled=False), data_dir=tmp_path
    )
    s.tick()
    s.stop()
    assert list(tmp_path.iterdir()) == []


def damage(path):
    con = sqlite3.connect(str(path))
    con.execute(power._SCHEMA)
    base = int(T0) - 3000 * 900
    for i in range(3000):
        con.execute(power._UPSERT, (base + 900 * i, 1, 1, 1, 1, 0, 0, 0, 1, 0))
    con.commit()
    con.close()
    raw = bytearray(path.read_bytes())
    for off in range(4096 * 2, len(raw), 4096):
        raw[off : off + 100] = b"\xff" * 100
    path.write_bytes(bytes(raw))


def test_partly_corrupt_database_falls_back_to_memory_with_one_warning(
    tmp_path, caplog
):
    db = tmp_path / "power.db"
    damage(db)
    clock = Clock()
    s = sampler(clock, Readers(gpu=[10.0]), data_dir=tmp_path)
    with caplog.at_level(logging.WARNING, logger="taskpaw.power"):
        s.tick()  # must not raise
        run_ticks(s, clock, 3)
    assert s.live() is not None
    assert s.history("24h")["totals"]["wh"] > 0
    assert len(list(tmp_path.glob("power.db.corrupt-*"))) == 1
    assert not db.exists()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    s.stop()


def test_corrupt_database_that_cannot_be_renamed_still_runs(tmp_path, monkeypatch):
    damage(tmp_path / "power.db")

    def refuse(*_a, **_k):
        raise OSError("locked")

    monkeypatch.setattr(power.os, "replace", refuse)
    clock = Clock()
    s = sampler(clock, Readers(gpu=[10.0]), data_dir=tmp_path)
    s.tick()
    run_ticks(s, clock, 1)
    assert s.live() is not None
    s.stop()


def test_future_buckets_are_not_in_today_week_month(tmp_path):
    seed_db(
        tmp_path,
        [(T0 - 900, 1.0), (T0 + 5 * 86400, 100.0), (T0 + 2 * 3600, 50.0)],
    )
    clock = Clock()
    s = sampler(clock, Readers(gpu=[None]), data_dir=tmp_path)
    s.settings_holder["cfg"] = settings(other_w=0, idle_w=0, tdp_w=0)
    s.tick()
    live = s.live()
    assert live["today_wh"] == pytest.approx(1.0)
    assert live["week_wh"] == pytest.approx(1.0)
    assert live["month_wh"] == pytest.approx(1.0)
    s.stop()


def test_forward_clock_jump_does_not_prune_history(tmp_path, caplog):
    keep = int(T0) - 34 * 86400
    seed_db(tmp_path, [(keep, 1.0)])
    clock = Clock()
    s = sampler(clock, Readers(gpu=[10.0]), data_dir=tmp_path)
    s.tick()
    run_ticks(s, clock, 1)
    clock.wall_t += 400 * 86400  # the RTC jumps a year ahead
    with caplog.at_level(logging.WARNING, logger="taskpaw.power"):
        run_ticks(s, clock, 1)  # closes the old bucket under the new time
    assert keep in [row[0] for row in rows(tmp_path / "power.db")]
    assert keep in s._buckets
    jumps = [r for r in caplog.records if "clock" in r.getMessage().lower()]
    assert len(jumps) == 1  # logged once
    s.stop()


def test_normal_operation_still_prunes_old_buckets(tmp_path):
    old = int(T0) - 36 * 86400
    seed_db(tmp_path, [(old, 1.0)])
    clock = Clock(wall=T0 + 890)
    s = sampler(clock, Readers(gpu=[10.0]), data_dir=tmp_path)
    s.tick()
    run_ticks(s, clock, 4)  # crosses 10:15 normally
    assert old not in [row[0] for row in rows(tmp_path / "power.db")]
    s.stop()


# ── PW11: local calendar aggregates ─────────────────────────────────────────
def test_today_week_month_use_local_boundaries(tmp_path):
    # Thursday 2026-10-22 10:00 UTC+8; the week started Monday 10-19.
    db = tmp_path / "power.db"
    clock = Clock()
    s = sampler(clock, Readers(gpu=[10.0]), data_dir=tmp_path)
    s.tick()
    s.stop()

    def local(*args):
        return int(datetime(*args, tzinfo=CST).timestamp())

    con = sqlite3.connect(str(db))
    entries = [
        (local(2026, 10, 22, 0, 0), 1.0),  # today 00:00 local (yesterday in UTC)
        (local(2026, 10, 21, 23, 45), 2.0),  # yesterday, this week
        (local(2026, 10, 19, 0, 0), 4.0),  # Monday: this week
        (local(2026, 10, 18, 23, 45), 8.0),  # Sunday: last week, this month
        (local(2026, 10, 1, 0, 0), 16.0),  # this month
        (local(2026, 9, 30, 23, 45), 32.0),  # last month
    ]
    for start, wh in entries:
        con.execute(
            "INSERT INTO power_buckets VALUES (?, 900, ?, 0, 0, 0, 0, 0, ?, 0)",
            (start, wh, wh),
        )
    con.commit()
    con.close()
    s2 = sampler(clock, Readers(gpu=[None], cpu=[0.0]), data_dir=tmp_path)
    s2.settings_holder["cfg"] = settings(other_w=0, idle_w=0, tdp_w=0)
    s2.tick()
    live = s2.live()
    assert live["today_wh"] == pytest.approx(1.0)
    assert live["week_wh"] == pytest.approx(7.0)
    assert live["month_wh"] == pytest.approx(31.0)
    s2.stop()


# ── history ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "rng,points,step", [("24h", 96, 900), ("7d", 168, 3600), ("30d", 120, 21600)]
)
def test_history_is_bounded(rng, points, step):
    clock = Clock()
    s = sampler(clock, Readers(gpu=[10.0]))
    s.tick()
    run_ticks(s, clock, 1)
    h = s.history(rng)
    assert h["range"] == rng and h["step_s"] == step and h["estimated"] is True
    for key in ("t", "covered_s", "avg_w", "wh", "gpu_wh", "cpu_wh", "other_wh"):
        assert len(h[key]) == points
    for series in h["activity_wh"].values():
        assert len(series) == points
    assert h["t"][1] - h["t"][0] == step
    assert h["t"][0] <= clock.wall() < h["t"][-1] + step
    # the newest point holds the sample; an empty point is a gap, not a zero
    assert h["avg_w"][-1] == pytest.approx(10 + 10 + 35, abs=0.1)
    assert h["avg_w"][0] is None


def test_history_points_align_to_local_hours():
    clock = Clock(wall=T0 + 37 * 60)  # 10:37 local
    s = sampler(clock, Readers(gpu=[10.0]))
    s.tick()
    h = s.history("7d")
    last = datetime.fromtimestamp(h["t"][-1], CST)
    assert (last.hour, last.minute) == (10, 0)
    h = s.history("30d")
    last = datetime.fromtimestamp(h["t"][-1], CST)
    assert last.minute == 0 and last.hour % 6 == 0


def test_history_rejects_an_unknown_range():
    s = sampler(Clock(), Readers())
    with pytest.raises(ValueError):
        s.history("1y")


# ── thread lifecycle ────────────────────────────────────────────────────────
def test_thread_samples_until_stopped(tmp_path):
    r = Readers(gpu=[10.0], cpu=[0.0])
    s = power.PowerSampler(
        lambda: settings(),
        activity=r.read_activity,
        gpu=r.read_gpu,
        cpu=r.read_cpu,
        data_dir=tmp_path,
        interval=0.01,
        platform="win32",
    )
    s.start()
    deadline = time.monotonic() + 5
    while s.ticks < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert s.ticks >= 3
    assert s.stop(timeout=5) is True
    count = s.ticks
    time.sleep(0.05)
    assert s.ticks == count
    assert s.stop(timeout=1) is True  # idempotent


def test_loop_flushes_the_open_bucket_when_it_exits_after_stop_timed_out(tmp_path):
    gate = threading.Event()
    entered = threading.Event()
    blocking = [False]
    r = Readers(gpu=[10.0], cpu=[0.0])

    def gpu():
        if blocking[0]:
            entered.set()
            gate.wait(5)
        return r.read_gpu()

    s = power.PowerSampler(
        lambda: settings(),
        activity=r.read_activity,
        gpu=gpu,
        cpu=r.read_cpu,
        data_dir=tmp_path,
        interval=0.01,
        platform="win32",
    )
    s.start()
    deadline = time.monotonic() + 5
    while s.ticks < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    blocking[0] = True
    assert entered.wait(5)
    assert s.stop(timeout=0.05) is False  # the thread is still inside a tick
    assert rows(tmp_path / "power.db") == []
    gate.set()
    thread = s._thread
    thread.join(5)
    assert not thread.is_alive()
    assert len(rows(tmp_path / "power.db")) >= 1  # the late exit still wrote
    assert s.stop(timeout=1) is True


def test_default_cpu_reader_uses_psutil_without_blocking(monkeypatch):
    seen = []
    fake = SimpleNamespace(
        cpu_percent=lambda interval=None: seen.append(interval) or 42.0
    )
    monkeypatch.setattr(power, "psutil", fake)
    assert power.read_cpu_percent() == 42.0
    assert seen == [None]
    monkeypatch.setattr(power, "psutil", None)
    assert power.read_cpu_percent() is None


def test_default_gpu_reader_uses_the_shared_sample(monkeypatch):
    fake_smi(monkeypatch, "5, 1, 2, 47.25\n")
    assert power.read_gpu_power() == 47.25


def test_process_holders_default_to_none_and_defaults():
    assert power.get_sampler() is None
    assert power.get_power_settings() == PowerSettings()
