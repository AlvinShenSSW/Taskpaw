"""R05 counter recovery and destructive-read admission (temporary fixtures only)."""

# These are real OS/filesystem/SQLite fixtures, not mocked state bodies.
import json
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict

import pytest
import uvicorn
from fastapi.testclient import TestClient

from taskpaw_v3.agent.server.app import create_network_app
from taskpaw_v3.agent.server.launcher import build_queue
from taskpaw_v3.core import state as state_module
from taskpaw_v3.core.config import AgentConfig, save_yaml
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.core.state import (
    StateError,
    StateSession,
    initialize_state,
    load_next_id,
    state_paths,
)
from taskpaw_v3.hub.server.poller import Poller
from taskpaw_v3.hub.server.store import HubStore
from taskpaw_v3.packaging.backend_main import main as backend_main


@pytest.mark.parametrize("body", [None, "", "{broken", "{}", '{"next_event_id":0}'])
def test_unverified_counter_does_not_fallback(tmp_path, body):
    path = tmp_path / "agent.state.json"
    if body is not None:
        path.write_text(body)
    with pytest.raises(ValueError):
        load_next_id(path)
    with pytest.raises(ValueError):
        build_queue(AgentConfig(server_id="fixture", machine="fixture"), path)


def test_unoffered_ack_inside_current_range_retains_queue():
    queue = EventQueue("fixture")
    for number in range(1000):
        queue.add("fixture", str(number))
    client = TestClient(
        create_network_app(AgentConfig(server_id="fixture", machine="fixture"), queue)
    )
    before = queue.recent(1000)
    assert client.get("/events?ack=900").status_code == 409
    assert len(queue) == 1000
    assert queue.recent(1000) == before


def test_packaged_offline_inspect_is_reachable(tmp_path):
    config = tmp_path / "agent.yaml"
    save_yaml(AgentConfig(server_id="fixture", machine="fixture"), config)
    assert backend_main(["agent-state", "--config", str(config), "inspect"]) == 0
    assert not (tmp_path / "agent.state.json").exists()


def trusted_queue(tmp_path, next_id=1):
    config = AgentConfig(
        server_id="fixture",
        machine="fixture",
        host_metrics=False,
        api_token="fixture-r05",
    )
    path = tmp_path / "agent.state.json"
    initialize_state(path, config.server_id, next_id)
    return config, build_queue(config, path)


@contextmanager
def actual_agent(config, queue, *, before_events=None):
    """App-only HTTP: one held random socket, no reclaim/supervisor/ready."""
    app = create_network_app(config, queue)
    requests = []

    @app.middleware("http")
    async def observe(request, call_next):
        requests.append((request.url.path, request.url.query))
        if request.url.path == "/events" and before_events is not None:
            response = before_events()
            if response is not None:
                return response
        return await call_next(request)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    try:
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        yield port, requests
    finally:
        server.should_exit = True
        thread.join(5)
        sock.close()
        assert not thread.is_alive()


@pytest.mark.parametrize(
    "field,value",
    [
        ("next_event_id", True),
        ("next_event_id", "42"),
        ("next_event_id", 1.5),
        ("next_event_id", 0),
        ("next_event_id", -1),
        ("next_event_id", 1 << 64),
        ("version", True),
        ("version", 3),
        ("stream_id", "bad"),
        ("lineage_origin", "unknown"),
    ],
)
def test_strict_state_parser_preserves_fault(tmp_path, field, value):
    path = tmp_path / "agent.state.json"
    record = asdict(initialize_state(path, "fixture", 42))
    record[field] = value
    raw = json.dumps(record)
    path.write_text(raw)
    with pytest.raises(StateError) as caught:
        StateSession.open(path, "fixture")
    assert path.read_text() == raw
    assert caught.value.backups
    first = {p: p.read_bytes() for p in caught.value.backups}
    with pytest.raises(StateError):
        StateSession.open(path, "fixture")
    assert all(p.read_bytes() == content for p, content in first.items())


@pytest.mark.parametrize(
    "fault",
    ["delete-primary", "delete-anchor", "lower-primary", "empty-primary", "identity"],
)
def test_single_state_fault_never_starts_queue(tmp_path, fault):
    path = tmp_path / "agent.state.json"
    record = asdict(initialize_state(path, "fixture", 901))
    primary, anchor, _ = state_paths(path)
    if fault.startswith("delete"):
        (primary if fault == "delete-primary" else anchor).unlink()
    else:
        if fault == "lower-primary":
            record["next_event_id"] = 1
        elif fault == "identity":
            record["server_id"] = "replacement"
        primary.write_text("" if fault == "empty-primary" else json.dumps(record))
    with pytest.raises(StateError):
        build_queue(AgentConfig(server_id="fixture", machine="fixture"), path)


def test_real_competing_process_lease(tmp_path):
    path = tmp_path / "agent.state.json"
    initialize_state(path, "fixture")
    session = StateSession.open(path, "fixture")
    program = "import sys; from pathlib import Path; from taskpaw_v3.core.state import FileLease,StateError;\ntry: lease=FileLease(Path(sys.argv[1])).acquire()\nexcept StateError: sys.exit(21)\nlease.close()"
    try:
        assert (
            subprocess.run(
                [sys.executable, "-c", program, str(state_paths(path)[2])],
                capture_output=True,
            ).returncode
            == 21
        )
    finally:
        session.close()
    assert (
        subprocess.run(
            [sys.executable, "-c", program, str(state_paths(path)[2])],
            capture_output=True,
        ).returncode
        == 0
    )


