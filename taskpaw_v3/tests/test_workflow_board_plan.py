"""U1 V': the 待处理 group, the plan step and the §7 row fields of the board."""

from __future__ import annotations

from datetime import datetime, timezone

from taskpaw_v3.tests.test_workflow_board import done_through, film
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.workflow import board
from taskpaw_v3.workflow import models as m


def keys(row):
    return [step["key"] for step in row["steps"]]


def test_the_step_line_has_the_plan_step_after_intake():
    assert board.STEP_KEYS[:3] == ("intake", "plan", "restore")
    row = board.film_row(film(), {})
    assert keys(row) == list(board.STEP_KEYS)


def test_an_unplanned_film_is_in_the_planning_group():
    row = board.film_row(film(done_through("intake")), {})
    assert (row["group"], row["stage"], row["status"]) == (
        "planning",
        "restore",
        "pending",
    )
    assert row["plan"] is None
    plan = row["steps"][1]
    assert (plan["key"], plan["state"], plan["note"]) == ("plan", "waiting", "待安排")


def test_a_planned_film_is_active_with_its_plan():
    machine = identity()
    value = film(done_through("intake")).model_copy(
        update={"planned": True, "plan_ltx_model": "high", "plan_machine": machine}
    )
    row = board.film_row(value, {})
    assert row["group"] == "active"
    assert row["plan"] == {"ltx_model": "high", "machine": machine}
    plan = row["steps"][1]
    assert (plan["state"], plan["note"]) == ("done", "高")


def test_a_plan_without_a_tier_reads_no_restore():
    value = film(done_through("intake")).model_copy(
        update={"planned": True, "plan_ltx_model": None, "plan_machine": identity()}
    )
    assert board.film_row(value, {})["steps"][1]["note"] == "无需修复"


def test_a_film_past_restore_never_returns_to_planning():
    row = board.film_row(film(done_through("restore")), {})
    assert row["group"] == "active" and row["steps"][1]["state"] == "done"


def test_trouble_still_wins_over_planning():
    row = board.film_row(film({"intake": "failed"}), {})
    assert row["group"] == "attention"


def test_rows_carry_resolution_receipt_and_revisions():
    value = film(done_through("scrape"), final_name="ABC-123").model_copy(
        update={
            "planned": True,
            "plan_ltx_model": "standard",
            "plan_machine": identity(),
            "input_revision": 2,
            "input_hash": "a" * 64,
            "final_revision": 1,
            "final_hash": "b" * 64,
            "receipt_id": "r-1",
            "max_width": 3840,
            "max_height": 2160,
        }
    )
    row = board.film_row(value, {})
    assert row["resolution"] == "4K"
    assert row["receipt_id"] == "r-1"
    assert row["revisions"] == {
        "input": {"revision": 2, "hash": "a" * 64},
        "final": {"revision": 1, "hash": "b" * 64},
    }
    hd = value.model_copy(update={"max_width": 1920, "max_height": 1080})
    assert board.film_row(hd, {})["resolution"] == "1080p"
    none = value.model_copy(update={"max_width": 0, "max_height": 0})
    assert board.film_row(none, {})["resolution"] is None


def test_counts_cover_every_group_and_the_packages_needing_attention():
    rows = [
        board.film_row(film(done_through("intake")), {}),
        board.film_row(film(done_through("intake")), {}),
        board.film_row(film(done_through("scrape"), final_name="X"), {}),
    ]
    packages = [{"group": "attention"}, {"group": "active"}]
    counts = board.counts(rows, packages)
    assert counts == {
        "planning": 2,
        "active": 0,
        "attention": 0,
        "confirm": 1,
        "archiving": 0,
        "done": 0,
        "packages_attention": 1,
        "waiting_for_you": 3,
    }


def test_facts_prefer_final_then_lookup_then_input():
    class Found:
        ok = True
        number, title, actors = "ABF-394", "查到的标题", ("演员甲",)

    facts = board.Facts.merge(
        final=(None, (), None),
        lookup=Found(),
        input_nfo=("输入标题", ("输入演员",), "INPUT-1"),
        video="movie",
    )
    assert (facts.title, facts.actors, facts.number) == (
        "查到的标题",
        ("演员甲",),
        "ABF-394",
    )
    final = board.Facts.merge(
        final=("最终标题", ("最终演员",), "FINAL-1"),
        lookup=Found(),
        input_nfo=(None, (), None),
        video="movie",
    )
    assert (final.title, final.actors, final.number) == (
        "最终标题",
        ("最终演员",),
        "FINAL-1",
    )
    bare = board.Facts.merge(
        final=(None, (), None), lookup=None, input_nfo=("T", ("A",), "N"), video="v"
    )
    assert (bare.title, bare.actors, bare.number, bare.label) == (
        "T",
        ("A",),
        "N",
        ("N", "v"),
    )


def test_the_lookup_number_names_a_film_before_the_package_folder():
    value = film(done_through("intake")).model_copy(
        update={"package_path": "[site]ABF-394 raw", "package_films": 1}
    )
    assert board.film_name(value, "N", "v") == "[site]ABF-394 raw"
    assert board.film_name(value, "N", "v", looked_up="ABF-394") == "ABF-394"
    named = value.model_copy(update={"final_name": "ABF-394 final"})
    assert board.film_name(named, looked_up="ABF-394") == "ABF-394 final"


def test_board_film_defaults_are_unplanned():
    value = m.BoardFilm(
        film_id=identity(),
        display_name="x",
        created_at=datetime(2026, 10, 21, tzinfo=timezone.utc),
        final_name=None,
        stages=(),
        approval_active=False,
        archive_open=False,
        archive_error=None,
    )
    assert value.planned is False and value.plan_ltx_model is None
