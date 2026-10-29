"""The controller's archive engine and the facade the finalizer calls (#361 §9, #363 L2a).

Inherited from Filmpaw's workflow archive (`transfer.py`): a target proof, then
one background worker that copies one approved package at a time into a staging
folder beside the target, re-hashes every byte against the approved manifest
and commits with a rename that never replaces. Nothing on the NAS is ever
overwritten or deleted except the engine's own write probe. Every step is
journalled in `library.db` before and after it runs; a failed journal or task
write stops the worker until restart, and a restart reconciles, never resumes.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from . import models as m
from .library_store import PAGE_SIZE_DEFAULT, LibraryStore, normalize, now_iso

log = logging.getLogger("taskpaw.workflow.library_archive")

STAGING_PREFIX = ".taskpaw-archive-"
CHUNK_SIZE = 1024 * 1024
SAVE_SECONDS = 0.2
REFUSED = 412  # every permanent refusal (§9.1): waiting cannot help
CONFLICT = "目标已有同名影片目录"
MISMATCH = "目标目录内容与批准清单不一致"
ACTOR_EXISTS = "演员目录已存在，请选择已有演员"
_REPARSE_POINT = 0x400
_RELATIVE = TypeAdapter(m.RelativePath)

# Filmpaw `directory_reads.py`: network failures that clear by waiting. WinError
# first: Windows maps some of them to ENOENT/EACCES.
_TEMPORARY_WINDOWS = frozenset(
    {32, 33, 51, 53, 54, 55, 59, 64, 67, 71, 121, 170, 1231, 1232, 1236, 1237, 1460}
)
_TEMPORARY_ERRNO = frozenset(
    {
        errno.EAGAIN,
        errno.EBUSY,
        errno.EINTR,
        errno.ECONNABORTED,
        errno.ECONNRESET,
        errno.ECONNREFUSED,
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.EHOSTUNREACH,
        errno.ENOTCONN,
        errno.ETIMEDOUT,
    }
)


class LibraryUnavailable(Exception):
    """The library or its share cannot be reached now; the caller waits."""


class LibraryRefused(Exception):
    """A permanent refusal: status 412 and a `code` (§9.1)."""

    def __init__(self, status: int, message: str, code: str):
        super().__init__(f"{status}: {message}")
        self.status, self.message, self.code = status, message, code


class _Failed(Exception):
    """The task fails with this message."""


class _Conflict(_Failed):
    """A same-name film folder in the target: never overwritten."""


class _StorageFailed(Exception):
    """A task or journal write failed: the worker stops until restart."""


class _Interrupted(Exception):
    """Shutdown mid-hash: the task row is left for the restart recovery."""


def retryable(exc: BaseException) -> bool:
    if not isinstance(exc, OSError):
        return False
    winerror = getattr(exc, "winerror", None)
    if winerror is not None:
        return winerror in _TEMPORARY_WINDOWS
    return isinstance(exc, TimeoutError) or exc.errno in _TEMPORARY_ERRNO


def _refused(message: str, code: str = "invalid") -> LibraryRefused:
    return LibraryRefused(REFUSED, message, code)


def _os_error(exc: OSError, label: str) -> Exception:
    """A retryable error waits; any other is a permanent refusal."""
    if retryable(exc):
        return LibraryUnavailable(f"{label}：{exc}")
    code = "permission" if isinstance(exc, PermissionError) else "missing"
    return _refused(f"{label}：{exc}", code)


def check_name(name: str) -> str:
    """Filmpaw's `_name` rule plus `RelativePath` (reserved names, NFC, control
    characters), so an approval never meets a pydantic error (§9.1)."""
    if (
        not name
        or name in (".", "..")
        or name.rstrip(" .") != name
        or any(c in name for c in '/\\:\x00<>"|?*')
        or name.startswith(STAGING_PREFIX)
    ):
        raise _refused(f"名称无效：{name!r}")
    try:
        _RELATIVE.validate_python(name)
    except ValidationError:
        raise _refused(f"名称无效：{name!r}") from None
    return name


def directory_identity(path: Path) -> list[str]:
    """`[st_dev, st_ino]` of a plain directory from `lstat`, as strings (§9.2)."""
    value = os.lstat(path)
    if stat.S_ISLNK(value.st_mode) or (
        getattr(value, "st_file_attributes", 0) & _REPARSE_POINT
    ):
        raise _refused(f"不支持符号链接或重解析点：{path}")
    if not stat.S_ISDIR(value.st_mode):
        raise _refused(f"目标不是目录：{path}", "missing")
    return [str(value.st_dev), str(value.st_ino)]


def _names(parent: Path) -> set[str]:
    """The casefolded names in `parent`, without this engine's staging folders."""
    with os.scandir(parent) as entries:
        return {
            e.name.casefold() for e in entries if not e.name.startswith(STAGING_PREFIX)
        }


