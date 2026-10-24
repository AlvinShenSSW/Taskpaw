"""P3 E1-E3 (#304): a film whose INBOX source vanished is 片源丢失, never
dispatched, and can be deleted by the operator without touching INBOX, the
output directory or NAS. Fakes and tmp dirs only."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from taskpaw_v3.agent.server.app import create_control_app
from taskpaw_v3.core.config import AgentConfig
from taskpaw_v3.tests.test_workflow_films import STAGES, input_manifest
from taskpaw_v3.tests.test_workflow_local_dispatch import (
    dispatcher,
    plan_film,
    runtime,
    stage,
)
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.workflow_route_helpers import StepClock
from taskpaw_v3.workflow import board
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow import operator_actions as ops
from taskpaw_v3.workflow.source_watch import INTERVAL_SECONDS, SourceWatch
from taskpaw_v3.workflow.timing import WorkflowLog

TOKEN = "control-token"
FILES = ("movie.mkv", "movie.ja.srt")  # the fixture manifest's INBOX members


def files(stem: str) -> tuple[str, str]:
    return (f"{stem}.mkv", f"{stem}.ja.srt")


def make_film(rt, stem: str, *, plan: bool = True) -> str:
    """`add_film` with the manifest's INBOX members renamed to `stem`."""
    _, auth = rt.grant_intake_adapter()
    film_id = identity()
    rt.store.create_film(
        auth,
        rt.meta(),
        m.FilmCreate(
            film_id=film_id,
            intake_session_id=identity(),
            display_name=stem,
            policy=m.FilmPolicy(
                policy_revision=1,
                required_stages=STAGES,
                subtitle_requirement="chinese",
                required_image_roles=("poster",),
                folder_cover_required=False,
            ),
        ),
    )

    def rename(data):
        for item in data["items"]:
            item["relative_path"] = item["relative_path"].replace("movie", stem)

    rt.store.put_input_manifest(
        auth,
        rt.meta(),
        m.InputManifestPut(
            film_id=film_id,
            expected_version=1,
            manifest=input_manifest(film_id, change=rename),
        ),
    )
    if plan:
        plan_film(rt, film_id)
    return film_id


class Ticker:
    """A fake monotonic clock."""

    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Sink:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def record(self, task, kind, *, task_type, film=None, severity="info", data=None):
        self.rows.append(
            {
                "task": task,
                "kind": kind,
                "film": film,
                "severity": severity,
                **(data or {}),
            }
        )


@dataclass
class World:
    rt: object
    inbox: Path
    clock: StepClock
    tick: Ticker = field(default_factory=Ticker)
    changes: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.watch = SourceWatch(
            self.rt,
            self.inbox,
            monotonic=self.tick,
            on_change=lambda: self.changes.append(1),
        )

    def film(self, stem: str = "movie", **kw) -> str:
        """A planned film whose INBOX members are `<stem>.mkv`/`<stem>.ja.srt`."""
        self.restore(stem)
        return make_film(self.rt, stem, **kw)

    def lose(self, stem: str = "movie") -> None:
        for name in files(stem):
            (self.inbox / name).unlink(missing_ok=True)

    def restore(self, stem: str = "movie") -> None:
        for name in files(stem):
            (self.inbox / name).write_bytes(b"x")


@pytest.fixture
def world(tmp_path):
    clock = StepClock()
    inbox = tmp_path / "INBOX"
    inbox.mkdir()
    rt = runtime(tmp_path / "wf", clock=clock.now)
    try:
        yield World(rt, inbox, clock)
    finally:
        rt.close()


# ── E1: the throttled existence check ───────────────────────────────────
def test_a_present_source_is_not_lost(world):
    film_id = world.film()
    assert world.watch.check(film_id) is None
    assert world.watch.ids() == frozenset()


def test_a_missing_member_makes_the_film_lost_with_paths_and_found_at(world):
    film_id = world.film()
    world.lose()
    lost = world.watch.check(film_id)
    assert lost is not None
    assert sorted(Path(p).name for p in lost["paths"]) == sorted(FILES)
    assert all(Path(p).parent == world.inbox for p in lost["paths"])
    assert lost["found_at"] <= world.rt.clock.now()
    assert world.watch.ids() == frozenset({film_id})
    assert world.changes == [1]  # the scheduler is told once


def test_one_missing_member_is_enough_and_only_it_is_listed(world):
    film_id = world.film()
    (world.inbox / "movie.ja.srt").unlink()
    lost = world.watch.check(film_id)
    assert [Path(p).name for p in lost["paths"]] == ["movie.ja.srt"]


