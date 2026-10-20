"""M01-P1 scrape: Miko post-processing output becomes the film's final (M2–M5, M9)."""

import hashlib
import json
import sys
from pathlib import Path

import pytest

from taskpaw_v3.core.config import WorkflowSettings
from taskpaw_v3.core.gpu_lease import GpuLease
from taskpaw_v3.tests import test_workflow_return_step as ret
from taskpaw_v3.tests.test_workflow_local_dispatch import Crash
from taskpaw_v3.tests.workflow_exec_helpers import fake, video
from taskpaw_v3.workflow.executors import failed_kind
from taskpaw_v3.workflow.local_dispatch import LocalDispatcher
from taskpaw_v3.workflow.pipeline import default_scrape_step
from taskpaw_v3.workflow.return_step import LocalReturnStep, package_dir
from taskpaw_v3.workflow.scrape_step import LocalScrapeStep

FAKE_MDCX = Path(__file__).with_name("workflow_fake_mdcx.py")


@pytest.fixture(autouse=True)
def probeable_video(monkeypatch):
    # the fake ffprobe reads a video's media facts from its JSON content
    monkeypatch.setattr(ret, "VIDEO", video(1920, 1080, 120.0))


@pytest.fixture
def rt(tmp_path):
    value = ret.make_runtime(tmp_path / "wf")
    try:
        yield value
    finally:
        value.close()


def scrape_step(tmp_path, mode="ok", definition="1080P"):
    profile = tmp_path / "miko" / "config.json"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(
        json.dumps({"fake_mode": mode, "fake_definition": definition}), encoding="utf-8"
    )
    (profile.parent / "userdata").mkdir(exist_ok=True)
    (profile.parent / "userdata" / "mapping.xml").write_text("<map/>", "utf-8")
    (profile.parent / "userdata" / "success.txt").write_text("volatile", "utf-8")
    return LocalScrapeStep(
        python=sys.executable,
        root=str(tmp_path),
        profile=str(profile),
        ffprobe=fake("ffprobe"),
        launch_prefix=[sys.executable, str(FAKE_MDCX)],
    )


def dispatcher(rt, step):
    return LocalDispatcher(
        rt,
        lease=GpuLease(),
        poll=0.05,
        return_step=LocalReturnStep(),
        scrape_step=step,
    )


def package_hashes(rt, film):
    root = package_dir(rt, film, 1)
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def final_of(rt, film):
    view = rt.store.get_film(rt.operator, film)
    return view.film.current_final_revision


def test_miko_output_becomes_the_published_final(rt, tmp_path):
    film = ret.add_real_film(rt)
    d = dispatcher(rt, scrape_step(tmp_path))
    for _ in range(4):
        assert d.run_once()
    before = package_hashes(rt, film)
    assert d.run_once() is True
    assert ret.status(rt, film, "scrape") == "succeeded", d.status().reason
    assert final_of(rt, film) == 1
    assert package_hashes(rt, film) == before  # M2: the return package is untouched
    out = next((rt.work_dir / "scrape").rglob("ABC-123.nfo")).parent
    assert sorted(p.name for p in out.iterdir()) == [
        "ABC-123.chs.srt",
        "ABC-123.ja.srt",
        "ABC-123.mkv",
        "ABC-123.nfo",
        "fanart.jpg",
        "poster.jpg",
        "thumb.jpg",
    ]
    assert not any((rt.work_dir / "scrape").rglob("in"))  # staging inputs dropped


@pytest.mark.parametrize(
    ("mode", "kind", "attempts"),
    [
        ("fail", failed_kind("transient"), 3),
        ("no_result", failed_kind("transient"), 3),
        ("extra_file", failed_kind("needs_attention"), 1),
        ("wrong_hash", failed_kind("needs_attention"), 1),
    ],
)
def test_failures_are_classified_and_never_publish(rt, tmp_path, mode, kind, attempts):
    film = ret.add_real_film(rt)
    d = dispatcher(rt, scrape_step(tmp_path, mode))
    ret.run_all(d)
    assert ret.status(rt, film, "scrape") == "failed"
    job = next(
        s for s in rt.store.get_film(rt.operator, film).stages if s.stage == "scrape"
    )
    history = rt.store.list_attempts(rt.operator, job.job_id)
    assert [a.evidence[0].kind for a in history] == [kind] * attempts
    assert final_of(rt, film) is None


