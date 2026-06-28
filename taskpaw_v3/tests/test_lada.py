"""V3 `lada` plugin — port of V2 LadaWatcher (#59)."""

from __future__ import annotations

import sys

from taskpaw_v3.monitors.plugins.lada import (
    LadaConfig,
    LadaInstance,
    parse_progress_line,
)
from taskpaw_v3.monitors.registry import default_registry


def _events():
    evs: list[tuple] = []

    def emit(level, title, message, data=None, dedupe_key=None):
        evs.append((level, title, message))

    return evs, emit


def _cfg(**kw) -> LadaConfig:
    base = dict(name="lada")
    base.update(kw)
    return LadaConfig(**base)


# ── progress parsing (the 5 V2 regexes) ───────────────────────────────────
def test_parse_filename_then_progress():
    p = parse_progress_line("sample-video.mp4:", {})
    assert p == {"current_file": "sample-video.mp4"}
    line = ("正在处理视频： 27%|███ |已处理： 26:58 (84163帧) | "
            "剩余： 1:45:32 (230599帧) | 速度：36.4 帧/秒")
    p = parse_progress_line(line, p)
    assert p["current_file"] == "sample-video.mp4"
    assert p["percent"] == 27
    assert p["processed_frames"] == 84163
    assert p["eta"] == "1:45:32"
    assert p["remaining_frames"] == 230599
    assert p["fps"] == 36.4


def test_parse_new_file_resets_stale_progress():
    p = {"current_file": "a.mp4", "percent": 90, "fps": 10.0}
    assert parse_progress_line("b.mp4:", p) == {"current_file": "b.mp4"}


def test_parse_ignores_noise():
    assert parse_progress_line("", {"x": 1}) == {"x": 1}
    assert parse_progress_line("loading model...", {}) == {}   # no % → unchanged


