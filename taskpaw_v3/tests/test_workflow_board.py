"""T06-P1 媒体入库 board: one T01 read, grouping, notes and the NFO summary (D02–D04, D10, N01)."""

# ruff: noqa: F811

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from taskpaw_v3.tests import test_workflow_return_step as ret
from taskpaw_v3.tests import test_workflow_scrape_step as scr
from taskpaw_v3.tests.test_workflow_finalize import (  # noqa: F401
    World,
    probeable_video,
    world,
)
from taskpaw_v3.tests.test_workflow_image_size import jpeg
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import board
from taskpaw_v3.workflow import intake_models as im
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.exec_common import MISSING_LTX
from taskpaw_v3.workflow.local_host import retry_failed_stage
from taskpaw_v3.workflow.manifest import parse_nfo_summary

ORDER = board.STAGE_ORDER


def film(
    statuses: dict[str, str] | None = None,
    *,
    notes: dict[str, str] | None = None,
    reasons: dict[str, str] | None = None,
    final_name: str | None = None,
    approval_active: bool = False,
    archive_open: bool = False,
    archive_error: str | None = None,
    film_id: str | None = None,
) -> m.BoardFilm:
    """A synthetic board row: every stage pending unless named."""
    statuses = statuses or {}
    notes = notes or {}
    reasons = reasons or {}
    return m.BoardFilm(
        film_id=film_id or identity(),
        display_name="原始包名",
        created_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
        final_name=final_name,
        stages=tuple(
            m.BoardStage(
                stage=stage,
                status=statuses.get(stage, "pending"),
                note=notes.get(stage),
                reason=reasons.get(stage),
            )
            for stage in ORDER
        ),
        approval_active=approval_active,
        archive_open=archive_open,
        archive_error=archive_error,
    )


def done_through(stage: str) -> dict[str, str]:
    return {s: "succeeded" for s in ORDER[: ORDER.index(stage) + 1]}


# ── grouping (D04, N01) ──────────────────────────────────────────────────
def test_a_film_in_progress_is_active_at_its_first_unfinished_stage():
    row = board.film_row(film({**done_through("restore"), "asr": "running"}), {})
    assert (row["group"], row["stage"], row["status"]) == ("active", "asr", "running")
    assert row["can_confirm"] is False and row["can_retry"] is False
    assert row["retry_stage"] is None


def test_skipped_stages_count_as_finished():
    statuses = {**done_through("intake"), "restore": "skipped"}
    assert board.film_row(film(statuses), {})["stage"] == "asr"


def test_a_scraped_film_waits_for_confirmation():
    row = board.film_row(film(done_through("scrape"), final_name="ABC-123"), {})
    assert (row["group"], row["stage"], row["status"]) == (
        "confirm",
        "archive",
        "pending",
    )
    assert row["can_confirm"] is True and row["name"] == "ABC-123"


def test_a_failed_archive_can_be_confirmed_again_with_its_reason():
    row = board.film_row(
        film(
            {**done_through("scrape"), "archive": "failed"},
            archive_error="片库拒绝归档：空间不足",
        ),
        {},
    )
    assert row["group"] == "confirm" and row["can_confirm"] is True
    assert row["note"] == "片库拒绝归档：空间不足"
    assert row["can_retry"] is False


@pytest.mark.parametrize(
    "value",
    [
        {"approval_active": True},
        {"archive_open": True},
    ],
)
def test_a_confirmed_film_is_archiving_not_confirmable(value):
    row = board.film_row(film(done_through("scrape"), **value), {})
    assert row["group"] == "archiving" and row["can_confirm"] is False


@pytest.mark.parametrize(
    "approved,reason,expected",
    [
        (True, "等待确认入库", None),
        (False, "等待确认入库", "等待确认入库"),
        (True, "等待片库可用", "等待片库可用"),
        (True, "归档/清理暂停：存储不可用", "归档/清理暂停：存储不可用"),
    ],
)
def test_pending_archive_ignores_only_stale_confirmation_wait(
    approved, reason, expected
):
    value = film(done_through("scrape"), approval_active=approved)
    waiting = {value.film_id: reason, "another-film": "等待确认入库"}
    row = board.film_row(value, waiting)
    assert row["note"] == expected
    if approved:
        assert row["group"] == "archiving" and row["can_confirm"] is False
        steps = _steps(row)
        assert steps["confirm"][0] == "done"
        assert steps["archive"] == ("waiting", expected, None)
    assert waiting == {value.film_id: reason, "another-film": "等待确认入库"}


def test_confirming_again_after_a_failed_archive_drops_the_old_reason():
    value = film(
        {**done_through("scrape"), "archive": "failed"},
        approval_active=True,
        archive_error="归档未完成（failed）：旧原因",
    )
    row = board.film_row(value, {value.film_id: "归档中"})
    assert (row["group"], row["note"]) == ("archiving", "归档中")


def test_an_archived_film_is_archiving_until_both_cleanups_succeed():
    statuses = {**done_through("archive"), "cleanup_workcopy": "succeeded"}
    row = board.film_row(film(statuses), {})
    assert (row["group"], row["stage"]) == ("archiving", "cleanup_input")
    assert board.film_row(film(done_through("cleanup_workcopy")), {})["group"] == (
        "done"
    )


@pytest.mark.parametrize("status", ["failed", "blocked"])
@pytest.mark.parametrize("stage", ["restore", "asr", "translate", "return", "scrape"])
def test_failed_local_stages_need_attention_and_can_be_retried(stage, status):
    statuses = {**done_through(ORDER[ORDER.index(stage) - 1]), stage: status}
    row = board.film_row(film(statuses, notes={stage: "工具崩溃"}), {})
    assert row["group"] == "attention"
    assert (row["can_retry"], row["retry_stage"]) == (True, stage)
    assert (row["stage"], row["status"], row["note"]) == (stage, status, "工具崩溃")


def test_an_unknown_stage_needs_attention_but_is_not_retryable():
    statuses = {**done_through("intake"), "restore": "unknown"}
    row = board.film_row(film(statuses, notes={"restore": "旧说明"}), {})
    assert row["group"] == "attention"
    assert (row["can_retry"], row["retry_stage"]) == (False, None)
    assert row["note"] == board.UNKNOWN_NOTE == "等待确认停止"