def test_the_check_runs_at_most_once_per_film_per_60_seconds(world, monkeypatch):
    film_id = world.film()
    calls = []
    original = Path.exists

    def counting(self, *a, **kw):
        calls.append(self.name)
        return original(self, *a, **kw)

    monkeypatch.setattr(Path, "exists", counting)
    world.watch.check(film_id)
    first = len(calls)
    assert first >= 1
    world.lose()
    world.tick.advance(INTERVAL_SECONDS - 1)
    assert world.watch.check(film_id) is None  # throttled: still the old answer
    assert len(calls) == first
    world.tick.advance(1)
    assert world.watch.check(film_id) is not None
    assert len(calls) > first


def test_a_lost_film_keeps_its_found_at_while_it_stays_lost(world):
    film_id = world.film()
    world.lose()
    first = world.watch.check(film_id)
    world.clock.advance(timedelta(minutes=5))
    world.tick.advance(INTERVAL_SECONDS)
    again = world.watch.check(film_id)
    assert again["found_at"] == first["found_at"]
    assert world.changes == [1]


def test_recheck_runs_at_once_and_a_returned_source_clears_the_state(world):
    film_id = world.film()
    world.lose()
    assert world.watch.check(film_id) is not None
    world.restore()
    assert world.watch.check(film_id) is not None  # throttled
    assert world.watch.check(film_id, force=True) is None  # 重新检查
    assert world.watch.ids() == frozenset()
    assert world.changes == [1, 1]


def test_a_film_that_started_a_stage_is_never_flagged(world):
    film_id = world.film()
    assert dispatcher(world.rt).run_once() is True  # restore ran
    assert stage(world.rt, film_id, "restore").status == "succeeded"
    world.lose()
    assert world.watch.check(film_id, force=True) is None


def test_an_unreachable_inbox_root_is_unknown_not_lost(world):
    film_id = world.film()
    world.lose()
    world.inbox.rmdir()
    assert world.watch.check(film_id, force=True) is None


def test_a_film_in_a_package_folder_is_checked_under_that_folder(world, monkeypatch):
    film_id = world.film()
    world.lose()
    (world.inbox / "ABF-394").mkdir()
    (world.inbox / "ABF-394" / "movie.mkv").write_bytes(b"x")
    (world.inbox / "ABF-394" / "movie.ja.srt").write_bytes(b"x")
    real = board.board_film(world.rt, film_id)
    found = real.model_copy(update={"package_path": "ABF-394"})
    monkeypatch.setattr(board, "board_film", lambda rt, fid: found)
    assert world.watch.check(film_id, force=True) is None
    (world.inbox / "ABF-394" / "movie.mkv").unlink()
    lost = world.watch.check(film_id, force=True)
    assert [Path(p) for p in lost["paths"]] == [world.inbox / "ABF-394" / "movie.mkv"]


# ── no dispatch while lost ──────────────────────────────────────────────
def test_a_lost_film_is_not_dispatched_and_runs_after_the_files_return(world):
    film_id = world.film()
    world.lose()
    d = dispatcher(world.rt, sources=world.watch)
    assert d.run_once() is False
    assert stage(world.rt, film_id, "restore").status == "pending"
    assert world.watch.ids() == frozenset({film_id})
    world.restore()
    world.watch.check(film_id, force=True)  # 重新检查
    assert d.run_once() is True
    assert stage(world.rt, film_id, "restore").status == "succeeded"


def test_other_films_are_dispatched_around_a_lost_one(world):
    lost = world.film("gone")
    other = world.film("here")
    world.lose("gone")
    d = dispatcher(world.rt, sources=world.watch)
    assert d.run_once() is True
    assert stage(world.rt, other, "restore").status == "succeeded"
    assert stage(world.rt, lost, "restore").status == "pending"
    assert world.watch.ids() == frozenset({lost})


# ── the scheduler holds a lost film ─────────────────────────────────────
def test_routing_snapshot_marks_a_held_film_inadmissible(world):
    film_id = world.film()
    snap = world.rt.store.routing_snapshot(world.rt.operator)
    assert all(f.source_admissible for f in snap.films)
    held = world.rt.store.routing_snapshot(world.rt.operator, hold=frozenset({film_id}))
    flags = {f.film_id: f.source_admissible for f in held.films}
    assert flags == {film_id: False}


# ── E3: the guarded delete ──────────────────────────────────────────────
def remove(world, film_id, sink=None):
    return ops.remove_film(
        world.rt,
        film_id=film_id,
        sources=world.watch,
        log=WorkflowLog(sink) if sink is not None else None,
    )


def refused(world, film_id) -> str:
    with pytest.raises(ops.ActionRefused) as info:
        remove(world, film_id)
    return info.value.code


