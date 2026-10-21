"""F02-P1 end to end: 确认入库 → archive → receipt → exact cleanup → reclaim (C1–C10).

A real film runs through the real dispatcher up to Miko (fake CLI); Filmpaw is an in-process fake with
the same wire contract that really copies into a temporary "NAS". The INBOX originals are real files
whose identities the input manifest records, as intake would.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

import pytest

from taskpaw_v3.tests import test_workflow_return_step as ret
from taskpaw_v3.tests import test_workflow_scrape_step as scr
from taskpaw_v3.tests.test_workflow_local_dispatch import Crash
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.workflow_exec_helpers import video
from taskpaw_v3.tests.workflow_fake_filmpaw import FakeFilmpaw
from taskpaw_v3.workflow import file_identity
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.filmpaw_client import FilmpawClient
from taskpaw_v3.workflow.finalize import ConfirmError, Finalizer

FILM_DIR = "ABC-123"


@pytest.fixture(autouse=True)
def probeable_video(monkeypatch):
    monkeypatch.setattr(ret, "VIDEO", video(1920, 1080, 120.0))


class World:
    def __init__(
        self,
        tmp_path: Path,
        extras: dict[str, bytes] | None = None,
        *,
        plan: bool = True,
    ):
        self.tmp = tmp_path
        self.inbox = tmp_path / "inbox"
        self.nas = tmp_path / "nas" / "Actor"
        self.filmpaw_dir = tmp_path / "Filmpaw"
        self.inbox.mkdir()
        self.nas.mkdir(parents=True)
        self.rt = self.runtime()
        self.film = self._film(extras, plan=plan)
        self.fake = FakeFilmpaw(self.filmpaw_dir, {"actor": self.nas})

    def runtime(self):
        """A (re)started runtime whose film came from the INBOX package `pkg`, as intake records it."""
        rt = ret.make_runtime(self.tmp / "wf")
        real = rt.store.film_workflow

        def film_workflow(auth, film_id):
            return real(auth, film_id).model_copy(update={"package_path": "pkg"})

        rt.store.film_workflow = film_workflow  # type: ignore[method-assign]
        return rt

    def _film(
        self, extras: dict[str, bytes] | None = None, *, plan: bool = True
    ) -> str:
        """add_real_film, plus real INBOX originals recorded with their identities."""
        rt, inbox = self.rt, self.inbox
        real_put = rt.store.put_input_manifest

        def put(auth, meta, command):
            items = []
            for item in command.manifest.items:
                raw = (rt.work_dir / ret.WORKCOPY / item.relative_path).read_bytes()
                path = inbox / "pkg" / item.relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                items.append(
                    item.model_copy(
                        update={
                            "source_root_id": rt.identity.root_id,
                            "native_identity": file_identity.native(
                                path, rt.identity.root_id
                            ),
                        }
                    )
                )
            changed = command.manifest.model_copy(update={"items": tuple(items)})
            manifest = m.InputManifest.model_validate(
                {
                    **changed.model_dump(mode="json"),
                    "manifest_hash": m.manifest_digest(changed),
                }
            )
            return real_put(
                auth, meta, command.model_copy(update={"manifest": manifest})
            )

        rt.store.put_input_manifest = put  # type: ignore[method-assign]
        try:
            return ret.add_real_film(rt, extras=extras, plan=plan)
        finally:
            del rt.store.put_input_manifest

    def scrape(self) -> None:
        d = scr.dispatcher(self.rt, scr.scrape_step(self.tmp))
        for _ in range(6):
            d.run_once()
        assert ret.status(self.rt, self.film, "scrape") == "succeeded", (
            d.status().reason
        )

    def finalizer(self, rt=None) -> Finalizer:
        return Finalizer(rt or self.rt, self.inbox, FilmpawClient(self.filmpaw_dir))

    def status(self, stage: str) -> str:
        return ret.status(self.rt, self.film, stage)

    def ticks(self, fin: Finalizer, n: int = 4) -> None:
        for _ in range(n):
            fin.run_once()


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    try:
        yield w
    finally:
        w.rt.close()


def _files(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def _scrape_out(world: World) -> Path:
    return next((world.rt.work_dir / "scrape").rglob(f"{FILM_DIR}.nfo")).parent


def test_one_click_archives_once_and_cleans_both_scopes(world):
    world.scrape()
    out = _scrape_out(world)
    package = _files(out)
    with world.fake:
        fin = world.finalizer()
        fin.run_once()
        assert fin.waiting[world.film] == "等待确认入库"
        fin.confirm(world.film, "actor")
        world.ticks(fin)
    assert world.status("archive") == "succeeded"
    assert world.status("cleanup_input") == "succeeded"
    assert world.status("cleanup_workcopy") == "succeeded"
    assert _files(world.nas / FILM_DIR) == package  # C2/C3: the exact approved package
    assert world.fake.copies == 1 and world.fake.posts == 1
    assert not any(p.is_file() for p in world.inbox.rglob("*"))  # C5/C6 input
    assert not out.exists()  # workcopy
    # C10: Taskpaw's private per-film dirs are reclaimed
    assert not list((world.rt.work_dir / "return").glob(f"{world.film}-r*"))
    assert not list((world.rt.work_dir / "scrape").glob(f"{world.film}-r*"))


def test_the_lookup_cache_is_reclaimed_with_the_film(world):
    """IR07: the Miko preview of a cleaned film is Taskpaw's to remove too."""
    cache = world.rt.work_dir / "lookup" / f"{world.film}-r1"
    cache.mkdir(parents=True)
    (cache / "lookup.json").write_text("{}", "utf-8")
    (cache / "poster.jpg").write_bytes(b"jpeg")
    other = world.rt.work_dir / "lookup" / "someone-else-r1"
    other.mkdir()
    world.scrape()
    with world.fake:
        fin = world.finalizer()
        fin.run_once()
        fin.confirm(world.film, "actor")
        world.ticks(fin)
    assert world.status("cleanup_workcopy") == "succeeded"
    assert not cache.exists()
    assert other.is_dir()  # only this film's


