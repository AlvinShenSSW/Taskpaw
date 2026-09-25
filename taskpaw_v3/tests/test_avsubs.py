"""#179 standalone「AV 翻译」task (`avsubs`): config, tree planning, lifecycle,
settlement, the C7 abort, stop/start, the GPU lease hooks and status.

Nothing real is executed: the WhisperJAV child is a `ChildProcess` fake injected
through `AV.ChildProcess` (→ `AvsubsInstance._spawn`), the translator is replaced
wholesale through `AV.Translator`, and the process-wide lease is fresh per test
(conftest `_gpu_lease_isolation`) — never `taskkill`, the network or a real LLM
worker (D10).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest
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
from taskpaw_v3.monitors.plugins import avsubs as AV
from taskpaw_v3.monitors.plugins.avsubs import (
    AvsubsConfig,
    AvsubsInstance,
    AvsubsPlugin,
    plan_tree,
    sweep_srt_temps,
)
from taskpaw_v3.monitors.registry import default_registry
from taskpaw_v3.monitors.subs import srt
from taskpaw_v3.monitors.subs.checkpoint import CHECKPOINTS_DIRNAME
from taskpaw_v3.monitors.subs.job import PublishResult, SubsJob, source_identity
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
from taskpaw_v3.monitors.subs.util import paused_alert_message, translation_suffix
from taskpaw_v3.monitors.subs.whisperjav import attempt_dir
from taskpaw_v3.monitors.supervisor import Supervisor

SRT_JA = (
    "1\n00:00:00,000 --> 00:00:01,000\nはい\n\n"
    "2\n00:00:01,500 --> 00:00:02,500\nいいえ\n"
)
SRT_ZH = "1\n00:00:00,000 --> 00:00:01,000\n好\n"
IID = "av1"


# ── fakes ─────────────────────────────────────────────────────────────────
def _lock_free(inst: AvsubsInstance) -> bool:
    """Whether ANOTHER thread can take `_launch_lock` right now."""
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
        self.kill_result: Optional[bool] = None  # what terminate_tree returns
        self.survive = False  # the direct child keeps running after a kill
        self._owner = owner if owner is not None else {}
        self.out_dir = Path(argv[argv.index("--output-dir") + 1])

    def poll(self) -> Optional[int]:
        return self.rc

    def tail(self, lines: int = 10, max_chars: int = 800) -> str:
        return "ASR TAIL"

    def terminate_tree(self, timeout: float = 5.0) -> Optional[bool]:
        self.calls.append("terminate_tree")
        inst = self._owner.get("inst")
        free = _lock_free(inst) if inst is not None else True
        self.terminate_lock_free.append(free)
        self._owner.setdefault("order", []).append(("kill", free))
        if self.rc is None and not self.survive:
            self.rc = 1
        return self.kill_result

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
        self.kws: list[dict] = []

    def __call__(self, argv, **kw):
        self.argvs.append(list(argv))
        self.kws.append(kw)
        if len(self.argvs) in self.fail_at:
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
        self.join_timeouts: list[float] = []
        self.cancelled = False
        self.cancel_calls = 0
        self.cancel_lock_free: list[bool] = []
        self.flight = False
        self.live: Optional[dict] = None  # #189: what progress() reports
        self._owner = owner if owner is not None else {}
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
        self.submitted.append(req)

    def queued(self) -> int:
        if self.cancelled:
            return 0
        return len([r for r in self.submitted if r.job_id not in self.answered])

    def in_flight(self) -> bool:
        return self.flight and not self.cancelled

    def cancel(self) -> None:
        self.cancel_calls += 1
        inst = self._owner.get("inst")
        free = _lock_free(inst) if inst is not None else True
        self.cancel_lock_free.append(free)
        self._owner.setdefault("order", []).append(("cancel", free))
        self.cancelled = True
        self.results.put(CANCELLED)

    def join(self, timeout: float) -> None:
        self.join_timeouts.append(timeout)
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
        detail: str = "network",
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
            cues = tuple(Cue(c.index, c.start_ms, c.end_ms, "好") for c in req.cues)
            res = TranslateResult(
                req.run, job_id, kind, cues, "", kept_ja=kept_ja, checkpoint_key=key
            )
        elif kind in ("paused", "no_key"):
            text = TRANSLATION_PAUSED if kind == "paused" else NO_LLM_KEY
            res = TranslateResult(req.run, job_id, kind, (), text, checkpoint_key=key)
        else:
            res = TranslateResult(req.run, job_id, "failed", (), detail)
        self.results.put(res)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _events():
    evs: list[tuple] = []

    def emit(level, title, message, data=None, dedupe_key=None):
        evs.append((level, title, message, dedupe_key))

    return evs, emit


def _key(on: bool = True) -> None:
    """The primary's settings AND the provider chain the key check reads
    (#192 AC10): on = one usable provider, off = an empty chain."""
    s = LLMSettings("https://api.x.ai/v1", "grok-4.3", "sk-test" if on else "", "none")
    set_llm_settings(s)
    set_llm_chain((s,) if on else ())


def _touch(path: Path, data: bytes = b"video") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _stem_path(root: Path, rel: str, suffix: str) -> Path:
    p = root / rel
    return p.with_name(p.stem + suffix)


def _setup(
    tmp_path: Path,
    monkeypatch,
    *,
    full=(),
    ja=(),
    zh=(),
    key: bool = True,
    exe: bool = True,
    **kw,
):
    """`full`: videos with nothing; `ja`: videos with an existing `.ja.srt`
    (translate-only); `zh`: videos that already have their `.srt` (done)."""
    root = tmp_path / "lib"
    root.mkdir(parents=True, exist_ok=True)
    for rel in (*full, *ja, *zh):
        _touch(root / rel, b"video " + rel.encode())
    for rel in ja:
        _stem_path(root, rel, ".ja.srt").write_text(SRT_JA, encoding="utf-8")
    for rel in zh:
        _stem_path(root, rel, ".srt").write_text(SRT_ZH, encoding="utf-8")
    wj = tmp_path / "wj" / "whisperjav.exe"
    wj.parent.mkdir(parents=True, exist_ok=True)
    if exe:
        wj.write_bytes(b"MZ")
    # gpu monitor off: `read_gpu()` would run nvidia-smi through the (possibly
    # patched) subprocess module — tests never execute real programs.
    base: dict = dict(
        name="AV",
        avsubs_root_folder=str(root),
        whisperjav_exe_path=str(wj),
        avsubs_gpu_monitor=False,
    )
    base.update(kw)
    cfg = AvsubsConfig(**base)
    owner: dict = {}
    translators: list[_FakeTranslator] = []

    def factory(run, *, name, **k):
        t = _FakeTranslator(run, name=name, owner=owner, **k)
        translators.append(t)
        return t

    monkeypatch.setattr(AV, "Translator", factory)
    spawner = _Spawner(owner)
    monkeypatch.setattr(AV, "ChildProcess", spawner)
    _key(key)
    inst = AvsubsInstance(IID, cfg)
    owner["inst"] = inst
    evs, emit = _events()
    return SimpleNamespace(
        cfg=cfg,
        root=root,
        spawner=spawner,
        translators=translators,
        inst=inst,
        evs=evs,
        emit=emit,
        owner=owner,
    )


def _lease_spy(monkeypatch, owner: dict) -> list[tuple]:
    """Record every lease call (and whether `_launch_lock` was free then)."""
    log = owner.setdefault("order", [])
    real_try, real_rel, real_wd = (
        gpu_lease.try_acquire,
        gpu_lease.release,
        gpu_lease.withdraw,
    )

    def free() -> bool:
        inst = owner.get("inst")
        return _lock_free(inst) if inst is not None else True

    def try_acquire(run, poll_interval, label=""):
        ok = real_try(run, poll_interval, label)
        log.append(("acquire" if ok else "refused", free()))
        return ok

    def release(run):
        ok = real_rel(run)
        log.append(("release" if ok else "release-refused", free()))
        return ok

    def withdraw(run):
        real_wd(run)
        log.append(("withdraw", free()))

    monkeypatch.setattr(gpu_lease, "try_acquire", try_acquire)
    monkeypatch.setattr(gpu_lease, "release", release)
    monkeypatch.setattr(gpu_lease, "withdraw", withdraw)
    return log


def _names(log: list[tuple]) -> list[str]:
    return [n for n, _ in log]


def _done(evs) -> list:
    return [e for e in evs if e[0] == "done"]


def _alerts(evs) -> list:
    return [e for e in evs if e[0] == "alert"]


def _keyed(evs, key: str) -> list:
    return [e for e in evs if e[3] == key]


def _ja(r, rel: str) -> Path:
    return _stem_path(r.root, rel, ".ja.srt")


def _zh(r, rel: str) -> Path:
    return _stem_path(r.root, rel, ".srt")


def _staging_root(r) -> Path:
    digest = hashlib.sha1(IID.encode("utf-8")).hexdigest()[:8]
    return r.root / ".avsubs" / f"avsubs-{digest}"


def _job_dir(r, rel: str) -> Path:
    return attempt_dir(_staging_root(r), rel, 1).parent


def _wait(cond, timeout: float = 8.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


def _other_holds(label: str = "Jasna") -> tuple[str, int]:
    run = ("other", 999)
    assert gpu_lease.try_acquire(run, 10.0, label=label)
    return run


# ── config / plugin ───────────────────────────────────────────────────────
def test_config_defaults_schema_and_plugin_identity(tmp_path):
    c = AvsubsConfig(name="a", avsubs_root_folder="C:/lib", whisperjav_exe_path="w")
    assert c.avsubs_recursive is True
    assert c.avsubs_extensions == ["mp4"]
    assert c.whisperjav_engine == "anime-whisper"
    assert c.whisperjav_extra_args == ""
    assert c.avsubs_gpu_monitor is True
    props = AvsubsPlugin.json_schema()["properties"]
    for f in (
        "avsubs_root_folder",
        "avsubs_recursive",
        "avsubs_extensions",
        "whisperjav_exe_path",
        "whisperjav_engine",
        "whisperjav_extra_args",
        "avsubs_gpu_monitor",
    ):
        assert props[f].get("description"), f
    assert props["avsubs_extensions"]["default"] == ["mp4"]
    root_text = props["avsubs_root_folder"]["description"]
    assert "overlapping" in root_text and "Jasna" in root_text
    assert "quote" in props["whisperjav_extra_args"]["description"]
    ui = AvsubsPlugin.ui_schema()
    assert ui["avsubs_root_folder"]["ui:options"]["taskpawPath"] == "directory"
    assert ui["whisperjav_exe_path"]["ui:options"]["taskpawPath"] == "file"
    assert ui["ui:order"][:2] == ["name", "avsubs_root_folder"]
    assert ui["ui:order"][-1] == "*"
    p = AvsubsPlugin()
    assert p.type_id == "avsubs"
    assert p.display_name == "AV 翻译 (subtitles)"
    assert p.category == "task" and p.system is False
    assert p.manual_start(c) is True  # managed only: never auto-starts
    assert isinstance(p.create("x", c), AvsubsInstance)
    reg = default_registry()
    assert reg.has("avsubs") and isinstance(reg.get("avsubs"), AvsubsPlugin)


def test_config_requires_the_root_and_the_exe():
    with pytest.raises(ValueError, match="avsubs_root_folder"):
        AvsubsConfig(name="a", whisperjav_exe_path="w")
    with pytest.raises(ValueError, match="avsubs_root_folder"):
        AvsubsConfig(name="a", avsubs_root_folder="  ", whisperjav_exe_path="w")
    with pytest.raises(
        ValueError, match=r"AV 翻译 \(subtitles\) needs whisperjav_exe_path"
    ):
        AvsubsConfig(name="a", avsubs_root_folder="C:/lib")


def test_extensions_are_normalized_and_validated():
    c = AvsubsConfig(
        name="a",
        avsubs_root_folder="r",
        whisperjav_exe_path="w",
        avsubs_extensions=[" .MP4", "mkv", "mp4", "MKV"],
    )
    assert c.avsubs_extensions == ["mp4", "mkv"]
    for bad in ([], [""], ["."], ["m p4"], ["mp4!"], ["..mp4"], ["視頻"]):
        with pytest.raises(ValueError, match="avsubs_extensions"):
            AvsubsConfig(
                name="a",
                avsubs_root_folder="r",
                whisperjav_exe_path="w",
                avsubs_extensions=bad,
            )


@pytest.mark.parametrize(
    "extra,match",
    [
        ("--output-dir x", "whisperjav_extra_args"),
        ("--temp x", "whisperjav_extra_args"),
        ("--translate-api-key k", "whisperjav_extra_args"),
        ('--sensitivity "aggressive', "quote"),
    ],
)
def test_whisperjav_extra_args_rules(extra, match):
    with pytest.raises(ValueError, match=match):
        AvsubsConfig(
            name="a",
            avsubs_root_folder="r",
            whisperjav_exe_path="w",
            whisperjav_extra_args=extra,
        )
    ok = AvsubsConfig(
        name="a",
        avsubs_root_folder="r",
        whisperjav_exe_path="w",
        whisperjav_engine="custom",
        whisperjav_extra_args="--mode balanced --sensitivity aggressive",
    )
    assert ok.whisperjav_engine == "custom"


# ── plan_tree (pure) ──────────────────────────────────────────────────────
def _rels(plan) -> list[str]:
    return [i.relpath for i in plan.items]


def test_plan_tree_recursive_and_flat(tmp_path):
    for rel in ("a.mp4", "sub/b.mp4", "sub/deeper/c.mp4", "notes.txt"):
        _touch(tmp_path / rel)
    plan = plan_tree(str(tmp_path), True, ["mp4"])
    assert _rels(plan) == ["a.mp4", "sub/b.mp4", "sub/deeper/c.mp4"]
    assert plan.done == 0 and plan.collisions == [] and plan.errors == []
    flat = plan_tree(str(tmp_path), False, ["mp4"])
    assert _rels(flat) == ["a.mp4"]
    item = plan.items[1]
    assert item.source == tmp_path / "sub" / "b.mp4"
    assert item.ja_target == tmp_path / "sub" / "b.ja.srt"
    assert item.zh_target == tmp_path / "sub" / "b.srt"
    assert item.kind == "full"


def test_plan_tree_skips_hidden_and_symlinked_dirs(tmp_path):
    _touch(tmp_path / "a.mp4")
    _touch(tmp_path / ".hidden" / "h.mp4")
    _touch(tmp_path / ".avsubs" / "x" / "s.mp4")
    real = tmp_path / "elsewhere"
    _touch(real / "l.mp4")
    lib = tmp_path / "lib"
    lib.mkdir()
    _touch(lib / "k.mp4")
    try:
        os.symlink(real, lib / "link", target_is_directory=True)
    except (OSError, NotImplementedError):
        link_made = False
    else:
        link_made = True
    plan = plan_tree(str(lib), True, ["mp4"])
    assert _rels(plan) == ["k.mp4"]
    whole = plan_tree(str(tmp_path), True, ["mp4"])
    assert "a.mp4" in _rels(whole)
    assert not any(r.startswith(".") for r in _rels(whole))
    if not link_made:
        pytest.skip("symlink creation needs a privilege on this machine")


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS junctions are Windows-only")
def test_plan_tree_skips_a_windows_junction(tmp_path, monkeypatch):
    lib = tmp_path / "lib"
    _touch(lib / "k.mp4")
    real = tmp_path / "real"
    _touch(real / "j.mp4")
    r = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(lib / "jn"), str(real)],
        capture_output=True,
        check=False,
    )
    assert r.returncode == 0, r
    assert _rels(plan_tree(str(lib), True, ["mp4"])) == ["k.mp4"]
    # Python 3.10/3.11 have no os.path.isjunction — the helper must not need it.
    monkeypatch.delattr(os.path, "isjunction", raising=False)
    entry = next(e for e in os.scandir(lib) if e.name == "jn")
    assert AV._is_link_dir(entry) is True
    assert _rels(plan_tree(str(lib), True, ["mp4"])) == ["k.mp4"]