def _raise(exc: OSError) -> None:
    raise exc


def _tree(root: Path) -> dict[str, int]:
    """Every file below `root` (posix relative path → size); any read error raises."""
    files: dict[str, int] = {}
    for folder, _, names in os.walk(root, onerror=_raise):
        for name in names:
            path = Path(folder) / name
            files[path.relative_to(root).as_posix()] = os.stat(path).st_size
    return files


def _approved(files: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The approved final manifest (already validated by its model); every
    '/'-separated part is still a name the NAS accepts."""
    if not files:
        raise _refused("清单为空")
    for entry in files:
        for part in entry["path"].split("/"):
            check_name(part)
    return [{key: f[key] for key in ("path", "size", "sha256")} for f in files]


def _spec(
    performer_id: str | None,
    target_dir: str | None,
    category_id: str | None = None,
    library_id: str | None = None,
    actor_name: str | None = None,
) -> dict[str, Any]:
    """One target (§9.1): a performer, a manual directory, a category, or a new
    actor directory in an actor library root."""
    if sum(bool(v) for v in (performer_id, target_dir, category_id, library_id)) != 1:
        raise ValueError("exactly one target is required")
    return {
        "performer_id": performer_id or None,
        "target_dir": target_dir or None,
        "category_id": category_id or None,
        "library_id": library_id or None,
        "actor_name": actor_name if library_id else None,
    }


def _expected(value: dict[str, Any]) -> dict[str, Any]:
    """The approval's target proof; Filmpaw-era manual proofs hold integers."""
    try:
        return {
            "target_path": str(value["target_path"]),
            "directory_identity": [str(v) for v in value["directory_identity"]],
        }
    except (KeyError, TypeError) as exc:
        raise _refused("缺少审批时的目标证明") from exc


def _same(parent: Path, identity: list[str], expected: dict[str, Any]) -> bool:
    return (
        str(parent) == expected["target_path"]
        and identity == expected["directory_identity"]
    )


def _message(exc: BaseException) -> str:
    if isinstance(exc, LibraryRefused):
        return exc.message
    return str(exc) or type(exc).__name__


def _load(row: Any) -> dict[str, Any]:
    """A task row as the worker holds it in memory."""
    return {
        "operation_id": row["operation_id"],
        "task_id": row["task_id"],
        "status": row["status"],
        "phase": row["phase"],
        "message": row["message"] or "",
        "total_bytes": row["total_bytes"],
        "progress_bytes": row["progress_bytes"],
        "verified_bytes": row["verified_bytes"],
        "targets": json.loads(row["targets_json"]),
        "receipt": json.loads(row["receipt_json"]) if row["receipt_json"] else None,
        "created_at": row["created_at"],
    }


def _public(task: dict[str, Any]) -> dict[str, Any]:
    """The snapshot `lookup` returns (the shape the finalizer reads)."""
    targets = task["targets"]
    return {
        "id": task["task_id"],
        "operation_id": task["operation_id"],
        "status": task["status"],
        "phase": task["phase"],
        "message": task["message"],
        "total_bytes": task["total_bytes"],
        "progress_bytes": task["progress_bytes"],
        "verified_bytes": task["verified_bytes"],
        "total_count": len(targets),
        "completed_count": sum(t["status"] == "committed" for t in targets),
        "items": [
            {
                key: t[key]
                for key in (
                    "name",
                    "target_path",
                    "status",
                    "size_bytes",
                    "progress_bytes",
                    "message",
                )
            }
            for t in targets
        ],
        "receipt": task["receipt"],
        "created_at": task["created_at"],
    }


class LibraryArchive:
    """The five calls `finalize.Finalizer` makes, served by `library.db` and one
    archive worker thread (one task at a time, FIFO; §4).

    `store` is None while the library could not be opened: every call then
    raises `LibraryUnavailable` with `reason`, and the finalizer waits.
    """

    def __init__(self, store: LibraryStore | None, reason: str = "片库尚未启动"):
        self.store, self.reason = store, reason
        self._cond = threading.Condition()
        self._queue: list[str] = []
        self._stopping = False
        # a failed task/journal write: nothing more is accepted until restart
        self._broken: str | None = None
        self._frozen: dict[str, dict[str, Any]] = {}  # its tasks as last known
        self._thread: threading.Thread | None = None

    def _db(self) -> LibraryStore:
        if self.store is None:
            raise LibraryUnavailable(self.reason)
        return self.store

    # ── performers (§9.6, §10) ──────────────────────────────────────────
    def performers(
        self,
        q: str,
        *,
        include_missing: bool = False,
        library_id: str | None = None,
        filter: str | None = None,
        page: int = 1,
        page_size: int = PAGE_SIZE_DEFAULT,
        group: bool = False,
    ) -> dict[str, Any]:
        """The library search envelope `{items, total, page, page_size}`."""
        return self._db().search(
            q,
            include_missing=include_missing,
            library_id=library_id,
            filter=filter,
            page=page,
            page_size=page_size,
            group=group,
        )

    def thumb(self, performer_id: str) -> tuple[bytes, str] | None:
        """The thumbnail and its ETag, or None."""
        return self._db().thumb(performer_id)

    def candidates(self, actors: list[str], number: str) -> dict[str, Any]:
        """The 待确认 candidates and preselection for a film (§9.6)."""
        return self._db().candidates(actors, number)

    # ── the second target bound to one approval (§9.4) ──────────────────
    def bind_second(
        self,
        binding_hash: str,
        category_id: str | None,
        expected_target: dict[str, Any] | None,
    ) -> None:
        """Written for every performer / new-actor approval, NULL without a
        second target; a row whose binding never became an approval is inert."""
        with self._db().transaction() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO second_targets(binding_hash, category_id,"
                " target_path, directory_identity, created_at) VALUES (?,?,?,?,?)",
                (
                    binding_hash,
                    category_id,
                    expected_target["target_path"] if expected_target else None,
                    json.dumps(expected_target["directory_identity"])
                    if expected_target
                    else None,
                    now_iso(),
                ),
            )

    def second_target(self, binding_hash: str) -> dict[str, Any] | None:
        """None without a row; `second` None when none was chosen, else
        `{category_id, expected_target}` as `archive(second=...)` takes it."""
        with self._db().reading() as conn:
            row = conn.execute(
                "SELECT * FROM second_targets WHERE binding_hash=?", (binding_hash,)
            ).fetchone()
        if row is None:
            return None
        if row["category_id"] is None:
            return {"second": None}
        return {
            "second": {
                "category_id": row["category_id"],
                "expected_target": {
                    "target_path": row["target_path"],
                    "directory_identity": json.loads(row["directory_identity"]),
                },
            }
        }

    # ── target proof (§9.2) ─────────────────────────────────────────────
    @staticmethod
    def _parent(store: LibraryStore, spec: dict[str, Any]) -> Path:
        """The directory the target's proof is taken of (§9.1)."""
        if spec["target_dir"] is not None:
            if not os.path.isabs(spec["target_dir"]):
                raise _refused("入库目录必须是绝对路径")
            return Path(spec["target_dir"])
        with store.reading() as conn:
            if spec["category_id"] is not None:
                row = conn.execute(
                    "SELECT path FROM libraries WHERE id=? AND kind='category'",
                    (spec["category_id"],),
                ).fetchone()
                if row is None:
                    raise _refused("所选类型目录已不存在", "missing")
                return Path(row["path"])
            if spec["library_id"] is not None:
                row = conn.execute(
                    "SELECT path, kind, role FROM libraries WHERE id=?",
                    (spec["library_id"],),
                ).fetchone()
                if row is None:
                    raise _refused("所选片库已不存在", "missing")
                if row["kind"] != "actor" or row["role"] != "archive":
                    raise _refused("只能在可入库的演员片库中新建演员目录", "readonly")
                return Path(row["path"])
            row = conn.execute(
                "SELECT p.path, p.is_missing, l.role FROM performers p"
                " JOIN libraries l ON l.id = p.library_id WHERE p.id=?",
                (spec["performer_id"],),
            ).fetchone()
        if row is None:
            raise _refused("所选演员记录已不存在", "missing")
        if row["is_missing"]:
            raise _refused("所选演员目录已失效，请刷新片库", "missing")
        if row["role"] != "archive":
            raise _refused("所选演员位于只读片库，不能入库", "readonly")
        return Path(row["path"])

    def _prove(
        self, store: LibraryStore, spec: dict[str, Any], film: str
    ) -> tuple[Path, list[str], bool, Path]:
        """The proved parent, its identity, whether `film` is absent there and
        the film folder. A new actor's parent is the library root; the folder
        is `<root>\\<name>\\<film>` and the actor directory must not exist."""
        parent = self._parent(store, spec)
        name = spec["actor_name"]
        if spec["library_id"] is not None:
            check_name(name)
            if name.startswith("."):  # the scan skips dot folders: it would go missing
                raise _refused("演员目录名不能以 . 开头")
        try:
            identity, names = directory_identity(parent), _names(parent)
        except OSError as exc:
            raise _os_error(exc, "无法读取入库目录") from exc
        if spec["library_id"] is None:
            return parent, identity, film.casefold() not in names, parent / film
        if name.casefold() in names:
            raise _refused(f"{ACTOR_EXISTS}：{name}", "actor_exists")
        return parent, identity, True, parent / name / film

    @staticmethod
    def _archived(
        store: LibraryStore, folder: Path, final_hash: str, identity: list[str]
    ) -> bool:
        """This engine committed this package into `folder` under this parent."""
        with store.reading() as conn:
            row = conn.execute(
                "SELECT final_hash, directory_identity FROM archived_targets"
                " WHERE target_key=?",
                (str(folder).casefold(),),
            ).fetchone()
        return (
            row is not None
            and row["final_hash"] == final_hash
            and json.loads(row["directory_identity"]) == identity
        )

    def target(
        self,
        performer_id: str | None,
        film_dir: str,
        *,
        target_dir: str | None = None,
        category_id: str | None = None,
        library_id: str | None = None,
        actor_name: str | None = None,
        final_hash: str | None = None,
    ) -> dict[str, Any]:
        spec = _spec(performer_id, target_dir, category_id, library_id, actor_name)
        store = self._db()
        film = check_name(film_dir)
        parent, identity, absent, folder = self._prove(store, spec, film)
        return {
            "target_path": str(folder),
            "absent": absent,
            "archived": not absent
            and final_hash is not None
            and self._archived(store, folder, final_hash, identity),
            "expected_target": {
                "target_path": str(parent),
                "directory_identity": identity,
            },
        }

    # ── archive task (§9.3) ─────────────────────────────────────────────
    def archive(
        self,
        operation_id: str,
        package_dir: Path,
        performer_id: str | None,
        expected_target: dict[str, Any],
        files: list[dict[str, Any]],
        *,
        target_dir: str | None = None,
        category_id: str | None = None,
        library_id: str | None = None,
        actor_name: str | None = None,
        second: dict[str, Any] | None = None,
        final_hash: str | None = None,
    ) -> dict[str, Any]:
        """Queue the copy of one approved package to its target and, when
        `second` (`{category_id, expected_target}`) is given, to that category
        too (§9.4); the same operation key and payload answers the task's
        snapshot, a different payload is refused."""
        specs = [
            {
                **_spec(performer_id, target_dir, category_id, library_id, actor_name),
                "expected_target": _expected(expected_target),
            }
        ]
        if second is not None:
            specs.append(
                {
                    **_spec(None, None, second["category_id"]),
                    "expected_target": _expected(second["expected_target"]),
                }
            )
        store = self._db()
        if self._broken:
            raise LibraryUnavailable(self._broken)
        package = Path(package_dir)
        film = check_name(package.name)
        approved = _approved(files)
        payload = {
            "package_dir": str(package),
            "film": film,
            "final_hash": final_hash,
            "files": approved,
            "targets": specs,
        }
        payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        payload_hash = hashlib.sha256(payload_json.encode("utf-8")).hexdigest()
        previous = self._row(store, operation_id)
        if previous is not None:
            if previous["payload_hash"] != payload_hash:
                raise _refused("操作键已用于另一个归档请求", "changed")
            return _public(_load(previous))
        size = sum(f["size"] for f in approved)
        targets = []
        for spec in specs:
            parent, identity, _, folder = self._prove(store, spec, film)
            if not _same(parent, identity, spec["expected_target"]):
                raise _refused("入库目录已变化，请重新确认入库", "changed")
            targets.append(
                {
                    "name": film,
                    "target_path": str(folder),
                    "status": "pending",
                    "size_bytes": size,
                    "progress_bytes": 0,
                    "staging_path": None,
                    "directory_identity": None,
                    "message": "",
                }
            )
        now = now_iso()
        with store.transaction() as conn:
            conn.execute(
                "INSERT INTO archive_tasks(operation_id, task_id, payload_hash,"
                " payload_json, status, phase, total_bytes, targets_json, message,"
                " created_at, updated_at) VALUES (?,?,?,?,'queued','queued',?,?,?,?,?)",
                (
                    operation_id,
                    str(uuid.uuid4()),
                    payload_hash,
                    payload_json,
                    size * len(targets),
                    json.dumps(targets, ensure_ascii=False),
                    "排队等待归档",
                    now,
                    now,
                ),
            )
        self._enqueue(operation_id)
        row = self._row(store, operation_id)
        assert row is not None
        return _public(_load(row))

    def lookup(self, operation_id: str) -> dict[str, Any] | None:
        """The task's snapshot; None when this engine never accepted it."""
        store = self._db()
        with self._cond:
            if operation_id in self._frozen:
                return dict(self._frozen[operation_id])
        row = self._row(store, operation_id)
        return None if row is None else _public(_load(row))

    @staticmethod
    def _row(store: LibraryStore, operation_id: str) -> Any:
        with store.reading() as conn:
            return conn.execute(
                "SELECT * FROM archive_tasks WHERE operation_id=?", (operation_id,)
            ).fetchone()

    def _enqueue(self, operation_id: str) -> None:
        with self._cond:
            if operation_id not in self._queue:
                self._queue.append(operation_id)
                self._cond.notify_all()

    # ── worker thread ───────────────────────────────────────────────────
    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="library-archive", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 30.0) -> bool:
        """A copy in progress stops at its next chunk; the restart settles it."""
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        if self._thread is not None:
            self._thread.join(timeout)
            return not self._thread.is_alive()
        return True

    def _run(self) -> None:
        try:
            self.recover()
        except _Interrupted:
            return
        except Exception:
            log.exception("archive recovery failed")
        while True:
            with self._cond:
                while (self._broken or not self._queue) and not self._stopping:
                    self._cond.wait()
                if self._stopping:
                    return
            try:
                self.run_next()
            except Exception:  # never let one task end the worker
                log.exception("archive task failed")

    def _check_stop(self) -> None:
        if self._stopping:
            raise _Interrupted()

    def run_next(self) -> bool:
        """Run the oldest queued task; False when there is none (or the worker
        stopped after a failed write)."""
        with self._cond:
            if self._broken or not self._queue:
                return False
            operation_id = self._queue.pop(0)
        store = self._db()
        row = self._row(store, operation_id)
        if row is None or row["status"] != "queued":
            return True
        task, payload = _load(row), json.loads(row["payload_json"])
        try:
            self._execute(task, payload)
        except _StorageFailed as exc:
            self._freeze(task, str(exc))
        except _Interrupted:
            pass
        return True

    def _freeze(self, task: dict[str, Any], error: str) -> None:
        """Filmpaw's storage failure: the task needs review and nothing more runs."""
        message = f"片库数据库写入失败，归档已停止；请重启后核对结果：{error}"
        log.error("archive %s: %s", task["operation_id"], message)
        task.update(status="needs_review", phase="finished", message=message)
        with self._cond:
            self._broken = message
            self._frozen[task["operation_id"]] = _public(task)
            self._queue.clear()
        try:
            self._save(task)
        except _StorageFailed:
            pass

    # ── storage (every failure is _StorageFailed) ───────────────────────
    @contextmanager
    def _writing(self) -> Iterator[Any]:
        try:
            with self._db().transaction() as conn:
                yield conn
        except Exception as exc:
            raise _StorageFailed(_message(exc)) from exc

    def _save(self, task: dict[str, Any]) -> None:
        with self._writing() as conn:
            conn.execute(
                "UPDATE archive_tasks SET status=?, phase=?, total_bytes=?,"
                " progress_bytes=?, verified_bytes=?, targets_json=?, message=?,"
                " receipt_json=?, updated_at=? WHERE operation_id=?",
                (
                    task["status"],
                    task["phase"],
                    task["total_bytes"],
                    task["progress_bytes"],
                    task["verified_bytes"],
                    json.dumps(task["targets"], ensure_ascii=False),
                    task["message"],
                    json.dumps(task["receipt"], ensure_ascii=False)
                    if task["receipt"]
                    else None,
                    now_iso(),
                    task["operation_id"],
                ),
            )

    def _event(
        self,
        task: dict[str, Any],
        action: str,
        index: int,
        outcome: str,
        detail: str | None = None,
    ) -> None:
        with self._writing() as conn:
            _insert_event(conn, task["operation_id"], action, index, outcome, detail)

    def _step(
        self, task: dict[str, Any], action: str, index: int, fn: Callable[[], Any]
    ) -> Any:
        """`started` before and `succeeded`/`failed` after one journalled action."""
        self._event(task, action, index, "started")
        try:
            result = fn()
        except (_StorageFailed, _Interrupted):
            raise
        except Exception as exc:
            self._event(task, action, index, "failed", _message(exc)[:2000])
            raise
        self._event(task, action, index, "succeeded")
        return result

    def _record(
        self, task: dict[str, Any], payload: dict[str, Any], folder: Path
    ) -> None:
        """`archived_targets` with the identity of the folder's parent now."""
        identity = directory_identity(folder.parent)
        with self._writing() as conn:
            conn.execute(
                "INSERT INTO archived_targets(target_key, final_hash,"
                " directory_identity, operation_id, committed_at) VALUES (?,?,?,?,?)"
                " ON CONFLICT(target_key) DO UPDATE SET final_hash=excluded.final_hash,"
                " directory_identity=excluded.directory_identity,"
                " operation_id=excluded.operation_id, committed_at=excluded.committed_at",
                (
                    str(folder).casefold(),
                    payload["final_hash"] or "",
                    json.dumps(identity),
                    task["operation_id"],
                    now_iso(),
                ),
            )

    # ── the task ────────────────────────────────────────────────────────
    def _execute(self, task: dict[str, Any], payload: dict[str, Any]) -> None:
        current: dict[str, Any] | None = None
        notes: list[str] = []
        try:
            task.update(status="running", phase="preflight", message="正在预检入库目标")
            self._save(task)
            notes = self._preflight(task, payload)
            for index, target in enumerate(task["targets"]):
                if target["status"] != "committed":
                    current = target
                    self._copy_target(task, payload, index, target)
            task.update(
                status="completed",
                message="；".join(["影片包已归档到 NAS", *notes]),
                receipt=self._receipt(task, payload),
            )
        except (_StorageFailed, _Interrupted):
            raise
        except Exception as exc:
            # only a commit that may have happened is ambiguous (Filmpaw)
            ambiguous = (
                current is not None
                and current["status"] == "committing"
                and not isinstance(exc, _Conflict)
            )
            status = "needs_review" if ambiguous else "failed"
            message = _message(exc)
            if current is not None:
                current.update(status=status, message=message)
            task.update(status=status, message=message + _leftovers(task))
        task["phase"] = "finished"
        self._save(task)

    def _preflight(self, task: dict[str, Any], payload: dict[str, Any]) -> list[str]:
        """Every target before anything is copied (§9.3); notes for the message."""
        store, film, files = self._db(), payload["film"], payload["files"]
        try:
            actual = _tree(Path(payload["package_dir"]))
        except OSError as exc:
            raise _Failed(f"影片包不可读：{exc}") from exc
        expected = {f["path"]: f["size"] for f in files}
        if actual != expected:
            extra = sorted(actual.keys() - expected.keys())[:5]
            missing = sorted(expected.keys() - actual.keys())[:5]
            raise _Failed(
                f"影片包内容与已批准清单不一致：多出 {extra}，缺少 {missing}"
                if extra or missing
                else "影片包文件大小与已批准清单不一致"
            )
        size, notes, copies = sum(expected.values()), [], 0
        for spec, target in zip(payload["targets"], task["targets"]):
            parent, identity, absent, folder = self._prove(store, spec, film)
            if not _same(parent, identity, spec["expected_target"]):
                raise _Failed("入库目录已变化，请重新确认入库")
            if not absent:
                final_hash = payload["final_hash"]
                if not final_hash or not self._archived(
                    store, folder, final_hash, identity
                ):
                    raise _Conflict(CONFLICT)
                try:  # this engine's verified copy of this package (§9.5)
                    extras = self._reverify(folder, files)
                    folder_identity = directory_identity(folder)
                except OSError as exc:
                    raise _Failed(f"{MISMATCH}：{exc}") from exc
                target.update(
                    status="committed",
                    directory_identity=folder_identity,
                    message="已入库（内容已核对）",
                )
                if extras:
                    notes.append(
                        f"目标目录中的清单外文件未改动：{'、'.join(extras[:20])}"
                    )
                continue
            try:
                if shutil.disk_usage(parent).free < size:
                    raise _Failed("NAS 可用空间不足，未开始复制")
                # the only file this engine ever deletes on the NAS
                probe = parent / f"{STAGING_PREFIX}{task['task_id']}.probe"
                with open(probe, "xb"):
                    pass
                os.unlink(probe)
            except OSError as exc:
                raise _Failed(f"入库目录不可写：{exc}") from exc
            copies += 1
        task["total_bytes"] = size * copies
        return notes

    def _copy_target(
        self,
        task: dict[str, Any],
        payload: dict[str, Any],
        index: int,
        target: dict[str, Any],
    ) -> None:
        folder = Path(target["target_path"])
        parent = folder.parent
        spec = payload["targets"][index]
        if spec["library_id"] is not None:
            self._mkdir_actor(task, index, spec, parent)
        staging = parent / f"{STAGING_PREFIX}{task['task_id']}-{uuid.uuid4().hex}"
        # the intended path is recorded before it exists (Filmpaw)
        target.update(status="copying", staging_path=str(staging), message="正在复制")
        task.update(phase="copying", message="正在复制影片包")
        self._save(task)
        hashes = self._step(
            task, "copy", index, lambda: self._copy(task, target, payload, staging)
        )
        target.update(status="verifying", message="正在校验 SHA-256")
        task.update(phase="verifying", message="正在读取目标并校验 SHA-256")
        self._save(task)
        self._step(
            task, "verify", index, lambda: self._verify(task, payload, staging, hashes)
        )
        target.update(status="committing", message="校验通过，正在提交")
        task.update(phase="committing", message="校验通过，正在提交目标目录")
        self._save(task)
        self._step(
            task, "commit", index, lambda: self._commit(task, payload, target, folder)
        )

    def _mkdir_actor(
        self, task: dict[str, Any], index: int, spec: dict[str, Any], actor: Path
    ) -> None:
        """§9.3: create the new actor directory exclusively; its performer row
        is written with the `succeeded` journal entry, so a later failure
        leaves an actor the operator can pick when confirming again."""
        self._event(task, "mkdir_actor", index, "started")
        try:
            os.mkdir(actor)
        except OSError as exc:
            exists = isinstance(exc, FileExistsError)
            message = (
                f"{ACTOR_EXISTS}：{actor.name}"
                if exists
                else f"无法创建演员目录：{exc}"
            )
            self._event(task, "mkdir_actor", index, "failed", message[:2000])
            raise _Failed(message) from exc
        now = now_iso()
        with self._writing() as conn:
            _insert_event(conn, task["operation_id"], "mkdir_actor", index, "succeeded")
            conn.execute(
                "INSERT INTO performers(id, name, name_norm, library_id, folder_name,"
                " path, first_seen_at, last_seen_at, is_missing)"
                " VALUES (?,?,?,?,?,?,?,?,0) ON CONFLICT(path) DO UPDATE SET"
                " is_missing=0, last_seen_at=excluded.last_seen_at",
                (
                    str(uuid.uuid4()),
                    actor.name,
                    normalize(actor.name),
                    spec["library_id"],
                    actor.name,
                    str(actor),
                    now,
                    now,
                ),
            )

    def _copy(
        self,
        task: dict[str, Any],
        target: dict[str, Any],
        payload: dict[str, Any],
        staging: Path,
    ) -> dict[str, str]:
        package = Path(payload["package_dir"])
        os.mkdir(staging)
        target["directory_identity"] = directory_identity(staging)
        hashes: dict[str, str] = {}
        last = time.monotonic()
        for entry in payload["files"]:
            source, copied = package / entry["path"], staging / entry["path"]
            copied.parent.mkdir(parents=True, exist_ok=True)  # inside our staging
            digest = hashlib.sha256()
            with open(source, "rb") as inp, open(copied, "xb") as out:
                while chunk := inp.read(CHUNK_SIZE):
                    self._check_stop()
                    out.write(chunk)
                    digest.update(chunk)
                    target["progress_bytes"] += len(chunk)
                    task["progress_bytes"] += len(chunk)
                    if time.monotonic() - last >= SAVE_SECONDS:
                        self._save(task)
                        last = time.monotonic()
                out.flush()
                os.fsync(out.fileno())
            st = os.stat(source)
            os.utime(copied, ns=(st.st_atime_ns, st.st_mtime_ns))
            hashes[entry["path"]] = digest.hexdigest()
        self._save(task)
        return hashes

    def _hash(self, path: Path, progress: Callable[[int], None] | None = None) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            while chunk := stream.read(CHUNK_SIZE):
                self._check_stop()
                digest.update(chunk)
                if progress is not None:
                    progress(len(chunk))
        return digest.hexdigest()

    def _verify(
        self,
        task: dict[str, Any],
        payload: dict[str, Any],
        staging: Path,
        hashes: dict[str, str],
    ) -> None:
        last = time.monotonic()

        def progress(count: int) -> None:
            nonlocal last
            task["verified_bytes"] += count
            if time.monotonic() - last >= SAVE_SECONDS:
                self._save(task)
                last = time.monotonic()

        for entry in payload["files"]:
            digest = self._hash(staging / entry["path"], progress)
            if digest != hashes[entry["path"]] or digest != entry["sha256"]:
                raise _Failed(f"SHA-256 内容校验失败：{entry['path']}")

    def _reverify(self, folder: Path, files: list[dict[str, Any]]) -> list[str]:
        """Every approved file present with its size and SHA-256; the names of
        files not in the manifest (ignored, e.g. a media server's artwork)."""
        present = _tree(folder)
        for entry in files:
            if present.get(entry["path"]) != entry["size"] or (
                self._hash(folder / entry["path"]) != entry["sha256"]
            ):
                raise _Failed(f"{MISMATCH}：{entry['path']}")
        return sorted(present.keys() - {entry["path"] for entry in files})

    def _commit(
        self,
        task: dict[str, Any],
        payload: dict[str, Any],
        target: dict[str, Any],
        folder: Path,
    ) -> None:
        if payload["film"].casefold() in _names(folder.parent):
            raise _Conflict(CONFLICT)
        try:  # Windows never replaces an existing destination (§13)
            os.rename(target["staging_path"], folder)
        except FileExistsError:
            raise _Conflict(CONFLICT) from None
        self._record(task, payload, folder)
        target.update(status="committed", message="已入库")

    @staticmethod
    def _receipt(task: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        primary, *second = task["targets"]
        return {
            "task_id": task["task_id"],
            "target_path": primary["target_path"],
            "directory_identity": primary["directory_identity"],
            "files": sorted(payload["files"], key=lambda f: f["path"]),
            "second": {
                "target_path": second[0]["target_path"],
                "directory_identity": second[0]["directory_identity"],
            }
            if second
            else None,
        }

    # ── restart recovery (Filmpaw `transfer.py` 1361–1446) ──────────────
    def recover(self) -> None:
        """Re-queue queued tasks and reconcile interrupted ones; nothing is
        resumed or deleted."""
        store = self._db()
        with store.reading() as conn:
            rows = conn.execute(
                "SELECT * FROM archive_tasks WHERE status IN ('queued','running')"
                " ORDER BY rowid"
            ).fetchall()
        for row in rows:
            if row["status"] == "queued":
                self._enqueue(row["operation_id"])
                continue
            task = _load(row)
            try:
                self._reconcile(task, json.loads(row["payload_json"]))
            except _StorageFailed as exc:
                self._freeze(task, str(exc))
                return
            except _Interrupted:
                raise
            except Exception:  # left as it is; the other tasks still settle
                log.exception("archive recovery of %s failed", row["operation_id"])

    def _reconcile(self, task: dict[str, Any], payload: dict[str, Any]) -> None:
        with self._db().reading() as conn:
            events = conn.execute(
                "SELECT action, target_index, outcome FROM archive_events"
                " WHERE operation_id=? AND action IN ('commit','mkdir_actor')",
                (task["operation_id"],),
            ).fetchall()
        journal: dict[tuple[str, int], set[str]] = {}
        for event in events:
            key = (event["action"], event["target_index"])
            journal.setdefault(key, set()).add(event["outcome"])
        for index, target in enumerate(task["targets"]):
            outcomes = journal.get(("commit", index), set())
            if target["status"] == "committed" or "succeeded" in outcomes:
                target["status"] = "committed"
            elif "started" in outcomes and "failed" not in outcomes:
                self._recover_commit(task, payload, index, target)
            elif "failed" in outcomes:
                target.update(
                    status="needs_review", message="上次提交失败，请人工核对目标目录"
                )
            elif journal.get(("mkdir_actor", index)) == {"started"}:
                target.update(
                    status="needs_review",
                    message="上次新建演员目录中断，请人工核对片库根目录",
                )
            elif target["status"] in ("copying", "verifying", "committing"):
                target.update(status="failed", message="上次复制或校验中断，来源保留")
        if all(t["status"] == "committed" for t in task["targets"]):
            task.update(
                status="completed",
                message="已核对：影片包已完整归档",
                receipt=self._receipt(task, payload),
            )
        else:
            review = any(t["status"] == "needs_review" for t in task["targets"])
            task.update(
                status="needs_review" if review else "failed",
                message="上次归档中断，未自动继续或删除" + _leftovers(task),
            )
        task["phase"] = "finished"
        self._save(task)

    def _recover_commit(
        self,
        task: dict[str, Any],
        payload: dict[str, Any],
        index: int,
        target: dict[str, Any],
    ) -> None:
        """A commit that started: done only when the staging folder is gone and
        the film folder re-hashes to the approved manifest."""
        folder = Path(target["target_path"])
        staging = target["staging_path"]
        try:
            if staging and not os.path.lexists(staging):
                self._reverify(folder, payload["files"])
                identity = directory_identity(folder)
                self._record(task, payload, folder)
                self._event(task, "commit", index, "succeeded", "restart recovery")
                target.update(
                    status="committed",
                    directory_identity=identity,
                    message="已核对：上次提交已完成",
                )
                return
        except (OSError, _Failed, LibraryRefused) as exc:
            log.warning("archive recovery of %s: %s", folder, exc)
        target.update(
            status="needs_review",
            message="上次提交中断，无法确认结果，请人工核对目标目录",
        )


def _insert_event(
    conn: Any,
    op: str,
    action: str,
    index: int,
    outcome: str,
    detail: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO archive_events(event_key, operation_id, action,"
        " target_index, outcome, at, detail) VALUES (?,?,?,?,?,?,?)",
        (
            f"{op}:{action}:{index}:{outcome}",
            op,
            action,
            index,
            outcome,
            now_iso(),
            detail,
        ),
    )


def _leftovers(task: dict[str, Any]) -> str:
    """Staging folders that stay on the NAS (never deleted automatically)."""
    paths = [
        t["staging_path"]
        for t in task["targets"]
        if t.get("staging_path")
        and t["status"] != "committed"
        and os.path.lexists(t["staging_path"])
    ]
    return f"；暂存目录已保留：{'、'.join(paths)}" if paths else ""
