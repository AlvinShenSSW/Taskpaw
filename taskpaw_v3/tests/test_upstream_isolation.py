"""R06: admission/atomic disposal and owned-only transport fixtures."""

from __future__ import annotations

import json

import pytest

from taskpaw_v3.hub.server.poller import Poller
from taskpaw_v3.hub.server.store import HubStore


@pytest.mark.parametrize(
    "raw",
    [
        '{"x":NaN}',
        '{"x":Infinity}',
        '{"x":1e999}',
        '{"x":"\\ud800"}',
        '{"monitors":42}',
        '{"x":1,"x":2}',
    ],
)
def test_status_rejects_unpublishable_values(raw):
    assert Poller._parse_status(raw) is None


def test_mixed_bad_id_preserves_valid_neighbors(tmp_path, monkeypatch):
    cursor = {
        "version": 1,
        "durable": True,
        "server_id": "fixture",
        "stream_id": "1" * 32,
        "boot_id": "2" * 32,
        "resume_floor": 0,
        "offered_highwater": 2,
        "next_event_id": 3,
    }

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {
                    "event_cursor": cursor,
                    "events": [
                        {"id": 1, "message": "one"},
                        {"id": "bad"},
                        {"id": 2, "message": "two"},
                    ],
                }
            ).encode()

    store = HubStore(tmp_path / "hub.db")
    try:
        sid = store.add_server("fixture", "127.0.0.1", 5680)
        p = Poller(store, "", lambda: False, lambda: "")
        from taskpaw_v3.tests.test_hub import fake_request

        monkeypatch.setattr(
            p, "_request", lambda req: fake_request(req, lambda *a, **kw: Response())
        )
        assert [
            e["id"]
            for e in p.fetch_events(store.get_server(sid), {"event_cursor": cursor})
        ] == [1, 2]
    finally:
        store.close()


