"""U1 P2': the pure 待处理 recommendation."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from taskpaw_v3.workflow import plan
from taskpaw_v3.workflow.plan import PastPlan, recommend, recommend_tier

T0 = datetime(2026, 10, 21, tzinfo=timezone.utc)


def past(film, tier, minutes=0, *actors, revision=1):
    return PastPlan(film, revision, tier, T0 + timedelta(minutes=minutes), actors)


def tier(*, actors=(), history=(), needs_gpu=True, film="f", default="standard"):
    return recommend_tier(
        film_id=film,
        needs_gpu=needs_gpu,
        actors=actors,
        history=list(history),
        default=default,
    )


def test_no_history_recommends_the_restore_default():
    assert tier() == ("standard", "默认")
    assert tier(default="good") == ("good", "默认")


def test_an_actors_most_recent_tier_wins():
    history = [past("a", "standard", 1, "三上悠亜"), past("b", "high", 2, "三上悠亜")]
    assert tier(actors=["三上悠亜"], history=history) == (
        "high",
        "三上悠亜 上次选择 高",
    )
    history.append(past("c", "good", 3, "三上悠亜"))
    assert tier(actors=["三上悠亜"], history=history) == (
        "good",
        "三上悠亜 上次选择 良好",
    )


def test_actor_names_match_after_nfkc_and_trim_only():
    history = [past("a", "high", 1, "ＡＢＣ ")]  # full-width letters, trailing space
    assert tier(actors=[" ABC"], history=history)[0] == "high"
    assert tier(actors=["AB C"], history=history)[0] == "standard"
    assert tier(actors=["abc"], history=history)[0] == "standard"  # exact, not casefold


def test_several_actors_take_the_highest_of_their_tiers():
    history = [
        past("a", "good", 1, "甲"),
        past("b", "high", 2, "乙"),
        past("c", "standard", 3, "丙"),
    ]
    assert tier(actors=["丙", "甲", "乙"], history=history) == (
        "high",
        "乙 上次选择 高",
    )


def test_the_films_previous_revision_comes_first():
    history = [
        past("f", "good", 1, revision=1),
        past("f", "standard", 5, revision=2),
        past("x", "high", 9, "甲"),
    ]
    assert tier(actors=["甲"], history=history) == ("standard", "上次选择 标准")


def test_plans_without_a_tier_say_nothing():
    history = [past("f", None, 1), past("a", None, 2, "甲")]
    assert tier(actors=["甲"], history=history) == ("standard", "默认")


def test_a_restored_input_needs_no_tier():
    history = [past("a", "high", 1, "甲")]
    assert tier(actors=["甲"], history=history, needs_gpu=False) == (None, "无需修复")


def test_machine_reason_names_the_card_or_the_uhd_cap():
    base = dict(
        film_id="f",
        needs_gpu=True,
        actors=(),
        history=(),
        default="standard",
        machine="w1",
        gpu_name="NVIDIA GeForce RTX 5060",
        vram_mib=8151,
    )
    value = recommend(**base, uhd=False)
    assert (value.machine, value.machine_reason) == (
        "w1",
        "NVIDIA GeForce RTX 5060 8 GB",
    )
    assert recommend(**base, uhd=True).machine_reason == "本机修复输出上限 1080p"
    assert recommend(**{**base, "gpu_name": None}, uhd=False).machine_reason == (
        plan.NO_GPU_REASON
    )
    assert value.ltx_model == "standard" and value.ltx_reason == "默认"


def test_the_recommendation_is_deterministic():
    history = [past("b", "good", 1, "甲"), past("a", "high", 1, "甲")]  # same time
    first = tier(actors=["甲"], history=history)
    assert all(
        tier(actors=["甲"], history=list(reversed(history))) == first for _ in range(3)
    )
