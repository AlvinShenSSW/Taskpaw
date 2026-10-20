"""Restore stage: Jasna LTX per main video, in volume order (T04-P1 E1, E8, §3.3).

The launch reuses the Jasna plugin's pure pieces: its preset (`preset_catalogue`,
model ltx, `standard`), `build_argv`, the LTX model check, the staging /
published names, the hev1→hvc1 retag and the LTX progress parser. A video whose
name already carries the plugin's restored suffix is `reused`, never restored
again. A failed LTX run is `needs_attention` and never relaunched (#265 D2).
"""

from __future__ import annotations

import json
import math
import os
import shutil
import threading
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from taskpaw_v3.monitors.plugins import jasna
from taskpaw_v3.monitors.plugins.jasna_presets import preset_catalogue
from taskpaw_v3.monitors.plugins.jasna_progress import parse_jasna_progress_line

from . import models as m
from .exec_common import (
    MISSING_LTX,
    attempt_id,
    attention,
    cancelled,
    guarded,
    input_name,
    tool_versions,
    workcopy_ref,
)
from .executors import ComputeStage, ExecOutcome, JobContext
from .local_gpu import FULL_HD, max_resolution
from .local_runtime import LocalRuntime
from .owned_process import OwnedJob
from .stage_results import (
    FileRef,
    RestoreFacts,
    StageResult,
    VideoResult,
    file_ref,
    main_videos,
    workcopy_path,
    write_stage_result,
)

PROFILE_ID = "workflow-restore-ltx"
TIER: jasna.Tier = "1080p"  # T04-P1 runs on the controller's 8 GB card only
DURATION_SLACK_MS = 2000
DURATION_SLACK_SHARE = 0.02
LTX_WORK_DIRNAME = ".jasna-work"
# Without tags every JSON line stays far below the 4096-character line cap.
FFPROBE_ARGS = (
    "-v",
    "error",
    "-show_entries",
    "format=duration:stream=index,codec_type,width,height,duration"
    ":stream_disposition=attached_pic",
    "-of",
    "json",
)
_TAIL_LINES = 8

# The plugin's restored names (#187): `<stem>-破解.mp4`, legacy `<stem>_restored.mp4`.
_RESTORED_SUFFIXES = tuple(
    suffix.casefold() for suffix in (jasna._FINAL_SUFFIX, jasna._LEGACY_FINAL_SUFFIX)
)

GpuQuery = Callable[[], "list[tuple[float, int]] | None"]


def is_restored_name(name: str) -> bool:
    """Whether a file name is already a Jasna restore result (plugin rule)."""
    return name.casefold().endswith(_RESTORED_SUFFIXES)


def workflow_jasna_config(gpu_id: str) -> jasna.JasnaConfig:
    """The `standard` LTX preset of `gpu_id`; no exe path (skips folder checks)."""
    for profile in preset_catalogue()["profiles"]:
        if profile["model"] == "ltx" and profile["gpu"] == gpu_id:
            return jasna.JasnaConfig(
                name="workflow", jasna_exe_path="", **profile["standard"]
            )
    raise ValueError(f"no LTX preset for GPU {gpu_id!r}")


@dataclass(frozen=True)
class ProbeFacts:
    width: int | None
    height: int | None
    duration_ms: int | None
    video_streams: int
    audio_streams: int


