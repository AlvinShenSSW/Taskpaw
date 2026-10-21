"""U1 L: the MDCX metadata preview (lookup_one/1) with a fake tool."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from taskpaw_v3.tests.test_workflow_local_dispatch import add_film, runtime
from taskpaw_v3.tests.test_workflow_plans import new_revision
from taskpaw_v3.workflow import lookup as lk
from taskpaw_v3.workflow import models as m

FAKE = Path(__file__).with_name("workflow_fake_lookup.py")


@pytest.fixture
def rt(tmp_path):
    value = runtime(tmp_path / "wf")
    try:
        yield value
    finally:
        value.close()


class Profile:
    """An operator's Miko profile folder (config.json + userdata)."""

    def __init__(self, root: Path, **config):
        self.dir = root / "mdcx-profile"
        (self.dir / "userdata").mkdir(parents=True)
        (self.dir / "userdata" / "mapping_actor.xml").write_text("<a/>", "utf-8")
        self.log = root / "lookup-log.jsonl"
        self.path = self.dir / "config.json"
        self.set(**config)

    def set(self, **config):
        self.path.write_text(
            json.dumps({"fake_log": str(self.log), **config}), encoding="utf-8"
        )

    def runs(self) -> list[dict]:
        if not self.log.is_file():
            return []
        return [json.loads(line) for line in self.log.read_text("utf-8").splitlines()]


@pytest.fixture
def profile(tmp_path):
    return Profile(tmp_path)


def make(rt, profile, **kw) -> lk.MetadataLookup:
    root = profile.dir.parent / "mdcx-root"
    root.mkdir(exist_ok=True)
    kw.setdefault("launch_prefix", [sys.executable, str(FAKE)])
    return lk.MetadataLookup(
        rt, python=sys.executable, root=str(root), profile=str(profile.path), **kw
    )


def test_a_new_film_is_looked_up_once_per_revision(rt, profile):
    film = add_film(rt, plan=False)
    lookup = make(rt, profile)
    assert lookup.state(film, 1)["status"] == "queued"
    assert lookup.run_once() is True
    result = lookup.result(film, 1)
    assert result is not None and result.ok
    assert (result.number, result.title, result.actors) == (
        "MOVIE",
        "MOVIE 的标题",
        ("演员甲", "演员乙"),
    )
    assert result.poster is not None and result.poster.read_bytes().startswith(
        b"\xff\xd8"
    )
    assert result.file_name == "movie.mkv"
    assert lookup.run_once() is False  # cached: never twice for one revision
    assert len(profile.runs()) == 1
    view = lookup.state(film, 1)
    assert view["status"] == "succeeded" and view["has_poster"] is True
    # a new input revision is a new lookup
    new_revision(rt, film)
    assert lookup.run_once() is True
    assert lookup.result(film, 2) is not None and len(profile.runs()) == 2


def test_the_run_uses_a_discarded_copy_of_the_profile(rt, profile):
    add_film(rt, plan=False)
    lookup = make(rt, profile)
    lookup.run_once()
    (run,) = profile.runs()
    assert run["config"] == ["config.json", "userdata/mapping_actor.xml"]  # no mark
    config = Path(run["config_dir"])
    assert config.is_relative_to(rt.work_dir / "lookup")
    assert not config.exists()  # the per-run directory is discarded
    assert Path(run["cwd"]) == profile.dir.parent / "mdcx-root"
    # the operator's profile is never written
    assert sorted(p.name for p in profile.dir.iterdir()) == ["config.json", "userdata"]


def test_the_cache_survives_a_new_worker(rt, profile):
    film = add_film(rt, plan=False)
    make(rt, profile).run_once()
    again = make(rt, profile)
    assert again.run_once() is False
    assert again.result(film, 1).title == "MOVIE 的标题"


def test_pending_films_go_first_then_planned_ones_without_a_final(rt, profile):
    planned = add_film(rt)  # confirmed before its lookup ran, and older
    pending = add_film(rt, plan=False)
    lookup = make(rt, profile)
    assert lookup.run_once() is True
    assert lookup.result(pending, 1) is not None and lookup.result(planned, 1) is None
    assert lookup.run_once() is True
    assert lookup.result(planned, 1) is not None
    assert lookup.run_once() is False


@pytest.mark.parametrize(
    ("mode", "error_class", "message"),
    [
        ("fail", "needs_attention", "未查到资料"),
        ("no_result", "transient", "资料查询没有返回结果（退出码 3）"),
        ("bad_schema", "needs_attention", "资料查询结果版本不符"),
    ],
)
def test_failures_are_cached_with_their_class(rt, profile, mode, error_class, message):
    film = add_film(rt, plan=False)
    profile.set(fake_lookup=mode)
    lookup = make(rt, profile)
    assert lookup.run_once() is True
    result = lookup.result(film, 1)
    assert (result.status, result.error_class, result.message) == (
        "failed",
        error_class,
        message,
    )
    assert result.can_requery and lookup.run_once() is False
    profile.set()
    lookup.requery(film, 1)  # 重新查询
    assert lookup.run_once() is True
    assert lookup.result(film, 1).ok


def test_a_name_without_a_number_cannot_be_requeried(rt, profile):
    film = add_film(rt, plan=False)
    profile.set(fake_lookup="no_number")
    lookup = make(rt, profile)
    lookup.run_once()
    view = lookup.state(film, 1)
    assert (view["status"], view["message"], view["can_requery"]) == (
        "failed",
        "文件名中没有番号",
        False,
    )
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        lookup.requery(film, 1)


