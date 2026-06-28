"""One-click bootstrap (python -m taskpaw_v3.bootstrap)."""

from __future__ import annotations

import sys

import pytest

from taskpaw_v3 import bootstrap
from taskpaw_v3.hub.server import service as hub_service
from taskpaw_v3.hub.server.store import HubStore


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point platform config paths at a temp HOME (mac layout)."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return tmp_path


# ── scaffold ─────────────────────────────────────────────────────────────--
def test_scaffold_agent_creates_config(home):
    path, created = bootstrap.scaffold("agent")
    assert created and path.exists() and path.name == "agent.yaml"
    assert "monitors: []" in path.read_text()


def test_scaffold_hub_creates_config(home):
    path, created = bootstrap.scaffold("hub")
    assert created and path.exists() and path.name == "hub.yaml"


def test_scaffold_does_not_clobber(home):
    path, _ = bootstrap.scaffold("agent")
    path.write_text("server_id: mine\nmachine: mine\n")
    path2, created = bootstrap.scaffold("agent")
    assert path2 == path and created is False
    assert "server_id: mine" in path.read_text()   # untouched


def test_scaffold_force_overwrites(home):
    path, _ = bootstrap.scaffold("agent")
    path.write_text("custom")
    _, created = bootstrap.scaffold("agent", force=True)
    assert created and "custom" not in path.read_text()


def test_scaffold_is_atomic_no_tmp_left(home):
    """Atomic write: the dst is the example content and no .tmp residue remains."""
    path, _ = bootstrap.scaffold("agent")
    assert "monitors: []" in path.read_text()
    assert list(path.parent.glob(".*.tmp")) == []


# ── agent spec parsing ───────────────────────────────────────────────────--
def test_parse_agent_spec_variants():
    assert bootstrap._parse_agent_spec("moomoo,192.168.1.50") == ("moomoo", "192.168.1.50", 5680)
    assert bootstrap._parse_agent_spec("m,10.0.0.1,5999") == ("m", "10.0.0.1", 5999)


@pytest.mark.parametrize("bad", ["", "nameonly", "n,ip,0", "n,ip,70000", "n,,5680"])
def test_parse_agent_spec_rejects_bad(bad):
    with pytest.raises(ValueError):
        bootstrap._parse_agent_spec(bad)


# ── register_agents ──────────────────────────────────────────────────────--
def test_register_agents_adds_and_skips_dupes(home):
    bootstrap.scaffold("hub")
    lines = bootstrap.register_agents(["moomoo,192.168.1.50", "mac,127.0.0.1,5680"])
    assert any("+ moomoo" in l for l in lines) and any("+ mac" in l for l in lines)
    again = bootstrap.register_agents(["moomoo,192.168.1.50"])
    assert any("already registered" in l for l in again)

    db = hub_service.default_db_path(hub_service.default_config_path())
    names = {s["name"] for s in HubStore(db).list_servers()}
    assert names == {"moomoo", "mac"}


def test_register_agents_validates_before_writing(home):
    bootstrap.scaffold("hub")
    with pytest.raises(ValueError):
        bootstrap.register_agents(["good,1.2.3.4", "bad-no-ip"])
    db = hub_service.default_db_path(hub_service.default_config_path())
    # nothing persisted — the whole batch is rejected on the bad spec
    assert HubStore(db).list_servers() == []


# ── main() ───────────────────────────────────────────────────────────────--
def test_main_agent_scaffolds_and_reports(home, capsys):
    rc = bootstrap.main(["agent"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "created config" in out and "agent.yaml" in out


def test_main_hub_with_agents(home, capsys):
    rc = bootstrap.main(["hub", "--agent", "moomoo,192.168.1.50"])
    assert rc == 0
    assert "+ moomoo" in capsys.readouterr().out


def test_main_agent_flag_rejected_for_agent_role(home, capsys):
    rc = bootstrap.main(["agent", "--agent", "x,1.2.3.4"])
    assert rc == 2
    assert "only valid for the hub" in capsys.readouterr().err


def test_main_bad_agent_spec_returns_2(home, capsys):
    rc = bootstrap.main(["hub", "--agent", "broken"])
    assert rc == 2


# ── --preset moomoo ──────────────────────────────────────────────────────--
def test_preset_moomoo_injects_four_valid_monitors(home, capsys):
    from taskpaw_v3.core.config import AgentConfig, load_yaml
    from taskpaw_v3.monitors.registry import default_registry

    rc = bootstrap.main(["agent", "--preset", "moomoo"])
    assert rc == 0
    assert "applied moomoo preset: 4 monitors" in capsys.readouterr().out

    from taskpaw_v3.agent.server import service as agent_service
    cfg: AgentConfig = load_yaml(AgentConfig, agent_service.default_config_path())
    assert cfg.machine == "moomoo" and len(cfg.monitors) == 4
    reg = default_registry()
    names = sorted(m["config"]["name"] for m in cfg.monitors)
    assert names == ["moomoo-opend", "moomoo-orchestrator",
                     "moomoo-orchestrator-heartbeat", "moomoo-pm2-daemon"]
    for m in cfg.monitors:                     # all validate against real plugins
        reg.get(m["type_id"]).validate_config(m["config"])


def test_preset_rejected_for_hub_role(home, capsys):
    rc = bootstrap.main(["hub", "--preset", "moomoo"])
    assert rc == 2
    assert "only valid for the agent" in capsys.readouterr().err


def test_preset_refuses_existing_without_force(home, capsys):
    bootstrap.main(["agent", "--preset", "moomoo"])          # create + apply
    rc = bootstrap.main(["agent", "--preset", "moomoo"])     # again, no force
    assert rc == 2
    assert "refusing to edit" in capsys.readouterr().err


def test_bind_host_sets_lan_address(home):
    from taskpaw_v3.core.config import AgentConfig, load_yaml
    from taskpaw_v3.agent.server import service as agent_service

    rc = bootstrap.main(["agent", "--bind-host", "192.168.1.77"])
    assert rc == 0
    cfg: AgentConfig = load_yaml(AgentConfig, agent_service.default_config_path())
    assert cfg.bind_host == "192.168.1.77"


def test_preset_with_bind_host_together(home):
    from taskpaw_v3.core.config import AgentConfig, load_yaml
    from taskpaw_v3.agent.server import service as agent_service

    rc = bootstrap.main(["agent", "--preset", "moomoo", "--bind-host", "10.0.0.9"])
    assert rc == 0
    cfg: AgentConfig = load_yaml(AgentConfig, agent_service.default_config_path())
    assert cfg.machine == "moomoo" and len(cfg.monitors) == 4
    assert cfg.bind_host == "10.0.0.9"


def test_bind_host_rejected_for_hub(home, capsys):
    assert bootstrap.main(["hub", "--bind-host", "10.0.0.1"]) == 2
    assert "only valid for the agent" in capsys.readouterr().err


def test_preset_force_reapplies(home):
    from taskpaw_v3.core.config import AgentConfig, load_yaml
    from taskpaw_v3.agent.server import service as agent_service

    bootstrap.main(["agent", "--preset", "moomoo"])
    # user wipes monitors, then re-applies with --force
    p = agent_service.default_config_path()
    p.write_text("server_id: x\nmachine: x\nmonitors: []\n")
    assert bootstrap.main(["agent", "--preset", "moomoo", "--force"]) == 0
    cfg: AgentConfig = load_yaml(AgentConfig, p)
    assert len(cfg.monitors) == 4
