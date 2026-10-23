"""P2-4 (#295) R4/R12: the #286 observer and the Mac Hub in remote mode.

A film on a 失联 worker raises one `workflow.film.device_lost:<worker>` warn
(per film, revision and worker); the 待处理 notice reads 「· 自动」; the
summary names the machine of each running film (`currents`, at most four),
and the Hub's status.md prints it. The network app stays GET-only."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from taskpaw_v3.agent.server import workflow_events as events
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.hub.server.status_md import render_status_md
from taskpaw_v3.tests.test_workflow_board_inputs import (  # noqa: F401
    Host,
    online,
    world,
)
from taskpaw_v3.tests.test_workflow_local_dispatch import add_film
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.workflow_route_helpers import (
    claim_remote,
    enrol_remote,
    route,
)


@pytest.fixture
def sink(tmp_path):
    queue = EventQueue("windows")
    return events.WorkflowEvents(queue, tmp_path / "workflow-events.json"), queue


def kinds(queue):
    return [
        (e["data"]["kind"], e["level"], e["message"])
        for e in queue.recent()  # non-destructive: every event so far
    ]


def test_a_lost_device_notifies_once_per_film_revision_and_worker(world, sink):  # noqa: F811
    observer, queue = sink
    rt = world.rt
    w1, w2 = enrol_remote(rt, "5070 Ti"), enrol_remote(rt, "5060 Ti-1")
    film_id = add_film(rt)
    route(rt, film_id, w1.worker_id)
    claim_remote(rt, w1, film_id, "restore")
    online(world, w1)
    online(world, w2)
    host = Host(rt, world.fleet)
    observer.observe(host)
    assert kinds(queue) == []
    world.clock.advance(timedelta(seconds=61))
    observer.observe(host)
    observer.observe(host)
    found = kinds(queue)
    assert len(found) == 1
    kind, level, message = found[0]
    assert kind == f"workflow.film.device_lost:{w1.worker_id}"
    assert level == "warn" and "设备失联：5070 Ti · 后台重连中" in message
    online(world, w1)  # back, then lost again: the same film, revision, worker
    observer.observe(host)
    world.clock.advance(timedelta(seconds=61))
    observer.observe(host)
    assert len(kinds(queue)) == 1
    summary = observer.snapshot()
    assert summary["counts"]["attention"] == 1


def test_a_device_lost_row_raises_no_stage_attention_event(world, sink):  # noqa: F811
    observer, queue = sink
    rt = world.rt
    w1 = enrol_remote(rt, "5070 Ti")
    film_id = add_film(rt)
    route(rt, film_id, w1.worker_id)
    online(world, w1)
    world.clock.advance(timedelta(seconds=61))
    observer.observe(Host(rt, world.fleet))
    assert [k for k, _l, _m in kinds(queue)] == [
        f"workflow.film.device_lost:{w1.worker_id}"
    ]


def test_the_planning_notice_reads_auto_in_remote_mode(world, sink):  # noqa: F811
    observer, queue = sink
    add_film(world.rt, plan=False)
    observer.observe(Host(world.rt, world.fleet))
    (found,) = kinds(queue)
    assert found[0] == "workflow.film.needs_planning"
    assert found[2].endswith("· 自动")


def test_the_summary_names_each_running_films_machine(world, sink):  # noqa: F811
    observer, _queue = sink
    rt = world.rt
    remotes = [enrol_remote(rt, f"5060 Ti-{i}") for i in range(5)]
    films = [add_film(rt) for _ in range(5)]
    for remote, film_id in zip(remotes, films):
        current = {
            "film_id": film_id,
            "stage": "restore",
            "attempt_id": identity(),
            "phase": "running",
            "progress": "LTX 2/4 encode 52%",
            "elapsed_s": 60.0,
        }
        online(world, remote, current=current)
    local = add_film(rt)
    host = Host(rt, world.fleet)
    host.local = {"film_id": local, "film": "ABF-394", "stage": "translate"}
    status = host.status

    def with_local():
        value = status()
        value["dispatch"]["current"] = host.local
        return value

    host.status = with_local
    observer.observe(host)
    summary = observer.snapshot()
    currents = summary["currents"]
    assert len(currents) == 4  # bounded
    assert currents[0] == {
        "film": "ABF-394",
        "stage": "translate",
        "progress": None,
        "machine": "本机",
    }
    assert {c["machine"] for c in currents[1:]} <= {r.name for r in remotes}
    assert all(c["stage"] == "restore" for c in currents[1:])
    assert all(c["progress"] == "LTX 2/4 encode 52%" for c in currents[1:])
    assert summary["current"]["film"] == "ABF-394"  # older Hubs read this
    text = render_status_md(
        [
            {
                "name": "Windows",
                "reachable": True,
                "status_json": json.dumps({"workflow": summary}),
            }
        ],
        "2026-10-23 12:00:00",
    )
    assert "- ABF-394 翻译 · 本机" in text
    assert text.count("· 5060 Ti-") == 3


def test_status_md_falls_back_to_current_without_currents():
    workflow = {
        "counts": {},
        "current": {"film": "ABF-1", "stage": "restore", "progress": "50%"},
    }
    text = render_status_md(
        [
            {
                "name": "Windows",
                "reachable": True,
                "status_json": json.dumps({"workflow": workflow}),
            }
        ],
        "2026-10-23 12:00:00",
    )
    assert "- ABF-1 修复 · 50%" in text


def test_status_md_sanitizes_the_machine_name():
    workflow = {
        "counts": {},
        "currents": [
            {
                "film": "ABF-1",
                "stage": "asr",
                "progress": None,
                "machine": "evil\n## injected",
            }
        ],
    }
    text = render_status_md(
        [
            {
                "name": "Windows",
                "reachable": True,
                "status_json": json.dumps({"workflow": workflow}),
            }
        ],
        "2026-10-23 12:00:00",
    )
    assert "\n## injected" not in text
