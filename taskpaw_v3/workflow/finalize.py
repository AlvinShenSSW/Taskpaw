"""确认入库 → archive → receipt → exact cleanup (F02-P1).

``confirm`` is the operator's one click: it binds the film's final package, the chosen archive
folder and both cleanup scopes into a T01 approval. ``Finalizer.run_once`` then moves every approved
film forward a step per tick without further input: submit the package to the library archive
(`library_archive.py`), accept its receipt, delete the bound sources on the same handle that proved
them, and reclaim Taskpaw's private work dirs.

Nothing here retries a copy: an archive is submitted once per approval under its operation key and only
looked up afterwards; a failed archive waits for the operator to confirm again.
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import uuid
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal

from taskpaw_v3.core.state import atomic_json

from . import file_identity
from . import models as m
from .intake_reclaim import remove_work_tree
from .library_archive import LibraryArchive, LibraryRefused, LibraryUnavailable
from .local_runtime import PRODUCER, LocalRuntime

log = logging.getLogger("taskpaw.workflow.finalize")

APPROVAL_LIFETIME = timedelta(
    days=3650
)  # single user: a late receipt must still allow cleanup (D03)
_PERFORMER_NS = uuid.UUID("0b7f3c2e-6f2d-4a8e-9c1d-3e5a7b9d2f41")
_STAGES = ("archive", "cleanup_input", "cleanup_workcopy")
_OPEN = ("pending", "failed", "running", "unknown")
_RETRYABLE = ("pending", "blocked_writing", "failed", "unknown")
LIBRARY_WAIT = "等待片库可用"
# §9.8: an archive approved through Filmpaw and still open at the upgrade
CUTOVER = "升级前未完成的归档，请重新确认入库"
# preflight error codes the 待确认 dialog knows; the message says the rest
_UI_CODES = {"readonly": "permission", "invalid": "permission"}

Scope = Literal["input", "workcopy"]


class ConfirmError(Exception):
    """Why 确认入库 was refused; nothing was written anywhere.

    `code` is the store's error code when the refusal wraps one (a revision
    conflict is `cas_conflict`); a plain refusal has none.
    """

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def performer_uuid(performer_id: str) -> str:
    return str(uuid.uuid5(_PERFORMER_NS, f"taskpaw-performer:{performer_id}"))


def _host(target_path: str) -> str:
    drive = PureWindowsPath(target_path).drive
    return drive.strip("\\").split("\\")[0] if drive.startswith("\\\\") else "local"


def _new_id() -> str:
    return str(uuid.uuid4())


class Finalizer:
    def __init__(
        self, runtime: LocalRuntime, inbox: Path | None, library: LibraryArchive
    ) -> None:
        self.runtime, self.inbox, self.library = runtime, inbox, library
        self.waiting: dict[str, str] = {}
        self.progress: dict[str, dict[str, Any]] = {}
        # set by the dispatcher: stop between films and between members on shutdown
        self.cancel = threading.Event()
        self.reclaim_dir = runtime.data_dir / "reclaim"

    # ── paths ───────────────────────────────────────────────────────────
    def _member_path(
        self, member: m.CleanupMember, final: m.FinalManifest, package: str | None
    ) -> Path:
        """Final items are film-dir relative; intake items are relative to their INBOX package."""
        rt = self.runtime
        if (
            member.root_id == rt.identity.work_root_id
        ):  # final items are film-dir relative
            return rt.final_dir(final) / member.relative_path
        if member.root_id == rt.identity.root_id and self.inbox is not None:
            base = self.inbox / package if package else self.inbox
            return base / member.relative_path
        raise ConfirmError(f"无法定位清理成员的根目录：{member.root_id}")

    def _package_dir(self, final: m.FinalManifest) -> Path:
        return self.runtime.final_dir(final)

    # ── 图文确认 preflight (V3 §7.3) ─────────────────────────────────────
    def preflight(
        self,
        film_id: str,
        performer_id: str | None = None,
        target_dir: str | None = None,
    ) -> dict[str, Any]:
        """Where 确认入库 would archive, read-only: the target folder under the
        chosen performer or manual directory and whether it is free, plus the library's
        candidates for the film's NFO actors (0 / 1 / many; the dialog pre-selects only one;
        missing performers are not listed).

        `error_code`: offline (the library or its share unreachable), permission
        (also a read-only library or an invalid name), missing (the performer
        record is gone), duplicate (the folder exists). It is mapped from the
        refusal's `code`, never from its status (§9.1).

        Without an explicit target, exactly one candidate is checked in the same
        call (U1 Codex r2 F4; `performer_id` then names it): the list reads one
        answer per film and still shows a blocked target.
        """
        from . import board

        if performer_id and target_dir:
            raise ConfirmError("请选择演员或手动入库目录，不能同时选择")
        rt = self.runtime
        film = rt.store.get_film(rt.operator, film_id).film
        if film.current_final_revision is None:
            raise ConfirmError("影片尚未完成整理（刮削）")
        final = rt.store.get_final_manifest(
            rt.operator, film_id, film.current_final_revision
        )
        film_dir = PurePosixPath(final.output_relative_path).name
        value: dict[str, Any] = {
            "film_dir": film_dir,
            "performer_id": performer_id,
            "target_dir": target_dir,
            "manual": bool(target_dir),
            "target_path": None,
            "absent": None,
            "error_code": None,
            "message": None,
            "candidates": None,
        }
        row = board.board_film(rt, film_id)
        actors = board.film_facts(rt, row, final=final).actors if row else ()
        try:
            items: dict[str, dict[str, Any]] = {}
            for actor in dict.fromkeys(actors):
                for item in self.library.performers(actor)["items"]:
                    items.setdefault(item["id"], item)
            value["candidates"] = {
                "query": list(dict.fromkeys(actors)),
                "items": list(items.values()),
            }
            if not performer_id and not target_dir and len(items) == 1:
                performer_id = value["performer_id"] = next(iter(items))
            if performer_id or target_dir:
                proof = self.library.target(
                    performer_id, film_dir, target_dir=target_dir
                )
                value["absent"] = bool(proof["absent"])
                value["target_path"] = proof["target_path"]
                if not value["absent"]:
                    value.update(
                        error_code="duplicate",
                        message=f"{'目标' if target_dir else '演员'}目录下已有同名目录：{film_dir}",
                    )
        except LibraryUnavailable as e:
            value.update(error_code="offline", message=f"片库不可用：{e}")
        except LibraryRefused as e:
            value.update(error_code=_UI_CODES.get(e.code, e.code), message=e.message)
        return value

    # ── 确认入库 ────────────────────────────────────────────────────────
    def confirm(
        self,
        film_id: str,
        performer_id: str | None = None,
        target_dir: str | None = None,
    ) -> str:
        """Approve archiving this film's final package under the chosen folder; the approval id."""
        if bool(performer_id) == bool(target_dir):
            raise ConfirmError("请选择一个演员或手动入库目录")
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
            proof = self.library.target(performer_id, film_dir, target_dir=target_dir)
        except LibraryUnavailable as e:
            raise ConfirmError(f"片库不可用：{e}") from e
        except LibraryRefused as e:
            raise ConfirmError(e.message) from e
        if not proof["absent"]:
            raise ConfirmError(
                f"{'目标' if target_dir else '演员'}目录下已有同名目录：{film_dir}"
            )
        target = self._target(
            performer_id, film_dir, proof["expected_target"], target_dir
        )
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
            if (
                status := file_identity.check(
                    self._member_path(member, final, wf.package_path), member
                )
            )
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
                "operator_choice",
                f"manual {target_dir}" if target_dir else f"performer {performer_id}",
                producer=signer,
            ),
            target=rt.evidence(
                "library_target", payload_digest=m.digest(proof), producer=signer
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
            raise ConfirmError(
                f"影片状态已变化，请刷新后重试：{exc}", code=exc.code
            ) from exc
        return approval_id

    def _target(
        self,
        performer_id: str | None,
        film_dir: str,
        expected: dict[str, Any],
        target_dir: str | None = None,
    ) -> m.TargetIdentity:
        root = (  # the manual root id is unchanged from the Filmpaw era (§9.7)
            str(uuid.uuid5(_PERFORMER_NS, f"filmpaw-manual-target:{target_dir}"))
            if target_dir
            else performer_uuid(str(performer_id))
        )
        return m.TargetIdentity(
            root_id=root,
            host_identity=_host(expected["target_path"]),
            export_identity=expected["target_path"],
            native_identity=m.NativeIdentity(
                scheme="manual_directory" if target_dir else "library_performer",
                root_id=root,
                volume_or_export_id=target_dir or str(performer_id),
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
            if self.cancel.is_set():
                break
            try:
                worked |= self._advance(film_id, waiting)
            except m.WorkflowError as exc:
                if str(exc) == "authority_unavailable":
                    raise
                if (
                    str(exc) == "cas_conflict"
                ):  # a lost race: retried next tick, quietly
                    log.info("finalize %s: cas_conflict, retrying", film_id)
                    if film_id in self.waiting:
                        waiting[film_id] = self.waiting[film_id]
                    continue
                log.warning("finalize %s: %s", film_id, exc)
                waiting[film_id] = f"归档/清理暂停：{exc}"
            except Exception as exc:  # one broken film must not stop the others (IR04)
                log.exception("finalize %s failed", film_id)
                waiting[film_id] = f"归档/清理出错：{exc}"
        self.waiting = waiting
        self._reclaim_pending()
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
        target = binding.target
        scheme, volume = (
            target.native_identity.scheme,
            target.native_identity.volume_or_export_id,
        )
        if scheme not in ("library_performer", "manual_directory"):
            self._fail(operation_id, CUTOVER)  # a Filmpaw-era binding (§9.8)
            return
        manual = scheme == "manual_directory"
        final = self.runtime.store.get_final_manifest(
            self.runtime.operator, film_id, binding.final_revision
        )
        try:
            self.library.archive(
                operation_id,
                self._package_dir(final),
                performer_id=None if manual else volume,
                target_dir=volume if manual else None,
                expected_target={
                    "target_path": target.export_identity,
                    "directory_identity": json.loads(target.native_identity.object_id),
                },
                files=[
                    {
                        "path": item.relative_path,
                        "size": item.size_bytes,
                        "sha256": item.sha256,
                    }
                    for item in final.items
                ],
                final_hash=final.manifest_hash,
            )
            waiting[film_id] = "归档中"
        except LibraryUnavailable:
            waiting[film_id] = LIBRARY_WAIT  # looked up by operation key next tick
        except LibraryRefused as e:  # permanent (§9.1): a new confirm is needed
            self._fail(operation_id, f"片库拒绝归档：{e.message}")

    def _poll(
        self,
        wf: m.FilmWorkflow,
        op: m.WorkflowArchiveOperation,
        waiting: dict[str, str],
    ) -> bool:
        approval = wf.approval
        if approval is None or approval.approval_id != op.approval_id:
            self.progress.pop(wf.film_id, None)
            waiting[wf.film_id] = "归档审批已变化"
            return False
        try:
            task = self.library.lookup(op.operation_id)
        except LibraryUnavailable:
            waiting[wf.film_id] = LIBRARY_WAIT
            return False
        if task is None:  # never accepted: the same operation key is safe to submit
            self.progress.pop(wf.film_id, None)
            self._submit(wf.film_id, op.operation_id, approval.binding, waiting)
            return True
        status = task.get("status")
        if status in ("queued", "running"):
            previous = self.progress.get(wf.film_id, {})
            self.progress[wf.film_id] = {
                "phase": "verifying"
                if task.get("phase") in ("verifying", "committing")
                else "copying",
                "copied_bytes": task.get("progress_bytes", 0),
                "verified_bytes": task.get("verified_bytes", 0),
                "total_bytes": task.get("total_bytes", 0),
                "files_done": task.get("completed_count", 0),
                "files_total": task.get("total_count", 0),
                "items": [
                    {
                        "name": item["name"],
                        "bytes": item["progress_bytes"],
                        "total": item["size_bytes"],
                    }
                    for item in task.get("items", [])
                ],
                "target": str(
                    Path(approval.binding.target.export_identity)
                    / approval.binding.target.relative_path
                ),
                "started_at": task.get("created_at")
                or previous.get("started_at")
                or self.runtime.clock.now().isoformat(),
            }
            waiting[wf.film_id] = "归档中"
            return False
        self.progress.pop(wf.film_id, None)
        if status == "completed" and task.get("receipt"):
            self._accept(wf, op, approval.binding, task)
            return True
        self._fail(
            op.operation_id,
            f"归档未完成（{status}）：{task.get('message') or ''}",
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
            self._fail(op.operation_id, "归档回执与已批准清单不一致", op.version)
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
                    "archive_failed", message[:2000], producer=rt.archive_auth
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
        if scope == "workcopy":
            self._retain(wf, final)
        results = []
        for member in manifest.members:
            if self.cancel.is_set():
                break  # record what was done; the rest is retried after restart
            if statuses.get(member.item_id, "pending") not in _RETRYABLE:
                continue  # done, or changed/replaced: kept for the operator (C7)
            status = file_identity.delete_exact(
                self._member_path(member, final, wf.package_path), member, self.cancel
            )
            if status == "interrupted":
                break  # shutdown mid-hash: nothing deleted, retried after restart
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
        # Reclaim is housekeeping after the recorded cleanup. The marker is written
        # first so a crash right after the record cannot lose it (H1-4); `_reclaim`
        # does nothing until the scope has succeeded, and the marker keeps it
        # retried (locked file, crash, waiting siblings) until it is complete.
        self.reclaim_dir.mkdir(parents=True, exist_ok=True)
        (self.reclaim_dir / wf.film_id).touch()
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
        if done != "succeeded":
            blocked = [
                r for r in results if r.status not in ("deleted", "already_absent")
            ]
            reason = blocked[0].status if blocked else "部分文件已变化"
            waiting[wf.film_id] = f"待清理：{len(blocked)} 个文件未能删除（{reason}）"
        return True

    def _retain(self, wf: m.FilmWorkflow, final: m.FinalManifest) -> None:
        """#302: small history assets, outside all C10 work dirs; best effort."""
        from . import board

        rt = self.runtime
        retained = rt.data_dir / "retained" / wf.film_id
        try:
            retained.mkdir(parents=True, exist_ok=True)
            poster = board._poster_item(final)
            thumb = next((i for i in final.items if i.role == "thumb"), None)
            for item in (poster, thumb):
                if item is None:
                    continue
                source = self._package_dir(final) / item.relative_path
                if (
                    not source.is_file()
                ):  # cleanup resumed after this member was deleted
                    continue
                target = retained / item.relative_path
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    tmp = target.with_suffix(target.suffix + ".tmp")
                    shutil.copyfile(source, tmp)
                    tmp.replace(target)
                except OSError as exc:
                    log.warning("retained image for %s failed: %s", wf.film_id, exc)
            nfo = board._item_path(rt, final, "nfo")
            if nfo is not None:
                title, actors, number = board._read_nfo(nfo)
                atomic_json(
                    retained / "summary.json",
                    {
                        "title": title,
                        "actors": list(actors),
                        "number": number,
                        "marks": list(final.postprocess.actual_marks),
                        "final_name": PurePosixPath(final.output_relative_path).name,
                        "archive_path": wf.archive_path,
                    },
                )
        except OSError as exc:
            log.warning("retained history for %s failed: %s", wf.film_id, exc)

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
            if file_identity.check(
                self._member_path(member, final, wf.package_path), member
            )
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
    def _reclaim_pending(self) -> None:
        if not self.reclaim_dir.is_dir():
            return
        for marker in sorted(self.reclaim_dir.iterdir()):
            if self.cancel.is_set():
                return
            try:
                if self._reclaim(marker.name):
                    marker.unlink()
            except Exception:
                log.exception("reclaim of %s failed; retried next tick", marker.name)

    def _reclaim(self, film_id: str) -> bool:
        """Remove what Taskpaw kept for a cleaned film; True once nothing is left to do."""
        rt = self.runtime
        wf = rt.store.film_workflow(rt.operator, film_id)
        done = True
        if wf.cleanup_workcopy_status == "succeeded":
            done &= self._reclaim_film(film_id)
            if wf.package_id and wf.snapshot_work_path:
                if self._package_done(wf.package_id, "workcopy"):
                    snapshot = rt.work_dir / wf.snapshot_work_path
                    remove_work_tree(
                        snapshot,
                        rt.work_dir,
                        operation_id=wf.intake_operation_id or film_id,
                    )
                    done &= not snapshot.exists()
                else:
                    done = False  # siblings still use the shared snapshot
        else:
            done = False
        if wf.cleanup_input_status != "succeeded":
            return False
        if wf.package_id is None:
            return done
        if not self._package_done(wf.package_id, "input"):
            return False
        self._retire(wf)
        return done and not rt.store.film_workflow(rt.operator, film_id).package_current

    def _retire(self, wf: m.FilmWorkflow) -> None:
        rt = self.runtime
        if self.inbox is not None and wf.package_path:
            package = self.inbox / wf.package_path
            if package.resolve() != self.inbox.resolve():  # never the INBOX root itself
                _rmdir_empty(package)
                _rmdir_parents(package, self.inbox)  # an actor folder left empty
        latest = rt.store.film_workflow(
            rt.operator, wf.film_id
        )  # rmdir may have bumped it
        if (
            wf.package_id is not None
            and latest.package_current
            and latest.package_version is not None
        ):
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

    def _reclaim_film(self, film_id: str) -> bool:
        """Taskpaw's own per-film dirs: compute attempts, return package, scrape
        attempts and the Miko lookup cache (IR07)."""
        film = self.runtime.store.get_film(self.runtime.operator, film_id).film
        if film.current_final_revision is not None:
            final = self.runtime.store.get_final_manifest(
                self.runtime.operator, film_id, film.current_final_revision
            )
            _rmdir_empty(self._package_dir(final))
        dirs = film_work_dirs(self.runtime, film_id)
        for path in dirs:
            shutil.rmtree(path, ignore_errors=True)  # a locked file: retried next tick
        return not any(path.exists() for path in dirs)


def film_work_dirs(rt: LocalRuntime, film_id: str) -> list[Path]:
    """The film's private dirs under the work dir: one per compute attempt, and
    its return package, scrape attempts and Miko lookup cache (IR07)."""
    view = rt.store.get_film(rt.operator, film_id)
    dirs = [
        rt.work_dir / attempt.attempt_id
        for stage in view.stages
        if stage.stage in ("restore", "asr", "translate")
        for attempt in rt.store.list_attempts(rt.operator, stage.job_id)
    ]
    for parent in ("return", "scrape", "lookup"):
        dirs.extend((rt.work_dir / parent).glob(f"{film_id}-r*"))
    return dirs


def _rmdir_parents(package: Path, inbox: Path) -> None:
    """Remove the now-empty folders above `package`, up to but never the INBOX
    root: an actor folder goes with its last film and stays while any other
    film (any entry) is left in it."""
    stop = inbox.resolve()
    parent = package.parent
    while parent.resolve() != stop and stop in parent.resolve().parents:
        try:
            parent.rmdir()  # fails while anything is left inside
        except OSError:
            return
        parent = parent.parent


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
