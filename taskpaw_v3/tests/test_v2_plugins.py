"""V2-parity plugins: folder / comfyui / custom_cmd (#20)."""

from __future__ import annotations

import sys

import pytest

from taskpaw_v3.monitors.plugins.comfyui import ComfyUIConfig, ComfyUIInstance, ComfyUIPlugin
from taskpaw_v3.monitors.plugins.custom_cmd import (
    CustomCmdConfig,
    CustomCmdInstance,
    CustomCmdPlugin,
)
from taskpaw_v3.monitors.plugins.folder import FolderConfig, FolderInstance, FolderPlugin
from taskpaw_v3.monitors.registry import default_registry


def _collector():
    events: list = []
    return events, (lambda *a, **k: events.append((a, k)))


# ── registry wiring ──────────────────────────────────────────────────────--
def test_v2_plugins_registered():
    reg = default_registry()
    assert {"folder", "comfyui", "custom_cmd"} <= set(reg.types())
    assert reg.get("custom_cmd").category == "both"
    assert reg.get("folder").category == "task"


# ── custom_cmd ───────────────────────────────────────────────────────────--
def test_custom_cmd_uses_no_shell_and_reports_exit():
    inst = CustomCmdInstance("c1", CustomCmdConfig(name="c", command=f"{sys.executable} -c \"import sys;sys.exit(0)\""))
    events, emit = _collector()
    st = inst.check(emit)
    assert st.state == "ok"
    assert events == []  # first ok, no transition


def test_custom_cmd_emits_alert_then_recovery():
    fail = f"{sys.executable} -c \"import sys;sys.exit(1)\""
    ok = f"{sys.executable} -c \"import sys;sys.exit(0)\""
    inst = CustomCmdInstance("c1", CustomCmdConfig(name="job", command=fail))
    events, emit = _collector()
    st = inst.check(emit)
    assert st.state == "error"
    assert events and events[0][0][0] == "alert"
    # now flip to ok
    inst.config = CustomCmdConfig(name="job", command=ok)
    events.clear()
    st = inst.check(emit)
    assert st.state == "ok"
    assert events and events[0][0][0] == "done"


def test_custom_cmd_empty_command_rejected():
    with pytest.raises(Exception):
        CustomCmdConfig(name="x", command="")


# ── folder ───────────────────────────────────────────────────────────────--
def test_folder_completes_when_stable(tmp_path):
    f = tmp_path / "video.mp4"
    f.write_bytes(b"abc")
    inst = FolderInstance("f1", FolderConfig(name="dl", path=str(tmp_path), stable_seconds=0))
    events, emit = _collector()
    # stable_seconds=0 → completes on the very next check after it's first seen
    inst.check(emit)        # first sight, records
    st = inst.check(emit)   # now stable
    assert any(a[0] == "done" for a, _ in events)
    assert st.state == "ok"


def test_folder_skips_zero_byte(tmp_path):
    (tmp_path / "empty.mp4").write_bytes(b"")
    inst = FolderInstance("f1", FolderConfig(name="dl", path=str(tmp_path), stable_seconds=0))
    events, emit = _collector()
    inst.check(emit)
    inst.check(emit)
    assert not events


def test_folder_extension_filter(tmp_path):
    (tmp_path / "keep.mp4").write_bytes(b"x")
    (tmp_path / "skip.txt").write_bytes(b"x")
    inst = FolderInstance("f1", FolderConfig(
        name="dl", path=str(tmp_path), extensions=["mp4"], stable_seconds=0))
    events, emit = _collector()
    inst.check(emit)
    inst.check(emit)
    done = [a for a, _ in events if a[0] == "done"]
    assert len(done) == 1 and "keep.mp4" in done[0][2]


def test_folder_resets_on_size_change(tmp_path):
    f = tmp_path / "video.mp4"
    f.write_bytes(b"abc")
    inst = FolderInstance("f1", FolderConfig(name="dl", path=str(tmp_path), stable_seconds=0))
    events, emit = _collector()
    inst.check(emit)            # first sight
    f.write_bytes(b"abcdef")    # grew → resets clock
    inst.check(emit)            # size changed, not done
    assert not [a for a, _ in events if a[0] == "done"]
    inst.check(emit)            # now stable
    assert [a for a, _ in events if a[0] == "done"]


def test_folder_baselines_existing_files_on_start(tmp_path):
    """Files already present at start() must NOT replay completions — only new
    arrivals fire (Codex #20 r4)."""
    (tmp_path / "old1.mp4").write_bytes(b"abc")
    (tmp_path / "old2.mp4").write_bytes(b"def")
    inst = FolderInstance("f1", FolderConfig(name="dl", path=str(tmp_path), stable_seconds=0))
    events, emit = _collector()
    inst.start(emit)            # baseline the two existing files
    inst.check(emit)
    inst.check(emit)
    assert not [a for a, _ in events if a[0] == "done"]  # no replay
    # a NEW file after start still completes
    (tmp_path / "new.mp4").write_bytes(b"xyz")
    inst.check(emit)
    inst.check(emit)
    done = [a for a, _ in events if a[0] == "done"]
    assert len(done) == 1 and "new.mp4" in done[0][2]


