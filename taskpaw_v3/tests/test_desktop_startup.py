"""Desktop startup and explicitly confirmed offline actions, in temp fixtures."""

import errno
import json
from dataclasses import asdict

import pytest

from taskpaw_v3.agent import state as offline
from taskpaw_v3.agent.server import service
from taskpaw_v3.core.config import AgentConfig, load_yaml, save_yaml
from taskpaw_v3.core.net import PortInUseError
from taskpaw_v3.core.startup import STARTUP_CODES, emit_startup_error, startup_code
from taskpaw_v3.core.state import (
    FileLease,
    StateError,
    StateSession,
    initialize_state,
    read_record,
    state_paths,
)
from taskpaw_v3.packaging import backend_main


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "agent.yaml"
    save_yaml(
        AgentConfig(
            server_id="upgrade-fixture", machine="fixture", api_token="fake-secret"
        ),
        path,
    )
    monkeypatch.setattr(service, "default_config_path", lambda: path)
    return path


def desktop(action):
    flag = {
        "migrate": "--confirm-intact-legacy-counter",
        "initialize": "--confirm-new-pairing",
    }[action]
    return backend_main.main(["agent-desktop-state", action, flag])


def test_desktop_migration_keeps_legacy_counter_identity_and_exact_backup(
    config_path, capsys
):
    path = config_path.with_name("agent.state.json")
    original = b'{"next_event_id": 32560}\n'
    path.write_bytes(original)
    config_before = config_path.read_bytes()
    # A genuine failed upgrade start may already have preserved fault evidence.
    with pytest.raises(StateError, match="migration_required"):
        StateSession.open(path, "upgrade-fixture")
    assert desktop("migrate") == 0
    assert json.loads(capsys.readouterr().out) == {"result": "migrate"}
    record = read_record(path)
    assert record.server_id == "upgrade-fixture"
    assert record.next_event_id == 32560 and record.lineage_origin == "legacy_migration"
    assert read_record(state_paths(path)[1]) == record
    assert config_path.read_bytes() == config_before
    backups = list(path.parent.glob(path.name + ".fault-*"))
    assert backups and all(p.read_bytes() == original for p in backups)
    snapshot = {
        p: p.read_bytes() for p in (config_path, *state_paths(path)[:2], *backups)
    }
    assert desktop("migrate") == 2  # Duplicate confirmation cannot reset or rewrite.
    assert all(p.read_bytes() == raw for p, raw in snapshot.items())
    session = StateSession.open(path, "upgrade-fixture")
    try:
        assert session.resume_floor == 32559 and session.record.next_event_id == 32560
    finally:
        session.close()


def test_desktop_first_initialization_then_repeat_refuses(config_path, capsys):
    path = config_path.with_name("agent.state.json")
    assert desktop("initialize") == 0
    assert json.loads(capsys.readouterr().out) == {"result": "initialize"}
    config = load_yaml(AgentConfig, config_path)
    record = read_record(path)
    assert record.server_id == config.server_id != "upgrade-fixture"
    assert record.next_event_id == 1 and record.lineage_origin == "new_pairing"
    assert read_record(state_paths(path)[1]) == record
    snapshot = {p: p.read_bytes() for p in (config_path, *state_paths(path)[:2])}
    assert desktop("initialize") == 2
    assert all(p.read_bytes() == raw for p, raw in snapshot.items())


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["migrate"],
        ["initialize"],
        ["recover", "--confirm-surviving-record-intact"],
        ["migrate", "--confirm-intact-legacy-counter", "--config", "elsewhere.yaml"],
        ["initialize", "--confirm-new-pairing", "--confirm-new-pairing"],
        ["initialize", "--confirm-intact-legacy-counter"],
    ],
)
def test_desktop_route_no_confirmation_or_extra_arguments_never_write(
    config_path, args
):
    before = {p: p.read_bytes() for p in config_path.parent.iterdir()}
    assert backend_main.main(["agent-desktop-state", *args]) == 2
    assert {p: p.read_bytes() for p in config_path.parent.iterdir()} == before


