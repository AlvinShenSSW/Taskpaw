"""#362 L1: the controller's NAS library index (`library.db`, #361 §4–§8)."""

from __future__ import annotations

import io
import logging
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from taskpaw_v3.workflow import library_import, library_scan, library_store
from taskpaw_v3.workflow.library_scan import LibraryScanner, scan_library
from taskpaw_v3.workflow.library_store import (
    SCHEMA_VERSION,
    LibraryError,
    LibraryStore,
    normalize,
    normalize_path,
)
from taskpaw_v3.workflow.schema import SchemaError

TABLES = {
    "schema_version",
    "libraries",
    "performers",
    "aliases",
    "settings",
    "archive_tasks",
    "archive_events",
    "archived_targets",
    "second_targets",
}


@pytest.fixture(autouse=True)
def _no_real_filmpaw(monkeypatch, tmp_path):
    """The default import source follows APPDATA: never the operator's."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))


@pytest.fixture
def store(tmp_path):
    value = LibraryStore.open(tmp_path / "library.db")
    try:
        yield value
    finally:
        value.close()


def root(tmp_path: Path, name: str, *actors: str, films: int = 0) -> Path:
    path = tmp_path / name
    path.mkdir()
    for actor in actors:
        (path / actor).mkdir()
        for n in range(films):
            (path / actor / f"FILM-{n:03d}").mkdir()
    return path


def performers(store: LibraryStore, library_id: str) -> dict[str, sqlite3.Row]:
    with store.reading() as conn:
        rows = conn.execute(
            "SELECT * FROM performers WHERE library_id=?", (library_id,)
        ).fetchall()
    return {row["folder_name"]: row for row in rows}


def actor_library(store, tmp_path, *actors, name="actors", films=0):
    return store.add_library("actor", str(root(tmp_path, name, *actors, films=films)))[
        "id"
    ]


def expect(status, fn, *args, **kwargs):
    with pytest.raises(LibraryError) as info:
        fn(*args, **kwargs)
    assert info.value.status == status, info.value.detail
    return info.value


# ── schema (§4, §5) ──────────────────────────────────────────────────────
def test_schema_v1_creates_every_table_and_reopens(tmp_path):
    path = tmp_path / "library.db"
    LibraryStore.open(path).close()
    again = LibraryStore.open(path)
    try:
        with again.reading() as conn:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            (version,) = conn.execute("SELECT version FROM schema_version").fetchone()
        assert TABLES <= names and version == SCHEMA_VERSION == 1
    finally:
        again.close()


def test_a_newer_schema_is_refused_untouched(tmp_path):
    path = tmp_path / "library.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version VALUES (2)")
    conn.commit()
    conn.close()
    before = path.read_bytes()
    error = expect(503, LibraryStore.open, path)
    assert "2" in error.detail and path.read_bytes() == before


def test_a_failed_creation_leaves_no_partial_schema(tmp_path, monkeypatch):
    def broken(conn):
        conn.execute("CREATE TABLE libraries (id TEXT)")
        raise sqlite3.OperationalError("boom")

    monkeypatch.setitem(library_store._STEPS, 0, broken)
    path = tmp_path / "library.db"
    with pytest.raises(sqlite3.OperationalError):
        LibraryStore.open(path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT name FROM sqlite_master").fetchall() == []
    conn.close()


@pytest.mark.parametrize(
    "path", ["\\\\nas\\share\\library.db", "//nas/share/library.db"]
)
def test_a_network_path_is_refused_like_workflow_db(path):
    with pytest.raises(SchemaError, match="local_database_required"):
        LibraryStore.open(Path(path))


def test_normalize_is_filmpaw_s():
    assert normalize(" 倉木華 ") == "仓木华"
    assert normalize("張") == "张"
    assert normalize("ＡＢＣ（Ｘ）") == "abc(x)"


def test_library_check_role_runs_in_process(capsys):
    from taskpaw_v3.packaging import backend_main

    assert backend_main.main(["library-check"]) == 0
    assert capsys.readouterr().out.strip() == r"\u5f20"  # 张, ASCII-escaped


def test_packaged_library_normalizes_traditional_chinese():
    """The built sidecar's zhconv loader (pkg_resources path) must work frozen."""
    import subprocess

    supplied = os.environ.get("TASKPAW_TEST_UPSTREAM_BACKEND")
    if not supplied:
        pytest.skip("actual built sidecar not supplied; no frozen acceptance claim")
    done = subprocess.run(
        [str(Path(supplied).resolve()), "library-check"],
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.decode("ascii").strip() == r"\u5f20"  # 张, ASCII-escaped


# ── libraries (§6) ───────────────────────────────────────────────────────
def test_add_normalizes_the_path_and_defaults_label_and_role(store, tmp_path):
    path = root(tmp_path, "Actors")
    raw = str(path).replace("\\", "/") if os.sep == "\\" else str(path)
    lib = store.add_library("actor", raw)
    assert lib["path"] == str(path) + os.sep
    assert (lib["label"], lib["role"], lib["prefixes"], lib["position"]) == (
        "Actors",
        "archive",
        [],
        1,
    )
    assert lib["last_scan_ok"] is None and lib["performer_count"] == 0
    second = store.add_library("actor", str(root(tmp_path, "More")), role="readonly")
    assert (second["position"], second["role"]) == (2, "readonly")


def test_add_category_uppercases_prefixes(store, tmp_path):
    lib = store.add_library(
        "category",
        str(root(tmp_path, "Series")),
        label="系列",
        prefixes=[" fc2 ", "FC2", ""],
    )
    assert (lib["label"], lib["role"], lib["prefixes"]) == ("系列", "archive", ["FC2"])
    expect(
        422, store.add_library, "category", str(root(tmp_path, "B")), role="readonly"
    )
    expect(422, store.add_library, "actor", str(root(tmp_path, "C")), prefixes=["X"])


def test_add_requires_an_existing_directory(store, tmp_path):
    expect(422, store.add_library, "actor", str(tmp_path / "nope"))
    (tmp_path / "file").write_text("x")
    expect(422, store.add_library, "actor", str(tmp_path / "file"))


def test_add_refuses_the_same_root_in_any_case(store, tmp_path):
    path = root(tmp_path, "Actors")
    store.add_library("actor", str(path))
    expect(409, store.add_library, "category", str(path) + os.sep)
    if sys.platform == "win32":  # a case-insensitive file system
        expect(409, store.add_library, "actor", str(path).upper())


def test_equivalent_spellings_of_a_root_are_one_root(store, tmp_path):
    path = root(tmp_path, "Actors", "sub")
    (tmp_path / "other").mkdir()
    store.add_library("actor", str(path))
    for spelling in (
        os.path.join(str(path), ".", ""),
        os.path.join(str(path), "sub", ".."),
        os.path.join(str(tmp_path), "other", "..", "Actors"),
    ):
        assert (
            expect(409, store.add_library, "category", spelling).detail
            == "该片库已存在"
        )
    nested = os.path.join(str(tmp_path), "other", "..", "Actors", "sub")
    assert (
        expect(409, store.add_library, "category", nested).detail
        == "该目录与已有片库互相包含"
    )
    assert len(store.libraries()) == 1


def test_a_root_may_not_contain_or_sit_inside_another(store, tmp_path):
    outer = root(tmp_path, "outer", "inner")
    store.add_library("category", str(outer / "inner"))
    expect(409, store.add_library, "actor", str(outer))  # contains
    other = root(tmp_path, "other", "sub")
    store.add_library("actor", str(other))
    expect(409, store.add_library, "category", str(other / "sub"))  # inside
    sibling = root(tmp_path, "other2")  # a shared name prefix is not nesting
    assert store.add_library("actor", str(sibling))["kind"] == "actor"


def test_update_changes_only_label_role_and_prefixes(store, tmp_path):
    actor = actor_library(store, tmp_path)
    category = store.add_library("category", str(root(tmp_path, "cat")))["id"]
    lib = store.update_library(actor, label=" 新名 ", role="readonly")
    assert (lib["label"], lib["role"]) == ("新名", "readonly")
    assert store.update_library(category, prefixes=["abp", "ssis"])["prefixes"] == [
        "ABP",
        "SSIS",
    ]
    expect(422, store.update_library, actor, label="  ")
    expect(422, store.update_library, category, role="readonly")
    expect(422, store.update_library, actor, prefixes=["X"])
    expect(404, store.update_library, "missing", label="x")


def test_remove_cascades_performers_and_keeps_aliases(store, tmp_path):
    lib = actor_library(store, tmp_path, "Alice")
    scan_library(store, lib)
    performer = performers(store, lib)["Alice"]["id"]
    store.add_alias(performer, "Ali")
    store.remove_library(lib)
    assert performers(store, lib) == {}
    with store.reading() as conn:
        assert conn.execute("SELECT alias FROM aliases").fetchall()[0][0] == "Ali"
    expect(404, store.remove_library, lib)


# ── scan (§6) ────────────────────────────────────────────────────────────
def test_scan_adds_visible_actor_dirs_with_their_film_counts(store, tmp_path):
    lib = actor_library(store, tmp_path, "Alice", "Bob", ".hidden", films=2)
    base = Path(store.library(lib)["path"])
    (base / "notes.txt").write_text("x")
    (base / "Alice" / ".trash").mkdir()
    (base / "Alice" / "cover.jpg").write_text("x")
    result = scan_library(store, lib)
    assert (result.ok, result.added, result.missing, result.film_count) == (
        True,
        2,
        0,
        4,
    )
    rows = performers(store, lib)
    assert set(rows) == {"Alice", "Bob"}
    alice = rows["Alice"]
    assert (alice["film_count"], alice["name_norm"], alice["is_missing"]) == (
        2,
        "alice",
        0,
    )
    assert alice["path"] == str(base / "Alice")
    info = store.library(lib)
    assert (info["last_scan_ok"], info["film_count"], info["performer_count"]) == (
        True,
        4,
        2,
    )
    assert info["last_scan_at"] and info["last_scan_ms"] is not None
    assert isinstance(info["free_bytes"], int)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows attributes")
def test_scan_skips_hidden_and_system_directories(store, tmp_path):
    import ctypes

    lib = actor_library(store, tmp_path, "Alice", "Hidden", "System")
    base = Path(store.library(lib)["path"])
    ctypes.windll.kernel32.SetFileAttributesW(str(base / "Hidden"), 0x2)
    ctypes.windll.kernel32.SetFileAttributesW(str(base / "System"), 0x4)
    scan_library(store, lib)
    assert set(performers(store, lib)) == {"Alice"}


def test_an_unreadable_attribute_rejects_the_whole_snapshot(
    store, tmp_path, monkeypatch
):
    lib = actor_library(store, tmp_path, "Alice", "Bob")
    scan_library(store, lib)
    before = performers(store, lib)
    real = library_scan._hidden

    def flaky(entry):
        if entry.name == "Bob":
            raise PermissionError("attributes")
        return real(entry)

    monkeypatch.setattr(library_scan, "_hidden", flaky)
    shutil.rmtree(Path(store.library(lib)["path"]) / "Alice")
    result = scan_library(store, lib)
    assert not result.ok and result.error
    after = performers(store, lib)
    assert [dict(r) for r in after.values()] == [dict(r) for r in before.values()]
    assert store.library(lib)["last_scan_ok"] is False


def test_an_unreachable_root_writes_nothing_but_its_status(store, tmp_path):
    lib = actor_library(store, tmp_path, "Alice", films=1)
    scan_library(store, lib)
    first = store.library(lib)
    base = Path(first["path"])
    base.rename(tmp_path / "offline")
    result = scan_library(store, lib)
    assert (result.ok, result.added, result.missing) == (False, 0, 0)
    (row,) = performers(store, lib).values()
    assert row["is_missing"] == 0
    info = store.library(lib)
    assert info["last_scan_ok"] is False
    assert (info["film_count"], info["free_bytes"]) == (
        first["film_count"],
        first["free_bytes"],
    )
    (tmp_path / "offline").rename(base)
    assert scan_library(store, lib).ok and store.library(lib)["last_scan_ok"] is True


def test_a_case_only_rename_keeps_the_row(store, tmp_path):
    lib = actor_library(store, tmp_path, "alice")
    scan_library(store, lib)
    old = performers(store, lib)["alice"]
    base = Path(store.library(lib)["path"])
    os.rename(base / "alice", base / "ALICE")
    result = scan_library(store, lib)
    assert (result.added, result.refreshed, result.missing) == (0, 1, 0)
    new = performers(store, lib)["ALICE"]
    assert new["id"] == old["id"] and new["first_seen_at"] == old["first_seen_at"]
    assert (new["name"], new["path"]) == ("ALICE", str(base / "ALICE"))


def test_a_vanished_folder_is_marked_missing_and_clears_on_return(store, tmp_path):
    lib = actor_library(store, tmp_path, "Alice", "Bob")
    scan_library(store, lib)
    bob = performers(store, lib)["Bob"]["id"]
    base = Path(store.library(lib)["path"])
    (base / "Bob").rename(tmp_path / "Bob-away")
    assert scan_library(store, lib).missing == 1
    assert scan_library(store, lib).missing == 0  # counts newly missing only
    row = performers(store, lib)["Bob"]
    assert (row["id"], row["is_missing"]) == (bob, 1)
    assert store.library(lib)["missing_count"] == 1
    (tmp_path / "Bob-away").rename(base / "Bob")
    scan_library(store, lib)
    assert performers(store, lib)["Bob"]["is_missing"] == 0


def test_a_category_scan_ignores_film_folder_contents(store, tmp_path, monkeypatch):
    lib = store.add_library("category", str(root(tmp_path, "cat", "FC2-1", "FC2-2")))
    real = library_scan.list_dirs

    def flaky(path):
        if os.path.basename(path.rstrip(os.sep + "/")) == "FC2-1":
            raise OSError("unlistable")
        return real(path)

    monkeypatch.setattr(library_scan, "list_dirs", flaky)
    result = scan_library(store, lib["id"])
    assert (result.ok, result.film_count) == (True, 2)
    assert store.library(lib["id"])["last_scan_ok"] in (1, True)


def test_a_failed_commit_rolls_back_and_the_next_write_works(store, tmp_path):
    class Flaky:
        def __init__(self, conn):
            self.conn, self.failed = conn, False

        def execute(self, sql, *args):
            if sql == "COMMIT" and not self.failed:
                self.failed = True
                raise sqlite3.OperationalError("database is locked")
            return self.conn.execute(sql, *args)

        def __getattr__(self, name):
            return getattr(self.conn, name)

    real = store._conn
    store._conn = Flaky(real)
    with pytest.raises(sqlite3.OperationalError):
        with store.transaction() as conn:
            conn.execute("UPDATE settings SET value=value")
    assert not real.in_transaction
    store._conn = real
    store.add_library("actor", str(root(tmp_path, "after")))


def test_a_category_scan_counts_films_and_records_no_performers(store, tmp_path):
    lib = store.add_library(
        "category", str(root(tmp_path, "cat", "FC2-1", "FC2-2", ".x"))
    )
    result = scan_library(store, lib["id"])
    assert (result.ok, result.added, result.film_count) == (True, 0, 2)
    assert performers(store, lib["id"]) == {}
    assert store.library(lib["id"])["film_count"] == 2


def test_free_space_failure_is_recorded_as_unknown(store, tmp_path, monkeypatch):
    lib = actor_library(store, tmp_path, "Alice")

    def fail(_path):
        raise OSError("no")

    monkeypatch.setattr(library_scan.shutil, "disk_usage", fail)
    assert scan_library(store, lib).ok
    assert store.library(lib)["free_bytes"] is None


def test_an_error_while_writing_rolls_the_library_back(store, tmp_path, monkeypatch):
    lib = actor_library(store, tmp_path, "Alice", "Bob")
    names = []

    def boom(name):
        names.append(name)
        if len(names) == 2:
            raise RuntimeError("boom")
        return normalize(name)

    monkeypatch.setattr(library_scan, "normalize", boom)
    with pytest.raises(RuntimeError):
        scan_library(store, lib)
    assert performers(store, lib) == {}
    assert store.library(lib)["last_scan_at"] is None


def test_a_removed_library_scans_as_not_found(store, tmp_path):
    lib = actor_library(store, tmp_path, "Alice")
    store.remove_library(lib)
    expect(404, scan_library, store, lib)


# ── thumbnails (§6) ──────────────────────────────────────────────────────
def jpeg_file(path: Path, size=(1200, 1600), color=(200, 30, 30)) -> None:
    image = pytest.importorskip("PIL.Image")
    image.new("RGB", size, color).save(path, format="JPEG")


def thumb_row(store, lib, name):
    return performers(store, lib)[name]


def test_folder_jpg_becomes_a_512_rgb_jpeg_thumb(store, tmp_path, monkeypatch):
    image = pytest.importorskip("PIL.Image")
    lib = actor_library(store, tmp_path, "Alice", "Bob")
    base = Path(store.library(lib)["path"])
    jpeg_file(base / "Alice" / "folder.jpg")
    scan_library(store, lib)
    row = thumb_row(store, lib, "Alice")
    with image.open(io.BytesIO(row["thumb"])) as img:
        assert (img.format, img.mode, max(img.size)) == ("JPEG", "RGB", 512)
        assert img.size == (384, 512)
    assert row["thumb_side"] == 512
    assert row["thumb_mtime"] == os.stat(base / "Alice" / "folder.jpg").st_mtime
    assert thumb_row(store, lib, "Bob")["thumb"] is None

    built = []
    real = library_scan.make_thumb
    monkeypatch.setattr(
        library_scan, "make_thumb", lambda m, p: built.append(p) or real(m, p)
    )
    scan_library(store, lib)
    assert built == []  # same mtime, same side
    with store.transaction() as conn:
        conn.execute("UPDATE performers SET thumb_side=256")
    scan_library(store, lib)
    assert len(built) == 1  # a different side rebuilds
    jpg = base / "Alice" / "folder.jpg"
    os.utime(jpg, (time.time(), os.stat(jpg).st_mtime + 10))
    scan_library(store, lib)
    assert len(built) == 2  # a changed mtime rebuilds
    jpg.unlink()
    scan_library(store, lib)
    row = thumb_row(store, lib, "Alice")
    assert (row["thumb"], row["thumb_mtime"], row["thumb_side"]) == (None, None, None)


def test_an_unreadable_folder_jpg_keeps_the_old_thumb(store, tmp_path, caplog):
    pytest.importorskip("PIL.Image")
    lib = actor_library(store, tmp_path, "Alice")
    jpg = Path(store.library(lib)["path"]) / "Alice" / "folder.jpg"
    jpeg_file(jpg)
    scan_library(store, lib)
    old = thumb_row(store, lib, "Alice")
    jpg.write_bytes(b"not a jpeg")
    os.utime(jpg, (time.time(), old["thumb_mtime"] + 10))
    with caplog.at_level(logging.WARNING, logger=library_scan.__name__):
        scan_library(store, lib)
    row = thumb_row(store, lib, "Alice")
    assert (row["thumb"], row["thumb_mtime"]) == (old["thumb"], old["thumb_mtime"])
    assert "thumbnail failed" in caplog.text


def test_a_stat_error_on_folder_jpg_keeps_the_old_thumb(store, tmp_path, monkeypatch):
    pytest.importorskip("PIL.Image")
    lib = actor_library(store, tmp_path, "Alice")
    jpg = Path(store.library(lib)["path"]) / "Alice" / "folder.jpg"
    jpeg_file(jpg)
    scan_library(store, lib)
    old = thumb_row(store, lib, "Alice")
    real = os.stat

    def deny(path, *a, **kw):
        if str(path).endswith("folder.jpg"):
            raise PermissionError("denied")
        return real(path, *a, **kw)

    monkeypatch.setattr(library_scan.os, "stat", deny)
    scan_library(store, lib)
    monkeypatch.undo()
    row = thumb_row(store, lib, "Alice")
    assert (row["thumb"], row["thumb_mtime"]) == (old["thumb"], old["thumb_mtime"])


def test_without_pillow_thumbnails_are_skipped(store, tmp_path, monkeypatch):
    monkeypatch.setattr(library_scan, "_pillow", lambda: None)
    lib = actor_library(store, tmp_path, "Alice")
    (Path(store.library(lib)["path"]) / "Alice" / "folder.jpg").write_bytes(b"x")
    assert scan_library(store, lib).added == 1
    assert thumb_row(store, lib, "Alice")["thumb"] is None


# ── search (§6) ──────────────────────────────────────────────────────────
@pytest.fixture
def library(store, tmp_path):
    """Two actor libraries; 仓木华 in both (one name group, two locations)."""
    a = actor_library(store, tmp_path, "倉木華", "Alice", "葵・つかさ", "X", name="a")
    b = actor_library(store, tmp_path, "仓木华", "Bob_1", name="b")
    scan_library(store, a)
    scan_library(store, b)
    return store, a, b


def names(result):
    return [item["name"] for item in result["items"]]


def test_search_matches_contains_and_reverse_containment(library):
    store, a, b = library
    result = store.search("仓木")
    assert sorted(names(result)) == ["仓木华", "倉木華"] and result["total"] == 2
    item = result["items"][0]
    assert set(item) >= {
        "id",
        "name",
        "source_label",
        "unc_path",
        "has_thumb",
        "aliases",
    }
    assert item["fuzzy"] is False and item["library_id"] in (a, b)
    # the query contains the name (≥2 characters); a single-character name does not
    assert sorted(names(store.search("仓木华 ABC-123"))) == ["仓木华", "倉木華"]
    assert names(store.search("X ABC")) == []
    assert names(store.search("x")) == ["X"]


def test_search_escapes_like_wildcards(library):
    store, _, _ = library
    assert names(store.search("%")) == []
    assert names(store.search("b_1")) == ["Bob_1"]
    assert names(store.search("o_")) == []


def test_an_alias_hit_returns_every_location_of_the_group(library):
    store, a, _ = library
    alice = store.search("alice")["items"][0]
    store.add_alias(alice["id"], "爱丽丝")
    result = store.search("爱丽丝")
    assert names(result) == ["Alice"]
    assert (result["items"][0]["aliases"], len(result["items"][0]["alias_ids"])) == (
        ["爱丽丝"],
        1,
    )
    kura = store.search("仓木华")["items"][0]
    store.add_alias(kura["id"], "Hana")
    assert sorted(names(store.search("hana"))) == ["仓木华", "倉木華"]
    assert names(store.search("hana", aliases=False)) == []


def test_fuzzy_hits_are_listed_and_flagged(library):
    store, _, _ = library
    assert names(store.search("葵つかさ")) == []
    result = store.search("葵 つかさ", fuzzy=True)
    assert names(result) == ["葵・つかさ"] and result["items"][0]["fuzzy"] is True
    exact = store.search("葵・つかさ", fuzzy=True)
    assert exact["items"][0]["fuzzy"] is False


def test_missing_performers_are_excluded_unless_asked(library, tmp_path):
    store, a, _ = library
    (tmp_path / "a" / "Alice").rename(tmp_path / "gone")
    scan_library(store, a)
    assert names(store.search("alice")) == []
    (item,) = store.search("alice", include_missing=True)["items"]
    assert item["is_missing"] is True


def test_filters_library_and_paging(library, tmp_path):
    store, a, b = library
    assert sorted(names(store.search(filter="multi"))) == ["仓木华", "倉木華"]
    alice = store.search("alice")["items"][0]
    store.add_alias(alice["id"], "A")
    assert names(store.search(filter="alias")) == ["Alice"]
    assert len(names(store.search(filter="nothumb"))) == 6
    (tmp_path / "b" / "Bob_1").rename(tmp_path / "gone")
    scan_library(store, b)
    assert names(store.search(filter="missing")) == ["Bob_1"]
    assert sorted(names(store.search(library_id=b, include_missing=True))) == [
        "Bob_1",
        "仓木华",
    ]
    expect(422, store.search, library_id="nope")
    expect(422, store.search, filter="odd")
    expect(422, store.search, page_size=201)
    expect(422, store.search, page=0)
    everyone = store.search(page_size=200)["items"]
    assert [(i["name"]) for i in everyone] == [
        i["name"]
        for i in sorted(everyone, key=lambda i: (normalize(i["name"]), i["id"]))
    ]
    page2 = store.search(page=2, page_size=2)
    assert (page2["total"], page2["page"], names(page2)) == (
        5,
        2,
        [i["name"] for i in everyone[2:4]],
    )


def test_performer_detail_lists_every_location(library):
    store, a, b = library
    item = store.search("倉木華")["items"][0]
    store.add_alias(item["id"], "Hana")
    detail = store.performer(item["id"])
    assert detail["id"] == item["id"]
    assert {loc["library_id"] for loc in detail["locations"]} == {a, b}
    assert detail["aliases"] == ["Hana"]
    expect(404, store.performer, "nope")


def test_thumb_returns_bytes_and_the_etag(store, tmp_path):
    pytest.importorskip("PIL.Image")
    lib = actor_library(store, tmp_path, "Alice")
    jpeg_file(Path(store.library(lib)["path"]) / "Alice" / "folder.jpg")
    scan_library(store, lib)
    row = thumb_row(store, lib, "Alice")
    data, etag = store.thumb(row["id"])
    assert data == row["thumb"] and etag == f'"{row["thumb_mtime"]}-512"'
    assert store.thumb("nope") is None


# ── aliases (§6) ─────────────────────────────────────────────────────────
def test_alias_rules(library):
    store, _, _ = library
    alice = store.search("alice")["items"][0]["id"]
    added = store.add_alias(alice, "  Ali  ")
    assert added["alias"] == "Ali" and isinstance(added["id"], int)
    expect(422, store.add_alias, alice, "   ")
    expect(409, store.add_alias, alice, "ＡＬＩＣＥ")  # normalizes to the name
    expect(409, store.add_alias, alice, "ali")  # the same alias_norm
    expect(404, store.add_alias, "nope", "x")
    store.delete_alias(added["id"])
    expect(404, store.delete_alias, added["id"])


# ── rules (§8) ───────────────────────────────────────────────────────────
def test_rules_defaults_and_partial_update(tmp_path):
    store = LibraryStore.open(tmp_path / "library.db")
    rules = store.rules()
    assert rules == {
        "multi_location": "priority",
        "alias_match": True,
        "preselect_single": True,
        "fuzzy": True,
        "allow_new_actor": True,
        "new_actor_library": None,
        "new_actor_name": "actor",
        "category_prefix": True,
        "second_target_hint": True,
        "category_always": True,
        "scan_on_start": True,
        "scan_every_hours": 6,
    }
    updated = store.put_rules(
        {"fuzzy": False, "scan_every_hours": 0, "new_actor_name": "ask"}
    )
    assert (
        updated["fuzzy"],
        updated["scan_every_hours"],
        updated["new_actor_name"],
    ) == (
        False,
        0,
        "ask",
    )
    assert updated["alias_match"] is True
    store.close()
    again = LibraryStore.open(tmp_path / "library.db")
    try:
        assert again.rules()["scan_every_hours"] == 0
    finally:
        again.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"unknown": True},
        {"fuzzy": 1},
        {"scan_every_hours": 3},
        {"scan_every_hours": True},
        {"multi_location": "first"},
        {"new_actor_name": None},
        {"new_actor_library": 5},
    ],
)
def test_invalid_rules_are_refused(store, changes):
    expect(422, store.put_rules, changes)
    assert store.rules()["fuzzy"] is True


