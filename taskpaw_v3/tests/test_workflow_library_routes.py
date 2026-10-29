"""#362 L1: the 片库 control routes (#361 §10), controller only."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from taskpaw_v3.agent.server.app import create_control_app
from taskpaw_v3.agent.server.library_routes import etag_matches, thumb_response
from taskpaw_v3.core.config import AgentConfig, WorkflowSettings
from taskpaw_v3.tests.test_workflow_library import fake_filmpaw, jpeg_file, root
from taskpaw_v3.tests.test_workflow_local_host import _wait
from taskpaw_v3.workflow.library_scan import LibraryScanner, scan_library
from taskpaw_v3.workflow.library_store import LibraryStore, normalize_path
from taskpaw_v3.workflow.local_host import LocalWorkflowHost

BASE = "/control/workflow/library"


@pytest.fixture(autouse=True)
def _no_real_filmpaw(monkeypatch, tmp_path):
    """The default import source follows APPDATA: never the operator's."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))


@pytest.fixture
def host(tmp_path):
    store = LibraryStore.open(tmp_path / "library.db")
    value = SimpleNamespace(
        state="running",
        reason="",
        runtime=object(),
        finalizer=None,
        library=store,
        library_scanner=LibraryScanner(store),  # not started: tests run its queue
    )
    try:
        yield value
    finally:
        store.close()


def client_for(provider, role="controller") -> TestClient:
    config = AgentConfig(
        server_id="s1", machine="dev", workflow=WorkflowSettings(role=role)
    )
    return TestClient(
        create_control_app(
            config,
            control_token="t",
            control_active=lambda: True,
            workflow_provider=provider,
        ),
        headers={"Authorization": "Bearer t"},
    )


@pytest.fixture
def client(host):
    return client_for(lambda: host)


def test_library_crud(client, host, tmp_path):
    path = root(tmp_path, "Actors", "Alice")
    created = client.post(
        f"{BASE}/libraries", json={"kind": "actor", "path": str(path)}
    )
    assert created.status_code == 201
    lib = created.json()
    assert (lib["path"], lib["label"], lib["role"]) == (
        str(path) + os.sep,
        "Actors",
        "archive",
    )
    assert host.library_scanner.status()["queued"] == [lib["id"]]  # scanned next
    again = client.post(
        f"{BASE}/libraries", json={"kind": "category", "path": str(path)}
    )
    assert again.status_code == 409
    inside = client.post(
        f"{BASE}/libraries", json={"kind": "category", "path": str(path / "Alice")}
    )
    assert inside.status_code == 409
    missing = client.post(
        f"{BASE}/libraries", json={"kind": "actor", "path": str(tmp_path / "nope")}
    )
    assert missing.status_code == 422
    odd = client.post(f"{BASE}/libraries", json={"kind": "film", "path": str(path)})
    assert odd.status_code == 422
    listed = client.get(f"{BASE}/libraries").json()
    (item,) = listed["items"]
    assert item["id"] == lib["id"]
    assert listed["db_path"] == str(host.library.path)  # 设置 · 片库数据

    patched = client.patch(
        f"{BASE}/libraries/{lib['id']}", json={"label": "演员", "role": "readonly"}
    )
    assert (patched.json()["label"], patched.json()["role"]) == ("演员", "readonly")
    assert (
        client.patch(f"{BASE}/libraries/{lib['id']}", json={"path": "x"}).status_code
        == 422
    )
    assert (
        client.patch(
            f"{BASE}/libraries/{lib['id']}", json={"prefixes": ["A"]}
        ).status_code
        == 422
    )
    assert (
        client.patch(f"{BASE}/libraries/nope", json={"label": "x"}).status_code == 404
    )
    assert client.delete(f"{BASE}/libraries/{lib['id']}").status_code == 204
    assert client.delete(f"{BASE}/libraries/{lib['id']}").status_code == 404
    assert client.get(f"{BASE}/libraries").json()["items"] == []


def test_scan_scan_all_and_status(client, host, tmp_path):
    a = host.library.add_library("actor", str(root(tmp_path, "a", "Alice")))["id"]
    b = host.library.add_library("category", str(root(tmp_path, "b", "FC2-1")))["id"]
    queued = client.post(f"{BASE}/libraries/{a}/scan").json()
    assert queued == {"running": None, "queued": [a], "results": {}}
    assert client.post(f"{BASE}/libraries/nope/scan").status_code == 404
    assert client.post(f"{BASE}/scan-all").json()["queued"] == [a, b]
    while host.library_scanner.run_next():
        pass
    status = client.get(f"{BASE}/scan-status").json()
    assert status["queued"] == [] and status["results"][a]["added"] == 1
    assert status["results"][b]["film_count"] == 1
    (lib,) = [
        i for i in client.get(f"{BASE}/libraries").json()["items"] if i["id"] == a
    ]
    assert (lib["last_scan_ok"], lib["performer_count"]) == (True, 1)


