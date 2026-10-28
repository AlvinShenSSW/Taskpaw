"""#355: status.md shows media-center work under the machine that runs it."""

from __future__ import annotations

import json

from taskpaw_v3.hub.server.status_md import render_status_md

NOW = "2026-10-28 13:53:00"
OK = {"state": "ok", "age_seconds": 3, "last_good_at": "2026-10-28T04:52:57+00:00"}
LEGACY = {
    "LADA": {"type_id": "lada", "state": "stopped", "enabled": False},
    "Jasna": {"type_id": "jasna", "state": "stopped", "enabled": False},
    "Translate": {"type_id": "avsubs", "state": "stopped", "enabled": False},
    "Disk": {"type_id": "folder", "state": "stopped", "enabled": False},
}


def ltx(index, name, percent):
    return {
        "stage_index": index,
        "stage_total": 4,
        "stage_name": name,
        "percent": percent,
        "mode": "restore",
    }


def controller_311():
    """The shape a 3.11.1 controller actually served on 2026-10-28."""
    return {
        "counts": {"planning": 0, "active": 8, "archiving": 1, "done": 57},
        "current": {"film": "LOCAL-001", "stage": "asr"},
        "currents": [
            {
                "film": "LOCAL-001",
                "stage": "asr",
                "progress": "识别日语：Generating scene 365/377",
                "started_at": "2026-10-28T04:40:15+00:00",
                "machine": "本机",
            },
            {
                "film": "FILM-107",
                "stage": "restore",
                "progress": "修复 · 阶段 2/4 encode · 21% · 剩余 6:33:54",
                "started_at": "2026-10-28T04:00:51+00:00",
                "ltx": ltx(2, "encode", 21.0),
                "machine": "Blacksilverpig",
            },
            {
                "film": "FILM-027",
                "stage": "restore",
                "progress": "修复 · 阶段 4/4 compose · 7% · 剩余 1:32:49",
                "started_at": "2026-10-28T02:10:00+00:00",
                "ltx": ltx(4, "compose", 7.0),
                "machine": "Bushpig",
            },
        ],
        "completed_at": [],
    }


def worker(name, state="online", **changes):
    row = {
        "name": name,
        "state": state,
        "heartbeat_age_s": 2,
        "paused": False,
        "admission": "enabled",
        "gpu": {
            "name": "NVIDIA GeForce RTX 4090",
            "util": 98.0,
            "used_mib": 20070,
            "vram_mib": 24564,
            "power_w": 351.5,
            "hold": "workflow",
        },
        "current": None,
        "staged": 0,
        "transfers": [],
    }
    row.update(changes)
    return row


def server(name, data, *, reachable=True, health=OK):
    return {
        "name": name,
        "reachable": reachable,
        "status_json": json.dumps(data),
        "status_health": health,
    }


def fleet(workflow, *, controller_health=OK, controller_reachable=True, extra=()):
    statuses = [
        server(
            "BlackGoldPig",
            {
                "machine": "BlackGoldPig",
                "version": "3.11.1",
                "monitors": dict(LEGACY),
                "workflow": workflow,
            },
            reachable=controller_reachable,
            health=controller_health,
        ),
        server(
            "BlackSilverPig",
            {"machine": "BlackSilverPig", "version": "3.11.1", "monitors": LEGACY},
        ),
        server(
            "DarkbluePig",
            {"machine": "DarkbluePig", "version": "3.11.1", "monitors": LEGACY},
        ),
        *extra,
    ]
    return render_status_md(statuses, NOW)


def section(text, name):
    start = text.index(f"## {name}:")
    end = text.find("\n## ", start + 1)
    return text[start : end if end != -1 else len(text)]