class _Entry:
    """A fake `os.DirEntry` for the attribute / reparse-tag rules."""

    def __init__(
        self,
        name: str,
        *,
        symlink: bool = False,
        tag: int = 0,
        attrs: int = 0x10,
        stat_error: bool = False,
    ) -> None:
        self.name = name
        self.path = name
        self._symlink = symlink
        self._st = SimpleNamespace(st_reparse_tag=tag, st_file_attributes=attrs)
        self._stat_error = stat_error

    def is_symlink(self) -> bool:
        return self._symlink

    def is_dir(self, follow_symlinks: bool = True) -> bool:
        return True

    def stat(self, follow_symlinks: bool = True):
        if self._stat_error:
            raise PermissionError("denied")
        return self._st


def test_is_link_dir_uses_the_reparse_tag_not_isjunction(monkeypatch):
    monkeypatch.delattr(os.path, "isjunction", raising=False)
    assert AV._is_link_dir(_Entry("s", symlink=True)) is True
    assert AV._is_link_dir(_Entry("j", tag=0xA0000003, attrs=0x410)) is True
    assert AV._is_link_dir(_Entry("l", tag=0xA000000C, attrs=0x410)) is True
    # OneDrive placeholders (IO_REPARSE_TAG_CLOUD_*) are real folders: descended
    assert AV._is_link_dir(_Entry("od", tag=0x9000601A, attrs=0x410)) is False
    assert AV._is_link_dir(_Entry("plain")) is False
    assert AV._is_link_dir(_Entry("err", stat_error=True)) is False


def test_windows_hidden_or_system_dirs_are_not_descended():
    assert AV._should_descend(_Entry("Films")) is True
    assert AV._should_descend(_Entry("od", tag=0x9000601A, attrs=0x410)) is True
    assert AV._should_descend(_Entry("$RECYCLE.BIN", attrs=0x16)) is False
    assert AV._should_descend(_Entry("System Volume Information", attrs=0x14)) is False
    assert AV._should_descend(_Entry("h", attrs=0x12)) is False
    assert AV._should_descend(_Entry(".git")) is False
    assert AV._should_descend(_Entry("jn", tag=0xA0000003, attrs=0x410)) is False


@pytest.mark.skipif(sys.platform != "win32", reason="attrib is Windows-only")
def test_plan_tree_skips_a_real_hidden_attribute_dir(tmp_path):
    _touch(tmp_path / "a.mp4")
    _touch(tmp_path / "secret" / "s.mp4")
    r = subprocess.run(
        ["attrib", "+h", str(tmp_path / "secret")], capture_output=True, check=False
    )
    assert r.returncode == 0, r
    assert _rels(plan_tree(str(tmp_path), True, ["mp4"])) == ["a.mp4"]


def test_plan_tree_extension_case_and_tmp_files(tmp_path):
    for rel in (
        "A.MP4",
        "b.Mp4",
        "c.mkv",
        "x_restored.tmp.mp4",
        "y.TMP.mp4",
        "noext",
        ".mp4",
    ):
        _touch(tmp_path / rel)
    assert _rels(plan_tree(str(tmp_path), True, ["mp4"])) == ["A.MP4", "b.Mp4"]
    both = plan_tree(str(tmp_path), True, [".MKV", "mp4"])
    assert _rels(both) == ["A.MP4", "b.Mp4", "c.mkv"]


def test_plan_tree_skips_macos_appledouble_files(tmp_path):
    # `._film.mp4` is Finder metadata copied from a Mac, not a video; other
    # dot-files are not special-cased.
    _touch(tmp_path / "film.mp4")
    _touch(tmp_path / "._film.mp4", b"AppleDouble metadata")
    _touch(tmp_path / "sub" / "._other.mp4")
    plan = plan_tree(str(tmp_path), True, ["mp4"])
    assert _rels(plan) == ["film.mp4"]
    assert plan.errors == [] and plan.collisions == []


def test_plan_tree_reports_a_name_that_is_not_utf8_encodable(tmp_path):
    _touch(tmp_path / "ok.mp4")
    try:
        _touch(tmp_path / "bad\udc80name.mp4")
    except (OSError, UnicodeError, ValueError):
        pytest.skip("this filesystem cannot hold a non-UTF-8 name")
    plan = plan_tree(str(tmp_path), True, ["mp4"])
    assert _rels(plan) == ["ok.mp4"]
    assert len(plan.errors) == 1
    assert "bad" in plan.errors[0]
    plan.errors[0].encode("utf-8")  # the report itself is always encodable


def test_plan_tree_classifies_done_translate_only_and_full(tmp_path):
    for rel in ("a.mp4", "b.mp4", "c.mp4", "d.mp4"):
        _touch(tmp_path / rel)
    (tmp_path / "a.srt").write_text(SRT_ZH, encoding="utf-8")
    (tmp_path / "b.srt").write_bytes(b"")  # a 0-byte zh counts as done
    (tmp_path / "c.ja.srt").write_text(SRT_JA, encoding="utf-8")
    plan = plan_tree(str(tmp_path), True, ["mp4"])
    assert plan.done == 2
    assert [(i.relpath, i.kind) for i in plan.items] == [
        ("c.mp4", "translate_only"),
        ("d.mp4", "full"),
    ]


def test_plan_tree_cross_extension_and_cross_role_collisions(tmp_path):
    for rel in ("a.mp4", "a.mkv", "m.ja.mp4", "m.mp4", "z.mp4"):
        _touch(tmp_path / rel)
    plan = plan_tree(str(tmp_path), True, ["mp4", "mkv"])
    assert _rels(plan) == ["a.mkv", "m.ja.mp4", "z.mp4"]
    lost = {(a.name, b.name) for a, b in plan.collisions}
    # a.mp4's targets are a.mkv's; m.mp4's ja target (m.ja.srt) is m.ja.mp4's zh
    assert lost == {("a.mp4", "a.mkv"), ("m.mp4", "m.ja.mp4")}


def test_plan_tree_same_stem_in_two_folders_gets_two_staging_dirs(tmp_path):
    _touch(tmp_path / "x" / "film.mp4")
    _touch(tmp_path / "y" / "film.mp4")
    plan = plan_tree(str(tmp_path), True, ["mp4"])
    assert _rels(plan) == ["x/film.mp4", "y/film.mp4"]
    assert plan.collisions == []
    dirs = {attempt_dir(tmp_path / "s", i.relpath, 1).parent for i in plan.items}
    assert len(dirs) == 2


def test_plan_tree_spaces_cjk_and_case_variants(tmp_path):
    names = ("my film 01.mp4", "日本語 タイトル.mp4", "子目录/片 2.mp4")
    for rel in names:
        _touch(tmp_path / rel)
    _touch(tmp_path / "Case.mp4")
    _touch(tmp_path / "case.mkv")
    plan = plan_tree(str(tmp_path), True, ["mp4", "mkv"])
    rels = _rels(plan)
    for rel in names:
        assert rel in rels
    item = next(i for i in plan.items if i.relpath == "子目录/片 2.mp4")
    assert item.zh_target == tmp_path / "子目录" / "片 2.srt"
    if os.path.normcase("A") == os.path.normcase("a"):
        # case-insensitive targets (Windows): Case.srt and case.srt are one file
        assert [(a.name, b.name) for a, b in plan.collisions] == [
            ("Case.mp4", "case.mkv")
        ]
    else:
        assert plan.collisions == []


def test_plan_tree_order_is_stable_and_deterministic(tmp_path):
    for rel in ("b.mp4", "A.mp4", "a/z.mp4", "C/y.mp4", "c.mp4"):
        _touch(tmp_path / rel)
    one = _rels(plan_tree(str(tmp_path), True, ["mp4"]))
    two = _rels(plan_tree(str(tmp_path), True, ["mp4"]))
    assert one == two
    assert one == sorted(one, key=lambda r: (r.casefold(), r))


def test_plan_tree_unreadable_subdir_is_reported_and_root_raises(tmp_path, monkeypatch):
    _touch(tmp_path / "a.mp4")
    _touch(tmp_path / "locked" / "b.mp4")
    real = os.scandir
    locked = str(tmp_path / "locked")

    def fake_scandir(path="."):
        if os.fspath(path) == locked:
            raise PermissionError("denied")
        return real(path)

    monkeypatch.setattr(os, "scandir", fake_scandir)
    plan = plan_tree(str(tmp_path), True, ["mp4"])
    assert _rels(plan) == ["a.mp4"]
    assert len(plan.errors) == 1 and "locked" in plan.errors[0]
    monkeypatch.setattr(os, "scandir", real)
    with pytest.raises(OSError):
        plan_tree(str(tmp_path / "missing"), True, ["mp4"])


def test_plan_tree_identity_equals_source_identity(tmp_path):
    src = _touch(tmp_path / "a.mp4", b"12345")
    plan = plan_tree(str(tmp_path), True, ["mp4"])
    assert plan.items[0].identity == source_identity(src)


def test_age_gated_srt_temp_sweep(tmp_path):
    old = tmp_path / "a.srt.7.tmp"
    old_ja = tmp_path / "a.ja.srt.12.tmp"
    fresh = tmp_path / "b.srt.8.tmp"
    other = tmp_path / "c.srt.tmp"
    for p in (old, old_ja, fresh, other):
        p.write_text("x", encoding="utf-8")
    past = time.time() - 3600
    for p in (old, old_ja, other):
        os.utime(p, (past, past))
    removed = sweep_srt_temps([tmp_path])
    assert set(removed) == {old, old_ja}
    assert fresh.exists() and other.exists()
    assert not old.exists() and not old_ja.exists()


# ── lifecycle ─────────────────────────────────────────────────────────────
def test_full_lifecycle_in_order_asr_translate_done(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, full=["b.mp4", "a.mp4"])
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    assert len(sp.argvs) == 1 and sp.argvs[0][1] == str(r.root / "a.mp4")
    assert gpu_lease.holder() == inst._run
    st = inst.check(emit)
    assert st.state == "running" and st.metrics["phase"] == "asr"
    sp.last.finish(0)
    inst.check(emit)  # a transcribed → ja published, submitted, b launched
    assert _ja(r, "a.mp4").read_text(encoding="utf-8").startswith("1\n")
    tr = r.translators[0]
    assert [q.job_id for q in tr.submitted] == ["a.mp4"]
    assert len(sp.argvs) == 2 and sp.argvs[1][1] == str(r.root / "b.mp4")
    tr.answer("a.mp4")
    inst.check(emit)
    assert _zh(r, "a.mp4").exists()
    sp.last.finish(0)
    inst.check(emit)
    assert gpu_lease.holder() is None
    tr.answer("b.mp4")
    st = inst.check(emit)
    assert _zh(r, "b.mp4").exists()
    done = _done(r.evs)
    assert len(done) == 1
    assert done[0][1] == "AV complete"
    assert "AV 翻译 complete | Queue: 2/2 done, 0 failed, 0 skipped | " in done[0][2]
    assert st.state == "idle"
    assert inst._translator is None and tr.cancelled and tr.joined  # F2
    assert inst._run not in gpu_lease.waiters()
    inst.check(emit)
    assert len(_done(r.evs)) == 1


def test_translate_only_is_submitted_at_start_and_never_acquires(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, ja=["t.mp4"])
    log = _lease_spy(monkeypatch, r.owner)
    r.inst.start(r.emit)
    tr = r.translators[0]
    assert [q.job_id for q in tr.submitted] == ["t.mp4"]
    assert "acquire" not in _names(log) and "refused" not in _names(log)
    assert r.spawner.argvs == []
    tr.answer("t.mp4")
    r.inst.check(r.emit)
    assert _zh(r, "t.mp4").exists() and len(_done(r.evs)) == 1
    assert "acquire" not in _names(log)


