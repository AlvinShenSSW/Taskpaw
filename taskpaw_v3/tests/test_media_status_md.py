"""#351: media-center summaries are independent of legacy Jasna monitors."""

from __future__ import annotations

import json

import pytest

from taskpaw_v3.hub.server.status_md import render_status_md

NOW = "2026-10-28 12:00:00"


def render(workflow=..., *, reachable=True, health=None, **source):
    data = {"machine": "blackgoldpig", "version": "3.11.1", "monitors": {}}
    data.update(source)
    if workflow is not ...:
        data["workflow"] = workflow
    return render_status_md(
        [
            {
                "name": "Controller",
                "reachable": reachable,
                "status_json": json.dumps(data),
                "status_health": health,
            }
        ],
        NOW,
    )


def summary(**changes):
    return {
        "counts": {"planning": 0, "active": 2},
        "current": None,
        "currents": [],
        "completed_at": [],
        "dispatch": {"state": "idle", "paused": True, "reason": "用户暂停派发"},
        "sampled_at": "2040-01-01T00:00:00Z",  # another clock, diagnostic only
        "sample_age_seconds": 2,
        "films": [
            {
                "film_id": "a",
                "name": "ABF-394",
                "group": "active",
                "stage": "restore",
                "status": "pending",
                "note": "等待 GPU",
                "machine": "5060 Ti",
                "route_position": 0,
                "route_live": "waiting",
            }
        ],
        "films_total": 1,
        "films_more": 0,
        "currents_total": 0,
        "currents_more": 0,
        **changes,
    }


def test_controller_queue_and_pause_without_legacy_monitor():
    text = render(summary(), health={"state": "ok", "age_seconds": 30})
    assert "来源 blackgoldpig · Agent 3.11.1" in text
    assert "排队/处理中 2" in text
    assert "调度 已暂停 · idle · 用户暂停派发" in text
    assert (
        "ABF-394 · restore · pending · 5060 Ti · 队列第 1 位 · waiting · 等待 GPU"
        in text
    )
    assert "源采样年龄 2s · Hub缓存年龄 30s · 数据年龄约 32s" in text
    assert "源采样时间 2040-01-01T00:00:00Z（仅诊断）" in text
    assert "队列预览 1/1" in text


@pytest.mark.parametrize(
    "workflow", [None, {}, [], "bad", {"counts": {}}, {"counts": "bad"}]
)
def test_missing_or_invalid_workflow_is_unavailable(workflow):
    text = render(workflow)
    assert "## 媒体处理" in text and "暂不可用" in text
    assert "待处理 0" not in text and "完成今日 0" not in text


def test_agent_without_workflow_keeps_legacy_monitor_only_output():
    assert "## 媒体处理" not in render()


@pytest.mark.parametrize(
    "workflow",
    [
        {"counts": {"active": 2}},
        {"current": {"film": "Legacy", "stage": "translate", "progress": "50%"}},
    ],
)
def test_legacy_summary_remains_useful_without_invented_new_fields(workflow):
    text = render(workflow)
    assert "暂不可用" not in text
    assert "源采样年龄未知" in text
    assert "调度状态未知" in text
    assert "队列预览未知" in text
    assert "队列预览 0/0" not in text
    if "current" in workflow:
        assert "Legacy 翻译 · 50%" in text
        assert "队列计数未知" in text
    else:
        assert "排队/处理中 2" in text


@pytest.mark.parametrize(
    "age", [None, "2", -1, True, float("nan"), float("inf"), 15.01]
)
def test_invalid_or_expired_source_age_cannot_render_activity(age):
    text = render(summary(sample_age_seconds=age))
    assert "暂不可用" in text
    assert "ABF-394" not in text and "排队/处理中" not in text


@pytest.mark.parametrize(
    "reachable, health",
    [
        (False, {"state": "error", "error_code": "http_auth"}),
        (True, {"state": "stale", "age_seconds": 999}),
        (True, {"state": "error", "error_code": "network_unreachable"}),
    ],
)
def test_unhealthy_hub_snapshot_does_not_make_live_media_claims(reachable, health):
    text = render(summary(), reachable=reachable, health=health)
    assert "ABF-394" not in text and "排队/处理中" not in text
    assert "离线" in text or "暂不可用" in text


def test_bounded_queue_and_current_previews_show_omissions():
    films = [
        {"name": f"queue-{i}", "stage": "restore", "status": "pending"}
        for i in range(40)
    ]
    currents = [
        {
            "film": f"current-{i}",
            "stage": "return",
            "progress": "回传 1/4 GB",
            "machine": f"worker-{i}",
        }
        for i in range(20)
    ]
    text = render(
        summary(
            films=films,
            films_total=45,
            films_more=5,
            currents=currents,
            currents_total=21,
            currents_more=1,
        )
    )
    assert "队列预览 32/45 · 另有 13 部未展示" in text
    assert "当前任务 16/21 · 另有 5 台未展示" in text
    assert "queue-31 ·" in text and "queue-32 ·" not in text
    assert "current-15 回传 · 回传 1/4 GB · worker-15" in text
    assert "current-16 回传" not in text


def test_unknown_or_malformed_preview_fields_do_not_invent_zeros():
    text = render(
        summary(
            films=None,
            films_total="n/a",
            films_more=-1,
            currents_total=True,
            currents_more=float("nan"),
        )
    )
    assert "队列预览未知" in text
    assert "队列预览 0/0" not in text
    assert "当前任务 0/0" not in text


def test_preview_text_is_bounded_sanitized_and_queue_position_is_validated():
    poison = "evil\n## injected\t" + "x" * 2000
    text = render(
        summary(
            films=[
                {
                    "name": poison,
                    "stage": poison,
                    "status": poison,
                    "note": poison,
                    "machine": poison,
                    "route_position": True,
                    "route_live": poison,
                    "attention_kind": poison,
                },
                None,
            ],
            films_total=2,
            dispatch={"state": poison, "paused": False, "reason": poison},
        ),
        machine=poison,
        version=poison,
    )
    assert "\n## injected" not in text and "\t" not in text
    assert "队列第" not in text
    assert "x" * 401 not in text
    assert "True" not in text and "None" not in text
    assert "队列预览 1/2 · 另有 1 部未展示" in text


def test_unknown_machine_and_position_stay_unknown():
    text = render(
        summary(
            films=[
                {
                    "name": "unassigned",
                    "stage": "restore",
                    "status": "pending",
                    "machine": None,
                    "route_position": -1,
                }
            ]
        )
    )
    assert "unassigned · restore · pending" in text
    assert "队列第" not in text and "· 本机" not in text