def _seconds(value: object) -> float | None:
    try:
        seconds = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _dimension(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def parse_probe(text: str) -> ProbeFacts | None:
    """Stream counts, size and duration from `ffprobe -of json` output."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    streams = [s for s in data.get("streams") or () if isinstance(s, dict)]

    def cover(stream: dict) -> bool:
        disposition = stream.get("disposition")
        return isinstance(disposition, dict) and disposition.get("attached_pic") == 1

    videos = [s for s in streams if s.get("codec_type") == "video" and not cover(s)]
    audio = sum(1 for s in streams if s.get("codec_type") == "audio")
    first = videos[0] if videos else {}
    fmt = data.get("format")
    seconds = _seconds(fmt.get("duration")) if isinstance(fmt, dict) else None
    if seconds is None and videos:
        seconds = _seconds(first.get("duration"))
    return ProbeFacts(
        width=_dimension(first.get("width")),
        height=_dimension(first.get("height")),
        duration_ms=None if seconds is None else round(seconds * 1000),
        video_streams=len(videos),
        audio_streams=audio,
    )


def progress_text(volume: str, state: dict) -> str:
    """Short status: volume · LTX stage · percent · ETA (E8)."""
    parts = [f"修复{volume}"]
    if stage := state.get("ltx_stage"):
        parts.append(f"阶段 {stage}")
    percent = state.get("percent")
    if percent is not None:
        parts.append(f"{percent}%")
    if eta := state.get("eta"):
        parts.append(f"剩余 {eta}")
    return " · ".join(parts)


def probe_video(
    processes: OwnedJob,
    ffprobe: Sequence[str] | None,
    path: Path,
    cwd: Path,
    cancel: threading.Event,
) -> ProbeFacts:
    """ffprobe one video inside the attempt's Job (restore and M01 scrape)."""
    if ffprobe is None:
        raise attention("找不到 ffprobe，无法核对视频")
    lines: list[str] = []
    lock = threading.Lock()

    def on_line(stream: str, text: str) -> None:
        if stream == "stdout":
            with lock:
                lines.append(text)

    child = processes.launch(
        [*ffprobe, *FFPROBE_ARGS, "-i", str(path)], cwd=str(cwd), on_line=on_line
    )
    code = child.wait(cancel)
    if code is None:
        raise cancelled()
    with lock:
        facts = parse_probe("\n".join(lines)) if code == 0 else None
    if facts is None:
        raise attention(f"ffprobe 无法读取：{path.name}")
    return facts


def ffprobe_command(value: str | Sequence[str] | None) -> list[str] | None:
    """The ffprobe command: a path, an argv prefix, or `ffprobe` on PATH."""
    if value is None:
        found = shutil.which("ffprobe")
        return [found] if found else None
    if isinstance(value, str):
        return [value]
    return list(value) or None


class RestoreExecutor:
    """Jasna LTX restore of every main video (uses the GPU unless all reused)."""

    stage: ComputeStage = "restore"
    uses_gpu = True

    def __init__(
        self,
        runtime: LocalRuntime,
        jasna_exe: str,
        gpu_id: str = "5060-8",
        *,
        ffprobe: str | Sequence[str] | None = None,
        launch_prefix: Sequence[str] = (),
        gpus: GpuQuery = jasna.query_gpus,
    ) -> None:
        """`ffprobe`: path or command (default: `ffprobe` on PATH).
        `launch_prefix` goes before the Jasna argv (tests run a fake tool)."""
        self.runtime, self.jasna_exe, self.gpu_id = runtime, jasna_exe, gpu_id
        self.cfg = workflow_jasna_config(gpu_id)
        self.ffprobe = ffprobe_command(ffprobe)
        self.launch_prefix = tuple(launch_prefix)
        self._gpus = gpus
        # the last preflight's model folder and fast flag while files were missing
        self._models_lock = threading.Lock()
        self._models: tuple[Path, bool] | None = None

    # ── contract ────────────────────────────────────────────────────────
    def tools(self) -> m.ToolVersions:
        cfg = self.cfg
        return tool_versions(
            tool="jasna",
            model="ltx-undistilled" if cfg.ltx_best else "ltx",
            model_version="fast" if cfg.ltx_fast else "standard",
            profile=PROFILE_ID,
            settings={
                "gpu": self.gpu_id,
                "tier": TIER,
                "exe": self.jasna_exe,
                "jasna": cfg.model_dump(mode="json"),
                "duration_slack": [DURATION_SLACK_MS, DURATION_SLACK_SHARE],
            },
        )

    def max_resolution(self, vram_mib: int) -> tuple[int, int]:
        # Phase 1 runs only the 1080p LTX tier, whatever the card (T04-P1 scope).
        return min(max_resolution(vram_mib), FULL_HD)

    def needs_gpu(self, manifest: m.InputManifest) -> bool:
        """False only when every main video already is a restore result (D19)."""
        return not all(
            is_restored_name(input_name(video)) for video in main_videos(manifest)
        )

    def run(self, context: JobContext) -> ExecOutcome:
        return guarded(lambda: self._run(context))

    # ── model download offer (H3 M1, D07, D10) ──────────────────────────
    def _weights(self) -> Path:
        """Where this Jasna loads its LTX models; the download goes there too."""
        return Path(self.jasna_exe).parent / "model_weights" / "ltx-restore"

    def missing_models(self) -> tuple[Path, list[str], str] | None:
        """`(destination, missing files, variant)` the last preflight stopped on,
        rechecked on disk; None once they are all there. Never queries the GPU:
        the variant is the one that preflight chose."""
        with self._models_lock:
            cached = self._models
        if cached is None:
            return None
        weights, fast = cached
        missing = jasna.ltx_missing_files(
            str(weights.parent.parent), self.cfg.ltx_best, fast
        )
        if not missing:
            with self._models_lock:
                if self._models is cached:
                    self._models = None
            return None
        return weights, missing, jasna._ltx_mode(self.cfg, fast, False)

    # ── stage body ──────────────────────────────────────────────────────
    def _run(self, context: JobContext) -> ExecOutcome:
        rt, manifest = self.runtime, context.manifest
        videos = main_videos(manifest)
        probes = {probe.item_id: probe for probe in manifest.readiness.media_probes}
        pending = [v for v in videos if not is_restored_name(input_name(v))]
        pending_ids = {video.item_id for video in pending}
        exe, fast = self._preflight(context, pending)
        results: list[VideoResult] = []
        for index, video in enumerate(videos, start=1):
            if context.cancel.is_set():
                raise cancelled()
            label = f" 第 {index}/{len(videos)} 卷" if len(videos) > 1 else ""
            probe = probes.get(video.item_id)
            if video.item_id in pending_ids:
                assert exe is not None
                source = workcopy_path(rt, manifest, video.item_id)
                if not source.is_file():
                    raise attention(f"收件工作副本缺失：{video.relative_path}")
                facts = self._restore(context, exe, fast, source, label, probe)
            else:
                facts = self._reused(context, video, probe)
            results.append(
                VideoResult(
                    video_item_id=video.item_id,
                    volume_ordinal=video.volume_ordinal,
                    restore=facts,
                )
            )
        digest = write_stage_result(
            context.workdir,
            StageResult(
                film_id=context.job.film_id,
                input_revision=context.job.input_revision,
                stage="restore",
                attempt_id=attempt_id(context),
                videos=tuple(results),
            ),
        )
        applied = sum(
            1 for r in results if r.restore and r.restore.decision == "applied"
        )
        return ExecOutcome.succeeded(
            f"修复完成：{applied} 卷修复，{len(results) - applied} 卷复用", digest
        )

    def _preflight(
        self, context: JobContext, pending: list[m.ManifestItem]
    ) -> tuple[Path | None, bool]:
        """Everything a launch needs, checked before any child starts."""
        if not pending:
            return None, False
        exe = Path(self.jasna_exe)
        if not self.jasna_exe or not exe.is_file():
            raise attention(f"找不到 Jasna 程序：{self.jasna_exe or '未配置'}")
        if self.ffprobe is None:
            raise attention("找不到 ffprobe，无法核对修复结果")
        fast = self.cfg.ltx_fast and jasna.fast_supported(self._gpus())
        missing = jasna.ltx_missing_files(str(exe.parent), self.cfg.ltx_best, fast)
        with self._models_lock:
            self._models = (self._weights(), fast) if missing else None
        if missing:
            raise attention(MISSING_LTX + "、".join(missing))
        names = [
            jasna.output_path_for(str(context.workdir), Path(input_name(v))).name
            for v in pending
        ]
        if len({name.casefold() for name in names}) != len(names):
            raise attention("多个分卷的修复输出同名")
        return exe, fast

    def _reused(
        self,
        context: JobContext,
        video: m.ManifestItem,
        probe: m.MediaProbe | None,
    ) -> RestoreFacts:
        path, ref = workcopy_ref(self.runtime, context.manifest, video.item_id)
        facts = (
            ProbeFacts(
                probe.width,
                probe.height,
                probe.duration_ms,
                probe.video_streams,
                probe.audio_streams,
            )
            if probe is not None
            else None
        )
        if facts is None or None in (facts.width, facts.height, facts.duration_ms):
            facts = self._probe(context, path)
        return self._facts("reused", ref, facts)

    def _restore(
        self,
        context: JobContext,
        exe: Path,
        fast: bool,
        source: Path,
        label: str,
        probe: m.MediaProbe | None,
    ) -> RestoreFacts:
        workdir = context.workdir
        staging = jasna.staging_path_for(str(workdir), source)
        final = jasna.output_path_for(str(workdir), source)
        ltx_work = workdir / LTX_WORK_DIRNAME
        ltx_work.mkdir(parents=True, exist_ok=True)
        staging.unlink(missing_ok=True)
        argv = [
            *self.launch_prefix,
            *jasna.build_argv(
                self.cfg, str(exe), source, staging, TIER, False, False, ltx_work, fast
            ),
        ]
        lock = threading.Lock()
        state: dict = {}
        tail: deque[str] = deque(maxlen=_TAIL_LINES)
        volume = label.strip() or source.name
        context.progress(progress_text(label, state))

        def on_line(_stream: str, text: str) -> None:
            nonlocal state
            with lock:
                updated = parse_jasna_progress_line(text, state)
                if updated is state:
                    if text.strip():
                        tail.append(text.strip()[:300])
                    return
                state = updated
            # the monitor's LtxProgress metrics: its parse state and the volume (M3)
            context.progress(
                progress_text(label, updated), ltx={**updated, "current_file": volume}
            )

        child = context.processes.launch(argv, cwd=str(workdir), on_line=on_line)
        code = child.wait(context.cancel)
        if code is None:
            raise cancelled()
        if code != 0:
            detail = f"：{tail[-1]}" if tail else ""
            raise attention(
                f"Jasna LTX 失败（退出码 {code}），不会自动重跑{label}{detail}"
            )
        if not staging.is_file():
            raise attention(f"Jasna 已退出但没有生成修复文件{label}")
        jasna.retag_hevc_hvc1(staging)
        os.replace(staging, final)
        facts = self._probe(context, final)
        expected = probe.duration_ms if probe is not None else None
        actual = facts.duration_ms
        if actual is not None and expected:
            slack = max(DURATION_SLACK_MS, expected * DURATION_SLACK_SHARE)
            if abs(actual - expected) > slack:
                raise attention(
                    f"修复结果时长不符{label}：{actual / 1000:.1f} 秒，"
                    f"输入 {expected / 1000:.1f} 秒"
                )
        return self._facts("applied", file_ref(self.runtime.work_dir, final), facts)

    def _probe(self, context: JobContext, path: Path) -> ProbeFacts:
        return probe_video(
            context.processes, self.ffprobe, path, context.workdir, context.cancel
        )

    @staticmethod
    def _facts(
        decision: Literal["applied", "reused"], ref: FileRef, facts: ProbeFacts
    ) -> RestoreFacts:
        if (
            facts.video_streams < 1
            or facts.width is None
            or facts.height is None
            or not facts.duration_ms
        ):
            raise attention("ffprobe 读不到有效的视频流")
        return RestoreFacts(
            decision=decision,
            file=ref,
            width=facts.width,
            height=facts.height,
            duration_ms=facts.duration_ms,
            video_streams=facts.video_streams,
            audio_streams=facts.audio_streams,
        )
