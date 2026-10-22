"""P2-2 (#293): the controller's side of both transfer directions (§3.5, §4).

**Download.** A worker reads the route envelope of a film revision routed to
it, then each work copy by item id only (`FileResponse`, Range). **Upload.**
The worker posts a plan `[{path, size, sha256}]` for its attempt, kept in
`work/.incoming/<a>/plan.json`, then one streaming PUT per file from the
received offset (offset CAS). The controller hashes while receiving. A
whole-file match is renamed atomically to `work/<path>` and its
`{sha, size, mtime_ns}` recorded; a mismatch resets the file to 0 (S1, C13).

**Resume (IR3).** The hash state of a cut stream stays in memory, so the
resumed PUT streams at once. A PUT never re-reads a prefix: without that state
(after a controller restart) it answers 409 `prefix_unverified`, and the next
`GET /outputs` rebuilds it — the worker makes that call with a long timeout.
`GET /outputs` (and an identical plan re-post) also settles what a crash or a
refused rename left: a complete part is verified and committed, or reset to 0
on a mismatch; a refused commit (503 `commit_pending`) is retried there (IR2).

Disk: a plan is admitted only while free − Σ(planned − received of the other
live plans) − its own remainder ≥ 100 GB (C8). The plans of attempts that are
resolved, superseded or unknown are swept (IR5). Each worker has at most one
stream per direction (S5, R17). `validate_finish_refs()` is the C3/C13 check
the finish route will run (P2-3); it is not wired yet (R16).
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import re
import shutil
import threading
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from pydantic import TypeAdapter, ValidationError

from taskpaw_v3.core.state import atomic_json

from . import models as m
from .executors import SUCCEEDED_KIND
from .scheduler import CONTROLLER_RESERVE_BYTES
from .stage_results import RESULT_NAME, FileRef, StageResult

if TYPE_CHECKING:
    from .local_runtime import LocalRuntime

log = logging.getLogger("taskpaw.workflow.transfer")

INCOMING = ".incoming"
PLAN_NAME = "plan.json"
PART = ".part"
HASH_CHUNK = 4 << 20
RETRY_AFTER_SECONDS = 5
MAX_PLAN_FILES = 256
COMPUTE_STAGES = ("restore", "asr", "translate")
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)\Z")
_PATH = TypeAdapter(m.RelativePath)

LiveAttempts = Callable[[], Collection[str]]


class TransferError(Exception):
    """An HTTP-shaped refusal: status, a stable code and optional extras."""

    def __init__(self, status: int, code: str, **extra: Any) -> None:
        super().__init__(code)
        self.status, self.code, self.extra = status, code, extra


@dataclass(frozen=True)
class PlanFile:
    path: str
    size: int
    sha256: str


def disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


def parse_content_range(header: str | None) -> tuple[int, int]:
    """`bytes o-(n-1)/n` → (o, n); the PUT carries the rest of the file."""
    match = _RANGE.fullmatch(header or "")
    if match is None:
        raise TransferError(422, "invalid_range")
    offset, last, total = (int(value) for value in match.groups())
    if total <= 0 or last != total - 1 or offset > last:
        raise TransferError(422, "invalid_range")
    return offset, total


def hash_prefix(path: Path, size: int) -> Any:
    """sha256 state of the first `size` bytes of `path` (a resumed part)."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        left = size
        while left:
            chunk = stream.read(min(HASH_CHUNK, left))
            if not chunk:
                raise TransferError(409, "offset_mismatch", received=size - left)
            digest.update(chunk)
            left -= len(chunk)
    return digest


def _sha_file(path: Path) -> str:
    return hash_prefix(path, path.stat().st_size).hexdigest()


def live_attempts(rt: LocalRuntime) -> frozenset[str]:
    """IR5: the compute attempts that may still upload — the assignment
    unresolved (an `unknown` one included) and the attempt current for its job
    (the C7 rule of `worker_attempt`, for every worker)."""
    found = set()
    for open_ in rt.store.list_open_assignments(rt.operator):
        assignment = open_.assignment
        if open_.stage not in COMPUTE_STAGES:
            continue
        stage = rt.store.get_stage(rt.operator, assignment.job_id)
        if stage.attempt_id == assignment.attempt_id:
            found.add(assignment.attempt_id)
    return frozenset(found)


