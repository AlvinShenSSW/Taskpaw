"""TranslateExecutor with an injected fake Translator (T04-P1 E3, E4)."""

import json
import threading
import time

import pytest

from taskpaw_v3.monitors.subs import srt
from taskpaw_v3.tests.workflow_exec_helpers import (
    JA_SRT,
    ZH_SRT,
    FakeTranslator,
    File,
    Tools,
    add_film,
    attempts,
    dispatcher,
    run_direct,
    runtime,
    stage,
    translate,
    video,
)
from taskpaw_v3.workflow.exec_translate import NO_MODEL
from taskpaw_v3.workflow.executors import SUCCEEDED_KIND, failed_kind
from taskpaw_v3.workflow.stage_results import check_file, load_stage_result

MOVIE = "movie-破解.mp4"


@pytest.fixture(autouse=True)
def fresh_translators():
    FakeTranslator.instances.clear()
    yield
    FakeTranslator.instances.clear()


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


def ready_film(rt, *subs, name=MOVIE):
    """Restore and asr done; translate is pending."""
    files = [File(name, video())]
    for path, text, language in subs:
        files.append(File(path, text.encode(), "source_subtitle", language, name))
    film = add_film(rt, files)
    d = dispatcher(rt)
    assert d.run_once() is True and d.run_once() is True
    assert stage(rt, film, "asr").status == "succeeded"
    return film


def translate_once(rt, film, mode="translated", **kw):
    rt.executors["translate"] = translate(rt, mode, **kw)
    assert dispatcher(rt).run_once() is True
    return stage(rt, film, "translate")


def only(rt, film, name):
    (value,) = load_stage_result(rt, film, name).videos
    return value.subtitle


@pytest.mark.parametrize(
    "subs, decision",
    [((("movie.ja.srt", JA_SRT, "ja"),), "source_ja"), ((), "generated_ja")],
)
def test_japanese_is_translated_into_the_restored_name(rt, tools, subs, decision):
    film = ready_film(rt, *subs)
    asr = only(rt, film, "asr")
    job = translate_once(rt, film)
    assert job.status == "succeeded"
    facts = only(rt, film, "translate")
    assert facts.decision == decision
    assert facts.ja == asr.ja  # the source stays referenced, digest-checked
    assert facts.zh.path == f"{job.attempt_id}/movie-破解.zh.srt"
    assert facts.kept_ja == 1
    cues = srt.load(check_file(rt.work_dir, facts.zh))
    assert [c.text for c in cues] == ["中文1", "中文2", "中文3"]
    (fake,) = FakeTranslator.instances
    assert fake.checkpoint_dir == rt.work_dir / "translate-checkpoints"
    assert [len(r.cues) for r in fake.requests] == [3]
    assert fake.started and fake.cancelled and fake.joined
    assert fake.discarded == [f"key-{fake.requests[0].job_id}"]


def test_full_pipeline_names_follow_the_restored_video(rt, tools):
    film = add_film(rt, [File("ABC-123.mkv", video())])
    d = dispatcher(rt)
    assert [d.run_once() for _ in range(3)] == [True, True, True]
    restored = load_stage_result(rt, film, "restore").videos[0].restore
    assert restored.file.path.endswith("ABC-123-无码.mp4")  # #303: LTX is 无码
    assert only(rt, film, "asr").ja.path.endswith("ABC-123-无码.ja.srt")
    assert only(rt, film, "translate").zh.path.endswith("ABC-123-无码.zh.srt")


@pytest.mark.parametrize(
    "subs, mode",
    [((("movie.zh.srt", ZH_SRT, "zh"),), None), ((), "empty")],
    ids=["reused_zh", "no_speech"],
)
def test_reused_chinese_and_no_speech_pass_through(rt, tools, subs, mode):
    if mode:
        tools.set_whisper(mode=mode)
    film = ready_film(rt, *subs)
    before = only(rt, film, "asr")
    job = translate_once(rt, film)
    assert job.status == "succeeded"
    assert only(rt, film, "translate") == before
    assert FakeTranslator.instances == []
    assert not list((rt.work_dir / job.attempt_id).glob("*.srt"))
    (attempt,) = attempts(rt, film, "translate")
    assert attempt.evidence[-1].summary.note == "无需翻译"
    assert attempt.evidence[-1].summary.item_count is None  # EPIC #387 C1


def test_success_records_the_translated_cue_count(rt):
    """EPIC #387 C1: the success evidence counts the cues written to `.zh.srt`."""
    film = ready_film(rt, ("movie.ja.srt", JA_SRT, "ja"))
    assert translate_once(rt, film).status == "succeeded"
    (attempt,) = attempts(rt, film, "translate")  # read back: still validates
    (evidence,) = attempt.evidence
    assert evidence.kind == SUCCEEDED_KIND
    assert evidence.summary.note == "翻译完成：1 卷"
    assert evidence.summary.item_count == 3


