"""P2-2 (#293): the controller's side of both transfer directions (§3.5, §4).

**Download.** A worker reads the route envelope of a film revision routed to
it, then each work copy by item id only (`FileResponse`, Range). **Upload.**
The worker posts a plan `[{path, size, sha256}]` for its attempt, kept in
`work/.incoming/<a>/plan.json`, then one streaming PUT per file from the
received offset (offset CAS). The controller hashes while receiving; on resume
it rebuilds the hash from the `.part` prefix (off the event loop). A
whole-file match is renamed atomically to `work/<path>` and its
`{sha, size, mtime_ns}` recorded; a mismatch resets the file to 0 (S1, C13).

Disk: a plan is admitted only while free − Σ(planned − received of the other
plans) − its own remainder ≥ 100 GB (C8). Each worker has at most one stream
per direction (S5, R17). `validate_finish_refs()` is the C3/C13 check the
finish route will run (P2-3); it is not wired yet (R16).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import threading
from collections.abc import Callable, Iterator
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


class Upload:
    """One PUT into a `.part` from its received offset; blocking calls only
    (the route runs them in a worker thread)."""

    def __init__(
        self,
        transfers: Transfers,
        attempt_id: str,
        planned: PlanFile,
        offset: int,
    ) -> None:
        self.transfers, self.attempt_id, self.planned = transfers, attempt_id, planned
        self.offset = offset
        self.part = transfers.part_path(attempt_id, planned.path)
        self.part.parent.mkdir(parents=True, exist_ok=True)
        self._hash = hash_prefix(self.part, offset) if offset else hashlib.sha256()
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
        self.close()

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.flush()
                os.fsync(self._stream.fileno())
            finally:
                self._stream.close()
                self._stream = None

    def finish(self) -> dict[str, Any]:
        """The whole file is in: commit on a sha match, else reset to 0."""
        self.close()
        if self._hash.hexdigest() != self.planned.sha256:
            self.part.unlink(missing_ok=True)
            count = self.transfers.count_mismatch(self.attempt_id, self.planned.path)
            raise TransferError(422, "hash_mismatch", mismatches=count)
        return self.transfers.commit(self.attempt_id, self.planned)


def _disk_error(exc: OSError) -> TransferError | OSError:
    if exc.errno == errno.ENOSPC:
        return TransferError(507, "controller_disk_full")
    return exc


class Transfers:
    def __init__(
        self, work_dir: Path, *, free_bytes: Callable[[Path], int] = disk_free
    ) -> None:
        self.work_dir = work_dir
        self._free = free_bytes
        self._lock = threading.RLock()
        self._streams: set[tuple[str, str]] = set()

    # ── R17: one stream per worker and direction ─────────────────────────
    @contextmanager
    def stream(self, worker_id: str, direction: str) -> Iterator[None]:
        self.acquire(worker_id, direction)
        try:
            yield
        finally:
            self.release(worker_id, direction)

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
        if not value.startswith(f"{attempt_id}/") or value == (
            f"{attempt_id}/{RESULT_NAME}"
        ):
            raise TransferError(422, "invalid_path")
        root = (self.work_dir / attempt_id).resolve()
        target = (self.work_dir / value).resolve()
        incoming = self.incoming(attempt_id).resolve()
        part = self.part_path(attempt_id, value).resolve()
        if root not in target.parents or incoming not in part.parents:
            raise TransferError(422, "invalid_path")
        return value

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
        """A target renamed in but not recorded (a crash between the two) is
        recorded once its content is proven identical."""
        changed = False
        for f in self._files(plan):
            target = self.work_dir / f.path
            if f.path in plan["committed"] or not target.is_file():
                continue
            if target.stat().st_size != f.size or _sha_file(target) != f.sha256:
                raise TransferError(409, "plan_conflict")
            plan["committed"][f.path] = _record(target, f)
            changed = True
        if changed:
            self._write(attempt_id, plan)

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
        if len({p.casefold() for p in paths}) != len(paths) or any(
            f.size < 0 or not re.fullmatch(r"[0-9a-f]{64}", f.sha256) for f in files
        ):
            raise TransferError(422, "invalid_plan")
        wanted = [{"path": f.path, "size": f.size, "sha256": f.sha256} for f in files]
        with self._lock:
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
            others = sum(
                self._outstanding(other.name, data)
                for other, data in self._plans()
                if other.name != attempt_id
            )
            remainder = self._outstanding(attempt_id, plan)
            free = self._free(self.work_dir)
            if free - others - remainder < CONTROLLER_RESERVE_BYTES:
                raise TransferError(507, "controller_disk_full")
            self._write(attempt_id, plan)
            return self._view(attempt_id, plan)

    def _plans(self) -> list[tuple[Path, dict[str, Any]]]:
        root = self.work_dir / INCOMING
        found = []
        for folder in sorted(root.iterdir()) if root.is_dir() else ():
            try:
                plan = self._read(folder.name)
            except (TransferError, ValueError, KeyError):
                continue
            if plan is not None:
                found.append((folder, plan))
        return found

    def view(self, attempt_id: str) -> dict[str, Any]:
        """GET outputs: each planned file's received offset and state."""
        with self._lock:
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
        """Offset CAS against the received size; the prefix is re-hashed."""
        value = self.check_path(attempt_id, path)
        with self._lock:
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
        try:
            return Upload(self, attempt_id, planned, offset)
        except OSError as exc:
            raise _disk_error(exc) from None

    def count_mismatch(self, attempt_id: str, path: str) -> int:
        with self._lock:
            plan = self._read(attempt_id)
            if plan is None:
                return 0
            count = int(plan["mismatches"].get(path, 0)) + 1
            plan["mismatches"][path] = count
            self._write(attempt_id, plan)
            return count

    def commit(self, attempt_id: str, planned: PlanFile) -> dict[str, Any]:
        """Atomic rename into `work/<path>`, then record {sha, size, mtime_ns}."""
        part = self.part_path(attempt_id, planned.path)
        target = self.work_dir / planned.path
        with self._lock:
            plan = self._read(attempt_id)
            if plan is None:
                raise TransferError(404, "not_found")
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(part, target)
            plan["committed"][planned.path] = _record(target, planned)
            self._write(attempt_id, plan)
            return {"path": planned.path, "received": planned.size, "done": True}

    def committed(self, attempt_id: str) -> dict[str, dict[str, Any]]:
        with self._lock:
            plan = self._read(attempt_id)
            return dict(plan["committed"]) if plan is not None else {}


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
    }


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
