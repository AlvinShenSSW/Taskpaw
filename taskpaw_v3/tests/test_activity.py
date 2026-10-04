"""state_file plugin + activity_writer (#22 dev-agent activity monitor)."""

from __future__ import annotations

import json
import re
import shlex
import time
from pathlib import Path

import pytest

from taskpaw_v3.integrations import activity_writer as aw
from taskpaw_v3.monitors.plugins.state_file import (
    StateFileConfig,
    StateFileInstance,
)
from taskpaw_v3.monitors.registry import default_registry


@pytest.fixture(autouse=True)
def _no_parent_process_probe(monkeypatch):
    monkeypatch.setattr(aw, "_producer_identity", lambda: None)


def _collector():
    events: list = []
    return events, (lambda *a, **k: events.append((a, k)))


def _write(path, state, ts=None, tool="claude"):
    obj = {"tool": tool, "state": state, "session": "s1"}
    if ts is not None:
        obj["ts"] = ts
    path.write_text(json.dumps(obj), encoding="utf-8")


# ── registry ─────────────────────────────────────────────────────────────--
def test_state_file_registered():
    reg = default_registry()
    assert "state_file" in set(reg.types())
    assert reg.get("state_file").type_id == "state_file"


# ── state mapping + transitions ──────────────────────────────────────────--
def test_busy_idle_waiting_states(tmp_path):
    f = tmp_path / "act.json"
    inst = StateFileInstance("a1", StateFileConfig(name="agent", path=str(f)))
    events, emit = _collector()

    _write(f, "busy", ts=time.time())
    assert inst.check(emit).state == "running"  # busy → running (baseline, no emit)
    _write(f, "waiting", ts=time.time())
    st = inst.check(emit)
    assert st.state == "idle" and "waiting" in st.detail
    _write(f, "idle", ts=time.time())
    assert inst.check(emit).state == "idle"

    kinds = [a[0] for a, _ in events]
    # first check is baseline (no event); then waiting (info) + idle (done)
    assert "info" in kinds and "done" in kinds


def test_transition_busy_to_idle_emits_done(tmp_path):
    f = tmp_path / "act.json"
    inst = StateFileInstance("a1", StateFileConfig(name="agent", path=str(f)))
    events, emit = _collector()
    _write(f, "busy", ts=time.time())
    inst.check(emit)  # baseline busy
    _write(f, "idle", ts=time.time())
    inst.check(emit)  # busy → idle
    done = [a for a, _ in events if a[0] == "done"]
    assert len(done) == 1


def test_first_observation_no_event(tmp_path):
    f = tmp_path / "act.json"
    _write(f, "busy", ts=time.time())
    inst = StateFileInstance("a1", StateFileConfig(name="agent", path=str(f)))
    events, emit = _collector()
    inst.check(emit)
    assert not events  # baseline only


# ── watchdogs ────────────────────────────────────────────────────────────--
def test_busy_too_long_alerts_once(tmp_path):
    f = tmp_path / "act.json"
    inst = StateFileInstance(
        "a1", StateFileConfig(name="agent", path=str(f), busy_alert_seconds=60)
    )
    events, emit = _collector()
    _write(f, "busy", ts=time.time() - 120)  # busy started 2 min ago
    inst.check(emit)
    inst.check(emit)
    alerts = [a for a, _ in events if a[0] == "alert"]
    assert len(alerts) == 1 and "busy too long" in alerts[0][1]


def test_busy_duration_survives_refresh(tmp_path):
    """Repeated busy writes (fresh ts) must not reset the busy-start clock — the
    watchdog measures continuous busy time, not time-since-last-write (Codex #22)."""
    f = tmp_path / "act.json"
    inst = StateFileInstance(
        "a1", StateFileConfig(name="agent", path=str(f), busy_alert_seconds=9999)
    )
    _, emit = _collector()
    _write(f, "busy", ts=time.time() - 30)
    inst.check(emit)
    start1 = inst._busy_start_ts
    _write(f, "busy", ts=time.time())  # producer refreshes ts mid-turn
    inst.check(emit)
    start2 = inst._busy_start_ts
    assert start1 is not None and start1 == start2  # busy-start NOT reset by refresh


def test_busy_alerts_despite_refreshes(tmp_path):
    """A long busy episode still alerts even when the file ts keeps refreshing."""
    f = tmp_path / "act.json"
    inst = StateFileInstance(
        "a1", StateFileConfig(name="agent", path=str(f), busy_alert_seconds=60)
    )
    events, emit = _collector()
    _write(f, "busy", ts=time.time() - 120)  # episode began 2 min ago
    inst.check(emit)
    _write(f, "busy", ts=time.time())  # refresh with a brand-new ts
    inst.check(emit)  # still flagged busy-too-long
    assert len([a for a, _ in events if a[0] == "alert"]) == 1


def test_stale_file_degrades(tmp_path):
    f = tmp_path / "act.json"
    inst = StateFileInstance(
        "a1", StateFileConfig(name="agent", path=str(f), stale_seconds=30)
    )
    events, emit = _collector()
    _write(f, "busy", ts=time.time() - 300)  # not updated for 5 min
    st = inst.check(emit)
    assert st.state == "degraded"
    assert any(a[0] == "alert" for a, _ in events)


def test_busy_watchdog_resets_after_idle(tmp_path):
    f = tmp_path / "act.json"
    inst = StateFileInstance(
        "a1", StateFileConfig(name="agent", path=str(f), busy_alert_seconds=60)
    )
    events, emit = _collector()
    _write(f, "busy", ts=time.time() - 120)
    inst.check(emit)  # alert 1
    _write(f, "idle", ts=time.time())
    inst.check(emit)  # reset
    _write(f, "busy", ts=time.time() - 120)
    inst.check(emit)  # alert 2 (new busy episode)
    assert len([a for a, _ in events if a[0] == "alert"]) == 2


# ── missing / malformed ──────────────────────────────────────────────────--
def test_missing_file_is_idle_by_default(tmp_path):
    inst = StateFileInstance(
        "a1", StateFileConfig(name="agent", path=str(tmp_path / "none.json"))
    )
    _, emit = _collector()
    assert inst.check(emit).state == "idle"


