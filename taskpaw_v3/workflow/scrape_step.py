"""M01-P1 scrape: Miko full post-processing of a return package, as a T01 final.

The return package is staged privately (videos hard-linked, the Chinese subtitle
copied as `<video>.srt`, old NFO/images left out; a Japanese subtitle is never
staged), MDCX304's one-shot `postprocess_one` runs in its own environment inside the attempt's kill-on-close
Job, and its verified output becomes the film's FinalManifest. Contract:
docs/specs/2026-10-20-media-workflow-m01-p1-{design,interface}.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Sequence
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from . import file_identity
from . import models as m
from .exec_restore import ffprobe_command, is_ltx_name, is_restored_name, probe_video
from .executors import SUCCEEDED_KIND, ErrorClass, ExecOutcome
from .local_dispatch import ReturnContext
from .local_runtime import LocalRuntime
from .return_step import MANIFEST_NAME, package_dir
from .stage_results import sha256_file

FORCED_TABLE_VERSION = 1  # must match mdcx.cmd.postprocess_one
CLI_VERSION = "postprocess_one/1"
SCHEMA = 1
_ITEM_NS = uuid.UUID("7e3b9a41-2c6d-4f08-8a51-6d2c0b9e4f17")
_VOLATILE = frozenset({"success.txt", "remain.txt"})  # interactive Miko rewrites
_ROLES = {
    "main_video",
    "nfo",
    "poster",
    "thumb",
    "fanart",
    "folder_cover",
    "subtitle",
}


class _Failure(Exception):
    def __init__(self, error_class: ErrorClass, message: str) -> None:
        super().__init__(message)
        self.error_class = error_class


def _userdata_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        (p for p in root.rglob("*") if p.is_file() and p.name not in _VOLATILE),
        key=lambda p: p.relative_to(root).as_posix(),
    )


def config_digest(profile: Path, userdata: Path) -> str:
    """Everything that shapes Miko's output, known before the claim (D10, D18)."""
    digest = hashlib.sha256()
    for part in (str(FORCED_TABLE_VERSION).encode(), CLI_VERSION.encode()):
        digest.update(len(part).to_bytes(8, "big") + part)
    raw = profile.read_bytes()
    digest.update(len(raw).to_bytes(8, "big") + raw)
    for path in _userdata_files(userdata):
        name = path.relative_to(userdata).as_posix().encode()
        data = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