@pytest.mark.parametrize("stage", ["cleanup_input", "cleanup_workcopy"])
def test_a_cleanup_that_kept_members_says_why_never_the_stop_message(stage):
    statuses = {**done_through("archive"), stage: "unknown"}
    value = film(statuses)
    reason = "待清理：2 个文件未能删除（blocked_changed）"
    row = board.film_row(value, {value.film_id: reason})
    assert (row["group"], row["stage"], row["status"]) == (
        "attention",
        stage,
        "unknown",
    )
    assert row["note"] == reason
    # after a restart the host has no reason yet: the kept members explain it
    kept = {stage: [{"path": "a.mp4", "status": "blocked_identity"}]}
    assert board.film_row(value, {}, kept)["note"] == (
        "待清理：1 个文件未能删除（blocked_identity）"
    )
    assert board.film_row(value, {})["note"] == board.KEPT_NOTE
    assert board.KEPT_NOTE != board.UNKNOWN_NOTE


def test_only_local_stages_wait_for_a_stop_confirmation():
    value = film({**done_through("scrape"), "archive": "unknown"})
    row = board.film_row(value, {value.film_id: "片库不可用：超时"})
    assert (row["stage"], row["note"]) == ("archive", "片库不可用：超时")
    intake = film({"intake": "unknown"})
    assert (
        board.film_row(intake, {intake.film_id: "入库扫描中"})["note"] == "入库扫描中"
    )


def test_a_failed_intake_is_not_retryable_here():
    row = board.film_row(film({"intake": "failed"}), {})
    assert row["group"] == "attention" and row["can_retry"] is False


# ── notes (D02) and names (D12) ───────────────────────────────────────────
def test_waiting_reason_is_the_note_unless_the_stage_failed():
    value = film({**done_through("restore"), "asr": "pending"}, notes={"asr": "旧"})
    assert board.film_row(value, {value.film_id: "等待 GPU：手动任务"})["note"] == (
        "等待 GPU：手动任务"
    )
    assert board.film_row(value, {})["note"] is None
    failed = film({"intake": "succeeded", "restore": "failed"})
    assert board.film_row(failed, {failed.film_id: "排队"})["note"] == "排队"


def test_name_is_the_final_directory_else_the_input_never_the_grouping_key():
    """H3 D06: final folder → single-film package folder → NFO number → video stem."""
    one = film().model_copy(
        update={"package_path": "Downloads/(HD)ABF-393 河合あすな", "package_films": 1}
    )
    assert board.film_name(one, "ABF-393", "4K688.com@ABF-393") == (
        "(HD)ABF-393 河合あすな"
    )
    two = one.model_copy(update={"package_films": 2})
    assert board.film_name(two, "ABF-393", "4K688.com@ABF-393") == "ABF-393"
    assert board.film_name(two, None, "4K688.com@ABF-393") == "4K688.com@ABF-393"
    final = two.model_copy(update={"final_name": "ABF-393"})
    assert board.film_name(final, "ABF-394", "x") == "ABF-393"
    assert board.film_row(film(final_name="ABC-123"), {})["name"] == "ABC-123"
    assert board.film_row(two, {}, label=("ABF-393", "x"))["name"] == "ABF-393"
    assert board.film_row(one, {})["name"] == "(HD)ABF-393 河合あすな"
    rows = board.film_rows((two,), {}, labels={two.film_id: (None, "abf-393")})
    assert rows[0]["name"] == "abf-393"
    # only a film without an input manifest has nothing better than its key
    assert board.film_row(film(), {})["name"] == "原始包名"


def test_rows_carry_the_frozen_shape():
    row = board.film_row(film(), {})
    assert set(row) == {
        "film_id",
        "name",
        "group",
        "stage",
        "status",
        "note",
        "can_confirm",
        "can_retry",
        "retry_stage",
        "created_at",
        "completed_at",
        "steps",
        # U1 V' / V3 §7
        "number",
        "title",
        "actors",
        "resolution",
        "plan",
        "receipt_id",
        "revisions",
        # the cover of the queue and review cards (UI contract)
        "has_poster",
        "poster_source",
        # U1b: the full jacket the 大图 shows
        "has_cover",
        "cover_source",
        # U1e: stage times, the restore breakdown and 修复→翻译
        "timing",
        # PWR-2: the probe duration, the energy and the GPU owner's live power
        "duration_ms",
        "energy",
        "live_power",
        # P2-4 R2/R3: remote mode's route, pin, actions and 设备失联
        "attention_kind",
        "route",
        "pin",
        "actions",
        # #298 R1 (§9 CX27): the 失联 worker {id, name}
        "lost_worker",
        # P3 E1/E3 (#304): 片源丢失 {paths, found_at}; 删除此任务 offered
        "source_missing",
        "can_remove",
    }
    assert (row["route"], row["pin"], row["actions"]) == (None, None, None)
    assert row["lost_worker"] is None
    assert (row["has_poster"], row["poster_source"]) == (False, None)
    assert (row["has_cover"], row["cover_source"]) == (False, None)
    assert row["created_at"] == "2026-10-20T00:00:00Z"
    assert [s["key"] for s in row["steps"]] == list(board.STEP_KEYS)
    assert all(set(s) == {"key", "state", "note", "progress"} for s in row["steps"])


# ── lifecycle step line (H2 R5, G3–G5) ────────────────────────────────────
def _steps(row: dict) -> dict[str, tuple]:
    return {s["key"]: (s["state"], s["note"], s["progress"]) for s in row["steps"]}


STEP_STATES = {"done", "running", "waiting", "failed", "pending"}


def test_a_fresh_film_is_past_intake_and_nothing_else_has_started():
    steps = _steps(board.film_row(film({"intake": "succeeded"}), {}))
    assert steps["intake"] == ("done", None, None)
    assert steps["plan"] == ("waiting", "待安排", None)  # U1: 待处理 first
    assert {
        k: v[0] for k, v in steps.items() if k not in ("intake", "plan")
    } == dict.fromkeys(board.STEP_KEYS[2:], "pending")


