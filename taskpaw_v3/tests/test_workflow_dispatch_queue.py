"""P3-6 #305: dispatch policy over fake snapshots; no worker or disk I/O."""

from pathlib import Path

import pytest

from taskpaw_v3.tests.test_workflow_scheduler import (
    NOW,
    UHD,
    film,
    open_route,
    routed,
    run,
    sample,
    worker,
)
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import scheduler as s


def running(w, minutes=0):
    return film(
        minutes,
        pin=w.worker_id,
        restore="running",
        claimed_by=w.worker_id,
        claimed_stage="restore",
        claimed_at=NOW,
    )


def test_auto_waits_for_actual_first_free_worker_not_the_prediction():
    fast, slow = worker("fast", gpu="fast"), worker("slow", gpu="slow")
    a, b, queued = running(fast), running(slow), film(1)
    samples = [sample(fast, 0.1), sample(slow, 10)]
    routes = [open_route(a, fast), open_route(b, slow)]
    assert (
        run([a, b, queued], [fast, slow], routes=routes, samples=samples).routes == ()
    )
    result = run([a, queued], [fast, slow], routes=routes[:1], samples=samples)
    assert routed(result) == {queued.film_id: slow.worker_id}


@pytest.mark.parametrize("claimed", [False, True])
def test_auto_never_prefetches(claimed):
    w = worker("GPU")
    first = running(w) if claimed else film()
    queued = film(1)
    result = run([first, queued], [w], routes=[open_route(first, w)])
    assert result.routes == ()
    assert result.waiting[queued.film_id] == "第 1 位 · 哪台先空给哪台"


def test_three_pins_are_running_prefetched_and_first_waiting():
    w = worker("GPU")
    first, second, third = (
        running(w),
        film(1, pin=w.worker_id),
        film(2, pin=w.worker_id),
    )
    result = run([third, second, first], [w], routes=[open_route(first, w)])
    assert routed(result) == {second.film_id: w.worker_id}
    assert result.waiting == {third.film_id: "第 1 位"}


def test_pin_waits_on_controller_and_never_uses_another_free_worker():
    w, other = worker("GPU"), worker("other")
    first, pinned = film(), film(1, pin=w.worker_id)
    result = run([first, pinned], [w, other], routes=[open_route(first, w)])
    assert result.routes == ()
    assert result.waiting[pinned.film_id] == "第 1 位"


def test_pins_precede_auto_even_when_auto_was_confirmed_first():
    w = worker("GPU")
    auto, pinned = film(), film(1, pin=w.worker_id)
    result = run([auto, pinned], [w])
    assert routed(result) == {pinned.film_id: w.worker_id}
    assert result.waiting[auto.film_id] == "第 1 位 · 哪台先空给哪台"


def test_confirm_order_and_unlimited_controller_queue_including_4k():
    w = worker("GPU")
    films = [film(i, size=UHD if i % 2 else (1920, 1080)) for i in range(30)]
    result = run(list(reversed(films)), [w])
    assert [r.film_id for r in result.routes] == [films[0].film_id]
    assert len(result.waiting) == 29
    assert (
        result.waiting[films[1].film_id] == "第 1 位 · 哪台先空给哪台 · 只给 16 GB 卡"
    )
    assert result.waiting[films[-1].film_id].startswith("第 29 位 ·")


def test_busy_device_preview_is_factual_and_not_offered_as_free():
    w = worker("GPU")
    first, second, queued = running(w), film(1, pin=w.worker_id), film(2)
    snapshot = m.RoutingSnapshot(
        films=(first, second, queued),
        workers=(w,),
        routes=(open_route(first, w), open_route(second, w)),
        samples=(),
    )
    preview = s.preview(
        snapshot,
        queued.film_id,
        ["standard"],
        live={w.worker_id: s.WorkerLive(NOW)},
        now=NOW,
        controller_id="controller",
    )["standard"]
    assert preview.machine is None
    assert preview.devices[0].reason == "正在修复，下一部已排好"


def test_retired_queue_copy_is_absent_from_operator_sources():
    root = Path(__file__).resolve().parents[1]
    sources = [
        root / "workflow/board.py",
        root / "workflow/scheduler.py",
        root / "agent/server/workflow_reads.py",
    ]
    sources += list((root / "ui/src/components").rglob("*.ts"))
    sources += list((root / "ui/src/components").rglob("*.tsx"))
    for path in sources:
        text = path.read_text(encoding="utf-8")
        assert "待领取" not in text, path
        assert "设备队列已满" not in text, path
