"""P2-1 (#292): the pure scheduler over a fake clock and five fake workers.

The fleet is the operator's (OD6) plus one 8 GB card that OD5 guards against:
three 5060 Ti 16 GB, one 5070 Ti 16 GB and one 5060 8 GB.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import scheduler as s

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
CONTROLLER = identity()
GB = 10**9
UHD, FHD = (3840, 2160), (1920, 1080)


def worker(name, *, gpu="NVIDIA GeForce RTX 5060 Ti", vram=16311, **kw):
    limit = UHD if vram >= 15360 else FHD
    values = dict(
        worker_id=identity(),
        name=name,
        enrollment_state="verified",
        admission_state="enabled",
        participation=True,
        boot_reconciled=True,
        gpu_model=gpu,
        vram_mib=vram,
        profiles=tuple(
            m.RoutingProfile(stage=stage, max_width=limit[0], max_height=limit[1])
            for stage in ("restore", "asr")
        ),
    )
    values.update(kw)
    return m.RoutingWorker(**values)


def fleet():
    return [
        worker("5060 Ti-1"),
        worker("5060 Ti-2"),
        worker("5060 Ti-3"),
        worker("5070 Ti", gpu="NVIDIA GeForce RTX 5070 Ti", vram=16303),
        worker("5060", gpu="NVIDIA GeForce RTX 5060", vram=8151),
    ]


def film(
    minutes=0,
    *,
    size=FHD,
    restore="pending",
    asr="pending",
    pin=None,
    tier="standard",
    hours=1.0,
    source=20 * GB,
    **kw,
):
    values = dict(
        film_id=identity(),
        input_revision=1,
        display_name="Film",
        planned=True,
        confirmed_at=NOW - timedelta(hours=10) + timedelta(minutes=minutes),
        ltx_model=tier,
        pin_worker_id=pin,
        pin_version=1,
        restore=restore,
        asr=asr,
        translate="pending",
        max_width=size[0],
        max_height=size[1],
        duration_ms=int(hours * 3_600_000),
        source_bytes=source,
        created_at=NOW - timedelta(days=1),
    )
    values.update(kw)
    return m.RoutingFilm(**values)


def open_route(f, w, *, kind="auto", minutes=30):
    return m.FilmRoute(
        route_id=identity(),
        film_id=f.film_id,
        input_revision=f.input_revision,
        worker_id=w.worker_id,
        kind=kind,
        state="open",
        reason="测试",
        routed_at=NOW - timedelta(minutes=minutes),
        version=1,
    )


def sample(w, ratio, *, stage="restore", tier="standard", size=FHD, watts=None, n=0):
    hours = 1.0
    return m.RateSample(
        stage=stage,
        worker_id=w.worker_id,
        gpu_model=w.gpu_model,
        tier=tier if stage == "restore" else None,
        max_width=size[0],
        max_height=size[1],
        duration_ms=int(hours * 3_600_000),
        wall_seconds=ratio * hours * 3600.0,
        avg_w=watts,
        ended_at=NOW - timedelta(days=1, minutes=n),
    )


def live(workers, **overrides):
    """Every worker online now unless overridden per name."""
    values = {}
    for w in workers:
        extra = overrides.get(w.name, {})
        values[w.worker_id] = s.WorkerLive(
            last_seen=extra.pop("last_seen", NOW - timedelta(seconds=5)), **extra
        )
    return values


def run(films, workers, *, routes=(), samples=(), now=NOW, older=(), **kw):
    snapshot = m.RoutingSnapshot(
        films=tuple(films),
        older_revisions=tuple(older),
        routes=tuple(routes),
        workers=tuple(workers),
        samples=tuple(samples),
    )
    kw.setdefault("live", live(workers))
    kw.setdefault("remote_enabled", True)
    return s.schedule(snapshot, now=now, controller_id=CONTROLLER, **kw)


def routed(result):
    return {r.film_id: r.worker_id for r in result.routes}


# ── OD5: 8 GB cards never restore ────────────────────────────────────────
def test_an_8gb_card_never_takes_restore_but_takes_asr():
    ws = fleet()
    small = ws[4]
    films = [film(i) for i in range(10)]
    result = run(films, ws)
    assert small.worker_id not in routed(result).values()
    assert len(result.routes) == 8  # 4 × 16 GB, 2 routes each (S4)
    asr_only = film(20, restore="succeeded")
    queued = [open_route(f, ws[i]) for i, f in enumerate(films[:4])]
    result = run([asr_only, *films[:4]], ws, routes=queued)
    assert routed(result) == {asr_only.film_id: small.worker_id}


def test_a_restore_pinned_to_an_8gb_card_waits_with_the_reason():
    ws = fleet()
    pinned = film(pin=ws[4].worker_id)
    result = run([pinned], ws)
    assert result.routes == ()
    assert result.waiting[pinned.film_id] == "等待 5060：显存不足，不参与修复"


# ── one open route, capacity, ordering ───────────────────────────────────
def test_at_most_one_route_per_film_and_two_per_worker():
    ws = fleet()[:2]
    films = [film(i) for i in range(6)]
    result = run(films, ws)
    assert len({r.film_id for r in result.routes}) == len(result.routes) == 4
    per_worker = {}
    for r in result.routes:
        per_worker[r.worker_id] = per_worker.get(r.worker_id, 0) + 1
    assert set(per_worker.values()) == {2}
    left = {f.film_id for f in films} - set(routed(result))
    assert all(result.waiting[f].startswith("等待设备") for f in left)


def test_a_film_with_an_open_route_is_not_routed_again():
    ws = fleet()
    f = film()
    result = run([f], ws, routes=[open_route(f, ws[0])])
    assert result.routes == ()


def test_4k_films_go_first_so_16gb_cards_are_not_starved():
    w = fleet()[0]
    early = [film(i) for i in range(2)]
    late_4k = film(30, size=UHD)
    result = run([*early, late_4k], [w])
    assert late_4k.film_id in routed(result)
    assert routed(result).keys() == {late_4k.film_id, early[0].film_id}


# ── prediction ───────────────────────────────────────────────────────────
def test_no_history_means_fifo_onto_the_least_loaded_worker():
    a, b = fleet()[0], fleet()[1]
    busy = film(
        0,
        restore="running",
        claimed_by=a.worker_id,
        claimed_stage="restore",
        claimed_at=NOW - timedelta(minutes=5),
    )
    f = film(5)
    result = run([busy, f], [a, b], routes=[open_route(busy, a)])
    (decision,) = result.routes
    assert decision.worker_id == b.worker_id
    assert "无历史，按排队先后" in decision.reason


def test_the_faster_gpu_model_wins_and_the_reason_names_what_it_beat():
    ws = fleet()
    slow, fast = ws[0], ws[3]
    samples = [sample(slow, 1.0, n=i) for i in range(3)] + [
        sample(fast, 0.5, n=i) for i in range(3)
    ]
    f = film()
    result = run([f], [slow, fast], samples=samples)
    (decision,) = result.routes
    assert decision.worker_id == fast.worker_id
    assert decision.kind == "auto"
    assert decision.reason.startswith("自动 · 约 ")
    assert "后在 5070 Ti 完成" in decision.reason
    assert "胜过 5060 Ti-1 约 " in decision.reason
    assert "无历史" not in decision.reason


def test_history_is_keyed_on_gpu_model_and_resolution_class():
    """A 5060 Ti that has no 4K sample uses its model's other cards'."""
    ws = fleet()
    a, b, fast = ws[0], ws[1], ws[3]
    samples = [sample(b, 0.4, size=UHD, n=i) for i in range(3)] + [
        sample(fast, 0.8, size=UHD, n=i) for i in range(3)
    ]
    f = film(size=UHD)
    result = run([f], [a, fast], samples=samples)
    assert routed(result) == {f.film_id: a.worker_id}


def test_a_tier_factor_scales_the_estimate():
    ws = fleet()
    a, fast = ws[0], ws[3]
    # 5060 Ti only ran `high` (2 × standard), 5070 Ti only `standard`
    samples = (
        [sample(a, 1.0, tier="high", n=i) for i in range(3)]
        + [sample(fast, 0.6, n=i) for i in range(3)]
        + [sample(fast, 1.2, tier="high", n=10 + i) for i in range(3)]
    )
    f = film(tier="standard", hours=4.0)
    result = run([f], [a, fast], samples=samples)
    # 5060 Ti standard ≈ 1.0 / 2 = 0.5 beats 5070 Ti standard 0.6
    assert routed(result) == {f.film_id: a.worker_id}


def test_energy_breaks_a_tie_inside_the_window():
    ws = fleet()
    a, fast = ws[0], ws[3]
    samples = [sample(a, 1.0, watts=300.0, n=i) for i in range(3)] + [
        sample(fast, 0.98, watts=400.0, n=i) for i in range(3)
    ]
    f = film()
    result = run([f], [a, fast], samples=samples)
    (decision,) = result.routes
    assert decision.worker_id == a.worker_id  # slower by < 10 min, less energy
    assert decision.energy_wh is not None and "度" in decision.reason


def test_no_energy_tie_break_outside_the_window():
    ws = fleet()
    a, fast = ws[0], ws[3]
    samples = [sample(a, 1.0, watts=300.0, n=i) for i in range(3)] + [
        sample(fast, 0.5, watts=400.0, n=i) for i in range(3)
    ]
    f = film(hours=2.0)
    assert routed(run([f], [a, fast], samples=samples)) == {f.film_id: fast.worker_id}


def test_a_running_claim_counts_its_remaining_time():
    a, b = fleet()[0], fleet()[1]
    samples = [sample(a, 1.0, n=i) for i in range(3)]
    long_run = film(
        0,
        hours=5.0,
        restore="running",
        claimed_by=a.worker_id,
        claimed_stage="restore",
        claimed_at=NOW - timedelta(hours=1),
    )
    short_queue = film(1, hours=1.0)
    f = film(5)
    result = run(
        [long_run, short_queue, f],
        [a, b],
        routes=[open_route(long_run, a), open_route(short_queue, b)],
        samples=samples,
    )
    assert routed(result) == {f.film_id: b.worker_id}


# ── pins ─────────────────────────────────────────────────────────────────
def test_a_pinned_film_routes_only_to_its_worker():
    ws = fleet()
    target = ws[2]
    busy = [film(i) for i in range(2)]
    pinned = film(5, pin=target.worker_id)
    result = run([*busy, pinned], ws, routes=[open_route(f, target) for f in busy])
    assert pinned.film_id not in routed(result)
    assert result.waiting[pinned.film_id] == "等待 5060 Ti-3：设备队列已满"
    result = run([pinned], ws)
    (decision,) = result.routes
    assert (decision.worker_id, decision.kind) == (target.worker_id, "pinned")
    assert decision.reason.startswith("指定 5060 Ti-3 · 约 ")


def test_a_pin_to_the_controller_is_never_routed():
    """IR9: the store keeps 自动 as NULL; a stray controller pin only waits."""
    f = film(pin=CONTROLLER)
    result = run([f], fleet())
    assert result.routes == () and result.waiting[f.film_id] == "指定设备不可用"


def test_a_drained_worker_keeps_pinned_routes_and_loses_auto_ones():
    ws = fleet()
    drained = ws[0].model_copy(update={"admission_state": "draining"})
    ws[0] = drained
    pinned = film(0, pin=drained.worker_id)
    auto = film(1)
    routes = [open_route(pinned, drained, kind="pinned"), open_route(auto, drained)]
    result = run([pinned, auto], ws, routes=routes)
    assert [w.route_id for w in result.withdrawals] == [routes[1].route_id]
    assert result.withdrawals[0].reason == "停领"
    assert routed(result)[auto.film_id] != drained.worker_id  # re-routed now
    assert pinned.film_id not in routed(result)


def test_an_auto_route_kept_by_a_repin_is_never_withdrawn():
    """改派 onto the worker the film was already routed to keeps that route."""
    ws = fleet()
    drained = ws[0].model_copy(update={"admission_state": "draining"})
    f = film(pin=drained.worker_id)
    result = run([f], [drained, *ws[1:]], routes=[open_route(f, drained)])
    assert result.withdrawals == () and result.routes == ()


def test_a_claimed_route_is_never_withdrawn():
    ws = fleet()
    drained = ws[0].model_copy(update={"admission_state": "draining"})
    f = film(
        restore="running",
        claimed_by=drained.worker_id,
        claimed_stage="restore",
        claimed_at=NOW,
    )
    result = run([f], [drained, *ws[1:]], routes=[open_route(f, drained)])
    assert result.withdrawals == () and result.routes == ()


# ── OD2: 失联 never moves a film ─────────────────────────────────────────
def test_a_lost_worker_raises_attention_and_keeps_its_films():
    ws = fleet()
    lost = ws[0]
    claimed = film(
        0,
        restore="running",
        claimed_by=lost.worker_id,
        claimed_stage="restore",
        claimed_at=NOW - timedelta(minutes=20),
    )
    queued = film(1)
    new = film(2)
    routes = [open_route(claimed, lost), open_route(queued, lost)]
    gone = {"5060 Ti-1": {"last_seen": NOW - timedelta(seconds=61)}}
    result = run([claimed, queued, new], ws, routes=routes, live=live(ws, **gone))
    assert result.withdrawals == ()
    assert result.attention == {claimed.film_id: "设备失联", queued.film_id: "设备失联"}
    assert routed(result)[new.film_id] != lost.worker_id


def test_a_worker_without_any_heartbeat_is_offline():
    ws = fleet()[:1]
    f = film(pin=ws[0].worker_id)
    result = run([f], ws, live={})
    assert result.waiting[f.film_id] == "等待 5060 Ti-1：离线"


# ── eligibility reasons ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("change", "overrides", "reason"),
    [
        ({"enrollment_state": "pending_validation"}, {}, "未核验"),
        ({"participation": False}, {}, "未启用"),
        ({"admission_state": "disabled"}, {}, "未启用"),
        ({"admission_state": "draining"}, {}, "停领"),
        ({}, {"paused": True}, "暂停领取"),
        ({}, {"version_ok": False}, "版本不一致"),
        ({"boot_reconciled": False}, {}, "等待对账"),
        ({}, {"tiers_ready": frozenset({"standard"})}, "缺少高画质模型"),
        ({}, {"asr_ready": False}, "识别未就绪"),
        ({}, {"free_bytes": 80 * GB}, "空间不足"),
    ],
)
def test_each_failing_rule_is_the_reason(change, overrides, reason):
    w, other = fleet()[0].model_copy(update=change), fleet()[1]  # a remote is set up
    f = film(pin=w.worker_id, tier="high")
    result = run([f], [w, other], live=live([w], **{w.name: dict(overrides)}))
    assert result.waiting[f.film_id] == f"等待 5060 Ti-1：{reason}"


def test_a_4k_film_needs_a_4k_profile():
    w = fleet()[0]
    narrow = w.model_copy(
        update={
            "profiles": tuple(
                m.RoutingProfile(stage=p.stage, max_width=1920, max_height=1080)
                for p in w.profiles
            )
        }
    )
    f = film(size=UHD, pin=w.worker_id)
    assert run([f], [narrow]).waiting[f.film_id] == "等待 5060 Ti-1：显存不足 4K"


def test_the_staging_cap_counts_the_workers_open_routes():
    w = fleet()[0]
    queued = film(0, source=150 * GB)
    f = film(1, source=100 * GB, pin=w.worker_id)
    # 2.2 × 150 + 2 = 332 GB queued; + 222 GB > 500 GB
    result = run([queued, f], [w], routes=[open_route(queued, w)])
    assert result.waiting[f.film_id] == "等待 5060 Ti-1：空间不足"


def test_free_disk_counts_the_staging_queued_in_the_same_pass():
    """IR11/CX1: two 50 GB sources on 200 GB free do not both fit."""
    w = worker("5060 Ti-1")
    first, second = film(0, source=50 * GB), film(1, source=50 * GB)
    result = run(
        [first, second], [w], live=live([w], **{"5060 Ti-1": {"free_bytes": 200 * GB}})
    )
    assert routed(result) == {first.film_id: w.worker_id}
    assert result.waiting[second.film_id] == "等待设备：空间不足"


def test_free_disk_counts_the_staging_of_open_routes():
    w = worker("5060 Ti-1")
    queued, f = film(0, source=50 * GB), film(1, source=50 * GB)
    result = run(
        [queued, f],
        [w],
        routes=[open_route(queued, w)],
        live=live([w], **{"5060 Ti-1": {"free_bytes": 200 * GB}}),
    )
    assert result.waiting[f.film_id] == "等待设备：空间不足"


def test_the_controller_disk_gate_stops_all_new_routes():
    ws = fleet()
    f = film(source=40 * GB)
    result = run([f], ws, controller_free_bytes=120 * GB)
    assert result.routes == ()
    assert result.waiting[f.film_id] == "主控磁盘空间不足"
    assert routed(run([f], ws, controller_free_bytes=200 * GB))


def test_the_controller_disk_gate_stops_every_later_route_in_the_pass():
    """IR10: once the gate trips, a smaller film behind it waits too."""
    big, small = film(0, source=400 * GB), film(1, source=1 * GB)
    result = run([big, small], fleet(), controller_free_bytes=500 * GB)
    assert result.routes == ()
    assert result.waiting == {
        big.film_id: "主控磁盘空间不足",
        small.film_id: "主控磁盘空间不足",
    }


def test_retired_revoked_and_unpaired_workers_configure_no_remote():
    """IR1/IR5: without a verified, unrevoked remote worker the pass decides
    nothing; the controller runs every stage itself (phase 1)."""
    ws = [
        fleet()[0].model_copy(update={"admission_state": "revoked"}),
        fleet()[1].model_copy(update={"enrollment_state": "retired"}),
        fleet()[2].model_copy(update={"enrollment_state": "pending_pairing"}),
    ]
    result = run([film()], ws)
    assert result == s.SchedulePass((), (), {}, {})
    assert s.remote_workers(ws, CONTROLLER, enabled=True) == ()
    assert s.remote_workers([*ws, fleet()[3]], CONTROLLER, enabled=True) == ("5070 Ti",)
    # IR1: `workflow.remote_workers: false` is no remote at all
    assert s.remote_workers([*ws, fleet()[3]], CONTROLLER, enabled=False) == ()
    assert run([film()], fleet(), remote_enabled=False) == s.SchedulePass(
        (), (), {}, {}
    )


# ── which films are candidates ───────────────────────────────────────────
def test_only_films_with_a_pending_gpu_stage_are_candidates():
    ws = fleet()
    done = film(0, restore="succeeded", asr="succeeded")
    failed = film(1, restore="failed")
    unplanned = film(2, planned=False)
    no_gpu = film(3)
    asr_wait = film(4, restore="succeeded", asr="pending")
    result = run(
        [done, failed, unplanned, no_gpu, asr_wait],
        ws,
        gpu_stages={(no_gpu.film_id, 1): frozenset()},
    )
    assert set(routed(result)) == {asr_wait.film_id}


def controller_worker():
    return m.RoutingWorker(
        worker_id=CONTROLLER,
        name="本机",
        enrollment_state="verified",
        admission_state="enabled",
        participation=True,
        boot_reconciled=True,
        gpu_model="NVIDIA GeForce RTX 5060",
        vram_mib=8151,
        profiles=(),
    )


def test_the_controllers_unclaimed_local_route_is_handed_over_for_gpu_work():
    """C12: the controller ran a no-GPU restore; its ASR needs a GPU worker."""
    ws = fleet()
    controller = controller_worker()
    f = film(restore="succeeded")
    local = open_route(f, controller, kind="local")
    result = run([f], [controller, *ws], routes=[local])
    assert [w.route_id for w in result.withdrawals] == [local.route_id]
    assert routed(result)[f.film_id] in {w.worker_id for w in ws}
    assert CONTROLLER not in routed(result).values()


def test_a_single_machine_pass_never_touches_the_local_route():
    """IR1: only the controller is enrolled; its local route stays put."""
    controller = controller_worker()
    f = film(restore="succeeded")
    local = open_route(f, controller, kind="local")
    result = run([f], [controller], routes=[local])
    assert result == s.SchedulePass((), (), {}, {})


def test_the_local_route_is_handed_over_only_when_the_film_is_routed():
    """IR1: no eligible worker with a free slot keeps the controller's route."""
    controller, w = controller_worker(), worker("5060 Ti-1")
    f = film(restore="succeeded")
    local = open_route(f, controller, kind="local")
    gone = {"5060 Ti-1": {"last_seen": NOW - timedelta(minutes=5)}}
    offline = run([f], [controller, w], routes=[local], live=live([w], **gone))
    assert offline.withdrawals == () and offline.routes == ()
    assert offline.waiting[f.film_id] == "等待设备：离线"
    busy = [film(i + 1) for i in range(2)]
    full = run(
        [f, *busy],
        [controller, w],
        routes=[local, *(open_route(b, w) for b in busy)],
    )
    assert full.withdrawals == () and full.routes == ()
    online = run([f], [controller, w], routes=[local])
    assert [x.route_id for x in online.withdrawals] == [local.route_id]
    assert routed(online) == {f.film_id: w.worker_id}