def test_real_311_currents_are_shown_under_each_machine_with_eta():
    text = fleet(controller_311())
    silver = section(text, "BlackSilverPig")
    # case-insensitive match of the controller's worker name to the Hub server
    # a monitor-style line, so the unchanged V2 readers see the machine busy
    assert (
        "- 媒体处理中心: running | FILM-107 | 修复 · LTX 2/4 encode 21% · 剩余 6:33:54 · 已用 "
        in silver
    )
    gold = section(text, "BlackGoldPig")
    assert "  媒体主控: 调度状态未知 · 详见「媒体处理」" in gold
    assert (
        "- 媒体处理中心: running | LOCAL-001 | 识别 · 识别日语：Generating scene 365/377"
        in gold
    )
    # Bushpig is not a Hub server: its work still appears in 媒体处理
    media = text[text.index("## 媒体处理") :]
    assert "FILM-027 修复 · LTX 4/4 compose 7% · 剩余 1:32:49" in media
    assert "Bushpig" in media
    # a machine without media work gets no invented lines
    assert "媒体处理中心" not in section(text, "DarkbluePig")


def test_legacy_media_monitors_are_annotated_on_media_machines():
    text = fleet(controller_311())
    note = "disabled（旧版独立监控未使用；媒体任务见「媒体处理」）"
    for name in ("BlackGoldPig", "BlackSilverPig"):
        part = section(text, name)
        for monitor in ("LADA", "Jasna", "Translate"):
            assert f"- {monitor}: {note}" in part
        assert "- Disk: disabled\n" in part  # not a media monitor
    # DarkbluePig takes no part in media work here: unchanged legacy lines
    assert "- Jasna: disabled\n" in section(text, "DarkbluePig")


def test_workers_show_node_state_gpu_lost_and_idle():
    workflow = {
        **controller_311(),
        "workers": [
            worker("Blacksilverpig"),
            worker("Bushpig", paused=True),
            worker("darkbluepig", "lost", heartbeat_age_s=95),
        ],
        "workers_total": 3,
        "workers_more": 0,
    }
    text = fleet(workflow)
    silver = section(text, "BlackSilverPig")
    assert "  媒体节点: 在线 · GPU 98% · 显存 19.6/24.0GB · 352W" in silver
    dark = section(text, "DarkbluePig")
    assert "  媒体节点: 失联 · 心跳 95s 前" in dark
    assert "- 媒体处理中心: lost（失联 · 心跳 95s 前）" in dark
    assert "GPU 98%" not in dark  # a lost node's last GPU sample is not current
    assert "- Jasna: disabled（旧版独立监控未使用" in dark
    media = text[text.index("## 媒体处理") :]
    assert "  GPU 节点 3/3" in media
    assert "  - Bushpig: 在线 · 已暂停 · GPU 98%" in media
    assert "  - darkbluepig: 失联 · 心跳 95s 前" in media


def test_idle_online_worker_and_transfers():
    workflow = {
        **controller_311(),
        "currents": [],
        "workers": [
            worker(
                "BlackSilverPig",
                staged=2,
                transfers=[
                    {"film": "FILM-200", "dir": "down", "percent": 25, "rate": 5e7}
                ],
            )
        ],
        "workers_total": 1,
        "workers_more": 0,
    }
    silver = section(fleet(workflow), "BlackSilverPig")
    assert "已预取 2 部" in silver
    assert "下载 FILM-200 25%" in silver
    assert "- 媒体处理中心: idle（无运行任务）" in silver


def test_workers_preview_reports_omissions():
    rows = [worker(f"GPU-{index:02}") for index in range(16)]
    workflow = {
        **controller_311(),
        "workers": rows,
        "workers_total": 20,
        "workers_more": 4,
    }
    assert "GPU 节点 16/20 · 另有 4 台未展示" in fleet(workflow)


def test_stale_or_offline_controller_makes_no_live_machine_claims():
    for kwargs in (
        {"controller_health": {"state": "stale", "age_seconds": 400}},
        {"controller_reachable": False},
    ):
        text = fleet(
            {**controller_311(), "workers": [worker("BlackSilverPig")]}, **kwargs
        )
        silver = section(text, "BlackSilverPig")
        assert "媒体处理中心" not in silver and "媒体节点" not in silver
        assert "旧版独立监控未使用" not in silver


