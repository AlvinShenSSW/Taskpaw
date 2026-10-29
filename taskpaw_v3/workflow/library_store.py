"""The controller's own NAS library index, `library.db` (#361 §4–§8, #362 L1).

Inherited from Filmpaw (`db.py`, `routes_sources.py`, `routes_performers.py`):
one sqlite connection serialized by one lock, the schema version read before
any DDL, name normalization and the group-level search and alias rules. Errors
carry the HTTP status the control routes answer with (`LibraryError`).
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import stat
import threading
import unicodedata
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from zhconv import convert

from .schema import _path as local_database_path

SCHEMA_VERSION = 1
PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 200
FILTERS = ("multi", "alias", "missing", "nothumb")

_V1 = (
    "CREATE TABLE schema_version (version INTEGER NOT NULL)",
    """CREATE TABLE libraries (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL CHECK(kind IN ('actor','category')),
        path TEXT NOT NULL,
        path_key TEXT NOT NULL UNIQUE,
        label TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('archive','readonly')),
        prefixes TEXT NOT NULL DEFAULT '[]',
        position INTEGER NOT NULL,
        last_scan_at TEXT,
        last_scan_ok INTEGER,
        last_scan_ms INTEGER,
        film_count INTEGER,
        free_bytes INTEGER,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE performers (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        name_norm TEXT NOT NULL,
        library_id TEXT NOT NULL REFERENCES libraries(id) ON DELETE CASCADE,
        folder_name TEXT NOT NULL,
        path TEXT NOT NULL UNIQUE,
        first_seen_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        is_missing INTEGER NOT NULL DEFAULT 0,
        film_count INTEGER,
        thumb BLOB,
        thumb_mtime REAL,
        thumb_side INTEGER
    )""",
    "CREATE INDEX idx_performers_name_norm ON performers(name_norm)",
    "CREATE INDEX idx_performers_library ON performers(library_id)",
    """CREATE TABLE aliases (
        id INTEGER PRIMARY KEY,
        name_norm TEXT NOT NULL,
        alias TEXT NOT NULL,
        alias_norm TEXT NOT NULL,
        UNIQUE(name_norm, alias_norm)
    )""",
    "CREATE INDEX idx_aliases_alias_norm ON aliases(alias_norm)",
    "CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE archive_tasks (
        operation_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL UNIQUE,
        payload_hash TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN
            ('queued','running','completed','failed','needs_review')),
        phase TEXT NOT NULL CHECK(phase IN
            ('queued','preflight','copying','verifying','committing','finished')),
        total_bytes INTEGER NOT NULL DEFAULT 0,
        progress_bytes INTEGER NOT NULL DEFAULT 0,
        verified_bytes INTEGER NOT NULL DEFAULT 0,
        targets_json TEXT NOT NULL DEFAULT '[]',
        message TEXT,
        receipt_json TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE archive_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_key TEXT NOT NULL UNIQUE,
        operation_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK(action IN ('mkdir_actor','copy','verify','commit')),
        target_index INTEGER NOT NULL,
        outcome TEXT NOT NULL CHECK(outcome IN ('started','succeeded','failed')),
        at TEXT NOT NULL,
        detail TEXT
    )""",
    "CREATE INDEX idx_archive_events_operation ON archive_events(operation_id)",
    """CREATE TABLE archived_targets (
        target_key TEXT PRIMARY KEY,
        final_hash TEXT NOT NULL,
        directory_identity TEXT NOT NULL,
        operation_id TEXT NOT NULL,
        committed_at TEXT NOT NULL
    )""",
    """CREATE TABLE second_targets (
        binding_hash TEXT PRIMARY KEY,
        category_id TEXT,
        target_path TEXT,
        directory_identity TEXT,
        created_at TEXT NOT NULL
    )""",
)


def _create_v1(conn: sqlite3.Connection) -> None:
    for statement in _V1:
        conn.execute(statement)
    conn.execute("INSERT INTO schema_version(version) VALUES (0)")


# from_version -> the step to from_version + 1; 0 is a new, empty file
_STEPS = {0: _create_v1}


class LibraryError(Exception):
    """A refused library request; `status` is the control route's answer."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status = status
        self.detail = detail


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize(s: str) -> str:
    """Filmpaw's `normalize.py`: NFKC, lower case, traditional → simplified."""
    return convert(unicodedata.normalize("NFKC", s.strip()).lower(), "zh-hans")


