"""#177 Jasna「AV 翻译」: planning, the subs/translate phases, settlement, stop/start.

Nothing real is executed: `jasna.exe` is the existing `_FakePopen` harness from
`test_jasna.py` (patched `subprocess.Popen`), the WhisperJAV ASR child is a
`ChildProcess` fake injected through `J.ChildProcess` (→ `JasnaInstance._spawn`),
and the translator is replaced wholesale by monkeypatching `J.Translator` — so a
test here never reaches `taskkill`, the network or a real LLM worker (D10).
"""

from __future__ import annotations

import json
import logging
import os
import queue
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest
from test_jasna import _events, _Launcher, _managed, _patch, _videos
from test_subs_translate import (
    FakeClock,
    Spawner,
    _ids,
    _reply,
    _seq,
    _stepper,
    down,
    good,
)

from taskpaw_v3.core import gpu_lease
from taskpaw_v3.core.datadir import set_data_dir
from taskpaw_v3.core.llm import LLMSettings, set_llm_chain, set_llm_settings
from taskpaw_v3.monitors.plugins import jasna as J
from taskpaw_v3.monitors.plugins.jasna import (
    JasnaConfig,
    JasnaInstance,
    JasnaPlugin,
    output_path_for,
    plan_queue,
    plan_subs,
)
from taskpaw_v3.monitors.subs import job as subs_job_mod
from taskpaw_v3.monitors.subs import srt
from taskpaw_v3.monitors.subs.checkpoint import CHECKPOINTS_DIRNAME
from taskpaw_v3.monitors.subs.job import PublishResult, SubsJob
from taskpaw_v3.monitors.subs.progress import AsrProgress, LiveFacts
from taskpaw_v3.monitors.subs.srt import Cue
from taskpaw_v3.monitors.subs.translate import (
    CANCELLED,
    NO_LLM_KEY,
    TRANSLATION_PAUSED,
    Notice,
    TranslateResult,
    Translator,
)
from taskpaw_v3.monitors.subs.whisperjav import attempt_dir
from taskpaw_v3.monitors.supervisor import Supervisor

SRT_JA = (
    "1\n00:00:00,000 --> 00:00:01,000\nはい\n\n"
    "2\n00:00:01,500 --> 00:00:02,500\nいいえ\n"
)
SRT_ZH = "1\n00:00:00,000 --> 00:00:01,000\n好\n"


# ── fakes ─────────────────────────────────────────────────────────────────
def _lock_free(inst: JasnaInstance) -> bool:
    """Whether ANOTHER thread can take `_launch_lock` right now (RLock: a
    same-thread probe would always succeed)."""
    got: list[bool] = []

    def grab() -> None:
        ok = inst._launch_lock.acquire(timeout=0)
        got.append(ok)
        if ok:
            inst._launch_lock.release()

    t = threading.Thread(target=grab, daemon=True)
    t.start()
    t.join(timeout=5)
    return bool(got and got[0])


