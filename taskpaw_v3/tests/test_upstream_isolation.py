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


def _capture_upstream_process(monkeypatch, captured, callback=None, phases=None):
    import os
    import time

    from taskpaw_v3.hub.server import upstream_worker as uw

    if os.name == "nt":
        original = uw._WindowsProcess.start

        def start(proc, *args, **kwargs):
            captured.append(proc)
            if phases is not None:
                phases["spawn_enter"] = time.monotonic()
            original(proc, *args, **kwargs)
            if phases is not None:
                phases["spawn_return"] = time.monotonic()
            if callback:
                callback()

        monkeypatch.setattr(uw._WindowsProcess, "start", start)
    else:
        original = uw.subprocess.Popen

        def popen(*args, **kwargs):
            if phases is not None:
                phases["spawn_enter"] = time.monotonic()
            proc = original(*args, **kwargs)
            captured.append(proc)
            if phases is not None:
                phases["spawn_return"] = time.monotonic()
            if callback:
                callback()
            return proc

        monkeypatch.setattr(uw.subprocess, "Popen", popen)


def _observe_upstream_transport(monkeypatch, transport, phases, snapshots):
    import time

    import psutil

    from taskpaw_v3.hub.server import upstream_worker as uw

    write, read, cleanup = transport._write, transport._read, transport._cleanup

    class ObservedPipe:
        def __init__(self, pipe):
            self.pipe = pipe

        def read(self, size):
            part = self.pipe.read(size)
            if part:
                phases.setdefault("first_stdout", time.monotonic())
            return part

        def __getattr__(self, name):
            return getattr(self.pipe, name)

    def writer(rec, raw):
        try:
            write(rec, raw)
        finally:
            phases["writer_complete"] = time.monotonic()

    def reader(rec):
        rec.proc.stdout = ObservedPipe(rec.proc.stdout)
        try:
            read(rec)
        finally:
            phases["reader_complete"] = time.monotonic()

    def snapshot(rec):
        owned = []
        try:
            if rec.proc.pid <= 0:
                raise psutil.NoSuchProcess(rec.proc.pid)
            parent = psutil.Process(rec.proc.pid)
            owned = [
                {"pid": p.pid, "created": p.create_time()}
                for p in [parent, *parent.children(recursive=True)]
            ]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
        native = isinstance(rec.proc, uw._WindowsProcess)
        active = None
        if native and rec.proc.job is not None:
            try:
                active = rec.proc.api.active(rec.proc.job)
            except OSError:
                pass  # Observation cannot change the cleanup outcome.
        return {
            "owned_identities": owned,
            "joined": not any(t.is_alive() for t in rec.threads),
            "keeper_closed": rec.keeper < 0,
            "pipes_closed": all(
                p is None or p.closed for p in (rec.proc.stdin, rec.proc.stdout)
            ),
            "reaped": rec.proc.returncode is not None,
            "job_active": active,
            "native_handles_closed": native
            and all(
                getattr(rec.proc, name) is None for name in ("process", "thread", "job")
            )
            and not rec.proc.handles
            and not rec.proc.fds
            and all(m.closed for m in rec.proc.members),
            "member_coverage": len([m for m in rec.proc.members if m.valid])
            if native
            else None,
            "final_accounting": rec.proc.final_accounting if native else None,
            "member_receipts": [
                {
                    "pid": m.pid,
                    "created": m.created,
                    "valid": m.valid,
                    "signaled": m.signaled,
                    "closed": m.closed,
                }
                for m in rec.proc.members
            ]
            if native
            else None,
        }

    def observed_cleanup(rec, cancel):
        snapshots["before_cleanup"] = snapshot(rec)
        phases["cleanup_enter"] = time.monotonic()
        try:
            return cleanup(rec, cancel)
        finally:
            phases["cleanup_return"] = time.monotonic()
            snapshots["after_cleanup"] = snapshot(rec)

    monkeypatch.setattr(transport, "_write", writer)
    monkeypatch.setattr(transport, "_read", reader)
    monkeypatch.setattr(transport, "_cleanup", observed_cleanup)