@pytest.mark.parametrize(
    "failed_target", ["anchor", "primary", "directory-sync", "primary-directory-sync"]
)
def test_atomic_failure_invisible_retry_and_restart(
    tmp_path, monkeypatch, failed_target
):
    config, queue = trusted_queue(tmp_path, 42)
    path, anchor, _ = state_paths(tmp_path / "agent.state.json")
    old_replace, old_sync = state_module.os.replace, state_module._sync_dir
    failed = False
    syncs = 0

    def replace(source, target):
        nonlocal failed
        if not failed and (
            (failed_target == "anchor" and target == anchor)
            or (failed_target == "primary" and target == path)
        ):
            failed = True
            raise OSError("fixture reservation interrupted")
        return old_replace(source, target)

    def sync(parent):
        nonlocal failed, syncs
        syncs += 1
        if not failed and (
            (failed_target == "directory-sync" and syncs == 1)
            or (failed_target == "primary-directory-sync" and syncs == 2)
        ):
            failed = True
            raise OSError("fixture directory sync interrupted")
        old_sync(parent)

    monkeypatch.setattr(state_module.os, "replace", replace)
    monkeypatch.setattr(state_module, "_sync_dir", sync)
    try:
        with pytest.raises(OSError):
            queue.add("fixture", "failed")
        assert len(queue) == 0 and queue.recent() == [] and queue.next_id == 42
        assert queue.add("fixture", "retry")["id"] == 42
    finally:
        queue.close()
    resumed = build_queue(config, path)
    try:
        assert resumed.add("fixture", "after restart")["id"] == 43
    finally:
        resumed.close()


def test_interrupted_reservation_requires_explicit_recovery(tmp_path, monkeypatch):
    config, queue = trusted_queue(tmp_path, 42)
    config_path = tmp_path / "agent.yaml"
    save_yaml(config, config_path)
    path = tmp_path / "agent.state.json"
    original = state_module.os.replace

    def replace(source, target):
        if target == path:
            raise OSError("fixture primary interruption")
        return original(source, target)

    with monkeypatch.context() as patch:
        patch.setattr(state_module.os, "replace", replace)
        with pytest.raises(OSError):
            queue.add("fixture", "invisible")
    queue.close()
    with pytest.raises(StateError):
        build_queue(config, path)
    assert backend_main(["agent-state", "--config", str(config_path), "recover"]) == 2
    assert (
        backend_main(
            [
                "agent-state",
                "--config",
                str(config_path),
                "recover",
                "--confirm-surviving-record-intact",
            ]
        )
        == 0
    )
    resumed = build_queue(config, path)
    try:
        assert (
            resumed.add("fixture", "recovered")["id"] == 43
        )  # skip reserved, unpublished42
    finally:
        resumed.close()


@pytest.mark.parametrize(
    "query",
    [
        "ack=900",
        "ack=0&ack=900",
        "ack=garbage",
        "ack=-2",
        "cursor_stream=old",
        "cursor_stream=&cursor_boot=",
        "cursor_boot=old&cursor_boot=new",
    ],
)
def test_http_refusal_exactly_retains_state(tmp_path, query):
    config, queue = trusted_queue(tmp_path)
    queue.add("fixture", "retained")
    before = (
        queue.next_id,
        queue.recent(),
        (tmp_path / "agent.state.json").read_bytes(),
    )
    client = TestClient(create_network_app(config, queue))
    try:
        assert client.get("/events?" + query).status_code == 401
        assert (
            client.get(
                "/events?" + query, headers={"Authorization": "Bearer fixture-r05"}
            ).status_code
            == 409
        )
        assert len(queue) == 1
        assert (
            queue.next_id,
            queue.recent(),
            (tmp_path / "agent.state.json").read_bytes(),
        ) == before
    finally:
        queue.close()


def test_trusted_legacy_numeric_and_noack_requests(tmp_path):
    config, queue = trusted_queue(tmp_path, 901)
    queue.add("fixture", "901")
    client = TestClient(create_network_app(config, queue))
    headers = {"Authorization": "Bearer fixture-r05"}
    try:
        assert (
            client.get("/events?ack=900", headers=headers).json()["events"][0]["id"]
            == 901
        )
        assert (
            client.get("/events?ack=900", headers=headers).json()["events"][0]["id"]
            == 901
        )
        assert client.get("/events?ack=901", headers=headers).json()["events"] == []
        queue.add("fixture", "902")
        assert client.get("/events", headers=headers).json()["events"][0]["id"] == 902
        assert client.get("/events", headers=headers).json()["events"] == []
        assert len(queue.recent()) == 2
    finally:
        queue.close()


def test_actual_http_sqlite_replay_hub_restart_and_agent_restart(tmp_path, monkeypatch):
    config, queue = trusted_queue(tmp_path)
    store = HubStore(tmp_path / "hub.db")
    try:
        queue.add("fixture", "first")
        with actual_agent(config, queue) as (port, requests):
            sid = store.add_server("fixture", "127.0.0.1", port)
            poller = Poller(
                store,
                "http://unused.invalid",
                lambda: False,
                lambda: "",
                lambda: "fixture-r05",
            )
            poller.poll_once()
            assert poller.snapshot_acks() == {sid: 1} and len(queue) == 1
            assert len(store.recent_events(sid)) == 1
            # Hub-only restart, Agent same boot floor0 with ack1: must trim normally.
            store.close()
            store = HubStore(tmp_path / "hub.db")
            poller = Poller(
                store,
                "http://unused.invalid",
                lambda: False,
                lambda: "",
                lambda: "fixture-r05",
            )
            poller.poll_once()
            assert len(queue) == 0
            queue.add("fixture", "second")
            store._conn.execute(
                "CREATE TRIGGER owned_ack_failure BEFORE UPDATE ON config WHEN NEW.key='last_event_ids' BEGIN SELECT RAISE(FAIL,'owned ack failure'); END"
            )
            try:
                poller.poll_once()
            finally:
                store._conn.execute("DROP TRIGGER owned_ack_failure")
            # The whole ingest transaction rolled back, including event2.
            assert len(store.recent_events(sid)) == 1
            assert poller.snapshot_acks() == {sid: 1} and len(queue) == 1
            poller.poll_once()
            assert poller.snapshot_acks() == {sid: 2}
            assert [
                r[0]
                for r in store._conn.execute(
                    "SELECT event_id FROM events ORDER BY event_id"
                )
            ] == [1, 2]
            assert all(
                "cursor_stream=" in query and "cursor_boot=" in query
                for path, query in requests
                if path == "/events"
            )
        queue.close()
        queue = build_queue(config, tmp_path / "agent.state.json")
        queue.add("fixture", "third")
        with actual_agent(config, queue) as (port, _):
            store.update_server(sid, port=port)
            poller.poll_once()
            assert poller.snapshot_acks() == {sid: 3}
            assert poller.snapshot_event_channels()[sid]["state"] == "ready"
    finally:
        queue.close()
        store.close()