def test_download_extras_are_cleaned_with_the_original_never_archived(
    tmp_path, monkeypatch
):
    """H2 R1: the cover and advert are input members; the package folder goes too."""
    extras = {"ABF-393.jpg": b"cover from the download site", "新建文本文档.txt": b"ad"}
    world = World(tmp_path, extras)
    try:
        _in_package(world, monkeypatch, [])
        assert {p.name for p in (world.inbox / "pkg").iterdir()} >= set(extras)
        world.scrape()
        out = _scrape_out(world)
        assert not set(extras) & {p.name for p in out.rglob("*")}  # never reach Miko
        with world.fake:
            fin = world.finalizer()
            fin.confirm(world.film, "actor")
            world.ticks(fin)
        assert world.status("cleanup_input") == "succeeded"
        assert world.status("cleanup_workcopy") == "succeeded"
        archived = {p.name for p in (world.nas / FILM_DIR).rglob("*")}
        assert not set(extras) & archived  # nor the NAS
        assert not (world.inbox / "pkg").exists()  # emptied and removed
        assert world.inbox.is_dir()
    finally:
        world.rt.close()


def test_confirm_refusals_write_nothing(world, tmp_path):
    with pytest.raises(ConfirmError, match="刮削"):
        world.finalizer().confirm(world.film, "actor")
    world.scrape()
    with pytest.raises(ConfirmError, match="Filmpaw 未运行"):
        world.finalizer().confirm(world.film, "actor")
    (world.nas / FILM_DIR.lower()).mkdir()
    with world.fake:
        with pytest.raises(ConfirmError, match="同名"):
            world.finalizer().confirm(world.film, "actor")
        with pytest.raises(ConfirmError, match="演员"):
            world.finalizer().confirm(world.film, "nobody")
    assert world.rt.store.film_workflow(world.rt.operator, world.film).approval is None
    assert world.fake.posts == 0


def test_lost_response_is_looked_up_never_copied_twice(world):
    world.scrape()
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        world.fake.mode = "lose_response"
        world.ticks(fin)
    assert world.status("cleanup_input") == "succeeded"
    assert world.fake.copies == 1


def test_filmpaw_down_waits_and_a_restart_continues_the_same_operation(world, tmp_path):
    world.scrape()
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
    fin.run_once()  # Filmpaw gone: admitted, not submitted
    assert fin.waiting[world.film] == "等待 Filmpaw 运行"
    assert world.status("archive") == "running"
    world.rt.close()
    world.rt = world.runtime()  # restart: same fixed archive principal (C9)
    with world.fake:
        world.ticks(world.finalizer())
    assert world.status("cleanup_workcopy") == "succeeded"
    assert world.fake.copies == 1