_FUZZY = re.compile(r"[\s・·.]")


def fuzzy_key(norm: str) -> str:
    """§6 fuzzy candidates: a normalized name without whitespace and `・·.`."""
    return _FUZZY.sub("", norm)


def normalize_path(path: str) -> str:
    """`/` → `\\` and one trailing separator (the native one off Windows)."""
    path = path.strip()
    if path:  # `.`/`..` segments and mixed separators must not split one root
        path = os.path.normpath(os.path.abspath(path))
    return path if path.endswith(os.sep) else path + os.sep


def is_dir(path: str) -> bool:
    """`os.stat` and a directory; never `isdir()` as a deletion test (§6)."""
    try:
        return stat.S_ISDIR(os.stat(path).st_mode)
    except OSError:
        return False


def path_key(path: str) -> str:
    return normalize_path(path).casefold()


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _read_version(conn: sqlite3.Connection) -> int | None:
    try:
        row = conn.execute("SELECT version FROM schema_version").fetchone()
    except sqlite3.OperationalError:  # no table: a new file
        return None
    return int(row[0]) if row is not None else None


def _bool(value: Any) -> bool:
    return isinstance(value, bool)


def _choice(*allowed: Any) -> Any:
    return lambda value: not isinstance(value, bool) and value in allowed


# §8 rules plus the §6 scan schedule: key -> (default, validator)
RULES: dict[str, tuple[Any, Any]] = {
    "multi_location": ("priority", _choice("priority", "most", "recent")),
    "alias_match": (True, _bool),
    "preselect_single": (True, _bool),
    "fuzzy": (True, _bool),
    "allow_new_actor": (True, _bool),
    # None: the first archive-role actor library (checked against the libraries)
    "new_actor_library": (None, lambda value: value is None or isinstance(value, str)),
    "new_actor_name": ("actor", _choice("actor", "ask")),
    "category_prefix": (True, _bool),
    "second_target_hint": (True, _bool),
    "category_always": (True, _bool),
    "scan_on_start": (True, _bool),
    "scan_every_hours": (6, _choice(0, 2, 6, 12, 24)),
}


def root_conflict(conn: sqlite3.Connection, key: str) -> str | None:
    """`exists` for the same root, `overlap` when one root contains the other."""
    for (other,) in conn.execute("SELECT path_key FROM libraries"):
        if other == key:
            return "exists"
        if other.startswith(key) or key.startswith(other):
            return "overlap"
    return None