def test_the_running_stage_shows_the_host_progress_for_that_film_only():
    value = film({**done_through("intake"), "restore": "running"})
    current = {"film_id": value.film_id, "film": "x", "stage": "restore"}
    steps = _steps(
        board.film_row(value, {}, current={**current, "progress": "42% 1/3"})
    )
    assert steps["restore"] == ("running", None, "42% 1/3")
    assert steps["asr"][0] == "pending"
    other = {**current, "film_id": identity(), "progress": "9%"}
    assert _steps(board.film_row(value, {}, current=other))["restore"] == (
        "running",
        None,
        None,
    )


def _step_of(row: dict, key: str) -> dict:
    return next(s for s in row["steps"] if s["key"] == key)


def test_the_running_restore_step_always_carries_its_start_and_console():
    """U1c: the heartbeat — the stage start and the latest console line —
    is on the restore step even before any progress line was recognised."""
    value = film({**done_through("intake"), "restore": "running"})
    current = {
        "film_id": value.film_id,
        "film": "x",
        "stage": "restore",
        "progress": "修复",
        "started_at": "2026-10-22T08:00:00+00:00",
        "ltx": {
            "current_file": "a.mp4",
            "console": "loading models",
            "params": {"ltx_model": "high", "argv": "jasna.exe --input a.mp4"},
        },
    }
    restore = _step_of(board.film_row(value, {}, current=current), "restore")
    assert restore["started_at"] == "2026-10-22T08:00:00+00:00"
    assert restore["console"] == "loading models"
    assert restore["params"] == current["ltx"]["params"]
    # IR07: kept on the step only, not repeated inside `ltx`
    assert restore["ltx"] == {"current_file": "a.mp4"}
    assert "console" in current["ltx"] and "params" in current["ltx"]  # input intact
    bare = {k: v for k, v in current.items() if k != "ltx"}
    restore = _step_of(board.film_row(value, {}, current=bare), "restore")
    assert (restore["started_at"], restore["console"], restore["params"]) == (
        "2026-10-22T08:00:00+00:00",
        None,
        None,
    )
    other = {**current, "film_id": identity()}
    restore = _step_of(board.film_row(value, {}, current=other), "restore")
    assert not ({"started_at", "console", "params"} & restore.keys())


def test_the_running_restore_step_carries_the_ltx_detail_and_its_volume():
    """H3 M3: the step keeps the text and gains the LtxProgress metrics."""
    value = film({**done_through("intake"), "restore": "running"})
    ltx = {
        "ltx_progress": {
            "mode": "restore",
            "stage_index": 2,
            "stage_total": 4,
            "stage_name": "encode",
        },
        "ltx_stage": "2/4 encode",
        "current_file": "第 1/2 卷",
    }
    current = {
        "film_id": value.film_id,
        "film": "x",
        "stage": "restore",
        "progress": "修复 第 1/2 卷 · 阶段 2/4 encode",
        "ltx": ltx,
    }
    restore = _step_of(board.film_row(value, {}, current=current), "restore")
    assert restore["progress"] == "修复 第 1/2 卷 · 阶段 2/4 encode"
    assert restore["ltx"] == ltx and restore["current_file"] == "第 1/2 卷"
    other = {**current, "film_id": identity()}
    assert "ltx" not in _step_of(board.film_row(value, {}, current=other), "restore")
    plain = {k: v for k, v in current.items() if k != "ltx"}
    assert "ltx" not in _step_of(board.film_row(value, {}, current=plain), "restore")


def test_a_missing_model_failure_offers_the_download_on_the_restore_step():
    """H3 M1: the step stays failed with its reason and carries the session."""
    note = MISSING_LTX + "vae.safetensors.enc、vae-decoder.safetensors.enc"
    value = film(
        {**done_through("intake"), "restore": "failed"}, notes={"restore": note}
    )
    download = {
        "state": "downloading",
        "request_id": "r1",
        "files": ["vae.safetensors.enc", "vae-decoder.safetensors.enc"],
        "total_bytes": 10,
        "downloaded_bytes": 4,
        "destination": "D:/Jasna/model_weights/ltx-restore",
        "variant": "ltx standard fast",
        "error": None,
    }
    row = board.film_row(value, {}, download=download)
    restore = _step_of(row, "restore")
    assert (restore["state"], restore["note"]) == ("failed", note)
    assert restore["download"] == download
    assert (row["can_retry"], row["retry_stage"]) == (True, "restore")
    assert "download" not in _step_of(board.film_row(value, {}), "restore")
    crashed = film(
        {**done_through("intake"), "restore": "failed"}, notes={"restore": "工具崩溃"}
    )
    assert "download" not in _step_of(
        board.film_row(crashed, {}, download=download), "restore"
    )


def test_a_waiting_reason_belongs_to_the_first_unstarted_step():
    value = film(done_through("intake"))
    steps = _steps(board.film_row(value, {value.film_id: "等待 GPU：手动任务"}))
    assert steps["restore"] == ("waiting", "等待 GPU：手动任务", None)
    assert (steps["asr"][0], steps["translate"][0]) == ("pending", "pending")


def test_no_work_and_skipped_stages_are_done_with_their_reason():
    """G3/G5: there is no 'skipped' step state; the reason says why."""
    value = film(
        {**done_through("restore"), "asr": "skipped", "translate": "succeeded"},
        notes={"translate": "无需翻译：已有中文字幕"},
        reasons={"asr": "Existing validated output"},
    )
    steps = _steps(board.film_row(value, {}))
    assert steps["asr"] == ("done", "Existing validated output", None)
    assert steps["translate"] == ("done", "无需翻译：已有中文字幕", None)