def listed(world, film_id) -> bool:
    return any(
        f.film_id == film_id for f in world.rt.store.film_board(world.rt.operator)
    )


def test_a_removed_film_leaves_the_queue_the_board_and_the_scheduler(world):
    film_id = world.film()
    unplanned = world.film("fresh", plan=False)
    rt = world.rt
    assert any(
        j.film_id == film_id
        for j in rt.store.list_jobs(rt.operator, "pending", ("restore",))
    )
    assert remove(world, film_id)["ok"] is True
    assert not listed(world, film_id)
    assert listed(world, unplanned)
    assert all(
        j.film_id != film_id
        for j in rt.store.list_jobs(rt.operator, "pending", ("restore",))
    )
    assert [u.film_id for u in rt.store.list_unplanned(rt.operator)] == [unplanned]
    assert all(
        f.film_id != film_id for f in rt.store.routing_snapshot(rt.operator).films
    )
    assert rt.store.get_film(rt.operator, film_id).film.removed_at is not None
    assert rt.store.film_board(rt.operator, film_id=film_id) == ()
    assert dispatcher(rt).run_once() is False
    assert stage(rt, film_id, "restore").status == "pending"


def test_a_removed_film_is_refused_a_second_time(world):
    film_id = world.film()
    remove(world, film_id)
    with pytest.raises(m.WorkflowError):
        world.rt.store.remove_film(
            world.rt.operator,
            world.rt.meta(),
            m.FilmRemove(
                film_id=film_id,
                expected_version=world.rt.store.get_film(
                    world.rt.operator, film_id
                ).film.version,
                reason="again",
            ),
        )


def test_delete_never_touches_inbox_and_reclaims_only_the_films_private_dirs(world):
    film_id = world.film()
    other = world.film("other")
    work = world.rt.work_dir
    mine = [
        work / parent / f"{film_id}-r1" for parent in ("return", "scrape", "lookup")
    ]
    kept = [work / parent / f"{other}-r1" for parent in ("return", "scrape", "lookup")]
    kept.append(work / "unrelated")
    for path in (*mine, *kept):
        path.mkdir(parents=True)
        (path / "data.bin").write_bytes(b"x")
    world.lose()  # the source is gone: the very case the button exists for
    world.watch.check(film_id, force=True)
    assert world.watch.ids() == frozenset({film_id})
    assert remove(world, film_id)["ok"] is True
    assert not any(path.exists() for path in mine)
    assert all(path.exists() for path in kept)
    assert (world.inbox / "other.mkv").exists()
    assert world.watch.ids() == frozenset()  # nothing left to show


def test_the_shared_intake_snapshot_goes_with_the_last_film_of_its_package(
    world, monkeypatch
):
    first, second = world.film("one"), world.film("two")
    snapshot = world.rt.work_dir / "intake" / "op1"
    snapshot.mkdir(parents=True)
    (snapshot / "copy.bin").write_bytes(b"x")
    store, real = world.rt.store, world.rt.store.film_workflow
    monkeypatch.setattr(
        store,
        "film_workflow",
        lambda auth, fid: real(auth, fid).model_copy(
            update={"package_id": "p1", "snapshot_work_path": "intake/op1"}
        ),
    )
    siblings = [object()]
    monkeypatch.setattr(store, "package_films", lambda auth, pid: tuple(siblings))
    remove(world, first)
    assert snapshot.exists()  # a sibling still uses it
    siblings.clear()
    remove(world, second)
    assert not snapshot.exists()
    assert world.rt.work_dir.exists()


def test_delete_leaves_inbox_files_of_a_present_source(world):
    film_id = world.film()
    remove(world, film_id)
    assert sorted(p.name for p in world.inbox.iterdir()) == sorted(FILES)


def test_delete_writes_a_task_log_row(world):
    film_id = world.film()
    sink = Sink()
    remove(world, film_id, sink)
    assert [(r["kind"], r["film"]) for r in sink.rows] == [
        ("workflow.film.removed", "movie")
    ]
    assert sink.rows[0]["text"] == "已删除任务 movie：只删除任务和工作目录里的临时文件"


def test_delete_is_refused_while_a_stage_is_running_or_claimed(world):
    from taskpaw_v3.tests.workflow_route_helpers import (
        claim_remote,
        enrol_remote,
        route,
    )

    remote = enrol_remote(world.rt, "5060 Ti-1")
    film_id = world.film()
    route(world.rt, film_id, remote.worker_id)
    claim_remote(world.rt, remote, film_id, "restore")
    assert stage(world.rt, film_id, "restore").status == "running"
    assert refused(world, film_id) == "film_running"
    assert listed(world, film_id)


