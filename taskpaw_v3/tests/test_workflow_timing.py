"""U1e: how long a film took — LTX phase timing, film timing and the log."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from taskpaw_v3.monitors.plugins.jasna_progress import parse_jasna_progress_line
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import board
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import timing as tm

SCAN = "Mosaic scan:  12%|█▏        | 600/5000 [00:40<04:53, 15.00sample/s]"
SPLIT = "ABF-394.mp4: LTX auto split: 300 s with mosaics, 120 s (40%) of it with LTX"
BASIC = (
    "Processing video:  15%|███ |Processed: 06:09 (36703f) | "
    "Remaining: 30:47 (207454f) | Speed: 112.3fps"
)


def ltx(index: int, name: str, *, speed: bool = False, percent: int = 50) -> str:
    head = "LTX speed test" if speed else "LTX"
    return (
        f"{head} {index}/4 {name}:  {percent}%|█████     | 150/300 "
        "[00:27<00:25,  5.62frame/s, run 12:12 left]"
    )


STAGES = ((1, "scan"), (2, "encode"), (3, "denoise"), (4, "compose"))


class Feed:
    """Feeds console lines through the parser and the timer at given times."""

    def __init__(self, start: float = 0.0) -> None:
        self.state: dict = {}
        self.timer = tm.PhaseTimer(start)

    def line(self, text: str, now: float) -> None:
        updated = parse_jasna_progress_line(text, self.state)
        if updated is not self.state:
            self.state = updated
            self.timer.observe(updated, now)


def test_phases_are_classified_from_the_parse_state():
    assert tm.phase_of(parse_jasna_progress_line(SCAN, {})) == "scan"
    assert tm.phase_of(parse_jasna_progress_line(BASIC, {})) == "basicvsr"
    assert tm.phase_of(parse_jasna_progress_line(ltx(2, "encode"), {})) == "ltx:encode"
    speed = parse_jasna_progress_line(ltx(3, "denoise", speed=True), {})
    assert tm.phase_of(speed) == "speed_test"
    assert tm.phase_of(parse_jasna_progress_line(SPLIT, {})) is None
    assert tm.phase_of({}) is None


def test_ltx_stage_times_accumulate_over_every_run_with_the_speed_test_apart():
    """Two auto-split spans, a speed test first, a scan and BasicVSR++ between."""
    feed = Feed(0.0)
    feed.line(SCAN, 5.0)  # 0–5 s: Jasna loading, never attributed
    feed.line(SPLIT, 65.0)  # scan 5 → 65 (60 s); the split note names no phase
    feed.line(ltx(1, "scan", speed=True), 70.0)
    feed.line(ltx(4, "compose", speed=True), 80.0)  # speed test 70 → 90 (20 s)
    t = 90.0
    for index, name in STAGES:  # run 1: 10 s, 20 s, 30 s, 40 s
        feed.line(ltx(index, name), t)
        feed.line(ltx(index, name, percent=99), t + 1)  # same stage: no switch
        t += 10.0 * index
    assert t == 190.0
    feed.line(BASIC, 190.0)  # BasicVSR++ 190 → 250 (60 s)
    t = 250.0
    for index, name in STAGES:  # run 2: 1 s, 2 s, 3 s, 4 s
        feed.line(ltx(index, name), t)
        t += index
    feed.timer.close(t)  # the child exits at 260
    value = feed.timer.snapshot()
    assert value.scan == 60.0 and value.basicvsr == 60.0
    assert value.speed_test == 20.0
    assert value.ltx_stages == {
        "scan": 11.0,
        "encode": 22.0,
        "denoise": 33.0,
        "compose": 44.0,
    }
    assert value.ltx == 110.0  # the four stages only (IR03)
    assert value.ltx_runs == 2
    assert value.wall == 260.0


def test_a_live_snapshot_counts_the_current_phase_up_to_now():
    feed = Feed(100.0)
    feed.line(ltx(2, "encode"), 110.0)
    live = feed.timer.snapshot(140.5)
    assert live.ltx_stages == {"encode": 30.5} and live.ltx == 30.5
    assert live.wall == 40.5 and live.ltx_runs == 1
    # the snapshot never closes the phase
    assert feed.timer.snapshot(150.0).ltx_stages == {"encode": 40.0}


def test_a_phase_ends_when_its_bar_reaches_100_percent():
    """IR02: model loading / muxing / copying between spans is nobody's time."""
    feed = Feed(0.0)
    feed.line(ltx(2, "encode"), 10.0)
    feed.line(ltx(2, "encode", percent=100), 20.0)  # 10 s of encode
    feed.line(ltx(2, "encode", percent=100), 25.0)  # a repeated final redraw
    assert feed.timer.snapshot(60.0).ltx_stages == {"encode": 10.0}  # not running
    feed.line(ltx(3, "denoise"), 70.0)  # the 50 s gap is nobody's
    feed.line(ltx(3, "denoise", percent=100), 80.0)
    value = feed.timer.snapshot(90.0)
    assert value.ltx_stages == {"encode": 10.0, "denoise": 10.0}
    assert value.ltx == 20.0 and value.ltx_runs == 1  # a redraw is no new run
    assert value.wall == 90.0