class Upload:
    """One PUT into a `.part` from its received offset; blocking calls only
    (the route runs them in a worker thread). `digest` is the hash state at
    `offset`: the stream never re-reads its prefix (IR3)."""

    def __init__(
        self,
        transfers: Transfers,
        attempt_id: str,
        planned: PlanFile,
        offset: int,
        digest: Any,
    ) -> None:
        self.transfers, self.attempt_id, self.planned = transfers, attempt_id, planned
        self.offset = offset
        self.part = transfers.part_path(attempt_id, planned.path)
        self.part.parent.mkdir(parents=True, exist_ok=True)
        self._start = digest.copy()
        self._hash = digest
        self._stream: BinaryIO | None = self.part.open("r+b" if offset else "wb")
        self._stream.truncate(offset)
        self._stream.seek(offset)
        self.written = 0

    def write(self, chunk: bytes) -> None:
        assert self._stream is not None
        try:
            self._stream.write(chunk)
        except OSError as exc:
            raise _disk_error(exc) from None
        self._hash.update(chunk)
        self.written += len(chunk)

    def truncate(self) -> None:
        """413: the body ran past the file; back to where it started."""
        if self._stream is not None:
            self._stream.truncate(self.offset)
            self._hash, self.written = self._start.copy(), 0
        self.close()

    def close(self) -> None:
        """Flush what arrived and keep its hash state for the resume (IR3,
        IR11: a dead or cut stream keeps every byte already received)."""
        if self._stream is None:
            return
        try:
            self._stream.flush()
            os.fsync(self._stream.fileno())
        finally:
            self._stream.close()
            self._stream = None
            self.transfers._closed(self.attempt_id, self.planned.path, self._hash)

    def finish(self) -> dict[str, Any]:
        """The whole file is in: commit on a sha match, else reset to 0."""
        self.close()
        if self._hash.hexdigest() != self.planned.sha256:
            self.transfers.reset(self.attempt_id, self.planned.path)
            count = self.transfers.count_mismatch(self.attempt_id, self.planned.path)
            raise TransferError(422, "hash_mismatch", mismatches=count)
        return self.transfers.commit(self.attempt_id, self.planned)


def _disk_error(exc: OSError) -> TransferError | OSError:
    if exc.errno == errno.ENOSPC:
        return TransferError(507, "controller_disk_full")
    return exc


