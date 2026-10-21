"""#285 C8/C9: the output bitrate follows the source (`match_source_bitrate`) and
the cq default moves to 20. ffprobe and jasna.exe are NEVER executed here: the
bitrate probe runs against a fake `subprocess.run`, launches against `_Launcher`."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import tasklog_rows as _tasklog
from test_jasna import _events, _Launcher, _ltx_model, _managed, _patch, _videos

from taskpaw_v3.monitors.plugins import jasna as J
from taskpaw_v3.monitors.plugins.jasna import JasnaConfig, JasnaInstance, build_argv

# conftest replaces `J.probe_bitrate` for every test; this is the real one.
_REAL_PROBE_BITRATE = J.probe_bitrate
_B = 45_234_567  # bits/s → "maxrate 45.2 Mbps"


@pytest.fixture(autouse=True)
def _no_gpu(monkeypatch):
    monkeypatch.setattr(J, "query_gpus", lambda: None)  # never the real nvidia-smi


def _cfg(**kw) -> JasnaConfig:
    return JasnaConfig(**{"name": "jasna", **kw})


def _argv(cfg: JasnaConfig, bitrate, tier="1080p") -> list[str]:
    return build_argv(
        cfg, "jasna.exe", Path("a.mp4"), Path("a.tmp.mp4"), tier, False, False,
        source_bitrate=bitrate,
    )  # fmt: skip


class _Run:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def _probe_out(stream=None, tags=None, fmt=None) -> str:
    s: dict = {}
    if stream is not None:
        s["bit_rate"] = stream
    if tags is not None:
        s["tags"] = tags
    doc: dict = {"programs": [], "streams": [s]}
    if fmt is not None:
        doc["format"] = {"bit_rate": fmt}
    return json.dumps(doc)


# ── the probe: Jasna's own fallback order ────────────────────────────────────
def test_probe_bitrate_reads_the_video_stream_with_one_bounded_ffprobe_call(
    monkeypatch,
):
    seen: dict = {}

    def run(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return _Run(_probe_out("12000000", {"BPS": "9"}, "13000000"))

    monkeypatch.setattr(J.subprocess, "run", run)
    assert _REAL_PROBE_BITRATE(Path("a.mkv"), "ffprobe") == 12_000_000
    assert seen["argv"] == [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=bit_rate:stream_tags=BPS,BPS-eng:format=bit_rate",
        "-of",
        "json",
        "a.mkv",
    ]
    assert seen["kw"]["timeout"] == 5.0


@pytest.mark.parametrize(
    ("out", "expected"),
    [
        (_probe_out("N/A", {"BPS": "8000000"}, "9000000"), 8_000_000),
        (_probe_out(None, {"BPS-eng": "7000000"}, "9000000"), 7_000_000),
        (_probe_out(None, {"BPS": "x", "BPS-eng": "6000000"}), 6_000_000),
        (_probe_out(None, {"title": "t"}, "9000000"), 9_000_000),  # the container
        (_probe_out("0", None, "5000000"), 5_000_000),
        (_probe_out("12000000.7"), 12_000_000),  # Jasna's int(float(x))
        (_probe_out("nan", {"bps": "4e6"}), 4_000_000),  # tag case does not matter
        (_probe_out("inf", None, "3000000"), 3_000_000),
        (_probe_out("-5", None, "2000000"), 2_000_000),
    ],
)
def test_probe_bitrate_falls_back_like_jasna(monkeypatch, out, expected):
    monkeypatch.setattr(J.subprocess, "run", lambda *a, **k: _Run(out))
    assert _REAL_PROBE_BITRATE(Path("a.mkv"), "ffprobe") == expected


def test_probe_bitrate_failures_return_none(monkeypatch):
    assert _REAL_PROBE_BITRATE(Path("a.mp4"), None) is None
    for out in (
        _Run("", 1),
        _Run("garbage"),
        _Run("[]"),
        _Run(json.dumps({"streams": []})),
        _Run(_probe_out("N/A", {"BPS": "N/A"}, "N/A")),
        _Run(_probe_out("0", None, "0")),
    ):
        monkeypatch.setattr(J.subprocess, "run", lambda *a, _o=out, **k: _o)
        assert _REAL_PROBE_BITRATE(Path("a.mp4"), "ffprobe") is None

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("ffprobe", 5)

    monkeypatch.setattr(J.subprocess, "run", boom)
    assert _REAL_PROBE_BITRATE(Path("a.mp4"), "ffprobe") is None


# ── C8: the argv ─────────────────────────────────────────────────────────────
def test_argv_caps_the_output_at_the_source_bitrate_before_the_extra_args():
    cfg = _cfg(jasna_extra_args="--device cuda:1")
    assert cfg.match_source_bitrate is True  # on by default
    argv = _argv(cfg, 12_000_000)
    assert argv[-4:] == [
        "--encoder-settings",
        "maxrate=12000000,qmax=51",
        "--device",
        "cuda:1",
    ]
    assert argv.count("--encoder-settings") == 1


def test_argv_without_a_probed_bitrate_or_with_the_option_off_adds_nothing():
    plain = _argv(_cfg(match_source_bitrate=False), None)
    assert "--encoder-settings" not in plain
    assert _argv(_cfg(), None) == plain
    assert _argv(_cfg(), 0) == plain
    assert _argv(_cfg(match_source_bitrate=False), 12_000_000) == plain


def test_ltx_launches_carry_the_cap_after_the_ltx_options():
    argv = _argv(_cfg(ltx_1080p=True, ltx_auto_split=0, ltx_fast=False), 8_000_000)
    i = argv.index("--encoder-settings")
    assert argv[i - 2 :] == [
        "--restoration-model-name",
        "ltx",
        "--encoder-settings",
        "maxrate=8000000,qmax=51",
    ]


# ── C8: the extra args ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "extra",
    [
        "--encoder-settings maxrate=1",
        "--encoder-settings=cq=20",
        "--encoder maxrate=1",  # an argparse abbreviation
    ],
)
def test_encoder_settings_in_extra_args_is_rejected_while_on(extra):
    with pytest.raises(ValueError, match="match_source_bitrate"):
        _cfg(jasna_extra_args=extra)
    assert _cfg(jasna_extra_args=extra, match_source_bitrate=False)


def test_other_extra_args_pass_while_on():
    assert _cfg(jasna_extra_args="--device cuda:1")


# ── C9: cq 20 and the legacy migration ───────────────────────────────────────
def test_cq_defaults_to_20_and_the_help_names_jasnas_own_default():
    assert _cfg().cq == 20
    desc = J.JasnaPlugin.json_schema()["properties"]["cq"]["description"]
    assert "28" in desc and "24" in desc and "20" in desc
    assert "24 is Jasna's default" not in desc


@pytest.mark.parametrize(
    ("stored", "cq"),
    [
        ({"ltx_best": True, "cq": 24}, 20),
        ({"ltx_best": False, "cq": 24}, 20),
        ({"ltx_best": False, "cq": 22}, 22),  # a hand-set level stays
        ({"ltx_best": False}, 20),
        ({"cq": 24}, 24),  # no ltx_best: a 3.9.18 config chose 24
    ],
)
def test_pre_3918_cq_24_moves_to_20_only_with_ltx_best(stored, cq):
    assert _cfg(**stored).cq == cq


def test_an_unreleased_top_tier_boots_as_high():
    # #285 V03: a config saved by a four-tier build of 3.9.18 still loads
    stored = {"name": "j", "ltx_model": "top", "cq": 24}
    assert J.migrate_legacy_ltx(stored) == {"name": "j", "ltx_model": "high", "cq": 24}
    assert stored["ltx_model"] == "top"  # never mutated
    assert _cfg(ltx_model="top").ltx_model == "high"
    assert J.JasnaPlugin.migrate_config({"ltx_model": "top"}) == {"ltx_model": "high"}


def test_migration_maps_the_tier_and_cq_together_without_mutating():
    stored = {"name": "j", "ltx_best": True, "cq": 24}
    assert J.migrate_legacy_ltx(stored) == {"name": "j", "ltx_model": "high", "cq": 20}
    assert stored == {"name": "j", "ltx_best": True, "cq": 24}


# ── C8: the mode / label text ────────────────────────────────────────────────
def test_ltx_mode_and_label_carry_the_maxrate_when_applied():
    cfg = _cfg(ltx_1080p=True, ltx_auto_split=0)
    assert J._ltx_mode(cfg, True, False, _B) == "ltx standard fast maxrate 45.2 Mbps"
    assert J._ltx_label(cfg, False, False, _B) == ["LTX standard", "maxrate 45.2 Mbps"]
    assert J._ltx_mode(cfg, True, False) == "ltx standard fast"  # download variant
    off = _cfg(ltx_1080p=True, ltx_auto_split=0, match_source_bitrate=False)
    assert J._ltx_mode(off, True, False, _B) == "ltx standard fast"


def _run_one(tmp_path, monkeypatch, bitrate, rcs=(0,), **kw):
    cfg, inp, _out, home = _managed(tmp_path, **kw)
    if kw.get("ltx_1080p"):
        _ltx_model(home)
    _videos(inp, "a.mp4")
    calls: list[str] = []

    def probe(video, ffprobe):
        calls.append(Path(video).name)
        return bitrate

    launcher = _Launcher(list(rcs))
    _patch(monkeypatch, launcher)
    monkeypatch.setattr(J, "probe_bitrate", probe)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)
    return inst, launcher, calls, emit


def test_a_launch_probes_caps_and_names_the_maxrate(tmp_path, monkeypatch):
    inst, launcher, calls, _emit = _run_one(tmp_path, monkeypatch, _B, rcs=[None])
    try:
        assert calls == ["a.mp4"]
        assert launcher.arg(0, "--encoder-settings") == (f"maxrate={_B},qmax=51")
        (row,) = _tasklog("restore.started")
        assert row["data"]["mode"] == "unet-4x maxrate 45.2 Mbps"
        assert "running: a.mp4 [1080p 1920x1080, unet-4x, maxrate 45.2 Mbps]" in (
            inst._build_status("running").detail
        )
    finally:
        inst.stop(timeout=0.5)


def test_an_ltx_launch_names_the_maxrate_in_mode_and_label(tmp_path, monkeypatch):
    inst, launcher, _calls, _emit = _run_one(
        tmp_path, monkeypatch, _B, rcs=[None], ltx_1080p=True
    )
    try:
        assert launcher.arg(0, "--encoder-settings") == (f"maxrate={_B},qmax=51")
        (row,) = _tasklog("restore.started")
        assert row["data"]["mode"] == (
            "ltx standard fast auto-split 12% maxrate 45.2 Mbps"
        )
        assert (
            "running: a.mp4 [1080p 1920x1080, LTX standard fast, auto-split 12%, "
            "maxrate 45.2 Mbps]"
        ) in inst._build_status("running").detail
    finally:
        inst.stop(timeout=0.5)


def test_a_failed_probe_adds_no_flag_and_warns_once_per_file(
    tmp_path, monkeypatch, caplog
):
    with caplog.at_level("WARNING", logger="taskpaw.monitors.jasna"):
        inst, launcher, calls, emit = _run_one(
            tmp_path, monkeypatch, None, rcs=[1, 0], match_source_bitrate=True
        )
        inst.check(emit)  # a fails → the plain retry of the same file
        inst.check(emit)
    assert launcher.inputs() == ["a.mp4", "a.mp4"]
    assert all("--encoder-settings" not in argv for argv in launcher.launches)
    assert calls == ["a.mp4"]  # the retry reuses the probe
    warned = [r for r in caplog.records if "source bitrate" in r.getMessage()]
    assert len(warned) == 1 and "a.mp4" in warned[0].getMessage()
    modes = [r["data"]["mode"] for r in _tasklog("restore.started")]
    assert modes == ["unet-4x", "plain"]


def test_the_degrade_relaunch_and_retries_reuse_the_probed_bitrate(
    tmp_path, monkeypatch
):
    # #285 V09: a failed unet-4x launch is relaunched without unet-4x, then
    # retried — every launch of the file carries the same, once-probed cap.
    inst, launcher, calls, emit = _run_one(tmp_path, monkeypatch, _B, rcs=[1, 1, 0])
    for _ in range(4):
        inst.check(emit)
    assert launcher.inputs() == ["a.mp4"] * launcher.n and launcher.n >= 2
    assert launcher.secondary(0) == "unet-4x"
    assert launcher.secondary(1) == "none"  # the degrade relaunch
    assert {launcher.arg(i, "--encoder-settings") for i in range(launcher.n)} == {
        f"maxrate={_B},qmax=51"
    }
    assert calls == ["a.mp4"]
    modes = [r["data"]["mode"] for r in _tasklog("restore.started")]
    assert modes[:2] == ["unet-4x maxrate 45.2 Mbps", "plain maxrate 45.2 Mbps"]


def test_with_the_option_off_nothing_is_probed(tmp_path, monkeypatch, caplog):
    with caplog.at_level("WARNING", logger="taskpaw.monitors.jasna"):
        inst, launcher, calls, _emit = _run_one(
            tmp_path, monkeypatch, _B, rcs=[None], match_source_bitrate=False
        )
    try:
        assert calls == []
        assert "--encoder-settings" not in launcher.launches[0]
        assert not [r for r in caplog.records if "source bitrate" in r.getMessage()]
    finally:
        inst.stop(timeout=0.5)


def test_the_tickbox_sits_next_to_codec_and_cq():
    order = J.JasnaPlugin.ui_schema()["ui:order"]
    assert order[order.index("codec") : order.index("codec") + 3] == [
        "codec",
        "cq",
        "match_source_bitrate",
    ]
    prop = J.JasnaPlugin.json_schema()["properties"]["match_source_bitrate"]
    assert prop["default"] is True
    assert prop["title"] == "Output bitrate follows the source"
    for part in ("1.0x", "qmax=51", "H.264", "NVIDIA", "AMD"):
        assert part in prop["description"]