def test_missing_file_unknown_when_configured(tmp_path):
    inst = StateFileInstance(
        "a1",
        StateFileConfig(
            name="agent", path=str(tmp_path / "none.json"), missing_is_idle=False
        ),
    )
    _, emit = _collector()
    assert inst.check(emit).state == "unknown"


def test_malformed_file_is_error(tmp_path):
    f = tmp_path / "act.json"
    f.write_text("not json", encoding="utf-8")
    inst = StateFileInstance("a1", StateFileConfig(name="agent", path=str(f)))
    _, emit = _collector()
    assert inst.check(emit).state == "error"


# ── activity_writer ──────────────────────────────────────────────────────--
def test_writer_explicit_state(tmp_path):
    out = tmp_path / "a.json"
    aw.write_activity(str(out), "codex", "idle", session="x")
    data = json.loads(out.read_text())
    assert data["tool"] == "codex" and data["state"] == "idle"
    assert data["session"] == "x" and isinstance(data["ts"], float)


def test_writer_atomic_replace(tmp_path):
    out = tmp_path / "a.json"
    aw.write_activity(str(out), "claude", "busy")
    aw.write_activity(str(out), "claude", "idle")  # overwrite
    assert json.loads(out.read_text())["state"] == "idle"
    # no leftover temp files
    assert list(tmp_path.glob(".*.tmp")) == []


def test_writer_creates_parent_dir(tmp_path):
    out = tmp_path / "nested" / "deep" / "a.json"
    aw.write_activity(str(out), "claude", "busy")
    assert out.exists()


@pytest.mark.parametrize(
    "event,expected",
    [
        ("UserPromptSubmit", "busy"),
        ("SessionStart", "busy"),
        ("Notification", "waiting"),
        ("Stop", "idle"),
        ("SubagentStop", "idle"),
        ("UnknownEvent", None),
    ],
)
def test_writer_maps_claude_hook_events(event, expected):
    state, session = aw.state_from_stdin(
        json.dumps({"hook_event_name": event, "session_id": "sess1"})
    )
    assert state == expected
    if expected:
        assert session == "sess1"


def test_writer_stdin_garbage_is_none():
    assert aw.state_from_stdin("not json") == (None, None)


def test_writer_main_ignores_codex_notify_extra_arg(tmp_path):
    # Codex's `notify` program appends its event JSON as a trailing argv. The writer
    # must IGNORE that extra arg (not argparse-error out) so the notify actually
    # records state — otherwise the documented Codex config writes nothing (#168).
    out = tmp_path / "a.json"
    rc = aw.main(
        [
            "--tool",
            "codex",
            "--path",
            str(out),
            "--state",
            "idle",
            '{"type":"agent-turn-complete","turn-id":"x"}',
        ]
    )
    assert rc == 0
    data = json.loads(out.read_text())
    assert data["tool"] == "codex" and data["state"] == "idle"


# ── docs: Claude hook examples must survive Git Bash on Windows (#206) ──────--
_GUIDE = Path(__file__).parents[2] / "docs" / "guides" / "dev-agent-activity.md"


def _guide_hook_commands() -> list[str]:
    text = _GUIDE.read_text(encoding="utf-8")
    cmds: list[str] = []
    for block in re.findall(r"```json\n(.*?)```", text, re.S):
        for groups in json.loads(block).get("hooks", {}).values():
            for group in groups:
                cmds += [h["command"] for h in group["hooks"]]
    return cmds


def test_guide_hook_commands_have_no_backslashes():
    # Claude Code runs hook commands through Git Bash on Windows, which eats the
    # backslashes of a `d:\...` path → `command not found` on every event and the
    # activity file is never written (#206). Every documented command uses `/`.
    cmds = _guide_hook_commands()
    assert cmds, "no Claude hook examples found in the guide"
    assert all("\\" not in c for c in cmds)
    # ...and the guide shows the Windows form (drive letter + forward slashes).
    assert any(re.match(r"[A-Za-z]:/", shlex.split(c)[0]) for c in cmds)


# ── end-to-end: writer → plugin reads it ─────────────────────────────────--
def test_writer_then_plugin_reads_state(tmp_path):
    out = tmp_path / "a.json"
    inst = StateFileInstance("a1", StateFileConfig(name="agent", path=str(out)))
    events, emit = _collector()
    aw.write_activity(str(out), "claude", "busy")
    assert inst.check(emit).state == "running"
    aw.write_activity(str(out), "claude", "idle")
    assert inst.check(emit).state == "idle"
    assert any(a[0] == "done" for a, _ in events)


def test_codex_documented_synthetic_events():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/activity_hooks/codex.json").read_text()
    )
    assert fixture["provenance"]["synthetic"] is True
    for case in fixture["cases"]:
        assert (
            aw.state_from_stdin(json.dumps(case["input"]), tool="codex")[0]
            == case["state"]
        )


def test_writer_ignores_sensitive_fields_and_cleans_failure(
    tmp_path, monkeypatch, capsys
):
    import io

    payload = {
        "hookEventName": "PermissionRequest",
        "sessionId": "synthetic",
        "prompt": "PRIVATE SENTINEL",
    }
    monkeypatch.setattr(aw.sys, "stdin", io.StringIO(json.dumps(payload)))
    out = tmp_path / "out.json"
    assert aw.main(["--tool", "codex", "--path", str(out)]) == 0
    data = json.loads(out.read_text())
    assert set(data) == {
        "tool",
        "state",
        "session",
        "ts",
        "activity_schema",
        "fact_id",
        "fact_committed",
        "fact_ts",
        "link_nonce",
    }
    assert data["activity_schema"] == 3 and data["fact_committed"] is True
    assert aw._projection_matches(data, aw.read_facts(out, "codex"))
    assert "PRIVATE SENTINEL" not in aw.sidecar_path(out).read_bytes().decode("latin1")
    monkeypatch.setattr(
        aw.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("PRIVATE SENTINEL"))
    )
    assert aw.main(["--tool", "codex", "--state", "idle", "--path", str(out)]) == 1
    assert "PRIVATE SENTINEL" not in str(capsys.readouterr())
    assert not list(tmp_path.glob(".*.tmp"))


