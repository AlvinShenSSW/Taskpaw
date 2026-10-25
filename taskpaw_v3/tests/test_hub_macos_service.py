"""Mac Hub service manager: owned temporary files and fake launchctl only."""

from __future__ import annotations

import json
import logging
import os
import plistlib
import subprocess
import sys
import time

import pytest

from taskpaw_v3.hub import macos_service as service
from taskpaw_v3.packaging import backend_main

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file ownership")


@pytest.fixture
def manager(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    app = home / "Applications" / "TaskPaw Hub.app"
    source = app / "Contents" / "MacOS" / "taskpaw-backend"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"first verified frozen backend")
    source.chmod(0o755)
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "com.taskpaw.app.hub"})
    )
    obj = service.Manager(home, source)
    state = {"loaded": False, "ready": False, "calls": [], "fail_bootstrap": False}

    def registration():
        return (
            '{\n\t"Label" = "com.taskpaw.hub.v3";\n'
            '\t"LimitLoadToSessionType" = "Aqua";\n'
            f'\t"Program" = "{obj.runtime}";\n'
            '\t"ProgramArguments" = (\n'
            f'\t\t"{obj.runtime}";\n\t\t"hub-service";\n\t\t"run";\n'
            "\t);\n};\n"
        )

    def query(argv):
        state["calls"].append(argv)
        if argv[1] == "manageruid":
            return str(os.getuid()) + "\n"
        if argv[1] == "managername":
            return "Aqua\n"
        assert argv == ["/bin/launchctl", "list", service.LABEL]
        return state.get("registration", registration())

    def command(argv, **kwargs):
        state["calls"].append(argv)
        assert kwargs.get("timeout", 0) > 0
        if argv[0] == "/usr/bin/codesign":
            return 0
        assert argv[0] == "/bin/launchctl"
        action = argv[1]
        if action == "print":
            return 0 if argv[2] == obj.domain or state["loaded"] else 113
        if action == "bootstrap":
            if state["fail_bootstrap"]:
                state["fail_bootstrap"] = False
                return 5
            state["loaded"] = state["ready"] = True
            return 0
        if action == "bootout":
            state["loaded"] = state["ready"] = False
            return 0
        raise AssertionError(argv)

    monkeypatch.setattr(service, "_command", command)
    monkeypatch.setattr(service, "_query", query, raising=False)
    monkeypatch.setattr(obj, "_processes", lambda: [])
    monkeypatch.setattr(obj, "_ready", lambda: state["ready"])
    monkeypatch.setattr(obj, "_resources_released", lambda record: True)
    return obj, state, source


def test_install_runs_private_verified_copy_with_finite_launchd_policy(manager):
    obj, state, source = manager
    assert obj.execute("install")["state"] == "running"
    assert obj.runtime.read_bytes() == source.read_bytes()
    plist = plistlib.loads(obj.plist.read_bytes())
    assert plist["ProgramArguments"] == [str(obj.runtime), "hub-service", "run"]
    assert plist["KeepAlive"] is True
    assert 0 < plist["ExitTimeOut"] < service.STOP_TIMEOUT
    assert plist["ThrottleInterval"] >= 10
    assert plist["EnvironmentVariables"]["TMPDIR"] == str(obj.temp)
    assert "TOKEN" not in json.dumps(plist)
    assert obj.runtime.stat().st_mode & 0o777 == 0o700
    assert obj.manifest.stat().st_mode & 0o777 == 0o600
    assert state["loaded"]


def test_identical_reinstall_does_not_restart_running_service(manager):
    obj, state, _ = manager
    obj.execute("install")
    state["calls"].clear()
    assert obj.execute("install")["state"] == "running"
    assert not any(c[1] in {"bootstrap", "bootout"} for c in state["calls"])


def test_upgrade_failure_restores_previous_runtime_without_data_rollback(manager):
    obj, state, source = manager
    obj.execute("install")
    previous = obj.runtime.read_bytes()
    config = obj.home / "Library/Application Support/TaskPaw/hub.yaml"
    config.write_bytes(b"operator configuration")
    db = obj.home / ".taskpaw-hub/hub.db"
    db.parent.mkdir()
    db.write_bytes(b"latest sqlite state must be preserved")
    source.write_bytes(b"new failed runtime")
    state["fail_bootstrap"] = True
    with pytest.raises(service.ServiceError, match="upgrade_failed_rolled_back"):
        obj.execute("install")
    assert obj.runtime.read_bytes() == previous
    assert obj.execute("status")["state"] == "running"
    assert config.read_bytes() == b"operator configuration"
    assert db.read_bytes() == b"latest sqlite state must be preserved"


