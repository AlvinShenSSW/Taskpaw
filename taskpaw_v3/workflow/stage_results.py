"""Per-stage output ledger for the local pipeline (T04-P1 E4).

Each compute stage writes `work/<attempt_id>/stage-result.json`; the sha256 of
those exact bytes is the `payload_digest` of the attempt's success evidence.
A later stage reads only the current succeeded attempt's file and checks the
digest first, so a stale or altered result is never used.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict

from . import models as m
from .executors import SUCCEEDED_KIND
from .local_runtime import LocalRuntime

RESULT_NAME = "stage-result.json"


class StageResultError(Exception):
    """The ledger cannot vouch for a stage's outputs (→ needs_attention)."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class FileRef(_Frozen):
    path: str  # POSIX, relative to the work root
    sha256: str
    size: int


class RestoreFacts(_Frozen):
    decision: Literal["applied", "reused"]
    file: FileRef
    width: int
    height: int
    duration_ms: int
    video_streams: int
    audio_streams: int


class SubtitleFacts(_Frozen):
    decision: Literal["reused_zh", "source_ja", "generated_ja", "no_speech"]
    ja: FileRef | None = None
    zh: FileRef | None = None
    kept_ja: int | None = None


class VideoResult(_Frozen):
    video_item_id: str
    volume_ordinal: int | None
    restore: RestoreFacts | None = None
    subtitle: SubtitleFacts | None = None


class StageResult(_Frozen):
    schema_version: Literal[1] = 1
    film_id: str
    input_revision: int
    stage: Literal["restore", "asr", "translate"]
    attempt_id: str
    videos: tuple[VideoResult, ...]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def relative(work_root: Path, path: Path) -> str:
    return PurePosixPath(path.resolve().relative_to(work_root.resolve())).as_posix()


def file_ref(work_root: Path, path: Path) -> FileRef:
    return FileRef(
        path=relative(work_root, path),
        sha256=sha256_file(path),
        size=path.stat().st_size,
    )


def check_file(work_root: Path, ref: FileRef) -> Path:
    """The file behind `ref`, after confirming size and content are unchanged."""
    path = work_root / ref.path
    if (
        not path.is_file()
        or path.stat().st_size != ref.size
        or sha256_file(path) != ref.sha256
    ):
        raise StageResultError(f"产物已变化或缺失：{ref.path}")
    return path


def write_stage_result(workdir: Path, result: StageResult) -> str:
    """Write the ledger atomically; return the sha256 of the written bytes."""
    raw = json.dumps(
        result.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    target = workdir / RESULT_NAME
    tmp = workdir / f".{RESULT_NAME}.tmp"
    with tmp.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, target)
    return hashlib.sha256(raw).hexdigest()


def load_stage_result(rt: LocalRuntime, film_id: str, stage: str) -> StageResult:
    """The current succeeded attempt's ledger of `stage`, digest-checked."""
    view = rt.store.get_film(rt.operator, film_id)
    record = next((s for s in view.stages if s.stage == stage), None)
    if record is None or record.status != "succeeded" or record.attempt_id is None:
        raise StageResultError(f"{stage} 阶段尚未成功")
    attempt = next(
        (
            a
            for a in rt.store.list_attempts(rt.operator, record.job_id)
            if a.attempt_id == record.attempt_id
        ),
        None,
    )
    digests = [
        e.payload_digest
        for e in (attempt.evidence if attempt else ())
        if e.kind == SUCCEEDED_KIND
    ]
    path = rt.work_dir / record.attempt_id / RESULT_NAME
    if not digests or not path.is_file():
        raise StageResultError(f"{stage} 阶段没有可核对的结果")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digests[0]:
        raise StageResultError(f"{stage} 阶段结果与记录不符")
    result = StageResult.model_validate_json(raw)
    if (
        result.film_id != film_id
        or result.attempt_id != record.attempt_id
        or result.input_revision != record.input_revision
    ):
        raise StageResultError(f"{stage} 阶段结果属于其他影片或版本")
    return result


def main_videos(manifest: m.InputManifest) -> list[m.ManifestItem]:
    """Main videos in volume order (unnumbered single videos first)."""
    videos = [item for item in manifest.items if item.role == "main_video"]
    return sorted(
        videos, key=lambda item: (item.volume_ordinal or 0, item.relative_path)
    )


def workcopy_path(rt: LocalRuntime, manifest: m.InputManifest, item_id: str) -> Path:
    """The intake's private copy of an input item (never the user's original)."""
    for copy in manifest.readiness.workcopy.items:
        if copy.source_item_id == item_id:
            return rt.work_dir / copy.relative_path
    raise StageResultError("找不到收件工作副本")
