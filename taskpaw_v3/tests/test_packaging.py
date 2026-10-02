"""Bundled-backend entry dispatch (#40/#41)."""

from __future__ import annotations

# Offline commands exercise actual filesystem and SQLite; no runtime services.
import json
import os
import subprocess
from pathlib import Path

import pytest

from taskpaw_v3.core.config import AgentConfig, save_yaml
from taskpaw_v3.core.state import FileLease, db_lease_path, state_paths
from taskpaw_v3.hub.server.store import HubStore
from taskpaw_v3.packaging import backend_main


def test_dispatch_agent(monkeypatch):
    called = {}
    import taskpaw_v3.agent.server.service as agent_service

    def fake():
        called["role"] = "agent"
        return 0

    monkeypatch.setattr(agent_service, "main", fake)
    assert backend_main.main(["agent"]) == 0
    assert called["role"] == "agent"


def test_dispatch_hub(monkeypatch):
    called = {}
    import taskpaw_v3.hub.server.service as hub_service

    def fake():
        called["role"] = "hub"
        return 0

    monkeypatch.setattr(hub_service, "main", fake)
    assert backend_main.main(["hub"]) == 0
    assert called["role"] == "hub"


def test_dispatch_defaults_to_agent(monkeypatch):
    called = {}
    import taskpaw_v3.agent.server.service as agent_service

    def fake():
        called["role"] = "agent"
        return 0

    monkeypatch.setattr(agent_service, "main", fake)
    assert backend_main.main([]) == 0  # no arg → agent
    assert called["role"] == "agent"


def test_dispatch_unknown_role():
    assert backend_main.main(["bogus"]) == 2  # clean exit code, no crash


def test_dispatch_llm_worker(monkeypatch):
    # #178: the Tauri-bundled backend doubles as the terminable llm-worker
    # sidecar (`taskpaw-backend llm-worker`, see core.llm_worker.worker_argv).
    called = {}
    import taskpaw_v3.core.llm_worker as llm_worker

    def fake(argv=None):
        called["role"] = "llm-worker"
        return 0

    monkeypatch.setattr(llm_worker, "main", fake)
    assert backend_main.main(["llm-worker"]) == 0
    assert called["role"] == "llm-worker"


def test_unknown_role_message_names_every_role(capsys):
    assert backend_main.main(["bogus"]) == 2
    err = capsys.readouterr().err
    for role in ("'agent'", "'hub'", "'llm-worker'"):
        assert role in err


def test_agent_service_scaffolds_missing_config(tmp_path, monkeypatch):
    # Fresh install / no config → the service self-initializes a default and runs,
    # instead of exiting and leaving the packaged UI with no backend (#40).
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    import taskpaw_v3.agent.server.service as svc

    ran = {}
    monkeypatch.setattr(svc, "run_agent", lambda *a, **k: ran.setdefault("ran", True))
    assert svc.main() == 0
    assert svc.default_config_path().exists()  # default agent.yaml created
    assert ran["ran"]


def test_hub_service_scaffolds_missing_config(tmp_path, monkeypatch):
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    import taskpaw_v3.hub.server.service as svc

    ran = {}
    monkeypatch.setattr(svc, "run_hub", lambda *a, **k: ran.setdefault("ran", True))
    assert svc.run_from_config() == 0  # no --config → scaffold default
    assert svc.default_config_path().exists()
    assert ran["ran"]


def test_examples_bundled_for_scaffold():
    # bootstrap.scaffold() reads these templates at runtime; the PyInstaller spec
    # MUST bundle them or the packaged backend crashes with FileNotFoundError on a
    # no-config first run (#53). This guards the source contract two ways:
    #   1. both role templates exist where scaffold reads them (catch rename/delete);
    #   2. the spec still declares the examples->taskpaw_v3/examples bundling.
    # (A full in-bundle assertion needs a real PyInstaller run — deferred to a CI
    # build smoke; this is the proportionate unit-level tripwire.)
    from taskpaw_v3 import bootstrap

    for name in ("agent.example.yaml", "hub.example.yaml"):
        assert (bootstrap.EXAMPLES / name).exists(), f"missing scaffold template {name}"

    spec = (
        Path(__file__).resolve().parents[2]
        / "taskpaw_v3"
        / "packaging"
        / "taskpaw-backend.spec"
    )
    spec_text = spec.read_text(encoding="utf-8")
    # Match the actual data-binding statement, not the substring — the explanatory
    # comment above it also mentions "taskpaw_v3/examples", so a plain `in` check
    # would still pass if the datas.append(...) line were deleted (Kimi P2).
    import re

    assert re.search(
        r'datas\.append\(\([^)]*,\s*"taskpaw_v3/examples"\)\)', spec_text
    ), "taskpaw-backend.spec no longer bundles taskpaw_v3/examples/*.yaml (#53)"


