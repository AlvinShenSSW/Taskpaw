"""#299 R1/R2/§6: the first-run marker, the silent first event lineage and
the setup wizard's step API. Every file is a tmp fixture; nothing is printed
and nothing reaches %APPDATA%."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from taskpaw_v3.agent.server import first_run, service
from taskpaw_v3.agent.server.admin import MonitorAdmin
from taskpaw_v3.agent.server.app import create_control_app
from taskpaw_v3.core.config import AgentConfig, load_yaml, save_yaml
from taskpaw_v3.core.state import StateError, read_record, state_paths
from taskpaw_v3.monitors.registry import PluginRegistry

TOKEN = "control-token"


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A fresh install: no agent.yaml yet; the platform path is a tmp dir."""
    path = tmp_path / "TaskPaw" / "agent.yaml"
    monkeypatch.setattr(service, "default_config_path", lambda: path)

    from taskpaw_v3.agent.server import service as agent_service

    monkeypatch.setattr(agent_service, "default_config_path", lambda: path)
    return path


def _run_records(monkeypatch) -> list[dict]:
    calls: list[dict] = []

    def fake_run(config, **kwargs):
        state = kwargs["state_path"]
        try:
            record = read_record(state)
        except StateError as exc:
            # the real launcher refuses to start without verified state
            raise StateError(exc.reason) from None
        calls.append({"server_id": config.server_id, "record": record})

    monkeypatch.setattr(service, "run_agent", fake_run)
    return calls


# ── R1: the marker matrix ───────────────────────────────────────────────
def test_a_fresh_install_writes_the_marker_and_initializes_silently(
    home, monkeypatch, capsys
):
    calls = _run_records(monkeypatch)
    assert service.main() == 0
    marker = json.loads((home.parent / first_run.SETUP_NAME).read_text())
    config = load_yaml(AgentConfig, home)
    assert marker == {
        "v": 1,
        "status": "pending",
        "step": "role",
        "server_id": config.server_id,
    }
    (call,) = calls
    assert call["record"].server_id == config.server_id
    assert call["record"].lineage_origin == "new_pairing"
    assert call["record"].next_event_id == 1
    assert "initialization_required" not in capsys.readouterr().out


def test_a_crash_after_the_scaffold_keeps_the_confirmation(home, monkeypatch, capsys):
    """agent.yaml was written but the marker never was: the dialog path."""
    home.parent.mkdir(parents=True)
    save_yaml(AgentConfig(server_id="scaffolded", machine="m"), home)
    _run_records(monkeypatch)
    assert service.main() == 1
    assert json.loads(capsys.readouterr().out)["code"] == "initialization_required"
    assert not (home.parent / first_run.SETUP_NAME).exists()
    assert not home.with_name("agent.state.json").exists()


def test_a_crash_after_the_marker_initializes_on_the_next_start(home, monkeypatch):
    home.parent.mkdir(parents=True)
    save_yaml(AgentConfig(server_id="fresh-1", machine="m"), home)
    first_run.write_pending(home, "fresh-1")
    calls = _run_records(monkeypatch)
    assert service.main() == 0
    assert calls[0]["record"].server_id == "fresh-1"


@pytest.mark.parametrize("case", ["done", "mismatch", "fault", "primary", "anchor"])
def test_anything_but_a_clean_pending_marker_keeps_the_dialog(
    home, monkeypatch, capsys, case
):
    home.parent.mkdir(parents=True)
    save_yaml(AgentConfig(server_id="fresh-1", machine="m"), home)
    first_run.write_pending(home, "other-id" if case == "mismatch" else "fresh-1")
    marker = home.with_name(first_run.SETUP_NAME)
    if case == "done":
        data = json.loads(marker.read_text())
        marker.write_text(json.dumps({**data, "status": "done"}))
    primary, anchor, _ = state_paths(home.with_name("agent.state.json"))
    if case == "fault":
        primary.with_name(primary.name + ".fault-20261023T000000-ab").write_text("x")
    if case == "primary":
        primary.write_text("{}")
    if case == "anchor":
        anchor.write_text("{}")
    before = {p.name: p.read_bytes() for p in home.parent.iterdir() if p.is_file()}
    _run_records(monkeypatch)
    assert service.main() == 1
    assert json.loads(capsys.readouterr().out)["code"] in {
        "initialization_required",
        "state_recovery_required",
    }
    after = {p.name: p.read_bytes() for p in home.parent.iterdir() if p.is_file()}
    assert after == before  # nothing initialized, nothing rewritten


def test_an_unreadable_marker_is_never_trusted(tmp_path):
    config = tmp_path / "agent.yaml"
    state = tmp_path / "agent.state.json"
    config.with_name(first_run.SETUP_NAME).write_text("{not json")
    assert first_run.read_marker(config) is None
    assert not first_run.maybe_initialize(config, state, "x")
    config.with_name(first_run.SETUP_NAME).write_text(
        json.dumps({"v": 2, "status": "pending", "step": "role", "server_id": "x"})
    )
    assert not first_run.maybe_initialize(config, state, "x")
    assert not state.exists()