def test_issue_216_release_is_399():
    from taskpaw_v3 import __version__

    assert __version__ == "3.9.9"


def _fact(event, session, ts, *, unit=None, producer=None, turn=None, tool="codex"):
    data = {
        "hook_event_name": event,
        "session_id": session,
        "turn_id" if tool == "codex" else "prompt_id": turn or "turn-" + session,
    }
    if unit is not None:
        data["tool_use_id"] = unit
    result = aw.hook_fact(json.dumps(data), tool, ts, producer)
    assert result is not None
    return result


def test_i216_documented_claude_rich_kinds_and_privacy(tmp_path):
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/activity_hooks/claude.json").read_text()
    )
    assert fixture["provenance"]["synthetic"]
    for case in fixture["cases"]:
        payload = {
            **case["input"],
            "prompt": "PRIVATE_SENTINEL",
            "tool_input": "PRIVATE_SENTINEL",
            "transcript_path": "PRIVATE_SENTINEL",
        }
        assert aw.state_from_stdin(json.dumps(payload), "claude")[0] == case["state"]
        fact = aw.hook_fact(json.dumps(payload), "claude", 1000)
        assert fact is not None and fact["kind"] == case["rich_kind"]
        assert "PRIVATE_SENTINEL" not in json.dumps(fact)
        assert "synthetic-session" not in json.dumps(fact)
        aw.publish_fact(tmp_path / "state.json", fact)
    assert "PRIVATE_SENTINEL" not in aw.sidecar_path(
        tmp_path / "state.json"
    ).read_bytes().decode("latin1")


def test_i216_duplicate_fact_preserves_first_timestamp(tmp_path):
    path = tmp_path / "state.json"
    aw.publish_fact(path, _fact("UserPromptSubmit", "A", 1000))
    aw.publish_fact(path, _fact("UserPromptSubmit", "A", 1050))
    rows = aw.read_facts(path, "codex")["facts"]
    assert len(rows) == 1 and rows[0]["ts"] == 1000


@pytest.mark.parametrize("fault", ["summary", "delete", "insert", "commit"])
def test_i216_unknown_transfer_failure_rolls_back_all(tmp_path, monkeypatch, fault):
    import sqlite3
    from contextlib import closing
    from types import SimpleNamespace

    path = tmp_path / "state.json"
    monkeypatch.setattr(aw, "_FACT_CAP", 2)
    a = _fact("UserPromptSubmit", "A", 1000)
    aw.publish_fact(path, a, freshness=60)
    aw.publish_fact(
        path, _fact("PostToolUse", "B", 1061, unit="fake-call"), freshness=60
    )
    before = aw.read_facts(path, "codex")
    if fault != "commit":
        clause = {
            "summary": "BEFORE INSERT ON summaries",
            "delete": "BEFORE DELETE ON facts",
            "insert": "BEFORE INSERT ON facts WHEN NEW.kind='interrupt'",
        }[fault]
        with closing(sqlite3.connect(aw.sidecar_path(path))) as conn, conn:
            conn.execute(
                f"CREATE TRIGGER fail_transfer {clause} BEGIN SELECT RAISE(ABORT, 'synthetic'); END"
            )
    else:
        original = aw._open_store

        def opened(p, *, writable):
            conn = original(p, writable=writable)
            if not writable:
                return conn
            assert conn is not None
            return SimpleNamespace(
                execute=conn.execute,
                commit=lambda: (_ for _ in ()).throw(
                    sqlite3.OperationalError("synthetic")
                ),
                rollback=conn.rollback,
                close=conn.close,
            )

        monkeypatch.setattr(aw, "_open_store", opened)
    with pytest.raises(aw.ActivityStoreError):
        aw.publish_fact(path, _fact("Interrupt", "B", 1062), freshness=60)
    assert aw.read_facts(path, "codex") == before
    assert before["facts"][0]["id"] == a["id"]


def test_i216_summary_overflow_has_no_expiry_or_unrelated_clear(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    monkeypatch.setattr(aw, "_SUMMARY_CAP", 1)
    for session in ("A", "C"):
        aw.publish_fact(path, _fact("UserPromptSubmit", session, 1000), freshness=60)
    aw.publish_fact(path, _fact("Interrupt", "B", 90000), freshness=60)
    before = aw.read_facts(path, "codex")
    assert len(before["summaries"]) == 1 and before["overflow"]
    aw.publish_fact(path, _fact("Interrupt", "A", 180000), freshness=60)
    aw.publish_fact(path, _fact("Interrupt", "C", 180000), freshness=60)
    after = aw.read_facts(path, "codex")
    assert not after["summaries"] and after["overflow"]


def test_i216_future_foreign_store_not_rebuilt_and_connections_close(tmp_path):
    import sqlite3
    from contextlib import closing

    path = tmp_path / "state.json"
    fact = _fact("UserPromptSubmit", "A", 1000)
    aw.publish_fact(path, fact)
    db = aw.sidecar_path(path)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("PRAGMA user_version=99")
    before = db.read_bytes()
    with pytest.raises(aw.ActivityStoreError):
        aw.publish_fact(path, fact)
    with pytest.raises(aw.ActivityStoreError):
        aw.read_facts(path, "codex")
    assert db.read_bytes() == before
    db.unlink()  # Product connection is closed (native Windows executes this).
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute("CREATE TABLE unrelated(x TEXT)")
        conn.execute("INSERT INTO unrelated VALUES('fake-marker')")
    if __import__("os").name != "nt":
        db.chmod(0o600)
    before = db.read_bytes()
    with pytest.raises(aw.ActivityStoreError):
        aw.publish_fact(path, fact)
    assert db.read_bytes() == before
    db.unlink()


def test_i216_copied_absolute_writer_concurrent_sessions(tmp_path):
    import subprocess
    import sys
    from concurrent.futures import ThreadPoolExecutor

    writer = tmp_path / "writer-copy.py"
    writer.write_bytes(Path(aw.__file__).read_bytes())
    path = tmp_path / "state.json"

    def invoke(index):
        return subprocess.run(
            [sys.executable, str(writer), "--tool", "codex", "--path", str(path)],
            input=json.dumps(
                {
                    "session_id": "fake-" + str(index),
                    "turn_id": "fake-turn",
                    "hook_event_name": "UserPromptSubmit",
                }
            ),
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )

    # Initialize first, then exercise actual multi-process transactions. The
    # copied script probes only its own controlled Python test parent.
    assert invoke(0).returncode == 0
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(invoke, range(1, 13)))
    assert all(r.returncode == 0 for r in results)
    assert len(aw.read_facts(path, "codex")["facts"]) == 13
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("kind,cap", [("sessions", 64), ("turns", 256)])
def test_i216_scope_capacity_failure_preserves_existing_facts(tmp_path, kind, cap):
    path = tmp_path / "state.json"
    for n in range(cap):
        data = {
            "hook_event_name": "UserPromptSubmit",
            "session_id": str(n) if kind == "sessions" else "A",
            "turn_id": str(n),
        }
        fact = aw.hook_fact(json.dumps(data), "codex", 1000)
        assert fact is not None
        aw.publish_fact(path, fact)
    before = aw.read_facts(path, "codex")
    fact = aw.hook_fact(
        json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "new" if kind == "sessions" else "A",
                "turn_id": "new",
            }
        ),
        "codex",
        1000,
    )
    assert fact is not None
    with pytest.raises(aw.ActivityStoreError):
        aw.publish_fact(path, fact)
    after = aw.read_facts(path, "codex")
    assert after["facts"] == before["facts"]
    assert after["watermark"] == before["watermark"] and not after["overflow"]
    assert len(after["summaries"]) == 1
    assert after["summaries"][0]["session"] == fact["session"]
    assert after["summaries"][0]["turn"] == fact["turn"]