def test_agent_service_scaffold_oserror_clean_exit(tmp_path, monkeypatch):
    # If the default config dir isn't writable, fail cleanly (exit 1), don't crash.
    import sys

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    import taskpaw_v3.agent.server.service as svc
    from taskpaw_v3 import bootstrap

    def boom(role, force=False):
        raise OSError("permission denied")

    monkeypatch.setattr(bootstrap, "scaffold", boom)
    monkeypatch.setattr(
        svc,
        "run_agent",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")),
    )
    assert svc.main() == 1


@pytest.mark.parametrize(
    "verb,args",
    [
        ("inspect", []),
        ("initialize", ["--confirm-new-pairing"]),
        ("migrate", ["--confirm-intact-legacy-counter"]),
        ("recover", ["--confirm-surviving-record-intact"]),
        ("export", ["--output", "report.json"]),
    ],
)
def test_dispatch_agent_state_exact_forwarding(monkeypatch, verb, args):
    import taskpaw_v3.agent.state as state

    seen = []
    monkeypatch.setattr(state, "main", lambda argv: (seen.append(argv), 17)[1])
    argv = ["--config", "explicit.yaml", verb, *args]
    assert backend_main.main(["agent-state", *argv]) == 17
    assert seen == [argv]


@pytest.mark.parametrize(
    "verb,args",
    [("event-cursor", []), ("adopt-event-cursor", ["--state-report", "report.json"])],
)
def test_dispatch_hub_cursor_exact_allowlist(monkeypatch, verb, args):
    import taskpaw_v3.hub.__main__ as hub

    seen = []

    def run(argv, **kwargs):
        seen.append((argv, kwargs))
        return 19

    monkeypatch.setattr(hub, "main", run)
    argv = ["--db", "explicit.db", verb, "--id", "1", *args]
    assert backend_main.main(["hub-cursor", *argv]) == 19
    assert seen == [
        (
            argv,
            {
                "allowed_commands": frozenset({"event-cursor", "adopt-event-cursor"}),
                "require_explicit_db": True,
            },
        )
    ]


@pytest.mark.parametrize(
    "argv",
    [
        ["agent-state"],
        ["agent-state", "inspect"],
        ["agent-state", "-m", "os"],
        ["agent-state", "--config", "absent.yaml", "initialize"],
        ["agent-state", "--config", "absent.yaml", "run"],
        ["agent-state", "--config", "absent.yaml", "inspect", "--unknown"],
        ["hub-cursor"],
        ["hub-cursor", "run"],
        ["hub-cursor", "list-servers"],
        [
            "hub-cursor",
            "--db",
            "absent.db",
            "add-server",
            "--name",
            "x",
            "--ip",
            "127.0.0.1",
        ],
        ["hub-cursor", "event-cursor", "--id", "1"],
        ["hub-cursor", "--db", "absent.db", "adopt-event-cursor", "--id", "1"],
        ["hub-cursor", "--db", "absent.db", "event-cursor", "--id", "1", "--unknown"],
        ["-m", "os"],
    ],
)
def test_offline_dispatch_negative_never_opens_store_or_service(
    tmp_path, monkeypatch, argv
):
    import taskpaw_v3.agent.server.service as agent_service
    import taskpaw_v3.hub.__main__ as hub
    import taskpaw_v3.hub.server.service as hub_service

    def forbidden(*args, **kwargs):
        raise AssertionError("offline refusal must not start/open runtime")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(hub, "HubStore", forbidden)
    monkeypatch.setattr(agent_service, "main", forbidden)
    monkeypatch.setattr(hub_service, "main", forbidden)
    assert backend_main.main(argv) == 2
    assert list(tmp_path.iterdir()) == []


