"""Restore stage: Jasna LTX per main video, in volume order (T04-P1 E1, E8, §3.3).

The launch reuses the Jasna plugin's pure pieces: its preset (`preset_catalogue`,
model ltx, `standard`), `build_argv`, the LTX model check, the staging /
published names, the hev1→hvc1 retag and the LTX progress parser. A video whose
name already carries the plugin's restored suffix is `reused`, never restored
again. A failed LTX run is `needs_attention` and never relaunched (#265 D2).

J2: the preset's Jasna 0.11.3 tier (`ltx_model`, #283) picks the model and its
download, and — like the monitor (#285) — each video's launch is capped at its
source bitrate (`probe_source_bitrate`, once per video; a retry reuses it). The
probe is Jasna's own (`jasna.bitrate_probe_args` / `parse_bitrate`) but runs
inside the attempt's Job like `probe_video`: a stop ends it, and it gets
`BITRATE_PROBE_SECONDS` before it reads as unknown.

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
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from taskpaw_v3.monitors.plugins import jasna
from taskpaw_v3.monitors.plugins.jasna_download import MODEL_FILES
from taskpaw_v3.monitors.plugins.jasna_presets import preset_catalogue
from taskpaw_v3.monitors.plugins.jasna_progress import (
    console_text,
    parse_jasna_progress_line,
)

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
from .executors import RESTORE_DONE, ComputeStage, ExecOutcome, JobContext
from .local_gpu import FULL_HD, LocalGpu, max_resolution
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
from .timing import PhaseTimer, merge, timing_json

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
# U1c: an unrecognised console line refreshes the step at most this often
CONSOLE_INTERVAL_S = 1.0
# U1e: the clock of the phase times (a module name, so tests can drive it)
monotonic = time.monotonic
# The monitor's own bound for one bitrate probe (`jasna._PROBE_TIMEOUT`).
BITRATE_PROBE_SECONDS = 5.0
_BITRATE_POLL = 0.05

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


# (name fragment, preset id, VRAM MiB the preset assumes), most specific first. The
# VRAM floor keeps a name from claiming more card than there is (an 8 GB 5060 Ti).
_PRESET_BY_NAME = (
    ("5090", "5090-32", 30 * 1024),
    ("4090", "4090-24", 22 * 1024),
    ("5070 ti", "5070ti-16", 15 * 1024),
    ("5060 ti", "5060ti-16", 15 * 1024),
    ("5060", "5060-8", 0),
)
_PRESET_BY_VRAM = (
    (30 * 1024, "5090-32"),
    (22 * 1024, "4090-24"),
    (15 * 1024, "5070ti-16"),
)
SAFE_PRESET = "5060-8"


def resolve_gpu_preset(gpu: LocalGpu | None) -> str:
    """U1d: the Jasna preset id for the local card (`ltx_gpu` unset in agent.yaml):
    by model name, else by VRAM class; an unreadable or unknown card gets the
    8 GB preset, which is safe on any card."""
    if gpu is None:
        return SAFE_PRESET
    name = " ".join(gpu.name.casefold().split())
    for fragment, preset, floor in _PRESET_BY_NAME:
        if fragment in name and gpu.vram_mib >= floor:
            return preset
    for floor, preset in _PRESET_BY_VRAM:
        if gpu.vram_mib >= floor:
            return preset
    return SAFE_PRESET


def preset_label(gpu_id: str) -> str:
    """The catalogue's name of a preset id (the id itself when unknown)."""
    for entry in preset_catalogue()["gpus"]:
        if entry["id"] == gpu_id:
            return str(entry["label"])
    return gpu_id


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
    scan = state.get("scan_progress")
    if isinstance(scan, dict):  # U1c: the auto split's pre-LTX scan
        percent_text = scan.get("percent_text")
        parts.append(f"扫描马赛克 {percent_text}%" if percent_text else "扫描马赛克")
    if stage := state.get("ltx_stage"):
        parts.append(f"阶段 {stage}")
    percent = state.get("percent")
    if percent is not None:
        parts.append(f"{percent}%")
    if eta := state.get("eta"):
        parts.append(f"剩余 {eta}")
    return " · ".join(parts)


_PATH_FLAGS = ("--input", "--output", "--working-directory")
_MODEL_TIERS = {name: tier for tier, name in jasna.LTX_MODEL_NAMES.items()}