# ── §6: the wizard's step API ───────────────────────────────────────────
class World:
    def __init__(self, tmp_path: Path, marker: bool = True, **workflow) -> None:
        self.path = tmp_path / "agent.yaml"
        data = {"server_id": "s1", "machine": "m", "host_metrics": False}
        if workflow:
            data["workflow"] = workflow
        self.path.write_text(yaml.safe_dump(data), encoding="utf-8")
        if marker:
            first_run.write_pending(self.path, "s1")
        self.config = load_yaml(AgentConfig, self.path)
        self.admin = MonitorAdmin(self.config, None, PluginRegistry(), self.path)
        app = create_control_app(
            self.config,
            admin=self.admin,
            control_token=TOKEN,
            control_active=lambda: True,
        )
        self.client = TestClient(app, base_url="http://127.0.0.1")
        self.h = {"Authorization": f"Bearer {TOKEN}"}

    def get(self):
        return self.client.get("/control/setup", headers=self.h)

    def put(self, step, role):
        return self.client.put(
            "/control/setup", json={"step": step, "role": role}, headers=self.h
        )

    def complete(self):
        return self.client.post("/control/setup/complete", headers=self.h)

    def marker(self) -> dict:
        return json.loads(self.path.with_name(first_run.SETUP_NAME).read_text())


def test_no_marker_reads_absent_and_refuses_writes(tmp_path):
    world = World(tmp_path, marker=False)
    assert world.get().json() == {"status": "absent", "step": None, "role": None}
    assert world.put("network", "agent").status_code == 409
    assert world.complete().status_code == 409


def test_a_plain_agent_walks_role_network_done(tmp_path):
    world = World(tmp_path)
    assert world.get().json() == {"status": "pending", "step": "role", "role": None}
    assert world.put("network", "agent").json()["step"] == "network"
    assert world.put("done", "agent").json() == {
        "status": "pending",
        "step": "done",
        "role": "agent",
    }
    assert world.complete().json()["status"] == "done"
    assert world.marker()["status"] == "done"
    assert world.marker()["server_id"] == "s1"  # the identity is kept
    assert world.put("network", "agent").status_code == 409  # finished


def test_steps_cannot_be_skipped_or_invented(tmp_path):
    world = World(tmp_path)
    skip = world.put("controller", "controller")
    assert skip.status_code == 422 and "step" in skip.json()["fields"]
    assert world.put("role", "agent").status_code == 422
    assert world.put("worker_join", "controller").status_code == 422
    assert world.put("network", "hub").json()["fields"] == {"role": "请选择角色"}
    bad = world.client.put("/control/setup", content=b"[", headers=world.h)
    assert bad.status_code == 422
    assert world.marker()["step"] == "role"  # refusals write nothing


def test_going_back_and_changing_the_role_restarts_at_the_network_step(tmp_path):
    world = World(tmp_path)
    world.put("network", "controller")
    world.put("controller", "controller")
    assert world.put("done", "worker").status_code == 422
    assert world.put("worker_fields", "worker").status_code == 422
    assert world.put("network", "worker").json()["role"] == "worker"
    assert world.put("worker_fields", "worker").status_code == 200
    # an earlier step again (修改) is fine
    assert world.put("network", "worker").json()["step"] == "network"


def test_done_needs_the_saved_workflow_role(tmp_path):
    world = World(tmp_path)
    world.put("network", "controller")
    world.put("controller", "controller")
    refused = world.put("done", "controller")
    assert refused.status_code == 422
    assert refused.json()["fields"] == {"role": "角色设置尚未保存"}


def test_a_worker_completes_only_after_joining(tmp_path):
    from taskpaw_v3.workflow.remote_worker import IDENTITY_NAME

    world = World(tmp_path, enabled=True, role="worker")
    world.put("network", "worker")
    world.put("worker_fields", "worker")
    world.put("worker_join", "worker")
    refused = world.put("done", "worker")
    assert refused.status_code == 422
    assert refused.json()["detail"] == first_run.NOT_JOINED
    assert world.complete().status_code == 422
    identity = tmp_path / "workflow" / IDENTITY_NAME
    identity.parent.mkdir(parents=True)
    identity.write_text(
        json.dumps(
            {
                "controller_host": "192.168.1.20",
                "controller_port": 5682,
                "pairing_id": "p",
                "credential_id": "c",
                "secret": "fake-secret-never-shown",
                "staging_dir": str(tmp_path / "staging"),
                "ticket": None,
                "worker_id": None,
            }
        )
    )
    assert world.complete().status_code == 422  # pending: not confirmed yet
    data = json.loads(identity.read_text())
    identity.write_text(json.dumps({**data, "worker_id": "w-1"}))
    done = world.complete()
    assert done.status_code == 200 and done.json()["status"] == "done"
    assert "fake-secret" not in done.text


def test_complete_needs_a_role(tmp_path):
    world = World(tmp_path)
    refused = world.complete()
    assert refused.status_code == 422 and refused.json()["fields"] == {
        "role": "请选择角色"
    }
