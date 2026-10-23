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


@pytest.fixture(autouse=True)
def no_ffprobe_on_path(monkeypatch):
    """The host's PATH never decides a test: no ffprobe is found unless a
    test says so (§9 IR6)."""
    from taskpaw_v3.agent.server import workflow_settings as ws

    monkeypatch.setattr(ws, "detect_ffprobe", lambda: None)


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
    assert body["busy"] == {"films": 0, "remote": 0, "unknown": False}
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
    runtime(old).close()  # an idle store (unreadable is not idle, CX15)
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
    world = World(
        tmp_path,
        enabled=True,
        remote_workers=True,
        data_dir=str(tmp_path / "wf"),
        **tools,
    )
    rt = runtime(tmp_path / "wf")
    try:
        worker = enrol_remote(rt, "5070 Ti")
        remote_film, local_film = add_film(rt), add_film(rt)
        route(rt, remote_film, worker.worker_id)
        claim_remote(rt, worker, remote_film, "restore")
        route(rt, local_film, rt.identity.worker_id, kind="local")
        world.host = _running_host(rt)
        busy = world.get().json()["busy"]
        assert busy == {"films": 2, "remote": 1, "unknown": False}
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
    import sqlite3

    data = tmp_path / "wf"
    rt = runtime(data)
    try:
        film = add_film(rt)
        principal = rt.identity.operator_principal_id
    finally:
        rt.close()
    conn = sqlite3.connect(data / "workflow.db")
    try:  # an archive operation admitted and not yet resolved
        conn.execute(
            "INSERT INTO operations (operation_id,film_id,kind,owner_principal_id,"
            "status,created_at,updated_at,body_json)"
            " VALUES ('op1',?,'archive',?,'admitted','t','t','{}')",
            (film, principal),
        )
        conn.commit()
    finally:
        conn.close()
    world = World(tmp_path, enabled=True, data_dir=str(data), **tools)
    response = world.put({"role": "off"})
    assert response.status_code == 409
    assert response.json()["detail"].startswith("还有 1 部影片在处理中（0 部")


UNKNOWN = "无法确认是否有影片在处理，暂不允许切换"
IN_FLIGHT_1_1 = "还有 1 部影片在处理中（1 部在 GPU 工作机上），请先暂停派发并等待完成"


def _store_in_flight(directory: Path) -> None:
    """A retained store (no host running it) with one film routed to and
    claimed by a GPU worker: an unresolved remote compute assignment."""
    rt = runtime(directory)
    try:
        worker = enrol_remote(rt, "5070 Ti")
        film = add_film(rt)
        route(rt, film, worker.worker_id)
        claim_remote(rt, worker, film, "restore")
    finally:
        rt.close()


@pytest.mark.parametrize(
    "host",
    [
        None,
        SimpleNamespace(state="failed", runtime=None, reason="boom"),
        SimpleNamespace(state="starting", runtime=None, reason=""),
    ],
)
def test_the_controller_guard_reads_the_retained_store_whatever_the_host(
    tmp_path, tools, host
):
    """§9 CX15 (Codex r5): a controller that failed to start (or is still
    starting, or never ran) has no live runtime, but its store retains an
    unresolved remote assignment: leaving 主控 or turning GPU workers off is
    refused from the store on disk."""
    data = tmp_path / "wf"
    _store_in_flight(data)
    world = World(
        tmp_path, enabled=True, remote_workers=True, data_dir=str(data), **tools
    )
    world.host = host
    assert world.get().json()["busy"] == {"films": 1, "remote": 1, "unknown": False}
    before = world.path.read_bytes()
    for body in (
        {"role": "off"},
        {"role": "controller", "remote_workers": False},
        {"role": "off", "data_dir": str(data)},
    ):
        response = world.put(body)
        assert response.status_code == 409, body
        assert response.json() == {"detail": IN_FLIGHT_1_1, "code": "workflow_busy"}
        assert world.path.read_bytes() == before
    ok = world.put({"role": "controller", "mdcx_root": str(tmp_path)})
    assert ok.status_code == 200, ok.text