class Transfers:
    """Upload plans and parts under `work/.incoming`, the in-memory hash state
    of the parts and the stream slots. `live`: the attempts that may still
    upload (`live_attempts`); without it every plan counts and none is swept."""

    def __init__(
        self,
        work_dir: Path,
        *,
        free_bytes: Callable[[Path], int] = disk_free,
        live: LiveAttempts | None = None,
    ) -> None:
        self.work_dir = work_dir
        self._free = free_bytes
        self._live = live
        self._lock = threading.RLock()  # slots, hash states, the disk gate
        self._locks: dict[str, threading.RLock] = {}  # one per attempt
        self._streams: set[tuple[str, str]] = set()
        # (attempt, path) → (size, mtime_ns, sha256 state) of a closed part
        self._digests: dict[tuple[str, str], tuple[int, int, Any]] = {}
        self._uploading: set[tuple[str, str]] = set()
        # CX15: a counter of plan writes and each attempt's latest one
        self._writes = 0
        self._written: dict[str, int] = {}

    # ── R17: one stream per worker and direction ─────────────────────────
    def acquire(self, worker_id: str, direction: str) -> None:
        with self._lock:
            if (worker_id, direction) in self._streams:
                raise TransferError(429, "busy", retry_after=RETRY_AFTER_SECONDS)
            self._streams.add((worker_id, direction))

    def release(self, worker_id: str, direction: str) -> None:
        with self._lock:
            self._streams.discard((worker_id, direction))

    # ── paths ────────────────────────────────────────────────────────────
    def incoming(self, attempt_id: str) -> Path:
        return self.work_dir / INCOMING / attempt_id

    def part_path(self, attempt_id: str, path: str) -> Path:
        return self.incoming(attempt_id) / f"{path}{PART}"

    def check_path(self, attempt_id: str, path: str) -> str:
        """§4: a canonical relative path under `<a>/`, never the ledger itself,
        resolving under `work/<a>` (no `..`, `\\`, ADS, reserved names, NFC)."""
        try:
            value = _PATH.validate_python(path)
        except ValidationError:
            raise TransferError(422, "invalid_path") from None
        # NTFS is case-insensitive: no spelling of the ledger is an output
        if (
            not value.startswith(f"{attempt_id}/")
            or value.casefold() == f"{attempt_id}/{RESULT_NAME}".casefold()
        ):
            raise TransferError(422, "invalid_path")
        root = (self.work_dir / attempt_id).resolve()
        target = (self.work_dir / value).resolve()
        incoming = self.incoming(attempt_id).resolve()
        part = self.part_path(attempt_id, value).resolve()
        if root not in target.parents or incoming not in part.parents:
            raise TransferError(422, "invalid_path")
        return value

    # ── locks, hash states, sweep ────────────────────────────────────────
    @contextmanager
    def _attempt(self, attempt_id: str) -> Iterator[None]:
        """One attempt's plan and parts; a long hash blocks no other worker.
        Order: an attempt lock, then `_lock` (never the other way round)."""
        with self._lock:
            lock = self._locks.setdefault(attempt_id, threading.RLock())
        with lock:
            yield

    def _closed(self, attempt_id: str, path: str, digest: Any) -> None:
        part = self.part_path(attempt_id, path)
        with self._lock:
            self._uploading.discard((attempt_id, path))
            try:
                stat = part.stat()
            except OSError:
                self._digests.pop((attempt_id, path), None)
                return
            self._digests[attempt_id, path] = (
                stat.st_size,
                stat.st_mtime_ns,
                digest.copy(),
            )

    def _digest(self, attempt_id: str, path: str) -> Any | None:
        """The kept hash state of a part, if the part is unchanged since."""
        try:
            stat = self.part_path(attempt_id, path).stat()
        except OSError:
            return None
        with self._lock:
            kept = self._digests.get((attempt_id, path))
        if kept is None or kept[:2] != (stat.st_size, stat.st_mtime_ns):
            return None
        return kept[2].copy()

    def reset(self, attempt_id: str, path: str) -> None:
        """A whole-file mismatch: the part goes back to 0."""
        self.part_path(attempt_id, path).unlink(missing_ok=True)
        with self._lock:
            self._digests.pop((attempt_id, path), None)

    def _live_ids(self) -> frozenset[str] | None:
        if self._live is None:
            return None
        try:
            return frozenset(self._live())
        except m.WorkflowError:
            log.warning("upload plans: the store could not list live attempts")
            return None

    def sweep(self) -> None:
        """IR5: delete the plans and parts of attempts that can no longer
        upload — resolved, not current for their job, or unknown to the store
        — so they stop reserving disk (start, plan creation, GET outputs).
        CX15: each folder is re-checked under its attempt lock right before
        it goes (a fresh liveness read, no upload holding it, no plan written
        since the snapshot), so a claim, plan or PUT landing between the
        snapshot and the scan is never deleted."""
        with self._lock:
            started = self._writes
        live = self._live_ids()
        root = self.work_dir / INCOMING
        if live is None or not root.is_dir():
            return
        with self._lock:
            busy = {attempt for attempt, _ in self._uploading}
        for folder in root.iterdir():
            attempt_id = folder.name
            if attempt_id in live or attempt_id in busy:
                continue
            with self._attempt(attempt_id):
                if not self._sweepable(attempt_id, started):
                    continue
                shutil.rmtree(folder, ignore_errors=True)
            with self._lock:
                self._locks.pop(attempt_id, None)
                self._written.pop(attempt_id, None)
                for key in [k for k in self._digests if k[0] == attempt_id]:
                    del self._digests[key]
            log.info("swept the upload plan of settled attempt %s", attempt_id)

    def _sweepable(self, attempt_id: str, started: int) -> bool:
        """CX15, under the attempt's lock: still not live on a fresh read, no
        stream holds it and its plan (if any) predates the sweep's snapshot."""
        fresh = self._live_ids()
        if fresh is None or attempt_id in fresh:
            return False
        with self._lock:
            if any(attempt == attempt_id for attempt, _ in self._uploading):
                return False
            return self._written.get(attempt_id, -1) <= started

    # ── plans ────────────────────────────────────────────────────────────
    def _plan_file(self, attempt_id: str) -> Path:
        return self.incoming(attempt_id) / PLAN_NAME

    def _read(self, attempt_id: str) -> dict[str, Any] | None:
        path = self._plan_file(attempt_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except ValueError:
            raise TransferError(409, "plan_conflict") from None
        if not isinstance(data, dict) or data.get("attempt_id") != attempt_id:
            raise TransferError(409, "plan_conflict")
        return data

    def _write(self, attempt_id: str, plan: dict[str, Any]) -> None:
        atomic_json(self._plan_file(attempt_id), plan)
        with self._lock:  # CX15: a sweep that started before this keeps it
            self._writes += 1
            self._written[attempt_id] = self._writes

    @staticmethod
    def _files(plan: dict[str, Any]) -> list[PlanFile]:
        return [PlanFile(f["path"], f["size"], f["sha256"]) for f in plan["files"]]

    def _received(self, attempt_id: str, plan: dict[str, Any], f: PlanFile) -> int:
        if f.path in plan["committed"]:
            return f.size
        try:
            return self.part_path(attempt_id, f.path).stat().st_size
        except FileNotFoundError:
            return 0

    def _settle(self, attempt_id: str, plan: dict[str, Any]) -> None:
        """Under the attempt's lock. A target renamed in but not recorded (a
        crash between the two) is recorded once its content is proven
        identical. A part no stream holds is settled (IR2): too long → reset;
        complete → verified, then committed or reset on a mismatch; a refused
        rename stays for the next request. A partial part without a kept hash
        state gets one (IR3: the slow re-hash happens here, never in a PUT)."""
        changed = False
        for f in self._files(plan):
            if f.path in plan["committed"]:
                continue
            target = self.work_dir / f.path
            if target.is_file():
                if target.stat().st_size != f.size or _sha_file(target) != f.sha256:
                    raise TransferError(409, "plan_conflict")
                plan["committed"][f.path] = _record(target, f)
                self.reset(attempt_id, f.path)
                changed = True
                continue
            with self._lock:
                if (attempt_id, f.path) in self._uploading:
                    continue
            part = self.part_path(attempt_id, f.path)
            try:
                size = part.stat().st_size
            except FileNotFoundError:
                continue
            if size > f.size:
                self.reset(attempt_id, f.path)
                continue
            digest = self._digest(attempt_id, f.path)
            if digest is None:
                digest = hash_prefix(part, size)
                self._closed(attempt_id, f.path, digest)
            if size < f.size:
                continue
            if digest.hexdigest() != f.sha256:
                self.reset(attempt_id, f.path)
                count = int(plan["mismatches"].get(f.path, 0)) + 1
                plan["mismatches"][f.path] = count
                changed = True
                continue
            try:
                self._rename(part, target)
            except OSError:
                log.warning("commit of an uploaded file refused; retried later")
                continue
            plan["committed"][f.path] = _record(target, f)
            self.reset(attempt_id, f.path)
            changed = True
        if changed:
            self._write(attempt_id, plan)

    def _rename(self, part: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(part, target)

    def _outstanding(self, attempt_id: str, plan: dict[str, Any]) -> int:
        return sum(
            max(0, f.size - self._received(attempt_id, plan, f))
            for f in self._files(plan)
        )

    def put_plan(self, attempt_id: str, files: list[PlanFile]) -> dict[str, Any]:
        """POST outputs: a new plan, or an identical re-post's offsets."""
        if not files or len(files) > MAX_PLAN_FILES:
            raise TransferError(422, "invalid_plan")
        paths = [self.check_path(attempt_id, f.path) for f in files]
        folded = {p.casefold() for p in paths}
        folders = {  # a file cannot also be another file's folder, nor its part's
            "/".join(p.split("/")[:end])
            for p in folded
            for end in range(2, p.count("/") + 1)
        }
        if (
            len(folded) != len(paths)
            or folders & (folded | {p + PART for p in folded})
            or any(
                f.size < 0 or not re.fullmatch(r"[0-9a-f]{64}", f.sha256) for f in files
            )
        ):
            raise TransferError(422, "invalid_plan")
        wanted = [{"path": f.path, "size": f.size, "sha256": f.sha256} for f in files]
        self.sweep()
        with self._attempt(attempt_id):
            plan = self._read(attempt_id)
            if plan is not None:
                if plan["files"] != wanted:
                    raise TransferError(409, "plan_conflict")
                self._settle(attempt_id, plan)
                return self._view(attempt_id, plan)
            plan = {
                "attempt_id": attempt_id,
                "files": wanted,
                "committed": {},
                "mismatches": {},
            }
            self._settle(attempt_id, plan)  # a target present must be identical
            for f in files:  # an empty output has no PUT: it is complete now
                target = self.work_dir / f.path
                if f.size == 0 and f.path not in plan["committed"]:
                    if f.sha256 != EMPTY_SHA256:
                        raise TransferError(422, "invalid_plan")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.touch()
                    plan["committed"][f.path] = _record(target, f)
            with self._lock:  # one admission at a time (C8)
                live = self._live_ids()
                others = sum(
                    self._outstanding(other.name, data)
                    for other, data in self._plans(live)
                    if other.name != attempt_id
                )
                remainder = self._outstanding(attempt_id, plan)
                self.work_dir.mkdir(parents=True, exist_ok=True)
                free = self._free(self.work_dir)
                if free - others - remainder < CONTROLLER_RESERVE_BYTES:
                    raise TransferError(507, "controller_disk_full")
                self._write(attempt_id, plan)
            return self._view(attempt_id, plan)

    def _plans(
        self, live: frozenset[str] | None = None
    ) -> list[tuple[Path, dict[str, Any]]]:
        """The plans that still reserve disk: those of live attempts (IR5)."""
        root = self.work_dir / INCOMING
        found = []
        for folder in sorted(root.iterdir()) if root.is_dir() else ():
            if live is not None and folder.name not in live:
                continue
            try:
                plan = self._read(folder.name)
            except (TransferError, ValueError, KeyError):
                continue
            if plan is not None:
                found.append((folder, plan))
        return found

    def committed(self, attempt_id: str) -> dict[str, dict[str, Any]]:
        """P2-3 finish: the plan's committed outputs `{path: {sha256, size,
        mtime_ns}}` after settling it; empty without a plan."""
        with self._attempt(attempt_id):
            plan = self._read(attempt_id)
            if plan is None:
                return {}
            self._settle(attempt_id, plan)
            return dict(plan["committed"])

    def view(self, attempt_id: str) -> dict[str, Any]:
        """GET outputs: each planned file's received offset and state, after
        settling its parts (IR2, IR3: this call may hash for minutes)."""
        self.sweep()
        with self._attempt(attempt_id):
            plan = self._read(attempt_id)
            if plan is None:
                raise TransferError(404, "not_found")
            self._settle(attempt_id, plan)
            return self._view(attempt_id, plan)

    def _view(self, attempt_id: str, plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "attempt_id": attempt_id,
            "files": [
                {
                    "path": f.path,
                    "size": f.size,
                    "sha256": f.sha256,
                    "received": self._received(attempt_id, plan, f),
                    "done": f.path in plan["committed"],
                    "mismatches": int(plan["mismatches"].get(f.path, 0)),
                }
                for f in self._files(plan)
            ],
        }

    # ── one PUT ──────────────────────────────────────────────────────────
    def open_upload(
        self, attempt_id: str, path: str, offset: int, total: int
    ) -> Upload:
        """Offset CAS against the received size. A resume needs the part's
        kept hash state; without one it is 409 `prefix_unverified` and the
        worker's next GET outputs rebuilds it (IR3)."""
        value = self.check_path(attempt_id, path)
        with self._attempt(attempt_id):
            plan = self._read(attempt_id)
            planned = next(
                (f for f in (self._files(plan) if plan else ()) if f.path == value),
                None,
            )
            if plan is None or planned is None:
                raise TransferError(404, "not_found")
            if total != planned.size:
                raise TransferError(422, "invalid_range")
            received = self._received(attempt_id, plan, planned)
            if value in plan["committed"] or offset != received:
                raise TransferError(409, "offset_mismatch", received=received)
            digest = self._digest(attempt_id, value) if offset else hashlib.sha256()
            if digest is None:
                raise TransferError(409, "prefix_unverified", received=received)
            with self._lock:
                self._uploading.add((attempt_id, value))
            try:
                return Upload(self, attempt_id, planned, offset, digest)
            except OSError as exc:
                with self._lock:
                    self._uploading.discard((attempt_id, value))
                raise _disk_error(exc) from None

    def count_mismatch(self, attempt_id: str, path: str) -> int:
        with self._attempt(attempt_id):
            plan = self._read(attempt_id)
            if plan is None:
                return 0
            count = int(plan["mismatches"].get(path, 0)) + 1
            plan["mismatches"][path] = count
            self._write(attempt_id, plan)
            return count

    def commit(self, attempt_id: str, planned: PlanFile) -> dict[str, Any]:
        """Atomic rename into `work/<path>`, then record {sha, size, mtime_ns}.
        A refused rename keeps the verified part and its hash state: 503
        `commit_pending`, retried by the next GET outputs (IR2)."""
        part = self.part_path(attempt_id, planned.path)
        target = self.work_dir / planned.path
        with self._attempt(attempt_id):
            plan = self._read(attempt_id)
            if plan is None:
                raise TransferError(404, "not_found")
            try:
                self._rename(part, target)
            except OSError:
                log.warning("commit of an uploaded file refused; retried later")
                raise TransferError(
                    503, "commit_pending", retry_after=RETRY_AFTER_SECONDS
                ) from None
            plan["committed"][planned.path] = _record(target, planned)
            self._write(attempt_id, plan)
            self.reset(attempt_id, planned.path)
            return {"path": planned.path, "received": planned.size, "done": True}


def _record(target: Path, planned: PlanFile) -> dict[str, Any]:
    stat = target.stat()
    return {
        "sha256": planned.sha256,
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


# ── download side ───────────────────────────────────────────────────────
def _routed(
    rt: LocalRuntime, worker_id: str, film_id: str, revision: int
) -> m.FilmRoute:
    """S-4: only a film revision with an open route to the caller."""
    try:
        route = rt.store.film_route(rt.operator, film_id, revision)
    except m.WorkflowError:
        route = None
    if route is None or route.worker_id != worker_id:
        raise TransferError(404, "not_routed")
    return route


def _ledgers(rt: LocalRuntime, film_id: str, revision: int) -> dict[str, Any]:
    """Each succeeded compute stage's ledger text with its digest (D2)."""
    view = rt.store.get_film(rt.operator, film_id)
    if view.film.current_input_revision != revision:
        return {}
    ledgers: dict[str, Any] = {}
    for stage in view.stages:
        if (
            stage.stage not in COMPUTE_STAGES
            or stage.status != "succeeded"
            or stage.attempt_id is None
        ):
            continue
        attempt = next(
            (
                a
                for a in rt.store.list_attempts(rt.operator, stage.job_id)
                if a.attempt_id == stage.attempt_id
            ),
            None,
        )
        digests = [
            e.payload_digest
            for e in (attempt.evidence if attempt else ())
            if e.kind == SUCCEEDED_KIND
        ]
        path = rt.work_dir / stage.attempt_id / RESULT_NAME
        if not digests or not path.is_file():
            continue
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digests[0]:
            continue
        ledgers[stage.stage] = {
            "attempt_id": stage.attempt_id,
            "payload_digest": digests[0],
            "text": raw.decode("utf-8"),
        }
    return ledgers


def envelope(
    rt: LocalRuntime, worker_id: str, film_id: str, revision: int
) -> dict[str, Any]:
    """R11: the route envelope of a film revision routed to the caller."""
    route = _routed(rt, worker_id, film_id, revision)
    manifest = rt.store.get_input_manifest(rt.operator, film_id, revision)
    plan = next(
        (
            p
            for p in rt.store.list_plans(rt.operator)
            if (p.film_id, p.input_revision) == (film_id, revision)
        ),
        None,
    )
    return {
        "film": film_id,
        "rev": revision,
        "route": {"id": route.route_id, "version": route.version},
        "ltx_model": plan.ltx_model if plan is not None else None,
        "manifest": manifest.model_dump(mode="json"),
        "workcopy": [
            {
                "item_id": copy.source_item_id,
                "relative_path": copy.relative_path,
                "size": copy.size_bytes,
                "sha256": copy.sha256,
            }
            for copy in manifest.readiness.workcopy.items
        ],
        "ledgers": _ledgers(rt, film_id, revision),
        # P2-3: the claim carries the job's id and version verbatim
        "stages": _stages(rt, film_id, revision),
        "name": _name(rt, film_id, manifest),
    }


def _stages(rt: LocalRuntime, film_id: str, revision: int) -> dict[str, Any]:
    view = rt.store.get_film(rt.operator, film_id)
    return {
        stage.stage: {
            "job_id": stage.job_id,
            "version": stage.version,
            "status": stage.status,
        }
        for stage in view.stages
        if stage.stage in ("restore", "asr") and stage.input_revision == revision
    }


def _name(rt: LocalRuntime, film_id: str, manifest: m.InputManifest) -> str:
    """The film's shown name for the worker's monitor (H3 D06)."""
    from . import board

    try:
        return board.name_of(rt, film_id, manifest) or film_id
    except Exception:  # a name never stops the envelope
        log.exception("film name unavailable for %s", film_id)
        return film_id


def item_file(
    rt: LocalRuntime, worker_id: str, film_id: str, revision: int, item_id: str
) -> tuple[Path, m.WorkcopyItem]:
    """A work copy resolved only by item id through the manifest (§2)."""
    _routed(rt, worker_id, film_id, revision)
    manifest = rt.store.get_input_manifest(rt.operator, film_id, revision)
    copy = next(
        (c for c in manifest.readiness.workcopy.items if c.source_item_id == item_id),
        None,
    )
    if copy is None:
        raise TransferError(404, "not_found")
    path = rt.work_dir / copy.relative_path
    if not path.is_file() or path.stat().st_size != copy.size_bytes:
        raise TransferError(404, "not_found")
    return path, copy


# ── finish refs (C3, C13): unit-tested, wired by the finish route (P2-3) ─
def ledger_refs(result: StageResult) -> list[FileRef]:
    refs: list[FileRef] = []
    for video in result.videos:
        if video.restore is not None:
            refs.append(video.restore.file)
        if video.subtitle is not None:
            refs.extend(ref for ref in (video.subtitle.ja, video.subtitle.zh) if ref)
    return refs


def validate_finish_refs(
    work_dir: Path,
    raw: bytes,
    *,
    attempt_id: str,
    film_id: str,
    input_revision: int,
    stage: str,
    manifest: m.InputManifest,
    committed: dict[str, dict[str, Any]],
) -> StageResult:
    """Each `FileRef` is (a) committed under `<a>/` with its recorded sha and
    size and an unchanged size and mtime_ns, or (b) equal in (path, sha, size)
    to a manifest work-copy item. The ledger names this film, revision, stage
    and attempt."""
    try:
        result = StageResult.model_validate_json(raw)
    except ValidationError:
        raise TransferError(422, "invalid_ledger") from None
    if (result.film_id, result.input_revision, result.stage, result.attempt_id) != (
        film_id,
        input_revision,
        stage,
        attempt_id,
    ):
        raise TransferError(422, "ledger_mismatch")
    copies = {
        (c.relative_path, c.sha256, c.size_bytes)
        for c in manifest.readiness.workcopy.items
    }
    for ref in ledger_refs(result):
        if (ref.path, ref.sha256, ref.size) in copies:
            continue
        record = committed.get(ref.path)
        if (
            record is None
            or not ref.path.startswith(f"{attempt_id}/")
            or (record["sha256"], record["size"]) != (ref.sha256, ref.size)
        ):
            raise TransferError(422, "unverified_output", path=ref.path)
        try:
            stat = (work_dir / ref.path).stat()
        except OSError:
            raise TransferError(422, "unverified_output", path=ref.path) from None
        if (stat.st_size, stat.st_mtime_ns) != (record["size"], record["mtime_ns"]):
            raise TransferError(422, "unverified_output", path=ref.path)
    return result