@pytest.mark.parametrize("failure", ["store_event", "enqueue_delivery", "ack"])
def test_actual_http_outbox_failure_replays_without_loss(
    tmp_path, monkeypatch, failure
):
    config, queue = trusted_queue(tmp_path)
    queue.add("fixture", "preserved body")
    store = HubStore(tmp_path / "hub.db")
    try:
        with actual_agent(config, queue) as (port, _):
            sid = store.add_server("fixture", "127.0.0.1", port)
            poller = Poller(
                store,
                "http://unused.invalid",
                lambda: True,
                lambda: "fake-openclaw",
                lambda: "fixture-r05",
            )
            # Exercise admission/store/enqueue/ack without making any OpenClaw request.
            poller.fetch_events(store.get_server(sid))
            table = {
                "store_event": "events",
                "enqueue_delivery": "delivery_outbox",
                "ack": "config",
            }[failure]
            operation = "UPDATE" if failure == "ack" else "INSERT"
            condition = " WHEN NEW.key='last_event_ids'" if failure == "ack" else ""
            store._conn.execute(
                f"CREATE TRIGGER owned_ingest_failure BEFORE {operation} ON {table}{condition} BEGIN SELECT RAISE(FAIL,'owned persistence interruption'); END"
            )
            try:
                poller._poll_server(store.get_server(sid), True)
                assert (
                    poller.snapshot_event_channels()[sid]["reason"]
                    == "event_store_failed"
                )
                assert store.recent_events(sid) == []
                assert store._conn.execute(
                    "SELECT count(*) FROM delivery_outbox"
                ).fetchone() == (0,)
            finally:
                store._conn.execute("DROP TRIGGER owned_ingest_failure")
            assert poller.snapshot_acks() == {sid: -1}
            assert len(queue) == 1
            poller._poll_server(store.get_server(sid), True)
            assert store.read_acks() == {sid: 1}
            assert len(store.recent_events(sid)) == 1
            rows = store._conn.execute(
                "SELECT dedupe_key,payload_json FROM delivery_outbox"
            ).fetchall()
            assert len(rows) == 1 and rows[0][0] == f"{sid}:1"
            assert "preserved body" in rows[0][1]
            # A paused replacement keeps the exact stored event/outbox bodies and ack.
            before = (store.recent_events(sid), rows, store.read_acks())
            status = {
                "event_cursor": {**queue.cursor_snapshot(), "stream_id": "f" * 32}
            }
            assert poller.fetch_events(store.get_server(sid), status) == []
            assert (
                poller.snapshot_event_channels()[sid]["reason"]
                == "state_identity_changed"
            )
            assert (
                store.recent_events(sid),
                store._conn.execute(
                    "SELECT dedupe_key,payload_json FROM delivery_outbox"
                ).fetchall(),
                store.read_acks(),
            ) == before
    finally:
        queue.close()
        store.close()


@pytest.mark.parametrize(
    "change", ["stream", "server", "floor", "missing-binding", "missing-ack"]
)
def test_hub_uncertain_cursor_sends_zero_events(tmp_path, monkeypatch, change):
    config, queue = trusted_queue(tmp_path, 901)
    store = HubStore(tmp_path / "hub.db")
    try:
        with actual_agent(config, queue) as (port, requests):
            sid = store.add_server("fixture", "127.0.0.1", port)
            poller = Poller(
                store, "unused", lambda: False, lambda: "", lambda: "fixture-r05"
            )
            poller.poll_once()
            cursor = queue.cursor_snapshot()
            if change == "stream":
                cursor["stream_id"] = "f" * 32
            elif change == "server":
                cursor["server_id"] = "different-agent"
            elif change == "floor":
                store.set_config("last_event_ids", json.dumps({str(sid): 900}))
                poller.last_event_ids[sid] = 900
                cursor.update(boot_id="f" * 32, resume_floor=0)
            elif change == "missing-binding":
                store._conn.execute(
                    "DELETE FROM event_cursors WHERE server_id=?", (sid,)
                )
                store._conn.commit()
            else:
                poller.last_event_ids.clear()
            requests.clear()
            assert (
                poller.fetch_events(store.get_server(sid), {"event_cursor": cursor})
                == []
            )
            assert requests == []
            assert poller.snapshot_event_channels()[sid]["state"] == "paused"
    finally:
        queue.close()
        store.close()


def test_actual_status_events_boot_race_retains_queue_and_db(tmp_path):
    config, queue = trusted_queue(tmp_path)
    queue.add("fixture", "retained after stale proof")
    store = HubStore(tmp_path / "hub.db")
    try:

        def boot_changes():
            queue._boot_id = "f" * 32

        with actual_agent(config, queue, before_events=boot_changes) as (
            port,
            requests,
        ):
            sid = store.add_server("fixture", "127.0.0.1", port)
            poller = Poller(
                store, "unused", lambda: False, lambda: "", lambda: "fixture-r05"
            )
            poller.poll_once()
            assert [path for path, _ in requests] == ["/status", "/events"]
            assert (
                poller.snapshot_event_channels()[sid]["reason"] == "event_http_refused"
            )
            assert len(queue) == 1 and store.recent_events(sid) == []
            assert store.read_acks() == {sid: -1}
            assert (
                store._conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[
                    0
                ]
                == 0
            )
    finally:
        queue.close()
        store.close()


def test_hub_current_failed_status_does_not_use_cached_proof(tmp_path, monkeypatch):
    config, queue = trusted_queue(tmp_path)
    store = HubStore(tmp_path / "hub.db")
    try:
        with actual_agent(config, queue) as (port, requests):
            sid = store.add_server("fixture", "127.0.0.1", port)
            poller = Poller(
                store, "unused", lambda: False, lambda: "", lambda: "fixture-r05"
            )
            poller.poll_once()
            requests.clear()
            monkeypatch.setattr(poller, "fetch_status", lambda server: (False, None))
            poller.poll_once()
            assert requests == []
            assert (
                poller.snapshot_event_channels()[sid]["reason"]
                == "current_status_unavailable"
            )
    finally:
        queue.close()
        store.close()