@pytest.mark.parametrize("bad", ["corrupt", "oversize"])
def test_i216_unavailable_store_remains_unchanged(tmp_path, bad):
    import sqlite3
    from contextlib import closing

    path = tmp_path / "state.json"
    db = aw.sidecar_path(path)
    assert not aw.read_facts(path, "codex")["facts"] and not db.exists()
    if bad == "corrupt":
        db.write_bytes(b"synthetic-invalid-sqlite")
    else:
        aw.publish_fact(path, _fact("UserPromptSubmit", "A", 1000))
        with closing(sqlite3.connect(db)) as conn, conn:
            conn.execute("CREATE TABLE synthetic_padding(x BLOB)")
            conn.execute("INSERT INTO synthetic_padding VALUES(zeroblob(9437184))")
    if __import__("os").name != "nt":
        db.chmod(0o600)
    before = db.read_bytes()
    for operation in (
        lambda: aw.read_facts(path, "codex"),
        lambda: aw.publish_fact(path, _fact("Interrupt", "A", 1001)),
    ):
        with pytest.raises(aw.ActivityStoreError):
            operation()
        assert db.read_bytes() == before
    db.unlink()


@pytest.mark.skipif(
    __import__("sys").platform == "win32",
    reason="Native symlink creation requires Windows privileges",
)
def test_i216_symlink_sidecar_never_adopted(tmp_path):
    path = tmp_path / "state.json"
    target = tmp_path / "owned-fake-target"
    target.write_bytes(b"synthetic-marker")
    aw.sidecar_path(path).symlink_to(target)
    with pytest.raises(aw.ActivityStoreError):
        aw.publish_fact(path, _fact("UserPromptSubmit", "A", 1000))
    with pytest.raises(aw.ActivityStoreError):
        aw.read_facts(path, "codex")
    assert target.read_bytes() == b"synthetic-marker"


def test_i216_projection_failure_keeps_committed_fact(tmp_path, monkeypatch, capsys):
    import io

    path = tmp_path / "state.json"
    monkeypatch.setattr(
        aw.sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "UserPromptSubmit",
                    "session_id": "A",
                    "turn_id": "T",
                    "prompt": "PRIVATE_SENTINEL",
                }
            )
        ),
    )
    monkeypatch.setattr(
        aw,
        "_write_projection",
        lambda *a, **k: (_ for _ in ()).throw(OSError("PRIVATE_SENTINEL")),
    )
    assert aw.main(["--tool", "codex", "--path", str(path)]) != 0
    assert len(aw.read_facts(path, "codex")["facts"]) == 1
    assert "PRIVATE_SENTINEL" not in capsys.readouterr().err
    assert not path.exists()


@pytest.fixture(scope="module")
def sr003_templates(tmp_path_factory):
    root = tmp_path_factory.mktemp("sr003-owned").resolve()
    result = {}
    for full in (False, True):
        path = root / ("full.json" if full else "empty.json")
        if full:
            for n in range(256):
                aw.publish_fact(
                    path, _fact("UserPromptSubmit", "old", 1000, turn=f"old-{n}")
                )
        now = 90000 if full else 1000
        for n in range(2048):
            aw.publish_fact(path, _fact("Interrupt", "B", now, producer=(n + 1, 1.0)))
        assert len(aw.read_facts(path, "codex")["summaries"]) == (256 if full else 0)
        result[full] = (aw.sidecar_path(path).read_bytes(), now)
    return result


def _sr003_copy(tmp_path, template):
    path = tmp_path / "agent-activity-codex.json"
    aw.sidecar_path(path).write_bytes(template[0])
    if __import__("os").name != "nt":
        aw.sidecar_path(path).chmod(0o600)
    return path, template[1]


