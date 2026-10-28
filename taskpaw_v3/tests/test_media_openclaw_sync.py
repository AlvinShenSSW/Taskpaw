"""Media observer -> authenticated Agent -> real Hub transport -> read-only DB.

All stores, sockets and event queues belong to this fixture, never a LAN service.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import uvicorn

from taskpaw_v3.agent.server.app import create_network_app
from taskpaw_v3.agent.server.workflow_events import WorkflowEvents
from taskpaw_v3.hub.server.poller import Poller
from taskpaw_v3.hub.server.status_md import write_status_md
from taskpaw_v3.hub.server.store import HubStore
from taskpaw_v3.tests.test_event_cursor import trusted_queue
from taskpaw_v3.tests.test_workflow_board_inputs import (  # noqa: F401
    Host,
    online,
    world,
)
from taskpaw_v3.tests.test_workflow_local_dispatch import add_film
from taskpaw_v3.tests.test_workflow_store import identity
from taskpaw_v3.tests.workflow_route_helpers import claim_remote, enrol_remote, route


@contextmanager
def media_agent(config, queue, observer):
    app = create_network_app(
        config,
        queue,
        lambda: {
            "machine": "fixture-controller",
            "monitors": {},
            "workflow": observer.snapshot(),
        },
    )
    requests = []

    @app.middleware("http")
    async def record(request, call_next):
        requests.append(request.url.path)
        return await call_next(request)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    owner = threading.Thread(target=lambda: server.run(sockets=[sock]))
    owner.start()
    try:
        deadline = time.monotonic() + 10
        wake = threading.Event()
        while not server.started and owner.is_alive() and time.monotonic() < deadline:
            wake.wait(0.01)
        assert server.started
        yield port, requests
    finally:
        server.should_exit = True
        owner.join(5)
        sock.close()
        assert not owner.is_alive()


def test_media_queue_survives_sampling_and_restart_without_new_event_consumer(
    tmp_path,
    capsys,
    world,  # noqa: F811
):
    config, queue = trusted_queue(tmp_path)
    marker_path = tmp_path / "workflow-events.json"
    observer = WorkflowEvents(queue, marker_path)
    host = Host(world.rt, world.fleet)
    remotes = [enrol_remote(world.rt, f"fixture-worker-{i}") for i in range(5)]
    for remote in remotes:
        film = add_film(world.rt)
        route(world.rt, film, remote.worker_id)
        claim_remote(world.rt, remote, film, "restore")
        online(
            world,
            remote,
            current={
                "film_id": film,
                "stage": "restore",
                "attempt_id": identity(),
                "phase": "running",
                "progress": "LTX 2/4 encode 52%",
                "elapsed_s": 60.0,
            },
        )
    waiting = add_film(world.rt)
    planned = add_film(world.rt, plan=False)
    local = add_film(world.rt)
    original = host.status

    def paused():
        status = original()
        status["dispatch"].update(
            paused=True,
            reason="fixture paused",
            current={"film_id": local, "film": "fixture-local", "stage": "translate"},
            waiting={waiting: "fixture waiting for GPU"},
        )
        return status

    host.status = paused
    observer.observe(host)
    notices = queue.recent()
    assert notices  # one existing needs-planning event, no new progress stream
    observer.observe(host)
    assert queue.recent() == notices
    database = tmp_path / "hub.db"
    markdown = tmp_path / "status.md"
    guide = (
        Path(__file__).resolve().parents[2] / "docs/guides/openclaw-integration.md"
    ).read_text(encoding="utf-8")
    example = guide.split("```python\n", 1)[1].split("```", 1)[0]
    # Only substitute the installation-specific path; execute the shipped reader.
    locator = 'Path("~/.taskpaw-hub/hub.db").expanduser()'
    assert locator in example
    example = compile(
        example.replace(locator, "Path(__fixture_db)"), "OpenClaw guide", "exec"
    )
    store = HubStore(database)
    poller = None
    try:
        with media_agent(config, queue, observer) as (port, requests):
            sid = store.add_server("fixture registration", "127.0.0.1", port)

            def new_poller():
                return Poller(
                    store,
                    "",
                    lambda: False,
                    lambda: "",
                    get_polling_token=lambda: config.api_token,
                    seed_fresh_seconds=120,
                )

            poller = new_poller()
            for _ in range(2):
                poller.poll_once()
            with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as reader:
                raw, error = reader.execute(
                    "SELECT status_json,error_code FROM upstream_status WHERE server_id=?",
                    (sid,),
                ).fetchone()
                saved = json.loads(raw)["workflow"]
                assert error is None
                assert len(saved["currents"]) == 6
                assert saved["currents_total"] == 6 and saved["currents_more"] == 0
                assert saved["dispatch"]["paused"] is True
                films = {row["film_id"]: row for row in saved["films"]}
                assert films[planned]["group"] == "planning"
                assert films[waiting]["status"] == "pending"
                assert films[waiting]["note"] == "fixture waiting for GPU"
                assert reader.execute("SELECT count(*) FROM events").fetchone()[
                    0
                ] == len(notices)
                assert (
                    reader.execute("SELECT count(*) FROM delivery_outbox").fetchone()[0]
                    == 0
                )
                historical = json.loads(
                    reader.execute(
                        "SELECT status_json FROM status_log ORDER BY id DESC LIMIT 1"
                    ).fetchone()[0]
                )
                assert historical["workflow"] == saved
            exec(example, {"__fixture_db": str(database)})
            readout = capsys.readouterr().out
            assert all(remote.name in readout for remote in remotes)
            assert "fixture-controller" in readout
            assert "fixture waiting for GPU" in readout
            write_status_md(
                markdown,
                poller.status_snapshot(),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            text = markdown.read_text(encoding="utf-8")
            assert all(remote.name in text for remote in remotes)
            assert "fixture-controller" in text and "fixture paused" in text
            assert "fixture waiting for GPU" in text
            assert set(requests) == {"/status", "/events"}
            assert len(queue) == 0  # only the owned Hub acknowledged it
            ack = poller.snapshot_acks()
            poller.stop()
            assert poller.stopped()
            store.close()
            store = HubStore(database)
            poller = new_poller()
            poller.poll_once()
            assert poller.snapshot_acks() == ack
            assert len(store.recent_events()) == len(notices)
            resumed = WorkflowEvents(queue, marker_path)
            resumed.observe(host)
            assert queue.recent() == notices
            assert resumed.snapshot()["films_total"] == saved["films_total"]
            assert resumed.stop()

            observer.stop()
            poller.poll_once()
            assert (
                json.loads(store.upstream_statuses()[0]["status_json"])["workflow"]
                is None
            )
            write_status_md(
                markdown,
                poller.status_snapshot(),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )
            assert "暂不可用" in markdown.read_text(encoding="utf-8")
            assert "fixture waiting for GPU" not in markdown.read_text(encoding="utf-8")
            exec(example, {"__fixture_db": str(database)})
            readout = capsys.readouterr().out
            assert "media: unavailable; not an empty queue" in readout
            assert "fixture waiting for GPU" not in readout
    finally:
        if poller is not None:
            poller.stop()
            assert poller.stopped()
        store.close()
        observer.stop()
        queue.close()
