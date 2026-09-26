"""V3 `jasna` plugin — per-file Jasna queue, per-resolution unet-4x (#173).

`jasna.exe` and `ffprobe` are NEVER executed here: `subprocess.Popen` is replaced
by a scripted `_Launcher` and `probe_resolution` / `subprocess.run` are mocked.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from conftest import tasklog_rows as _tasklog

from taskpaw_v3.monitors.plugins import jasna as J
from taskpaw_v3.monitors.plugins.jasna import (
    JasnaConfig,
    JasnaInstance,
    JasnaPlugin,
    build_argv,
    engines_present,
    find_ffprobe,
    is_license_failure,
    large_detector_available,
    output_path_for,
    owned_flags_in,
    plan_queue,
    probe_resolution,
    staging_path_for,
    sweep_orphan_staging,
    tier_for,
)
from taskpaw_v3.monitors.registry import default_registry

_PROBE_NAME = "ffprobe.exe" if sys.platform == "win32" else "ffprobe"


# ── harness ───────────────────────────────────────────────────────────────
def _events():
    evs: list[tuple] = []

    def emit(level, title, message, data=None, dedupe_key=None):
        evs.append((level, title, message, dedupe_key))

    return evs, emit


def _cfg(**kw) -> JasnaConfig:
    base: dict = dict(name="jasna")
    base.update(kw)
    return JasnaConfig(**base)


def _managed(tmp_path: Path, **kw):
    """(cfg, input_folder, output_folder, jasna_home) for a managed instance."""
    inp, out, home = tmp_path / "in", tmp_path / "out", tmp_path / "jasna"
    for d in (inp, out, home / "tools"):
        d.mkdir(parents=True, exist_ok=True)
    exe = home / "jasna.exe"
    exe.write_bytes(b"MZ")
    (home / "tools" / _PROBE_NAME).write_bytes(b"x")
    base: dict = dict(
        name="JASNA",
        jasna_exe_path=str(exe),
        jasna_input_folder=str(inp),
        jasna_output_folder=str(out),
        jasna_gpu_monitor=False,
    )
    base.update(kw)
    return JasnaConfig(**base), inp, out, home


def _videos(folder: Path, *names: str) -> None:
    for n in names:
        (folder / n).write_bytes(b"video")


class _FakeStdout:
    def __init__(self, text: str) -> None:
        self._data = text.encode("utf-8")
        self._pos = 0

    def read(self, n: int) -> bytes:
        if self._pos >= len(self._data):
            return b""
        chunk = self._data[self._pos : self._pos + n]
        self._pos += n
        return chunk


class _FakePopen:
    """A Jasna stand-in: writes the staging file it was told to produce (so the
    rc-0 rename has something to move) and exposes a scripted return code.
    `rc=None` means "still running"."""

    def __init__(self, argv: list[str], rc, output: str, capture: bool) -> None:
        self.argv = list(argv)
        self._rc = rc
        self.pid = 4242
        self.terminated = False
        self.killed = False
        self.stdout = _FakeStdout(output) if capture else None
        staging = Path(argv[argv.index("--output") + 1])
        staging.write_bytes(b"partial")

    def poll(self):
        return self._rc

    def terminate(self) -> None:
        self.terminated = True
        if self._rc is None:
            self._rc = -15

    def kill(self) -> None:
        self.killed = True
        if self._rc is None:
            self._rc = -9

    def wait(self, timeout=None):
        if self._rc is None:
            raise subprocess.TimeoutExpired("jasna", timeout or 0)
        return self._rc


class _Launcher:
    """subprocess.Popen replacement with a per-launch return-code script."""

    def __init__(self, rcs, output: str = "", on_init=None) -> None:
        self._rcs = list(rcs)
        self._output = output
        self._on_init = on_init
        self.launches: list[list[str]] = []
        self.procs: list[_FakePopen] = []

    def __call__(self, argv, creationflags=0, **kw):
        self.launches.append(list(argv))
        rc = self._rcs.pop(0) if self._rcs else 0
        if self._on_init is not None:
            self._on_init(len(self.launches))
        proc = _FakePopen(argv, rc, self._output, kw.get("stdout") is not None)
        self.procs.append(proc)
        return proc

    @property
    def n(self) -> int:
        return len(self.launches)

    def arg(self, i: int, flag: str) -> str:
        argv = self.launches[i]
        return argv[argv.index(flag) + 1]

    def secondary(self, i: int) -> str:
        return self.arg(i, "--secondary-restoration")

    def inputs(self) -> list[str]:
        return [Path(self.arg(i, "--input")).name for i in range(self.n)]


def _probe(mapping=None, default=(1920, 1080)):
    calls: list[str] = []
    table = mapping or {}

    def probe(video, ffprobe):
        calls.append(Path(video).name)
        return table.get(Path(video).name, default)

    probe.calls = calls  # type: ignore[attr-defined]
    return probe


def _patch(monkeypatch, launcher, probe=None):
    monkeypatch.setattr(J.subprocess, "Popen", launcher)
    monkeypatch.setattr(J, "probe_resolution", probe or _probe())


# ── config ────────────────────────────────────────────────────────────────
def test_defaults_match_the_owner_rules():
    c = _cfg()
    assert c.unet4x_1080p is True and c.unet4x_4k is False
    assert c.clip_size_1080p == 90 and c.clip_size_4k == 60
    assert c.temporal_overlap == 8
    assert c.codec == "hevc" and c.cq == 24
    assert c.detection_model == "rfdetr-v6"
    assert c.jasna_capture_progress is False
    assert c.jasna_gpu_monitor is True
    assert c.process_name == "jasna"


def test_json_schema_exposes_the_tickbox_defaults():
    props = JasnaPlugin.json_schema()["properties"]
    assert props["unet4x_1080p"]["default"] is True
    assert props["unet4x_4k"]["default"] is False
    assert props["clip_size_1080p"]["default"] == 90
    assert props["clip_size_4k"]["default"] == 60
    assert props["cq"]["default"] == 24
    # every jasna-owned field carries a description (rjsf renders it as help text)
    own = set(JasnaConfig.model_fields) - set(
        JasnaConfig.__bases__[0].model_fields  # type: ignore[attr-defined]
    )
    assert len(own) == 19  # 15 restore fields + 4「AV 翻译」fields (#177)
    for name in own:
        assert props[name].get("description"), f"{name} has no description"


def test_passive_needs_no_folders():
    assert _cfg().jasna_exe_path == ""  # passive: valid with nothing else set


def test_managed_needs_both_folders(tmp_path):
    with pytest.raises(ValueError, match="jasna_input_folder"):
        _cfg(jasna_exe_path=str(tmp_path / "jasna.exe"))
    with pytest.raises(ValueError, match="jasna_output_folder"):
        _cfg(
            jasna_exe_path=str(tmp_path / "jasna.exe"),
            jasna_input_folder=str(tmp_path),
        )


def test_managed_rejects_identical_folders(tmp_path):
    same = tmp_path / "videos"
    same.mkdir()
    with pytest.raises(ValueError, match="different"):
        _cfg(
            jasna_exe_path=str(tmp_path / "jasna.exe"),
            jasna_input_folder=str(same),
            jasna_output_folder=str(same) + os.sep,  # resolves to the same folder
        )


def test_owned_flags_rejected_in_extra_args():
    for bad in (
        "--input C:/x",
        "--output=C:/y",
        "--max-clip-size 40",
        "--temporal-overlap=2",
        "--codec h264",
        "--cq 30",
        "--detection-model rfdetr-v6-large",
        "--output-pattern {original}",
        # argparse abbreviations select the same options (Codex 外门 C-5)
        "--inp other.mp4",
        "--outp=C:/y",
        "--max-clip 40",
        "--det rfdetr-v6-large",
    ):
        with pytest.raises(ValueError, match="TaskPaw owns"):
            _cfg(jasna_extra_args=bad)
    assert owned_flags_in("--inp x") == ["--input"]
    assert owned_flags_in("--outp x") == ["--output", "--output-pattern"]


def test_secondary_override_detection_covers_abbreviations():
    assert J.secondary_overridden("--sec none")
    assert J.secondary_overridden("--secondary=tvai")
    assert not J.secondary_overridden("--secondary-restoration-x 1")
    assert not J.secondary_overridden("--device cuda:1")


def test_lookalike_flags_and_secondary_restoration_are_accepted():
    # a substring test would wrongly reject these
    assert owned_flags_in("--input-size 512 --output-format mkv") == []
    c = _cfg(jasna_extra_args="--secondary-restoration tvai --device cuda:1")
    assert "tvai" in c.jasna_extra_args


def test_clip_overlap_cross_validation():
    with pytest.raises(ValueError, match="temporal_overlap"):
        _cfg(temporal_overlap=30)  # 60 >= min(90, 60)
    with pytest.raises(ValueError, match="temporal_overlap"):
        _cfg(clip_size_4k=16, temporal_overlap=8)  # 16 >= 16
    assert _cfg(clip_size_4k=17, temporal_overlap=8).clip_size_4k == 17


def test_numeric_bounds():
    with pytest.raises(ValueError):
        _cfg(cq=64)
    with pytest.raises(ValueError):
        _cfg(cq=-1)
    with pytest.raises(ValueError):
        _cfg(clip_size_1080p=7)
    with pytest.raises(ValueError):
        _cfg(temporal_overlap=-1)
    with pytest.raises(ValueError):
        _cfg(codec="vp9")  # not in the Literal
    with pytest.raises(ValueError):
        _cfg(unknown_field=1)  # extra="forbid"


def test_extra_args_description_documents_the_override():
    desc = JasnaConfig.model_fields["jasna_extra_args"].description or ""
    assert "--secondary-restoration" in desc
    assert "overrides the tickboxes" in desc
    assert "disables the automatic unet-4x degrade" in desc
    cap = JasnaConfig.model_fields["jasna_capture_progress"].description or ""
    assert "OWN console window" in cap
    assert "model_weights/*.engine" in cap


# ── pure helpers ──────────────────────────────────────────────────────────
def test_tier_for_uses_the_pixel_count_rule():
    assert tier_for(1920, 1080) == "1080p"
    assert tier_for(1920, 1200) == "1080p"  # cinematic 1080p stays 1080p
    assert tier_for(2560, 1080) == "1080p"
    assert tier_for(2560, 1440) == "4k"
    assert tier_for(3840, 2160) == "4k"


def test_output_and_staging_paths(tmp_path):
    v = tmp_path / "Clip 01.mkv"
    assert output_path_for(str(tmp_path), v).name == "Clip 01-破解.mp4"
    assert staging_path_for(str(tmp_path), v).name == "Clip 01-破解.tmp.mp4"


def test_build_argv_1080p_with_unet(tmp_path):
    cfg = _cfg()
    argv = build_argv(
        cfg, "C:/J/jasna.exe", Path("a.mp4"), Path("out/a.tmp.mp4"), "1080p", True, True
    )
    assert argv == [
        "C:/J/jasna.exe",
        "--input",
        "a.mp4",
        "--output",
        str(Path("out/a.tmp.mp4")),
        "--max-clip-size",
        "90",
        "--temporal-overlap",
        "8",
        "--secondary-restoration",
        "unet-4x",
        "--codec",
        "hevc",
        "--cq",
        "24",
        "--detection-model",
        "rfdetr-v6",  # large detector is 4K-only
    ]


def test_build_argv_4k_upgrades_the_detector_only_when_available():
    cfg = _cfg()
    with_large = build_argv(cfg, "j", Path("a.mp4"), Path("s.mp4"), "4k", False, True)
    assert with_large[with_large.index("--max-clip-size") + 1] == "60"
    assert with_large[with_large.index("--secondary-restoration") + 1] == "none"
    assert with_large[with_large.index("--detection-model") + 1] == "rfdetr-v6-large"
    without = build_argv(cfg, "j", Path("a.mp4"), Path("s.mp4"), "4k", True, False)
    assert without[without.index("--detection-model") + 1] == "rfdetr-v6"
    assert without[without.index("--secondary-restoration") + 1] == "unet-4x"


def test_build_argv_respects_an_explicit_detection_model_and_appends_extra_args():
    cfg = _cfg(detection_model="rtdetr", jasna_extra_args="--device cuda:1")
    argv = build_argv(cfg, "j", Path("a.mp4"), Path("s.mp4"), "4k", False, True)
    assert argv[argv.index("--detection-model") + 1] == "rtdetr"
    assert argv[-2:] == ["--device", "cuda:1"]  # extra args come LAST (last-wins)


def test_find_ffprobe_lookup_order(tmp_path, monkeypatch):
    home = tmp_path / "jasna"
    (home / "tools").mkdir(parents=True)
    monkeypatch.setattr(J.shutil, "which", lambda _n: None)
    assert find_ffprobe(str(home)) is None
    beside = home / _PROBE_NAME
    beside.write_bytes(b"x")
    assert find_ffprobe(str(home)) == str(beside)  # 3rd choice
    monkeypatch.setattr(J.shutil, "which", lambda _n: "/usr/bin/ffprobe")
    assert find_ffprobe(str(home)) == "/usr/bin/ffprobe"  # 2nd choice wins
    tools = home / "tools" / _PROBE_NAME
    tools.write_bytes(b"x")
    assert find_ffprobe(str(home)) == str(tools)  # 1st choice wins
    assert find_ffprobe(None) == "/usr/bin/ffprobe"


class _Run:
    def __init__(self, stdout="", returncode=0):
        self.stdout = stdout
        self.returncode = returncode


def test_probe_resolution_parses_the_csv(monkeypatch):
    seen: dict = {}

    def run(argv, **kw):
        seen["argv"] = argv
        seen["kw"] = kw
        return _Run("1920,1080\n")

    monkeypatch.setattr(J.subprocess, "run", run)
    assert probe_resolution(Path("a.mp4"), "ffprobe") == (1920, 1080)
    assert seen["argv"][:8] == [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
    ]
    assert seen["kw"]["timeout"] == 5.0


def test_probe_resolution_failures_return_none(monkeypatch):
    assert probe_resolution(Path("a.mp4"), None) is None
    monkeypatch.setattr(J.subprocess, "run", lambda *a, **k: _Run("", 1))
    assert probe_resolution(Path("a.mp4"), "ffprobe") is None
    monkeypatch.setattr(J.subprocess, "run", lambda *a, **k: _Run("garbage\n"))
    assert probe_resolution(Path("a.mp4"), "ffprobe") is None
    monkeypatch.setattr(J.subprocess, "run", lambda *a, **k: _Run("0,0\n"))
    assert probe_resolution(Path("a.mp4"), "ffprobe") is None
    monkeypatch.setattr(J.subprocess, "run", lambda *a, **k: _Run("N/A,1080\n"))
    assert probe_resolution(Path("a.mp4"), "ffprobe") is None

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("ffprobe", 5)

    monkeypatch.setattr(J.subprocess, "run", boom)
    assert probe_resolution(Path("a.mp4"), "ffprobe") is None


def test_plan_queue_skips_done_sorts_and_ignores_staging(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    _videos(inp, "c.mp4", "a.mp4", "b.mp4", "notes.txt", "d_restored.mp4")
    (out / "a_restored.mp4").write_bytes(b"done")
    (out / "b_restored.tmp.mp4").write_bytes(b"partial")  # staging is NOT done
    pending, done, collisions = plan_queue(str(inp), str(out))
    assert done == 1 and collisions == []
    # sorted, a skipped as done, a source literally named *_restored.mp4 is queued
    assert [p.name for p in pending] == ["b.mp4", "c.mp4", "d_restored.mp4"]


def test_plan_queue_reports_output_collisions(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    _videos(inp, "a.mp4", "a.mkv")
    pending, done, collisions = plan_queue(str(inp), str(out))
    assert [p.name for p in pending] == ["a.mkv"]  # sorted: .mkv first
    assert done == 0
    assert [(a.name, b.name) for a, b in collisions] == [("a.mp4", "a.mkv")]


def test_plan_queue_collision_is_casefolded(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    _videos(inp, "A.mp4")
    pending, _done, collisions = plan_queue(str(inp), str(out))
    # On a case-insensitive FS "a.MP4" IS "A.mp4"; emulate the pair explicitly.
    key = str(output_path_for(str(out), Path("a.MP4"))).casefold()
    assert key == str(output_path_for(str(out), Path("A.mp4"))).casefold()
    assert len(pending) == 1 and collisions == []


def test_plan_queue_on_a_missing_folder_raises(tmp_path):
    # Codex 外门 C-2: an unreadable input folder is an error, not an empty queue.
    with pytest.raises(OSError):
        plan_queue(str(tmp_path / "nope"), str(tmp_path))


def test_unreadable_input_folder_is_an_error_not_idle(tmp_path, monkeypatch):
    cfg, inp, _out, _home = _managed(tmp_path)
    launcher = _Launcher([0])
    _patch(monkeypatch, launcher)
    inp.rmdir()  # the folder vanished between saving the config and Start
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()
    inst.start(emit)  # never raises
    st = inst.check(emit)
    assert st.state == "error"
    assert "cannot scan jasna_input_folder" in st.detail
    assert [e for e in evs if e[0] == "alert" and e[3] == "j1:launch"]
    assert launcher.n == 0


def test_sweep_orphan_staging(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir()
    out.mkdir()
    _videos(inp, "keep.mp4")
    orphan = out / "gone_restored.tmp.mp4"
    kept = out / "keep_restored.tmp.mp4"
    fresh = out / "other_restored.tmp.mp4"
    final = out / "gone_restored.mp4"
    for p in (orphan, kept, fresh, final):
        p.write_bytes(b"x")
    old = time.time() - 600
    for p in (orphan, kept, final):
        os.utime(p, (old, old))
    removed = sweep_orphan_staging(str(inp), str(out))
    assert [p.name for p in removed] == ["gone_restored.tmp.mp4"]
    assert not orphan.exists()
    assert kept.exists()  # its source is still in the queue
    assert fresh.exists()  # too recent — something may be writing it
    assert final.exists()  # a published output is never swept


def test_license_and_weights_helpers(tmp_path):
    assert is_license_failure("RuntimeError: unet-4x is a Supporter Feature. …")
    assert not is_license_failure("CUDA out of memory")
    assert not is_license_failure("")
    assert large_detector_available(str(tmp_path)) is False
    assert engines_present(str(tmp_path)) is False
    weights = tmp_path / "model_weights"
    weights.mkdir()
    (weights / "rfdetr-v6-large.onnx").write_bytes(b"x")
    (weights / "restore.engine").write_bytes(b"x")
    assert large_detector_available(str(tmp_path)) is True
    assert engines_present(str(tmp_path)) is True
    assert large_detector_available(None) is False
    assert engines_present(None) is False


# ── lifecycle ─────────────────────────────────────────────────────────────
def test_sequential_batch_renames_on_success_and_emits_one_done(tmp_path, monkeypatch):
    cfg, inp, out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4", "b.mp4")
    launcher = _Launcher([0, 0])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()

    inst.start(emit)
    assert launcher.n == 1 and launcher.inputs() == ["a.mp4"]
    assert launcher.arg(0, "--output").endswith("a-破解.tmp.mp4")

    st = inst.check(emit)  # a exits 0 → renamed, b launched
    assert st.state == "running"
    assert (out / "a-破解.mp4").exists()
    assert not (out / "a-破解.tmp.mp4").exists()
    assert launcher.inputs() == ["a.mp4", "b.mp4"]

    st = inst.check(emit)  # b exits 0 → batch complete
    assert st.state == "idle"
    assert (out / "b-破解.mp4").exists()
    done = [e for e in evs if e[0] == "done"]
    assert len(done) == 1
    assert "Jasna processing complete | Queue: 2/2 done, 0 failed" in done[0][2]

    # post-batch stability: nothing relaunches and the counters hold
    before = launcher.n
    for _ in range(3):
        st = inst.check(emit)
        assert st.state == "idle"
        assert st.metrics["queue_completed"] == 2
        assert st.metrics["queue_remaining"] == 0
    assert launcher.n == before
    assert len([e for e in evs if e[0] == "done"]) == 1


def test_unet_failure_degrades_only_that_tier(tmp_path, monkeypatch):
    cfg, inp, _out, _home = _managed(tmp_path, unet4x_4k=True)
    _videos(inp, "a.mp4", "b.mp4", "c.mp4")
    probe = _probe({"c.mp4": (3840, 2160)})
    launcher = _Launcher([1, 0, 0, 0])
    _patch(monkeypatch, launcher, probe)
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()

    inst.start(emit)  # launch 1: a, 1080p, unet-4x → fails
    for _ in range(4):
        inst.check(emit)

    assert launcher.inputs() == ["a.mp4", "a.mp4", "b.mp4", "c.mp4"]
    assert launcher.secondary(0) == "unet-4x"
    assert launcher.secondary(1) == "none"  # the plain relaunch
    assert launcher.secondary(2) == "none"  # 1080p stays degraded
    assert launcher.secondary(3) == "unet-4x"  # the 4K tier is untouched
    degrade = [e for e in evs if "unet-4x disabled" in e[1]]
    assert len(degrade) == 1
    assert degrade[0][3] == "j1:unet:1080p"
    assert "not enough VRAM" in degrade[0][2]
    assert inst._done == 3 and inst._failed == 0


def test_secondary_override_in_extra_args_disables_the_degrade(tmp_path, monkeypatch):
    # Codex 外门 C-1: with `--secondary-restoration` in the extra args the launch
    # is not a unet-4x launch (argparse last-wins), so a failure takes the plain
    # retry path and never produces a false license/VRAM alert.
    cfg, inp, _out, _home = _managed(
        tmp_path, jasna_extra_args="--secondary-restoration none"
    )
    _videos(inp, "a.mp4")
    launcher = _Launcher([1, 0])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()
    inst.start(emit)
    inst.check(emit)
    inst.check(emit)
    assert launcher.inputs() == ["a.mp4", "a.mp4"]  # plain retry, then done
    assert not [e for e in evs if "unet-4x disabled" in e[1]]
    assert inst._done == 1 and inst._failed == 0
    assert J.secondary_overridden("--secondary-restoration=tvai")
    assert not J.secondary_overridden("--secondary-restoration-x 1 --device cuda:1")


def test_always_failing_file_costs_exactly_three_launches(tmp_path, monkeypatch):
    cfg, inp, out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    launcher = _Launcher([1, 1, 1])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()

    inst.start(emit)
    for _ in range(4):
        inst.check(emit)

    assert launcher.n == 3  # unet → plain → plain; no unbounded relaunch loop
    assert launcher.inputs() == ["a.mp4"] * 3
    assert launcher.secondary(0) == "unet-4x"
    assert launcher.secondary(1) == "none"  # the retry argv
    assert launcher.secondary(2) == "none"
    fails = [e for e in evs if e[1].endswith("a.mp4 failed")]
    assert len(fails) == 1 and "exit code 1" in fails[0][2]
    assert inst._failed == 1 and inst._done == 0
    assert not (out / "a-破解.tmp.mp4").exists()  # staging cleaned up
    assert not (out / "a-破解.mp4").exists()  # never published


def test_next_file_gets_a_fresh_retry_budget(tmp_path, monkeypatch):
    cfg, inp, _out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4", "b.mp4")
    launcher = _Launcher([1, 1, 1, 0])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)
    for _ in range(5):
        inst.check(emit)
    assert launcher.inputs() == ["a.mp4", "a.mp4", "a.mp4", "b.mp4"]
    assert launcher.secondary(3) == "unet-4x"  # b starts with unet again
    assert inst._failed == 1 and inst._done == 1


def test_three_consecutive_failed_files_abort_the_batch(tmp_path, monkeypatch):
    cfg, inp, _out, _home = _managed(tmp_path, unet4x_1080p=False)
    _videos(inp, "a.mp4", "b.mp4", "c.mp4", "d.mp4")
    launcher = _Launcher([1] * 8)
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()

    inst.start(emit)
    for _ in range(8):
        st = inst.check(emit)

    assert launcher.n == 6  # 2 launches per file, d never launched
    assert "d.mp4" not in launcher.inputs()
    aborts = [e for e in evs if "aborted" in e[1]]
    assert len(aborts) == 1
    assert st.state == "degraded"
    assert "3 consecutive failures" in st.detail
    assert not [e for e in evs if e[0] == "done"]
    # degraded is terminal for this run: still renders metrics, launches nothing
    st2 = inst.check(emit)
    assert st2.state == "degraded" and launcher.n == 6
    assert st2.metrics["queue_failed"] == 3


def test_rename_failure_counts_as_a_file_failure(tmp_path, monkeypatch):
    cfg, inp, _out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    launcher = _Launcher([0])
    _patch(monkeypatch, launcher)

    def boom(_src, _dst):
        raise OSError("output is read-only")

    monkeypatch.setattr(J.os, "replace", boom)
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()
    inst.start(emit)
    inst.check(emit)
    assert inst._done == 0 and inst._failed == 1
    assert any("could not publish" in e[2] for e in evs)


def test_relaunch_of_the_same_file_does_not_probe_again(tmp_path, monkeypatch):
    cfg, inp, _out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    probe = _probe()
    launcher = _Launcher([1, 0])
    _patch(monkeypatch, launcher, probe)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)
    inst.check(emit)
    inst.check(emit)
    assert launcher.n == 2
    assert probe.calls == ["a.mp4"]  # one probe per distinct file


def test_next_launch_after_an_exit_probes_outside_the_launch_lock(
    tmp_path, monkeypatch
):
    # Internal review I-1: the exit branch must release _launch_lock before
    # _launch_next probes the next file, or a concurrent stop() waits behind a
    # 5 s ffprobe. Another thread must be able to take the lock DURING the probe.
    cfg, inp, _out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4", "b.mp4")
    launcher = _Launcher([0, None])
    lock_free: list[bool] = []
    holder: dict = {}

    def probe(video, ffprobe):
        got: list[bool] = []

        def grab():
            ok = holder["inst"]._launch_lock.acquire(timeout=0.5)
            got.append(ok)
            if ok:
                holder["inst"]._launch_lock.release()

        t = threading.Thread(target=grab, daemon=True)
        t.start()
        t.join(timeout=5)
        lock_free.append(bool(got and got[0]))
        return (1920, 1080)

    _patch(monkeypatch, launcher, probe)
    inst = JasnaInstance("j1", cfg)
    holder["inst"] = inst
    _evs, emit = _events()
    inst.start(emit)  # probe #1 from start()
    inst.check(emit)  # a.mp4 exits 0 → b.mp4 probed on the exit path (probe #2)
    assert launcher.inputs() == ["a.mp4", "b.mp4"]
    assert lock_free == [True, True]


def test_empty_folder_is_idle_with_a_detail_and_no_event(tmp_path, monkeypatch):
    cfg, _inp, _out, _home = _managed(tmp_path)
    launcher = _Launcher([])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()
    inst.start(emit)
    st = inst.check(emit)
    assert st.state == "idle"
    assert st.detail == "nothing to process (0 already restored)"
    assert evs == [] and launcher.n == 0


def test_drained_folder_is_idle_with_the_done_count(tmp_path, monkeypatch):
    cfg, inp, out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    (out / "a_restored.mp4").write_bytes(b"done")
    launcher = _Launcher([])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()
    inst.start(emit)
    st = inst.check(emit)
    assert st.state == "idle"
    assert st.detail == "nothing to process (1 already restored)"
    assert evs == [] and launcher.n == 0


def test_a_killed_runs_staging_file_is_not_counted_as_done(tmp_path, monkeypatch):
    cfg, inp, out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    (out / "a_restored.tmp.mp4").write_bytes(b"partial")
    launcher = _Launcher([0])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)
    assert launcher.n == 1  # re-queued, not skipped
    assert inst._done == 0 and inst._total == 1


def test_start_sweeps_only_orphaned_staging_files(tmp_path, monkeypatch):
    cfg, inp, out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    orphan = out / "gone-破解.tmp.mp4"
    orphan.write_bytes(b"x")
    old = time.time() - 600
    os.utime(orphan, (old, old))
    mine = out / "a-破解.tmp.mp4"
    mine.write_bytes(b"x")
    os.utime(mine, (old, old))
    launcher = _Launcher([None])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)
    assert not orphan.exists()
    assert mine.exists()  # recreated by the launch for the pending source
    inst.stop(timeout=0.5)


def test_collisions_are_alerted_counted_and_skipped(tmp_path, monkeypatch):
    cfg, inp, _out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4", "a.mkv", "b.mp4")
    launcher = _Launcher([None])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()
    inst.start(emit)
    alerts = [e for e in evs if "collision" in e[1]]
    assert len(alerts) == 1 and alerts[0][3] == "j1:collisions"
    st = inst.check(emit)
    assert st.metrics["queue_total"] == 3
    assert st.metrics["queue_failed"] == 1
    assert st.metrics["queue_remaining"] == 2
    inst.stop(timeout=0.5)


# ── stop / races ──────────────────────────────────────────────────────────
def test_stop_terminates_a_live_child_and_removes_its_staging(tmp_path, monkeypatch):
    cfg, inp, out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    launcher = _Launcher([None])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)
    assert (out / "a-破解.tmp.mp4").exists()
    inst.stop(timeout=0.5)
    assert launcher.procs[0].terminated is True
    assert not (out / "a-破解.tmp.mp4").exists()


def test_stop_after_the_child_exited_leaves_the_staging_file(tmp_path, monkeypatch):
    cfg, inp, out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    launcher = _Launcher([0])  # already exited 0, not yet handled
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)
    inst.stop(timeout=0.5)
    assert launcher.procs[0].terminated is False
    # Codex 外门 C-4: the finished video is PUBLISHED by stop() itself, because
    # the worker may never run check() again — a Stop between files must not
    # throw away completed work.
    assert (out / "a-破解.mp4").exists()
    assert not (out / "a-破解.tmp.mp4").exists()
    # A later check() must not double-handle it, launch anything or emit done.
    st = inst.check(emit)
    assert st.state == "idle"
    assert launcher.n == 1
    assert not [e for e in _evs if e[0] == "done"]


def test_exit_branch_publishes_a_clean_exit_while_stopping(tmp_path, monkeypatch):
    # The other half of C-4: _stopping is already set when check() sees rc 0
    # (stop() set the flag but has not taken the lock yet) → publish, no
    # counters, no relaunch, no done event.
    cfg, inp, out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4", "b.mp4")
    launcher = _Launcher([0, 0])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()
    inst.start(emit)
    inst._stopping.set()
    st = inst.check(emit)
    assert (out / "a-破解.mp4").exists()
    assert launcher.n == 1  # b.mp4 not launched
    assert inst._done == 0  # run is ending; the next start() rescans
    assert st.state == "idle"
    assert not [e for e in evs if e[0] == "done"]


def test_stopping_set_inside_popen_is_caught_by_the_post_launch_recheck(
    tmp_path, monkeypatch
):
    cfg, inp, _out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    holder: dict = {}

    def on_init(_n):
        holder["inst"]._stopping.set()

    launcher = _Launcher([None], on_init=on_init)
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    holder["inst"] = inst
    _evs, emit = _events()
    inst.start(emit)
    assert launcher.procs[0].terminated is True
    assert inst._process is None


def test_stop_from_another_thread_does_not_deadlock(tmp_path, monkeypatch):
    cfg, inp, _out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    launched = threading.Event()
    holder: dict = {}

    def on_init(_n):
        launched.set()
        time.sleep(0.05)  # give the stopper thread time to set _stopping

    launcher = _Launcher([None], on_init=on_init)
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    holder["inst"] = inst
    _evs, emit = _events()

    def stopper():
        launched.wait(timeout=5)
        inst.stop(timeout=1)

    t = threading.Thread(target=stopper, daemon=True)
    t.start()
    inst.start(emit)
    t.join(timeout=5)
    assert not t.is_alive()  # no deadlock
    assert launcher.procs[0].poll() is not None  # no child left running
    assert inst._process is None or inst._process.poll() is not None


def test_stop_returns_within_budget_when_the_launch_lock_is_held(tmp_path, monkeypatch):
    cfg, inp, _out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    launcher = _Launcher([None])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)

    held = threading.Event()
    release = threading.Event()

    def hold():
        with inst._launch_lock:
            held.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert held.wait(timeout=5)
    started = time.monotonic()
    inst.stop(timeout=0.2)
    elapsed = time.monotonic() - started
    release.set()
    t.join(timeout=5)
    assert elapsed < 3.0  # did not wait on the lock forever
    assert launcher.procs[0].terminated is True  # child killed anyway (#40)


def test_terminate_child_tolerates_a_reaped_or_missing_child():
    J._terminate_child(None)

    class _Reaped:
        def poll(self):
            return 0

        def terminate(self):  # pragma: no cover - must not be reached
            raise AssertionError("must not terminate a reaped child")

    J._terminate_child(_Reaped())  # no raise

    class _Vanishing:
        pid = 1

        def poll(self):
            return None

        def terminate(self):
            raise ProcessLookupError()

    J._terminate_child(_Vanishing())  # OSError guard


def test_restart_resets_every_per_run_field(tmp_path, monkeypatch):
    cfg, inp, _out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    launcher = _Launcher([1, 1, 1, 0])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)
    for _ in range(4):
        inst.check(emit)
    assert inst._failed == 1

    (inp / "a_restored.mp4").unlink(missing_ok=True)
    inst.start(emit)  # second run
    assert inst._failed == 0 and inst._done == 0
    assert inst._batch_done_emitted is False and inst._batch_aborted is False
    assert inst._run_unet_disabled == {} and inst._last_failure_tail == ""
    assert inst._launch_error is None
    inst.stop(timeout=0.5)


# ── launch errors ─────────────────────────────────────────────────────────
def test_exe_path_is_a_folder_gives_an_actionable_error(tmp_path):
    cfg, _inp, _out, home = _managed(tmp_path)
    cfg = cfg.model_copy(update={"jasna_exe_path": str(home)})
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()
    inst.start(emit)  # must NOT raise
    assert inst._launch_error and "is a folder" in inst._launch_error
    assert "jasna.exe" in inst._launch_error
    assert evs and evs[0][0] == "alert" and evs[0][3] == "j1:launch"
    assert inst.check(emit).state == "error"


def test_missing_exe_sets_an_error_and_does_not_raise(tmp_path):
    cfg, _inp, _out, home = _managed(tmp_path)
    cfg = cfg.model_copy(update={"jasna_exe_path": str(home / "nope.exe")})
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()
    inst.start(emit)
    assert inst._launch_error and "not found" in inst._launch_error
    assert evs and evs[0][0] == "alert"
    assert inst.check(emit).state == "error"


def test_popen_failure_is_recorded_not_raised(tmp_path, monkeypatch):
    cfg, inp, _out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")

    def boom(*a, **k):
        raise PermissionError("WinError 5")

    _patch(monkeypatch, boom)
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()
    inst.start(emit)  # must NOT raise
    assert inst._launch_error and "access denied" in inst._launch_error
    assert evs and evs[0][0] == "alert"
    assert inst.check(emit).state == "error"


def test_missing_ffprobe_alerts_once_and_falls_back_to_the_1080p_tier(
    tmp_path, monkeypatch
):
    cfg, inp, _out, home = _managed(tmp_path)
    (home / "tools" / _PROBE_NAME).unlink()
    _videos(inp, "a.mp4")
    monkeypatch.setattr(J.shutil, "which", lambda _n: None)
    launcher = _Launcher([None])
    monkeypatch.setattr(J.subprocess, "Popen", launcher)  # real probe_resolution
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()
    inst.start(emit)
    probe_alerts = [e for e in evs if "ffprobe" in e[1]]
    assert len(probe_alerts) == 1 and probe_alerts[0][3] == "j1:ffprobe"
    assert inst._ffprobe is None
    assert launcher.arg(0, "--max-clip-size") == "90"  # 1080p tier
    inst.stop(timeout=0.5)


# ── capture mode / metrics ────────────────────────────────────────────────
_PROGRESS = (
    b"Processing video:  47%|@@@@      |Processed: 06:09 (36703f) | "
    b"Remaining: 30:47 (207454f) | Speed: 112.3fps"
)


def test_running_metrics_expose_all_per_task_fields_to_hub(tmp_path):
    # #161: the FULL per-task set must ride `metrics` (→ /status → hub.db →
    # openclaw). Lock the exact names/types so a rename can't silently drop one.
    cfg, _inp, _out, _home = _managed(tmp_path, jasna_capture_progress=True)
    inst = JasnaInstance("j1", cfg)
    inst._started = True
    inst._current = Path("X.mp4")
    inst._current_tier = "4k"
    inst._current_dims = (3840, 2160)
    inst._current_unet = True
    inst._total, inst._done, inst._failed = 5, 2, 0
    inst._consume_output(_PROGRESS)

    st = inst._build_status("running")
    m = st.metrics
    assert m["current_file"] == "X.mp4"
    assert isinstance(m["percent"], int) and m["percent"] == 47
    assert isinstance(m["processed_frames"], int) and m["processed_frames"] == 36703
    assert isinstance(m["remaining_frames"], int) and m["remaining_frames"] == 207454
    assert isinstance(m["fps"], float) and m["fps"] == 112.3
    assert isinstance(m["elapsed"], str) and m["elapsed"] == "06:09"
    assert isinstance(m["eta"], str) and m["eta"] == "30:47"
    assert m["queue_completed"] == 2 and m["queue_total"] == 5
    assert m["queue_failed"] == 0 and m["queue_remaining"] == 3
    assert set(inst._progress) == {
        "percent",
        "elapsed",
        "processed_frames",
        "eta",
        "remaining_frames",
        "fps",
    }
    assert st.detail == (
        "running: X.mp4 [4K 3840x2160, unet-4x] · 47% · ETA 30:47 · "
        "112.3 fps · 2/5 done"
    )


def test_idle_snapshot_omits_per_task_progress(tmp_path):
    cfg, _inp, _out, _home = _managed(tmp_path, jasna_capture_progress=True)
    inst = JasnaInstance("j1", cfg)
    inst._current = Path("X.mp4")
    inst._consume_output(_PROGRESS)
    m = inst._build_status("idle").metrics
    for k in ("current_file", "percent", "fps", "eta", "processed_frames"):
        assert k not in m


def test_detail_shows_the_tier_and_unet_state(tmp_path):
    cfg, _inp, _out, _home = _managed(tmp_path)
    inst = JasnaInstance("j1", cfg)
    inst._current = Path("clip.mkv")
    inst._current_tier = "1080p"
    inst._current_dims = (1920, 1080)
    inst._current_unet = False
    assert inst._build_status("running").detail == (
        "running: clip.mkv [1080p 1920x1080, unet-4x off]"
    )


def test_compiling_hint_shows_until_engines_exist(tmp_path, monkeypatch):
    cfg, inp, _out, home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    launcher = _Launcher([None])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)
    st = inst.check(emit)
    assert st.state == "running"
    assert st.detail.startswith("compiling TensorRT engines (first run, 15-60 min): ")
    weights = home / "model_weights"
    weights.mkdir()
    (weights / "restore.engine").write_bytes(b"x")
    st = inst.check(emit)
    assert not st.detail.startswith("compiling")
    inst.stop(timeout=0.5)


def test_failure_tail_is_captured_cleared_per_launch_and_sharpens_the_degrade(
    tmp_path, monkeypatch
):
    cfg, inp, _out, _home = _managed(tmp_path, jasna_capture_progress=True)
    _videos(inp, "a.mp4")
    launcher = _Launcher([1, 0])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()
    inst.start(emit)
    inst._consume_output(
        b"RuntimeError: unet-4x is a supporter feature. Enter your license."
    )
    inst.check(emit)  # the unet launch fails → tail snapshotted, relaunch cleared it
    assert "supporter feature" in inst._last_failure_tail
    with inst._lock:
        assert list(inst._recent_output) == []  # cleared per launch
    inst.check(emit)  # the plain relaunch succeeds → degrade alert
    degrade = [e for e in evs if "unet-4x disabled" in e[1]]
    assert len(degrade) == 1
    assert "not activated in Jasna's GUI" in degrade[0][2]


def test_capture_failure_alert_carries_the_bounded_tail_never_the_argv(
    tmp_path, monkeypatch
):
    cfg, inp, _out, _home = _managed(
        tmp_path, jasna_capture_progress=True, unet4x_1080p=False
    )
    _videos(inp, "a.mp4")
    launcher = _Launcher([1, 1])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()
    inst.start(emit)
    inst.check(emit)  # plain fail → retry
    inst._consume_output(b"RuntimeError: CUDA out of memory")
    inst.check(emit)  # retry fails → alert
    fail = next(e for e in evs if e[1].endswith("a.mp4 failed"))
    assert "exit code 1" in fail[2]
    assert "RuntimeError: CUDA out of memory" in fail[2]
    assert "--secondary-restoration" not in fail[2]
    assert len(fail[2]) < 1200


def test_non_capture_failure_alert_has_no_tail(tmp_path, monkeypatch):
    cfg, inp, _out, _home = _managed(tmp_path, unet4x_1080p=False)
    _videos(inp, "a.mp4")
    launcher = _Launcher([1, 1])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()
    inst.start(emit)
    inst._consume_output(b"RuntimeError: CUDA out of memory")
    inst.check(emit)
    inst.check(emit)
    fail = next(e for e in evs if e[1].endswith("a.mp4 failed"))
    assert fail[2] == "exit code 1"


# ── passive mode ──────────────────────────────────────────────────────────
def test_passive_running_then_gone_emits_done(monkeypatch):
    alive = {"v": True}
    monkeypatch.setattr(J, "process_alive", lambda _n: alive["v"])
    inst = JasnaInstance("j1", _cfg(jasna_gpu_monitor=False))
    evs, emit = _events()
    assert inst.check(emit).state == "running"
    assert evs == []
    alive["v"] = False
    st = inst.check(emit)
    assert st.state == "idle"
    assert [e[0] for e in evs] == ["done"]
    assert "Jasna processing complete" in evs[0][2]
    assert "queue_total" not in st.metrics


# ── plugin / registry ─────────────────────────────────────────────────────
# ── HEVC hev1 → hvc1 retag, so macOS can preview the output ───────────────
def _box(btype: bytes, payload: bytes) -> bytes:
    return (8 + len(payload)).to_bytes(4, "big") + btype + payload


def _sample_entry(name: bytes, *, hvcc: bool = True) -> bytes:
    """A VisualSampleEntry: 78 fixed bytes, then its child boxes."""
    children = _box(b"hvcC", b"\x01" + bytes(20)) if hvcc else b""
    return _box(name, bytes(78) + children)


def _mp4(entry: bytes, *, with_moov: bool = True, moov_last: bool = False) -> bytes:
    stsd = _box(b"stsd", bytes(4) + (1).to_bytes(4, "big") + entry)
    moov = _box(
        b"moov", _box(b"trak", _box(b"mdia", _box(b"minf", _box(b"stbl", stsd))))
    )
    ftyp = _box(b"ftyp", b"isom" + bytes(8))
    mdat = _box(b"mdat", b"\xde\xad\xbe\xef" * 8)
    if not with_moov:
        return ftyp + mdat
    return ftyp + mdat + moov if moov_last else ftyp + moov + mdat


def _write(tmp_path: Path, data: bytes, name: str = "v.mp4") -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_retag_rewrites_hev1_to_hvc1_in_place(tmp_path):
    data = _mp4(_sample_entry(b"hev1"))
    p = _write(tmp_path, data)
    assert J.retag_hevc_hvc1(p) == "patched"
    after = p.read_bytes()
    # Metadata only: same length, and nothing outside the 4-byte type field of
    # the sample entry changed (hev1 and hvc1 share their first and last byte,
    # so only two bytes actually move).
    assert len(after) == len(data)
    at = data.index(b"hev1")
    assert after[at : at + 4] == b"hvc1"
    assert after[:at] == data[:at] and after[at + 4 :] == data[at + 4 :]
    assert b"hev1" not in after and after.count(b"hvc1") == 1
    assert J.retag_hevc_hvc1(p) == "already-hvc1"  # idempotent


def test_retag_finds_moov_after_the_media_data(tmp_path):
    p = _write(tmp_path, _mp4(_sample_entry(b"hev1"), moov_last=True))
    assert J.retag_hevc_hvc1(p) == "patched"
    assert b"hvc1" in p.read_bytes()


def test_retag_handles_a_64_bit_largesize_moov(tmp_path):
    inner = _mp4(_sample_entry(b"hev1"))
    start = inner.index(b"moov") - 4
    moov = inner[start:]
    big = (
        (1).to_bytes(4, "big") + b"moov" + (len(moov) + 8).to_bytes(8, "big") + moov[8:]
    )
    p = _write(tmp_path, inner[:start] + big)
    assert J.retag_hevc_hvc1(p) == "patched"


@pytest.mark.parametrize(
    "name,data,expected",
    [
        ("avc1.mp4", _mp4(_sample_entry(b"avc1")), "no-hevc-entry"),
        ("hvc1.mp4", _mp4(_sample_entry(b"hvc1")), "already-hvc1"),
        ("nohvcc.mp4", _mp4(_sample_entry(b"hev1", hvcc=False)), "unsupported:no-hvcC"),
        (
            "nomoov.mp4",
            _mp4(_sample_entry(b"hev1"), with_moov=False),
            "unsupported:no-moov",
        ),
        ("empty.mp4", b"", "unsupported:no-moov"),
        ("junk.mp4", b"not an mp4 at all, really", "unsupported:no-moov"),
        # A box declaring size 4 advances the cursor by nothing: the walk must
        # end rather than spin inside check().
        (
            "spin.mp4",
            (4).to_bytes(4, "big") + b"moov" + bytes(32),
            "unsupported:no-moov",
        ),
        # A size that runs past the end of the file is never trusted.
        (
            "toolong.mp4",
            (1 << 30).to_bytes(4, "big") + b"moov" + bytes(8),
            "unsupported:no-moov",
        ),
    ],
)
def test_retag_leaves_other_codecs_and_broken_layouts_alone(
    tmp_path, name, data, expected
):
    p = _write(tmp_path, data, name)
    assert J.retag_hevc_hvc1(p) == expected
    assert p.read_bytes() == data  # untouched


def test_retag_on_a_missing_file_is_reported_not_raised(tmp_path):
    assert J.retag_hevc_hvc1(tmp_path / "nope.mp4").startswith("unsupported:")


def test_publish_retags_before_renaming(tmp_path, monkeypatch):
    # The published file must never be the unpreviewable hev1 variant, not even
    # briefly: the retag happens on the staging name, before the rename.
    cfg, inp, out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    launcher = _Launcher([0])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)
    # Stand in for Jasna's real output: HEVC tagged hev1, as ffmpeg writes it.
    (out / "a-破解.tmp.mp4").write_bytes(_mp4(_sample_entry(b"hev1")))
    inst.check(emit)
    published = (out / "a-破解.mp4").read_bytes()
    assert b"hvc1" in published and b"hev1" not in published
    assert inst._done == 1


def test_publish_still_happens_when_the_file_cannot_be_retagged(tmp_path, monkeypatch):
    # An output we don't understand is published as-is — a retag failure must
    # never cost the operator a finished video.
    cfg, inp, out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    launcher = _Launcher([0])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)
    (out / "a-破解.tmp.mp4").write_bytes(b"not an mp4")
    inst.check(emit)
    assert (out / "a-破解.mp4").read_bytes() == b"not an mp4"
    assert inst._done == 1


def _trak(*, stsd_entry: bytes = b"", extra: bytes = b"") -> bytes:
    """A trak: `tkhd` plus, unless `stsd_entry` is empty, the mdia→stsd chain."""
    body = _box(b"tkhd", bytes(84)) + extra
    if stsd_entry:
        stsd = _box(b"stsd", bytes(4) + (1).to_bytes(4, "big") + stsd_entry)
        body += _box(b"mdia", _box(b"minf", _box(b"stbl", stsd)))
    return _box(b"trak", body)


def _mp4_traks(*traks: bytes) -> bytes:
    return _box(b"ftyp", b"isom" + bytes(8)) + _box(b"moov", b"".join(traks))


def test_retag_scans_every_track_not_just_the_first(tmp_path):
    # E-1: a file whose audio track comes first is legal and common (ffmpeg
    # writes one on `-map 0:a -map 0:v`). Stopping at track 0 would leave the
    # video entry untagged and the fix would silently do nothing.
    data = _mp4_traks(
        _trak(stsd_entry=_sample_entry(b"mp4a", hvcc=False)),
        _trak(stsd_entry=_sample_entry(b"hev1")),
    )
    p = _write(tmp_path, data, "audio_first.mp4")
    assert J.retag_hevc_hvc1(p) == "patched"
    after = p.read_bytes()
    assert b"hev1" not in after and after.count(b"hvc1") == 1


def test_retag_skips_a_track_without_a_sample_table(tmp_path):
    # A tkhd-only timecode/chapter track must not abort the walk.
    data = _mp4_traks(_trak(), _trak(stsd_entry=_sample_entry(b"hev1")))
    p = _write(tmp_path, data, "tkhd_only_first.mp4")
    assert J.retag_hevc_hvc1(p) == "patched"


def test_retag_tries_the_next_entry_when_one_has_no_hvcc(tmp_path):
    # E-2: a bare hev1 entry must not veto a well-formed one after it.
    entries = _sample_entry(b"hev1", hvcc=False) + _sample_entry(b"hev1")
    stsd = _box(b"stsd", bytes(4) + (2).to_bytes(4, "big") + entries)
    data = _mp4_traks(_box(b"trak", _box(b"mdia", _box(b"minf", _box(b"stbl", stsd)))))
    p = _write(tmp_path, data, "two_entries.mp4")
    assert J.retag_hevc_hvc1(p) == "patched"
    assert p.read_bytes().count(b"hvc1") == 1


def test_retag_reports_no_trak_and_a_size_zero_moov(tmp_path):
    assert J.retag_hevc_hvc1(_write(tmp_path, _mp4_traks(), "notrak.mp4")) == (
        "unsupported:no-trak"
    )
    # A final box declaring size 0 runs to the end of the file (ISO 14496-12).
    inner = _mp4(_sample_entry(b"hev1"), moov_last=True)
    at = inner.index(b"moov") - 4
    zero = inner[:at] + (0).to_bytes(4, "big") + inner[at + 4 :]
    assert J.retag_hevc_hvc1(_write(tmp_path, zero, "size0.mp4")) == "patched"


def test_publish_retags_the_staging_file_before_the_rename(tmp_path, monkeypatch):
    # E-4: assert the ORDER, not just the outcome — retagging after the rename
    # would publish the unpreviewable variant first and still end up correct.
    cfg, inp, out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    launcher = _Launcher([0])
    _patch(monkeypatch, launcher)
    seen: list[tuple[str, bool]] = []
    real = J.retag_hevc_hvc1

    def spy(path):
        seen.append((path.name, (out / "a-破解.mp4").exists()))
        return real(path)

    monkeypatch.setattr(J, "retag_hevc_hvc1", spy)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)
    (out / "a-破解.tmp.mp4").write_bytes(_mp4(_sample_entry(b"hev1")))
    inst.check(emit)
    assert seen == [("a-破解.tmp.mp4", False)]
    assert b"hvc1" in (out / "a-破解.mp4").read_bytes()


def test_publish_warns_when_the_tag_could_not_be_fixed(tmp_path, monkeypatch, caplog):
    cfg, inp, out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    launcher = _Launcher([0])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)
    (out / "a-破解.tmp.mp4").write_bytes(b"not an mp4")
    with caplog.at_level("WARNING", logger="taskpaw.monitors.jasna"):
        inst.check(emit)
    assert any("kept its ffmpeg codec tag" in r.message for r in caplog.records)


def test_publish_does_not_warn_about_a_non_hevc_output(tmp_path, monkeypatch, caplog):
    # Codex 外门: with codec=h264 there is no HEVC entry to retag, so the
    # "macOS will not preview it" warning would be false on every finished job.
    cfg, inp, out, _home = _managed(tmp_path, codec="h264")
    _videos(inp, "a.mp4")
    launcher = _Launcher([0])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)
    (out / "a-破解.tmp.mp4").write_bytes(_mp4(_sample_entry(b"avc1")))
    with caplog.at_level("WARNING", logger="taskpaw.monitors.jasna"):
        inst.check(emit)
    assert not any("kept its ffmpeg codec tag" in r.message for r in caplog.records)
    assert inst._done == 1


def test_publish_warns_when_an_hevc_job_produced_no_hevc_entry(
    tmp_path, monkeypatch, caplog
):
    # The same status IS an anomaly when we asked Jasna for HEVC.
    cfg, inp, out, _home = _managed(tmp_path)  # codec defaults to hevc
    _videos(inp, "a.mp4")
    launcher = _Launcher([0])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    _evs, emit = _events()
    inst.start(emit)
    (out / "a-破解.tmp.mp4").write_bytes(_mp4(_sample_entry(b"avc1")))
    with caplog.at_level("WARNING", logger="taskpaw.monitors.jasna"):
        inst.check(emit)
    assert any("no-hevc-entry" in r.message for r in caplog.records)


def test_publish_does_not_retag_a_missing_staging_file(tmp_path, monkeypatch, caplog):
    # The os.replace failure is the one honest report; a retag warning on top of
    # it would just be noise.
    cfg, inp, out, _home = _managed(tmp_path)
    _videos(inp, "a.mp4")
    launcher = _Launcher([0])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j1", cfg)
    evs, emit = _events()
    inst.start(emit)
    (out / "a-破解.tmp.mp4").unlink()
    with caplog.at_level("WARNING", logger="taskpaw.monitors.jasna"):
        inst.check(emit)
    # (match the log TEXT, not the word "retag": pytest's tmp_path is named
    # after this test function, so it appears inside every logged path.)
    assert not any(
        "could not retag" in r.message or "kept its ffmpeg codec tag" in r.message
        for r in caplog.records
    )
    assert [e for e in evs if "failed" in e[1]]


def test_plugin_is_registered_and_self_describing():
    reg = default_registry()
    assert reg.has("jasna")
    plugin = reg.get("jasna")
    assert plugin.type_id == "jasna"
    assert plugin.display_name == "Jasna (video restore)"
    assert plugin.category == "task"
    assert plugin.config_version == 1
    assert plugin.system is False
    order = plugin.ui_schema()["ui:order"]
    assert order[:6] == [
        "name",
        "jasna_exe_path",
        "jasna_input_folder",
        "jasna_output_folder",
        "unet4x_1080p",
        "unet4x_4k",
    ]
    assert order[-1] == "*"
    assert reg.has("lada")  # jasna does not replace lada in the registry


def test_manual_start_only_for_managed(tmp_path):
    plugin = JasnaPlugin()
    cfg, _inp, _out, _home = _managed(tmp_path)
    assert plugin.manual_start(cfg) is True
    assert plugin.manual_start(_cfg()) is False


def test_create_builds_a_jasna_instance(tmp_path):
    cfg, _inp, _out, _home = _managed(tmp_path)
    inst = JasnaPlugin().create("j1", cfg)
    assert isinstance(inst, JasnaInstance)


@pytest.mark.parametrize("path", ["normal", "stopping", "stop"])
def test_tasklog_restore_publish_paths(tmp_path, monkeypatch, path):
    cfg, inp, out, _ = _managed(tmp_path)
    _videos(inp, "a.mp4")
    launcher = _Launcher([0])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j", cfg)
    _, emit = _events()
    inst.start(emit)
    if path == "stop":
        inst.stop()
    else:
        if path == "stopping":
            inst._stopping.set()
        inst.check(emit)
    assert len(_tasklog("restore.started")) == 1
    assert len(_tasklog("restore.finished")) == 1
    row = _tasklog("restore.finished")[0]
    assert row["film"] == "a.mp4"
    assert row["data"]["output"] == "a-\u7834\u89e3.mp4"
    assert row["data"]["duration"] >= 0
    assert not _tasklog("task.interrupted")
    if path == "normal":
        inst.check(emit)
        assert len(_tasklog("task.done")) == 1
    inst.stop()


@pytest.mark.parametrize("rc", [None, 7])
def test_tasklog_restore_stop_live_or_failed(tmp_path, monkeypatch, rc):
    cfg, inp, _, _ = _managed(tmp_path)
    _videos(inp, "a.mp4")
    launcher = _Launcher([rc])
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j", cfg)
    _, emit = _events()
    inst.start(emit)
    inst.stop()
    inst.stop()
    if rc is None:
        rows = _tasklog("task.interrupted")
        assert len(rows) == 1 and rows[0]["data"]["step"] == "restore"
    else:
        assert not _tasklog("task.interrupted")
        assert _tasklog("restore.failed")[0]["data"]["at_stop"] is True
        assert len(_tasklog("restore.failed")) == 1


@pytest.mark.parametrize("exit_first", [True, False])
@pytest.mark.parametrize("rc", [7, None])
def test_tasklog_restore_stop_exit_race_records_once(
    tmp_path, monkeypatch, exit_first, rc
):
    from test_jasna_subs import _setup

    h = _setup(tmp_path, monkeypatch, pending=("a.mp4",), rcs=[rc])
    h.inst.start(h.emit)
    child = h.inst._process
    cancel = h.translators[0].cancel

    def check_during_cancel():
        h.inst.check(h.emit)
        cancel()

    if exit_first:
        monkeypatch.setattr(h.translators[0], "cancel", check_during_cancel)
    h.inst.stop()
    h.inst.check(h.emit)
    h.inst.stop()
    assert h.inst._process is None
    if rc is None:
        assert child.terminated
        assert not _tasklog("restore.failed")
        rows = _tasklog("task.interrupted")
        assert len(rows) == 1 and rows[0]["data"]["step"] == "restore"
    else:
        assert not child.terminated
        assert not _tasklog("task.interrupted")
        rows = _tasklog("restore.failed")
        assert len(rows) == 1
        assert rows[0]["film"] == "a.mp4"
        assert rows[0]["data"] == {"exit_code": 7, "at_stop": True}


def test_tasklog_restore_retry_abort_and_errors(tmp_path, monkeypatch):
    cfg, inp, _, _ = _managed(tmp_path, unet4x_1080p=False)
    _videos(inp, "a.mp4", "b.mp4", "c.mp4")
    launcher = _Launcher([1] * 6)
    _patch(monkeypatch, launcher)
    inst = JasnaInstance("j", cfg)
    _, emit = _events()
    inst.start(emit)
    for _ in range(8):
        inst.check(emit)
    assert len(_tasklog("restore.failed")) == 6
    assert len(_tasklog("restore.retry")) == 3
    assert len(_tasklog("task.aborted")) == 1
    assert not _tasklog("task.done")
    inst.stop()


def test_tasklog_gpu_wait_acquire_only_at_spawn(tmp_path, monkeypatch):
    from taskpaw_v3.core import gpu_lease

    cfg, inp, _, _ = _managed(tmp_path)
    _videos(inp, "a.mp4")
    _patch(monkeypatch, _Launcher([None]))
    other = ("other", 1)
    gpu_lease.try_acquire(other, 5, label="other task")
    inst = JasnaInstance("j", cfg)
    _, emit = _events()
    inst.start(emit)
    inst.check(emit)
    assert len(_tasklog("task.gpu_wait")) == 1
    assert _tasklog("task.gpu_wait")[0]["data"]["holder"] == "other task"
    gpu_lease.release(other)
    inst.check(emit)
    assert len(_tasklog("task.gpu_acquired")) == 1
    inst.stop()
    assert len(_tasklog("task.gpu_acquired")) == 1


def test_tasklog_restore_skips_and_setup_errors(tmp_path, monkeypatch):
    cfg, inp, out, _ = _managed(tmp_path)
    _videos(inp, "a.mp4", "b.mp4", "b.mkv")
    output_path_for(str(out), inp / "a.mp4").write_bytes(b"done")
    _patch(monkeypatch, _Launcher([None]))
    monkeypatch.setattr(J, "find_ffprobe", lambda _: None)
    inst = JasnaInstance("j", cfg)
    _, emit = _events()
    inst.start(emit)
    assert len(_tasklog("restore.skipped")) == 2
    assert {r["data"]["reason"] for r in _tasklog("task.error")} == {
        "ffprobe_missing",
        "name_collision",
    }
    assert _tasklog("task.started")[0]["data"] == {"queued": 1, "done": 1, "skipped": 1}
    # Only the log counters are under test; finish the fake restore queue.
    inst._current = None
    inst._process = None
    inst._pending.clear()
    inst._subs_skipped = 3
    inst._maybe_done(emit)
    assert _tasklog("task.done")[0]["data"]["skipped"] == 1
    assert _tasklog("task.done")[0]["data"]["subs_skipped"] == 3
    inst.stop()


def test_tasklog_restore_and_translation_stop_snapshot(tmp_path, monkeypatch):
    from test_jasna_subs import _setup

    h = _setup(tmp_path, monkeypatch, pending=("a.mp4",), rcs=[None])
    h.inst.start(h.emit)
    h.translators[0].live = {"job_id": "previous.mp4", "elapsed_s": 8}
    h.inst.stop()
    assert {r["data"]["step"] for r in _tasklog("task.interrupted")} == {
        "restore",
        "translate",
    }


def test_tasklog_restore_finishes_during_cancel_not_interrupted(tmp_path, monkeypatch):
    from test_jasna_subs import _setup

    h = _setup(tmp_path, monkeypatch, pending=("LMNO-123.mp4",), rcs=[None])
    h.inst.start(h.emit)
    child = h.inst._process
    cancel = h.translators[0].cancel

    def finish_during_cancel():
        child._rc = 0
        cancel()

    monkeypatch.setattr(h.translators[0], "cancel", finish_during_cancel)
    h.inst.stop()
    assert len(_tasklog("restore.finished")) == 1
    assert not _tasklog("task.interrupted")


def test_tasklog_jasna_launch_error_no_secret(tmp_path, monkeypatch):
    cfg, inp, _, _ = _managed(tmp_path, jasna_extra_args="--device PLANTED_SECRET")
    _videos(inp, "a.mp4")

    def fail(*args, **kwargs):
        raise OSError("PLANTED_SECRET https://user:pass@host:4321 --private-argv")

    _patch(monkeypatch, fail)
    inst = JasnaInstance("j", cfg)
    _, emit = _events()
    inst.start(emit)
    assert _tasklog("task.error")
    assert "PLANTED_SECRET" not in str(_tasklog())
    assert "user:pass" not in str(_tasklog())
    assert not _tasklog("restore.started")
    inst.stop()
