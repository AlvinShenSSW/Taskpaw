"""Local, append-only task history (#196). Independent of the Hub event queue.

The store lock is a leaf: writers assign ids, append and publish to the ring in
one critical section; readers take the same lock. Callbacks and pruning run
outside it. Producers must pass observations, never config/argv or file contents.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import urlsplit, urlunsplit

log = logging.getLogger("taskpaw.tasklog")
MAX_BYTES = 50 * 1024 * 1024
MIRROR_CAP = 500
RING_SIZE = 2000
_FILE = re.compile(r"tasklog-(\d{8})\.jsonl\Z")
_ID = re.compile(r"(\d{8})-(\d+)\Z")
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_PRIVATE = {
    "argv",
    "command",
    "commands",
    "custom_command",
    "api_key",
    "api_token",
    "token",
    "password",
    "userinfo",
    "port",
    "ports",
    "subtitle",
    "subtitle_text",
    "file_contents",
}


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _id_key(value: str) -> tuple[str, int]:
    match = _ID.fullmatch(value)
    if match is None:
        raise ValueError("invalid task log id")
    return match[1], int(match[2])


def _valid_day(day: str) -> bool:
    try:
        return (
            len(day) == 8 and datetime.strptime(day, "%Y%m%d").strftime("%Y%m%d") == day
        )
    except ValueError:
        return False


def _safe_string(value: str) -> str:
    value = value.encode("utf-8", "backslashreplace").decode("utf-8")

    def public_url(match: re.Match[str]) -> str:
        try:
            url = urlsplit(match[0])
            host = url.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            # Query strings may hold credentials too; labels never need them.
            return urlunsplit((url.scheme, host, url.path, "", ""))
        except ValueError:
            return "[url]"

    return _URL.sub(public_url, value)


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return _safe_string(value)
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {
            _safe_string(str(k)): _sanitize(v)
            for k, v in value.items()
            if str(k).lower() not in _PRIVATE
            and not str(k)
            .lower()
            .endswith(("_extra_args", "_api_key", "_api_token", "_password", "_port"))
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    # Do not stringify arbitrary objects (their repr may expose config/secrets).
    return None


class TaskLog:
    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        on_first_failure: Callable[[str], None] | None = None,
        clock: Callable[[], datetime] = _local_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.boot = uuid.uuid4().hex
        self.write_failures = 0
        self._clock = clock
        self._monotonic = monotonic
        self._folder = data_dir / "logs" if data_dir is not None else None
        self._lock = threading.Lock()
        self._ring: deque[dict[str, Any]] = deque(maxlen=RING_SIZE)
        self._day = clock().strftime("%Y%m%d")
        self._n = 0
        self._latest_record_day = ""
        self._cache: dict[str, tuple[int, set[str]]] = {}
        self._mirrored: dict[str, int] = {}
        self._capped: set[str] = set()
        self._pending: dict[str, int] = {}
        self._types: dict[str, str] = {}
        self._last_delta: dict[str, float] = {}
        self._on_first_failure = on_first_failure
        self._failure_pending = False
        self._failure_notified = False
        self._load_day()
        self.prune()

    def _files(self) -> dict[str, Path]:
        if self._folder is None:
            return {}
        try:
            return {
                match[1]: path
                for path in self._folder.glob("tasklog-*.jsonl")
                if (match := _FILE.fullmatch(path.name)) and _valid_day(match[1])
            }
        except OSError as exc:
            log.warning("Task log listing failed: %s", type(exc).__name__)
            return {}

    def _read_day(self, day: str) -> Iterator[dict[str, Any]]:
        """Stream complete lines only. Corrupt/torn lines are not records."""
        if self._folder is None:
            return
        try:
            with (self._folder / f"tasklog-{day}.jsonl").open(
                "r", encoding="utf-8", errors="replace"
            ) as stream:
                for line in stream:
                    if not line.endswith("\n"):
                        continue
                    try:
                        row = json.loads(line)
                        if (
                            isinstance(row, dict)
                            and _id_key(row.get("id", ""))[0] == day
                        ):
                            yield _sanitize(row)
                    except (ValueError, TypeError, RecursionError):
                        continue
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning("Task log read failed: %s", type(exc).__name__)

    def _load_day(self) -> None:
        self._n = 0
        self._mirrored.clear()
        self._capped.clear()
        self._pending.clear()
        self._types.clear()
        self._last_delta.clear()
        for row in self._read_day(self._day):
            self._n = max(self._n, _id_key(row["id"])[1])
            self._latest_record_day = max(self._latest_record_day, self._day)
            task = row.get("task", "")
            kind = row.get("kind")
            if kind == "event.mirrored":
                self._mirrored[task] = self._mirrored.get(task, 0) + 1
            elif kind == "event.suppressed":
                self._capped.add(task)
                self._last_delta[task] = self._monotonic()

    def _append(self, row: dict[str, Any]) -> None:
        if self._folder is None:
            return
        self._folder.mkdir(parents=True, exist_ok=True)
        path = self._folder / f"tasklog-{_id_key(row['id'])[0]}.jsonl"
        prefix = ""
        try:
            with path.open("rb") as stream:
                stream.seek(0, 2)
                if stream.tell():
                    stream.seek(-1, 2)
                    if stream.read(1) != b"\n":
                        prefix = "\n"
        except FileNotFoundError:
            pass  # first record of the day
        payload = json.dumps(
            row, ensure_ascii=True, allow_nan=False, separators=(",", ":")
        )
        with path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(prefix + payload + "\n")

    def _failed(self) -> None:
        self.write_failures += 1
        self._failure_pending = True

    def _insert(self, row: dict[str, Any]) -> None:
        """Called only under _lock. A failed append still consumes this id."""
        self._n += 1
        row["id"] = f"{self._day}-{self._n}"
        for attempt in range(2):
            try:
                # A close() error can occur after a full line reached disk. Do
                # not append that id twice on the retry; torn lines are repaired.
                if attempt and any(
                    r["id"] == row["id"] for r in self._read_day(self._day)
                ):
                    break
                self._append(row)
                break
            except Exception as exc:
                if attempt:
                    self._failed()
                    log.warning("Task log append failed: %s", type(exc).__name__)
        self._ring.append(row)
        self._latest_record_day = max(self._latest_record_day, self._day)
        self._cache.pop(self._day, None)

    def _row(
        self, task: str, kind: str, task_type: str, now: datetime, **fields: Any
    ) -> dict[str, Any]:
        return {
            "v": 1,
            "ts": now.astimezone().isoformat(),
            "task": task,
            "task_type": task_type,
            "kind": kind,
            "severity": "info",
            **fields,
        }

    def _flush_deltas(self, now: datetime) -> None:
        for task, count in self._pending.items():
            if count:
                self._insert(
                    self._row(
                        task,
                        "event.suppressed",
                        self._types[task],
                        now,
                        data={"count": count},
                    )
                )
                self._pending[task] = 0
                self._last_delta[task] = self._monotonic()

    def record(
        self,
        task: str,
        kind: str,
        *,
        task_type: str,
        film: str | None = None,
        severity: str = "info",
        pid: int | None = None,
        proc: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        """Observe one activity; logging must never break a producer."""
        rolled = False
        try:
            now = self._clock()
            fields = {
                k: v
                for k, v in {
                    "film": film,
                    "pid": pid,
                    "proc": proc,
                    "data": data,
                }.items()
                if v is not None
            }
            row = _sanitize(
                self._row(task, kind, task_type, now, severity=severity, **fields)
            )
            # Severity is a closed vocabulary even for malformed caller input.
            if row["severity"] not in {"info", "warn", "error"}:
                row["severity"] = "info"
            with self._lock:
                day = max(self._day, now.strftime("%Y%m%d"))
                if day != self._day:
                    self._flush_deltas(now)  # the OLD day's final lines (W2)
                    self._day = day
                    self._load_day()
                    rolled = True
                if kind == "agent.stopping":
                    self._flush_deltas(now)
                if kind == "event.mirrored":
                    task = row["task"]
                    self._types[task] = row["task_type"]
                    count = self._mirrored.get(task, 0)
                    if count >= MIRROR_CAP:
                        if task not in self._capped:
                            self._capped.add(task)
                            self._last_delta[task] = self._monotonic()
                            self._insert(
                                self._row(
                                    task,
                                    "event.suppressed",
                                    row["task_type"],
                                    now,
                                    data={"task": task, "since_cap": True},
                                )
                            )
                        self._pending[task] = self._pending.get(task, 0) + 1
                        if self._monotonic() - self._last_delta[task] >= 3600:
                            self._insert(
                                self._row(
                                    task,
                                    "event.suppressed",
                                    row["task_type"],
                                    now,
                                    data={"count": self._pending[task]},
                                )
                            )
                            self._pending[task] = 0
                            self._last_delta[task] = self._monotonic()
                    else:
                        self._mirrored[task] = count + 1
                        self._insert(row)
                else:
                    self._insert(row)
        except Exception as exc:
            with self._lock:
                self._failed()
            log.warning("Task log record rejected: %s", type(exc).__name__)
        finally:
            self._notify_failure()
            if rolled:
                self.prune()

    def set_on_first_failure(self, callback: Callable[[str], None]) -> None:
        """Wire the queue after boot; deliver an earlier latched failure once."""
        with self._lock:
            self._on_first_failure = callback
        self._notify_failure()

    def _notify_failure(self) -> None:
        callback = None
        with self._lock:
            if (
                self._failure_pending
                and not self._failure_notified
                and self._on_first_failure is not None
            ):
                self._failure_notified = True
                callback = self._on_first_failure
        if callback is not None:
            try:
                callback(
                    "Task log could not be saved; recent entries remain in memory."
                )
            except Exception as exc:
                log.warning("Task log failure callback failed: %s", type(exc).__name__)

    def prune(self) -> None:
        """Best effort outside the store lock. Locked files retry next prune."""
        today = self._clock().date()
        age_cutoff = (today - timedelta(days=30)).strftime("%Y%m%d")
        floor = (today - timedelta(days=6)).strftime("%Y%m%d")
        files: list[tuple[str, Path, int]] = []
        for day, path in sorted(self._files().items()):
            try:
                files.append((day, path, path.stat().st_size))
            except OSError as exc:
                log.warning("Task log stat failed: %s", type(exc).__name__)
        total = sum(size for _, _, size in files)
        for day, path, size in files:
            if day >= floor or day >= self._day:
                continue
            if day < age_cutoff or total > MAX_BYTES:
                try:
                    path.unlink()
                    total -= size
                    with self._lock:
                        self._cache.pop(day, None)
                except OSError as exc:
                    log.warning("Task log prune deferred: %s", type(exc).__name__)

    def _days(self) -> list[str]:
        return sorted(
            set(self._files()) | {_id_key(r["id"])[0] for r in self._ring}, reverse=True
        )

    def _rows(self, day: str) -> list[dict[str, Any]]:
        rows = {r["id"]: r for r in self._read_day(day)}
        rows.update({r["id"]: r for r in self._ring if _id_key(r["id"])[0] == day})
        return sorted(rows.values(), key=lambda row: _id_key(row["id"]))

    def _summary(self, day: str) -> tuple[int, set[str]]:
        if day in self._cache:
            return self._cache[day]
        rows = self._rows(day)
        summary = len(rows), {r.get("task", "") for r in rows}
        # Wall time alone doesn't close a day: its rollover delta is pending.
        # Memory-only counts can shrink on ring eviction, so never cache those.
        if day < self._latest_record_day and self._folder is not None:
            self._cache[day] = summary
        return summary

    @staticmethod
    def _matches(
        row: dict[str, Any], task: str | None, severities: set[str], q: str
    ) -> bool:
        if task is not None and row.get("task") != task:
            return False
        if severities and row.get("severity") not in severities:
            return False
        data = row.get("data") or {}
        labels = [
            row.get("film", ""),
            data.get("title", ""),
            data.get("model", ""),
            data.get("from", ""),
            data.get("to", ""),
            *data.get("by_model", {}).keys(),
        ]
        return not q or any(q in str(label).casefold() for label in labels)

    def query(
        self,
        *,
        day: str | None = None,
        task: str | None = None,
        severity: str | None = None,
        q: str = "",
        before: str | None = None,
        after: str | None = None,
        limit: int = 200,
        days: bool = False,
    ) -> dict[str, Any]:
        """Day/newest, task/across-days, or after/oldest; all include boot."""
        if day is not None and not _valid_day(day):
            raise ValueError("invalid task log day")
        before_key = _id_key(before) if before else None
        after_key = _id_key(after) if after else None
        limit = max(1, min(500, limit))
        severities = set(filter(None, re.split(r"[,\s]+", severity or "")))
        with self._lock:
            available = self._days()
            if days:
                return {
                    "boot": self.boot,
                    "days": [
                        {"day": d, "count": self._summary(d)[0]} for d in available
                    ],
                }
            if after_key:
                selected = [d for d in reversed(available) if d >= after_key[0]]
            elif day:
                selected = [day]
            else:
                selected = available[:30] if task is not None else [self._day]
            found = []
            for d in selected:
                if (
                    task is not None
                    and d in self._cache
                    and task not in self._cache[d][1]
                ):
                    continue
                rows = self._rows(d)
                if d < self._latest_record_day and self._folder is not None:
                    self._cache[d] = len(rows), {r.get("task", "") for r in rows}
                for row in rows if after_key else reversed(rows):
                    key = _id_key(row["id"])
                    if (
                        before_key
                        and key >= before_key
                        or after_key
                        and key <= after_key
                    ):
                        continue
                    if self._matches(row, task, severities, q.casefold()):
                        found.append(row)
                        if len(found) > limit:
                            break
                if len(found) > limit:
                    break
            more = len(found) > limit
            found = found[:limit]
            # Return a detached JSON-safe snapshot, not mutable ring references.
            return {
                "boot": self.boot,
                "entries": _sanitize(found),
                "next_before": found[-1]["id"] if more and not after_key else None,
            }

    def reconcile(self) -> dict[str, Any]:
        """Reconstruct open activities up to the previous session boundary."""
        task_closers = {
            "task.done",
            "task.aborted",
            "task.started",
            "subs.skipped_bulk",
            "operator.stop",
            "operator.remove",
            "operator.update",
        }
        closers = {
            "restore": {
                "restore.finished",
                "restore.failed",
                "restore.retry",
                "restore.skipped",
            },
            "asr": {"asr.finished", "asr.retry"},
            "translate": {"translate.finished", "translate.paused"},
        }
        closed_tasks: set[str] = set()
        closed: set[tuple[str, str | None, str]] = set()
        opened: list[tuple[dict[str, Any], str]] = []
        last_ts = None
        boundary = ""
        with self._lock:
            for day in self._days():
                for row in reversed(self._rows(day)):
                    if last_ts is None:
                        last_ts = row.get("ts", "")
                    kind, task, film = (
                        row.get("kind", ""),
                        row.get("task", ""),
                        row.get("film"),
                    )
                    if kind in {"agent.started", "agent.stopping"}:
                        boundary = kind
                        break
                    if kind in task_closers:
                        closed_tasks.add(task)
                    for step, ends in closers.items():
                        key = task, film, step
                        if kind == f"{step}.started":
                            if task not in closed_tasks and key not in closed:
                                opened.append((row, step))
                            closed.add(key)
                        if (
                            kind in ends
                            or (step != "restore" and kind.startswith("subs."))
                            or (
                                kind == "task.interrupted"
                                and (row.get("data") or {}).get("step", step) == step
                            )
                        ):
                            closed.add(key)
                if boundary:
                    break
        if boundary == "agent.stopping":
            return {"previous_exit": "clean"}
        if last_ts is None:
            return {"previous_exit": "first"}
        for row, step in reversed(opened):
            self.record(
                row.get("task", ""),
                "task.interrupted",
                task_type=row.get("task_type", ""),
                film=row.get("film"),
                severity="warn",
                data={"reconstructed": True, "film": row.get("film"), "step": step},
            )
        return {"previous_exit": "unclean", "last_ts": last_ts}


_holder = TaskLog()


def set_task_log(store: TaskLog | None) -> None:
    """Publish this agent's store; None restores the safe memory-only default."""
    global _holder
    _holder = store if store is not None else TaskLog()


def get_task_log() -> TaskLog:
    return _holder