def test_every_speed_test_stage_is_timed_after_an_earlier_one_completes():
    """Codex r2: the speed test's stages share one bucket, but each of them
    starts its own timing after the one before it reached 100%."""
    feed = Feed(0.0)
    feed.line(ltx(1, "scan", speed=True), 10.0)
    feed.line(ltx(1, "scan", speed=True, percent=100), 20.0)  # 10 s
    feed.line(ltx(1, "scan", speed=True, percent=100), 21.0)  # a final redraw
    feed.line(ltx(2, "encode", speed=True), 30.0)
    feed.line(ltx(2, "encode", speed=True, percent=100), 50.0)  # 20 s
    value = feed.timer.snapshot(60.0)
    assert value.speed_test == 30.0
    assert value.ltx == 0.0 and value.ltx_stages == {}


def test_a_bar_that_counts_to_its_total_is_complete_without_a_percent():
    feed = Feed(0.0)
    feed.line(SCAN, 1.0)
    line = "Mosaic scan: |          | 5000/5000 [04:00<00:00, 15.00sample/s]"
    feed.line(line, 11.0)
    assert feed.timer.snapshot(50.0).scan == 10.0
    full = "LTX 1/4 scan: | | 300/300 [00:27<00:25,  5.62frame/s]"
    feed.line(ltx(1, "scan"), 60.0)
    feed.line(full, 62.0)
    assert feed.timer.snapshot(99.0).ltx_stages == {"scan": 2.0}


def test_a_complete_basicvsr_bar_stops_billing():
    feed = Feed(0.0)
    feed.line(BASIC, 5.0)
    feed.line(BASIC.replace("15%", "100%"), 15.0)
    assert feed.timer.snapshot(500.0).basicvsr == 10.0


def test_a_new_run_starts_when_a_stage_does_not_follow_a_lower_one():
    feed = Feed(0.0)
    for now, text in enumerate(
        (ltx(1, "scan"), ltx(2, "encode"), ltx(1, "scan"), ltx(3, "denoise"))
    ):
        feed.line(text, float(now))
    feed.line(ltx(2, "encode"), 4.0)  # back below the current stage: run 3
    assert feed.timer.snapshot(5.0).ltx_runs == 3


def test_timings_merge_over_volumes():
    a = m.RestoreTiming(scan=1.0, ltx=3.0, ltx_stages={"encode": 3.0}, ltx_runs=1)
    b = m.RestoreTiming(
        basicvsr=2.0,
        ltx=5.0,
        speed_test=1.0,
        ltx_stages={"encode": 1.0, "denoise": 3.0},
    )
    total = tm.merge(a, b, None)
    assert total == m.RestoreTiming(
        scan=1.0,
        basicvsr=2.0,
        ltx=8.0,
        speed_test=1.0,
        ltx_stages={"encode": 4.0, "denoise": 3.0},
        ltx_runs=1,
    )
    assert tm.merge() is None