def run_dirs(rt) -> list[Path]:
    return [p for p in (rt.work_dir / "lookup").rglob("run-*") if p.is_dir()]


def test_a_hanging_lookup_is_killed_with_its_tree_and_leaves_no_run_dir(
    rt, profile, tmp_path
):
    """M02 IR: a stalled browser close can outlive lookup_one's own deadline;
    the caller's hard timeout kills the whole tree (transient failure)."""
    film = add_film(rt, plan=False)
    beat = tmp_path / "heartbeat.txt"
    profile.set(fake_lookup="hang", fake_heartbeat=str(beat))
    lookup = make(rt, profile, timeout=5.0)
    lookup.run_once()
    result = lookup.result(film, 1)
    assert (result.status, result.error_class, result.message) == (
        "failed",
        "transient",
        lk.TIMED_OUT,
    )
    assert run_dirs(rt) == []
    assert beat.is_file()  # the grandchild ran ...
    assert heartbeat_stops(beat)  # ... and died with its parent


def heartbeat_stops(beat: Path, *, window: float = 0.6, deadline: float = 30.0) -> bool:
    """True once the 0.1 s heartbeat holds still for a whole `window`.

    Polled with a generous deadline: a slow runner may take a while to reap the
    tree, but a live grandchild never holds still for six of its beats."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        before = beat.read_text()
        time.sleep(window)
        if beat.read_text() == before:
            return True
    return False


def test_stopping_the_app_mid_lookup_leaves_it_queued_not_failed(rt, profile):
    """IR01: a shutdown during a lookup is not a 资料查询超时; the next start
    looks the film up again."""
    film = add_film(rt, plan=False)
    profile.set(fake_lookup="hang")
    lookup = make(rt, profile, poll=0.05)
    lookup.start()
    deadline = time.monotonic() + 30
    while lookup.state(film, 1)["status"] != "running" and time.monotonic() < deadline:
        time.sleep(0.05)
    assert lookup.state(film, 1)["status"] == "running"
    assert lookup.stop(30)
    assert run_dirs(rt) == []
    again = make(rt, profile)  # the next app start
    assert again.result(film, 1) is None
    assert again.state(film, 1)["status"] == "queued"
    profile.set()
    assert again.run_once() is True
    assert again.result(film, 1).ok


def test_forced_exit_temp_files_go_with_the_run_dir(rt, profile):
    film = add_film(rt, plan=False)
    profile.set(fake_lookup="temp_files")
    lookup = make(rt, profile)
    lookup.run_once()
    assert lookup.result(film, 1).ok
    assert run_dirs(rt) == []
    cached = sorted(p.name for p in (rt.work_dir / "lookup" / f"{film}-r1").iterdir())
    assert cached == ["lookup.json", "poster.jpg"]


@pytest.mark.parametrize("mode", ["ok", "fail", "no_result", "bad_schema"])
def test_every_outcome_removes_the_run_dir(rt, profile, mode):
    add_film(rt, plan=False)
    profile.set(fake_lookup=mode)
    make(rt, profile).run_once()
    assert run_dirs(rt) == []


def test_a_poster_free_result_has_no_cover(rt, profile):
    film = add_film(rt, plan=False)
    profile.set(fake_lookup="no_poster")
    lookup = make(rt, profile)
    lookup.run_once()
    assert lookup.result(film, 1).poster is None


def test_a_missing_profile_is_a_failure_not_a_crash(rt, profile):
    film = add_film(rt, plan=False)
    profile.path.unlink()
    lookup = make(rt, profile)
    lookup.run_once()
    result = lookup.result(film, 1)
    assert result.status == "failed" and result.message.startswith("找不到 Miko 配置")
    assert profile.runs() == []


def test_the_worker_thread_runs_and_stops(rt, profile):
    film = add_film(rt, plan=False)
    lookup = make(rt, profile, poll=0.05)
    lookup.start()
    try:
        deadline = time.monotonic() + 20
        while lookup.result(film, 1) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert lookup.result(film, 1) is not None
    finally:
        assert lookup.stop(10)


def test_the_shipped_lookup_needs_the_whole_miko_environment(rt, profile):
    from taskpaw_v3.core.config import WorkflowSettings
    from taskpaw_v3.workflow.pipeline import default_lookup

    assert default_lookup(WorkflowSettings()) is None
    assert default_lookup(WorkflowSettings(mdcx_python="p", mdcx_root="r")) is None
    build = default_lookup(
        WorkflowSettings(
            mdcx_python=sys.executable, mdcx_root="r", mdcx_profile=str(profile.path)
        )
    )
    assert build is not None
    lookup = build(rt)
    assert (lookup.python, lookup.profile, lookup.timeout) == (
        sys.executable,
        profile.path,
        lk.TIMEOUT_SECONDS,
    )


def test_the_hard_timeout_outlasts_lookup_ones_own_budget():
    """U1b: MDCX304 8a3f519 gives lookup_one 240 s for metadata and 30 s for
    the poster; Taskpaw's wall-clock kill comes only after that budget, so a
    slow site ends in lookup_one's own 资料查询超时, not in a killed process."""
    assert lk.MDCX_BUDGET_SECONDS == 270.0
    assert lk.TIMEOUT_SECONDS == 300.0
    assert lk.TIMEOUT_SECONDS - lk.MDCX_BUDGET_SECONDS >= 30.0