def _flag(argv: Sequence[str], flag: str) -> str | None:
    """The value of `flag`; the LAST occurrence wins, as in argparse (the
    operator's extra args come after the ones the preset built)."""
    for index in range(len(argv) - 2, -1, -1):
        if argv[index] == flag:
            return argv[index + 1]
    return None


def _switch(argv: Sequence[str], flag: str) -> bool | None:
    """Whether `--x` or `--no-x` came last (None: neither was given)."""
    off = f"--no-{flag[2:]}"
    for token in reversed(argv):
        if token == flag:
            return True
        if token == off:
            return False
    return None


_SMART_CODECS = {"h264", "hevc", "av1"}  # the codecs Jasna's smart render keeps


def _source_codec(probe: m.MediaProbe | None) -> str | None:
    """The primary video's codec as Jasna names it (None: unknown or not one
    Jasna keeps)."""
    facts = getattr(probe, "raw_facts", None)
    if facts is None:
        return None
    for stream in facts.streams:
        if stream.index == facts.primary_video_index:
            name = (stream.codec_name or "").casefold()
            return name if name in _SMART_CODECS else None
    return None


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def launch_params(
    argv: Sequence[str],
    *,
    match_source_bitrate: bool,
    source_bitrate: int | None,
    probe: m.MediaProbe | None,
    gpu: LocalGpu | None,
    preset: str,
    preset_auto: bool = False,
) -> dict[str, Any]:
    """U1c: what Jasna was launched with for this video, read from its argv
    (the effective values after the GPU check, never the configured ones), and
    the argv itself with every path cut to its file name, ready to copy."""
    model_name = _flag(argv, "--restoration-model-name")
    fast = _switch(argv, "--ltx-fast") is True
    switch = _switch(argv, "--experimental-ltx-less-overlap")
    overlap = "auto" if switch is None else "on" if switch else "off"
    rate = _flag(argv, "--encoder-settings") or ""
    maxrate = next(
        (_int(part[8:]) for part in rate.split(",") if part.startswith("maxrate=")),
        None,
    )
    codec = _flag(argv, "--codec")
    auto_split = _int(_flag(argv, "--ltx-auto-split")) or 0
    # Jasna smart-renders the auto split's spans in the source's codec, but it may
    # skip the split; `codec` stays the configured one and the UI picks by the
    # split note
    source_codec = _source_codec(probe)
    if not auto_split or source_codec == codec:
        source_codec = None
    short = list(argv)
    if short:
        short[0] = Path(short[0]).name
    for index in range(len(short) - 1):
        if short[index] in _PATH_FLAGS:  # every repeat, not only the last
            short[index + 1] = Path(short[index + 1]).name
    return {
        "ltx_model": _MODEL_TIERS.get(model_name or ""),
        "model_name": model_name,
        "fast": fast,
        "precision": "nvfp4" if fast else "int8",
        "auto_split": auto_split,
        "large_canvas": _switch(argv, "--ltx-large-canvas") is True,
        "overlap": overlap,
        "codec": codec,
        "source_codec": source_codec,
        "cq": _int(_flag(argv, "--cq")),
        "match_source_bitrate": match_source_bitrate,
        "maxrate_bps": maxrate,
        "source_bitrate_bps": source_bitrate,
        "detection_model": _flag(argv, "--detection-model"),
        "clip_size": _int(_flag(argv, "--max-clip-size")),
        "temporal_overlap": _int(_flag(argv, "--temporal-overlap")),
        "tier": TIER,
        "source_width": probe.width if probe is not None else None,
        "source_height": probe.height if probe is not None else None,
        "gpu": gpu.name if gpu is not None else None,
        "gpu_vram_mib": gpu.vram_mib if gpu is not None else None,
        "preset": preset,
        "preset_label": preset_label(preset),
        "preset_auto": preset_auto,
        "argv": subprocess.list2cmdline(short),
    }


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