def test_agent_state_refuses_before_claim_and_releases_failed_startup(
    tmp_path, monkeypatch
):
    from taskpaw_v3.agent.server import launcher

    config = AgentConfig(
        server_id="fixture", machine="fixture", bind_port=15680, control_port=15681
    )
    monkeypatch.setattr(
        launcher, "reclaim_ports_from_stale_instance", lambda *a, **kw: None
    )
    claims = []

    def fail_claim(*args, **kwargs):
        claims.append(args)
        raise launcher.PortInUseError("fixture occupied")

    monkeypatch.setattr(launcher, "claim_port", fail_claim)
    config_path = tmp_path / "agent.yaml"
    with pytest.raises(StateError):
        launcher.run_agent(
            config,
            state_path=tmp_path / "agent.state.json",
            config_path=config_path,
            block=False,
        )
    assert claims == [] and not (tmp_path / "logs").exists()
    initialize_state(tmp_path / "agent.state.json", config.server_id)
    with pytest.raises(launcher.PortInUseError):
        launcher.run_agent(
            config,
            state_path=tmp_path / "agent.state.json",
            config_path=config_path,
            block=False,
        )
    assert len(claims) == 1
    reopened = StateSession.open(tmp_path / "agent.state.json", config.server_id)
    reopened.close()


def test_backup_failure_does_not_repair_or_publish(tmp_path, monkeypatch):
    path = tmp_path / "agent.state.json"
    raw = b'{"fake_secret":"fixture-r05-never-echo"}'
    path.write_bytes(raw)
    original = state_module.os.open

    def fail_backup(target, *args, **kwargs):
        if ".fault-" in str(target):
            raise OSError("fixture disk full")
        return original(target, *args, **kwargs)

    monkeypatch.setattr(state_module.os, "open", fail_backup)
    with pytest.raises(StateError, match="backup_failed") as caught:
        StateSession.open(path, "fixture")
    assert path.read_bytes() == raw
    assert "fixture-r05-never-echo" not in str(caught.value)
    assert not state_paths(path)[1].exists()


def test_counter_exhaustion_refuses_before_visibility(tmp_path):
    config, queue = trusted_queue(tmp_path, state_module.MAX_EVENT_ID)
    try:
        assert queue.add("fixture", "last")["id"] == state_module.MAX_EVENT_ID
        with pytest.raises(ValueError, match="counter_exhausted"):
            queue.add("fixture", "overflow")
        assert len(queue) == 1 and len(queue.recent()) == 1
    finally:
        queue.close()
    with pytest.raises(StateError, match="counter_exhausted"):
        build_queue(config, tmp_path / "agent.state.json")


def test_old_agent_current_status_continues_without_any_event_request(
    tmp_path, monkeypatch
):
    from taskpaw_v3.tests.test_hub import FakeResp

    store = HubStore(tmp_path / "hub.db")
    seen = []
    try:
        sid = store.add_server("old", "127.0.0.1", 12345)
        store.set_config("last_event_ids", json.dumps({str(sid): 900}))
        poller = Poller(store, "http://unused.invalid", lambda: False, lambda: "")

        def transport(request, timeout):
            seen.append(request.full_url)
            assert request.full_url.endswith("/status")
            return FakeResp({"machine": "old", "monitors": {}})

        from taskpaw_v3.tests.test_hub import fake_request

        monkeypatch.setattr(
            poller, "_request", lambda req: fake_request(req, transport)
        )
        poller.poll_once()
        assert len(seen) == 1
        assert poller.snapshot_statuses()[sid]["online"] is True
        assert (
            poller.snapshot_event_channels()[sid]["reason"]
            == "legacy_cursor_unverifiable"
        )
        assert store.read_acks() == {sid: 900}
    finally:
        store.close()


@pytest.mark.parametrize(
    "raw",
    ['{"1":900,"1":0}', '{"1":true}', '{"1":"900"}', "[]", '{"01":900}', "{broken"],
)
def test_malformed_ack_store_pauses_without_repair(tmp_path, raw):
    config, queue = trusted_queue(tmp_path)
    store = HubStore(tmp_path / "hub.db")
    try:
        with actual_agent(config, queue) as (port, requests):
            sid = store.add_server("fixture", "127.0.0.1", port)
            store.set_config("last_event_ids", raw)
            poller = Poller(
                store, "unused", lambda: False, lambda: "", lambda: "fixture-r05"
            )
            poller.poll_once()
            assert requests == [("/status", "")]
            assert store.get_config("last_event_ids") == raw
            assert (
                poller.snapshot_event_channels()[sid]["reason"]
                == "cursor_store_invalid"
            )
    finally:
        queue.close()
        store.close()


def test_duplicate_counter_evidence_refuses_without_fallback(tmp_path):
    path = tmp_path / "agent.state.json"
    path.write_text('{"next_event_id":901,"next_event_id":1}')
    with pytest.raises(StateError, match="duplicate_json_key"):
        StateSession.open(path, "fixture")
    assert path.read_text() == '{"next_event_id":901,"next_event_id":1}'


def test_external_state_change_blocks_next_publication(tmp_path):
    config, queue = trusted_queue(tmp_path, 901)
    queue.add("fixture", "901")
    before = queue.recent()
    path = tmp_path / "agent.state.json"
    data = json.loads(path.read_text())
    data["next_event_id"] = 1
    path.write_text(json.dumps(data))
    try:
        with pytest.raises(StateError):
            queue.add("fixture", "must not publish")
        assert queue.recent() == before and queue.next_id == 902 and len(queue) == 1
    finally:
        queue.close()