def test_delete_is_refused_while_an_open_route_exists(world):
    from taskpaw_v3.tests.workflow_route_helpers import enrol_remote, route

    remote = enrol_remote(world.rt, "5060 Ti-1")
    film_id = world.film()
    route(world.rt, film_id, remote.worker_id)
    assert refused(world, film_id) == "film_routed"
    assert listed(world, film_id)


def test_the_store_itself_refuses_a_film_with_work_in_flight(world):
    from taskpaw_v3.tests.workflow_route_helpers import (
        claim_remote,
        enrol_remote,
        route,
    )

    remote = enrol_remote(world.rt, "5060 Ti-1")
    film_id = world.film()
    route(world.rt, film_id, remote.worker_id)
    claim_remote(world.rt, remote, film_id, "restore")
    with pytest.raises(m.WorkflowError) as info:
        world.rt.store.remove_film(
            world.rt.operator,
            world.rt.meta(),
            m.FilmRemove(
                film_id=film_id,
                expected_version=world.rt.store.get_film(
                    world.rt.operator, film_id
                ).film.version,
                reason="race",
            ),
        )
    assert info.value.code == "precondition_failed"


def test_an_unknown_film_is_not_found(world):
    with pytest.raises(m.WorkflowError) as info:
        remove(world, identity())
    assert info.value.code == "not_found"


def board_film_with(world, film_id, **stages):
    film = board.board_film(world.rt, film_id)
    return film.model_copy(
        update={
            "stages": tuple(
                s.model_copy(update={"status": stages.get(s.stage, s.status)})
                for s in film.stages
            )
        }
    )


@pytest.mark.parametrize(
    "stages",
    [
        {"archive": "succeeded"},
        {"archive": "failed"},
        {"cleanup_input": "succeeded"},
        {"cleanup_workcopy": "unknown"},
    ],
)
def test_an_archived_or_archiving_film_cannot_be_deleted(world, stages):
    film_id = world.film()
    film = board_film_with(world, film_id, **stages)
    code, _ = ops.removal_refusal(world.rt, film, claimed=False, routed=False)
    assert code in ("film_archived", "film_running")


def test_a_confirmed_film_waiting_to_archive_cannot_be_deleted(world):
    film_id = world.film()
    film = board.board_film(world.rt, film_id).model_copy(
        update={"approval_active": True}
    )
    assert (
        ops.removal_refusal(world.rt, film, claimed=False, routed=False)[0]
        == "film_archived"
    )
    open_archive = film.model_copy(
        update={"approval_active": False, "archive_open": True}
    )
    assert (
        ops.removal_refusal(world.rt, open_archive, claimed=False, routed=False)[0]
        == "film_archived"
    )


def test_a_waiting_film_can_be_deleted(world):
    film_id = world.film()
    film = board.board_film(world.rt, film_id)
    assert ops.removal_refusal(world.rt, film, claimed=False, routed=False) is None


# ── the board and the control routes ────────────────────────────────────
class Waker:
    def __init__(self) -> None:
        self.wakes = 0

    def wake(self) -> None:
        self.wakes += 1


class Api:
    """The loopback control API over a host double that owns `sources`."""

    def __init__(self, world: World, tmp_path: Path, *, sources: bool = True) -> None:
        self.world = world
        self.sink = Sink()
        self.dispatcher, self.scheduler = Waker(), Waker()
        self.host = SimpleNamespace(
            state="running",
            reason="",
            runtime=world.rt,
            fleet=None,
            dispatcher=self.dispatcher,
            scheduler=self.scheduler,
            finalizer=None,
            sources=world.watch if sources else None,
            workflow_log=WorkflowLog(self.sink),
            status=lambda: {"state": "running", "dispatch": None},
        )
        config = AgentConfig(
            server_id="s",
            machine="m",
            host_metrics=False,
            workflow={"enabled": True, "inbox_root": str(world.inbox)},
        )
        app = create_control_app(
            config,
            control_token=TOKEN,
            control_active=lambda: True,
            workflow_provider=lambda: self.host,
        )
        self.client = TestClient(app, base_url="http://127.0.0.1")

    def get(self, path):
        return self.client.get(path, headers={"Authorization": f"Bearer {TOKEN}"})

    def post(self, path, body=None):
        return self.client.post(
            path, json=body or {}, headers={"Authorization": f"Bearer {TOKEN}"}
        )

    def row(self, film_id) -> dict:
        rows = self.get("/control/workflow/films").json()["films"]
        return next(r for r in rows if r["film_id"] == film_id)