def test_an_old_revision_route_keeps_its_slot_and_frees_the_new_revision():
    """IR6: a claimed old-revision route stays open until its work resolves;
    its worker is busy, and the film's new revision may be routed meanwhile."""
    a, b = worker("5060 Ti-1"), worker("5060 Ti-2")
    done = [film(i) for i in range(2)]  # new revisions all done: not in the snapshot
    old = [open_route(f, a).model_copy(update={"input_revision": 1}) for f in done]
    new = film(5)
    assert routed(run([new], [a, b], routes=old)) == {new.film_id: b.worker_id}
    revised = film(6, input_revision=2)
    stale = open_route(revised, a).model_copy(update={"input_revision": 1})
    result = run([revised], [a, b], routes=[stale])
    assert revised.film_id in routed(result) and result.withdrawals == ()


def test_an_old_revision_route_is_accounted_with_its_own_facts():
    """CX5: a claimed old-revision route holds its own revision's staging,
    busy time and output, whether the current revision is finished (absent
    from `films`) or still has smaller work of its own."""
    w = worker("5060 Ti-1")
    claim = dict(claimed_by=w.worker_id, claimed_stage="restore", claimed_at=NOW)
    old = film(0, source=150 * GB, restore="running", **claim)
    stale = open_route(old, w)
    f = film(1, source=100 * GB, pin=w.worker_id)
    # 2.2 × 150 + 2 = 332 GB queued; + 222 GB > 500 GB
    finished = run([f], [w], routes=[stale], older=[old])
    assert finished.waiting[f.film_id] == "等待 5060 Ti-1：空间不足"
    current = old.model_copy(
        update={"input_revision": 2, "source_bytes": 1 * GB, "restore": "pending"}
        | dict.fromkeys(claim)
    )
    revised = run([current, f], [w], routes=[stale], older=[old])
    assert revised.waiting[f.film_id] == "等待 5060 Ti-1：空间不足"
    assert revised.withdrawals == ()
    gate = run([f], [w], routes=[stale], older=[old], controller_free_bytes=380 * GB)
    # 1.2 × 150 = 180 GB coming back + 120 GB + 100 GB reserve > 380 GB
    assert gate.waiting[f.film_id] == "主控磁盘空间不足"


