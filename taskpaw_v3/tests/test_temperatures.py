"""Current temperatures stay independent of power and HTTP request latency (#323)."""

from __future__ import annotations

import subprocess
import threading
from types import SimpleNamespace

import pytest

from taskpaw_v3.core import temperatures


def test_multiple_gpus_report_highest_readable_and_complete_nvidia_coverage():
    reading = temperatures.parse_gpu("0, 42\n1, N/A\n2, 67\n")
    assert reading.celsius == 67
    assert (reading.readable, reading.total, reading.reason, reading.valid) == (
        2,
        3,
        None,
        True,
    )


@pytest.mark.parametrize(
    "cell", ["N/A", "[Not Supported]", "NaN", "inf", "-inf", "201", "-51", "oops"]
)
def test_bad_temperature_cell_does_not_hide_other_gpus(cell):
    reading = temperatures.parse_gpu(f"0, {cell}\n1, 0")
    assert (reading.celsius, reading.readable, reading.total) == (0, 1, 2)


@pytest.mark.parametrize(
    "text", ["0, 42\n0, 50", "x, 42", "-1, 42", "0,42,43", "0", "0.0,42"]
)
def test_ambiguous_gpu_inventory_is_invalid(text):
    with pytest.raises(ValueError):
        temperatures.parse_gpu(text)


def test_empty_inventory_and_all_unreadable_have_distinct_coverage():
    empty = temperatures.parse_gpu("\n")
    unreadable = temperatures.parse_gpu("0, N/A\n1, nan")
    assert (empty.celsius, empty.readable, empty.total, empty.reason) == (
        None,
        0,
        0,
        "no_sensor",
    )
    assert (
        unreadable.celsius,
        unreadable.readable,
        unreadable.total,
        unreadable.reason,
    ) == (None, 0, 2, "invalid_reading")
    assert empty.valid and unreadable.valid


def test_native_query_has_fixed_argv_closed_stdin_timeout_and_hidden_window(
    monkeypatch,
):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="0, 49")

    monkeypatch.setattr(temperatures.sys, "platform", "win32")
    monkeypatch.setattr(temperatures.subprocess, "run", run)
    assert temperatures.read_gpu().celsius == 49
    argv, kwargs = calls[0]
    assert argv == [
        "nvidia-smi",
        "--query-gpu=index,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["timeout"] == 5
    assert kwargs["shell"] is False
    assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)


@pytest.mark.parametrize(
    "error,reason",
    [
        (FileNotFoundError(), "provider_missing"),
        (PermissionError(), "permission_denied"),
        (subprocess.TimeoutExpired("nvidia-smi", 5), "timeout"),
        (OSError("failure"), "query_failed"),
    ],
)
def test_query_errors_are_explicit_unavailable(monkeypatch, error, reason):
    monkeypatch.setattr(temperatures.sys, "platform", "win32")

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(temperatures.subprocess, "run", fail)
    reading = temperatures.read_gpu()
    assert (reading.celsius, reading.reason, reading.valid) == (None, reason, False)


@pytest.mark.parametrize(
    "returncode,stdout,reason",
    [
        (1, "raw secret text", "query_failed"),
        (4, "raw permission text", "permission_denied"),
        (0, "x, 42", "invalid_reading"),
    ],
)
def test_bad_provider_response_is_not_current(monkeypatch, returncode, stdout, reason):
    monkeypatch.setattr(temperatures.sys, "platform", "win32")
    monkeypatch.setattr(
        temperatures.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=returncode, stdout=stdout),
    )
    assert temperatures.read_gpu().reason == reason
    assert temperatures.read_gpu().valid is False


def test_unsupported_platform_does_not_spawn(monkeypatch):
    monkeypatch.setattr(temperatures.sys, "platform", "darwin")
    monkeypatch.setattr(
        temperatures.subprocess, "run", lambda *a, **k: pytest.fail("must not spawn")
    )
    assert temperatures.read_gpu().reason == "unsupported_platform"