@pytest.mark.parametrize(
    "fault", ["corrupt", "anchor", "identity", "fault-only", "dangling"]
)
@pytest.mark.parametrize("action", ["initialize", "migrate"])
def test_desktop_damaged_or_existing_evidence_is_never_reset(
    config_path, fault, action
):
    path = config_path.with_name("agent.state.json")
    primary, anchor, _ = state_paths(path)
    if fault == "corrupt":
        primary.write_text('{"next_event_id": true}')
    elif fault == "anchor":
        primary.write_text('{"next_event_id": 32560}')
        anchor.write_text('{"next_event_id": 32560}')
    elif fault == "identity":
        initialize_state(path, "different-identity", 32560)
    elif fault == "fault-only":
        primary.with_name(primary.name + ".fault-preserved").write_text(
            "retained evidence"
        )
    else:
        try:
            anchor.symlink_to(path.parent / "missing-target")
        except OSError:
            pytest.skip("symlink creation unavailable")
    files = {p: p.read_bytes() for p in path.parent.iterdir() if p.is_file()}
    assert desktop(action) == 2
    assert all(p.read_bytes() == raw for p, raw in files.items())
    if fault == "dangling":
        assert anchor.is_symlink() and not primary.exists()
    if fault == "fault-only":
        assert not primary.exists() and not anchor.exists()


def test_desktop_held_runtime_lease_refuses_without_state_mutation(config_path):
    path = config_path.with_name("agent.state.json")
    path.write_text('{"next_event_id": 32560}')
    original = path.read_bytes(), config_path.read_bytes()
    with FileLease(state_paths(path)[2]):
        assert desktop("migrate") == 2
        assert desktop("initialize") == 2
    assert (path.read_bytes(), config_path.read_bytes()) == original
    assert not state_paths(path)[1].exists()


def test_desktop_absence_is_checked_after_lease_acquisition(config_path, monkeypatch):
    path = config_path.with_name("agent.state.json")
    acquire = FileLease.acquire

    def changed(lease):
        acquired = acquire(lease)
        path.write_text('{"next_event_id": 32560}')
        return acquired

    monkeypatch.setattr(FileLease, "acquire", changed)
    before = config_path.read_bytes()
    assert desktop("initialize") == 2
    assert path.read_text() == '{"next_event_id": 32560}'
    assert config_path.read_bytes() == before and not state_paths(path)[1].exists()


@pytest.mark.parametrize("change", ["counter", "config"])
def test_desktop_changed_inputs_after_backup_refuse_publication(
    config_path, monkeypatch, change
):
    path = config_path.with_name("agent.state.json")
    path.write_text('{"next_event_id": 32560}')
    backup = offline.backup_sources

    def changed(source):
        result = backup(source)
        if change == "counter":
            path.write_text('{"next_event_id": 32561}')
        else:
            save_yaml(AgentConfig(server_id="changed", machine="fixture"), config_path)
        return result

    monkeypatch.setattr(offline, "backup_sources", changed)
    assert desktop("migrate") == 2
    assert not state_paths(path)[1].exists()
    assert (
        path.read_text()
        == '{"next_event_id": ' + ("32561" if change == "counter" else "32560") + "}"
    )
    assert all(
        p.read_text() == '{"next_event_id": 32560}'
        for p in path.parent.glob(path.name + ".fault-*")
    )


@pytest.mark.parametrize(
    "reason,code",
    [
        ("migration_required", "migration_required"),
        ("initialization_required", "initialization_required"),
        ("lease_held_or_unavailable", "state_lease_unavailable"),
        ("corrupt_state", "state_recovery_required"),
        ("fake-secret-in-exception", "state_recovery_required"),
    ],
)
def test_service_state_failure_frames_are_strict_and_nonsecret(
    config_path, monkeypatch, capsys, reason, code
):
    if reason == "migration_required":
        config_path.with_name("agent.state.json").write_text('{"next_event_id": 32560}')

    def refuse(*args, **kwargs):
        raise StateError(reason)

    monkeypatch.setattr(service, "run_agent", refuse)
    assert service.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {
        "taskpaw_startup_error": 1,
        "role": "agent",
        "code": code,
    }
    assert (
        "fake-secret" not in output.out + output.err
        and "taskpaw_ready" not in output.out
    )