def test_failed_blocked_and_unknown_stages():
    value = film(
        {**done_through("asr"), "translate": "failed", "return": "blocked"},
        notes={"translate": "工具崩溃"},
        reasons={"return": "policy_blocked"},
    )
    steps = _steps(board.film_row(value, {}))
    assert steps["translate"] == ("failed", "工具崩溃", None)
    assert steps["return"] == ("failed", "policy_blocked", None)  # G4
    unknown = film({**done_through("intake"), "restore": "unknown"})
    assert _steps(board.film_row(unknown, {}))["restore"] == (
        "waiting",
        board.UNKNOWN_NOTE,
        None,
    )


def test_a_scraped_film_waits_for_the_operator():
    steps = _steps(board.film_row(film(done_through("scrape")), {}))
    assert steps["scrape"][0] == "done"
    assert steps["confirm"] == ("waiting", board.CONFIRM_NOTE, None)
    assert board.CONFIRM_NOTE == "等你确认入库"
    assert (steps["archive"][0], steps["cleanup"][0], steps["done"][0]) == (
        "pending",
        "pending",
        "pending",
    )


def test_a_failed_archive_waits_for_a_new_confirm_with_its_error():
    value = film(
        {**done_through("scrape"), "archive": "failed"}, archive_error="NAS 空间不足"
    )
    steps = _steps(board.film_row(value, {}))
    assert steps["confirm"] == ("waiting", "NAS 空间不足", None)
    assert steps["archive"] == ("failed", "NAS 空间不足", None)


def test_a_confirmed_film_is_archiving():
    queued = film(done_through("scrape"), approval_active=True)
    steps = _steps(board.film_row(queued, {queued.film_id: "归档中"}))
    assert steps["confirm"][0] == "done"
    assert steps["archive"] == ("waiting", "归档中", None)
    started = film({**done_through("scrape"), "archive": "running"}, archive_open=True)
    steps = _steps(board.film_row(started, {started.film_id: "等待片库可用"}))
    assert steps["confirm"][0] == "done"
    assert steps["archive"] == ("running", "等待片库可用", None)


def test_cleanup_combines_both_scopes_and_says_what_was_kept():
    archived = film(done_through("archive"))
    steps = _steps(board.film_row(archived, {}))
    assert (steps["archive"][0], steps["cleanup"][0], steps["done"][0]) == (
        "done",
        "running",
        "pending",
    )
    kept = film({**done_through("archive"), "cleanup_input": "unknown"})
    files = {"cleanup_input": [{"path": "a.mp4", "status": "blocked_identity"}]}
    assert _steps(board.film_row(kept, {}, files))["cleanup"] == (
        "waiting",
        "待清理：1 个文件未能删除（blocked_identity）",
        None,
    )
    blocked = film(
        {**done_through("archive"), "cleanup_workcopy": "blocked"},
        notes={"cleanup_workcopy": "工作区被占用"},
    )
    assert _steps(board.film_row(blocked, {}))["cleanup"][:2] == (
        "failed",
        "工作区被占用",
    )
    done = _steps(board.film_row(film(done_through("cleanup_workcopy")), {}))
    assert (done["cleanup"][0], done["done"][0]) == ("done", "done")
    assert {v[0] for v in done.values()} == {"done"}


@pytest.mark.parametrize(
    "statuses",
    [
        {},
        done_through("intake"),
        {**done_through("restore"), "asr": "running"},
        {**done_through("scrape"), "archive": "unknown"},
        {**done_through("archive"), "cleanup_input": "failed"},
        done_through("cleanup_workcopy"),
    ],
)
def test_every_step_state_is_one_of_the_frozen_five(statuses):
    row = board.film_row(film(statuses), {})
    assert {s["state"] for s in row["steps"]} <= STEP_STATES


# ── package rows (H2 R2/R2a) ──────────────────────────────────────────────
def package(state: str, reason: str, **kw) -> im.IntakeBoardPackage:
    return im.IntakeBoardPackage(
        package_id=kw.pop("package_id", identity()),
        relative_path=kw.pop("relative_path", "Downloads/ABF-393"),
        state=state,
        reason=reason,
        **kw,
    )


@pytest.mark.parametrize(
    ("state", "reason", "group", "step"),
    [
        ("receiving", "temporary_member", "active", "waiting"),
        ("receiving", "writer_busy", "active", "waiting"),
        ("waiting_stability", "observed", "active", "waiting"),
        ("snapshotting", "stable", "active", "running"),
        ("waiting_capacity", "waiting_capacity", "active", "waiting"),
        ("blocked", "changed", "active", "waiting"),
        ("needs_attention", "ambiguous_grouping", "attention", "failed"),
        ("blocked", "publication_blocked", "attention", "failed"),
        ("blocked", "profile_required", "attention", "failed"),
        ("unknown_owner", "recovery_required", "attention", "failed"),
    ],
)
def test_a_package_row_is_grouped_by_its_intake_state(state, reason, group, step):
    row = board.package_row(package(state, reason), None)
    assert (row["group"], row["state"], row["reason"]) == (group, state, reason)
    steps = _steps(row)
    assert steps["intake"] == (step, reason, None)
    assert {v[0] for k, v in steps.items() if k != "intake"} == {"pending"}


def test_a_stuck_package_names_its_reasons_and_files():
    value = package(
        "needs_attention",
        "ambiguous_grouping",
        grouping_reasons=("unknown_attachment",),
        unassigned_paths=("setup.exe",),
    )
    row = board.package_row(value, None)
    assert row == {
        "package_id": value.package_id,
        "name": "Downloads/ABF-393",
        "state": "needs_attention",
        "reason": "ambiguous_grouping",
        "grouping_reasons": ["unknown_attachment"],
        "files": ["setup.exe"],
        "group": "attention",
        "steps": row["steps"],
    }


def test_the_package_being_copied_shows_the_intake_progress():
    value = package("snapshotting", "stable")
    copying = {
        "package_id": value.package_id,
        "phase": "copy",
        "bytes_completed": 5,
        "total": 10,
    }
    assert _steps(board.package_row(value, copying))["intake"] == (
        "running",
        "stable",
        copying,
    )
    other = {**copying, "package_id": identity()}
    assert _steps(board.package_row(value, other))["intake"][2] is None