def test_partial_thread_start_still_reaps_owned_child(monkeypatch):
    import sys
    import threading

    from taskpaw_v3.hub.server import upstream_worker as uw

    captured = []
    original_start = threading.Thread.start

    def start(thread):
        if thread.name == "upstream-writer":
            raise RuntimeError("owned setup failure")
        return original_start(thread)

    monkeypatch.setattr(
        uw, "worker_argv", lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    _capture_upstream_process(monkeypatch, captured)
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
    import sys

    from taskpaw_v3.hub.server import upstream_worker as uw

    transport = uw.Transport()
    captured = []

    def cancel():
        transport.cancel()
        transport.cancel()

    monkeypatch.setattr(
        uw, "worker_argv", lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    _capture_upstream_process(monkeypatch, captured, cancel)
    assert transport.request({}, 0.3)["reason"] == "helper_cancelled"
    assert transport.clean() and captured[0].poll() is not None
    assert transport.request({}, 0.3)["reason"] == "helper_cancelled"
    assert len(captured) == 1


@pytest.mark.parametrize("assigned", [False, True])
@pytest.mark.parametrize(
    "failure", ["terminate", "query", "close", "after-close", None]
)
def test_windows_owned_cleanup_failure_retains_exact_record(assigned, failure):
    """Hermetic model: an exited launcher is not proof its job is empty."""
    from taskpaw_v3.hub.server import upstream_worker as uw

    class Native:
        def __init__(self):
            self.failure = failure
            self.live = True
            self.closed = []
            self.killed = []

        def poll(self, handle):
            return 0 if assigned or not self.live else None

        def wait(self, handle, timeout):
            if self.live and not assigned:
                raise __import__("subprocess").TimeoutExpired(
                    "owned unassigned", timeout
                )
            return 0

        def kill(self, handle):
            self.killed.append(("process", handle))
            if self.failure == "terminate":
                raise OSError("owned termination failed")
            self.live = False

        def terminate_job(self, handle):
            self.killed.append(("job", handle))
            if handle is None:
                raise OSError("NULL is not an owned job handle")
            if self.failure == "terminate":
                raise OSError("owned termination failed")
            self.live = False

        def identity(self, handle, job):
            return 17, 100

        def members(self, job):
            return [17]

        def accounting(self, job):
            if self.failure == "query":
                raise OSError("owned query failed")
            return 1, int(self.live and assigned)

        def active(self, handle):
            return self.accounting(handle)[1]

        def close(self, handle):
            if self.failure == "close":
                raise OSError("owned closure failed")
            self.closed.append(handle)

    native = Native()
    proc = uw._WindowsProcess(native)
    proc.process, proc.thread, proc.job = 11, 12, 13
    proc.pid, proc.assigned = 17, assigned
    rec = uw._Owned(proc, -1)
    transport = uw.Transport()
    transport._owned = rec
    cleanup = transport._cleanup
    if failure == "after-close":
        transport._cleanup = lambda rec, cancel: cleanup(rec, cancel) and False
    if failure:
        assert not transport.retry_cleanup() and transport._owned is rec
        assert transport.request({}, 1)["reason"] == "helper_cleanup_failed"
        native.failure = None
        transport._cleanup = cleanup
    assert transport.retry_cleanup() and transport.clean()
    assert not native.live
    assert native.killed[0] == (
        "job" if assigned else "process",
        13 if assigned else 11,
    )
    assert sorted(native.closed) == [11, 12, 13]
    assert all(handle is not None for _, handle in native.killed)
    # Fixtures and subprocess consumers may inspect after native handles close.
    assert proc.poll() == proc.wait(timeout=0) == proc.returncode == 0


@pytest.mark.skipif(
    __import__("os").name == "nt", reason="existing POSIX launch expiry"
)
def test_expiry_during_posix_creation_preserves_existing_reason(monkeypatch):
    import sys

    from taskpaw_v3.hub.server import upstream_worker as uw

    captured = []
    _capture_upstream_process(monkeypatch, captured)
    monkeypatch.setattr(
        uw, "worker_argv", lambda: [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    transport = uw.Transport()
    assert transport.request({}, 0) == {"ok": False, "reason": "helper_cancelled"}
    assert transport.clean() and captured[0].poll() is not None
    assert captured[0].stdin.closed and captured[0].stdout.closed


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
    prefix_flushed = threading.Event()
    release = threading.Event()
    forwarded = []
    phases, snapshots = {}, {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            phases["handler_enter"] = time.monotonic()
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
                        phases["prefix_flushed"] = time.monotonic()
                        prefix_flushed.set()
                        release.wait(15)
                    self.close_connection = True
                    return
                if mode == "chunk-drip":
                    self.wfile.write(
                        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n1;"
                    )
                    self.wfile.flush()
                    phases["prefix_flushed"] = time.monotonic()
                    prefix_flushed.set()
                    release.wait(15)
                    return
                if mode in ("drip", "stop"):
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nX-Owned: ")
                    self.wfile.flush()
                    phases["prefix_flushed"] = time.monotonic()
                    prefix_flushed.set()
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
            finally:
                try:
                    self.wfile.flush()
                    phases["http_response_complete"] = time.monotonic()
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
    import psutil

    _capture_upstream_process(monkeypatch, captured, phases=phases)
    _observe_upstream_transport(monkeypatch, transport, phases, snapshots)
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
    adverse = mode in ("drip", "body-drip", "chunk-drip", "stop")
    # Cycle7 explicitly gives cold shipped adverse phases the production default;
    # source adverse controls retain 3s. No deadline is reset after readiness.
    parent_budget = 3 if adverse and not frozen else 5
    cleanup_grace, scheduling_margin = (
        1,
        1,
    )  # Existing lifecycle room, not a new budget.
    elapsed_bound = parent_budget + cleanup_grace + scheduling_margin if adverse else 5
    test_end = started + elapsed_bound

    def call():
        try:
            phases["request_entry"] = time.monotonic()
            results.append(transport.request(request, parent_budget))
        finally:
            phases["caller_complete"] = time.monotonic()

    caller = threading.Thread(target=call)
    try:
        caller.start()
        assert entered.wait(max(0, test_end - time.monotonic()))
        original_deadline = phases["request_entry"] + parent_budget
        if adverse:
            assert prefix_flushed.wait(max(0, test_end - time.monotonic()))
            assert phases["handler_enter"] < original_deadline
            assert phases["prefix_flushed"] < original_deadline
            assert not release.is_set()
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
            phases["cancel_admitted"] = time.monotonic()
            assert phases["cancel_admitted"] < original_deadline
            transport.cancel()
            transport.cancel()
        caller_end = test_end
        if mode == "stop":
            caller_end = min(
                caller_end,
                phases["cancel_admitted"] + cleanup_grace + scheduling_margin,
            )
        caller.join(max(0, caller_end - time.monotonic()))
        assert not caller.is_alive() and len(results) == 1
        assert phases["caller_complete"] - phases["request_entry"] < elapsed_bound
        if adverse:
            assert not release.is_set() and "http_response_complete" not in phases
        if mode == "stop":
            assert (
                phases["caller_complete"] - phases["cancel_admitted"]
                < cleanup_grace + scheduling_margin
            )
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
    finally:
        # Emit even when the handler barrier/result oracle fails, before release
        # or any fixture teardown changes the observed lifecycle.
        evidence = {
            "mode": mode,
            "frozen": frozen,
            "result": results[0].get("reason", "ok") if results else None,
            "request_bytes": raw_bytes,
            "clean": transport.clean(),
            "parent_budget": parent_budget,
            "deadline": parent_budget,
            "elapsed": (
                phases.get("caller_complete", time.monotonic())
                - phases.get("request_entry", started)
            ),
            "elapsed_bound": elapsed_bound,
            "cleanup_grace": cleanup_grace,
            "scheduling_margin": scheduling_margin,
            "release_set": release.is_set(),
            "caller_alive": caller.is_alive(),
            "phases": {
                name: phases.get(name) - started if name in phases else None
                for name in (
                    "request_entry",
                    "spawn_enter",
                    "spawn_return",
                    "writer_complete",
                    "handler_enter",
                    "prefix_flushed",
                    "cancel_admitted",
                    "http_response_complete",
                    "first_stdout",
                    "reader_complete",
                    "cleanup_enter",
                    "cleanup_return",
                    "caller_complete",
                )
            },
            **snapshots,
            "architecture": __import__("platform").machine(),
            "python": __import__("sys").version.split()[0],
            "source_sha256": hashlib.sha256(
                __import__("pathlib").Path(uw.__file__).read_bytes()
            ).hexdigest(),
        }
        if frozen:
            evidence["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
        print("R06_HELPER_NATIVE " + json.dumps(evidence, sort_keys=True))
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
        temporary = ready.with_name(ready.name + ".tmp")
        try:
            temporary.write_text(json.dumps({"pid": os.getpid(), "created": psutil.Process().create_time(), "consumed": 0}), encoding="utf-8")
            os.replace(temporary, ready)
        finally:
            temporary.unlink(missing_ok=True)
        while not release.exists():
            time.sleep(.01)
        return original.buffer.readline(size)
sys.stdin = FirstRead()
raise SystemExit(upstream_worker.main())
"""


@pytest.mark.parametrize("boundary", ["publish", "write-fail", "replace-fail"])
def test_preread_marker_publication_is_atomic(tmp_path, monkeypatch, boundary):
    """Execute the real entry, observing its actual open and partial-write seam."""
    import io
    import os
    import sys
    from pathlib import Path

    from taskpaw_v3.hub.server import upstream_worker as uw

    ready, release = tmp_path / "ready.json", tmp_path / "release"
    release.touch()
    observed = []

    class Input(io.BytesIO):
        @property
        def buffer(self):
            return self

    def write(path, text, *args, **kwargs):
        with path.open("w", encoding=kwargs.get("encoding")) as stream:
            observed.append("opened")
            assert not ready.exists(), "final marker exposed before content"
            stream.write(text[:1])
            stream.flush()
            observed.append("partial")
            assert not ready.exists(), "final marker exposed with partial content"
            if boundary == "write-fail":
                raise OSError("owned marker write failure")
            stream.write(text[1:])
        return len(text)

    def replace(source, destination):
        assert not ready.exists()
        assert json.loads(Path(source).read_text())["consumed"] == 0
        raise OSError("owned marker replace failure")

    def main():
        assert sys.stdin.buffer.readline(100) == b"owned input\n"
        return 0

    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdin", Input(b"owned input\n"))
        patch.setattr(sys, "argv", ["owned entry", str(release), str(ready)])
        patch.setattr(uw, "main", main)
        patch.setattr(Path, "write_text", write)
        if boundary == "replace-fail":
            patch.setattr(os, "replace", replace)
        if boundary.endswith("-fail"):
            with pytest.raises(OSError, match="owned marker"):
                exec(_PREREAD_ENTRY, {})
            assert not ready.exists()
        else:
            with pytest.raises(SystemExit) as exc:
                exec(_PREREAD_ENTRY, {})
            assert exc.value.code == 0
            marker = json.loads(ready.read_text(encoding="utf-8"))
            assert marker["pid"] == os.getpid() and marker["created"] > 0
            assert marker["consumed"] == 0
    assert observed == ["opened", "partial"]
    assert list(tmp_path.glob("*.tmp")) == []


class _WindowsProcessObserver:
    """Test-owned identity handle; independent of production Job accounting."""

    def __init__(self, pid, created, kernel=None):
        import ctypes
        from ctypes import wintypes as w

        self.ctypes, self.w = ctypes, w
        self.kernel = (
            kernel
            if kernel is not None
            else ctypes.WinDLL("kernel32", use_last_error=True)
        )
        self.pid, self.created = pid, created
        self.handle = None
        self.closed_receipt = None
        self.last_wait = None
        for name, args, result in (
            ("OpenProcess", [w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            (
                "GetProcessTimes",
                [w.HANDLE, *([ctypes.POINTER(w.FILETIME)] * 4)],
                w.BOOL,
            ),
            ("GetHandleInformation", [w.HANDLE, ctypes.POINTER(w.DWORD)], w.BOOL),
            ("WaitForSingleObject", [w.HANDLE, w.DWORD], w.DWORD),
            ("CloseHandle", [w.HANDLE], w.BOOL),
        ):
            fn = getattr(self.kernel, name)
            fn.argtypes, fn.restype = args, result
        self.handle = self.checked(
            self.kernel.OpenProcess(0x101000, False, pid), "OpenProcess"
        )
        try:
            assert self.creation_time() == created, "observer process identity mismatch"
            flags = w.DWORD()
            self.checked(
                self.kernel.GetHandleInformation(self.handle, ctypes.byref(flags)),
                "GetHandleInformation",
            )
            assert not flags.value & 1, "observer handle must not be inherited"
        except BaseException:
            self.close()
            raise

    def checked(self, value, operation):
        if not value:
            error = getattr(self.ctypes, "get_last_error", lambda: 0)()
            raise OSError(error, "owned observer " + operation + " failed")
        return value

    def creation_time(self):
        times = [self.w.FILETIME() for _ in range(4)]
        self.checked(
            self.kernel.GetProcessTimes(
                self.handle, *(self.ctypes.byref(t) for t in times)
            ),
            "GetProcessTimes",
        )
        ticks = (times[0].dwHighDateTime << 32) + times[0].dwLowDateTime
        # Match pinned psutil5.9.8: integer subtraction BEFORE double conversion.
        return float(ticks - 116444736000000000) / 10000000

    def wait(self, milliseconds=0):
        result = self.kernel.WaitForSingleObject(self.handle, milliseconds)
        self.last_wait = result
        if result not in (0, 258):
            raise OSError("owned observer WaitForSingleObject failed")
        return result

    def require_live(self):
        assert self.wait() == 258, "owned observer unexpectedly terminated"

    def require_terminated(self):
        assert self.wait() == 0, "owned interpreter still executing after cleanup"
        assert self.creation_time() == self.created, "observer process identity changed"

    def close(self):
        if self.handle is not None:
            self.checked(self.kernel.CloseHandle(self.handle), "CloseHandle")
            self.handle = None
            self.closed_receipt = True


@pytest.mark.parametrize(
    "fault", [None, "open", "identity", "times", "inherit", "wait", "close"]
)
def test_windows_process_observer_rejects_uncertain_or_live_identity(fault):
    """Signal proves termination even while the same object stays queryable."""
    from types import SimpleNamespace

    ticks = 116444736000000000 + 17910166258845840
    created = float(ticks - 116444736000000000) / 10000000
    closed = []

    def open_process(access, inherit, pid):
        assert access == 0x101000 and inherit is False and pid == 17
        return 0 if fault == "open" else 11

    def times(handle, creation, *unused):
        assert handle == 11
        creation._obj.dwLowDateTime = ticks & 0xFFFFFFFF
        creation._obj.dwHighDateTime = ticks >> 32
        return fault != "times"

    def info(handle, flags):
        flags._obj.value = int(fault == "inherit")
        return True

    def close(handle):
        if fault == "close":
            return False
        closed.append(handle)
        return True

    kernel = SimpleNamespace(
        OpenProcess=open_process,
        GetProcessTimes=times,
        GetHandleInformation=info,
        WaitForSingleObject=lambda handle, timeout: 258,
        CloseHandle=close,
    )
    if fault in ("open", "identity", "times", "inherit"):
        with pytest.raises((OSError, AssertionError)):
            _WindowsProcessObserver(17, created + int(fault == "identity"), kernel)
        assert closed == ([] if fault == "open" else [11])
        return
    observer = _WindowsProcessObserver(17, created, kernel)
    try:
        observer.require_live()
        with pytest.raises(AssertionError, match="still executing"):
            observer.require_terminated()
        if fault == "wait":
            kernel.WaitForSingleObject = lambda handle, timeout: 0xFFFFFFFF
            with pytest.raises(OSError):
                observer.require_terminated()
        else:
            kernel.WaitForSingleObject = lambda handle, timeout: 0
            observer.require_terminated()
            assert observer.creation_time() == created
        if fault == "close":
            with pytest.raises(OSError):
                observer.close()
            assert observer.handle == 11 and observer.closed_receipt is None
            kernel.CloseHandle = lambda handle: closed.append(handle) or True
    finally:
        observer.close()
    assert observer.closed_receipt is True and closed == [11]
    observer.close()
    assert closed == [11]


def _windows_preread_pipe(capacities):
    """Provision only: production owns the real 4KiB pipe and native spawn."""
    import ctypes
    from ctypes import wintypes as w

    def create(api):
        read, write = api.pipe(4096)
        try:
            query = api.kernel.GetNamedPipeInfo
            query.argtypes = [
                w.HANDLE,
                ctypes.POINTER(w.DWORD),
                ctypes.POINTER(w.DWORD),
                ctypes.POINTER(w.DWORD),
                ctypes.POINTER(w.DWORD),
            ]
            query.restype = w.BOOL
            outgoing, incoming = w.DWORD(), w.DWORD()
            assert query(
                read, None, ctypes.byref(outgoing), ctypes.byref(incoming), None
            ), ctypes.get_last_error()
            capacity = max(outgoing.value, incoming.value)
            assert 0 < capacity < 30000, "real backpressure not established"
            capacities.append(capacity)
            return read, write
        except BaseException:
            api.close(read)
            api.close(write)
            raise

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
@pytest.mark.parametrize(
    "case",
    [
        "deadline",
        "stop",
        "success",
        "error",
        "terminate-fail",
        "query-fail",
        "close-fail",
    ],
)
def test_windows_preread_backpressure(tmp_path, monkeypatch, request, frozen, case):
    import hashlib
    import subprocess
    import sys
    import threading
    import time

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
    captured, capacities = [], []
    phases, snapshots = {}, {}
    _capture_upstream_process(monkeypatch, captured, phases=phases)
    monkeypatch.setattr(uw, "_windows_stdin_pipe", _windows_preread_pipe(capacities))
    transport = uw.Transport()
    _observe_upstream_transport(monkeypatch, transport, phases, snapshots)
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

    def call():
        try:
            results.append(transport.request(payload, 10))
        finally:
            phases["caller_complete"] = time.monotonic()

    caller = threading.Thread(target=call)
    marked = None
    observer = sibling_observer = None
    observer_initial_wait = None
    fault_api = fault_method = original_method = None
    sibling = sibling_identity = None
    sibling_release, sibling_ready = (
        tmp_path / "sibling-release",
        tmp_path / "sibling-ready.json",
    )
    if case == "stop":
        sibling_argv = [*argv[:-2], str(sibling_release), str(sibling_ready)]
        sibling = subprocess.Popen(
            sibling_argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    started = time.monotonic()
    try:
        if sibling is not None:
            while not sibling_ready.exists() and time.monotonic() - started < 8:
                time.sleep(0.01)
            assert sibling_ready.exists(), "owned independent sibling barrier"
            sibling_identity = json.loads(sibling_ready.read_text())
            sibling_observer = _WindowsProcessObserver(
                sibling_identity["pid"], sibling_identity["created"]
            )
            sibling_observer.require_live()
            started = time.monotonic()
        caller.start()
        while not ready.exists() and time.monotonic() - started < 8:
            time.sleep(0.01)
        assert ready.exists(), "real helper first-read barrier not reached"
        marked = json.loads(ready.read_text())
        assert marked["consumed"] == 0
        observer = _WindowsProcessObserver(marked["pid"], marked["created"])
        observer.require_live()
        observer_initial_wait = observer.last_wait
        rec = transport._owned
        assert rec is not None and not rec.writer_done.is_set()
        assert capacities[0] < 32000 <= uw.REQUEST_BYTES
        assert any(t.name == "upstream-writer" and t.is_alive() for t in rec.threads)
        if case.endswith("-fail"):
            fault_api = rec.proc.api
            fault_method = {
                "terminate-fail": "terminate_job",
                "query-fail": "accounting",
                "close-fail": "close",
            }[case]
            original_method = getattr(fault_api, fault_method)
            job = rec.proc.job

            def fail(handle):
                if handle == job:
                    raise OSError("owned native cleanup failure")
                return original_method(handle)

            monkeypatch.setattr(fault_api, fault_method, fail)
            transport.cancel()
        elif case == "stop":
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
            "terminate-fail": "helper_cleanup_failed",
            "query-fail": "helper_cleanup_failed",
            "close-fail": "helper_cleanup_failed",
        }[case]
        reason = result.get("reason", "ok" if result.get("ok") else "invalid_result")
        assert reason in (expected, "helper_cleanup_failed")
        if case == "success" and reason == "ok":
            assert result["status"] == {"owned": True}
        inner_alive = observer.wait() == 258
        if reason == "helper_cleanup_failed":
            assert not transport.clean() and transport._owned is rec
            assert transport.request({}, 0.01)["reason"] in (
                "helper_cleanup_failed",
                "helper_cancelled",
            )
            assert len(captured) == 1
        else:
            assert transport.clean()
            observer.require_terminated()
        if sibling_observer is not None:
            sibling_observer.require_live()
        print(
            "R06_PREREAD_NATIVE "
            + json.dumps(
                {
                    "frozen": frozen,
                    "case": case,
                    "capacity": capacities[0],
                    "request_bytes": input_bytes,
                    "result": reason,
                    "inner_alive": inner_alive,
                    "observer_initial_wait": observer_initial_wait,
                    "observer_wait": observer.last_wait,
                    "observer_closed": observer.closed_receipt,
                    "clean": transport.clean(),
                    "source_sha256": hashlib.sha256(
                        __import__("pathlib").Path(uw.__file__).read_bytes()
                    ).hexdigest(),
                    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()
                    if frozen
                    else None,
                    "architecture": __import__("platform").machine(),
                    "python": sys.version.split()[0],
                    "popen_pid": captured[0].pid,
                    "owned_pid": marked["pid"],
                    "owned_created": marked["created"],
                    "elapsed": round(time.monotonic() - started, 4),
                    "joined": not any(t.is_alive() for t in rec.threads),
                    "keeper_closed": rec.keeper < 0,
                    "pipes_closed": captured[0].stdin.closed
                    and captured[0].stdout.closed,
                    "popen_reaped": captured[0].poll() is not None,
                },
                sort_keys=True,
            )
        )
        # Required native cleanup acceptance is distinct from the retained
        # negative safety invariant above. A live wrapper/interpreter is RED.
        if case.endswith("-fail"):
            assert reason == expected and transport._owned is rec
            monkeypatch.setattr(fault_api, fault_method, original_method)
            assert transport.retry_cleanup() and transport.clean()
            assert rec.proc.process is rec.proc.thread is rec.proc.job is None
            assert rec.proc.poll() == rec.proc.wait(timeout=0) == rec.proc.returncode
            observer.require_terminated()
        else:
            assert reason == expected and transport.clean() and not inner_alive
    finally:
        print(
            "R06_PREREAD_PHASES "
            + json.dumps(
                {
                    "case": case,
                    "frozen": frozen,
                    "result": results[0] if results else None,
                    "phases": {name: value - started for name, value in phases.items()},
                    "caller_alive": caller.is_alive(),
                    "clean": transport.clean(),
                    "sibling_identity": sibling_identity,
                    "observer_initial_wait": observer_initial_wait,
                    "observer_wait": observer.last_wait if observer else None,
                    "observer_closed": observer.closed_receipt if observer else None,
                    "sibling_wait": sibling_observer.last_wait
                    if sibling_observer
                    else None,
                    "sibling_observer_closed": sibling_observer.closed_receipt
                    if sibling_observer
                    else None,
                    **snapshots,
                },
                sort_keys=True,
            )
        )
        try:
            if original_method is not None:
                monkeypatch.setattr(fault_api, fault_method, original_method)
            # Release only fixture barriers; keep the original Job cleanup owner.
            release.touch()
            sibling_release.touch()
            transport.cancel()
            try:
                if caller.ident is not None:
                    caller.join(12)
                cleaned = transport.retry_cleanup() and transport.clean()
            finally:
                try:
                    if sibling is not None:
                        sibling.communicate(b"\n", timeout=5)
                        assert sibling.poll() is not None
                finally:
                    if server is not None:
                        server.shutdown()
                        server.server_close()
                        server_thread.join(3)
            assert cleaned
            if observer is not None:
                assert observer.wait(3000) == 0
            if sibling_observer is not None:
                assert sibling_observer.wait(3000) == 0
            for proc in captured:
                assert (
                    proc.poll() is not None and proc.stdin.closed and proc.stdout.closed
                )
        finally:
            try:
                if observer is not None:
                    observer.close()
            finally:
                if sibling_observer is not None:
                    sibling_observer.close()
                print(
                    "R06_OBSERVER_CLOSED "
                    + json.dumps(
                        {
                            "case": case,
                            "frozen": frozen,
                            "observer_closed": observer.closed_receipt
                            if observer
                            else None,
                            "sibling_observer_closed": sibling_observer.closed_receipt
                            if sibling_observer
                            else None,
                        },
                        sort_keys=True,
                    )
                )


@pytest.mark.skipif(__import__("os").name != "nt", reason="native Windows ownership")
@pytest.mark.parametrize("frozen", [False, True], ids=["source", "frozen-preread"])
@pytest.mark.parametrize(
    "fault", ["job", "configure", "assign", "resume", "stop", "deadline"]
)
def test_windows_preread_backpressure_before_resume(
    tmp_path, monkeypatch, request, frozen, fault
):
    """Even an assignment failure owns the suspended unassigned process."""
    import sys

    import psutil

    from taskpaw_v3.hub.server import upstream_worker as uw

    ready, release = tmp_path / "ready.json", tmp_path / "release"
    if frozen:
        binary = request.getfixturevalue("frozen_preread_fixture")
        argv = [str(binary), str(release), str(ready)]
    else:
        argv = [sys.executable, "-c", "raise SystemExit('must never execute')"]
    monkeypatch.setattr(uw, "worker_argv", lambda: argv)
    transport = uw.Transport()
    captured, identities, resumes = [], [], []
    _capture_upstream_process(monkeypatch, captured)
    create, assign, resume = (
        uw._WindowsAPI.create_process,
        uw._WindowsAPI.assign,
        uw._WindowsAPI.resume,
    )

    def created(api, *args):
        value = create(api, *args)
        proc = psutil.Process(value[2])
        identities.append((proc.pid, proc.create_time()))
        return value

    def assigned(api, *args):
        if fault == "assign":
            raise OSError("owned assignment failure")
        assign(api, *args)
        if fault == "stop":
            transport.cancel()
            transport.cancel()

    def resumed(api, thread):
        resumes.append(thread)
        if fault == "resume":
            raise OSError("owned resume failure")
        resume(api, thread)

    def fail(*args):
        raise OSError("owned job setup failure")

    monkeypatch.setattr(uw._WindowsAPI, "create_process", created)
    monkeypatch.setattr(uw._WindowsAPI, "assign", assigned)
    monkeypatch.setattr(uw._WindowsAPI, "resume", resumed)
    if fault in ("job", "configure"):
        monkeypatch.setattr(
            uw._WindowsAPI, "create_job" if fault == "job" else "configure_job", fail
        )
    result = transport.request({}, 0 if fault == "deadline" else 3)
    expected = (
        "helper_cancelled"
        if fault == "stop"
        else "upstream_deadline"
        if fault == "deadline"
        else "helper_failed"
    )
    print(
        "R06_SETUP_NATIVE "
        + json.dumps(
            {
                "frozen": frozen,
                "fault": fault,
                "result": result,
                "clean": transport.clean(),
                "owned_identities": identities,
                "resume_calls": len(resumes),
            },
            sort_keys=True,
        )
    )
    try:
        assert result == {"ok": False, "reason": expected}
        assert transport.clean() and not ready.exists()
        assert len(resumes) == int(fault == "resume")
        proc = captured[0]
        assert proc.process is proc.thread is proc.job is None
        assert not proc.handles and not proc.fds
        assert proc.poll() == proc.wait(timeout=0) == proc.returncode
        for pid, created_time in identities:
            assert (
                not psutil.pid_exists(pid)
                or psutil.Process(pid).create_time() != created_time
            )
    finally:
        release.touch()
        transport.cancel()
        assert transport.retry_cleanup()


@pytest.mark.parametrize("count", [0, 2, 0xFFFFFFFF, 1])
def test_windows_resume_checks_previous_suspend_count(count):
    from types import SimpleNamespace

    from taskpaw_v3.hub.server import upstream_worker as uw

    api = uw._WindowsAPI.__new__(uw._WindowsAPI)
    api.kernel = SimpleNamespace(ResumeThread=lambda handle: count)
    if count == 1:
        api.resume(11)
    else:
        with pytest.raises(OSError, match="resume"):
            api.resume(11)


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


@pytest.fixture
def windows_census_model():
    """Actual Transport, fake native boundary with delayed runtime rundown."""
    import subprocess

    from taskpaw_v3.hub.server import upstream_worker as uw

    class Native:
        def __init__(self):
            self.total, self.running = 2, True
            self.runtime_signal = False
            self.signal_on_wait = True
            self.fail = None
            self.closed, self.waits, self.opened = [], [], []
            self.identities = {11: (17, 100), 21: (18, 101)}
            self.listed = [17, 18]
            self.account_calls = []

        def identity(self, handle, job):
            if self.fail == "identity":
                raise OSError("owned identity query")
            if self.fail == "membership":
                raise OSError("wrong private job")
            return self.identities[handle]

        def members(self, job):
            if self.fail == "list":
                raise OSError("truncated native list")
            return self.listed if self.running else []

        def open_process(self, pid):
            if self.fail == "open":
                raise OSError("owned open failure")
            handle = next(
                h for h, identity in self.identities.items() if identity[0] == pid
            )
            self.opened.append(handle)
            return handle

        def accounting(self, job):
            if self.fail == "accounting":
                raise OSError("owned accounting failure")
            self.account_calls.append(
                (self.total, int(self.running), self.runtime_signal)
            )
            return self.total, int(self.running)

        def active(self, job):
            return self.accounting(job)[1]

        def terminate_job(self, job):
            assert job == 13
            if self.fail == "terminate":
                raise OSError("owned terminate failure")
            self.running = False

        def poll(self, handle):
            if self.fail == "poll":
                raise OSError("owned process status query")
            return 0 if not self.running else None

        def wait(self, handle, timeout):
            self.waits.append((handle, timeout))
            if self.fail == "wait":
                raise OSError("owned process wait failed")
            if handle != 11:
                if self.signal_on_wait:
                    self.runtime_signal = True
                if not self.runtime_signal:
                    raise subprocess.TimeoutExpired("owned runtime", timeout)
            return 0

        def close(self, handle):
            if self.fail == "close" or self.fail == ("close", handle):
                raise OSError("owned checked close")
            assert handle not in self.closed, "native handle double-close"
            self.closed.append(handle)

    native = Native()
    proc = uw._WindowsProcess(native)
    proc.process, proc.thread, proc.job = 11, 12, 13
    proc.pid, proc.assigned = 17, True
    rec = uw._Owned(proc, -1)
    transport = uw.Transport()
    transport._owned = rec
    return transport, rec, native


def test_windows_census_waits_runtime_after_active_zero(windows_census_model):
    transport, rec, native = windows_census_model
    assert transport.retry_cleanup() and transport.clean()
    assert native.runtime_signal, "active0/launcher exit cannot certify runtime exit"
    assert [handle for handle, _ in native.waits] == [11, 11, 21]
    assert native.account_calls[-1] == (2, 0, True)
    assert sorted(native.closed) == [11, 12, 13, 21]
    assert rec.proc.poll() == rec.proc.wait(0) == 0


def test_windows_census_unsignaled_member_retains_and_retry(windows_census_model):
    transport, rec, native = windows_census_model
    native.signal_on_wait = False
    assert not transport.retry_cleanup() and transport._owned is rec
    assert not native.closed and len(rec.proc.members) == 2
    assert transport.request({}, 1)["reason"] == "helper_cleanup_failed"
    native.signal_on_wait = True
    assert transport.retry_cleanup() and transport.clean()
    assert native.opened == [21]


def test_windows_census_birth_race_gone_generation_cannot_clear(windows_census_model):
    transport, rec, native = windows_census_model
    terminate = native.terminate_job

    def birth(job):
        native.total = 3  # Birth after the complete pre-kill snapshot.
        terminate(job)

    native.terminate_job = birth
    assert not transport.retry_cleanup() and transport._owned is rec
    assert native.runtime_signal and not native.closed
    assert not transport.retry_cleanup()  # Empty later list is not exit coverage.
    assert len(native.opened) == 1 and len(rec.proc.members) == 2


def test_windows_census_late_member_can_complete_retry(windows_census_model):
    transport, rec, native = windows_census_model
    native.total = 3
    native.identities[22] = (19, 102)
    assert not transport.retry_cleanup()
    native.members = lambda job: [19]
    assert transport.retry_cleanup() and transport.clean()
    assert native.opened == [21, 22]
    assert sorted(native.closed) == [11, 12, 13, 21, 22]


@pytest.mark.parametrize(
    "fault",
    [
        "open",
        "identity",
        "membership",
        "list",
        "accounting",
        "terminate",
        "poll",
        "wait",
    ],
)
def test_windows_census_capture_fault_still_terminates_and_retries(
    windows_census_model, fault
):
    transport, rec, native = windows_census_model
    native.fail = fault
    assert not transport.retry_cleanup() and transport._owned is rec
    assert native.running == (fault == "terminate")
    assert len(rec.proc.members) <= 16
    native.fail = None
    # Restore access to any uncaptured but still retained actual process object.
    native.members = lambda job: [17, 18]
    assert transport.retry_cleanup() and transport.clean()
    assert sorted(native.closed) == [11, 12, 13, 21]


def test_windows_census_pending_validation_close_failure_is_bounded(
    windows_census_model,
):
    transport, rec, native = windows_census_model
    rec.proc.capture_launcher()
    identity, close = native.identity, native.close
    native.identity = lambda handle, job: (
        identity(handle, job)
        if handle == 11
        else (_ for _ in ()).throw(OSError("pending identity"))
    )
    native.close = lambda handle: (
        close(handle)
        if handle != 21
        else (_ for _ in ()).throw(OSError("pending close"))
    )
    native.members = lambda job: [17, 18]
    for _ in range(20):
        assert not transport.retry_cleanup()
        assert transport._owned is rec and len(rec.proc.members) == 2
    assert native.opened == [21]  # Retry owns the original failed handle.
    native.identity, native.close = identity, close
    assert transport.retry_cleanup() and transport.clean()
    assert sorted(native.closed) == [11, 12, 13, 21]


@pytest.mark.parametrize("handle", [21, 11, 13])
def test_windows_census_partial_close_receipts_survive_retry(
    windows_census_model, handle
):
    transport, rec, native = windows_census_model
    native.fail = ("close", handle)
    assert not transport.retry_cleanup() and transport._owned is rec
    assert native.runtime_signal
    identities = [(m.pid, m.created) for m in rec.proc.members]
    native.fail = None
    assert transport.retry_cleanup() and transport.clean()
    assert [(m.pid, m.created) for m in rec.proc.members] == identities
    assert sorted(native.closed) == [11, 12, 13, 21]
    assert all(m.closed and m.signaled for m in rec.proc.members)


@pytest.mark.parametrize("total,active", [(0, 0), (17, 0), (2**32, 0), (2, 3), (1, 0)])
def test_windows_census_invalid_or_regressing_accounting_retains(
    windows_census_model, total, active
):
    transport, rec, native = windows_census_model
    rec.proc.capture_members()
    native.accounting = lambda job: (total, active)
    assert not transport.retry_cleanup() and transport._owned is rec
    assert not native.closed
    if total == 17:
        native.accounting = lambda job: (2, 0)
        assert not transport.retry_cleanup()  # Rejected high-water cannot disappear.


def test_windows_census_capacity_reserved_before_open(windows_census_model):
    transport, rec, native = windows_census_model
    rec.proc.capture_members()
    from taskpaw_v3.hub.server import upstream_worker as uw

    rec.proc.members.extend(
        uw._WindowsMember(100 + i, None, i, True, True, True) for i in range(14)
    )
    native.members = lambda job: [19]
    assert not transport.retry_cleanup() and transport._owned is rec
    assert native.opened == [21] and len(rec.proc.members) == 16


def test_windows_census_shared_cleanup_end(windows_census_model, monkeypatch):
    from taskpaw_v3.hub.server import upstream_worker as uw

    transport, rec, native = windows_census_model
    clock = [100.0]
    monkeypatch.setattr(uw.time, "monotonic", lambda: clock[0])
    wait = native.wait

    def elapsed(handle, timeout):
        result = wait(handle, timeout)
        clock[0] += 0.3
        return result

    native.wait = elapsed
    assert transport.retry_cleanup()
    assert [remaining for _, remaining in native.waits] == pytest.approx([1, 0.7, 0.4])


def test_windows_census_duplicate_failed_close_does_not_inflate_coverage(
    windows_census_model,
):
    transport, rec, native = windows_census_model
    rec.proc.capture_members()
    member = rec.proc.members[1]
    # A checked-closed proven identity remains in the lifetime receipt table.
    native.running, native.runtime_signal = False, True
    member.signaled = True
    native.close(21)
    member.handle, member.closed = None, True
    native.identities[22] = (18, 101)  # Another handle to that same object.
    native.members = lambda job: [18]
    native.open_process = lambda pid: native.opened.append(22) or 22
    native.fail = ("close", 22)
    for _ in range(3):
        assert not transport.retry_cleanup() and transport._owned is rec
        assert sum(m.valid for m in rec.proc.members) == 2
        assert len(rec.proc.members) == 3
    assert native.opened == [21, 22]
    native.fail = None
    assert transport.retry_cleanup() and transport.clean()
    assert len(rec.proc.members) == 2
    assert sorted(native.closed) == [11, 12, 13, 21, 22]
