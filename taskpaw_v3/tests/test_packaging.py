"""Bundled-backend entry dispatch (#40/#41)."""

from __future__ import annotations

from pathlib import Path

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
    assert backend_main.main([]) == 0          # no arg → agent
    assert called["role"] == "agent"


def test_dispatch_unknown_role():
    assert backend_main.main(["bogus"]) == 2   # clean exit code, no crash


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
    assert svc.default_config_path().exists()   # default agent.yaml created
    assert ran["ran"]


def test_hub_service_scaffolds_missing_config(tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    import taskpaw_v3.hub.server.service as svc

    ran = {}
    monkeypatch.setattr(svc, "run_hub", lambda *a, **k: ran.setdefault("ran", True))
    assert svc.run_from_config() == 0           # no --config → scaffold default
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

    spec = Path(__file__).resolve().parents[2] / "taskpaw_v3" / "packaging" / "taskpaw-backend.spec"
    spec_text = spec.read_text(encoding="utf-8")
    assert '"taskpaw_v3/examples"' in spec_text, \
        "taskpaw-backend.spec no longer bundles taskpaw_v3/examples/*.yaml (#53)"


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
    monkeypatch.setattr(svc, "run_agent", lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))
    assert svc.main() == 1
