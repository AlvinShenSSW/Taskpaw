"""F02-P1 end to end: 确认入库 → archive → receipt → exact cleanup → reclaim (C1–C10).

A real film runs through the real dispatcher up to Miko (fake CLI); the archive is the real library
engine over a temporary `library.db`, copying into a temporary "NAS". The INBOX originals are real
files whose identities the input manifest records, as intake would.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from taskpaw_v3.tests import test_workflow_return_step as ret
from taskpaw_v3.tests import test_workflow_scrape_step as scr
from taskpaw_v3.tests.test_workflow_local_dispatch import Crash
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.workflow_exec_helpers import video
from taskpaw_v3.tests.workflow_fake_library import FakeLibrary
from taskpaw_v3.workflow import file_identity
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.finalize import ConfirmError, Finalizer
from taskpaw_v3.workflow.library_store import insert_library, normalize

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
        self.inbox.mkdir()
        self.nas.mkdir(parents=True)
        self.rt = self.runtime()
        self.film = self._film(extras, plan=plan)
        self.fake = FakeLibrary(tmp_path / "library", {"actor": self.nas})

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
        return Finalizer(rt or self.rt, self.inbox, self.fake.archive)

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
        w.fake.close()


def _files(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def _scrape_out(world: World) -> Path:
    # #303: Miko names the files after the film (`ABC-123-无码-C.nfo`)
    return world.rt.output_dir / FILM_DIR


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


def test_manual_confirm_archives_and_cleans_both_scopes(world):
    world.scrape()
    destination = world.tmp / "自选目录"
    destination.mkdir()
    package = _files(_scrape_out(world))
    with world.fake:
        fin = world.finalizer()
        with (
            patch.object(fin.library, "archive", wraps=fin.library.archive) as archive,
            patch.object(world.rt, "evidence", wraps=world.rt.evidence) as evidence,
        ):
            fin.confirm(world.film, target_dir=str(destination))
            evidence.assert_any_call(
                "operator_choice",
                f"manual {destination}",
                producer=world.rt.archive_auth,
            )
            approval = world.rt.store.film_workflow(
                world.rt.operator, world.film
            ).approval
            target = approval.binding.target
            assert target.native_identity.scheme == "manual_directory"
            assert target.native_identity.volume_or_export_id == str(destination)
            assert target.root_id == str(
                uuid.uuid5(
                    uuid.UUID("0b7f3c2e-6f2d-4a8e-9c1d-3e5a7b9d2f41"),
                    f"filmpaw-manual-target:{destination}",
                )
            )
            world.ticks(fin)
            assert archive.call_count == 1
            assert archive.call_args.kwargs["target_dir"] == str(destination)
            assert archive.call_args.kwargs["performer_id"] is None
    assert world.status("archive") == "succeeded"
    assert world.status("cleanup_input") == "succeeded"
    assert world.status("cleanup_workcopy") == "succeeded"
    assert _files(destination / FILM_DIR) == package
    assert not list(world.nas.iterdir())


@pytest.mark.parametrize(
    "targets", [{}, {"performer_id": "actor", "target_dir": "C:/library"}]
)
def test_confirm_requires_exactly_one_target(world, targets):
    with pytest.raises(ConfirmError, match="请选择一个"):
        world.finalizer().confirm(world.film, **targets)
    assert world.rt.store.film_workflow(world.rt.operator, world.film).approval is None


def test_a_new_final_revision_shows_no_receipt_until_it_is_archived(world):
    """U1 Codex r2 F2: the receipt shown is the current final revision's only;
    revision 1's receipt never stands in for revision 2."""
    from taskpaw_v3.workflow import board

    world.scrape()
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        world.ticks(fin)
    rt = world.rt
    receipt = rt.store.film_workflow(rt.operator, world.film).archive_receipt_id
    assert receipt is not None
    (row,) = rt.store.film_board(rt.operator, film_id=world.film)
    assert row.receipt_id == receipt
    assert board.film_detail(rt, world.film, {})["receipt_id"] == receipt
    conn = rt.store._conn  # a later scrape published revision 2 (synthetic)
    old = conn.execute(
        "SELECT * FROM manifests WHERE film_id=? AND kind='final' AND revision=1",
        (world.film,),
    ).fetchone()
    data = m.FinalManifest.model_validate_json(old["body_json"]).model_dump(mode="json")
    data["revision"] = 2
    data["manifest_hash"] = m.manifest_digest(data)
    second = m.FinalManifest.model_validate(data)
    with conn:
        conn.execute(
            "INSERT INTO manifests(film_id,kind,revision,manifest_hash,input_revision,"
            "input_hash,attempt_id,created_at,body_json) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                world.film,
                "final",
                2,
                second.manifest_hash,
                old["input_revision"],
                old["input_hash"],
                old["attempt_id"],
                old["created_at"],
                m.canonical_json(second),
            ),
        )
        conn.execute(
            "UPDATE films SET current_final_revision=2,"
            "body_json=json_set(body_json,'$.current_final_revision',2) WHERE film_id=?",
            (world.film,),
        )
    (row,) = rt.store.film_board(rt.operator, film_id=world.film)
    assert row.final_revision == 2 and row.receipt_id is None
    assert (
        next(
            f for f in rt.store.film_board(rt.operator) if f.film_id == world.film
        ).receipt_id
        is None
    )
    assert board.film_detail(rt, world.film, {})["receipt_id"] is None


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
        world.fake.close()