def test_busy_filmpaw_is_retried_and_failure_needs_a_new_confirm(world):
    world.scrape()
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        world.fake.mode = "busy"
        fin.run_once()
        assert world.status("archive") == "running" and world.fake.copies == 0
        world.fake.mode = "fail"
        fin.run_once()  # resubmitted under the same key; Filmpaw fails it
        fin.run_once()  # the lookup reads the failure
        assert world.status("archive") == "failed"
        world.fake.mode = ""
        world.ticks(fin)
        assert world.status("archive") == "failed"  # C8: no automatic retry
        assert "重新确认" in fin.waiting[world.film]
        fin.confirm(world.film, "actor")
        world.ticks(fin)
    assert world.status("cleanup_input") == "succeeded"
    assert world.fake.copies == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows sharing semantics")
def test_a_source_still_being_written_waits_then_cleans(world):
    world.scrape()
    original = next(p for p in world.inbox.rglob("*") if p.suffix == ".mkv")
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        with open(original, "r+b"):
            world.ticks(fin)
            assert world.status("cleanup_input") != "succeeded"
            assert original.exists()
        world.ticks(fin)
    assert world.status("cleanup_input") == "succeeded"
    assert not original.exists() and world.fake.copies == 1


def test_a_replaced_original_is_kept_and_the_rest_is_cleaned(world):
    world.scrape()
    original = next(p for p in world.inbox.rglob("*") if p.suffix == ".mkv")
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        fin.run_once()  # archive submitted
        fin.run_once()  # receipt accepted
        fresh = original.with_name(original.name + ".new")
        fresh.write_bytes(original.read_bytes())
        os.replace(
            fresh, original
        )  # same bytes, new file (no inode reuse): not the approved object
        world.ticks(fin)
    assert original.exists()
    assert world.status("cleanup_input") != "succeeded"
    assert world.status("cleanup_workcopy") == "succeeded"
    assert "待清理" in fin.waiting[world.film]
    others = [p for p in world.inbox.rglob("*") if p.is_file() and p != original]
    assert others == []


def test_an_original_deleted_by_hand_counts_as_already_absent(world):
    world.scrape()
    original = next(p for p in world.inbox.rglob("*") if p.suffix == ".srt")
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        fin.run_once()
        fin.run_once()
        original.unlink()
        world.ticks(fin)
    assert world.status("cleanup_input") == "succeeded"
    wf = world.rt.store.film_workflow(world.rt.operator, world.film)
    statuses = {
        i.status for c in wf.cleanup_operations if c.scope == "input" for i in c.items
    }
    assert statuses == {"deleted", "already_absent"}


def test_confirm_twice_while_archiving_is_refused(world):
    world.scrape()
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
    fin.run_once()  # admitted, Filmpaw down
    with world.fake:
        with pytest.raises(ConfirmError):
            fin.confirm(world.film, "actor")
    assert os.listdir(world.nas) == []
    assert identity()  # keep helper import used


def test_target_replaced_after_confirm_fails_and_a_new_confirm_archives(world):
    """IR01: a stale target proof is a failure to re-confirm, never an endless 'busy'."""
    world.scrape()
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        world.nas.rename(world.nas.with_name("Actor-old"))
        world.nas.mkdir()
        world.ticks(fin, 2)
        assert world.status("archive") == "failed"
        fin.confirm(world.film, "actor")
        world.ticks(fin)
    assert world.status("cleanup_input") == "succeeded"
    assert (world.nas / FILM_DIR).is_dir()


def _in_package(world, monkeypatch, siblings):
    """Pretend the film came from an INBOX package shared with `siblings` (IR02)."""
    package = identity()
    real = world.rt.store.film_workflow

    def film_workflow(auth, film_id):
        return real(auth, film_id).model_copy(update={"package_id": package})

    def package_films(auth, package_id):
        own = real(auth, world.film)
        return (
            m.PackageFilm(
                film_id=world.film,
                archive_status=own.archive_status,
                cleanup_input_status=own.cleanup_input_status,
                cleanup_workcopy_status=own.cleanup_workcopy_status,
            ),
            *siblings,
        )

    monkeypatch.setattr(world.rt.store, "film_workflow", film_workflow)
    monkeypatch.setattr(world.rt.store, "package_films", package_films)


