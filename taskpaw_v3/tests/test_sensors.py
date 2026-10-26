"""#336: CPU / GPU temperature and fan RPM from a resident LibreHardwareMonitor.

No live LHM here: the parser runs on fixture `/metrics` text, and `read_lhm`
reads a fake server on 127.0.0.1. The conftest stub of `read_sensors` keeps
every other test away from port 8085 and nvidia-smi; the real function is
bound below at import, before that stub applies.
"""

from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from taskpaw_v3.core import gpu_sample, sensors

READ_SENSORS = sensors.read_sensors

# LibreHardwareMonitor 0.9.6 `HttpServer.cs`: a space before `{`, quoted label
# keys and `, ` separators; `#` is stripped from sensor names.
LHM_096 = """\
lhm_cpu_temperature_celsius {"sensorName"="CPU Package", "sensorAlias"="CPU Package (/temperature/0)", "hardwareName"="Intel Core Ultra 9 275HX", "hardwareAlias"="Intel Core Ultra 9 275HX (/intelcpu/0)", "sensorId"="/temperature/0", "hardwareId"="/intelcpu/0", "host"="BLACKGOLDPIG"} 68
lhm_gpunvidia_temperature_celsius {"sensorName"="GPU Core", "sensorAlias"="GPU Core (/temperature/0)", "hardwareName"="NVIDIA GeForce RTX 5090 Laptop GPU", "hardwareAlias"="NVIDIA GeForce RTX 5090 Laptop GPU (/gpu-nvidia/0)", "sensorId"="/temperature/0", "hardwareId"="/gpu-nvidia/0", "host"="BLACKGOLDPIG"} 44
lhm_motherboard_fan_rpm {"sensorName"="Chassis Fan 1", "sensorAlias"="Chassis Fan 1 (/fan/0)", "hardwareName"="Nuvoton NCT6799D", "hardwareAlias"="Nuvoton NCT6799D (/lpc/nct6799d/0)", "sensorId"="/fan/0", "hardwareId"="/lpc/nct6799d/0", "host"="BLACKGOLDPIG"} 830
"""

METRICS = (
    LHM_096
    + """\
# HELP lhm_cpu_temperature_celsius Temperature
# TYPE lhm_cpu_temperature_celsius gauge
lhm_cpu_temperature_celsius{sensorName="Core #1",hardwareName="Intel Core i9",sensorId="/intelcpu/0/temperature/0"} 52
lhm_cpu_temperature_celsius{sensorName="CPU Package",hardwareName="Intel Core i9",sensorId="/intelcpu/0/temperature/8"} 68.25 1761460000000
lhm_gpunvidia_temperature_celsius{sensorName="GPU Core",hardwareName="NVIDIA GeForce RTX 4080",sensorId="/gpu-nvidia/0/temperature/0"} 40
lhm_gpunvidia_temperature_celsius{sensorName="GPU Hot Spot",hardwareName="NVIDIA GeForce RTX 4080",sensorId="/gpu-nvidia/0/temperature/2"} 51
lhm_gpunvidia_temperature_celsius{sensorName="GPU Core",hardwareName="NVIDIA GeForce RTX 3090",sensorId="/gpu-nvidia/1/temperature/0"} 47.04
lhm_gpunvidia_temperature_celsius{sensorName="GPU Core",hardwareName="Broken GPU",sensorId="/gpu-nvidia/2/temperature/0"} NaN
lhm_motherboard_fan_rpm{sensorName="CPU Fan",hardwareName="Nuvoton NCT6798D",sensorId="/lpc/nct6798d/0/fan/0"} 1218.4
lhm_motherboard_fan_rpm{sensorName="SYS Fan1",hardwareName="Nuvoton NCT6798D",sensorId="/lpc/nct6798d/0/fan/1"} 2554
lhm_motherboard_fan_rpm{sensorName="SYS Fan2",hardwareName="Nuvoton NCT6798D",sensorId="/lpc/nct6798d/0/fan/2"} 0
lhm_motherboard_fan_rpm{sensorName="Pump \\"AIO\\"",hardwareName="Nuvoton NCT6798D",sensorId="/lpc/nct6798d/0/fan/3"} 2890
lhm_gpunvidia_fan_rpm{sensorName="GPU Fan 1",hardwareName="NVIDIA GeForce RTX 4080",sensorId="/gpu-nvidia/0/fan/0"} 1100
lhm_gpunvidia_fan_rpm{sensorName="GPU Fan 2",hardwareName="NVIDIA GeForce RTX 4080",sensorId="/gpu-nvidia/0/fan/1"} +Inf
lhm_cpu_load_percent{sensorName="CPU Package",hardwareName="Intel Core i9",sensorId="/intelcpu/0/load/0"} 99
"""
)