def test_new_actor_library_defaults_to_the_first_archive_actor_library(store, tmp_path):
    readonly = store.add_library("actor", str(root(tmp_path, "r")), role="readonly")[
        "id"
    ]
    first = actor_library(store, tmp_path, name="a")
    second = actor_library(store, tmp_path, name="b")
    category = store.add_library("category", str(root(tmp_path, "c")))["id"]
    assert store.rules()["new_actor_library"] == first
    assert store.put_rules({"new_actor_library": second})["new_actor_library"] == second
    for bad in (readonly, category, "nope"):
        expect(422, store.put_rules, {"new_actor_library": bad})
    store.remove_library(second)
    assert store.rules()["new_actor_library"] == first


# ── the scanner thread and its schedule (§6) ─────────────────────────────
class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_start_scans_everything_when_scan_on_start(store, tmp_path):
    a = actor_library(store, tmp_path, "Alice", name="a")
    b = actor_library(store, tmp_path, name="b")
    scanner = LibraryScanner(store)
    scanner.start()
    try:
        deadline = time.monotonic() + 10
        while len(scanner.status()["results"]) < 2:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        results = scanner.status()["results"]
        assert results[a]["ok"] and results[a]["added"] == 1 and results[b]["ok"]
    finally:
        assert scanner.stop(timeout=10)


