"""Local return: one hash-verified package per film revision, receipted (T04-P1 E5/E6).

`work/return/<film_id>-r<revision>/` holds the restored videos (hard-linked when
possible), the final subtitles and copies of the intake work copy's sidecar
files. `return-manifest.json` inside it is the hand-off to M01. The original
input is never touched; a completed package is re-verified and reused.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from . import models as m
from .executors import ExecOutcome
from .local_dispatch import ReturnContext
from .local_runtime import LocalRuntime
from .stage_results import (
    RESULT_NAME,
    StageResult,
    StageResultError,
    check_file,
    load_stage_result,
    main_videos,
    sha256_file,
    workcopy_path,
)

MANIFEST_NAME = "return-manifest.json"
_ITEM_NS = uuid.UUID("0b8c3f2e-6a1d-4b7e-9c55-2f1e7d3a9b40")
_RENAME_TRIES = 5


@dataclass(frozen=True)
class _Member:
    item_id: str
    role: str
    relative_path: str  # inside the package (POSIX)
    source: Path
    sha256: str
    size: int
    link: bool  # hard link (videos) or copy
    video_item_id: str | None = None
    language: str | None = None
    volume_ordinal: int | None = None
    source_item_id: str | None = None
    # The film's verified Chinese subtitle from the translate ledger (IR01);
    # an input file merely labelled "zh" never sets this.
    final_zh: bool = False


def package_dir(rt: LocalRuntime, film_id: str, revision: int) -> Path:
    return rt.work_dir / "return" / f"{film_id}-r{revision}"


class LocalReturnStep:
    def tools(self) -> m.ToolVersions:
        settings = {"schema": 1, "layout": "film-revision"}
        return m.ToolVersions(
            tool="workflow-return",
            tool_version="1",
            model="none",
            model_version="1",
            profile="workflow-return",
            profile_revision=1,
            config_digest=hashlib.sha256(
                json.dumps(settings, sort_keys=True).encode()
            ).hexdigest(),
        )

    def run(self, context: ReturnContext) -> ExecOutcome:
        rt, job = context.runtime, context.job
        try:
            restore = load_stage_result(rt, job.film_id, "restore")
            translate = load_stage_result(rt, job.film_id, "translate")
            asr = load_stage_result(rt, job.film_id, "asr")
            members = self._members(rt, context.manifest, restore, translate)
        except StageResultError as exc:
            return ExecOutcome.failed("needs_attention", str(exc))
        package = package_dir(rt, job.film_id, job.input_revision)
        manifest = self._manifest(
            context, members, {"restore": restore, "asr": asr, "translate": translate}
        )
        raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if not self._complete(package, members, raw):
            try:
                self._build(package, members, raw)
            except _Busy as exc:
                return ExecOutcome.failed("transient", str(exc))
            except StageResultError as exc:
                return ExecOutcome.failed("needs_attention", str(exc))
        digest = hashlib.sha256(raw).hexdigest()
        self._receipt(context, members, digest)
        self._drop_old_revisions(rt, job.film_id, job.input_revision)
        return ExecOutcome.succeeded("回传完成", result_digest=digest)

    # ── package contents ───────────────────────────────────────────────
    def _members(
        self,
        rt: LocalRuntime,
        manifest: m.InputManifest,
        restore: StageResult,
        translate: StageResult,
    ) -> list[_Member]:
        film = manifest.film_id
        restored = {v.video_item_id: v.restore for v in restore.videos}
        subtitles = {v.video_item_id: v.subtitle for v in translate.videos}
        members: list[_Member] = []
        reused_sources: set[str] = set()
        workcopies = {c.relative_path for c in manifest.readiness.workcopy.items}
        for video in main_videos(manifest):
            facts = restored.get(video.item_id)
            if facts is None:
                raise StageResultError("修复结果缺少一个主视频")
            source = check_file(rt.work_dir, facts.file)
            folder = PurePosixPath(video.relative_path).parent
            name = PurePosixPath(facts.file.path).name
            path = (folder / name).as_posix()
            members.append(
                self._member(
                    film,
                    "main_video",
                    path,
                    source,
                    facts.file.sha256,
                    facts.file.size,
                    link=True,
                    volume_ordinal=video.volume_ordinal,
                    source_item_id=video.item_id,
                )
            )
            stem = PurePosixPath(name).stem
            subtitle = subtitles.get(video.item_id)
            if subtitle is None:
                raise StageResultError("字幕结果缺少一个主视频")
            for language, ref in (("zh", subtitle.zh), ("ja", subtitle.ja)):
                if ref is None:
                    continue
                file = check_file(rt.work_dir, ref)
                reused = ref.path in workcopies  # an input subtitle kept as is (IR05)
                if reused:
                    reused_sources.add(ref.path)
                members.append(
                    self._member(
                        film,
                        "source_subtitle" if reused else "generated_subtitle",
                        (folder / f"{stem}.{language}.srt").as_posix(),
                        file,
                        ref.sha256,
                        ref.size,
                        link=False,
                        video_item_id=video.item_id,
                        language=language,
                        final_zh=language == "zh",
                    )
                )
        for item in manifest.items:
            if item.role == "main_video":
                continue
            source = workcopy_path(rt, manifest, item.item_id)
            if _relative(rt, source) in reused_sources:
                continue  # already included under its final .zh/.ja name
            members.append(
                self._member(
                    film,
                    item.role,
                    item.relative_path,
                    source,
                    item.sha256,
                    item.size_bytes,
                    link=False,
                    video_item_id=item.video_item_id,
                    language=item.language,
                    volume_ordinal=item.volume_ordinal,
                    source_item_id=item.item_id,
                )
            )
        return members

    @staticmethod
    def _member(
        film: str, role: str, path: str, source: Path, sha: str, size: int, **kw: object
    ) -> _Member:
        return _Member(
            item_id=str(uuid.uuid5(_ITEM_NS, f"{film}/{path}")),
            role=role,
            relative_path=path,
            source=source,
            sha256=sha,
            size=size,
            **kw,  # type: ignore[arg-type]
        )

    def _manifest(
        self,
        context: ReturnContext,
        members: list[_Member],
        results: dict[str, StageResult],
    ) -> dict[str, object]:
        """The M01 hand-off; it never names this return attempt (D16)."""
        restore = results["restore"]
        videos = [v.restore for v in restore.videos if v.restore is not None]
        subtitles = [v.subtitle for v in results["translate"].videos if v.subtitle]
        zh_items = [x.item_id for x in members if x.final_zh]
        return {
            "schema": 1,
            "film_id": context.job.film_id,
            "input_revision": context.job.input_revision,
            "input_hash": self._input_hash(context),
            "attempts": {name: r.attempt_id for name, r in sorted(results.items())},
            "items": [
                {
                    "item_id": x.item_id,
                    "role": x.role,
                    "relative_path": x.relative_path,
                    "size": x.size,
                    "sha256": x.sha256,
                    "video_item_id": x.video_item_id,
                    "language": x.language,
                    "volume_ordinal": x.volume_ordinal,
                    "source_item_id": x.source_item_id,
                }
                for x in sorted(members, key=lambda x: x.relative_path)
            ],
            "restoration": {
                "status": "applied"
                if any(v.decision == "applied" for v in videos)
                else "reused",
                "videos": [
                    {
                        "source_item_id": v.video_item_id,
                        "width": f.width,
                        "height": f.height,
                        "duration_ms": f.duration_ms,
                        "video_streams": f.video_streams,
                        "audio_streams": f.audio_streams,
                    }
                    for v in restore.videos
                    if (f := v.restore) is not None
                ],
            },
            "subtitles": {
                "status": "present" if zh_items else "no_speech",
                "language": "zh" if zh_items else None,
                "item_ids": zh_items,
                "kept_ja": sum(s.kept_ja or 0 for s in subtitles),
            },
        }

    def _input_hash(self, context: ReturnContext) -> str:
        rt = context.runtime
        return rt.store.get_stage(rt.operator, context.job.job_id).input_hash

    # ── building ───────────────────────────────────────────────────────
    @staticmethod
    def _complete(package: Path, members: list[_Member], raw: bytes) -> bool:
        """A finished package with exactly these bytes is reused (E5)."""
        listing = package / MANIFEST_NAME
        if not listing.is_file() or listing.read_bytes() != raw:
            return False
        for member in members:
            path = package / member.relative_path
            if (
                not path.is_file()
                or path.stat().st_size != member.size
                or sha256_file(path) != member.sha256
            ):
                return False
        return True

    def _build(self, package: Path, members: list[_Member], raw: bytes) -> None:
        partial = package.with_name(package.name + ".partial")
        shutil.rmtree(partial, ignore_errors=True)
        for member in members:
            target = partial / member.relative_path
            target.parent.mkdir(parents=True, exist_ok=True)
            if member.link:
                try:
                    os.link(member.source, target)
                except OSError:
                    shutil.copyfile(member.source, target)
            else:
                shutil.copyfile(member.source, target)
            if sha256_file(target) != member.sha256:
                raise StageResultError(f"回传文件核对失败：{member.relative_path}")
        (partial / MANIFEST_NAME).write_bytes(raw)
        if package.exists():
            shutil.rmtree(package)
        for attempt in range(_RENAME_TRIES):
            try:
                os.replace(partial, package)
                return
            except PermissionError:  # an indexer or antivirus holding a handle
                time.sleep(0.5 * (attempt + 1))
        raise _Busy("回传目录暂时被占用，稍后重试")

    # ── receipt ────────────────────────────────────────────────────────
    def _receipt(
        self, context: ReturnContext, members: list[_Member], digest: str
    ) -> None:
        rt = context.runtime
        receiver = rt.receiver_auth
        items = tuple(
            m.VerifiedItem(
                item_id=x.item_id,
                size_bytes=x.size,
                sha256=x.sha256,
                evidence=rt.evidence(
                    "artifact_verified", producer=receiver, item_ids=(x.item_id,)
                ),
            )
            for x in members
        )
        rt.store.accept_artifact_receipt(
            receiver,
            rt.meta(),
            m.ArtifactReceipt(
                receipt_id=str(uuid.uuid4()),
                receiver_id=receiver.principal_id,
                operation_id=context.operation_id,
                attempt_id=context.attempt_id,
                input_hash=self._input_hash(context),
                output_manifest_hash=digest,
                status="passed",
                items=items,
                durable_reference=context.operation_id,
                accepted_at=rt.clock.now(),
            ),
        )

    @staticmethod
    def _drop_old_revisions(rt: LocalRuntime, film_id: str, revision: int) -> None:
        """Old revisions' packages and stage outputs are superseded (D20)."""
        root = rt.work_dir / "return"
        for old in range(1, revision):
            shutil.rmtree(root / f"{film_id}-r{old}", ignore_errors=True)
        for result in rt.work_dir.glob(f"*/{RESULT_NAME}"):
            try:
                data = json.loads(result.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                data.get("film_id") == film_id
                and data.get("input_revision", revision) < revision
            ):
                shutil.rmtree(result.parent, ignore_errors=True)


class _Busy(Exception):
    pass


def _relative(rt: LocalRuntime, path: Path) -> str:
    return PurePosixPath(path.resolve().relative_to(rt.work_dir.resolve())).as_posix()