def test_confirm_refusals_write_nothing(world, tmp_path):
    with pytest.raises(ConfirmError, match="刮削"):
        world.finalizer().confirm(world.film, "actor")
    world.scrape()
    with pytest.raises(ConfirmError, match="片库不可用"):
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


def test_library_down_waits_and_a_restart_continues_the_same_operation(world, tmp_path):
    world.scrape()
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
    fin.run_once()  # the library is closed: admitted, not submitted
    assert fin.waiting[world.film] == "等待片库可用"
    assert world.status("archive") == "running"
    world.rt.close()
    world.rt = world.runtime()  # restart: same fixed archive principal (C9)
    with world.fake:
        world.ticks(world.finalizer())
    assert world.status("cleanup_workcopy") == "succeeded"
    assert world.fake.copies == 1


def test_a_failed_archive_needs_a_new_confirm(world):
    world.scrape()
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        world.fake.mode = "fail"
        fin.run_once()  # submitted; the preflight fails it (no free space)
        assert world.status("archive") == "running" and world.fake.copies == 0
        fin.run_once()  # the lookup reads the failure
        assert world.status("archive") == "failed"
        failure = world.rt.store.film_workflow(world.rt.operator, world.film)
        assert "NAS 可用空间不足" in (failure.archive_operations[-1].error_code or "")
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
    fin.run_once()  # admitted, the library closed
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


# ── the library target (#363: §9.7 encoding, §9.6 candidates, §9.8 cutover) ──
def _binding_target(world) -> m.TargetIdentity:
    return world.rt.store.film_workflow(
        world.rt.operator, world.film
    ).approval.binding.target


def test_a_performer_target_is_encoded_as_a_library_performer(world):
    world.scrape()
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        target = _binding_target(world)
        native = target.native_identity
        assert native.scheme == "library_performer"
        assert native.volume_or_export_id == "actor"
        assert (
            target.root_id
            == native.root_id
            == str(
                uuid.uuid5(
                    uuid.UUID("0b7f3c2e-6f2d-4a8e-9c1d-3e5a7b9d2f41"),
                    "taskpaw-performer:actor",
                )
            )
        )
        assert target.export_identity == str(world.nas)
        assert target.relative_path == FILM_DIR and target.expected_absent
        st = os.lstat(world.nas)
        assert native.object_id == json.dumps([str(st.st_dev), str(st.st_ino)])
        approval = world.rt.store.film_workflow(world.rt.operator, world.film).approval
        assert approval.binding.actor_ids == (target.root_id,)
        with patch.object(fin.library, "archive", wraps=fin.library.archive) as call:
            world.ticks(fin)
        assert call.call_args.kwargs["performer_id"] == "actor"
        assert call.call_args.kwargs["target_dir"] is None
    assert world.status("cleanup_input") == "succeeded"