# ── NFO summary (D10) ────────────────────────────────────────────────────
def test_nfo_summary_reads_title_actor_names_and_number():
    payload = (
        "﻿<movie><title> 标题 </title><actor><name>甲</name></actor>"
        "<actor><name>乙</name><role>x</role></actor><actor><name>甲</name></actor>"
        "<actor/><num> ABF-393 </num></movie>"
    ).encode()
    assert parse_nfo_summary(payload) == ("标题", ("甲", "乙"), "ABF-393")
    assert parse_nfo_summary(b"<movie><title>t</title></movie>") == ("t", (), None)


@pytest.mark.parametrize(
    "payload",
    [
        b'<!DOCTYPE movie [<!ENTITY x SYSTEM "file:///private">]><movie>&x;</movie>',
        b"<movie>",
        b"\xff\xfe",
        b"",
        b"<other><title>x</title></other>",
        b"<movie><title>" + b"x" * 300000 + b"</title></movie>",
        "not bytes",
    ],
    ids=["entity", "unclosed", "not-utf8", "empty", "not-movie", "oversized", "str"],
)
def test_bad_nfo_gives_an_empty_summary_never_an_error(payload):
    assert parse_nfo_summary(payload) == (None, (), None)  # type: ignore[arg-type]


# ── the T01 read against a real store (D03) ──────────────────────────────
def _board(world: World, **kw) -> dict[str, m.BoardFilm]:
    return {f.film_id: f for f in world.rt.store.film_board(world.rt.operator, **kw)}


def test_film_board_follows_a_film_from_intake_to_done(world):
    before = _board(world)[world.film]
    assert before.final_name is None and before.display_name == "Film"
    assert board.film_row(before, {})["group"] == "active"
    world.scrape()
    scraped = _board(world)[world.film]
    assert scraped.final_name == "ABC-123"
    assert {s.stage: s.status for s in scraped.stages}["scrape"] == "succeeded"
    assert board.film_row(scraped, {})["group"] == "confirm"
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
        confirmed = _board(world)[world.film]
        assert confirmed.approval_active and not confirmed.archive_open
        assert board.film_row(confirmed, {})["group"] == "archiving"
        world.ticks(fin)
    done = _board(world)[world.film]
    assert board.film_row(done, {})["group"] == "done"
    assert not done.approval_active and done.archive_error is None
    assert world.film not in _board(world, done_limit=0)  # D13: only done films
    assert world.film in _board(world, done_limit=1)


def test_film_board_reports_an_open_and_a_failed_archive(world):
    world.scrape()
    with world.fake:
        fin = world.finalizer()
        fin.confirm(world.film, "actor")
    fin.run_once()  # admitted, the library closed
    assert _board(world)[world.film].archive_open
    with world.fake:
        world.fake.mode = "fail"
        world.ticks(fin, 2)
    failed = _board(world)[world.film]
    assert not failed.archive_open and not failed.approval_active
    assert "NAS 可用空间不足" in (failed.archive_error or "")
    row = board.film_row(failed, {})
    assert (row["group"], row["status"], row["can_confirm"]) == (
        "confirm",
        "failed",
        True,
    )
    assert "NAS 可用空间不足" in row["note"]


def test_film_board_carries_the_last_attempt_note_and_retry_requeues(world):
    ret.run_all(scr.dispatcher(world.rt, scr.scrape_step(world.tmp, "extra_file")))
    failed = _board(world)[world.film]
    scrape = next(s for s in failed.stages if s.stage == "scrape")
    assert scrape.status == "failed" and scrape.note
    row = board.film_row(failed, {})
    assert (row["group"], row["retry_stage"], row["note"]) == (
        "attention",
        "scrape",
        scrape.note,
    )
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        retry_failed_stage(world.rt, world.film, "restore")  # succeeded, not failed
    with pytest.raises(m.WorkflowError, match="precondition_failed"):
        retry_failed_stage(world.rt, world.film, "archive")  # not a local stage
    retry_failed_stage(world.rt, world.film, "scrape")
    assert world.status("scrape") == "pending"


def test_film_board_is_operator_only(world):
    _, intake = world.rt.grant_intake_adapter()
    with pytest.raises(m.WorkflowError, match="forbidden"):
        world.rt.store.film_board(intake)


