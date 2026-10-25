"""Conservative intake reclamation; selection is read-only, effects are explicit."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import stat
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .store import WorkflowStore

log = logging.getLogger("taskpaw.workflow.intake_reclaim")


def _link(info: os.stat_result) -> bool:
    # Includes Windows junctions and other reparse points, also on Python 3.10.
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _guard(path: Path, root: Path) -> None:
    if ".." in path.parts or path == root or not path.is_relative_to(root):
        raise ValueError("outside reclamation root")
    # Even an ancestor junction targeting another directory INSIDE root is unsafe.
    for parent in path.parents:
        if _link(parent.lstat()):
            raise ValueError("linked reclamation ancestor")
    # For a leaf link the object being deleted is the directory entry itself.
    resolved = path.parent.resolve() / path.name
    if resolved == root.resolve() or not resolved.is_relative_to(root.resolve()):
        raise ValueError("outside resolved reclamation root")


def _data_paths(directory: Path) -> list[Path]:
    paths = []
    for name in ("snapshot", "partial"):
        path = directory / name
        try:
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode) or _link(info):
                paths.append(path)
        except FileNotFoundError:
            continue
    return paths


def intake_orphans(
    conn: sqlite3.Connection, work_dir: Path, *, operation_id: str | None = None
) -> list[Path]:
    """Read-only dry run: return operation DIRS, never deletion targets themselves.

    Call with a read-only connection to a database copy and the work directory.
    Only each returned directory's `snapshot`/`partial` may be removed; directories
    containing only metadata are omitted. Historical
    revisions of live films also retain their snapshots. No auth, writes or store
    opening/recovery occurs here. `operation_id` scopes terminal-runner cleanup.
    """
    root = work_dir.absolute() / "intake"
    if not root.exists():
        return []
    _guard(root, work_dir.absolute())
    if _link(root.lstat()):
        return []
    rows = conn.execute(
        "SELECT o.operation_id FROM intake_operations o "
        "JOIN intake_packages p ON p.package_id=o.package_id "
        "WHERE o.state IN ('completed','failed','cancelled') AND o.unresolved=0 "
        "AND p.active_operation_id IS NOT o.operation_id "
        "AND (? IS NULL OR o.operation_id=?) ORDER BY o.operation_id",
        (operation_id, operation_id),
    ).fetchall()
    references: list[Path] = []
    for (body,) in conn.execute(
        "SELECT DISTINCT s.body_json FROM intake_films i "
        "JOIN films f ON f.film_id=i.film_id "
        "JOIN intake_snapshots s ON s.snapshot_id=i.snapshot_id "
        "WHERE json_extract(f.body_json,'$.removed_at') IS NULL"
    ):
        relative = json.loads(body)["relative_work_path"]
        if (
            not isinstance(relative, str)
            or not relative
            or Path(relative).is_absolute()
            or ".." in Path(relative).parts
        ):
            raise ValueError("invalid live snapshot path; keep intake data")
        reference = (work_dir / relative).absolute()
        references.extend((reference, reference.resolve()))
    eligible = {row[0] for row in rows}
    result = []
    for path in sorted(root.iterdir()):
        if path.name not in eligible:
            continue
        try:
            _guard(path, root)
            info = path.lstat()
            if _link(info) or not stat.S_ISDIR(info.st_mode):
                continue
            resolved = path.resolve()
            if _data_paths(path) and not any(
                ref.is_relative_to(resolved) or resolved.is_relative_to(ref)
                for ref in references
            ):
                result.append(path)
        except FileNotFoundError:
            continue
    return result


def remove_work_tree(path: Path, root: Path, *, operation_id: str) -> None:
    """Best-effort removal strictly within root, without traversing any links.

    Python 3.12 shutil.rmtree documents junction safety (since 3.8). Walk here
    explicitly to count ONLY files actually unlinked, and log failures. A link
    contributes zero bytes: its target was never inspected or reclaimed.
    """
    path, root = path.absolute(), root.absolute()
    freed = 0
    deleted = False

    def remove(target: Path) -> None:
        nonlocal freed, deleted
        try:
            _guard(target, root)
            info = target.lstat()
            if _link(info):
                if stat.S_ISDIR(info.st_mode):
                    target.rmdir()  # Windows directory junction, not its target
                else:
                    target.unlink()
            elif stat.S_ISDIR(info.st_mode):
                for child in target.iterdir():
                    remove(child)
                target.rmdir()
            elif stat.S_ISREG(info.st_mode):
                if target == path:
                    raise ValueError("expected work directory; keep loose file")
                # Windows refuses unlink of read-only files. Private copies only;
                # never chmod a link (including a directory reparse point).
                if (
                    os.name == "nt"
                    and info.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
                ):
                    target.chmod(stat.S_IWRITE)
                target.unlink()
                freed += info.st_size
            else:
                raise ValueError("unrecognized work entry; keep")
            deleted = True
        except FileNotFoundError:
            return
        except (OSError, ValueError, RuntimeError):
            log.warning(
                "intake reclaim skipped operation=%s path=%s",
                operation_id,
                target,
                exc_info=True,
            )

    remove(path)
    if deleted:
        log.info(
            "intake reclaim operation=%s path=%s bytes_freed=%d",
            operation_id,
            path,
            freed,
        )


def reclaim_intake(
    store: WorkflowStore, work_dir: Path, *, operation_id: str | None = None
) -> None:
    """Startup sweep, or cleanup of ONE durably settled failed/cancelled owner."""
    try:
        with store._lock:
            for directory in intake_orphans(
                store._conn, work_dir, operation_id=operation_id
            ):
                for path in _data_paths(directory):
                    remove_work_tree(
                        path,
                        work_dir / "intake",
                        operation_id=directory.name,
                    )
    except (sqlite3.Error, OSError, ValueError, KeyError, TypeError, RuntimeError):
        log.exception("intake reclaim skipped operation=%s", operation_id or "startup")
