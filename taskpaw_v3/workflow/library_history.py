"""归档历史 (#361 §10): `workflow.db`'s archive receipts, no table of its own.

Each receipt is joined by operation id to the archive engine's task in
`library.db` for both target paths (`ArchiveReceipt` keeps no second target);
its kind is the bound target's scheme, so a Filmpaw-era receipt, which has no
task, still reads as an actor (`export_object_id`) or manual archive. Film
facts come from the board's helpers; cleanup state is each scope's latest
cleanup operation under the receipt's approval.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from typing import Any

from . import board
from .library_store import LibraryError, LibraryStore, path_key
from .local_runtime import LocalRuntime

PAGE_SIZE = 50
FILTERS = ("pending", "new", "category", "manual", "second")
_FILTER_KINDS = {"new": "new_actor", "category": "category", "manual": "manual"}


Task = tuple[dict[str, Any], str]  # the engine's receipt and final message


def _rows(
    rt: LocalRuntime, store: LibraryStore
) -> list[tuple[dict[str, Any], dict[str, Any], Task | None]]:
    """(row, the receipt as read, its task) of every archive receipt, newest
    first; the task is None for a Filmpaw-era receipt."""
    tasks = store.task_receipts()
    out = []
    for receipt in rt.store.archive_receipts(rt.operator):
        target = receipt["target"]
        found = tasks.get(receipt["operation_id"])
        task = found[0] if found else None
        second = task["second"] if task else None
        out.append(
            (
                {
                    "receipt_id": receipt["receipt_id"],
                    "film_id": receipt["film_id"],
                    "operation_id": receipt["operation_id"],
                    "accepted_at": receipt["accepted_at"],
                    "kind": board.TARGET_KINDS.get(target["native_identity"]["scheme"]),
                    "film_dir": PurePosixPath(target["relative_path"]).name,
                    "target_path": task["target_path"]
                    if task
                    else str(Path(target["export_identity"]) / target["relative_path"]),
                    "second_target_path": second["target_path"] if second else None,
                    "cleanup": receipt["cleanup"],
                    "pending": any(
                        status != "verified" for status in receipt["cleanup"].values()
                    ),
                },
                receipt,
                found,
            )
        )
    return out


def _facts(rt: LocalRuntime, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Number, title and actors by the board's precedence (final NFO, then
    the retained summary after cleanup); the film folder name as the number
    of a film the board no longer lists."""
    films = {
        film.film_id: film
        for film in rt.store.film_board(rt.operator, None, energies=False)
    }
    for row in rows:
        film = films.get(row["film_id"])
        facts = board.film_facts(rt, film) if film else None
        row.update(
            number=(facts.number if facts else None) or row["film_dir"],
            title=facts.title if facts else None,
            actors=list(facts.actors) if facts else [],
        )
    return rows


def _matches(row: dict[str, Any], filter: str | None) -> bool:
    if filter == "pending":
        return bool(row["pending"])
    if filter == "second":
        return row["second_target_path"] is not None
    return filter is None or row["kind"] == _FILTER_KINDS[filter]


def _searched(row: dict[str, Any], q: str) -> bool:
    text = " ".join(
        value or ""
        for value in (
            row["number"],
            row["title"],
            *row["actors"],
            row["film_dir"],
            row["target_path"],
            row["second_target_path"],
        )
    )
    return q.casefold() in text.casefold()


def history(
    rt: LocalRuntime,
    store: LibraryStore,
    filter: str | None = None,
    q: str = "",
    page: int = 1,
) -> dict[str, Any]:
    """`GET /library/history`: `{items, total, page, page_size}`, newest first."""
    rows = [row for row, _, _ in _rows(rt, store) if _matches(row, filter)]
    q = q.strip()
    if q:  # the facts are needed to search; otherwise only the page's are read
        rows = [row for row in _facts(rt, rows) if _searched(row, q)]
    items = rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    return {
        "items": items if q else _facts(rt, items),
        "total": len(rows),
        "page": page,
        "page_size": PAGE_SIZE,
    }


def receipt(rt: LocalRuntime, store: LibraryStore, receipt_id: str) -> dict[str, Any]:
    """`GET /library/history/{id}`: the row with the approved files and the
    archive task's final message (None for a Filmpaw-era receipt)."""
    found = next(
        (each for each in _rows(rt, store) if each[0]["receipt_id"] == receipt_id),
        None,
    )
    if found is None:
        raise LibraryError(404, "归档记录不存在")
    row, read, task = found
    _facts(rt, [row])
    if task is not None:
        files, message = task[0]["files"], task[1]
    else:
        final = (
            rt.store.get_final_manifest(
                rt.operator, row["film_id"], read["final_revision"]
            )
            if read["final_revision"] is not None
            else None
        )
        files = [
            {"path": i.relative_path, "size": i.size_bytes, "sha256": i.sha256}
            for i in (final.items if final else ())
        ]
        message = None
    return {**row, "files": files, "message": message}


def recent(
    rt: LocalRuntime, store: LibraryStore, locations: list[str], limit: int = 10
) -> list[dict[str, Any]]:
    """The newest receipts whose film folder sits directly in one of an actor
    group's locations (`GET /performers/{id}`)."""
    keys = {str(Path(path)).casefold() for path in locations}
    rows = [
        row
        for row, _, _ in _rows(rt, store)
        if str(Path(row["target_path"]).parent).casefold() in keys
    ]
    return _facts(rt, rows[:limit])


def may_open(rt: LocalRuntime, store: LibraryStore, path: str) -> bool:
    """`POST /library/open`: only a path inside a registered library root, or
    equal to or inside a receipt's target path (§10)."""
    if not os.path.isabs(path):
        return False
    key = path_key(path)
    allowed = [library["path"] for library in store.libraries()]
    for row, _, _ in _rows(rt, store):
        allowed += [p for p in (row["target_path"], row["second_target_path"]) if p]
    return any(key.startswith(path_key(p)) for p in allowed)