def _prefixes(values: list[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        prefix = value.strip().upper()
        if prefix and prefix not in out:
            out.append(prefix)
    return out


def insert_library(
    conn: sqlite3.Connection,
    kind: str,
    path: str,
    *,
    label: str | None = None,
    role: str | None = None,
    prefixes: list[str] | None = None,
) -> str:
    """Add one root inside the caller's transaction; 409 when it overlaps."""
    if kind not in ("actor", "category"):
        raise LibraryError(422, "片库类型无效")
    path = normalize_path(path)
    if path == os.sep:
        raise LibraryError(422, "请填写片库路径")
    key = path.casefold()
    conflict = root_conflict(conn, key)
    if conflict == "exists":
        raise LibraryError(409, "该片库已存在")
    if conflict == "overlap":
        raise LibraryError(409, "该目录与已有片库互相包含")
    if kind == "category":
        if role not in (None, "archive"):
            raise LibraryError(422, "类型目录只能作为入库目标")
        role = "archive"
    else:
        if prefixes:
            raise LibraryError(422, "只有类型目录可以设置番号前缀")
        role = role or "archive"
    if role not in ("archive", "readonly"):
        raise LibraryError(422, "片库角色无效")
    label = (label or "").strip() or Path(path.rstrip(os.sep)).name or path
    library_id = str(uuid.uuid4())
    (position,) = conn.execute(
        "SELECT COALESCE(MAX(position), 0) + 1 FROM libraries WHERE kind=?", (kind,)
    ).fetchone()
    conn.execute(
        "INSERT INTO libraries(id, kind, path, path_key, label, role, prefixes,"
        " position, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (
            library_id,
            kind,
            path,
            key,
            label,
            role,
            json.dumps(_prefixes(prefixes or []), ensure_ascii=False),
            position,
            now_iso(),
        ),
    )
    return library_id


def _library(row: sqlite3.Row) -> dict[str, Any]:
    ok = row["last_scan_ok"]
    return {
        "id": row["id"],
        "kind": row["kind"],
        "path": row["path"],
        "label": row["label"],
        "role": row["role"],
        "prefixes": json.loads(row["prefixes"]),
        "position": row["position"],
        "last_scan_at": row["last_scan_at"],
        "last_scan_ok": None if ok is None else bool(ok),
        "last_scan_ms": row["last_scan_ms"],
        "film_count": row["film_count"],
        "free_bytes": row["free_bytes"],
        "created_at": row["created_at"],
        "performer_count": row["performer_count"],
        "missing_count": row["missing_count"],
    }


_LIBRARY_SELECT = (
    "SELECT l.*,"
    " (SELECT COUNT(*) FROM performers p WHERE p.library_id=l.id AND p.is_missing=0)"
    " AS performer_count,"
    " (SELECT COUNT(*) FROM performers p WHERE p.library_id=l.id AND p.is_missing=1)"
    " AS missing_count FROM libraries l"
)


def _hits(column: str, prefix: str) -> str:
    """Filmpaw's group match: contains the query, or the query contains it (≥2)."""
    return (
        f"{column} LIKE '%' || :{prefix}_like || '%' ESCAPE '\\'"
        f" OR (LENGTH({column}) >= 2 AND INSTR(:{prefix}_raw, {column}) > 0)"
    )


class LibraryStore:
    """`library.db` behind one lock; every method is safe from any thread."""

    def __init__(self, conn: sqlite3.Connection, path: Path):
        self._conn = conn
        self._lock = threading.Lock()
        self.path = path

    @classmethod
    def open(cls, path: Path) -> LibraryStore:
        """Create or open schema v1; a newer file is refused untouched (§4)."""
        path = local_database_path(path)
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            version = _read_version(conn)
            if version is not None and version > SCHEMA_VERSION:
                raise LibraryError(
                    503, f"片库数据库版本 {version} 比本程序新（{SCHEMA_VERSION}）"
                )
            version = version or 0
            while version < SCHEMA_VERSION:
                step = _STEPS.get(version)
                if step is None:
                    raise LibraryError(503, f"片库数据库版本 {version} 无法识别")
                conn.execute("BEGIN IMMEDIATE")
                try:
                    step(conn)
                    conn.execute("UPDATE schema_version SET version=?", (version + 1,))
                    conn.execute("COMMIT")
                except BaseException:
                    if conn.in_transaction:
                        conn.execute("ROLLBACK")
                    raise
                version += 1
            conn.execute("PRAGMA foreign_keys = ON")
            conn.create_function("fuzzy", 1, fuzzy_key, deterministic=True)
        except BaseException:
            conn.close()
            raise
        return cls(conn, path)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @contextmanager
    def reading(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self._conn

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """One write transaction; any exception rolls it back."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")
            except BaseException:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                raise

    # ── libraries (§6) ──────────────────────────────────────────────────
    def libraries(self) -> list[dict[str, Any]]:
        """Last scan results only; nothing here touches the NAS (§6)."""
        with self.reading() as conn:
            rows = conn.execute(
                f"{_LIBRARY_SELECT} ORDER BY l.kind, l.position, l.created_at"
            ).fetchall()
        return [_library(row) for row in rows]

    def library(self, library_id: str) -> dict[str, Any]:
        with self.reading() as conn:
            row = conn.execute(
                f"{_LIBRARY_SELECT} WHERE l.id=?", (library_id,)
            ).fetchone()
        if row is None:
            raise LibraryError(404, "片库不存在")
        return _library(row)

    def add_library(
        self,
        kind: str,
        path: str,
        *,
        label: str | None = None,
        role: str | None = None,
        prefixes: list[str] | None = None,
    ) -> dict[str, Any]:
        """The root must be an existing directory now (§6)."""
        if not path.strip():  # blank would resolve to the drive root (CX5)
            raise LibraryError(422, "请填写片库路径")
        normalized = normalize_path(path)
        if not is_dir(normalized):
            raise LibraryError(422, f"路径不可达或不是目录：{normalized}")
        with self.transaction() as conn:
            library_id = insert_library(
                conn, kind, normalized, label=label, role=role, prefixes=prefixes
            )
        return self.library(library_id)

    def update_library(
        self,
        library_id: str,
        *,
        label: str | None = None,
        role: str | None = None,
        prefixes: list[str] | None = None,
    ) -> dict[str, Any]:
        """Only label, role (actor) and prefixes (category) change (§6)."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT kind FROM libraries WHERE id=?", (library_id,)
            ).fetchone()
            if row is None:
                raise LibraryError(404, "片库不存在")
            if label is not None:
                if not label.strip():
                    raise LibraryError(422, "名称不能为空")
                conn.execute(
                    "UPDATE libraries SET label=? WHERE id=?",
                    (label.strip(), library_id),
                )
            if role is not None:
                if row["kind"] != "actor":
                    raise LibraryError(422, "类型目录只能作为入库目标")
                if role not in ("archive", "readonly"):
                    raise LibraryError(422, "片库角色无效")
                conn.execute(
                    "UPDATE libraries SET role=? WHERE id=?", (role, library_id)
                )
            if prefixes is not None:
                if row["kind"] != "category":
                    raise LibraryError(422, "只有类型目录可以设置番号前缀")
                conn.execute(
                    "UPDATE libraries SET prefixes=? WHERE id=?",
                    (json.dumps(_prefixes(prefixes), ensure_ascii=False), library_id),
                )
        return self.library(library_id)

    def remove_library(self, library_id: str) -> None:
        """Its performers go with it; aliases are group-level and stay (§5)."""
        with self.transaction() as conn:
            if not conn.execute(
                "DELETE FROM libraries WHERE id=?", (library_id,)
            ).rowcount:
                raise LibraryError(404, "片库不存在")

    def reorder(self, ids: list[str]) -> list[dict[str, Any]]:
        """The actor libraries' priority (§8 `priority`): `ids` is every actor
        library exactly once, in the new order; categories keep theirs."""
        with self.transaction() as conn:
            current = {
                row["id"]
                for row in conn.execute("SELECT id FROM libraries WHERE kind='actor'")
            }
            if len(ids) != len(set(ids)) or set(ids) != current:
                raise LibraryError(422, "顺序必须恰好包含每个演员片库一次")
            conn.executemany(
                "UPDATE libraries SET position=? WHERE id=?",
                [(position, library_id) for position, library_id in enumerate(ids, 1)],
            )
        return self.libraries()

    def unreachable(self) -> list[tuple[str, str]]:
        """(id, path) of every library whose last scan could not reach it."""
        with self.reading() as conn:
            return [
                (row["id"], row["path"])
                for row in conn.execute(
                    "SELECT id, path FROM libraries WHERE last_scan_ok=0"
                    " ORDER BY kind, position"
                )
            ]

    def library_ids(self) -> list[str]:
        with self.reading() as conn:
            return [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM libraries ORDER BY kind, position"
                )
            ]

    # ── rules (§8) ──────────────────────────────────────────────────────
    def rules(self) -> dict[str, Any]:
        with self.reading() as conn:
            stored = {
                row["key"]: json.loads(row["value"])
                for row in conn.execute("SELECT key, value FROM settings")
            }
            archive = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM libraries WHERE kind='actor' AND role='archive'"
                    " ORDER BY position"
                )
            ]
        out = {key: stored.get(key, default) for key, (default, _) in RULES.items()}
        if out["new_actor_library"] not in archive:  # unset, removed or read-only
            out["new_actor_library"] = archive[0] if archive else None
        return out

    def put_rules(self, changes: Mapping[str, Any]) -> dict[str, Any]:
        """Validate every given key, then store them in one transaction."""
        for key, value in changes.items():
            if key not in RULES:
                raise LibraryError(422, f"未知规则：{key}")
            if not RULES[key][1](value):
                raise LibraryError(422, f"规则 {key} 的值无效")
        with self.transaction() as conn:
            library = changes.get("new_actor_library")
            if (
                library is not None
                and not conn.execute(
                    "SELECT 1 FROM libraries WHERE id=? AND kind='actor' AND role='archive'",
                    (library,),
                ).fetchone()
            ):
                raise LibraryError(422, "新建演员目录的默认片库必须是可入库的演员片库")
            for key, value in changes.items():
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?)"
                    " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value, ensure_ascii=False)),
                )
        return self.rules()

    # ── performers (§6, served by /performers from L2a) ─────────────────
    def search(
        self,
        q: str = "",
        *,
        include_missing: bool = False,
        library_id: str | None = None,
        filter: str | None = None,
        aliases: bool = True,
        fuzzy: bool = False,
        page: int = 1,
        page_size: int = PAGE_SIZE_DEFAULT,
        group: bool = False,
    ) -> dict[str, Any]:
        """Every performer of each matched name group, by `name_norm, id`.

        A group found only through the fuzzy comparison has `fuzzy: true`: it
        is listed, never preselected (§6). With `group` the page and `total`
        count name groups, and each group on the page comes with every one of
        its matching locations (the 演员 tab, #365).
        """
        if page < 1 or not 1 <= page_size <= PAGE_SIZE_MAX:
            raise LibraryError(422, f"page ≥ 1，page_size 1–{PAGE_SIZE_MAX}")
        if filter is not None and filter not in FILTERS:
            raise LibraryError(422, "筛选条件无效")
        params: dict[str, Any] = {}
        prefix, join, fuzzy_hit = "", "", "0"
        if q.strip():
            nq = normalize(q)
            params.update(n_like=_escape_like(nq), n_raw=nq)
            hits = [
                f"SELECT name_norm, 1 FROM performers WHERE {_hits('name_norm', 'n')}"
            ]
            if aliases:
                hits.append(
                    f"SELECT name_norm, 1 FROM aliases WHERE {_hits('alias_norm', 'n')}"
                )
            fq = fuzzy_key(nq)
            if fuzzy and fq:
                params.update(f_like=_escape_like(fq), f_raw=fq)
                hits.append(
                    "SELECT name_norm, 0 FROM performers"
                    f" WHERE {_hits('fuzzy(name_norm)', 'f')}"
                )
                if aliases:
                    hits.append(
                        "SELECT name_norm, 0 FROM aliases"
                        f" WHERE {_hits('fuzzy(alias_norm)', 'f')}"
                    )
            prefix = (
                f"WITH hits(name_norm, exact) AS ({' UNION ALL '.join(hits)}),"
                " matched AS (SELECT name_norm, MAX(exact) AS exact FROM hits"
                " GROUP BY name_norm) "
            )
            join, fuzzy_hit = (
                "JOIN matched g ON g.name_norm = p.name_norm",
                "g.exact = 0",
            )
        where = ["1=1"]
        if filter == "missing":
            where.append("p.is_missing = 1")
        elif not include_missing:
            where.append("p.is_missing = 0")
        if filter == "multi":
            where.append(
                "(SELECT COUNT(*) FROM performers x WHERE x.name_norm = p.name_norm) > 1"
            )
        elif filter == "alias":
            where.append(
                "EXISTS (SELECT 1 FROM aliases a WHERE a.name_norm = p.name_norm)"
            )
        elif filter == "nothumb":
            where.append("p.thumb IS NULL")
        with self.reading() as conn:
            if library_id is not None:
                params["library_id"] = library_id
                where.append("p.library_id = :library_id")
                if not conn.execute(
                    "SELECT 1 FROM libraries WHERE id=?", (library_id,)
                ).fetchone():
                    raise LibraryError(422, "library_id 无效")
            cond = " AND ".join(where)
            counted = "DISTINCT p.name_norm" if group else "*"
            (total,) = conn.execute(
                f"{prefix}SELECT COUNT({counted}) FROM performers p {join}"
                f" WHERE {cond}",
                params,
            ).fetchone()
            paged, limit = cond, " LIMIT :limit OFFSET :offset"
            if group:  # the page's groups, then all their rows
                # filters pick the groups; each shows all its locations
                shown = (
                    "1=1"
                    if include_missing or filter == "missing"
                    else "p.is_missing = 0"
                )
                paged, limit = (
                    f"{shown} AND p.name_norm IN (SELECT DISTINCT p.name_norm"
                    f" FROM performers p {join} WHERE {cond}"
                    " ORDER BY p.name_norm LIMIT :limit OFFSET :offset)",
                    "",
                )
            rows = conn.execute(
                f"{prefix}SELECT p.*, l.label AS source_label, l.role AS library_role,"
                f" {fuzzy_hit} AS fuzzy_hit"
                f" FROM performers p {join} JOIN libraries l ON l.id = p.library_id"
                f" WHERE {paged} ORDER BY p.name_norm, p.id{limit}",
                {**params, "limit": page_size, "offset": (page - 1) * page_size},
            ).fetchall()
            alias_map = self._aliases(conn, {row["name_norm"] for row in rows})
        return {
            "items": [_performer(row, alias_map) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    @staticmethod
    def _aliases(
        conn: sqlite3.Connection, norms: set[str]
    ) -> dict[str, list[tuple[int, str]]]:
        out: dict[str, list[tuple[int, str]]] = {norm: [] for norm in norms}
        if norms:
            marks = ",".join("?" for _ in norms)
            for row in conn.execute(
                f"SELECT id, name_norm, alias FROM aliases WHERE name_norm IN ({marks})"
                " ORDER BY id",
                sorted(norms),
            ):
                out[row["name_norm"]].append((row["id"], row["alias"]))
        return out

    def performer(self, performer_id: str) -> dict[str, Any]:
        """One performer with every location of its name group and its aliases."""
        with self.reading() as conn:
            row = conn.execute(
                "SELECT name_norm FROM performers WHERE id=?", (performer_id,)
            ).fetchone()
            if row is None:
                raise LibraryError(404, "演员不存在")
            rows = conn.execute(
                "SELECT p.*, l.label AS source_label, l.role AS library_role,"
                " 0 AS fuzzy_hit FROM performers p"
                " JOIN libraries l ON l.id = p.library_id WHERE p.name_norm=?"
                " ORDER BY l.kind, l.position, p.id",
                (row["name_norm"],),
            ).fetchall()
            alias_map = self._aliases(conn, {row["name_norm"]})
        this = next(r for r in rows if r["id"] == performer_id)
        return {
            **_performer(this, alias_map),
            "locations": [_performer(r, alias_map) for r in rows],
        }

    def thumb(self, performer_id: str) -> tuple[bytes, str] | None:
        """The thumbnail and its ETag `"{thumb_mtime}-{thumb_side}"`, or None."""
        with self.reading() as conn:
            row = conn.execute(
                "SELECT thumb, thumb_mtime, thumb_side FROM performers WHERE id=?",
                (performer_id,),
            ).fetchone()
        if row is None or row["thumb"] is None:
            return None
        return bytes(row["thumb"]), f'"{row["thumb_mtime"]}-{row["thumb_side"]}"'

    # ── 待确认 candidates (§9.6) ────────────────────────────────────────
    def candidates(self, actors: list[str], number: str) -> dict[str, Any]:
        """Every location of each scraped actor's name group (plus aliases and
        fuzzy hits per the rules, missing ones left out), the category
        directories, and what the rules preselect: one actor group → that
        actor (by `multi_location` among its locations); no archive-role actor
        candidate → the category whose prefix matches `number` (the longest
        prefix wins). Fuzzy hits and read-only locations are listed only."""
        rules = self.rules()
        items: dict[str, dict[str, Any]] = {}
        for actor in dict.fromkeys(actors):
            found = self.search(
                actor,
                aliases=rules["alias_match"],
                fuzzy=rules["fuzzy"],
                page_size=PAGE_SIZE_MAX,
            )["items"]
            for item in found:
                if item["id"] not in items or items[item["id"]]["fuzzy"]:
                    items[item["id"]] = item
        archive = [item for item in items.values() if item["role"] == "archive"]
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in archive:
            if not item["fuzzy"]:
                groups.setdefault(normalize(item["name"]), []).append(item)
        number = number.upper()

        def longest(prefixes: list[str]) -> int:
            return max((len(p) for p in prefixes if number.startswith(p)), default=0)

        libraries = self.libraries()
        categories = [
            {
                "id": lib["id"],
                "label": lib["label"],
                "path": lib["path"],
                "prefixes": lib["prefixes"],
                "film_count": lib["film_count"],
                "prefix_match": longest(lib["prefixes"]) > 0,
            }
            for lib in libraries
            if lib["kind"] == "category"
        ]
        matched = max(
            (c for c in categories if c["prefix_match"]),
            key=lambda c: longest(c["prefixes"]),
            default=None,
        )
        if archive and not rules["category_always"]:
            categories = []
        selected = None
        if len(groups) == 1 and rules["preselect_single"]:
            (locations,) = groups.values()
            position = {lib["id"]: lib["position"] for lib in libraries}
            chosen = self._location(locations, rules["multi_location"], position)
            selected = {"kind": "performer", "id": chosen["id"]}
        elif not archive and rules["category_prefix"] and matched is not None:
            selected = {"kind": "category", "id": matched["id"]}
        hint = (
            matched is not None
            and selected is not None
            and selected["kind"] == "performer"
            and rules["second_target_hint"]
        )
        return {
            "items": list(items.values()),
            "categories": categories,
            "selected": selected,
            "second_hint": matched["id"] if hint and matched is not None else None,
            "new_actor": {
                "library_id": rules["new_actor_library"],
                "name": actors[0]
                if actors and rules["new_actor_name"] == "actor"
                else "",
            }
            if rules["allow_new_actor"]
            else None,
        }

    def _location(
        self, locations: list[dict[str, Any]], rule: str, position: dict[str, int]
    ) -> dict[str, Any]:
        """One actor's location by `multi_location` (§8); library order breaks
        ties and stands in when no receipt names a location."""
        ordered = sorted(locations, key=lambda item: position[item["library_id"]])
        if rule == "most":
            return max(ordered, key=lambda item: item["film_count"] or 0)
        if rule == "recent":
            paths = {str(Path(item["unc_path"])).casefold(): item for item in ordered}
            for receipt, _ in self.task_receipts().values():
                parent = str(Path(receipt["target_path"]).parent).casefold()
                if parent in paths:
                    return paths[parent]
        return ordered[0]

    def task_receipts(self) -> dict[str, tuple[dict[str, Any], str]]:
        """operation id → (receipt, message) of every archive task this engine
        completed, newest first."""
        with self.reading() as conn:
            rows = conn.execute(
                "SELECT operation_id, receipt_json, message FROM archive_tasks"
                " WHERE receipt_json IS NOT NULL ORDER BY updated_at DESC, rowid DESC"
            ).fetchall()
        return {
            row["operation_id"]: (json.loads(row["receipt_json"]), row["message"] or "")
            for row in rows
        }

    # ── aliases (§6) ────────────────────────────────────────────────────
    def add_alias(self, performer_id: str, alias: str) -> dict[str, Any]:
        alias = alias.strip()
        if not alias:
            raise LibraryError(422, "别名不能为空")
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT name_norm FROM performers WHERE id=?", (performer_id,)
            ).fetchone()
            if row is None:
                raise LibraryError(404, "演员不存在")
            alias_norm = normalize(alias)
            if alias_norm == row["name_norm"]:
                raise LibraryError(409, "别名与名字相同")
            try:
                cursor = conn.execute(
                    "INSERT INTO aliases(name_norm, alias, alias_norm) VALUES (?,?,?)",
                    (row["name_norm"], alias, alias_norm),
                )
            except sqlite3.IntegrityError:
                raise LibraryError(409, "该别名已存在") from None
        return {"id": cursor.lastrowid, "alias": alias}

    def delete_alias(self, alias_id: int) -> None:
        with self.transaction() as conn:
            if not conn.execute("DELETE FROM aliases WHERE id=?", (alias_id,)).rowcount:
                raise LibraryError(404, "别名不存在")


def _performer(
    row: sqlite3.Row, alias_map: dict[str, list[tuple[int, str]]]
) -> dict[str, Any]:
    """Today's /performers item (`api.ts`) plus the library's own fields."""
    group = alias_map.get(row["name_norm"], [])
    return {
        "id": row["id"],
        "name": row["name"],
        "name_norm": row["name_norm"],
        "source_label": row["source_label"],
        "unc_path": row["path"],
        "has_thumb": row["thumb"] is not None,
        "aliases": [alias for _, alias in group],
        "alias_ids": [alias_id for alias_id, _ in group],
        "library_id": row["library_id"],
        "role": row["library_role"],
        "is_missing": bool(row["is_missing"]),
        "film_count": row["film_count"],
        "fuzzy": bool(row["fuzzy_hit"]),
    }