def test_a_store_holding_only_local_work_may_turn_gpu_workers_off(tmp_path, tools):
    """R16: turning 允许 GPU 工作机接入 off strands only work on a GPU worker;
    leaving 主控 strands any."""
    data = tmp_path / "wf"
    rt = runtime(data)
    try:
        film = add_film(rt)
        route(rt, film, rt.identity.worker_id, kind="local")
    finally:
        rt.close()
    world = World(
        tmp_path, enabled=True, remote_workers=True, data_dir=str(data), **tools
    )
    assert world.put({"role": "off"}).status_code == 409
    off = world.put({"role": "controller", "remote_workers": False})
    assert off.status_code == 200, off.text


@pytest.mark.parametrize("damage", ["corrupt", "locked"])
def test_an_unreadable_store_refuses_leaving_the_controller(
    tmp_path, tools, monkeypatch, damage
):
    """§9 CX15: a store that cannot be read is not idle — the guarded change
    is refused with 「无法确认是否有影片在处理，暂不允许切换」; staying 主控 with
    GPU workers on strands nothing and is saved."""
    import sqlite3

    from taskpaw_v3.agent.server import workflow_settings as ws

    data = tmp_path / "wf"
    runtime(data).close()
    if damage == "corrupt":
        (data / "workflow.db").write_bytes(b"not a database" * 64)
    else:

        def locked(_path):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(ws, "_open_store", locked)
    world = World(
        tmp_path, enabled=True, remote_workers=True, data_dir=str(data), **tools
    )
    assert world.get().json()["busy"]["unknown"] is True
    before = world.path.read_bytes()
    for body in ({"role": "off"}, {"role": "controller", "remote_workers": False}):
        response = world.put(body)
        assert response.status_code == 409, body
        assert response.json() == {"detail": UNKNOWN, "code": "workflow_busy"}
        assert world.path.read_bytes() == before
    stay = world.put({"role": "controller", "mdcx_root": str(tmp_path)})
    assert stay.status_code == 200, stay.text


def _worker(routes: int, busy: bool):
    """The worker host as the settings guard reads it: `summary()` (§9 CX6)."""
    return SimpleNamespace(summary=lambda: {"routes": routes, "busy": busy})


def test_a_restarted_worker_with_unsettled_attempts_is_busy_without_a_controller(
    tmp_path,
):
    """§9 CX6 (Codex r2): after a restart with the controller unreachable there
    is no dispatcher and no route, but an attempt file on the staging disk is
    unsettled work: the real host reads busy, so the role and directories
    stay put until it settles."""
    from taskpaw_v3.core.config import WorkflowSettings
    from taskpaw_v3.workflow.remote_dispatch import ATTEMPTS_DIR
    from taskpaw_v3.workflow.remote_worker import WorkerHost

    staging = tmp_path / "staging"
    (staging / ATTEMPTS_DIR).mkdir(parents=True)
    attempt = staging / ATTEMPTS_DIR / "a1.json"
    attempt.write_text("{}", encoding="utf-8")
    settings = WorkflowSettings(enabled=True, role="worker", staging_dir=str(staging))
    world = World(tmp_path, enabled=True, role="worker", staging_dir=str(staging))
    world.worker = WorkerHost(settings, tmp_path / "workflow")
    assert world.worker.dispatcher is None
    before = world.path.read_bytes()
    for body in (
        {"role": "off"},
        {"role": "worker", "staging_dir": str(tmp_path / "staging-2")},
        {
            "role": "worker",
            "staging_dir": str(staging),
            "data_dir": str(tmp_path / "x"),
        },
    ):
        response = world.put(body)
        assert response.status_code == 409, body
        assert response.json()["code"] == "workflow_busy"
        assert world.path.read_bytes() == before
    attempt.unlink()  # settled
    assert world.put({"role": "off"}).status_code == 200


def test_a_worker_with_routes_or_a_claim_cannot_leave(tmp_path):
    staging = str(tmp_path / "staging")
    world = World(tmp_path, enabled=True, role="worker", staging_dir=staging)
    world.worker = _worker(routes=1, busy=False)
    before = world.path.read_bytes()
    response = world.put({"role": "off"})
    assert response.status_code == 409 and response.json()["code"] == "workflow_busy"
    assert world.path.read_bytes() == before
    world.worker = _worker(routes=0, busy=True)
    assert world.put({"role": "off"}).status_code == 409
    world.worker = _worker(routes=0, busy=False)
    assert world.put({"role": "off"}).status_code == 200