def test_restore_timing_bounds_its_stage_names():
    with pytest.raises(ValueError):
        m.RestoreTiming(ltx_stages={f"s{i}": 1.0 for i in range(9)})
    with pytest.raises(ValueError):
        m.RestoreTiming(scan=-1.0)


def test_clock_text():
    assert tm.clock(0) == "0:00:00"
    assert tm.clock(3725.6) == "1:02:06"
    assert tm.clock(None) == "—"


# ── film timing from the store's attempts (E2) ─────────────────────────
T0 = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def board_film(
    times: dict[str, tuple[float, float]],
    *,
    statuses: dict[str, str] | None = None,
    restore_timing: m.RestoreTiming | None = None,
    confirmed: float | None = -30.0,
    backfilled: bool = False,
    first: dict[str, float] | None = None,
) -> m.BoardFilm:
    first = first or {}
    statuses = statuses or {}
    return m.BoardFilm(
        film_id=identity(),
        display_name="Film",
        created_at=T0 - timedelta(hours=1),
        final_name=None,
        stages=tuple(
            m.BoardStage(
                stage=stage,
                status=statuses.get(
                    stage, "succeeded" if stage in times else "pending"
                ),
                started_at=at(times[stage][0]) if stage in times else None,
                ended_at=at(times[stage][1]) if stage in times else None,
                first_started_at=at(first[stage]) if stage in first else None,
                timing=restore_timing if stage == "restore" else None,
            )
            for stage in board.STAGE_ORDER
        ),
        approval_active=False,
        archive_open=False,
        archive_error=None,
        planned=True,
        plan_ltx_model="standard",
        plan_machine=identity(),
        plan_confirmed_at=at(confirmed) if confirmed is not None else None,
        plan_backfilled=backfilled,
    )


BREAKDOWN = m.RestoreTiming(
    scan=300.0,
    basicvsr=600.0,
    ltx=2340.0,
    speed_test=60.0,
    ltx_stages={"scan": 60.0, "encode": 600.0, "denoise": 1200.0, "compose": 480.0},
    ltx_runs=2,
    wall=3500.0,
)


def test_film_timing_reads_each_succeeded_attempt_and_the_whole_run():
    value = tm.film_timing(
        board_film(
            {
                "intake": (-90, -89),
                "restore": (0, 60),
                "asr": (61, 71),
                "translate": (75, 95.5),
                "return": (96, 97),
            },
            restore_timing=BREAKDOWN,
        )
    )
    assert value["stages"] == {
        "intake": 60.0,
        "restore": 3600.0,
        "asr": 600.0,
        "translate": 1230.0,
        "return": 60.0,
    }
    # restore start → translate end, the waits between stages included
    assert value["total_restore_to_translate"] == 95.5 * 60
    assert value["queue_wait"] == 1800.0  # plan confirmed 30 min before restore
    breakdown = value["restore_breakdown"]
    assert breakdown["ltx"] == 2340.0 and breakdown["ltx_runs"] == 2
    assert breakdown["ltx_stages"]["denoise"] == 1200.0
    assert "schema_version" not in breakdown


def test_a_failed_first_attempt_belongs_to_the_total_not_to_the_queue_wait():
    """IR01: restore fails after 10 min, then succeeds; 全程 and 排队等待 both
    start at the first attempt, each stage keeps its succeeded attempt's time."""
    value = tm.film_timing(
        board_film(
            {"restore": (20, 80), "asr": (81, 91), "translate": (92, 112)},
            first={"restore": 0.0},
        )
    )
    assert value["queue_wait"] == 1800.0  # plan confirmed 30 min before attempt 1
    assert value["total_restore_to_translate"] == 112 * 60  # from attempt 1
    assert value["stages"]["restore"] == 3600.0  # the succeeded attempt only


def test_film_timing_without_translate_has_no_total_and_running_stages_no_time():
    value = tm.film_timing(
        board_film({"restore": (0, 60), "asr": (61, 71)}, statuses={"asr": "running"})
    )
    assert value["stages"] == {"restore": 3600.0}
    assert value["total_restore_to_translate"] is None
    assert value["restore_breakdown"] is None


