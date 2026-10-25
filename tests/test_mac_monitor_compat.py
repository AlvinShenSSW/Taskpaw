"""Mac monitor-only upgrade: real read routes, Hub decoder and controller reader.

All state is disposable. TestClient serves in-memory requests; no installed
Agent, live event queue, media executor or real LAN peer is contacted.
"""

# ruff: noqa: E402

from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from taskpaw_v3.agent.server.app import create_network_app
from taskpaw_v3.core.config import AgentConfig, PowerPeer, load_yaml, save_yaml
from taskpaw_v3.core.power import PowerSampler
from taskpaw_v3.core.power_fleet import PowerFleet
from taskpaw_v3.core.protocol import EventQueue
from taskpaw_v3.hub.server.upstream_worker import decode_status
from taskpaw_v3.monitors.runtime import effective_monitors, merge_status
from taskpaw_v3.workflow.local_host import start_local_workflow

TOKEN = "fixture-monitor-bearer"


@pytest.fixture
def monitor_config(tmp_path):
    # Pre-power/pre-workflow YAML shape; preserve the operator's non-default
    # port and existing monitors when current defaults are introduced.
    legacy = {
        "server_id": "mac-monitor-fixture",
        "machine": "Mac monitor",
        "bind_host": "192.168.1.40",
        "bind_port": 5678,
        "control_host": "127.0.0.1",
        "control_port": 5681,
        "api_token": TOKEN,
        "host_metrics": True,
        "monitors": [
            {
                "type_id": "dev_activity",
                "name": "Development",
                "enabled": True,
                "config": {"name": "Development"},
            },
            {
                "type_id": "jellyfin",
                "name": "Library health",
                "enabled": True,
                "config": {"name": "Library health"},
            },
        ],
    }
    path = tmp_path / "agent.yaml"
    path.write_text(yaml.safe_dump(legacy), encoding="utf-8")
    config = load_yaml(AgentConfig, path)
    assert isinstance(config, AgentConfig)
    return config, legacy, path


def test_legacy_mac_config_keeps_monitors_ports_and_media_off(monitor_config):
    config, legacy, path = monitor_config
    assert config.lan_access is True
    assert config.workflow.enabled is False
    assert config.workflow.remote_workers is False
    assert config.power.enabled is True
    assert config.power.peers == []
    assert start_local_workflow(config.workflow, path.parent) is None
    assert not (path.parent / "workflow").exists()
    assert {m["type_id"] for m in effective_monitors(config)} == {
        "host_metrics",
        "dev_activity",
        "jellyfin",
    }

    save_yaml(config, path)
    upgraded = load_yaml(AgentConfig, path)
    assert isinstance(upgraded, AgentConfig)
    for key, value in legacy.items():
        assert getattr(upgraded, key) == value


@pytest.mark.parametrize(
    ("power_mode", "expected_state"),
    [("absent", "no_power"), ("off", "off"), ("on", "ok")],
)
def test_mac_read_status_and_power_stay_compatible_without_event_consumption(
    monitor_config, tmp_path, monkeypatch, power_mode, expected_state
):
    config, _, _ = monitor_config
    config.power.enabled = power_mode == "on"
    sampler = PowerSampler(
        lambda: config.power,
        gpu=lambda: None,
        cpu=lambda: 25.0,
        platform="darwin",
        data_dir=tmp_path,
    )
    sampler.tick()
    queue = EventQueue(config.machine)
    queue.add(monitor="Development", message="fixture pending event")
    cursor_before = queue.cursor_snapshot()

    def reject_event_read(*args, **kwargs):
        pytest.fail("controller read must not consume or acknowledge /events")

    monkeypatch.setattr(queue, "network_payload", reject_event_read)

    def status_provider():
        value = {
            "machine": config.machine,
            "server_id": config.server_id,
            "os": "Darwin-fixture",
            "monitors": merge_status(
                config,
                {
                    "Development": {"state": "running", "metrics": {}},
                    "Library health": {"state": "ok", "metrics": {}},
                },
            ),
        }
        if power_mode != "absent":
            value.update(power=sampler.live(), energy=sampler.energy())
        return value

    app = create_network_app(
        config,
        queue,
        status_provider,
        power_history=sampler.history if power_mode != "absent" else None,
    )
    calls = []
    with TestClient(app) as client:
        headers = {"Authorization": f"Bearer {TOKEN}"}

        def fetch(url, token):
            assert token == TOKEN
            parsed = urlsplit(url)
            assert parsed.netloc == "127.0.0.1:45678"
            assert parsed.path in {"/status", "/power/history"}
            path = parsed.path + ("?" + parsed.query if parsed.query else "")
            calls.append(path)
            response = client.get(path, headers=headers)
            assert response.status_code == 200
            return response.json()

        fleet = PowerFleet(
            lambda: [PowerPeer(name="Mac", address="127.0.0.1:45678")],
            lambda: TOKEN,
            fetch=fetch,
        )
        try:
            # Auth still applies to both new and existing read interfaces.
            assert client.get("/status").status_code == 401
            assert client.get("/power/history").status_code == 401
            response = client.get("/status", headers=headers)
            status, _ = decode_status(response.content)
            assert status["monitors"]["Development"]["state"] == "running"
            assert status["monitors"]["Library health"]["state"] == "ok"
            assert "workflow" not in status and "workflow_worker" not in status
            assert client.get("/workflow/films", headers=headers).status_code == 404
            assert client.post("/power/history", headers=headers).status_code == 405

            fleet.refresh_once(wait=True)
            row = fleet.snapshot("24h")[0]
            assert row["state"] == expected_state
            assert row["machine"] == config.machine
            assert row["version"] == status["version"]
            quiet = fleet.quiet("127.0.0.1:45678")
            assert quiet is not None and quiet.answered
            assert quiet.state == "no_busy"
            assert quiet.quiet_s is None
            assert calls.count("/status") == 1
            histories = {path for path in calls if path.startswith("/power/")}
            assert histories == (
                set()
                if power_mode == "absent"
                else {f"/power/history?range={r}" for r in ("24h", "7d", "30d")}
            )
            assert queue.cursor_snapshot() == cursor_before
            assert len(queue.recent()) == 1
        finally:
            assert fleet.stop(timeout=2)
            assert sampler.stop(timeout=2)