class _FakeAsr:
    """`ChildProcess` stand-in for whisperjav.exe: exits when told and writes
    the WhisperJAV manifest (+ srt) into its `--output-dir`."""

    def __init__(self, argv: list[str], owner: Optional[dict] = None) -> None:
        self.argv = list(argv)
        self.pid = 5151
        self.rc: Optional[int] = None
        self.calls: list[str] = []
        self.terminate_lock_free: list[bool] = []
        self._owner = owner or {}
        self.out_dir = Path(argv[argv.index("--output-dir") + 1])

    def poll(self) -> Optional[int]:
        return self.rc

    def tail(self, lines: int = 10, max_chars: int = 800) -> str:
        return "ASR TAIL"

    def terminate_tree(self, timeout: float = 5.0) -> None:
        self.calls.append("terminate_tree")
        inst = self._owner.get("inst")
        if inst is not None:
            self.terminate_lock_free.append(_lock_free(inst))
        if self.rc is None:
            self.rc = 1

    def join_readers(self, timeout: float = 2.0) -> None:
        self.calls.append("join_readers")

    def finish(self, rc: int = 0, state: str = "done", text: str = SRT_JA) -> None:
        out = self.out_dir / "m.ja.whisperjav.srt"
        out.write_text(text, encoding="utf-8")
        manifest = {
            "files": [{"path": "m", "state": state, "output": str(out), "detail": ""}]
        }
        (self.out_dir / "whisperjav_run.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )
        self.rc = rc


class _Spawner:
    def __init__(self, owner: dict) -> None:
        self.owner = owner
        self.argvs: list[list[str]] = []
        self.children: list[_FakeAsr] = []
        self.fail_at: set[int] = set()
        self.on_spawn = None

    def __call__(self, argv, **kw):
        self.argvs.append(list(argv))
        n = len(self.argvs)
        if self.on_spawn is not None:
            self.on_spawn(n)
        if n in self.fail_at:
            raise OSError("boom")
        child = _FakeAsr(argv, self.owner)
        self.children.append(child)
        return child

    @property
    def last(self) -> _FakeAsr:
        return self.children[-1]


class _FakeTranslator:
    def __init__(self, run, *, name, owner: Optional[dict] = None, **kw) -> None:
        self.run = run
        self.name = name
        self.results: "queue.Queue[object]" = queue.Queue()
        self.submitted: list = []
        self.answered: set[str] = set()
        self.started = False
        self.joined = False
        self.cancelled = False
        self.cancel_calls = 0
        self.cancel_lock_free: list[bool] = []
        self.on_submit = None
        self.live: Optional[dict] = None  # #189: what progress() reports
        self._owner = owner or {}
        self.kw = kw  # #192: the plugin's construction arguments (checkpoint_dir)
        self.notices: list[Notice] = []  # what drain_notices() hands over next
        self.discarded: list[str] = []  # discard_checkpoint() keys, in order

    def start(self) -> None:
        self.started = True

    def progress(self, now: Optional[float] = None) -> Optional[dict]:
        """#189: the in-flight request's counters; None when idle/cancelled."""
        if self.cancelled or self.live is None:
            return None
        return dict(self.live)

    def submit(self, req) -> None:
        if self.cancelled:
            return
        if self.on_submit is not None:
            self.on_submit(req)
        self.submitted.append(req)

    def queued(self) -> int:
        if self.cancelled:
            return 0
        return len([r for r in self.submitted if r.job_id not in self.answered])

    def in_flight(self) -> bool:
        return False

    def cancel(self) -> None:
        self.cancel_calls += 1
        inst = self._owner.get("inst")
        if inst is not None:
            self.cancel_lock_free.append(_lock_free(inst))
        self.cancelled = True
        self.results.put(CANCELLED)

    def join(self, timeout: float) -> None:
        self.joined = True

    def is_alive(self) -> bool:
        return self.started and not self.joined

    def drain_notices(self) -> list[Notice]:
        out, self.notices = self.notices, []
        return out

    def discard_checkpoint(self, key: str) -> None:
        self.discarded.append(key)

    def answer(
        self,
        job_id: str,
        ok: bool = True,
        zh: str = "好",
        *,
        outcome: Optional[str] = None,
        kept_ja: int = 0,
    ) -> None:
        """`translated` (ok) / `failed` (not ok), or #192's `paused` / `no_key`
        (`outcome`) with the engine's detail; `checkpoint_key` = `ck:<job>`."""
        req = next(r for r in self.submitted if r.job_id == job_id)
        self.answered.add(job_id)
        kind = outcome or ("translated" if ok else "failed")
        key = f"ck:{job_id}"
        if kind == "translated":
            cues = tuple(Cue(c.index, c.start_ms, c.end_ms, zh) for c in req.cues)
            res = TranslateResult(
                req.run, job_id, kind, cues, "", kept_ja=kept_ja, checkpoint_key=key
            )
        elif kind in ("paused", "no_key"):
            text = TRANSLATION_PAUSED if kind == "paused" else NO_LLM_KEY
            res = TranslateResult(req.run, job_id, kind, (), text, checkpoint_key=key)
        else:
            res = TranslateResult(req.run, job_id, "failed", (), "network")
        self.results.put(res)


def _key(on: bool = True) -> None:
    """The primary's settings AND the provider chain the key check reads
    (#192 AC10): on = one usable provider, off = an empty chain."""
    s = LLMSettings("https://api.x.ai/v1", "grok-4.3", "sk-test" if on else "", "none")
    set_llm_settings(s)
    set_llm_chain((s,) if on else ())


def _setup(
    tmp_path: Path,
    monkeypatch,
    *,
    pending=(),
    restored=(),
    legacy=(),
    ja=(),
    zh=(),
    rcs=None,
    key: bool = True,
    exe: bool = True,
    probe=None,
    **kw,
):
    wj = tmp_path / "wj" / "whisperjav.exe"
    wj.parent.mkdir(parents=True, exist_ok=True)
    if exe:
        wj.write_bytes(b"MZ")
    base: dict = dict(
        av_translate=True, whisperjav_exe_path=str(wj), unet4x_1080p=False
    )
    base.update(kw)
    cfg, inp, out, _home = _managed(tmp_path, **base)
    _videos(inp, *dict.fromkeys((*pending, *restored, *legacy)))
    for n in restored:
        output_path_for(str(out), Path(n)).write_bytes(b"restored " + n.encode())
    for n in legacy:  # #187: a `<stem>_restored.mp4` from 3.5.1 and earlier
        (out / f"{Path(n).stem}_restored.mp4").write_bytes(b"legacy " + n.encode())
    # Subtitles sit next to the video they belong to (#187): the legacy file's
    # names only when there is no new-name output.
    old = set(legacy) - set(restored)
    for n in ja:
        _sub(out, n, ".ja.srt", n in old).write_text(SRT_JA, encoding="utf-8")
    for n in zh:
        _sub(out, n, ".srt", n in old).write_text(SRT_ZH, encoding="utf-8")
    launcher = _Launcher(list(rcs) if rcs is not None else [0] * 20)
    _patch(monkeypatch, launcher, probe)
    owner: dict = {}
    translators: list[_FakeTranslator] = []

    def factory(run, *, name, **k):
        t = _FakeTranslator(run, name=name, owner=owner, **k)
        translators.append(t)
        return t

    monkeypatch.setattr(J, "Translator", factory)
    spawner = _Spawner(owner)
    monkeypatch.setattr(J, "ChildProcess", spawner)
    _key(key)
    inst = JasnaInstance("j1", cfg)
    owner["inst"] = inst
    evs, emit = _events()
    return SimpleNamespace(
        cfg=cfg,
        inp=inp,
        out=out,
        launcher=launcher,
        spawner=spawner,
        translators=translators,
        inst=inst,
        evs=evs,
        emit=emit,
        owner=owner,
    )


def _gpu_spy(monkeypatch) -> list[str]:
    log: list[str] = []

    def acquire(self) -> bool:
        log.append("acquire")
        return True

    def release(self) -> None:
        log.append("release")

    monkeypatch.setattr(JasnaInstance, "_gpu_acquire", acquire)
    monkeypatch.setattr(JasnaInstance, "_gpu_release", release)
    return log


def _assert_strict_pairs(log: list[str], open_ok: bool = False) -> None:
    """acquire/release alternate, starting with acquire, and are balanced (one
    trailing acquire allowed while a GPU child is still live)."""
    for i, what in enumerate(log):
        assert what == ("acquire" if i % 2 == 0 else "release"), log
    if not open_ok:
        assert len(log) % 2 == 0, log


def _done(evs) -> list:
    return [e for e in evs if e[0] == "done"]


def _keyed(evs, key: str) -> list:
    return [e for e in evs if e[3] == key]


def _sub(out: Path, name: str, suffix: str, legacy: bool = False) -> Path:
    """`<stem>-破解<suffix>` next to the new output, or `<stem>_restored<suffix>`
    next to a legacy one (#187) — spelled out, never via the plugin helpers."""
    tag = "_restored" if legacy else "-破解"
    return out / f"{Path(name).stem}{tag}{suffix}"


def _ja(r, name: str, legacy: bool = False) -> Path:
    return _sub(r.out, name, ".ja.srt", legacy)


def _zh(r, name: str, legacy: bool = False) -> Path:
    return _sub(r.out, name, ".srt", legacy)


def _wait(cond, timeout: float = 8.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _depth() -> int:
    f = sys._getframe(1)
    n = 0
    while f is not None:
        n += 1
        f = f.f_back
    return n


# ── config ────────────────────────────────────────────────────────────────
def test_subs_config_defaults_and_schema():
    c = JasnaConfig(name="j")
    assert c.av_translate is False
    assert c.whisperjav_exe_path == ""
    assert c.whisperjav_engine == "anime-whisper"
    assert c.whisperjav_extra_args == ""
    props = JasnaPlugin.json_schema()["properties"]
    assert props["av_translate"]["default"] is False
    assert props["av_translate"]["title"] == "AV 翻译"
    assert props["whisperjav_engine"]["default"] == "anime-whisper"
    assert set(props["whisperjav_engine"]["enum"]) == {
        "anime-whisper",
        "large-v3",
        "large-v2",
        "qwen3",
        "custom",
    }
    for f in ("av_translate", "whisperjav_exe_path", "whisperjav_engine"):
        assert props[f].get("description")
    ui = JasnaPlugin.ui_schema()
    order = ui["ui:order"]
    i = order.index("unet4x_4k")
    assert order[i + 1 : i + 5] == [
        "av_translate",
        "whisperjav_exe_path",
        "whisperjav_engine",
        "whisperjav_extra_args",
    ]
    assert ui["whisperjav_exe_path"]["ui:options"]["taskpawPath"] == "file"


def test_ticked_av_translate_needs_the_whisperjav_exe():
    with pytest.raises(ValueError, match="whisperjav_exe_path"):
        JasnaConfig(name="j", av_translate=True)
    with pytest.raises(ValueError, match="whisperjav_exe_path"):
        JasnaConfig(name="j", av_translate=True, whisperjav_exe_path="   ")
    ok = JasnaConfig(name="j", av_translate=True, whisperjav_exe_path="C:/wj.exe")
    assert ok.av_translate is True


@pytest.mark.parametrize(
    "extra",
    [
        "--output-dir x",
        "--out x",
        "--language=japanese",
        "--lang ja",
        "--temp x",
        "--no-sig",
        "--mode fast",
        "--mod fast",
        "--qwen-gen x",
        "--translate",
        "--translate-api-key k",
        "--translate=deepseek",
    ],
)
def test_owned_and_forbidden_whisperjav_flags_are_rejected(extra):
    with pytest.raises(ValueError, match="whisperjav_extra_args"):
        JasnaConfig(
            name="j",
            av_translate=True,
            whisperjav_exe_path="C:/wj.exe",
            whisperjav_extra_args=extra,
        )


def test_allowed_whisperjav_extras_pass_and_custom_frees_the_preset_flags():
    c = JasnaConfig(
        name="j",
        av_translate=True,
        whisperjav_exe_path="C:/wj.exe",
        whisperjav_extra_args=(
            "--sensitivity aggressive --vad-version 4 --qwen-segmenter x "
            "--fail-on never --ensemble"
        ),
    )
    assert "--ensemble" in c.whisperjav_extra_args
    c2 = JasnaConfig(
        name="j",
        av_translate=True,
        whisperjav_exe_path="C:/wj.exe",
        whisperjav_engine="custom",
        whisperjav_extra_args="--mode balanced --model large-v3",
    )
    assert c2.whisperjav_engine == "custom"
    with pytest.raises(ValueError, match="--output-dir"):
        JasnaConfig(
            name="j",
            whisperjav_engine="custom",
            whisperjav_extra_args="--output-dir x",
        )


def test_unbalanced_quote_in_whisperjav_extras_is_a_config_error():
    with pytest.raises(ValueError, match="quote"):
        JasnaConfig(name="j", whisperjav_extra_args='--sensitivity "aggressive')


# ── plan_subs (pure) ──────────────────────────────────────────────────────
def test_plan_subs_classifies_orders_and_counts(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    _videos(inp, "a.mp4", "b.mp4", "c.mp4", "d.mp4", "e.mkv", "f.mp4")
    (inp / "z.srt").write_text(SRT_JA, encoding="utf-8")  # never scanned
    (inp / ".avsubs").mkdir()  # never scanned
    for n in ("c", "d", "e", "f"):
        (out / f"{n}-破解.mp4").write_bytes(b"r")
    (out / "b-破解.ja.srt").write_text(SRT_JA, encoding="utf-8")  # pending, ja
    (out / "c-破解.srt").write_text(SRT_ZH, encoding="utf-8")  # zh → none
    (out / "d-破解.ja.srt").write_text(SRT_JA, encoding="utf-8")  # translate
    (out / "f-破解.srt").write_bytes(b"")  # 0-byte zh counts as done
    pending, _done, collisions = plan_queue(str(inp), str(out))
    plan = plan_subs(str(inp), str(out), pending, [a for a, _ in collisions])
    assert {p.name: k for p, k in plan.for_pending.items()} == {
        "a.mp4": "full",
        "b.mp4": "translate_only",
    }
    assert [p.name for p in plan.subs_only] == ["d.mp4", "e.mkv"]
    assert plan.total == 4


def test_plan_subs_pending_with_an_existing_zh_needs_nothing(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    _videos(inp, "a.mp4")
    (out / "a-破解.srt").write_text(SRT_ZH, encoding="utf-8")
    plan = plan_subs(str(inp), str(out), [inp / "a.mp4"], [])
    assert plan.for_pending == {inp / "a.mp4": "none"}
    assert plan.subs_only == [] and plan.total == 0


def test_plan_subs_excludes_collision_losers_so_one_media_gets_one_job(tmp_path):
    # D21: a.mkv + a.mp4 share a-破解.mp4 — without `excluded` the loser would
    # become a duplicate subs-only job on the same media and the same targets.
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    _videos(inp, "a.mkv", "a.mp4")
    (out / "a-破解.mp4").write_bytes(b"r")
    pending, _done, collisions = plan_queue(str(inp), str(out))
    assert [a.name for a, _ in collisions] == ["a.mp4"]
    plan = plan_subs(str(inp), str(out), pending, [a for a, _ in collisions])
    assert [p.name for p in plan.subs_only] == ["a.mkv"]
    assert plan.total == 1


def test_collision_loser_never_gets_asr(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, restored=["a.mkv", "a.mp4"])
    r.inst.start(r.emit)
    assert r.launcher.n == 0
    assert len(r.spawner.argvs) == 1
    assert r.inst.check(r.emit).metrics["subs_total"] == 1


# ── lifecycle ─────────────────────────────────────────────────────────────
def test_restore_then_asr_then_translation_end_to_end(tmp_path, monkeypatch):
    gpu = _gpu_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    assert r.launcher.n == 1 and r.spawner.argvs == []

    st = inst.check(emit)  # a restored → ASR for a, NOT a second jasna.exe (D3)
    assert r.launcher.n == 1
    assert len(r.spawner.argvs) == 1
    argv = r.spawner.argvs[0]
    media = r.out / "a-破解.mp4"
    assert argv[0] == r.cfg.whisperjav_exe_path
    assert argv[1] == str(media)  # C10: the restored file
    out_dir = attempt_dir(r.out / ".avsubs", "a.mp4", 1)
    assert argv[argv.index("--output-dir") + 1] == str(out_dir)
    assert argv[argv.index("--temp-dir") + 1] == str(r.out / ".avsubs" / "tmp")
    assert "--no-signature" in argv
    assert argv[argv.index("--language") + 1] == "japanese"
    assert st.state == "running"
    assert st.metrics["phase"] == "subs"
    assert st.metrics["current_file"] == "a-破解.mp4"
    assert "percent" not in st.metrics
    assert st.detail.startswith("subtitling: a-破解.mp4 [anime-whisper] · ")
    assert "elapsed · translating 0 · subs 0/2" in st.detail
    _assert_strict_pairs(gpu, open_ok=True)

    r.spawner.last.finish(0)
    st = inst.check(emit)  # ja published, translation submitted, b launched
    assert _ja(r, "a.mp4").read_text(encoding="utf-8").count("-->") == 2
    tr = r.translators[0]
    assert [q.job_id for q in tr.submitted] == ["a.mp4"]
    assert len(tr.submitted[0].cues) == 2
    assert tr.submitted[0].run == inst._run
    assert r.launcher.n == 2 and r.launcher.inputs()[-1] == "b.mp4"
    assert st.metrics["phase"] == "restore"
    assert st.metrics["subs_translating"] == 1
    assert "translating 1" in st.detail

    tr.answer("a.mp4")
    st = inst.check(emit)  # zh published; b restored → ASR for b
    assert _zh(r, "a.mp4").read_text(encoding="utf-8").count("好") == 2
    assert not _ja(r, "a.mp4").exists()  # #187: the checkpoint goes with the zh
    assert st.metrics["subs_completed"] == 1
    assert len(r.spawner.argvs) == 2

    r.spawner.last.finish(0, state="empty", text="")
    st = inst.check(emit)  # no speech → an empty zh, completed, no ja left
    assert not _ja(r, "b.mp4").exists()
    assert _zh(r, "b.mp4").read_bytes() == b""
    done = _done(r.evs)
    assert len(done) == 1
    assert (
        "Queue: 2/2 done, 0 failed | Subs: 2/2 done, 0 failed, 0 skipped"
        in (done[0][2])
    )
    assert st.state == "idle"
    assert st.metrics["subs_remaining"] == 0
    for _ in range(3):
        inst.check(emit)
    assert len(_done(r.evs)) == 1
    assert r.launcher.n == 2
    _assert_strict_pairs(gpu)
    # #179 C5: file-scoped hold — each file acquires once (restore → ASR)
    assert gpu.count("acquire") == 2
    inst.stop(timeout=1)


def test_result_for_an_already_settled_job_is_never_published(tmp_path, monkeypatch):
    # D7: _settled is checked before any publish.
    r = _setup(tmp_path, monkeypatch, restored=["d.mp4"], ja=["d.mp4"])
    r.inst.start(r.emit)
    tr = r.translators[0]
    assert [q.job_id for q in tr.submitted] == ["d.mp4"]
    with r.inst._launch_lock:
        r.inst._settle("d.mp4", "skipped", "cancelled", r.emit)
    req = tr.submitted[0]
    tr.results.put(
        TranslateResult(req.run, "d.mp4", "translated", (Cue(1, 0, 1000, "好"),), "")
    )
    r.inst.check(r.emit)
    assert not _zh(r, "d.mp4").exists()
    assert r.inst._subs_completed == 0 and r.inst._subs_skipped == 1


def test_asr_failure_retries_once_then_alerts_failed(tmp_path, monkeypatch):
    gpu = _gpu_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    r.inst.start(r.emit)
    r.inst.check(r.emit)
    r.spawner.last.finish(1)
    r.inst.check(r.emit)  # retry in a NEW attempt dir
    assert len(r.spawner.argvs) == 2
    a2 = r.spawner.argvs[1]
    assert a2[a2.index("--output-dir") + 1] == str(
        attempt_dir(r.out / ".avsubs", "a.mp4", 2)
    )
    r.spawner.last.finish(1)
    st = r.inst.check(r.emit)
    alerts = _keyed(r.evs, "j1:subs:a.mp4")
    assert len(alerts) == 1
    assert "exit code 1" in alerts[0][2]
    assert "--output-dir" not in alerts[0][2]  # never the argv
    assert st.metrics["queue_completed"] == 1 and st.metrics["queue_failed"] == 0
    assert st.metrics["subs_failed"] == 1
    done = _done(r.evs)
    assert len(done) == 1 and "Subs: 0/1 done, 1 failed, 0 skipped" in done[0][2]
    _assert_strict_pairs(gpu)


def test_media_changed_during_asr_is_skipped_unstable(tmp_path, monkeypatch):
    gpu = _gpu_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    r.inst.start(r.emit)
    r.inst.check(r.emit)
    (r.out / "a-破解.mp4").write_bytes(b"changed underneath, longer")
    r.spawner.last.finish(0)
    r.inst.check(r.emit)
    assert len(_keyed(r.evs, "j1:subs-unstable:a.mp4")) == 1
    assert r.inst._settled["a.mp4"][0] == "skipped"
    assert not _ja(r, "a.mp4").exists()
    assert "Subs: 0/1 done, 0 failed, 1 skipped" in _done(r.evs)[0][2]
    _assert_strict_pairs(gpu)


def test_restore_failure_settles_the_subs_job_skipped_restore_failed(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"], rcs=[1, 1])
    r.inst.start(r.emit)
    r.inst.check(r.emit)
    r.inst.check(r.emit)
    assert r.spawner.argvs == []
    assert r.inst._settled["a.mp4"] == ("skipped", "restore_failed")
    done = _done(r.evs)
    assert len(done) == 1
    assert (
        "Queue: 0/1 done, 1 failed | Subs: 0/1 done, 0 failed, 1 skipped"
        in (done[0][2])
    )


def test_subs_only_with_nothing_pending_starts_without_jasna(tmp_path, monkeypatch):
    # D2: an empty _pending with subs-only work starts subtitling at Start.
    r = _setup(tmp_path, monkeypatch, restored=["e.mp4"])
    r.inst.start(r.emit)
    assert r.launcher.n == 0
    assert len(r.spawner.argvs) == 1
    st = r.inst.check(r.emit)
    assert st.state == "running" and st.metrics["phase"] == "subs"
    assert r.launcher.n == 0
    r.inst.stop(timeout=1)


def test_translate_only_skips_asr_and_never_acquires_the_gpu(tmp_path, monkeypatch):
    gpu = _gpu_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, restored=["d.mp4"], ja=["d.mp4"])
    r.inst.start(r.emit)
    assert r.spawner.argvs == [] and gpu == []
    st = r.inst.check(r.emit)
    assert st.state == "running"
    assert st.metrics["phase"] == "translate"
    assert "current_file" not in st.metrics  # D17: no live child
    assert st.detail == "translating 1 · subs 0/1"
    r.translators[0].answer("d.mp4")
    st = r.inst.check(r.emit)
    assert _zh(r, "d.mp4").exists()
    assert st.state == "idle"
    assert len(_done(r.evs)) == 1
    assert gpu == []


def test_missing_key_skips_per_job_one_alert_and_a_later_key_translates(
    tmp_path, monkeypatch
):
    # D31: a translate-only no-key file followed by a pending restore → exactly
    # one jasna.exe launch in the same check.
    r = _setup(
        tmp_path,
        monkeypatch,
        pending=["a.mp4", "b.mp4", "c.mp4"],
        ja=["a.mp4", "b.mp4", "c.mp4"],
        key=False,
    )
    r.inst.start(r.emit)
    assert r.launcher.n == 1
    r.inst.check(r.emit)  # a restored → translate-only → no key → b launched
    assert r.inst._settled["a.mp4"] == ("skipped", "no_llm_key")
    assert r.launcher.n == 2
    r.inst.check(r.emit)  # b → no key again: still ONE alert per run
    assert r.launcher.n == 3
    assert len(_keyed(r.evs, "j1:subs-nokey")) == 1
    _key(True)
    r.inst.check(r.emit)  # c → the key is live now → submitted
    assert [q.job_id for q in r.translators[0].submitted] == ["c.mp4"]
    assert r.spawner.argvs == []


def test_three_consecutive_subs_failures_disable_subs_via_the_deferred_step(
    tmp_path, monkeypatch
):
    gpu = _gpu_spy(monkeypatch)
    r = _setup(
        tmp_path, monkeypatch, pending=["a.mp4", "b.mp4", "c.mp4", "d.mp4", "e.mp4"]
    )
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    inst.check(emit)  # a → ASR a
    sp.last.finish(1)
    inst.check(emit)  # retry
    sp.last.finish(1)
    inst.check(emit)  # a failed (1) → b launched
    inst.check(emit)  # b → ASR b
    sp.last.finish(0)
    inst.check(emit)  # tB submitted → c launched
    inst.check(emit)  # c → ASR c
    sp.last.finish(0)
    inst.check(emit)  # tC submitted → d launched
    inst.check(emit)  # d → ASR d (live)
    asr_d = sp.last
    assert asr_d.rc is None and r.launcher.n == 4
    tr = r.translators[0]
    tr.answer("b.mp4", ok=False)
    tr.answer("c.mp4", ok=False)
    st = inst.check(emit)  # (2) then (3) → disable, deferred cancel + terminate
    assert len(_keyed(r.evs, "j1:subs-disabled")) == 1
    # the deferred disable cancels it; the run then completes in this same check
    # and F2 releases the (already cancelled) translator — cancel is idempotent
    assert tr.cancel_calls == 2 and tr.cancel_lock_free == [True, True]
    assert len(_done(r.evs)) == 1 and r.inst._translator is None
    assert asr_d.calls[:2] == ["terminate_tree", "join_readers"]
    assert asr_d.terminate_lock_free == [True]
    assert inst._settled["d.mp4"] == ("skipped", "cancelled")
    assert inst._settled["e.mp4"] == ("skipped", "cancelled")
    assert inst._subs_job is None
    assert r.launcher.n == 5  # restores continue
    # settlement runs before the restore poll, so e's (instant) exit is handled
    # in this same check — with subtitles off it just completes the queue
    assert st.metrics["queue_completed"] == 5
    inst.check(emit)
    assert len(sp.argvs) == 5  # a twice, b, c, d — nothing for e
    done = _done(r.evs)
    assert len(done) == 1
    assert (
        "Queue: 5/5 done, 0 failed | Subs: 0/5 done, 3 failed, 2 skipped"
        in (done[0][2])
    )
    _assert_strict_pairs(gpu)


def test_restore_abort_disables_subs_and_runs_the_deferred_part_in_that_check(
    tmp_path, monkeypatch
):
    # C2 + D8: the abort short-circuit must not strand a translator or a child.
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4", "c.mp4"], rcs=[1] * 6)
    inst, emit = r.inst, r.emit
    inst.start(emit)
    for _ in range(5):
        inst.check(emit)
    tr = r.translators[0]
    assert tr.cancel_calls == 0
    # A live ASR child the abort must reach (constructed: GPU work is serial).
    job = inst._jobs["c.mp4"]
    child = _FakeAsr(["wj", "m", "--output-dir", str(tmp_path)], r.owner)
    job.child = child
    inst._subs_job = job
    st = inst.check(emit)
    assert st.state == "degraded"
    assert tr.cancel_calls == 1 and tr.cancel_lock_free == [True]
    assert "terminate_tree" in child.calls and child.terminate_lock_free == [True]
    titles = [e[1] for e in r.evs if e[0] == "alert"]
    disabled = next(i for i, t in enumerate(titles) if "AV 翻译 disabled" in t)
    aborted = next(i for i, t in enumerate(titles) if "batch aborted" in t)
    assert disabled < aborted
    assert not _done(r.evs)
    assert inst.check(emit).state == "degraded"


@pytest.mark.parametrize("asr", ["failed", "succeeded"])
def test_third_failure_from_a_translation_while_the_asr_exited_unpolled(
    tmp_path, monkeypatch, asr
):
    # D22 + D26: the closure terminates/joins the exited-but-unpolled child,
    # releases the GPU hook once, and no ASR retry / ja publish follows.
    gpu = _gpu_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4", "c.mp4"])
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    inst.check(emit)  # ASR a
    sp.last.finish(0)
    inst.check(emit)  # tA submitted → b
    inst.check(emit)  # ASR b
    sp.last.finish(1 if asr == "failed" else 0)  # exited, NOT polled yet
    inst._subs_consecutive_failures = 2
    r.translators[0].answer("a.mp4", ok=False)
    inst.check(emit)
    child_b = sp.children[1]
    assert child_b.calls == ["terminate_tree", "join_readers"]
    assert len(sp.argvs) == 2  # no retry
    assert not _ja(r, "b.mp4").exists()
    assert inst._subs_job is None
    assert r.launcher.n == 3  # c launched once
    _assert_strict_pairs(gpu, open_ok=True)
    inst.check(emit)  # c restored → done
    assert len(_done(r.evs)) == 1
    _assert_strict_pairs(gpu)


def test_asr_final_failure_as_third_failure_launches_exactly_one_restore(
    tmp_path, monkeypatch
):
    # D25: the poll terminal branch and the disable closure must not both advance.
    gpu = _gpu_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4", "c.mp4"])
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    inst.check(emit)
    inst._subs_consecutive_failures = 2
    sp.last.finish(1)
    inst.check(emit)
    sp.last.finish(1)
    assert r.launcher.n == 1
    inst.check(emit)
    assert len(_keyed(r.evs, "j1:subs-disabled")) == 1
    assert r.launcher.n == 2
    _assert_strict_pairs(gpu, open_ok=True)


def test_asr_launch_error_then_next_probe_runs_outside_the_launch_lock(
    tmp_path, monkeypatch
):
    # D27: after a _start_subs launch error the ffprobe for the next restore
    # never runs under _launch_lock.
    lock_free: list[bool] = []
    holder: dict = {}

    def probe(video, ffprobe):
        if "inst" in holder:
            lock_free.append(_lock_free(holder["inst"]))
        return (1920, 1080)

    gpu = _gpu_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"], probe=probe)
    holder["inst"] = r.inst
    r.spawner.fail_at = {1, 2}
    r.inst.start(r.emit)
    r.inst.check(r.emit)
    assert len(r.spawner.argvs) == 2  # one retry of the launch
    alerts = _keyed(r.evs, "j1:subs:a.mp4")
    assert len(alerts) == 1 and "launch: OSError: boom" in alerts[0][2]
    assert r.launcher.n == 2
    assert lock_free == [True, True]
    _assert_strict_pairs(gpu, open_ok=True)


def test_1500_translate_only_files_are_walked_iteratively(tmp_path, monkeypatch):
    # D29: no _advance → _start_subs → _dispatch → _advance recursion.
    names = [f"v{i:04d}.mp4" for i in range(1500)]
    r = _setup(tmp_path, monkeypatch, restored=names, ja=names)
    depths: list[int] = []
    orig = J.Translator

    def factory(run, *, name, **k):
        t = orig(run, name=name, **k)
        t.on_submit = lambda req: depths.append(_depth())
        return t

    monkeypatch.setattr(J, "Translator", factory)
    r.inst.start(r.emit)
    r.inst.check(r.emit)
    tr = r.translators[0]
    assert len(tr.submitted) == 1500
    assert max(depths) - min(depths) <= 2


def test_1500_translate_only_files_without_a_key_finish_with_done(
    tmp_path, monkeypatch
):
    # D32: Start walks them all, one alert, and `done` fires.
    names = [f"v{i:04d}.mp4" for i in range(1500)]
    r = _setup(tmp_path, monkeypatch, restored=names, ja=names, key=False)
    r.inst.start(r.emit)
    r.inst.check(r.emit)
    assert r.inst._subs_skipped == 1500
    assert len(_keyed(r.evs, "j1:subs-nokey")) == 1
    done = _done(r.evs)
    assert len(done) == 1 and "Subs: 0/1500 done, 0 failed, 1500 skipped" in done[0][2]


@pytest.mark.parametrize("retry", ["unstable", "raises"])
def test_asr_retry_that_cannot_start_settles_like_a_final_failure(
    tmp_path, monkeypatch, retry
):
    # D33
    gpu = _gpu_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"])
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    inst.check(emit)
    sp.last.finish(1)
    if retry == "raises":
        sp.fail_at = {2}
    else:
        real = subs_job_mod.source_identity
        calls = {"n": 0}

        def ident(path):
            calls["n"] += 1
            if calls["n"] >= 2:  # (patched after start #1) poll, then the retry
                return (0, 0)
            return real(path)

        monkeypatch.setattr(subs_job_mod, "source_identity", ident)
    inst.check(emit)
    if retry == "raises":
        assert inst._settled["a.mp4"][0] == "failed"
        assert "launch: OSError" in inst._settled["a.mp4"][1]
        assert len(_keyed(r.evs, "j1:subs:a.mp4")) == 1
    else:
        assert inst._settled["a.mp4"] == ("skipped", "unstable")
        assert len(_keyed(r.evs, "j1:subs-unstable:a.mp4")) == 1
    assert inst._subs_job is None
    assert r.launcher.n == 2  # next restore launched
    # #179 C5: a's ASR continued with its restore hold; b is live
    assert gpu.count("acquire") == 2 and gpu.count("release") == 1
    _assert_strict_pairs(gpu, open_ok=True)


def test_stop_winning_the_lock_before_start_asr_still_releases_the_gpu(
    tmp_path, monkeypatch
):
    # D30 — on the subs-only path, which still acquires (#179: a restored
    # file's ASR continues with the restore's hold instead).
    r = _setup(tmp_path, monkeypatch, restored=["e.mp4"])
    log: list[str] = []

    def acquire(self) -> bool:
        log.append("acquire")
        if len(log) == 1:  # the subs-only ASR acquire
            self._stopping.set()
        return True

    def release(self) -> None:
        log.append("release")

    monkeypatch.setattr(JasnaInstance, "_gpu_acquire", acquire)
    monkeypatch.setattr(JasnaInstance, "_gpu_release", release)
    r.inst.start(r.emit)
    r.inst.check(r.emit)
    assert r.spawner.argvs == []
    assert log == ["acquire", "release"]
    assert not _done(r.evs)


def test_stop_racing_the_asr_retry_releases_the_gpu_once(tmp_path, monkeypatch):
    # D35: the retry's post-spawn _stopping re-check terminates, releases,
    # clears _subs_job and requests no advance.
    gpu = _gpu_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"])
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    inst.check(emit)
    sp.last.finish(1)

    def on_spawn(n):
        if n == 2:
            inst._stopping.set()

    sp.on_spawn = on_spawn
    inst.check(emit)
    assert len(sp.children) == 2
    assert "terminate_tree" in sp.children[1].calls
    assert inst._subs_job is None
    assert r.launcher.n == 1
    assert "a.mp4" not in inst._settled
    _assert_strict_pairs(gpu)
    inst.stop(timeout=1)
    _assert_strict_pairs(gpu)


@pytest.mark.parametrize("av", [True, False])
@pytest.mark.parametrize("case", ["empty", "all_restored", "all_none"])
def test_zero_work_start_emits_nothing(tmp_path, monkeypatch, av, case):
    # D20
    if case == "empty":
        kw: dict = {}
    elif case == "all_restored":
        kw = dict(restored=["a.mp4"], zh=["a.mp4"])
    else:
        kw = dict(restored=["a.mp4", "b.mp4"], zh=["a.mp4", "b.mp4"])
    r = _setup(tmp_path, monkeypatch, av_translate=av, **kw)
    r.inst.start(r.emit)
    st = r.inst.check(r.emit)
    r.inst.check(r.emit)
    assert r.evs == []
    assert st.state == "idle" and st.detail.startswith("nothing to process")
    assert r.launcher.n == 0 and r.spawner.argvs == []


def test_done_fires_after_the_last_translation_settles(tmp_path, monkeypatch):
    # D1: no further _advance happens — check() itself evaluates `done`.
    r = _setup(tmp_path, monkeypatch, restored=["d.mp4"], ja=["d.mp4"])
    r.inst.start(r.emit)
    for _ in range(3):
        r.inst.check(r.emit)
    assert not _done(r.evs)
    r.translators[0].answer("d.mp4")
    r.inst.check(r.emit)
    r.inst.check(r.emit)
    assert len(_done(r.evs)) == 1


@pytest.mark.parametrize(
    "block",
    [
        None,
        "pending",
        "process",
        "unsettled",
        "subs_job",
        "subs_only",
        "queued",
        "result",
        "stopping",
        "aborted",
        "no_work",
    ],
)
def test_each_done_condition_alone_blocks_done(tmp_path, monkeypatch, block):
    r = _setup(tmp_path, monkeypatch, restored=["d.mp4"], ja=["d.mp4"])
    inst = r.inst
    inst.start(r.emit)
    tr = r.translators[0]
    tr.answer("d.mp4")
    inst._settle_results(r.emit)
    assert inst._settled["d.mp4"][0] == "completed"
    r.evs.clear()
    inst._batch_done_emitted = False
    if block == "pending":
        inst._pending = [r.inp / "d.mp4"]
    elif block == "process":
        inst._process = object()  # type: ignore[assignment]
    elif block == "unsettled":
        del inst._settled["d.mp4"]
    elif block == "subs_job":
        inst._subs_job = inst._jobs["d.mp4"]
    elif block == "subs_only":
        inst._subs_only = [r.inp / "d.mp4"]
    elif block == "queued":
        tr.answered.clear()
    elif block == "result":
        tr.results.put(TranslateResult(inst._run, "zz", "failed", (), "x"))
    elif block == "stopping":
        inst._stopping.set()
    elif block == "aborted":
        inst._batch_aborted = True
    elif block == "no_work":
        inst._had_work = False
    inst._maybe_done(r.emit)
    assert len(_done(r.evs)) == (1 if block is None else 0)
    inst._process = None


# ── stop / restart ────────────────────────────────────────────────────────
def test_stop_with_a_live_asr_child_terminates_it_and_releases_the_gpu(
    tmp_path, monkeypatch
):
    gpu = _gpu_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, restored=["e.mp4"])
    r.inst.start(r.emit)
    child = r.spawner.last
    tr = r.translators[0]
    r.inst.stop(timeout=2)
    assert tr.cancel_calls == 1 and tr.cancel_lock_free == [True]  # first
    assert tr.joined
    assert "terminate_tree" in child.calls
    assert r.inst._subs_job is None
    _assert_strict_pairs(gpu)
    r.inst.check(r.emit)
    assert not _done(r.evs)


def test_stop_with_an_exited_asr_child_publishes_the_ja_only(tmp_path, monkeypatch):
    gpu = _gpu_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, restored=["e.mp4"])
    r.inst.start(r.emit)
    r.spawner.last.finish(0)
    r.inst.stop(timeout=2)
    assert _ja(r, "e.mp4").exists()
    assert not _zh(r, "e.mp4").exists()
    assert r.translators[0].submitted == []
    _assert_strict_pairs(gpu)
    r.inst.check(r.emit)
    assert not _done(r.evs) and not _zh(r, "e.mp4").exists()


def test_restart_takes_a_new_generation_cleans_up_and_sweeps(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, restored=["d.mp4"], ja=["d.mp4"])
    r.inst.start(r.emit)
    old_run = r.inst._run
    old_tr = r.translators[0]
    stale = r.out / "d_restored.srt.99.tmp"
    stale.write_text("x", encoding="utf-8")
    # #179 (C3/M12): the Start sweep only removes temporaries older than 10 min,
    # so a same-folder avsubs publish in flight is never deleted — age this one.
    os.utime(stale, (time.time() - 660,) * 2)
    keep = r.out / "notes.tmp"
    keep.write_text("x", encoding="utf-8")
    junk = r.out / ".avsubs" / "tmp" / "audio.wav"
    junk.parent.mkdir(parents=True, exist_ok=True)
    junk.write_bytes(b"x")
    r.inst.start(r.emit)  # only the translator was alive
    assert old_tr.cancel_calls == 1 and old_tr.joined
    assert r.inst._run[0] == "j1" and r.inst._run[1] > old_run[1]
    assert not stale.exists() and keep.exists()
    assert not junk.exists()
    new_tr = r.translators[1]
    # a result from the previous generation is dropped
    new_tr.results.put(
        TranslateResult(old_run, "d.mp4", "translated", (Cue(1, 0, 1000, "旧"),), "")
    )
    r.inst.check(r.emit)
    assert not _zh(r, "d.mp4").exists()
    assert "d.mp4" not in r.inst._settled
    r.inst.stop(timeout=1)


def test_exe_missing_skips_every_job_and_done_still_fires(tmp_path, monkeypatch):
    # D34: _subs_only is emptied too, so `done` can fire after the last restore.
    r = _setup(
        tmp_path,
        monkeypatch,
        pending=["a.mp4", "b.mp4"],
        restored=["e.mp4"],
        exe=False,
    )
    r.inst.start(r.emit)
    assert len(_keyed(r.evs, "j1:subs-noexe")) == 1
    assert all(v == ("skipped", "no_exe") for v in r.inst._settled.values())
    assert len(r.inst._settled) == 3
    r.inst.check(r.emit)
    assert not _done(r.evs)
    r.inst.check(r.emit)
    done = _done(r.evs)
    assert len(done) == 1
    assert (
        "Queue: 3/3 done, 0 failed | Subs: 0/3 done, 0 failed, 3 skipped"
        in (done[0][2])
    )
    assert r.spawner.argvs == [] and r.launcher.n == 2
    assert len(_keyed(r.evs, "j1:subs-noexe")) == 1


def test_exe_missing_with_only_subs_work_is_done_on_the_first_check(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, restored=["e.mp4"], exe=False)
    r.inst.start(r.emit)
    st = r.inst.check(r.emit)
    done = _done(r.evs)
    assert len(done) == 1 and "Subs: 0/1 done, 0 failed, 1 skipped" in done[0][2]
    assert st.state == "idle"
    assert r.spawner.argvs == []


# ── metrics ───────────────────────────────────────────────────────────────
def test_metrics_contract_with_and_without_av_translate(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"], restored=["d.mp4"])
    r.inst.start(r.emit)
    st = r.inst.check(r.emit)  # a restored → ASR a
    m = st.metrics
    for k in (
        "subs_total",
        "subs_completed",
        "subs_failed",
        "subs_skipped",
        "subs_remaining",
        "subs_translating",
    ):
        assert isinstance(m[k], int), k
    assert m["subs_total"] == 2 and m["subs_remaining"] == 2
    assert m["phase"] == "subs"
    r.inst.stop(timeout=1)

    off = tmp_path / "off"
    off.mkdir()
    r2 = _setup(off, monkeypatch, pending=["a.mp4"], av_translate=False, rcs=[None])
    r2.inst.start(r2.emit)
    m2 = r2.inst.check(r2.emit).metrics
    assert m2["phase"] == "restore" and m2["current_file"] == "a.mp4"
    assert not [k for k in m2 if k.startswith("subs_")]
    assert r2.translators == []
    r2.inst.stop(timeout=1)


# ── real supervisor paths (D11) ───────────────────────────────────────────
def test_supervisor_unregister_register_and_reconfigure(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, restored=["e.mp4"], poll_interval=1.0)
    sink: list = []
    sup = Supervisor(sink=lambda *a: sink.append(a))
    plugin = JasnaPlugin()
    sup.start()
    try:
        sup.register(plugin, r.cfg, "j1")
        assert _wait(lambda: len(r.spawner.children) == 1)
        old = sup._monitors["j1"].instance
        old_child, old_tr = r.spawner.children[0], r.translators[0]

        cycled = threading.Event()

        def cycle():
            sup.unregister("j1", timeout=5)
            sup.register(plugin, r.cfg, "j1")  # admin.set_enabled's re-Start
            cycled.set()

        t = threading.Thread(target=cycle, daemon=True)
        t.start()
        t.join(timeout=15)
        assert cycled.is_set()  # immediate re-Start does not deadlock
        assert "terminate_tree" in old_child.calls
        assert old_tr.cancel_calls == 1 and old_tr.joined
        assert _wait(lambda: len(r.spawner.children) == 2)
        new = sup._monitors["j1"].instance
        assert new is not old and new._run[1] > old._run[1]
        new_tr = r.translators[1]
        new_tr.results.put(
            TranslateResult(old._run, "e.mp4", "translated", (Cue(1, 0, 9, "旧"),), "")
        )
        assert _wait(lambda: new_tr.results.empty())
        time.sleep(0.2)
        assert not _zh(r, "e.mp4").exists()
        assert "e.mp4" not in new._settled

        cfg2 = r.cfg.model_copy(update={"whisperjav_engine": "large-v3"})
        sup.reconfigure("j1", cfg2)
        assert "terminate_tree" in r.spawner.children[1].calls
        assert new_tr.cancel_calls == 1 and new_tr.joined
        assert _wait(lambda: len(r.spawner.children) == 3)
        newest = sup._monitors["j1"].instance
        assert newest._run[1] > new._run[1]
        assert "large-v3" in r.spawner.children[2].argv
    finally:
        sup.stop(timeout=5)
    assert "terminate_tree" in r.spawner.children[-1].calls


def test_default_spawn_goes_through_the_module_level_child_process(
    tmp_path, monkeypatch
):
    # D10: J.ChildProcess is the seam for supervisor-created instances.
    r = _setup(tmp_path, monkeypatch, restored=["e.mp4"])
    fresh = JasnaInstance("j2", r.cfg)
    evs, emit = _events()
    fresh.start(emit)
    assert len(r.spawner.argvs) == 1
    fresh.stop(timeout=1)


def test_subs_job_is_a_plain_subs_job_keyed_by_video_name(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    r.inst.start(r.emit)
    job = r.inst._jobs["a.mp4"]
    assert isinstance(job, SubsJob)
    assert job.run == r.inst._run
    assert job.media == r.out / "a-破解.mp4"
    assert job.ja_target == r.out / "a-破解.ja.srt"
    assert job.zh_target == r.out / "a-破解.srt"
    assert job.staging_root == r.out / ".avsubs"
    assert os.path.basename(job.exe) == "whisperjav.exe"
    r.inst.stop(timeout=1)


def test_asr_child_env_never_carries_the_llm_key(monkeypatch):
    # AC10: the key travels only in the llm-worker's env — never the ASR child's.
    seen: dict = {}

    def fake_child(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return object()

    monkeypatch.setenv("TASKPAW_LLM_API_KEY", "sk-secret")
    monkeypatch.setenv("TASKPAW_LLM_API_BASE", "https://x.invalid/v1")
    monkeypatch.setenv("TASKPAW_KEEP_ME", "1")
    monkeypatch.setattr(J, "ChildProcess", fake_child)
    J._default_spawn(["whisperjav.exe", "m.mp4"])
    env = seen["kw"]["env"]
    assert "TASKPAW_LLM_API_KEY" not in env and "TASKPAW_LLM_API_BASE" not in env
    assert env["TASKPAW_KEEP_ME"] == "1"
    assert "sk-secret" not in " ".join(seen["argv"])
    assert "stdin_pipe" not in seen["kw"]  # stdin stays DEVNULL (D15)


def test_long_failure_detail_keeps_its_head_and_is_bounded():
    text = "exit code 1: " + "x" * 5000 + " LAST"
    out = J._bounded(text)
    assert out.startswith("exit code 1: ") and out.endswith(" LAST")
    assert len(out) <= J._CRASH_DETAIL_CHARS


# ── #177 repair batch ─────────────────────────────────────────────────────
def test_translation_settles_after_a_restore_launch_error(tmp_path, monkeypatch):
    # S1: settlement runs before the launch-error return, and a dispatch after a
    # launch error never relaunches.
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"])
    calls = {"n": 0}

    def popen(argv, creationflags=0, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("jasna blocked")
        return r.launcher(argv, creationflags, **kw)

    monkeypatch.setattr(J.subprocess, "Popen", popen)
    inst, emit = r.inst, r.emit
    inst.start(emit)
    inst.check(emit)  # a restored → ASR a
    r.spawner.last.finish(0)
    st = inst.check(emit)  # ja published, tA submitted; b's launch raises
    assert st.state == "error" and "failed to launch jasna" in st.detail
    assert [q.job_id for q in r.translators[0].submitted] == ["a.mp4"]
    assert calls["n"] == 2
    r.translators[0].answer("a.mp4")
    st = inst.check(emit)
    assert _zh(r, "a.mp4").exists()
    assert inst._settled["a.mp4"][0] == "completed"
    assert inst._subs_completed == 1
    assert st.state == "error"
    assert calls["n"] == 2  # no new launch
    inst.check(emit)
    assert calls["n"] == 2 and not _done(r.evs)
    inst.stop(timeout=1)


def test_stop_during_start_never_leaves_an_idle_translator(tmp_path, monkeypatch):
    # IR-b: a Stop that lands while start() creates the translator cancels and
    # joins it right away.
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    made: list[_FakeTranslator] = []

    class _StopOnStart(_FakeTranslator):
        def start(self) -> None:
            super().start()
            r.inst._stopping.set()  # a concurrent Stop

    def factory(run, *, name, **k):
        t = _StopOnStart(run, name=name)
        made.append(t)
        return t

    monkeypatch.setattr(J, "Translator", factory)
    r.inst.start(r.emit)
    tr = made[0]
    assert tr.cancel_calls == 1 and tr.joined
    assert not tr.is_alive()
    assert r.launcher.n == 0 and r.spawner.argvs == []


def test_degraded_snapshot_never_shows_a_stale_phase(tmp_path, monkeypatch):
    # IR-e: the degraded snapshot's phase is recomputed from the live children.
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4", "c.mp4"], rcs=[1] * 6)
    inst, emit = r.inst, r.emit
    inst.start(emit)
    for _ in range(5):
        inst.check(emit)
    inst._phase = "translate"  # as left by a translation phase
    st = inst.check(emit)  # the third file's final failure → abort
    assert st.state == "degraded"
    assert st.metrics["phase"] == "restore"
    inst._phase = "translate"
    st = inst.check(emit)  # the abort short-circuit path
    assert st.state == "degraded" and st.metrics["phase"] == "restore"


# ── #177 repair cycle 2 ───────────────────────────────────────────────────
def test_translator_is_assigned_before_the_post_start_stop_recheck(
    tmp_path, monkeypatch
):
    # CX2: a Stop landing after the re-check must still find the translator, so
    # the assignment has to precede the re-check.
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    seen: list[bool] = []

    class _StopOnStart(_FakeTranslator):
        def start(self) -> None:
            super().start()
            seen.append(r.inst._translator is self)
            r.inst._stopping.set()

    made: list[_FakeTranslator] = []

    def factory(run, *, name, **k):
        t = _StopOnStart(run, name=name)
        made.append(t)
        return t

    monkeypatch.setattr(J, "Translator", factory)
    r.inst.start(r.emit)
    assert seen == [True]
    assert made[0].cancel_calls == 1 and made[0].joined
    r.inst.stop(timeout=1)  # stop() sees it too — idempotent, no raise
    assert not made[0].is_alive()


def test_empty_ja_without_a_key_still_gets_its_empty_zh(tmp_path, monkeypatch):
    # CX3: a 0-cue transcript needs no request, so the key is irrelevant.
    r = _setup(tmp_path, monkeypatch, restored=["d.mp4"], key=False)
    _ja(r, "d.mp4").write_bytes(b"")
    r.inst.start(r.emit)
    r.inst.check(r.emit)
    assert _zh(r, "d.mp4").exists() and _zh(r, "d.mp4").read_bytes() == b""
    assert r.inst._settled["d.mp4"] == ("completed", "no speech")
    assert not _keyed(r.evs, "j1:subs-nokey")
    done = _done(r.evs)
    assert len(done) == 1 and "Subs: 1/1 done, 0 failed, 0 skipped" in done[0][2]


def test_done_releases_the_translator_and_stop_start_still_work(tmp_path, monkeypatch):
    # F2: no idle translator thread / llm-worker survives a finished run.
    r = _setup(tmp_path, monkeypatch, restored=["d.mp4"], ja=["d.mp4"])
    r.inst.start(r.emit)
    tr = r.translators[0]
    tr.answer("d.mp4")
    st = r.inst.check(r.emit)
    assert len(_done(r.evs)) == 1
    assert tr.cancel_calls == 1 and tr.joined
    assert r.inst._translator is None
    assert st.state == "idle" and st.metrics["subs_translating"] == 0
    r.inst.check(r.emit)  # settling with no translator is fine
    r.inst.stop(timeout=1)  # no raise
    assert tr.cancel_calls == 1
    _zh(r, "d.mp4").unlink()
    r.inst.start(r.emit)  # a fresh run gets a fresh translator
    assert len(r.translators) == 2 and r.inst._translator is r.translators[1]
    assert r.translators[1].started
    r.inst.stop(timeout=1)


def test_extra_args_and_av_translate_descriptions_state_the_rules():
    props = JasnaPlugin.json_schema()["properties"]
    extra = props["whisperjav_extra_args"]["description"]
    assert "4" in extra and "any argparse prefix" not in extra
    av = props["av_translate"]["description"]
    assert "delete its old .srt files first" in av


# ── #189: per-film progress (film / steps / films / model, queue counts) ──────
# Observation only: the stepper metrics are derived at status time from the
# tracker marks at the existing counter points; with「AV 翻译」on the queue's
# `queue_completed` counts fully-done films (D1) and the detail follows (D6).
QWEN_TAIL = (
    "[QwenPipeline PID 4242] Phase 1: extracting audio\n"
    "[QwenPipeline PID 4242] Phase 5: decoupled ASR\n"
    "[DecoupledPipeline] Generating scene 3/10 (41.0s)\n"
)
_TQDM = (
    "Processing video:  57%|███ |Processed: 06:09 (36703f) | "
    "Remaining: 7:18 (207454f) | Speed: 157.0fps\n"
)
_NEW_KEYS = ("film", "steps", "films", "films_more", "model", "queue_restored")
_OTHER = ("other", 1)
_LIVE_TR = {
    "job_id": "a.mp4",
    "model": "grok-4.3 · api.x.ai",
    "batches_done": 1,
    "batches_total": 2,
    "cues_done": 40,
    "cues_total": 80,
    "started_at": 12.5,
    "elapsed_s": 30,
    "percent": 50,
    "eta_s": 30,
}


@pytest.fixture
def lease_clock():
    """A frozen lease clock: no reservation expires unless a test moves it."""
    t = [1000.0]
    gpu_lease._reset_for_tests(clock=lambda: t[0])
    return t


def _mono(monkeypatch, t: float = 100.0) -> SimpleNamespace:
    """Freeze `time.monotonic` as the plugin module sees it (only J's `time`)."""
    clk = SimpleNamespace(t=t)
    fake = SimpleNamespace(monotonic=lambda: clk.t, time=time.time, sleep=time.sleep)
    monkeypatch.setattr(J, "time", fake)
    return clk


def _step(m: dict, key: str) -> dict:
    return next(s for s in m["steps"] if s["key"] == key)


def _row(m: dict, name: str) -> dict:
    return next(f for f in m["films"] if f["name"] == name)


def _rows(m: dict) -> list:
    return [(f["name"], f["status"]) for f in m["films"]]


def _states(inst, name: str) -> dict:
    rec = inst._tracker.record(name)
    return {k: s["state"] for k, s in rec["steps"].items()}


def _qc(m: dict) -> tuple:
    """(queue_restored, queue_completed, queue_remaining, queue_failed)."""
    return (
        m["queue_restored"],
        m["queue_completed"],
        m["queue_remaining"],
        m["queue_failed"],
    )


def test_progress_av_off_emits_no_new_keys_and_keeps_the_restore_counts(
    tmp_path, monkeypatch
):
    r = _setup(
        tmp_path,
        monkeypatch,
        pending=["a.mp4", "b.mp4"],
        av_translate=False,
        rcs=[0, None],
    )
    r.inst.start(r.emit)
    st = r.inst.check(r.emit)  # a restored, b restoring
    assert not [k for k in _NEW_KEYS if k in st.metrics]
    assert st.metrics["queue_completed"] == 1 and st.metrics["queue_remaining"] == 1
    assert st.detail.endswith(" · 1/2 done")
    r.inst.stop(timeout=1)


def test_progress_restore_active_with_capture_numbers_and_queue_counts(
    tmp_path, monkeypatch
):
    r = _setup(
        tmp_path,
        monkeypatch,
        pending=["a.mp4"],
        rcs=[None],
        jasna_capture_progress=True,
    )
    r.launcher._output = _TQDM
    r.inst.start(r.emit)
    assert _wait(lambda: r.inst._progress.get("percent") == 57)
    st = r.inst.check(r.emit)
    m = st.metrics
    assert m["film"] == "a.mp4"
    assert m["steps"] == [
        {
            "key": "restore",
            "state": "active",
            "percent": 57,
            "eta_s": 438,
            "elapsed_s": 369,
        },
        {"key": "asr", "state": "pending"},
        {"key": "translate", "state": "pending"},
    ]
    assert m["films"] == [
        {
            "name": "a.mp4",
            "steps": {"restore": "active", "asr": "pending", "translate": "pending"},
            "status": "active",
            "percent": 57,
            "eta_s": 438,
            "duration_s": None,
        }
    ]
    assert m["films_more"] == 0 and "model" not in m
    assert _qc(m) == (0, 0, 1, 0)
    assert m["percent"] == 57 and m["eta"] == "7:18"  # the capture keys stay
    assert st.detail.startswith("running: a.mp4 ")
    assert st.detail.endswith(" · 0/1 done")
    r.inst.stop(timeout=1)


def test_progress_restore_active_without_capture_has_no_numbers(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"], rcs=[None])
    r.inst.start(r.emit)
    m = r.inst.check(r.emit).metrics
    assert m["film"] == "a.mp4"
    assert m["steps"][0] == {"key": "restore", "state": "active"}
    assert _rows(m) == [("a.mp4", "active"), ("b.mp4", "pending")]
    assert _row(m, "a.mp4")["percent"] is None
    assert _qc(m) == (0, 0, 2, 0)
    r.inst.stop(timeout=1)


def test_progress_asr_active_reports_whisperjav_phase_and_scene(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    r.inst.start(r.emit)
    r.inst.check(r.emit)  # a restored → ASR a
    r.spawner.last.tail = lambda lines=10, max_chars=800: QWEN_TAIL
    st = r.inst.check(r.emit)
    m = st.metrics
    expect = AsrProgress(0.0)
    expect.feed_text(QWEN_TAIL, 1.0)
    pct = expect.snapshot(1.0)["percent"]
    assert m["film"] == "a.mp4" and m["current_file"] == "a-破解.mp4"
    restore, asr, tr = m["steps"]
    assert restore["state"] == "done" and isinstance(restore["duration_s"], int)
    assert asr["key"] == "asr" and asr["state"] == "active"
    assert (asr["phase"], asr["phase_n"], asr["scene"], asr["scenes"]) == (5, 8, 3, 10)
    assert asr["percent"] == pct and 0 < pct < 99
    assert isinstance(asr["elapsed_s"], int)
    assert "eta_s" not in asr  # no ETA yet: absent, never null
    assert tr == {"key": "translate", "state": "pending"}
    row = _row(m, "a.mp4")
    assert row["status"] == "active" and row["percent"] == pct
    assert _qc(m) == (1, 0, 1, 0)
    assert st.detail.startswith("subtitling: a-破解.mp4 [anime-whisper] · ")
    r.inst.stop(timeout=1)


def test_progress_translate_active_shows_the_model_and_only_design_keys(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    inst.check(emit)  # ASR a
    r.spawner.last.finish(0)
    st = inst.check(emit)  # ja published, a submitted → queued (N2)
    assert _step(st.metrics, "asr")["state"] == "done"
    assert _step(st.metrics, "translate") == {"key": "translate", "state": "queued"}
    assert _row(st.metrics, "a.mp4")["status"] == "queued"
    assert "model" not in st.metrics
    tr = r.translators[0]
    tr.live = dict(_LIVE_TR)
    st = inst.check(emit)
    m = st.metrics
    assert m["film"] == "a.mp4" and m["model"] == "grok-4.3 · api.x.ai"
    assert _step(m, "translate") == {
        "key": "translate",
        "state": "active",
        "model": "grok-4.3 · api.x.ai",
        "batches_done": 1,
        "batches_total": 2,
        "cues_done": 40,
        "cues_total": 80,
        "elapsed_s": 30,
        "percent": 50,
        "eta_s": 30,
    }  # never the request's job_id / started_at
    row = _row(m, "a.mp4")
    assert (row["status"], row["percent"], row["eta_s"]) == ("active", 50, 30)
    assert _qc(m) == (1, 0, 1, 0)
    assert st.detail == "translating 1 · subs 0/1"
    tr.live = None  # the request ends
    tr.answer("a.mp4")
    st = inst.check(emit)
    m = st.metrics
    assert "model" not in m
    assert [s["state"] for s in m["steps"]] == ["done", "done", "done"]
    assert all(isinstance(s.get("duration_s"), int) for s in m["steps"])
    assert _row(m, "a.mp4")["status"] == "done"
    assert _qc(m) == (1, 1, 0, 0)
    assert st.detail == "idle · 1/1 done"
    assert "Queue: 1/1 done, 0 failed" in _done(r.evs)[0][2]


def test_progress_batch_one_film_translating_while_the_next_restores(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"], rcs=[0, None])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    inst.check(emit)  # a restored → ASR a
    r.spawner.last.finish(0)
    inst.check(emit)  # a submitted, b launched
    r.translators[0].live = dict(_LIVE_TR)
    st = inst.check(emit)
    m = st.metrics
    assert m["film"] == "b.mp4"  # the GPU child wins the focus
    assert _step(m, "restore")["state"] == "active"
    assert m["model"] == "grok-4.3 · api.x.ai"
    assert _rows(m) == [("a.mp4", "active"), ("b.mp4", "active")]
    a = _row(m, "a.mp4")
    assert a["steps"] == {"restore": "done", "asr": "done", "translate": "active"}
    assert (a["percent"], a["eta_s"]) == (50, 30)
    assert _row(m, "b.mp4")["steps"] == {
        "restore": "active",
        "asr": "pending",
        "translate": "pending",
    }
    assert _qc(m) == (1, 0, 2, 0)
    # D6: the detail's "X/Y done" is the fully-done count, not the restores
    assert st.detail.endswith(" · translating 1 · 0/2 done")
    inst.stop(timeout=1)


def test_progress_translate_only_film_is_pending_until_submitted(tmp_path, monkeypatch):
    # N2: an existing .ja.srt makes asr `done` at add (no duration); translate
    # stays `pending` until translator.submit, `queued` after.
    r = _setup(tmp_path, monkeypatch, pending=["b.mp4"], ja=["b.mp4"], rcs=[None])
    r.inst.start(r.emit)
    m = r.inst.check(r.emit).metrics
    assert _row(m, "b.mp4")["steps"] == {
        "restore": "active",
        "asr": "done",
        "translate": "pending",
    }
    assert "duration_s" not in _step(m, "asr")
    r.launcher.procs[0]._rc = 0
    m = r.inst.check(r.emit).metrics  # restored → submitted
    assert _row(m, "b.mp4")["steps"] == {
        "restore": "done",
        "asr": "done",
        "translate": "queued",
    }
    assert _row(m, "b.mp4")["status"] == "queued"


def test_progress_waiting_gpu_holder_waited_s_and_reset(
    tmp_path, monkeypatch, lease_clock
):
    clk = _mono(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"], rcs=[None, None])
    inst, emit = r.inst, r.emit
    assert gpu_lease.try_acquire(_OTHER, 1.0, label="Other")
    inst.start(emit)  # refused → waits
    st = inst.check(emit)
    assert st.state == "idle" and st.metrics["film"] == "a.mp4"
    assert _step(st.metrics, "restore") == {
        "key": "restore",
        "state": "waiting_gpu",
        "holder": "Other",
        "waited_s": 0,
    }
    assert _rows(st.metrics) == [("a.mp4", "waiting_gpu"), ("b.mp4", "pending")]
    clk.t += 30
    st = inst.check(emit)  # N6: the refusal flag toggles inside check()
    assert _step(st.metrics, "restore")["waited_s"] == 30
    clk.t += 15
    assert gpu_lease.release(_OTHER)
    assert gpu_lease.reserved_for() == inst._run
    st = inst._build_status("idle")  # N5: free and reserved for THIS run
    assert _step(st.metrics, "restore") == {
        "key": "restore",
        "state": "waiting_gpu",
        "holder": "",
        "waited_s": 45,
    }
    assert st.detail == "waiting for GPU · 0/2 done · subs 0/2"
    st = inst.check(emit)  # takes it: a restores, the wait is over
    assert _step(st.metrics, "restore") == {"key": "restore", "state": "active"}
    assert not gpu_lease.try_acquire(_OTHER, 1.0, label="Other")  # Other waits
    r.launcher.procs[0]._rc = 0
    inst.check(emit)  # a restored → a's ASR on the same hold
    r.spawner.last.finish(0)
    clk.t += 100
    st = inst.check(emit)  # a's GPU work ends → reserved for Other → b waits
    assert inst._gpu_waiting and st.metrics["film"] == "b.mp4"
    assert _step(st.metrics, "restore") == {
        "key": "restore",
        "state": "waiting_gpu",
        "holder": "Other",
        "waited_s": 0,  # a new wait starts from zero
    }
    clk.t += 5
    assert _step(inst.check(emit).metrics, "restore")["waited_s"] == 5
    inst.stop(timeout=1)


def test_progress_subs_only_head_waits_for_the_gpu_on_its_asr_step(
    tmp_path, monkeypatch, lease_clock
):
    # Nothing to restore: the queue head is a subs-only film's ASR.
    clk = _mono(monkeypatch)
    r = _setup(tmp_path, monkeypatch, restored=["e.mp4"])
    inst, emit = r.inst, r.emit
    assert gpu_lease.try_acquire(_OTHER, 1.0, label="Other")
    inst.start(emit)  # refused → waits
    st = inst.check(emit)
    assert inst._gpu_waiting and not inst._pending
    assert [p.name for p in inst._subs_only] == ["e.mp4"]
    assert st.state == "idle" and st.metrics["film"] == "e.mp4"
    assert _step(st.metrics, "restore") == {"key": "restore", "state": "done"}
    assert _step(st.metrics, "asr") == {
        "key": "asr",
        "state": "waiting_gpu",
        "holder": "Other",
        "waited_s": 0,
    }
    assert _rows(st.metrics) == [("e.mp4", "waiting_gpu")]
    clk.t += 20
    st = inst.check(emit)
    assert _step(st.metrics, "asr")["waited_s"] == 20
    clk.t += 5
    st = inst.check(emit)
    assert _step(st.metrics, "asr") == {
        "key": "asr",
        "state": "waiting_gpu",
        "holder": "Other",
        "waited_s": 25,
    }
    inst.stop(timeout=1)


def test_progress_mixed_run_counts_rows_and_the_row_count_mapping(
    tmp_path, monkeypatch
):
    # a: restore fails; b: translation fails; c: kind none (zh exists);
    # d: translate-only; e: subs-only; y.mkv: restored with zh (counted, not
    # tracked); y.mp4: an output-name collision (counted, not tracked) — W1.
    r = _setup(
        tmp_path,
        monkeypatch,
        pending=["a.mp4", "b.mp4", "c.mp4", "d.mp4"],
        restored=["e.mp4", "y.mkv", "y.mp4"],
        zh=["c.mp4", "y.mkv"],
        ja=["d.mp4"],
        rcs=[1, 1, 0, 0, 0],
    )
    inst, emit = r.inst, r.emit
    inst.start(emit)
    assert sorted(inst._jobs) == ["a.mp4", "b.mp4", "d.mp4", "e.mp4"]
    assert [f["name"] for f in inst._tracker.view(LiveFacts(), 0.0)["films"]] == [
        "a.mp4",
        "b.mp4",
        "c.mp4",
        "d.mp4",
        "e.mp4",
    ]  # plan order: pending (kind none included), then subs-only
    inst.check(emit)  # a: plain retry
    inst.check(emit)  # a failed → b launched
    inst.check(emit)  # b restored → ASR b
    r.spawner.last.finish(0)
    st = inst.check(emit)  # b submitted → c launched
    m = st.metrics
    assert _rows(m) == [
        ("a.mp4", "failed"),
        ("b.mp4", "queued"),
        ("c.mp4", "active"),
        ("d.mp4", "pending"),
        ("e.mp4", "pending"),
    ]
    # restored: y.mkv, e, b; b and e still unsettled → 1 fully done (y.mkv)
    assert _qc(m) == (3, 1, 4, 2)
    assert st.detail.endswith(" · translating 1 · 1/7 done")
    inst.check(emit)  # c restored (no job) → d launched
    inst.check(emit)  # d restored → d submitted; subs-only e → ASR e
    r.spawner.last.finish(0)
    inst.check(emit)  # e submitted
    tr = r.translators[0]
    tr.answer("b.mp4", ok=False)
    tr.answer("d.mp4")
    tr.answer("e.mp4")
    st = inst.check(emit)
    m = st.metrics
    assert _qc(m) == (5, 5, 0, 2)
    statuses = inst._tracker.statuses(LiveFacts())
    assert statuses == {
        "a.mp4": "failed",
        "b.mp4": "failed",
        "c.mp4": "done",
        "d.mp4": "done",
        "e.mp4": "done",
    }
    assert {n: _states(inst, n) for n in statuses} == {
        "a.mp4": {"restore": "failed", "asr": "skipped", "translate": "skipped"},
        "b.mp4": {"restore": "done", "asr": "done", "translate": "failed"},
        "c.mp4": {"restore": "done", "asr": "skipped", "translate": "skipped"},
        "d.mp4": {"restore": "done", "asr": "done", "translate": "done"},
        "e.mp4": {"restore": "done", "asr": "done", "translate": "done"},
    }
    # the row ↔ count mapping (documented in the openclaw guide)
    plan_done, subs_only, collisions = 2, 1, 1
    restored_terminal = [
        n
        for n, s in statuses.items()
        if _states(inst, n)["restore"] == "done" and s in ("done", "failed", "skipped")
    ]
    restore_failed = [n for n in statuses if _states(inst, n)["restore"] == "failed"]
    assert m["queue_completed"] - (plan_done - subs_only) == len(restored_terminal)
    assert m["queue_failed"] - collisions == len(restore_failed) == 1
    done = _done(r.evs)
    assert len(done) == 1
    assert (
        "Queue: 5/7 done, 2 failed | Subs: 2/4 done, 1 failed, 1 skipped" in done[0][2]
    )


@pytest.mark.parametrize(
    "outcome", ["completed", "asr_failed", "unstable", "no_key", "translate_failed"]
)
def test_progress_queue_completed_counts_a_film_once_its_job_settles(
    tmp_path, monkeypatch, outcome
):
    # D1: a settled job of ANY outcome makes its restored film fully done.
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"], key=outcome != "no_key")
    inst, emit = r.inst, r.emit
    inst.start(emit)
    st = inst.check(emit)  # a restored → ASR a
    assert _qc(st.metrics) == (1, 0, 1, 0)
    if outcome == "asr_failed":
        r.spawner.last.finish(1)
        inst.check(emit)  # retry
        r.spawner.last.finish(1)
    elif outcome == "unstable":
        (r.out / "a-破解.mp4").write_bytes(b"changed underneath, longer")
        r.spawner.last.finish(0)
    else:
        r.spawner.last.finish(0)
    st = inst.check(emit)
    if outcome in ("completed", "translate_failed"):
        assert _qc(st.metrics) == (1, 0, 1, 0)  # still translating
        r.translators[0].answer("a.mp4", ok=outcome == "completed")
        st = inst.check(emit)
    assert _qc(st.metrics) == (1, 1, 0, 0)
    assert st.detail == "idle · 1/1 done"
    row = _row(st.metrics, "a.mp4")
    assert (row["status"], row["steps"]) == {
        "completed": ("done", {"restore": "done", "asr": "done", "translate": "done"}),
        "asr_failed": (
            "failed",
            {"restore": "done", "asr": "failed", "translate": "skipped"},
        ),
        "unstable": (
            "skipped",
            {"restore": "done", "asr": "skipped", "translate": "skipped"},
        ),
        "no_key": (
            "skipped",
            {"restore": "done", "asr": "done", "translate": "skipped"},
        ),
        "translate_failed": (
            "failed",
            {"restore": "done", "asr": "done", "translate": "failed"},
        ),
    }[outcome]
    done = _done(r.evs)
    assert len(done) == 1 and "Queue: 1/1 done, 0 failed" in done[0][2]


def test_progress_no_exe_at_start_skips_subtitles_while_restores_progress(
    tmp_path, monkeypatch
):
    r = _setup(
        tmp_path,
        monkeypatch,
        pending=["a.mp4", "b.mp4"],
        restored=["e.mp4"],
        exe=False,
        rcs=[0, None],
    )
    inst, emit = r.inst, r.emit
    inst.start(emit)  # every job skipped(no_exe) before any restore finished
    for n in ("a.mp4", "b.mp4", "e.mp4"):
        s = _states(inst, n)
        assert (s["asr"], s["translate"]) == ("skipped", "skipped"), n
    assert _states(inst, "a.mp4")["restore"] == "pending"  # never touched (N1)
    assert _states(inst, "e.mp4")["restore"] == "done"
    st = inst.check(emit)  # a restored, b restoring
    m = st.metrics
    assert _rows(m) == [
        ("a.mp4", "skipped"),
        ("b.mp4", "active"),  # R1: settled early, restore still active
        ("e.mp4", "skipped"),
    ]
    assert m["film"] == "b.mp4"
    assert [s["state"] for s in m["steps"]] == ["active", "skipped", "skipped"]
    assert _qc(m) == (2, 2, 1, 0)
    r.launcher.procs[1]._rc = 0
    st = inst.check(emit)
    assert _row(st.metrics, "b.mp4")["status"] == "skipped"
    assert _qc(st.metrics) == (3, 3, 0, 0)
    assert "Queue: 3/3 done, 0 failed" in _done(r.evs)[0][2]


def test_progress_translator_start_failure_skips_subtitles_restores_continue(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"], restored=["e.mp4"], rcs=[None])

    def no_thread(run, *, name, **k):
        raise RuntimeError("no thread")

    monkeypatch.setattr(J, "Translator", no_thread)
    r.inst.start(r.emit)
    assert r.inst._subs_disabled.startswith("translator did not start")
    m = r.inst.check(r.emit).metrics
    assert _rows(m) == [("a.mp4", "active"), ("e.mp4", "skipped")]
    assert _row(m, "a.mp4")["steps"] == {
        "restore": "active",
        "asr": "skipped",
        "translate": "skipped",
    }
    r.launcher.procs[0]._rc = 0
    m = r.inst.check(r.emit).metrics
    assert _row(m, "a.mp4")["status"] == "skipped"
    assert _qc(m) == (2, 2, 0, 0)


def test_progress_disable_subs_mid_batch_marks_skipped_not_done(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"], rcs=[0, None])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    inst.check(emit)  # a restored → ASR a
    with inst._launch_lock:
        inst._disable_subs("test", emit)
    inst._run_deferred()  # kills a's ASR, requests the advance
    inst._dispatch(emit)  # b launched
    m = inst.check(emit).metrics
    a = _row(m, "a.mp4")
    assert a["steps"] == {"restore": "done", "asr": "skipped", "translate": "skipped"}
    assert a["status"] == "skipped"  # R1: skipped, never `done`
    b = _row(m, "b.mp4")
    assert b["steps"]["restore"] == "active" and b["status"] == "active"
    r.launcher.procs[1]._rc = 0
    m = inst.check(emit).metrics
    assert _row(m, "b.mp4")["status"] == "skipped"
    assert _qc(m) == (2, 2, 0, 0)


def test_progress_kind_none_film_is_done_once_restored(tmp_path, monkeypatch):
    # M2: an existing zh → no job; asr/translate skipped at add.
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"], zh=["a.mp4"], rcs=[None])
    r.inst.start(r.emit)
    assert r.inst._jobs == {}
    m = r.inst.check(r.emit).metrics
    assert _row(m, "a.mp4") == {
        "name": "a.mp4",
        "steps": {"restore": "active", "asr": "skipped", "translate": "skipped"},
        "status": "active",
        "percent": None,
        "eta_s": None,
        "duration_s": None,
    }
    r.launcher.procs[0]._rc = 0
    m = r.inst.check(r.emit).metrics
    assert _row(m, "a.mp4")["status"] == "done"
    assert _qc(m) == (1, 1, 0, 0)


def test_progress_planning_failure_adds_every_pending_film_with_subs_skipped(
    tmp_path, monkeypatch
):
    # R2
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"], rcs=[0, 0])

    def raced(*a, **k):
        raise OSError("raced")

    monkeypatch.setattr(J, "plan_subs", raced)
    r.inst.start(r.emit)
    assert r.inst._subs_disabled == "planning failed"
    for n in ("a.mp4", "b.mp4"):
        assert _states(r.inst, n) == {
            "restore": "pending",
            "asr": "skipped",
            "translate": "skipped",
        }
    r.inst.check(r.emit)  # a restored → b launched
    m = r.inst.check(r.emit).metrics
    assert _rows(m) == [("a.mp4", "done"), ("b.mp4", "done")]
    assert _qc(m) == (2, 2, 0, 0)
    assert len(_done(r.evs)) == 1


def test_progress_abort_keeps_the_restore_based_texts(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4", "c.mp4"], rcs=[1] * 6)
    inst, emit = r.inst, r.emit
    inst.start(emit)
    for _ in range(6):
        st = inst.check(emit)
    assert st.state == "degraded"
    assert st.detail == (
        "batch aborted after 3 consecutive failures · 0/3 done, 3 failed"
    )
    assert _rows(st.metrics) == [
        ("a.mp4", "failed"),
        ("b.mp4", "failed"),
        ("c.mp4", "failed"),
    ]
    assert _qc(st.metrics) == (0, 0, 0, 3)
    aborted = [e for e in r.evs if e[1].endswith("batch aborted")]
    assert aborted and aborted[0][2].endswith("| Queue: 0/3 done, 3 failed")


def test_progress_restart_takes_a_fresh_tracker(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    inst.check(emit)  # a restored → ASR a
    old = inst._tracker
    assert _states(inst, "a.mp4")["restore"] == "done"
    inst.stop(timeout=1)
    inst.start(emit)  # a is restored now: a subs-only film of the new run
    assert inst._tracker is not old
    m = inst.check(emit).metrics
    assert m["film"] == "a.mp4" and _step(m, "asr")["state"] == "active"
    assert _step(m, "restore") == {"key": "restore", "state": "done"}  # no duration
    assert _qc(m) == (1, 0, 1, 0)
    inst.stop(timeout=1)


def test_progress_restore_retry_keeps_one_start_stamp_and_one_duration(
    tmp_path, monkeypatch
):
    clk = _mono(monkeypatch)
    r = _setup(
        tmp_path, monkeypatch, pending=["a.mp4"], rcs=[None, None], unet4x_1080p=True
    )
    inst, emit = r.inst, r.emit
    inst.start(emit)  # t=100: the unet-4x launch
    clk.t = 110.0
    inst.check(emit)  # first derived-active poll
    r.launcher.procs[0]._rc = 1
    clk.t = 150.0
    st = inst.check(emit)  # unet-4x failed → the same file relaunched plain
    assert r.launcher.n == 2
    assert _step(st.metrics, "restore")["state"] == "active"
    assert _qc(st.metrics) == (0, 0, 1, 0)  # requeued: not restored, not failed
    r.launcher.procs[1]._rc = 0
    clk.t = 200.0
    st = inst.check(emit)
    assert _qc(st.metrics) == (1, 0, 1, 0)  # restored once; subtitles to do
    rec = inst._tracker.record("a.mp4")["steps"]["restore"]
    assert (rec["state"], rec["started_at"], rec["activated_at"], rec["ended_at"]) == (
        "done",
        100.0,
        110.0,
        200.0,
    )
    assert rec["duration_s"] == 90
    assert len(_keyed(r.evs, "j1:unet:1080p")) == 1
    inst.stop(timeout=1)


@pytest.mark.parametrize("fail_current_raises", [False, True])
def test_progress_exit_internal_error_marks_the_restore_failed(
    tmp_path, monkeypatch, fail_current_raises
):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"], rcs=[0, None])

    def bug(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(r.inst, "_handle_success", bug)
    if fail_current_raises:  # the fence's direct `_failed` bump
        monkeypatch.setattr(r.inst, "_fail_current", bug)
    r.inst.start(r.emit)
    m = r.inst.check(r.emit).metrics
    assert _row(m, "a.mp4")["steps"] == {
        "restore": "failed",
        "asr": "skipped",
        "translate": "skipped",
    }
    assert _row(m, "a.mp4")["status"] == "failed"
    assert _row(m, "b.mp4")["status"] == "active"
    assert _qc(m) == (0, 0, 1, 1)
    r.inst.stop(timeout=1)


def test_progress_fence_internal_direct_write_marks_subtitles_failed(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    inst.check(emit)  # ASR a
    job = inst._jobs["a.mp4"]

    def bug(*a, **k):
        raise RuntimeError("bug")

    job.poll_asr = bug  # type: ignore[method-assign]
    inst._settle = bug  # type: ignore[method-assign]  # `_settle` itself raises
    r.spawner.last.rc = 0
    inst.check(emit)
    del job.poll_asr
    del inst._settle
    assert inst._settled["a.mp4"] == ("failed", "internal: RuntimeError")
    assert _states(inst, "a.mp4") == {
        "restore": "done",
        "asr": "failed",
        "translate": "skipped",
    }
    m = inst.check(emit).metrics
    assert _row(m, "a.mp4")["status"] == "failed"
    assert _qc(m) == (1, 1, 0, 0)


def test_progress_restore_mark_survives_a_raising_failure_alert(tmp_path, monkeypatch):
    # M1: the mark sits before the emit — a sink that raises cannot skip it.
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"], rcs=[1, 1])
    raised: list[str] = []

    def emit(level, title, message, data=None, dedupe_key=None):
        if title.endswith("a.mp4 failed") and not raised:
            raised.append(title)
            raise RuntimeError("sink down")
        r.evs.append((level, title, message, dedupe_key))

    r.inst.start(emit)
    r.inst.check(emit)  # plain retry
    r.inst.check(emit)  # final failure: the alert raises inside _fail_current
    assert raised
    assert _states(r.inst, "a.mp4") == {
        "restore": "failed",
        "asr": "skipped",
        "translate": "skipped",
    }


def test_progress_settle_mark_survives_a_raising_disable_alert(tmp_path, monkeypatch):
    # M1: `_settle` marks before `_disable_subs` can emit.
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    inst = r.inst

    def emit(level, title, message, data=None, dedupe_key=None):
        if "AV 翻译 disabled" in title:
            raise RuntimeError("sink down")
        r.evs.append((level, title, message, dedupe_key))

    inst.start(emit)
    inst.check(emit)  # ASR a
    inst._subs_consecutive_failures = 2
    with inst._launch_lock:
        with pytest.raises(RuntimeError):
            inst._settle("a.mp4", "failed", "exit code 1", emit)
    assert inst._settled["a.mp4"] == ("failed", "exit code 1")
    assert _states(inst, "a.mp4") == {
        "restore": "done",
        "asr": "failed",
        "translate": "skipped",
    }
    inst.stop(timeout=1)


# ── #191: recognise existing subtitles, fail closed, never overwrite ──────────
LIB_ZH = "1\n00:00:00,000 --> 00:00:01,000\n店主的中文字幕\n"
LIB_JA = SRT_JA.replace("はい", "店主")
_SKIPPED = {"restore": "done", "asr": "skipped", "translate": "skipped"}


def _scandir_spy(monkeypatch, folder: Path, fail=None) -> list[str]:
    """Record `os.scandir(folder)` calls; raise `fail` for them when given."""
    real, key, calls = os.scandir, str(folder), []

    def scandir(path="."):
        if os.fspath(path) == key:
            calls.append(key)
            if fail is not None:
                raise fail
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    return calls


def test_plan_subs_judges_every_film_from_one_output_listing(tmp_path, monkeypatch):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    _videos(inp, *(f"{n}.mp4" for n in "abcdefghi"), "JAP-001.mp4")
    for name, text in {
        "a-破解.mp4": "r",  # restored, nothing yet → full
        "b-破解.mp4": "r",
        "b-破解.chs.srt": LIB_ZH,  # (b)
        "c-破解.mp4": "r",
        "c-破解.srt": "",  # (a), 0 bytes counts
        "d-破解.mp4": "r",
        "d-破解.ja.srt": SRT_JA,  # translate-only
        "e_restored.mp4": "r",
        "e_restored.zh.srt": LIB_ZH,  # legacy media, (b)
        "f_restored.mp4": "r",  # legacy media, nothing yet
        "g-破解.mp4": "r",  # F1: the only restored video here ...
        "h-破解.chs.srt": LIB_ZH,  # ... and pending h's pre-placed subtitle
        "JAP-001-破解.mp4": "r",
        "JAP-001-破解.srt": LIB_ZH,  # F12: a Japanese-looking stem
    }.items():
        (out / name).write_text(text, encoding="utf-8")
    pending, _done_n, collisions = plan_queue(str(inp), str(out))
    assert [p.name for p in pending] == ["h.mp4", "i.mp4"]
    calls = _scandir_spy(monkeypatch, out)
    plan = plan_subs(str(inp), str(out), pending, [a for a, _ in collisions])
    assert calls == [str(out)]  # ONE listing per planning pass (AC3)
    assert {p.name: k for p, k in plan.for_pending.items()} == {
        "h.mp4": "none",
        "i.mp4": "full",
    }
    assert [p.name for p in plan.subs_only] == ["a.mp4", "d.mp4", "f.mp4", "g.mp4"]
    assert {p.name: k for p, k in plan.kinds.items()} == {
        "JAP-001.mp4": "none",
        "a.mp4": "full",
        "b.mp4": "none",
        "c.mp4": "none",
        "d.mp4": "translate_only",
        "e.mp4": "none",
        "f.mp4": "full",
        "g.mp4": "full",  # rule (c) is off for Jasna (C4)
        "h.mp4": "none",
        "i.mp4": "full",
    }
    media = {p.name: m.name for p, m in plan.media.items()}
    assert media["e.mp4"] == "e_restored.mp4" and media["f.mp4"] == "f_restored.mp4"
    assert media["a.mp4"] == "a-破解.mp4" and media["i.mp4"] == "i-破解.mp4"
    assert plan.total == 5


def test_plan_subs_never_credits_another_films_subtitle_f1(tmp_path):
    # C4/F1: the ONLY restored video plus another (pending) film's pre-placed
    # subtitle — rule (c) would credit the wrong film forever; it is off here.
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    (out / ".avsubs").mkdir()
    _videos(inp, "A.mp4", "C.mp4")
    (out / "A-破解.mp4").write_bytes(b"r")
    (out / "C-破解.chs.srt").write_text(LIB_ZH, encoding="utf-8")
    pending, _done_n, _coll = plan_queue(str(inp), str(out))
    assert [p.name for p in pending] == ["C.mp4"]
    plan = plan_subs(str(inp), str(out), pending, [])
    assert plan.kinds == {inp / "A.mp4": "full", inp / "C.mp4": "none"}
    assert plan.subs_only == [inp / "A.mp4"] and plan.total == 1


def test_plan_subs_prefers_the_new_media_name_from_the_listing(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    _videos(inp, "c.mp4")
    (out / "c_restored.mp4").write_bytes(b"legacy")
    (out / "c_restored.srt").write_text(LIB_ZH, encoding="utf-8")
    (out / "C-破解.MP4").write_bytes(b"new")  # case-variant on disk
    plan = plan_subs(str(inp), str(out), [], [])
    if os.path.normcase("A") == os.path.normcase("a"):
        assert plan.media[inp / "c.mp4"] == out / "c-破解.mp4"
        assert plan.kinds[inp / "c.mp4"] == "full"  # the new file's own subs
    else:
        assert plan.media[inp / "c.mp4"] == out / "c_restored.mp4"
        assert plan.kinds[inp / "c.mp4"] == "none"


def test_plan_subs_counts_a_missing_output_folder_as_empty(tmp_path):
    inp = tmp_path / "in"
    inp.mkdir()
    _videos(inp, "a.mp4")
    plan = plan_subs(str(inp), str(tmp_path / "out"), [inp / "a.mp4"], [])
    assert plan.for_pending == {inp / "a.mp4": "full"} and plan.total == 1


@pytest.mark.parametrize("case", ["unreachable", "denied"])
def test_an_unreadable_output_folder_fails_planning(tmp_path, monkeypatch, case):
    # F13: an unreachable share also reads FileNotFoundError on Windows — the
    # parent still lists the folder, so it is not "missing".
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"], restored=["e.mp4"])
    fail = (
        FileNotFoundError(2, "WinError 53")
        if case == "unreachable"
        else PermissionError(13, "denied")
    )
    with monkeypatch.context() as m:
        _scandir_spy(m, r.out, fail)
        r.inst.start(r.emit)
    assert r.inst._subs_disabled == "planning failed"
    assert r.inst._jobs == {}
    assert len(_keyed(r.evs, "j1:subs-disabled")) == 1
    assert r.launcher.n == 1  # restores continue
    r.inst.stop(timeout=1)


def test_setup_subs_uses_the_plan_and_never_probes_again(tmp_path, monkeypatch):
    r = _setup(
        tmp_path,
        monkeypatch,
        pending=["a.mp4"],
        restored=["e.mp4"],
        legacy=["f.mp4"],
        ja=["f.mp4"],
    )
    real = J.plan_subs

    def forbid(*a, **k):
        raise AssertionError("probed again after planning")

    def plan_then_forbid(*a, **k):
        plan = real(*a, **k)
        monkeypatch.setattr(J, "exists_quietly", forbid)
        monkeypatch.setattr(J, "judge", forbid)
        monkeypatch.setattr(J, "list_names_missing_ok", forbid)
        return plan

    monkeypatch.setattr(J, "plan_subs", plan_then_forbid)
    r.inst.start(r.emit)
    assert r.inst._kinds == {
        "a.mp4": "full",
        "e.mp4": "full",
        "f.mp4": "translate_only",
    }
    assert r.inst._jobs["a.mp4"].media == r.out / "a-破解.mp4"
    assert r.inst._jobs["f.mp4"].media == r.out / "f_restored.mp4"
    assert r.inst._jobs["f.mp4"].zh_target == r.out / "f_restored.srt"
    r.inst.stop(timeout=1)


def test_a_pending_film_with_subtitles_is_restored_without_a_job(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    lib = _sub(r.out, "a.mp4", ".chs.srt")
    lib.write_text(LIB_ZH, encoding="utf-8")
    r.inst.start(r.emit)
    assert "a.mp4" not in r.inst._jobs and r.launcher.n == 1
    st = r.inst.check(r.emit)
    assert r.spawner.argvs == [] and (r.out / "a-破解.mp4").exists()
    assert lib.read_text(encoding="utf-8") == LIB_ZH
    assert st.metrics["queue_restored"] == 1 and len(_done(r.evs)) == 1


def test_a_subtitle_dropped_in_before_the_asr_skips_and_consumes_the_carry(
    tmp_path, monkeypatch, caplog
):
    gpu = _gpu_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4", "b.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    lib = _sub(r.out, "a.mp4", ".chs.srt")
    lib.write_text(LIB_ZH, encoding="utf-8")
    inst._subs_consecutive_failures = 2
    with caplog.at_level(logging.INFO, logger="taskpaw.monitors.jasna"):
        st = inst.check(emit)  # a restored → re-check → skipped; b launched
    assert inst._settled["a.mp4"] == ("skipped", "subtitle exists")
    assert r.spawner.argvs == []  # no ASR for a
    assert inst._carried is None  # the carried hold was consumed and given
    assert r.launcher.n == 2 and r.launcher.inputs()[-1] == "b.mp4"
    assert gpu == ["acquire", "release", "acquire"]  # a's hold released first
    assert lib.read_text(encoding="utf-8") == LIB_ZH
    assert not _ja(r, "a.mp4").exists()
    assert inst._subs_consecutive_failures == 2 and inst._subs_disabled is None
    assert not [e for e in r.evs if e[0] == "alert"]
    msgs = [rec.getMessage() for rec in caplog.records]
    assert sum("a.mp4" in m and "subtitle exists" in m for m in msgs) == 1
    assert _states(inst, "a.mp4") == _SKIPPED
    assert st.metrics["subs_skipped"] == 1 and inst._done == 1
    inst.stop(timeout=1)


def test_no_listing_happens_while_the_gpu_is_refused(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, restored=["e.mp4"])
    other = ("other", 3)
    assert gpu_lease.try_acquire(other, 1.0, label="Other")
    calls: list = []
    real = J.list_names
    monkeypatch.setattr(J, "list_names", lambda f: calls.append(f) or real(f))
    r.inst.start(r.emit)
    for _ in range(3):
        r.inst.check(r.emit)
    assert r.inst._gpu_waiting and calls == [] and r.spawner.argvs == []
    assert gpu_lease.release(other)
    r.inst.check(r.emit)  # the hold is taken → one listing → ASR
    assert calls == [r.out] and len(r.spawner.argvs) == 1
    r.inst.stop(timeout=1)


@pytest.mark.parametrize(
    "exc", [PermissionError(13, "denied"), OSError(53, "net"), RuntimeError("bug")]
)
def test_an_unreadable_folder_before_the_asr_skips_and_releases_the_hold(
    tmp_path, monkeypatch, exc
):
    # AC5 + F14: the listing (outside the K1 fence) never raises; the film is
    # skipped, the hold released; one alert per run (F16).
    gpu = _gpu_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, restored=["d.mp4", "e.mp4"])
    real = J.plan_subs

    def plan_then_fail(*a, **k):
        plan = real(*a, **k)
        _scandir_spy(monkeypatch, r.out, exc)  # the share drops after the scan
        return plan

    monkeypatch.setattr(J, "plan_subs", plan_then_fail)
    r.inst.start(r.emit)
    for n in ("d.mp4", "e.mp4"):
        assert r.inst._settled[n] == ("skipped", "subtitle state unreadable")
        assert _states(r.inst, n) == _SKIPPED
    assert r.spawner.argvs == []
    assert gpu == ["acquire", "release", "acquire", "release"]
    alerts = _keyed(r.evs, "j1:subs-unreadable")
    assert len(alerts) == 1 and "d.mp4" in alerts[0][2]
    r.inst.check(r.emit)
    done = _done(r.evs)
    assert len(done) == 1 and "Subs: 0/2 done, 0 failed, 2 skipped" in done[0][2]


@pytest.mark.parametrize("case", ["subtitle", "unreadable"])
def test_a_translate_only_film_is_rechecked_before_its_submit(
    tmp_path, monkeypatch, case
):
    # F15: submitted hours after the scan — listed again first (no GPU).
    gpu = _gpu_spy(monkeypatch)
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"], ja=["a.mp4"])
    r.inst.start(r.emit)
    if case == "subtitle":
        _sub(r.out, "a.mp4", ".srt").write_text(LIB_ZH, encoding="utf-8")
    else:
        _scandir_spy(monkeypatch, r.out, PermissionError(13, "denied"))
    r.inst.check(r.emit)  # a restored → translate-only → re-check
    assert r.translators[0].submitted == []
    reason = "subtitle exists" if case == "subtitle" else "subtitle state unreadable"
    assert r.inst._settled["a.mp4"] == ("skipped", reason)
    assert _ja(r, "a.mp4").read_text(encoding="utf-8") == SRT_JA  # library's
    assert gpu == ["acquire", "release"]  # only the restore's hold
    assert _states(r.inst, "a.mp4") == {
        "restore": "done",
        "asr": "done",
        "translate": "skipped",
    }
    assert len(_done(r.evs)) == 1


def test_a_skipped_translate_only_film_lets_the_subs_only_walk_continue(
    tmp_path, monkeypatch
):
    names = ["d.mp4", "e.mp4", "f.mp4"]
    r = _setup(tmp_path, monkeypatch, restored=names, ja=names)
    real = J.list_names
    seen: list = []

    def drop_on_first(folder):
        if not seen:  # the owner places d's subtitle right after the scan
            _sub(r.out, "d.mp4", ".zh.srt").write_text(LIB_ZH, encoding="utf-8")
        seen.append(folder)
        return real(folder)

    monkeypatch.setattr(J, "list_names", drop_on_first)
    r.inst.start(r.emit)
    assert len(seen) == 3
    assert r.inst._settled["d.mp4"] == ("skipped", "subtitle exists")
    assert [q.job_id for q in r.translators[0].submitted] == ["e.mp4", "f.mp4"]
    assert _ja(r, "d.mp4").exists()  # a library transcript is never discarded


@pytest.mark.parametrize("case", ["subtitle", "unreadable"])
def test_a_full_film_is_rechecked_before_its_translation(tmp_path, monkeypatch, case):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    r.inst.start(r.emit)
    r.inst.check(r.emit)  # a restored → ASR
    assert len(r.spawner.argvs) == 1
    if case == "subtitle":
        lib = _sub(r.out, "a.mp4", ".zh.srt")
        lib.write_text(LIB_ZH, encoding="utf-8")
    else:
        _scandir_spy(monkeypatch, r.out, PermissionError(13, "denied"))
    r.spawner.last.finish(0)
    r.inst.check(r.emit)
    assert r.translators[0].submitted == []
    assert _states(r.inst, "a.mp4") == {
        "restore": "done",
        "asr": "done",
        "translate": "skipped",
    }
    if case == "subtitle":
        assert r.inst._settled["a.mp4"] == ("skipped", "subtitle exists")
        assert not _ja(r, "a.mp4").exists()  # this run's own transcript goes
        assert lib.read_text(encoding="utf-8") == LIB_ZH
    else:
        assert r.inst._settled["a.mp4"] == ("skipped", "subtitle state unreadable")
        assert _ja(r, "a.mp4").exists()  # kept: the next Start only translates
        assert len(_keyed(r.evs, "j1:subs-unreadable")) == 1
    assert len(_done(r.evs)) == 1


@pytest.mark.parametrize("kind", ["full", "translate_only"])
def test_a_refused_srt_skips_and_discards_only_this_runs_transcript(
    tmp_path, monkeypatch, kind
):
    if kind == "full":
        r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
        r.inst.start(r.emit)
        r.inst.check(r.emit)  # restored → ASR
        r.spawner.last.finish(0)
        r.inst.check(r.emit)  # ja published + submitted
    else:
        r = _setup(tmp_path, monkeypatch, restored=["a.mp4"], ja=["a.mp4"])
        _ja(r, "a.mp4").write_text(LIB_JA, encoding="utf-8")
        r.inst.start(r.emit)
    assert [q.job_id for q in r.translators[0].submitted] == ["a.mp4"]
    zh = _zh(r, "a.mp4")
    zh.write_text(LIB_ZH, encoding="utf-8")  # the owner's own .srt
    r.inst._subs_consecutive_failures = 2
    r.translators[0].answer("a.mp4")
    st = r.inst.check(r.emit)
    assert r.inst._settled["a.mp4"] == ("skipped", "subtitle exists")
    assert zh.read_text(encoding="utf-8") == LIB_ZH  # never overwritten
    if kind == "full":
        assert not _ja(r, "a.mp4").exists()
    else:
        assert _ja(r, "a.mp4").read_text(encoding="utf-8") == LIB_JA
    assert not list(r.out.glob("*.tmp"))
    assert r.inst._subs_consecutive_failures == 2 and r.inst._subs_disabled is None
    assert not [e for e in r.evs if e[0] == "alert"]
    assert st.metrics["subs_skipped"] == 1
    assert _states(r.inst, "a.mp4") == {
        "restore": "done",
        "asr": "done",
        "translate": "skipped",
    }
    assert len(_done(r.evs)) == 1


def test_a_transcript_that_appeared_mid_run_is_never_replaced(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    r.inst.start(r.emit)
    r.inst.check(r.emit)  # restored → ASR
    lib = _ja(r, "a.mp4")
    lib.write_text(LIB_JA, encoding="utf-8")
    r.spawner.last.finish(0)
    r.inst.check(r.emit)
    assert r.inst._settled["a.mp4"] == ("skipped", "transcript exists")
    assert lib.read_text(encoding="utf-8") == LIB_JA
    assert r.translators[0].submitted == []
    assert _states(r.inst, "a.mp4") == _SKIPPED
    r.inst.stop(timeout=1)
    r.inst.start(r.emit)  # the next Start only translates it
    assert r.inst._kinds["a.mp4"] == "translate_only"
    r.inst.stop(timeout=1)


def test_no_speech_with_a_srt_that_appeared_removes_only_our_empty_transcript(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    r.inst.start(r.emit)
    r.inst.check(r.emit)  # restored → ASR
    zh = _zh(r, "a.mp4")
    zh.write_text(LIB_ZH, encoding="utf-8")
    r.spawner.last.finish(0, state="empty", text="")
    r.inst.check(r.emit)
    assert r.inst._settled["a.mp4"] == ("skipped", "subtitle exists")
    assert zh.read_text(encoding="utf-8") == LIB_ZH
    assert not _ja(r, "a.mp4").exists()
    assert _states(r.inst, "a.mp4") == {
        "restore": "done",
        "asr": "done",
        "translate": "skipped",
    }


def test_a_zero_cue_resume_whose_srt_appeared_keeps_the_library_transcript(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, restored=["a.mp4"], ja=["a.mp4"])
    _ja(r, "a.mp4").write_bytes(b"")
    real = SubsJob.load_ja

    def load_then_drop(self):
        cues = real(self)
        self.zh_target.write_text(LIB_ZH, encoding="utf-8")
        return cues

    monkeypatch.setattr(SubsJob, "load_ja", load_then_drop)
    r.inst.start(r.emit)
    assert r.inst._settled["a.mp4"] == ("skipped", "subtitle exists")
    assert _zh(r, "a.mp4").read_text(encoding="utf-8") == LIB_ZH
    assert _ja(r, "a.mp4").read_bytes() == b""


def test_stop_never_replaces_a_transcript_that_appeared(tmp_path, monkeypatch, caplog):
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    r.inst.start(r.emit)
    r.inst.check(r.emit)  # restored → ASR
    r.spawner.last.finish(0)  # exited, unpolled
    lib = _ja(r, "a.mp4")
    lib.write_text(LIB_JA, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="taskpaw.monitors.jasna"):
        r.inst.stop(timeout=2)
    assert lib.read_text(encoding="utf-8") == LIB_JA
    assert any("a-破解.ja.srt already exists" in m.getMessage() for m in caplog.records)


def test_av_translate_description_states_the_library_rules():
    av = JasnaConfig.model_fields["av_translate"].description or ""
    for text in (".chs.srt", "Japanese", "never overwritten", "cannot be read"):
        assert text in av, text


def test_plan_subs_finds_a_case_variant_restored_file_like_the_filesystem(
    tmp_path, monkeypatch
):
    # CX2: the planning listing compares names the way the platform's default
    # filesystem does, and the media is the name actually listed, so the
    # subtitles follow the real file.
    from taskpaw_v3.monitors.subs import existing as E

    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    _videos(inp, "a.mp4")
    (out / "A-破解.MP4").write_bytes(b"restored")
    monkeypatch.setattr(E, "_PLATFORM", "darwin")
    plan = plan_subs(str(inp), str(out), [], [])
    assert plan.subs_only == [inp / "a.mp4"]
    assert plan.media[inp / "a.mp4"].name == "A-破解.MP4"
    monkeypatch.setattr(E, "_PLATFORM", "linux")
    assert plan_subs(str(inp), str(out), [], []).subs_only == []


def test_plan_subs_carries_the_transcript_as_it_is_named(tmp_path):
    # CX1: a translate_only file's job loads its `.ja.srt` by its actual name.
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    _videos(inp, "a.mp4")
    (out / "a-破解.mp4").write_bytes(b"restored")
    (out / "a-破解.JA.srt").write_text(SRT_JA, encoding="utf-8")
    plan = plan_subs(str(inp), str(out), [], [])
    assert plan.kinds[inp / "a.mp4"] == "translate_only"
    assert plan.ja[inp / "a.mp4"].name == "a-破解.JA.srt"


def test_a_pending_films_subtitles_belong_to_the_file_its_restore_writes(tmp_path):
    # CX3: an older restored file whose name differs only in Unicode form (NFD,
    # e.g. from a Mac client) is not this film's restore on NTFS / Linux; the
    # pending film's subtitles target the file its restore will publish.
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    _videos(inp, "caf\u00e9.mp4")
    (out / "cafe\u0301-破解.mp4").write_bytes(b"older restore")
    video = inp / "caf\u00e9.mp4"
    plan = plan_subs(str(inp), str(out), [video], [])
    assert plan.media[video] == output_path_for(str(out), video)


def test_a_recheck_keeps_every_planned_film_as_a_subtitle_owner(tmp_path, monkeypatch):
    # CX4: `Movie-破解.part2-破解.srt` belongs to the pending `Movie-破解.part2.mp4`
    # (kind none: no subtitle job). While that film is not restored (or its
    # restore failed), `Movie.mp4`'s re-check must still not claim it.
    r = _setup(tmp_path, monkeypatch, pending=["Movie.mp4", "Movie-破解.part2.mp4"])
    (r.out / "Movie-破解.part2-破解.srt").write_text(SRT_ZH, encoding="utf-8")
    r.inst.start(r.emit)
    assert "Movie-破解.part2.mp4" not in r.inst._jobs  # kind none
    job = r.inst._jobs["Movie.mp4"]
    assert r.inst._recheck(job, J.list_names(r.out)) is None
    r.inst.stop(timeout=1)


# ── #192/#190: resumable translation with fallback models ─────────────────
DS = LLMSettings("https://api.deepseek.com/v1", "deepseek-chat", "sk-ds", "config")
SRT_90 = srt.serialize(
    [Cue(i + 1, i * 1000, i * 1000 + 500, f"台詞{i}") for i in range(90)]
)
FOUR = ["a.mp4", "b.mp4", "c.mp4", "d.mp4"]


def _alerts(evs) -> list:
    return [e for e in evs if e[0] == "alert"]


def _four(tmp_path, monkeypatch) -> SimpleNamespace:
    """Four restored films with their .ja.srt: all submitted at Start."""
    r = _setup(tmp_path, monkeypatch, restored=FOUR, ja=FOUR)
    r.inst.start(r.emit)
    r.inst.check(r.emit)
    assert sorted(q.job_id for q in r.translators[0].submitted) == FOUR
    return r


def test_the_translator_checkpoints_under_the_data_dir_only_when_one_is_set(
    tmp_path, monkeypatch
):
    # C5: no data dir (every other test) = memory only, never a real folder.
    r = _setup(tmp_path, monkeypatch, restored=["a.mp4"], ja=["a.mp4"])
    r.inst.start(r.emit)
    assert r.translators[0].kw["checkpoint_dir"] is None
    r.inst.stop(timeout=1)
    set_data_dir(tmp_path / "data")
    r.inst.start(r.emit)
    assert r.translators[1].kw["checkpoint_dir"] == (
        tmp_path / "data" / CHECKPOINTS_DIRNAME
    )
    assert not (tmp_path / "data").exists()  # the plugin itself creates nothing
    r.inst.stop(timeout=1)


@pytest.mark.parametrize("case", ["fallback_only", "no_usable_provider"])
def test_the_key_check_reads_the_provider_chain(tmp_path, monkeypatch, case):
    # AC10/G11: only an EMPTY chain skips — a keyless primary with a usable
    # fallback translates; a primary with a key but no model is not usable.
    r = _setup(tmp_path, monkeypatch, restored=["a.mp4"], ja=["a.mp4"], key=False)
    if case == "fallback_only":
        set_llm_chain((DS,))
    else:
        set_llm_settings(LLMSettings("https://api.x.ai/v1", "", "sk-test", "config"))
    r.inst.start(r.emit)
    r.inst.check(r.emit)
    tr = r.translators[0]
    if case == "fallback_only":
        assert [q.job_id for q in tr.submitted] == ["a.mp4"]
        assert "a.mp4" not in r.inst._settled
        assert not _keyed(r.evs, "j1:subs-nokey")
    else:
        assert tr.submitted == []
        assert r.inst._settled["a.mp4"] == ("skipped", "no_llm_key")
        assert len(_keyed(r.evs, "j1:subs-nokey")) == 1


def test_a_no_key_result_is_a_skip_with_one_alert_never_a_failure(
    tmp_path, monkeypatch
):
    # AC10/G11: one rule in both plugins — the translator's no-key result (the
    # chain emptied after the submit) settles `skipped no_llm_key` with the
    # run's one alert, and never counts toward the 3-failure disable.
    r = _four(tmp_path, monkeypatch)
    inst, emit, tr = r.inst, r.emit, r.translators[0]
    inst._subs_consecutive_failures = 2
    for n in ("a.mp4", "b.mp4", "c.mp4"):
        tr.answer(n, outcome="no_key")
    inst.check(emit)
    for n in ("a.mp4", "b.mp4", "c.mp4"):
        assert inst._settled[n] == ("skipped", "no_llm_key")
        assert _ja(r, n).exists()
    assert inst._subs_failed == 0 and inst._subs_disabled is None
    assert inst._subs_consecutive_failures == 0  # reset, as a submit-time skip
    assert len(_keyed(r.evs, "j1:subs-nokey")) == 1
    assert not _keyed(r.evs, "j1:subs:a.mp4")
    assert tr.discarded == []
    tr.answer("d.mp4")
    inst.check(emit)
    done = _done(r.evs)
    assert len(done) == 1
    assert "| Subs: 1/4 done, 0 failed, 3 skipped | " in done[0][2]


def test_a_paused_film_is_skipped_streak_neutral_with_one_alert_per_run(
    tmp_path, monkeypatch
):
    # AC8: no translation service for 2 h → skipped `translation_paused` (its
    # .ja.srt and checkpoint kept), streak neutral, ONE alert per run, and the
    # done text counts them.
    r = _four(tmp_path, monkeypatch)
    inst, emit, tr = r.inst, r.emit, r.translators[0]
    tr.answer("a.mp4", ok=False)
    inst.check(emit)
    assert inst._subs_consecutive_failures == 1
    tr.answer("b.mp4", outcome="paused")
    tr.answer("c.mp4", outcome="paused")
    inst.check(emit)
    for n in ("b.mp4", "c.mp4"):
        assert inst._settled[n] == ("skipped", "translation_paused")
        assert _ja(r, n).exists() and not _zh(r, n).exists()
    assert inst._subs_consecutive_failures == 1  # neither counted nor reset
    alerts = _keyed(r.evs, "j1:translation-paused")
    assert len(alerts) == 1 and alerts[0][1] == "JASNA: translation paused"
    assert alerts[0][2].startswith("2 file(s) paused")
    assert not _keyed(r.evs, "j1:subs:b.mp4")
    tr.answer("d.mp4", outcome="paused")
    st = inst.check(emit)
    assert len(_keyed(r.evs, "j1:translation-paused")) == 1  # once per run
    assert inst._subs_disabled is None and tr.discarded == []
    assert st.metrics["subs_skipped"] == 3
    done = _done(r.evs)
    assert len(done) == 1
    assert "| Subs: 0/4 done, 1 failed, 3 skipped; 3 paused | " in done[0][2]


@pytest.mark.parametrize("outcome", ["paused", "no_key"])
def test_paused_and_no_key_films_settle_their_rows_skipped(
    tmp_path, monkeypatch, outcome
):
    # #189: the new skips end the film's subtitle steps like any other skip.
    r = _setup(tmp_path, monkeypatch, pending=["a.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    inst.check(emit)  # restored → ASR
    r.spawner.last.finish(0)
    inst.check(emit)  # ja published + submitted
    r.translators[0].answer("a.mp4", outcome=outcome)
    st = inst.check(emit)
    reason = "translation_paused" if outcome == "paused" else "no_llm_key"
    assert inst._settled["a.mp4"] == ("skipped", reason)
    assert _states(inst, "a.mp4") == {
        "restore": "done",
        "asr": "done",
        "translate": "skipped",
    }
    assert _rows(st.metrics) == [("a.mp4", "skipped")]
    assert st.metrics["subs_skipped"] == 1
    assert _ja(r, "a.mp4").exists()
    assert len(_done(r.evs)) == 1


def test_kept_japanese_lines_are_logged_per_film_and_suffix_the_done_text(
    tmp_path, monkeypatch, caplog
):
    # AC9: counts only (never a line's text); one info line per film with any.
    r = _four(tmp_path, monkeypatch)
    inst, emit, tr = r.inst, r.emit, r.translators[0]
    tr.answer("a.mp4", kept_ja=2)
    tr.answer("b.mp4")
    tr.answer("c.mp4", kept_ja=1)
    tr.answer("d.mp4", outcome="paused")
    with caplog.at_level(logging.INFO, logger="taskpaw.monitors.jasna"):
        inst.check(emit)
    kept = [m for m in caplog.messages if "kept in Japanese" in m]
    assert len(kept) == 2
    assert "a.mp4: 2 line(s) kept in Japanese" in kept[0]
    assert "c.mp4: 1 line(s) kept in Japanese" in kept[1]
    assert not any("はい" in m or "好" in m for m in caplog.messages)
    done = _done(r.evs)
    assert len(done) == 1
    assert (
        "| Subs: 3/4 done, 0 failed, 1 skipped; 3 lines kept in Japanese; 1 paused | "
        in done[0][2]
    )


def test_translator_notices_become_alerts_with_their_dedupe_keys(tmp_path, monkeypatch):
    # AC6/AC3: a provider that opened, a checkpoint that cannot be written —
    # raised once by the translator, alerted under `<iid>:<notice key>`.
    r = _setup(tmp_path, monkeypatch, restored=["a.mp4"], ja=["a.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    tr = r.translators[0]
    grok, ds = "grok-4.3 · api.x.ai", "deepseek-chat · api.deepseek.com"
    tr.notices = [
        Notice(f"llm-provider:{grok}", f"translation model unavailable: {grok}", "w"),
        Notice("checkpoint-write", "translation checkpoint not saved", "disk"),
    ]
    inst.check(emit)
    assert _alerts(r.evs) == [
        (
            "alert",
            f"JASNA: translation model unavailable: {grok}",
            "w",
            f"j1:llm-provider:{grok}",
        ),
        (
            "alert",
            "JASNA: translation checkpoint not saved",
            "disk",
            "j1:checkpoint-write",
        ),
    ]
    inst.check(emit)  # drained: nothing twice
    assert len(_alerts(r.evs)) == 2
    # a notice raised with the run's last result is alerted before `done`
    tr.notices = [
        Notice(f"llm-provider:{ds}", f"translation model unavailable: {ds}", "x")
    ]
    tr.answer("a.mp4")
    inst.check(emit)
    assert len(_keyed(r.evs, f"j1:llm-provider:{ds}")) == 1
    assert len(_done(r.evs)) == 1


@pytest.mark.parametrize(
    "case", ["published", "srt_exists", "publish_error", "failed", "paused", "no_key"]
)
def test_the_checkpoint_is_discarded_only_after_the_zh_was_published(
    tmp_path, monkeypatch, case
):
    # AC3: a film's checkpoint goes only once its zh publish returned ok
    # (after the #187 .ja.srt rule); every other end keeps it for next Start.
    r = _setup(tmp_path, monkeypatch, restored=["a.mp4"], ja=["a.mp4"])
    if case == "publish_error":
        monkeypatch.setattr(
            SubsJob,
            "publish_zh",
            lambda self, cues: PublishResult("error", f"publish {self.zh_target.name}"),
        )
    inst, emit = r.inst, r.emit
    inst.start(emit)
    tr = r.translators[0]
    if case == "srt_exists":
        _zh(r, "a.mp4").write_text(SRT_ZH, encoding="utf-8")  # #191: appeared
    if case in ("failed", "paused", "no_key"):
        tr.answer("a.mp4", ok=False, outcome=None if case == "failed" else case)
    else:
        tr.answer("a.mp4", kept_ja=1)
    inst.check(emit)
    ok = case == "published"
    assert tr.discarded == (["ck:a.mp4"] if ok else [])
    assert _ja(r, "a.mp4").exists() is not ok
    done = _done(r.evs)
    assert len(done) == 1
    assert ("; 1 lines kept in Japanese" in done[0][2]) is ok


def test_stop_mid_film_then_start_resumes_only_the_open_lines(tmp_path, monkeypatch):
    # AC3/AC4 end to end: the REAL Translator (driven by test_subs_translate's
    # fake llm-workers) with the plugin's checkpoint dir under a tmp data dir.
    # Stop lands while batch 2 is in flight; the next Start asks only for
    # lines 41–90, and the checkpoint goes with the published zh.
    set_data_dir(tmp_path / "data")
    r = _setup(tmp_path, monkeypatch, restored=["a.mp4"], ja=["a.mp4"])
    _ja(r, "a.mp4").write_text(SRT_90, encoding="utf-8")
    responder, held = _stepper()
    spawners = [Spawner(responder), Spawner(good)]
    made: list[Translator] = []

    def factory(run, *, name, **k):
        t = Translator(
            run,
            name=name,
            spawn=spawners[len(made)],
            worker_argv_fn=lambda: ["llm-worker"],
            job_fn=lambda proc: None,
            **k,
        )
        made.append(t)
        return t

    monkeypatch.setattr(J, "Translator", factory)
    inst, emit = r.inst, r.emit
    inst.start(emit)
    _reply(held.get(timeout=5))  # lines 1–40 translated → checkpointed
    assert _ids(held.get(timeout=5)[0])[0] == "41"  # batch 2 in flight
    inst.stop(timeout=5)
    ckpt = tmp_path / "data" / CHECKPOINTS_DIRNAME
    assert len(list(ckpt.glob("*.json"))) == 1
    assert _ja(r, "a.mp4").exists() and not _zh(r, "a.mp4").exists()
    inst.start(emit)

    def finished() -> bool:
        inst.check(emit)
        return bool(_done(r.evs))

    assert _wait(finished)
    assert inst._settled["a.mp4"] == ("completed", "")
    assert [_seq(q) for q in spawners[1].requests] == ["41-80", "81-90"]
    zh = srt.parse(_zh(r, "a.mp4").read_text(encoding="utf-8"))
    assert [c.text for c in zh] == [f"中台詞{i}" for i in range(90)]
    assert not list(ckpt.glob("*.json"))  # discarded once the zh was published
    assert not _ja(r, "a.mp4").exists()
    assert inst._translator is None and not made[1].is_alive()  # F2 at done


def test_a_film_the_real_translator_defers_holds_done_until_it_pauses(
    tmp_path, monkeypatch
):
    # AC8/H3 with the REAL Translator and a fake clock (2 h of deferral run in
    # ms): every model is down, so the only film is deferred — still in flight
    # and unsettled, `done` waits — until 2 h of deferral pause it: skipped
    # `translation_paused`, the model's notice and the one paused alert, and
    # `done` says `; 1 paused`.
    r = _setup(tmp_path, monkeypatch, restored=["a.mp4"], ja=["a.mp4"])
    clock = FakeClock()
    deferred, release = threading.Event(), threading.Event()
    made: list[Translator] = []

    def hook(seconds: float) -> None:  # runs in the translator's waits
        p = made[0].progress()
        if p is not None and p["paused"] and not release.is_set():
            deferred.set()
            release.wait(5)

    clock.hook = hook

    def factory(run, *, name, **k):
        t = Translator(
            run,
            name=name,
            spawn=Spawner(down),
            clock=clock,
            wait_fn=clock.wait,
            worker_argv_fn=lambda: ["llm-worker"],
            job_fn=lambda proc: None,
            **k,
        )
        clock.cancelled = t._cancel.is_set
        made.append(t)
        return t

    monkeypatch.setattr(J, "Translator", factory)
    inst, emit = r.inst, r.emit
    inst.start(emit)
    assert deferred.wait(5)
    st = inst.check(emit)  # deferred: in flight, unsettled, no `done`
    assert st.state == "running" and st.metrics["subs_translating"] == 1
    assert _step(st.metrics, "translate")["paused"] is True
    assert "a.mp4" not in inst._settled and not _done(r.evs)
    assert len(_keyed(r.evs, "j1:llm-provider:grok-4.3 · api.x.ai")) == 1
    release.set()

    def finished() -> bool:
        inst.check(emit)
        return bool(_done(r.evs))

    assert _wait(finished)
    assert inst._settled["a.mp4"] == ("skipped", "translation_paused")
    assert len(_keyed(r.evs, "j1:translation-paused")) == 1
    assert len(_keyed(r.evs, "j1:llm-provider:grok-4.3 · api.x.ai")) == 1
    assert "| Subs: 0/1 done, 0 failed, 1 skipped; 1 paused | " in _done(r.evs)[0][2]
    assert _ja(r, "a.mp4").exists() and not _zh(r, "a.mp4").exists()