# ── many films in one read (IR01) ─────────────────────────────────────────
class Seed:
    """Raw rows for board reads only: a film per state, no T01 commands."""

    def __init__(self, rt):
        self.conn = rt.store._conn
        self.clock = datetime(2026, 10, 20, tzinfo=timezone.utc)
        self.ids: dict[str, str] = {}

    def __enter__(self) -> Seed:
        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute("BEGIN")
        return self

    def __exit__(self, *exc) -> None:
        self.conn.execute("COMMIT")
        self.conn.execute("PRAGMA foreign_keys=ON")

    def _now(self) -> str:
        self.clock += timedelta(seconds=1)
        return self.clock.isoformat().replace("+00:00", "Z")

    def film(
        self,
        name: str,
        statuses: dict[str, str] | None = None,
        *,
        notes: dict[str, str] | None = None,
        reasons: dict[str, str] | None = None,
        context: int = 1,
        context_state: str = "ready",
        revision: int = 1,
        final: bool = True,
    ) -> str:
        fid = self.ids[name] = identity()
        self.conn.execute(
            "INSERT INTO films(film_id,intake_session_id,display_name,policy_revision,"
            "current_input_revision,current_final_revision,current_context_revision,"
            "context_state,created_at,body_json) VALUES(?,?,?,1,?,?,?,?,?,'{}')",
            (
                fid,
                "s",
                name,
                revision,
                1 if final else None,
                context,
                context_state,
                self._now(),
            ),
        )
        if final:
            body = json.dumps({"output_relative_path": f"out/{name.upper()}-1"})
            self.conn.execute(
                "INSERT INTO manifests VALUES(?,'final',1,?,1,'h',NULL,'t',?)",
                (fid, "0" * 64, body),
            )
        for rev in range(1, revision + 1):
            for stage in ORDER if statuses is not None else ():
                old = rev < revision
                status = "failed" if old else statuses.get(stage, "pending")  # type: ignore[union-attr]
                note = "旧版本" if old else (notes or {}).get(stage)
                attempt = identity() if note else None
                job = identity()
                reason = None if old else (reasons or {}).get(stage)
                self.conn.execute(
                    "INSERT INTO stages VALUES(?,?,?,'h',?,?,?,1,?)",
                    (
                        job,
                        fid,
                        rev,
                        stage,
                        status,
                        attempt,
                        json.dumps({"reason": reason} if reason else {}),
                    ),
                )
                if attempt:
                    evidence = {"evidence": [{"summary": {"note": note}}]}
                    self.conn.execute(
                        "INSERT INTO attempts VALUES(?,?,1,'p',NULL,'b',1,1,'h',"
                        "'failed',NULL,'t',NULL,1,?)",
                        (attempt, job, json.dumps(evidence)),
                    )
        return fid

    def package(self, path: str, *names: str, revision: int = 1) -> None:
        """An INBOX package whose input revision `revision` made the named films."""
        package = identity()
        self.conn.execute(
            "INSERT INTO intake_packages(package_id,root_id,root_config_revision,"
            "relative_path,path_key,source_generation,version,state,reason,dirty,"
            "observation_digest,observed_at,observation_json) "
            "VALUES(?,'r',1,?,?,1,1,'operational_ready','ready',0,?,?,'{}')",
            (package, path, path.casefold(), "0" * 64, self._now()),
        )
        for name in names:
            self.conn.execute(
                "INSERT INTO intake_films(film_id,input_revision,package_id,"
                "snapshot_id,source_generation,grouping_revision,created_at) "
                "VALUES(?,?,?,?,1,1,?)",
                (self.ids[name], revision, package, identity(), self._now()),
            )

    def approval(self, name: str, *, context: int = 1, revoked: bool = False) -> str:
        approval = identity()
        self.conn.execute(
            "INSERT INTO approvals VALUES(?,?,?,'h','b','p',1,'t','t',?,1,'{}')",
            (approval, self.ids[name], context, "t" if revoked else None),
        )
        return approval

    def archive(
        self, name: str, approval: str, *, open: bool = False, error: str | None = None
    ) -> None:
        now = self._now()
        self.conn.execute(
            "INSERT INTO operations(operation_id,film_id,kind,approval_id,"
            "owner_principal_id,status,created_at,updated_at,resolved_at,body_json) "
            "VALUES(?,?,'archive',?,'p',?,?,?,?,?)",
            (
                identity(),
                self.ids[name],
                approval,
                "running" if open else "failed" if error else "resolved",
                now,
                now,
                None if open else now,
                json.dumps({"error_code": error}),
            ),
        )


@pytest.fixture
def seeded(tmp_path):
    rt = ret.make_runtime(tmp_path / "seed")
    try:
        yield rt, Seed(rt)
    finally:
        rt.close()


def _summary(value: m.BoardFilm) -> tuple:
    return (
        value.display_name,
        value.final_name,
        value.approval_active,
        value.archive_open,
        value.archive_error,
    )


def test_film_board_reads_every_film_state_at_once(seeded):
    rt, seed = seeded
    scraped = done_through("scrape")
    done = done_through("cleanup_workcopy")
    with seed:
        seed.film(
            "fresh",
            {"intake": "succeeded", "restore": "running", "asr": "skipped"},
            notes={"restore": "50%"},
            reasons={"asr": "Existing validated output"},
        )
        seed.film("waiting", scraped)
        seed.approval("waiting")  # confirmed, not used by any archive yet
        seed.film("failed", {**scraped, "archive": "failed"})
        seed.archive("failed", seed.approval("failed"), error="NAS 可用空间不足")
        seed.film("again", {**scraped, "archive": "failed"})
        seed.archive("again", seed.approval("again"), error="片库拒绝")
        seed.approval("again")  # confirmed again after the failure
        seed.film("open", {**scraped, "archive": "pending"})
        seed.archive("open", seed.approval("open"), error="旧原因")
        seed.archive("open", seed.approval("open"), open=True)
        seed.film("revoked", scraped)
        seed.approval("revoked", revoked=True)
        seed.film("stale", scraped, context=2)
        seed.approval("stale", context=1)  # an older approval context
        seed.film("bare", None, final=False)
        # Codex T06 r1: an intake recheck invalidated the context before archiving
        seed.film("invalid", scraped, context_state="invalidated")
        seed.approval("invalid")
        seed.film("rev2", {"intake": "succeeded"}, revision=2)
        for name in ("done1", "done2", "done3"):
            seed.film(name, done)
            seed.archive(name, seed.approval(name))
    every = rt.store.film_board(rt.operator, None)
    assert [f.display_name for f in every] == list(seed.ids)  # oldest first
    got = {f.display_name: f for f in every}
    assert {name: _summary(value)[1:] for name, value in got.items()} == {
        "fresh": ("FRESH-1", False, False, None),
        "waiting": ("WAITING-1", True, False, None),
        "failed": ("FAILED-1", False, False, "NAS 可用空间不足"),
        "again": ("AGAIN-1", True, False, "片库拒绝"),
        "open": ("OPEN-1", False, True, None),
        "revoked": ("REVOKED-1", False, False, None),
        "stale": ("STALE-1", False, False, None),
        "bare": (None, False, False, None),
        "invalid": ("INVALID-1", False, False, None),
        "rev2": ("REV2-1", False, False, None),
        "done1": ("DONE1-1", False, False, None),
        "done2": ("DONE2-1", False, False, None),
        "done3": ("DONE3-1", False, False, None),
    }
    assert [(s.stage, s.status, s.note, s.reason) for s in got["fresh"].stages][:4] == [
        ("intake", "succeeded", None, None),
        ("restore", "running", "50%", None),
        ("asr", "skipped", None, "Existing validated output"),  # H2 G4
        ("translate", "pending", None, None),
    ]
    assert got["bare"].stages == ()
    assert [s.stage for s in got["rev2"].stages] == list(ORDER)
    assert {s.status for s in got["rev2"].stages} == {"succeeded", "pending"}
    groups = {name: board.film_row(value, {})["group"] for name, value in got.items()}
    assert (groups["failed"], groups["again"], groups["waiting"]) == (
        "confirm",
        "archiving",
        "archiving",
    )
    assert board.film_row(got["failed"], {})["note"] == "NAS 可用空间不足"
    assert (groups["open"], groups["revoked"], groups["stale"]) == (
        "archiving",
        "confirm",
        "confirm",
    )
    assert groups["invalid"] == "confirm"  # confirm again, never a dead 'archiving'
    # D13: only the newest finished films are kept; unfinished ones always are
    names = [f.display_name for f in rt.store.film_board(rt.operator, done_limit=1)]
    assert names == list(seed.ids)[:-3] + ["done3"]
    assert len(rt.store.film_board(rt.operator)) == len(seed.ids)
    one = rt.store.film_board(rt.operator, done_limit=0, film_id=seed.ids["done1"])
    assert [_summary(f) for f in one] == [_summary(got["done1"])]
    assert rt.store.film_board(rt.operator, film_id=identity()) == ()


