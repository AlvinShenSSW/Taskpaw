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
    entry = {
        "status": {
            "completed": False,
            "status_str": "error",
            "messages": [
                ["execution_start", {}],
                [
                    "execution_error",
                    {"exception_message": "CUDA out of memory: tried to allocate 2GB"},
                ],
            ],
        }
    }
    assert "CUDA out of memory" in extract_history_error(entry)


def test_extract_history_error_clean_entry_is_none():
    assert (
        extract_history_error({"status": {"completed": True, "status_str": "success"}})
        is None
    )
    assert extract_history_error(None) is None
    assert extract_history_error({}) is None


def test_extract_history_error_caps_long_message():
    entry = {
        "status": {
            "completed": False,
            "status_str": "error",
            "messages": [["execution_error", {"exception_message": "E" * 200}]],
        }
    }
    out = extract_history_error(entry)
    assert len(out) == 80 and out.endswith("...")


# ── log tail ────────────────────────────────────────────────────────────────
def test_tail_log_for_errors(tmp_path):
    log = tmp_path / "comfy.log"
    log.write_text(
        "loading model\nstep 1/20\nRuntimeError: CUDA out of memory\n", encoding="utf-8"
    )
    err, pos = tail_log_for_errors(str(log), 0)
    assert err and "CUDA out of memory" in err
    assert pos == log.stat().st_size
    # no new content → no re-alert, position unchanged
    assert tail_log_for_errors(str(log), pos) == (None, pos)
    # missing file → None
    assert tail_log_for_errors(str(tmp_path / "nope.log"), 0) == (None, 0)
    # empty path → no-op
    assert tail_log_for_errors("", 5) == (None, 5)


def test_tail_log_does_not_rereport_consumed_error(tmp_path):
    # An already-consumed error must not be re-reported when the file later grows
    # with unrelated lines (only NEW bytes after the offset are scanned); a NEW
    # error after the offset still IS reported (Codex #60).
    log = tmp_path / "comfy.log"
    log.write_text("RuntimeError: boom\n", encoding="utf-8")
    err, pos = tail_log_for_errors(str(log), 0)
    assert err and "boom" in err
    with open(log, "a", encoding="utf-8") as f:
        f.write("step 2\nstep 3\n")  # unrelated growth
    err2, pos2 = tail_log_for_errors(str(log), pos)
    assert err2 is None  # stale error not re-reported
    with open(log, "a", encoding="utf-8") as f:
        f.write("CUDA out of memory\n")  # a genuinely new error
    err3, _ = tail_log_for_errors(str(log), pos2)
    assert err3 and "CUDA out of memory" in err3


def test_tail_log_handles_rotation(tmp_path):
    # After the log is rotated/truncated to a SMALLER file, a new error must still
    # be detected — the saved offset can't stay stuck high (Codex #60).
    log = tmp_path / "comfy.log"
    log.write_text("x" * 5000 + "\n", encoding="utf-8")  # big, no error
    _, pos = tail_log_for_errors(str(log), 0)
    assert pos == log.stat().st_size and pos > 1000
    log.write_text("CUDA out of memory\n", encoding="utf-8")  # rotated: smaller, error
    err, pos2 = tail_log_for_errors(str(log), pos)
    assert err and "CUDA out of memory" in err
    assert pos2 == log.stat().st_size


# ── diagnostics folded into alerts ──────────────────────────────────────────
def test_stall_alert_includes_diagnosed_error(monkeypatch):
    monkeypatch.setattr(cf, "queue_snapshot", lambda h, p, t: ([], 1))  # stalled shape
    monkeypatch.setattr(
        cf, "check_recent_history_errors", lambda h, p, t: "RuntimeError: boom"
    )
    inst = ComfyUIInstance("comfy", _cfg(stall_confirm=1))
    evs, emit = _events()
    st = inst.check(emit)
    assert st.state == "error" and "RuntimeError: boom" in st.detail
    assert evs and evs[0][0] == "alert" and "boom" in evs[0][2]


def test_bad_host_is_unreachable_not_crash():
    # A host with whitespace → http.client.InvalidURL (HTTPException, not OSError);
    # must report a clean unreachable, not raise out of check() (Codex #60).
    inst = ComfyUIInstance("comfy", _cfg(host="bad host"))
    _, emit = _events()
    st = inst.check(emit)
    assert st.state == "error" and "unreachable" in st.detail