NULLS = {"source": None, "cpu_c": None, "gpu_c": None, "fans": []}


# ── parser ──────────────────────────────────────────────────────────────────
def test_metrics_give_cpu_package_hottest_gpu_core_and_spinning_fans():
    assert sensors.parse_metrics(METRICS) == {
        "cpu_c": 68.2,
        "gpu_c": 47.0,
        "fans": [
            {"name": "Chassis Fan 1", "rpm": 830},
            {"name": "CPU Fan", "rpm": 1218},
            {"name": "SYS Fan1", "rpm": 2554},
            {"name": 'Pump "AIO"', "rpm": 2890},
            {"name": "GPU Fan 1", "rpm": 1100},
        ],
    }


def test_lhm_0_9_6_output_parses():
    assert sensors.parse_metrics(LHM_096) == {
        "cpu_c": 68.0,
        "gpu_c": 44.0,
        "fans": [{"name": "Chassis Fan 1", "rpm": 830}],
    }


def test_selection_ignores_the_hardware_type_segment():
    text = (
        'lhm_amdcpu_temperature_celsius{sensorName="CPU Package"} 55\n'
        'lhm_superio_fan_rpm{sensorName="CPU Fan"} 900\n'
    )
    assert sensors.parse_metrics(text) == {
        "cpu_c": 55.0,
        "gpu_c": None,
        "fans": [{"name": "CPU Fan", "rpm": 900}],
    }


@pytest.mark.parametrize("value", ["NaN", "+Inf", "-Inf", "x"])
def test_a_non_finite_temperature_is_null(value):
    text = (
        f'lhm_cpu_temperature_celsius{{sensorName="CPU Package"}} {value}\n'
        f'lhm_gpunvidia_temperature_celsius{{sensorName="GPU Core"}} {value}\n'
    )
    parsed = sensors.parse_metrics(text)
    assert parsed == {"cpu_c": None, "gpu_c": None, "fans": []}
    json.dumps(parsed, allow_nan=False)


def test_text_without_lhm_samples_is_all_absent():
    assert sensors.parse_metrics("<html>not metrics</html>\n") == {
        "cpu_c": None,
        "gpu_c": None,
        "fans": [],
    }


# ── read_lhm against a local fake ───────────────────────────────────────────
@pytest.fixture
def lhm(monkeypatch):
    """A fake LHM on 127.0.0.1; `answer` is (status, body)."""
    answer = {"status": 200, "body": METRICS.encode()}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            answer["path"] = self.path
            self.send_response(answer["status"])
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(answer["body"])

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(
        sensors, "LHM_URL", f"http://127.0.0.1:{server.server_port}/metrics"
    )
    try:
        yield answer
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_read_lhm_fetches_the_metrics_page(lhm):
    assert sensors.read_lhm() == sensors.parse_metrics(METRICS)
    assert lhm["path"] == "/metrics"


@pytest.mark.parametrize("status,body", [(500, b"oops"), (200, b"\xff\xfe\xfa")])
def test_read_lhm_is_none_on_error_status_or_undecodable_body(lhm, status, body):
    lhm.update(status=status, body=body)
    assert sensors.read_lhm() is None


def test_read_lhm_is_none_when_nothing_listens(monkeypatch):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    monkeypatch.setattr(sensors, "LHM_URL", f"http://127.0.0.1:{port}/metrics")
    # Windows retries a refused loopback connect until the timeout
    monkeypatch.setattr(sensors, "LHM_TIMEOUT", 0.3)
    assert sensors.read_lhm() is None