class LocalScrapeStep:
    def __init__(
        self,
        *,
        python: str,
        root: str,
        profile: str,
        userdata: str | None = None,
        ffprobe: str | Sequence[str] | None = None,
        launch_prefix: Sequence[str] = (),
    ) -> None:
        """`launch_prefix` replaces `python -m mdcx.cmd.postprocess_one` in tests."""
        self.python, self.root, self.profile = python, Path(root), Path(profile)
        self.userdata = Path(userdata) if userdata else self.profile.parent / "userdata"
        self.ffprobe = ffprobe_command(ffprobe)
        self.launch_prefix = list(launch_prefix)

    def tools(self) -> m.ToolVersions:
        return m.ToolVersions(
            tool="mdcx-postprocess",
            tool_version=CLI_VERSION,
            model="miko",
            model_version=str(FORCED_TABLE_VERSION),
            profile="workflow-scrape",
            profile_revision=1,
            config_digest=config_digest(self.profile, self.userdata),
        )

    def run(self, context: ReturnContext) -> ExecOutcome:
        try:
            return self._run(context)
        except _Failure as exc:
            return ExecOutcome.failed(exc.error_class, str(exc))

    # ── steps ──────────────────────────────────────────────────────────
    def _run(self, context: ReturnContext) -> ExecOutcome:
        rt, job = context.runtime, context.job
        handoff, handoff_sha = self._handoff(rt, job)
        tools = next(  # what was claimed, never recomputed (D10)
            a.tool_versions
            for a in rt.store.list_attempts(rt.operator, job.job_id)
            if a.attempt_id == context.attempt_id
        )
        base = rt.work_dir / "scrape" / f"{job.film_id}-r{job.input_revision}"
        attempt = base / context.attempt_id
        shutil.rmtree(attempt, ignore_errors=True)
        dirs = {name: attempt / name for name in ("config", "in", "out", "failed")}
        for path in dirs.values():
            path.mkdir(parents=True, exist_ok=True)
        self._copy_config(dirs["config"], tools.config_digest)
        package = package_dir(rt, job.film_id, job.input_revision)
        videos = self._stage(handoff, package, dirs["in"])
        request = {
            "schema": SCHEMA,
            "attempt_id": context.attempt_id,
            "staging": {k: str(dirs[k]) for k in ("in", "out", "failed")},
            "videos": videos,
        }
        (attempt / "request.json").write_text(
            json.dumps(request, ensure_ascii=False), encoding="utf-8"
        )
        result, result_sha = self._launch(context, attempt, dirs["config"])
        files = self._verify(dirs["out"], result)
        final = self._final(
            context, tools, handoff, handoff_sha, result, result_sha, files, attempt
        )
        film = rt.store.get_film(rt.operator, job.film_id).film
        rt.store.put_final_manifest(
            rt.scrape_auth,
            rt.meta(),
            m.FinalManifestPut(
                expected_version=film.version,
                film_id=job.film_id,
                attempt_id=context.attempt_id,
                manifest=final,
            ),
        )
        shutil.rmtree(dirs["in"], ignore_errors=True)
        for old in base.iterdir() if base.is_dir() else ():
            if old.name != context.attempt_id:  # superseded attempts
                shutil.rmtree(old, ignore_errors=True)
        return ExecOutcome.succeeded("刮削完成", result_digest=final.manifest_hash)

    def _handoff(
        self, rt: LocalRuntime, job: m.PendingComputeJob
    ) -> tuple[dict[str, Any], str]:
        """The return hand-off, checked against the succeeded return attempt (D19)."""
        view = rt.store.get_film(rt.operator, job.film_id)
        ret = next((s for s in view.stages if s.stage == "return"), None)
        if ret is None or ret.status != "succeeded" or ret.attempt_id is None:
            raise _Failure("needs_attention", "回传尚未完成")
        attempt = next(
            (
                a
                for a in rt.store.list_attempts(rt.operator, ret.job_id)
                if a.attempt_id == ret.attempt_id
            ),
            None,
        )
        digests = [
            e.payload_digest
            for e in (attempt.evidence if attempt else ())
            if e.kind == SUCCEEDED_KIND
        ]
        path = package_dir(rt, job.film_id, job.input_revision) / MANIFEST_NAME
        if not digests or not path.is_file():
            raise _Failure("needs_attention", "找不到可核对的回传包")
        raw = path.read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if sha != digests[0]:
            raise _Failure("needs_attention", "回传包与回执不符")
        return json.loads(raw), sha

    def _copy_config(self, config: Path, claimed: str) -> None:
        if not self.profile.is_file():
            raise _Failure("needs_attention", f"找不到 Miko 配置：{self.profile}")
        shutil.copyfile(self.profile, config / "user-config.json")
        target = config / "userdata"
        for path in _userdata_files(self.userdata):
            dest = target / path.relative_to(self.userdata)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, dest)
        if config_digest(config / "user-config.json", target) != claimed:
            raise _Failure("transient", "Miko 配置在领取后发生变化")

    def _stage(
        self, handoff: dict[str, Any], package: Path, staging: Path
    ) -> list[dict[str, Any]]:
        items = handoff["items"]
        zh_ids = set(handoff["subtitles"]["item_ids"])
        restored = handoff["restoration"]["status"] in ("applied", "reused")
        videos = []
        for entry in self._stage_index(handoff):
            item, staged, volume = entry["item"], entry["file"], entry["volume"]
            source = _checked(package, item)
            try:
                os.link(source, staging / staged)
            except OSError:
                shutil.copyfile(source, staging / staged)
            subs = [
                i for i in items if i.get("video_item_id") == item["source_item_id"]
            ]
            zh = [s for s in subs if s["item_id"] in zh_ids]
            for sub in zh[:1]:
                shutil.copyfile(
                    _checked(package, sub),
                    staging / f"{PurePosixPath(staged).stem}.srt",
                )
            videos.append(
                {
                    "file": staged,
                    "volume": volume,
                    "restored": restored,
                    "ltx": entry["ltx"],  # #303: Miko stamps 无码, not 破解
                    "zh_subtitle": bool(zh),
                }
            )
        return videos

    def _launch(
        self, context: ReturnContext, attempt: Path, config: Path
    ) -> tuple[dict[str, Any], str]:
        assert context.processes is not None
        request, result_path = attempt / "request.json", attempt / "result.json"
        command = self.launch_prefix or [self.python, "-m", "mdcx.cmd.postprocess_one"]
        env = {**os.environ, "MDCX_CONFIG_DIR": str(config)}
        child = context.processes.launch(
            [*command, "--request", str(request), "--result", str(result_path)],
            cwd=str(self.root),
            env=env,
            on_line=lambda _, text: None,
        )
        code = child.wait(context.cancel)
        if code is None:
            raise _Failure("cancelled", "已停止")
        try:
            raw = result_path.read_bytes()
            result = json.loads(raw)
        except (OSError, ValueError):
            raise _Failure(
                "transient", f"Miko 后处理没有返回结果（退出码 {code}）"
            ) from None
        if result.get("schema") != SCHEMA:
            raise _Failure("needs_attention", "Miko 后处理结果版本不符")
        if result.get("status") != "succeeded" or code != 0:
            error_class = result.get("error_class")
            raise _Failure(
                error_class
                if error_class in ("needs_attention", "transient")
                else "needs_attention",
                result.get("message") or "Miko 后处理失败",
            )
        return result, hashlib.sha256(raw).hexdigest()

    @staticmethod
    def _verify(out: Path, result: dict[str, Any]) -> list[dict[str, Any]]:
        """Every file on disk is listed, every listed file has its hash (M3)."""
        listed = {f["path"]: f for f in result["files"]}
        actual = {p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file()}
        if actual != set(listed):
            raise _Failure("needs_attention", "Miko 输出与结果清单不一致")
        for path, entry in listed.items():
            if entry["role"] not in _ROLES:
                raise _Failure("needs_attention", f"未知的输出类型：{path}")
            file = out / path
            if (
                file.stat().st_size != entry["size"]
                or sha256_file(file) != entry["sha256"]
            ):
                raise _Failure("needs_attention", f"输出文件核对失败：{path}")
        return list(listed.values())

    # ── final manifest ─────────────────────────────────────────────────
    def _final(
        self,
        context: ReturnContext,
        tools: m.ToolVersions,
        handoff: dict[str, Any],
        handoff_sha: str,
        result: dict[str, Any],
        result_sha: str,
        files: list[dict[str, Any]],
        attempt: Path,
    ) -> m.FinalManifest:
        rt, job = context.runtime, context.job
        film_dir = PurePosixPath(result["film_dir"])
        output = (PurePosixPath(_relative(rt, attempt)) / "out" / film_dir).as_posix()
        zh_role = {
            i["item_id"]: i["role"]
            for i in handoff["items"]
            if i["item_id"] in set(handoff["subtitles"]["item_ids"])
        }
        staged = {v["file"]: v for v in self._stage_index(handoff)}

        def item_id(path: str) -> str:
            return str(uuid.uuid5(_ITEM_NS, f"{job.film_id}/{output}/{path}"))

        def rel(path: str) -> str:
            return PurePosixPath(path).relative_to(film_dir).as_posix()

        def identity(path: str) -> m.NativeIdentity:
            # The same identity the cleanup delete checks on its handle (D08).
            try:
                return file_identity.native(
                    rt.work_dir / output / path, rt.identity.work_root_id
                )
            except OSError as exc:
                raise _Failure("needs_attention", f"无法读取文件身份：{path}") from exc

        videos = {f["video"]: f for f in files if f["role"] == "main_video"}
        if set(videos) != set(staged):
            raise _Failure("needs_attention", "Miko 输出的视频数量不符")
        for mark in result.get("expected_marks", ()):
            # M7/#303: the stamp Miko judged must be the one this video was sent for
            video = staged.get(mark.get("video"))
            wanted = "无码" if video and video["ltx"] else "无码破解"
            if video and mark.get("mosaic") and mark["mosaic"] != wanted:
                raise _Failure(
                    "needs_attention",
                    f"Miko 判定为 {mark['mosaic']}，应为 {wanted}：{mark['video']}",
                )
        first_file = next(iter(staged), None)  # volume order (M8)
        poster_id: str | None = None
        items: list[m.ManifestItem] = []
        by_role: dict[str, list[str]] = {}
        zh_items: list[str] = []
        for entry in files:
            role, path = entry["role"], rel(entry["path"])
            video_id = (
                item_id(rel(videos[entry["video"]]["path"]))
                if entry.get("video")
                else None
            )
            language = None
            if role == "subtitle":
                language = entry.get("language", "zh")
                role = entry.get("source_role") or next(
                    iter(zh_role.values()), "generated_subtitle"
                )
            ordinal = (
                staged[entry["video"]]["volume"]
                if entry["role"] == "main_video"
                else None
            )
            value = m.ManifestItem(
                item_id=item_id(path),
                role=role,
                relative_path=path,
                size_bytes=entry["size"],
                sha256=entry["sha256"],
                source_root_id=rt.identity.work_root_id,
                native_identity=identity(path),
                volume_ordinal=ordinal,
                video_item_id=video_id if language else None,
                language=language,
            )
            items.append(value)
            by_role.setdefault(entry["role"], []).append(value.item_id)
            if role == "poster" and entry.get("video") == first_file:
                poster_id = value.item_id  # the first volume's, never cd2's
            if language == "zh":
                zh_items.append(value.item_id)
        for role in ("nfo", "poster"):
            if len(by_role.get(role, ())) < 1:
                raise _Failure("needs_attention", f"缺少 {role}")
        poster_id = poster_id or by_role["poster"][0]
        multi = len(videos) > 1
        folder = by_role.get("folder_cover", [None])[0]
        if multi and folder is None:
            raise _Failure("needs_attention", "多卷影片缺少 folder.jpg")
        present = handoff["subtitles"]["status"] == "present"
        if present != bool(zh_items):
            raise _Failure("needs_attention", "中文字幕与回传结果不符")
        signer = rt.scrape_auth
        facts = rt.evidence(
            "return_handoff", producer=signer, payload_digest=handoff_sha
        )
        main_ids = [
            item_id(rel(f["path"]))
            for f in sorted(
                videos.values(), key=lambda f: staged[f["video"]]["volume"] or 0
            )
        ]
        probes = []
        for entry in videos.values():
            assert context.processes is not None
            facts_probe = probe_video(
                context.processes,
                self.ffprobe,
                rt.work_dir / output / rel(entry["path"]),
                attempt,
                context.cancel,
            )
            probes.append(
                m.MediaProbe(
                    item_id=item_id(rel(entry["path"])),
                    width=facts_probe.width,
                    height=facts_probe.height,
                    duration_ms=facts_probe.duration_ms,
                    video_streams=facts_probe.video_streams,
                    audio_streams=facts_probe.audio_streams,
                    evidence=rt.evidence("media_probe", producer=signer),
                )
            )
        marks: list[Literal["restored", "uncensored", "chinese_subtitles", "4k"]] = []
        if handoff["restoration"]["status"] in ("applied", "reused"):
            # an LTX video is stamped 无码, any other restored one 破解 (#303)
            if any(not v["ltx"] for v in staged.values()):
                marks.append("restored")
            if any(v["ltx"] for v in staged.values()):
                marks.append("uncensored")
        if present:
            marks.append("chinese_subtitles")
        definitions = [
            str(x.get("definition") or "") for x in result.get("expected_marks", ())
        ]
        if all(p.width and p.width >= 3840 for p in probes) and any(
            d.upper() in ("4K", "8K", "UHD", "UHD8")
            for d in definitions  # hd_name styles
        ):
            marks.append("4k")  # M7: only when Miko and our probe agree
        outputs = [
            i
            for i in items
            if i.role in ("nfo", "poster", "thumb", "fanart", "folder_cover")
        ]
        # Every part is a validated model, so the digest sees the canonical wire form.
        parts: dict[str, Any] = {
            "film_id": job.film_id,
            "revision": rt.store.latest_final_revision(rt.operator, job.film_id) + 1,
            "input_revision": job.input_revision,
            "input_hash": handoff["input_hash"],
            "items": tuple(items),
            "output_root_id": rt.identity.work_root_id,
            "output_relative_path": output,
            "tool_versions": tools,
            "restoration": m.RestorationResult(
                status=handoff["restoration"]["status"],
                video_item_ids=tuple(main_ids),
                evidence=facts,
            ),
            "subtitles": m.SubtitleResult(
                status=handoff["subtitles"]["status"],
                language="zh" if present else None,
                item_ids=tuple(zh_items),
                evidence=facts,
            ),
            "postprocess": m.PostprocessEvidence(
                attempt_id=context.attempt_id,
                tool_version=tools.tool_version,
                config_digest=tools.config_digest,
                status="passed",
                full_process=True,
                items=tuple(
                    m.PostprocessItem(
                        item_id=i.item_id, sha256=i.sha256, status="passed"
                    )
                    for i in outputs
                ),
                nfo_item_id=by_role["nfo"][0],
                poster_item_id=poster_id,
                required_image_item_ids=(poster_id,),
                actual_marks=tuple(marks),
                image_refresh="fresh",
                folder_required=multi,
                folder_item_id=folder if multi else None,
                parent_context=film_dir.as_posix() if multi else None,
                volume_item_ids=tuple(main_ids) if multi else (),
                evidence=rt.evidence(
                    "miko_postprocess", producer=signer, payload_digest=result_sha
                ),
            ),
            "media_probes": tuple(probes),
        }
        data = m.Model.model_validate({}).model_dump(mode="json") | {
            key: value.model_dump(mode="json")
            if isinstance(value, m.Model)
            else [v.model_dump(mode="json") for v in value]
            if isinstance(value, tuple)
            else value
            for key, value in parts.items()
        }
        data["manifest_hash"] = m.manifest_digest(data)
        return m.FinalManifest.model_validate(data)

    @staticmethod
    def _stage_index(handoff: dict[str, Any]) -> list[dict[str, Any]]:
        """Main videos in volume order with their staging names (M8)."""
        mains = sorted(
            (i for i in handoff["items"] if i["role"] == "main_video"),
            key=lambda i: (i["volume_ordinal"] or 0, i["relative_path"]),
        )
        multi = len(mains) > 1
        status = handoff["restoration"]["status"]
        out = []
        for index, item in enumerate(mains, start=1):
            name = PurePosixPath(item["relative_path"])
            volume = (item["volume_ordinal"] or index) if multi else None
            staged = f"{name.stem}-cd{volume}{name.suffix}" if multi else name.name
            out.append(
                {
                    "file": staged,
                    "volume": volume,
                    "item": item,
                    "ltx": _is_ltx(name.name, status),
                }
            )
        return out


def _is_ltx(name: str, status: str) -> bool:
    """Whether a video was restored with LTX (#303 D2): a `-无码` file is, a kept
    `-破解` / `_restored` one is not; any other name was made by this workflow's
    own (LTX) restore when the film's restoration was `applied`."""
    if is_ltx_name(name):
        return True
    return status == "applied" and not is_restored_name(name)


def _checked(package: Path, item: dict[str, Any]) -> Path:
    """A return-package file whose bytes still match the hand-off (Codex CX-T1).

    The facts carried into the final (restoration, Chinese subtitle) are only
    true of the receipted bytes, so a changed file stops the scrape.
    """
    path = package / item["relative_path"]
    if (
        not path.is_file()
        or path.stat().st_size != item["size"]
        or sha256_file(path) != item["sha256"]
    ):
        raise _Failure("needs_attention", f"回传包文件已变化：{item['relative_path']}")
    return path


def _relative(rt: LocalRuntime, path: Path) -> str:
    return PurePosixPath(path.resolve().relative_to(rt.work_dir.resolve())).as_posix()
