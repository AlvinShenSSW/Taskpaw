"""One-time import of Filmpaw's NAS roots and aliases (#361 §7, #362 L1).

Filmpaw's `library.db` is opened read-only and never written. Only new roots
(as archive-role actor libraries, appended to the priority order) and aliases
not yet present are added; Taskpaw's own data is never changed or deleted.
Performer rows are not copied: each imported library is scanned afterwards.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from .library_store import (
    LibraryError,
    LibraryStore,
    insert_library,
    path_key,
    root_conflict,
)


def default_filmpaw_db() -> Path:
    return Path(os.environ.get("APPDATA") or Path.home()) / "Filmpaw" / "library.db"


def _db_path(path: str | None) -> Path:
    db = Path(path) if path else default_filmpaw_db()
    return db / "library.db" if db.is_dir() else db


def read_filmpaw(path: str | None) -> dict[str, Any]:
    """Filmpaw's roots (with performer counts) and aliases, read-only."""
    db = _db_path(path)
    if not db.is_file():
        raise LibraryError(404, f"找不到 Filmpaw 片库：{db}")
    try:
        conn = sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise LibraryError(422, f"无法打开 Filmpaw 片库：{exc}") from None
    try:
        roots = [
            {"path": unc_path, "label": label, "performers": count}
            for unc_path, label, count in conn.execute(
                "SELECT s.unc_path, s.label,"
                " (SELECT COUNT(*) FROM performers p WHERE p.source_id=s.id)"
                " FROM sources s ORDER BY s.id"
            )
        ]
        aliases = conn.execute(
            "SELECT name_norm, alias, alias_norm FROM aliases ORDER BY id"
        ).fetchall()
    except sqlite3.Error as exc:
        raise LibraryError(422, f"不是可读的 Filmpaw 片库：{exc}") from None
    finally:
        conn.close()
    return {"path": str(db), "roots": roots, "aliases": aliases}


def _conflict(keys: list[str], key: str) -> str | None:
    """`root_conflict` against roots added earlier in the same import."""
    for other in keys:
        if other == key:
            return "exists"
        if other.startswith(key) or key.startswith(other):
            return "overlap"
    return None


def preview(store: LibraryStore, path: str | None) -> dict[str, Any]:
    """Each root as `new`, `exists` or `overlap`, and how many aliases are new."""
    source = read_filmpaw(path)
    with store.reading() as conn:
        roots, adding = [], []  # adding: the roots this import would add, in order
        for root in source["roots"]:
            key = path_key(root["path"])
            status = root_conflict(conn, key) or _conflict(adding, key) or "new"
            if status == "new":
                adding.append(key)
            roots.append({**root, "status": status})
        known = {
            (row["name_norm"], row["alias_norm"])
            for row in conn.execute("SELECT name_norm, alias_norm FROM aliases")
        }
    new_aliases = {(a[0], a[2]) for a in source["aliases"]} - known
    return {
        "path": source["path"],
        "roots": roots,
        "aliases": len(source["aliases"]),
        "new_aliases": len(new_aliases),
    }


def import_filmpaw(store: LibraryStore, path: str | None) -> dict[str, Any]:
    """Add the new roots and aliases in one transaction; report what was skipped."""
    source = read_filmpaw(path)
    added: list[str] = []
    skipped: list[dict[str, str]] = []
    with store.transaction() as conn:
        for root in source["roots"]:
            try:
                added.append(
                    insert_library(
                        conn, "actor", root["path"], label=root["label"], role="archive"
                    )
                )
            except LibraryError as exc:
                skipped.append({"path": root["path"], "reason": exc.detail})
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO aliases(name_norm, alias, alias_norm) VALUES (?,?,?)",
            [tuple(alias) for alias in source["aliases"]],
        )
        aliases_added = conn.total_changes - before
    return {
        "path": source["path"],
        "added": added,
        "skipped": skipped,
        "aliases_added": aliases_added,
    }