def test_the_controller_is_never_routed():
    controller = worker("本机", worker_id=CONTROLLER)
    f = film()
    result = run([f], [controller])
    assert result.routes == ()


def test_a_finished_or_claimed_route_needs_no_decision():
    ws = fleet()
    f = film(
        restore="succeeded",
        asr="running",
        claimed_by=ws[0].worker_id,
        claimed_stage="asr",
        claimed_at=NOW,
    )
    result = run([f], ws, routes=[open_route(f, ws[0])])
    assert result == s.SchedulePass((), (), {}, {})


def test_the_pass_is_deterministic():
    ws = fleet()
    films = [film(i, size=UHD if i % 3 == 0 else FHD) for i in range(9)]
    first, second = run(films, ws), run(list(reversed(films)), list(reversed(ws)))
    assert first == second


# ── CX7: a film whose intake source is no longer admissible ──────────────
def test_a_film_with_a_changed_source_is_not_routed_and_says_why():
    ws = fleet()
    changed, fine = film(source_admissible=False), film(1)
    result = run([changed, fine], ws)
    assert routed(result) == {fine.film_id: routed(result)[fine.film_id]}
    assert result.waiting[changed.film_id] == s.SOURCE_CHANGED


def test_films_with_a_changed_source_do_not_take_route_slots():
    w = fleet()[0]
    bad = [film(i, source_admissible=False) for i in range(3)]
    fine = [film(10 + i) for i in range(2)]
    result = run([*bad, *fine], [w])
    assert routed(result).keys() == {f.film_id for f in fine}
    assert {f.film_id for f in bad} <= set(result.waiting)