def test_existing_registration_migration_is_not_fresh(tmp_path):
    db = tmp_path / "hub.db"
    store = HubStore(db)
    sid = store.add_server("existing", "127.0.0.1", 15680)
    store._conn.execute("DROP TABLE event_cursors")
    store._conn.commit()
    store.close()
    store = HubStore(db)
    try:
        assert store.get_event_cursor(sid)["state"] == "unverified"
        fresh = store.add_server("fresh", "127.0.0.1", 15680)
        assert store.get_event_cursor(fresh)["state"] == "fresh"
    finally:
        store.close()


def test_actual_advertised_events_404_never_legacy_fallback(tmp_path):
    from starlette.responses import JSONResponse

    config, queue = trusted_queue(tmp_path)
    queue.add("fixture", "retained")
    store = HubStore(tmp_path / "hub.db")
    try:
        with actual_agent(
            config,
            queue,
            before_events=lambda: JSONResponse({"error": "fixture"}, status_code=404),
        ) as (port, requests):
            sid = store.add_server("fixture", "127.0.0.1", port)
            poller = Poller(
                store, "unused", lambda: False, lambda: "", lambda: "fixture-r05"
            )
            poller.poll_once()
            assert [path for path, query in requests] == ["/status", "/events"]
            assert "ack=" in requests[-1][1]
            assert len(queue) == 1 and store.read_acks() == {sid: -1}
            assert (
                poller.snapshot_event_channels()[sid]["reason"] == "event_http_refused"
            )
    finally:
        queue.close()
        store.close()


def test_actual_legacy_agent_status_only(tmp_path):
    config = AgentConfig(
        server_id="fixture", machine="fixture", api_token="fixture-r05"
    )
    queue = EventQueue("fixture")  # semantic old Agent: no durable cursor proof
    queue.add("fixture", "low reset id")
    store = HubStore(tmp_path / "hub.db")
    try:
        with actual_agent(config, queue) as (port, requests):
            sid = store.add_server("fixture", "127.0.0.1", port)
            store.set_config("last_event_ids", json.dumps({str(sid): 900}))
            poller = Poller(
                store, "unused", lambda: False, lambda: "", lambda: "fixture-r05"
            )
            poller.poll_once()
            assert requests == [("/status", "")]
            assert len(queue) == 1 and store.read_acks() == {sid: 900}
            assert poller.snapshot_statuses()[sid]["online"] is True
            assert (
                poller.snapshot_event_channels()[sid]["reason"]
                == "legacy_cursor_unverifiable"
            )
    finally:
        store.close()


def test_custom_status_cannot_replace_authoritative_cursor(tmp_path):
    config, queue = trusted_queue(tmp_path)
    try:
        client = TestClient(
            create_network_app(
                config,
                queue,
                status_provider=lambda: {
                    "event_cursor": {"boot_id": "forged"},
                    "custom": 42,
                },
            )
        )
        response = client.get(
            "/status", headers={"Authorization": "Bearer fixture-r05"}
        ).json()
        assert (
            response["custom"] == 42
            and response["event_cursor"] == queue.cursor_snapshot()
        )
    finally:
        queue.close()


def test_hub_claim_failure_releases_maintenance_lease(tmp_path, monkeypatch):
    from taskpaw_v3.core import net
    from taskpaw_v3.core.config import HubConfig
    from taskpaw_v3.core.state import FileLease, db_lease_path
    from taskpaw_v3.hub.server import app

    monkeypatch.setattr(net, "reclaim_ports_from_stale_instance", lambda *a, **kw: None)

    def fail(*a, **kw):
        raise net.PortInUseError("fixture occupied")

    monkeypatch.setattr(net, "claim_port", fail)
    db = tmp_path / "hub.db"
    store = HubStore(db)
    try:
        with pytest.raises(net.PortInUseError):
            app.run_hub(
                HubConfig(machine="fixture", bind_port=15690), store, block=False
            )
        with FileLease(db_lease_path(db)):
            pass
        assert store.list_servers() == [] and store.read_acks() == {}
    finally:
        store.close()


def test_preupgrade_proof_is_captured_once(tmp_path):
    config, queue = trusted_queue(tmp_path)
    db = tmp_path / "hub.db"
    store = HubStore(db)
    sid = store.add_server("existing", "127.0.0.1", 15680)
    old_cursor = queue.cursor_snapshot()
    store.log_status(sid, True, json.dumps({"event_cursor": old_cursor}))
    store._conn.execute("DROP TABLE event_cursors")
    store._conn.commit()
    store.close()
    store = HubStore(db)
    try:
        original = store.get_event_cursor(sid)
        assert (
            original["state"] == "unverified"
            and original["identity"]["stream_id"] == old_cursor["stream_id"]
        )
        store.log_status(
            sid,
            True,
            json.dumps({"event_cursor": {**old_cursor, "stream_id": "f" * 32}}),
        )
        store.close()
        store = HubStore(db)
        assert store.get_event_cursor(sid) == original
    finally:
        queue.close()
        store.close()


def test_startup_failure_stops_actual_writer_before_lease_release(
    tmp_path, monkeypatch
):
    from unittest.mock import Mock

    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.monitors import runtime

    config, queue = trusted_queue(tmp_path)
    config.bind_port, config.control_port = 15680, 15681
    stopped = threading.Event()
    started = threading.Event()
    observations = []

    def write_once_then_wait():
        queue.add("fixture", "startup writer")
        started.set()
        stopped.wait(5)

    writer = threading.Thread(target=write_once_then_wait)
    supervisor = Mock()
    supervisor._watchdog = None
    supervisor.snapshot.side_effect = lambda: {"fixture": {"alive": writer.is_alive()}}

    def start():
        writer.start()
        assert started.wait(2)
        raise RuntimeError("fixture partial start interruption")

    def stop():
        # State must still be exclusively owned while this writer lives.
        with pytest.raises(StateError):
            StateSession.open(tmp_path / "agent.state.json", config.server_id)
        observations.append(writer.is_alive())
        stopped.set()
        writer.join(2)

    supervisor.start.side_effect, supervisor.stop.side_effect = start, stop
    monkeypatch.setattr(
        launcher, "reclaim_ports_from_stale_instance", lambda *a, **kw: None
    )
    monkeypatch.setattr(launcher, "claim_port", lambda *a, **kw: Mock())
    monkeypatch.setattr(runtime, "build_supervisor", lambda *a, **kw: supervisor)

    try:
        with pytest.raises(RuntimeError, match="fixture partial start"):
            launcher.run_agent(
                config, queue=queue, config_path=tmp_path / "agent.yaml", block=False
            )
        assert observations == [True] and not writer.is_alive()
        resumed = StateSession.open(tmp_path / "agent.state.json", config.server_id)
        assert resumed.record.next_event_id == 2
        resumed.close()
        with pytest.raises(StateError, match="event_queue_closed"):
            queue.add("fixture", "closed writer")
    finally:
        stopped.set()
        if writer.is_alive():
            writer.join(2)
        queue.close()


