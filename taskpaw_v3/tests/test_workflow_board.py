"""T06-P1 媒体入库 board: one T01 read, grouping, notes and the NFO summary (D02–D04, D10, N01)."""

# ruff: noqa: F811

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from taskpaw_v3.tests import test_workflow_return_step as ret
from taskpaw_v3.tests import test_workflow_scrape_step as scr
from taskpaw_v3.tests.test_workflow_finalize import (  # noqa: F401
    World,
    probeable_video,
    world,
)
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import board
from taskpaw_v3.workflow import models as m
from taskpaw_v3.workflow.local_host import retry_failed_stage
from taskpaw_v3.workflow.manifest import parse_nfo_summary

ORDER = board.STAGE_ORDER


def film(
    statuses: dict[str, str] | None = None,
    *,
    notes: dict[str, str] | None = None,
    final_name: str | None = None,
    approval_active: bool = False,
    archive_open: bool = False,
    archive_error: str | None = None,
    film_id: str | None = None,
) -> m.BoardFilm:
    """A synthetic board row: every stage pending unless named."""
    statuses = statuses or {}
    notes = notes or {}
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
            archive_error="Filmpaw 拒绝归档：空间不足",
        ),
        {},
    )
    assert row["group"] == "confirm" and row["can_confirm"] is True
    assert row["note"] == "Filmpaw 拒绝归档：空间不足"
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


def test_confirming_again_after_a_failed_archive_drops_the_old_reason():
    value = film(
        {**done_through("scrape"), "archive": "failed"},
        approval_active=True,
        archive_error="Filmpaw failed：旧原因",
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


def test_name_is_the_final_directory_else_the_display_name():
    assert board.film_row(film(), {})["name"] == "原始包名"
    assert board.film_row(film(final_name="ABC-123"), {})["name"] == "ABC-123"


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
    }
    assert row["created_at"] == "2026-10-20T00:00:00Z"


# ── NFO summary (D10) ────────────────────────────────────────────────────
def test_nfo_summary_reads_title_and_actor_names():
    payload = (
        "﻿<movie><title> 标题 </title><actor><name>甲</name></actor>"
        "<actor><name>乙</name><role>x</role></actor><actor><name>甲</name></actor>"
        "<actor/></movie>"
    ).encode()
    assert parse_nfo_summary(payload) == ("标题", ("甲", "乙"))


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
    assert parse_nfo_summary(payload) == (None, ())  # type: ignore[arg-type]


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
    fin.run_once()  # admitted, Filmpaw down
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
