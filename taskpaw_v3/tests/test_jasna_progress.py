"""LTX console telemetry only: no real Jasna, GPU, model or live configuration."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_jasna import _events, _Launcher, _ltx_model, _managed, _patch, _probe, _videos
from test_jasna_subs import _setup

from taskpaw_v3.monitors.plugins import jasna as J
from taskpaw_v3.monitors.plugins import jasna_progress as P

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

    def record(buf, generation=None):
        consume(buf, generation)
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


class _HeldEOF(io.BytesIO):
    """Hold a reader after its last byte, before EOF flushes its buffered line."""

    def __init__(self, data):
        super().__init__(data)
        self.waiting = threading.Event()
        self.release = threading.Event()

    def read(self, size=-1):
        chunk = super().read(size)
        if chunk == b"":
            self.waiting.set()
            if not self.release.wait(5):
                raise AssertionError("test did not release its old reader")
        return chunk


def _start_held_reader(
    tmp_path, monkeypatch, payload, *, late_entry=False, second_file=False
):
    cfg, inp, _, home = _managed(tmp_path, ltx_1080p=True, jasna_capture_progress=False)
    _ltx_model(home)
    _videos(inp, "a.mp4", *(("b.mp4",) if second_file else ()))
    launcher = _Launcher([None, None])
    held = _HeldEOF(payload)

    def launch(*args, **kwargs):
        proc = launcher(*args, **kwargs)
        if launcher.n == 1:
            proc.stdout = io.BytesIO(payload) if late_entry else held
        return proc

    # A 1080p LTX file followed by uncaptured 4K BasicVSR++ has no new
    # progress reader to hide an old reader's late writes.
    _patch(monkeypatch, launch, _probe({"a.mp4": (1920, 1080), "b.mp4": (3840, 2160)}))
    inst = J.JasnaInstance("j1", cfg)
    if late_entry:
        reader_loop = inst._reader_loop

        def enter_later(proc, *args):
            if proc is launcher.procs[0]:
                held.waiting.set()
                if not held.release.wait(5):
                    raise AssertionError("test did not release its old reader entry")
            reader_loop(proc, *args)

        monkeypatch.setattr(inst, "_reader_loop", enter_later)
    _, emit = _events()
    inst.start(emit)
    old_reader = inst._reader
    assert old_reader is not None and held.waiting.wait(2)
    joins = []
    # Deterministically model an expired bounded join while the reader is
    # still blocked, without adding one/two-second wall-clock sleeps.
    monkeypatch.setattr(old_reader, "join", lambda timeout=None: joins.append(timeout))
    return inst, emit, launcher, held, old_reader, joins


@pytest.mark.parametrize("payload", [ENCODE.encode(), b"old process crash detail"])
@pytest.mark.parametrize("late_entry", [False, True])
def test_old_reader_cannot_write_into_next_uncaptured_model(
    tmp_path, monkeypatch, payload, late_entry
):
    inst, emit, launcher, held, old_reader, joins = _start_held_reader(
        tmp_path, monkeypatch, payload, late_entry=late_entry, second_file=True
    )
    try:
        launcher.procs[0]._rc = 0
        status = inst.check(emit)
        assert status.metrics["current_file"] == "b.mp4"
        assert launcher.procs[1].stdout is None and 1 in joins
        held.release.set()
        threading.Thread.join(old_reader, timeout=2)
        assert not old_reader.is_alive()
        assert (inst._progress, list(inst._recent_output)) == ({}, [])
        assert "ltx_progress" not in inst.check(emit).metrics
    finally:
        held.release.set()
        threading.Thread.join(old_reader, timeout=2)
        inst.stop(timeout=0.5)


@pytest.mark.parametrize("payload", [ENCODE.encode(), b"old process crash detail"])
@pytest.mark.parametrize("restart", [False, True])
def test_stopped_reader_cannot_write_after_stop_or_restart(
    tmp_path, monkeypatch, payload, restart
):
    inst, emit, launcher, held, old_reader, joins = _start_held_reader(
        tmp_path, monkeypatch, payload
    )
    try:
        inst.stop(timeout=0.5)
        assert 2 in joins
        if restart:
            launcher._output = DENOISE + "\rnew process note"
            inst.start(emit)
            current_reader = inst._reader
            assert current_reader is not None and current_reader is not old_reader
            current_reader.join(timeout=2)
            assert inst._progress["ltx_progress"]["stage_name"] == "denoise"
            expected = (dict(inst._progress), ["new process note"])
        else:
            expected = ({}, [])
        held.release.set()
        threading.Thread.join(old_reader, timeout=2)
        assert not old_reader.is_alive()
        assert (inst._progress, list(inst._recent_output)) == expected
    finally:
        held.release.set()
        threading.Thread.join(old_reader, timeout=2)
        inst.stop(timeout=0.5)


def test_current_reader_eof_tail_survives_process_exit_before_drain(
    tmp_path, monkeypatch
):
    inst, emit, launcher, held, old_reader, _ = _start_held_reader(
        tmp_path, monkeypatch, b"current process final crash detail"
    )
    try:
        launcher.procs[0]._rc = 1
        inst.check(emit)
        assert inst._process is None
        held.release.set()
        threading.Thread.join(old_reader, timeout=2)
        assert not old_reader.is_alive()
        assert list(inst._recent_output) == ["current process final crash detail"]
    finally:
        held.release.set()
        threading.Thread.join(old_reader, timeout=2)
        inst.stop(timeout=0.5)


# ── U1c: the auto-split mosaic scan that runs before any LTX stage ─────────
# tqdm(total=total, desc="Mosaic scan", unit="sample") in jasna/mosaic_scan.py
SCAN_START = "Mosaic scan:   0%|          | 0/5000 [00:00<?, ?sample/s]"
SCAN = "Mosaic scan:  12%|█▏        | 600/5000 [00:40<04:53, 15.00sample/s]"
SCAN_SLOW = "Mosaic scan:   1%|          | 50/5000 [01:40<2:45:00,  2.00s/sample]"
SCAN_DONE = "Mosaic scan: 100%|██████████| 5000/5000 [05:33<00:00, 15.00sample/s]"
BASIC = (
    "Processing video:  15%|███ |Processed: 06:09 (36703f) | "
    "Remaining: 30:47 (207454f) | Speed: 112.3fps"
)
SPLIT = (
    "2026-10-22 10:00:00 INFO ABF-394.mp4: LTX auto split: 300 s with mosaics, "
    "120 s (40%) of it with LTX"
)
SKIPPED = (
    "ABF-394.mp4: LTX auto split skipped (not smart-render compatible); "
    "restoring the whole video with LTX"
)
NO_MOSAICS = (
    "ABF-394.mp4: LTX auto split found no mosaics; restoring the whole video with LTX"
)


def test_mosaic_scan_is_a_pre_ltx_phase_snapshot_not_film_progress():
    value = J.parse_jasna_progress_line(SCAN, {"current_file": "a.mp4"})
    assert value == {
        "current_file": "a.mp4",
        "scan_progress": {
            "percent": 12.0,
            "percent_text": "12",
            "current": "600",
            "total": "5000",
            "elapsed": "00:40",
            "eta": "04:53",
            "rate_value": "15.00",
            "rate_unit": "sample/s",
            "raw_line": SCAN,
        },
    }
    assert json.loads(json.dumps(value)) == value


def test_mosaic_scan_unknowns_stay_missing_and_slow_rates_stay_literal():
    start = J.parse_jasna_progress_line(SCAN_START, {})["scan_progress"]
    assert start == {
        "percent": 0.0,
        "percent_text": "0",
        "current": "0",
        "total": "5000",
        "elapsed": "00:00",
        "raw_line": SCAN_START,
    }
    slow = J.parse_jasna_progress_line(SCAN_SLOW, {})["scan_progress"]
    assert (slow["rate_value"], slow["rate_unit"], slow["eta"]) == (
        "2.00",
        "s/sample",
        "2:45:00",
    )


@pytest.mark.parametrize(
    "line",
    [
        "\x1b[32m" + SCAN_START + "\r" + SCAN + "\x1b[0m",
        SCAN_START + "\r" + SCAN + "\r",
        SCAN + "\rwarning: GPU cache 50%",
        "\x1b]0;title\x07" + SCAN + "\x1b[K",
    ],
)
def test_cr_joined_and_ansi_scan_lines_keep_the_latest_bar(line):
    value = J.parse_jasna_progress_line(line, {})
    assert value["scan_progress"]["current"] == "600"
    assert value["scan_progress"]["raw_line"] == SCAN
    assert "percent" not in value


def test_scan_ends_on_ltx_or_basicvsr_and_ignores_unrelated_percentages():
    scan = J.parse_jasna_progress_line(SCAN_DONE, {"current_file": "a.mp4"})
    assert J.parse_jasna_progress_line("warning: GPU cache 50%", scan) is scan
    ltx = J.parse_jasna_progress_line(ENCODE, scan)
    assert "scan_progress" not in ltx and ltx["ltx_stage"] == "2/4 encode"
    basic = J.parse_jasna_progress_line(BASIC, scan)
    assert "scan_progress" not in basic
    assert (basic["current_file"], basic["percent"], basic["fps"]) == (
        "a.mp4",
        15,
        112.3,
    )
    # the next film's scan ends the previous film's LTX observation
    again = J.parse_jasna_progress_line(SCAN, ltx)
    assert not ({"ltx_progress", "ltx_stage", "percent", "fps", "eta"} & again.keys())
    assert again["scan_progress"]["percent"] == 12.0


@pytest.mark.parametrize(
    ("line", "kind"),
    [(SPLIT, "planned"), (SKIPPED, "skipped"), (NO_MOSAICS, "no_mosaics")],
)
def test_auto_split_log_lines_mark_the_phase_without_a_fake_percent(line, kind):
    scan = J.parse_jasna_progress_line(SCAN_DONE, {"current_file": "a.mp4"})
    value = J.parse_jasna_progress_line("\x1b[33m" + line + "\x1b[0m", scan)
    assert value == {
        "current_file": "a.mp4",
        "split_note": {"kind": kind, "text": line},
    }
    # the note is a fact of this film: it survives into LTX, a new scan drops it
    ltx = J.parse_jasna_progress_line(ENCODE, value)
    assert ltx["split_note"]["kind"] == kind
    assert "split_note" not in J.parse_jasna_progress_line(SCAN, ltx)


def test_console_text_is_the_latest_plain_segment_bounded():
    assert P.console_text("\x1b[32m" + SCAN_START + "\r" + SCAN + "\x1b[0m\r") == SCAN
    assert P.console_text("  loading model…  ") == "loading model…"
    assert P.console_text("\r\n \x1b[K") == ""
    assert len(P.console_text("x" * 5000)) == 300


def test_monitor_shows_the_scan_without_reporting_it_as_film_progress(
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
    r.inst._consume_output(SCAN.encode())
    status = r.inst.check(r.emit)
    assert status.metrics["scan_progress"]["current"] == "600"
    assert "percent" not in status.metrics
    assert "Mosaic scan 12%" in status.detail
    r.inst.stop(timeout=1)
