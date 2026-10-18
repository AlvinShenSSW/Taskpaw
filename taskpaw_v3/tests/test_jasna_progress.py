"""LTX console telemetry only: no real Jasna, GPU, model or live configuration."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_jasna import _events, _Launcher, _ltx_model, _managed, _patch, _videos
from test_jasna_subs import _setup

from taskpaw_v3.monitors.plugins import jasna as J

ENCODE = (
    "LTX 2/4 encode:  52.00%|█████▏    | 156/300 "
    "[00:27<00:25,  5.620frame/s, run 12:12 left]"
)
DENOISE = (
    "LTX 3/4 denoise: 100%|██████████| 120/120 "
    "[04:41<00:00,  2.34s/step, run 04:57 left]"
)
FIXTURES = Path(__file__).with_name("fixtures")


@pytest.fixture(autouse=True)
def no_gpu(monkeypatch):
    monkeypatch.setattr(J, "query_gpus", lambda: None)


def test_complete_snapshot_preserves_console_precision_and_legacy_compatibility():
    value = J.parse_jasna_progress_line(ENCODE, {"current_file": "a.mp4"})
    assert value["ltx_progress"] == {
        "mode": "restore",
        "stage_index": 2,
        "stage_total": 4,
        "stage_name": "encode",
        "percent": 52,
        "percent_text": "52.00",
        "current": "156",
        "total": "300",
        "rate_value": "5.620",
        "rate_unit": "frame/s",
        "elapsed": "00:27",
        "eta": "00:25",
        "run_eta": "12:12",
        "raw_line": ENCODE,
    }
    assert {k: v for k, v in value.items() if k != "ltx_progress"} == {
        "current_file": "a.mp4",
        "ltx_stage": "2/4 encode",
        "percent": 52,
        "fps": 5.62,
        "eta": "12:12",
    }
    assert json.loads(json.dumps(value)) == value


@pytest.mark.parametrize(
    "unit", ["frame/s", "s/frame", "s/step", "step/s", "it/s", "s/it"]
)
def test_rate_units_stay_literal(unit):
    line = f"LTX 3/4 denoise: 1%| | 1/120 [00:03<05:00, 1.230e+01{unit}]"
    value = J.parse_jasna_progress_line(line, {})
    assert value["ltx_progress"]["rate_value"] == "1.230e+01"
    assert value["ltx_progress"]["rate_unit"] == unit
    assert ("fps" in value) == (unit == "frame/s")


@pytest.mark.parametrize("prefix", ["LTX", "LTX speed test"])
@pytest.mark.parametrize(
    "stage", ["2/4 encode", "3/4 denoise", "1/4 scan", "1/2 unexpected"]
)
def test_each_update_replaces_missing_data_even_for_repeated_stage(prefix, stage):
    before = J.parse_jasna_progress_line(
        ENCODE, {"elapsed": "10:00", "processed_frames": 5}
    )
    line = f"{prefix} {stage}: | | 0/300 [00:00<?, ?frame/s]"
    after = J.parse_jasna_progress_line(line, before)
    snap = after["ltx_progress"]
    assert snap["mode"] == ("speed_test" if "speed test" in prefix else "restore")
    assert snap["current"] == "0" and snap["total"] == "300"
    assert snap["elapsed"] == "00:00"
    assert not (
        {"percent", "percent_text", "eta", "run_eta", "rate_value", "rate_unit"}
        & snap.keys()
    )
    assert not ({"percent", "eta", "fps", "elapsed", "processed_frames"} & after.keys())
    assert before["ltx_progress"]["rate_value"] == "5.620"  # pure parser


def test_unknown_stage_and_hour_times_are_original_values():
    line = "LTX 5/7 finalize: 12.5%| | 1.25e3/1.00e4 [1:02:03<10:22:33, 12.00it/s, run 123:45:56 left]"
    snap = J.parse_jasna_progress_line(line, {})["ltx_progress"]
    assert (snap["stage_name"], snap["stage_index"], snap["stage_total"]) == (
        "finalize",
        5,
        7,
    )
    assert (snap["current"], snap["total"], snap["percent_text"]) == (
        "1.25e3",
        "1.00e4",
        "12.5",
    )
    assert (snap["elapsed"], snap["eta"], snap["run_eta"]) == (
        "1:02:03",
        "10:22:33",
        "123:45:56",
    )


@pytest.mark.parametrize("body", ["-1%", "101%", "NaN%", "1e999%", "?%"])
def test_invalid_values_are_missing_without_fabricating_zero(body):
    line = f"LTX 1/4 scan: {body}| | NaN/Infinity [??<99:99, 1e999frame/s, run nonsense left]"
    value = J.parse_jasna_progress_line(line, {})
    assert set(value["ltx_progress"]) == {
        "mode",
        "stage_index",
        "stage_total",
        "stage_name",
        "raw_line",
    }
    assert "percent" not in value and "fps" not in value and "eta" not in value


@pytest.mark.parametrize(
    "line",
    [
        "Jasna: checked 14563/19444 files (74.9%)",
        "LTX plan: 300 frames, 3 windows, 50% logged",
        "LTX 0/4 scan: 50%",
        "LTX 5/4 scan: 50%",
        "LTX 1/0 scan: 50%",
        "LTX 1/4 " + "x" * 129 + ": 50%",
        "warning: GPU cache 50%",
        "",
        "model loading",
    ],
)
def test_unrelated_or_invalid_headers_never_replace_current_observation(line):
    before = J.parse_jasna_progress_line(ENCODE, {})
    assert J.parse_jasna_progress_line(line, before) is before


def test_ordinary_progress_clears_all_ltx_values_before_partial_basic_update():
    before = J.parse_jasna_progress_line(ENCODE, {"current_file": "a.mp4"})
    assert J.parse_jasna_progress_line("Processing video: 5%| |", before) == {
        "current_file": "a.mp4",
        "percent": 5,
    }
    assert J.parse_jasna_progress_line("b.mp4:", before) == {"current_file": "b.mp4"}


def test_plain_text_raw_excludes_terminal_sequences_and_appended_warning():
    line = (
        "\x1b]0;private window title\x07\x1b[32m"
        + ENCODE
        + "\x1b[0m\x1b[Kprivate warning 99%"
    )
    snap = J.parse_jasna_progress_line(line, {})["ltx_progress"]
    assert snap["raw_line"] == ENCODE
    assert "private" not in json.dumps(snap)
    assert (
        len(
            J.parse_jasna_progress_line("LTX 1/4 scan: 10%|" + "█" * 5000, {})[
                "ltx_progress"
            ]["raw_line"]
        )
        == 4096
    )


def test_reader_handles_cr_lf_ansi_split_utf8_and_eof_without_newline(tmp_path):
    cfg, *_ = _managed(tmp_path)
    inst = J.JasnaInstance("j1", cfg)
    observed = []
    consume = inst._consume_output

    def record(buf):
        consume(buf)
        if buf:
            observed.append(dict(inst._progress))

    inst._consume_output = record
    stream = "\x1b[32m" + ENCODE + "\x1b[0m\r\n" + DENOISE + "\rLTX 4/4 compose: 0%"
    inst._reader_loop(SimpleNamespace(stdout=io.BytesIO(stream.encode("utf-8"))))
    assert [m["ltx_progress"]["stage_name"] for m in observed] == [
        "encode",
        "denoise",
        "compose",
    ]
    assert observed[0]["ltx_progress"]["raw_line"] == ENCODE
    assert inst._progress["ltx_progress"] == {
        "mode": "restore",
        "stage_index": 4,
        "stage_total": 4,
        "stage_name": "compose",
        "percent": 0,
        "percent_text": "0",
        "raw_line": "LTX 4/4 compose: 0%",
    }


def test_recorded_and_source_formatter_fixtures_parse_without_losing_units():
    for filename in (
        "jasna_ltx_recorded.txt",
        "jasna_ltx_formatter.txt",
        "jasna_ltx_upstream_formatter.txt",
    ):
        previous = {}
        stages = set()
        modes = set()
        for line in (FIXTURES / filename).read_text(encoding="utf-8").splitlines():
            if not line or line.startswith("#"):
                continue
            previous = J.parse_jasna_progress_line(line, previous)
            snap = previous["ltx_progress"]
            assert snap["raw_line"] == line
            stages.add(snap["stage_name"])
            modes.add(snap["mode"])
            if "?" in line:
                assert "rate_value" not in snap and "eta" not in snap
            else:
                assert snap["rate_value"] + snap["rate_unit"] in line
                assert snap["current"] + "/" + snap["total"] in line
                assert snap["elapsed"] + "<" + snap["eta"] in line
        assert {"scan", "encode", "denoise", "compose"} <= stages
        if filename != "jasna_ltx_recorded.txt":
            assert modes == {"restore", "speed_test"}


def test_run_eta_and_stage_completion_never_enter_whole_film_pipeline(
    tmp_path, monkeypatch
):
    r = _setup(
        tmp_path,
        monkeypatch,
        pending=["a.mp4"],
        rcs=[None],
        jasna_capture_progress=True,
    )
    r.inst.start(r.emit)
    r.inst._consume_output(DENOISE.encode())
    metrics = r.inst.check(r.emit).metrics
    assert metrics["ltx_progress"]["run_eta"] == "04:57"
    assert metrics["percent"] == 100 and metrics["eta"] == "04:57"
    assert "percent" not in metrics["steps"][0] and "eta_s" not in metrics["steps"][0]
    assert (
        metrics["films"][0]["percent"] is None and metrics["films"][0]["eta_s"] is None
    )
    assert metrics["queue_completed"] == 0 and metrics["queue_total"] == 1
    r.inst.stop(timeout=1)


@pytest.mark.parametrize("outcome", ["stop", "failure", "complete"])
def test_lifecycle_removes_live_snapshot_and_restart_starts_clean(
    tmp_path, monkeypatch, outcome
):
    cfg, inp, _, home = _managed(tmp_path, ltx_1080p=True, jasna_capture_progress=False)
    _ltx_model(home)
    _videos(inp, "a.mp4")
    launcher = _Launcher([None, None])
    _patch(monkeypatch, launcher)
    inst = J.JasnaInstance("j1", cfg)
    _, emit = _events()
    inst.start(emit)
    inst._consume_output(ENCODE.encode())
    assert "ltx_progress" in inst.check(emit).metrics
    if outcome == "stop":
        inst.stop(timeout=0.5)
    else:
        launcher.procs[0]._rc = 1 if outcome == "failure" else 0
    metrics = inst.check(emit).metrics
    assert not ({"ltx_progress", "ltx_stage", "percent", "fps", "eta"} & metrics.keys())
    inst.start(emit)
    assert "ltx_progress" not in inst.check(emit).metrics
    inst.stop(timeout=0.5)
