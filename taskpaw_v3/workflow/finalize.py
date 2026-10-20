"""确认入库 → archive → receipt → exact cleanup (F02-P1).

``confirm`` is the operator's one click: it binds the film's final package, the chosen Filmpaw performer
folder and both cleanup scopes into a T01 approval. ``Finalizer.run_once`` then moves every approved
film forward a step per tick without further input: submit the package to Filmpaw, accept its receipt,
delete the bound sources on the same handle that proved them, and reclaim Taskpaw's private work dirs.

Nothing here retries a copy: an archive is submitted once per approval under its operation key and only
looked up afterwards; a failed archive waits for the operator to confirm again.
"""

from __future__ import annotations

import json
import logging
import shutil
import uuid
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from . import file_identity
from . import models as m
from .filmpaw_client import FilmpawClient, FilmpawRefused, FilmpawUnavailable
from .local_runtime import PRODUCER, LocalRuntime

log = logging.getLogger("taskpaw.workflow.finalize")

APPROVAL_LIFETIME = timedelta(
    days=3650
)  # single user: a late receipt must still allow cleanup (D03)
_PERFORMER_NS = uuid.UUID("0b7f3c2e-6f2d-4a8e-9c1d-3e5a7b9d2f41")
_STAGES = ("archive", "cleanup_input", "cleanup_workcopy")
_OPEN = ("pending", "failed", "running", "unknown")
_RETRYABLE = ("pending", "blocked_writing", "failed", "unknown")
FILMPAW_WAIT = "等待 Filmpaw 运行"

Scope = Literal["input", "workcopy"]


class ConfirmError(Exception):
    """Why 确认入库 was refused; nothing was written anywhere."""


def performer_uuid(performer_id: str) -> str:
    return str(uuid.uuid5(_PERFORMER_NS, f"filmpaw-performer:{performer_id}"))


def _host(target_path: str) -> str:
    drive = PureWindowsPath(target_path).drive
    return drive.strip("\\").split("\\")[0] if drive.startswith("\\\\") else "local"


def _new_id() -> str:
    return str(uuid.uuid4())