@pytest.mark.parametrize(
    "full,missing_identity", [(False, False), (True, False), (False, True)]
)
def test_i216_sr003_refusal_keeps_durable_unknown(
    tmp_path, sr003_templates, full, missing_identity
):
    path, now = _sr003_copy(tmp_path, sr003_templates[full])
    before = aw.read_facts(path, "codex")
    incoming = _fact("UserPromptSubmit", "A", now + 1, producer=(9001, 1.0))
    if missing_identity:
        incoming = aw.hook_fact(
            json.dumps({"hook_event_name": "UserPromptSubmit"}), "codex", now + 1
        )
        assert incoming is not None
    for _ in range(2):
        with pytest.raises(aw.ActivityStoreError):
            aw.publish_fact(path, incoming)
    after = aw.read_facts(path, "codex")
    assert (
        before["facts"] == after["facts"] and before["watermark"] == after["watermark"]
    )
    assert all(row in after["summaries"] for row in before["summaries"])
    if full or missing_identity:
        assert after["overflow"] and len(after["summaries"]) == len(before["summaries"])
    else:
        assert len(after["summaries"]) == 1 and not after["overflow"]
        assert after["summaries"][0]["session"] == incoming["session"]
        assert after["summaries"][0]["reason"] & 4
    aw.publish_fact(path, _fact("Interrupt", "B", now + 2, producer=(1, 1.0)))
    assert aw.read_facts(path, "codex")["summaries"] == after["summaries"]
    for later in (now + 1000, now + 90000):
        aw.publish_fact(path, _fact("Interrupt", "B", later, producer=(1, 1.0)))
        reopened = aw.read_facts(path, "codex")
        assert (
            reopened["summaries"] == after["summaries"]
            and reopened["overflow"] == after["overflow"]
        )
    if not full and not missing_identity:
        aw.publish_fact(
            path, _fact("Interrupt", "A", now + 90001, producer=(9002, 1.0))
        )
        assert aw.read_facts(path, "codex")["summaries"]
        aw.publish_fact(
            path, _fact("Interrupt", "A", now + 90002, producer=(9001, 1.0))
        )
        assert not aw.read_facts(path, "codex")["summaries"]


def test_i216_sr003_refusal_coalesces_reason_with_full_summaries(
    tmp_path, sr003_templates
):
    path, now = _sr003_copy(tmp_path, sr003_templates[True])
    before = aw.read_facts(path, "codex")
    incoming = _fact("UserPromptSubmit", "old", now + 1, turn="old-0")
    original = next(
        row for row in before["summaries"] if row["turn"] == incoming["turn"]
    )
    assert original["reason"] == 1
    for _ in range(2):
        with pytest.raises(aw.ActivityStoreError):
            aw.publish_fact(path, incoming)
    after = aw.read_facts(path, "codex")
    assert after["facts"] == before["facts"] and len(after["summaries"]) == 256
    updated = next(row for row in after["summaries"] if row["turn"] == incoming["turn"])
    assert updated == {**original, "reason": 5}
    assert not after["overflow"]
    aw.publish_fact(path, _fact("Interrupt", "old", now + 90000, turn="old-0"))
    assert aw.read_facts(path, "codex")["summaries"] == [
        row for row in before["summaries"] if row != original
    ]


@pytest.mark.parametrize("kind,cap", [("sessions", 64), ("turns", 256)])
def test_i216_sr003_refusal_reverts_provisional_expiry(tmp_path, kind, cap):
    path = tmp_path / "state.json"
    for n in range(cap):
        aw.publish_fact(
            path,
            _fact(
                "UserPromptSubmit",
                str(n) if kind == "sessions" else "S",
                89999,
                turn=str(n),
            ),
        )
    late = _fact(
        "PostToolUse",
        "0" if kind == "sessions" else "S",
        1000,
        turn="0",
        unit="late-call",
    )
    aw.publish_fact(path, late)
    before = aw.read_facts(path, "codex")
    incoming = _fact(
        "UserPromptSubmit", "new" if kind == "sessions" else "S", 90000, turn="new"
    )
    with pytest.raises(aw.ActivityStoreError):
        aw.publish_fact(path, incoming)
    after = aw.read_facts(path, "codex")
    assert (
        after["facts"] == before["facts"] and after["watermark"] == before["watermark"]
    )
    assert (
        len(after["summaries"]) == 1
        and after["summaries"][0]["turn"] == incoming["turn"]
    )
    assert not after["overflow"]
    aw.publish_fact(
        path, _fact("Interrupt", "0" if kind == "sessions" else "S", 90001, turn="0")
    )
    assert any(
        row["turn"] == incoming["turn"]
        for row in aw.read_facts(path, "codex")["summaries"]
    )


