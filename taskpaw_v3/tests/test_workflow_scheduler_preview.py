"""P2-4 (#295) R3/R5: `scheduler.lost_films` and `scheduler.preview`.

`lost_films` is the one rule behind the pass's 设备失联 attention and the
board's; `preview` answers 待处理's machine select from the pass's own state:
自动 is what the next pass would decide, each device its own finish or why
it cannot take the film."""

from __future__ import annotations

from datetime import timedelta

from taskpaw_v3.tests.test_workflow_local_dispatch import add_film, runtime
from taskpaw_v3.tests.test_workflow_scheduler import (
    CONTROLLER,
    NOW,
    UHD,
    film,
    fleet,
    live,
    open_route,
    run,
    sample,
    worker,
)
from taskpaw_v3.tests.workflow_route_helpers import (
    GPU_8,
    GPU_16,
    GPU_16_FAST,
    StepClock,
    enrol_remote,
)
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import routing
from taskpaw_v3.workflow import scheduler as s


def snapshot(films, workers, *, routes=(), samples=()):
    return m.RoutingSnapshot(
        films=tuple(films),
        routes=tuple(routes),
        workers=tuple(workers),
        samples=tuple(samples),
    )


# ── lost_films (R3) ─────────────────────────────────────────────────────
def test_a_route_on_a_silent_worker_is_lost_and_clears_with_its_heartbeat():
    ws = fleet()[:2]
    f = film()
    routes = [open_route(f, ws[0])]
    quiet = live(ws, **{ws[0].name: {"last_seen": NOW - timedelta(seconds=61)}})
    assert s.lost_films(routes, live=quiet, now=NOW, controller_id=CONTROLLER) == {
        f.film_id: ws[0].worker_id
    }
    assert s.lost_films(routes, live=live(ws), now=NOW, controller_id=CONTROLLER) == {}


def test_lost_films_honours_the_restart_grace_and_skips_the_controller():
    ws = fleet()[:1]
    f, g = film(), film(1)
    local = m.FilmRoute(
        **{
            **open_route(g, ws[0]).model_dump(),
            "worker_id": CONTROLLER,
            "kind": "local",
        }
    )
    routes = [open_route(f, ws[0]), local]
    since = NOW - timedelta(seconds=30)
    assert (
        s.lost_films(
            routes, live={}, now=NOW, controller_id=CONTROLLER, live_since=since
        )
        == {}
    )
    later = NOW + timedelta(seconds=31)
    assert s.lost_films(
        routes, live={}, now=later, controller_id=CONTROLLER, live_since=since
    ) == {f.film_id: ws[0].worker_id}


def test_the_pass_attention_is_lost_films():
    ws = fleet()[:2]
    f = film(claimed_by=None)
    routes = [open_route(f, ws[1])]
    quiet = live(ws, **{ws[1].name: {"last_seen": NOW - timedelta(minutes=5)}})
    result = run([f], ws, routes=routes, live=quiet)
    lost = s.lost_films(routes, live=quiet, now=NOW, controller_id=CONTROLLER)
    assert result.attention == {key: s.LOST for key in lost}
    assert result.withdrawals == ()  # OD2: nothing moves


# ── preview (R5) ────────────────────────────────────────────────────────
def unplanned(**kw):
    return film(planned=False, confirmed_at=None, ltx_model=None, pin_version=0, **kw)


def test_preview_lists_every_device_and_picks_what_the_pass_would():
    ws = fleet()
    fast = ws[3]
    samples = [
        sample(fast, 0.5, n=i) for i in range(3)
    ]  # the 5070 Ti restores at twice the speed
    target = unplanned()
    view = s.preview(
        snapshot([target], ws, samples=samples),
        target.film_id,
        ("standard",),
        live=live(ws),
        now=NOW,
        controller_id=CONTROLLER,
    )["standard"]
    assert view.machine == fast.worker_id and view.waiting is None
    assert view.finish_at is not None and view.finish_at > NOW
    by_name = {d.name: d for d in view.devices}
    assert set(by_name) == {w.name for w in ws}
    assert by_name["5060"].reason == "显存不足，不参与修复"
    assert by_name["5060"].finish_at is None
    assert by_name["5070 Ti"].reason is None
    assert by_name["5070 Ti"].finish_at == view.finish_at
    assert by_name["5060 Ti-1"].finish_at > by_name["5070 Ti"].finish_at