def test_an_unclaimed_route_of_a_changed_source_is_withdrawn():
    ws = fleet()
    remote = film(source_admissible=False)
    local = film(1, source_admissible=False)
    routes = [open_route(remote, ws[0]), open_route(local, ws[1], kind="local")]
    routes[1] = routes[1].model_copy(update={"worker_id": CONTROLLER})
    result = run([remote, local], ws, routes=routes)
    assert {(x.route_id, x.reason) for x in result.withdrawals} == {
        (r.route_id, s.SOURCE_CHANGED) for r in routes
    }
    assert result.routes == ()
    assert {remote.film_id, local.film_id} <= set(result.waiting)


def test_a_claimed_route_survives_a_changed_source():
    ws = fleet()
    f = film(
        restore="running",
        source_admissible=False,
        claimed_by=ws[0].worker_id,
        claimed_stage="restore",
        claimed_at=NOW,
    )
    result = run([f], ws, routes=[open_route(f, ws[0])])
    assert result == s.SchedulePass((), (), {}, {})


def test_a_film_is_routed_again_once_its_source_is_admissible():
    ws = fleet()
    f = film(source_admissible=False)
    route = open_route(f, ws[0])
    assert run([f], ws, routes=[route]).withdrawals
    back = f.model_copy(update={"source_admissible": True})
    assert run([back], ws, routes=[route]).withdrawals == ()
    result = run([back], ws)
    assert back.film_id in routed(result) and back.film_id not in result.waiting