def test_no_speech_publishes_empty_srts_and_completes(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    r.inst.start(r.emit)
    r.spawner.last.finish(0, state="empty", text="")
    r.inst.check(r.emit)
    assert not _ja(r, "a.mp4").exists()  # #187: gone once the zh is published
    assert _zh(r, "a.mp4").read_bytes() == b""
    assert r.inst._settled["a.mp4"] == ("completed", "no speech")
    assert r.translators[0].submitted == []
    assert len(_done(r.evs)) == 1


def test_media_changed_during_asr_is_skipped_unstable(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4"])
    r.inst.start(r.emit)
    (r.root / "a.mp4").write_bytes(b"a different, longer video")
    r.spawner.last.finish(0)
    r.inst.check(r.emit)
    assert r.inst._settled["a.mp4"] == ("skipped", "unstable")
    assert len(_keyed(r.evs, f"{IID}:avsubs-unstable:a.mp4")) == 1
    assert not _ja(r, "a.mp4").exists()
    assert len(r.spawner.argvs) == 2  # b launched
    assert r.inst._streak == 0


def test_asr_failure_retries_once_then_alerts_failed(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4"])
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    sp.last.finish(1)
    inst.check(emit)  # retry, same file, same lease hold
    assert len(sp.argvs) == 2 and sp.argvs[1][1] == str(r.root / "a.mp4")
    assert "attempt-2" in sp.argvs[1][sp.argvs[1].index("--output-dir") + 1]
    assert not _alerts(r.evs)
    assert gpu_lease.holder() == inst._run
    sp.last.finish(1)
    inst.check(emit)
    assert inst._settled["a.mp4"][0] == "failed"
    alert = _keyed(r.evs, f"{IID}:avsubs:a.mp4")
    assert len(alert) == 1 and "exit code 1" in alert[0][2]
    assert inst._streak == 1
    assert len(sp.argvs) == 3 and sp.argvs[2][1] == str(r.root / "b.mp4")


def test_spawn_failure_releases_the_lease(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    r.spawner.fail_at = {1, 2}
    log = _lease_spy(monkeypatch, r.owner)
    r.inst.start(r.emit)
    assert r.inst._settled["a.mp4"][0] == "failed"
    assert "launch: OSError" in r.inst._settled["a.mp4"][1]
    assert len(_keyed(r.evs, f"{IID}:avsubs:a.mp4")) == 1
    assert _names(log)[:2] == ["acquire", "release"]
    assert gpu_lease.holder() is None
    assert len(_done(r.evs)) == 1


def test_an_exception_inside_the_launch_releases_and_settles_failed(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4"])

    def boom(self, spawn):
        raise RuntimeError("bug")

    monkeypatch.setattr(SubsJob, "start_asr", boom)
    r.inst.start(r.emit)
    assert r.inst._settled["a.mp4"] == ("failed", "internal: RuntimeError")
    assert r.inst._settled["b.mp4"] == ("failed", "internal: RuntimeError")
    assert len(_keyed(r.evs, f"{IID}:avsubs:a.mp4")) == 1
    assert gpu_lease.holder() is None
    assert r.inst._asr_job is None


def test_missing_key_skips_with_one_alert_and_a_later_key_translates(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4", "c.mp4"], key=False)
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    sp.last.finish(0)
    inst.check(emit)  # a: ja published, no key → skipped
    assert inst._settled["a.mp4"] == ("skipped", "no_llm_key")
    assert _ja(r, "a.mp4").exists() and not _zh(r, "a.mp4").exists()
    sp.last.finish(0)
    inst.check(emit)  # b: skipped too, one alert only
    assert inst._settled["b.mp4"] == ("skipped", "no_llm_key")
    assert len(_keyed(r.evs, f"{IID}:avsubs-nokey")) == 1
    _key(True)
    sp.last.finish(0)
    inst.check(emit)
    assert [q.job_id for q in r.translators[0].submitted] == ["c.mp4"]
    r.translators[0].answer("c.mp4")
    inst.check(emit)
    assert _zh(r, "c.mp4").exists()
    done = _done(r.evs)
    assert len(done) == 1 and "Queue: 1/3 done, 0 failed, 2 skipped" in done[0][2]


def test_a_no_llm_key_translator_result_is_a_skip(tmp_path, monkeypatch):
    # C8: the key disappeared between the check and the request.
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4", "b.mp4", "c.mp4", "d.mp4"])
    r.inst.start(r.emit)
    tr = r.translators[0]
    for rel in ("a.mp4", "b.mp4", "c.mp4"):
        tr.answer(rel, outcome="no_key")
    tr.answer("d.mp4")
    r.inst.check(r.emit)
    for rel in ("a.mp4", "b.mp4", "c.mp4"):
        assert r.inst._settled[rel] == ("skipped", "no_llm_key")
    assert r.inst._settled["d.mp4"] == ("completed", "")
    assert not r.inst._aborted
    assert not _keyed(r.evs, f"{IID}:avsubs:a.mp4")


def test_unreadable_existing_ja_fails_without_counting_the_streak(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4", "b.mp4", "c.mp4"], full=["d.mp4"])
    for rel in ("a.mp4", "b.mp4", "c.mp4"):
        _ja(r, rel).write_text("not an srt at all", encoding="utf-8")
    r.inst.start(r.emit)
    for rel in ("a.mp4", "b.mp4", "c.mp4"):
        assert r.inst._settled[rel][0] == "failed"
        assert r.inst._settled[rel][1].startswith("unreadable .ja.srt")
    assert r.inst._streak == 0 and not r.inst._aborted  # D9
    assert len(_alerts(r.evs)) == 3
    assert len(r.spawner.argvs) == 1  # d still transcribes
    for rel in ("a.mp4", "b.mp4", "c.mp4"):  # #187: a failed job keeps its ja
        assert _ja(r, rel).read_text(encoding="utf-8") == "not an srt at all"


# ── the 3-strike abort (C7 / m3) ──────────────────────────────────────────
def test_three_failures_abort_in_the_c7_order_in_the_same_check(tmp_path, monkeypatch):
    r = _setup(
        tmp_path, monkeypatch, full=["a.mp4", "b.mp4", "c.mp4", "d.mp4", "e.mp4"]
    )
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    for _ in range(3):  # a, b, c transcribed and submitted
        sp.last.finish(0)
        inst.check(emit)
    live = sp.last  # d's ASR, still running
    assert live.rc is None and inst._asr_job is inst._jobs["d.mp4"]
    assert _job_dir(r, "d.mp4").is_dir()
    tr = r.translators[0]
    order = r.owner.setdefault("order", [])
    _lease_spy(monkeypatch, r.owner)
    order.clear()
    for rel in ("a.mp4", "b.mp4", "c.mp4"):
        tr.answer(rel, ok=False)
    st = inst.check(emit)
    assert st.state == "degraded"
    # kill → release → withdraw → translator cancel, all outside the lock
    assert _names(order) == ["kill", "release", "withdraw", "cancel"], order
    assert all(free for _, free in order), order
    assert gpu_lease.holder() is None and inst._run not in gpu_lease.waiters()
    assert inst._settled["d.mp4"] == ("skipped", "cancelled")
    assert inst._settled["e.mp4"] == ("skipped", "cancelled")
    assert inst._asr_job is None
    assert not _job_dir(r, "d.mp4").exists()  # N6: removed after its kill
    aborted = _keyed(r.evs, f"{IID}:avsubs-aborted")
    assert len(aborted) == 1
    assert (
        "AV 翻译 aborted after 3 consecutive failures | Queue: 0/5 done, "
        "3 failed, 2 skipped" in aborted[0][2]
    )
    assert not _keyed(r.evs, f"{IID}:avsubs-survivor")
    st = inst.check(emit)
    assert st.state == "degraded" and not _done(r.evs)
    assert len(sp.argvs) == 4


def test_the_live_jobs_staging_dir_survives_until_its_kill(tmp_path, monkeypatch):
    # N6: the settle of the live job must not queue its rmtree before the kill.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4", "c.mp4", "d.mp4"])
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    for _ in range(3):
        sp.last.finish(0)
        inst.check(emit)
    live = sp.last
    seen: list[bool] = []
    real = live.terminate_tree

    def kill(timeout: float = 5.0):
        seen.append(_job_dir(r, "d.mp4").is_dir())
        return real(timeout)

    live.terminate_tree = kill  # type: ignore[method-assign]
    for rel in ("a.mp4", "b.mp4", "c.mp4"):
        r.translators[0].answer(rel, ok=False)
    inst.check(emit)
    assert seen == [True]
    assert not _job_dir(r, "d.mp4").exists()


def test_abort_from_the_start_time_translate_only_path(tmp_path, monkeypatch):
    # D9: the third failure is settled at Start (the empty zh cannot be written);
    # the deferred abort runs right there, before the first check.
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4", "b.mp4", "c.mp4"], full=["d.mp4"])
    for rel in ("a.mp4", "b.mp4", "c.mp4"):
        _ja(r, rel).write_bytes(b"")
    monkeypatch.setattr(
        SubsJob,
        "publish_zh",
        lambda self, cues: PublishResult("error", f"publish {self.zh_target.name}: X"),
    )
    order = r.owner.setdefault("order", [])
    _lease_spy(monkeypatch, r.owner)
    r.inst.start(r.emit)
    assert r.inst._aborted
    assert _names(order) == ["withdraw", "cancel"], order
    assert all(free for _, free in order)
    assert r.spawner.argvs == []
    assert r.inst._settled["d.mp4"] == ("skipped", "cancelled")
    assert len(_keyed(r.evs, f"{IID}:avsubs-aborted")) == 1
    assert r.inst.check(r.emit).state == "degraded"
    assert not _done(r.evs)


def test_a_false_kill_alerts_survivor_once_and_still_releases(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4", "c.mp4", "d.mp4"])
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    for _ in range(3):
        sp.last.finish(0)
        inst.check(emit)
    live = sp.last
    live.kill_result = False  # a tracked grandchild survived
    for rel in ("a.mp4", "b.mp4", "c.mp4"):
        r.translators[0].answer(rel, ok=False)
    assert inst.check(emit).state == "degraded"
    survivor = _keyed(r.evs, f"{IID}:avsubs-survivor")
    assert len(survivor) == 1 and "Task Manager" in survivor[0][2]
    assert gpu_lease.holder() is None  # m3: released anyway
    inst.check(emit)
    assert len(_keyed(r.evs, f"{IID}:avsubs-survivor")) == 1


def test_a_direct_child_that_survives_the_abort_stays_live(tmp_path, monkeypatch):
    # Pilot A rule (c): SubsJob.terminate keeps `child` while the direct child
    # still runs — the job stays live (no launch) until it is reaped.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4", "c.mp4", "d.mp4"])
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    for _ in range(3):
        sp.last.finish(0)
        inst.check(emit)
    live = sp.last
    live.survive = True
    live.kill_result = False
    for rel in ("a.mp4", "b.mp4", "c.mp4"):
        r.translators[0].answer(rel, ok=False)
    inst.check(emit)
    assert gpu_lease.holder() is None
    assert inst._asr_job is inst._jobs["d.mp4"] and inst._asr_job.child is live
    assert _job_dir(r, "d.mp4").is_dir()  # not removed while it may still write
    assert len(_keyed(r.evs, f"{IID}:avsubs-survivor")) == 1
    live.rc = 137  # it finally dies
    inst.check(emit)
    assert inst._asr_job is None and not _job_dir(r, "d.mp4").exists()
    assert len(sp.argvs) == 4 and not _done(r.evs)


def test_stop_never_publishes_for_an_aborted_survivor(tmp_path, monkeypatch):
    # An aborted run's surviving child that later exits 0 is reaped by Stop,
    # but its job is settled (cancelled): no .ja.srt is written for it.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4", "c.mp4", "d.mp4"])
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    for _ in range(3):
        sp.last.finish(0)
        inst.check(emit)
    live = sp.last
    live.survive = True
    for rel in ("a.mp4", "b.mp4", "c.mp4"):
        r.translators[0].answer(rel, ok=False)
    inst.check(emit)
    assert inst._asr_job is inst._jobs["d.mp4"]
    live.finish(0)  # it exits cleanly after all
    inst.stop(timeout=1)
    assert not _ja(r, "d.mp4").exists()
    assert inst._asr_job is None


def test_stop_winning_the_spawn_race_keeps_a_surviving_child_live(
    tmp_path, monkeypatch
):
    # S1 / rule (c): stop() lands during the spawn; the post-spawn kill cannot
    # end the direct child, so it stays `_asr_job` (stop() reaches it again and
    # releases the lease), and a later check reaps it once it has exited.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4"])
    inst = r.inst

    def racing_spawn(argv):
        child = r.spawner(argv)
        child.survive = True
        child.kill_result = False
        inst._stopping.set()  # a concurrent Stop
        return child

    inst._spawn = racing_spawn  # type: ignore[assignment]
    inst.start(r.emit)
    child = r.spawner.last
    job = inst._jobs["a.mp4"]
    assert child.calls[:1] == ["terminate_tree"]  # the post-spawn kill ran
    assert inst._asr_job is job and job.child is child  # kept live (rule c)
    assert gpu_lease.holder() == inst._run  # not released next to a live child
    inst.stop(timeout=1)
    assert child.calls.count("terminate_tree") == 2  # stop() reached it again
    assert gpu_lease.holder() is None  # stop released the lease
    assert inst._run not in gpu_lease.waiters()
    assert inst._asr_job is job
    inst.check(r.emit)
    assert inst._asr_job is job  # still running: nothing to reap
    child.rc = 137  # it finally exits
    inst.check(r.emit)
    assert inst._asr_job is None and job.child is None
    assert "a.mp4" not in inst._settled and not _ja(r, "a.mp4").exists()
    assert len(r.spawner.argvs) == 1  # nothing launched after the Stop


@pytest.mark.parametrize("direct_survives", [True, False])
def test_an_unkillable_child_in_the_launch_exception_path_alerts_survivor(
    tmp_path, monkeypatch, direct_survives
):
    # S2: the exception path kills the spawned child; a False kill raises the
    # one-time survivor alert. A direct child that still runs keeps the lease
    # (rule c) until the poll path reaps it; else the lease goes at once.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4"])
    inst, emit, sp = r.inst, r.emit, r.spawner
    real = SubsJob.start_asr

    def start_then_raise(self, spawn):
        err = real(self, spawn)
        if self.job_id == "a.mp4":
            self.child.kill_result = False
            self.child.survive = direct_survives
            raise RuntimeError("bug after the spawn")
        return err

    monkeypatch.setattr(SubsJob, "start_asr", start_then_raise)
    inst.start(emit)
    first = sp.children[0]
    assert "terminate_tree" in first.calls
    assert inst._settled["a.mp4"] == ("failed", "internal: RuntimeError")
    survivor = _keyed(r.evs, f"{IID}:avsubs-survivor")
    assert len(survivor) == 1 and "Task Manager" in survivor[0][2]
    if direct_survives:
        assert inst._asr_job is inst._jobs["a.mp4"]
        assert gpu_lease.holder() == inst._run  # kept with the live child
        assert len(sp.argvs) == 1  # b waits behind it
        inst.check(emit)
        assert len(sp.argvs) == 1 and inst._asr_job is inst._jobs["a.mp4"]
        first.rc = 137
        inst.check(emit)  # reaped → released → b launches (same run)
    assert inst._asr_job is inst._jobs["b.mp4"]
    assert len(sp.argvs) == 2 and gpu_lease.holder() == inst._run
    assert len(_keyed(r.evs, f"{IID}:avsubs-survivor")) == 1
    inst.stop(timeout=1)


@pytest.mark.parametrize("via", ["flag", "cancel_window"])
@pytest.mark.parametrize("state", ["done", "empty"])
def test_a_check_inside_the_stop_window_never_discards_a_finished_transcript(
    tmp_path, monkeypatch, state, via
):
    # IR8: the child exited 0 unpolled; stop() has set `_stopping` and is still
    # cancelling the translator (1–2.6 s) when the worker's check() runs. That
    # check must leave the job to `_stop_asr`, which publishes its ja (CX5: the
    # empty one for no speech) — it must not reap it and drop the outcome.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    inst = r.inst
    inst.start(r.emit)
    r.spawner.last.finish(0, state=state, text=SRT_JA if state == "done" else "")
    job = inst._jobs["a.mp4"]
    if via == "flag":
        inst._stopping.set()  # exactly the reviewer's repro
        inst.check(r.emit)
        assert inst._asr_job is job and job.child is not None  # left to stop()
    else:
        tr = r.translators[0]
        real_cancel = tr.cancel

        def cancel_with_a_check_inside() -> None:
            assert inst._stopping.is_set()
            inst.check(r.emit)  # the supervisor worker, inside the window
            real_cancel()

        tr.cancel = cancel_with_a_check_inside  # type: ignore[method-assign]
    inst.stop(timeout=2)
    ja = _ja(r, "a.mp4")
    assert ja.exists(), "the finished transcript was discarded"
    if state == "done":
        assert ja.read_text(encoding="utf-8").startswith("1\n")
    else:
        assert ja.read_bytes() == b""
    assert not _zh(r, "a.mp4").exists()
    assert inst._asr_job is None and gpu_lease.holder() is None
    assert inst._survivor_jobs == set()


def test_waiting_text_never_names_this_run_itself(tmp_path, monkeypatch):
    # IR9 (Jasna S5 parity): free and reserved for THIS run → no "held by".
    clk = _Clock()
    gpu_lease._reset_for_tests(clock=clk)
    other = _other_holds("Jasna")
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    r.inst.start(r.emit)
    assert r.inst._waiting_gpu
    assert r.inst._build_status("idle").detail == (
        "waiting for GPU (held by Jasna) · 0/1 done"
    )
    assert gpu_lease.release(other)
    assert gpu_lease.reserved_for() == r.inst._run
    assert gpu_lease.blocking_label() == "AV"  # the lease names the reserved us
    st = r.inst._build_status("idle")
    assert st.detail == "waiting for GPU · 0/1 done"
    assert "held by" not in st.detail
    r.inst.stop(timeout=1)


def test_a_third_failure_settled_by_the_poll_is_degraded_in_that_check(
    tmp_path, monkeypatch
):
    # CX1: three videos each exhaust both ASR attempts; the check that settles
    # the third final failure (inside _poll_asr, after the early abort guard)
    # must already report `degraded`, not `idle`.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4", "c.mp4", "d.mp4"])
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    states = []
    for _ in range(3):
        sp.last.finish(1)
        states.append(inst.check(emit).state)  # retry
        sp.last.finish(1)
        states.append(inst.check(emit).state)  # final failure
    assert inst._aborted
    assert states[:-1] == ["running"] * 5
    st_detail = inst._aborted_detail()
    assert states[-1] == "degraded"
    st = inst.check(emit)
    assert st.state == "degraded" and st.detail == st_detail
    assert len(_keyed(r.evs, f"{IID}:avsubs-aborted")) == 1
    assert len(sp.argvs) == 6 and gpu_lease.holder() is None


def test_idle_note_for_a_plan_of_only_collisions(tmp_path, monkeypatch):
    # K2: no "(0 already have subtitles)"; the collisions are named instead.
    # a.mkv / z.mkv win their targets (a.srt / z.srt exist: done); a.mp4 and
    # z.mp4 collide with them.
    r = _setup(
        tmp_path,
        monkeypatch,
        full=["a.mp4", "z.mp4"],
        zh=["a.mkv", "z.mkv"],
        avsubs_extensions=["mp4", "mkv"],
    )
    r.inst.start(r.emit)
    st = r.inst.check(r.emit)
    assert st.state == "idle"
    assert st.detail == (
        "nothing to subtitle (2 already have subtitles, 2 skipped as name collisions)"
    )
    # done == 0 with only collisions (the winners were not plannable, e.g. a
    # failed stat): no "(0 already have subtitles)".
    loser, owner = tmp_path / "x.mp4", tmp_path / "x.mkv"
    plan = AV.TreePlan([], 0, [(loser, owner)] * 3, ["x.mkv: PermissionError"])
    note = AV._idle_note(plan, "R")
    assert note == "nothing to subtitle (3 skipped as name collisions)"
    assert AV._idle_note(AV.TreePlan([], 1, [], []), "R") == (
        "nothing to subtitle (1 already have subtitles)"
    )
    assert AV._idle_note(AV.TreePlan([], 0, [], []), "R") == ("no video files under R")


def test_a_raising_job_lookup_in_start_asr_still_releases_the_lease(
    tmp_path, monkeypatch
):
    # K3: the pop and the `_jobs` lookup are inside the exception fence.
    _other_holds()
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4"])
    inst = r.inst
    inst.start(r.emit)
    assert inst._waiting_gpu and r.spawner.argvs == []

    class _Boom(dict):
        armed = True

        def __getitem__(self, key):
            if key == "a.mp4" and _Boom.armed:
                _Boom.armed = False
                raise KeyError("lookup blew up")
            return super().__getitem__(key)

    inst._jobs = _Boom(inst._jobs)
    log = _lease_spy(monkeypatch, r.owner)
    assert gpu_lease.release(("other", 999))
    inst.check(r.emit)  # a: acquire → raise → released; b: acquire → launch
    assert _names(log)[:3] == ["release", "acquire", "release"]
    assert "acquire" in _names(log)[3:]
    assert [a[1] for a in r.spawner.argvs] == [str(r.root / "b.mp4")]
    assert inst._asr_job is inst._jobs["b.mp4"]
    assert gpu_lease.holder() == inst._run
    assert all(i.relpath != "a.mp4" for i in inst._queue)  # no endless retry
    # F2: the planned job still reaches a terminal state …
    assert inst._settled["a.mp4"] == ("failed", "internal: KeyError")
    assert len(_keyed(r.evs, f"{IID}:avsubs:a.mp4")) == 1
    # … so `done` can fire once b is finished
    r.spawner.last.finish(0, state="empty", text="")
    inst.check(r.emit)
    done = _done(r.evs)
    assert len(done) == 1 and "Queue: 1/2 done, 1 failed, 0 skipped" in done[0][2]
    inst.stop(timeout=1)


def test_a_raise_inside_the_poll_settles_failed_and_releases_the_lease(
    tmp_path, monkeypatch
):
    # F1 (a): a raise after the child was reaped but before settlement. check()
    # must not raise; the job is failed(internal), the lease is released and
    # the next file launches.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4"])
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    real = SubsJob.publish_ja
    armed = {"on": True}

    def publish_ja_once(self, cues):
        if armed["on"]:
            armed["on"] = False
            raise RuntimeError("publish bug")
        return real(self, cues)

    monkeypatch.setattr(SubsJob, "publish_ja", publish_ja_once)
    log = _lease_spy(monkeypatch, r.owner)
    sp.last.finish(0)
    st = inst.check(emit)  # must not raise
    assert inst._settled["a.mp4"] == ("failed", "internal: RuntimeError")
    assert len(_keyed(r.evs, f"{IID}:avsubs:a.mp4")) == 1
    assert _names(log)[:2] == ["release", "acquire"]  # released, then b's turn
    assert len(sp.argvs) == 2 and sp.argvs[1][1] == str(r.root / "b.mp4")
    assert inst._asr_job is inst._jobs["b.mp4"]
    assert st.state == "running" and st.metrics["current_file"] == "b.mp4"
    sp.last.finish(0, state="empty", text="")
    inst.check(emit)
    assert gpu_lease.holder() is None
    assert len(_done(r.evs)) == 1


def test_the_poll_fence_reaps_an_exited_child_without_taskkill(tmp_path, monkeypatch):
    # IR12: the raise happens before the exited child was reaped. The fence
    # reaps it like `_reap_settled` (poll_asr: kill_tracked + reader join) —
    # never `terminate()`, which would run taskkill under `_launch_lock`.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4"])
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    child = sp.last
    real = SubsJob.poll_asr
    armed = {"on": True}

    def poll_asr_once(self):
        if armed["on"]:
            armed["on"] = False
            raise RuntimeError("poll bug")
        return real(self)

    monkeypatch.setattr(SubsJob, "poll_asr", poll_asr_once)
    child.finish(0)  # exited, still attached to the job
    st = inst.check(emit)  # must not raise
    assert inst._settled["a.mp4"] == ("failed", "internal: RuntimeError")
    assert "terminate_tree" not in child.calls  # no taskkill under the lock
    assert "join_readers" in child.calls  # but its readers were joined
    assert inst._jobs["a.mp4"].child is None
    assert len(sp.argvs) == 2 and inst._asr_job is inst._jobs["b.mp4"]
    assert st.state == "running" and gpu_lease.holder() == inst._run
    assert not _job_dir(r, "a.mp4").exists()
    inst.stop(timeout=1)


def test_a_raise_inside_the_poll_with_a_live_child_keeps_the_lease(
    tmp_path, monkeypatch
):
    # F1 (b): the retry spawned a new child, then something raised. Rule (c):
    # the live direct child keeps `_asr_job` and the lease until it exits and
    # the reap path releases.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4"])
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    real = SubsJob.start_asr

    def retry_then_raise(self, spawn):
        err = real(self, spawn)
        if self.attempt == 2 and self.job_id == "a.mp4":
            raise RuntimeError("bug after the retry spawn")
        return err

    monkeypatch.setattr(SubsJob, "start_asr", retry_then_raise)
    sp.last.finish(1)
    inst.check(emit)  # a's retry spawns, then the raise → fenced
    retry = sp.last
    assert len(sp.argvs) == 2 and retry.rc is None
    assert inst._settled["a.mp4"] == ("failed", "internal: RuntimeError")
    assert inst._asr_job is inst._jobs["a.mp4"] and inst._asr_job.child is retry
    assert gpu_lease.holder() == inst._run  # kept with the live child
    assert _job_dir(r, "a.mp4").is_dir()  # not removed while it may still write
    inst.check(emit)
    assert len(sp.argvs) == 2  # b waits behind it
    retry.rc = 1  # the child exits
    inst.check(emit)  # reaped → released → b launches
    assert inst._asr_job is inst._jobs["b.mp4"]
    assert len(sp.argvs) == 3 and sp.argvs[2][1] == str(r.root / "b.mp4")
    assert not _job_dir(r, "a.mp4").exists()
    inst.stop(timeout=1)


# ── done ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "block", ["none", "queue", "asr", "unsettled", "queued", "in_flight", "results"]
)
def test_each_done_condition_alone_blocks_done(tmp_path, monkeypatch, block):
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    tr = r.translators[0]
    tr.answered.add("a.mp4")  # the translator is idle
    with inst._launch_lock:
        inst._settle("a.mp4", "completed", "", emit)
    inst._run_deferred()
    if block == "queue":
        inst._queue = [object()]  # type: ignore[list-item]
    elif block == "asr":
        inst._asr_job = inst._jobs["a.mp4"]
    elif block == "unsettled":
        inst._settled.pop("a.mp4")
    elif block == "queued":
        tr.answered.discard("a.mp4")
    elif block == "in_flight":
        tr.flight = True
    elif block == "results":
        tr.results.put("pending")
    inst._maybe_done(emit)
    assert len(_done(r.evs)) == (1 if block == "none" else 0)


def test_done_fires_once_and_releases_the_translator(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4"], zh=["z.mp4"])
    r.inst.start(r.emit)
    tr = r.translators[0]
    r.inst.check(r.emit)
    assert not _done(r.evs)
    tr.answer("a.mp4")
    r.inst.check(r.emit)
    r.inst.check(r.emit)
    done = _done(r.evs)
    assert len(done) == 1 and "Queue: 2/2 done, 0 failed, 0 skipped" in done[0][2]
    assert r.inst._translator is None and tr.cancel_calls == 1 and tr.joined
    r.inst.stop(timeout=1)
    r.inst.start(r.emit)  # everything done now
    assert r.inst.check(r.emit).state == "idle"
    assert len(_done(r.evs)) == 1


# ── stop / start ──────────────────────────────────────────────────────────
def test_stop_with_a_live_asr_kills_releases_and_withdraws(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4"])
    r.inst.start(r.emit)
    child = r.spawner.last
    tr = r.translators[0]
    order = r.owner.setdefault("order", [])
    _lease_spy(monkeypatch, r.owner)
    order.clear()
    r.inst.stop(timeout=2)
    assert _names(order) == ["cancel", "kill", "release", "withdraw"], order
    assert tr.cancel_lock_free == [True] and tr.joined
    assert "terminate_tree" in child.calls
    assert gpu_lease.holder() is None
    assert r.inst._asr_job is None
    r.inst.check(r.emit)
    assert not _done(r.evs) and len(r.spawner.argvs) == 1


def test_stop_lock_timeout_branch_kills_without_the_lock(tmp_path, monkeypatch):
    # D13: a stuck lock holder cannot keep the ASR tree (and the lease) alive.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    r.inst.start(r.emit)
    child = r.spawner.last
    held, go = threading.Event(), threading.Event()

    def hog() -> None:
        with r.inst._launch_lock:
            held.set()
            go.wait(10)

    t = threading.Thread(target=hog, daemon=True)
    t.start()
    assert held.wait(5)
    t0 = time.monotonic()
    r.inst.stop(timeout=0.5)
    assert time.monotonic() - t0 < 3.0
    go.set()
    t.join(5)
    assert "terminate_tree" in child.calls
    assert gpu_lease.holder() is None
    assert r.inst._run not in gpu_lease.waiters()


@pytest.mark.parametrize("state", ["empty", "done"])
def test_stop_with_an_exited_unpolled_child_keeps_its_ja_only(
    tmp_path, monkeypatch, state
):
    # CX5: a no-speech result that exited inside the poll window keeps its empty
    # ja; a transcript keeps its ja; the zh is never written by stop.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    r.inst.start(r.emit)
    r.spawner.last.finish(0, state=state, text="" if state == "empty" else SRT_JA)
    r.inst.stop(timeout=2)
    assert _ja(r, "a.mp4").exists()
    if state == "empty":
        assert _ja(r, "a.mp4").read_bytes() == b""
    assert not _zh(r, "a.mp4").exists()
    assert r.translators[0].submitted == []
    assert gpu_lease.holder() is None
    r.inst.check(r.emit)
    assert not _done(r.evs) and not _zh(r, "a.mp4").exists()


def test_stop_while_a_request_hangs_joins_within_the_budget(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4"])
    r.inst.start(r.emit)
    tr = r.translators[0]
    tr.flight = True
    t0 = time.monotonic()
    r.inst.stop(timeout=1.0)
    assert time.monotonic() - t0 < 2.0
    assert tr.cancel_calls == 1 and tr.join_timeouts
    assert 0 < tr.join_timeouts[-1] <= 1.0


def test_immediate_restart_takes_a_new_generation_without_deadlock(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    r.inst.start(r.emit)
    old_run, old_child, old_tr = r.inst._run, r.spawner.last, r.translators[0]
    finished = threading.Event()

    def restart() -> None:
        r.inst.start(r.emit)  # start() stops the leftovers itself
        finished.set()

    t = threading.Thread(target=restart, daemon=True)
    t.start()
    assert finished.wait(10)
    assert "terminate_tree" in old_child.calls
    assert old_tr.cancel_calls == 1 and old_tr.joined
    assert r.inst._run[1] > old_run[1]
    assert gpu_lease.holder() == r.inst._run
    assert len(r.spawner.argvs) == 2
    r.inst.stop(timeout=1)


def test_translator_is_assigned_before_the_post_start_stop_recheck(
    tmp_path, monkeypatch
):
    # CX2
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
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

    monkeypatch.setattr(AV, "Translator", factory)
    r.inst.start(r.emit)
    assert seen == [True]
    assert made[0].cancel_calls == 1 and made[0].joined
    assert r.spawner.argvs == [] and gpu_lease.holder() is None
    r.inst.stop(timeout=1)


def test_start_time_translate_only_submit_rechecks_stopping(tmp_path, monkeypatch):
    # m5: a Stop landing while the .ja.srt files are loaded (outside the lock)
    # must not be followed by a submission.
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4"])
    real = SubsJob.load_ja

    def load_then_stop(self):
        cues = real(self)
        r.inst._stopping.set()
        return cues

    monkeypatch.setattr(SubsJob, "load_ja", load_then_stop)
    r.inst.start(r.emit)
    assert r.translators[0].submitted == []
    assert "a.mp4" not in r.inst._settled


def test_supervisor_register_unregister_register_and_reconfigure(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"], poll_interval=1.0)
    sink: list = []
    sup = Supervisor(sink=lambda *a: sink.append(a))
    plugin = AvsubsPlugin()
    sup.start()
    try:
        sup.register(plugin, r.cfg, IID)
        assert _wait(lambda: len(r.spawner.children) == 1)
        old = sup._monitors[IID].instance
        old_run = old._run
        assert _wait(lambda: gpu_lease.holder() == old_run)
        old_child, old_tr = r.spawner.children[0], r.translators[0]

        cycled = threading.Event()

        def cycle() -> None:
            sup.unregister(IID, timeout=5)
            sup.register(plugin, r.cfg, IID)
            cycled.set()

        t = threading.Thread(target=cycle, daemon=True)
        t.start()
        t.join(timeout=15)
        assert cycled.is_set()
        assert "terminate_tree" in old_child.calls
        assert old_tr.cancel_calls == 1 and old_tr.joined
        assert _wait(lambda: len(r.spawner.children) == 2)
        new = sup._monitors[IID].instance
        assert new is not old and new._run[1] > old_run[1]
        assert _wait(lambda: gpu_lease.holder() == new._run)
        # a late release from the old generation never frees the new hold
        assert gpu_lease.release(old_run) is False
        old._gpu_held_run = old_run
        old._gpu_rel()
        assert gpu_lease.holder() == new._run
        # an old-generation result is ignored
        new_tr = r.translators[1]
        new_tr.results.put(
            TranslateResult(old_run, "a.mp4", "translated", (Cue(1, 0, 9, "旧"),), "")
        )
        assert _wait(lambda: new_tr.results.empty())
        time.sleep(0.2)
        assert not _zh(r, "a.mp4").exists()
        assert "a.mp4" not in new._settled

        cfg2 = r.cfg.model_copy(update={"whisperjav_engine": "large-v3"})
        sup.reconfigure(IID, cfg2)
        assert "terminate_tree" in r.spawner.children[1].calls
        assert new_tr.cancel_calls == 1 and new_tr.joined
        assert _wait(lambda: len(r.spawner.children) == 3)
        newest = sup._monitors[IID].instance
        assert newest._run[1] > new._run[1]
        assert "large-v3" in r.spawner.children[2].argv
        assert _wait(lambda: gpu_lease.holder() == newest._run)
        assert gpu_lease.release(new._run) is False
        assert gpu_lease.holder() == newest._run
    finally:
        sup.stop(timeout=5)
    assert "terminate_tree" in r.spawner.children[-1].calls
    assert gpu_lease.holder() is None


# ── start-time errors / empty runs ────────────────────────────────────────
def test_exe_missing_is_an_error_with_every_job_skipped(tmp_path, monkeypatch):
    # C9
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"], ja=["b.mp4"], exe=False)
    log = _lease_spy(monkeypatch, r.owner)
    r.inst.start(r.emit)
    st = r.inst.check(r.emit)
    assert st.state == "error" and "whisperjav.exe not found" in st.detail
    assert len(_keyed(r.evs, f"{IID}:avsubs-noexe")) == 1
    assert r.inst._settled == {
        "a.mp4": ("skipped", "no_exe"),
        "b.mp4": ("skipped", "no_exe"),
    }
    assert r.translators == [] and r.spawner.argvs == []
    assert not [n for n in _names(log) if n in ("acquire", "refused")]
    r.inst.check(r.emit)
    assert not _done(r.evs)
    assert st.metrics["queue_skipped"] == 2


def test_root_missing_is_a_launch_error(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    cfg = r.cfg.model_copy(update={"avsubs_root_folder": str(tmp_path / "nope")})
    inst = AvsubsInstance(IID, cfg)
    inst.start(r.emit)
    st = inst.check(r.emit)
    assert st.state == "error" and "nope" in st.detail
    assert len(_keyed(r.evs, f"{IID}:launch")) == 1
    assert r.translators == [] and gpu_lease.holder() is None
    not_dir = tmp_path / "file.txt"
    not_dir.write_text("x", encoding="utf-8")
    inst2 = AvsubsInstance(
        "av2", r.cfg.model_copy(update={"avsubs_root_folder": str(not_dir)})
    )
    inst2.start(r.emit)
    assert inst2.check(r.emit).state == "error"


@pytest.mark.parametrize("case", ["empty", "all_done"])
def test_nothing_to_do_is_idle_without_an_event_or_a_translator(
    tmp_path, monkeypatch, case
):
    r = _setup(tmp_path, monkeypatch, zh=["a.mp4"] if case == "all_done" else [])
    r.inst.start(r.emit)
    st = r.inst.check(r.emit)
    assert st.state == "idle"
    assert r.evs == []
    assert r.translators == []
    if case == "empty":
        assert st.detail.startswith("no video files under ")
    else:
        assert st.detail == "nothing to subtitle (1 already have subtitles)"
    assert not [t for t in threading.enumerate() if t.name.startswith("subs-translate")]


def test_collisions_and_scan_errors_alert_once_and_count(tmp_path, monkeypatch):
    r = _setup(
        tmp_path, monkeypatch, full=["a.mp4", "a.mkv"], avsubs_extensions=["mp4", "mkv"]
    )
    r.inst.start(r.emit)
    st = r.inst.check(r.emit)
    assert len(_keyed(r.evs, f"{IID}:collisions")) == 1
    assert st.metrics["queue_failed"] == 1
    assert st.metrics["queue_total"] == 2
    r.inst.stop(timeout=1)


def test_start_sweeps_only_its_own_tmp(tmp_path, monkeypatch):
    # C2: a sibling (Jasna's `.avsubs/tmp`, another avsubs task's) survives.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    own = _staging_root(r) / "tmp" / "chunk.wav"
    jasna = r.root / ".avsubs" / "tmp" / "chunk.wav"
    other = r.root / ".avsubs" / "avsubs-deadbeef" / "tmp" / "chunk.wav"
    for p in (own, jasna, other):
        _touch(p)
    r.inst.start(r.emit)
    assert not own.exists()
    assert jasna.exists() and other.exists()
    r.inst.stop(timeout=1)


def test_start_runs_the_age_gated_temp_sweep_in_planned_folders(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, full=["sub/a.mp4"])
    old = r.root / "sub" / "a.srt.3.tmp"
    fresh = r.root / "sub" / "a.ja.srt.4.tmp"
    for p in (old, fresh):
        p.write_text("x", encoding="utf-8")
    past = time.time() - 3600
    os.utime(old, (past, past))
    r.inst.start(r.emit)
    assert not old.exists() and fresh.exists()
    r.inst.stop(timeout=1)


def test_job_staging_dir_is_removed_after_it_settles(tmp_path, monkeypatch):
    # M11 (and #177 IR-d): attempt dirs do not pile up.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    r.inst.start(r.emit)
    job_dir = _job_dir(r, "a.mp4")
    assert job_dir.is_dir()
    r.spawner.last.finish(0)
    r.inst.check(r.emit)
    assert job_dir.is_dir()  # not settled yet (translation pending)
    r.translators[0].answer("a.mp4")
    r.inst.check(r.emit)
    assert not job_dir.exists()
    assert _staging_root(r).is_dir()


# ── status / GPU lease ────────────────────────────────────────────────────
def test_metrics_phase_and_detail_contract(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, full=["d/a.mp4", "b.mp4"], zh=["z.mp4"])
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)  # b.mp4 first (sorted), then d/a.mp4
    st = inst.check(emit)
    m = st.metrics
    assert st.state == "running"
    assert m["phase"] == "asr" and m["current_file"] == "b.mp4"
    assert m["queue_total"] == 3 and m["queue_completed"] == 1
    assert m["queue_failed"] == 0 and m["queue_skipped"] == 0
    assert m["queue_remaining"] == 2 and m["subs_translating"] == 0
    assert st.detail.startswith("transcribing: b.mp4 [anime-whisper] · 00:")
    assert st.detail.endswith(" elapsed · translating 0 · 1/3 done")
    sp.last.finish(0)
    inst.check(emit)  # b submitted, d/a launched
    st = inst.check(emit)
    assert st.metrics["current_file"] == "d/a.mp4"
    assert "translating 1" in st.detail
    sp.last.finish(0)
    inst.check(emit)  # d/a submitted; queue empty now
    st = inst.check(emit)
    assert st.state == "running" and st.metrics["phase"] == "translate"
    assert "current_file" not in st.metrics
    assert st.detail == "translating 2 · 1/3 done"
    r.translators[0].answer("b.mp4")
    r.translators[0].answer("d/a.mp4")
    st = inst.check(emit)
    assert st.state == "idle" and "phase" not in st.metrics
    assert st.metrics["queue_completed"] == 3 and st.metrics["queue_remaining"] == 0


def test_waiting_for_gpu_detail_phase_and_later_launch(tmp_path, monkeypatch):
    clk = _Clock()
    gpu_lease._reset_for_tests(clock=clk)
    other = _other_holds("Jasna")
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"], ja=["t.mp4"])
    r.inst.start(r.emit)
    assert r.spawner.argvs == [] and r.inst._waiting_gpu
    assert r.inst._run in gpu_lease.waiters()
    st = r.inst.check(r.emit)  # translating t while waiting
    assert st.state == "running" and st.metrics["phase"] == "translate"
    assert st.detail == "translating 1 · 0/2 done · waiting for GPU (held by Jasna)"
    r.translators[0].answer("t.mp4")
    st = r.inst.check(r.emit)
    assert st.state == "idle" and st.metrics["phase"] == "waiting_gpu"
    assert st.detail == "waiting for GPU (held by Jasna) · 1/2 done"
    assert not r.evs  # waiting is not an event
    assert gpu_lease.release(other)
    st = r.inst.check(r.emit)  # reserved for us → launch
    assert len(r.spawner.argvs) == 1 and not r.inst._waiting_gpu
    assert gpu_lease.holder() == r.inst._run
    assert st.metrics["phase"] == "asr"


def test_waiting_detail_names_the_reserved_waiter_while_the_lease_is_free(
    tmp_path, monkeypatch
):
    # D8: free-but-reserved has no holder; the detail names the reserved waiter.
    clk = _Clock()
    gpu_lease._reset_for_tests(clock=clk)
    holder = _other_holds("Other")
    assert not gpu_lease.try_acquire(("jasna", 5), 10.0, label="Jasna")
    assert gpu_lease.release(holder)
    assert gpu_lease.holder() is None and gpu_lease.reserved_for() == ("jasna", 5)
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    r.inst.start(r.emit)
    st = r.inst.check(r.emit)
    assert st.detail == "waiting for GPU (held by Jasna) · 0/1 done"
    assert st.state == "idle" and st.metrics["phase"] == "waiting_gpu"


def test_a_refused_try_while_stopping_withdraws(tmp_path, monkeypatch):
    # D6
    _other_holds()
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    r.inst.start(r.emit)
    assert r.inst._run in gpu_lease.waiters()
    r.inst._stopping.set()
    assert r.inst._gpu_try() is False
    assert r.inst._run not in gpu_lease.waiters()


def test_a_retry_that_finds_nothing_to_do_withdraws(tmp_path, monkeypatch):
    # D12: the GPU is no longer needed → the waiter entry goes (a translation
    # is still pending, so it is not `done`'s withdraw that removes it).
    _other_holds()
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"], ja=["t.mp4"])
    r.inst.start(r.emit)
    assert r.inst._run in gpu_lease.waiters()
    job_id = r.inst._queue[0].relpath
    with r.inst._launch_lock:
        r.inst._queue = []
        r.inst._settle(job_id, "skipped", "cancelled", r.emit)
    r.inst._run_deferred()
    st = r.inst.check(r.emit)
    assert r.inst._run not in gpu_lease.waiters()
    assert not r.inst._waiting_gpu and not _done(r.evs)
    assert st.state == "running" and st.metrics["phase"] == "translate"


def test_stop_while_waiting_withdraws(tmp_path, monkeypatch):
    _other_holds()
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    r.inst.start(r.emit)
    assert r.inst._run in gpu_lease.waiters()
    r.inst.stop(timeout=1)
    assert r.inst._run not in gpu_lease.waiters()
    assert gpu_lease.holder() == ("other", 999)


def test_default_spawn_goes_through_the_module_child_process_with_a_clean_env(
    monkeypatch,
):
    seen: dict = {}

    def fake_child(argv, **kw):
        seen["argv"], seen["kw"] = argv, kw
        return object()

    monkeypatch.setenv("TASKPAW_LLM_API_KEY", "sk-secret")
    monkeypatch.setenv("TASKPAW_KEEP_ME", "1")
    monkeypatch.setattr(AV, "ChildProcess", fake_child)
    AV._default_spawn(["whisperjav.exe", "m.mp4"])
    env = seen["kw"]["env"]
    assert "TASKPAW_LLM_API_KEY" not in env and env["TASKPAW_KEEP_ME"] == "1"
    assert "stdin_pipe" not in seen["kw"]


def test_jobs_are_plain_subs_jobs_keyed_by_relpath(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, full=["x/a.mp4"])
    r.inst.start(r.emit)
    job = r.inst._jobs["x/a.mp4"]
    assert isinstance(job, SubsJob)
    assert job.run == r.inst._run and job.relpath == "x/a.mp4"
    assert job.media == r.root / "x" / "a.mp4"
    assert job.staging_root == _staging_root(r)
    argv = r.spawner.argvs[0]
    assert argv[argv.index("--temp-dir") + 1] == str(_staging_root(r) / "tmp")
    assert job.identity == source_identity(r.root / "x" / "a.mp4")
    r.inst.stop(timeout=1)


# ── #187: the .ja.srt goes once the Chinese .srt is published ─────────────
@pytest.mark.parametrize(
    "case", ["translated", "no_speech", "zero_cue_resume", "library_ja_resume"]
)
def test_ja_is_deleted_once_the_zh_is_published(tmp_path, monkeypatch, case):
    if case in ("translated", "no_speech"):
        r = _setup(tmp_path, monkeypatch, full=["片/a.mp4"])
    else:
        r = _setup(tmp_path, monkeypatch, ja=["片/a.mp4"])
        if case == "zero_cue_resume":
            _ja(r, "片/a.mp4").write_bytes(b"")
    inst, emit = r.inst, r.emit
    inst.start(emit)
    if case == "translated":
        r.spawner.last.finish(0)
        inst.check(emit)
        assert _ja(r, "片/a.mp4").exists()  # the checkpoint while translating
        r.translators[0].answer("片/a.mp4")
    elif case == "no_speech":
        r.spawner.last.finish(0, state="empty", text="")
    elif case == "library_ja_resume":
        # the owner's rule: a pre-existing library .ja.srt goes too
        assert _ja(r, "片/a.mp4").exists() and r.spawner.argvs == []
        r.translators[0].answer("片/a.mp4")
    inst.check(emit)
    assert inst._settled["片/a.mp4"][0] == "completed"
    assert _zh(r, "片/a.mp4").exists()
    assert not _ja(r, "片/a.mp4").exists()
    assert (r.root / "片" / "a.mp4").exists()
    assert len(_done(r.evs)) == 1


@pytest.mark.parametrize(
    "case", ["failed", "zh_publish_failed", "no_key", "no_key_result", "cancelled"]
)
def test_ja_is_kept_as_the_resume_checkpoint(tmp_path, monkeypatch, case):
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4"], key=case != "no_key")
    if case == "zh_publish_failed":
        monkeypatch.setattr(
            SubsJob,
            "publish_zh",
            lambda self, cues: PublishResult("error", f"publish {self.zh_target.name}"),
        )
    inst, emit = r.inst, r.emit
    inst.start(emit)
    if case == "failed":
        r.translators[0].answer("a.mp4", ok=False)
        inst.check(emit)
        assert inst._settled["a.mp4"][0] == "failed"
    elif case == "zh_publish_failed":
        r.translators[0].answer("a.mp4")
        inst.check(emit)
        assert inst._settled["a.mp4"][0] == "failed"
    elif case == "no_key":
        assert inst._settled["a.mp4"] == ("skipped", "no_llm_key")
    elif case == "no_key_result":
        r.translators[0].answer("a.mp4", outcome="no_key")
        inst.check(emit)
        assert inst._settled["a.mp4"] == ("skipped", "no_llm_key")
    else:
        with inst._launch_lock:
            inst._abort(emit)
        inst._run_deferred()
        assert inst._settled["a.mp4"] == ("skipped", "cancelled")
    assert _ja(r, "a.mp4").read_text(encoding="utf-8") == SRT_JA
    assert not _zh(r, "a.mp4").exists()


def _publish_ja_only(self: SubsJob) -> PublishResult:
    """`SubsJob.publish_empty` whose zh half fails: the empty ja is on disk."""
    res = self.publish_ja([])
    return PublishResult("error", f"publish {self.zh_target.name}") if res.ok else res


@pytest.mark.parametrize(
    "case", ["zero_cue_zh_publish_failed", "no_speech_publish_failed"]
)
def test_ja_is_kept_when_the_empty_zh_cannot_be_published(tmp_path, monkeypatch, case):
    # The 0-cue resume and the no-speech outcome settle `completed` only after
    # their (empty) zh is published; when that publish fails the .ja.srt stays
    # as the next Start's checkpoint.
    if case == "no_speech_publish_failed":
        r = _setup(tmp_path, monkeypatch, full=["片/a.mp4"])
        monkeypatch.setattr(SubsJob, "publish_empty", _publish_ja_only)
    else:
        r = _setup(tmp_path, monkeypatch, ja=["片/a.mp4"])
        _ja(r, "片/a.mp4").write_bytes(b"")
        monkeypatch.setattr(
            SubsJob,
            "publish_zh",
            lambda self, cues: PublishResult("error", f"publish {self.zh_target.name}"),
        )
    inst, emit = r.inst, r.emit
    inst.start(emit)
    if case == "no_speech_publish_failed":
        r.spawner.last.finish(0, state="empty", text="")
    inst.check(emit)
    assert inst._settled["片/a.mp4"][0] == "failed"
    assert _ja(r, "片/a.mp4").read_text(encoding="utf-8") == ""
    assert not _zh(r, "片/a.mp4").exists()


def test_a_ja_kept_at_stop_is_resumed_translate_only_and_then_deleted(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    r.spawner.last.finish(0)
    inst.stop(timeout=2)  # Stop publishes the ja only
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
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)  # the ja is loaded and submitted
    ja = _ja(r, "a.mp4")
    ja.unlink()
    ja.mkdir()  # something in the way: the unlink fails
    (ja / "locked").write_text("x", encoding="utf-8")
    r.translators[0].answer("a.mp4")
    with caplog.at_level(logging.WARNING, logger="taskpaw.monitors.avsubs"):
        st = inst.check(emit)  # never raises
    assert inst._settled["a.mp4"] == ("completed", "")
    assert _zh(r, "a.mp4").exists() and ja.is_dir()
    assert any(ja.name in rec.getMessage() for rec in caplog.records)
    done = _done(r.evs)
    assert len(done) == 1 and "Queue: 1/1 done, 0 failed, 0 skipped" in done[0][2]
    assert not _alerts(r.evs)
    assert st.state == "idle"


def test_plan_tree_skips_jasna_staging_names_old_and_new(tmp_path):
    for rel in ("a.mp4", "a-破解.tmp.mp4", "a_restored.tmp.mp4", "b-破解.mp4"):
        _touch(tmp_path / rel)
    assert _rels(plan_tree(str(tmp_path), True, ["mp4"])) == ["a.mp4", "b-破解.mp4"]
    item = plan_tree(str(tmp_path), True, ["mp4"]).items[1]
    assert item.zh_target == tmp_path / "b-破解.srt"  # same name as Jasna's


def test_root_folder_description_states_the_ja_cleanup():
    desc = AvsubsConfig.model_fields["avsubs_root_folder"].description or ""
    assert "<name>.srt" in desc and "<name>.ja.srt" in desc
    assert "deleted" in desc


# ── #189: per-film progress (film / steps / films / model, queue_pre_done) ────
# Observation only; avsubs' queue semantics are unchanged (queue_completed
# still includes the videos that had their .srt at scan = queue_pre_done).
QWEN_TAIL = (
    "[QwenPipeline PID 4242] Phase 1: extracting audio\n"
    "[QwenPipeline PID 4242] Phase 5: decoupled ASR\n"
    "[DecoupledPipeline] Generating scene 3/10 (41.0s)\n"
)
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


def _mono(monkeypatch, t: float = 100.0) -> SimpleNamespace:
    """Freeze `time.monotonic` as the plugin module sees it (only AV's `time`)."""
    clk = SimpleNamespace(t=t)
    fake = SimpleNamespace(monotonic=lambda: clk.t, time=time.time, sleep=time.sleep)
    monkeypatch.setattr(AV, "time", fake)
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


def test_progress_asr_active_with_whisperjav_progress_and_queue_pre_done(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"], zh=["z.mp4"])
    r.inst.start(r.emit)
    r.spawner.last.tail = lambda lines=10, max_chars=800: QWEN_TAIL
    st = r.inst.check(r.emit)
    m = st.metrics
    expect = AsrProgress(0.0)
    expect.feed_text(QWEN_TAIL, 1.0)
    pct = expect.snapshot(1.0)["percent"]
    assert m["film"] == "a.mp4" and m["current_file"] == "a.mp4"
    asr, tr = m["steps"]
    assert asr["key"] == "asr" and asr["state"] == "active"
    assert (asr["phase"], asr["phase_n"], asr["scene"], asr["scenes"]) == (5, 8, 3, 10)
    assert asr["percent"] == pct and isinstance(asr["elapsed_s"], int)
    assert "eta_s" not in asr
    assert tr == {"key": "translate", "state": "pending"}
    assert m["films"] == [
        {
            "name": "a.mp4",
            "steps": {"asr": "active", "translate": "pending"},
            "status": "active",
            "percent": pct,
            "eta_s": None,
            "duration_s": None,
        }
    ]  # z.mp4 had its .srt at scan: counted, never a tracker film
    assert m["films_more"] == 0 and "model" not in m
    assert m["queue_pre_done"] == 1
    assert (m["queue_completed"], m["queue_remaining"]) == (1, 1)  # unchanged
    assert st.detail.endswith(" elapsed · translating 0 · 1/2 done")
    r.inst.stop(timeout=1)


def test_progress_translate_active_shows_the_model_then_done(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    r.spawner.last.finish(0)
    st = inst.check(emit)  # ja published, submitted
    assert _row(st.metrics, "a.mp4")["steps"] == {"asr": "done", "translate": "queued"}
    assert _row(st.metrics, "a.mp4")["status"] == "queued"
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
    assert st.detail == "translating 1 · 0/1 done"
    tr.live = None
    tr.answer("a.mp4")
    st = inst.check(emit)
    m = st.metrics
    assert "model" not in m
    assert [s["state"] for s in m["steps"]] == ["done", "done"]
    assert all(isinstance(s.get("duration_s"), int) for s in m["steps"])
    assert _rows(m) == [("a.mp4", "done")]
    assert (m["queue_completed"], m["queue_pre_done"]) == (1, 0)


def test_progress_translate_only_films_are_queued_from_start(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, ja=["t.mp4"])
    r.inst.start(r.emit)
    m = r.inst.check(r.emit).metrics
    assert m["film"] == "t.mp4"
    assert m["steps"] == [
        {"key": "asr", "state": "done"},  # an existing .ja.srt: no duration
        {"key": "translate", "state": "queued"},
    ]
    assert _rows(m) == [("t.mp4", "queued")]


def test_progress_waiting_gpu_holder_waited_s_and_reset(tmp_path, monkeypatch):
    clk = _Clock()
    gpu_lease._reset_for_tests(clock=clk)
    mono = _mono(monkeypatch)
    other = _other_holds("Jasna")
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    st = inst.check(emit)
    assert st.metrics["film"] == "a.mp4"
    assert _step(st.metrics, "asr") == {
        "key": "asr",
        "state": "waiting_gpu",
        "holder": "Jasna",
        "waited_s": 0,
    }
    assert _rows(st.metrics) == [("a.mp4", "waiting_gpu"), ("b.mp4", "pending")]
    mono.t += 20
    st = inst.check(emit)  # N6: the refusal flag toggles inside check()
    assert _step(st.metrics, "asr")["waited_s"] == 20
    assert gpu_lease.release(other)
    assert gpu_lease.reserved_for() == inst._run
    st = inst._build_status("idle")  # N5: free and reserved for THIS run
    assert _step(st.metrics, "asr") == {
        "key": "asr",
        "state": "waiting_gpu",
        "holder": "",
        "waited_s": 20,
    }
    assert st.detail == "waiting for GPU · 0/2 done"
    st = inst.check(emit)  # takes it
    assert _step(st.metrics, "asr")["state"] == "active"
    assert "waited_s" not in _step(st.metrics, "asr")
    assert not gpu_lease.try_acquire(other, 10.0, label="Jasna")  # Jasna waits
    r.spawner.last.finish(0)
    mono.t += 100
    st = inst.check(emit)  # a's ASR ends → reserved for Jasna → b waits
    assert inst._waiting_gpu and st.metrics["film"] == "b.mp4"
    assert _step(st.metrics, "asr") == {
        "key": "asr",
        "state": "waiting_gpu",
        "holder": "Jasna",
        "waited_s": 0,  # a new wait starts from zero
    }
    mono.t += 5
    assert _step(inst.check(emit).metrics, "asr")["waited_s"] == 5
    inst.stop(timeout=1)


def test_progress_mixed_run_rows_match_the_queue_counts(tmp_path, monkeypatch):
    # a completes; b's ASR fails twice; c.mkv changes during ASR (skipped);
    # c.mp4 collides with c.mkv; t is translate-only and its translation
    # fails; z had its .srt at scan.
    r = _setup(
        tmp_path,
        monkeypatch,
        full=["a.mp4", "b.mp4", "c.mkv", "c.mp4"],
        ja=["t.mp4"],
        zh=["z.mp4"],
        avsubs_extensions=["mp4", "mkv"],
    )
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)  # t submitted; ASR a
    assert [f["name"] for f in inst._tracker.view(LiveFacts(), 0.0)["films"]] == [
        "a.mp4",
        "b.mp4",
        "c.mkv",
        "t.mp4",
    ]  # plan order; the collision and the pre-done video are not films
    sp.last.finish(0)
    inst.check(emit)  # a submitted; ASR b
    sp.last.finish(1)
    inst.check(emit)  # b retried
    sp.last.finish(1)
    inst.check(emit)  # b failed; ASR c.mkv
    (r.root / "c.mkv").write_bytes(b"changed")
    sp.last.finish(0)
    inst.check(emit)  # c.mkv unstable → skipped
    tr = r.translators[0]
    tr.answer("a.mp4")
    tr.answer("t.mp4", ok=False)
    st = inst.check(emit)
    m = st.metrics
    statuses = inst._tracker.statuses(LiveFacts())
    assert statuses == {
        "a.mp4": "done",
        "b.mp4": "failed",
        "c.mkv": "skipped",
        "t.mp4": "failed",
    }
    assert {n: _states(inst, n) for n in statuses} == {
        "a.mp4": {"asr": "done", "translate": "done"},
        "b.mp4": {"asr": "failed", "translate": "skipped"},
        "c.mkv": {"asr": "skipped", "translate": "skipped"},
        "t.mp4": {"asr": "done", "translate": "failed"},
    }
    collisions = 1

    def count(state: str) -> int:
        return sum(1 for s in statuses.values() if s == state)

    assert m["queue_completed"] - m["queue_pre_done"] == count("done") == 1
    assert m["queue_skipped"] == count("skipped") == 1
    assert m["queue_failed"] - collisions == count("failed") == 2
    done = _done(r.evs)
    assert len(done) == 1
    assert "AV 翻译 complete | Queue: 2/6 done, 3 failed, 1 skipped" in done[0][2]


def test_progress_no_exe_at_start_every_film_is_skipped(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"], ja=["t.mp4"], exe=False)
    r.inst.start(r.emit)
    st = r.inst.check(r.emit)
    assert st.state == "error"
    assert [(f["name"], f["status"], f["steps"]) for f in st.metrics["films"]] == [
        ("a.mp4", "skipped", {"asr": "skipped", "translate": "skipped"}),
        ("t.mp4", "skipped", {"asr": "done", "translate": "skipped"}),
    ]
    assert st.metrics["queue_skipped"] == 2


def test_progress_abort_skips_a_queued_translation_not_done(tmp_path, monkeypatch):
    # R1: asr done + translate skipped (the abort) → the row is `skipped`.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4"])
    inst, emit, sp = r.inst, r.emit, r.spawner
    inst.start(emit)
    sp.last.finish(0)
    inst.check(emit)  # a submitted; ASR b
    inst._streak = 2
    sp.last.finish(1)
    inst.check(emit)  # b retried
    sp.last.finish(1)
    st = inst.check(emit)  # b failed → third consecutive failure → abort
    assert st.state == "degraded"
    assert st.detail == (
        "AV 翻译 aborted after 3 consecutive failures · 0/2 done, 1 failed, 1 skipped"
    )
    assert [(f["name"], f["status"], f["steps"]) for f in st.metrics["films"]] == [
        ("a.mp4", "skipped", {"asr": "done", "translate": "skipped"}),
        ("b.mp4", "failed", {"asr": "failed", "translate": "skipped"}),
    ]


def test_progress_rows_keep_the_focus_and_plan_order_under_the_cap(
    tmp_path, monkeypatch
):
    # N4: 30 queued translate-only films sort before the ASR film.
    names = [f"a{i:02d}.mp4" for i in range(30)]
    r = _setup(tmp_path, monkeypatch, ja=names, full=["z.mp4"])
    r.inst.start(r.emit)
    assert len(r.translators[0].submitted) == 30
    m = r.inst.check(r.emit).metrics
    assert m["film"] == "z.mp4"
    shown = [f["name"] for f in m["films"]]
    assert len(shown) == 12 and shown[-1] == "z.mp4"
    assert shown == sorted(shown)  # plan order
    assert m["films_more"] == 19
    r.inst.stop(timeout=1)


def test_progress_settle_mark_survives_a_raising_abort_alert(tmp_path, monkeypatch):
    # M1: `_settle` marks before `_abort` can emit.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    inst = r.inst

    def emit(level, title, message, data=None, dedupe_key=None):
        if "AV 翻译 aborted" in title:
            raise RuntimeError("sink down")
        r.evs.append((level, title, message, dedupe_key))

    inst.start(emit)
    inst._streak = 2
    with inst._launch_lock:
        with pytest.raises(RuntimeError):
            inst._settle("a.mp4", "failed", "exit code 1", emit)
    assert _states(inst, "a.mp4") == {"asr": "failed", "translate": "skipped"}
    inst.stop(timeout=1)


def test_progress_restart_takes_a_fresh_tracker(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    r.inst.start(r.emit)
    old = r.inst._tracker
    r.inst.stop(timeout=1)
    r.inst.start(r.emit)
    assert r.inst._tracker is not old
    m = r.inst.check(r.emit).metrics
    assert _rows(m) == [("a.mp4", "active")]
    r.inst.stop(timeout=1)


# ── #191: recognise existing subtitles, fail closed, never overwrite ──────────
LIBRARY_ZH = "1\n00:00:00,000 --> 00:00:01,000\n店主的中文字幕\n"
LIBRARY_JA = SRT_JA.replace("はい", "店主")


def _put(path: Path, text: str = LIBRARY_ZH) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _refuse_listing(monkeypatch, folder: Path, exc: BaseException) -> None:
    """`os.scandir(folder)` raises `exc` from now on (e.g. the NAS dropped)."""
    real, key = os.scandir, str(folder)

    def scandir(path="."):
        if os.fspath(path) == key:
            raise exc
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)


def _gpu_turn(tmp_path, monkeypatch, **kw) -> SimpleNamespace:
    """A Start whose first ASR waits for the GPU another task holds — the
    window in which the library changes before the AC5 re-check."""
    other = _other_holds("Jasna")
    r = _setup(tmp_path, monkeypatch, **kw)
    r.inst.start(r.emit)
    assert r.spawner.argvs == [] and r.inst._waiting_gpu
    r.other = other
    return r


EVIDENCE = {
    "LMNO-047": ["LMNO-047-破解-C-4K.mp4", "LMNO-047-破解-4K-C.zh.srt"],
    "LMNO-079": ["LMNO-079-破解-C-4K.mp4", "LMNO-079-破解-C-4K-C.zh.srt"],
    "PQRS-218": ["PQRS-218.mp4", "PQRS-218.chs.srt"],
    "PQRS-860": ["PQRS-860-破解-C.mp4", "PQRS-860-破解-C.chs.srt"],
    "PQRS-948": [
        "PQRS-948-破解-C.mp4",
        "PQRS-948-破解-C.chs.srt",
        "dl.example.com@pqrs00948.srt",
    ],
    "LMNO-005": ["LMNO-005-破解-C-4K.mp4", "LMNO-005-破解-C-4K.srt"],
    "DEFG-594": [f"DEFG-594-cd{i}.mp4" for i in range(1, 5)]
    + [f"DEFG-594-cd{i}.srt" for i in range(1, 4)],
    "HIJK-100": [f"HIJK-100-cd{i}.mp4" for i in range(1, 6)]
    + [f"HIJK-100-cd{i}.srt" for i in range(1, 6)],
    "NEW-001": ["NEW-001.mp4"],
    "NEW-002": ["NEW-002.mp4", "NEW-002.ja.srt"],
    "ABC-003": ["ABC-003.mp4", "ABC-003.jpn.zh.srt"],
    "MIX": ["Movie.mp4", "Movie.part2.mkv", "Movie.part2.srt"],
}


def test_plan_tree_judges_every_film_from_its_folder_listing(tmp_path, monkeypatch):
    for folder, names in EVIDENCE.items():
        for n in names:
            _touch(tmp_path / folder / n, b"x")

    def no_probe(self, *a, **k):  # AC2/C2: no per-file subtitle probe remains
        raise AssertionError(f"probed {self}")

    with monkeypatch.context() as m:
        m.setattr(Path, "exists", no_probe)
        plan = plan_tree(str(tmp_path), True, ["mp4"])
    assert plan.errors == [] and plan.collisions == []
    assert plan.done == 14  # 6 single films + DEFG cd1–cd3 + HIJK cd1–cd5
    assert [(i.relpath, i.kind) for i in plan.items] == [
        ("ABC-003/ABC-003.mp4", "full"),  # a Japanese tag never counts
        ("DEFG-594/DEFG-594-cd4.mp4", "full"),  # each part only its own
        ("MIX/Movie.mp4", "full"),  # Movie.part2.srt belongs to the .mkv (F17)
        ("NEW-001/NEW-001.mp4", "full"),
        ("NEW-002/NEW-002.mp4", "translate_only"),
    ]


def test_plan_tree_translates_a_ja_token_film_from_its_own_transcript(tmp_path):
    # IR1: `JA-001.mp4` alone with its own `JA-001.ja.srt` is NOT subtitled — (c)
    # never re-reads an attributed subtitle — so it is planned translate_only.
    _touch(tmp_path / "JA-001" / "JA-001.mp4", b"x")
    _touch(tmp_path / "JA-001" / "JA-001.ja.srt", b"x")
    plan = plan_tree(str(tmp_path), True, ["mp4"])
    assert plan.done == 0
    assert [(i.relpath, i.kind) for i in plan.items] == [
        ("JA-001/JA-001.mp4", "translate_only")
    ]


def test_idle_note_counts_the_films_that_already_have_subtitles(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch)
    _touch(r.root / "PQRS-218.mp4")
    _put(r.root / "PQRS-218.chs.srt")
    r.inst.start(r.emit)
    st = r.inst.check(r.emit)
    assert st.detail == "nothing to subtitle (1 already have subtitles)"
    assert st.metrics["queue_pre_done"] == 1 and r.evs == []
    assert (r.root / "PQRS-218.chs.srt").read_text(encoding="utf-8") == LIBRARY_ZH


def test_a_subtitle_dropped_in_before_the_asr_skips_and_hands_the_gpu_on(
    tmp_path, monkeypatch, caplog
):
    r = _gpu_turn(tmp_path, monkeypatch, full=["a/a.mp4", "b/b.mp4"])
    sub = _put(r.root / "a" / "a.chs.srt")
    assert gpu_lease.release(r.other)  # reserved for us now
    third = ("third", 7)
    assert not gpu_lease.try_acquire(third, 10.0, label="Other")  # behind us
    with caplog.at_level(logging.INFO, logger="taskpaw.monitors.avsubs"):
        st = r.inst.check(r.emit)
    assert r.inst._settled["a/a.mp4"] == ("skipped", "subtitle exists")
    assert r.spawner.argvs == []  # no ASR for a
    assert gpu_lease.holder() is None
    assert gpu_lease.reserved_for() == third  # the freed turn went on
    assert r.inst._waiting_gpu  # b waits for its own turn
    assert sub.read_text(encoding="utf-8") == LIBRARY_ZH
    assert not _ja(r, "a/a.mp4").exists()
    assert not _alerts(r.evs)  # an info line, never an alert
    lines = [rec.getMessage() for rec in caplog.records]
    assert sum("a/a.mp4" in m and "subtitle exists" in m for m in lines) == 1
    assert st.metrics["queue_skipped"] == 1
    assert _states(r.inst, "a/a.mp4") == {"asr": "skipped", "translate": "skipped"}
    assert gpu_lease.release(third) is False  # not granted yet: reserved only
    assert gpu_lease.try_acquire(third, 10.0) and gpu_lease.release(third)
    r.inst.check(r.emit)  # b's turn now
    assert len(r.spawner.argvs) == 1 and r.spawner.argvs[0][1] == str(
        r.root / "b" / "b.mp4"
    )
    r.inst.stop(timeout=1)


@pytest.mark.parametrize(
    "exc", [PermissionError(13, "denied"), OSError(53, "net"), RuntimeError("bug")]
)
def test_an_unreadable_folder_before_the_asr_skips_with_one_alert(
    tmp_path, monkeypatch, exc
):
    # AC5 + F14 (the listing helper swallows anything) + F16 (one alert/run).
    r = _gpu_turn(tmp_path, monkeypatch, full=["a.mp4", "b.mp4"])
    _refuse_listing(monkeypatch, r.root, exc)
    r.inst._streak = 2
    assert gpu_lease.release(r.other)
    log = _lease_spy(monkeypatch, r.owner)
    st = r.inst.check(r.emit)
    for rel in ("a.mp4", "b.mp4"):
        assert r.inst._settled[rel] == ("skipped", "subtitle state unreadable")
    assert r.spawner.argvs == []
    alerts = _keyed(r.evs, f"{IID}:avsubs-unreadable")
    assert len(alerts) == 1 and "a.mp4" in alerts[0][2]
    assert _names(log).count("acquire") == 2  # each film's own turn...
    assert _names(log).count("release") == 2  # ...released every time
    assert gpu_lease.holder() is None
    assert r.inst._streak == 2 and not r.inst._aborted  # a skip never counts
    assert st.metrics["queue_skipped"] == 2
    done = _done(r.evs)
    assert len(done) == 1 and "Queue: 0/2 done, 0 failed, 2 skipped" in done[0][2]


@pytest.mark.parametrize("case", ["subtitle", "unreadable"])
def test_a_full_film_is_rechecked_before_its_translation(tmp_path, monkeypatch, case):
    # AC5 (F15): under the lock, right after its .ja.srt was published.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4"])
    r.inst.start(r.emit)
    assert len(r.spawner.argvs) == 1  # a transcribing
    if case == "subtitle":
        sub = _put(r.root / "a.zh.srt")
    else:
        _refuse_listing(monkeypatch, r.root, PermissionError(13, "denied"))
    r.spawner.last.finish(0)
    r.inst.check(r.emit)
    assert r.translators[0].submitted == []
    assert _states(r.inst, "a.mp4") == {"asr": "done", "translate": "skipped"}
    if case == "subtitle":
        assert r.inst._settled["a.mp4"] == ("skipped", "subtitle exists")
        assert not _ja(r, "a.mp4").exists()  # ours (this run's) goes, AC6
        assert sub.read_text(encoding="utf-8") == LIBRARY_ZH
        assert not _alerts(r.evs)
        assert len(r.spawner.argvs) == 2  # b is not affected by a.zh.srt
    else:
        assert r.inst._settled["a.mp4"] == ("skipped", "subtitle state unreadable")
        assert _ja(r, "a.mp4").read_text(encoding="utf-8").count("-->") == 2  # kept
        assert r.inst._settled["b.mp4"] == ("skipped", "subtitle state unreadable")
        assert len(_keyed(r.evs, f"{IID}:avsubs-unreadable")) == 1
        assert gpu_lease.holder() is None
    r.inst.stop(timeout=1)


def test_translate_only_films_submitted_at_start_are_not_relisted(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, ja=["t.mp4", "u.mp4"])
    calls: list = []
    real = AV.list_names
    monkeypatch.setattr(AV, "list_names", lambda f: calls.append(f) or real(f))
    r.inst.start(r.emit)
    assert [q.job_id for q in r.translators[0].submitted] == ["t.mp4", "u.mp4"]
    assert calls == []


@pytest.mark.parametrize("kind", ["full", "translate_only"])
def test_a_refused_srt_skips_and_discards_only_this_runs_transcript(
    tmp_path, monkeypatch, kind
):
    # AC6: the owner's .srt appeared while translating → never replaced.
    if kind == "full":
        r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4"])
        r.inst.start(r.emit)
        r.spawner.last.finish(0)
        r.inst.check(r.emit)  # a: ja published + submitted; b transcribing
    else:
        r = _setup(tmp_path, monkeypatch, ja=["a.mp4", "b.mp4"])
        _ja(r, "a.mp4").write_text(LIBRARY_JA, encoding="utf-8")
        r.inst.start(r.emit)
    assert [q.job_id for q in r.translators[0].submitted][0] == "a.mp4"
    zh = _put(_zh(r, "a.mp4"))
    r.inst._streak = 2
    r.translators[0].answer("a.mp4")
    st = r.inst.check(r.emit)
    assert r.inst._settled["a.mp4"] == ("skipped", "subtitle exists")
    assert zh.read_text(encoding="utf-8") == LIBRARY_ZH  # byte-identical
    if kind == "full":
        assert not _ja(r, "a.mp4").exists()  # this run's own transcript
    else:
        assert _ja(r, "a.mp4").read_text(encoding="utf-8") == LIBRARY_JA
    assert not list(r.root.glob("*.tmp"))
    assert r.inst._streak == 2 and not r.inst._aborted
    assert not _alerts(r.evs)
    assert st.metrics["queue_skipped"] == 1
    assert _states(r.inst, "a.mp4") == {"asr": "done", "translate": "skipped"}
    r.inst.stop(timeout=1)


def test_a_transcript_that_appeared_mid_run_is_never_replaced(tmp_path, monkeypatch):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4", "b.mp4"])
    r.inst.start(r.emit)
    lib = _put(_ja(r, "a.mp4"), LIBRARY_JA)
    r.spawner.last.finish(0)
    r.inst.check(r.emit)
    assert r.inst._settled["a.mp4"] == ("skipped", "transcript exists")
    assert lib.read_text(encoding="utf-8") == LIBRARY_JA
    assert r.translators[0].submitted == [] and not _alerts(r.evs)
    assert _states(r.inst, "a.mp4") == {"asr": "skipped", "translate": "skipped"}
    r.inst.stop(timeout=1)
    r.inst.start(r.emit)  # the next Start only translates it
    assert r.inst._kinds["a.mp4"] == "translate_only"
    assert lib.read_text(encoding="utf-8") == LIBRARY_JA
    r.inst.stop(timeout=1)


def test_no_speech_with_a_srt_that_appeared_removes_only_our_empty_transcript(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    r.inst.start(r.emit)
    zh = _put(_zh(r, "a.mp4"))
    r.spawner.last.finish(0, state="empty", text="")
    r.inst.check(r.emit)
    assert r.inst._settled["a.mp4"] == ("skipped", "subtitle exists")
    assert zh.read_text(encoding="utf-8") == LIBRARY_ZH
    assert not _ja(r, "a.mp4").exists()
    assert _states(r.inst, "a.mp4") == {"asr": "done", "translate": "skipped"}
    done = _done(r.evs)
    assert len(done) == 1 and "Queue: 0/1 done, 0 failed, 1 skipped" in done[0][2]


def test_a_zero_cue_resume_whose_srt_appeared_keeps_the_library_transcript(
    tmp_path, monkeypatch
):
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4"])
    _ja(r, "a.mp4").write_bytes(b"")
    real = SubsJob.load_ja

    def load_then_drop(self):
        cues = real(self)
        _put(self.zh_target)  # the owner's .srt lands right then
        return cues

    monkeypatch.setattr(SubsJob, "load_ja", load_then_drop)
    r.inst.start(r.emit)
    assert r.inst._settled["a.mp4"] == ("skipped", "subtitle exists")
    assert _zh(r, "a.mp4").read_text(encoding="utf-8") == LIBRARY_ZH
    assert _ja(r, "a.mp4").read_bytes() == b""  # the library's, kept


def test_stop_never_replaces_a_transcript_that_appeared(tmp_path, monkeypatch, caplog):
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    r.inst.start(r.emit)
    r.spawner.last.finish(0)  # exited, unpolled
    lib = _put(_ja(r, "a.mp4"), LIBRARY_JA)
    with caplog.at_level(logging.WARNING, logger="taskpaw.monitors.avsubs"):
        r.inst.stop(timeout=2)
    assert lib.read_text(encoding="utf-8") == LIBRARY_JA
    assert not _zh(r, "a.mp4").exists()
    assert any("a.ja.srt already exists" in m.getMessage() for m in caplog.records)


def test_root_folder_description_states_the_library_rules():
    desc = AvsubsConfig.model_fields["avsubs_root_folder"].description or ""
    for text in (".chs.srt", "Japanese", "only this one video", "never overwritten"):
        assert text in desc, text
    assert "cannot be read" in desc
    assert "same-named .srt" not in desc


@pytest.mark.parametrize(
    "video, ja_name",
    [("t.mp4", "T.JA.SRT"), ("caf\u00e9.mp4", "cafe\u0301.ja.srt")],
    ids=["case", "nfd"],
)
def test_a_translate_only_job_loads_the_transcript_as_it_is_named(
    tmp_path, monkeypatch, video, ja_name
):
    # CX1: a transcript matched only after normalisation (case / Unicode form)
    # is loaded by its actual name — never a reconstructed `<stem>.ja.srt` that
    # is not there (which would fail as "unreadable .ja.srt" on every Start).
    r = _setup(tmp_path, monkeypatch, full=[video])
    (r.root / ja_name).write_text(SRT_JA, encoding="utf-8")
    plan = plan_tree(str(r.root), True, ["mp4"])
    assert [(i.relpath, i.kind, i.ja_target.name) for i in plan.items] == [
        (video, "translate_only", ja_name)
    ]
    r.inst.start(r.emit)
    assert [q.job_id for q in r.translators[0].submitted] == [video]
    assert r.spawner.argvs == []


# ── #192/#190: resumable translation with fallback models ─────────────────
DS = LLMSettings("https://api.deepseek.com/v1", "deepseek-chat", "sk-ds", "config")
SRT_90 = srt.serialize(
    [Cue(i + 1, i * 1000, i * 1000 + 500, f"台詞{i}") for i in range(90)]
)


def test_the_translator_checkpoints_under_the_data_dir_only_when_one_is_set(
    tmp_path, monkeypatch
):
    # C5: no data dir (every other test) = memory only, never a real folder.
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4"])
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
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4"], key=False)
    if case == "fallback_only":
        set_llm_chain((DS,))
    else:
        set_llm_settings(LLMSettings("https://api.x.ai/v1", "", "sk-test", "config"))
    r.inst.start(r.emit)
    tr = r.translators[0]
    if case == "fallback_only":
        assert [q.job_id for q in tr.submitted] == ["a.mp4"]
        assert "a.mp4" not in r.inst._settled
        assert not _keyed(r.evs, f"{IID}:avsubs-nokey")
    else:
        assert tr.submitted == []
        assert r.inst._settled["a.mp4"] == ("skipped", "no_llm_key")
        assert len(_keyed(r.evs, f"{IID}:avsubs-nokey")) == 1


def test_a_paused_film_is_skipped_streak_neutral_with_one_alert_per_run(
    tmp_path, monkeypatch
):
    # AC8: no translation service for 2 h → skipped `translation_paused` (its
    # .ja.srt and checkpoint kept), streak neutral, ONE alert per run, and the
    # done text counts them.
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4", "b.mp4", "c.mp4", "d.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    tr = r.translators[0]
    tr.answer("a.mp4", ok=False)
    inst.check(emit)
    assert inst._streak == 1
    tr.answer("b.mp4", outcome="paused")
    tr.answer("c.mp4", outcome="paused")
    inst.check(emit)
    for rel in ("b.mp4", "c.mp4"):
        assert inst._settled[rel] == ("skipped", "translation_paused")
        assert _ja(r, rel).exists() and not _zh(r, rel).exists()
    assert inst._streak == 1  # neither counted nor reset
    alerts = _keyed(r.evs, f"{IID}:translation-paused")
    assert len(alerts) == 1 and alerts[0][1] == "AV: translation paused"
    assert alerts[0][2] == paused_alert_message()  # no count (IR2)
    assert not _keyed(r.evs, f"{IID}:avsubs:b.mp4")
    tr.answer("d.mp4", outcome="paused")
    st = inst.check(emit)
    assert len(_keyed(r.evs, f"{IID}:translation-paused")) == 1  # once per run
    assert not inst._aborted and tr.discarded == []
    assert st.metrics["queue_skipped"] == 3
    done = _done(r.evs)
    assert len(done) == 1
    assert "Queue: 0/4 done, 1 failed, 3 skipped; 3 paused | " in done[0][2]


@pytest.mark.parametrize("outcome", ["paused", "no_key"])
def test_paused_and_no_key_films_settle_their_rows_skipped(
    tmp_path, monkeypatch, outcome
):
    # #189: the new skips end the film's row like any other skip.
    r = _setup(tmp_path, monkeypatch, full=["a.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    r.spawner.last.finish(0)
    inst.check(emit)  # ja published, submitted
    r.translators[0].answer("a.mp4", outcome=outcome)
    st = inst.check(emit)
    reason = "translation_paused" if outcome == "paused" else "no_llm_key"
    assert inst._settled["a.mp4"] == ("skipped", reason)
    assert _states(inst, "a.mp4") == {"asr": "done", "translate": "skipped"}
    assert _rows(st.metrics) == [("a.mp4", "skipped")]
    assert _ja(r, "a.mp4").exists()
    assert len(_done(r.evs)) == 1


def test_kept_japanese_lines_are_logged_per_film_and_suffix_the_done_text(
    tmp_path, monkeypatch, caplog
):
    # AC9: counts only (never a line's text); one info line per film with any.
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4", "b.mp4", "c.mp4", "d.mp4"])
    inst, emit = r.inst, r.emit
    inst.start(emit)
    tr = r.translators[0]
    tr.answer("a.mp4", kept_ja=2)
    tr.answer("b.mp4")
    tr.answer("c.mp4", kept_ja=1)
    tr.answer("d.mp4", outcome="paused")
    with caplog.at_level(logging.INFO, logger="taskpaw.monitors.avsubs"):
        inst.check(emit)
    kept = [m for m in caplog.messages if "kept in Japanese" in m]
    assert len(kept) == 2
    assert "a.mp4: 2 line(s) kept in Japanese" in kept[0]
    assert "c.mp4: 1 line(s) kept in Japanese" in kept[1]
    assert not any("はい" in m or "好" in m for m in caplog.messages)
    done = _done(r.evs)
    assert len(done) == 1
    assert (
        "Queue: 3/4 done, 0 failed, 1 skipped; 3 lines kept in Japanese; 1 paused | "
        in done[0][2]
    )


def test_the_paused_alert_is_raised_once_in_each_run(tmp_path, monkeypatch):
    # AC8 (D-SR5): the once-per-run flag resets at Start — films paused again
    # in the next run alert again, once.
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4", "b.mp4"])
    inst, emit = r.inst, r.emit
    for run in (1, 2):
        inst.start(emit)
        tr = r.translators[-1]
        for film in ("a.mp4", "b.mp4"):
            tr.answer(film, outcome="paused")
            inst.check(emit)
        assert len(_keyed(r.evs, f"{IID}:translation-paused")) == run
        assert len(_done(r.evs)) == run
        inst.stop(timeout=1)


def test_the_done_suffix_and_the_paused_alert_text():
    # D-SR7: "1 line" / "N lines"; IR2: the one per-run alert names no count
    # (it is raised at the first pause) — the done text carries it.
    assert translation_suffix(0, 0) == ""
    assert translation_suffix(1, 0) == "; 1 line kept in Japanese"
    assert translation_suffix(2, 1) == "; 2 lines kept in Japanese; 1 paused"
    assert translation_suffix(0, 3) == "; 3 paused"
    assert paused_alert_message() == (
        "Some files were paused: no translation service was available for 2 h. "
        "Their progress is kept; the next Start continues them. "
        "The run summary gives the count."
    )


def test_translator_notices_become_alerts_with_their_dedupe_keys(tmp_path, monkeypatch):
    # AC6/AC3: a provider that opened, a checkpoint that cannot be written —
    # raised once by the translator, alerted under `<iid>:<notice key>`.
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4"])
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
            f"AV: translation model unavailable: {grok}",
            "w",
            f"{IID}:llm-provider:{grok}",
        ),
        (
            "alert",
            "AV: translation checkpoint not saved",
            "disk",
            f"{IID}:checkpoint-write",
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
    assert len(_keyed(r.evs, f"{IID}:llm-provider:{ds}")) == 1
    assert len(_done(r.evs)) == 1


@pytest.mark.parametrize(
    "case", ["published", "srt_exists", "publish_error", "failed", "paused", "no_key"]
)
def test_the_checkpoint_is_discarded_only_after_the_zh_was_published(
    tmp_path, monkeypatch, case
):
    # AC3: a film's checkpoint goes only once its zh publish returned ok
    # (after the #187 .ja.srt rule); every other end keeps it for next Start.
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4"])
    if case == "publish_error":
        monkeypatch.setattr(
            SubsJob,
            "publish_zh",
            lambda self, cues: PublishResult("error", f"publish {self.zh_target.name}"),
        )
    inst, emit = r.inst, r.emit
    inst.start(emit)
    tr = r.translators[0]
    seen: list = []  # D-SR6: at discard time, a.mp4's settle + its .ja.srt
    real_discard = tr.discard_checkpoint

    def discard(key: str) -> None:
        seen.append((inst._settled.get("a.mp4"), _ja(r, "a.mp4").exists()))
        real_discard(key)

    monkeypatch.setattr(tr, "discard_checkpoint", discard)
    if case == "srt_exists":
        _put(_zh(r, "a.mp4"))  # #191: the owner's .srt appeared meanwhile
    if case in ("failed", "paused", "no_key"):
        tr.answer("a.mp4", ok=False, outcome=None if case == "failed" else case)
    else:
        tr.answer("a.mp4", kept_ja=1)
    inst.check(emit)
    ok = case == "published"
    assert tr.discarded == (["ck:a.mp4"] if ok else [])
    # D-SR6: after `_settle_completed` — settled completed, .ja.srt already gone
    assert seen == ([(("completed", ""), False)] if ok else [])
    assert _ja(r, "a.mp4").exists() is not ok
    done = _done(r.evs)
    assert len(done) == 1
    assert ("; 1 line kept in Japanese" in done[0][2]) is ok  # D-SR7


def test_stop_mid_film_then_start_resumes_only_the_open_lines(tmp_path, monkeypatch):
    # AC3/AC4 end to end: the REAL Translator (driven by test_subs_translate's
    # fake llm-workers) with the plugin's checkpoint dir under a tmp data dir.
    # Stop lands while batch 2 is in flight; the next Start asks only for
    # lines 41–90, and the checkpoint goes with the published zh.
    set_data_dir(tmp_path / "data")
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4"])
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

    monkeypatch.setattr(AV, "Translator", factory)
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
    r = _setup(tmp_path, monkeypatch, ja=["a.mp4"])
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

    monkeypatch.setattr(AV, "Translator", factory)
    inst, emit = r.inst, r.emit
    inst.start(emit)
    assert deferred.wait(5)
    st = inst.check(emit)  # deferred: in flight, unsettled, no `done`
    assert st.state == "running" and st.metrics["subs_translating"] == 1
    assert _step(st.metrics, "translate")["paused"] is True
    assert "a.mp4" not in inst._settled and not _done(r.evs)
    assert len(_keyed(r.evs, f"{IID}:llm-provider:grok-4.3 · api.x.ai")) == 1
    release.set()

    def finished() -> bool:
        inst.check(emit)
        return bool(_done(r.evs))

    assert _wait(finished)
    assert inst._settled["a.mp4"] == ("skipped", "translation_paused")
    assert len(_keyed(r.evs, f"{IID}:translation-paused")) == 1
    assert len(_keyed(r.evs, f"{IID}:llm-provider:grok-4.3 · api.x.ai")) == 1
    assert "Queue: 0/1 done, 0 failed, 1 skipped; 1 paused | " in _done(r.evs)[0][2]
    assert _ja(r, "a.mp4").exists() and not _zh(r, "a.mp4").exists()
