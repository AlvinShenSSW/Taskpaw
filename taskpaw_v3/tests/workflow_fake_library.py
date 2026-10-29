"""The real archive engine over a temporary `library.db`, for tests only (#363).

`FakeLibrary` registers the given performer directories as one actor library
each parent and serves them through a real `LibraryArchive`. Inside `with` the
library is open; outside it every call answers `LibraryUnavailable`, as an
unopened library does. Archives run inline: `archive()` queues the task and the
worker runs until idle. `mode` injects failures: "lose_response" (the task runs,
then the call raises `LibraryUnavailable` once), "fail" (the preflight finds no
free space; nothing is copied).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from taskpaw_v3.workflow import library_archive
from taskpaw_v3.workflow.library_archive import LibraryArchive, LibraryUnavailable
from taskpaw_v3.workflow.library_store import (
    LibraryStore,
    insert_library,
    normalize,
    now_iso,
)


class InlineArchive(LibraryArchive):
    def __init__(self, fake: FakeLibrary):
        super().__init__(None)
        self.fake = fake

    def archive(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        self._db()  # a closed library answers before anything is counted
        self.fake.posts += 1
        snapshot = super().archive(*args, **kwargs)
        self.drain()
        if self.fake.mode == "lose_response":
            self.fake.mode = ""
            raise LibraryUnavailable("response lost")
        return snapshot

    def drain(self) -> None:
        if self.fake.mode == "fail":
            full = SimpleNamespace(total=1, used=1, free=0)
            with patch.object(library_archive.shutil, "disk_usage", return_value=full):
                while self.run_next():
                    pass
            return
        while self.run_next():
            pass


class FakeLibrary:
    def __init__(self, data_dir: Path, performers: dict[str, Path]):
        data_dir.mkdir(parents=True, exist_ok=True)
        self.store = LibraryStore.open(data_dir / "library.db")
        self.mode = ""
        self.posts = 0
        self.archive = InlineArchive(self)
        now = now_iso()
        with self.store.transaction() as conn:
            libraries: dict[Path, str] = {}
            for performer_id, path in performers.items():
                if path.parent not in libraries:
                    libraries[path.parent] = insert_library(
                        conn, "actor", str(path.parent), label="NAS"
                    )
                conn.execute(
                    "INSERT INTO performers(id, name, name_norm, library_id,"
                    " folder_name, path, first_seen_at, last_seen_at)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (
                        performer_id,
                        path.name,
                        normalize(path.name),
                        libraries[path.parent],
                        path.name,
                        str(path),
                        now,
                        now,
                    ),
                )

    def __enter__(self) -> FakeLibrary:
        self.archive.store = self.store
        return self

    def __exit__(self, *exc: Any) -> None:
        self.archive.store = None

    def close(self) -> None:
        self.store.close()

    def set_thumb(self, performer_id: str, data: bytes) -> None:
        with self.store.transaction() as conn:
            conn.execute(
                "UPDATE performers SET thumb=?, thumb_mtime=1.0, thumb_side=512"
                " WHERE id=?",
                (data, performer_id),
            )

    @property
    def copies(self) -> int:
        """Committed copies: the journal's `commit succeeded` events."""
        with self.store.reading() as conn:
            (count,) = conn.execute(
                "SELECT COUNT(*) FROM archive_events"
                " WHERE action='commit' AND outcome='succeeded'"
            ).fetchone()
        return int(count)