def test_the_lhm_address_is_fixed():
    assert sensors.LHM_URL == "http://127.0.0.1:8085/metrics"


# ── read_sensors: LHM first, else the shared nvidia-smi sample ──────────────
def test_lhm_readings_are_marked_lhm(monkeypatch):
    monkeypatch.setattr(sensors, "read_lhm", lambda: sensors.parse_metrics(METRICS))
    monkeypatch.setattr(
        gpu_sample, "gpu_sample", lambda: pytest.fail("LHM answered; no nvidia-smi")
    )
    got = READ_SENSORS()
    assert got["source"] == "lhm"
    assert (got["cpu_c"], got["gpu_c"], len(got["fans"])) == (68.2, 47.0, 5)


def test_without_lhm_the_gpu_comes_from_the_shared_nvidia_smi_sample(monkeypatch):
    monkeypatch.setattr(sensors, "read_lhm", lambda: None)
    monkeypatch.setattr(
        gpu_sample, "gpu_sample", lambda: {"util_pct": 3.0, "temp_c": 41.0}
    )
    assert READ_SENSORS() == {
        "source": "nvidia-smi",
        "cpu_c": None,
        "gpu_c": 41.0,
        "fans": [],
    }


@pytest.mark.parametrize("sample", [None, {"util_pct": 3.0, "temp_c": None}])
def test_without_lhm_or_a_gpu_temperature_there_is_no_source(monkeypatch, sample):
    monkeypatch.setattr(sensors, "read_lhm", lambda: None)
    monkeypatch.setattr(gpu_sample, "gpu_sample", lambda: sample)
    assert READ_SENSORS() == NULLS


# ── the sampler ─────────────────────────────────────────────────────────────
def test_snapshot_before_and_after_a_sample():
    now = [10.0]
    reading = {"source": "lhm", "cpu_c": 60.0, "gpu_c": 40.0, "fans": []}
    calls = []

    def reader():
        calls.append(True)
        now[0] += 2
        return reading

    sampler = sensors.SensorSampler(
        reader=reader, clock=lambda: now[0], wall=lambda: 1_800_000_000.0
    )
    assert sampler.snapshot() == {"at": None, **NULLS}
    assert sampler.age_s() is None
    sampler.tick()
    assert sampler.snapshot() == {"at": 1_800_000_000.0, **reading}
    assert sampler.age_s() == 2
    now[0] += 5
    assert sampler.age_s() == 7
    assert len(calls) == 1  # snapshots never sample


def test_snapshot_is_a_copy():
    sampler = sensors.SensorSampler(
        reader=lambda: {
            "source": "lhm",
            "cpu_c": None,
            "gpu_c": None,
            "fans": [{"name": "CPU Fan", "rpm": 900}],
        }
    )
    sampler.tick()
    sampler.snapshot()["fans"][0]["rpm"] = 1
    assert sampler.snapshot()["fans"] == [{"name": "CPU Fan", "rpm": 900}]


def test_tick_reads_through_the_module_reader_at_call_time(monkeypatch):
    sampler = sensors.SensorSampler()
    monkeypatch.setattr(
        sensors,
        "read_sensors",
        lambda: {"source": "nvidia-smi", "cpu_c": None, "gpu_c": 39.0, "fans": []},
    )
    sampler.tick()
    assert sampler.snapshot()["gpu_c"] == 39.0


def test_the_sampler_thread_samples_and_stops():
    sampled = threading.Event()

    def reader():
        sampled.set()
        return dict(NULLS, source="lhm")

    sampler = sensors.SensorSampler(reader=reader, interval=0.01)
    sampler.start()
    sampler.start()
    try:
        assert sampled.wait(3)
    finally:
        assert sampler.stop()
    assert sampler.stop()
    assert sampler._thread is not None and not sampler._thread.is_alive()
    assert sampler.snapshot()["source"] == "lhm"