def test_owned_request_deadline_before_stdin_read(monkeypatch):
    import sys
    import time

    from taskpaw_v3.hub.server import upstream_worker as uw

    # Real plain child does not read; a real large request fills its stdin pipe.
    monkeypatch.setattr(
        uw, "worker_argv", lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    transport = uw.Transport()
    started = time.monotonic()
    result = transport.request({"padding": "x" * 30000}, 0.15)
    assert result["reason"] == "upstream_deadline"
    assert time.monotonic() - started < 1.4
    assert transport.clean()


def test_helper_real_owned_http(tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from taskpaw_v3.hub.server.upstream_worker import Transport

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            body = b'{"machine":"owned","monitors":{}}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    transport = Transport()
    try:
        result = transport.request(
            {
                "kind": "status",
                "url": f"http://127.0.0.1:{server.server_port}/status",
                "headers": {"Authorization": "Bearer fake-only"},
                "timeout": 1,
            },
            3,
        )
        assert result["ok"], result
        assert result["status"]["machine"] == "owned"
        assert transport.clean()
    finally:
        transport.cancel()
        server.shutdown()
        server.server_close()
        thread.join(2)


def cursor(highwater=3):
    return {
        "version": 1,
        "durable": True,
        "server_id": "owned",
        "stream_id": "1" * 32,
        "boot_id": "2" * 32,
        "resume_floor": 0,
        "offered_highwater": highwater,
        "next_event_id": highwater + 1,
    }


def bound(store, sid, proof):
    store.commit_event_cursor(
        sid,
        {
            "state": "bound",
            "identity": {k: proof[k] for k in ("server_id", "stream_id")},
            "boot_id": proof["boot_id"],
            "resume_floor": proof["resume_floor"],
        },
        {sid: -1},
    )


@pytest.mark.parametrize("stop,failed", [(False, False), (True, False), (True, True)])
def test_cancel_result_after_owned_cleanup(monkeypatch, stop, failed):
    import sys

    from taskpaw_v3.hub.server import upstream_worker as uw

    monkeypatch.setattr(
        uw,
        "worker_argv",
        lambda: [
            sys.executable,
            "-c",
            "import sys; sys.stdin.buffer.readline(); print('{\"ok\":true}')",
        ],
    )
    transport = uw.Transport()
    cleanup = transport._cleanup
    records = []

    def at_cleanup(rec, cancel):
        records.append(rec)
        assert rec.proc.poll() == 0
        assert rec.reader_done.is_set() and rec.writer_done.is_set()
        if stop:
            transport.cancel()
        return cleanup(rec, cancel) and not failed

    monkeypatch.setattr(transport, "_cleanup", at_cleanup)
    try:
        result = transport.request({}, 3)
        if failed:
            assert result == {"ok": False, "reason": "helper_cleanup_failed"}
            assert transport._owned is records[0] and not transport.clean()
        elif stop:
            assert result == {"ok": False, "reason": "helper_cancelled"}
        else:
            assert result == {"ok": True}
        assert records[0].proc.stdin.closed and records[0].proc.stdout.closed
    finally:
        monkeypatch.setattr(transport, "_cleanup", cleanup)
        transport.cancel()
        assert transport.retry_cleanup() and transport.clean()


@pytest.fixture
def cancellation_poller(tmp_path):
    from taskpaw_v3.hub.server.upstream_worker import canonical, decode_events

    store = HubStore(tmp_path / "hub.db")
    sid = store.add_server("owned", "127.0.0.1", 1)
    store.record_status(sid, canonical({"phase": "previous"}), None)
    poller = Poller(store, "", lambda: False, lambda: "")
    status = {"phase": "next", "event_cursor": cursor(1)}
    batch = decode_events(canonical({"event_cursor": cursor(1), "events": [{"id": 1}]}))
    try:
        yield store, poller, store.get_server(sid), status, batch
    finally:
        poller.stop()
        assert poller.stopped()
        store.close()


def test_cancel_between_delivery_and_status_admission(cancellation_poller, monkeypatch):
    from taskpaw_v3.hub.server.upstream_worker import canonical

    store, poller, server, status, _ = cancellation_poller
    before = dict(poller._status_snapshot[server["id"]])

    def delivered(_server):
        poller.stop()
        return True, canonical(status)

    monkeypatch.setattr(poller, "fetch_status", delivered)
    poller._poll_server(server, False)
    assert store._conn.execute("SELECT count(*) FROM status_log").fetchone() == (1,)
    assert poller._status_snapshot[server["id"]] == before
    assert store.upstream_statuses()[0]["parsed_status"] == {"phase": "previous"}
    assert store.get_event_cursor(server["id"])["state"] == "fresh"


def test_cancel_owned_cleanup_before_status_admission(cancellation_poller, monkeypatch):
    import sys

    from taskpaw_v3.hub.server import upstream_worker as uw

    store, poller, server, status, _ = cancellation_poller
    before = dict(poller._status_snapshot[server["id"]])
    reply = json.dumps({"ok": True, "raw": uw.canonical(status)})
    monkeypatch.setattr(
        uw,
        "worker_argv",
        lambda: [
            sys.executable,
            "-c",
            f"import sys; sys.stdin.buffer.readline(); print({reply!r})",
        ],
    )
    cleanup = poller._transport._cleanup

    def stop_at_cleanup(rec, cancel):
        assert rec.proc.poll() == 0
        assert rec.reader_done.is_set() and rec.writer_done.is_set()
        poller.stop()
        return cleanup(rec, cancel)

    monkeypatch.setattr(poller._transport, "_cleanup", stop_at_cleanup)
    poller._poll_server(server, False)
    assert poller._transport.clean()
    assert store._conn.execute("SELECT count(*) FROM status_log").fetchone() == (1,)
    assert poller._status_snapshot[server["id"]] == before
    assert store.get_event_cursor(server["id"])["state"] == "fresh"


def test_cancel_at_cursor_admission(cancellation_poller, monkeypatch):
    store, poller, server, status, _ = cancellation_poller
    floor = store.event_floor

    def stop_before_admission(*args):
        result = floor(*args)
        poller.stop()
        return result

    monkeypatch.setattr(store, "event_floor", stop_before_admission)
    assert poller.fetch_events(server, status) == []
    assert store.get_event_cursor(server["id"])["state"] == "fresh"
    assert store.read_acks() == {}


@pytest.mark.parametrize("phase", ["cursor", "batch"])
def test_cancel_while_waiting_for_ack_admission(
    cancellation_poller, monkeypatch, phase
):
    import threading

    from taskpaw_v3.hub.server.upstream_worker import canonical

    store, poller, server, status, batch = cancellation_poller
    acquired = threading.Event()
    lock = threading.Lock()
    lock.acquire()

    class WaitingLock:
        def __enter__(self):
            acquired.set()
            lock.acquire()

        def __exit__(self, *args):
            lock.release()

    poller._acks_lock = WaitingLock()
    monkeypatch.setattr(poller, "fetch_status", lambda s: (True, canonical(status)))
    if phase == "batch":
        bound(store, server["id"], cursor(1))
        poller.last_event_ids = {server["id"]: -1}

        def fetched(_server, _status):
            poller._batches[server["id"]] = batch
            poller._channel(server["id"])
            return batch["events"]

        monkeypatch.setattr(poller, "fetch_events", fetched)

        def target():
            return poller._poll_server(server, False)

    else:
        monkeypatch.setattr(poller, "_request", lambda req: batch)

        def target():
            return poller.fetch_events(server, status)

    errors = []

    def run():
        try:
            target()
        except BaseException as exc:
            errors.append(exc)

    caller = threading.Thread(target=run)
    try:
        caller.start()
        assert acquired.wait(3)
        poller.stop()
        lock.release()
        caller.join(3)
        assert not caller.is_alive() and not errors
        assert store.recent_events(server["id"]) == []
        if phase == "cursor":
            assert store.get_event_cursor(server["id"])["state"] == "fresh"
            assert store.read_acks() == {}
        else:
            assert store.read_acks() == {server["id"]: -1}
    finally:
        if lock.locked():
            lock.release()
        caller.join(3)


@pytest.mark.parametrize("stop_after", ["none", "status", "batch"])
def test_cancel_preserves_admitted_effects(
    cancellation_poller, monkeypatch, stop_after
):
    from taskpaw_v3.hub.server.upstream_worker import canonical

    store, poller, server, status, batch = cancellation_poller
    monkeypatch.setattr(
        poller,
        "_request",
        lambda req: (
            {"ok": True, "raw": canonical(status)}
            if req["kind"] == "status"
            else {"ok": True, **batch}
        ),
    )
    method = "record_status" if stop_after == "status" else "commit_upstream_batch"
    original = getattr(store, method)

    def commit_then_cancel(*args):
        result = original(*args)
        if stop_after != "none":
            poller.stop()
        return result

    monkeypatch.setattr(store, method, commit_then_cancel)
    poller._poll_server(server, False)
    assert store.upstream_statuses()[0]["parsed_status"] == status
    assert poller.snapshot_statuses()[server["id"]]["snapshot"] == status
    if stop_after == "status":
        assert store.get_event_cursor(server["id"])["state"] == "fresh"
        assert store.read_acks() == {}
    else:
        assert store.read_acks() == poller.snapshot_acks() == {server["id"]: 1}
        assert [ev["event_id"] for ev in store.recent_events(server["id"])] == [1]


@pytest.mark.parametrize("bad", [True, False, None, 0, -1, "2", 1.5, 1 << 63])
def test_bad_ids_are_receipts_not_batch_failures(bad):
    from taskpaw_v3.hub.server.upstream_worker import decode_events

    result = decode_events(
        json.dumps(
            {
                "event_cursor": cursor(),
                "events": [
                    {"id": 1, "message": "one"},
                    {"id": bad},
                    {"id": 3, "message": "three"},
                ],
            }
        )
    )
    assert [r["id"] for r in result["events"]] == [1, 3]
    assert len(result["receipts"]) == 1
    assert result["receipts"][0]["reason"] == "event_id"
    assert "message" not in result["receipts"][0]


@pytest.mark.parametrize(
    "bad",
    [
        {"id": 2, "message": True},
        {"id": 2, "data": []},
        {"id": 2, "x": float("nan")},
        {"id": 2, "message": "\ud800"},
        {"id": 2, "message": "x" * 32769},
        {"id": 4, "message": "unoffered"},
    ],
)
def test_bad_payload_isolated_and_unique_sorted(bad):
    from taskpaw_v3.hub.server.upstream_worker import decode_events

    result = decode_events(
        json.dumps(
            {
                "event_cursor": cursor(),
                "events": [
                    {"id": 3, "message": "three"},
                    bad,
                    {"id": 1, "message": "one"},
                    {"id": 1, "message": "conflict"},
                    {"id": 2, "message": "two"},
                ],
            }
        )
    )
    assert [r["id"] for r in result["events"]] == [1, 2, 3]
    assert result["events"][0]["message"] == "one"
    reasons = {r["reason"] for r in result["receipts"]}
    assert "event_out_of_order" in reasons and "event_conflicting_duplicate" in reasons
    assert len(result["receipts"]) == 3


def test_duplicate_keys_locality():
    from taskpaw_v3.hub.server.upstream_worker import UpstreamError, decode_events

    body = (
        '{"event_cursor":'
        + json.dumps(cursor())
        + ',"events":[{"id":1,"message":"a","message":"b"},{"id":2,"message":"safe"}]}'
    )
    result = decode_events(body)
    assert [r["id"] for r in result["events"]] == [2]
    assert result["receipts"][0]["reason"] == "duplicate_key"
    with pytest.raises(UpstreamError):
        decode_events(
            '{"events":[],"events":[],"event_cursor":' + json.dumps(cursor()) + "}"
        )


@pytest.mark.parametrize(
    "field",
    [
        "events",
        "delivery_outbox",
        "upstream_quarantine",
        "upstream_consumed",
        "config",
        "commit",
    ],
)
def test_atomic_batch_every_sql_phase_rolls_back(tmp_path, field):
    import sqlite3

    from taskpaw_v3.hub.server.upstream_worker import decode_events

    store = HubStore(tmp_path / "hub.db")
    try:
        sid = store.add_server("owned", "127.0.0.1", 1)
        proof = cursor()
        bound(store, sid, proof)
        batch = decode_events(
            json.dumps(
                {
                    "event_cursor": proof,
                    "events": [
                        {"id": 1, "message": "one"},
                        {"id": "bad"},
                        {"id": 3, "message": "three"},
                    ],
                }
            )
        )
        operation = "UPDATE" if field == "config" else "INSERT"
        condition = " WHEN NEW.key='last_event_ids'" if field == "config" else ""
        if field == "commit":
            # A real deferred FK fails at COMMIT, after every batch write.
            store._conn.execute(
                "CREATE TABLE owned_commit_failure(sid INTEGER REFERENCES servers(id) DEFERRABLE INITIALLY DEFERRED)"
            )
            store._conn.execute(
                "CREATE TRIGGER owned_failure AFTER INSERT ON events BEGIN INSERT INTO owned_commit_failure VALUES(-999); END"
            )
        else:
            store._conn.execute(
                f"CREATE TRIGGER owned_failure BEFORE {operation} ON {field}{condition} BEGIN SELECT RAISE(FAIL,'owned'); END"
            )
        with pytest.raises(sqlite3.DatabaseError):
            store.commit_upstream_batch(store.get_server(sid), batch, True)
        assert store.read_acks() == {sid: -1}
        for table in (
            "events",
            "delivery_outbox",
            "upstream_quarantine",
            "upstream_consumed",
        ):
            assert store._conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (
                0,
            )
        store._conn.execute("DROP TRIGGER owned_failure")
        assert store.commit_upstream_batch(store.get_server(sid), batch, True) == {
            sid: 3
        }
        assert [e["event_id"] for e in reversed(store.recent_events(sid))] == [1, 3]
        assert store._conn.execute(
            "SELECT count(*) FROM delivery_outbox"
        ).fetchone() == (2,)
        store.commit_upstream_batch(store.get_server(sid), batch, True)
        assert store._conn.execute(
            "SELECT count(*) FROM delivery_outbox"
        ).fetchone() == (2,)
    finally:
        store.close()


def test_poison_only_disposal_floor_survives_evidence_retention(tmp_path):
    from taskpaw_v3.hub.server.upstream_worker import decode_events

    store = HubStore(tmp_path / "hub.db")
    try:
        sid = store.add_server("owned", "127.0.0.1", 1)
        proof = cursor(5)
        bound(store, sid, proof)
        bad = [{"id": "bad-" + str(i)} for i in range(300)]
        batch = decode_events(json.dumps({"event_cursor": proof, "events": bad}))
        assert store.commit_upstream_batch(store.get_server(sid), batch, False) == {
            sid: 5
        }
        assert store.quarantine_summary(sid)["retained"] == 256
        assert store.quarantine_summary(sid)["evicted"] == 44
        # No acks/history/outbox inference is needed: consumed floor is permanent.
        store.set_config("last_event_ids", "{}")
        store._conn.execute("DELETE FROM upstream_quarantine")
        store._conn.commit()
        assert store.event_floor(sid, {}) == 5
        assert store.recent_events(sid) == []
        store.update_server(sid, enabled=False)
        report = store.get_event_cursor(sid)
        store.commit_event_cursor(sid, report, {sid: 5}, require_disabled=True)
        assert store.event_floor(sid, {sid: 5}) == 5
        store.remove_server(sid)
        for table in ("upstream_quarantine", "upstream_consumed", "upstream_summary"):
            assert store._conn.execute(f"SELECT count(*) FROM {table}").fetchone() == (
                0,
            )
    finally:
        store.close()


def test_history_recovery_is_bounded_continued_and_prune_safe(tmp_path):
    store = HubStore(tmp_path / "hub.db")
    try:
        sid = store.add_server("owned", "127.0.0.1", 1)
        store.log_status(sid, True, '{"machine":"last-good","monitors":{}}')
        for _ in range(140):
            store.log_status(sid, True, '{"x":NaN}')
        store._conn.execute("UPDATE status_log SET timestamp='2000-01-01 00:00:00'")
        store._conn.commit()
        p = Poller(store, "", lambda: False, lambda: "")
        assert p.snapshot_statuses()[sid]["snapshot"] is None
        assert p.snapshot_statuses()[sid]["status_health"]["state"] == "recovering"
        assert store._conn.execute(
            "SELECT count(*) FROM upstream_quarantine"
        ).fetchone() == (128,)
        assert store.prune_status_logs() == 0
        store.recover_statuses()
        row = store.upstream_statuses()[0]
        assert row["parsed_status"]["machine"] == "last-good"
        assert (
            row["last_good_at"] is None
            and row["error_code"] == "historical_status_invalid"
        )
        assert store.prune_status_logs() == 141
        assert store.upstream_statuses()[0]["parsed_status"]["machine"] == "last-good"
    finally:
        store.close()


def test_bad_history_and_live_agent_cannot_break_fleet_json(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from taskpaw_v3.core.config import HubConfig
    from taskpaw_v3.hub.server.app import create_hub_app
    from taskpaw_v3.hub.server.upstream_worker import handle_request
    from taskpaw_v3.tests.test_hub import FakeResp, fake_request

    store = HubStore(tmp_path / "hub.db")
    try:
        bad = store.add_server("bad", "127.0.0.1", 1)
        good = store.add_server("good", "127.0.0.1", 2)
        store.log_status(bad, True, '{"x":"\\ud800"}')
        app, service = create_hub_app(
            HubConfig(data_dir=str(tmp_path), host_metrics=False), store
        )
        samples = {
            bad: {"machine": "first-good"},
            good: {"machine": "healthy", "monitors": {"host": {"metrics": {"cpu": 1}}}},
        }
        monkeypatch.setattr(
            service.poller,
            "_request",
            lambda req: fake_request(
                req,
                lambda *a, **kw: FakeResp(
                    samples[bad if ":1/" in req["url"] else good]
                ),
            ),
        )
        service.poller.poll_once()
        before = service.poller.snapshot_statuses()[bad]
        samples[bad] = {"x": float("inf")}
        service.poller.poll_once()
        response = TestClient(app).get("/status")
        assert response.status_code == 200
        rows = {r["id"]: r for r in response.json()["servers"]}
        assert rows[good]["snapshot"]["machine"] == "healthy"
        assert rows[bad]["snapshot"] == before["snapshot"]
        assert not rows[bad]["online"]
        assert rows[bad]["status_health"]["error_code"] == "json_nonfinite"
        assert rows[bad]["status_health"]["age_seconds"] >= 0
        assert len(response.content) < 20000
        assert (
            handle_request(
                {"kind": "status", "url": "bad", "headers": {}, "timeout": 1}
            )["reason"]
            == "request_invalid"
        )
    finally:
        store.close()


@pytest.mark.parametrize(
    "payload,reason",
    [
        (b"[" * 33 + b"0" + b"]" * 33, "json_depth"),
        (b'{"x":' + b"1" * 257 + b"}", "json_number_size"),
        (b'{"x":1e999}', "json_nonfinite"),
    ],
)
def test_json_resource_limits(payload, reason):
    from taskpaw_v3.hub.server.upstream_worker import UpstreamError, decode_status

    with pytest.raises(UpstreamError) as exc:
        decode_status(payload)
    assert exc.value.reason == reason


def test_streaming_body_cap_stops_before_read_all():
    from taskpaw_v3.hub.server.upstream_worker import (
        CHUNK_BYTES,
        STATUS_BYTES,
        handle_request,
    )

    class Response:
        headers = {}
        count = 0

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read1(self, n):
            assert 0 < n <= CHUNK_BYTES
            self.count += n
            return b"x" * n

    response = Response()

    class Opener:
        def open(self, *a, **k):
            return response

    result = handle_request(
        {"kind": "status", "url": "http://127.0.0.1:1/", "headers": {}, "timeout": 1},
        Opener(),
    )
    assert result["reason"] == "body_oversize"
    assert response.count == STATUS_BYTES + 1


def test_parent_result_capture_is_independently_bounded(monkeypatch):
    import sys

    from taskpaw_v3.hub.server import upstream_worker as uw

    monkeypatch.setattr(
        uw,
        "worker_argv",
        lambda: [
            sys.executable,
            "-c",
            f'import sys; sys.stdin.buffer.readline(); sys.stdout.buffer.write(b"x"*{uw.REPLY_BYTES + 65536}); sys.stdout.buffer.flush()',
        ],
    )
    transport = uw.Transport()
    result = transport.request({}, 2)
    assert result["reason"] == "helper_output_oversize"
    assert transport.clean()


def test_partial_thread_start_still_reaps_owned_child(monkeypatch):
    import subprocess
    import sys
    import threading

    from taskpaw_v3.hub.server import upstream_worker as uw

    captured = []
    original_popen = subprocess.Popen
    original_start = threading.Thread.start

    def popen(*args, **kwargs):
        proc = original_popen(*args, **kwargs)
        captured.append(proc)
        return proc

    def start(thread):
        if thread.name == "upstream-writer":
            raise RuntimeError("owned setup failure")
        return original_start(thread)

    monkeypatch.setattr(
        uw, "worker_argv", lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    monkeypatch.setattr(uw.subprocess, "Popen", popen)
    monkeypatch.setattr(threading.Thread, "start", start)
    transport = uw.Transport()
    try:
        result = transport.request({}, 0.2)
        assert not result["ok"] and transport.clean()
        assert captured[0].poll() is not None
        assert captured[0].stdin.closed and captured[0].stdout.closed
    finally:
        for proc in captured:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=3)
            proc.stdin.close()
            proc.stdout.close()


def test_cancel_during_popen_publication_is_sticky(monkeypatch):
    import subprocess
    import sys

    from taskpaw_v3.hub.server import upstream_worker as uw

    original = subprocess.Popen
    transport = uw.Transport()
    captured = []

    def create(*args, **kwargs):
        proc = original(*args, **kwargs)
        captured.append(proc)
        transport.cancel()
        transport.cancel()
        return proc

    monkeypatch.setattr(
        uw, "worker_argv", lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    monkeypatch.setattr(uw.subprocess, "Popen", create)
    assert transport.request({}, 0.3)["reason"] == "helper_cancelled"
    assert transport.clean() and captured[0].poll() is not None
    assert transport.request({}, 0.3)["reason"] == "helper_cancelled"
    assert len(captured) == 1


def test_retained_cleanup_denies_replacement_and_can_retry(monkeypatch):
    import sys

    from taskpaw_v3.hub.server import upstream_worker as uw

    monkeypatch.setattr(
        uw, "worker_argv", lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    transport = uw.Transport()
    cleanup = transport._cleanup
    monkeypatch.setattr(
        transport, "_cleanup", lambda rec, cancel: cleanup(rec, cancel) and False
    )
    assert transport.request({}, 0.05)["reason"] == "helper_cleanup_failed"
    record = transport._owned
    assert record is not None and not transport.clean()
    assert transport.request({}, 0.05)["reason"] == "helper_cleanup_failed"
    assert transport._owned is record
    monkeypatch.setattr(transport, "_cleanup", cleanup)
    assert transport.retry_cleanup() and transport.clean()


@pytest.mark.parametrize(
    "mode",
    [
        "success",
        "oversize",
        "header",
        "chunk",
        "drip",
        "body-drip",
        "chunk-drip",
        "drop",
        "redirect",
        "auth",
        "stop",
    ],
)
@pytest.mark.parametrize("frozen", [False, True], ids=["source", "shipped"])
def test_native_upstream_helper_only(tmp_path, monkeypatch, mode, frozen):
    """Only upstream-http: never normal roles, bootstrap, config, DB or ready."""
    import hashlib
    import os
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from pathlib import Path

    from taskpaw_v3.hub.server import upstream_worker as uw

    if frozen:
        supplied = os.environ.get("TASKPAW_TEST_UPSTREAM_BACKEND")
        if not supplied:
            pytest.skip("actual built sidecar not supplied; no frozen acceptance claim")
        binary = Path(supplied).resolve()
        assert binary.is_file()
        monkeypatch.setattr(uw, "worker_argv", lambda: [str(binary), "upstream-http"])
    entered = threading.Event()
    release = threading.Event()
    forwarded = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            entered.set()
            try:
                if self.path == "/forbidden":
                    forwarded.append(self.headers.get("Authorization"))
                    self.send_error(500)
                    return
                if mode in ("redirect", "auth"):
                    self.send_response(302 if mode == "redirect" else 401)
                    if mode == "redirect":
                        self.send_header(
                            "Location",
                            f"http://127.0.0.1:{self.server.server_port}/forbidden",
                        )
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if mode in ("body-drip", "drop"):
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{")
                    self.wfile.flush()
                    if mode == "body-drip":
                        release.wait(15)
                    self.close_connection = True
                    return
                if mode == "chunk-drip":
                    self.wfile.write(
                        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1;"
                    )
                    self.wfile.flush()
                    release.wait(15)
                    return
                if mode in ("drip", "stop"):
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Owned: ")
                    self.wfile.flush()
                    # No bytes after this partial header; parent deadline must win.
                    release.wait(15)
                    return
                if mode == "header":
                    self.wfile.write(
                        b"HTTP/1.1 200 OK\r\nX-Owned: "
                        + b"a" * (uw.LINE_BYTES + 1)
                        + b"\r\n\r\n"
                    )
                    return
                if mode == "chunk":
                    self.wfile.write(
                        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1;"
                        + b"a" * uw.LINE_BYTES
                        + b"\r\nx\r\n0\r\n\r\n"
                    )
                    return
                self.send_response(200)
                if mode == "oversize":
                    self.send_header("Content-Length", str(uw.STATUS_BYTES + 1))
                    self.end_headers()
                    return
                body = b'{"machine":"owned","monitors":{},"extension":{"allowed":true}}'
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    class Server(ThreadingHTTPServer):
        daemon_threads = False

    server = Server(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    transport = uw.Transport()
    results = []
    captured = []
    import subprocess

    import psutil

    original_popen = subprocess.Popen

    def capture(*args, **kwargs):
        proc = original_popen(*args, **kwargs)
        captured.append(proc)
        return proc

    monkeypatch.setattr(uw.subprocess, "Popen", capture)
    request = {
        "kind": "status",
        "url": f"http://127.0.0.1:{server.server_port}/status",
        "headers": {"Authorization": "Bearer fake-r06-only"},
        "timeout": 10,
        "padding": "x" * 30000,
    }
    raw_bytes = len((uw.canonical(request) + "\n").encode())
    assert 30000 < raw_bytes <= uw.REQUEST_BYTES
    started = time.monotonic()
    caller = threading.Thread(
        target=lambda: results.append(transport.request(request, 3))
    )
    try:
        caller.start()
        assert entered.wait(10)
        # Inspect only descendants of this fixture's returned Popen, never a
        # system process scan. Frozen bootloader/interpreter acceptance is real.
        owned_descendants = []
        if frozen:
            try:
                owned_descendants = psutil.Process(captured[0].pid).children(
                    recursive=True
                )
            except psutil.NoSuchProcess:
                pass
        if mode == "stop":
            transport.cancel()
            transport.cancel()
        caller.join(5)
        assert not caller.is_alive() and len(results) == 1
        result = results[0]
        if mode == "success":
            assert result["ok"] and result["status"]["extension"] == {"allowed": True}
        else:
            reason = {
                "oversize": "body_oversize",
                "header": "http_metadata_oversize",
                "chunk": "http_metadata_oversize",
                "drip": "upstream_deadline",
                "body-drip": "upstream_deadline",
                "chunk-drip": "upstream_deadline",
                "drop": "json_invalid",
                "redirect": "http_refused",
                "auth": "http_auth",
                "stop": "helper_cancelled",
            }[mode]
            expected = {"ok": False, "reason": reason}
            if mode in ("redirect", "auth"):
                expected["status_code"] = 302 if mode == "redirect" else 401
            assert result == expected
            assert not forwarded
        assert transport.clean()
        assert captured[0].poll() is not None
        assert captured[0].stdin.closed and captured[0].stdout.closed
        for owned in owned_descendants:
            try:
                owned.wait(timeout=1)
                assert not owned.is_running()
            except psutil.NoSuchProcess:
                pass
        assert time.monotonic() - started < 5
        evidence = {
            "mode": mode,
            "frozen": frozen,
            "result": result.get("reason", "ok"),
            "request_bytes": raw_bytes,
            "clean": transport.clean(),
            "reaped": captured[0].poll() is not None,
            "pipes_closed": captured[0].stdin.closed and captured[0].stdout.closed,
            "observed_owned_descendants": len(owned_descendants),
        }
        if frozen:
            evidence["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
        print("R06_HELPER_NATIVE " + json.dumps(evidence, sort_keys=True))
    finally:
        release.set()
        transport.cancel()
        caller.join(5)
        transport.retry_cleanup()
        server.shutdown()
        server.server_close()
        server_thread.join(3)


# This fixture entry is generated only in an owned pytest directory. It imports
# the unchanged helper and blocks its first read, without a production test hook.
_PREREAD_ENTRY = r"""
import json, os, pathlib, sys, time
import psutil
from taskpaw_v3.hub.server import upstream_worker
release, ready = map(pathlib.Path, sys.argv[1:3])
original = sys.stdin
class FirstRead:
    buffer = None
    def __init__(self):
        self.buffer = self
    def fileno(self):
        return original.fileno()
    def readline(self, size):
        ready.write_text(json.dumps({"pid": os.getpid(), "created": psutil.Process().create_time(), "consumed": 0}))
        while not release.exists():
            time.sleep(.01)
        return original.buffer.readline(size)
sys.stdin = FirstRead()
raise SystemExit(upstream_worker.main())
"""


def _windows_preread_popen(original, captured):
    """Fixture-owned 4KiB raw anonymous pipe, same production I/O owners."""
    import ctypes
    import io
    import msvcrt
    import os
    from ctypes import wintypes as w

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)

    class SA(ctypes.Structure):
        _fields_ = [("length", w.DWORD), ("descriptor", w.LPVOID), ("inherit", w.BOOL)]

    kernel.CreatePipe.argtypes = [
        ctypes.POINTER(w.HANDLE),
        ctypes.POINTER(w.HANDLE),
        ctypes.POINTER(SA),
        w.DWORD,
    ]
    kernel.CreatePipe.restype = w.BOOL
    kernel.GetNamedPipeInfo.argtypes = [
        w.HANDLE,
        ctypes.POINTER(w.DWORD),
        ctypes.POINTER(w.DWORD),
        ctypes.POINTER(w.DWORD),
        ctypes.POINTER(w.DWORD),
    ]
    kernel.GetNamedPipeInfo.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.CloseHandle.restype = w.BOOL

    def create(*args, **kwargs):
        read, write = w.HANDLE(), w.HANDLE()
        attrs = SA(ctypes.sizeof(SA), None, True)
        assert kernel.CreatePipe(
            ctypes.byref(read), ctypes.byref(write), ctypes.byref(attrs), 4096
        ), ctypes.get_last_error()
        read_file = write_file = None
        try:
            outgoing, incoming = w.DWORD(), w.DWORD()
            assert kernel.GetNamedPipeInfo(
                read, None, ctypes.byref(outgoing), ctypes.byref(incoming), None
            ), ctypes.get_last_error()
            capacity = max(outgoing.value, incoming.value)
            assert 0 < capacity < 30000, "real backpressure not established"
            read_file = io.FileIO(
                msvcrt.open_osfhandle(read.value, os.O_RDONLY | os.O_BINARY),
                "rb",
                closefd=True,
            )
            read = w.HANDLE()
            write_file = io.FileIO(
                msvcrt.open_osfhandle(write.value, os.O_WRONLY | os.O_BINARY),
                "wb",
                closefd=True,
            )
            write = w.HANDLE()
            os.set_inheritable(write_file.fileno(), False)
            kwargs["stdin"] = read_file
            proc = original(*args, **kwargs)
            proc.stdin = write_file
            captured.append((proc, capacity))
            write_file = None  # now owned by production writer/cleanup
            return proc
        finally:
            if read_file is not None:
                read_file.close()
            if write_file is not None:
                write_file.close()
            if read.value:
                kernel.CloseHandle(read)
            if write.value:
                kernel.CloseHandle(write)

    return create


@pytest.fixture(scope="module")
def frozen_preread_fixture(tmp_path_factory):
    import os
    import subprocess
    import sys
    from pathlib import Path

    from taskpaw_v3.core.control import without_control_env
    from taskpaw_v3.core.llm import without_llm_env

    if os.name != "nt" or os.environ.get("TASKPAW_TEST_UPSTREAM_PREREAD_FROZEN") != "1":
        pytest.skip(
            "genuine Windows frozen preread fixture requires explicit native build gate"
        )
    root = tmp_path_factory.mktemp("r06-preread-frozen")
    entry = root / "fixture_entry.py"
    entry.write_text(_PREREAD_ENTRY, encoding="utf-8")
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--onefile",
            "--name",
            "r06-upstream-preread-fixture",
            "--distpath",
            str(root / "dist"),
            "--workpath",
            str(root / "build"),
            "--specpath",
            str(root / "spec"),
            "--paths",
            str(repo),
            str(entry),
        ],
        cwd=repo,
        env=without_control_env(without_llm_env()),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=180,
    )
    assert result.returncode == 0, "genuine preread fixture build failed"
    binary = root / "dist" / "r06-upstream-preread-fixture.exe"
    assert binary.is_file()
    return binary


@pytest.mark.skipif(
    __import__("os").name != "nt", reason="actual Windows anonymous pipe required"
)
@pytest.mark.parametrize("frozen", [False, True], ids=["source", "frozen-preread"])
@pytest.mark.parametrize("case", ["deadline", "stop", "success", "error"])
def test_windows_preread_backpressure(tmp_path, monkeypatch, request, frozen, case):
    import hashlib
    import subprocess
    import sys
    import threading
    import time

    import psutil

    from taskpaw_v3.hub.server import upstream_worker as uw

    release, ready = tmp_path / "release", tmp_path / "ready.json"
    if frozen:
        binary = request.getfixturevalue("frozen_preread_fixture")
        argv = [str(binary), str(release), str(ready)]
    else:
        entry = tmp_path / "fixture_entry.py"
        from pathlib import Path

        entry.write_text(
            _PREREAD_ENTRY.replace(
                "from taskpaw_v3.hub",
                "sys.path.insert(0, "
                + repr(str(Path(__file__).resolve().parents[2]))
                + ")\nfrom taskpaw_v3.hub",
            ),
            encoding="utf-8",
        )
        argv = [sys.executable, str(entry), str(release), str(ready)]
    monkeypatch.setattr(uw, "worker_argv", lambda: argv)
    captured = []
    monkeypatch.setattr(
        uw.subprocess, "Popen", _windows_preread_popen(subprocess.Popen, captured)
    )
    transport = uw.Transport()
    results = []
    server = server_thread = None
    payload = {
        "padding": "x" * 32000,
        "kind": "invalid",
        "url": "http://127.0.0.1/",
        "headers": {},
        "timeout": 1,
    }
    if case == "success":
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                body = b'{"owned":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server_thread = threading.Thread(target=server.serve_forever)
        server_thread.start()
        payload.update(
            kind="status", url=f"http://127.0.0.1:{server.server_port}/status"
        )
    input_bytes = len((uw.canonical(payload) + "\n").encode())
    assert 32000 < input_bytes <= uw.REQUEST_BYTES
    caller = threading.Thread(
        target=lambda: results.append(transport.request(payload, 10))
    )
    marked = None
    started = time.monotonic()
    try:
        caller.start()
        while not ready.exists() and time.monotonic() - started < 8:
            time.sleep(0.01)
        assert ready.exists(), "real helper first-read barrier not reached"
        marked = json.loads(ready.read_text())
        assert marked["consumed"] == 0
        rec = transport._owned
        assert rec is not None and not rec.writer_done.is_set()
        assert captured[0][1] < 32000 <= uw.REQUEST_BYTES
        assert any(t.name == "upstream-writer" and t.is_alive() for t in rec.threads)
        if case == "stop":
            transport.cancel()
            transport.cancel()
        elif case in ("success", "error"):
            release.touch()
        caller.join(12)
        assert not caller.is_alive() and len(results) == 1
        result = results[0]
        expected = {
            "deadline": "upstream_deadline",
            "stop": "helper_cancelled",
            "success": "ok",
            "error": "request_invalid",
        }[case]
        reason = result.get("reason", "ok" if result.get("ok") else "invalid_result")
        assert reason in (expected, "helper_cleanup_failed")
        if case == "success" and reason == "ok":
            assert result["status"] == {"owned": True}
        try:
            owned = psutil.Process(marked["pid"])
            inner_alive = (
                owned.is_running() and owned.create_time() == marked["created"]
            )
        except psutil.NoSuchProcess:
            inner_alive = False
        if reason == "helper_cleanup_failed":
            assert not transport.clean() and transport._owned is rec
            assert transport.request({}, 0.01)["reason"] in (
                "helper_cleanup_failed",
                "helper_cancelled",
            )
        else:
            assert transport.clean() and not inner_alive
        print(
            "R06_PREREAD_NATIVE "
            + json.dumps(
                {
                    "frozen": frozen,
                    "case": case,
                    "capacity": captured[0][1],
                    "request_bytes": input_bytes,
                    "result": reason,
                    "inner_alive": inner_alive,
                    "clean": transport.clean(),
                    "source_sha256": hashlib.sha256(
                        __import__("pathlib").Path(uw.__file__).read_bytes()
                    ).hexdigest(),
                    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()
                    if frozen
                    else None,
                    "architecture": __import__("platform").machine(),
                    "python": sys.version.split()[0],
                    "popen_pid": captured[0][0].pid,
                    "owned_pid": marked["pid"],
                    "owned_created": marked["created"],
                    "elapsed": round(time.monotonic() - started, 4),
                    "joined": not any(t.is_alive() for t in rec.threads),
                    "keeper_closed": rec.keeper < 0,
                    "pipes_closed": captured[0][0].stdin.closed
                    and captured[0][0].stdout.closed,
                    "popen_reaped": captured[0][0].poll() is not None,
                },
                sort_keys=True,
            )
        )
        # Required native cleanup acceptance is distinct from the retained
        # negative safety invariant above. A live wrapper/interpreter is RED.
        assert reason == expected and transport.clean() and not inner_alive
    finally:
        # Only this fixture's barrier and PID: no process scan or production tree control.
        release.touch()
        if marked:
            try:
                owned = psutil.Process(marked["pid"])
                if owned.create_time() == marked["created"]:
                    owned.wait(timeout=3)
            except psutil.TimeoutExpired:
                owned.kill()
                owned.wait(timeout=3)
            except psutil.NoSuchProcess:
                pass
        caller.join(12)
        transport.cancel()
        assert transport.retry_cleanup() and transport.clean()
        for proc, _ in captured:
            assert proc.poll() is not None and proc.stdin.closed and proc.stdout.closed
        if server is not None:
            server.shutdown()
            server.server_close()
            server_thread.join(3)


@pytest.mark.parametrize(
    "monitors",
    [
        {"one": {"metrics": {"cpu": 1}, "unknown": [True, None]}},
        [{"state": "ok", "metrics": {}}],
    ],
)
def test_status_dict_list_and_safe_additive_fields_remain_legal(monitors):
    from taskpaw_v3.hub.server.upstream_worker import decode_status

    value = {"monitors": monitors, "future": {"rich": [1, "x", False]}}
    assert decode_status(json.dumps(value))[0] == value


def test_status_exact_cap_and_node_caps():
    from taskpaw_v3.hub.server import upstream_worker as uw

    body = b'{"x":"' + b"x" * (uw.STATUS_BYTES - 8) + b'"}'
    assert len(body) == uw.STATUS_BYTES
    assert len(uw.decode_status(body)[1].encode()) == uw.STATUS_BYTES
    with pytest.raises(uw.UpstreamError, match="body_oversize"):
        uw.decode_status(body + b" ")
    with pytest.raises(uw.UpstreamError, match="json_nodes"):
        uw.decode_status(json.dumps({"x": [0] * 20000}))


def test_event_count_item_size_and_node_limits():
    from taskpaw_v3.hub.server import upstream_worker as uw

    proof = cursor(10001)
    with pytest.raises(uw.UpstreamError, match="event_count"):
        uw.decode_events(
            json.dumps(
                {"event_cursor": proof, "events": [{"id": i} for i in range(1, 10002)]}
            )
        )
    for event, reason in [
        ({"id": 1, "extra": "x" * uw.ITEM_BYTES}, "event_size"),
        ({"id": 1, "extra": [0] * 4096}, "json_nodes"),
    ]:
        batch = uw.decode_events(
            json.dumps({"event_cursor": proof, "events": [event, {"id": 2}]})
        )
        assert batch["events"] == [{"id": 2}]
        assert batch["receipts"][0]["reason"] == reason
    good = {
        "id": 1,
        "extra": "x"
        * (uw.ITEM_BYTES - len(uw.canonical({"id": 1, "extra": ""}).encode())),
    }
    assert len(uw.canonical(good).encode()) == uw.ITEM_BYTES
    assert uw.decode_events(json.dumps({"event_cursor": proof, "events": [good]}))[
        "events"
    ] == [good]


def test_cumulative_header_and_chunk_terminator_budget():
    import io

    from taskpaw_v3.hub.server import upstream_worker as uw

    fp = uw._MetadataReader(io.BytesIO((b"x" * 7000 + b"\r\n") * 10))
    for _ in range(9):
        fp.readline()
    with pytest.raises(uw.UpstreamError, match="http_metadata_oversize"):
        fp.readline()
    fp = uw._MetadataReader(io.BytesIO(b"\r\n"))
    fp.used = uw.METADATA_BYTES - 1
    with pytest.raises(uw.UpstreamError, match="http_metadata_oversize"):
        fp.read(2)


def test_expiry_global_retention_summary_and_saturation(tmp_path):
    from taskpaw_v3.hub.server.upstream_worker import MAX_COUNTER, decode_events

    store = HubStore(tmp_path / "hub.db")
    try:
        ids = []
        for n in range(17):
            sid = store.add_server("owned-" + str(n), "127.0.0.1", 1)
            ids.append(sid)
            proof = cursor(1)
            bound(store, sid, proof)
            batch = decode_events(
                json.dumps(
                    {
                        "event_cursor": proof,
                        "events": [{"id": "bad-" + str(i)} for i in range(256)],
                    }
                )
            )
            store.commit_upstream_batch(store.get_server(sid), batch, False)
        assert store._conn.execute(
            "SELECT count(*) FROM upstream_quarantine"
        ).fetchone() == (4096,)
        assert sum(store.quarantine_summary(sid)["evicted"] for sid in ids) == 256
        sid = ids[-1]
        store._conn.execute(
            "UPDATE upstream_quarantine SET last_seen='2000-01-01T00:00:00+00:00' WHERE server_id=?",
            (sid,),
        )
        store._conn.execute(
            "INSERT OR REPLACE INTO upstream_summary VALUES(?,?,'2000-01-01T00:00:00+00:00','event_id')",
            (sid, MAX_COUNTER),
        )
        store._conn.commit()
        store.commit_upstream_batch(
            store.get_server(sid),
            {"proof": cursor(1), "events": [], "receipts": []},
            False,
        )
        summary = store.quarantine_summary(sid)
        assert summary["retained"] == 0 and summary["evicted"] == MAX_COUNTER
        assert store.event_floor(sid, {}) == 1
    finally:
        store.close()


def test_restart_corrupt_cache_and_legacy_oversize_never_publish(tmp_path):
    store = HubStore(tmp_path / "hub.db")
    sid = store.add_server("owned", "127.0.0.1", 1)
    store.log_status(sid, True, '{"machine":"legacy-good"}')
    store.log_status(sid, True, '{"x":"' + "x" * (256 * 1024) + '"}')
    store.record_status(sid, '{"machine":"current"}', None)
    store._conn.execute(
        "UPDATE upstream_status SET status_json='{\"x\":NaN}' WHERE server_id=?", (sid,)
    )
    store._conn.commit()
    store.close()
    store = HubStore(tmp_path / "hub.db")
    try:
        poller = Poller(store, "", lambda: False, lambda: "")
        snap = poller.snapshot_statuses()[sid]
        assert snap["snapshot"] == {"machine": "current"}
        assert snap["status_health"]["last_good_at"] is None
        assert snap["status_health"]["age_seconds"] is None
        assert snap["status_health"]["error_code"] == "status_store_invalid"
        assert not snap["online"]
    finally:
        store.close()


def test_utc_restart_age_clock_rollback_and_monotonic_runtime(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from taskpaw_v3.hub.server import poller as module

    store = HubStore(tmp_path / "hub.db")
    try:
        sid = store.add_server("owned", "127.0.0.1", 1)
        store.record_status(sid, "{}", None)
        now = datetime.now(timezone.utc)
        monkeypatch.setattr(module, "_now", lambda: now - timedelta(days=1))
        poller = Poller(store, "", lambda: False, lambda: "")
        health = poller.snapshot_statuses()[sid]["status_health"]
        assert health["age_seconds"] is None and health["error_code"] == "clock_changed"
        monkeypatch.setattr(module.time, "monotonic", lambda: 15)
        assert (
            Poller._health({"status_json": "{}", "good_monotonic": 10})["age_seconds"]
            == 5
        )
    finally:
        store.close()


def test_early_cursor_seed_bad_json_does_not_abort_store(tmp_path):
    path = tmp_path / "hub.db"
    store = HubStore(path)
    sid = store.add_server("owned", "127.0.0.1", 1)
    store.log_status(sid, True, '{"x":NaN}')
    store._conn.execute("DROP TABLE event_cursors")
    store._conn.commit()
    store.close()
    store = HubStore(path)
    try:
        assert store.get_event_cursor(sid)["state"] == "unverified"
        assert store.get_event_cursor(sid)["identity"] is None
    finally:
        store.close()


def test_global_history_python_admission_budget(tmp_path, monkeypatch):
    from taskpaw_v3.hub.server import upstream_worker as uw

    path = tmp_path / "hub.db"
    store = HubStore(path)
    for n in range(12):
        sid = store.add_server("owned-" + str(n), "127.0.0.1", 1)
        store.log_status(sid, True, '{"x":"' + "x" * (uw.STATUS_BYTES - 8) + '"}')
    store._conn.execute("DROP TABLE event_cursors")
    store._conn.commit()
    store.close()
    original = uw.decode_status
    admitted = []

    def decode(raw):
        admitted.append(len(raw))
        return original(raw)

    monkeypatch.setattr(uw, "decode_status", decode)
    store = HubStore(path)
    try:
        store.recover_statuses()
        assert sum(admitted) <= 2 * 1024 * 1024
        # Next tick's separate budget recovers samples left after the first turn.
        admitted.clear()
        store.recover_statuses()
        assert sum(admitted) <= 2 * 1024 * 1024
        assert any(
            row["parsed_status"] is not None for row in store.upstream_statuses()
        )
    finally:
        store.close()