def _filmpaw_era(world, monkeypatch, **native):
    """Confirm as Filmpaw did: the same target with the old scheme or identity."""
    real = Finalizer._target

    def old(self, *args, **kwargs):
        target = real(self, *args, **kwargs)
        return target.model_copy(
            update={"native_identity": target.native_identity.model_copy(update=native)}
        )

    monkeypatch.setattr(Finalizer, "_target", old)


def test_an_open_filmpaw_era_archive_fails_once_and_a_new_confirm_archives(
    world, monkeypatch
):
    world.scrape()
    _filmpaw_era(world, monkeypatch, scheme="export_object_id")
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        monkeypatch.undo()
        world.ticks(fin)
        assert world.status("archive") == "failed"
        wf = world.rt.store.film_workflow(world.rt.operator, world.film)
        (failed,) = wf.archive_operations
        assert failed.error_code == "升级前未完成的归档，请重新确认入库"
        assert world.fake.posts == 0  # never reached the new engine
        assert "重新确认入库" in fin.waiting[world.film]
        fin.confirm(world.film, "actor")
        world.ticks(fin)
    assert world.status("cleanup_input") == "succeeded"
    assert _binding_target(world).native_identity.scheme == "library_performer"


def test_an_open_filmpaw_era_manual_archive_resubmits_to_the_engine(world, monkeypatch):
    """§9.8: a manual binding is kind-compatible; its integer identity is the same."""
    world.scrape()
    destination = world.tmp / "manual"
    destination.mkdir()
    st = os.lstat(destination)
    _filmpaw_era(world, monkeypatch, object_id=json.dumps([st.st_dev, st.st_ino]))
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, target_dir=str(destination))
        world.ticks(fin)
    assert world.status("cleanup_input") == "succeeded"
    assert (destination / FILM_DIR).is_dir()


def _library(world, kind: str, path: Path, **kw) -> str:
    path.mkdir(parents=True, exist_ok=True)
    with world.fake.store.transaction() as conn:
        return insert_library(conn, kind, str(path), **kw)


def _location(world, performer_id: str, path: Path, *, films: int = 0) -> str:
    """One more location of the `Actor` group, in a library of its own."""
    library = _library(world, "actor", path.parent)
    path.mkdir(exist_ok=True)
    with world.fake.store.transaction() as conn:
        conn.execute(
            "INSERT INTO performers(id, name, name_norm, library_id, folder_name,"
            " path, first_seen_at, last_seen_at, is_missing, film_count)"
            " VALUES (?,?,?,?,?,?,'t','t',0,?)",
            (
                performer_id,
                path.name,
                normalize(path.name),
                library,
                path.name,
                str(path),
                films,
            ),
        )
    return library


def _rules(world, **rules) -> None:
    world.fake.store.put_rules(rules)


def test_candidates_come_from_the_library_and_the_rules_preselect(world, tmp_path):
    """§9.6 step 1: one group preselects, its locations by `multi_location`."""
    world.scrape()
    with world.fake:
        fin = world.finalizer()
        only = fin.preflight(world.film)
        assert only["performer_id"] == "actor" and only["absent"] is True
        assert only["selected"] == {"kind": "performer", "id": "actor"}
        assert only["archived"] is False and only["categories"] == []
        _location(world, "second", tmp_path / "nas2" / "Actor", films=5)
        several = fin.preflight(world.film)
        assert [i["id"] for i in several["candidates"]["items"]] == ["actor", "second"]
        assert several["performer_id"] == "actor"  # priority: library order
        _rules(world, multi_location="most")
        assert fin.preflight(world.film)["performer_id"] == "second"
        _rules(world, multi_location="recent")  # no receipt yet: library order
        assert fin.preflight(world.film)["performer_id"] == "actor"
        _rules(world, preselect_single=False)
        unselected = fin.preflight(world.film)
        assert (unselected["selected"], unselected["target_path"]) == (None, None)
        _rules(world, preselect_single=True)
        with world.fake.store.transaction() as conn:
            conn.execute("UPDATE performers SET is_missing=1 WHERE id='second'")
        again = fin.preflight(world.film)  # include_missing=false
        assert [i["id"] for i in again["candidates"]["items"]] == ["actor"]