def test_snapshot_includes_query_duration_ages_monotonically_and_never_queries():
    now = [10.0]
    calls = []

    def reader():
        calls.append(True)
        now[0] += 4
        return temperatures.parse_gpu("0, 50")

    sampler = temperatures.TemperatureSampler(
        reader=reader, clock=lambda: now[0], wall=lambda: 100.0
    )
    before = sampler.snapshot()
    assert before["age_s"] is None and before["stale"] is True
    assert before["gpu"]["reason"] == "pending"
    sampler.tick()
    view = sampler.snapshot()
    assert (view["sampled_at"], view["age_s"], view["stale"]) == (100, 4, False)
    assert view["gpu"]["label"] == "NVIDIA GPU core (highest readable)"
    assert view["gpu"]["source"] == "nvidia-smi"
    for key in ("cpu", "board"):
        assert view[key]["celsius"] is None
        assert view[key]["reason"] == "no_reader"
        assert view[key]["source"] is None
    now[0] = 40
    assert sampler.snapshot()["stale"] is True
    assert sampler.snapshot()["age_s"] == 30
    assert len(calls) == 1
    view["gpu"]["celsius"] = 100
    assert sampler.snapshot()["gpu"]["celsius"] == 50


def test_provider_failure_clears_prior_value_timestamp_and_coverage(caplog):
    readings = iter(
        [
            temperatures.parse_gpu("0, 50"),
            temperatures.GpuReading(reason="timeout", valid=False),
            temperatures.GpuReading(reason="timeout", valid=False),
            temperatures.parse_gpu("0, 48"),
        ]
    )
    sampler = temperatures.TemperatureSampler(reader=lambda: next(readings))
    sampler.tick()
    sampler.tick()
    view = sampler.snapshot()
    assert view["sampled_at"] is None and view["age_s"] is None and view["stale"]
    assert (
        view["gpu"]["celsius"],
        view["gpu"]["readable"],
        view["gpu"]["total"],
        view["gpu"]["reason"],
    ) == (None, 0, 0, "timeout")
    sampler.tick()
    assert sum("timeout" in record.message for record in caplog.records) == 1
    sampler.tick()
    assert sampler.snapshot()["gpu"]["celsius"] == 48
    assert sampler.snapshot()["stale"] is False


def test_unexpected_provider_exception_logs_only_bounded_reason_once(caplog):
    def reader():
        raise RuntimeError("raw provider text must stay private")

    sampler = temperatures.TemperatureSampler(reader=reader)
    sampler.tick()
    sampler.tick()
    assert sampler.snapshot()["stale"]
    assert sampler.snapshot()["gpu"]["reason"] == "query_failed"
    assert "raw provider text" not in caplog.text
    assert len(caplog.records) == 1


def test_snapshot_stays_nonblocking_during_query_and_stop_joins_worker():
    entered, release = threading.Event(), threading.Event()

    def reader():
        entered.set()
        assert release.wait(3)
        return temperatures.parse_gpu("0, 43")

    sampler = temperatures.TemperatureSampler(reader=reader)
    sampler.start()
    sampler.start()
    try:
        assert entered.wait(2)
        done = threading.Event()
        reader_thread = threading.Thread(
            target=lambda: (sampler.snapshot(), done.set())
        )
        reader_thread.start()
        try:
            assert done.wait(0.5), "snapshot waited behind provider IO"
        finally:
            release.set()
            reader_thread.join(2)
    finally:
        release.set()
        assert sampler.stop()
    assert sampler.stop()
    assert sampler._thread is not None and not sampler._thread.is_alive()


def test_valid_unreadable_query_can_be_fresh_unavailable():
    sampler = temperatures.TemperatureSampler(
        reader=lambda: temperatures.parse_gpu("0, N/A")
    )
    sampler.tick()
    view = sampler.snapshot()
    assert view["stale"] is False
    assert view["gpu"]["celsius"] is None
    assert view["gpu"]["total"] == 1


def test_thermal_failure_does_not_touch_shared_power_query_or_cache(monkeypatch):
    from taskpaw_v3.core import gpu_sample

    monkeypatch.setattr(
        gpu_sample, "gpu_sample", lambda: pytest.fail("temperature path queried power")
    )
    monkeypatch.setattr(
        gpu_sample, "_query", lambda: pytest.fail("temperature path queried power")
    )
    sampler = temperatures.TemperatureSampler(
        reader=lambda: temperatures.GpuReading(reason="timeout", valid=False)
    )
    sampler.tick()
    assert sampler.snapshot()["gpu"]["reason"] == "timeout"
