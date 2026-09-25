"""#196 frozen store contract; all persistence is in pytest's private directory."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from taskpaw_v3.core.tasklog import TaskLog, get_task_log, set_task_log


class Clock:
    def __init__(self):
        self.now = datetime(2026, 9, 26, 23, 59, tzinfo=timezone.utc)
        self.tick = 0.0

    def __call__(self):
        return self.now


def record(store, kind="task.started", task="a", **kw):
    store.record(task, kind, task_type="fake", **kw)


def entries(store, **kw):
    return store.query(**kw)["entries"]


@pytest.mark.parametrize(
    "module",
    [
        "test_lada",
        "test_jasna",
        "test_jasna_subs",
        "test_avsubs",
        "test_subs_translate",
    ],
)
def test_producers_share_tasklog_reader(module):
    import importlib

    reader = importlib.import_module(module)._tasklog
    assert reader.__module__ == "conftest"


@pytest.mark.parametrize(
    "path,phrases",
    [
        ("README.md", ("task log", "30 days")),
        (
            "docs/guides/openclaw-integration.md",
            ("task log", "not forwarded to OpenClaw or your phone"),
        ),
        (
            "docs/specs/2026-06-27-taskpaw-v3-design.md",
            ("3.9.0", "get_task_log().record(...)"),
        ),
        (
            "docs/specs/2026-09-26-196-activity-log-design.md",
            ("title/message verbatim", "fields TaskPaw composes"),
        ),
    ],
)
def test_tasklog_documentation_contract(path, phrases):
    text = (Path(__file__).resolve().parents[2] / path).read_text(encoding="utf-8")
    assert all(phrase in text for phrase in phrases)


def test_complete_unterminated_tail_reserves_id(tmp_path):
    store = TaskLog(tmp_path, clock=Clock())
    record(store)
    path = tmp_path / "logs/tasklog-20260926.jsonl"
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    restarted = TaskLog(tmp_path, clock=Clock())
    record(restarted)
    assert [json.loads(line)["id"] for line in path.read_text().splitlines()] == [
        "20260926-1",
        "20260926-2",
    ]


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_unreadable_startup_scan_blocks_append_until_recovery(
    tmp_path, monkeypatch, error
):
    store = TaskLog(tmp_path, clock=Clock())
    store._n = 40
    record(store)
    path = tmp_path / "logs/tasklog-20260926.jsonl"
    original = Path.open
    locked = True

    def open_file(self, mode="r", *args, **kwargs):
        if self == path and mode == "r" and locked:
            raise error("scan unavailable")
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    failures = []
    restarted = TaskLog(tmp_path, clock=Clock(), on_first_failure=failures.append)
    record(restarted)
    record(restarted)
    assert restarted.write_failures == 2
    assert len(failures) == 1
    assert len(path.read_bytes().splitlines()) == 1
    locked = False
    record(restarted)
    ids = [json.loads(line)["id"] for line in path.read_text().splitlines()]
    assert ids[0] == "20260926-41"
    assert int(ids[1].split("-")[1]) > 41
    assert len(entries(restarted)) == 4


@pytest.mark.parametrize("file_max", [1, 41])
def test_unscanned_ids_remain_unique_monotonic_and_pollable(
    tmp_path, monkeypatch, file_max
):
    store = TaskLog(tmp_path, clock=Clock())
    store._n = file_max - 1
    record(store, task="persisted")
    path = tmp_path / "logs/tasklog-20260926.jsonl"
    original = Path.open
    locked = True

    def open_file(self, mode="r", *args, **kwargs):
        if self == path and mode == "r" and locked:
            raise PermissionError("scan unavailable")
        return original(self, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    recovering = TaskLog(tmp_path, clock=Clock())
    for _ in range(3):
        record(recovering, task="memory")
    memory = entries(recovering, day="20260926")
    memory_ids = {row["id"] for row in memory}
    assert len(memory_ids) == 3
    assert f"20260926-{file_max}" not in memory_ids
    assert all(int(row["id"].split("-")[1]) > file_max for row in memory)
    cursor = memory[0]["id"]

    locked = False
    merged = entries(recovering, day="20260926")
    assert len(merged) == 4
    assert {row["task"] for row in merged} == {"persisted", "memory"}
    assert entries(recovering, after=cursor) == []
    for _ in range(2):
        record(recovering, task="recovered")
    polled = entries(recovering, after=cursor)
    assert [row["task"] for row in polled] == ["recovered", "recovered"]
    assert int(polled[0]["id"].split("-")[1]) > int(cursor.split("-")[1])
    assert entries(recovering, after=polled[-1]["id"]) == []
    assert len(entries(recovering, day="20260926")) == 6

    restarted = TaskLog(tmp_path, clock=Clock())
    record(restarted, task="restarted")
    resumed = entries(restarted, after=polled[-1]["id"])
    assert len(resumed) == 1 and resumed[0]["task"] == "restarted"
    assert (
        int(resumed[0]["id"].split("-")[1]) == int(polled[-1]["id"].split("-")[1]) + 1
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("task", [1]),
        ("kind", 5),
        ("task_type", {}),
        ("severity", []),
        ("severity", "fatal"),
        ("data", [1]),
        ("film", [1]),
        ("proc", 2),
        ("pid", "1"),
        ("pid", True),
    ],
)
def test_malformed_record_shapes_are_skipped_everywhere(tmp_path, field, value):
    store = TaskLog(tmp_path, clock=Clock())
    record(store, "event.mirrored")
    path = tmp_path / "logs/tasklog-20260926.jsonl"
    row = entries(store)[0]
    row[field] = value
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    restarted = TaskLog(tmp_path, clock=Clock())
    assert restarted.reconcile() == {"previous_exit": "first"}
    for query in ({"day": "20260926"}, {"task": "a"}, {"after": "20260926-0"}):
        assert entries(restarted, **query) == []
    assert restarted.query(days=True)["days"] == [{"day": "20260926", "count": 0}]


def test_closed_day_count_tracks_evicted_memory_only_rows(tmp_path, monkeypatch):
    monkeypatch.setattr("taskpaw_v3.core.tasklog.RING_SIZE", 2)
    clock = Clock()
    store = TaskLog(tmp_path, clock=clock)
    record(store)
    original = store._append

    def fail(row):
        raise OSError("locked")

    monkeypatch.setattr(store, "_append", fail)
    record(store)
    monkeypatch.setattr(store, "_append", original)
    clock.now += timedelta(days=1)
    record(store)
    assert store.query(days=True)["days"][1]["count"] == 2
    record(store)
    assert store.query(days=True)["days"][1]["count"] == 1
    assert len(entries(store, day="20260926")) == 1


def test_append_resume_max_counter_torn_line_and_boot(tmp_path):
    clock = Clock()
    store = TaskLog(tmp_path, clock=clock)
    record(store)
    path = tmp_path / "logs/tasklog-20260926.jsonl"
    with path.open("a", encoding="utf-8") as f:
        row = entries(store)[0]
        f.write(json.dumps({**row, "id": "20260926-1000"}) + "\n")
        f.write(json.dumps({**row, "id": "20260926-900"}) + "\n")
        f.write('{"id":"20260926-9000"')
    restarted = TaskLog(tmp_path, clock=clock)
    record(restarted)
    rows = entries(restarted, day="20260926")
    assert [r["id"] for r in rows] == [
        "20260926-1001",
        "20260926-1000",
        "20260926-900",
        "20260926-1",
    ]
    assert restarted.boot != store.boot
    assert path.read_bytes().endswith(b"\n")


def test_memory_only_ring_and_holder():
    store = TaskLog(clock=Clock())
    set_task_log(store)
    assert get_task_log() is store
    for _ in range(2005):
        record(store)
    assert store.query(days=True)["days"] == [{"day": "20260926", "count": 2000}]
    assert len(entries(store, limit=9999)) == 500
    assert len(entries(store, limit=-1)) == 1
    set_task_log(None)
    assert get_task_log() is not store
    assert entries(get_task_log()) == []


def test_failed_write_consumes_id_retry_memory_and_callback_outside_lock(
    tmp_path, monkeypatch
):
    clock = Clock()
    store = TaskLog(tmp_path, clock=clock)
    original = store._append
    attempts = []

    def fail(row):
        attempts.append(row["id"])
        raise PermissionError("locked")

    monkeypatch.setattr(store, "_append", fail)
    record(store)
    record(store)
    assert attempts == ["20260926-1"] * 2 + ["20260926-2"] * 2
    assert store.write_failures == 2
    callbacks = []

    def callback(message):
        assert store._lock.acquire(blocking=False)
        store._lock.release()
        callbacks.append(message)
        raise RuntimeError("callback fails too")

    store.set_on_first_failure(callback)
    record(store)
    assert len(callbacks) == 1
    monkeypatch.setattr(store, "_append", original)
    record(store)
    assert [r["id"] for r in entries(store)] == [f"20260926-{n}" for n in (4, 3, 2, 1)]
    restarted = TaskLog(tmp_path, clock=clock)
    record(restarted)
    assert [r["id"] for r in entries(restarted)] == ["20260926-5", "20260926-4"]


def test_single_retry_can_recover(tmp_path, monkeypatch):
    store = TaskLog(tmp_path, clock=Clock())
    original = store._append
    calls = []

    def transient(row):
        calls.append(row)
        if len(calls) == 1:
            raise OSError("temporary")
        original(row)

    monkeypatch.setattr(store, "_append", transient)
    record(store)
    assert len(calls) == 2 and store.write_failures == 0
    assert len(entries(TaskLog(tmp_path, clock=Clock()))) == 1


def test_retry_repairs_partial_write_and_does_not_duplicate_full_write(
    tmp_path, monkeypatch
):
    store = TaskLog(tmp_path, clock=Clock())
    original = store._append
    path = tmp_path / "logs/tasklog-20260926.jsonl"
    attempts = []

    def torn(row):
        attempts.append(row["id"])
        if len(attempts) == 1:
            path.parent.mkdir(exist_ok=True)
            path.write_text('{"id":', encoding="utf-8")
            raise OSError("interrupted write")
        original(row)

    monkeypatch.setattr(store, "_append", torn)
    record(store)
    assert attempts == ["20260926-1", "20260926-1"]
    assert len(entries(TaskLog(tmp_path, clock=Clock()))) == 1

    def close_failed(row):
        original(row)
        raise OSError("close failed after full append")

    monkeypatch.setattr(store, "_append", close_failed)
    record(store)
    assert path.read_text().count('"id":"20260926-2"') == 1
    assert store.write_failures == 0


def test_concurrent_writers_have_complete_unique_increasing_lines(tmp_path):
    store = TaskLog(tmp_path, clock=Clock())
    threads = [
        threading.Thread(target=lambda: [record(store) for _ in range(30)])
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
        assert not thread.is_alive()
    lines = (tmp_path / "logs/tasklog-20260926.jsonl").read_text().splitlines()
    assert [json.loads(line)["id"] for line in lines] == [
        f"20260926-{i}" for i in range(1, 241)
    ]


def test_numeric_cursors_and_prune_runs_after_writer_unlock(tmp_path, monkeypatch):
    clock = Clock()
    store = TaskLog(tmp_path, clock=clock)
    store._n = 899
    record(store)
    store._n = 999
    record(store)
    assert [r["id"] for r in entries(store, after="20260926-900")] == ["20260926-1000"]
    assert [r["id"] for r in entries(store, before="20260926-1000")] == ["20260926-900"]
    pruned = []

    def prune():
        assert store._lock.acquire(blocking=False)
        store._lock.release()
        pruned.append(True)

    monkeypatch.setattr(store, "prune", prune)
    clock.now += timedelta(days=1)
    record(store)
    assert pruned == [True]


def test_midnight_monotonic_day_filters_task_across_days_and_after(tmp_path):
    clock = Clock()
    store = TaskLog(tmp_path, clock=clock)
    record(store, film="Alpha", severity="warn", data={"model": "Grok"})
    cursor = entries(store)[0]["id"]
    clock.now += timedelta(minutes=2)
    record(store, film="Beta")
    clock.now -= timedelta(days=1)
    record(store, task="b", data={"title": "ALPHA alert"})
    assert [r["id"] for r in entries(store, after=cursor)] == [
        "20260927-1",
        "20260927-2",
    ]
    assert len(entries(store, task="a")) == 2
    assert len(entries(store, day="20260926", severity="warn,error", q="gRoK")) == 1
    assert len(entries(store, day="20260927", q="alpha")) == 1
    page = store.query(day="20260927", limit=1)
    assert page["next_before"] == "20260927-2"
    assert (
        entries(store, day="20260927", before=page["next_before"])[0]["film"] == "Beta"
    )
    assert store.query(days=True)["days"] == [
        {"day": "20260927", "count": 2},
        {"day": "20260926", "count": 1},
    ]


def test_slow_writer_cannot_publish_later_id_first(tmp_path, monkeypatch):
    store = TaskLog(tmp_path, clock=Clock())
    entered, release = threading.Event(), threading.Event()
    original = store._append

    def slow(row):
        if row["id"].endswith("-1"):
            entered.set()
            assert release.wait(3)
        original(row)

    monkeypatch.setattr(store, "_append", slow)
    one = threading.Thread(target=lambda: record(store))
    two = threading.Thread(target=lambda: record(store))
    read = []
    reader = threading.Thread(
        target=lambda: read.extend(entries(store, after="20260926-0"))
    )
    one.start()
    assert entered.wait(3)
    two.start()
    reader.start()
    release.set()
    for thread in (one, two, reader):
        thread.join(3)
        assert not thread.is_alive()
    assert read[0]["id"] == "20260926-1"
    assert [r["id"] for r in entries(store, after="20260926-0")] == [
        "20260926-1",
        "20260926-2",
    ]


def test_sanitizes_every_string_recursively_nonfinite_and_forbidden_values(tmp_path):
    store = TaskLog(tmp_path, clock=Clock())
    bad = "x\ud800"
    store.record(
        bad,
        bad,
        task_type=bad,
        severity=bad,
        film=bad,
        proc=bad,
        data={
            bad: [bad, {bad: float("nan")}],
            "infinity": float("inf"),
            "argv": ["PLANTED_ARGV"],
            "api_key": "PLANTED_SECRET",
            "foo_extra_args": "PLANTED_ARGS",
            "url": "https://USER:PASS@host.test:123/x",
        },
    )
    text = json.dumps(store.query(), ensure_ascii=False, allow_nan=False)
    text.encode("utf-8")
    assert "PLANTED" not in text and "USER:PASS" not in text
    assert "123" not in text
    assert entries(store)[0]["data"]["infinity"] is None
    cyclic = {}
    cyclic["cycle"] = cyclic
    record(store, data=cyclic)  # invalid caller payload never breaks monitoring


def test_mirror_cap_append_only_deltas_restart_rollover_and_stopping(tmp_path):
    clock = Clock()
    store = TaskLog(tmp_path, clock=clock, monotonic=lambda: clock.tick)
    for _ in range(503):
        record(store, "event.mirrored")
    rows = entries(store, after="20260926-499")
    assert [r["kind"] for r in rows] == ["event.mirrored", "event.suppressed"]
    assert rows[-1]["data"] == {"task": "a", "since_cap": True}
    clock.tick = 3601
    record(store, "event.mirrored")
    assert entries(store)[0]["data"] == {"count": 4}
    restarted = TaskLog(tmp_path, clock=clock, monotonic=lambda: clock.tick)
    record(restarted, "event.mirrored")
    assert entries(restarted)[0]["data"] == {"count": 4}  # no new cap marker
    # A wall clock change alone MUST NOT close/cache yesterday before its delta.
    clock.now += timedelta(minutes=2)
    count_before = restarted.query(days=True)["days"][0]["count"]
    record(restarted)
    old_rows = entries(restarted, day="20260926", limit=2)
    assert old_rows[0]["data"] == {"count": 1}
    assert restarted.query(days=True)["days"][1]["count"] == count_before + 1
    for _ in range(502):
        record(restarted, "event.mirrored")
    record(restarted, "agent.stopping", task="")
    assert entries(restarted)[0]["kind"] == "agent.stopping"
    assert entries(restarted)[1]["data"] == {"count": 2}


def test_closed_day_task_cache_skips_absent_tasks(tmp_path, monkeypatch):
    clock = Clock()
    store = TaskLog(tmp_path, clock=clock)
    record(store, task="target")
    clock.now += timedelta(days=1)
    record(store, task="other")
    clock.now += timedelta(days=1)
    record(store, task="other")
    store.query(days=True)
    original = store._read_day
    reads = []

    def read(day):
        reads.append(day)
        return original(day)

    monkeypatch.setattr(store, "_read_day", read)
    assert len(entries(store, task="target")) == 1
    assert "20260927" not in reads


def test_retention_age_size_floor_locked_retry_and_unrelated_files(
    tmp_path, monkeypatch
):
    clock = Clock()
    folder = tmp_path / "logs"
    folder.mkdir()
    for age in (0, 6, 7, 20, 31):
        day = (clock.now - timedelta(days=age)).strftime("%Y%m%d")
        (folder / f"tasklog-{day}.jsonl").write_text("{}\n")
    unrelated = folder / "keep.jsonl"
    unrelated.write_text("keep")
    invalid = folder / "tasklog-not-a-day.jsonl"
    invalid.write_text("keep")
    locked = folder / "tasklog-20260826.jsonl"
    unlink = Path.unlink

    def deny(path, *a, **kw):
        if path == locked:
            raise PermissionError("reader holds file")
        return unlink(path, *a, **kw)

    monkeypatch.setattr(Path, "unlink", deny)
    store = TaskLog(tmp_path, clock=clock)
    assert locked.exists()
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr("taskpaw_v3.core.tasklog.MAX_BYTES", 1)
    store.prune()
    assert sorted(p.name for p in folder.glob("tasklog-*.jsonl")) == [
        "tasklog-20260920.jsonl",
        "tasklog-20260926.jsonl",
        "tasklog-not-a-day.jsonl",
    ]
    assert unrelated.exists()


@pytest.mark.parametrize(
    "step,closer",
    [
        ("restore", "restore.finished"),
        ("restore", "restore.failed"),
        ("restore", "restore.retry"),
        ("restore", "restore.skipped"),
        ("asr", "asr.finished"),
        ("asr", "asr.retry"),
        ("asr", "subs.failed"),
        ("asr", "subs.published"),
        ("translate", "translate.finished"),
        ("translate", "translate.paused"),
        ("translate", "subs.skipped"),
        ("restore", "task.interrupted"),
        ("translate", "task.interrupted"),
    ],
)
def test_reconcile_activity_closers_and_latest_start(tmp_path, step, closer):
    clock = Clock()
    store = TaskLog(tmp_path, clock=clock)
    record(store, "agent.started", task="")
    record(store, f"{step}.started", film="closed")
    record(store, closer, film="closed", data={"step": step})
    record(store, f"{step}.started", film="again")
    record(store, closer, film="again", data={"step": step})
    record(store, f"{step}.started", film="again")
    restarted = TaskLog(tmp_path, clock=clock)
    assert restarted.reconcile()["previous_exit"] == "unclean"
    reconstructed = [
        r for r in entries(restarted) if r.get("data", {}).get("reconstructed")
    ]
    assert [(r["film"], r["data"]["step"]) for r in reconstructed] == [("again", step)]


@pytest.mark.parametrize(
    "closer",
    [
        "task.done",
        "task.aborted",
        "task.started",
        "subs.skipped_bulk",
        "operator.stop",
        "operator.remove",
        "operator.update",
    ],
)
def test_reconcile_task_closers(tmp_path, closer):
    store = TaskLog(tmp_path, clock=Clock())
    record(store, "agent.started", task="")
    for step in ("restore", "asr", "translate"):
        record(store, f"{step}.started", film="old")
    record(store, closer)
    assert TaskLog(tmp_path, clock=Clock()).reconcile()["previous_exit"] == "unclean"
    assert not any(
        r["kind"] == "task.interrupted"
        for r in entries(TaskLog(tmp_path, clock=Clock()))
    )


def test_reconcile_first_clean_missing_start_and_multiday(tmp_path):
    clock = Clock()
    store = TaskLog(tmp_path, clock=clock)
    assert store.reconcile() == {"previous_exit": "first"}
    record(store, "restore.started", film="lost-start")
    clock.now += timedelta(days=1)
    record(store, "translate.started", film="other")
    restarted = TaskLog(tmp_path, clock=clock)
    result = restarted.reconcile()
    assert result["previous_exit"] == "unclean" and result["last_ts"]
    assert (
        len(
            [r for r in entries(restarted, task="a") if r["kind"] == "task.interrupted"]
        )
        == 2
    )
    record(restarted, "agent.stopping", task="")
    record(restarted, "restore.started", film="shutdown-tail")
    assert TaskLog(tmp_path, clock=clock).reconcile() == {"previous_exit": "clean"}


def test_reconcile_started_session_spanning_days_and_step_specific_interrupt(tmp_path):
    clock = Clock()
    store = TaskLog(tmp_path, clock=clock)
    record(store, "agent.started", task="")
    record(store, "task.started")
    record(store, "restore.started", film="same")
    record(store, "translate.started", film="same")
    record(store, "task.interrupted", film="same", data={"step": "restore"})
    clock.now += timedelta(days=2)
    record(store, "task.started", task="other")
    restarted = TaskLog(tmp_path, clock=clock)
    assert restarted.reconcile()["previous_exit"] == "unclean"
    inferred = [r for r in entries(restarted) if r.get("data", {}).get("reconstructed")]
    assert [(r["task"], r["film"], r["data"]["step"]) for r in inferred] == [
        ("a", "same", "translate")
    ]