def test_a_busy_worker_cannot_move_its_staging_or_data_dir(tmp_path):
    """§9 IR3: moving the staging or the data directory strands the routes,
    claims and held children as leaving the role does."""
    staging = str(tmp_path / "staging")
    world = World(tmp_path, enabled=True, role="worker", staging_dir=staging)
    world.worker = _worker(routes=0, busy=True)
    before = world.path.read_bytes()
    for change in (
        {"staging_dir": str(tmp_path / "staging-2")},
        {"data_dir": str(tmp_path / "wf-2")},
    ):
        response = world.put({"role": "worker", "staging_dir": staging, **change})
        assert response.status_code == 409, change
        assert response.json()["code"] == "workflow_busy"
        assert world.path.read_bytes() == before
    same = world.put({"role": "worker", "staging_dir": staging, "staging_cap_gb": 300})
    assert same.status_code == 200, same.text
    world.worker = _worker(routes=0, busy=False)  # idle: it may move
    moved = world.put({"role": "worker", "staging_dir": str(tmp_path / "staging-2")})
    assert moved.status_code == 200, moved.text


def test_the_data_dir_guard_applies_to_every_role(tmp_path, tools):
    """§9 CX3: turning the workflow off does not let its data directory move
    away from the store either."""
    old = tmp_path / "wf-old"
    old.mkdir()
    (old / "workflow.db").write_bytes(b"store")
    world = World(tmp_path, enabled=True, **tools, data_dir=str(old))
    response = world.put({"role": "off", "data_dir": str(tmp_path / "wf-new")})
    assert response.status_code == 422
    assert response.json()["fields"]["data_dir"] == "工作目录已有媒体数据"


def test_the_data_dir_guard_compares_with_the_running_directory(tmp_path, tools):
    """§9 CX3: a store the running process created after an earlier save
    still holds the directory, whatever the saved value says."""
    world = World(tmp_path, enabled=True, **tools)  # running: the default dir
    other = str(tmp_path / "wf-b")
    assert world.put({"role": "controller", "data_dir": other}).status_code == 200
    default = tmp_path / "workflow"
    default.mkdir()
    runtime(default).close()  # an idle store (unreadable is not idle, CX15)
    for target in (str(tmp_path / "wf-c"), other):
        response = world.put({"role": "controller", "data_dir": target})
        assert response.status_code == 422, target
        assert response.json()["fields"]["data_dir"] == "工作目录已有媒体数据"
    back = world.put({"role": "controller", "data_dir": None})
    assert back.status_code == 200, back.text


def test_a_paired_workers_data_dir_cannot_move(tmp_path):
    """§9 IR5: the worker's data directory holds `worker-identity.json`;
    moving it would unpair the worker."""
    from taskpaw_v3.workflow.remote_worker import IDENTITY_NAME

    staging = str(tmp_path / "staging")
    paired = tmp_path / "workflow"  # the default, beside agent.yaml
    paired.mkdir()
    (paired / IDENTITY_NAME).write_text("{}", encoding="utf-8")
    world = World(tmp_path, enabled=True, role="worker", staging_dir=staging)
    for role in ("worker", "off"):
        response = world.put(
            {"role": role, "staging_dir": staging, "data_dir": str(tmp_path / "x")}
        )
        assert response.status_code == 422, role
        assert response.json()["fields"]["data_dir"] == "工作目录已有工作机配对"


def test_a_paired_workers_staging_dir_cannot_change_but_an_unpaired_one_can(tmp_path):
    """§9 CX8 (Codex r2): a paired worker's staging directory lives in
    `worker-identity.json` (chosen at 加入), so saving a new one to agent.yaml
    would change nothing: refused with a field error. Unpaired: allowed."""
    from taskpaw_v3.workflow.remote_worker import IDENTITY_NAME

    staging = str(tmp_path / "staging")
    world = World(tmp_path, enabled=True, role="worker", staging_dir=staging)
    other = str(tmp_path / "staging-2")
    paired = tmp_path / "workflow"  # the default, beside agent.yaml
    _pair(paired, staging)
    before = world.path.read_bytes()
    response = world.put({"role": "worker", "staging_dir": other})
    assert response.status_code == 422, response.text
    assert response.json()["fields"]["staging_dir"] == STAGING_PAIRED
    assert world.path.read_bytes() == before
    same = world.put({"role": "worker", "staging_dir": staging, "staging_cap_gb": 300})
    assert same.status_code == 200, same.text
    (paired / IDENTITY_NAME).unlink()  # not paired (any more)
    moved = world.put({"role": "worker", "staging_dir": other})
    assert moved.status_code == 200, moved.text