def test_the_cue_count_sums_every_translated_volume(rt, tools):
    files = [
        File("cd1-破解.mp4", video(), volume=1),
        File("cd2-破解.mp4", video(), volume=2),
        File("cd3-破解.mp4", video(), volume=3),
        File("cd1.ja.srt", JA_SRT.encode(), "source_subtitle", "ja", "cd1-破解.mp4"),
        File("cd3.zh.srt", ZH_SRT.encode(), "source_subtitle", "zh", "cd3-破解.mp4"),
    ]
    film = add_film(rt, files)
    d = dispatcher(rt)
    assert d.run_once() is True and d.run_once() is True
    assert translate_once(rt, film).status == "succeeded"
    videos = load_stage_result(rt, film, "translate").videos
    written = [
        len(srt.load(check_file(rt.work_dir, v.subtitle.zh)))
        for v in videos
        if v.subtitle.decision in ("source_ja", "generated_ja")
    ]
    assert len(written) == 2 and written[0] == 3 and written[1] > 0
    (attempt,) = attempts(rt, film, "translate")
    assert attempt.evidence[-1].summary.note == "翻译完成：2 卷"
    assert attempt.evidence[-1].summary.item_count == sum(written)


def test_no_llm_key_needs_attention(rt):
    film = ready_film(rt, ("movie.ja.srt", JA_SRT, "ja"))
    job = translate_once(rt, film, "no_key")
    assert job.status == "failed"
    (attempt,) = attempts(rt, film, "translate")
    assert attempt.evidence[0].kind == failed_kind("needs_attention")
    assert attempt.evidence[0].summary.note == NO_MODEL
    (fake,) = FakeTranslator.instances
    assert fake.cancelled and fake.joined


def test_an_empty_llm_chain_needs_attention_without_a_translator(rt):
    film = ready_film(rt, ("movie.ja.srt", JA_SRT, "ja"))
    job = translate_once(rt, film, chain_fn=lambda: ())
    assert job.status == "failed"
    (attempt,) = attempts(rt, film, "translate")
    assert attempt.evidence[0].kind == failed_kind("needs_attention")
    assert FakeTranslator.instances == []


@pytest.mark.parametrize("mode", ["failed", "paused"])
def test_failed_or_paused_translation_is_transient(rt, mode):
    film = ready_film(rt, ("movie.ja.srt", JA_SRT, "ja"))
    translate_once(rt, film, mode)
    first = attempts(rt, film, "translate")[0]
    assert first.evidence[0].kind == failed_kind("transient")
    assert f"{mode} detail" in first.evidence[0].summary.note


def test_tampered_asr_result_needs_attention(rt):
    film = ready_film(rt, ("movie.ja.srt", JA_SRT, "ja"))
    ledger = rt.work_dir / stage(rt, film, "asr").attempt_id / "stage-result.json"
    data = json.loads(ledger.read_bytes())
    data["videos"][0]["subtitle"]["decision"] = "reused_zh"
    ledger.write_text(json.dumps(data), encoding="utf-8")
    job = translate_once(rt, film)
    assert job.status == "failed"
    (attempt,) = attempts(rt, film, "translate")
    assert attempt.evidence[0].kind == failed_kind("needs_attention")
    assert "识别阶段" in attempt.evidence[0].summary.note
    assert FakeTranslator.instances == []


def test_changed_japanese_file_needs_attention(rt):
    film = ready_film(rt)  # WhisperJAV generated the Japanese file
    path = rt.work_dir / only(rt, film, "asr").ja.path
    path.write_text(JA_SRT + "\n", encoding="utf-8")
    job = translate_once(rt, film)
    assert job.status == "failed"
    (attempt,) = attempts(rt, film, "translate")
    assert attempt.evidence[0].kind == failed_kind("needs_attention")
    assert FakeTranslator.instances == []


def test_stop_cancels_and_joins_the_translator(rt):
    film = ready_film(rt, ("movie.ja.srt", JA_SRT, "ja"))
    executor = translate(rt, "hang")
    cancel = threading.Event()
    box = {}
    worker = threading.Thread(
        target=lambda: box.update(
            run=run_direct(rt, film, "translate", executor, cancel=cancel)
        )
    )
    worker.start()
    deadline = time.monotonic() + 20
    while not (
        FakeTranslator.instances and FakeTranslator.instances[0].submitted.is_set()
    ):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    cancel.set()
    worker.join(20)
    assert not worker.is_alive()
    assert box["run"].outcome.error_class == "cancelled"
    (fake,) = FakeTranslator.instances
    assert fake.cancelled and fake.joined


def test_tools_profile_is_stable(rt):
    tools = rt.executors["translate"].tools()
    assert (tools.profile, tools.profile_revision) == ("workflow-translate", 1)
    assert translate(rt).tools().config_digest == tools.config_digest