def test_worker_self_report_without_controller_data():
    own = server(
        "Bushpig",
        {
            "machine": "Bushpig",
            "version": "3.11.2",
            "monitors": LEGACY,
            "workflow_worker": {
                "state": "running",
                "reason": "",
                "connected": True,
                "routes": 1,
                "current": {"film_id": "x", "stage": "restore", "phase": "running"},
                "paused": False,
                "busy": True,
            },
        },
    )
    text = fleet(None, extra=[own])
    part = section(text, "Bushpig")
    assert "- 媒体处理中心: running（本机自报 · 已连接主控 · 修复 running）" in part
    assert "- Jasna: disabled（旧版独立监控未使用" in part


def test_machine_names_cannot_inject_lines():
    workflow = controller_311()
    workflow["currents"][1]["machine"] = "Blacksilverpig\n## Fake: ONLINE"
    workflow["currents"][1]["film"] = "X\n## Evil: ONLINE"
    text = fleet(workflow)
    assert "\n## Fake" not in text and "\n## Evil" not in text


def test_offline_server_keeps_controller_view_indented():
    text = render_status_md(
        [
            server(
                "BlackGoldPig",
                {
                    "machine": "BlackGoldPig",
                    "monitors": {},
                    "workflow": controller_311(),
                },
            ),
            server("BlackSilverPig", {"monitors": LEGACY}, reachable=False),
        ],
        NOW,
    )
    silver = section(text, "BlackSilverPig")
    assert silver.startswith("## BlackSilverPig: OFFLINE")
    assert "\n  媒体处理中心: running | FILM-107 | 修复" in silver
    assert "\n- " not in silver  # an OFFLINE section has no monitor lines


def test_ltx_remaining_time_under_an_hour_is_kept():
    # B1: Jasna prints `run 09:54 left`; exec_restore passes it on as `剩余 09:54`
    workflow = controller_311()
    workflow["currents"][1]["progress"] = "修复 · 阶段 2/4 encode · 36% · 剩余 09:54"
    silver = section(fleet(workflow), "BlackSilverPig")
    assert "LTX 2/4 encode 21% · 剩余 09:54 · 已用 " in silver


def test_idle_worker_self_report_is_not_running():
    # I-2: `running` is the worker service state, not a task
    idle = {
        "state": "running",
        "reason": "",
        "connected": True,
        "routes": 0,
        "current": None,
        "paused": False,
        "busy": False,
    }
    stopped = {**idle, "state": "stopped", "connected": False}
    text = fleet(
        None,
        extra=[
            server("Bushpig", {"monitors": {}, "workflow_worker": idle}),
            server("Wildpig", {"monitors": {}, "workflow_worker": stopped}),
        ],
    )
    assert "- 媒体处理中心: idle（本机自报 · 已连接主控 · 无运行任务）" in section(
        text, "Bushpig"
    )
    assert "- 媒体处理中心: stopped（本机自报 · 未连接主控 · 无运行任务）" in section(
        text, "Wildpig"
    )


def test_worker_omitted_from_bounded_currents_is_not_idle():
    # Codex: the currents preview is capped, the worker row still has `current`
    workflow = {
        **controller_311(),
        "currents": [],
        "currents_total": 17,
        "currents_more": 17,
        "workers": [
            worker(
                "BlackSilverPig",
                current={
                    "film": "FILM-300",
                    "stage": "restore",
                    "phase": "running",
                    "progress": "修复 · 阶段 3/4 denoise · 51% · 剩余 07:05",
                    "ltx": ltx(3, "denoise", 51.0),
                },
            )
        ],
        "workers_total": 1,
        "workers_more": 0,
    }
    silver = section(fleet(workflow), "BlackSilverPig")
    assert "idle" not in silver
    assert (
        "- 媒体处理中心: running | FILM-300 | 修复 · LTX 3/4 denoise 51% · 剩余 07:05"
        in silver
    )