def test_the_recent_rule_picks_the_location_of_the_newest_receipt(world, tmp_path):
    world.scrape()
    second = tmp_path / "nas2" / "Actor"
    _location(world, "second", second)
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "second")
        world.ticks(fin)
        assert (second / FILM_DIR).is_dir()
        _rules(world, multi_location="recent")
        assert world.fake.store.candidates(["Actor"], "X")["selected"] == {
            "kind": "performer",
            "id": "second",
        }


def test_several_groups_fuzzy_and_read_only_candidates_are_never_preselected(
    world, tmp_path
):
    world.scrape()
    store = world.fake.store
    _location(world, "other", tmp_path / "nas2" / "Actress")
    with store.transaction() as conn:  # a second group found by an alias
        conn.execute(
            "INSERT INTO aliases(name_norm, alias, alias_norm)"
            " VALUES ('actress','Actor','actor')"
        )
    groups = store.candidates(["Actor"], "X")
    assert {i["id"] for i in groups["items"]} == {"actor", "other"}
    assert groups["selected"] is None  # §9.6 step 2
    _rules(world, alias_match=False)
    assert store.candidates(["Actor"], "X")["selected"]["id"] == "actor"
    fuzzy = store.candidates(["Act or"], "X")  # §6: listed only
    assert [(i["id"], i["fuzzy"]) for i in fuzzy["items"]] == [("actor", True)]
    assert fuzzy["selected"] is None
    _rules(world, fuzzy=False)
    assert store.candidates(["Act or"], "X")["items"] == []
    with store.transaction() as conn:
        conn.execute("UPDATE libraries SET role='readonly' WHERE kind='actor'")
    readonly = store.candidates(["Actor"], "X")
    assert [i["role"] for i in readonly["items"]] == ["readonly"]
    assert readonly["selected"] is None


def test_a_category_prefix_preselects_only_without_an_actor_and_hints_otherwise(
    world, tmp_path
):
    """§9.6 steps 3–4 and the `category_*` / `second_target_hint` rules."""
    world.scrape()
    store = world.fake.store
    photo = _library(world, "category", tmp_path / "photo", prefixes=["AB"])
    series = _library(world, "category", tmp_path / "series", prefixes=["abc"])
    with world.fake:
        fin = world.finalizer()
        actor = fin.preflight(world.film)
        assert actor["selected"] == {"kind": "performer", "id": "actor"}
        assert actor["second_hint"] == series  # the longest prefix
        assert [(c["id"], c["prefix_match"]) for c in actor["categories"]] == [
            (photo, True),
            (series, True),
        ]
        assert actor["categories"][1]["prefixes"] == ["ABC"]
        _rules(world, second_target_hint=False)
        assert fin.preflight(world.film)["second_hint"] is None
        _rules(world, category_always=False)
        _rules(world, second_target_hint=True)
        listing = fin.preflight(world.film)
        assert listing["categories"] == []  # the switch only hides the listing
        assert listing["second_hint"] == series  # ... not the §8 hint
        with store.transaction() as conn:
            conn.execute("DELETE FROM performers")
        none = fin.preflight(world.film)
        assert none["selected"] == {"kind": "category", "id": series}
        assert none["category_id"] == series and none["second_hint"] is None
        assert none["target_path"] == str(tmp_path / "series" / FILM_DIR)
        assert none["absent"] is True and none["error_code"] is None
        _rules(world, category_prefix=False)
        assert fin.preflight(world.film)["selected"] is None
        assert store.candidates([], "XYZ-1")["selected"] is None


