"""P2-4 (#295) R13–R16: the 设置 · 媒体工作流 backend.

`GET/PUT /control/workflow/settings` edit agent.yaml's `workflow` through the
existing config writer: validated per field, saved with a backup and every
unknown key kept, refused while switching away would strand work, and applied
only after a restart. Every file here is a tmp yaml; nothing is printed."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from fastapi.testclient import TestClient

from taskpaw_v3.agent.server.admin import MonitorAdmin
from taskpaw_v3.agent.server.app import create_control_app
from taskpaw_v3.core import config as config_mod
from taskpaw_v3.core.config import AgentConfig, load_yaml, save_yaml
from taskpaw_v3.monitors.registry import PluginRegistry
from taskpaw_v3.tests.test_workflow_local_dispatch import add_film, runtime
from taskpaw_v3.tests.workflow_route_helpers import claim_remote, enrol_remote, route

TOKEN = "control-token"
LLM_KEY = "sk-secret-never-shown-1234567890"
URL = "/control/workflow/settings"


def _tools(tmp_path: Path) -> dict[str, str]:
    inbox = tmp_path / "INBOX"
    inbox.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("ffprobe.exe", "jasna.exe", "whisperjav.exe"):
        (tools / name).write_bytes(b"x")
    return {
        "inbox_root": str(inbox),
        "ffprobe_path": str(tools / "ffprobe.exe"),
        "jasna_exe_path": str(tools / "jasna.exe"),
        "whisperjav_exe_path": str(tools / "whisperjav.exe"),
    }


def _write_config(path: Path, **workflow) -> None:
    data = {
        "server_id": "s",
        "machine": "m",
        "host_metrics": False,
        "llm_api_key": LLM_KEY,
        "power": {"peers": [{"name": "Mac", "address": "192.168.1.20"}]},
        "monitors": [{"type_id": "fake", "name": "keep-me", "config": {}}],
        "workflow": workflow,
        "future_key": {"kept": True},  # written by a newer build
    }
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


class World:
    def __init__(self, tmp_path: Path, **workflow) -> None:
        self.path = tmp_path / "agent.yaml"
        _write_config(self.path, **workflow)
        self.config = load_yaml(AgentConfig, self.path)
        self.admin = MonitorAdmin(self.config, None, PluginRegistry(), self.path)
        self.host = None
        self.worker = None
        app = create_control_app(
            self.config,
            admin=self.admin,
            control_token=TOKEN,
            control_active=lambda: True,
            workflow_provider=lambda: self.host,
            worker_provider=lambda: self.worker,
        )
        self.client = TestClient(app, base_url="http://127.0.0.1")

    def get(self):
        return self.client.get(URL, headers={"Authorization": f"Bearer {TOKEN}"})

    def put(self, body):
        return self.client.put(
            URL, json=body, headers={"Authorization": f"Bearer {TOKEN}"}
        )

    def saved(self) -> dict:
        return yaml.safe_load(self.path.read_text(encoding="utf-8"))


@pytest.fixture
def tools(tmp_path):
    return _tools(tmp_path)


# ── save_yaml (R15) ─────────────────────────────────────────────────────
def test_save_yaml_keeps_unknown_keys_and_backs_up_the_previous_bytes(tmp_path):
    path = tmp_path / "agent.yaml"
    _write_config(path)
    before = path.read_bytes()
    cfg = load_yaml(AgentConfig, path)
    cfg.machine = "renamed"
    save_yaml(cfg, path, backup=True)
    assert (tmp_path / "agent.yaml.bak").read_bytes() == before
    after = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert after["machine"] == "renamed"
    assert after["future_key"] == {"kept": True}
    assert not list(tmp_path.glob("*.tmp"))


def test_save_yaml_without_backup_writes_no_bak(tmp_path):
    path = tmp_path / "agent.yaml"
    _write_config(path)
    save_yaml(load_yaml(AgentConfig, path), path)
    assert not (tmp_path / "agent.yaml.bak").exists()
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["future_key"]


def test_a_failed_write_leaves_both_files(tmp_path, monkeypatch):
    path = tmp_path / "agent.yaml"
    _write_config(path)
    save_yaml(load_yaml(AgentConfig, path), path, backup=True)
    config_before = path.read_bytes()
    bak_before = (tmp_path / "agent.yaml.bak").read_bytes()
    real = os.replace

    def failing(src, dst):
        if Path(dst) == path:
            raise OSError("disk full")
        real(src, dst)

    monkeypatch.setattr(config_mod.os, "replace", failing)
    cfg = load_yaml(AgentConfig, path)
    cfg.machine = "lost"
    with pytest.raises(OSError):
        save_yaml(cfg, path, backup=True)
    assert path.read_bytes() == config_before
    # the backup holds what the config file held before the attempt
    assert (tmp_path / "agent.yaml.bak").read_bytes() in (bak_before, config_before)
    assert not list(tmp_path.glob("*.tmp"))


# ── GET (R13) ───────────────────────────────────────────────────────────
def test_get_shows_saved_running_presets_and_no_secret(tmp_path, tools):
    world = World(tmp_path, enabled=True, **tools)
    response = world.get()
    assert response.status_code == 200
    body = response.json()
    assert body["saved"]["role"] == "controller"
    assert body["running"]["role"] == "controller"
    assert body["saved"]["inbox_root"] == tools["inbox_root"]
    assert body["restart_required"] is False
    assert body["busy"] == {"films": 0, "remote": 0}
    ids = [p["id"] for p in body["presets"]]
    assert ids[0] == "auto" and "5070ti-16" in ids
    assert LLM_KEY not in response.text and "api_token" not in response.text


def test_get_role_off_when_disabled(tmp_path):
    world = World(tmp_path)
    assert world.get().json()["saved"]["role"] == "off"


# ── PUT: round trip and restart (R15, R16) ──────────────────────────────
def test_an_idle_switch_saves_with_a_backup_and_keeps_everything(tmp_path, tools):
    world = World(tmp_path)
    before = world.path.read_bytes()
    response = world.put({"role": "controller", **tools, "remote_workers": True})
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert response.json()["restart_required"] is True
    assert response.json()["message"] == "已保存，重启 TaskPaw 后生效"
    saved = world.saved()
    assert saved["workflow"]["enabled"] is True
    assert saved["workflow"]["role"] == "controller"
    assert saved["workflow"]["remote_workers"] is True
    assert saved["workflow"]["inbox_root"] == tools["inbox_root"]
    # monitors, the LLM key, power peers and an unknown key survive
    assert saved["monitors"][0]["name"] == "keep-me"
    assert saved["llm_api_key"] == LLM_KEY
    assert saved["power"]["peers"][0]["address"] == "192.168.1.20"
    assert saved["future_key"] == {"kept": True}
    assert (tmp_path / "agent.yaml.bak").read_bytes() == before
    # the running config is untouched: it applies at the next start
    assert world.config.workflow.enabled is False
    view = world.get().json()
    assert view["restart_required"] is True
    assert view["saved"]["role"] == "controller" and view["running"]["role"] == "off"
    assert LLM_KEY not in response.text


def test_a_partial_put_keeps_the_other_saved_fields(tmp_path, tools):
    world = World(tmp_path, enabled=True, **tools, mdcx_root=str(tmp_path))
    response = world.put({"role": "controller", "remote_workers": True})
    assert response.status_code == 200, response.text
    saved = world.saved()["workflow"]
    assert saved["mdcx_root"] == str(tmp_path)
    assert saved["ffprobe_path"] == tools["ffprobe_path"]


def test_role_off_writes_enabled_false(tmp_path, tools):
    world = World(tmp_path, enabled=True, **tools)
    assert world.put({"role": "off"}).status_code == 200
    saved = world.saved()["workflow"]
    assert saved["enabled"] is False and saved["role"] == "controller"


def test_the_worker_role_needs_a_staging_dir(tmp_path, tools):
    world = World(tmp_path)
    response = world.put({"role": "worker"})
    assert response.status_code == 422
    assert "staging_dir" in response.json()["fields"]
    staging = tmp_path / "staging"
    response = world.put(
        {
            "role": "worker",
            "staging_dir": str(staging),
            "staging_cap_gb": 400,
            "ltx_gpu": "5060ti-16",
            "jasna_exe_path": tools["jasna_exe_path"],
        }
    )
    assert response.status_code == 200, response.text
    saved = world.saved()["workflow"]
    assert (saved["role"], saved["staging_cap_gb"], saved["ltx_gpu"]) == (
        "worker",
        400,
        "5060ti-16",
    )


def test_the_generic_config_patch_cannot_edit_the_workflow(tmp_path, tools):
    world = World(tmp_path)
    response = world.client.patch(
        "/control/config",
        json={"workflow": {"enabled": True, **tools}},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    assert response.status_code == 400
    assert world.saved()["workflow"] == {}


# ── validation (R14) ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("change", "field", "message"),
    [
        ({"inbox_root": "relative/inbox"}, "inbox_root", "必须是完整路径"),
        ({"inbox_root": None}, "inbox_root", "请填写"),
        ({"inbox_root": "\\\\nas\\inbox"}, "inbox_root", "不能是网络路径"),
        ({"ffprobe_path": None}, "ffprobe_path", "请填写"),
        ({"worker_api_port": 70000}, "worker_api_port", "1–65535"),
        ({"worker_api_port": 5680}, "worker_api_port", "网络端口"),
        ({"worker_api_port": 5681}, "worker_api_port", "控制端口"),
        ({"ltx_gpu": "titan-99"}, "ltx_gpu", "未知"),
        ({"remote_workers": "yes"}, "remote_workers", "格式不正确"),
        ({"surprise": 1}, "surprise", "未知字段"),
    ],
)
def test_validation_names_the_field_in_chinese(tmp_path, tools, change, field, message):
    world = World(tmp_path)
    before = world.path.read_bytes()
    response = world.put({"role": "controller", **tools, **change})
    assert response.status_code == 422, response.text
    fields = response.json()["fields"]
    assert message in fields[field]
    assert world.path.read_bytes() == before  # refused: the file is untouched
    assert not (tmp_path / "agent.yaml.bak").exists()


def test_paths_must_exist_and_have_no_dotdot(tmp_path, tools):
    world = World(tmp_path)
    missing = world.put(
        {"role": "controller", **tools, "inbox_root": str(tmp_path / "nope")}
    )
    assert "不存在" in missing.json()["fields"]["inbox_root"]
    dotted = world.put(
        {
            "role": "controller",
            **tools,
            "mdcx_root": str(tmp_path / "a" / ".." / "b"),
        }
    )
    assert ".." in dotted.json()["fields"]["mdcx_root"]
    no_file = world.put(
        {"role": "controller", **tools, "jasna_exe_path": str(tmp_path / "x.exe")}
    )
    assert "不存在" in no_file.json()["fields"]["jasna_exe_path"]


def test_a_field_error_never_echoes_the_value(tmp_path, tools):
    world = World(tmp_path)
    secret = "relative-" + LLM_KEY
    response = world.put({"role": "controller", **tools, "inbox_root": secret})
    assert response.status_code == 422
    assert secret not in response.text and LLM_KEY not in response.text


def test_the_data_dir_cannot_move_away_from_a_workflow_store(tmp_path, tools):
    old = tmp_path / "wf-old"
    old.mkdir()
    (old / "workflow.db").write_bytes(b"store")
    world = World(tmp_path, enabled=True, **tools, data_dir=str(old))
    response = world.put({"role": "controller", "data_dir": str(tmp_path / "wf-new")})
    assert response.status_code == 422
    assert response.json()["fields"]["data_dir"] == "工作目录已有媒体数据"
    # unchanged is fine
    same = world.put({"role": "controller", "data_dir": str(old)})
    assert same.status_code == 200, same.text


def test_the_default_data_dir_counts_as_the_old_one(tmp_path, tools):
    default = tmp_path / "workflow"  # beside agent.yaml
    default.mkdir()
    (default / "workflow.db").write_bytes(b"store")
    world = World(tmp_path, enabled=True, **tools)
    response = world.put({"role": "controller", "data_dir": str(tmp_path / "x")})
    assert response.json()["fields"]["data_dir"] == "工作目录已有媒体数据"


# ── busy refusal (R16) ──────────────────────────────────────────────────
def _running_host(rt):
    return SimpleNamespace(state="running", runtime=rt, reason="", fleet=None)


def test_leaving_the_controller_is_refused_while_films_are_in_flight(tmp_path, tools):
    world = World(tmp_path, enabled=True, remote_workers=True, **tools)
    rt = runtime(tmp_path / "wf")
    try:
        worker = enrol_remote(rt, "5070 Ti")
        remote_film, local_film = add_film(rt), add_film(rt)
        route(rt, remote_film, worker.worker_id)
        claim_remote(rt, worker, remote_film, "restore")
        route(rt, local_film, rt.identity.worker_id, kind="local")
        world.host = _running_host(rt)
        assert world.get().json()["busy"] == {"films": 2, "remote": 1}
        before = world.path.read_bytes()
        for body in (
            {"role": "off"},
            {"role": "worker", "staging_dir": str(tmp_path / "staging")},
            {"role": "controller", "remote_workers": False},
        ):
            response = world.put(body)
            assert response.status_code == 409, body
            assert response.json()["code"] == "workflow_busy"
            assert response.json()["detail"] == (
                "还有 2 部影片在处理中（1 部在 GPU 工作机上），请先暂停派发并等待完成"
            )
            assert world.path.read_bytes() == before
        # a change that strands nothing is still saved
        ok = world.put({"role": "controller", "mdcx_root": str(tmp_path)})
        assert ok.status_code == 200, ok.text
    finally:
        rt.close()


def test_an_open_archive_counts_as_busy(tmp_path, tools):
    world = World(tmp_path, enabled=True, **tools)

    class Store:
        def list_open_assignments(self, _op):
            return ()

        def list_routes(self, _op):
            return ()

        def film_board(self, _op, done_limit=None, **_kw):
            return (SimpleNamespace(film_id="f1", archive_open=True),)

    rt = SimpleNamespace(
        store=Store(), operator=None, identity=SimpleNamespace(worker_id="c")
    )
    world.host = SimpleNamespace(state="running", runtime=rt)
    response = world.put({"role": "off"})
    assert response.status_code == 409
    assert response.json()["detail"].startswith("还有 1 部影片在处理中（0 部")


def test_a_starting_controller_refuses_the_switch(tmp_path, tools):
    world = World(tmp_path, enabled=True, **tools)
    world.host = SimpleNamespace(state="starting", runtime=None)
    response = world.put({"role": "off"})
    assert response.status_code == 409 and response.json()["code"] == "workflow_busy"


def test_a_worker_with_routes_or_a_claim_cannot_leave(tmp_path):
    staging = str(tmp_path / "staging")
    world = World(tmp_path, enabled=True, role="worker", staging_dir=staging)
    dispatcher = SimpleNamespace(claimed=lambda: False)
    world.worker = SimpleNamespace(routes=[{"film": "f"}], dispatcher=dispatcher)
    before = world.path.read_bytes()
    response = world.put({"role": "off"})
    assert response.status_code == 409 and response.json()["code"] == "workflow_busy"
    assert world.path.read_bytes() == before
    world.worker = SimpleNamespace(
        routes=[], dispatcher=SimpleNamespace(claimed=lambda: True)
    )
    assert world.put({"role": "off"}).status_code == 409
    world.worker = SimpleNamespace(routes=[], dispatcher=None)
    assert world.put({"role": "off"}).status_code == 200


def test_the_task_log_and_logs_never_hold_the_file(tmp_path, tools, caplog):
    from taskpaw_v3.core.tasklog import get_task_log

    world = World(tmp_path)
    caplog.set_level(logging.DEBUG)
    assert world.put({"role": "controller", **tools}).status_code == 200
    assert LLM_KEY not in caplog.text and tools["inbox_root"] not in caplog.text
    rows = get_task_log().query(limit=20)
    text = repr(rows)
    assert LLM_KEY not in text and tools["inbox_root"] not in text
