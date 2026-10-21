"""Restore stage: Jasna LTX per main video, in volume order (T04-P1 E1, E8, §3.3).

The launch reuses the Jasna plugin's pure pieces: its preset (`preset_catalogue`,
model ltx, `standard`), `build_argv`, the LTX model check, the staging /
published names, the hev1→hvc1 retag and the LTX progress parser. A video whose
name already carries the plugin's restored suffix is `reused`, never restored
again. A failed LTX run is `needs_attention` and never relaunched (#265 D2).

J2: the preset's Jasna 0.11.3 tier (`ltx_model`, #283) picks the model and its
download, and — like the monitor (#285) — each video's launch is capped at its
source bitrate (`jasna.probe_bitrate`, once per video; a retry reuses it).

U1 P4': the film's confirmed plan carries its tier on the job (`ltx_model`),
read once by the list that offered it; the claim's settings and the launch use
it. The tier is a setting (a new config digest), never the tool identity: the
record's `model` stays "ltx" so the single approved profile matches (U05).
"""

from __future__ import annotations

import json
import logging
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
from taskpaw_v3.monitors.plugins.jasna_download import MODEL_FILES
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
MODEL = "ltx"  # the record's model for every tier (U1 U05)
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
# (film, input revision, video item) → the probed source bitrate (None: unknown)
_BitrateKey = tuple[str, int, str]

log = logging.getLogger("taskpaw.workflow.restore")


def is_restored_name(name: str) -> bool:
    """Whether a file name is already a Jasna restore result (plugin rule)."""
    return name.casefold().endswith(_RESTORED_SUFFIXES)


def needs_restore(manifest: m.InputManifest) -> bool:
    """False only when every main video already is a restore result (D19)."""
    return not all(
        is_restored_name(input_name(video)) for video in main_videos(manifest)
    )


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


