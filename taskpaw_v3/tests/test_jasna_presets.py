from pathlib import Path

import pytest

from taskpaw_v3.monitors.plugins.jasna import JasnaConfig, build_argv
from taskpaw_v3.monitors.plugins.jasna_presets import preset_catalogue


def test_frontend_catalogue_fixture_matches_backend():
    import json

    from taskpaw_v3.monitors.plugins.jasna import JasnaPlugin

    fixture = Path(__file__).parents[1] / "ui/src/test/fixtures/jasna-catalog.json"
    data = json.loads(fixture.read_text(encoding="utf-8"))
    assert data["json_schema"] == JasnaPlugin.json_schema()
    assert data["ui_schema"] == JasnaPlugin.ui_schema()


def test_every_preset_validates_and_preserves_mixed_tiers():
    catalog = preset_catalogue()
    assert len(catalog["profiles"]) == 15
    for profile in catalog["profiles"]:
        for variant in ("standard", "vr"):
            cfg = JasnaConfig(name="test", **profile[variant])
            assert cfg.clip_size_1080p > 2 * cfg.temporal_overlap
            assert cfg.clip_size_4k > 2 * cfg.temporal_overlap
            assert cfg.vr_8k_custom
        cfg = JasnaConfig(name="test", **profile["standard"])
        assert cfg.ltx_1080p == (profile["model"] == "ltx")
        assert cfg.ltx_4k == (profile["model"] == "ltx" and profile["gpu"] != "5060-8")
        assert cfg.ltx_fast == (profile["gpu"] != "4090-24")
        assert not cfg.ltx_large_canvas
        assert (
            not {"cq", "codec", "jasna_exe_path", "jasna_extra_args", "av_translate"}
            & profile["standard"].keys()
        )


@pytest.mark.parametrize(
    "overlap,flag",
    [
        ("auto", None),
        ("on", "--experimental-ltx-less-overlap"),
        ("off", "--no-experimental-ltx-less-overlap"),
    ],
)
def test_overlap_only_on_ltx_tier(overlap, flag):
    cfg = JasnaConfig(name="test", ltx_1080p=True, ltx_overlap=overlap)
    for tier in ("1080p", "4k"):
        argv = build_argv(
            cfg, "jasna", Path("in.mp4"), Path("out.mp4"), tier, False, False
        )
        options = [arg for arg in argv if "less-overlap" in arg]
        assert options == ([flag] if flag and tier == "1080p" else [])


@pytest.mark.parametrize(
    "flag",
    [
        "--experimental-ltx-less-overlap",
        "--no-experimental-ltx-less-overlap",
        "--experimental-ltx-less",
        "--no-experimental-ltx-less",
    ],
)
def test_overlap_extra_args_rejected(flag):
    with pytest.raises(ValueError, match="LTX options"):
        JasnaConfig(name="test", jasna_extra_args=flag)


def test_custom_vr_obeys_manual_values_but_keeps_sbs_detector():
    cfg = JasnaConfig(
        name="test", vr_8k=True, vr_8k_custom=True, clip_size_4k=60, temporal_overlap=12
    )
    argv = build_argv(cfg, "jasna", Path("in.mp4"), Path("out.mp4"), "4k", True, True)
    assert argv[argv.index("--max-clip-size") + 1] == "60"
    assert argv[argv.index("--temporal-overlap") + 1] == "12"
    assert argv[argv.index("--detection-model") + 1] == "rfdetr-vr-v1"
    assert "sbs" in argv


def test_new_ltx_stage_drops_old_rate_and_eta():
    from taskpaw_v3.monitors.plugins.jasna import parse_jasna_progress_line

    before = {"ltx_stage": "2/4 encode", "percent": 100, "fps": 12, "eta": "10:00"}
    after = parse_jasna_progress_line("LTX 3/4 denoise: 1%", before)
    assert after == {"ltx_stage": "3/4 denoise", "percent": 1}
