"""Library scans and their schedule (#361 §6, #362 L1).

The scan rules are Filmpaw's `scan.py`: an unreachable root writes nothing but
its scan status; hidden entries are skipped and an unreadable attribute rejects
the whole snapshot; names match by casefold, a case-only rename keeps the row;
a vanished folder is marked missing, never deleted. The NAS is read outside the
store lock and each library's result is written in one transaction.
"""

from __future__ import annotations

import io
import logging
import os
import shutil
import stat
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from .library_store import LibraryError, LibraryStore, is_dir, normalize, now_iso

log = logging.getLogger(__name__)

THUMB_MAX_SIDE = 512
THUMB_QUALITY = 80
REPROBE_SECONDS = 15 * 60
IDLE_WAIT_SECONDS = 60.0


class LibraryUnreachable(Exception):
    pass


@dataclass
class ScanResult:
    library_id: str
    ok: bool
    at: str
    added: int = 0
    refreshed: int = 0
    missing: int = 0
    film_count: int = 0
    error: str | None = None


def _hidden(entry: os.DirEntry[str]) -> bool:
    if entry.name.startswith("."):
        return True
    if sys.platform == "win32":
        # an unreadable attribute raises: the caller rejects the whole snapshot
        attrs = entry.stat(follow_symlinks=False).st_file_attributes
        return bool(attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))
    return False


def list_dirs(root: str) -> list[str]:
    """First-level visible directories; any OSError means unreachable."""
    try:
        # never isdir(): it hides an offline share behind "not a directory"
        if not stat.S_ISDIR(os.stat(root).st_mode):
            raise LibraryUnreachable(root)
        with os.scandir(root) as entries:
            return sorted(
                e.name
                for e in entries
                if e.is_dir(follow_symlinks=False) and not _hidden(e)
            )
    except OSError as exc:
        raise LibraryUnreachable(root) from exc


def _pillow() -> Any:
    """PIL.Image, or None where Pillow is not installed (Python 3.10)."""
    try:
        from PIL import Image
    except ImportError:
        return None
    return Image


def make_thumb(image_module: Any, path: str) -> bytes:
    with image_module.open(path) as img:
        img.draft("RGB", (THUMB_MAX_SIDE, THUMB_MAX_SIDE))  # decode downscaled
        img = img.convert("RGB")
        img.thumbnail((THUMB_MAX_SIDE, THUMB_MAX_SIDE))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=THUMB_QUALITY)
        return buf.getvalue()


def _thumb(
    image_module: Any, folder: str, cached_mtime: Any, cached_side: Any
) -> tuple[bytes, float] | tuple[()] | None:
    """None: leave the thumb; (): clear it; (blob, mtime): replace it."""
    jpg = os.path.join(folder, "folder.jpg")
    try:
        mtime = os.stat(jpg).st_mtime
    except FileNotFoundError:  # removed: clear whatever was stored
        return () if cached_mtime is not None or cached_side is not None else None
    except OSError as exc:  # unreadable is not removed: keep the old thumb
        log.warning("thumbnail stat failed for %s: %s", jpg, exc)
        return None
    if cached_mtime == mtime and cached_side == THUMB_MAX_SIDE:
        return None
    try:
        return make_thumb(image_module, jpg), mtime
    except Exception as exc:  # unreadable or corrupt: keep the old one, retry next scan
        log.warning("thumbnail failed for %s: %s", jpg, exc)
        return None


def _free_bytes(root: str) -> int | None:
    try:
        return shutil.disk_usage(root).free
    except OSError:
        return None