def test_alias_routes(client, host, tmp_path):
    lib = host.library.add_library("actor", str(root(tmp_path, "a", "Alice")))["id"]
    scan_library(host.library, lib)
    performer = host.library.search("alice")["items"][0]["id"]
    url = f"{BASE}/performers/{performer}/aliases"
    added = client.post(url, json={"alias": " Ali "})
    assert added.status_code == 201 and added.json()["alias"] == "Ali"
    assert client.post(url, json={"alias": "ali"}).status_code == 409
    assert client.post(url, json={"alias": "Alice"}).status_code == 409
    assert client.post(url, json={"alias": "  "}).status_code == 422
    assert (
        client.post(f"{BASE}/performers/nope/aliases", json={"alias": "x"}).status_code
        == 404
    )
    alias_id = added.json()["id"]
    assert client.delete(f"{BASE}/aliases/{alias_id}").status_code == 204
    assert client.delete(f"{BASE}/aliases/{alias_id}").status_code == 404


def test_rules_routes(client):
    rules = client.get(f"{BASE}/rules").json()
    assert rules["scan_every_hours"] == 6 and rules["fuzzy"] is True
    put = client.put(f"{BASE}/rules", json={"fuzzy": False, "scan_every_hours": 12})
    assert put.status_code == 200
    assert (put.json()["fuzzy"], put.json()["scan_every_hours"]) == (False, 12)
    assert client.put(f"{BASE}/rules", json={"nope": 1}).status_code == 422
    assert client.put(f"{BASE}/rules", json={"scan_every_hours": 5}).status_code == 422
    assert client.put(f"{BASE}/rules", json=[1]).status_code == 422
    assert client.get(f"{BASE}/rules").json()["scan_every_hours"] == 12


def test_import_routes_scan_what_they_added(client, host, tmp_path):
    new_root = normalize_path(str(tmp_path / "nas1"))
    db = fake_filmpaw(tmp_path, [(new_root, "NAS 1", 2)])
    view = client.get(f"{BASE}/import/preview", params={"path": str(db)}).json()
    assert [(r["status"], r["performers"]) for r in view["roots"]] == [("new", 2)]
    done = client.post(f"{BASE}/import", json={"path": str(db)}).json()
    (added,) = done["added"]
    assert host.library_scanner.status()["queued"] == [added]
    missing = client.get(
        f"{BASE}/import/preview", params={"path": str(tmp_path / "none.db")}
    )
    assert missing.status_code == 404


def test_routes_need_a_controller_with_its_library(host, tmp_path):
    assert (
        client_for(lambda: host, role="worker").get(f"{BASE}/rules").status_code == 409
    )
    host.library, host.library_reason = None, "片库启动失败：x"
    off = client_for(lambda: host).get(f"{BASE}/libraries")
    assert (off.status_code, off.json()["detail"]) == (503, "片库启动失败：x")
    assert client_for(lambda: None).get(f"{BASE}/libraries").status_code == 503


def test_routes_require_the_control_token(host):
    bare = TestClient(
        create_control_app(
            AgentConfig(server_id="s1", machine="dev"),
            control_token="t",
            control_active=lambda: True,
            workflow_provider=lambda: host,
        )
    )
    assert bare.get(f"{BASE}/libraries").status_code == 401


def test_thumb_response_uses_the_etag(host, tmp_path):
    pytest.importorskip("PIL.Image")
    lib = host.library.add_library("actor", str(root(tmp_path, "a", "Alice")))["id"]
    jpeg_file(tmp_path / "a" / "Alice" / "folder.jpg")
    scan_library(host.library, lib)
    performer = host.library.search("alice")["items"][0]["id"]
    full = thumb_response(host.library, performer, None)
    etag = full.headers["etag"]
    assert full.status_code == 200 and full.media_type == "image/jpeg" and full.body
    assert etag.endswith('-512"')
    for header in (etag, f"W/{etag}", f'"x", {etag}', "*"):
        cached = thumb_response(host.library, performer, header)
        assert cached.status_code == 304 and cached.headers["etag"] == etag
    assert thumb_response(host.library, performer, '"other"').status_code == 200
    with pytest.raises(HTTPException) as info:
        thumb_response(host.library, "nope", None)
    assert info.value.status_code == 404
    assert not etag_matches(None, etag)


# ── the host opens it beside workflow.db (§4) ────────────────────────────
def test_the_host_opens_the_library_beside_workflow_db(tmp_path):
    data = tmp_path / "data"
    host = LocalWorkflowHost(WorkflowSettings(enabled=True), data)
    host.start()
    try:
        _wait(lambda: host.state != "starting")
        assert host.state == "running", host.reason
        assert host.library is not None and host.library.path == data / "library.db"
        assert (data / "workflow.db").is_file() and host.library_scanner is not None
    finally:
        host.stop(timeout=10)
    assert host.state == "stopped" and host.library is None


def test_a_library_that_cannot_open_leaves_the_workflow_running(tmp_path):
    import sqlite3

    data = tmp_path / "data"
    data.mkdir()
    conn = sqlite3.connect(data / "library.db")
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO schema_version VALUES (9)")
    conn.commit()
    conn.close()
    host = LocalWorkflowHost(WorkflowSettings(enabled=True), data)
    host.start()
    try:
        _wait(lambda: host.state != "starting")
        assert host.state == "running"
        assert host.library is None and "版本 9" in host.library_reason
    finally:
        host.stop(timeout=10)
    assert host.state == "stopped"
