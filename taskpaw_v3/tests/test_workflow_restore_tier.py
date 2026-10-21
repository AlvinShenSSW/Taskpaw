"""U1 P4': the planned LTX tier reaches the restore launch, its claim and its model offer."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from taskpaw_v3.monitors.plugins.jasna_download import MODEL_FILES
from taskpaw_v3.tests.workflow_exec_helpers import (
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