STAGING_PAIRED = "已配对的工作机改暂存目录需要重新加入"


def _pair(directory: Path, staging: str, *, pending: bool = False) -> None:
    """A worker identity (fake controller, fake credential) whose staging
    directory was chosen at 加入; `pending`: a pairing not yet confirmed."""
    from pydantic import SecretStr

    from taskpaw_v3.workflow.remote_worker import (
        IDENTITY_NAME,
        PENDING_NAME,
        WorkerIdentity,
    )

    directory.mkdir(parents=True, exist_ok=True)
    WorkerIdentity(
        controller_host="192.0.2.1",
        controller_port=5682,
        pairing_id="pairing-1",
        credential_id="credential-1",
        secret=SecretStr("fake-secret-for-tests"),
        staging_dir=staging,
        worker_id="worker-1",
    ).save(directory / (PENDING_NAME if pending else IDENTITY_NAME))


def test_a_partial_put_on_a_paired_worker_keeps_the_identity_staging_dir(tmp_path):
    """§9 CX24 (Codex r8): the identity's staging directory differs from the
    one agent.yaml holds (chosen at 加入); a save that does not touch staging
    — omitted or repeating the YAML value — is not a change. Only an explicit
    move to another directory is refused."""
    yaml_dir = str(tmp_path / "staging-yaml")
    chosen = str(tmp_path / "staging-chosen")
    _pair(tmp_path / "workflow", chosen)
    world = World(tmp_path, enabled=True, role="worker", staging_dir=yaml_dir)
    before = world.path.read_bytes()
    other = world.put({"role": "off", "staging_dir": str(tmp_path / "staging-x")})
    assert other.status_code == 422, other.text
    assert other.json()["fields"]["staging_dir"] == STAGING_PAIRED
    assert world.path.read_bytes() == before
    off = world.put({"role": "off"})
    assert off.status_code == 200, off.text
    again = world.put({"role": "off", "staging_dir": yaml_dir})
    assert again.status_code == 200, again.text
    chose = world.put({"role": "worker", "staging_dir": chosen})
    assert chose.status_code == 200, chose.text


def test_save_yaml_backups_and_temp_files_are_gitignored(tmp_path):
    """§9 CX23 (Codex r8): `agent.yaml.bak` and the `.tmp` of every atomic
    write hold the same secrets as the file itself."""
    import shutil
    import subprocess

    root = Path(__file__).resolve().parents[2]
    repo = tmp_path / "repo"
    repo.mkdir()
    shutil.copy(root / ".gitignore", repo / ".gitignore")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    names = [
        "agent.yaml",
        "agent.yaml.bak",
        "agent.yaml.tmp",
        "hub.yaml",
        "hub.yaml.bak",
        "hub.yaml.tmp",
    ]
    for name in names:
        result = subprocess.run(
            ["git", "check-ignore", "-q", name], cwd=repo, check=False
        )
        assert result.returncode == 0, name
    tracked = subprocess.run(
        ["git", "check-ignore", "-q", "agent.example.yaml"], cwd=repo, check=False
    )
    assert tracked.returncode == 1