def test_the_new_actor_defaults_follow_the_rules(world):
    world.scrape()
    store = world.fake.store
    with world.fake:
        fin = world.finalizer()
        (library,) = store.library_ids()
        assert fin.preflight(world.film)["new_actor"] == {
            "library_id": library,
            "name": "Actor",
        }
        _rules(world, new_actor_name="ask")
        assert fin.preflight(world.film)["new_actor"]["name"] == ""
        _rules(world, allow_new_actor=False)
        assert fin.preflight(world.film)["new_actor"] is None


def test_a_new_actor_preflight_proves_the_root_and_refuses_an_existing_actor(world):
    world.scrape()
    (library,) = world.fake.store.library_ids()
    with world.fake:
        fin = world.finalizer()
        new = fin.preflight(world.film, library_id=library, actor_name="新人")
        assert new["selected"] == {"kind": "new_actor", "id": library}
        assert new["target_path"] == str(world.nas.parent / "新人" / FILM_DIR)
        assert (new["absent"], new["error_code"]) == (True, None)
        taken = fin.preflight(world.film, library_id=library, actor_name="actor")
        assert taken["error_code"] == "actor_exists"  # casefold of nas/Actor
        bad = fin.preflight(world.film, library_id=library, actor_name="a:b")
        assert bad["error_code"] == "permission"
        with pytest.raises(ConfirmError, match="只选择一个"):
            fin.preflight(world.film, "actor", category_id="c")


def _root(key: str) -> str:
    return str(uuid.uuid5(uuid.UUID("0b7f3c2e-6f2d-4a8e-9c1d-3e5a7b9d2f41"), key))


def _second_row(world) -> dict | None:
    with world.fake.store.reading() as conn:
        row = conn.execute("SELECT * FROM second_targets").fetchone()
    return dict(row) if row else None


def test_a_category_confirm_is_encoded_and_archived_into_the_category(world, tmp_path):
    world.scrape()
    photo = tmp_path / "photo"
    category = _library(world, "category", photo, prefixes=["ABC"])
    package = _files(_scrape_out(world))
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, category_id=category)
        target = _binding_target(world)
        native = target.native_identity
        assert (native.scheme, native.volume_or_export_id) == (
            "library_category",
            category,
        )
        assert target.root_id == _root(f"taskpaw-category:{category}")
        assert target.export_identity == str(photo)
        assert target.relative_path == FILM_DIR
        assert _second_row(world) is None  # only actor targets carry one
        with patch.object(fin.library, "archive", wraps=fin.library.archive) as call:
            world.ticks(fin)
        assert call.call_args.kwargs["category_id"] == category
        assert call.call_args.kwargs["second"] is None
    assert world.status("cleanup_input") == "succeeded"
    assert _files(photo / FILM_DIR) == package
    with pytest.raises(ConfirmError, match="第二目标"):
        world.finalizer().confirm(
            world.film, category_id=category, second_category_id=category
        )


def test_a_new_actor_confirm_creates_the_directory_and_its_performer(world):
    world.scrape()
    (library,) = world.fake.store.library_ids()
    actor = world.nas.parent / "新人"
    package = _files(_scrape_out(world))
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, library_id=library, actor_name="新人")
        assert not actor.exists()  # created only by the archive (D3)
        target = _binding_target(world)
        native = target.native_identity
        assert (native.scheme, native.volume_or_export_id) == (
            "library_new_actor",
            library,
        )
        assert target.root_id == _root(f"taskpaw-new-actor:{library}:新人")
        assert target.export_identity == str(world.nas.parent)
        assert target.relative_path == f"新人/{FILM_DIR}"
        st = os.lstat(world.nas.parent)
        assert native.object_id == json.dumps([str(st.st_dev), str(st.st_ino)])
        assert _second_row(world)["category_id"] is None  # always written
        world.ticks(fin)
        (row,) = world.fake.store.search("新人")["items"]
        assert (row["unc_path"], row["is_missing"]) == (str(actor), False)
    assert world.status("cleanup_input") == "succeeded"
    assert _files(actor / FILM_DIR) == package
    wf = world.rt.store.film_workflow(world.rt.operator, world.film)
    assert wf.archive_path == str(actor / FILM_DIR)