def test_hub_held_db_lease_refuses_before_claim_or_service(tmp_path, monkeypatch):
    from taskpaw_v3.core import net
    from taskpaw_v3.core.config import HubConfig
    from taskpaw_v3.core.state import FileLease, db_lease_path
    from taskpaw_v3.hub.server import app

    store = HubStore(tmp_path / "custom-name.db")
    monkeypatch.setattr(net, "reclaim_ports_from_stale_instance", lambda *a, **kw: None)

    def forbidden(*args, **kwargs):
        raise AssertionError("held maintenance lease must precede writers/claims")

    monkeypatch.setattr(net, "claim_port", forbidden)
    monkeypatch.setattr(app, "create_hub_app", forbidden)
    try:
        with FileLease(db_lease_path(store.db_path)):
            with pytest.raises(StateError, match="lease_held_or_unavailable"):
                app.run_hub(
                    HubConfig(machine="fixture", bind_port=15690), store, block=False
                )
        assert store.list_servers() == [] and store.read_acks() == {}
    finally:
        store.close()


def test_interrupted_new_pairing_config_write_fails_closed(tmp_path, monkeypatch):
    import taskpaw_v3.agent.state as offline

    config = AgentConfig(server_id="old-fixture", machine="fixture")
    path = tmp_path / "agent.yaml"
    save_yaml(config, path)
    before = path.read_bytes()

    def fail(*args, **kwargs):
        raise OSError("fixture config persistence interruption")

    monkeypatch.setattr(offline, "save_yaml", fail)
    assert (
        backend_main(
            [
                "agent-state",
                "--config",
                str(path),
                "initialize",
                "--confirm-new-pairing",
            ]
        )
        == 2
    )
    assert path.read_bytes() == before
    with pytest.raises(StateError, match="state_identity_mismatch"):
        build_queue(config, tmp_path / "agent.state.json")


@pytest.mark.parametrize(
    "outcome",
    ["success", "before-write-failure", "anchor-failure", "after-write-failure"],
)
def test_closing_reservation_retains_native_lease_until_unwound(
    tmp_path, monkeypatch, outcome
):
    """Closing stays bounded while real disk reservation ownership remains exclusive."""
    config = AgentConfig(server_id="fixture", machine="fixture")
    config_path = tmp_path / "agent.yaml"
    save_yaml(config, config_path)
    path = tmp_path / "agent.state.json"
    initialize_state(path, config.server_id)
    session = StateSession.open(path, config.server_id)
    entered, release = threading.Event(), threading.Event()
    results = []
    original = state_module.write_pair

    def stalled(target, record):
        entered.set()
        assert release.wait(10), "owned fixture deadline"
        if outcome == "before-write-failure":
            raise OSError("fixture no write")
        if outcome == "anchor-failure":
            state_module.atomic_json(state_paths(target)[1], asdict(record))
            raise OSError("fixture primary failure")
        original(target, record)
        if outcome == "after-write-failure":
            raise OSError("fixture final sync failure")

    monkeypatch.setattr(state_module, "write_pair", stalled)

    def reserve():
        try:
            session.reserve(2)
            results.append("success")
        except OSError:
            results.append("failure")

    writer = threading.Thread(target=reserve)
    program = "import sys; from pathlib import Path; from taskpaw_v3.core.state import FileLease,StateError;\ntry: lease=FileLease(Path(sys.argv[1])).acquire()\nexcept StateError: sys.exit(21)\nlease.close()"
    writer.start()
    try:
        assert entered.wait(2)
        for _ in range(3):
            started = time.monotonic()
            session.close()
            assert time.monotonic() - started < 1  # no join/storage wait
        with pytest.raises(StateError, match="state_session_closed"):
            session.reserve(2)
        assert (
            subprocess.run(
                [sys.executable, "-c", program, str(state_paths(path)[2])],
                capture_output=True,
            ).returncode
            == 21
        )
        assert writer.is_alive()
    finally:
        release.set()
        writer.join(3)
        session.close()
    assert not writer.is_alive()
    assert results == ["success" if outcome == "success" else "failure"]
    assert (
        subprocess.run(
            [sys.executable, "-c", program, str(state_paths(path)[2])],
            capture_output=True,
        ).returncode
        == 0
    )
    with pytest.raises(StateError, match="state_session_closed"):
        session.reserve(2)
    monkeypatch.setattr(state_module, "write_pair", original)
    if outcome == "anchor-failure":
        with pytest.raises(StateError, match="state_records_disagree"):
            StateSession.open(path, config.server_id)
        assert (
            backend_main(
                [
                    "agent-state",
                    "--config",
                    str(config_path),
                    "recover",
                    "--confirm-surviving-record-intact",
                ]
            )
            == 0
        )
    resumed = build_queue(config, path)
    try:
        expected = 1 if outcome == "before-write-failure" else 2
        assert resumed.add("fixture", "after closed reservation")["id"] == expected
        assert state_module.read_record(path).next_event_id == expected + 1
    finally:
        resumed.close()