def test_partial_or_faulted_missing_state_requests_manual_recovery(config_path):
    path = config_path.with_name("agent.state.json")
    anchor = state_paths(path)[1]
    anchor.write_text(
        json.dumps(asdict(initialize_state(path.parent / "other.json", "fixture")))
    )
    assert (
        startup_code(StateError("initialization_required"), state_path=path)
        == "state_recovery_required"
    )
    anchor.unlink()
    path.with_name(path.name + ".fault-preserved").write_text("retained")
    assert (
        startup_code(StateError("initialization_required"), state_path=path)
        == "state_recovery_required"
    )


@pytest.mark.parametrize(
    "number,code",
    [
        (errno.EADDRNOTAVAIL, "bind_address_unavailable"),
        (errno.EADDRINUSE, "port_in_use"),
        (errno.EACCES, "startup_failed"),
    ],
)
def test_service_classifies_real_os_cause_not_misleading_port_wrapper(
    config_path, monkeypatch, capsys, number, code
):
    def refuse(*args, **kwargs):
        try:
            raise OSError(number, "fake-secret-address")
        except OSError as cause:
            raise PortInUseError("misleading fake-secret port text") from cause

    monkeypatch.setattr(service, "run_agent", refuse)
    assert service.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["code"] == code
    assert "fake-secret" not in output.out + output.err


@pytest.mark.parametrize(
    "body",
    [
        "api_token: [fake-secret",
        "llm_api_key: [fake-secret]\nserver_id: fixture\nmachine: fixture\n",
        "fake-secret-scalar",
    ],
)
def test_config_validation_does_not_log_yaml_or_validation_values(
    config_path, capsys, body
):
    config_path.write_text(body)
    assert service.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["code"] == "config_invalid"
    assert (
        "fake-secret" not in output.out + output.err and "Traceback" not in output.err
    )


def test_unwritable_scaffold_and_unexpected_failure_are_typed(
    config_path, monkeypatch, capsys
):
    from taskpaw_v3 import bootstrap

    config_path.unlink()

    def denied(*args, **kwargs):
        raise PermissionError("fake-secret-denial")

    monkeypatch.setattr(bootstrap, "scaffold", denied)
    assert service.main() == 1
    assert json.loads(capsys.readouterr().out)["code"] == "config_unwritable"
    monkeypatch.setattr(service, "default_config_path", denied)
    assert service.main() == 1
    output = capsys.readouterr()
    assert json.loads(output.out)["code"] == "startup_failed"
    assert "fake-secret" not in output.out + output.err


def test_unknown_startup_frame_codes_refuse_without_output(capsys):
    for code in STARTUP_CODES:
        emit_startup_error(code)
    output = capsys.readouterr().out
    assert len(output.splitlines()) == len(STARTUP_CODES)
    with pytest.raises(ValueError):
        emit_startup_error("fake-secret")
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "winerror,code", [(10049, "bind_address_unavailable"), (10048, "port_in_use")]
)
def test_windows_socket_error_codes_are_classified_without_message_parsing(
    winerror, code
):
    cause = OSError("fake-secret socket failure")
    cause.winerror = winerror
    error = PortInUseError("misleading fake-secret port error")
    error.__cause__ = cause
    assert startup_code(error) == code


def test_successful_service_keeps_existing_ready_protocol(
    config_path, monkeypatch, capsys
):
    monkeypatch.setattr(
        service, "run_agent", lambda *a, **k: print('{"taskpaw_ready":1}')
    )
    assert service.main() == 0
    assert capsys.readouterr().out == '{"taskpaw_ready":1}\n'
