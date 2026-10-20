"""Shared pieces of the T04-P1 stage executors (restore / asr / translate).

Executors never write into the intake's work copy: every output goes to the
attempt's own work directory (`JobContext.workdir`, named after the attempt).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path, PurePosixPath

from . import models as m
from .executors import ErrorClass, ExecOutcome, JobContext
from .local_runtime import LocalRuntime
from .stage_results import (
    FileRef,
    StageResultError,
    file_ref,
    load_stage_result,
    main_videos,
)

PROFILE_REVISION = 1
# The tools do not report a version the executor could pin; the capability
# changes through `config_digest` instead.
UNPINNED = "unpinned"


class StageFailure(Exception):
    """Ends a stage early with `outcome` (always a failure)."""

    def __init__(self, error_class: ErrorClass, message: str) -> None:
        super().__init__(message)
        self.outcome = ExecOutcome.failed(error_class, message)


def attention(message: str) -> StageFailure:
    return StageFailure("needs_attention", message)


def cancelled() -> StageFailure:
    return StageFailure("cancelled", "已停止")


def config_digest(settings: dict[str, object]) -> str:
    """sha256 of the executor's effective settings (a change → new capability)."""
    raw = json.dumps(settings, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def tool_versions(
    *, tool: str, model: str, model_version: str, profile: str, settings: dict
) -> m.ToolVersions:
    return m.ToolVersions(
        tool=tool,
        tool_version=UNPINNED,
        model=model,
        model_version=model_version,
        profile=profile,
        profile_revision=PROFILE_REVISION,
        config_digest=config_digest(settings),
    )


def attempt_id(context: JobContext) -> str:
    """The dispatcher names the work directory after the attempt."""
    return context.workdir.name


def input_stem(item: m.ManifestItem) -> str:
    return PurePosixPath(item.relative_path).stem


def input_name(item: m.ManifestItem) -> str:
    return PurePosixPath(item.relative_path).name


def workcopy_ref(
    rt: LocalRuntime, manifest: m.InputManifest, item_id: str
) -> tuple[Path, FileRef]:
    """The work copy of `item_id` and its ref, checked against the manifest."""
    copy = next(
        (c for c in manifest.readiness.workcopy.items if c.source_item_id == item_id),
        None,
    )
    if copy is None:
        raise attention("找不到收件工作副本")
    path = rt.work_dir / copy.relative_path
    try:
        ref = file_ref(rt.work_dir, path)
    except PermissionError:  # held by antivirus / the indexer (H1-2)
        raise StageFailure(
            "transient", f"收件工作副本暂时被占用：{copy.relative_path}"
        ) from None
    except OSError:
        raise attention(f"收件工作副本缺失：{copy.relative_path}") from None
    if (ref.sha256, ref.size) != (copy.sha256, copy.size_bytes):
        raise attention(f"收件工作副本已变化：{copy.relative_path}")
    return path, ref


def output_stems(rt: LocalRuntime, manifest: m.InputManifest) -> dict[str, str]:
    """Per main video, the stem subtitles are named after: the restored video's
    when the film has a restore stage, else the input's (E3)."""
    stems = {video.item_id: input_stem(video) for video in main_videos(manifest)}
    view = rt.store.get_film(rt.operator, manifest.film_id)
    if not any(record.stage == "restore" for record in view.stages):
        return stems
    restore = load_stage_result(rt, manifest.film_id, "restore")
    for video in restore.videos:
        if video.restore is not None and video.video_item_id in stems:
            stems[video.video_item_id] = PurePosixPath(video.restore.file.path).stem
    return stems


def write_text_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def guarded(run: Callable[[], ExecOutcome]) -> ExecOutcome:
    """Run a stage body; `StageFailure` / `StageResultError` become outcomes."""
    try:
        return run()
    except StageFailure as failure:
        return failure.outcome
    except StageResultError as exc:
        return ExecOutcome.failed("needs_attention", str(exc))