def test_a_second_target_is_copied_too_before_any_cleanup(world, tmp_path):
    """§9.4: one approval, two verified copies, then cleanup."""
    world.scrape()
    photo = tmp_path / "photo"
    category = _library(world, "category", photo)
    package = _files(_scrape_out(world))
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor", second_category_id=category)
        approval = world.rt.store.film_workflow(world.rt.operator, world.film).approval
        row = _second_row(world)
        assert row["binding_hash"] == approval.binding_hash
        assert (row["category_id"], row["target_path"]) == (category, str(photo))
        with patch.object(fin.library, "archive", wraps=fin.library.archive) as call:
            fin.run_once()  # submitted and copied twice inline
        assert call.call_args.kwargs["second"]["category_id"] == category
        world.ticks(fin)
    assert world.fake.copies == 2
    assert _files(world.nas / FILM_DIR) == _files(photo / FILM_DIR) == package
    assert world.status("cleanup_input") == "succeeded"
    assert world.status("cleanup_workcopy") == "succeeded"


def test_an_actor_binding_without_its_second_target_row_fails_unarchived(world):
    world.scrape()
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        with world.fake.store.transaction() as conn:
            conn.execute("DELETE FROM second_targets")
        world.ticks(fin, 2)
    assert world.status("archive") == "failed"
    (failed,) = world.rt.store.film_workflow(
        world.rt.operator, world.film
    ).archive_operations
    assert failed.error_code == "入库目标记录缺失，请重新确认入库"
    assert world.fake.posts == 0 and world.status("cleanup_input") == "pending"


def test_a_receipt_without_the_bound_second_copy_is_not_accepted(world, tmp_path):
    world.scrape()
    category = _library(world, "category", tmp_path / "photo")
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor", second_category_id=category)
        real = fin.library.lookup

        def one_copy(operation_id):
            task = real(operation_id)
            if task and task["receipt"]:
                task = {**task, "receipt": {**task["receipt"], "second": None}}
            return task

        with patch.object(fin.library, "lookup", one_copy):
            world.ticks(fin)
    assert world.status("archive") == "failed"
    assert world.status("cleanup_input") == "pending"
    wf = world.rt.store.film_workflow(world.rt.operator, world.film)
    assert wf.archive_operations[-1].error_code == "归档回执与已批准清单不一致"


def test_a_failed_second_copy_is_retried_alone_after_a_new_confirm(
    world, tmp_path, monkeypatch
):
    """§9.5: the primary reads as archived, is re-verified, and is not copied."""
    from taskpaw_v3.workflow import library_archive as la

    world.scrape()
    photo = tmp_path / "photo"
    category = _library(world, "category", photo)
    package = _files(_scrape_out(world))
    real = la.LibraryArchive._verify

    def second_fails(self, task, payload, staging, hashes):
        if staging.parent == photo:
            raise la._Failed("SHA-256 内容校验失败：模拟")
        return real(self, task, payload, staging, hashes)

    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor", second_category_id=category)
        monkeypatch.setattr(la.LibraryArchive, "_verify", second_fails)
        world.ticks(fin, 2)
        monkeypatch.undo()
        assert world.status("archive") == "failed"
        assert world.fake.copies == 1 and (world.nas / FILM_DIR).is_dir()
        assert world.status("cleanup_input") == "pending"  # nothing cleaned
        again = fin.preflight(world.film)
        assert (again["absent"], again["archived"], again["error_code"]) == (
            False,
            True,
            None,
        )
        fin.confirm(world.film, "actor", second_category_id=category)
        assert _binding_target(world).expected_absent is True
        world.ticks(fin)
    assert world.fake.copies == 2  # only the second copy ran again
    assert _files(world.nas / FILM_DIR) == _files(photo / FILM_DIR) == package
    assert world.status("cleanup_input") == "succeeded"