def probe_source_bitrate(
    processes: OwnedJob,
    ffprobe: Sequence[str] | None,
    path: Path,
    cwd: Path,
    cancel: threading.Event,
    timeout: float = BITRATE_PROBE_SECONDS,
) -> int | None:
    """#285 C8: the source bitrate in bits/s by Jasna's order, probed inside
    the attempt's Job like `probe_video`. None on any failure (no cap): no
    ffprobe, a launch error, a non-zero exit, no rate, or no exit within
    `timeout` seconds (that child is killed). A stop kills it and raises
    `cancelled()`."""
    if ffprobe is None:
        return None
    lines: list[str] = []
    lock = threading.Lock()

    def on_line(stream: str, text: str) -> None:
        if stream == "stdout":
            with lock:
                lines.append(text)

    try:
        child = processes.launch(
            [*ffprobe, *jasna.bitrate_probe_args(path)], cwd=str(cwd), on_line=on_line
        )
    except OSError as exc:
        log.debug("workflow restore: bitrate ffprobe did not start: %s", exc)
        return None
    deadline = time.monotonic() + timeout
    while (code := child.poll()) is None:
        left = deadline - time.monotonic()
        if left <= 0:
            child.kill()
            log.debug("workflow restore: bitrate ffprobe timed out for %s", path)
            return None
        if cancel.wait(min(_BITRATE_POLL, left)):
            child.kill()
            raise cancelled()
    child.wait()  # the output readers finish
    if code != 0:
        return None
    with lock:
        return jasna.parse_bitrate("\n".join(lines))


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
        preset_auto: bool = False,
        ffprobe: str | Sequence[str] | None = None,
        launch_prefix: Sequence[str] = (),
        gpus: GpuQuery = jasna.query_gpus,
    ) -> None:
        """`ffprobe`: path or command (default: `ffprobe` on PATH).
        `launch_prefix` goes before the Jasna argv (tests run a fake tool).
        `preset_auto`: `gpu_id` was picked from the local GPU, not configured."""
        self.runtime, self.jasna_exe, self.gpu_id = runtime, jasna_exe, gpu_id
        self.preset_auto = preset_auto
        self.cfg = workflow_jasna_config(gpu_id)
        self.ffprobe = ffprobe_command(ffprobe)
        self.launch_prefix = tuple(launch_prefix)
        self._gpus = gpus
        # per tier (U1 Codex r2 F1): the model folder, settings and fast flag of
        # that tier's last preflight while files were missing; insertion order is
        # failure order, so the most recent failure is the last entry
        self._models_lock = threading.Lock()
        self._models: dict[str, tuple[Path, jasna.JasnaConfig, bool]] = {}
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

    def _unresolved(self) -> list[tuple[Path, list[str], str, str]]:
        """Every cached tier still missing files on disk, the most recent failure
        first, as `(destination, missing files, variant, tier)`; tiers whose files
        are all there now are forgotten. Never queries the GPU: the variant is
        the one that tier's preflight chose."""
        with self._models_lock:
            cached = list(self._models.items())
        unresolved, resolved = [], []
        for tier, entry in reversed(cached):
            weights, cfg, fast = entry
            missing = jasna.ltx_missing_files(
                str(weights.parent.parent), cfg.ltx_model, fast
            )
            if missing:
                variant = jasna._ltx_mode(cfg, fast, False)
                unresolved.append((weights, missing, variant, tier))
            else:
                resolved.append((tier, entry))
        if resolved:
            with self._models_lock:
                for tier, entry in resolved:
                    if self._models.get(tier) is entry:
                        del self._models[tier]
        return unresolved

    def missing_models(self) -> tuple[Path, list[str], str] | None:
        """`(destination, missing files, variant)` of the most recent preflight
        failure whose tier still lacks files on disk; None once every tier's are
        there. The session downloads one variant at a time: once it is in, the
        next unresolved tier is offered."""
        unresolved = self._unresolved()
        if not unresolved:
            return None
        weights, missing, variant, _ = unresolved[0]
        return weights, missing, variant

    def unresolved_tiers(self) -> set[str]:
        """The tiers whose last preflight stopped on files still missing."""
        return {tier for *_, tier in self._unresolved()}

    def recall_missing(self, names: list[str], tier: str | None = None) -> None:
        """Seed the offer from a persisted preflight failure after a restart.

        Only while that tier has nothing cached; never queries the GPU. U1 P4': the tier
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
            self._models.setdefault(tier, (self._weights(), cfg, fast))

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
        bitrate = probe_source_bitrate(
            context.processes, self.ffprobe, source, context.workdir, context.cancel
        )
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
        timings: list[m.RestoreTiming] = []  # U1e: the volumes restored so far
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
                    context,
                    cfg,
                    exe,
                    fast,
                    source,
                    label,
                    probe,
                    bitrate,
                    merge(*timings),
                )
                if facts.timing is not None:
                    timings.append(facts.timing)
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
            RESTORE_DONE.format(applied=applied, reused=len(results) - applied),
            digest,
            merge(*timings),
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
        with self._models_lock:  # F1: this tier only; a failure moves it last
            self._models.pop(cfg.ltx_model, None)
            if missing:
                self._models[cfg.ltx_model] = (self._weights(), cfg, fast)
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
        earlier: m.RestoreTiming | None = None,
    ) -> RestoreFacts:
        """One video's Jasna run. `earlier`: the film's volumes before this one,
        so the step's live `timing` is the whole film's (U1e)."""
        workdir = context.workdir
        staging = jasna.staging_path_for(str(workdir), source)
        final = jasna.output_path_for(str(workdir), source)
        ltx_work = workdir / LTX_WORK_DIRNAME
        ltx_work.mkdir(parents=True, exist_ok=True)
        staging.unlink(missing_ok=True)
        jasna_argv = jasna.build_argv(
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
        )
        argv = [*self.launch_prefix, *jasna_argv]
        # U1c: this video's effective launch, shown on the step while it runs
        params = launch_params(
            jasna_argv,
            match_source_bitrate=cfg.match_source_bitrate,
            source_bitrate=bitrate,
            probe=probe,
            gpu=self.runtime.gpu,
            preset=self.gpu_id,
            preset_auto=self.preset_auto,
        )
        lock = threading.Lock()
        state: dict = {}
        tail: deque[str] = deque(maxlen=_TAIL_LINES)
        volume = label.strip() or source.name
        opening: dict[str, Any] = {"current_file": volume, "params": params}
        if earlier is not None:  # IR06: the card keeps the earlier volumes' times
            opening["timing"] = timing_json(earlier)
        context.progress(progress_text(label, state), ltx=opening)
        console = ""
        last_emit = -math.inf
        held = False  # a console line the throttle dropped, not yet on the step
        timer = PhaseTimer(monotonic())

        def publish(now: float) -> None:
            """The monitor's LtxProgress metrics (its parse state and the
            volume, M3) plus the latest plain console line (U1c) and the film's
            phase times so far (U1e). Caller holds `lock`."""
            nonlocal last_emit, held
            last_emit, held = now, False
            detail = {**state, "current_file": volume, "console": console}
            detail["params"] = params
            detail["timing"] = timing_json(merge(earlier, timer.snapshot(monotonic())))
            context.progress(progress_text(label, state), ltx=detail)

        def on_line(_stream: str, text: str) -> None:
            nonlocal state, console, held
            with lock:
                updated = parse_jasna_progress_line(text, state)
                line = console_text(text)
                console = line or console
                now = time.monotonic()
                if updated is state:
                    if text.strip():
                        tail.append(text.strip()[:300])
                    # U1c heartbeat: any console line shows the child is alive,
                    # throttled so a chatty child cannot flood the status
                    if not line:
                        return
                    if now - last_emit < CONSOLE_INTERVAL_S:
                        held = True  # the flusher publishes it when the window ends
                        return
                if updated is not state:
                    timer.observe(updated, monotonic())
                state = updated
                publish(now)

        stop_flush = threading.Event()

        def flush_held() -> None:
            """IR03: a dropped line must still reach the step when the child then
            goes quiet; U1e: while a Jasna phase is running the step's times are
            republished about once a second even if the child prints nothing.
            One thread for both; lives only while the child runs."""
            while True:
                with lock:
                    wait = (
                        CONSOLE_INTERVAL_S
                        if last_emit == -math.inf
                        else CONSOLE_INTERVAL_S - (time.monotonic() - last_emit)
                    )
                if stop_flush.wait(max(wait, 0.05)):
                    return
                with lock:
                    now = time.monotonic()
                    ticking = timer.current is not None
                    if (held or ticking) and now - last_emit >= CONSOLE_INTERVAL_S:
                        publish(now)

        child = context.processes.launch(argv, cwd=str(workdir), on_line=on_line)
        flusher = threading.Thread(
            target=flush_held, name="restore-console-flush", daemon=True
        )
        flusher.start()
        try:
            code = child.wait(context.cancel)
        finally:
            stop_flush.set()
            flusher.join(2)
            with lock:
                timer.close(monotonic())
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
            timer.snapshot(),
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
        timing: m.RestoreTiming | None = None,
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
            timing=timing,
        )