@pytest.mark.parametrize("pending", [False, True])
def test_a_paired_staging_dir_holds_for_every_role_across_saves(tmp_path, pending):
    """§9 CX16 (Codex r5): the guard compares with the identity's staging
    directory, not the previous agent.yaml value or the requested role — so
    {role: off, staging B} cannot pass and let a later {role: worker} keep
    B while the identity still uses A."""
    a, b = str(tmp_path / "staging-a"), str(tmp_path / "staging-b")
    _pair(tmp_path / "workflow", a, pending=pending)
    world = World(tmp_path, enabled=True, role="worker", staging_dir=a)
    before = world.path.read_bytes()
    for body in (
        {"role": "off", "staging_dir": b},
        {"role": "controller", "staging_dir": b},
        {"role": "worker", "staging_dir": b},
        {"role": "off", "staging_dir": None},
    ):
        response = world.put(body)
        assert response.status_code == 422, body
        assert response.json()["fields"]["staging_dir"] == STAGING_PAIRED
        assert world.path.read_bytes() == before
    off = world.put({"role": "off"})  # the staging directory stays A
    assert off.status_code == 200, off.text
    back = world.put({"role": "worker"})
    assert back.status_code == 200, back.text
    assert world.saved()["workflow"]["staging_dir"] == a


def test_a_paired_worker_with_unsettled_attempts_is_busy_with_no_host(tmp_path):
    """§9 CX15/CX16: a worker that failed to start (no host) still holds the
    attempts on its paired staging disk: the conservative `worker_busy` over
    the identity's staging directory refuses leaving the role."""
    from taskpaw_v3.workflow.remote_dispatch import ATTEMPTS_DIR

    staging = tmp_path / "staging"
    (staging / ATTEMPTS_DIR).mkdir(parents=True)
    attempt = staging / ATTEMPTS_DIR / "a1.json"
    attempt.write_text("{}", encoding="utf-8")
    _pair(tmp_path / "workflow", str(staging))
    world = World(tmp_path, enabled=True, role="worker", staging_dir=str(staging))
    assert world.worker is None
    response = world.put({"role": "off"})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "workflow_busy"
    attempt.unlink()
    assert world.put({"role": "off"}).status_code == 200


@pytest.mark.parametrize(
    "name", ["worker-identity.json", "worker-identity.pending.json"]
)
def test_an_unreadable_identity_refuses_the_switch(tmp_path, name):
    """§9 CX16: an identity that cannot be read hides the paired staging
    directory and its attempts: unknown is not idle, so the save is refused
    with 「无法确认是否有影片在处理，暂不允许切换」 until a new 加入 replaces it."""
    staging = str(tmp_path / "staging")
    paired = tmp_path / "workflow"
    paired.mkdir()
    (paired / name).write_text("{not json", encoding="utf-8")
    world = World(tmp_path, enabled=True, role="worker", staging_dir=staging)
    before = world.path.read_bytes()
    for body in ({"role": "off"}, {"role": "worker", "staging_dir": staging}):
        response = world.put(body)
        assert response.status_code == 409, body
        assert response.json() == {"detail": UNKNOWN, "code": "workflow_busy"}
        assert world.path.read_bytes() == before


def test_a_data_dir_differing_only_by_case_follows_the_filesystem(
    tmp_path, tools, monkeypatch
):
    """§9 CX9 (Codex r2): `normcase` decides, not a blanket casefold. On a
    case-sensitive filesystem `Data` and `data` are two directories (moving
    from a held one is refused); with Windows semantics they are the same."""
    import ntpath

    old = tmp_path / "Data"
    old.mkdir()
    runtime(old).close()  # an idle store (unreadable is not idle, CX15)
    world = World(tmp_path, enabled=True, **tools, data_dir=str(old))
    target = str(tmp_path / "dAtA")
    # not present on a case-sensitive disk; present-and-same on a case-insensitive one:
    monkeypatch.setattr(os.path, "normcase", lambda p: os.fspath(p))  # POSIX semantics
    real_samefile = os.path.samefile
    monkeypatch.setattr(os.path, "samefile", lambda a, b: False)
    refused = world.put({"role": "controller", "data_dir": target})
    assert refused.status_code == 422, refused.text
    assert refused.json()["fields"]["data_dir"] == "工作目录已有媒体数据"
    monkeypatch.setattr(os.path, "samefile", real_samefile)
    monkeypatch.setattr(os.path, "normcase", ntpath.normcase)  # Windows semantics
    same = world.put({"role": "controller", "data_dir": target})
    assert same.status_code == 200, same.text