def test_diagnose_only_session_log_errors(tmp_path, monkeypatch):
    # An error already in the log BEFORE start() must NOT be blamed for the first
    # stall; an error written DURING the session is the real cause (Codex #60).
    log = tmp_path / "comfy.log"
    log.write_text("CUDA out of memory\n", encoding="utf-8")  # pre-existing
    monkeypatch.setattr(cf, "queue_snapshot", lambda h, p, t: ([], 1))
    monkeypatch.setattr(cf, "check_recent_history_errors", lambda h, p, t: None)
    inst = ComfyUIInstance("comfy", _cfg(stall_confirm=1, comfyui_log_path=str(log)))
    _, emit = _events()
    inst.start(emit)  # prime past old error
    with open(log, "a", encoding="utf-8") as f:
        f.write("RuntimeError: session boom\n")  # error during session
    st = inst.check(emit)  # stall → diagnose
    assert "session boom" in st.detail
    assert "CUDA out of memory" not in st.detail


def test_log_error_cleared_at_idle_not_blamed_on_later_stall(tmp_path, monkeypatch):
    # An error during a COMPLETED prompt is consumed each poll and cleared when the
    # queue goes idle, so a later unrelated stall isn't blamed on it (Codex #60).
    log = tmp_path / "comfy.log"
    log.write_text("", encoding="utf-8")
    seq = iter([(["p1"], 0), ([], 0), ([], 1)])  # running → idle → stalled
    monkeypatch.setattr(cf, "queue_snapshot", lambda h, p, t: next(seq))
    monkeypatch.setattr(cf, "check_recent_history_errors", lambda h, p, t: None)
    inst = ComfyUIInstance(
        "comfy", _cfg(stall_confirm=1, idle_confirm=1, comfyui_log_path=str(log))
    )
    _, emit = _events()
    inst.start(emit)
    with open(log, "a", encoding="utf-8") as f:
        f.write("RuntimeError: from p1\n")
    inst.check(emit)  # running p1 → consumes the error
    inst.check(emit)  # idle → clears it
    st = inst.check(emit)  # unrelated stall
    assert "from p1" not in (st.detail or "")


def test_stuck_alert_diagnoses_running_prompt(monkeypatch):
    monkeypatch.setattr(cf, "queue_snapshot", lambda h, p, t: (["pid1"], 0))
    monkeypatch.setattr(
        cf, "check_history_error", lambda h, p, pid, t: f"err for {pid}"
    )
    monkeypatch.setattr(cf, "check_recent_history_errors", lambda h, p, t: None)
    inst = ComfyUIInstance("comfy", _cfg(stuck_checks=1))
    evs, emit = _events()
    st = inst.check(emit)
    assert st.state == "error" and "err for pid1" in st.detail
    assert evs and evs[0][0] == "alert"


def test_stuck_does_not_scan_recent_history(monkeypatch):
    # A stuck prompt with no error of its own must NOT borrow an unrelated recent
    # prompt's error (V2 only scans recent history for the stalled case) (Codex #60).
    def _must_not_run(*a, **k):
        raise AssertionError("recent-history scan must not run for a stuck prompt")

    monkeypatch.setattr(cf, "queue_snapshot", lambda h, p, t: (["pid1"], 0))
    monkeypatch.setattr(cf, "check_history_error", lambda h, p, pid, t: None)
    monkeypatch.setattr(cf, "check_recent_history_errors", _must_not_run)
    inst = ComfyUIInstance("comfy", _cfg(stuck_checks=1))
    _, emit = _events()
    st = inst.check(emit)
    assert st.state == "error" and "(" not in st.detail  # no borrowed error cause


def test_stall_recovers_and_can_realert(monkeypatch):
    # stalled → recovered → stalled again must alert a SECOND time (per-episode
    # one-shot, no permanent dedupe).
    seq = iter([([], 1), ([], 0), ([], 1)])  # stalled, empty(recover), stalled
    monkeypatch.setattr(cf, "queue_snapshot", lambda h, p, t: next(seq))
    monkeypatch.setattr(cf, "check_recent_history_errors", lambda h, p, t: None)
    inst = ComfyUIInstance("comfy", _cfg(stall_confirm=1, idle_confirm=1))
    evs, emit = _events()
    inst.check(emit)  # stall #1 → alert
    inst.check(emit)  # empty → recover (clears _stalled)
    inst.check(emit)  # stall #2 → alert again
    assert sum(1 for e in evs if "stalled" in e[1]) == 2
