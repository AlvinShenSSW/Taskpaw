"""Translate stage: ja → zh with the existing `Translator` (T04-P1 E3, §3.3).

Only `source_ja` / `generated_ja` videos are translated, into
`<restored stem>.zh.srt` in the attempt's work directory; `reused_zh` and
`no_speech` pass through unchanged (a no-op success: the policy has no
skippable stage). The asr ledger is read digest-checked; a stale or altered
one stops the stage for the operator.
"""

from __future__ import annotations

import queue
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from taskpaw_v3.core.llm import get_llm_chain
from taskpaw_v3.monitors.subs import srt
from taskpaw_v3.monitors.subs.translate import (
    BATCH_SIZE,
    RunId,
    TranslateRequest,
    TranslateResult,
    Translator,
)

from . import models as m
from .exec_common import (
    StageFailure,
    attempt_id,
    attention,
    cancelled,
    guarded,
    output_stems,
    tool_versions,
    write_text_atomic,
)
from .executors import ComputeStage, ExecOutcome, JobContext
from .local_gpu import max_resolution
from .local_runtime import LocalRuntime
from .stage_results import (
    StageResult,
    StageResultError,
    SubtitleFacts,
    VideoResult,
    check_file,
    file_ref,
    load_stage_result,
    write_stage_result,
)
from .subtitle_judge import decode

PROFILE_ID = "workflow-translate"
CHECKPOINTS_DIRNAME = "translate-checkpoints"
NO_MODEL = "没有可用的翻译模型配置"
_TRANSLATED = ("source_ja", "generated_ja")


class TranslatorLike(Protocol):
    """What the executor uses of `subs.translate.Translator`."""

    @property
    def results(self) -> queue.Queue[Any]: ...

    def start(self) -> None: ...

    def submit(self, req: TranslateRequest) -> None: ...

    def cancel(self) -> None: ...

    def join(self, timeout: float) -> None: ...

    def discard_checkpoint(self, key: str) -> None: ...


TranslatorFactory = Callable[[RunId, Path], TranslatorLike]


def default_translator(run: RunId, checkpoint_dir: Path) -> TranslatorLike:
    return Translator(
        run,
        name="workflow-translate",
        task_type="workflow",
        checkpoint_dir=checkpoint_dir,
    )


