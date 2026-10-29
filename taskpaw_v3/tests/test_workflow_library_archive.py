"""#363 L2a: the library archive engine (#361 §9.1–§9.3) on temporary folders only.

A "NAS" is a temp directory; the package is a temp folder with real bytes. The
worker runs inline (`run_next`) except where its thread is the subject.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
import time
import unicodedata
from pathlib import Path

import pytest

from taskpaw_v3.workflow import library_archive as la
from taskpaw_v3.workflow.library_archive import (
    CONFLICT,
    MISMATCH,
    STAGING_PREFIX,
    LibraryArchive,
    LibraryRefused,
    LibraryUnavailable,
)
from taskpaw_v3.workflow.library_store import (
    LibraryStore,
    insert_library,
    normalize,
    now_iso,
)

FILM = "ABC-123"
FINAL = "f" * 64


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Rig:
    """A library with performer `actor` at `nas/Actor` and one package."""

    def __init__(self, tmp_path: Path):
        self.tmp = tmp_path
        self.root = tmp_path / "nas"
        self.actor = self.root / "Actor"
        self.actor.mkdir(parents=True)
        self.store = LibraryStore.open(tmp_path / "library.db")
        with self.store.transaction() as conn:
            self.library_id = insert_library(conn, "actor", str(self.root), label="NAS")
            self.add_performer(conn, "actor", self.actor)
        self.archive = LibraryArchive(self.store)
        self.package = tmp_path / "out" / FILM
        self.contents = {
            f"{FILM}.mp4": b"video bytes " * 1000,
            f"{FILM}.nfo": b"<movie/>",
            "extrafanart/fanart1.jpg": b"jpeg",
        }
        for path, data in self.contents.items():
            (self.package / path).parent.mkdir(parents=True, exist_ok=True)
            (self.package / path).write_bytes(data)
        os.utime(self.package / f"{FILM}.nfo", (1_600_000_000, 1_600_000_000))
        self.files = [
            {"path": path, "size": len(data), "sha256": _sha(data)}
            for path, data in self.contents.items()
        ]

    def add_performer(self, conn, performer_id: str, path: Path) -> None:
        now = now_iso()
        conn.execute(
            "INSERT INTO performers(id, name, name_norm, library_id, folder_name,"
            " path, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                performer_id,
                path.name,
                normalize(path.name),
                self.library_id,
                path.name,
                str(path),
                now,
                now,
            ),
        )

    def submit(self, op: str = "op-1", archive: LibraryArchive | None = None, **kw):
        archive = archive or self.archive
        proof = archive.target("actor", FILM)
        return archive.archive(
            op,
            self.package,
            "actor",
            proof["expected_target"],
            kw.pop("files", self.files),
            final_hash=kw.pop("final_hash", FINAL),
            **kw,
        )

    def run(self, op: str = "op-1", archive: LibraryArchive | None = None):
        archive = archive or self.archive
        while archive.run_next():
            pass
        return archive.lookup(op)

    def events(self, op: str = "op-1") -> list[tuple[str, int, str]]:
        with self.store.reading() as conn:
            return [
                (row["action"], row["target_index"], row["outcome"])
                for row in conn.execute(
                    "SELECT action, target_index, outcome FROM archive_events"
                    " WHERE operation_id=? ORDER BY id",
                    (op,),
                )
            ]

    def archived(self) -> list[dict]:
        with self.store.reading() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM archived_targets")]


@pytest.fixture
def rig(tmp_path):
    value = Rig(tmp_path)
    try:
        yield value
    finally:
        value.store.close()


def _identity(path: Path) -> list[str]:
    st = os.lstat(path)
    return [str(st.st_dev), str(st.st_ino)]


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
    }


def _refused(code: str, fn, *args, **kwargs) -> LibraryRefused:
    with pytest.raises(LibraryRefused) as caught:
        fn(*args, **kwargs)
    assert (caught.value.status, caught.value.code) == (412, code)
    return caught.value


# ── target proof (§9.1, §9.2) ───────────────────────────────────────────
def test_the_proof_names_the_parent_its_identity_and_whether_the_film_is_free(rig):
    proof = rig.archive.target("actor", FILM)
    assert proof == {
        "target_path": str(rig.actor / FILM),
        "absent": True,
        "archived": False,
        "expected_target": {
            "target_path": str(rig.actor),
            "directory_identity": _identity(rig.actor),
        },
    }
    assert all(
        isinstance(v, str) for v in proof["expected_target"]["directory_identity"]
    )
    (rig.actor / f"{STAGING_PREFIX}x-y").mkdir()  # the engine's own staging
    assert rig.archive.target("actor", FILM)["absent"] is True
    (rig.actor / FILM.lower()).mkdir()  # casefold
    taken = rig.archive.target("actor", FILM)
    assert (taken["absent"], taken["archived"]) == (False, False)


def test_a_manual_target_is_any_absolute_directory(rig):
    manual = rig.tmp / "manual"
    manual.mkdir()
    proof = rig.archive.target(None, FILM, target_dir=str(manual))
    assert proof["expected_target"]["target_path"] == str(manual)
    assert proof["absent"] is True
    _refused("invalid", rig.archive.target, None, FILM, target_dir="relative/dir")
    _refused("missing", rig.archive.target, None, FILM, target_dir=str(rig.tmp / "no"))
    with pytest.raises(ValueError):
        rig.archive.target("actor", FILM, target_dir=str(manual))


def test_performer_refusals_carry_their_codes(rig):
    _refused("missing", rig.archive.target, "nobody", FILM)
    with rig.store.transaction() as conn:
        conn.execute("UPDATE performers SET is_missing=1 WHERE id='actor'")
    _refused("missing", rig.archive.target, "actor", FILM)
    with rig.store.transaction() as conn:
        conn.execute("UPDATE performers SET is_missing=0")
        conn.execute("UPDATE libraries SET role='readonly'")
    assert "只读" in _refused("readonly", rig.archive.target, "actor", FILM).message


@pytest.mark.parametrize(
    "name",
    [
        "",
        ".",
        "..",
        "trailing.",
        "trailing ",
        "a:b",
        "a/b",
        'a"b',
        "CON",
        "lpt1.txt",
        f"{STAGING_PREFIX}x",
        "tab\tname",
        unicodedata.normalize("NFD", "café"),
    ],
)
def test_an_invalid_film_folder_name_is_refused_before_the_nas(rig, name):
    _refused("invalid", rig.archive.target, "actor", name)


def test_a_parent_that_is_a_file_is_missing(rig, tmp_path):
    plain = tmp_path / "file"
    plain.write_bytes(b"x")
    _refused("missing", rig.archive.target, None, FILM, target_dir=str(plain))


@pytest.mark.skipif(sys.platform != "win32", reason="a junction is a reparse point")
def test_a_junction_parent_is_refused(rig, tmp_path):
    import _winapi

    link = tmp_path / "junction"
    _winapi.CreateJunction(str(rig.actor), str(link))
    _refused("invalid", rig.archive.target, None, FILM, target_dir=str(link))


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (TimeoutError(errno.ETIMEDOUT, "timed out"), "wait"),
        (OSError(errno.ENETUNREACH, "unreachable"), "wait"),
        (PermissionError(errno.EACCES, "denied"), "permission"),
        (OSError(errno.EIO, "io"), "missing"),
    ],
)
def test_only_a_retryable_os_error_waits(rig, monkeypatch, error, kind):
    def lstat(path, *args, **kwargs):
        raise error

    monkeypatch.setattr(la.os, "lstat", lstat)
    if kind == "wait":
        with pytest.raises(LibraryUnavailable):
            rig.archive.target("actor", FILM)
    else:
        _refused(kind, rig.archive.target, "actor", FILM)


def test_a_windows_network_error_waits_even_when_mapped_to_enoent():
    error = FileNotFoundError(errno.ENOENT, "gone")
    error.winerror = 53  # type: ignore[attr-defined]  # ERROR_BAD_NETPATH
    assert la.retryable(error)
    error.winerror = 2  # type: ignore[attr-defined]  # ERROR_FILE_NOT_FOUND
    assert not la.retryable(error)


def test_an_unopened_library_is_unavailable(rig):
    closed = LibraryArchive(None, "片库启动失败：x")
    for call in (
        lambda: closed.target("actor", FILM),
        lambda: closed.lookup("op"),
        lambda: closed.performers("a"),
        lambda: closed.thumb("actor"),
    ):
        with pytest.raises(LibraryUnavailable, match="片库启动失败"):
            call()


# ── the task (§9.3) ─────────────────────────────────────────────────────
def test_an_archive_copies_verifies_commits_and_returns_a_receipt(rig):
    queued = rig.submit()
    assert (queued["status"], queued["phase"]) == ("queued", "queued")
    assert rig.archive.lookup("never") is None
    task = rig.run()
    assert task["status"] == "completed" and task["phase"] == "finished", task
    folder = rig.actor / FILM
    assert _tree(folder) == rig.contents
    assert not [p for p in rig.actor.iterdir() if p.name.startswith(STAGING_PREFIX)]
    nfo = os.stat(folder / f"{FILM}.nfo")
    assert int(nfo.st_mtime) == 1_600_000_000  # mtime restored
    receipt = task["receipt"]
    assert receipt == {
        "task_id": task["id"],
        "target_path": str(folder),
        "directory_identity": _identity(folder),
        "files": sorted(rig.files, key=lambda f: f["path"]),
        "second": None,
    }
    assert (task["completed_count"], task["total_count"]) == (1, 1)
    assert task["progress_bytes"] == task["verified_bytes"] == task["total_bytes"]
    (row,) = rig.archived()
    assert row["target_key"] == str(folder).casefold()
    assert row["final_hash"] == FINAL and row["operation_id"] == "op-1"
    assert json.loads(row["directory_identity"]) == _identity(rig.actor)
    assert rig.events() == [
        (action, 0, outcome)
        for action in ("copy", "verify", "commit")
        for outcome in ("started", "succeeded")
    ]
    assert rig.package.is_dir() and _tree(rig.package) == rig.contents  # kept
    # the archived record makes the next proof of this package `archived`
    again = rig.archive.target("actor", FILM, final_hash=FINAL)
    assert (again["absent"], again["archived"]) == (False, True)
    assert rig.archive.target("actor", FILM, final_hash="e" * 64)["archived"] is False


def test_the_same_operation_answers_its_snapshot_a_different_payload_is_refused(rig):
    first = rig.submit()
    assert rig.submit() == first
    rig.run()
    done = rig.archive.lookup("op-1")
    assert rig.submit()["status"] == "completed" == done["status"]
    changed = [dict(rig.files[0], size=1), *rig.files[1:]]
    _refused("changed", rig.submit, files=changed)
    assert len(rig.events()) == 6  # copied once


def test_a_changed_target_identity_is_refused_before_a_task_exists(rig):
    proof = rig.archive.target("actor", FILM)
    rig.actor.rename(rig.actor.with_name("Actor-old"))
    rig.actor.mkdir()
    _refused(
        "changed",
        rig.archive.archive,
        "op-1",
        rig.package,
        "actor",
        proof["expected_target"],
        rig.files,
    )
    assert rig.archive.lookup("op-1") is None


def test_a_filmpaw_era_integer_identity_is_the_same_identity(rig, tmp_path):
    manual = tmp_path / "manual"
    manual.mkdir()
    st = os.lstat(manual)
    expected = {
        "target_path": str(manual),
        "directory_identity": [st.st_dev, st.st_ino],
    }
    rig.archive.archive(
        "op-1", rig.package, None, expected, rig.files, target_dir=str(manual)
    )
    assert rig.run()["status"] == "completed"
    assert _tree(manual / FILM) == rig.contents


@pytest.mark.parametrize(
    "files",
    [
        [],
        [{"path": "a\\b", "size": 1, "sha256": "0" * 64}],
        [{"path": "../x", "size": 1, "sha256": "0" * 64}],
        [{"path": "x", "size": -1, "sha256": "0" * 64}],
        [{"path": "x", "size": 1, "sha256": "ABC"}],
        [{"path": "x", "size": 1, "sha256": "0" * 64}] * 2,
    ],
)
def test_an_invalid_manifest_is_refused(rig, files):
    _refused("invalid", rig.submit, files=files)


def test_a_package_that_differs_from_the_manifest_fails_before_any_copy(rig):
    (rig.package / "extra.txt").write_bytes(b"not approved")
    task = rig.run(rig.submit()["operation_id"])
    assert task["status"] == "failed" and "extra.txt" in task["message"]
    assert sorted(p.name for p in rig.actor.iterdir()) == []
    assert rig.events() == []


def test_a_same_name_folder_fails_the_task_and_is_never_touched(rig):
    rig.submit()
    (rig.actor / FILM).mkdir()
    (rig.actor / FILM / "theirs.txt").write_bytes(b"keep")
    task = rig.run()
    assert (task["status"], task["message"]) == ("failed", CONFLICT)
    assert _tree(rig.actor / FILM) == {"theirs.txt": b"keep"}
    assert rig.events() == []


def test_no_free_space_or_no_write_access_fails_before_any_copy(rig, monkeypatch):
    class Usage:
        free = 10

    monkeypatch.setattr(la.shutil, "disk_usage", lambda path: Usage())
    rig.submit()
    task = rig.run()
    assert task["status"] == "failed" and "可用空间不足" in task["message"]
    monkeypatch.undo()
    real_open = open

    def refuse_probe(path, mode="r", *args, **kwargs):
        if str(path).endswith(".probe"):
            raise PermissionError(errno.EACCES, "denied")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(la, "open", refuse_probe, raising=False)
    rig.submit("op-2")
    task = rig.run("op-2")
    assert task["status"] == "failed" and "不可写" in task["message"]
    assert list(rig.actor.iterdir()) == []


def test_a_hash_that_differs_from_the_approval_fails_and_keeps_the_staging(rig):
    data = rig.contents[f"{FILM}.nfo"]
    (rig.package / f"{FILM}.nfo").write_bytes(data.upper())  # same size
    rig.submit()
    task = rig.run()
    assert task["status"] == "failed" and "SHA-256" in task["message"]
    (staging,) = [p for p in rig.actor.iterdir() if p.name.startswith(STAGING_PREFIX)]
    assert str(staging) in task["message"]  # kept, named
    assert not (rig.actor / FILM).exists()
    assert rig.events()[-1] == ("verify", 0, "failed")
    assert rig.archived() == []


def test_a_folder_appearing_before_the_commit_is_a_conflict(rig, monkeypatch):
    real = LibraryArchive._verify

    def verify_then_race(self, *args):
        real(self, *args)
        (rig.actor / FILM).mkdir()

    monkeypatch.setattr(LibraryArchive, "_verify", verify_then_race)
    rig.submit()
    task = rig.run()
    assert (task["status"], task["message"].split("；")[0]) == ("failed", CONFLICT)
    assert list((rig.actor / FILM).iterdir()) == []
    assert rig.events()[-1] == ("commit", 0, "failed")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows rename semantics (§13)")
@pytest.mark.parametrize("non_empty", [False, True])
def test_windows_rename_never_replaces_an_existing_folder(tmp_path, non_empty):
    source, destination = tmp_path / "staging", tmp_path / "film"
    source.mkdir()
    destination.mkdir()
    if non_empty:
        (destination / "x").write_bytes(b"x")
    with pytest.raises(FileExistsError):
        os.rename(source, destination)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows rename semantics")
def test_a_rename_refused_by_an_existing_folder_is_a_conflict(rig, monkeypatch):
    real = os.rename

    def race(source, destination):
        Path(destination).mkdir()
        real(source, destination)

    monkeypatch.setattr(la.os, "rename", race)
    rig.submit()
    task = rig.run()
    assert (task["status"], task["message"].split("；")[0]) == ("failed", CONFLICT)


def test_an_error_during_the_commit_needs_review(rig, monkeypatch):
    def broken(source, destination):
        raise OSError(errno.EIO, "network name deleted")

    monkeypatch.setattr(la.os, "rename", broken)
    rig.submit()
    task = rig.run()
    assert task["status"] == "needs_review", task
    assert task["items"][0]["status"] == "needs_review"


def test_a_failed_journal_write_stops_the_worker_until_restart(rig, monkeypatch):
    real = LibraryArchive._event

    def failing(self, task, action, index, outcome, detail=None):
        if action == "verify":
            raise la._StorageFailed("disk I/O error")
        return real(self, task, action, index, outcome, detail)

    monkeypatch.setattr(LibraryArchive, "_event", failing)
    rig.submit()
    rig.submit("op-2")
    task = rig.run()
    assert task["status"] == "needs_review" and "重启" in task["message"]
    assert rig.archive.run_next() is False  # op-2 is not run
    with pytest.raises(LibraryUnavailable):
        rig.submit("op-3")
    assert rig.archive.lookup("op-2")["status"] == "queued"
    monkeypatch.undo()
    restarted = LibraryArchive(rig.store)
    restarted.recover()  # op-1 needed review already; op-2 is queued again
    assert rig.run("op-2", restarted)["status"] == "completed"


def test_the_archived_copy_of_this_package_is_reverified_not_copied(rig):
    rig.submit()
    rig.run()
    (rig.actor / FILM / "poster-by-media-server.jpg").write_bytes(b"x")
    rig.submit("op-2")
    task = rig.run("op-2")
    assert task["status"] == "completed", task
    assert "poster-by-media-server.jpg" in task["message"]
    assert task["receipt"]["target_path"] == str(rig.actor / FILM)
    assert rig.events("op-2") == []  # nothing copied
    (rig.actor / FILM / f"{FILM}.nfo").write_bytes(b"<movie>")
    rig.submit("op-3")
    task = rig.run("op-3")
    assert task["status"] == "failed" and MISMATCH in task["message"]


def test_a_same_name_folder_this_engine_did_not_commit_is_never_archived(rig):
    (rig.actor / FILM).mkdir()
    for path, data in rig.contents.items():
        (rig.actor / FILM / path).parent.mkdir(parents=True, exist_ok=True)
        (rig.actor / FILM / path).write_bytes(data)  # identical, but not ours
    assert rig.archive.target("actor", FILM, final_hash=FINAL)["archived"] is False
    rig.archive.archive(
        "op-1",
        rig.package,
        "actor",
        rig.archive.target("actor", FILM)["expected_target"],
        rig.files,
        final_hash=FINAL,
    )
    assert rig.run()["message"] == CONFLICT


# ── worker thread and restart recovery ──────────────────────────────────
def _wait(archive: LibraryArchive, op: str) -> dict:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        task = archive.lookup(op)
        if task and task["status"] not in ("queued", "running"):
            return task
        time.sleep(0.02)
    raise AssertionError(f"{op} did not finish")


def test_the_worker_thread_runs_tasks_one_at_a_time_in_order(rig, tmp_path):
    other = tmp_path / "manual"
    other.mkdir()
    rig.archive.start()
    try:
        rig.submit()
        proof = rig.archive.target(None, FILM, target_dir=str(other))
        rig.archive.archive(
            "op-2",
            rig.package,
            None,
            proof["expected_target"],
            rig.files,
            target_dir=str(other),
        )
        first, second = _wait(rig.archive, "op-1"), _wait(rig.archive, "op-2")
    finally:
        assert rig.archive.stop(timeout=10)
    assert first["status"] == second["status"] == "completed"
    with rig.store.reading() as conn:
        rows = conn.execute(
            "SELECT operation_id FROM archive_events WHERE action='copy'"
            " AND outcome='started' ORDER BY id"
        ).fetchall()
    assert [r[0] for r in rows] == ["op-1", "op-2"]


def test_a_queued_task_is_queued_again_after_a_restart(rig):
    rig.submit()  # never run: the process stopped
    restarted = LibraryArchive(rig.store)
    restarted.recover()
    assert rig.run(archive=restarted)["status"] == "completed"


def test_a_shutdown_mid_copy_fails_the_task_at_restart_and_keeps_the_staging(rig):
    rig.submit()
    rig.archive._stopping = True  # stop() during the copy: the next chunk stops
    assert rig.archive.run_next() is True
    assert rig.archive.lookup("op-1")["status"] == "running"
    assert rig.events() == [("copy", 0, "started")]
    restarted = LibraryArchive(rig.store)
    restarted.recover()
    task = restarted.lookup("op-1")
    assert (task["status"], task["phase"]) == ("failed", "finished")
    (staging,) = [p for p in rig.actor.iterdir() if p.name.startswith(STAGING_PREFIX)]
    assert str(staging) in task["message"]
    assert task["items"][0]["status"] == "failed"


def _crash_after_rename(rig, monkeypatch):
    def crash(self, task, payload, folder):
        raise la._Interrupted()  # the process died right after the rename

    monkeypatch.setattr(LibraryArchive, "_record", crash)
    rig.submit()
    rig.run()
    monkeypatch.undo()
    assert rig.events()[-1] == ("commit", 0, "started")


def test_a_commit_that_happened_before_a_crash_is_recovered(rig, monkeypatch):
    _crash_after_rename(rig, monkeypatch)
    restarted = LibraryArchive(rig.store)
    restarted.recover()
    task = restarted.lookup("op-1")
    assert task["status"] == "completed", task
    assert task["receipt"]["target_path"] == str(rig.actor / FILM)
    assert task["receipt"]["directory_identity"] == _identity(rig.actor / FILM)
    (row,) = rig.archived()
    assert json.loads(row["directory_identity"]) == _identity(rig.actor)
    assert rig.events()[-1] == ("commit", 0, "succeeded")


def test_a_commit_whose_result_cannot_be_proved_needs_review(rig, monkeypatch):
    _crash_after_rename(rig, monkeypatch)
    (rig.actor / FILM / f"{FILM}.nfo").write_bytes(b"changed!")  # same size
    restarted = LibraryArchive(rig.store)
    restarted.recover()
    task = restarted.lookup("op-1")
    assert task["status"] == "needs_review"
    assert task["receipt"] is None and rig.archived() == []


def test_recovery_never_resumes_a_task_that_was_still_preflighting(rig):
    rig.submit()
    with rig.store.transaction() as conn:
        conn.execute("UPDATE archive_tasks SET status='running', phase='preflight'")
    restarted = LibraryArchive(rig.store)
    restarted.recover()
    assert restarted.lookup("op-1")["status"] == "failed"
    assert restarted.run_next() is False
    assert list(rig.actor.iterdir()) == []