def test_crash_after_a_candidate_final_reruns_with_the_next_revision(
    tmp_path, monkeypatch
):
    first = ret.make_runtime(tmp_path / "wf")
    film = ret.add_real_film(first)
    step = scrape_step(tmp_path)
    d = dispatcher(first, step)
    for _ in range(4):
        assert d.run_once()
    real = first.store.put_final_manifest

    def put_then_crash(*args, **kwargs):
        real(*args, **kwargs)
        raise Crash()

    monkeypatch.setattr(first.store, "put_final_manifest", put_then_crash)
    with pytest.raises(Crash):
        d.run_once()
    first.close()
    second = ret.make_runtime(tmp_path / "wf")
    try:
        assert ret.status(second, film, "scrape") == "pending"  # interrupted, requeued
        assert second.store.latest_final_revision(second.operator, film) == 1
        assert dispatcher(second, step).run_once() is True
        assert ret.status(second, film, "scrape") == "succeeded"
        assert final_of(second, film) == 2
        assert (
            len(list((second.work_dir / "scrape").glob("*/*"))) == 1
        )  # old attempt dir gone
    finally:
        second.close()


def test_profile_change_after_the_claim_is_retried(rt, tmp_path, monkeypatch):
    film = ret.add_real_film(rt)
    step = scrape_step(tmp_path)
    d = dispatcher(rt, step)
    for _ in range(4):
        assert d.run_once()
    original = step.tools

    def tools_then_edit():
        value = original()
        step.profile.write_text(json.dumps({"fake_mode": "ok", "x": 1}), "utf-8")
        return value

    monkeypatch.setattr(step, "tools", tools_then_edit)
    assert d.run_once() is True
    job = next(
        s for s in rt.store.get_film(rt.operator, film).stages if s.stage == "scrape"
    )
    (first,) = rt.store.list_attempts(rt.operator, job.job_id)[:1]
    assert first.evidence[0].kind == failed_kind("transient")


def test_scrape_is_off_until_miko_is_fully_configured(tmp_path):
    assert default_scrape_step(WorkflowSettings()) is None
    assert default_scrape_step(WorkflowSettings(mdcx_python="p", mdcx_root="r")) is None
    step = default_scrape_step(
        WorkflowSettings(mdcx_python="p", mdcx_root="r", mdcx_profile="c")
    )
    assert isinstance(step, LocalScrapeStep)


def test_volatile_userdata_does_not_change_the_config_digest(tmp_path):
    step = scrape_step(tmp_path)
    first = step.tools().config_digest
    (step.userdata / "success.txt").write_text("rewritten by interactive Miko", "utf-8")
    assert step.tools().config_digest == first
    (step.userdata / "mapping.xml").write_text("<map changed/>", "utf-8")
    assert step.tools().config_digest != first


def test_4k_is_claimed_only_when_miko_and_our_probe_agree(rt, tmp_path):
    film = ret.add_real_film(rt)  # the fake video probes as 1920x1080
    ret.run_all(dispatcher(rt, scrape_step(tmp_path, definition="4K")))
    assert ret.status(rt, film, "scrape") == "succeeded"
    revision = final_of(rt, film)
    marks = None
    for path in (rt.work_dir / "scrape").rglob("result.json"):
        marks = json.loads(path.read_text("utf-8"))["expected_marks"]
    assert marks and marks[0]["definition"] == "4K"  # Miko said 4K ...
    assert revision == 1  # ... yet the final was accepted without a 4k mark