def _board_steps(rt, films: int) -> int:
    seed = Seed(rt)
    with seed:
        for n in range(films):
            name = f"film{n}"
            seed.film(name, done_through("cleanup_workcopy"), notes={"scrape": "x"})
            seed.archive(name, seed.approval(name))
    steps = 0

    def count() -> int:
        nonlocal steps
        steps += 1
        return 0

    rt.store._conn.set_progress_handler(count, 100)
    try:
        assert len(rt.store.film_board(rt.operator, None)) >= films
    finally:
        rt.store._conn.set_progress_handler(None, 100)
    return steps


def test_film_board_work_grows_linearly_with_the_films(tmp_path):
    """IR01: no per-row subqueries over operations/approvals while holding the lock."""
    small, large = (ret.make_runtime(tmp_path / name) for name in ("small", "large"))
    try:
        ratio = _board_steps(large, 240) / _board_steps(small, 120)
    finally:
        small.close()
        large.close()
    assert ratio < 2.6, ratio


def test_film_board_names_the_package_and_counts_its_films(seeded):
    """H3 D06: the package folder names a film only when it holds that one film."""
    rt, seed = seeded
    with seed:
        for name in ("alone", "pair_a", "pair_b", "loose"):
            seed.film(name, {"intake": "succeeded"}, final=False)
        seed.film("moved", {"intake": "succeeded"}, revision=2, final=False)
        seed.package("Downloads/(HD)ABF-393 河合あすな", "alone")
        seed.package("Downloads/two", "pair_a", "pair_b")
        seed.package("Downloads/old", "moved", revision=1)  # not its current input
    got = {f.display_name: f for f in rt.store.film_board(rt.operator)}

    def link(name: str) -> tuple:
        return got[name].package_path, got[name].package_films

    assert link("alone") == ("Downloads/(HD)ABF-393 河合あすな", 1)
    assert link("pair_a") == link("pair_b") == ("Downloads/two", 2)
    assert link("loose") == link("moved") == (None, 0)
    assert got["moved"].input_revision == 2
    (one,) = rt.store.film_board(rt.operator, film_id=seed.ids["pair_b"])
    assert (one.package_path, one.package_films) == ("Downloads/two", 2)
    assert board.film_row(got["alone"], {})["name"] == "(HD)ABF-393 河合あすな"


# ── metadata before Miko (H3 M2, D11) ─────────────────────────────────────
NFO = (
    "<movie><title>预刮削标题</title><actor><name>河合あすな</name></actor>"
    "<num>ABF-393</num></movie>"
).encode()
VIDEO = "4K688.com@ABF-393.mp4"


@pytest.fixture
def staged(tmp_path):
    from taskpaw_v3.tests.workflow_exec_helpers import Tools, runtime

    rt = runtime(tmp_path / "wf", Tools(tmp_path / "tools"))
    try:
        yield rt
    finally:
        rt.close()


def _input_film(rt, *files: tuple[str, str]) -> str:
    from taskpaw_v3.tests.workflow_exec_helpers import File, add_film, video

    extra = [
        File(path, NFO if role == "nfo" else path.encode(), role=role)
        for path, role in files
    ]
    return add_film(rt, [File(VIDEO, video()), *extra], attachments=(".jpg", ".txt"))


@pytest.mark.parametrize(
    ("images", "served"),
    [
        (
            [("p.jpg", "poster"), ("t.jpg", "thumb"), ("ABF-393.jpg", "attachment")],
            "p.jpg",
        ),
        ([("t.jpg", "thumb"), ("ABF-393.jpg", "attachment")], "t.jpg"),
        ([("广告.txt", "attachment"), ("ABF-393.jpg", "attachment")], "ABF-393.jpg"),
    ],
    ids=["poster", "thumb", "attachment"],
)
def test_before_miko_the_detail_reads_the_input_nfo_and_cover(staged, images, served):
    rt = staged
    value = _input_film(rt, ("ABF-393.nfo", "nfo"), *images)
    detail = board.film_detail(rt, value, {})
    assert detail is not None
    assert (detail["title"], detail["actors"], detail["number"]) == (
        "预刮削标题",
        ["河合あすな"],
        "ABF-393",
    )
    assert detail["name"] == "ABF-393"  # no single-film package: the NFO number
    assert detail["has_poster"] is True and detail["poster_source"].startswith("input")
    poster = board.poster_path(rt, value)
    assert poster is not None and poster.read_bytes() == served.encode()
    assert poster.is_relative_to(rt.work_dir)  # the intake work copy, never the INBOX


def test_an_unscraped_input_is_named_by_its_video_and_has_no_cover(staged):
    rt = staged
    value = _input_film(rt, ("广告.txt", "attachment"))
    detail = board.film_detail(rt, value, {})
    assert detail is not None
    assert detail["name"] == "4K688.com@ABF-393"  # original case, never the key
    assert (detail["title"], detail["actors"], detail["number"]) == (None, [], None)
    assert detail["has_poster"] is False and detail["poster_source"] is None
    assert board.poster_path(rt, value) is None
    films = rt.store.film_board(rt.operator)
    (row,) = board.film_rows(films, {}, labels=board.input_labels(rt, films))
    assert row["name"] == "4K688.com@ABF-393"
    assert board.name_of(rt, value) == "4K688.com@ABF-393"


