"""ComfyUI plugin error diagnostics (#60) — /history + log-tail, folded into
the stall/stuck alerts (V2 parity)."""

from __future__ import annotations

from taskpaw_v3.monitors.plugins import comfyui as cf
from taskpaw_v3.monitors.plugins.comfyui import (
    ComfyUIConfig,
    ComfyUIInstance,
    extract_history_error,
    tail_log_for_errors,
)


def _events():
    evs: list[tuple] = []

    def emit(level, title, message, data=None, dedupe_key=None):
        evs.append((level, title, message))

    return evs, emit


def _cfg(**kw) -> ComfyUIConfig:
    base = dict(name="comfy")
    base.update(kw)
    return ComfyUIConfig(**base)


# ── history error extraction (pure) ────────────────────────────────────────
def test_extract_history_error_from_execution_error():
    entry = {"status": {"completed": False, "status_str": "error", "messages": [
        ["execution_start", {}],
        ["execution_error", {"exception_message": "CUDA out of memory: tried to allocate 2GB"}],
    ]}}
    assert "CUDA out of memory" in extract_history_error(entry)


def test_extract_history_error_clean_entry_is_none():
    assert extract_history_error({"status": {"completed": True, "status_str": "success"}}) is None
    assert extract_history_error(None) is None
    assert extract_history_error({}) is None


def test_extract_history_error_caps_long_message():
    entry = {"status": {"completed": False, "status_str": "error", "messages": [
        ["execution_error", {"exception_message": "E" * 200}]]}}
    out = extract_history_error(entry)
    assert len(out) == 80 and out.endswith("...")


# ── log tail ────────────────────────────────────────────────────────────────
def test_tail_log_for_errors(tmp_path):
    log = tmp_path / "comfy.log"
    log.write_text("loading model\nstep 1/20\nRuntimeError: CUDA out of memory\n", encoding="utf-8")
    err, pos = tail_log_for_errors(str(log), 0)
    assert err and "CUDA out of memory" in err
    assert pos == log.stat().st_size
    # no new content → no re-alert, position unchanged
    assert tail_log_for_errors(str(log), pos) == (None, pos)
    # missing file → None
    assert tail_log_for_errors(str(tmp_path / "nope.log"), 0) == (None, 0)
    # empty path → no-op
    assert tail_log_for_errors("", 5) == (None, 5)


# ── diagnostics folded into alerts ──────────────────────────────────────────
def test_stall_alert_includes_diagnosed_error(monkeypatch):
    monkeypatch.setattr(cf, "queue_snapshot", lambda h, p, t: ([], 1))           # stalled shape
    monkeypatch.setattr(cf, "check_recent_history_errors", lambda h, p, t: "RuntimeError: boom")
    inst = ComfyUIInstance("comfy", _cfg(stall_confirm=1))
    evs, emit = _events()
    st = inst.check(emit)
    assert st.state == "error" and "RuntimeError: boom" in st.detail
    assert evs and evs[0][0] == "alert" and "boom" in evs[0][2]


def test_stuck_alert_diagnoses_running_prompt(monkeypatch):
    monkeypatch.setattr(cf, "queue_snapshot", lambda h, p, t: (["pid1"], 0))
    monkeypatch.setattr(cf, "check_history_error", lambda h, p, pid, t: f"err for {pid}")
    monkeypatch.setattr(cf, "check_recent_history_errors", lambda h, p, t: None)
    inst = ComfyUIInstance("comfy", _cfg(stuck_checks=1))
    evs, emit = _events()
    st = inst.check(emit)
    assert st.state == "error" and "err for pid1" in st.detail
    assert evs and evs[0][0] == "alert"


def test_stall_recovers_and_can_realert(monkeypatch):
    # stalled → recovered → stalled again must alert a SECOND time (per-episode
    # one-shot, no permanent dedupe).
    seq = iter([([], 1), ([], 0), ([], 1)])   # stalled, empty(recover), stalled
    monkeypatch.setattr(cf, "queue_snapshot", lambda h, p, t: next(seq))
    monkeypatch.setattr(cf, "check_recent_history_errors", lambda h, p, t: None)
    inst = ComfyUIInstance("comfy", _cfg(stall_confirm=1, idle_confirm=1))
    evs, emit = _events()
    inst.check(emit)   # stall #1 → alert
    inst.check(emit)   # empty → recover (clears _stalled)
    inst.check(emit)   # stall #2 → alert again
    assert sum(1 for e in evs if "stalled" in e[1]) == 2