def test_stop_start_uninstall_preserve_configuration(manager):
    obj, state, _ = manager
    obj.execute("install")
    config = obj.root.parent / "hub.yaml"
    config.write_bytes(b"retained")
    assert obj.execute("stop")["state"] == "stopped"
    assert obj.runtime.exists()
    assert obj.execute("start")["state"] == "running"
    assert obj.execute("uninstall")["state"] == "not_installed"
    assert not obj.plist.exists() and not obj.runtime.exists()
    assert config.read_bytes() == b"retained"
    assert obj.execute("uninstall")["state"] == "not_installed"
    assert not state["loaded"]


def test_unowned_registered_label_is_never_booted_out(manager):
    obj, state, _ = manager
    state["loaded"] = True
    with pytest.raises(service.ServiceError, match="unowned_registration"):
        obj.execute("install")
    assert not any(c[1] == "bootout" for c in state["calls"])


@pytest.mark.parametrize("target", ["root", "plist", "runtime", "manifest"])
def test_symlink_is_rejected_without_following_target(manager, target):
    obj, state, _ = manager
    if target in {"runtime", "manifest"}:
        obj.execute("install")
        getattr(obj, target).unlink()
    else:
        getattr(obj, target).parent.mkdir(parents=True, exist_ok=True)
    victim = obj.home / "victim"
    victim.write_bytes(b"untouched")
    getattr(obj, target).symlink_to(victim)
    state["calls"].clear()
    with pytest.raises(service.ServiceError):
        obj.execute("install")
    assert victim.read_bytes() == b"untouched"
    assert not any(c[1] == "bootout" for c in state["calls"])


@pytest.mark.parametrize("target", ["runtime", "plist", "manifest"])
def test_external_edits_revoke_installation_ownership(manager, target):
    obj, state, _ = manager
    obj.execute("install")
    getattr(obj, target).write_bytes(b"external bytes")
    state["calls"].clear()
    with pytest.raises(service.ServiceError):
        obj.execute("uninstall")
    assert getattr(obj, target).read_bytes() == b"external bytes"
    assert not any(c[1] == "bootout" for c in state["calls"])


def test_unknown_stop_cleanup_never_overwrites_runtime(manager, monkeypatch):
    obj, _, source = manager
    obj.execute("install")
    previous = obj.runtime.read_bytes()
    source.write_bytes(b"new backend")
    monkeypatch.setattr(service, "STOP_TIMEOUT", 0.02)
    monkeypatch.setattr(obj, "_resources_released", lambda record: False)
    with pytest.raises(service.ServiceError, match="stop_incomplete"):
        obj.execute("install")
    assert obj.runtime.read_bytes() == previous
    assert obj.manifest.exists()


def test_launchctl_timeout_does_not_authorize_uninstall(manager, monkeypatch):
    obj, _, _ = manager
    obj.execute("install")
    original = service._command

    def timeout(argv, **kwargs):
        if argv[1] == "bootout":
            raise service.ServiceError("command_timeout")
        return original(argv, **kwargs)

    monkeypatch.setattr(service, "_command", timeout)
    with pytest.raises(service.ServiceError, match="command_timeout"):
        obj.execute("uninstall")
    assert obj.runtime.exists() and obj.manifest.exists() and obj.plist.exists()


def test_signature_failure_never_stops_old_installation(manager, monkeypatch):
    obj, state, source = manager
    obj.execute("install")
    previous = obj.runtime.read_bytes()
    source.write_bytes(b"unsigned update")
    original = service._command

    def invalid(argv, **kwargs):
        return 1 if argv[0] == "/usr/bin/codesign" else original(argv, **kwargs)

    monkeypatch.setattr(service, "_command", invalid)
    with pytest.raises(service.ServiceError, match="signature_invalid"):
        obj.execute("install")
    assert obj.runtime.read_bytes() == previous and state["loaded"]