def test_start_does_not_scan_when_scan_on_start_is_off(store, tmp_path):
    actor_library(store, tmp_path, "Alice")
    store.put_rules({"scan_on_start": False})
    scanner = LibraryScanner(store)
    scanner.start()
    try:
        time.sleep(0.2)
        assert scanner.status() == {"running": None, "queued": [], "results": {}}
    finally:
        assert scanner.stop(timeout=10)


def test_the_schedule_scans_every_n_hours(store, tmp_path):
    lib = actor_library(store, tmp_path, "Alice")
    clock = Clock()
    scanner = LibraryScanner(store, clock=clock)
    clock.now += 6 * 3600 - 1
    scanner.poll()
    assert scanner.status()["queued"] == []
    clock.now += 1
    scanner.poll()
    assert scanner.status()["queued"] == [lib]
    assert scanner.run_next() and not scanner.run_next()
    store.put_rules({"scan_every_hours": 0})
    clock.now += 100 * 3600
    scanner.poll()
    assert scanner.status()["queued"] == []
    store.put_rules({"scan_every_hours": 2})
    scanner.poll()
    assert scanner.status()["queued"] == [lib]


def test_an_unreachable_library_is_reprobed_every_15_minutes(store, tmp_path):
    store.put_rules({"scan_every_hours": 0})
    lib = actor_library(store, tmp_path, "Alice")
    base = Path(store.library(lib)["path"])
    base.rename(tmp_path / "offline")
    clock = Clock()
    scanner = LibraryScanner(store, clock=clock)
    scanner.request(lib)
    scanner.run_next()
    assert scanner.status()["results"][lib]["ok"] is False
    clock.now += 15 * 60
    scanner.poll()  # still offline: probed, not queued
    assert scanner.status()["queued"] == []
    (tmp_path / "offline").rename(base)
    clock.now += 15 * 60 - 1
    scanner.poll()
    assert scanner.status()["queued"] == []  # not yet due
    clock.now += 1
    scanner.poll()
    assert scanner.status()["queued"] == [lib]
    scanner.run_next()
    assert scanner.status()["results"][lib]["ok"] is True
    assert store.library(lib)["last_scan_ok"] is True