def scan_library(store: LibraryStore, library_id: str) -> ScanResult:
    """Scan one library; LibraryError 404 when it is gone."""
    started, at = time.monotonic(), now_iso()
    with store.reading() as conn:
        lib = conn.execute(
            "SELECT kind, path FROM libraries WHERE id=?", (library_id,)
        ).fetchone()
        cached = {
            row["folder_name"].casefold(): (row["thumb_mtime"], row["thumb_side"])
            for row in conn.execute(
                "SELECT folder_name, thumb_mtime, thumb_side FROM performers"
                " WHERE library_id=?",
                (library_id,),
            )
        }
        known = {  # present before the listing: only these can go missing now
            row["id"]
            for row in conn.execute(
                "SELECT id FROM performers WHERE library_id=? AND is_missing=0",
                (library_id,),
            )
        }
    if lib is None:
        raise LibraryError(404, "片库不存在")
    root = lib["path"]
    result = ScanResult(library_id, ok=True, at=at)
    try:
        names = list_dirs(root)
        counts = (
            {name: len(list_dirs(os.path.join(root, name))) for name in names}
            if lib["kind"] == "actor"
            else {}
        )
    except LibraryUnreachable:
        with store.transaction() as conn:  # the status only, no performer row
            conn.execute(
                "UPDATE libraries SET last_scan_at=?, last_scan_ok=0, last_scan_ms=?"
                " WHERE id=?",
                (at, _ms(started), library_id),
            )
        result.ok, result.error = False, "片库不可达，记录未变动"
        return result
    thumbs: dict[str, tuple[bytes, float] | tuple[()] | None] = {}
    image_module = _pillow() if lib["kind"] == "actor" else None
    if image_module is not None:
        for name in names:
            mtime, side = cached.get(name.casefold(), (None, None))
            thumbs[name] = _thumb(image_module, os.path.join(root, name), mtime, side)
    free = _free_bytes(root)
    with store.transaction() as conn:
        if (
            conn.execute("SELECT 1 FROM libraries WHERE id=?", (library_id,)).fetchone()
            is None
        ):
            raise LibraryError(404, "片库不存在")
        if lib["kind"] == "actor":
            _apply_actor(
                conn, library_id, root, names, counts, thumbs, at, result, known
            )
            result.film_count = sum(counts.values())
        else:
            result.film_count = len(names)
        conn.execute(
            "UPDATE libraries SET last_scan_at=?, last_scan_ok=1, last_scan_ms=?,"
            " film_count=?, free_bytes=? WHERE id=?",
            (at, _ms(started), result.film_count, free, library_id),
        )
    return result


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _apply_actor(
    conn: Any,
    library_id: str,
    root: str,
    names: list[str],
    counts: dict[str, int],
    thumbs: dict[str, tuple[bytes, float] | tuple[()] | None],
    now: str,
    result: ScanResult,
    known: set[str],
) -> None:
    """`known`: the library's performer ids read BEFORE the root was listed;
    only those can be missing (a row born after, e.g. an archive mkdir, is not)."""
    rows = {
        row["folder_name"].casefold(): row
        for row in conn.execute(
            "SELECT id, folder_name, is_missing FROM performers WHERE library_id=?",
            (library_id,),
        )
    }
    for name in names:
        path = os.path.join(root, name)
        row = rows.get(name.casefold())
        if row is not None:
            performer_id = row["id"]
            conn.execute(
                "UPDATE performers SET last_seen_at=?, is_missing=0, film_count=?"
                " WHERE id=?",
                (now, counts[name], performer_id),
            )
            if row["folder_name"] != name:  # case-only rename: adopt the new casing
                conn.execute(
                    "UPDATE performers SET name=?, name_norm=?, folder_name=?, path=?"
                    " WHERE id=?",
                    (name, normalize(name), name, path, performer_id),
                )
            result.refreshed += 1
        else:
            performer_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO performers(id, name, name_norm, library_id, folder_name,"
                " path, first_seen_at, last_seen_at, is_missing, film_count)"
                " VALUES (?,?,?,?,?,?,?,?,0,?)",
                (
                    performer_id,
                    name,
                    normalize(name),
                    library_id,
                    name,
                    path,
                    now,
                    now,
                    counts[name],
                ),
            )
            result.added += 1
        thumb = thumbs.get(name)
        if thumb == ():
            conn.execute(
                "UPDATE performers SET thumb=NULL, thumb_mtime=NULL, thumb_side=NULL"
                " WHERE id=?",
                (performer_id,),
            )
        elif thumb:
            conn.execute(
                "UPDATE performers SET thumb=?, thumb_mtime=?, thumb_side=? WHERE id=?",
                (thumb[0], thumb[1], THUMB_MAX_SIDE, performer_id),
            )
    present = {name.casefold() for name in names}
    for key, row in rows.items():
        if key not in present and row["id"] in known and not row["is_missing"]:
            conn.execute("UPDATE performers SET is_missing=1 WHERE id=?", (row["id"],))
            result.missing += 1


class LibraryScanner:
    """The one background scanner (§4): one scan at a time, in request order.

    It scans everything at start when `scan_on_start`, again every
    `scan_every_hours`, and re-probes unreachable libraries every 15 minutes
    to scan them as soon as they answer (§6).
    """

    def __init__(
        self, store: LibraryStore, *, clock: Callable[[], float] = time.monotonic
    ):
        self.store = store
        self._clock = clock
        self._cond = threading.Condition()
        self._queue: list[str] = []
        self._running: str | None = None
        self._results: dict[str, dict[str, Any]] = {}
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._last_all = self._last_probe = clock()

    def start(self) -> None:
        if self.store.rules()["scan_on_start"]:
            self.request_all()
        self._thread = threading.Thread(
            target=self._run, name="library-scanner", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> bool:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def request(self, library_id: str) -> bool:
        """Queue one scan; False when it is already queued."""
        with self._cond:
            if library_id in self._queue:
                return False
            self._queue.append(library_id)
            self._cond.notify_all()
            return True

    def request_all(self) -> None:
        self._last_all = self._clock()
        for library_id in self.store.library_ids():
            self.request(library_id)

    def status(self) -> dict[str, Any]:
        with self._cond:
            return {
                "running": self._running,
                "queued": list(self._queue),
                "results": dict(self._results),
            }

    def poll(self) -> None:
        """Queue whatever the schedule and the re-probe say is due."""
        now = self._clock()
        hours = self.store.rules()["scan_every_hours"]
        if hours and now - self._last_all >= hours * 3600:
            self.request_all()
        if now - self._last_probe >= REPROBE_SECONDS:
            self._last_probe = now
            for library_id, path in self.store.unreachable():
                if is_dir(path):
                    self.request(library_id)

    def run_next(self) -> bool:
        """Run the oldest queued scan; False when nothing is queued."""
        with self._cond:
            if not self._queue:
                return False
            library_id = self._running = self._queue.pop(0)
        try:
            outcome = asdict(scan_library(self.store, library_id))
        except LibraryError as exc:
            outcome = {"library_id": library_id, "ok": False, "error": exc.detail}
        except Exception as exc:  # a failed write rolled back; the next scan retries
            log.exception("library scan failed: %s", library_id)
            outcome = {"library_id": library_id, "ok": False, "error": str(exc)}
        with self._cond:
            self._running = None
            self._results[library_id] = outcome
        return True

    def _run(self) -> None:
        while True:
            with self._cond:
                if self._stopping:
                    return
            try:
                self.poll()
                if self.run_next():
                    continue
            except Exception:
                log.exception("library scanner pass failed")
            with self._cond:
                if not self._stopping and not self._queue:
                    self._cond.wait(IDLE_WAIT_SECONDS)