def test_a_folder_this_engine_did_not_commit_still_blocks_confirm(world):
    world.scrape()
    (world.nas / FILM_DIR).mkdir()
    with world.fake:
        fin = world.finalizer()
        found = fin.preflight(world.film)
        assert (found["archived"], found["error_code"]) == (False, "duplicate")
        with pytest.raises(ConfirmError, match="同名"):
            fin.confirm(world.film, "actor")


def test_preflight_maps_the_refusal_code_not_the_status(world):
    world.scrape()
    with world.fake:
        fin = world.finalizer()
        with world.fake.store.transaction() as conn:
            conn.execute("UPDATE libraries SET role='readonly'")
        readonly = fin.preflight(world.film, "actor")
        assert readonly["error_code"] == "permission" and "只读" in readonly["message"]
        gone = fin.preflight(world.film, "nobody")
        assert gone["error_code"] == "missing"
        with pytest.raises(ConfirmError, match="只读"):
            fin.confirm(world.film, "actor")
    offline = fin.preflight(world.film, "actor")
    assert (offline["error_code"], offline["candidates"]) == ("offline", None)


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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows read-only unlink")
def test_completed_package_reclaims_its_readonly_intake_snapshot(world, monkeypatch):
    world.scrape()
    _in_package(world, monkeypatch, [])
    real = world.rt.store.film_workflow

    def film_workflow(auth, film_id):
        return real(auth, film_id).model_copy(
            update={"snapshot_work_path": ret.WORKCOPY}
        )

    monkeypatch.setattr(world.rt.store, "film_workflow", film_workflow)
    snapshot = world.rt.work_dir / ret.WORKCOPY
    copied = snapshot / "movie.mkv"
    copied.chmod(stat.S_IREAD)
    try:
        with world.fake:
            fin = world.finalizer()
            fin.confirm(world.film, "actor")
            world.ticks(fin)
        assert world.status("cleanup_workcopy") == "succeeded"
        assert not snapshot.exists()
        assert not any(fin.reclaim_dir.iterdir())
    finally:
        if copied.exists():
            copied.chmod(stat.S_IWRITE)


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


def test_cas_conflict_is_retried_quietly(monkeypatch, tmp_path):
    """A lost optimistic-lock race is retried on the next tick; it is not shown
    as 「归档/清理暂停」 (operator, 2026-10-24)."""
    from types import SimpleNamespace

    from taskpaw_v3.workflow import models as wm
    from taskpaw_v3.workflow.finalize import Finalizer

    job = SimpleNamespace(film_id="film")
    store = SimpleNamespace(list_jobs=lambda *a, **k: [job])
    fin = Finalizer(
        SimpleNamespace(data_dir=tmp_path, store=store, operator=None),
        None,
        SimpleNamespace(),
    )
    fin.waiting = {"film": "归档中"}

    def conflict(film_id, waiting):
        raise wm.WorkflowError("cas_conflict")

    monkeypatch.setattr(fin, "_advance", conflict)
    monkeypatch.setattr(fin, "_reclaim_pending", lambda: None)
    assert fin.run_once() is False
    assert fin.waiting == {"film": "归档中"}


def test_empty_actor_folder_goes_with_its_last_film(tmp_path):
    """Operator 2026-10-24: after a film's INBOX folder is cleaned, its actor
    folder is removed only when no other film is left in it; the root stays."""
    from taskpaw_v3.workflow.finalize import _rmdir_parents

    inbox = tmp_path / "TODO"
    last = inbox / "actor-a"
    last.mkdir(parents=True)
    busy = inbox / "actor-b"
    (busy / "OTHER-001").mkdir(parents=True)
    _rmdir_parents(last / "FILM-001", inbox)
    _rmdir_parents(busy / "FILM-002", inbox)
    assert not last.exists()
    assert (busy / "OTHER-001").is_dir()
    assert inbox.is_dir()
    _rmdir_parents(inbox / "FILM-003", inbox)  # a root-level package: root kept
    assert inbox.is_dir()
