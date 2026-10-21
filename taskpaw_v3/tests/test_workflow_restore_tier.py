"""U1 P4': the planned LTX tier reaches the restore launch, its claim and its model offer."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from taskpaw_v3.monitors.plugins.jasna_download import MODEL_FILES
from taskpaw_v3.tests.workflow_exec_helpers import (
    GPU_8GB,
    File,
    Tools,
    add_film,
    attempts,
    run_direct,
    run_stages,
    runtime,
    stage,
    video,
)
from taskpaw_v3.workflow import exec_restore
from taskpaw_v3.workflow.exec_common import MISSING_LTX


@pytest.fixture
def tools(tmp_path):
    return Tools(tmp_path / "tools")


@pytest.fixture
def rt(tmp_path, tools):
    value = runtime(tmp_path / "wf", tools)
    try:
        yield value
    finally:
        value.close()


def job(tier):
    return SimpleNamespace(ltx_model=tier)


def install(tools, tier):
    weights = tools.jasna_dir / "model_weights" / "ltx-restore"
    for fast in (True, False):
        (weights / MODEL_FILES[(tier, fast)]).write_bytes(b"weights")


def test_the_tier_lives_only_in_the_settings(rt):
    executor = rt.executors["restore"]
    base = executor.tools()
    high = executor.tools(job("high"))
    # U05: one approved profile matches every tier's claim
    assert (high.tool, high.model, high.model_version, high.profile) == (
        base.tool,
        "ltx",
        base.model_version,
        base.profile,
    )
    assert high.config_digest != base.config_digest
    assert executor.tools(job("standard")).config_digest == base.config_digest
    assert executor.tools(job(None)).config_digest == base.config_digest
    assert executor.tools(job("good")).config_digest not in (
        base.config_digest,
        high.config_digest,
    )


def test_the_planned_tier_is_launched_and_its_models_are_checked(rt, tools):
    film = add_film(rt, [File("ABF-394.mp4", video())], tier="high")
    run = run_direct(rt, film, "restore")
    assert run.outcome.message.startswith(MISSING_LTX)
    executor = rt.executors["restore"]
    weights, missing, variant = executor.missing_models()
    assert missing and all("distill8" in name for name in missing)  # high's model
    assert variant.startswith("ltx high")
    install(tools, "high")
    assert run_direct(rt, film, "restore").outcome.ok
    (argv,) = tools.runs(tools.jasna_exe)
    assert argv[argv.index("--restoration-model-name") + 1] == "ltx-8step"


def test_the_claim_records_the_planned_tier_and_the_approved_profile_matches(rt, tools):
    install(tools, "good")
    film = add_film(rt, [File("ABF-394.mp4", video())], tier="good")
    run_stages(rt, "restore")
    assert stage(rt, film, "restore").status == "succeeded"
    (attempt,) = attempts(rt, film, "restore")
    executor = rt.executors["restore"]
    assert attempt.tool_versions == executor.tools(job("good"))
    (argv,) = tools.runs(tools.jasna_exe)
    assert argv[argv.index("--restoration-model-name") + 1] == "ltx-4step"


def test_a_film_without_a_tier_restores_with_the_default(rt, tools):
    film = add_film(rt, [File("ABF-394.mp4", video())], tier=None)
    run_stages(rt, "restore")
    (attempt,) = attempts(rt, film, "restore")
    assert attempt.tool_versions == rt.executors["restore"].tools()
    (argv,) = tools.runs(tools.jasna_exe)
    assert argv[argv.index("--restoration-model-name") + 1] == "ltx"


@pytest.mark.parametrize(
    "names, tier, expected, fast",
    [
        ([MODEL_FILES[("high", True)]], None, "high", True),
        ([MODEL_FILES[("good", False)]], None, "good", False),
        (["vae.safetensors.enc"], None, "standard", None),
        # the film's own plan wins over the names (a pre-upgrade note)
        ([MODEL_FILES[("high", True)]], "good", "good", True),
    ],
)
def test_recall_infers_the_tier_from_the_names_or_takes_the_plan(
    rt, names, tier, expected, fast
):
    executor = rt.executors["restore"]
    executor._gpus = lambda: pytest.fail("GPU queried")
    executor.recall_missing(names, tier)
    ((key, (_, cfg, recalled)),) = executor._models.items()
    assert key == expected
    assert cfg.ltx_model == expected
    if fast is not None:
        assert recalled is fast


def _after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


@pytest.mark.parametrize("bitrate", [12_200_000, None])
def test_the_launch_params_are_the_argv_jasna_got_for_this_film(
    rt, tools, monkeypatch, bitrate
):
    """U1c: the restore step shows the effective launch of THIS film — read from
    the argv itself — with the probed source bitrate, or none when it failed."""
    monkeypatch.setattr(
        exec_restore, "probe_source_bitrate", lambda *args, **kwargs: bitrate
    )
    install(tools, "high")
    film = add_film(rt, [File("ABF-394.mp4", video())], tier="high")
    run = run_direct(rt, film, "restore")
    assert run.outcome.ok
    (argv,) = tools.runs(tools.jasna_exe)
    params = run.ltx[0]["params"]
    assert all(state["params"] == params for state in run.ltx)
    fast = "--ltx-fast" in argv
    assert params == {
        "ltx_model": "high",
        "model_name": _after(argv, "--restoration-model-name"),
        "fast": fast,
        "precision": "nvfp4" if fast else "int8",
        "auto_split": int(_after(argv, "--ltx-auto-split")),
        "large_canvas": "--ltx-large-canvas" in argv,
        "overlap": "auto",
        "codec": _after(argv, "--codec"),
        "source_codec": None,
        "cq": int(_after(argv, "--cq")),
        "match_source_bitrate": True,
        "maxrate_bps": bitrate,
        "source_bitrate_bps": bitrate,
        "detection_model": _after(argv, "--detection-model"),
        "clip_size": int(_after(argv, "--max-clip-size")),
        "temporal_overlap": int(_after(argv, "--temporal-overlap")),
        "tier": "1080p",
        "source_width": 1920,
        "source_height": 1080,
        "gpu": GPU_8GB.name,
        "gpu_vram_mib": GPU_8GB.vram_mib,
        "preset": "5060-8",
        "preset_label": "RTX 5060 · 8 GB",
        "preset_auto": False,
        "argv": params["argv"],
    }
    assert params["model_name"] == "ltx-8step"
    assert ("--encoder-settings" in argv) == (bitrate is not None)
    if bitrate is not None:
        assert _after(argv, "--encoder-settings").startswith(f"maxrate={bitrate},")
    # the copyable command: the same tokens, every path cut to its file name
    short = [Path(a).name if a == argv[0] else a for a in argv]
    for flag in ("--input", "--output", "--working-directory"):
        i = short.index(flag) + 1
        short[i] = Path(argv[i]).name
    assert params["argv"] == subprocess.list2cmdline(short)
    assert str(tools.jasna_dir) not in params["argv"]
    assert "--input ABF-394.mp4" in params["argv"]


# ── IR05: the params read the argv the way argparse does ───────────────────
def _launch(argv: list[str], probe=None, **kw):
    return exec_restore.launch_params(
        ["jasna.exe", "--input", "a.mkv", *argv],
        match_source_bitrate=False,
        source_bitrate=None,
        probe=probe,
        gpu=None,
        preset="5060-8",
        **kw,
    )


def _probe(codec: str | None):
    streams = (SimpleNamespace(index=0, codec_type="video", codec_name=codec),)
    return SimpleNamespace(
        width=1920,
        height=1080,
        raw_facts=SimpleNamespace(primary_video_index=0, streams=streams),
    )


def test_a_repeated_flag_takes_the_last_value_like_argparse():
    params = _launch(
        ["--cq", "20", "--codec", "hevc", "--cq", "30", "--codec", "av1"]
        + ["--experimental-ltx-less-overlap", "--no-experimental-ltx-less-overlap"]
        + ["--max-clip-size", "90", "--max-clip-size", "180"]
        + ["--ltx-fast", "--no-ltx-fast"]
    )
    assert (params["cq"], params["codec"]) == (30, "av1")
    assert params["clip_size"] == 180
    assert params["overlap"] == "off"
    assert params["fast"] is False and params["precision"] == "int8"
    again = _launch(
        ["--no-experimental-ltx-less-overlap", "--experimental-ltx-less-overlap"]
    )
    assert again["overlap"] == "on"


@pytest.mark.parametrize(
    ("split", "configured", "source", "source_codec"),
    [
        (
            "12",
            "hevc",
            "h264",
            "h264",
        ),  # a split would smart-render in the source codec
        ("12", "hevc", "av1", "av1"),
        ("12", "av1", "hevc", "hevc"),
        ("12", "hevc", "hevc", None),  # already the same
        ("0", "hevc", "h264", None),  # no auto split: it re-encodes
        ("12", "hevc", "mpeg4", None),  # not a codec Jasna keeps
        ("12", "hevc", None, None),  # unreadable source codec
    ],
)
def test_params_keep_the_configured_codec_and_name_the_source_codec_a_split_would_use(
    split, configured, source, source_codec
):
    """Jasna may still skip the split, so `codec` is always the configured one;
    the UI decides from the split note which of the two applies."""
    argv = ["--codec", configured, "--ltx-auto-split", split]
    params = _launch(argv, _probe(source))
    assert params["codec"] == configured
    assert params["source_codec"] == source_codec
    assert "codec_follows_source" not in params


def test_there_is_no_source_codec_without_a_probe():
    params = _launch(["--codec", "hevc", "--ltx-auto-split", "12"], None)
    assert (params["codec"], params["source_codec"]) == ("hevc", None)