def test_a_skipped_restore_starts_the_run_at_the_first_attempt():
    value = tm.film_timing(
        board_film(
            {"asr": (10, 20), "translate": (20, 40)},
            statuses={"restore": "skipped"},
        )
    )
    assert value["total_restore_to_translate"] == 1800.0
    assert value["queue_wait"] is None  # no restore attempt


def test_queue_wait_is_none_for_a_backfilled_plan_or_a_negative_wait():
    assert (
        tm.film_timing(board_film({"restore": (0, 1)}, backfilled=True))["queue_wait"]
        is None
    )
    assert (
        tm.film_timing(board_film({"restore": (0, 1)}, confirmed=5.0))["queue_wait"]
        is None
    )


def test_film_rows_carry_the_timing():
    film = board_film({"restore": (0, 60)}, restore_timing=BREAKDOWN)
    row = board.film_row(film, {})
    assert row["timing"]["stages"]["restore"] == 3600.0
    assert row["timing"]["restore_breakdown"]["ltx"] == 2340.0


def test_the_running_restore_step_carries_the_live_timing():
    film = board_film({}, statuses={"restore": "running"})
    current = {
        "film_id": film.film_id,
        "stage": "restore",
        "ltx": {"current_file": "a.mp4", "timing": {"ltx": 12.0}},
    }
    step = next(
        s
        for s in board.film_row(film, {}, current=current)["steps"]
        if s["key"] == "restore"
    )
    assert step["timing"] == {"ltx": 12.0}
    assert "timing" not in step["ltx"]


# ── the log (E4) ────────────────────────────────────────────────────────
class Recorder:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record(self, task, kind, *, task_type, film=None, severity="info", data=None):
        self.rows.append(
            {
                "task": task,
                "kind": kind,
                "task_type": task_type,
                "film": film,
                "severity": severity,
                "data": data,
            }
        )


def test_stage_rows_name_the_film_the_stage_and_its_time():
    rec = Recorder()
    log = tm.WorkflowLog(rec)
    log.stage("ABF-394", "asr", ok=True, duration=612.34)
    log.stage("ABF-394", "restore", ok=False, duration=5.0, note="Jasna LTX 失败")
    ok, failed = rec.rows
    assert ok == {
        "task": "媒体处理中心",
        "kind": "workflow.stage.succeeded",
        "task_type": "workflow",
        "film": "ABF-394",
        "severity": "info",
        "data": {"stage": "asr", "duration": 612.3},
    }
    assert failed["kind"] == "workflow.stage.failed"
    assert failed["severity"] == "warn"
    assert failed["data"] == {
        "stage": "restore",
        "duration": 5.0,
        "detail": "Jasna LTX 失败",
    }


def test_the_translated_summary_states_the_whole_run():
    rec = Recorder()
    value = tm.film_timing(
        board_film(
            {"restore": (0, 60), "asr": (61, 71), "translate": (75, 95.5)},
            restore_timing=BREAKDOWN,
        )
    )
    tm.WorkflowLog(rec).translated("ABF-394", value)
    (row,) = rec.rows
    assert row["kind"] == "workflow.film.translated" and row["film"] == "ABF-394"
    assert row["data"] == {
        "run": 5730.0,
        "restore": 3600.0,
        "ltx": 2340.0,  # the four stages; the 60 s speed test is its own field
        "speed_test": 60.0,
        "asr": 600.0,
        "translate": 1230.0,
        "text": "ABF-394 全程 1:35:30"
        "（修复→翻译；修复 1:00:00，其中 LTX 0:39:00；识别 0:10:00；翻译 0:20:30）",
    }


def test_a_summary_without_ltx_leaves_that_part_out():
    value = tm.film_timing(
        board_film(
            {"asr": (0, 10), "translate": (10, 20)}, statuses={"restore": "skipped"}
        )
    )
    text = tm.summary_text("ABF-1", value)
    assert text == "ABF-1 全程 0:20:00（修复→翻译；修复 —；识别 0:10:00；翻译 0:10:00）"


def test_a_failing_log_never_breaks_the_caller():
    class Broken:
        def record(self, *args, **kwargs):
            raise OSError("disk full")

    tm.WorkflowLog(Broken()).stage("x", "asr", ok=True, duration=1.0)
    tm.WorkflowLog(None).stage("x", "asr", ok=True, duration=1.0)