def test_same_path_uses_the_filesystem_when_both_exist(tmp_path, monkeypatch):
    from taskpaw_v3.agent.server import workflow_settings as ws

    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    assert not ws._same_path(a, b)
    monkeypatch.setattr(os.path, "samefile", lambda x, y: True)
    assert ws._same_path(a, b)  # both exist: the filesystem says one directory
    assert not ws._same_path(a, tmp_path / "missing")  # one missing: names decide
    assert ws._same_path(a, tmp_path / "x" / ".." / "a")  # normalised names


def test_the_worker_port_cannot_change_under_paired_workers(tmp_path, tools):
    """§9 IR4: enrolled workers reach the controller at the running port
    (the running host's store is in the running data directory)."""
    world = World(
        tmp_path,
        enabled=True,
        remote_workers=True,
        data_dir=str(tmp_path / "wf"),
        **tools,
    )
    rt = runtime(tmp_path / "wf")
    try:
        world.host = _running_host(rt)
        enrol_remote(rt, "5070 Ti")
        unchanged = world.put({"role": "controller", "worker_api_port": 5682})
        assert unchanged.status_code == 200, unchanged.text
        before = world.path.read_bytes()
        response = world.put({"role": "controller", "worker_api_port": 5691})
        assert response.status_code == 422
        assert response.json()["fields"]["worker_api_port"] == (
            "已有工作机配对在此端口，改端口需先让它们重新配对"
        )
        assert world.path.read_bytes() == before
    finally:
        rt.close()


def test_the_worker_port_can_change_with_no_paired_worker(tmp_path, tools):
    world = World(tmp_path, enabled=True, remote_workers=True, **tools)
    rt = runtime(tmp_path / "wf")
    try:
        world.host = _running_host(rt)
        response = world.put({"role": "controller", "worker_api_port": 5690})
        assert response.status_code == 200, response.text
    finally:
        rt.close()


def test_the_worker_port_stays_guarded_after_a_disable_and_restart(tmp_path, tools):
    """§9 CX11: the guard reads the pairings the store retains, not only the
    running host, so turning 主控 off, restarting and turning it back on with
    a new port cannot orphan the enrolled workers."""
    rt = runtime(tmp_path / "wf")
    try:
        enrol_remote(rt, "5070 Ti")
    finally:
        rt.close()
    # the restarted agent runs with the role off: no host, the store on disk
    world = World(
        tmp_path,
        enabled=False,
        remote_workers=True,
        data_dir=str(tmp_path / "wf"),
        **tools,
    )
    assert world.host is None
    before = world.path.read_bytes()
    response = world.put({"role": "controller", "worker_api_port": 5691})
    assert response.status_code == 422, response.text
    assert response.json()["fields"]["worker_api_port"] == (
        "已有工作机配对在此端口，改端口需先让它们重新配对"
    )
    assert world.path.read_bytes() == before
    same = world.put({"role": "controller", "worker_api_port": 5682})
    assert same.status_code == 200, same.text


def test_the_port_is_free_to_change_when_the_retained_store_pairs_nobody(
    tmp_path, tools
):
    rt = runtime(tmp_path / "wf")
    rt.close()  # only the controller's own record
    world = World(
        tmp_path,
        enabled=False,
        remote_workers=True,
        data_dir=str(tmp_path / "wf"),
        **tools,
    )
    response = world.put({"role": "controller", "worker_api_port": 5691})
    assert response.status_code == 200, response.text


PORT_PAIRED = "已有工作机配对在此端口，改端口需先让它们重新配对"


def _paired_store(directory: Path, port: int) -> None:
    """A workflow store with one device added (paired) at `port`."""
    from taskpaw_v3.workflow import devices

    rt = runtime(directory)
    try:
        devices.add_device(rt, "5070 Ti", "192.0.2.30", host="192.0.2.1", port=port)
    finally:
        rt.close()


def test_an_off_role_save_cannot_move_the_port_away_from_the_pairings(tmp_path, tools):
    """§9 CX14 (Codex r4): the baseline is the port the pairings were made on,
    not the running or saved config — an off-role save with a new port is
    refused, whatever role it asks for."""
    data = tmp_path / "wf"
    _paired_store(data, 5682)
    world = World(
        tmp_path, enabled=True, remote_workers=True, data_dir=str(data), **tools
    )
    before = world.path.read_bytes()
    for body in (
        {"role": "off", "data_dir": str(data), "worker_api_port": 5691},
        {
            "role": "worker",
            "data_dir": str(data),
            "worker_api_port": 5691,
            "staging_dir": str(tmp_path / "staging"),
        },
    ):
        response = world.put(body)
        assert response.status_code == 422, body
        assert response.json()["fields"]["worker_api_port"] == PORT_PAIRED
        assert world.path.read_bytes() == before
    same = world.put({"role": "off", "data_dir": str(data)})
    assert same.status_code == 200, same.text