@pytest.mark.parametrize("local_first", [False, True])
@pytest.mark.parametrize("local_route", ["missing_identity", "summary_full"])
def test_i216_sr003_global_local_bits_preserved(tmp_path, local_first, local_route):
    import sqlite3
    from contextlib import closing

    path = tmp_path / "agent-activity.json"
    for n in range(64):
        aw.publish_fact(
            path,
            _fact(
                "UserPromptSubmit",
                "same",
                1000,
                tool="codex" if n == 0 else f"tool-{n:02}",
            ),
        )
    if local_route == "summary_full":
        for n in range(255):
            aw.publish_fact(path, _fact("PostToolUse", "old", 1000, turn=f"old-{n}"))

    def local():
        if local_route == "summary_full":
            aw.publish_fact(path, _fact("Interrupt", "same", 90000))
            with closing(sqlite3.connect(aw.sidecar_path(path))) as conn:
                assert (
                    conn.execute("SELECT count(*) FROM summaries").fetchone()[0] == 256
                )
        raw = (
            {"hook_event_name": "UserPromptSubmit"}
            if local_route == "missing_identity"
            else {
                "hook_event_name": "UserPromptSubmit",
                "session_id": "lost",
                "turn_id": "lost",
            }
        )
        fact = aw.hook_fact(json.dumps(raw), "codex", 1000)
        assert fact is not None
        aw.publish_fact(path, fact)
        aw.publish_fact(path, _fact("Interrupt", "same", 90000))
        assert aw.read_facts(path, "codex")["overflow"]

    if local_first:
        local()
        assert not aw.read_facts(path, "tool-01")["overflow"]
        assert not aw.read_facts(path, "tool-65")["overflow"]
    before_refusal = aw.read_facts(path, "codex")
    absent = _fact(
        "UserPromptSubmit", "new", 90001 if local_first else 1001, tool="tool-65"
    )
    with pytest.raises(aw.ActivityStoreError):
        aw.publish_fact(path, absent)
    assert aw.read_facts(path, "tool-65")["overflow"]
    assert aw.read_facts(path, "tool-01")["overflow"]
    after_refusal = aw.read_facts(path, "codex")
    assert after_refusal["facts"] == before_refusal["facts"]
    assert after_refusal["summaries"] == before_refusal["summaries"]
    assert after_refusal["watermark"] == before_refusal["watermark"]
    assert not aw.read_facts(path, "tool-65")["facts"]
    assert not aw.read_facts(path, "tool-65")["summaries"]
    if not local_first:
        local()
    for later in (90002, 180000):
        aw.publish_fact(path, _fact("Interrupt", "same", later))
        assert aw.read_facts(path, "tool-65")["overflow"]
    with closing(sqlite3.connect(aw.sidecar_path(path))) as conn:
        assert conn.execute("SELECT count(*) FROM tools").fetchone()[0] == 64
        carrier = conn.execute(
            "SELECT overflow FROM tools WHERE tool='codex'"
        ).fetchone()[0]
        assert carrier & 1 and carrier & 2
    # Fresh independent work is still usable with the store-wide unknown bit.
    aw.publish_fact(path, _fact("UserPromptSubmit", "healthy", 180001, turn="healthy"))
    assert any(r["kind"] == "busy" for r in aw.read_facts(path, "codex")["facts"])
    from taskpaw_v3.monitors.plugins import dev_activity as da

    assert (
        da.read_hook_activity(str(tmp_path), "codex", 300, 180001, {}, set(), [])[
            "state"
        ]
        == "busy"
    )
    aw.publish_fact(path, _fact("PermissionRequest", "healthy", 180001, tool="tool-01"))
    assert (
        da.read_hook_activity(str(tmp_path), "tool-01", 300, 180001, {}, set(), [])[
            "state"
        ]
        == "waiting"
    )


@pytest.mark.parametrize("fault", ["summary", "local", "global", "commit"])
def test_i216_sr003_fallback_sql_failure_full_rollback(tmp_path, monkeypatch, fault):
    import sqlite3
    from contextlib import closing
    from types import SimpleNamespace

    path = tmp_path / "state.json"
    monkeypatch.setattr(aw, "_FACT_CAP", 1)
    if fault == "global":
        monkeypatch.setattr(aw, "_TOOL_CAP", 1)
    aw.publish_fact(path, _fact("Interrupt", "B", 1000))
    before = aw.read_facts(path, "codex")

    def snapshot():
        with closing(sqlite3.connect(aw.sidecar_path(path))) as conn:
            return {
                table: conn.execute(f"SELECT * FROM {table} ORDER BY 1,2").fetchall()
                for table in ("facts", "summaries", "tools")
            }

    all_before = snapshot()
    incoming = _fact(
        "UserPromptSubmit",
        "A",
        1001,
        tool="new-tool" if fault in {"global", "summary"} else "codex",
    )
    if fault == "local":
        incoming["turn"] = ""
    statements, commits = [], []
    original = aw._open_store

    def opened(p, *, writable):
        conn = original(p, writable=writable)
        if not writable:
            return conn
        assert conn is not None
        conn.set_trace_callback(statements.append)
        if fault != "commit":
            return conn

        def commit():
            commits.append(True)
            raise sqlite3.OperationalError("PRIVATE_SENTINEL")

        return SimpleNamespace(
            execute=conn.execute,
            rollback=conn.rollback,
            close=conn.close,
            commit=commit,
        )

    monkeypatch.setattr(aw, "_open_store", opened)
    if fault != "commit":
        target = (
            "BEFORE INSERT ON summaries"
            if fault == "summary"
            else "BEFORE UPDATE OF overflow ON tools"
        )
        with closing(sqlite3.connect(aw.sidecar_path(path))) as conn, conn:
            conn.execute(
                f"CREATE TRIGGER reject_fallback {target} BEGIN SELECT RAISE(ABORT,'PRIVATE_SENTINEL'); END"
            )
    with pytest.raises(aw.ActivityStoreError) as raised:
        aw.publish_fact(path, incoming)
    assert "PRIVATE_SENTINEL" not in str(raised.value)
    assert aw.read_facts(path, "codex") == before
    assert snapshot() == all_before
    if fault == "commit":
        assert commits == [True]
    else:
        target = (
            "INSERT INTO summaries"
            if fault == "summary"
            else "UPDATE tools SET overflow"
        )
        assert any(target in statement for statement in statements)
    # No product connection survives the failed transaction (native Windows
    # runs the same unlink after every owned fixture connection has closed).
    aw.sidecar_path(path).unlink()


def _main_rich(
    monkeypatch,
    path,
    event="UserPromptSubmit",
    *,
    tool="codex",
    session="A",
    ts=1000.0,
    unit=None,
):
    import io

    raw = {
        "hook_event_name": event,
        "session_id": session,
        "turn_id" if tool == "codex" else "prompt_id": "turn-" + session,
    }
    if unit is not None:
        raw["tool_use_id"] = unit
    monkeypatch.setattr(aw.sys, "stdin", io.StringIO(json.dumps(raw)))
    monkeypatch.setattr(aw.time, "time", lambda: ts)
    monkeypatch.setattr(aw, "_producer_identity", lambda: (10, 1.0))
    return aw.main(["--tool", tool, "--path", str(path)])