def test_requests_queue_once_and_a_removed_library_reports_an_error(store, tmp_path):
    lib = actor_library(store, tmp_path, "Alice")
    scanner = LibraryScanner(store)
    assert scanner.request(lib) and not scanner.request(lib)
    store.remove_library(lib)
    assert scanner.run_next()
    assert scanner.status()["results"][lib] == {
        "library_id": lib,
        "ok": False,
        "error": "片库不存在",
    }


# ── Filmpaw import (§7) ──────────────────────────────────────────────────
FILMPAW_DDL = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
INSERT INTO schema_version VALUES (4);
CREATE TABLE sources (id INTEGER PRIMARY KEY, unc_path TEXT NOT NULL UNIQUE,
  label TEXT, last_scan_at TEXT);
CREATE TABLE performers (id TEXT PRIMARY KEY, name TEXT NOT NULL,
  name_norm TEXT NOT NULL, source_id INTEGER NOT NULL, folder_name TEXT NOT NULL,
  unc_path TEXT NOT NULL UNIQUE, first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL, is_missing INTEGER NOT NULL DEFAULT 0);
CREATE TABLE aliases (id INTEGER PRIMARY KEY, name_norm TEXT NOT NULL,
  alias TEXT NOT NULL, alias_norm TEXT NOT NULL, UNIQUE(name_norm, alias_norm));