def test_a_controller_save_is_refused_when_the_running_port_left_the_pairings(
    tmp_path, tools
):
    """§9 CX14: the running port already differs from the pairings' (an
    earlier bypass, a hand edit): a 主控 save keeping it is refused; returning
    to the pairing port works."""
    data = tmp_path / "wf"
    _paired_store(data, 5682)
    world = World(
        tmp_path,
        enabled=False,
        remote_workers=True,
        data_dir=str(data),
        worker_api_port=5691,
        **tools,
    )
    response = world.put({"role": "controller", "worker_api_port": 5691})
    assert response.status_code == 422, response.text
    assert response.json()["fields"]["worker_api_port"] == PORT_PAIRED
    back = world.put({"role": "controller", "worker_api_port": 5682})
    assert back.status_code == 200, back.text


def test_with_no_pairings_any_role_may_change_the_port(tmp_path, tools):
    data = tmp_path / "wf"
    runtime(data).close()  # only the controller's own record
    world = World(
        tmp_path, enabled=True, remote_workers=True, data_dir=str(data), **tools
    )
    for body in (
        {"role": "off", "data_dir": str(data), "worker_api_port": 5691},
        {"role": "controller", "worker_api_port": 5692},
    ):
        response = world.put(body)
        assert response.status_code == 200, response.text


def test_an_empty_ffprobe_is_fine_when_it_is_on_the_path(tmp_path, tools, monkeypatch):
    """§9 IR6: zero-config — the pipeline finds ffprobe on PATH by itself."""
    from taskpaw_v3.agent.server import workflow_settings as ws

    found = str(tmp_path / "bin" / "ffprobe.exe")
    monkeypatch.setattr(ws, "detect_ffprobe", lambda: found)
    world = World(tmp_path)
    response = world.put({"role": "controller", **tools, "ffprobe_path": None})
    assert response.status_code == 200, response.text
    assert world.saved()["workflow"]["ffprobe_path"] is None
    assert world.get().json()["ffprobe_detected"] == found


def test_a_concurrent_save_answers_409_not_a_field_error(tmp_path, tools, monkeypatch):
    """§9 IR12: the admission lock is held by another save."""
    world = World(tmp_path)

    def busy(*_a, **_k):
        raise ValueError("operation_busy")

    monkeypatch.setattr(world.admin, "update_config", busy)
    response = world.put({"role": "controller", **tools})
    assert response.status_code == 409
    assert response.json() == {
        "detail": "正在保存其他设置，请稍后再试",
        "code": "operation_busy",
    }


def test_the_task_log_and_logs_never_hold_the_file(tmp_path, tools, caplog):
    from taskpaw_v3.core.tasklog import get_task_log

    world = World(tmp_path)
    caplog.set_level(logging.DEBUG)
    assert world.put({"role": "controller", **tools}).status_code == 200
    assert LLM_KEY not in caplog.text and tools["inbox_root"] not in caplog.text
    rows = get_task_log().query(limit=20)
    text = repr(rows)
    assert LLM_KEY not in text and tools["inbox_root"] not in text


def test_a_failed_save_answers_500_and_leaves_the_file(tmp_path, tools, monkeypatch):
    import taskpaw_v3.agent.server.admin as admin_mod

    world = World(tmp_path)
    before = world.path.read_bytes()

    def boom(*_a, **_k):
        raise OSError(str(world.path))  # a path, never echoed

    monkeypatch.setattr(admin_mod, "save_yaml", boom)
    response = world.put({"role": "controller", **tools})
    assert response.status_code == 500
    assert str(world.path) not in response.text
    assert world.path.read_bytes() == before
    assert world.get().json()["saved"]["role"] == "off"  # nothing committed