class TranslateExecutor:
    """Translates the Japanese subtitle the asr stage chose or generated."""

    stage: ComputeStage = "translate"
    uses_gpu = False

    def __init__(
        self,
        runtime: LocalRuntime,
        *,
        translator_factory: TranslatorFactory | None = None,
        chain_fn: Callable[[], Sequence[object]] = get_llm_chain,
        poll: float = 0.2,
        join_timeout: float = 10.0,
    ) -> None:
        self.runtime = runtime
        self.translator_factory = translator_factory or default_translator
        self.chain_fn = chain_fn
        self.poll, self.join_timeout = poll, join_timeout

    # ── contract ────────────────────────────────────────────────────────
    def tools(self) -> m.ToolVersions:
        return tool_versions(
            tool="taskpaw-translate",
            model="llm-chain",
            model_version="configured",
            profile=PROFILE_ID,
            settings={
                "checkpoints": CHECKPOINTS_DIRNAME,
                "batch_size": BATCH_SIZE,
                "source": list(_TRANSLATED),
            },
        )

    def max_resolution(self, vram_mib: int) -> tuple[int, int]:
        return max_resolution(vram_mib)

    def run(self, context: JobContext) -> ExecOutcome:
        return guarded(lambda: self._run(context))

    # ── stage body ──────────────────────────────────────────────────────
    def _run(self, context: JobContext) -> ExecOutcome:
        rt, film_id = self.runtime, context.job.film_id
        try:
            asr = load_stage_result(rt, film_id, "asr")
            stems = output_stems(rt, context.manifest)
        except StageResultError as exc:
            raise attention(f"无法使用识别阶段的结果：{exc}") from None
        work: list[tuple[VideoResult, SubtitleFacts, tuple[srt.Cue, ...]]] = []
        for video in asr.videos:
            sub = video.subtitle
            if sub is None:
                raise attention("识别阶段的结果缺少字幕结论")
            if sub.decision in _TRANSLATED:
                work.append((video, sub, self._ja_cues(sub)))
        translated: dict[str, SubtitleFacts] = {}
        if work:
            if not self.chain_fn():
                raise attention(NO_MODEL)
            translated = self._translate(context, work, stems)
        results = tuple(
            video.model_copy(
                update={"subtitle": translated.get(video.video_item_id, video.subtitle)}
            )
            for video in asr.videos
        )
        digest = write_stage_result(
            context.workdir,
            StageResult(
                film_id=film_id,
                input_revision=context.job.input_revision,
                stage="translate",
                attempt_id=attempt_id(context),
                videos=results,
            ),
        )
        message = f"翻译完成：{len(work)} 卷" if work else "无需翻译"
        return ExecOutcome.succeeded(message, digest)

    def _ja_cues(self, sub: SubtitleFacts) -> tuple[srt.Cue, ...]:
        if sub.ja is None:
            raise attention("识别阶段的结果缺少日语字幕")
        path = check_file(self.runtime.work_dir, sub.ja)  # StageResultError → guarded
        text = decode(path.read_bytes())
        try:
            cues = srt.parse(text) if text is not None else None
        except srt.SrtError:
            cues = None
        if cues is None:
            raise attention(f"日语字幕无法解析：{sub.ja.path}")
        if not cues:
            raise attention(f"日语字幕没有台词：{sub.ja.path}")
        return tuple(cues)

    def _translate(
        self,
        context: JobContext,
        work: list[tuple[VideoResult, SubtitleFacts, tuple[srt.Cue, ...]]],
        stems: dict[str, str],
    ) -> dict[str, SubtitleFacts]:
        rt = self.runtime
        checkpoint_dir = rt.work_dir / CHECKPOINTS_DIRNAME
        checkpoint_dir.mkdir(parents=True, exist_ok=True)
        run: RunId = (context.job.job_id, context.job.input_revision)
        translator = self.translator_factory(run, checkpoint_dir)
        translator.start()
        try:
            for video, _, cues in work:
                translator.submit(TranslateRequest(run, video.video_item_id, cues))
            done = self._collect(
                context, translator, {v.video_item_id for v, *_ in work}
            )
            facts: dict[str, SubtitleFacts] = {}
            for video, sub, _ in work:
                result = done[video.video_item_id]
                target = context.workdir / f"{stems[video.video_item_id]}.zh.srt"
                write_text_atomic(target, srt.serialize(result.zh_cues))
                facts[video.video_item_id] = sub.model_copy(
                    update={
                        "zh": file_ref(rt.work_dir, target),
                        "kept_ja": result.kept_ja,
                    }
                )
                if result.checkpoint_key:
                    translator.discard_checkpoint(result.checkpoint_key)
            return facts
        finally:
            translator.cancel()
            translator.join(self.join_timeout)

    def _collect(
        self, context: JobContext, translator: TranslatorLike, pending: set[str]
    ) -> dict[str, TranslateResult]:
        done: dict[str, TranslateResult] = {}
        total = len(pending)
        context.progress(f"翻译中文字幕 0/{total}")
        while pending:
            if context.cancel.is_set():
                raise cancelled()  # the caller cancels and joins the translator
            try:
                item = translator.results.get(timeout=self.poll)
            except queue.Empty:
                continue
            if not isinstance(item, TranslateResult):
                if context.cancel.is_set():
                    raise cancelled()
                raise StageFailure("transient", "翻译意外停止")
            if item.job_id not in pending:
                continue
            if item.outcome == "no_key":
                raise attention(NO_MODEL)
            if item.outcome == "paused":
                raise StageFailure("transient", f"翻译暂停：{item.detail}")
            if item.outcome != "translated":
                raise StageFailure("transient", f"翻译失败：{item.detail[:300]}")
            pending.discard(item.job_id)
            done[item.job_id] = item
            context.progress(f"翻译中文字幕 {len(done)}/{total}")
        return done