def test_closing_queue_late_add_refuses_without_waiting_for_storage(
    tmp_path, monkeypatch
):
    config, queue = trusted_queue(tmp_path)
    entered, release, late_done = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    old_result, late_result = [], []
    original = state_module.write_pair

    def stalled(path, record):
        entered.set()
        assert release.wait(10), "owned fixture deadline"
        original(path, record)

    monkeypatch.setattr(state_module, "write_pair", stalled)

    def admitted():
        old_result.append(queue.add("fixture", "admitted before close")["id"])

    def late():
        try:
            queue.add("fixture", "must reject")
            late_result.append("published")
        except StateError:
            late_result.append("refused")
        finally:
            late_done.set()

    writer, newcomer = threading.Thread(target=admitted), threading.Thread(target=late)
    writer.start()
    try:
        assert entered.wait(2)
        started = time.monotonic()
        queue.close()
        queue.close()
        assert time.monotonic() - started < 1
        newcomer.start()
        assert late_done.wait(1), "late add blocked on in-flight storage"
        assert late_result == ["refused"]
    finally:
        release.set()
        writer.join(3)
        if newcomer.ident is not None:
            newcomer.join(3)
        queue.close()
    assert not writer.is_alive() and not newcomer.is_alive()
    assert old_result == [1] and queue.next_id == 2 and len(queue.recent()) == 1
    monkeypatch.setattr(state_module, "write_pair", original)
    resumed = build_queue(config, tmp_path / "agent.state.json")
    try:
        assert resumed.add("fixture", "new owner")["id"] == 2
    finally:
        resumed.close()


def test_closing_memory_queue_stays_closed_and_retains_history():
    queue = EventQueue("fixture")
    queue.add("fixture", "before close")
    before = queue.recent()
    queue.close()
    queue.close()
    with pytest.raises(StateError, match="event_queue_closed"):
        queue.add("fixture", "late")
    assert queue.next_id == 2 and queue.recent() == before


def test_closing_actual_unregister_launcher_retains_inflight_lease(
    tmp_path, monkeypatch
):
    """SR001 production chain with owned plugin/thread/files, no socket/reclaim."""
    from unittest.mock import Mock

    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.monitors import runtime
    from taskpaw_v3.monitors.base import (
        BaseMonitorConfig,
        MonitorInstance,
        MonitorPlugin,
        MonitorStatus,
    )
    from taskpaw_v3.monitors.supervisor import Supervisor

    class FixtureInstance(MonitorInstance):
        def check(self, emit):
            emit("info", "owned fixture", "old in-flight event")
            return MonitorStatus(state="idle")

    class FixturePlugin(MonitorPlugin):
        type_id = "owned-fixture"

        @classmethod
        def config_model(cls):
            return BaseMonitorConfig

        def create(self, instance_id, config):
            return FixtureInstance(instance_id, config)

    config, old = trusted_queue(tmp_path)
    config.bind_port, config.control_port = 15680, 15681
    supervisor = Supervisor(runtime.make_queue_sink(old, config.machine))
    supervisor.register(FixturePlugin(), BaseMonitorConfig(name="owned-fixture"))
    entered, release = threading.Event(), threading.Event()
    owned_worker = None
    original = state_module.write_pair

    def stalled(path, record):
        if threading.current_thread().name == "mon-owned-fixture":
            entered.set()
            assert release.wait(10), "owned fixture deadline"
        original(path, record)

    def cancel_setup(*args, **kwargs):
        nonlocal owned_worker
        assert entered.wait(3)
        owned_worker = supervisor._monitors["owned-fixture"].thread
        supervisor.unregister("owned-fixture", timeout=0.01)
        assert supervisor.snapshot() == {} and owned_worker.is_alive()
        raise RuntimeError("owned fixture setup failure")

    monkeypatch.setattr(state_module, "write_pair", stalled)
    monkeypatch.setattr(
        launcher, "reclaim_ports_from_stale_instance", lambda *a, **kw: None
    )
    monkeypatch.setattr(launcher, "claim_port", lambda *a, **kw: Mock())
    monkeypatch.setattr(runtime, "build_supervisor", lambda *a, **kw: supervisor)
    original_start = supervisor.start

    def partial_start():
        original_start()
        cancel_setup()

    monkeypatch.setattr(supervisor, "start", partial_start)
    try:
        with pytest.raises(RuntimeError, match="owned fixture setup failure"):
            launcher.run_agent(
                config, queue=old, config_path=tmp_path / "agent.yaml", block=False
            )
        assert owned_worker is not None and owned_worker.is_alive()
        try:
            competitor = StateSession.open(
                tmp_path / "agent.state.json", config.server_id
            )
        except StateError:
            pass
        else:
            competitor.close()
            pytest.fail(
                "launcher handed off the lease before the unregistered reservation ended"
            )
        release.set()
        owned_worker.join(3)
        assert not owned_worker.is_alive() and [e["id"] for e in old.recent()] == [1]
        with pytest.raises(StateError):
            old.add("fixture", "late stale sink")
        newcomer = build_queue(config, tmp_path / "agent.state.json")
        assert [newcomer.add("fixture", str(n))["id"] for n in range(2)] == [2, 3]
        newcomer.close()
        resumed = build_queue(config, tmp_path / "agent.state.json")
        assert resumed.add("fixture", "third owner")["id"] == 4
        resumed.close()
        primary, anchor, _ = state_paths(tmp_path / "agent.state.json")
        assert state_module.read_record(primary) == state_module.read_record(anchor)
        assert state_module.read_record(primary).next_event_id == 5
    finally:
        release.set()
        supervisor.stop(timeout=1)
        if owned_worker is not None:
            owned_worker.join(3)
        old.close()
        assert owned_worker is None or not owned_worker.is_alive()


