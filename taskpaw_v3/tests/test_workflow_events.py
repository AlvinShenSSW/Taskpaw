"""Workflow notifications remain a second, restart-deduped observation sink."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from taskpaw_v3.agent.server import workflow_events as events
from taskpaw_v3.core.config import WorkflowSettings
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.hub.server.status_md import render_status_md
from taskpaw_v3.monitors.plugins.jasna_progress import parse_jasna_progress_line
from taskpaw_v3.tests.test_workflow_timing import Recorder, board_film
from taskpaw_v3.workflow import board, planning
from taskpaw_v3.workflow.executors import ExecOutcome
from taskpaw_v3.workflow.local_dispatch import LocalDispatcher
from taskpaw_v3.workflow.local_host import LocalWorkflowHost
from taskpaw_v3.workflow.timing import WorkflowLog, film_timing, summary_text


@pytest.fixture
def observer(tmp_path):
    queue = EventQueue("windows")
    return events.WorkflowEvents(queue, tmp_path / "workflow-events.json"), queue


def film(**changes):
    return board_film({}).model_copy(update={"input_revision": 1, **changes})


def host_for(monkeypatch, films, *, current=None, waiting=None):
    calls = []

    def film_board(_auth, done_limit=20, *, film_id=None):
        calls.append(done_limit)
        return tuple(f for f in films if film_id is None or f.film_id == film_id)

    rt = SimpleNamespace(
        operator=object(),
        store=SimpleNamespace(film_board=film_board, list_intake_board=lambda _a: ()),
    )
    monkeypatch.setattr(board, "input_labels", lambda *_a: {})
    monkeypatch.setattr(board, "row_facts", lambda *_a: {})
    monkeypatch.setattr(planning, "plan_list", lambda _rt: {"plans": []})
    host = SimpleNamespace(
        state="running",
        runtime=rt,
        status=lambda: {"dispatch": {"current": current, "waiting": waiting or {}}},
    )
    return host, calls


def test_identity_revision_kind_dedupe_survives_agent_restart(observer):
    sink, queue = observer
    key = str(uuid4())
    assert sink.emit(key, 1, "planning", "待安排")
    assert not sink.emit(key, 1, "planning", "changed wording")
    restarted = events.WorkflowEvents(queue, sink.path)
    assert not restarted.emit(key, 1, "planning", "待安排")
    assert restarted.emit(key, 2, "planning", "待安排")
    assert restarted.emit(str(uuid4()), 1, "planning", "待安排")
    rows = queue.recent()
    assert len(rows) == 3
    assert rows[0]["monitor"] == "media-workflow"
    assert rows[0]["data"] == {
        "instance": "media-workflow",
        "film_id": key,
        "input_revision": 1,
        "kind": "planning",
    }


def test_failed_queue_add_remains_retryable(observer, monkeypatch):
    sink, queue = observer
    original = queue.add
    monkeypatch.setattr(queue, "add", lambda **_k: (_ for _ in ()).throw(OSError()))
    assert not sink.emit("film", 1, "planning", "待安排")
    assert not sink.path.exists()
    monkeypatch.setattr(queue, "add", original)
    assert sink.emit("film", 1, "planning", "待安排")


def test_sidecar_failure_stops_new_emission_until_saved(observer, monkeypatch, caplog):
    sink, queue = observer
    original = events.atomic_json
    monkeypatch.setattr(
        events, "atomic_json", lambda *_a: (_ for _ in ()).throw(OSError())
    )
    sink.emit("film", 1, "planning", "待安排")
    sink.emit("film", 1, "confirm", "待确认")
    assert len(queue) == 1
    assert "workflow event state" in caplog.text
    monkeypatch.setattr(events, "atomic_json", original)
    assert not sink.emit("film", 1, "planning", "待安排")
    assert sink.emit("film", 1, "confirm", "待确认")
    assert len(queue) == 2


def test_corrupt_sidecar_is_not_silently_reset(observer, caplog):
    sink, queue = observer
    sink.path.write_text("{broken", encoding="utf-8")
    assert not sink.emit("film", 1, "planning", "待安排")
    assert len(queue) == 0 and sink.path.read_text() == "{broken"
    assert "workflow event state" in caplog.text


def test_observer_planning_confirmation_and_attention(observer, monkeypatch):
    sink, queue = observer
    pending = film(planned=False)
    confirm = board_film(
        {
            s: (0, 1)
            for s in ("intake", "restore", "asr", "translate", "return", "scrape")
        }
    ).model_copy(update={"input_revision": 1})
    failed = film(stages=(board_film({}, statuses={"restore": "failed"}).stages[1],))
    films = [pending, confirm, failed]
    host, _ = host_for(monkeypatch, films)
    monkeypatch.setattr(
        planning,
        "plan_list",
        lambda _rt: {
            "plans": [
                {
                    "film_id": pending.film_id,
                    "input_revision": 1,
                    "recommendation": {"ltx_model": "good"},
                }
            ],
        },
    )
    sink.observe(host)
    sink.observe(host)
    rows = queue.recent()
    assert sum("待安排：推荐 良好 · 本机" in r["message"] for r in rows) == 1
    assert sum("待确认入库" in r["message"] for r in rows) == 1
    assert sum(r["level"] == "warn" for r in rows) == 1
    assert any("全程" in r["message"] for r in rows)


def test_dispatch_failure_and_observer_share_key_and_cancel_is_silent(
    observer, monkeypatch
):
    sink, queue = observer
    failed = film(stages=board_film({}, statuses={"restore": "failed"}).stages)
    host, _ = host_for(monkeypatch, [failed])
    job = SimpleNamespace(film_id=failed.film_id, input_revision=1, stage="restore")
    sink.resolved(
        host.runtime, job, "Film", ExecOutcome.failed("needs_attention", "failure")
    )
    sink.observe(host)
    assert len(queue) == 1
    assert queue.recent()[0]["message"] == "Film failure"
    job.stage = "asr"
    sink.resolved(host.runtime, job, "Film", ExecOutcome.failed("cancelled", "stop"))
    assert len(queue) == 1
    sink.resolved(
        host.runtime, job, "Film", ExecOutcome.failed("needs_attention", "ASR failure")
    )
    assert len(queue) == 2


def test_translated_uses_existing_summary_and_keeps_tasklog(observer, monkeypatch):
    sink, queue = observer
    translated = board_film(
        {"restore": (0, 90), "asr": (90, 120), "translate": (120, 150)}
    ).model_copy(update={"input_revision": 1})
    host, _ = host_for(monkeypatch, [translated])
    rec = Recorder()
    dispatch = LocalDispatcher(
        host.runtime, workflow_log=WorkflowLog(rec), on_resolved=sink.resolved
    )
    running = SimpleNamespace(
        name="Film",
        started_at=datetime.now(timezone.utc).isoformat(),
        job=SimpleNamespace(
            film_id=translated.film_id, input_revision=1, stage="translate"
        ),
    )
    dispatch._log_resolved(running, ExecOutcome.succeeded())
    sink.observe(host)
    assert [r["kind"] for r in rec.rows] == [
        "workflow.stage.succeeded",
        "workflow.film.translated",
    ]
    assert len(queue) == 1
    assert queue.recent()[0]["message"] == rec.rows[1]["data"]["text"]
    assert queue.recent()[0]["message"] == summary_text("Film", film_timing(translated))


def test_dispatch_event_failure_does_not_change_tasklog(monkeypatch):
    host, _ = host_for(monkeypatch, [])
    rec = Recorder()

    def broken(*_a):
        raise OSError("disk unavailable")

    dispatch = LocalDispatcher(
        host.runtime, workflow_log=WorkflowLog(rec), on_resolved=broken
    )
    running = SimpleNamespace(
        name="Film",
        started_at=datetime.now(timezone.utc).isoformat(),
        job=SimpleNamespace(film_id="f", input_revision=1, stage="restore"),
    )
    dispatch._log_resolved(running, ExecOutcome.failed("needs_attention", "failed"))
    assert [(r["kind"], r["severity"]) for r in rec.rows] == [
        ("workflow.stage.failed", "warn")
    ]


def test_archive_cleanup_results_are_distinct_and_deduped(observer, monkeypatch):
    sink, queue = observer
    archived = board_film({s: (0, 1) for s in board.STAGE_ORDER[:7]}).model_copy(
        update={"input_revision": 1}
    )
    films = [archived]
    host, _ = host_for(monkeypatch, films)
    sink.observe(host)
    assert any("已入库 · 待清理" in e["message"] for e in queue.recent())
    finished = archived.model_copy(
        update={
            "stages": tuple(
                stage.model_copy(update={"ended_at": datetime.now(timezone.utc)})
                if stage.stage in ("cleanup_input", "cleanup_workcopy")
                else stage
                for stage in board_film({s: (0, 1) for s in board.STAGE_ORDER}).stages
            )
        }
    )
    films[:] = [finished]
    sink.observe(host)
    sink.observe(host)
    assert sum("已入库 · 清理完成" in e["message"] for e in queue.recent()) == 1
    assert sum("已入库 · 待清理" in e["message"] for e in queue.recent()) == 1


def test_first_observation_does_not_replay_old_done_films(observer, monkeypatch):
    sink, queue = observer
    queue.add("other-monitor", "current activity")
    old = board_film({s: (0, 1) for s in board.STAGE_ORDER})
    films = [
        old.model_copy(update={"film_id": str(uuid4()), "input_revision": 1})
        for _ in range(251)
    ]
    host, calls = host_for(monkeypatch, films)
    assert not sink.path.exists()
    sink.observe(host)
    assert calls[0] is None
    assert len(queue) == 1
    assert queue.recent()[0]["monitor"] == "other-monitor"
    assert sink.snapshot()["counts"]["done"] == 251
    assert sink.snapshot()["completed_at"] == []
    restarted = events.WorkflowEvents(queue, sink.path)
    restarted.observe(host)
    assert len(queue) == 1


@pytest.mark.parametrize(
    ("hours_ago", "expected"), [(1, 2), (48, 2), (49, 0), (None, 0)]
)
def test_done_recovery_uses_the_existing_summary_window(
    observer, monkeypatch, hours_ago, expected
):
    sink, queue = observer
    now = datetime(2026, 10, 22, 12, tzinfo=timezone.utc)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(events, "datetime", Clock)
    finished = board_film({s: (0, 1) for s in board.STAGE_ORDER})
    ended = now - timedelta(hours=hours_ago) if hours_ago is not None else None
    finished = finished.model_copy(
        update={
            "input_revision": 1,
            "stages": tuple(
                stage.model_copy(update={"ended_at": ended})
                if stage.stage in ("cleanup_input", "cleanup_workcopy")
                else stage
                for stage in finished.stages
            ),
        }
    )
    host, _ = host_for(monkeypatch, [finished])
    sink.observe(host)
    events.WorkflowEvents(queue, sink.path).observe(host)
    assert len(queue) == expected
    if expected:
        assert {row["data"]["kind"] for row in queue.recent()} == {
            "workflow.film.translated",
            "workflow.film.completed",
        }
        assert sink.snapshot()["completed_at"] == [ended.isoformat()]
    else:
        assert sink.snapshot()["completed_at"] == []


def test_old_actionable_films_and_immediate_translated_still_notify(
    observer, monkeypatch
):
    sink, queue = observer
    pending = film(planned=False)
    confirm = board_film({s: (0, 1) for s in board.STAGE_ORDER[:6]}).model_copy(
        update={"input_revision": 1}
    )
    attention = film(stages=board_film({}, statuses={"restore": "failed"}).stages)
    cleanup = board_film({s: (0, 1) for s in board.STAGE_ORDER[:7]}).model_copy(
        update={"input_revision": 1}
    )
    completed = board_film({s: (0, 1) for s in board.STAGE_ORDER}).model_copy(
        update={"input_revision": 1}
    )
    host, _ = host_for(monkeypatch, [pending, confirm, attention, cleanup, completed])
    sink.observe(host)
    required = {
        (pending.film_id, "workflow.film.needs_planning"),
        (confirm.film_id, "workflow.film.needs_confirmation"),
        (attention.film_id, "workflow.stage.failed:restore"),
        (cleanup.film_id, "workflow.film.pending_cleanup"),
    }
    observed = {(r["data"]["film_id"], r["data"]["kind"]) for r in queue.recent()}
    assert required <= observed
    assert all(film_id != completed.film_id for film_id, _kind in observed)
    job = SimpleNamespace(
        film_id=completed.film_id, input_revision=1, stage="translate"
    )
    sink.resolved(host.runtime, job, "Film", ExecOutcome.succeeded())
    immediate = queue.recent()[-1]
    assert immediate["data"]["film_id"] == completed.film_id
    assert immediate["data"]["kind"] == "workflow.film.translated"
    assert immediate["message"] == summary_text("Film", film_timing(completed))


def test_summary_reads_all_completed_films_and_clears_on_error(observer, monkeypatch):
    sink, _queue = observer
    now = datetime.now(timezone.utc)
    completed = board_film({s: (0, 1) for s in board.STAGE_ORDER})
    stages = tuple(s.model_copy(update={"ended_at": now}) for s in completed.stages)
    films = [
        completed.model_copy(
            update={"film_id": str(uuid4()), "input_revision": 1, "stages": stages}
        )
        for _ in range(25)
    ]
    host, calls = host_for(
        monkeypatch, films, current={"film": "Film", "stage": "restore"}
    )
    sink.observe(host)
    assert calls[0] is None
    summary = sink.snapshot()
    assert summary["counts"]["done"] == 25 and len(summary["completed_at"]) == 25
    assert summary["current"]["film"] == "Film"
    host.runtime.store.film_board = lambda *_a, **_k: (_ for _ in ()).throw(
        RuntimeError()
    )
    sink.observe(host)
    assert sink.snapshot() is None


@pytest.mark.parametrize(
    ("line", "progress", "expected"),
    [
        (
            "LTX 2/4 encode:  52%|█████▏    | 156/300 "
            "[00:27<00:25,  5.62frame/s, run 12:12 left]",
            "修复 52%",
            "ABF-394 修复 · LTX 2/4 encode 52%",
        ),
        ("Opening video", "正在打开视频", "ABF-394 修复 · 正在打开视频"),
    ],
)
def test_real_restore_progress_reaches_hub_status(
    observer, monkeypatch, line, progress, expected
):
    sink, _queue = observer
    current = {
        "film": "ABF-394",
        "stage": "restore",
        "progress": progress,
        "ltx": parse_jasna_progress_line(line, {}),
    }
    host, _ = host_for(monkeypatch, [], current=current)
    sink.observe(host)
    text = render_status_md(
        [
            {
                "name": "Windows",
                "reachable": True,
                "status_json": json.dumps({"workflow": sink.snapshot()}),
            }
        ],
        "2026-10-22 12:12:42",
    )
    assert expected in text


def test_summary_drops_old_completions_and_snapshot_is_independent(
    observer, monkeypatch
):
    sink, _queue = observer
    old = board_film({s: (0, 1) for s in board.STAGE_ORDER}).model_copy(
        update={"input_revision": 1}
    )
    host, _ = host_for(monkeypatch, [old], current={"film": "Film"})
    sink.observe(host)
    snapshot = sink.snapshot()
    assert snapshot["completed_at"] == []
    snapshot["current"]["film"] = "edited"
    assert sink.snapshot()["current"]["film"] == "Film"
    host.state = "stopped"
    sink.observe(host)
    assert sink.snapshot() is None


def test_host_joins_observer_before_closing_runtime(tmp_path):
    calls = []
    host = LocalWorkflowHost(WorkflowSettings(), tmp_path, event_queue=EventQueue("m"))
    host.runtime = SimpleNamespace(close=lambda: calls.append("close"))
    host.events = SimpleNamespace(stop=lambda timeout: calls.append("join") or True)
    host.stop()
    assert calls == ["join", "close"]


def test_stuck_observer_keeps_store_open(tmp_path):
    host = LocalWorkflowHost(WorkflowSettings(), tmp_path, event_queue=EventQueue("m"))
    host.runtime = SimpleNamespace(close=lambda: pytest.fail("closed under observer"))
    host.events = SimpleNamespace(stop=lambda timeout: False)
    host.stop(timeout=0)
    assert host.state == "stop_unconfirmed"


def test_observer_thread_can_be_stopped_without_waiting_for_poll(observer, monkeypatch):
    sink, _queue = observer
    observed = threading.Event()
    host, _ = host_for(monkeypatch, [])
    original = sink.observe

    def observe(value):
        original(value)
        observed.set()

    monkeypatch.setattr(sink, "observe", observe)
    sink.start(host)
    assert observed.wait(2)
    assert sink.stop(timeout=2)
    assert sink.snapshot() is None


def test_stalled_state_write_does_not_block_snapshot_or_stop(observer, monkeypatch):
    sink, _queue = observer
    entered, release = threading.Event(), threading.Event()
    original = events.atomic_json

    def save(*args):
        entered.set()
        assert release.wait(5)
        original(*args)

    monkeypatch.setattr(events, "atomic_json", save)
    worker = threading.Thread(target=lambda: sink.emit("film", 1, "planning", "待安排"))
    worker.start()
    try:
        assert entered.wait(2)
        assert sink.snapshot() is None
        assert sink.stop(timeout=0)
    finally:
        release.set()
        worker.join(timeout=2)
    assert not worker.is_alive()


def test_attention_and_archive_failures_keep_warning_notes(observer, monkeypatch):
    sink, queue = observer
    attention = film(stages=board_film({}, statuses={"restore": "unknown"}).stages)
    failed_archive = board_film(
        {s: (0, 1) for s in board.STAGE_ORDER[:6]},
        statuses={"archive": "failed"},
    ).model_copy(update={"input_revision": 1, "archive_error": "NAS unavailable"})
    host, _ = host_for(monkeypatch, [attention, failed_archive])
    sink.observe(host)
    sink.observe(host)
    warnings = [r for r in queue.recent() if r["level"] == "warn"]
    assert len(warnings) == 2
    assert any("等待确认停止" in r["message"] for r in warnings)
    assert any("入库失败：NAS unavailable" in r["message"] for r in warnings)


def test_summary_expires_when_observer_stalls(observer, monkeypatch):
    sink, _queue = observer
    host, _ = host_for(monkeypatch, [])
    sink.observe(host)
    assert sink.snapshot() is not None
    sink._sampled -= events.POLL_SECONDS * 4
    assert sink.snapshot() is None
