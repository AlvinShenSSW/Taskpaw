"""#323: current temperatures travel independently of power and history."""

import threading

import pytest

from taskpaw_v3.core.power_fleet import PowerFleet
from taskpaw_v3.tests.test_power_fleet import GPU02, HISTORY, LIVE, Clock, fleet


def thermal(age=3.0):
    return {
        "sampled_at": 1_800_000_000.0,
        "age_s": age,
        "stale": False,
        "cpu": {"celsius": None, "label": "CPU", "source": None, "reason": "no_reader"},
        "gpu": {
            "celsius": 62.0,
            "label": "NVIDIA GPU core (highest readable)",
            "source": "nvidia-smi",
            "reason": None,
            "readable": 1,
            "total": 2,
        },
        "board": {
            "celsius": None,
            "label": "Board / chassis",
            "source": None,
            "reason": "no_reader",
        },
    }


def test_temperature_request_and_cache_ages_accumulate_without_io():
    clock = Clock()
    calls = []
    release_history = threading.Event()

    def fetch(url, token):
        if "/power/history" in url:
            assert release_history.wait(3)
            calls.append(url)
            return HISTORY
        calls.append(url)
        clock.now += 2
        return {"temperatures": thermal(4), "power": None}

    f = PowerFleet(
        lambda: [type("Peer", (), GPU02)()], lambda: "", fetch=fetch, clock=clock
    )
    try:
        histories = f._refresh((GPU02["name"], GPU02["address"]), "")
        release_history.set()
        for pending in histories:
            pending.result(timeout=3)
        # The refresh's background history IO must finish before measuring a
        # cache-only snapshot; otherwise unrelated completion races this check.
        assert len(histories) == 3
        before = len(calls)
        assert before == 4
        clock.now += 7
        t = f.snapshot()[0]["temperatures"]
        assert t["age_s"] == 13
        assert t["gpu"]["celsius"] == 62
        assert t["gpu"]["readable"] == 1 and t["gpu"]["total"] == 2
        assert len(calls) == before
        clock.now += 17
        assert f.snapshot()[0]["temperatures"]["stale"] is True
    finally:
        release_history.set()
        f.stop()


@pytest.mark.parametrize("power", [None, LIVE])
def test_history_failure_and_power_off_do_not_hide_temperature(power):
    f, _, _ = fleet(
        [GPU02],
        {"/status": {"power": power, "temperatures": thermal()}, "range=": "not_found"},
    )
    try:
        f.refresh_once()
        row = f.snapshot()[0]
        assert row["state"] == ("off" if power is None else "no_power")
        assert row["temperatures"]["gpu"]["celsius"] == 62
        assert row["temperatures"]["stale"] is False
    finally:
        f.stop()


@pytest.mark.parametrize(
    "failure", ["timeout", "unreachable", "unauthorized", "invalid"]
)
def test_status_failure_immediately_expires_temperature_without_refreshing_age(failure):
    f, fetch, clock = fleet(
        [GPU02],
        {"/status": {"power": LIVE, "temperatures": thermal()}, "range=": HISTORY},
    )
    try:
        f.refresh_once()
        clock.now += 2
        fetch.answers["/status"] = [] if failure == "invalid" else failure
        f.refresh_once()
        row = f.snapshot()[0]
        assert row["temperatures"]["stale"] is True
        assert row["temperatures"]["age_s"] == 5
        assert row["power"] is None
        assert row["energy"]["today_wh"] == LIVE["today_wh"]
    finally:
        f.stop()


def test_old_agent_clears_previous_temperature_without_affecting_power():
    f, fetch, _ = fleet(
        [GPU02],
        {"/status": {"power": LIVE, "temperatures": thermal()}, "range=": HISTORY},
    )
    try:
        f.refresh_once()
        fetch.answers["/status"] = {"power": LIVE}
        f.refresh_once()
        assert f.snapshot()[0].get("temperatures") is None
        assert f.snapshot()[0]["power"]["w"] == LIVE["w"]
    finally:
        f.stop()


@pytest.mark.parametrize("age", [None, -1, True, float("nan"), float("inf")])
def test_bad_temperature_age_never_becomes_live_and_power_survives(age):
    f, _, _ = fleet(
        [GPU02],
        {"/status": {"power": LIVE, "temperatures": thermal(age)}, "range=": HISTORY},
    )
    try:
        f.refresh_once()
        row = f.snapshot()[0]
        assert row["temperatures"] is None or row["temperatures"]["stale"] is True
        assert row["power"]["w"] == LIVE["w"]
    finally:
        f.stop()


@pytest.mark.parametrize("value", [float("inf"), float("nan"), True, 201, -51, "62"])
def test_invalid_gpu_reading_cannot_be_a_live_temperature(value):
    sample = thermal()
    sample["gpu"]["celsius"] = value
    f, _, _ = fleet(
        [GPU02], {"/status": {"power": LIVE, "temperatures": sample}, "range=": HISTORY}
    )
    try:
        f.refresh_once()
        row = f.snapshot()[0]
        t = row["temperatures"]
        assert t is None or t["gpu"]["celsius"] is None or t["stale"]
        assert row["power"]["w"] == LIVE["w"]
    finally:
        f.stop()


def test_snapshot_does_not_mutate_shared_thermal_cache():
    original = thermal()
    f, _, clock = fleet([GPU02], {"/status": {"temperatures": original}})
    try:
        f.refresh_once()
        row = f.snapshot()[0]
        row["temperatures"]["gpu"]["celsius"] = 199
        clock.now += 1
        assert f.snapshot()[0]["temperatures"]["gpu"]["celsius"] == 62
        assert original == thermal()
    finally:
        f.stop()


@pytest.mark.parametrize("key", ["age_s", "sampled_at"])
def test_oversized_numeric_thermal_fields_do_not_break_valid_power(key):
    sample = thermal()
    sample[key] = 10**400
    f, _, _ = fleet(
        [GPU02], {"/status": {"power": LIVE, "temperatures": sample}, "range=": HISTORY}
    )
    try:
        f.refresh_once()
        row = f.snapshot()[0]
        assert row["power"]["w"] == LIVE["w"]
        assert row["temperatures"] is None or row["temperatures"]["stale"]
    finally:
        f.stop()


@pytest.mark.parametrize(
    "readable,total", [(True, 1), (2, 1), (0, 1), (1, -1), (1, 1.5)]
)
def test_bad_gpu_coverage_is_not_claimed_live(readable, total):
    sample = thermal()
    sample["gpu"].update(readable=readable, total=total)
    f, _, _ = fleet([GPU02], {"/status": {"temperatures": sample}})
    try:
        f.refresh_once()
        assert f.snapshot()[0]["temperatures"] is None
    finally:
        f.stop()