# ── through the real executor, dispatcher and store ────────────────────
@pytest.fixture
def tools(tmp_path):
    from taskpaw_v3.tests.workflow_exec_helpers import Tools

    return Tools(tmp_path / "tools")


@pytest.fixture
def rt(tmp_path, tools):
    from taskpaw_v3.tests.workflow_exec_helpers import FakeTranslator, runtime

    FakeTranslator.instances.clear()
    value = runtime(tmp_path / "wf", tools)
    try:
        yield value
    finally:
        value.close()
        FakeTranslator.instances.clear()


@pytest.fixture
def ticking(monkeypatch):
    """The restore's timing clock advances one second per reading."""
    import itertools

    from taskpaw_v3.workflow import exec_restore

    ticks = itertools.count()
    monkeypatch.setattr(exec_restore, "monotonic", lambda: float(next(ticks)))


TWO_SPANS = [
    SCAN,
    SPLIT,
    ltx(1, "scan", speed=True),
    ltx(4, "compose", speed=True),
    *(ltx(i, n) for i, n in STAGES),
    BASIC,
    *(ltx(i, n) for i, n in STAGES),
]


def test_the_restore_keeps_its_phase_times_per_video_and_for_the_film(
    rt, tools, ticking
):
    from taskpaw_v3.tests.workflow_exec_helpers import File, add_film, run_direct, video
    from taskpaw_v3.workflow.stage_results import StageResult

    tools.set_jasna(progress=TWO_SPANS)
    film = add_film(
        rt, [File("cd1.mkv", video(), volume=1), File("cd2.mkv", video(), volume=2)]
    )
    run = run_direct(rt, film, "restore")
    assert run.outcome.ok
    result = StageResult.model_validate_json(
        (run.workdir / "stage-result.json").read_bytes()
    )
    per_video = [v.restore.timing for v in result.videos]
    for value in per_video:
        assert value is not None and value.ltx_runs == 2
        assert set(value.ltx_stages) == {"scan", "encode", "denoise", "compose"}
        assert value.scan > 0 and value.basicvsr > 0 and value.speed_test > 0
        assert value.ltx == pytest.approx(sum(value.ltx_stages.values()))
        assert value.wall >= value.ltx + value.scan + value.basicvsr
    film_total = run.outcome.timing
    assert film_total == tm.merge(*per_video)
    assert film_total.ltx_runs == 4
    # live: the step's timing grows and covers the earlier volume
    live = [s["timing"] for s in run.ltx if "timing" in s]
    assert live and live[-1]["ltx_runs"] == 4
    assert live[-1]["ltx"] >= per_video[0].ltx
    # IR06: the second volume's start already carries the first one's times
    starts = [s for s in run.ltx if "console" not in s]
    assert len(starts) == 2 and "timing" not in starts[0]
    assert starts[1]["timing"]["ltx_runs"] == 2
    assert starts[1]["timing"]["ltx"] == pytest.approx(per_video[0].ltx)


def test_the_live_timing_ticks_while_jasna_is_silent(rt, tools):
    """IR (Codex P2): one encode header, then a quiet but alive child; the
    flusher republishes the growing times about once a second."""
    from taskpaw_v3.tests.workflow_exec_helpers import File, add_film, run_direct, video

    tools.set_jasna(progress=[ltx(2, "encode")], linger=3.0)
    film = add_film(rt, [File("ABC-123.mkv", video())])
    run = run_direct(rt, film, "restore")
    assert run.outcome.ok
    totals = [s["timing"]["ltx"] for s in run.ltx if "timing" in s]
    assert len(totals) >= 3 and totals == sorted(totals)
    assert totals[-1] - totals[0] >= 1.0  # grew while the child was silent


def test_an_older_stage_result_without_timing_still_loads():
    from taskpaw_v3.workflow.stage_results import RestoreFacts

    facts = RestoreFacts.model_validate(
        {
            "decision": "applied",
            "file": {"path": "a/b.mp4", "sha256": "0" * 64, "size": 1},
            "width": 1920,
            "height": 1080,
            "duration_ms": 1000,
            "video_streams": 1,
            "audio_streams": 1,
        }
    )
    assert facts.timing is None