class Finalizer:
    def __init__(
        self, runtime: LocalRuntime, inbox: Path | None, filmpaw: FilmpawClient
    ) -> None:
        self.runtime, self.inbox, self.filmpaw = runtime, inbox, filmpaw
        self.waiting: dict[str, str] = {}

    # ── paths ───────────────────────────────────────────────────────────
    def _member_path(self, member: m.CleanupMember, final: m.FinalManifest) -> Path:
        rt = self.runtime
        if (
            member.root_id == rt.identity.work_root_id
        ):  # final items are film-dir relative
            return rt.work_dir / final.output_relative_path / member.relative_path
        if member.root_id == rt.identity.root_id and self.inbox is not None:
            return self.inbox / member.relative_path
        raise ConfirmError(f"无法定位清理成员的根目录：{member.root_id}")

    def _package_dir(self, final: m.FinalManifest) -> Path:
        return self.runtime.work_dir / final.output_relative_path

    # ── 确认入库 ────────────────────────────────────────────────────────
    def confirm(self, film_id: str, performer_id: str) -> str:
        """Approve archiving this film's final package under the performer's folder; the approval id."""
        rt = self.runtime
        op, store = rt.operator, rt.store
        wf = store.film_workflow(op, film_id)
        if wf.scrape_status != "succeeded":
            raise ConfirmError("影片尚未完成整理（刮削）")
        if wf.archive_status not in ("pending", "failed"):
            raise ConfirmError("影片已在归档或已入库")
        if any(not a.resolved for a in wf.archive_operations):
            raise ConfirmError("上一次归档仍在进行")
        film = store.get_film(op, film_id).film
        if film.current_input_revision is None or film.current_final_revision is None:
            raise ConfirmError("影片没有可归档的整理结果")
        source = store.get_input_manifest(op, film_id, film.current_input_revision)
        final = store.get_final_manifest(op, film_id, film.current_final_revision)
        film_dir = PurePosixPath(final.output_relative_path).name
        try:
            proof = self.filmpaw.target(performer_id, film_dir)
        except FilmpawUnavailable as e:
            raise ConfirmError(f"Filmpaw 未运行：{e}") from e
        except FilmpawRefused as e:
            raise ConfirmError(e.message) from e
        if not proof["absent"]:
            raise ConfirmError(f"演员目录下已有同名目录：{film_dir}")
        target = self._target(performer_id, film_dir, proof["expected_target"])
        input_cleanup = self._cleanup_manifest(
            "input", source.manifest_hash, source.items
        )
        workcopy_cleanup = self._cleanup_manifest(
            "workcopy", final.manifest_hash, final.items
        )
        problems = [
            f"{member.relative_path}：{status}"
            for manifest in (input_cleanup, workcopy_cleanup)
            for member in manifest.members
            if (status := file_identity.check(self._member_path(member, final), member))
            != "ok"
        ]
        if problems:
            raise ConfirmError("来源文件与整理结果不一致：" + "；".join(problems[:5]))
        binding = m.ApprovalBinding(
            film_id=film_id,
            input_revision=film.current_input_revision,
            input_hash=source.manifest_hash,
            final_revision=film.current_final_revision,
            final_hash=final.manifest_hash,
            policy_revision=film.policy.policy_revision,
            postprocess_digest=m.digest(final.postprocess),
            metadata_revision=1,
            candidate_revision=1,
            actor_ids=(target.root_id,),
            target=target,
            input_cleanup=input_cleanup,
            input_cleanup_hash=m.digest(input_cleanup),
            workcopy_cleanup=workcopy_cleanup,
            workcopy_cleanup_hash=m.digest(workcopy_cleanup),
        )
        signer = rt.archive_auth
        observations = m.ApprovalObservations(
            metadata=rt.evidence("final_metadata", film_dir, producer=signer),
            candidate=rt.evidence(
                "operator_choice", f"performer {performer_id}", producer=signer
            ),
            target=rt.evidence(
                "filmpaw_target", payload_digest=m.digest(proof), producer=signer
            ),
            input_cleanup=rt.evidence("input_members_checked", producer=signer),
            workcopy_cleanup=rt.evidence("workcopy_members_checked", producer=signer),
        )
        try:
            put = store.put_approval_context(
                op,
                rt.meta(),
                m.ApprovalContextPut(
                    film_id=film_id,
                    expected_version=film.version,
                    expected_context_revision=film.current_context_revision or 0,
                    binding=binding,
                    observations=observations,
                ),
            )
            if (
                put.status != "ready"
                or put.context_revision is None
                or put.context_hash is None
            ):
                raise ConfirmError(f"审批上下文未就绪：{put.status}")
            approval_id = _new_id()
            store.approve(
                op,
                rt.meta(),
                m.ApprovalCreate(
                    film_id=film_id,
                    expected_version=put.version,
                    approval_id=approval_id,
                    context_revision=put.context_revision,
                    context_hash=put.context_hash,
                    binding=binding,
                    expires_at=rt.clock.now() + APPROVAL_LIFETIME,
                ),
            )
        except m.WorkflowError as exc:  # e.g. an archive was admitted meanwhile (IR07)
            raise ConfirmError(f"影片状态已变化，请刷新后重试：{exc}") from exc
        return approval_id

    def _target(
        self, performer_id: str, film_dir: str, expected: dict[str, Any]
    ) -> m.TargetIdentity:
        root = performer_uuid(performer_id)
        return m.TargetIdentity(
            root_id=root,
            host_identity=_host(expected["target_path"]),
            export_identity=expected["target_path"],
            native_identity=m.NativeIdentity(
                scheme="export_object_id",
                root_id=root,
                volume_or_export_id=performer_id,  # the Filmpaw performer whose folder this is
                object_id=json.dumps(expected["directory_identity"]),
                observed_at=self.runtime.clock.now(),
                evidence_id=_new_id(),
            ),
            relative_path=film_dir,
            expected_absent=True,
        )

    @staticmethod
    def _cleanup_manifest(
        scope: Scope, manifest_hash: str, items: Iterable[m.ManifestItem]
    ) -> m.CleanupManifest:
        members = []
        for item in items:
            if item.native_identity is None:
                raise ConfirmError(f"文件缺少身份记录：{item.relative_path}")
            members.append(
                m.CleanupMember(
                    item_id=item.item_id,
                    root_id=item.source_root_id,
                    relative_path=item.relative_path,
                    size_bytes=item.size_bytes,
                    sha256=item.sha256,
                    native_identity=item.native_identity,
                )
            )
        return m.CleanupManifest(
            scope=scope, source_manifest_hash=manifest_hash, members=tuple(members)
        )

    # ── one tick ────────────────────────────────────────────────────────
    def run_once(self) -> bool:
        """Advance every approved film one step; True when anything changed."""
        rt = self.runtime
        films: list[str] = []
        for status in _OPEN:
            for job in rt.store.list_jobs(rt.operator, status, _STAGES):
                if job.film_id not in films:
                    films.append(job.film_id)
        waiting: dict[str, str] = {}
        worked = False
        for film_id in films:
            try:
                worked |= self._advance(film_id, waiting)
            except m.WorkflowError as exc:
                if str(exc) == "authority_unavailable":
                    raise
                log.warning("finalize %s: %s", film_id, exc)
                waiting[film_id] = f"归档/清理暂停：{exc}"
            except Exception as exc:  # one broken film must not stop the others (IR04)
                log.exception("finalize %s failed", film_id)
                waiting[film_id] = f"归档/清理出错：{exc}"
        self.waiting = waiting
        return worked

    def _advance(self, film_id: str, waiting: dict[str, str]) -> bool:
        wf = self.runtime.store.film_workflow(self.runtime.operator, film_id)
        if wf.archive_status == "succeeded":
            return self._cleanups(wf, waiting)
        open_ops = [a for a in wf.archive_operations if not a.resolved]
        if open_ops:
            return self._poll(wf, open_ops[0], waiting)
        approval = wf.approval
        if (
            wf.archive_status not in ("pending", "failed")
            or approval is None
            or approval.revoked
        ):
            waiting[film_id] = "等待确认入库"
            return False
        if any(a.approval_id == approval.approval_id for a in wf.archive_operations):
            failed = next(
                a
                for a in wf.archive_operations
                if a.approval_id == approval.approval_id
            )
            waiting[film_id] = (
                f"归档失败，请处理后重新确认入库：{failed.error_code or ''}"
            )
            return False
        return self._begin_archive(wf, approval, waiting)

    # ── archive ─────────────────────────────────────────────────────────
    def _begin_archive(
        self, wf: m.FilmWorkflow, approval: m.WorkflowApproval, waiting: dict[str, str]
    ) -> bool:
        rt = self.runtime
        operation_id = _new_id()
        rt.store.begin_archive(
            rt.operator,
            rt.meta(),
            m.ArchiveAdmission(
                film_id=wf.film_id,
                operation_id=operation_id,
                expected_version=wf.version,
                approval_id=approval.approval_id,
                binding_hash=approval.binding_hash,
                adapter_id=rt.archive_auth.principal_id,
            ),
        )
        self._submit(wf.film_id, operation_id, approval.binding, waiting)
        return True

    def _submit(
        self,
        film_id: str,
        operation_id: str,
        binding: m.ApprovalBinding,
        waiting: dict[str, str],
    ) -> None:
        final = self.runtime.store.get_final_manifest(
            self.runtime.operator, film_id, binding.final_revision
        )
        target = binding.target
        try:
            self.filmpaw.archive(
                operation_id,
                self._package_dir(final),
                target.native_identity.volume_or_export_id,
                {
                    "target_path": target.export_identity,
                    "directory_identity": json.loads(target.native_identity.object_id),
                },
                [
                    {
                        "path": item.relative_path,
                        "size": item.size_bytes,
                        "sha256": item.sha256,
                    }
                    for item in final.items
                ],
            )
            waiting[film_id] = "归档中"
        except FilmpawUnavailable:
            waiting[film_id] = FILMPAW_WAIT  # looked up by operation key next tick
        except FilmpawRefused as e:
            if e.status in (
                409,
                503,
            ):  # busy, nothing created: submit again later (D06)
                waiting[film_id] = f"Filmpaw 暂时无法归档：{e.message}"
            else:
                self._fail(operation_id, f"Filmpaw 拒绝归档：{e.message}")

    def _poll(
        self,
        wf: m.FilmWorkflow,
        op: m.WorkflowArchiveOperation,
        waiting: dict[str, str],
    ) -> bool:
        approval = wf.approval
        if approval is None or approval.approval_id != op.approval_id:
            waiting[wf.film_id] = "归档审批已变化"
            return False
        try:
            task = self.filmpaw.lookup(op.operation_id)
        except FilmpawUnavailable:
            waiting[wf.film_id] = FILMPAW_WAIT
            return False
        except FilmpawRefused as e:
            waiting[wf.film_id] = f"Filmpaw 查询失败：{e.message}"
            return False
        if task is None:  # never accepted: the same operation key is safe to submit
            self._submit(wf.film_id, op.operation_id, approval.binding, waiting)
            return True
        status = task.get("status")
        if status in ("queued", "running"):
            waiting[wf.film_id] = "归档中"
            return False
        if status == "completed" and task.get("receipt"):
            self._accept(wf, op, approval.binding, task)
            return True
        self._fail(
            op.operation_id,
            f"Filmpaw {status}：{task.get('message') or ''}",
            op.version,
        )
        return True

    def _accept(
        self,
        wf: m.FilmWorkflow,
        op: m.WorkflowArchiveOperation,
        binding: m.ApprovalBinding,
        task: dict[str, Any],
    ) -> None:
        rt = self.runtime
        final = rt.store.get_final_manifest(
            rt.operator, wf.film_id, binding.final_revision
        )
        receipt = task["receipt"]
        files = {f["path"]: (f["size"], f["sha256"]) for f in receipt["files"]}
        expected = {i.relative_path: (i.size_bytes, i.sha256) for i in final.items}
        target_path = str(
            Path(binding.target.export_identity) / binding.target.relative_path
        )
        if files != expected or receipt["target_path"] != target_path:
            self._fail(op.operation_id, "Filmpaw 回执与已批准清单不一致", op.version)
            return
        signer = rt.archive_auth
        rt.store.accept_archive_receipt(
            signer,
            rt.meta(),
            m.ArchiveReceipt(
                receipt_id=_new_id(),
                operation_id=op.operation_id,
                approval_id=op.approval_id,
                producer_id=signer.principal_id,
                producer_version=PRODUCER,
                target=binding.target,
                target_directory_identity=binding.target.native_identity.model_copy(
                    update={
                        "object_id": json.dumps(receipt["directory_identity"]),
                        "evidence_id": _new_id(),
                        "observed_at": rt.clock.now(),
                    }
                ),
                final_manifest_hash=final.manifest_hash,
                items=tuple(
                    m.VerifiedItem(
                        item_id=item.item_id,
                        size_bytes=item.size_bytes,
                        sha256=item.sha256,
                        evidence=rt.evidence(
                            "nas_sha256", item.relative_path, producer=signer
                        ),
                    )
                    for item in final.items
                ),
                commit_marker=m.digest(receipt),
                durable_reference=str(uuid.UUID(task["id"])),
                accepted_at=rt.clock.now(),
            ),
        )

    def _fail(
        self, operation_id: str, message: str, version: int | None = None
    ) -> None:
        rt = self.runtime
        if version is None:
            archive = rt.store.get_operation(rt.operator, operation_id).archive
            if archive is None:
                raise m.WorkflowError("not_found")
            version = archive.version
        rt.store.fail_archive(
            rt.archive_auth,
            rt.meta(),
            m.ArchiveFailure(
                operation_id=operation_id,
                expected_version=version,
                error_code=message[:2000],
                evidence=rt.evidence(
                    "filmpaw_failed", message[:2000], producer=rt.archive_auth
                ),
            ),
        )

    # ── cleanup ─────────────────────────────────────────────────────────
    def _cleanups(self, wf: m.FilmWorkflow, waiting: dict[str, str]) -> bool:
        approval = wf.approval
        if approval is None or approval.approval_id != wf.archive_receipt_approval_id:
            waiting[wf.film_id] = "清理等待：审批已变化"
            return False
        rt = self.runtime
        final = rt.store.get_final_manifest(
            rt.operator, wf.film_id, approval.binding.final_revision
        )
        worked = False
        scopes: tuple[tuple[Scope, m.StageStatus | None], ...] = (
            ("workcopy", wf.cleanup_workcopy_status),
            ("input", wf.cleanup_input_status),
        )
        for scope, status in scopes:
            if status == "succeeded":
                continue
            if scope == "input" and not self._package_archived(wf):
                # deleting this film's originals dirties the package and would block its siblings
                waiting[wf.film_id] = "包内仍有未入库影片"
                continue
            worked |= self._cleanup(wf, approval, final, scope, waiting)
        return worked

    def _cleanup(
        self,
        wf: m.FilmWorkflow,
        approval: m.WorkflowApproval,
        final: m.FinalManifest,
        scope: Scope,
        waiting: dict[str, str],
    ) -> bool:
        rt = self.runtime
        manifest = (
            approval.binding.input_cleanup
            if scope == "input"
            else approval.binding.workcopy_cleanup
        )
        ops = [
            c
            for c in wf.cleanup_operations
            if c.scope == scope and c.approval_id == approval.approval_id
        ]
        current = next((c for c in ops if not c.resolved), None)
        if current is None and ops:
            return False  # verified already
        if current is None:
            admitted = self._admit(wf, approval, manifest, final, scope, waiting)
            if admitted is None:
                return False
            operation_id, version, statuses = admitted, None, {}
        else:
            operation_id, version = current.operation_id, current.version
            statuses = {i.item_id: i.status for i in current.items}
        results = []
        for member in manifest.members:
            if statuses.get(member.item_id, "pending") not in _RETRYABLE:
                continue  # done, or changed/replaced: kept for the operator (C7)
            status = file_identity.delete_exact(
                self._member_path(member, final), member
            )
            results.append(
                m.CleanupItemResult(
                    item_id=member.item_id,
                    status=status,
                    evidence=rt.evidence(
                        f"same_handle_{status}",
                        member.relative_path,
                        producer=rt.cleanup_auth,
                    ),
                    error_code=None
                    if status in ("deleted", "already_absent")
                    else status,
                    recorded_at=rt.clock.now(),
                )
            )
        if not results:
            waiting[wf.film_id] = "待清理：来源文件已变化，请手动处理"
            return False
        if version is None:
            intent = rt.store.get_operation(rt.operator, operation_id).cleanup
            if intent is None:
                raise m.WorkflowError("not_found")
            version = intent.version
        rt.store.record_cleanup_result(
            rt.cleanup_auth,
            rt.meta(),
            m.CleanupResult(
                operation_id=operation_id,
                expected_version=version,
                items=tuple(results),
            ),
        )
        after = rt.store.film_workflow(rt.operator, wf.film_id)
        done = (
            after.cleanup_input_status
            if scope == "input"
            else after.cleanup_workcopy_status
        )
        if done == "succeeded":
            try:
                self._reclaim(after, scope, final)
            except Exception:  # the cleanup itself is recorded; reclaim is housekeeping
                log.exception(
                    "reclaim after %s cleanup of %s failed", scope, wf.film_id
                )
        else:
            blocked = [
                r for r in results if r.status not in ("deleted", "already_absent")
            ]
            reason = blocked[0].status if blocked else "部分文件已变化"
            waiting[wf.film_id] = f"待清理：{len(blocked)} 个文件未能删除（{reason}）"
        return True

    def _admit(
        self,
        wf: m.FilmWorkflow,
        approval: m.WorkflowApproval,
        manifest: m.CleanupManifest,
        final: m.FinalManifest,
        scope: Scope,
        waiting: dict[str, str],
    ) -> str | None:
        """Admit the bound members as they are; exactness is judged on each delete handle (D05)."""
        rt = self.runtime
        writing = [
            member.relative_path
            for member in manifest.members
            if file_identity.check(self._member_path(member, final), member)
            == "blocked_writing"
        ]
        if writing:
            waiting[wf.film_id] = f"等待来源文件写入结束：{writing[0]}"
            return None
        signer = rt.cleanup_auth
        bound = rt.evidence(
            "bound_members",
            "逐成员在删除句柄上核对身份、大小与 SHA-256",
            producer=signer,
        )
        writer = bound.model_copy(
            update={
                "kind": "no_writer_open",
                "summary": m.EvidenceSummary(active_writers=False),
            }
        )
        operation_id = _new_id()
        rt.store.begin_cleanup(
            rt.operator,
            rt.meta(),
            m.CleanupAdmission(
                film_id=wf.film_id,
                operation_id=operation_id,
                expected_version=wf.version,
                approval_id=approval.approval_id,
                archive_receipt_id=wf.archive_receipt_id or "",
                scope=scope,
                manifest=manifest,
                manifest_hash=m.digest(manifest),
                source_observation=m.SourceObservation(
                    manifest_hash=m.digest(manifest),
                    membership_digest=m.digest(
                        tuple(sorted(manifest.members, key=lambda item: item.item_id))
                    ),
                    items=manifest.members,
                    writer_check=writer,
                    content_check=bound,
                    identity_check=bound,
                    membership_check=bound,
                    shared_references_released=bound,
                ),
            ),
        )
        return operation_id

    # ── reclaim (C10) ───────────────────────────────────────────────────
    def _reclaim(
        self, wf: m.FilmWorkflow, scope: Scope, final: m.FinalManifest
    ) -> None:
        rt = self.runtime
        if scope == "workcopy":
            self._reclaim_film(wf.film_id, final)
            if (
                wf.package_id
                and wf.snapshot_work_path
                and self._package_done(wf.package_id, "workcopy")
            ):
                shutil.rmtree(rt.work_dir / wf.snapshot_work_path, ignore_errors=True)
            return
        if not (wf.package_id and self._package_done(wf.package_id, "input")):
            return
        if self.inbox is not None and wf.package_path:
            package = self.inbox / wf.package_path
            if package.resolve() != self.inbox.resolve():  # never the INBOX root itself
                _rmdir_empty(package)
        latest = rt.store.film_workflow(
            rt.operator, wf.film_id
        )  # rmdir may have bumped it
        if latest.package_current and latest.package_version is not None:
            rt.store.retire_archived_package(
                rt.operator,
                rt.meta(),
                m.ArchivedPackageRetirement(
                    package_id=wf.package_id, expected_version=latest.package_version
                ),
            )

    def _package_archived(self, wf: m.FilmWorkflow) -> bool:
        if wf.package_id is None:
            return True
        films = self.runtime.store.package_films(self.runtime.operator, wf.package_id)
        return all(f.archive_status == "succeeded" for f in films)

    def _package_done(self, package_id: str, scope: Scope) -> bool:
        films = self.runtime.store.package_films(self.runtime.operator, package_id)
        field = (
            "cleanup_input_status" if scope == "input" else "cleanup_workcopy_status"
        )
        return bool(films) and all(getattr(f, field) == "succeeded" for f in films)

    def _reclaim_film(self, film_id: str, final: m.FinalManifest) -> None:
        """Taskpaw's own per-film dirs: compute attempts, return package, scrape attempts."""
        rt = self.runtime
        view = rt.store.get_film(rt.operator, film_id)
        for stage in view.stages:
            if stage.stage in ("restore", "asr", "translate"):
                for attempt in rt.store.list_attempts(rt.operator, stage.job_id):
                    shutil.rmtree(rt.work_dir / attempt.attempt_id, ignore_errors=True)
        for parent in ("return", "scrape"):
            for path in (rt.work_dir / parent).glob(f"{film_id}-r*"):
                shutil.rmtree(path, ignore_errors=True)


def _rmdir_empty(root: Path) -> None:
    """Remove now-empty directories bottom-up; anything left (a new file) keeps its parents."""
    if not root.is_dir():
        return
    for path in sorted(
        (p for p in root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    ):
        try:
            path.rmdir()
        except OSError:
            pass
    try:
        root.rmdir()
    except OSError:
        pass