def bitrate_ffprobe(command: Sequence[str] | None) -> str | None:
    """The executable `jasna.probe_bitrate` runs: only a plain ffprobe path (an
    argv prefix such as a test fake reads as unknown — no cap)."""
    return command[0] if command and len(command) == 1 else None


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
        # the last preflight's model folder, its settings (the tier) and fast
        # flag while files were missing
        self._models_lock = threading.Lock()
        self._models: tuple[Path, jasna.JasnaConfig, bool] | None = None
        # J2 (#285): each pending video's probed source bitrate, kept until the
        # film's restore succeeds so a retry of the stage launches with the same cap.
        # Only successful probes are kept; a failed one is probed again on the
        # next attempt and its key only silences the repeated warning.
        self._bitrates_lock = threading.Lock()
        self._bitrates: dict[_BitrateKey, int] = {}
        self._bitrate_failed: set[_BitrateKey] = set()

    # ── contract ────────────────────────────────────────────────────────
    def tools(self, job: m.PendingComputeJob | None = None) -> m.ToolVersions:
        """The claim's record; `job` brings the planned tier (None: the preset)."""
        cfg = self._tier_cfg(job)
        return tool_versions(
            tool="jasna",
            model=MODEL,  # U05: the tier is in `settings` (the digest) only
            # D09: not a tier. A setting, not a fact: a pre-Blackwell card runs
            # int8 although `ltx_fast` is on, so the record says "fast", not "nvfp4".
            model_version="fast" if cfg.ltx_fast else "int8",
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
        return needs_restore(manifest)

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
        weights, cfg, fast = cached
        missing = jasna.ltx_missing_files(
            str(weights.parent.parent), cfg.ltx_model, fast
        )
        if not missing:
            with self._models_lock:
                if self._models is cached:
                    self._models = None
            return None
        return weights, missing, jasna._ltx_mode(cfg, fast, False)

    def recall_missing(self, names: list[str], tier: str | None = None) -> None:
        """Seed the offer from a persisted preflight failure after a restart.

        Only while nothing is cached; never queries the GPU. U1 P4': the tier
        is the film's planned one (`tier`, what its retry restores with), else
        the one whose model file the note names, else the preset's (only
        shared files were missing). The fast and the int8 weights differ by
        suffix, so the names tell which. When only shared VAE files were
        missing, the model preflight chose was present, so the one installed
        tells (the configured choice if both or neither are: an RTX 40 card runs
        int8 although `ltx_fast` is on)."""
        named = [key for key, name in MODEL_FILES.items() if name in names]
        tier = tier or (named[0][0] if named else None) or self.cfg.ltx_model
        cfg = self.cfg.model_copy(update={"ltx_model": tier})
        if any("-nvfp4" in name for name in names):
            fast = True
        elif any("-int8" in name for name in names):
            fast = False
        else:
            folder = self._weights()
            installed = [
                variant
                for variant in (True, False)
                if (folder / jasna._LTX_MODEL_FILES[(tier, variant)]).is_file()
            ]
            fast = installed[0] if len(installed) == 1 else cfg.ltx_fast
        with self._models_lock:
            if self._models is None:
                self._models = (self._weights(), cfg, fast)

    # ── per-film settings (U1 P4') ──────────────────────────────────────
    def _tier_cfg(self, job: m.PendingComputeJob | None) -> jasna.JasnaConfig:
        """The preset with the job's planned tier; the preset itself without one
        (a film that needs no restore, or no job at all)."""
        tier = getattr(job, "ltx_model", None)
        if not tier or tier == self.cfg.ltx_model:
            return self.cfg
        return self.cfg.model_copy(update={"ltx_model": tier})

    def _cfg_for(self, context: JobContext) -> jasna.JasnaConfig:
        """The Jasna settings this film restores with: its planned tier, as
        read with the job that the claim's `tools(job)` recorded."""
        return self._tier_cfg(context.job)

    def _source_bitrate(
        self,
        context: JobContext,
        cfg: jasna.JasnaConfig,
        video: m.ManifestItem,
        source: Path,
    ) -> int | None:
        """#285 C8: the source bitrate this video's launch is capped at, probed
        once while `match_source_bitrate` is on. A retry of the same video (same
        input revision) reuses a successful probe; a failed one (a transient
        ffprobe timeout) is probed again, and warns only the first time."""
        if not cfg.match_source_bitrate:
            return None
        key = (context.job.film_id, context.job.input_revision, video.item_id)
        with self._bitrates_lock:
            if key in self._bitrates:
                return self._bitrates[key]
        bitrate = jasna.probe_bitrate(source, bitrate_ffprobe(self.ffprobe))
        with self._bitrates_lock:
            self._drop_stale_revisions(key)
            if bitrate is not None:
                self._bitrate_failed.discard(key)
                self._bitrates[key] = bitrate
                return bitrate
            warn = key not in self._bitrate_failed
            self._bitrate_failed.add(key)
        if warn:
            log.warning(
                "workflow restore: no source bitrate for %s; Jasna's own maxrate "
                "cap applies",
                source.name,
            )
        return None

    def _drop_stale_revisions(self, key: _BitrateKey) -> None:
        """Forget this film's entries of another input revision (the caller holds
        the lock): a film that failed and was never retried, or whose input
        changed, would otherwise leave them behind for good."""
        film_id, revision, _ = key
        for stale in [
            k for k in self._bitrates if k[0] == film_id and k[1] != revision
        ]:
            del self._bitrates[stale]
        self._bitrate_failed = {
            k for k in self._bitrate_failed if k[0] != film_id or k[1] == revision
        }

    def _forget_bitrates(
        self, context: JobContext, videos: list[m.ManifestItem]
    ) -> None:
        """Drop the film's probes once every video is restored."""
        job = context.job
        with self._bitrates_lock:
            for video in videos:
                key = (job.film_id, job.input_revision, video.item_id)
                self._bitrates.pop(key, None)
                self._bitrate_failed.discard(key)

    # ── stage body ──────────────────────────────────────────────────────
    def _run(self, context: JobContext) -> ExecOutcome:
        rt, manifest = self.runtime, context.manifest
        videos = main_videos(manifest)
        probes = {probe.item_id: probe for probe in manifest.readiness.media_probes}
        pending = [v for v in videos if not is_restored_name(input_name(v))]
        pending_ids = {video.item_id for video in pending}
        cfg = self._cfg_for(context)
        exe, fast = self._preflight(context, cfg, pending)
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
                bitrate = self._source_bitrate(context, cfg, video, source)
                facts = self._restore(
                    context, cfg, exe, fast, source, label, probe, bitrate
                )
            else:
                facts = self._reused(context, video, probe)
            results.append(
                VideoResult(
                    video_item_id=video.item_id,
                    volume_ordinal=video.volume_ordinal,
                    restore=facts,
                )
            )
        self._forget_bitrates(context, pending)
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
        self,
        context: JobContext,
        cfg: jasna.JasnaConfig,
        pending: list[m.ManifestItem],
    ) -> tuple[Path | None, bool]:
        """Everything a launch needs, checked before any child starts."""
        if not pending:
            return None, False
        exe = Path(self.jasna_exe)
        if not self.jasna_exe or not exe.is_file():
            raise attention(f"找不到 Jasna 程序：{self.jasna_exe or '未配置'}")
        if self.ffprobe is None:
            raise attention("找不到 ffprobe，无法核对修复结果")
        fast = cfg.ltx_fast and jasna.fast_supported(self._gpus())
        missing = jasna.ltx_missing_files(str(exe.parent), cfg.ltx_model, fast)
        with self._models_lock:
            self._models = (self._weights(), cfg, fast) if missing else None
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
        cfg: jasna.JasnaConfig,
        exe: Path,
        fast: bool,
        source: Path,
        label: str,
        probe: m.MediaProbe | None,
        bitrate: int | None,
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
                cfg,
                str(exe),
                source,
                staging,
                TIER,
                False,
                False,
                ltx_work,
                fast,
                source_bitrate=bitrate,
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
        return self._facts(
            "applied",
            file_ref(self.runtime.work_dir, final),
            facts,
            jasna.applied_maxrate(cfg, bitrate),
        )

    def _probe(self, context: JobContext, path: Path) -> ProbeFacts:
        return probe_video(
            context.processes, self.ffprobe, path, context.workdir, context.cancel
        )

    @staticmethod
    def _facts(
        decision: Literal["applied", "reused"],
        ref: FileRef,
        facts: ProbeFacts,
        maxrate: int | None = None,
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
            maxrate=maxrate,
        )