def test_preview_counts_the_queue_ahead_of_the_film():
    ws = fleet()[:1]
    ahead = [film(i) for i in range(2)]  # confirmed earlier: they go first
    target = unplanned()
    view = s.preview(
        snapshot([*ahead, target], ws),
        target.film_id,
        ("standard",),
        live=live(ws),
        now=NOW,
        controller_id=CONTROLLER,
    )["standard"]
    assert view.machine is None
    assert view.waiting == f"等待设备：{s.QUEUE_FULL}"
    assert view.devices[0].reason == s.QUEUE_FULL


def test_preview_greys_a_device_per_tier_and_offline():
    ws = fleet()[:2]
    no_high = live(
        ws,
        **{
            ws[0].name: {"tiers_ready": frozenset({"standard", "good"})},
            ws[1].name: {"last_seen": NOW - timedelta(minutes=2)},
        },
    )
    target = unplanned()
    views = s.preview(
        snapshot([target], ws),
        target.film_id,
        ("standard", "high"),
        live=no_high,
        now=NOW,
        controller_id=CONTROLLER,
    )
    assert views["standard"].machine == ws[0].worker_id
    high = views["high"]
    assert high.machine is None
    assert {d.name: d.reason for d in high.devices} == {
        ws[0].name: "缺少高画质模型",
        ws[1].name: "离线",
    }
    assert high.waiting.startswith("等待设备：")


def test_preview_waits_on_the_controller_disk_gate():
    ws = fleet()[:1]
    target = unplanned()
    view = s.preview(
        snapshot([target], ws),
        target.film_id,
        ("standard",),
        live=live(ws),
        now=NOW,
        controller_id=CONTROLLER,
        controller_free_bytes=10 * s.GB,
    )["standard"]
    assert view.machine is None and view.waiting == s.CONTROLLER_DISK


def test_preview_of_a_4k_film_on_16gb_and_the_unknown_film():
    ws = [worker("16"), worker("8", gpu="NVIDIA GeForce RTX 5060", vram=8151)]
    target = unplanned(max_width=UHD[0], max_height=UHD[1])
    view = s.preview(
        snapshot([target], ws),
        target.film_id,
        ("standard",),
        live=live(ws),
        now=NOW,
        controller_id=CONTROLLER,
    )["standard"]
    assert view.machine == ws[0].worker_id
    assert (
        s.preview(
            snapshot([], ws),
            target.film_id,
            ("standard",),
            live=live(ws),
            now=NOW,
            controller_id=CONTROLLER,
        )
        == {}
    )


def test_preview_with_unchanged_state_equals_the_next_pass(tmp_path):
    clock = StepClock()
    rt = runtime(tmp_path / "wf", clock=clock.now)
    try:
        ws = [
            enrol_remote(rt, "5060 Ti-1", gpu=GPU_16),
            enrol_remote(rt, "5070 Ti", gpu=GPU_16_FAST),
            enrol_remote(rt, "5060", gpu=GPU_8),
        ]
        busy_film = add_film(rt)  # planned: it takes the first device
        target = add_film(rt, plan=False)
        now = rt.clock.now()
        beats = {w.worker_id: s.WorkerLive(last_seen=now) for w in ws}
        snap = rt.store.routing_snapshot(rt.operator)
        stages = routing.gpu_stages(rt.store, rt.operator, snap, rt.executors)
        view = s.preview(
            snap,
            target,
            ("standard",),
            live=beats,
            now=now,
            controller_id=rt.identity.worker_id,
            gpu_stages=stages,
        )["standard"]
        rt.store.confirm_plan(
            rt.operator,
            rt.meta(),
            m.FilmPlanConfirm(
                film_id=target,
                input_revision=1,
                ltx_model="standard",
                machine=None,
            ),
        )
        outcome = routing.run_pass(
            rt.store,
            rt.operator,
            rt.meta,
            controller_id=rt.identity.worker_id,
            live=beats,
            now=rt.clock.now(),
            executors=rt.executors,
            remote_enabled=True,
        )
        decided = {d.film_id: d.worker_id for d in outcome.routed}
        assert busy_film in decided
        assert decided[target] == view.machine
    finally:
        rt.close()