"""


def fake_filmpaw(tmp_path: Path, roots: list[tuple[str, str | None, int]]) -> Path:
    """A Filmpaw `library.db` built here; the real one is never opened."""
    folder = tmp_path / "Filmpaw"
    folder.mkdir()
    db = folder / "library.db"
    conn = sqlite3.connect(db)
    conn.executescript(FILMPAW_DDL)
    for source_id, (path, label, count) in enumerate(roots, start=1):
        conn.execute(
            "INSERT INTO sources(id, unc_path, label) VALUES (?,?,?)",
            (source_id, path, label),
        )
        for n in range(count):
            conn.execute(
                "INSERT INTO performers VALUES (?,?,?,?,?,?,?,?,0)",
                (f"p{source_id}-{n}", "n", "n", source_id, "n", f"{path}{n}", "t", "t"),
            )
    conn.executemany(
        "INSERT INTO aliases(name_norm, alias, alias_norm) VALUES (?,?,?)",
        [("仓木华", "Hana", "hana"), ("alice", "爱丽丝", "爱丽丝")],
    )
    conn.commit()
    conn.close()
    return db


def test_import_preview_import_and_reimport(store, tmp_path):
    existing = actor_library(store, tmp_path, "Alice", name="mine")
    store.update_library(existing, label="我的")
    scan_library(store, existing)
    alice = performers(store, existing)["Alice"]["id"]
    store.add_alias(alice, "爱丽丝")
    mine = store.library(existing)["path"]
    new_root = normalize_path(str(tmp_path / "nas1"))
    nested = normalize_path(str(tmp_path / "mine" / "Alice"))
    db = fake_filmpaw(
        tmp_path,
        [(new_root, "NAS 1", 3), (mine, "same", 1), (nested, None, 0)],
    )
    before = (db.read_bytes(), os.stat(db).st_mtime_ns)

    view = library_import.preview(store, str(db))
    assert view["path"] == str(db)
    assert [(r["path"], r["performers"], r["status"]) for r in view["roots"]] == [
        (new_root, 3, "new"),
        (mine, 1, "exists"),
        (nested, 0, "overlap"),
    ]
    assert (view["aliases"], view["new_aliases"]) == (2, 1)

    done = library_import.import_filmpaw(store, str(db.parent))  # the folder works
    (added,) = done["added"]
    assert [s["path"] for s in done["skipped"]] == [mine, nested]
    assert done["aliases_added"] == 1
    lib = store.library(added)
    assert (lib["kind"], lib["role"], lib["label"], lib["path"], lib["position"]) == (
        "actor",
        "archive",
        "NAS 1",
        new_root,
        2,
    )
    assert store.library(existing)["label"] == "我的"  # never changed
    assert performers(store, added) == {}  # performer rows are not copied
    with store.reading() as conn:
        assert conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0] == 2

    again = library_import.import_filmpaw(store, str(db))
    assert (again["added"], again["aliases_added"]) == ([], 0)
    assert len(again["skipped"]) == 3
    assert (db.read_bytes(), os.stat(db).st_mtime_ns) == before


def test_import_defaults_to_appdata_filmpaw(store, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert library_import.default_filmpaw_db() == tmp_path / "Filmpaw" / "library.db"
    expect(404, library_import.preview, store, None)
    fake_filmpaw(tmp_path, [(normalize_path(str(tmp_path / "x")), "X", 0)])
    assert len(library_import.preview(store, None)["roots"]) == 1


def test_import_refuses_a_file_that_is_not_filmpaw(store, tmp_path):
    other = tmp_path / "other.db"
    conn = sqlite3.connect(other)
    conn.execute("CREATE TABLE t (x)")
    conn.commit()
    conn.close()
    expect(422, library_import.preview, store, str(other))
    (tmp_path / "junk.db").write_bytes(b"junk" * 100)
    expect(422, library_import.import_filmpaw, store, str(tmp_path / "junk.db"))