def test_input_cleanup_waits_until_every_film_of_the_package_is_archived(
    world, monkeypatch
):
    world.scrape()
    sibling = m.PackageFilm(
        film_id=identity(),
        archive_status="pending",
        cleanup_input_status="pending",
        cleanup_workcopy_status="pending",
    )
    siblings = [sibling]
    _in_package(world, monkeypatch, siblings)
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        world.ticks(fin)
        assert world.status("cleanup_workcopy") == "succeeded"
        assert world.status("cleanup_input") == "pending"
        assert fin.waiting[world.film] == "包内仍有未入库影片"
        assert any(p.is_file() for p in world.inbox.rglob("*"))
        siblings[0] = sibling.model_copy(update={"archive_status": "succeeded"})
        world.ticks(fin)
    assert world.status("cleanup_input") == "succeeded"


def test_one_broken_film_does_not_stop_the_others(world, monkeypatch):
    fin = world.finalizer()
    bad, good = identity(), identity()
    jobs = [type("Job", (), {"film_id": bad})(), type("Job", (), {"film_id": good})()]
    monkeypatch.setattr(world.rt.store, "list_jobs", lambda auth, status, stages: jobs)
    seen = []

    def advance(film_id, waiting):
        seen.append(film_id)
        if film_id == bad:
            raise RuntimeError("boom")
        return False

    monkeypatch.setattr(fin, "_advance", advance)
    fin.run_once()
    assert good in seen and "boom" in fin.waiting[bad]


def test_store_refusal_during_confirm_is_a_confirm_error(world, monkeypatch):
    world.scrape()

    def refuse(*args, **kwargs):
        raise m.WorkflowError("precondition_failed")

    monkeypatch.setattr(world.rt.store, "put_approval_context", refuse)
    with world.fake, pytest.raises(ConfirmError):
        world.finalizer().confirm(world.film, "actor")


@pytest.mark.skipif(
    sys.platform != "win32", reason="an open file blocks deletion on Windows"
)
def test_reclaim_is_retried_until_a_locked_work_file_is_released(world):
    """Codex: a locked private file must not leak the film's work dirs forever."""
    world.scrape()
    returned = next((world.rt.work_dir / "return").rglob("*.mkv"))
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        with open(returned, "rb"):
            world.ticks(fin)
            assert world.status("cleanup_workcopy") == "succeeded"
            assert returned.exists()
        world.ticks(fin, 1)
    assert not list((world.rt.work_dir / "return").glob(f"{world.film}-r*"))
    assert not any(fin.reclaim_dir.iterdir())


def test_reclaim_survives_a_restart(world):
    world.scrape()
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        real = fin._reclaim_pending
        fin._reclaim_pending = lambda: None  # crash before reclaiming
        world.ticks(fin)
        fin._reclaim_pending = real
    assert world.status("cleanup_input") == "succeeded"
    assert list((world.rt.work_dir / "return").glob(f"{world.film}-r*"))
    world.rt.close()
    world.rt = world.runtime()
    world.finalizer().run_once()
    assert not list((world.rt.work_dir / "return").glob(f"{world.film}-r*"))


def test_a_crash_right_after_the_recorded_cleanup_still_reclaims(world):
    """H1-4: the reclaim intent is durable before the cleanup result is."""
    world.scrape()
    real = world.rt.store.record_cleanup_result

    def record_then_crash(*args, **kwargs):
        real(*args, **kwargs)
        raise Crash()

    world.rt.store.record_cleanup_result = record_then_crash  # type: ignore[method-assign]
    with world.fake:
        world.finalizer().confirm(world.film, "actor")
        for _ in range(8):
            try:
                world.finalizer().run_once()  # each crash is a fresh process
            except Crash:
                pass
    assert world.status("cleanup_input") == "succeeded"
    assert world.status("cleanup_workcopy") == "succeeded"
    world.rt.close()
    world.rt = world.runtime()
    world.finalizer().run_once()
    assert not list((world.rt.work_dir / "return").glob(f"{world.film}-r*"))
    assert not list((world.rt.work_dir / "scrape").glob(f"{world.film}-r*"))


def test_shutdown_stops_finalization(world):
    world.scrape()
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        fin.cancel.set()
        world.ticks(fin)
    assert world.status("archive") == "pending" and world.fake.posts == 0
