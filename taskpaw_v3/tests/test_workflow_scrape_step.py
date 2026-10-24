"""M01-P1 scrape: Miko post-processing output becomes the film's final (M2–M5, M9)."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from taskpaw_v3.core.config import WorkflowSettings
from taskpaw_v3.core.gpu_lease import GpuLease
from taskpaw_v3.tests import test_workflow_return_step as ret
from taskpaw_v3.tests.test_workflow_local_dispatch import Crash
from taskpaw_v3.tests.workflow_exec_helpers import fake, video
from taskpaw_v3.workflow import file_identity
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


def test_miko_output_becomes_the_published_final(rt, tmp_path, monkeypatch):
    film = ret.add_real_film(rt)
    published = []
    real_put = rt.store.put_final_manifest

    def spy(auth, meta, command):
        published.append(command.manifest)
        return real_put(auth, meta, command)

    monkeypatch.setattr(rt.store, "put_final_manifest", spy)
    d = dispatcher(rt, scrape_step(tmp_path))
    for _ in range(4):
        assert d.run_once()
    before = package_hashes(rt, film)
    assert d.run_once() is True
    assert ret.status(rt, film, "scrape") == "succeeded", d.status().reason
    assert final_of(rt, film) == 1
    assert package_hashes(rt, film) == before  # M2: the return package is untouched
    out = rt.output_dir / "ABC-123"
    # #303: a workflow restore is LTX -> 无码 names, images named after the film, the
    # Chinese subtitle only (the .ja.srt never reaches the scrape or the final)
    assert sorted(p.name for p in out.iterdir()) == [
        "ABC-123-无码-C-fanart.jpg",
        "ABC-123-无码-C-poster.jpg",
        "ABC-123-无码-C-thumb.jpg",
        "ABC-123-无码-C.mkv",
        "ABC-123-无码-C.nfo",
        "ABC-123-无码-C.zh.srt",
    ]
    assert not any((rt.work_dir / "scrape").rglob("in"))  # staging inputs dropped
    # D08: every final item carries the identity the C6 delete will check
    (final,) = published
    for item in final.items:
        assert item.native_identity is not None
        assert item.native_identity.root_id == item.source_root_id
        observed = file_identity.native(out / item.relative_path, item.source_root_id)
        assert (observed.scheme, observed.volume_or_export_id, observed.object_id) == (
            item.native_identity.scheme,
            item.native_identity.volume_or_export_id,
            item.native_identity.object_id,
        )


@pytest.mark.parametrize(
    ("mode", "kind", "attempts"),
    [
        ("fail", failed_kind("transient"), 3),
        ("no_result", failed_kind("transient"), 3),
        ("extra_file", failed_kind("needs_attention"), 1),
        ("wrong_mosaic", failed_kind("needs_attention"), 1),
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


def test_an_unconfigured_miko_is_a_visible_waiting_reason(rt):
    """M01-IR02 / design §4.4: scrape off is shown, not silently pending."""
    from taskpaw_v3.workflow.pipeline import MIKO_OFF

    film = ret.add_real_film(rt)
    d = LocalDispatcher(
        rt,
        lease=GpuLease(),
        poll=0.05,
        return_step=LocalReturnStep(),
        off_reasons={"scrape": MIKO_OFF},
    )
    ret.run_all(d)
    assert ret.status(rt, film, "return") == "succeeded"
    assert ret.status(rt, film, "scrape") == "pending"
    assert d.status().waiting == {film: MIKO_OFF}


def test_shipped_host_shows_why_scrape_is_off(tmp_path, monkeypatch):
    from taskpaw_v3.workflow.local_host import LocalWorkflowHost, start_local_workflow
    from taskpaw_v3.workflow.pipeline import MIKO_OFF

    monkeypatch.setattr(LocalWorkflowHost, "start", lambda self: None)
    host = start_local_workflow(
        WorkflowSettings(enabled=True, inbox_root="x"), tmp_path
    )
    assert host.scrape_step is None and host.off_reasons == {"scrape": MIKO_OFF}


def test_a_changed_return_package_file_stops_the_scrape(rt, tmp_path):
    """Codex CX-T1: facts carried forward are only true of the receipted bytes."""
    film = ret.add_real_film(rt)
    d = dispatcher(rt, scrape_step(tmp_path))
    for _ in range(4):
        assert d.run_once()
    package = package_dir(rt, film, 1)
    (zh,) = package.glob("*.zh.srt")
    zh.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n\n", encoding="utf-8")
    assert d.run_once() is True
    assert ret.status(rt, film, "scrape") == "failed"
    job = next(
        s for s in rt.store.get_film(rt.operator, film).stages if s.stage == "scrape"
    )
    (attempt,) = rt.store.list_attempts(rt.operator, job.job_id)
    assert attempt.evidence[0].kind == failed_kind("needs_attention")
    assert final_of(rt, film) is None


def scrape_once(rt, tmp_path, monkeypatch):
    """Run the scrape of the film in `rt`; (published final, the Miko request)."""
    published = []
    real_put = rt.store.put_final_manifest

    def spy(auth, meta, command):
        published.append(command.manifest)
        return real_put(auth, meta, command)

    monkeypatch.setattr(rt.store, "put_final_manifest", spy)
    ret.run_all(dispatcher(rt, scrape_step(tmp_path)))
    request = None
    for path in (rt.work_dir / "scrape").rglob("request.json"):
        request = json.loads(path.read_text("utf-8"))
    return (published[0] if published else None), request


@pytest.mark.parametrize(
    ("decision", "name", "ltx"),
    [
        ("applied", "movie-无码.mkv", True),  # this workflow's own LTX restore
        ("applied", "movie-无码.mp4", True),
        ("reused", "movie-无码.mp4", True),  # an earlier LTX result kept
        ("reused", "movie-破解.mp4", False),  # an INBOX source already 破解
        ("reused", "movie_restored.mp4", False),
    ],
)
def test_the_request_says_whether_a_video_was_restored_with_ltx(
    rt, tmp_path, monkeypatch, decision, name, ltx
):
    rt.executors["restore"].decision, rt.executors["restore"].name = decision, name
    film = ret.add_real_film(rt)
    final, request = scrape_once(rt, tmp_path, monkeypatch)
    assert ret.status(rt, film, "scrape") == "succeeded"
    (entry,) = request["videos"]
    assert entry["restored"] is True and entry["ltx"] is ltx
    marks = set(final.postprocess.actual_marks)
    # Miko stamps 无码 on an LTX film and 破解 on any other restored one (M7)
    assert "chinese_subtitles" in marks
    assert ("uncensored" in marks, "restored" in marks) == (ltx, not ltx)
    expected = "无码" if ltx else "破解"
    assert any(expected in i.relative_path for i in final.items if i.role == "nfo")


def test_only_the_chinese_subtitle_is_staged_and_kept(rt, tmp_path, monkeypatch):
    film = ret.add_real_film(rt)  # a Japanese source subtitle, translated to zh
    final, _ = scrape_once(rt, tmp_path, monkeypatch)
    assert ret.status(rt, film, "scrape") == "succeeded"
    subtitles = [i for i in final.items if i.language is not None]
    assert [i.language for i in subtitles] == ["zh"]
    assert not any(i.relative_path.endswith(".ja.srt") for i in final.items)
    assert [p.name for p in (rt.work_dir / "scrape").rglob("*.ja.srt")] == []


def test_the_poster_is_the_first_volumes_named_image(rt, tmp_path, monkeypatch):
    film = ret.add_real_film(rt)
    final, _ = scrape_once(rt, tmp_path, monkeypatch)
    assert ret.status(rt, film, "scrape") == "succeeded"
    poster_id = final.postprocess.poster_item_id
    poster = next(i for i in final.items if i.item_id == poster_id)
    assert poster.role == "poster"
    assert poster.relative_path == "ABC-123-无码-C-poster.jpg"


def test_the_fake_miko_lays_out_each_volume_and_a_folder_cover(tmp_path):
    """The fake follows MDCX304 #299: per-volume files, folder.jpg of cd1's poster."""
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    config = tmp_path / "config"
    config.mkdir()
    (config / "user-config.json").write_text("{}", encoding="utf-8")
    videos = []
    for volume in (1, 2):
        name = f"ABC-123-无码-cd{volume}.mp4"
        (inp / name).write_bytes(b"video")
        (inp / f"ABC-123-无码-cd{volume}.srt").write_bytes(b"zh")
        videos.append(
            {
                "file": name,
                "volume": volume,
                "restored": True,
                "ltx": True,
                "zh_subtitle": True,
            }
        )
    request = tmp_path / "request.json"
    request.write_text(
        json.dumps(
            {
                "schema": 1,
                "staging": {"in": str(inp), "out": str(out), "failed": str(tmp_path)},
                "videos": videos,
            }
        ),
        encoding="utf-8",
    )
    result = tmp_path / "result.json"
    env = {**os.environ, "MDCX_CONFIG_DIR": str(config)}
    command = [sys.executable, str(FAKE_MDCX)]
    command += ["--request", str(request), "--result", str(result)]
    subprocess.run(command, check=True, env=env)
    film = out / "Actor" / "ABC-123"
    expected = {"folder.jpg"}
    for volume in (1, 2):
        stem = f"ABC-123-无码-C-cd{volume}"
        expected |= {
            f"{stem}.mp4",
            f"{stem}.nfo",
            f"{stem}.zh.srt",
            f"{stem}-poster.jpg",
            f"{stem}-fanart.jpg",
            f"{stem}-thumb.jpg",
        }
    assert {p.name for p in film.iterdir()} == expected
    first = film / "ABC-123-无码-C-cd1-poster.jpg"
    assert (film / "folder.jpg").read_bytes() == first.read_bytes()
    marks = json.loads(result.read_text("utf-8"))["expected_marks"]
    assert [x["mosaic"] for x in marks] == ["无码", "无码"]