def test_copy_failure_keeps_old_running_and_can_retry(manager, monkeypatch):
    obj, state, source = manager
    obj.execute("install")
    original = obj.runtime.read_bytes()
    source.write_bytes(b"updated backend")
    copy = service.shutil.copyfileobj
    monkeypatch.setattr(
        service.shutil,
        "copyfileobj",
        lambda *a: (_ for _ in ()).throw(OSError("disk full")),
    )
    with pytest.raises(OSError):
        obj.execute("install")
    assert obj.runtime.read_bytes() == original and state["loaded"]
    assert not obj.staged.exists()
    monkeypatch.setattr(service.shutil, "copyfileobj", copy)
    assert obj.execute("install")["state"] == "running"


def test_interrupted_upgrade_is_recoverable_before_reinstall(manager, monkeypatch):
    obj, state, source = manager
    obj.execute("install")
    source.write_bytes(b"updated backend")
    start = obj._start
    monkeypatch.setattr(
        obj, "_start", lambda: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        obj.execute("install")
    assert obj.journal.exists() and obj.previous.exists()
    assert obj.execute("status") == {"ok": False, "state": "upgrade_incomplete"}
    monkeypatch.setattr(obj, "_start", start)
    assert obj.execute("install")["state"] == "running"
    assert obj.runtime.read_bytes() == source.read_bytes()
    assert not obj.journal.exists() and not obj.previous.exists()
    assert state["loaded"]


def test_stop_after_interrupted_upgrade_does_not_restart_old_job(manager, monkeypatch):
    obj, state, source = manager
    obj.execute("install")
    source.write_bytes(b"updated backend")
    monkeypatch.setattr(
        obj, "_start", lambda: (_ for _ in ()).throw(KeyboardInterrupt())
    )
    with pytest.raises(KeyboardInterrupt):
        obj.execute("install")
    state["calls"].clear()
    assert obj.execute("stop")["state"] == "stopped"
    assert not any(c[1] == "bootstrap" for c in state["calls"])


@pytest.mark.parametrize("upgrade", [False, True])
def test_committed_upgrade_recovers_forward_after_backup_cleanup_crash(
    manager, monkeypatch, upgrade
):
    from pathlib import Path

    obj, state, source = manager
    if upgrade:
        obj.execute("install")
    source.write_bytes(b"verified successful upgrade")
    unlink = Path.unlink

    def interrupted(path, *args, **kwargs):
        result = unlink(path, *args, **kwargs)
        if path == obj.previous:
            raise KeyboardInterrupt()
        return result

    with monkeypatch.context() as patch:
        patch.setattr(Path, "unlink", interrupted)
        with pytest.raises(KeyboardInterrupt):
            obj.execute("install")
    assert obj.journal.exists() and not obj.previous.exists()
    state["calls"].clear()
    assert obj.execute("start")["state"] == "running"
    assert obj.runtime.read_bytes() == source.read_bytes()
    assert not obj.journal.exists()
    assert not any(c[1] in {"bootstrap", "bootout"} for c in state["calls"])


@pytest.mark.parametrize(
    "action", ["stop", "restart", "uninstall", "install", "start", "status"]
)
def test_replaced_live_registration_is_never_adopted_or_booted_out(manager, action):
    obj, state, _ = manager
    obj.execute("install")
    obj.execute("stop")
    state.update(
        loaded=True,
        ready=True,
        registration=(
            '{\n\t"Label" = "com.taskpaw.hub.v3";\n'
            '\t"LimitLoadToSessionType" = "Aqua";\n'
            '\t"Program" = "/usr/bin/false";\n'
            '\t"ProgramArguments" = (\n\t\t"/usr/bin/false";\n\t);\n};\n'
        ),
    )
    state["calls"].clear()
    with pytest.raises(service.ServiceError, match="registration"):
        obj.execute(action)
    assert state["loaded"]
    assert obj.runtime.exists() and obj.plist.exists()
    assert not any(c[1] in {"bootstrap", "bootout"} for c in state["calls"])


@pytest.mark.parametrize(
    "change",
    [
        "missing_args",
        "duplicate_label",
        "wrong_args",
        "wrong_session",
        "unknown_format",
        "malformed_program",
        "duplicate_args",
    ],
)
def test_unknown_or_conflicting_live_provenance_fails_closed(manager, change):
    obj, state, _ = manager
    obj.execute("install")
    text = service._query(["/bin/launchctl", "list", service.LABEL])
    if change == "missing_args":
        text = text.replace('"ProgramArguments"', '"UnknownArguments"')
    elif change == "duplicate_label":
        text = text.replace("{\n", '{\n\t"Label" = "com.taskpaw.hub.v3";\n', 1)
    elif change == "wrong_args":
        text = text.replace('"run";', '"stop";')
    elif change == "wrong_session":
        text = text.replace('"Aqua"', '"Background"')
    elif change == "malformed_program":
        text = text.replace('"Program" = ', '"Program": ')
    elif change == "duplicate_args":
        text = text.replace("{\n", '{\n\t"ProgramArguments" = false;\n', 1)
    else:
        text = text.replace(" = ", ": ")
    state["registration"] = text
    state["calls"].clear()
    with pytest.raises(service.ServiceError, match="registration"):
        obj.execute("stop")
    assert not any(c[1] == "bootout" for c in state["calls"])


def test_implicit_program_is_bound_by_complete_runtime_argv(manager):
    obj, state, _ = manager
    obj.execute("install")
    state["registration"] = service._query(
        ["/bin/launchctl", "list", service.LABEL]
    ).replace(f'\t"Program" = "{obj.runtime}";\n', "")
    assert obj.execute("stop")["state"] == "stopped"


@pytest.mark.parametrize(
    "query_name,result", [("manageruid", "0\n"), ("managername", "Background\n")]
)
def test_foreign_legacy_query_context_cannot_authorize_stop(
    manager, monkeypatch, query_name, result
):
    obj, state, _ = manager
    obj.execute("install")
    if query_name == "manageruid":
        result = str(os.getuid() + 1) + "\n"
    query = service._query
    monkeypatch.setattr(
        service, "_query", lambda argv: result if argv[1] == query_name else query(argv)
    )
    state["calls"].clear()
    with pytest.raises(service.ServiceError, match="registration_unknown"):
        obj.execute("stop")
    assert not any(c[1] == "bootout" for c in state["calls"])


@pytest.mark.parametrize("code", ["import time; time.sleep(60)", "print('x'*20000)"])
def test_registration_read_bounds_output_time_and_reaps_its_child(monkeypatch, code):
    children = []
    popen = service.subprocess.Popen

    def child(*args, **kwargs):
        proc = popen(*args, **kwargs)
        children.append(proc)
        return proc

    monkeypatch.setattr(service.subprocess, "Popen", child)
    monkeypatch.setattr(service, "COMMAND_TIMEOUT", 0.2)
    before = time.monotonic()
    with pytest.raises(service.ServiceError, match="registration_unknown"):
        service._query([sys.executable, "-c", code])
    assert time.monotonic() - before < 2
    assert children[0].poll() is not None and children[0].stdout.closed


def test_unknown_registration_error_is_not_treated_as_absent(manager, monkeypatch):
    obj, _, _ = manager
    original = service._command

    def unknown(argv, **kwargs):
        return 5 if argv[-1] == obj.target else original(argv, **kwargs)

    monkeypatch.setattr(service, "_command", unknown)
    with pytest.raises(service.ServiceError, match="registration_unknown"):
        obj.execute("install")
    assert not obj.runtime.exists()


def test_stop_waits_for_owned_helper_identity_even_after_parent_disappears(
    manager, monkeypatch
):
    obj, state, _ = manager
    obj.execute("install")
    monkeypatch.setattr(service, "STOP_TIMEOUT", 0.02)
    monkeypatch.setattr(
        obj, "_processes", lambda: [(12345, 100.0)] if state["loaded"] else []
    )
    monkeypatch.setattr(obj, "_alive", lambda identity: True)
    with pytest.raises(service.ServiceError, match="stop_incomplete"):
        obj.execute("uninstall")
    assert obj.runtime.exists() and obj.manifest.exists()


def test_runtime_resources_include_db_lease_and_listeners(manager, monkeypatch):
    from taskpaw_v3.core.state import FileLease, db_lease_path

    obj, _, _ = manager
    obj.execute("install")
    # This test checks the production observer, not the fixture's fake observer.
    database = obj.home / "state.db"
    record = {"db": str(database), "ports": [["127.0.0.1", 45678]]}
    with FileLease(db_lease_path(database)):
        assert not service.Manager._resources_released(obj, record)
    monkeypatch.setattr("taskpaw_v3.core.net.port_available", lambda *a: False)
    assert not service.Manager._resources_released(obj, record)
    monkeypatch.setattr("taskpaw_v3.core.net.port_available", lambda *a: True)
    assert service.Manager._resources_released(obj, record)


def test_log_rotation_bounds_large_prints(tmp_path, monkeypatch):
    from logging.handlers import RotatingFileHandler

    path = tmp_path / "hub.log"
    handler = RotatingFileHandler(path, maxBytes=32768, backupCount=1)
    stream = service._LogStream(handler)
    try:
        assert stream.write("x" * 200000) == 200000
        stream.flush()
        assert path.stat().st_size <= 32768
        assert path.with_name("hub.log.1").stat().st_size <= 32768
        assert len(list(tmp_path.iterdir())) == 2
    finally:
        handler.close()


def test_operation_is_serialized(manager):
    from taskpaw_v3.core.state import FileLease

    obj, _, _ = manager
    obj.execute("install")
    with FileLease(obj.lock):
        with pytest.raises(service.ServiceError, match="operation_busy"):
            obj.execute("stop")


def test_source_must_be_installed_hub_bundle(manager):
    obj, _, source = manager
    (source.parent.parent / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleIdentifier": "com.taskpaw.app.agent"})
    )
    with pytest.raises(service.ServiceError, match="hub_app_required"):
        obj.execute("install")


def test_platform_and_frozen_gate_precede_any_filesystem_or_service_action(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    assert service.main(["install"]) == 2
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service.sys, "frozen", False, raising=False)
    assert service.main(["install"]) == 2


def test_fixed_packaging_dispatch_passes_only_explicit_service_arguments(monkeypatch):
    calls = []
    monkeypatch.setattr(service, "main", lambda args: calls.append(args) or 0)
    assert backend_main.main(["hub-service", "status"]) == 0
    assert calls == [["status"]]


def test_external_commands_are_bounded_and_never_capture_secrets(monkeypatch):
    observed = []

    def run(argv, **kwargs):
        observed.append(kwargs)
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(service.subprocess, "run", run)
    assert service._command(["/bin/launchctl", "print", "gui/501"], timeout=2) == 0
    assert observed[0]["timeout"] == 2
    assert observed[0]["stdout"] == subprocess.DEVNULL
    assert observed[0]["stderr"] == subprocess.DEVNULL
    assert observed[0].get("shell", False) is False


def test_service_run_cannot_reclaim_other_hub(manager, monkeypatch):
    from taskpaw_v3.hub.server import service as headless

    obj, _, _ = manager
    obj.execute("install")
    called = []
    monkeypatch.setattr(
        headless, "run_from_config", lambda **kw: called.append(kw) or 0
    )
    monkeypatch.setattr(service.sys, "executable", str(obj.runtime))
    prior_handlers = list(logging.getLogger().handlers)
    assert obj.run() == 0
    assert called[0]["reclaim_stale"] is False
    assert logging.getLogger().handlers == prior_handlers


def test_nonreclaim_hub_startup_does_not_touch_occupied_peer(tmp_path, monkeypatch):
    from taskpaw_v3.core.config import HubConfig
    from taskpaw_v3.hub.server.app import run_hub
    from taskpaw_v3.hub.server.store import HubStore

    def forbidden(*args, **kwargs):
        pytest.fail("service mode must never reclaim any listener")

    monkeypatch.setattr(
        "taskpaw_v3.core.net.reclaim_ports_from_stale_instance", forbidden
    )
    monkeypatch.setattr(
        "taskpaw_v3.core.net.claim_port",
        lambda *a: (_ for _ in ()).throw(OSError("occupied")),
    )
    store = HubStore(tmp_path / "hub.db")
    try:
        with pytest.raises(OSError, match="occupied"):
            run_hub(HubConfig(), store, reclaim_stale=False)
    finally:
        store.close()