def test_i216_d01_duplicate_nonce_precommit_and_failed_commit(tmp_path, monkeypatch):
    import sqlite3
    from types import SimpleNamespace

    from taskpaw_v3.monitors.plugins import dev_activity as da

    path = tmp_path / "agent-activity.json"
    assert _main_rich(monkeypatch, path) == 0
    before = aw.read_facts(path, "codex")
    projection = json.loads(path.read_text())
    original_open, original_write = aw._open_store, aw._write_projection
    visible = []

    def opened(p, **kwargs):
        conn = original_open(p, **kwargs)
        if not kwargs["writable"]:
            return conn
        return SimpleNamespace(
            execute=conn.execute,
            rollback=conn.rollback,
            close=conn.close,
            commit=lambda: (_ for _ in ()).throw(
                sqlite3.OperationalError("PRIVATE_SENTINEL")
            ),
        )

    def replaced(*args, **kwargs):
        result = original_write(*args, **kwargs)
        if kwargs.get("fact_committed") is True:
            current = json.loads(path.read_text())
            # An already-existing ID cannot borrow its previous committed nonce.
            assert current["fact_id"] == projection["fact_id"]
            assert current["link_nonce"] != projection["link_nonce"]
            out = da.read_hook_activity(
                str(tmp_path), "codex", 300, 1001, {}, set(), []
            )
            visible.append(out["unknown"] and out["unresolved"])
        return result

    monkeypatch.setattr(aw, "_open_store", opened)
    monkeypatch.setattr(aw, "_write_projection", replaced)
    assert _main_rich(monkeypatch, path, ts=1001) == 1
    assert visible == [True]
    assert aw.read_facts(path, "codex") == before
    assert json.loads(path.read_text())["fact_committed"] is False
    assert not list(tmp_path.glob(".*.tmp"))


def test_i216_d01_json_failure_commits_fact_and_routes_both_owners(
    tmp_path, monkeypatch
):
    from taskpaw_v3.monitors.plugins import dev_activity as da

    path = tmp_path / "agent-activity.json"
    assert _main_rich(monkeypatch, path) == 0
    old = path.read_bytes()
    monkeypatch.setattr(
        aw,
        "_write_projection",
        lambda *a, **k: (_ for _ in ()).throw(OSError("PRIVATE_SENTINEL")),
    )
    assert _main_rich(monkeypatch, path, tool="claude", session="B", ts=1001) == 1
    assert path.read_bytes() == old
    assert len(aw.read_facts(path, "claude")["facts"]) == 1
    assert aw.read_facts(path, "claude")["projection_link"]["tool"] == "claude"
    for tool in ("codex", "claude"):
        out = da.read_hook_activity(str(tmp_path), tool, 300, 1002, {}, set(), [])
        assert out["unknown"] and out["unresolved"]


def test_i216_d01_same_store_writers_serialize_actual_main(tmp_path, monkeypatch):
    import io
    import sqlite3
    import threading
    from types import SimpleNamespace

    path = tmp_path / "agent-activity.json"
    assert _main_rich(monkeypatch, path, session="seed", ts=999) == 0
    first_json, second_begin, release = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    first_committed = threading.Event()
    original_open, original_write = aw._open_store, aw._write_projection
    results, failures = {}, []

    def opened(p, **kwargs):
        conn = original_open(p, **kwargs)
        if not kwargs["writable"]:
            return conn

        def execute(sql, *args):
            if threading.current_thread().name == "second" and sql == "BEGIN IMMEDIATE":
                # Observe real SQLite exclusion while first is paused inside its
                # JSON publication; no scheduler delay or longer lock budget.
                conn.execute("PRAGMA busy_timeout=0")
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    conn.execute(sql, *args)
                conn.execute("PRAGMA busy_timeout=100")
                second_begin.set()
                assert first_committed.wait(3)
            return conn.execute(sql, *args)

        def commit():
            conn.commit()
            if threading.current_thread().name == "first":
                first_committed.set()

        return SimpleNamespace(
            execute=execute,
            rollback=conn.rollback,
            close=conn.close,
            commit=commit,
        )

    def replaced(*args, **kwargs):
        if threading.current_thread().name == "first":
            first_json.set()
            assert release.wait(3)
        return original_write(*args, **kwargs)

    def call(name):
        try:
            results[name] = aw.main(["--tool", "codex", "--path", str(path)])
        except BaseException as exc:
            failures.append(exc)

    monkeypatch.setattr(aw, "_open_store", opened)
    monkeypatch.setattr(aw, "_write_projection", replaced)
    monkeypatch.setattr(
        aw.time,
        "time",
        lambda: 1000 if threading.current_thread().name == "first" else 1001,
    )
    threads = [
        threading.Thread(target=call, args=(name,), name=name)
        for name in ("first", "second")
    ]
    try:
        monkeypatch.setattr(
            aw.sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "first",
                        "turn_id": "T",
                    }
                )
            ),
        )
        threads[0].start()
        assert first_json.wait(3), "first owns write transaction before replacement"
        monkeypatch.setattr(
            aw.sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": "second",
                        "turn_id": "T",
                    }
                )
            ),
        )
        threads[1].start()
        assert second_begin.wait(3), "actual BEGIN is excluded until JSON/commit finish"
        assert not first_committed.is_set() and "second" not in results
    finally:
        release.set()
        for thread in threads:
            if thread.ident is not None:
                thread.join(3)
    assert not failures and not any(t.is_alive() for t in threads)
    assert results == {"first": 0, "second": 0}
    data = json.loads(path.read_text())
    assert data["session"] == "second"
    assert aw._projection_matches(data, aw.read_facts(path, "codex"))


@pytest.mark.parametrize("fault", ["missing", "nonce", "owner", "future", "corrupt"])
def test_i216_d01_shared_singleton_routing_before_tool_filter(
    tmp_path, monkeypatch, fault
):
    import sqlite3
    from contextlib import closing

    from taskpaw_v3.monitors.plugins import dev_activity as da

    path = tmp_path / "agent-activity.json"
    for n in range(8):
        tool = "codex" if n % 2 == 0 else "claude"
        assert _main_rich(monkeypatch, path, tool=tool, session=tool, ts=1000 + n) == 0
        with closing(sqlite3.connect(aw.sidecar_path(path))) as conn:
            assert (
                conn.execute("SELECT count(*) FROM projection_link").fetchone()[0] == 1
            )
        for requested in ("codex", "claude"):
            out = da.read_hook_activity(
                str(tmp_path), requested, 300, 1008, {}, set(), []
            )
            assert out["state"] == ("busy" if n or requested == "codex" else None)
            assert not out["unknown"]
    data = json.loads(path.read_text())
    if fault == "missing":
        path.unlink()
    elif fault == "corrupt":
        path.write_text("owned-invalid-json")
    else:
        data[
            {"nonce": "link_nonce", "owner": "tool", "future": "activity_schema"}[fault]
        ] = {"nonce": "0" * 32, "owner": "codex", "future": 999}[fault]
        path.write_text(json.dumps(data))
    for tool in ("codex", "claude"):
        out = da.read_hook_activity(str(tmp_path), tool, 300, 1008, {}, set(), [])
        assert out["unknown"] == (tool == "claude" or fault in ("owner", "corrupt"))


