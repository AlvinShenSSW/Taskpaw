"""ASR stage: reuse an existing subtitle or transcribe with WhisperJAV (T04-P1 E2).

Per main video, the film's own SRT subtitles (other formats stay attachments)
are judged by `subtitle_judge.judge` (§3.4): a usable Chinese one is
`reused_zh`, a Japanese one `source_ja`; otherwise WhisperJAV transcribes the
video into `<stem>.ja.srt` (`generated_ja`) or finds no speech (`no_speech`,
no file written). A Chinese-tagged subtitle that is not usable Chinese stops
the stage for the operator (`needs_attention`) before any child starts.
"""

from __future__ import annotations

import shutil
import threading
from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from time import monotonic
from typing import Literal

from taskpaw_v3.monitors.plugins.jasna_progress import console_text
from taskpaw_v3.monitors.subs import srt, whisperjav
from taskpaw_v3.monitors.subs.child import asr_env
from taskpaw_v3.monitors.subs.progress import AsrProgress

from . import models as m
from .exec_common import (
    StageFailure,
    attempt_id,
    attention,
    cancelled,
    guarded,
    output_stems,
    tool_versions,
    workcopy_ref,
    write_text_atomic,
)
from .executors import ASR_DONE, ComputeStage, ExecOutcome, ExecRuntime, JobContext
from .local_gpu import UHD
from .stage_results import (
    FileRef,
    StageResult,
    SubtitleFacts,
    VideoResult,
    file_ref,
    main_videos,
    workcopy_path,
    write_stage_result,
)
from .subtitle_judge import judge

PROFILE_ID = "workflow-asr"
ASR_DIRNAME = "asr"
_TAIL_LINES = 8
CONSOLE_INTERVAL_S = 1.0
_PHASE_LABELS = (
    "提取音频",
    "划分场景",
    "语音增强",
    "语音分段",
    "识别语音",
    "生成场景字幕",
    "合并字幕",
    "过滤与清理",
)


@dataclass(frozen=True)
class SubtitlePlan:
    decision: Literal["reused_zh", "source_ja", "run_asr", "needs_attention"]
    ref: FileRef | None = None
    reason: str = ""


def srt_subtitles(manifest: m.InputManifest, video_id: str) -> list[m.ManifestItem]:
    """The film's SRT subtitles of one video (by extension), in path order."""
    return sorted(
        (
            item
            for item in manifest.items
            if item.role == "source_subtitle"
            and item.video_item_id == video_id
            and PurePosixPath(item.relative_path).suffix.casefold() == ".srt"
        ),
        key=lambda item: item.relative_path,
    )


