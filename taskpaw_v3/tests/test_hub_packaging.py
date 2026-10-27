"""Hub packaging must exercise the Hub, without touching any installed service."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import macos_release as mac


def test_hub_smoke_dispatches_to_hub_fixture(monkeypatch):
    from scripts import macos_hub_smoke

    calls = []
    monkeypatch.setattr(
        macos_hub_smoke, "smoke", lambda *a, **kw: calls.append((a, kw))
    )
    isolation = SimpleNamespace(require=lambda: None)
    sidecar = Path("never-execute-fixture")
    plan = mac.Plan("aarch64-apple-darwin", "adhoc")
    mac.smoke(sidecar, plan, isolation, {}, "3.10.2", role="hub")
    assert calls == [((sidecar, plan, isolation, {}, "3.10.2"), {"installer": None})]


@pytest.mark.parametrize(
    "cfg",
    [
        {"identifier": "com.taskpaw.app.agent", "productName": "TaskPaw Hub"},
        {"identifier": "com.taskpaw.app.hub", "productName": "TaskPaw Agent"},
        {"identifier": "foreign.hub", "productName": "TaskPaw Hub"},
    ],
)
def test_mismatched_role_cannot_publish_agent_evidence_as_hub(cfg, tmp_path):
    with pytest.raises(mac.BuildError, match="bundle_role_invalid"):
        mac.finalize(mac.Plan("aarch64-apple-darwin", "adhoc"), None, {}, tmp_path, cfg)


def test_hub_runtime_refuses_unqualified_host_before_allocating(monkeypatch):
    from scripts import macos_hub_smoke

    def refuse():
        raise mac.BuildError("macos_smoke_isolation_required")

    def forbidden(*args, **kwargs):
        pytest.fail("unqualified fixture must not allocate or start anything")

    monkeypatch.setattr(macos_hub_smoke.tempfile, "mkdtemp", forbidden)
    with pytest.raises(mac.BuildError, match="isolation_required"):
        mac.smoke(
            Path("never-execute-fixture"),
            mac.Plan("aarch64-apple-darwin", "adhoc"),
            SimpleNamespace(require=refuse),
            {},
            "3.10.2",
            role="hub",
        )


def test_hub_manager_is_explicit_and_does_not_eval_arguments(tmp_path):
    mac.stage_hub_manager(tmp_path)
    command = tmp_path / "Manage Hub Service.command"
    text = command.read_text()
    if os.name != "nt":  # Windows stat does not represent POSIX execute bits.
        assert command.stat().st_mode & 0o100
    assert 'hub-service "$action"' in text
    assert "eval" not in text and "sudo" not in text
    assert 'app="/Applications/TaskPaw Hub.app"' in text
    assert 'backend="$app/Contents/MacOS/taskpaw-backend"' in text
    assert "install" in text and "uninstall" in text


@pytest.mark.parametrize(
    "name,mode",
    [
        ("Manage Hub Service.command", 0o755),
        ("Install Hub.command", 0o755),
        ("Hub 安装说明.txt", 0o644),
    ],
)
def test_hub_package_stages_exact_files_and_modes(tmp_path, name, mode):
    mac.stage_hub_manager(tmp_path)
    target = tmp_path / name
    assert target.read_bytes() == Path(mac.__file__).with_name(name).read_bytes()
    if os.name != "nt":
        assert target.stat().st_mode & 0o777 == mode
    mac.verify_hub_package(tmp_path)


@pytest.mark.parametrize(
    "name", ["Manage Hub Service.command", "Install Hub.command", "Hub 安装说明.txt"]
)
@pytest.mark.parametrize("damage", ["bytes", "missing", "mode"])
def test_hub_package_rejects_changed_shipped_files(tmp_path, name, damage):
    if damage == "mode" and os.name == "nt":
        pytest.skip("Windows stat does not represent POSIX file modes")
    mac.stage_hub_manager(tmp_path)
    target = tmp_path / name
    if damage == "bytes":
        target.write_bytes(b"changed shipped contents")
    elif damage == "missing":
        target.unlink()
    else:
        target.chmod(0o600)
    with pytest.raises(mac.BuildError, match="macos_hub_package_invalid"):
        mac.verify_hub_package(tmp_path)


def test_dmg_smoke_consumes_exact_mounted_installer(monkeypatch, tmp_path):
    calls = []
    evidence = {"installer_entry_verified": True}

    def mount_fixture(stage, argv, **kwargs):
        assert stage == "dmg_mount"
        mount = Path(argv[argv.index("-mountpoint") + 1])
        (mount / "TaskPaw Hub.app").mkdir()
        mac.stage_hub_manager(mount)

    def verify_app(app, *args):
        return app / "Contents/MacOS/taskpaw-backend", 1

    def smoke(sidecar, plan, isolation, env, version, **kwargs):
        installer = kwargs["installer"]
        assert installer.name == "Install Hub.command"
        assert installer.parent.name == "mount"
        assert (
            installer.read_bytes()
            == Path(mac.__file__).with_name(installer.name).read_bytes()
        )
        calls.append(kwargs)
        return evidence

    monkeypatch.setattr(mac, "tool", mount_fixture)
    monkeypatch.setattr(mac, "verify_app", verify_app)
    monkeypatch.setattr(mac, "smoke", smoke)
    result = mac.verify_dmg(
        tmp_path / "fixture.dmg", None, None, {}, tmp_path, "3.11.1", "TaskPaw Hub"
    )
    assert calls[0]["role"] == "hub"
    assert result is evidence


def test_hub_smoke_dispatches_shipped_installer(monkeypatch, tmp_path):
    from scripts import macos_hub_smoke

    calls = []
    monkeypatch.setattr(
        macos_hub_smoke, "smoke", lambda *a, **kw: calls.append((a, kw))
    )
    installer = tmp_path / "Install Hub.command"
    mac.smoke(None, None, None, {}, "3.11.1", role="hub", installer=installer)
    assert calls == [((None, None, None, {}, "3.11.1"), {"installer": installer})]


def test_hub_finalizer_records_dmg_installer_evidence(monkeypatch, tmp_path):
    import json

    from taskpaw_v3.tests.test_build_macos import finalizer_fixture

    plan, cfg, bundle, _ = finalizer_fixture(tmp_path, monkeypatch)
    (bundle / "macos/TaskPaw Agent.app").rename(bundle / "macos/TaskPaw Hub.app")
    cfg.update(identifier="com.taskpaw.app.hub", productName="TaskPaw Hub")
    evidence = {"installer_entry_verified": True, "installer_entry_sha256": "d" * 64}
    monkeypatch.setattr(mac, "smoke", lambda *a, **kw: {"stage": "before-dmg"})

    def verified_dmg(*args):
        mac.verify_hub_package(args[0].parent / "dmg-staging")
        return evidence

    monkeypatch.setattr(mac, "verify_dmg", verified_dmg)
    mac.finalize(plan, None, {}, tmp_path, cfg)
    report = json.loads((bundle / "macos-verification.json").read_text())
    assert report["role"] == "hub"
    assert report["hub_runtime"] == evidence


def test_hub_readiness_does_not_accept_agent_frame():
    import json

    frame = {
        "taskpaw_ready": True,
        "role": "agent",
        "base_url": "http://127.0.0.1:12345",
        "boot_id": "1" * 32,
        "control_token": "x" * 32,
    }
    assert not mac.ready_line(json.dumps(frame).encode(), frame["base_url"], role="hub")
    frame["role"] = "hub"
    assert mac.ready_line(json.dumps(frame).encode(), frame["base_url"], role="hub")


def test_native_fixture_peers_use_real_protocol_and_join():
    import http.client
    import json

    from scripts.macos_hub_smoke import Peers, request
    from taskpaw_v3.hub.server.upstream_worker import decode_events, decode_status

    with Peers() as peers:
        port = peers.servers[0].server_port
        assert request(port, "wrong", "/status") is None
        status = request(port, peers.token, "/status")
        assert decode_status(json.dumps(status))[0]["machine"] == "controlled-agent"
        events = request(port, peers.token, "/events?ack=-1")
        assert decode_events(json.dumps(events))["events"] == []
        assert peers.samples == 1
        connection = http.client.HTTPConnection(
            "127.0.0.1", peers.servers[1].server_port, timeout=2
        )
        try:
            connection.request(
                "POST", "/hooks/wake", b"{}", {"Authorization": "Bearer " + peers.token}
            )
            assert connection.getresponse().status == 200
            assert peers.deliveries == 1
        finally:
            connection.close()
    assert all(not thread.is_alive() for thread in peers.threads)
    assert all(server.fileno() == -1 for server in peers.servers)


@pytest.mark.parametrize("publish_later", [True, False])
def test_hub_smoke_waits_for_status_file_after_api_ready(
    tmp_path, monkeypatch, publish_later
):
    import json

    from scripts import macos_hub_smoke as smoke

    class PassedFileGate(Exception):
        pass

    source = tmp_path / "source/TaskPaw Hub.app/Contents/MacOS/taskpaw-backend"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"never execute: fake manager only")
    root = tmp_path / "fixture"
    root.mkdir()
    home = root / "HOME"
    config = home / "Library/Application Support/TaskPaw/hub.yaml"
    status_file = home / "data/status.md"
    clock = {"now": 0.0, "api_ready_without_file": False, "published": False}
    actions = []
    staged_entry = tmp_path / "actual dmg mount"
    staged_entry.mkdir()
    mac.stage_hub_manager(staged_entry)
    installer = staged_entry / "Install Hub.command"

    def advance(seconds):
        clock["now"] += seconds
        if publish_later and clock["now"] >= 0.2:
            status_file.write_text("Published after the first completed poll")
            clock["published"] = True

    def fake_cli(stage, argv, **kwargs):
        if stage == "hub_install_entry":
            assert argv == [str(installer)]
            assert (
                Path(argv[0]).read_bytes()
                == Path(mac.__file__).with_name("Install Hub.command").read_bytes()
            )
            fake_open = root / "fake-bin/open"
            assert kwargs["env"]["PATH"].startswith(str(fake_open.parent) + ":")
            assert fake_open.is_file() and "opened-apps" in fake_open.read_text()
            with (root / "opened-apps").open("a") as out:
                out.write(str(home / "Applications/TaskPaw Hub.app") + "\n")
            action = "install"
        else:
            assert argv[1] == "hub-service"
            action = argv[2]
        actions.append(action)
        if action == "install":
            if actions.count("install") == 2:
                raise PassedFileGate  # Stop before unrelated lifecycle checks.
            record = config.parent / "HubService/runtime.json"
            record.parent.mkdir()
            record.write_text(json.dumps({"state": "running", "created": 1}))
        return json.dumps(
            {"ok": True, "state": "running", "version": "3.10.2"}
        ).encode(), b""

    def reply(port, token, path):
        if path == "/ping":
            return {"version": "3.10.2"}
        assert path == "/status"
        clock["api_ready_without_file"] = not status_file.exists()
        return {
            "machine": "hub-native-smoke",
            "servers": [{"online": True, "event_channel": {"state": "ready"}}],
            "notifications": {"last_success_at": "fixture-delivered"},
        }

    monkeypatch.setattr(smoke.tempfile, "mkdtemp", lambda **kw: str(root))
    monkeypatch.setattr(
        smoke, "time", SimpleNamespace(monotonic=lambda: clock["now"], sleep=advance)
    )
    monkeypatch.setattr(smoke.mac, "tool", fake_cli)
    monkeypatch.setattr(smoke, "request", reply)
    monkeypatch.setattr(
        smoke,
        "read_control_descriptor",
        lambda path: SimpleNamespace(
            role="hub",
            base_url=f"http://127.0.0.1:{json.loads(config.read_text())['control_port']}",
        ),
    )
    expected = PassedFileGate if publish_later else mac.BuildError
    with pytest.raises(
        expected, match=None if publish_later else "macos_hub_smoke_status_file"
    ):
        smoke.smoke(
            source,
            None,
            SimpleNamespace(require=lambda: None),
            {},
            "3.10.2",
            installer=installer,
        )
    assert clock["api_ready_without_file"]
    assert actions[-2:] == ["stop", "uninstall"]
    if publish_later:
        assert clock["published"] and 0.2 <= clock["now"] < 35
    else:
        assert 35 <= clock["now"] < 36 and not clock["published"]