@pytest.mark.parametrize("owner", [None, []])
def test_i216_c3_s03_unidentifiable_rich_owner_is_unknown_before_filter(
    tmp_path, monkeypatch, owner
):
    from taskpaw_v3.monitors.plugins import dev_activity as da

    path = tmp_path / "agent-activity.json"
    assert _main_rich(monkeypatch, path, session="B", event="Interrupt") == 0
    assert _main_rich(monkeypatch, path, tool="claude", session="A", ts=1001) == 0
    data = json.loads(path.read_text())
    data["tool"] = owner
    path.write_text(json.dumps(data))
    errors = []
    out = da.read_hook_activity(str(tmp_path), "codex", 300, 1002, {}, set(), errors)
    assert out["unknown"] and out["unresolved"]
    assert out["state"] != "idle"
    assert errors == ["unavailable"]


@pytest.mark.parametrize("version", [1, 99])
def test_i216_new_schema_rejects_experimental_and_future_unchanged(tmp_path, version):
    import sqlite3
    from contextlib import closing

    path = tmp_path / "state.json"
    aw.publish_fact(path, _fact("UserPromptSubmit", "A", 1000))
    db = aw.sidecar_path(path)
    with closing(sqlite3.connect(db)) as conn, conn:
        conn.execute(f"PRAGMA user_version={version}")
    before = db.read_bytes()
    for operation in (
        lambda: aw.read_facts(path, "codex"),
        lambda: aw.publish_fact(path, _fact("Interrupt", "A", 1001)),
        lambda: aw._confirm_session_ends(
            (path,), "codex", {("codex", 10, 1.0)}, 1001, None
        ),
    ):
        with pytest.raises(aw.ActivityStoreError):
            operation()
        assert db.read_bytes() == before
    assert not path.exists()


def test_i216_hook_parser_cannot_mint_internal_witness(tmp_path):
    raw = json.dumps(
        {
            "hook_event_name": "SessionEnd",
            "session_id": "A",
            "kind": "verified_session_end",
            "verified": True,
            "root_bound": True,
        }
    )
    fact = aw.hook_fact(raw, "codex", 1000, (10, 1.0))
    assert fact is not None and fact["kind"] == "session_end"
    witness = aw._witness(fact)
    with pytest.raises(aw.ActivityStoreError):
        aw.publish_fact(tmp_path / "state.json", witness)


def _i216_writer_main(monkeypatch, path, rich):
    import io

    monkeypatch.setattr(aw, "_producer_identity", lambda: (10, 1.0))
    monkeypatch.setattr(
        aw.sys,
        "stdin",
        io.StringIO(
            json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "owned"})
        ),
    )
    args = ["--tool", "codex", "--path", str(path)]
    if not rich:
        args += ["--state", "idle"]
    return aw.main(args)


@pytest.mark.skipif(aw.os.name == "nt", reason="POSIX directory mode contract")
@pytest.mark.parametrize("rich", [False, True])
@pytest.mark.parametrize("mode", [0o750, 0o755])
def test_i216_r2_writer_preserves_existing_parent_mode(
    tmp_path, monkeypatch, rich, mode
):
    import stat

    parent = tmp_path / "shared"
    parent.mkdir()
    parent.chmod(mode)
    path = parent / "activity.json"
    assert _i216_writer_main(monkeypatch, path, rich) == 0
    assert stat.S_IMODE(parent.stat().st_mode) == mode
    assert stat.S_IMODE(aw.sidecar_path(path).stat().st_mode) == 0o600


@pytest.mark.skipif(aw.os.name == "nt", reason="POSIX directory mode contract")
@pytest.mark.parametrize("rich", [False, True])
def test_i216_r2_writer_creates_private_leaf(tmp_path, monkeypatch, rich):
    import stat

    path = tmp_path / "new" / "activity.json"
    old_umask = aw.os.umask(0o022)
    try:
        assert _i216_writer_main(monkeypatch, path, rich) == 0
    finally:
        aw.os.umask(old_umask)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(aw.sidecar_path(path).stat().st_mode) == 0o600


@pytest.mark.skipif(aw.os.name == "nt", reason="POSIX directory mode contract")
def test_i216_r2_failed_fact_fallback_creates_private_leaf(
    tmp_path, monkeypatch, capsys
):
    import stat

    path = tmp_path / "fallback" / "activity.json"

    def fail(*a, **k):
        raise aw.ActivityStoreError("PLANTED_PRIVATE_PATH")

    monkeypatch.setattr(aw, "publish_hook", fail)
    old_umask = aw.os.umask(0o022)
    try:
        assert _i216_writer_main(monkeypatch, path, True) == 1
    finally:
        aw.os.umask(old_umask)
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert json.loads(path.read_text())["fact_committed"] is False
    assert not aw.sidecar_path(path).exists()
    assert "PLANTED" not in capsys.readouterr().err


@pytest.mark.parametrize("rich", [False, True])
def test_i216_r2_denied_parent_creation_stays_failed(
    tmp_path, monkeypatch, capsys, rich
):
    path = tmp_path / "denied" / "activity.json"
    original = Path.mkdir

    def denied(self, *a, **k):
        if self == path.parent:
            raise PermissionError("PLANTED_PRIVATE_PATH")
        return original(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", denied)
    assert _i216_writer_main(monkeypatch, path, rich) == 1
    assert not path.exists() and not aw.sidecar_path(path).exists()
    assert "PLANTED" not in capsys.readouterr().err