def test_preview_of_a_film_that_needs_no_gpu_names_no_device():
    """§9 CX2: restore and ASR still pending but every volume is reused (no
    GPU stage left): no device option and no crash, 「无需 GPU，本机完成」."""
    from taskpaw_v3.workflow import planning

    ws = fleet()
    target = unplanned()
    view = s.preview(
        snapshot([target], ws),
        target.film_id,
        ("standard",),
        live=live(ws),
        now=NOW,
        controller_id=CONTROLLER,
        gpu_stages={(target.film_id, target.input_revision): frozenset()},
    )["standard"]
    assert view.machine is None and view.waiting is None
    assert view.reason == s.NO_GPU == "无需 GPU，本机完成"
    assert view.devices == ()
    shown = planning.routing_view(view, {}, NOW)
    assert shown == {"auto": {"waiting": None, "text": s.NO_GPU}, "devices": []}


def test_preview_of_a_changed_source_waits_like_the_pass():
    """§9 CX17 (Codex r5): a film whose source the claim would refuse
    (`source_admissible` False) previews the pass's own wait,
    「片源变化，等待重新扫描」, with no machine — never a worker estimate."""
    ws = fleet()
    target = unplanned(source_admissible=False)
    view = s.preview(
        snapshot([target], ws),
        target.film_id,
        ("standard", "high"),
        live=live(ws),
        now=NOW,
        controller_id=CONTROLLER,
    )
    confirmed = target.model_copy(
        update={"planned": True, "confirmed_at": NOW, "ltx_model": "standard"}
    )
    planned = run([confirmed], ws)
    assert planned.routes == ()
    assert planned.waiting[target.film_id] == s.SOURCE_CHANGED
    for tier in ("standard", "high"):
        assert view[tier].waiting == planned.waiting[target.film_id], tier
        assert view[tier].machine is None and view[tier].finish_at is None
        assert view[tier].reason is None


def test_cx28_the_fallback_of_a_film_not_routable_yet_keeps_the_disk_gate():
    """P2-4 §9 CX28 (#298 R1): a film the pass would not route now (it
    holds its own route) previews through the fallback, which applies the
    controller-disk gate by the same helper as the pass (`_disk_full`)."""
    ws = fleet()[:2]
    target = film()
    routes = [open_route(target, ws[0])]
    roomy = s.preview(
        snapshot([target], ws, routes=routes),
        target.film_id,
        ("standard",),
        live=live(ws),
        now=NOW,
        controller_id=CONTROLLER,
    )["standard"]
    assert roomy.machine is not None and roomy.waiting is None
    full = s.preview(
        snapshot([target], ws, routes=routes),
        target.film_id,
        ("standard",),
        live=live(ws),
        now=NOW,
        controller_id=CONTROLLER,
        controller_free_bytes=10 * s.GB,
    )["standard"]
    assert full.machine is None and full.waiting == s.CONTROLLER_DISK
    assert s._disk_full(10 * s.GB, 0.0, 0.0)
    assert not s._disk_full(None, 10**15, 10**15)


def test_cx33_queued_finish_counts_only_the_routes_ahead_on_that_worker():
    """P2-4 §9 CX33 (#298 R1): a kept route's estimate is the work queued
    ahead of it on its worker plus its own — a route behind it never counts."""
    w = fleet()[0]
    ahead, target, behind = film(0), film(1), film(2)
    first = open_route(ahead, w, minutes=30)
    mine = open_route(target, w, minutes=20)
    last = open_route(behind, w, minutes=10)
    alone = snapshot([target], [w], routes=[mine])
    both = snapshot([ahead, target], [w], routes=[first, mine])
    three = snapshot([ahead, target, behind], [w], routes=[first, mine, last])
    solo = s.queued_finish(alone, target.film_id, now=NOW)
    queued = s.queued_finish(both, target.film_id, now=NOW)
    assert solo is not None and queued is not None
    assert queued[0] > solo[0]
    assert s.queued_finish(three, target.film_id, now=NOW) == queued
    assert s.queued_finish(snapshot([target], [w]), target.film_id, now=NOW) is None


def test_cx33_preview_can_keep_the_films_confirm_time():
    """CX33: 改派 previews the film at its own place in the confirm order."""
    w = fleet()[0]
    occupied = film(0)
    target, later = film(10), film(20)
    shot = snapshot([occupied, target, later], [w], routes=[open_route(occupied, w)])
    now_view = s.preview(
        shot,
        target.film_id,
        ("standard",),
        live=live([w]),
        now=NOW,
        controller_id=CONTROLLER,
    )["standard"]
    assert now_view.machine is None  # confirmed "now": the later film goes first
    kept = s.preview(
        shot,
        target.film_id,
        ("standard",),
        live=live([w]),
        now=NOW,
        controller_id=CONTROLLER,
        keep_confirmed=True,
    )["standard"]
    assert kept.machine == w.worker_id and kept.finish_at is not None