def test_folder_baselined_file_still_growing_completes(tmp_path):
    """A file present at start() but still being written must still fire when it
    finishes — baselining suppresses only files that never change (Codex #20 r5)."""
    f = tmp_path / "active.mp4"
    f.write_bytes(b"partial")
    inst = FolderInstance("f1", FolderConfig(name="dl", path=str(tmp_path), stable_seconds=0))
    events, emit = _collector()
    inst.start(emit)            # baselined as completed at current size
    f.write_bytes(b"partial+more")  # download continues → size changes
    inst.check(emit)            # size changed → reactivated, not done yet
    assert not [a for a, _ in events if a[0] == "done"]
    inst.check(emit)            # now stable → completes
    assert [a for a, _ in events if a[0] == "done"]


def test_folder_start_zero_byte_not_baselined(tmp_path):
    """A 0-byte placeholder present at start fires once it gets real content."""
    f = tmp_path / "dl.mp4"
    f.write_bytes(b"")
    inst = FolderInstance("f1", FolderConfig(name="dl", path=str(tmp_path), stable_seconds=0))
    events, emit = _collector()
    inst.start(emit)
    f.write_bytes(b"data")      # download fills in
    inst.check(emit)
    inst.check(emit)
    assert [a for a, _ in events if a[0] == "done"]


def test_folder_reused_filename_refires(tmp_path):
    """A completed file that's deleted then recreated with the same name must
    fire again, not be skipped by a stale completed record (Codex #20)."""
    f = tmp_path / "video.mp4"
    f.write_bytes(b"abc")
    inst = FolderInstance("f1", FolderConfig(name="dl", path=str(tmp_path), stable_seconds=0))
    events, emit = _collector()
    inst.check(emit)            # first sight
    inst.check(emit)            # complete
    assert len([a for a, _ in events if a[0] == "done"]) == 1
    f.unlink()
    inst.check(emit)            # gone → record purged
    f.write_bytes(b"xyz")       # same name, new download
    inst.check(emit)            # first sight again
    inst.check(emit)            # complete again
    assert len([a for a, _ in events if a[0] == "done"]) == 2


def test_folder_missing_dir_is_error():
    inst = FolderInstance("f1", FolderConfig(name="dl", path="/no/such/dir/xyz"))
    _, emit = _collector()
    assert inst.check(emit).state == "error"


# ── comfyui ──────────────────────────────────────────────────────────────--
def test_comfyui_done_after_idle_confirm(monkeypatch):
    import taskpaw_v3.monitors.plugins.comfyui as mod

    counts = iter([(1, 1), (0, 0), (0, 0)])  # busy → empty → empty
    monkeypatch.setattr(mod, "queue_counts", lambda *a, **k: next(counts))
    inst = ComfyUIInstance("q1", ComfyUIConfig(name="comfy", idle_confirm=2))
    events, emit = _collector()
    assert inst.check(emit).state == "running"   # busy
    inst.check(emit)                             # idle 1/2, no fire
    assert not events
    inst.check(emit)                             # idle 2/2, fire
    assert events and events[0][0][0] == "done"


def test_comfyui_no_false_done_when_never_busy(monkeypatch):
    import taskpaw_v3.monitors.plugins.comfyui as mod

    monkeypatch.setattr(mod, "queue_counts", lambda *a, **k: (0, 0))
    inst = ComfyUIInstance("q1", ComfyUIConfig(name="comfy", idle_confirm=1))
    events, emit = _collector()
    inst.check(emit)
    inst.check(emit)
    assert not events


def test_comfyui_unreachable_is_error(monkeypatch):
    import taskpaw_v3.monitors.plugins.comfyui as mod

    monkeypatch.setattr(mod, "queue_counts", lambda *a, **k: None)
    inst = ComfyUIInstance("q1", ComfyUIConfig(name="comfy"))
    _, emit = _collector()
    assert inst.check(emit).state == "error"


def test_comfyui_detects_stalled_queue(monkeypatch):
    """running==0 & pending>0 held for stall_confirm → error + one alert."""
    import taskpaw_v3.monitors.plugins.comfyui as mod

    monkeypatch.setattr(mod, "queue_counts", lambda *a, **k: (0, 2))
    inst = ComfyUIInstance("q1", ComfyUIConfig(name="comfy", stall_confirm=2))
    events, emit = _collector()
    assert inst.check(emit).state == "running"   # stall 1/2, still running
    assert not events
    st = inst.check(emit)                         # stall 2/2 → error + alert
    assert st.state == "error" and st.metrics["pending"] == 2
    alerts = [a for a, _ in events if a[0] == "alert"]
    assert len(alerts) == 1
    inst.check(emit)                             # still stalled, no duplicate alert
    assert len([a for a, _ in events if a[0] == "alert"]) == 1


def test_comfyui_stall_clears_then_completes(monkeypatch):
    import taskpaw_v3.monitors.plugins.comfyui as mod

    seq = iter([(0, 1), (1, 0), (0, 0)])  # stalled-ish → running → empty
    monkeypatch.setattr(mod, "queue_counts", lambda *a, **k: next(seq))
    inst = ComfyUIInstance("q1", ComfyUIConfig(name="comfy", idle_confirm=1, stall_confirm=2))
    events, emit = _collector()
    inst.check(emit)   # pending only, stall 1/2 (not yet error)
    inst.check(emit)   # running now → stall reset, busy
    inst.check(emit)   # empty → done
    assert any(a[0] == "done" for a, _ in events)
