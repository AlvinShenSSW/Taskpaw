"""#283: Jasna 0.11.3's four LTX quality tiers (`ltx_model`) and the migration
of the legacy `ltx_best` tickbox. Fakes only: no real Jasna, GPU or download."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_jasna import _events, _Launcher, _managed, _patch, _videos

from taskpaw_v3.agent.server.admin import MonitorAdmin
from taskpaw_v3.core.config import AgentConfig, load_yaml
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.monitors.plugins import jasna as J
from taskpaw_v3.monitors.plugins import jasna_download as D
from taskpaw_v3.monitors.plugins.jasna import (
    JasnaConfig,
    JasnaInstance,
    JasnaPlugin,
    build_argv,
)
from taskpaw_v3.monitors.plugins.jasna_presets import preset_catalogue
from taskpaw_v3.monitors.registry import default_registry
from taskpaw_v3.monitors.runtime import build_supervisor

_TIERS = ("standard", "good", "high", "top")
_CLI = {
    "standard": "ltx",
    "good": "ltx-4step",
    "high": "ltx-8step",
    "top": "ltx-undistilled",
}
# The design's table, verbatim (#283 C4).
_MODEL = {
    ("standard", False): (
        "ltx-restore-alpha1-distill2-int8.safetensors.enc",
        13368048749,
        "c23559f9d120df49266fc2dbe60ec384058d51834cf1a2da049e54b28aea51ad",
    ),
    ("standard", True): (
        "ltx-restore-alpha1-distill2-nvfp4.safetensors.enc",
        7733294997,
        "4aa08c146c8cc335e7f90d19aa293cc0dfe4f14520fd85efc257fe809510693b",
    ),
    ("good", False): (
        "ltx-restore-alpha1-distill4-int8.safetensors.enc",
        13368048733,
        "1c0f1642232813f690d949b05fe90bdc02d3203e0092950de25e9cc961f30965",
    ),
    ("good", True): (
        "ltx-restore-alpha1-distill4-nvfp4.safetensors.enc",
        7733294981,
        "57098e622d02598467f903049f4d1a613852f832847a01c43da148f506876973",
    ),
    ("high", False): (
        "ltx-restore-alpha1-distill8-int8.safetensors.enc",
        13368048773,
        "b006342a109c640a46ae91a5cd10e67186d968c63ef2f715c78bedf36936e057",
    ),
    ("high", True): (
        "ltx-restore-alpha1-distill8-nvfp4.safetensors.enc",
        7733295021,
        "54f2f4f6f7d3f5d60e18aca7bf6137da206a2b66f282e1a9078743c3173e62e2",
    ),
    ("top", False): (
        "ltx-restore-alpha1-int8.safetensors.enc",
        13368048821,
        "c72161b1afefe4d8cc0592fa57afef6c20466dcf912912f3628a451468780f83",
    ),
    ("top", True): (
        "ltx-restore-alpha1-nvfp4.safetensors.enc",
        7733295061,
        "8301d5ba17660b577ebe906d343963208f7e5453e589ef2f7470087a3ba2ccb5",
    ),
}
_SHARED = {
    "vae.safetensors.enc": (
        1472223815,
        "b9252d5a799748d1932d4eed60a2d77461949a19f6feb57f2238dbffe08f5e25",
    ),
    "vae-decoder.safetensors.enc": (
        38718609,
        "c60c3572871203bd364d6bb2390225fa4c347f7ea5c64fe468256c900caeea2d",
    ),
}


@pytest.fixture(autouse=True)
def _no_gpu(monkeypatch):
    monkeypatch.setattr(J, "query_gpus", lambda: None)  # never the real nvidia-smi


def _cfg(**kw) -> JasnaConfig:
    return JasnaConfig(**{"name": "jasna", **kw})


def _argv(cfg: JasnaConfig, fast_ok: bool = True) -> list[str]:
    return build_argv(
        cfg, "jasna.exe", Path("a.mp4"), Path("a.tmp.mp4"), "1080p", False, False,
        ltx_fast_ok=fast_ok,
    )  # fmt: skip


# ── C1 / C2: the setting and the legacy key ──────────────────────────────────
def test_ltx_model_defaults_to_standard_and_rejects_unknown_tiers():
    assert _cfg().ltx_model == "standard"
    for tier in _TIERS:
        assert _cfg(ltx_model=tier).ltx_model == tier
    with pytest.raises(ValueError):
        _cfg(ltx_model="best")
    assert "ltx_best" not in JasnaConfig.model_fields


@pytest.mark.parametrize(
    ("legacy", "expected"),
    [
        ({"ltx_best": True}, "top"),
        ({"ltx_best": False}, "standard"),
        ({}, "standard"),
        ({"ltx_best": True, "ltx_model": "standard"}, "standard"),
        ({"ltx_best": False, "ltx_model": "high"}, "high"),
    ],
)
def test_legacy_ltx_best_maps_and_an_explicit_ltx_model_wins(legacy, expected):
    cfg = _cfg(**legacy)
    assert cfg.ltx_model == expected
    assert "ltx_best" not in cfg.model_dump()


def test_migrate_config_never_mutates_the_stored_dict():
    stored = {"name": "j", "ltx_best": True}
    assert JasnaPlugin.migrate_config(stored) == {"name": "j", "ltx_model": "top"}
    assert stored == {"name": "j", "ltx_best": True}
    assert JasnaPlugin.migrate_config({"name": "j"}) == {"name": "j"}


def test_agent_boot_builds_a_legacy_yaml_spec(tmp_path):
    path = tmp_path / "agent.yaml"
    path.write_text(
        "server_id: s\nmachine: m\nhost_metrics: false\nmonitors:\n"
        "  - type_id: jasna\n    name: J\n"
        "    config: {name: J, ltx_best: true, ltx_1080p: true}\n",
        encoding="utf-8",
    )
    config = load_yaml(AgentConfig, path)
    assert isinstance(config, AgentConfig)
    sup = build_supervisor(default_registry(), config.monitors, EventQueue("m"), "m")
    try:
        cfg = sup._monitors["J"].instance.config
        assert isinstance(cfg, JasnaConfig) and cfg.ltx_model == "top"
    finally:
        sup.stop(timeout=1)


def _form(view_config: dict) -> dict:
    """What the RJSF form submits: every schema default, then the shown config."""
    props = JasnaPlugin.json_schema()["properties"]
    defaults = {k: p["default"] for k, p in props.items() if "default" in p}
    return {**defaults, **view_config}


def _editor(tmp_path) -> tuple[MonitorAdmin, Path]:
    path = tmp_path / "agent.yaml"
    config = AgentConfig(
        server_id="s",
        machine="m",
        host_metrics=False,
        monitors=[
            {
                "type_id": "jasna",
                "name": "J",
                "config": {"name": "J", "ltx_best": True, "ltx_1080p": True},
            }
        ],
    )
    return MonitorAdmin(config, None, default_registry(), path), path


def _shown(admin: MonitorAdmin) -> dict:
    (spec,) = admin.config_view()["monitors"]
    return spec["config"]


def test_editor_shows_a_never_resaved_ltx_best_config_as_top(tmp_path):
    admin, _path = _editor(tmp_path)
    shown = _shown(admin)
    assert shown["ltx_model"] == "top" and "ltx_best" not in shown
    # the view is derived: the stored spec keeps its legacy key until a save
    assert admin._config.monitors[0]["config"]["ltx_best"] is True


def test_editor_untouched_save_keeps_top(tmp_path):
    admin, path = _editor(tmp_path)
    admin.update("J", _form(_shown(admin)))
    stored = load_yaml(AgentConfig, path).monitors[0]["config"]
    assert stored["ltx_model"] == "top" and "ltx_best" not in stored
    assert _shown(admin)["ltx_model"] == "top"


def test_editor_explicit_standard_saves_standard(tmp_path):
    admin, path = _editor(tmp_path)
    admin.update("J", {**_form(_shown(admin)), "ltx_model": "standard"})
    stored = load_yaml(AgentConfig, path).monitors[0]["config"]
    assert stored["ltx_model"] == "standard" and "ltx_best" not in stored


# ── C3: every caller follows the setting ─────────────────────────────────────
@pytest.mark.parametrize("tier", _TIERS)
@pytest.mark.parametrize("fast", [False, True])
def test_build_argv_passes_the_tier_model_name(tier, fast):
    argv = _argv(_cfg(ltx_1080p=True, ltx_model=tier, ltx_fast=fast))
    assert argv[argv.index("--restoration-model-name") + 1] == _CLI[tier]
    assert ("--ltx-fast" in argv) == fast
    # a pre-Blackwell box drops fast whatever the tier
    assert "--ltx-fast" not in _argv(_cfg(ltx_1080p=True, ltx_model=tier), False)


@pytest.mark.parametrize("tier", _TIERS)
def test_basicvsrpp_launch_ignores_the_tier(tier):
    assert _argv(_cfg(ltx_model=tier)) == _argv(_cfg())


@pytest.mark.parametrize(("tier", "fast"), list(_MODEL))
def test_ltx_missing_files_per_model_and_fast(tmp_path, tier, fast):
    names = [_MODEL[tier, fast][0], *_SHARED]
    assert J.ltx_missing_files(str(tmp_path), tier, fast) == names
    assert J.ltx_missing_files(None, tier, fast) == names
    folder = tmp_path / "model_weights" / "ltx-restore"
    folder.mkdir(parents=True)
    for name in names:
        (folder / name).write_bytes(b"x")
    assert J.ltx_missing_files(str(tmp_path), tier, fast) == []


@pytest.mark.parametrize("tier", _TIERS)
def test_mode_and_label_name_the_tier(tier):
    cfg = _cfg(ltx_1080p=True, ltx_model=tier, ltx_auto_split=0)
    assert J._ltx_mode(cfg, True, False) == f"ltx {tier} fast"
    assert J._ltx_mode(cfg, False, False) == f"ltx {tier}"
    assert J._ltx_label(cfg, True, False) == [f"LTX {tier} fast"]
    assert J._ltx_label(cfg, False, True) == [f"LTX {tier}", "large canvas"]


def test_missing_good_model_requests_its_own_files(tmp_path, monkeypatch):
    cfg, inp, _out, home = _managed(
        tmp_path, ltx_1080p=True, ltx_model="good", ltx_fast=False
    )
    folder = home / "model_weights" / "ltx-restore"
    folder.mkdir(parents=True)
    for name in (_MODEL["standard", False][0], *_SHARED):  # another tier's model
        (folder / name).write_bytes(b"x")
    _videos(inp, "a.mp4")
    launcher = _Launcher([None])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)
    try:
        for _ in range(3):
            status = inst.check(emit)
        assert launcher.n == 0
        prompt = status.metrics["ltx_download"]
        assert prompt["files"] == [_MODEL["good", False][0]]
        assert prompt["variant"] == "ltx good auto-split 12%"
        assert prompt["total_bytes"] == _MODEL["good", False][1]
    finally:
        inst.stop(timeout=0.5)


def test_presets_build_valid_standard_configs():
    for profile in preset_catalogue()["profiles"]:
        for variant in ("standard", "vr"):
            assert "ltx_best" not in profile[variant]
            cfg = JasnaConfig(name="test", **profile[variant])
            assert cfg.ltx_model == "standard"


def test_ui_order_and_titled_select():
    order = JasnaPlugin.ui_schema()["ui:order"]
    assert "ltx_best" not in order
    assert order[order.index("ltx_auto_split") + 1] == "ltx_model"
    prop = JasnaPlugin.json_schema()["properties"]["ltx_model"]
    assert prop["default"] == "standard" and "enum" not in prop
    assert prop["oneOf"] == [
        {"const": "standard", "title": "Standard (default, fastest)"},
        {"const": "good", "title": "Good (~1.5x slower)"},
        {"const": "high", "title": "High (~2.5x slower; the pre-0.11.3 default)"},
        {"const": "top", "title": 'Top (~5x slower; formerly "best")'},
    ]
    assert "Jasna 0.11.3 or newer" in prop["description"]
    assert (
        "0.11.3 or newer"
        in JasnaPlugin.json_schema()["properties"]["ltx_1080p"]["description"]
    )


# ── C4: the downloader manifest ──────────────────────────────────────────────
def test_download_manifest_equals_the_design_table():
    assert D.REVISION == "a6e596656f9374dd85536a9313c9a5511cbdd407"
    assert {k: v[0] for k, v in _MODEL.items()} == D.MODEL_FILES
    assert D.SHARED_FILES == tuple(_SHARED)
    assert D.FILES == {
        **{name: (size, sha) for name, size, sha in _MODEL.values()},
        **_SHARED,
    }


def test_download_url_uses_the_new_revision(tmp_path, monkeypatch):
    urls: list[str] = []

    def opening(url, **kw):
        urls.append(url)
        raise OSError("offline")

    monkeypatch.setattr(D.urllib.request, "urlopen", opening)
    name = _MODEL["standard", True][0]
    with pytest.raises(OSError):
        D.download(tmp_path, [name], lambda m: None)
    assert urls[0] == (
        "https://huggingface.co/Kruk2/ltx-restore/resolve/"
        f"a6e596656f9374dd85536a9313c9a5511cbdd407/{name}"
    )