@pytest.fixture
def api(world, tmp_path):
    return Api(world, tmp_path)


def test_a_lost_film_is_an_attention_row_with_the_alert_facts(world, api):
    film_id = world.film()
    other = world.film("other")
    world.lose()
    world.watch.check(film_id, force=True)
    row = api.row(film_id)
    assert row["group"] == "attention"
    assert row["attention_kind"] == "source_missing"
    assert row["note"] == "INBOX 里的文件已不存在 · 不再派发"
    assert sorted(Path(p).name for p in row["source_missing"]["paths"]) == sorted(FILES)
    assert row["source_missing"]["found_at"].endswith(("Z", "+00:00"))
    assert row["can_remove"] is True
    waiting = next(s for s in row["steps"] if s["state"] == "waiting")
    assert waiting["note"] == row["note"]
    healthy = api.row(other)
    assert healthy["source_missing"] is None
    assert healthy["attention_kind"] is None
    assert healthy["can_remove"] is True
    assert api.get("/control/workflow/films").json()["counts"]["attention"] == 1


def test_the_detail_carries_the_same_facts(world, api):
    film_id = world.film()
    world.lose()
    world.watch.check(film_id, force=True)
    detail = api.get(f"/control/workflow/films/{film_id}").json()
    assert detail["attention_kind"] == "source_missing"
    assert (
        detail["source_missing"]["paths"] == api.row(film_id)["source_missing"]["paths"]
    )
    assert detail["can_remove"] is True


def test_a_running_film_cannot_be_removed_on_the_board(world, api):
    from taskpaw_v3.tests.workflow_route_helpers import (
        claim_remote,
        enrol_remote,
        route,
    )

    remote = enrol_remote(world.rt, "5060 Ti-1")
    film_id = world.film()
    route(world.rt, film_id, remote.worker_id)
    assert api.row(film_id)["can_remove"] is False  # routed
    claim_remote(world.rt, remote, film_id, "restore")
    assert api.row(film_id)["can_remove"] is False  # claimed and running


def test_recheck_source_runs_at_once_and_lets_a_returned_film_go_on(world, api):
    film_id = world.film()
    world.lose()
    world.watch.check(film_id, force=True)
    world.restore()
    assert api.row(film_id)["group"] == "attention"  # throttled: still lost
    response = api.post(f"/control/workflow/films/{film_id}/recheck-source")
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "unreachable": False, "source_missing": None}
    assert api.row(film_id)["group"] != "attention"
    assert api.dispatcher.wakes >= 1


def test_recheck_source_of_a_still_lost_film_reports_the_paths(world, api):
    film_id = world.film()
    world.lose()
    body = api.post(f"/control/workflow/films/{film_id}/recheck-source").json()
    assert body["ok"] is True
    assert sorted(Path(p).name for p in body["source_missing"]["paths"]) == sorted(
        FILES
    )


def test_recheck_source_of_an_unknown_film_is_404(api):
    assert (
        api.post(f"/control/workflow/films/{identity()}/recheck-source").status_code
        == 404
    )


def test_recheck_source_needs_the_watch(world, tmp_path):
    api = Api(world, tmp_path, sources=False)
    film_id = world.film()
    assert (
        api.post(f"/control/workflow/films/{film_id}/recheck-source").status_code == 503
    )


def test_remove_deletes_the_task_logs_it_and_leaves_inbox(world, api):
    film_id = world.film()
    world.lose()
    world.watch.check(film_id, force=True)
    response = api.post(f"/control/workflow/films/{film_id}/remove")
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    rows = api.get("/control/workflow/films").json()["films"]
    assert all(r["film_id"] != film_id for r in rows)
    assert [r["kind"] for r in api.sink.rows] == ["workflow.film.removed"]
    assert api.get(f"/control/workflow/films/{film_id}").status_code == 404
    assert api.scheduler.wakes >= 1


def test_remove_is_a_coded_409_while_the_film_is_busy(world, api):
    from taskpaw_v3.tests.workflow_route_helpers import (
        claim_remote,
        enrol_remote,
        route,
    )

    remote = enrol_remote(world.rt, "5060 Ti-1")
    film_id = world.film()
    route(world.rt, film_id, remote.worker_id)
    claim_remote(world.rt, remote, film_id, "restore")
    response = api.post(f"/control/workflow/films/{film_id}/remove")
    assert response.status_code == 409
    assert response.json()["detail"] == "有阶段正在运行，不能删除"
    assert api.row(film_id)["film_id"] == film_id
    assert api.sink.rows == []


def test_remove_of_an_unknown_film_is_404(api):
    assert api.post(f"/control/workflow/films/{identity()}/remove").status_code == 404