def test_runtime_trailing_flags_keep_original_role_semantics(monkeypatch):
    import taskpaw_v3.agent.server.service as service

    seen = []
    monkeypatch.setattr(service, "main", lambda: (seen.append(True), 0)[1])
    assert backend_main.main(["agent", "--config", "ignored.yaml", "initialize"]) == 0
    assert seen == [True]


def _exercise_cursor_commands(run, tmp_path):
    malformed = tmp_path / "malformed.yaml"
    malformed.write_text("api_token: [fake-r05-never-echo")
    assert run(["agent-state", "--config", str(malformed), "inspect"]) == 2
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    config = fresh / "agent.yaml"
    save_yaml(
        AgentConfig(
            server_id="fresh-fixture",
            machine="fixture",
            api_token="fake-r05-never-echo",
        ),
        config,
    )
    prefix = ["agent-state", "--config", str(config)]
    assert run([*prefix, "initialize"]) == 2
    assert run([*prefix, "initialize", "--confirm-new-pairing"]) == 0
    assert run([*prefix, "inspect"]) == 0
    path = fresh / "agent.state.json"
    record = json.loads(path.read_text())
    assert record["server_id"] != "fresh-fixture" and record["next_event_id"] == 1
    before = [p.read_bytes() for p in [config, *state_paths(path)[:2]]]
    assert run([*prefix, "initialize", "--confirm-new-pairing"]) == 2
    assert before == [p.read_bytes() for p in [config, *state_paths(path)[:2]]]
    assert run([*prefix, "export", "--output", str(config)]) == 2
    report = fresh / "report.json"
    assert run([*prefix, "export", "--output", str(report)]) == 0
    assert json.loads(report.read_text())["verified"] is True
    db = tmp_path / "hub.db"
    with_store = HubStore(db)
    sid = with_store.add_server("fresh", "127.0.0.1", 15680, enabled=False)
    with_store.close()
    hp = ["hub-cursor", "--db", str(db)]
    assert run([*hp, "event-cursor", "--id", str(sid)]) == 0
    assert (
        run(
            [*hp, "adopt-event-cursor", "--id", str(sid), "--state-report", str(report)]
        )
        == 0
    )
    store = HubStore(db)
    assert store.read_acks() == {sid: -1}
    assert store.get_event_cursor(sid)["identity"]["server_id"] == record["server_id"]
    store.close()

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    config = legacy / "agent.yaml"
    save_yaml(AgentConfig(server_id="legacy-fixture", machine="fixture"), config)
    path = legacy / "agent.state.json"
    path.write_text('{"next_event_id":901}')
    prefix = ["agent-state", "--config", str(config)]
    assert run([*prefix, "migrate"]) == 2
    assert run([*prefix, "migrate", "--confirm-intact-legacy-counter"]) == 0
    path.write_text('{"fixture_fault":"fake-r05-never-echo"}')
    assert run([*prefix, "recover"]) == 2
    assert run([*prefix, "recover", "--confirm-surviving-record-intact"]) == 0
    assert json.loads(path.read_text())["next_event_id"] == 901
    report = legacy / "report.json"
    assert run([*prefix, "export", "--output", str(report)]) == 0
    store = HubStore(db)
    sid = store.add_server("existing", "127.0.0.1", 15680, enabled=False)
    store._conn.execute(
        "UPDATE event_cursors SET state='unverified' WHERE server_id=?", (sid,)
    )
    store._conn.commit()
    store.store_event(sid, {"id": 900, "message": "old history body"})
    store.enqueue_delivery(
        server_name="existing",
        kind="event",
        payload_json='{"text":"old outbox body"}',
        dedupe_key=f"{sid}:900",
    )
    raw = '{"fake_corrupt_ack":"fake-r05-never-echo"}'
    store.set_config("last_event_ids", raw)
    history, outbox = (
        store.recent_events(sid),
        store._conn.execute("SELECT * FROM delivery_outbox").fetchall(),
    )
    store.close()
    adoption = [
        *hp,
        "adopt-event-cursor",
        "--id",
        str(sid),
        "--state-report",
        str(report),
    ]
    with FileLease(db_lease_path(db)):
        assert run(adoption) == 2
    assert run(adoption) == 0
    store = HubStore(db)
    assert store.read_acks() == {sid: 900}
    assert store.recent_events(sid) == history
    assert store._conn.execute("SELECT * FROM delivery_outbox").fetchall() == outbox
    assert (
        store._conn.execute(
            "SELECT value FROM config WHERE key LIKE 'last_event_ids.fault-%'"
        ).fetchone()[0]
        == raw
    )
    binding = store.get_event_cursor(sid)
    store.close()
    original = report.read_bytes()
    changed = json.loads(original)
    changed["record"]["next_event_id"] = 900
    report.write_text(json.dumps(changed))
    assert run(adoption) == 2

    store = HubStore(db)
    unknown = store.add_server("old-unknown", "127.0.0.1", 15680, enabled=False)
    store._conn.execute(
        "UPDATE event_cursors SET state='unverified' WHERE server_id=?", (unknown,)
    )
    store._conn.commit()
    store.close()
    assert (
        run(
            [
                *hp,
                "adopt-event-cursor",
                "--id",
                str(unknown),
                "--state-report",
                str(fresh / "report.json"),
            ]
        )
        == 2
    )
    changed["record"]["next_event_id"] = 901
    changed["record"]["stream_id"] = "f" * 32
    report.write_text(json.dumps(changed))
    assert run(adoption) == 2
    report.write_bytes(original)
    store = HubStore(db)
    assert store.get_event_cursor(sid) == binding and store.read_acks() == {sid: 900}
    store.set_server_enabled(sid, True)
    store.close()
    assert run(adoption) == 2