def test_the_board_reads_the_restore_timing_from_its_success_evidence(
    rt, tools, ticking
):
    from taskpaw_v3.tests.workflow_exec_helpers import (
        File,
        add_film,
        attempts,
        dispatcher,
        video,
    )

    tools.set_jasna(progress=TWO_SPANS)
    film = add_film(rt, [File("ABC-123.mkv", video())])
    assert dispatcher(rt).run_once() is True
    (attempt,) = attempts(rt, film, "restore")
    timing = attempt.evidence[-1].summary.timing
    assert timing is not None and timing.ltx_runs == 2
    (row,) = rt.store.film_board(rt.operator, film_id=film)
    restore = next(s for s in row.stages if s.stage == "restore")
    assert restore.timing == timing
    assert restore.started_at == attempt.started_at
    assert restore.first_started_at == attempt.started_at  # its only attempt
    assert restore.ended_at == attempt.ended_at
    value = board.film_row(row, {})["timing"]
    assert value["restore_breakdown"]["ltx_runs"] == 2
    assert value["stages"]["restore"] >= 0
    assert value["queue_wait"] is not None and value["queue_wait"] >= 0
    # evidence of other stages and of older records keeps its exact shape
    plain = m.EvidenceSummary(note="x")
    assert "timing" not in plain.model_dump(mode="json")


def test_the_pipeline_logs_each_stage_and_the_whole_run(rt, tools, ticking):
    from taskpaw_v3.core.gpu_lease import GpuLease
    from taskpaw_v3.tests.workflow_exec_helpers import File, add_film, video
    from taskpaw_v3.workflow.local_dispatch import LocalDispatcher

    tools.set_jasna(progress=TWO_SPANS)
    add_film(rt, [File("ABC-123.mkv", video())])
    rec = Recorder()
    d = LocalDispatcher(
        rt, lease=GpuLease(), poll=0.05, workflow_log=tm.WorkflowLog(rec)
    )
    assert [d.run_once() for _ in range(3)] == [True, True, True]
    assert [(r["kind"], (r["data"] or {}).get("stage")) for r in rec.rows] == [
        ("workflow.stage.succeeded", "restore"),
        ("workflow.stage.succeeded", "asr"),
        ("workflow.stage.succeeded", "translate"),
        ("workflow.film.translated", None),
    ]
    assert {r["film"] for r in rec.rows} == {"ABC-123"}
    assert {r["task"] for r in rec.rows} == {"媒体处理中心"}
    summary = rec.rows[-1]["data"]
    assert summary["text"].startswith("ABC-123 全程 ")
    assert "其中 LTX" in summary["text"]
    assert summary["ltx"] is not None and summary["ltx"] > 0
    assert summary["run"] >= summary["translate"]


def test_a_failed_stage_is_logged_with_its_reason(rt, tools):
    from taskpaw_v3.core.gpu_lease import GpuLease
    from taskpaw_v3.tests.workflow_exec_helpers import File, add_film, video
    from taskpaw_v3.workflow.local_dispatch import LocalDispatcher

    tools.set_jasna(mode="fail")
    add_film(rt, [File("ABC-123.mkv", video())])
    rec = Recorder()
    d = LocalDispatcher(
        rt, lease=GpuLease(), poll=0.05, workflow_log=tm.WorkflowLog(rec)
    )
    assert d.run_once() is True
    (row,) = rec.rows
    assert row["kind"] == "workflow.stage.failed" and row["severity"] == "warn"
    assert row["data"]["stage"] == "restore"
    assert "Jasna LTX 失败" in row["data"]["detail"]


def test_the_host_hands_its_task_log_to_the_dispatcher(tmp_path):
    from taskpaw_v3.workflow.local_host import LocalWorkflowHost

    settings = type("S", (), {})()
    rec = Recorder()
    host = LocalWorkflowHost(settings, tmp_path, task_log=rec)
    assert host.workflow_log.sink is rec