def test_closing_rejects_add_already_waiting_for_queue_lock(tmp_path, monkeypatch):
    config, queue = trusted_queue(tmp_path)
    entered, release, waiter_checked = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    results = []
    original_write, original_closed = state_module.write_pair, queue._closed.is_set

    def stalled(path, record):
        entered.set()
        assert release.wait(10), "owned fixture deadline"
        original_write(path, record)

    def observe_admission():
        closed = original_closed()
        if threading.current_thread().name == "owned-queued-add":
            waiter_checked.set()
        return closed

    monkeypatch.setattr(state_module, "write_pair", stalled)
    monkeypatch.setattr(queue._closed, "is_set", observe_admission)

    def admitted():
        results.append(("old", queue.add("fixture", "admitted")["id"]))

    def waiting():
        try:
            queue.add("fixture", "waited before close")
            results.append(("waiting", "published"))
        except StateError:
            results.append(("waiting", "refused"))

    writer = threading.Thread(target=admitted)
    waiter = threading.Thread(target=waiting, name="owned-queued-add")
    writer.start()
    try:
        assert entered.wait(2)
        waiter.start()
        assert waiter_checked.wait(2) and waiter.is_alive()
        queue.close()
        release.set()
    finally:
        release.set()
        writer.join(3)
        if waiter.ident is not None:
            waiter.join(3)
        queue.close()
    assert not writer.is_alive() and not waiter.is_alive()
    assert sorted(results) == [("old", 1), ("waiting", "refused")]
    assert queue.next_id == 2 and len(queue.recent()) == 1
    monkeypatch.setattr(state_module, "write_pair", original_write)
    resumed = build_queue(config, tmp_path / "agent.state.json")
    try:
        assert resumed.add("fixture", "new owner")["id"] == 2
    finally:
        resumed.close()


@pytest.mark.parametrize("role", ["agent", "hub"])
@pytest.mark.parametrize("failure", ["second_claim", "bootstrap"])
def test_merge244_early_failure_releases_both_socket_and_state_owners(
    tmp_path, monkeypatch, role, failure
):
    from unittest.mock import Mock

    from taskpaw_v3.agent.server import launcher
    from taskpaw_v3.core import control, net
    from taskpaw_v3.core.config import HubConfig
    from taskpaw_v3.core.state import FileLease, db_lease_path
    from taskpaw_v3.hub.server import app

    sockets = []

    def claim(*args):
        if failure == "second_claim" and len(sockets) == 1:
            raise net.PortInUseError("owned second claim")
        sock = Mock()
        sockets.append(sock)
        return sock

    def bootstrap(*args):
        raise RuntimeError("owned bootstrap failure")

    target = launcher if role == "agent" else net
    monkeypatch.setattr(
        target, "reclaim_ports_from_stale_instance", lambda *a, **kw: None
    )
    monkeypatch.setattr(target, "claim_port", claim)
    monkeypatch.setattr(
        launcher if role == "agent" else control, "bootstrap_control", bootstrap
    )
    error = net.PortInUseError if failure == "second_claim" else RuntimeError
    if role == "agent":
        config = AgentConfig(
            server_id="fixture", machine="fixture", bind_port=15680, control_port=15681
        )
        state = tmp_path / "agent.state.json"
        initialize_state(state, config.server_id)
        with pytest.raises(error):
            launcher.run_agent(
                config,
                state_path=state,
                config_path=tmp_path / "agent.yaml",
                block=False,
            )
        session = StateSession.open(state, config.server_id)
        session.close()
    else:
        store = HubStore(tmp_path / "hub.db")
        try:
            with pytest.raises(error):
                app.run_hub(
                    HubConfig(machine="fixture", bind_port=15690),
                    store,
                    block=False,
                    config_path=tmp_path / "hub.yaml",
                )
            with FileLease(db_lease_path(store.db_path)):
                pass
        finally:
            store.close()
    assert len(sockets) == (1 if failure == "second_claim" else 2)
    for sock in sockets:
        sock.close.assert_called_once()


@pytest.mark.parametrize("control_alive", [False, True])
def test_merge244_control_thread_owns_db_lease_until_actual_exit(
    tmp_path, monkeypatch, control_alive
):
    import sqlite3
    from types import SimpleNamespace
    from unittest.mock import Mock

    from taskpaw_v3.core import net
    from taskpaw_v3.core.config import HubConfig
    from taskpaw_v3.core.lifecycle import GracefulShutdown
    from taskpaw_v3.core.state import FileLease, db_lease_path
    from taskpaw_v3.hub.server import app

    threads = []

    class Thread:
        def __init__(self, *, name, **kwargs):
            self.name, self.ident, self.alive = name, None, False
            threads.append(self)

        def start(self):
            self.ident, self.alive = 7, True

        def is_alive(self):
            return self.alive

        def join(self, timeout):
            self.alive = control_alive and self.name == "hub-control"

    service = SimpleNamespace(
        _thread=None,
        self_supervisor=None,
        start=Mock(),
        stop=Mock(return_value=True),
    )
    monkeypatch.setattr(net, "reclaim_ports_from_stale_instance", lambda *a, **kw: None)
    monkeypatch.setattr(net, "claim_port", lambda *a, **kw: Mock())
    monkeypatch.setattr(net, "announce_ready", lambda *a, **kw: None)
    monkeypatch.setattr(app, "create_hub_app", lambda *a: (object(), service))
    monkeypatch.setattr(app, "create_hub_control_app", lambda *a, **kw: object())
    monkeypatch.setattr(app.threading, "Thread", Thread)
    monkeypatch.setattr(uvicorn, "Config", lambda *a, **kw: None)
    monkeypatch.setattr(
        uvicorn,
        "Server",
        lambda *a, **kw: SimpleNamespace(started=True, should_exit=False),
    )
    store = HubStore(tmp_path / "hub.db")
    close = Mock(wraps=store.close)
    monkeypatch.setattr(store, "close", close)
    shutdown = GracefulShutdown()
    monkeypatch.setattr(shutdown, "install_signal_handlers", lambda: None)
    try:
        app.run_hub(
            HubConfig(self_monitor=False), store, shutdown=shutdown, block=False
        )
        shutdown.shutdown()
        service.stop.assert_called_once()
        assert not threads[0].is_alive()
        if control_alive:
            assert threads[1].is_alive()
            close.assert_not_called()
            assert store.list_servers() == []
            with pytest.raises(StateError):
                FileLease(db_lease_path(store.db_path)).acquire()
        else:
            assert not threads[1].is_alive()
            close.assert_called_once()
            with pytest.raises(sqlite3.ProgrammingError):
                store._conn.execute("SELECT 1")
            with FileLease(db_lease_path(store.db_path)):
                pass
    finally:
        # These are owned simulated threads only; release the deliberately retained
        # lease after the assertions so no native lock survives this fixture.
        for thread in threads:
            thread.alive = False
        service._event_cursor_lease.close()
        store.close()