class AsrExecutor:
    """Subtitle source per main video; WhisperJAV only where nothing is reusable."""

    stage: ComputeStage = "asr"
    uses_gpu = True

    def __init__(
        self,
        runtime: ExecRuntime,
        whisperjav_exe: str,
        *,
        engine: whisperjav.Engine = whisperjav.DEFAULT_ENGINE,
        extra_args: str = "",
        launch_prefix: Sequence[str] = (),
    ) -> None:
        """`launch_prefix` goes before the WhisperJAV argv (tests run a fake)."""
        whisperjav.validate_fields(
            whisperjav_exe, engine, extra_args, required=False, owner="工作流"
        )
        self.runtime, self.whisperjav_exe = runtime, whisperjav_exe
        self.engine, self.extra_args = engine, extra_args
        self.launch_prefix = tuple(launch_prefix)

    # ── contract ────────────────────────────────────────────────────────
    def tools(self, job: m.PendingComputeJob | None = None) -> m.ToolVersions:
        return tool_versions(
            tool="whisperjav",
            model=self.engine,
            model_version="japanese",
            profile=PROFILE_ID,
            settings={
                "exe": self.whisperjav_exe,
                "engine": self.engine,
                "extra_args": self.extra_args,
                "argv": list(whisperjav.PRESETS[self.engine]),
            },
        )

    def max_resolution(self, vram_mib: int) -> tuple[int, int]:
        """OD7 R8: WhisperJAV reads the audio only, so any card takes 4K."""
        return UHD

    def needs_gpu(self, manifest: m.InputManifest) -> bool:
        """False when no video will run WhisperJAV (all reused, or the stage
        stops for the operator before any transcription)."""
        try:
            plans = [plan for _, plan in self.plan(manifest)]
        except Exception:  # the run reports it; holding the GPU is the safe side
            return True
        if any(plan.decision == "needs_attention" for plan in plans):
            return False
        return any(plan.decision == "run_asr" for plan in plans)

    def run(self, context: JobContext) -> ExecOutcome:
        return guarded(lambda: self._run(context))

    # ── planning (pure apart from reading the work copy) ────────────────
    def plan(
        self, manifest: m.InputManifest
    ) -> list[tuple[m.ManifestItem, SubtitlePlan]]:
        return [
            (video, self._plan_video(manifest, video))
            for video in main_videos(manifest)
        ]

    def _plan_video(
        self, manifest: m.InputManifest, video: m.ManifestItem
    ) -> SubtitlePlan:
        judged: list[tuple[str, FileRef, m.ManifestItem, str]] = []
        for item in srt_subtitles(manifest, video.item_id):
            path, ref = workcopy_ref(self.runtime, manifest, item.item_id)
            try:
                raw = path.read_bytes()
            except PermissionError:  # held by antivirus / the indexer (H1-2)
                raise StageFailure(
                    "transient", f"字幕暂时被占用：{item.relative_path}"
                ) from None
            except OSError:
                raise attention(f"无法读取字幕：{item.relative_path}") from None
            verdict = judge(raw, item.language or "")
            judged.append((verdict.decision, ref, item, verdict.reason))
        for decision, ref, _, _ in judged:
            if decision == "reused_zh":
                return SubtitlePlan("reused_zh", ref)
        for decision, _, item, reason in judged:
            if decision == "needs_attention":
                return SubtitlePlan(
                    "needs_attention", reason=f"{item.relative_path}：{reason}"
                )
        for decision, ref, _, _ in judged:
            if decision == "source_ja":
                return SubtitlePlan("source_ja", ref)
        return SubtitlePlan("run_asr")

    # ── stage body ──────────────────────────────────────────────────────
    def _run(self, context: JobContext) -> ExecOutcome:
        rt, manifest = self.runtime, context.manifest
        plans = self.plan(manifest)
        for _, plan in plans:
            if plan.decision == "needs_attention":
                raise attention(plan.reason)
        stems = output_stems(rt, manifest)
        if any(plan.decision == "run_asr" for _, plan in plans):
            if not self.whisperjav_exe or not Path(self.whisperjav_exe).is_file():
                raise attention(
                    f"找不到 WhisperJAV 程序：{self.whisperjav_exe or '未配置'}"
                )
        targets = [f"{stems[video.item_id]}.ja.srt".casefold() for video, _ in plans]
        if len(set(targets)) != len(targets):
            raise attention("多个分卷的字幕输出同名")
        results: list[VideoResult] = []
        for index, (video, plan) in enumerate(plans, start=1):
            if context.cancel.is_set():
                raise cancelled()
            label = f" 第 {index}/{len(plans)} 卷" if len(plans) > 1 else ""
            if plan.decision == "reused_zh":
                facts = SubtitleFacts(decision="reused_zh", zh=plan.ref)
            elif plan.decision == "source_ja":
                facts = SubtitleFacts(decision="source_ja", ja=plan.ref)
            else:
                facts = self._transcribe(
                    context, video, index, stems[video.item_id], label
                )
            results.append(
                VideoResult(
                    video_item_id=video.item_id,
                    volume_ordinal=video.volume_ordinal,
                    subtitle=facts,
                )
            )
        digest = write_stage_result(
            context.workdir,
            StageResult(
                film_id=context.job.film_id,
                input_revision=context.job.input_revision,
                stage="asr",
                attempt_id=attempt_id(context),
                videos=tuple(results),
            ),
        )
        counts = {d: 0 for d in ("reused_zh", "source_ja", "generated_ja", "no_speech")}
        for result in results:
            if result.subtitle is not None:
                counts[result.subtitle.decision] += 1
        summary = "，".join(f"{k} {v}" for k, v in counts.items() if v)
        return ExecOutcome.succeeded(ASR_DONE.format(summary=summary), digest)

    def _transcribe(
        self,
        context: JobContext,
        video: m.ManifestItem,
        index: int,
        stem: str,
        label: str,
    ) -> SubtitleFacts:
        source = workcopy_path(self.runtime, context.manifest, video.item_id)
        if not source.is_file():
            raise attention(f"收件工作副本缺失：{video.relative_path}")
        out_dir = context.workdir / ASR_DIRNAME / f"{index:02d}"
        tmp_dir = context.workdir / ASR_DIRNAME / "tmp"
        shutil.rmtree(out_dir, ignore_errors=True)
        out_dir.mkdir(parents=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        argv = [
            *self.launch_prefix,
            *whisperjav.build_argv(
                self.whisperjav_exe,
                source,
                out_dir,
                tmp_dir,
                self.engine,
                self.extra_args,
            ),
        ]
        tail: deque[str] = deque(maxlen=_TAIL_LINES)
        parser = AsrProgress(monotonic())
        lock = threading.Lock()  # stdout, stderr and the console flusher
        console = ""
        phase: int | None = None
        phase_started = monotonic()
        durations: dict[int, int] = {}
        last_emit = 0.0
        held = False

        def publish(now: float) -> None:
            nonlocal last_emit, held
            progress = parser.snapshot(now)
            if phase is None:
                progress = {"elapsed_s": progress["elapsed_s"]}
            else:
                progress["phase_label"] = _PHASE_LABELS[phase - 1]
                progress["phases"] = [
                    {
                        "n": n,
                        "label": name,
                        "state": "done"
                        if n < phase
                        else "current"
                        if n == phase
                        else "pending",
                        "seconds": int(now - phase_started)
                        if n == phase
                        else durations.get(n),
                    }
                    for n, name in enumerate(_PHASE_LABELS, 1)
                ]
            progress["console"] = console
            # The existing dict channel is named ltx; board maps it to ASR progress.
            context.progress(f"识别日语{label}：{console[-120:]}", ltx=progress)
            last_emit, held = now, False

        publish(monotonic())

        def on_line(_stream: str, text: str) -> None:
            nonlocal console, phase, phase_started, held
            with lock:
                now = monotonic()
                parser.feed_text(text, now)  # parse even when publication is throttled
                next_phase = parser.snapshot(now)["phase"]
                if next_phase != phase:
                    if phase is not None:
                        durations[phase] = int(now - phase_started)
                    phase, phase_started = next_phase, now
                line = console_text(text)
                if not line:
                    return
                console = line
                tail.append(line)
                held = True
                if now - last_emit >= CONSOLE_INTERVAL_S:
                    publish(now)

        stop_flush = threading.Event()

        def flush_held() -> None:
            # A burst's last line still reaches the UI if the child goes quiet.
            while not stop_flush.wait(CONSOLE_INTERVAL_S):
                with lock:
                    now = monotonic()
                    if (
                        held or phase is not None
                    ) and now - last_emit >= CONSOLE_INTERVAL_S:
                        publish(now)

        child = context.processes.launch(
            argv,
            cwd=str(context.workdir),
            env=asr_env(None, self.whisperjav_exe),
            on_line=on_line,
        )
        flusher = threading.Thread(
            target=flush_held, name="asr-console-flush", daemon=True
        )
        flusher.start()
        try:
            code = child.wait(context.cancel)
        finally:
            stop_flush.set()
            flusher.join(2)
            with lock:
                if held:
                    publish(monotonic())
        if code is None:
            raise cancelled()
        outcome = whisperjav.read_outcome(out_dir, code, " | ".join(tail))
        if outcome.kind == "failed":
            raise StageFailure(
                "transient", f"WhisperJAV 识别失败{label}：{outcome.detail[:300]}"
            )
        if outcome.kind == "no_speech":
            return SubtitleFacts(decision="no_speech")
        target = context.workdir / f"{stem}.ja.srt"
        write_text_atomic(target, srt.serialize(outcome.cues))
        return SubtitleFacts(
            decision="generated_ja", ja=file_ref(self.runtime.work_dir, target)
        )