# ── snapshot / queue / current-file ────────────────────────────────────────
def test_queue_counts_and_current_file(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir(); out.mkdir()
    for n in ["a.mp4", "b.mp4", "c.mp4"]:
        (inp / n).write_bytes(b"x")
    (out / "a_restored.mp4").write_bytes(b"x")   # 1 done (renamed output)
    inst = LadaInstance("lada", _cfg(lada_input_folder=str(inp), lada_output_folder=str(out)))
    _, emit = _events()
    inst.start(emit)   # passive (no cli_path) → snapshots inputs
    assert inst._queue_counts() == (1, 3)
    assert inst._detect_current_file() == "b.mp4"   # idx = output_count(1) → inputs[1]


def test_reconcile_drops_user_removed_pending(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir(); out.mkdir()
    for n in ["a.mp4", "b.mp4", "c.mp4"]:
        (inp / n).write_bytes(b"x")
    inst = LadaInstance("lada", _cfg(lada_input_folder=str(inp), lada_output_folder=str(out)))
    _, emit = _events()
    inst.start(emit)
    assert inst._inputs == ["a.mp4", "b.mp4", "c.mp4"]
    (inp / "c.mp4").unlink()                        # user removes a pending file
    inst._reconcile_snapshot()
    assert inst._inputs == ["a.mp4", "b.mp4"]       # total shrinks, not stuck at 3


# ── managed launch / errors / cleanup ──────────────────────────────────────
def test_managed_launch_builds_argv_no_shell(monkeypatch):
    captured: dict = {}

    class FakePopen:
        def __init__(self, cmd, creationflags=0, **kw):
            captured["cmd"] = cmd
            self.stdout = None

        def poll(self):
            return None

    monkeypatch.setattr("taskpaw_v3.monitors.plugins.lada.subprocess.Popen", FakePopen)
    inst = LadaInstance("lada", _cfg(
        lada_cli_path="/bin/lada-cli", lada_input_folder="/in",
        lada_output_folder="/out", lada_extra_args="--device cuda:1"))
    _, emit = _events()
    inst.start(emit)
    # list argv (shell=False) with input/output/extra args in order
    assert captured["cmd"] == ["/bin/lada-cli", "--input", "/in",
                               "--output", "/out", "--device", "cuda:1"]
    assert inst._launch_error is None


def test_cli_not_found_sets_error_and_does_not_raise(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError()

    monkeypatch.setattr("taskpaw_v3.monitors.plugins.lada.subprocess.Popen", boom)
    inst = LadaInstance("lada", _cfg(lada_cli_path="/nope/lada-cli",
                                     lada_input_folder="/in", lada_output_folder="/out"))
    evs, emit = _events()
    inst.start(emit)                                # must NOT raise
    assert inst._launch_error and "not found" in inst._launch_error
    assert evs and evs[0][0] == "alert"
    assert inst.check(emit).state == "error"


def test_cli_path_is_a_folder_gives_actionable_error(tmp_path):
    # The #1 real misconfig: lada_cli_path points at the install FOLDER, not the
    # exe → Popen would raise a cryptic "[WinError 5]". Catch it before launch with
    # a message that names the executable to use (#70).
    inst = LadaInstance("lada", _cfg(lada_cli_path=str(tmp_path),     # a directory
                                     lada_input_folder="/in", lada_output_folder="/out"))
    evs, emit = _events()
    inst.start(emit)                                # must NOT raise
    assert inst._launch_error and "is a folder" in inst._launch_error
    assert "lada-cli.exe" in inst._launch_error      # points at the executable
    assert evs and evs[0][0] == "alert"
    assert inst.check(emit).state == "error"


def test_managed_requires_input_output_folders():
    import pytest
    from taskpaw_v3.monitors.plugins.lada import LadaConfig
    # managed (cli path set) without folders → rejected with a clear message (#70)
    with pytest.raises(ValueError) as e:
        LadaConfig(name="l", lada_cli_path="/bin/lada-cli")
    assert "input AND output" in str(e.value)
    # passive (no cli path) needs neither
    LadaConfig(name="l")
    # managed WITH folders is fine
    LadaConfig(name="l", lada_cli_path="/bin/lada-cli",
               lada_input_folder="/in", lada_output_folder="/out")
    # …or with --input/--output passed through extra args (Codex #70)
    LadaConfig(name="l", lada_cli_path="/bin/lada-cli",
               lada_extra_args="--input /in --output /out")
    # …including the --input=… form
    LadaConfig(name="l", lada_cli_path="/bin/lada-cli",
               lada_extra_args="--input=/in --output=/out")
    # but LOOKALIKE flags (--input-size / --output-format) must NOT satisfy it —
    # exact-token match, not substring (Codex #70 r3)
    with pytest.raises(ValueError):
        LadaConfig(name="l", lada_cli_path="/bin/lada-cli",
                   lada_extra_args="--input-size 720 --output-format mp4")
    # …nor a BARE flag / empty value that supplies no actual path (Codex #70 r8)
    with pytest.raises(ValueError):
        LadaConfig(name="l", lada_cli_path="/bin/lada-cli",
                   lada_extra_args="--input --output")
    with pytest.raises(ValueError):
        LadaConfig(name="l", lada_cli_path="/bin/lada-cli", lada_input_folder="/in",
                   lada_extra_args="--output=")     # empty =value → output still missing


def test_process_name_matches_with_or_without_exe():
    from taskpaw_v3.monitors.plugins.lada import _proc_name_eq
    assert _proc_name_eq("lada-cli.exe", "lada-cli")     # actual .exe vs config without
    assert _proc_name_eq("lada-cli", "lada-cli.exe")     # and vice versa
    assert _proc_name_eq("LADA-CLI.EXE", "lada-cli")     # case-insensitive
    assert not _proc_name_eq("other", "lada-cli")


def test_passive_mode_does_not_launch(monkeypatch):
    called = {"popen": False}
    monkeypatch.setattr("taskpaw_v3.monitors.plugins.lada.subprocess.Popen",
                        lambda *a, **k: called.__setitem__("popen", True))
    inst = LadaInstance("lada", _cfg())             # no cli_path → passive
    _, emit = _events()
    inst.start(emit)
    assert called["popen"] is False


def test_passive_completion_emits_done(monkeypatch):
    seq = iter([True, True, False])
    monkeypatch.setattr("taskpaw_v3.monitors.plugins.lada.process_alive",
                        lambda name: next(seq))
    inst = LadaInstance("lada", _cfg())
    evs, emit = _events()
    inst.start(emit)
    inst.check(emit)   # running
    inst.check(emit)   # running
    inst.check(emit)   # exited → completion
    assert any(e[0] == "done" for e in evs)


def test_stop_terminates_managed_child():
    # Launch a real long-lived child and confirm stop() kills it (#40 no-orphan).
    inst = LadaInstance("lada", _cfg(
        lada_cli_path=sys.executable, lada_capture_progress=True,
        lada_input_folder="/in", lada_output_folder="/out",
        lada_extra_args='-c "import time; time.sleep(30)"'))
    _, emit = _events()
    inst.start(emit)
    assert inst._process is not None and inst._process.poll() is None
    inst.stop(timeout=3)
    assert inst._process.poll() is not None          # child terminated, no orphan


def test_idle_does_not_report_current_file(tmp_path, monkeypatch):
    # A passive monitor with queued files but no running process must NOT claim a
    # current_file (no phantom "idle: a.mp4"); queue facts still report (Codex #59).
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir(); out.mkdir()
    for n in ["a.mp4", "b.mp4"]:
        (inp / n).write_bytes(b"x")
    monkeypatch.setattr("taskpaw_v3.monitors.plugins.lada.process_alive", lambda name: False)
    inst = LadaInstance("lada", _cfg(lada_input_folder=str(inp), lada_output_folder=str(out)))
    _, emit = _events()
    inst.start(emit)
    st = inst.check(emit)                      # passive, not running → idle
    assert st.state == "idle"
    assert "current_file" not in st.metrics    # no phantom processing claim
    assert st.metrics["queue_total"] == 2      # queue facts still reported
    assert "idle" in st.detail and ".mp4" not in st.detail


def test_restart_resets_per_run_state():
    # A supervisor stop→start (or watchdog respawn) re-calls start() on the SAME
    # instance — it must reset so the new run isn't poisoned by old state (Codex).
    inst = LadaInstance("lada", _cfg())          # passive (no real process)
    _, emit = _events()
    inst.start(emit)
    inst._done_emitted = True
    inst._prev_running = True
    inst._stop.set()
    with inst._lock:
        inst._progress = {"percent": 99}
    inst.start(emit)                             # restart
    assert inst._done_emitted is False
    assert inst._prev_running is None
    assert inst._stop.is_set() is False
    assert inst._progress == {}


# ── registry ───────────────────────────────────────────────────────────────
def test_lada_registered():
    reg = default_registry()
    assert reg.has("lada")
    p = reg.get("lada")
    assert p.type_id == "lada" and p.system is False