# ── the full jacket for the 大图 (U1b) ────────────────────────────────────
def _image_film(rt, *files: tuple[str, str, bytes]) -> str:
    from taskpaw_v3.tests.workflow_exec_helpers import File, add_film, video

    extra = [File(path, data, role=role) for path, role, data in files]
    return add_film(rt, [File(VIDEO, video()), *extra], attachments=(".jpg", ".txt"))


JACKET = jpeg(2184, 1477)  # the downloaded fanart: the whole jacket
SMALL = jpeg(840, 568)  # its thumb
PORTRAIT = jpeg(421, 600)  # a front cover alone


@pytest.mark.parametrize(
    ("images", "served"),
    [
        (
            [
                ("ABF-393-poster.jpg", "poster", jpeg(2184, 1478)),
                ("ABF-393-thumb.jpg", "thumb", SMALL),
                ("ABF-393-fanart.jpg", "fanart", JACKET),
                ("ABF-393.jpg", "attachment", jpeg(3000, 2000)),
            ],
            "ABF-393-fanart.jpg",
        ),
        (  # the role decides before the size
            [("t.jpg", "thumb", SMALL), ("ABF-393.jpg", "attachment", JACKET)],
            "t.jpg",
        ),
        (  # among attachments the largest landscape one
            [
                ("a.jpg", "attachment", SMALL),
                ("b.jpg", "attachment", JACKET),
                ("c.jpg", "attachment", jpeg(1400, 2000)),
            ],
            "b.jpg",
        ),
        (  # a portrait fanart is no jacket
            [("f.jpg", "fanart", PORTRAIT), ("a.jpg", "attachment", SMALL)],
            "a.jpg",
        ),
    ],
    ids=["fanart", "thumb-before-attachment", "largest-attachment", "portrait-fanart"],
)
def test_the_cover_is_the_landscape_jacket_by_precedence(staged, images, served):
    rt = staged
    value = _image_film(rt, *images)
    data = {path: blob for path, _, blob in images}
    detail = board.film_detail(rt, value, {})
    assert detail is not None
    assert detail["has_cover"] is True
    assert detail["cover_source"].startswith("input-")
    path = board.cover_path(rt, value)
    assert path is not None and path.read_bytes() == data[served]
    assert path.is_relative_to(rt.work_dir)  # the intake work copy, never the INBOX
    films = rt.store.film_board(rt.operator)
    (row,) = board.film_rows(films, {}, facts=board.row_facts(rt, films))
    assert (row["has_cover"], row["cover_source"]) == (True, detail["cover_source"])
    assert detail["has_poster"] is True  # the cards keep showing the poster


def test_without_a_landscape_image_there_is_no_cover(staged):
    rt = staged
    value = _image_film(
        rt,
        ("p.jpg", "poster", PORTRAIT),
        ("t.jpg", "thumb", b"image t.jpg"),  # unreadable: no size
        ("广告.txt", "attachment", b"text"),
    )
    detail = board.film_detail(rt, value, {})
    assert detail is not None and detail["has_poster"] is True
    assert (detail["has_cover"], detail["cover_source"]) == (False, None)
    assert board.cover_path(rt, value) is None
    assert board.cover_path(rt, identity()) is None


def _final_with(poster_item_id, *roles_and_paths):
    from types import SimpleNamespace

    items = [
        SimpleNamespace(item_id=f"i{n}", role=role, relative_path=path)
        for n, (role, path) in enumerate(roles_and_paths)
    ]
    postprocess = SimpleNamespace(poster_item_id=poster_item_id)
    return SimpleNamespace(items=items, postprocess=postprocess)


def test_the_final_cover_is_the_recorded_first_volume_poster():
    """#303: `<name>-poster.jpg` per volume; the recorded one (cd1's) is the cover."""
    final = _final_with(
        "i2",  # the postprocess recorded cd1's poster, not the first listed
        ("poster", "ABC-123-cd2-poster.jpg"),
        ("folder_cover", "folder.jpg"),
        ("poster", "ABC-123-cd1-poster.jpg"),
    )
    assert board._poster_item(final).relative_path == "ABC-123-cd1-poster.jpg"


@pytest.mark.parametrize(
    ("items", "served"),
    [
        ([("poster", "poster.jpg")], "poster.jpg"),  # a final from before #303
        ([("poster", "ABC-123-poster.jpg")], "ABC-123-poster.jpg"),
        ([("folder_cover", "folder.jpg")], "folder.jpg"),
        ([], None),
    ],
)
def test_the_final_cover_accepts_every_poster_name(items, served):
    item = board._poster_item(_final_with("missing", *items))
    assert (item.relative_path if item else None) == served


@pytest.mark.parametrize(
    "progress",
    [
        {
            "phase": 5,
            "phase_n": 8,
            "phase_label": "识别语音",
            "scene": 214,
            "scenes": 346,
            "percent": 62,
            "eta_s": 660,
            "elapsed_s": 1122,
            "console": "Generating scene 214/346",
            "phases": [
                {"n": 5, "label": "识别语音", "state": "current", "seconds": 900}
            ],
        },
        {"elapsed_s": 1122, "console": "Scene 1/1"},
    ],
)
def test_running_asr_passes_progress_start_and_console_only_to_its_film(progress):
    value = film({**done_through("restore"), "asr": "running"})
    current = {
        "film_id": value.film_id,
        "stage": "asr",
        "progress": "识别语音",
        "started_at": "2026-10-24T08:00:00+00:00",
        "ltx": progress,
    }
    step = _step_of(board.film_row(value, {}, current=current), "asr")
    assert step["progress"] == progress
    assert step["started_at"] == current["started_at"]
    assert step["console"] == progress["console"]
    assert "ltx" not in step
    other = _step_of(
        board.film_row(value, {}, current={**current, "film_id": identity()}), "asr"
    )
    assert other["progress"] is None
    assert "started_at" not in other and "console" not in other