def test_real_packaged_dispatch_offline_recovery(tmp_path):
    _exercise_cursor_commands(backend_main.main, tmp_path)


@pytest.mark.skipif(
    not os.environ.get("TASKPAW_TEST_FROZEN_BACKEND"),
    reason="native frozen binary not supplied; not packaged-runtime evidence",
)
def test_frozen_event_cursor_offline_recovery(tmp_path):
    binary = Path(os.environ["TASKPAW_TEST_FROZEN_BACKEND"]).resolve()
    assert binary.is_file()
    empty_path = tmp_path / "empty-path"
    empty_path.mkdir()
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV"}
    }
    env["PATH"] = str(empty_path)
    env["HOME"] = str(tmp_path)
    env["APPDATA"] = str(tmp_path)

    def run(argv):
        result = subprocess.run(
            [str(binary), *argv],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout + result.stderr
        assert "taskpaw_ready" not in output and "fake-r05-never-echo" not in output
        assert "Traceback" not in output
        return result.returncode

    _exercise_cursor_commands(run, tmp_path)


def test_service_auto_scaffold_requires_explicit_event_admission(
    tmp_path, monkeypatch, capsys
):
    import sys

    from taskpaw_v3.agent.server import service
    from taskpaw_v3.core.state import StateError

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    # Avoid the baseline reclaim entirely; this proves the service diagnostic boundary.
    def refuse(config, **kwargs):
        assert kwargs["state_path"].parent.is_relative_to(tmp_path)
        raise StateError("initialization_required")

    monkeypatch.setattr(service, "run_agent", refuse)
    assert service.main() == 1
    output = capsys.readouterr()
    assert "agent-state" in output.err and "python -m" not in output.err
    assert "initialization_required" in output.err and "taskpaw_ready" not in output.out
    config = service.default_config_path()
    assert config.is_file() and not config.with_name("agent.state.json").exists()
    assert (
        backend_main.main(
            [
                "agent-state",
                "--config",
                str(config),
                "initialize",
                "--confirm-new-pairing",
            ]
        )
        == 0
    )


@pytest.mark.parametrize("body", ["api_token: [fake-r05-never-echo", "scalar-config"])
def test_offline_malformed_config_clean_no_defaults(tmp_path, capsys, body):
    config = tmp_path / "agent.yaml"
    config.write_text(body)
    before = config.read_bytes()
    assert backend_main.main(["agent-state", "--config", str(config), "inspect"]) == 2
    output = capsys.readouterr()
    assert "fake-r05-never-echo" not in output.err + output.out
    assert list(tmp_path.iterdir()) == [config] and config.read_bytes() == before
