"""#187 Jasna: `<stem>-破解.mp4` naming (legacy `<stem>_restored.mp4` still counts
as restored and keeps its own subtitle names) and the `.ja.srt` cleanup once
the Chinese `.srt` is published.

Runs on the `test_jasna` / `test_jasna_subs` fakes (patched `subprocess.Popen`,
`J.ChildProcess`, `J.Translator`): nothing real is executed.
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path

import pytest
from test_jasna import _events, _Launcher, _managed, _patch, _videos
from test_jasna_subs import _done, _ja, _setup, _zh

from taskpaw_v3.monitors.plugins import jasna as J
from taskpaw_v3.monitors.plugins.jasna import (
    JasnaConfig,
    JasnaInstance,
    legacy_output_path_for,
    output_path_for,
    plan_queue,
    plan_subs,
    restored_output_for,
    staging_path_for,
    sweep_orphan_staging,
)
from taskpaw_v3.monitors.subs.job import SubsJob

CJK = "SDAB-312 無修正.mp4"
_OLD = time.time() - 600


def _age(*paths: Path) -> None:
    for p in paths:
        os.utime(p, (_OLD, _OLD))


def _files(folder: Path) -> list[str]:
    return sorted(p.name for p in folder.iterdir() if p.is_file())


# ── naming (pure) ─────────────────────────────────────────────────────────
def test_new_output_staging_and_subtitle_names(tmp_path):
    out = str(tmp_path / "out")
    v = tmp_path / "in" / "SDAB-312.mp4"
    assert output_path_for(out, v).name == "SDAB-312-破解.mp4"
    assert staging_path_for(out, v).name == "SDAB-312-破解.tmp.mp4"
    assert legacy_output_path_for(out, v).name == "SDAB-312_restored.mp4"
    new = output_path_for(out, v)
    assert J.zh_target_for(new).name == "SDAB-312-破解.srt"
    assert J.ja_target_for(new).name == "SDAB-312-破解.ja.srt"
    old = legacy_output_path_for(out, v)
    assert J.zh_target_for(old).name == "SDAB-312_restored.srt"
    assert J.ja_target_for(old).name == "SDAB-312_restored.ja.srt"
    dotted = output_path_for(out, tmp_path / "Clip.01.mkv")
    assert dotted.name == "Clip.01-破解.mp4"
    assert J.zh_target_for(dotted).name == "Clip.01-破解.srt"
    assert J.zh_target_for(dotted).parent == dotted.parent


def test_restored_output_for_prefers_the_new_name_then_the_legacy_one(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    v = tmp_path / "in" / "a.mp4"
    assert restored_output_for(str(out), v) is None
    (out / "a-破解.tmp.mp4").write_bytes(b"partial")  # staging is never "done"
    (out / "a_restored.tmp.mp4").write_bytes(b"partial")
    assert restored_output_for(str(out), v) is None
    (out / "a_restored.mp4").write_bytes(b"legacy")
    assert restored_output_for(str(out), v) == out / "a_restored.mp4"
    (out / "a-破解.mp4").write_bytes(b"new")
    assert restored_output_for(str(out), v) == out / "a-破解.mp4"  # new wins
    (out / "a_restored.mp4").unlink()
    assert restored_output_for(str(out), v) == out / "a-破解.mp4"


def test_plan_queue_counts_new_and_legacy_outputs_as_done_once(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    _videos(inp, "a.mp4", "b.mp4", "c.mp4", "d.mp4")
    (out / "a-破解.mp4").write_bytes(b"new")
    (out / "b_restored.mp4").write_bytes(b"legacy")
    (out / "c-破解.mp4").write_bytes(b"new")
    (out / "c_restored.mp4").write_bytes(b"legacy")  # both: still ONE done file
    (out / "d-破解.tmp.mp4").write_bytes(b"partial")
    (out / "d_restored.tmp.mp4").write_bytes(b"partial")
    pending, done, collisions = plan_queue(str(inp), str(out))
    assert done == 3 and collisions == []
    assert [p.name for p in pending] == ["d.mp4"]


def test_collisions_keep_their_semantics_with_the_new_name(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    _videos(inp, "片.mp4", "片.mkv", "b.mp4")
    (out / "b_restored.mp4").write_bytes(b"legacy")
    pending, done, collisions = plan_queue(str(inp), str(out))
    assert [p.name for p in pending] == ["片.mkv"]  # sorted: .mkv first
    assert done == 1
    assert [(a.name, b.name) for a, b in collisions] == [("片.mp4", "片.mkv")]


# ── legacy outputs are never relaunched or renamed ─────────────────────────
def test_a_legacy_output_is_never_relaunched_and_never_renamed(tmp_path, monkeypatch):
    cfg, inp, out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4", "b.mp4")
    (out / "a_restored.mp4").write_bytes(b"legacy")
    launcher = _Launcher([0])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()
    inst.start(emit)
    assert launcher.inputs() == ["b.mp4"]
    assert launcher.arg(0, "--output") == str(out / "b-破解.tmp.mp4")
    inst.check(emit)
    assert _files(out) == ["a_restored.mp4", "b-破解.mp4"]
    assert (out / "a_restored.mp4").read_bytes() == b"legacy"
    done = [e for e in evs if e[0] == "done"]
    assert len(done) == 1 and "Queue: 2/2 done, 0 failed" in done[0][2]

    again = JasnaInstance("j1", cfg)
    evs2, emit2 = _events()
    again.start(emit2)
    st = again.check(emit2)
    assert st.state == "idle"
    assert st.detail == "nothing to process (2 already restored)"
    assert launcher.n == 1 and evs2 == []


def test_cjk_name_restores_to_the_po_jie_name(tmp_path, monkeypatch):
    cfg, inp, out, _home = _managed(tmp_path)
    _videos(inp, CJK)
    launcher = _Launcher([0])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()
    inst.start(emit)
    assert launcher.arg(0, "--output") == str(out / "SDAB-312 無修正-破解.tmp.mp4")
    inst.check(emit)
    assert _files(out) == ["SDAB-312 無修正-破解.mp4"]
    assert len([e for e in evs if e[0] == "done"]) == 1


# ── start-time sweeps cover both names ────────────────────────────────────
def test_sweep_orphan_staging_covers_the_new_and_the_legacy_staging_name(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    _videos(inp, "keep.mp4")
    gone_new = out / "gone-破解.tmp.mp4"
    gone_old = out / "gone_restored.tmp.mp4"
    keep_new = out / "keep-破解.tmp.mp4"
    keep_old = out / "keep_restored.tmp.mp4"
    fresh_new = out / "other-破解.tmp.mp4"
    fresh_old = out / "other_restored.tmp.mp4"
    finals = [out / "gone-破解.mp4", out / "gone_restored.mp4"]
    for p in (gone_new, gone_old, keep_new, keep_old, fresh_new, fresh_old, *finals):
        p.write_bytes(b"x")
    _age(gone_new, gone_old, keep_new, keep_old, *finals)
    removed = sweep_orphan_staging(str(inp), str(out))
    assert sorted(p.name for p in removed) == sorted([gone_new.name, gone_old.name])
    # a source still in the input folder keeps both of its staging names
    assert keep_new.exists() and keep_old.exists()
    assert fresh_new.exists() and fresh_old.exists()  # too recent
    assert all(p.exists() for p in finals)  # a published output is never swept


def test_launching_a_file_clears_its_legacy_staging_leftover(tmp_path, monkeypatch):
    # A 3.5.1 run killed mid-file leaves `<stem>_restored.tmp.mp4`; nothing
    # writes that name any more, so relaunching the file removes it.
    cfg, inp, out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    leftover = out / "a_restored.tmp.mp4"
    leftover.write_bytes(b"partial")
    _age(leftover)
    launcher = _Launcher([None])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)
    assert launcher.n == 1  # a legacy staging file is not "done"
    assert not leftover.exists()
    assert (out / "a-破解.tmp.mp4").exists()
    inst.stop(timeout=0.5)


def test_start_sweeps_both_subtitle_temp_patterns_age_gated(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch)
    old = [
        r.out / "b-破解.ja.srt.3.tmp",
        r.out / "c-破解.srt.5.tmp",
        r.out / "d_restored.srt.4.tmp",
        r.out / "e_restored.ja.srt.6.tmp",
    ]
    fresh = [r.out / "a-破解.srt.7.tmp", r.out / "f_restored.ja.srt.8.tmp"]
    other = r.out / "notes.srt.1.tmp"  # not a Jasna subtitle temp
    for p in (*old, *fresh, other):
        p.write_text("x", encoding="utf-8")
    stamp = time.time() - 11 * 60
    for p in (*old, other):
        os.utime(p, (stamp, stamp))
    r.inst.start(r.emit)
    assert [p.name for p in old if p.exists()] == []
    assert all(p.exists() for p in fresh)  # may belong to a task publishing
    assert other.exists()
    r.inst.stop(timeout=1)


# ── subtitles follow the video they sit next to ───────────────────────────
def test_cjk_name_end_to_end_gets_po_jie_subtitles_and_no_ja(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, pending=[CJK])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    inst.check(emit)  # restored → ASR on the restored file
    media = r.out / "SDAB-312 無修正-破解.mp4"
    assert media.exists()
    assert r.spawner.argvs[0][1] == str(media)  # C10: that same file
    r.spawner.last.finish(0)
    st = inst.check(emit)
    ja = r.out / "SDAB-312 無修正-破解.ja.srt"
    assert ja.exists()  # the checkpoint while translating
    assert st.metrics["subs_translating"] == 1
    r.translators[0].answer(CJK)
    inst.check(emit)
    zh = r.out / "SDAB-312 無修正-破解.srt"
    assert zh.read_text(encoding="utf-8").count("好") == 2
    assert _files(r.out) == [media.name, zh.name]  # no .ja.srt left
    assert len(_done(r.evs)) == 1


def test_a_legacy_output_gets_restored_named_subtitles_from_that_file(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, legacy=["e.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    legacy = r.out / "e_restored.mp4"
    assert r.launcher.n == 0  # never restored again
    job = inst._jobs["e.mp4"]
    assert job.media == legacy
    assert job.ja_target == r.out / "e_restored.ja.srt"
    assert job.zh_target == r.out / "e_restored.srt"
    assert r.spawner.argvs[0][1] == str(legacy)  # WhisperJAV transcribes it
    st = inst.check(emit)
    assert st.metrics["current_file"] == "e_restored.mp4"
    r.spawner.last.finish(0)
    inst.check(emit)
    assert _ja(r, "e.mp4", legacy=True).exists()
    r.translators[0].answer("e.mp4")
    inst.check(emit)
    assert _files(r.out) == ["e_restored.mp4", "e_restored.srt"]
    assert legacy.read_bytes() == b"legacy e.mp4"  # never renamed
    assert len(_done(r.evs)) == 1


def test_a_legacy_output_with_its_zh_needs_nothing(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, legacy=["e.mp4"], zh=["e.mp4"])
    assert _zh(r, "e.mp4", legacy=True).exists()
    r.inst.start(r.emit)
    st = r.inst.check(r.emit)
    assert st.state == "idle"
    assert st.detail == "nothing to process (1 already restored)"
    assert r.spawner.argvs == [] and r.launcher.n == 0 and r.evs == []


def test_a_legacy_output_with_a_ja_is_translated_only(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, legacy=["e.mp4"], ja=["e.mp4"])
    assert _ja(r, "e.mp4", legacy=True).exists()
    r.inst.start(r.emit)
    assert r.spawner.argvs == []
    r.translators[0].answer("e.mp4")
    r.inst.check(r.emit)
    assert _files(r.out) == ["e_restored.mp4", "e_restored.srt"]


def test_plan_subs_uses_the_legacy_file_and_prefers_the_new_one(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    _videos(inp, "a.mp4", "b.mp4", "c.mp4")
    (out / "a_restored.mp4").write_bytes(b"legacy")  # legacy only, no subs
    (out / "b_restored.mp4").write_bytes(b"legacy")
    (out / "b_restored.srt").write_text("", encoding="utf-8")  # legacy done
    (out / "c_restored.mp4").write_bytes(b"legacy")
    (out / "c_restored.srt").write_text("", encoding="utf-8")
    (out / "c-破解.mp4").write_bytes(b"new")  # new wins: needs its own zh
    pending, _done_n, collisions = plan_queue(str(inp), str(out))
    assert pending == []
    plan = plan_subs(str(inp), str(out), pending, [a for a, _ in collisions])
    assert [p.name for p in plan.subs_only] == ["a.mp4", "c.mp4"]
    assert J.subs_kind(str(out), inp / "b.mp4") == "none"


def test_both_new_and_legacy_outputs_subtitle_the_new_file(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, restored=["e.mp4"], legacy=["e.mp4"])
    r.inst.start(r.emit)
    job = r.inst._jobs["e.mp4"]
    assert job.media == r.out / "e-破解.mp4"
    assert job.zh_target == r.out / "e-破解.srt"
    assert r.spawner.argvs[0][1] == str(r.out / "e-破解.mp4")
    assert r.launcher.n == 0
    r.inst.stop(timeout=1)


# ── .ja.srt cleanup ───────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "case", ["translated", "no_speech", "zero_cue_resume", "translate_only_resume"]
)
def test_ja_is_deleted_once_the_zh_is_published(tmp_path, monkeypatch, case):
    if case in ("translated", "no_speech"):
        r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    else:
        r = _setup(tmp_path, monkeypatch, restored=["a.mp4"], ja=["a.mp4"])
        if case == "zero_cue_resume":
            _ja(r, "a.mp4").write_bytes(b"")
    inst, emit = r.inst, r.emit
    inst.start(emit)
    if case == "translated":
        inst.check(emit)  # restored → ASR
        r.spawner.last.finish(0)
        inst.check(emit)
        assert _ja(r, "a.mp4").exists()
        r.translators[0].answer("a.mp4")
    elif case == "no_speech":
        inst.check(emit)
        r.spawner.last.finish(0, state="empty", text="")
    elif case == "translate_only_resume":
        assert _ja(r, "a.mp4").exists()
        r.translators[0].answer("a.mp4")
    inst.check(emit)
    assert inst._settled["a.mp4"][0] == "completed"
    assert _zh(r, "a.mp4").exists()
    assert not _ja(r, "a.mp4").exists()
    assert (r.out / "a-破解.mp4").exists()
    assert len(_done(r.evs)) == 1


@pytest.mark.parametrize(
    "case", ["failed", "zh_publish_failed", "no_key", "cancelled", "stop"]
)
def test_ja_is_kept_as_the_resume_checkpoint(tmp_path, monkeypatch, case):
    if case == "stop":
        r = _setup(tmp_path, monkeypatch, restored=["a.mp4"])
    else:
        r = _setup(
            tmp_path,
            monkeypatch,
            restored=["a.mp4"],
            ja=["a.mp4"],
            key=case != "no_key",
        )
    if case == "zh_publish_failed":
        monkeypatch.setattr(
            SubsJob, "publish_zh", lambda self, cues: f"publish {self.zh_target.name}"
        )
    inst, emit = r.inst, r.emit
    inst.start(emit)
    if case in ("failed", "zh_publish_failed"):
        r.translators[0].answer("a.mp4", ok=case != "failed")
        inst.check(emit)
        assert inst._settled["a.mp4"][0] == "failed"
    elif case == "no_key":
        inst.check(emit)
        assert inst._settled["a.mp4"] == ("skipped", "no_llm_key")
    elif case == "cancelled":
        with inst._launch_lock:
            inst._disable_subs("test", emit)
        inst._run_deferred()
        assert inst._settled["a.mp4"] == ("skipped", "cancelled")
    else:
        r.spawner.last.finish(0)
        inst.stop(timeout=2)  # publishes the ja only
    assert _ja(r, "a.mp4").read_text(encoding="utf-8").count("-->") == 2
    assert not _zh(r, "a.mp4").exists()


def _publish_ja_only(self: SubsJob) -> str:
    """`SubsJob.publish_empty` whose zh half fails: the empty ja is on disk."""
    return self._publish(self.ja_target, "") or f"publish {self.zh_target.name}"


@pytest.mark.parametrize(
    "case", ["zero_cue_zh_publish_failed", "no_speech_publish_failed", "unreadable"]
)
def test_ja_is_kept_when_the_empty_or_unreadable_path_fails(
    tmp_path, monkeypatch, case
):
    # The 0-cue resume and the no-speech outcome settle `completed` only after
    # their (empty) zh is published; when that publish fails, or the transcript
    # cannot be read at all, the .ja.srt stays as the next Start's checkpoint.
    if case == "no_speech_publish_failed":
        r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
        monkeypatch.setattr(SubsJob, "publish_empty", _publish_ja_only)
        text = ""
    else:
        r = _setup(tmp_path, monkeypatch, restored=["a.mp4"], ja=["a.mp4"])
        text = "" if case == "zero_cue_zh_publish_failed" else "not an srt at all"
        _ja(r, "a.mp4").write_text(text, encoding="utf-8")
        monkeypatch.setattr(
            SubsJob, "publish_zh", lambda self, cues: f"publish {self.zh_target.name}"
        )
    inst, emit = r.inst, r.emit
    inst.start(emit)
    if case == "no_speech_publish_failed":
        inst.check(emit)  # restored → ASR
        r.spawner.last.finish(0, state="empty", text="")
    inst.check(emit)
    assert inst._settled["a.mp4"][0] == "failed"
    if case == "unreadable":
        assert inst._settled["a.mp4"] == ("failed", "unreadable .ja.srt")
    assert _ja(r, "a.mp4").read_text(encoding="utf-8") == text
    assert not _zh(r, "a.mp4").exists()
    assert (r.out / "a-破解.mp4").exists()


def test_a_kept_ja_is_resumed_translate_only_and_then_deleted(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, restored=["a.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    r.spawner.last.finish(0)
    inst.stop(timeout=2)  # Stop: only the ja was published
    assert _ja(r, "a.mp4").exists() and not _zh(r, "a.mp4").exists()
    inst.start(emit)  # the next Start resumes from the checkpoint
    assert inst._kinds["a.mp4"] == "translate_only"
    assert len(r.spawner.argvs) == 1  # no second transcription
    r.translators[1].answer("a.mp4")
    inst.check(emit)
    assert _zh(r, "a.mp4").exists()
    assert not _ja(r, "a.mp4").exists()
    assert len(_done(r.evs)) == 1


def test_a_ja_deletion_failure_is_logged_and_never_fails_the_job(
    tmp_path, monkeypatch, caplog
):
    r = _setup(tmp_path, monkeypatch, restored=["a.mp4"], ja=["a.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)  # the ja is loaded and submitted
    ja = _ja(r, "a.mp4")
    ja.unlink()
    ja.mkdir()  # something in the way: the unlink fails
    (ja / "locked").write_text("x", encoding="utf-8")
    r.translators[0].answer("a.mp4")
    with caplog.at_level(logging.WARNING, logger="taskpaw.monitors.jasna"):
        st = inst.check(emit)  # never raises
    assert inst._settled["a.mp4"] == ("completed", "")
    assert _zh(r, "a.mp4").exists() and ja.is_dir()
    assert any(ja.name in rec.getMessage() for rec in caplog.records)
    done = _done(r.evs)
    assert len(done) == 1 and "Subs: 1/1 done, 0 failed, 0 skipped" in done[0][2]
    assert not [e for e in r.evs if e[0] == "alert"]
    assert st.state == "idle"


# ── docs ──────────────────────────────────────────────────────────────────
def test_field_descriptions_state_the_new_names():
    fields = JasnaConfig.model_fields
    out = fields["jasna_output_folder"].description or ""
    assert "<name>-破解.mp4" in out and "<name>_restored.mp4" in out
    assert "*-破解.tmp.mp4" in out
    av = fields["av_translate"].description or ""
    assert "<name>-破解.srt" in av and "<name>_restored.srt" in av
    assert "deleted" in av
    assert "delete its old .srt files first" in av
