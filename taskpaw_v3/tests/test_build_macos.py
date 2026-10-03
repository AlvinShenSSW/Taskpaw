"""Mac build contracts; never start a packaged backend on a shared host."""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import os
import plistlib
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import macos_release as mac

ROOT = Path(__file__).resolve().parents[2]


def load_build():
    spec = importlib.util.spec_from_file_location(
        "taskpaw_build", ROOT / "scripts/build.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_empty_apple_inputs_are_not_inherited_by_tauri(monkeypatch):
    build = load_build()
    keys = (
        "APPLE_CERTIFICATE",
        "APPLE_CERTIFICATE_PASSWORD",
        "APPLE_SIGNING_IDENTITY",
        "APPLE_ID",
        "APPLE_PASSWORD",
        "APPLE_TEAM_ID",
    )
    for key in keys:
        monkeypatch.setenv(key, "")
    monkeypatch.setattr(build.sys, "platform", "darwin")
    calls = []

    def fake_run(cmd, **kwargs):
        if any("@tauri-apps/cli" in part for part in cmd):
            import os

            calls.append((cmd, kwargs.get("env", dict(os.environ))))

    monkeypatch.setattr(build, "run", fake_run)
    monkeypatch.setattr(mac, "finalize", lambda *args: None)
    build.build_tauri(
        mac.normalize({}, "aarch64-apple-darwin"), SimpleNamespace(require=lambda: None)
    )
    assert calls
    assert not set(keys).intersection(calls[0][1])


TARGET = "aarch64-apple-darwin"
FAKE_ID = "A" * 40
FORMAL = {
    "APPLE_SIGNING_IDENTITY": FAKE_ID,
    "APPLE_TEAM_ID": "FAKE123456",
    "TASKPAW_MACOS_SIGNING_KEYCHAIN": str(ROOT / "fake-private.keychain-db"),
    "TASKPAW_MACOS_NOTARY_PROFILE": "fixture-profile",
}
HOSTED = {
    "TASKPAW_MACOS_SMOKE_ISOLATION": "github-hosted-fresh",
    "GITHUB_ACTIONS": "true",
    "RUNNER_ENVIRONMENT": "github-hosted",
    "RUNNER_OS": "macOS",
    "RUNNER_ARCH": "ARM64",
    "GITHUB_RUN_ID": "123",
    "GITHUB_JOB": "bundle",
}


@pytest.mark.parametrize("value", ["", " ", "\t\n"])
def test_blank_signing_group_is_adhoc(value):
    inputs = {
        k: value for k in (*mac.FORMAL_KEYS, "APPLE_CERTIFICATE", "APPLE_PASSWORD")
    }
    assert mac.normalize(inputs, TARGET).mode == "adhoc"
    assert not any(
        k.startswith(("APPLE_", "TASKPAW_MACOS_")) for k in mac.child_env(inputs)
    )


def test_unset_and_explicit_adhoc():
    assert mac.normalize({}, TARGET).mode == "adhoc"
    assert mac.normalize({"APPLE_SIGNING_IDENTITY": "-"}, TARGET).mode == "adhoc"


@pytest.mark.parametrize("key", mac.FORMAL_KEYS)
def test_every_partial_formal_group_errors_without_values(key):
    inputs = dict(FORMAL)
    del inputs[key]
    with pytest.raises(mac.BuildError, match="configuration_incomplete") as exc:
        mac.normalize(inputs, TARGET)
    assert not any(v in str(exc.value) for v in FORMAL.values())


@pytest.mark.parametrize(
    "key",
    [
        "APPLE_CERTIFICATE",
        "APPLE_CERTIFICATE_PASSWORD",
        "APPLE_ID",
        "APPLE_PASSWORD",
        "APPLE_API_KEY",
        "APPLE_API_ISSUER",
        "APPLE_API_KEY_PATH",
        "APPLE_UNKNOWN",
    ],
)
def test_legacy_inputs_rejected_even_with_new_complete_group(key):
    with pytest.raises(mac.BuildError, match="legacy_credentials_unsupported") as exc:
        mac.normalize({**FORMAL, key: "FAKE-SECRET-DO-NOT-PRINT"}, TARGET)
    assert "FAKE-SECRET" not in str(exc.value)


def test_full_old_six_group_has_migration_error():
    old = {
        k: "fake"
        for k in (
            "APPLE_CERTIFICATE",
            "APPLE_CERTIFICATE_PASSWORD",
            "APPLE_SIGNING_IDENTITY",
            "APPLE_ID",
            "APPLE_PASSWORD",
            "APPLE_TEAM_ID",
        )
    }
    with pytest.raises(mac.BuildError, match="legacy_credentials_unsupported"):
        mac.normalize(old, TARGET)


@pytest.mark.parametrize(
    "key,value",
    [
        ("APPLE_SIGNING_IDENTITY", "Developer ID Application: Fake"),
        ("APPLE_TEAM_ID", "bad"),
        ("TASKPAW_MACOS_NOTARY_PROFILE", "bad profile"),
        ("TASKPAW_MACOS_SIGNING_KEYCHAIN", "relative"),
        ("TASKPAW_MACOS_SIGNING_KEYCHAIN", "/fake/../keychain"),
        ("TASKPAW_MACOS_SIGNING_KEYCHAIN", "/fake/keychain\ninvalid"),
    ],
)
def test_invalid_formal_values_are_not_echoed(key, value):
    with pytest.raises(mac.BuildError, match="configuration_invalid") as exc:
        mac.normalize({**FORMAL, key: value}, TARGET)
    assert value not in str(exc.value)


def test_none_is_not_absence_and_dash_conflicts():
    with pytest.raises(mac.BuildError):
        mac.normalize({"APPLE_SIGNING_IDENTITY": "None"}, TARGET)
    with pytest.raises(mac.BuildError, match="conflict"):
        mac.normalize({**FORMAL, "APPLE_SIGNING_IDENTITY": "-"}, TARGET)
    assert mac.normalize(FORMAL, TARGET).identity == FAKE_ID


@pytest.mark.parametrize("value", ["app", "dmg", "app,dmg", "dmg,app", ""])
def test_allowed_bundle_values(value):
    plan = mac.normalize({"TASKPAW_BUNDLE_TARGETS": value}, TARGET)
    assert plan.bundles <= {"app", "dmg"}


@pytest.mark.parametrize("value", ["deb", "all", "app,", "universal"])
def test_bundle_and_target_rejection(value):
    with pytest.raises(mac.BuildError):
        mac.normalize({"TASKPAW_BUNDLE_TARGETS": value}, TARGET)
    with pytest.raises(mac.BuildError):
        mac.normalize({}, "universal-apple-darwin")


def test_internal_env_is_darwin_build_metadata_only():
    build = load_build()
    plan = mac.normalize(FORMAL, TARGET)
    env = build.pyinstaller_env(plan)
    assert env["TASKPAW_PYI_TARGET_ARCH"] == "arm64"
    assert env["TASKPAW_PYI_CODESIGN_IDENTITY"] == FAKE_ID
    assert not any(k.startswith("APPLE_") for k in env)
    assert (
        plan.bundle_root(ROOT)
        == ROOT / "taskpaw_v3/src-tauri/target/aarch64-apple-darwin/release/bundle"
    )
    runtime = mac.child_env(
        {
            **env,
            "DYLD_FAKE": "fake",
            "PYTHONPATH": "fake",
            "TASKPAW_LLM_API_KEY": "fake",
        },
        runtime=True,
    )
    assert not any(
        k.startswith(("TASKPAW_PYI_", "DYLD_", "PYTHON", "TASKPAW_LLM_"))
        for k in runtime
    )


def test_mac_tauri_target_no_sign_and_own_finalizer(monkeypatch):
    build = load_build()
    calls, finalized = [], []
    monkeypatch.setattr(build.sys, "platform", "darwin")
    monkeypatch.setattr(build, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    monkeypatch.setattr(mac, "finalize", lambda *args: finalized.append(args))
    plan = mac.normalize({}, TARGET)
    build.build_tauri(plan, SimpleNamespace(require=lambda: None))
    cmd, kwargs = calls[-1]
    assert cmd[cmd.index("--target") + 1] == TARGET
    assert cmd[cmd.index("--bundles") + 1] == "app"
    assert "--no-sign" in cmd
    assert "signingIdentity" not in cmd[cmd.index("--config") + 1]
    assert finalized and kwargs["env"] == mac.child_env(os.environ)


def test_nonmac_build_preserves_default_tauri_output_and_commands(monkeypatch):
    build = load_build()
    calls = []
    monkeypatch.setattr(build.sys, "platform", "win32")
    monkeypatch.setattr(build, "run", lambda cmd, **kw: calls.append((cmd, kw)))
    monkeypatch.delenv("TASKPAW_BUNDLE_TARGETS", raising=False)
    build.build_tauri()
    cmd, kwargs = calls[-1]
    assert "--target" not in cmd and "--no-sign" not in cmd and "--bundles" not in cmd
    assert not kwargs.get("mac_stage")


def test_windows_run_removes_mac_metadata_but_retains_windows(monkeypatch):
    build = load_build()
    calls = []
    monkeypatch.setenv("APPLE_PASSWORD", "fake")
    monkeypatch.setenv("WINDOWS_CERTIFICATE", "fake-windows-cert")
    monkeypatch.setattr(
        build.subprocess, "run", lambda *args, **kw: calls.append((args, kw))
    )
    build.run(["fake-command"])
    assert "APPLE_PASSWORD" not in calls[0][1]["env"]
    assert calls[0][1]["env"]["WINDOWS_CERTIFICATE"] == "fake-windows-cert"


def hosted_isolation(tmp_path, monkeypatch, inputs=None):
    monkeypatch.setattr(mac.tempfile, "gettempdir", lambda: str(tmp_path))
    return mac.Isolation(
        mac.normalize({}, TARGET), HOSTED if inputs is None else inputs, ROOT
    )


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"TASKPAW_MACOS_SMOKE_ISOLATION": "shared"},
        {"TASKPAW_MACOS_SMOKE_ISOLATION": "unknown"},
        {"TASKPAW_MACOS_SMOKE_ISOLATION": "github-hosted-fresh"},
        {**HOSTED, "RUNNER_ENVIRONMENT": "self-hosted"},
        {**HOSTED, "RUNNER_ARCH": "X64"},
        {**HOSTED, "GITHUB_JOB": "other"},
        {**HOSTED, "GITHUB_RUN_ID": ""},
        {"TASKPAW_MACOS_SMOKE_ISOLATION": "disposable-native"},
    ],
)
def test_rejected_isolation_has_zero_backend_spawns(change, tmp_path, monkeypatch):
    isolation = hosted_isolation(tmp_path, monkeypatch, change)
    calls = []
    monkeypatch.setattr(mac.subprocess, "Popen", lambda *a, **k: calls.append(a))
    with pytest.raises(mac.BuildError, match="isolation_required"):
        mac.smoke(Path("nonexistent-backend"), isolation.plan, isolation, {}, "3.9.8")
    assert calls == []


@pytest.mark.skipif(sys.platform != "darwin", reason="Mac-only execution context lock")
def test_hosted_context_lock_and_invalidation(tmp_path, monkeypatch):
    first = hosted_isolation(tmp_path, monkeypatch)
    second = hosted_isolation(tmp_path, monkeypatch)
    try:
        first.require()
        with pytest.raises(mac.BuildError, match="isolation_required"):
            second.require()
        first.env["RUNNER_ENVIRONMENT"] = "self-hosted"
        with pytest.raises(mac.BuildError, match="isolation_required"):
            first.require()
    finally:
        first.close()
        second.close()


def attestation(tmp_path, monkeypatch):
    boot = str(uuid.uuid4())
    monkeypatch.setattr(mac, "boot_uuid", lambda: boot)
    record = {
        "version": 1,
        "kind": "disposable-native",
        "session_id": str(uuid.uuid4()),
        "boot_session_uuid": boot,
        "target": TARGET,
        **{
            k: True
            for k in (
                "dedicated",
                "clean",
                "disposable",
                "no_real_taskpaw",
                "no_independent_taskpaw_launches",
            )
        },
    }
    path = tmp_path / "attestation.json"
    path.write_text(json.dumps(record))
    path.chmod(0o600)
    return path, record


@pytest.mark.skipif(
    sys.platform != "darwin", reason="Mac-only private-file and context contract"
)
def test_native_attestation_context_changed_session_refuses(tmp_path, monkeypatch):
    path, record = attestation(tmp_path, monkeypatch)
    isolation = hosted_isolation(
        tmp_path,
        monkeypatch,
        {
            "TASKPAW_MACOS_SMOKE_ISOLATION": "disposable-native",
            "TASKPAW_MACOS_SMOKE_ATTESTATION": str(path),
        },
    )
    try:
        isolation.require()
        record["clean"] = False
        path.write_text(json.dumps(record))
        calls = []
        monkeypatch.setattr(mac.subprocess, "Popen", lambda *a, **k: calls.append(a))
        with pytest.raises(mac.BuildError, match="isolation_required"):
            mac.smoke(Path("nonexistent"), isolation.plan, isolation, {}, "3.9.8")
        assert calls == []
    finally:
        isolation.close()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="Mac-only private-file and context contract"
)
@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("clean", False),
        ("disposable", False),
        ("dedicated", False),
        ("no_real_taskpaw", False),
        ("no_independent_taskpaw_launches", False),
        ("target", "wrong"),
        ("boot_session_uuid", str(uuid.uuid4())),
        ("session_id", "invalid"),
    ],
)
def test_invalid_native_attestation_zero_spawn(field, value, tmp_path, monkeypatch):
    path, record = attestation(tmp_path, monkeypatch)
    record[field] = value
    path.write_text(json.dumps(record))
    isolation = hosted_isolation(
        tmp_path,
        monkeypatch,
        {
            "TASKPAW_MACOS_SMOKE_ISOLATION": "disposable-native",
            "TASKPAW_MACOS_SMOKE_ATTESTATION": str(path),
        },
    )
    calls = []
    monkeypatch.setattr(mac.subprocess, "Popen", lambda *a, **k: calls.append(a))
    with pytest.raises(mac.BuildError, match="isolation_required"):
        mac.smoke(Path("nonexistent"), isolation.plan, isolation, {}, "3.9.8")
    assert calls == []


def test_ready_and_metrics_require_actual_fixture_contract():
    base = "http://127.0.0.1:41001"
    good = {
        "taskpaw_ready": True,
        "role": "agent",
        "base_url": base,
        "control_credential_file": "ignored-fake-path",
    }
    assert mac.ready_line(json.dumps(good), base)
    for change in ({"role": "hub"}, {"base_url": base + "/"}, {"taskpaw_ready": 1}):
        assert not mac.ready_line(json.dumps({**good, **change}), base)
    assert not mac.ready_line(b"invalid", base)
    record = {
        "server_id": "release-smoke",
        "machine": "release-smoke",
        "version": "3.9.8",
        "monitors": {
            "release-smoke-metrics": {
                "state": "ok",
                "metrics": {
                    k: 0
                    for k in (
                        "cpu_pct",
                        "mem_pct",
                        "disk_pct",
                        "net_in_bps",
                        "net_out_bps",
                    )
                },
            }
        },
    }
    assert mac.metrics_ok(record, "3.9.8", "release-smoke")
    assert not mac.metrics_ok(record, "3.9.8", "another-pairing")
    record["monitors"]["release-smoke-metrics"]["state"] = "unknown"
    assert not mac.metrics_ok(record, "3.9.8", "release-smoke")


def test_adhoc_preflight_never_calls_keychain(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(mac, "tool", lambda *a, **k: calls.append(a))
    mac.preflight(mac.normalize({}, TARGET), {}, ROOT)
    assert calls == []


@pytest.mark.parametrize(
    "monitor", [None, [], "invalid", {"state": "ok", "metrics": None}]
)
def test_malformed_metrics_are_rejected(monitor):
    record = {
        "server_id": "release-smoke",
        "machine": "release-smoke",
        "version": "3.9.8",
        "monitors": {"release-smoke-metrics": monitor},
    }
    assert not mac.metrics_ok(record, "3.9.8", "release-smoke")


def test_native_target_probe_strips_apple_inputs(monkeypatch):
    # Reproduce the Windows os surface while keeping this contract runnable.
    monkeypatch.delattr(mac.os, "uname", raising=False)
    monkeypatch.setattr(mac.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        mac.os, "uname", lambda: SimpleNamespace(machine="arm64"), raising=False
    )
    calls = []

    def probe(*args, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(returncode=0, stdout="0")

    monkeypatch.setattr(mac.subprocess, "run", probe)
    monkeypatch.setattr(
        mac, "tool", lambda *a, **k: (b"host: aarch64-apple-darwin\n", b"")
    )
    assert mac.native_target({"APPLE_PASSWORD": "FAKE-ONLY"}) == TARGET
    assert "APPLE_PASSWORD" not in calls[0]["env"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX owned process group fixture")
def test_native_tool_default_environment_strips_apple_inputs(monkeypatch):
    monkeypatch.setenv("APPLE_PASSWORD", "FAKE-ONLY")
    out, _ = mac.tool(
        "fixture",
        [sys.executable, "-c", "import os; print('APPLE_PASSWORD' in os.environ)"],
    )
    assert out == b"False\n"


def test_profiles_exact_allowlist(tmp_path):
    plan = mac.normalize({}, TARGET)
    mac.validate_profiles(plan, ROOT)
    mac.validate_profiles(mac.normalize(FORMAL, TARGET), ROOT)
    assert plistlib.loads(plan.entitlements(ROOT).read_bytes()) == {
        k: True for k in mac.ADHOC_KEYS
    }
    assert plistlib.loads(plan.entitlements(ROOT, False).read_bytes()) == {}


def test_signing_commands_never_password_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mac, "tool", lambda stage, cmd, **k: calls.append(cmd) or (b"", b"")
    )
    formal = mac.normalize(FORMAL, TARGET)
    mac.sign(Path("fake-native"), formal, {}, ROOT)
    assert calls[0][calls[0].index("--sign") + 1] == FAKE_ID
    assert "--keychain" in calls[0] and "runtime" in calls[0]
    assert not any(
        x in calls[0]
        for x in ("--password", "-P", "import", "unlock-keychain", "--deep")
    )
    calls.clear()
    mac.sign(Path("fake-native"), mac.normalize({}, TARGET), {}, ROOT, backend=True)
    assert "--keychain" not in calls[0] and "--timestamp=none" in calls[0]


@pytest.mark.parametrize(
    "out",
    [b'{"status":"Rejected"}', b'{"status":"In Progress"}', b"{}", b"invalid", b"[]"],
)
def test_notary_nonaccepted_is_not_success(out, monkeypatch):
    monkeypatch.setattr(mac, "tool", lambda *a, **k: (out, b""))
    with pytest.raises(mac.BuildError, match="not_accepted"):
        mac.notarize(Path("fake-archive"), mac.normalize(FORMAL, TARGET), {})


def test_notary_profile_only_accepted(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mac,
        "tool",
        lambda stage, cmd, **kw: (
            calls.append(cmd) or (b'{"status":"Accepted","id":"fake-public-id"}', b"")
        ),
    )
    mac.notarize(Path("fake-archive"), mac.normalize(FORMAL, TARGET), {})
    assert "--keychain-profile" in calls[0] and "--password" not in calls[0]


def test_arch_mismatch_is_failure(monkeypatch):
    monkeypatch.setattr(mac, "tool", lambda *a, **k: (b"arm64 x86_64", b""))
    with pytest.raises(mac.BuildError, match="architecture_mismatch"):
        mac.arches(Path("fake"), mac.normalize({}, TARGET), {})


def test_required_native_inventory_and_every_binary_verified(monkeypatch):
    entries = [
        ("Python", macho_header(6)),
        ("pydantic_core/_pydantic_core.so", macho_header(6)),
        ("psutil/_psutil_osx.so", macho_header(8)),
        ("another-extension.so", macho_header(8)),
        ("nested/main.so", macho_header(2)),
    ]
    checked = []
    monkeypatch.setattr(mac, "archive_entries", lambda p: iter(entries))
    monkeypatch.setattr(mac, "arches", lambda p, *a: checked.append(p.name))
    contexts = []
    monkeypatch.setattr(
        mac,
        "signature",
        lambda *a, **k: contexts.append((k["backend"], k["native_library"])),
    )
    assert mac.verify_archive(Path("fake"), mac.normalize({}, TARGET), {}, ROOT) == 5
    assert len(checked) == 5
    assert contexts == [(False, True)] * 4 + [(True, False)]
    entries.pop(2)
    with pytest.raises(mac.BuildError, match="required_extension_missing"):
        mac.verify_archive(Path("fake"), mac.normalize({}, TARGET), {}, ROOT)


def test_dmg_attach_failure_still_detaches_owned_mount(monkeypatch):
    calls = []

    def fake_tool(stage, cmd, **kw):
        calls.append(stage)
        if stage == "dmg_mount":
            raise mac.BuildError("mount_failed")
        return b"", b""

    monkeypatch.setattr(mac, "tool", fake_tool)
    monkeypatch.setattr(mac.os.path, "ismount", lambda p: True)
    with pytest.raises(mac.BuildError, match="mount_failed"):
        mac.verify_dmg(
            Path("fake"),
            mac.normalize({}, TARGET),
            None,
            {},
            ROOT,
            "3.9.8",
            "TaskPaw Agent",
        )
    assert calls == ["dmg_mount", "dmg_unmount"]


@pytest.mark.skipif(
    sys.platform != "darwin", reason="owned native Mach-O fixture, not product smoke"
)
def test_owned_tiny_macho_codesign_and_corruption(tmp_path):
    arch = "arm64" if os.uname().machine == "arm64" else "x86_64"
    target = next(t for t, a in mac.TARGETS.items() if a == arch)
    plan = mac.normalize({}, target)
    binary = tmp_path / "owned-tiny-macho"
    mac.tool(
        "fixture_compile",
        ["xcrun", "clang", "-arch", arch, "-x", "c", "-o", str(binary), "-"],
        input_data=b"int main(void) { return 0; }\n",
        timeout=30,
    )
    mac.sign(binary, plan, {}, ROOT, backend=True)
    mac.arches(binary, plan, {})
    mac.signature(binary, plan, {}, ROOT, backend=True)
    with binary.open("r+b") as f:
        f.seek(4096)
        f.write(b"altered fixture")
    with pytest.raises(mac.BuildError, match="signature_verify_failed"):
        mac.signature(binary, plan, {}, ROOT, backend=True)
    # Fixture is deliberately never executed; no packaged backend or keychain used.


def test_release_windows_sign_step_is_unchanged():
    after = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    marker = "      - name: Sign Windows installers"
    block = after[
        after.index(marker) : after.index(
            "      - name: Collect verified Mac installers"
        )
    ]
    # Exact Windows step frozen from 91f7745564f17a0039f81fab8293a9c150232236.
    # This oracle needs neither HEAD's content nor repository history in CI.
    assert hashlib.sha256(block.encode()).hexdigest() == (
        "0b6b3ccc73ec50e1c780f943b1c6488bc245b0e2f3ea664a5a057a4d54235faf"
    )
    assert "os: windows-latest\n            label: windows" in after
    assert "macos-13" not in after


@pytest.mark.parametrize(
    "machine,uname,translated,target,host,error",
    [
        ("arm64", "arm64", "0", TARGET, TARGET, None),
        (
            "x86_64",
            "x86_64",
            "1",
            "x86_64-apple-darwin",
            "x86_64-apple-darwin",
            "rosetta",
        ),
        ("arm64", "x86_64", "0", TARGET, TARGET, "architecture_mismatch"),
        ("arm64", "arm64", "0", "x86_64-apple-darwin", TARGET, "target_mismatch"),
        ("arm64", "arm64", "0", TARGET, "x86_64-apple-darwin", "host_mismatch"),
    ],
)
def test_native_architecture_contract(
    machine, uname, translated, target, host, error, monkeypatch
):
    monkeypatch.setattr(mac.platform, "machine", lambda: machine)
    monkeypatch.setattr(
        mac.os, "uname", lambda: SimpleNamespace(machine=uname), raising=False
    )
    monkeypatch.setattr(
        mac.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout=translated),
    )
    monkeypatch.setattr(
        mac, "tool", lambda *a, **kw: (("host: " + host + "\n").encode(), b"")
    )
    if error:
        with pytest.raises(mac.BuildError, match=error):
            mac.native_target({"TASKPAW_BUILD_TARGET": target})
    else:
        assert mac.native_target({"TASKPAW_BUILD_TARGET": target}) == target


def test_no_rust_only_allowed_for_native_sidecar_only(monkeypatch):
    monkeypatch.setattr(mac.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(
        mac.os, "uname", lambda: SimpleNamespace(machine="arm64"), raising=False
    )
    monkeypatch.setattr(
        mac.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="")
    )

    def unavailable(*args, **kwargs):
        raise mac.BuildError("rust_host_unavailable")

    monkeypatch.setattr(mac, "tool", unavailable)
    assert mac.native_target({}, skip_tauri=True) == TARGET
    with pytest.raises(mac.BuildError, match="rust_host_unavailable"):
        mac.native_target({})


def test_main_refuses_shared_host_before_build_or_backend(monkeypatch, capsys):
    build = load_build()
    monkeypatch.setattr(build.sys, "platform", "darwin")
    monkeypatch.setattr(mac, "native_target", lambda *a, **k: TARGET)
    for key in tuple(os.environ):
        if key.startswith(("APPLE_", "TASKPAW_MACOS_")):
            monkeypatch.delenv(key)
    calls = []
    monkeypatch.setattr(build, "build_backend", lambda *a: calls.append("build"))
    monkeypatch.setattr(mac.subprocess, "Popen", lambda *a, **k: calls.append("Popen"))
    assert build.main([]) == 1
    assert calls == []
    assert "isolation_required" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform != "darwin", reason="Mac private-file contract")
@pytest.mark.parametrize(
    "condition",
    ["missing", "symlink", "mode", "outside", "search", "identity", "profile"],
)
def test_formal_preflight_failure_no_import_unlock(condition, tmp_path, monkeypatch):
    keychain = tmp_path / "private.keychain-db"
    keychain.write_bytes(b"FAKE-NOT-A-KEYCHAIN")
    keychain.chmod(0o600)
    if condition == "missing":
        keychain.unlink()
    elif condition == "symlink":
        dest = tmp_path / "link"
        dest.symlink_to(keychain)
        keychain = dest
    elif condition == "mode":
        keychain.chmod(0o644)
    root = tmp_path if condition == "outside" else ROOT
    plan = mac.normalize(
        {**FORMAL, "TASKPAW_MACOS_SIGNING_KEYCHAIN": str(keychain)}, TARGET
    )
    calls = []
    monkeypatch.setattr(mac, "validate_profiles", lambda *a: None)
    monkeypatch.setattr(mac, "sign", lambda *a, **k: None)
    monkeypatch.setattr(mac, "signature", lambda *a, **k: None)

    def fake_tool(stage, cmd, **kw):
        calls.append(cmd)
        if stage == "keychain_context":
            return (
                b'"/fake/other"'
                if condition == "search"
                else json.dumps(str(keychain)).encode()
            ), b""
        if stage == "keychain_identity":
            return (
                ("B" * 40 if condition == "identity" else FAKE_ID)
                + ' "Developer ID Application: Fake"'
            ).encode(), b""
        if stage == "notary_profile_verify":
            raise mac.BuildError("notary_profile_verify_failed")
        return b"", b""

    monkeypatch.setattr(mac, "tool", fake_tool)
    with pytest.raises(mac.BuildError):
        mac.preflight(plan, {}, root)
    assert not any(
        x in cmd
        for cmd in calls
        for x in (
            "import",
            "unlock-keychain",
            "set-key-partition-list",
            "--password",
            "-P",
        )
    )


@pytest.mark.skipif(sys.platform != "darwin", reason="Mac private-file contract")
def test_formal_preflight_success_is_mocked_never_real_keychain(tmp_path, monkeypatch):
    path = tmp_path / "fake-private.keychain-db"
    path.write_bytes(b"FAKE-NOT-A-KEYCHAIN")
    path.chmod(0o600)
    plan = mac.normalize(
        {**FORMAL, "TASKPAW_MACOS_SIGNING_KEYCHAIN": str(path)}, TARGET
    )
    stages = []
    monkeypatch.setattr(mac, "sign", lambda *a, **k: stages.append("sign"))
    monkeypatch.setattr(mac, "signature", lambda *a, **k: stages.append("verify"))

    def fake_tool(stage, cmd, **kw):
        stages.append(stage)
        if stage == "keychain_context":
            return json.dumps(str(path)).encode(), b""
        if stage == "keychain_identity":
            return (FAKE_ID + ' "Developer ID Application: Fixture"').encode(), b""
        return b"{}", b""

    monkeypatch.setattr(mac, "tool", fake_tool)
    mac.preflight(plan, {}, ROOT)
    assert stages == [
        "keychain_context",
        "keychain_identity",
        "signing_probe_compile",
        "sign",
        "verify",
        "notary_profile_verify",
    ]


@pytest.mark.skipif(os.name == "nt", reason="POSIX owned process group fixture")
@pytest.mark.parametrize("scenario", ["failure", "timeout", "large"])
def test_native_tool_output_never_leaks_and_is_bounded(scenario, capsys):
    scripts = {
        "failure": "import sys; print('FAKE-SENSITIVE-OUTPUT'); sys.exit(3)",
        "timeout": "import time; print('FAKE-SENSITIVE-OUTPUT',flush=True); time.sleep(60)",
        "large": "import sys; sys.stdout.write('x'*200000); sys.stderr.write('y'*200000)",
    }
    cmd = [sys.executable, "-c", scripts[scenario]]
    if scenario == "large":
        out, err = mac.tool("fixture", cmd, timeout=5)
        assert len(out) == mac.TAIL_LIMIT and len(err) == mac.TAIL_LIMIT
    else:
        with pytest.raises(
            mac.BuildError,
            match="fixture_" + ("failed" if scenario == "failure" else "timeout"),
        ):
            mac.tool("fixture", cmd, timeout=0.05 if scenario == "timeout" else 5)
    output = capsys.readouterr()
    assert "FAKE-SENSITIVE" not in output.out + output.err


def test_smoke_initializes_owned_state_before_agent_launch(monkeypatch):
    from taskpaw_v3.agent import state
    from taskpaw_v3.core.state import StateSession

    calls = []
    gates = []

    class LaunchObserved(Exception):
        pass

    def initialize(stage, command, **kwargs):
        assert stage == "smoke_state_initialize"
        assert command[1] == "agent-state"
        assert command[-2:] == ["initialize", "--confirm-new-pairing"]
        config = Path(command[3])
        assert config.is_relative_to(Path(kwargs["env"]["HOME"]))
        assert str(config.parents[4]) == kwargs["cwd"]
        assert gates
        assert state.main(command[2:]) == 0
        calls.append(command)
        return b"", b""

    def launch(command, **kwargs):
        config = (
            Path(kwargs["env"]["HOME"])
            / "Library/Application Support/TaskPaw/agent.yaml"
        )
        primary = config.with_name("agent.state.json")
        assert primary.exists(), "smoke must explicitly initialize its owned pairing"
        session = StateSession.open(primary)
        try:
            assert session.record.server_id.startswith("agent-")
            assert session.record.next_event_id == 1
        finally:
            session.close()
        assert len(gates) == 3
        assert command == ["FAKE-SIDECAR", "agent"]
        raise LaunchObserved

    monkeypatch.setattr(mac, "tool", initialize)
    monkeypatch.setattr(mac, "owned_child", launch)
    with pytest.raises(LaunchObserved):
        mac.smoke(
            Path("FAKE-SIDECAR"),
            mac.normalize({}, TARGET),
            SimpleNamespace(require=lambda: gates.append("gate")),
            {},
            "3.9.8",
        )
    assert len(calls) == 1


@pytest.mark.parametrize("mode", ["failed", "missing", "json", "identity"])
def test_smoke_bad_initialization_never_launches_agent(monkeypatch, capsys, mode):
    def initialize(stage, command, **kwargs):
        if mode == "failed":
            raise mac.BuildError("smoke_state_initialize_failed")
        path = Path(command[3]).with_name("agent.state.json")
        if mode == "json":
            path.write_text("FAKE-SENSITIVE-METADATA")
        elif mode == "identity":
            path.write_text(json.dumps({"server_id": []}))
        return b"", b""

    def forbidden(*args, **kwargs):
        raise AssertionError("agent must not launch after bad initialization")

    monkeypatch.setattr(mac, "tool", initialize)
    monkeypatch.setattr(mac, "owned_child", forbidden)
    with pytest.raises(mac.BuildError, match="smoke_state"):
        mac.smoke(
            Path("FAKE-SIDECAR"),
            mac.normalize({}, TARGET),
            SimpleNamespace(require=lambda: None),
            {},
            "3.9.8",
        )
    output = capsys.readouterr()
    assert "FAKE-SENSITIVE" not in output.out + output.err


@pytest.mark.skipif(
    sys.platform != "darwin", reason="Mac native-group protocol fixture"
)
def test_smoke_protocol_with_python_fixture_not_product(tmp_path, monkeypatch):
    # Redirect Popen to a plain Python emitter. It imports no TaskPaw code and
    # makes no listener. This is protocol/cleanup evidence, never product readiness.
    real_popen = subprocess.Popen
    calls = []

    def fake_backend(cmd, **kwargs):
        from taskpaw_v3.core.config import AgentConfig, load_yaml

        config = (
            Path(kwargs["env"]["HOME"])
            / "Library/Application Support/TaskPaw/agent.yaml"
        )
        loaded = load_yaml(AgentConfig, config)
        body["server_id"] = loaded.server_id
        ready = {
            "taskpaw_ready": True,
            "role": "agent",
            "base_url": f"http://127.0.0.1:{loaded.control_port}",
        }
        emitted = (
            f"import time; print({json.dumps(ready)!r},flush=True); time.sleep(60)"
        )
        calls.append(cmd)
        return real_popen([sys.executable, "-c", emitted], **kwargs)

    def initialize(stage, cmd, **kwargs):
        from taskpaw_v3.agent import state

        assert stage == "smoke_state_initialize"
        assert state.main(cmd[2:]) == 0
        return b"", b""

    monkeypatch.setattr(mac, "tool", initialize)
    monkeypatch.setattr(mac.subprocess, "Popen", fake_backend)
    body = {
        "server_id": "release-smoke",
        "machine": "release-smoke",
        "version": "3.9.8",
        "monitors": {
            "release-smoke-metrics": {
                "state": "ok",
                "metrics": {
                    k: 0
                    for k in (
                        "cpu_pct",
                        "mem_pct",
                        "disk_pct",
                        "net_in_bps",
                        "net_out_bps",
                    )
                },
            }
        },
    }
    headers_seen = []

    class Connection:
        def __init__(self, *a, **k):
            pass

        def request(self, method, path, headers):
            headers_seen.append(headers)
            assert method == "GET" and path == "/status"

        def getresponse(self):
            return SimpleNamespace(status=200, read=lambda n: json.dumps(body).encode())

        def close(self):
            pass

    monkeypatch.setattr(mac.http.client, "HTTPConnection", Connection)
    contexts = []
    isolation = SimpleNamespace(require=lambda: contexts.append("gate"))
    mac.smoke(
        Path("FAKE-PROTOCOL-EMITTER"), mac.normalize({}, TARGET), isolation, {}, "3.9.8"
    )
    assert (
        len(contexts) == 3
        and len(calls) == 1
        and calls[0] == ["FAKE-PROTOCOL-EMITTER", "agent"]
    )
    assert headers_seen[0]["Authorization"].startswith("Bearer ")
    assert headers_seen[0]["Authorization"] not in str(calls)


def finalizer_fixture(tmp_path, monkeypatch, mode="adhoc"):
    plan = mac.normalize(FORMAL if mode == "formal" else {}, TARGET)
    cfg = {
        "identifier": "com.taskpaw.app.agent",
        "productName": "TaskPaw Agent",
        "version": "3.9.8",
    }
    bundle = plan.bundle_root(tmp_path)
    app = bundle / "macos/TaskPaw Agent.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    (app / "Contents/MacOS/taskpaw-backend").write_bytes(b"fixture")
    (app / "Contents/MacOS/taskpaw").write_bytes(b"fixture")
    events = []

    def fake_code(app):
        sidecar = app / "Contents/MacOS/taskpaw-backend"
        return [sidecar, app / "Contents/MacOS/taskpaw"], [], sidecar

    monkeypatch.setattr(mac, "app_code", fake_code)
    monkeypatch.setattr(
        mac, "sign", lambda p, *a, **k: events.append(("sign", p.name, k))
    )
    monkeypatch.setattr(mac, "signature", lambda *a, **k: None)
    monkeypatch.setattr(
        mac,
        "verify_app",
        lambda p, *a: (
            events.append(("verify-app", p.name))
            or (p / "Contents/MacOS/taskpaw-backend", 4)
        ),
    )
    monkeypatch.setattr(mac, "smoke", lambda *a: events.append(("smoke",)))

    def zip_app(app, path, env):
        events.append(("zip", path.name))
        path.write_bytes(b"fake-zip")

    monkeypatch.setattr(mac, "zip_app", zip_app)
    monkeypatch.setattr(
        mac, "notarize", lambda p, *a: events.append(("submit", p.name))
    )
    monkeypatch.setattr(mac, "staple", lambda p, *a: events.append(("staple", p.name)))
    monkeypatch.setattr(mac, "verify_zip", lambda *a: events.append(("verify-zip",)))
    monkeypatch.setattr(mac, "verify_dmg", lambda *a: events.append(("verify-dmg",)))

    def tool(stage, cmd, **kw):
        events.append((stage,))
        if stage == "dmg_create":
            Path(cmd[-1]).write_bytes(b"fake-dmg")
        return b"fake-commit", b""

    monkeypatch.setattr(mac, "tool", tool)
    return plan, cfg, bundle, events


@pytest.mark.parametrize("mode", ["adhoc", "formal"])
def test_finalizer_stages_verified_outputs_and_formal_order(
    mode, tmp_path, monkeypatch
):
    plan, cfg, bundle, events = finalizer_fixture(tmp_path, monkeypatch, mode)
    mac.finalize(plan, None, {}, tmp_path, cfg)
    summary = json.loads((bundle / "macos-verification.json").read_text())
    assert summary["mode"] == mode and summary["runtime_verified"] is True
    assert len(summary["artifacts"]) == 2
    assert not any(v in json.dumps(summary) for v in FORMAL.values())
    names = [e[0] for e in events]
    assert names[:3] == ["sign", "sign", "sign"]
    assert names.index("smoke") < names.index("zip")
    assert (
        names.index("verify-zip")
        < names.index("dmg_create")
        < names.index("verify-dmg")
    )
    if mode == "formal":
        first_staple = names.index("staple")
        assert names.index("submit") < first_staple
        assert events[first_staple][1] == "TaskPaw Agent.app"
        assert events[first_staple + 2][0] == "zip"
    else:
        assert "submit" not in names and "staple" not in names


def test_deliverable_failure_never_publishes_partial_success(tmp_path, monkeypatch):
    plan, cfg, bundle, events = finalizer_fixture(tmp_path, monkeypatch)

    def fail(*args):
        raise mac.BuildError("fixture_dmg_verification_failed")

    monkeypatch.setattr(mac, "verify_dmg", fail)
    with pytest.raises(mac.BuildError, match="verification_failed"):
        mac.finalize(plan, None, {}, tmp_path, cfg)
    assert not (bundle / "macos-verification.json").exists()
    assert not list(bundle.rglob("*.dmg")) and not list(bundle.rglob("*.app.zip"))


@pytest.mark.skipif(sys.platform != "darwin", reason="Mac session signal/lock fixture")
def test_build_session_restores_signal_and_releases_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(mac.tempfile, "gettempdir", lambda: str(tmp_path))
    previous = mac.signal.getsignal(mac.signal.SIGTERM)
    with pytest.raises(mac.BuildError, match="cancelled"):
        with mac.build_session(mac.normalize({}, TARGET), HOSTED, ROOT) as isolation:
            handler = mac.signal.getsignal(mac.signal.SIGTERM)
            handler(mac.signal.SIGTERM, None)
    assert isolation.fd is None
    assert mac.signal.getsignal(mac.signal.SIGTERM) == previous


def test_group_transient_permission_is_not_treated_as_absence(monkeypatch):
    probes = iter([None, PermissionError(), ProcessLookupError()])

    def killpg(pid, sig):
        item = next(probes)
        if item is not None:
            raise item

    monkeypatch.setattr(mac.os, "killpg", killpg, raising=False)
    waited = []
    proc = SimpleNamespace(
        pid=123, poll=lambda: None, wait=lambda **k: waited.append(True)
    )
    assert mac.stop_group(proc) is False
    assert waited == [True]


@pytest.mark.parametrize("compressed", [False, True])
def test_real_pinned_carchive_bytes_are_inspected_not_outer_exe(compressed, tmp_path):
    pytest.importorskip("PyInstaller")
    from PyInstaller.archive.writers import CArchiveWriter

    source = tmp_path / "native-bytes"
    data = b"\xcf\xfa\xed\xfe" + b"native-fixture" * 20
    source.write_bytes(data)
    archive = tmp_path / "fixture.pkg"
    CArchiveWriter(
        str(archive),
        [("psutil/_psutil_osx.so", str(source), compressed, "b")],
        "Python",
    )
    assert list(mac.archive_entries(archive)) == [("psutil/_psutil_osx.so", data)]


@pytest.mark.parametrize(
    "name,code,data,error",
    [
        ("../escape.so", "b", b"\xcf\xfa\xed\xfe", "name_invalid"),
        ("ordinary-native.so", "b", b"not-MachO", "binary_invalid"),
    ],
)
def test_real_carchive_bad_entries_rejected(name, code, data, error, tmp_path):
    pytest.importorskip("PyInstaller")
    from PyInstaller.archive.writers import CArchiveWriter

    source = tmp_path / "input"
    source.write_bytes(data)
    archive = tmp_path / "fixture.pkg"
    CArchiveWriter(str(archive), [(name, str(source), True, code)], "Python")
    with pytest.raises(mac.BuildError, match=error):
        list(mac.archive_entries(archive))


def test_corrupted_actual_carchive_is_fixed_error(tmp_path):
    pytest.importorskip("PyInstaller")
    archive = tmp_path / "fixture.pkg"
    archive.write_bytes(b"corrupted-not-a-carchive")
    with pytest.raises(mac.BuildError, match="archive_invalid"):
        list(mac.archive_entries(archive))


@pytest.mark.skipif(sys.platform != "darwin", reason="owned Mac cancellation fixtures")
@pytest.mark.parametrize("entrypoint", ["tool", "smoke"])
@pytest.mark.parametrize("boundary", ["creation", "selector", "register"])
@pytest.mark.parametrize("sig", [mac.signal.SIGTERM, mac.signal.SIGINT])
def test_cancellation_acquisition_and_setup_reaps_owned_child(
    entrypoint, boundary, sig, tmp_path, monkeypatch
):
    real_popen = subprocess.Popen
    real_selector = mac.selectors.DefaultSelector
    children = []

    def spawn(cmd, **kwargs):
        proc = real_popen(
            [sys.executable, "-c", "import time; time.sleep(60)"], **kwargs
        )
        children.append(proc)
        if boundary == "creation":
            os.kill(os.getpid(), sig)
        return proc

    class Selector:
        def __init__(self):
            if boundary == "selector":
                os.kill(os.getpid(), sig)
            self.inner = real_selector()

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def register(self, *args):
            os.kill(os.getpid(), sig)

    monkeypatch.setattr(mac.subprocess, "Popen", spawn)
    monkeypatch.setattr(mac.selectors, "DefaultSelector", Selector)
    try:
        with pytest.raises(mac.BuildError, match="cancelled"):
            with mac.build_session(
                mac.normalize({}, TARGET), {}, ROOT, skip_tauri=True
            ):
                if entrypoint == "tool":
                    mac.tool("fixture", ["FAKE-NOT-PRODUCT"], env={})
                else:
                    mac.smoke(
                        Path("FAKE-NOT-PRODUCT"),
                        mac.normalize({}, TARGET),
                        SimpleNamespace(require=lambda: None),
                        {},
                        "3.9.8",
                    )
        assert len(children) == 1
        assert children[0].poll() is not None
        assert children[0].stdout.closed and children[0].stderr.closed
    finally:
        # A failing RED still leaves no fixture child behind.
        for proc in children:
            if proc.poll() is None:
                mac.stop_group(proc, grace=1)
            proc.stdout.close()
            proc.stderr.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="owned native Darwin ACL fixture")
@pytest.mark.parametrize(
    "acl",
    ["everyone allow read", "everyone allow write", "everyone allow write,inherited"],
)
def test_private_file_rejects_effective_nonowner_acl(tmp_path, acl):
    p = tmp_path / "fake-private-record"
    p.write_bytes(b"FAKE-NOT-A-KEYCHAIN-OR-ATTESTATION")
    p.chmod(0o600)
    subprocess.run(["/bin/chmod", "+a", acl, str(p)], check=True, capture_output=True)
    with pytest.raises(mac.BuildError, match="private_file_invalid"):
        mac.private_file(p, outside=ROOT)


@pytest.mark.skipif(sys.platform != "darwin", reason="owned native Darwin ACL fixture")
@pytest.mark.parametrize(
    "acl", [None, "everyone deny delete", "everyone allow readattr", "owner"]
)
def test_private_file_preserves_safe_native_acl(tmp_path, acl):
    p = tmp_path / "fake-private-record"
    p.write_bytes(b"FAKE-ONLY")
    p.chmod(0o600)
    try:
        if acl:
            if acl == "owner":
                # Use the native qualifier UUID; never log or look up user names.
                libc = mac.ctypes.CDLL("/usr/lib/libSystem.B.dylib")
                fn = libc.mbr_uid_to_uuid
                fn.argtypes = [mac.ctypes.c_uint32, mac.ctypes.c_void_p]
                fn.restype = mac.ctypes.c_int
                owner = mac.ctypes.create_string_buffer(16)
                assert fn(os.getuid(), owner) == 0
                ptr = mac.ctypes.c_void_p
                for name, args, result in (
                    ("acl_init", [mac.ctypes.c_int], ptr),
                    (
                        "acl_create_entry",
                        [mac.ctypes.POINTER(ptr), mac.ctypes.POINTER(ptr)],
                        mac.ctypes.c_int,
                    ),
                    ("acl_set_tag_type", [ptr, mac.ctypes.c_int], mac.ctypes.c_int),
                    ("acl_set_qualifier", [ptr, ptr], mac.ctypes.c_int),
                    (
                        "acl_set_permset_mask_np",
                        [ptr, mac.ctypes.c_uint64],
                        mac.ctypes.c_int,
                    ),
                    (
                        "acl_set_fd_np",
                        [mac.ctypes.c_int, ptr, mac.ctypes.c_int],
                        mac.ctypes.c_int,
                    ),
                    ("acl_free", [ptr], mac.ctypes.c_int),
                ):
                    api = getattr(libc, name)
                    api.argtypes, api.restype = args, result
                native_acl = ptr(libc.acl_init(1))
                entry = ptr()
                fd = os.open(p, os.O_RDWR)
                try:
                    assert native_acl.value
                    assert (
                        libc.acl_create_entry(
                            mac.ctypes.byref(native_acl), mac.ctypes.byref(entry)
                        )
                        == 0
                    )
                    assert libc.acl_set_tag_type(entry, 1) == 0
                    assert libc.acl_set_qualifier(entry, owner) == 0
                    assert libc.acl_set_permset_mask_np(entry, 6) == 0
                    assert libc.acl_set_fd_np(fd, native_acl, 0x100) == 0
                finally:
                    os.close(fd)
                    libc.acl_free(native_acl)
            else:
                subprocess.run(
                    ["/bin/chmod", "+a", acl, str(p)], check=True, capture_output=True
                )
        fd = mac.private_file(p, outside=ROOT)
        os.close(fd)

    finally:
        subprocess.run(["/bin/chmod", "-N", str(p)], check=True, capture_output=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="owned native Darwin ACL fixture")
def test_private_acl_uses_original_fd_and_closes_on_rejection(tmp_path, monkeypatch):
    p = tmp_path / "fake-private-record"
    p.write_bytes(b"FAKE-ONLY")
    p.chmod(0o600)
    subprocess.run(
        ["/bin/chmod", "+a", "everyone allow write", str(p)],
        check=True,
        capture_output=True,
    )
    real_check = mac.darwin_private_acl
    fds = []

    def replace_path(fd):
        fds.append(fd)
        safe = tmp_path / "safe-replacement"
        safe.write_bytes(b"FAKE-ONLY")
        safe.chmod(0o600)
        os.replace(safe, p)
        real_check(fd)

    monkeypatch.setattr(mac, "darwin_private_acl", replace_path)
    with pytest.raises(mac.BuildError, match="private_file_invalid"):
        mac.private_file(p, outside=ROOT)
    with pytest.raises(OSError):
        os.fstat(fds[0])


@pytest.mark.skipif(sys.platform != "darwin", reason="owned Mac cancellation fixture")
@pytest.mark.parametrize("initial_cancel", [False, True])
def test_cleanup_defers_repeat_cancel_and_stays_sticky(initial_cancel, monkeypatch):
    real_stop = mac.stop_group
    real_popen = subprocess.Popen
    children = []
    previous = {
        s: mac.signal.getsignal(s) for s in (mac.signal.SIGTERM, mac.signal.SIGINT)
    }

    def spawn(cmd, **kw):
        p = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kw)
        children.append(p)
        if initial_cancel:
            os.kill(os.getpid(), mac.signal.SIGTERM)
        return p

    def stop(p, **kw):
        os.kill(os.getpid(), mac.signal.SIGTERM)
        os.kill(os.getpid(), mac.signal.SIGINT)
        return real_stop(p, **kw)

    monkeypatch.setattr(mac.subprocess, "Popen", spawn)
    monkeypatch.setattr(mac, "stop_group", stop)
    try:
        with pytest.raises(mac.BuildError, match="cancelled"):
            with mac.build_session(
                mac.normalize({}, TARGET), {}, ROOT, skip_tauri=True
            ):
                with pytest.raises(mac.BuildError, match="cancelled"):
                    with mac.owned_child(
                        ["FAKE-NOT-PRODUCT"],
                        stage="fixture",
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    ):
                        pass
                assert children[0].poll() is not None
                with pytest.raises(mac.BuildError, match="cancelled"):
                    with mac.owned_child(["MUST-NOT-SPAWN"], stage="fixture"):
                        raise AssertionError("must not enter")
                assert len(children) == 1
        assert mac._cancellation is None
        assert {s: mac.signal.getsignal(s) for s in previous} == previous
    finally:
        for p in children:
            if p.poll() is None:
                real_stop(p, grace=1)


@pytest.mark.skipif(sys.platform != "darwin", reason="Mac private-file error contract")
@pytest.mark.parametrize("failure", ["api", "metadata"])
def test_private_acl_fail_closed_and_fd_closed(failure, tmp_path, monkeypatch):
    p = tmp_path / "fake-private-record"
    p.write_bytes(b"FAKE-ONLY")
    p.chmod(0o600)
    real_check = mac.darwin_private_acl
    fds = []

    def fail(fd):
        fds.append(fd)
        if failure == "api":
            monkeypatch.setattr(
                mac.ctypes,
                "CDLL",
                lambda *a, **k: (_ for _ in ()).throw(OSError("FAKE-PRIVATE-DETAIL")),
            )
            real_check(fd)
        else:
            raise mac.BuildError("macos_private_file_invalid")

    monkeypatch.setattr(mac, "darwin_private_acl", fail)
    with pytest.raises(mac.BuildError, match="^macos_private_file_invalid$"):
        mac.private_file(p, outside=ROOT)
    with pytest.raises(OSError):
        os.fstat(fds[0])


@pytest.mark.skipif(
    sys.platform != "darwin", reason="native fake-file ACL API failures"
)
@pytest.mark.parametrize(
    "fault",
    ["tag", "permissions", "flags", "entries", "valid", "qualifier", "owner", "fd"],
)
def test_private_acl_unknown_or_failed_native_metadata_is_rejected(
    fault, tmp_path, monkeypatch
):
    p = tmp_path / "fake-private-record"
    p.write_bytes(b"FAKE-ONLY")
    p.chmod(0o600)
    subprocess.run(
        ["/bin/chmod", "+a", "everyone allow read", str(p)],
        check=True,
        capture_output=True,
    )
    libc = mac.ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    name = {
        "tag": "acl_get_tag_type",
        "permissions": "acl_get_permset_mask_np",
        "flags": "acl_get_flag_np",
        "entries": "acl_get_entry",
        "valid": "acl_valid",
        "qualifier": "acl_get_qualifier",
        "owner": "mbr_uid_to_uuid",
        "fd": "acl_get_fd_np",
    }[fault]
    original = getattr(libc, name)

    def injected(*args):
        original.argtypes, original.restype = injected.argtypes, injected.restype
        if fault in {"tag", "permissions"}:
            assert original(*args) == 0
            args[1]._obj.value = 3 if fault == "tag" else 1 << 63
            return 0
        if fault == "flags":
            return 1 if args[1] == 1 else original(*args)
        if fault in {"entries", "fd"}:
            mac.ctypes.set_errno(mac.errno.EIO)
            return -1 if fault == "entries" else None
        return None if fault == "qualifier" else 1

    setattr(libc, name, injected)
    monkeypatch.setattr(mac.ctypes, "CDLL", lambda *a, **k: libc)
    with pytest.raises(mac.BuildError, match="^macos_private_file_invalid$"):
        mac.private_file(p, outside=ROOT)


@pytest.mark.skipif(sys.platform != "darwin", reason="owned setup-failure fixtures")
@pytest.mark.parametrize("entrypoint", ["tool", "smoke"])
def test_ordinary_selector_failure_reaps_owned_child(entrypoint, monkeypatch):
    real_popen = subprocess.Popen
    children = []

    def spawn(cmd, **kw):
        p = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kw)
        children.append(p)
        return p

    def unavailable():
        raise OSError("FAKE-SELECTOR-FAILURE")

    monkeypatch.setattr(mac.subprocess, "Popen", spawn)
    monkeypatch.setattr(mac.selectors, "DefaultSelector", unavailable)
    try:
        with pytest.raises(OSError, match="FAKE-SELECTOR-FAILURE"):
            if entrypoint == "tool":
                mac.tool("fixture", ["FAKE-NOT-PRODUCT"], env={})
            else:
                mac.smoke(
                    Path("FAKE-NOT-PRODUCT"),
                    mac.normalize({}, TARGET),
                    SimpleNamespace(require=lambda: None),
                    {},
                    "3.9.8",
                )
        assert children[0].poll() is not None
        assert children[0].stdout.closed and children[0].stderr.closed
        assert mac._cancellation is None
    finally:
        for p in children:
            if p.poll() is None:
                mac.stop_group(p, grace=1)
            p.stdout.close()
            p.stderr.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="owned cleanup-transition fixture")
@pytest.mark.parametrize("boundary", ["entry", "exit"])
@pytest.mark.parametrize("operation", ["success", "error", "timeout"])
@pytest.mark.parametrize("sig", [mac.signal.SIGTERM, mac.signal.SIGINT])
def test_first_cancel_at_cleanup_transition_never_skips_child_cleanup(
    boundary, operation, sig, monkeypatch
):
    function = mac.owned_child.__wrapped__
    lines, start = inspect.getsourcelines(function)
    marker = (
        "if proc is not None:"
        if boundary == "entry"
        else "if forced and reject_forced:"
    )
    lineno = start + next(i for i, line in enumerate(lines) if marker in line)
    fired = False
    children = []
    real_popen = subprocess.Popen
    real_close = mac.Isolation.close
    closed_after_child = []

    def trace(frame, event, arg):
        nonlocal fired
        if (
            not fired
            and event == "line"
            and frame.f_code is function.__code__
            and frame.f_lineno == lineno
        ):
            fired = True
            os.kill(os.getpid(), sig)
        return trace

    def spawn(cmd, **kw):
        p = real_popen([sys.executable, "-c", "import time; time.sleep(60)"], **kw)
        children.append(p)
        return p

    def close(isolation):
        p = children[0]
        closed_after_child.append(
            p.poll() is not None and p.stdout.closed and p.stderr.closed
        )
        real_close(isolation)

    monkeypatch.setattr(mac.subprocess, "Popen", spawn)
    monkeypatch.setattr(mac.Isolation, "close", close)
    previous_trace = sys.gettrace()
    try:
        sys.settrace(trace)
        with pytest.raises(mac.BuildError, match="cancelled"):
            with mac.build_session(
                mac.normalize({}, TARGET), {}, ROOT, skip_tauri=True
            ):
                if operation == "timeout":
                    mac.tool("fixture", ["FAKE-NOT-PRODUCT"], env={}, timeout=0.01)
                else:
                    with mac.owned_child(
                        ["FAKE-NOT-PRODUCT"],
                        stage="fixture",
                        env={},
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    ):
                        if operation == "error":
                            raise RuntimeError("FAKE-OPERATION-ERROR")
        assert fired and len(children) == 1
        assert children[0].poll() is not None
        assert children[0].stdout.closed and children[0].stderr.closed
        assert closed_after_child == [True]
        assert mac._cancellation is None
    finally:
        sys.settrace(previous_trace)
        for p in children:
            if p.poll() is None:
                mac.stop_group(p, grace=1)
            p.stdout.close()
            p.stderr.close()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="owned bounded-wait cancellation fixture"
)
@pytest.mark.parametrize("sig", [mac.signal.SIGTERM, mac.signal.SIGINT])
def test_recorded_cancel_interrupts_pipe_closed_child_wait(sig, monkeypatch):
    import time

    function = mac.tool
    lines, start = inspect.getsourcelines(function)
    lineno = start + next(
        i for i, line in enumerate(lines) if "while proc.poll() is None:" in line
    )
    real_popen = subprocess.Popen
    children = []
    fired = False

    def spawn(cmd, **kw):
        p = real_popen(
            [
                sys.executable,
                "-c",
                "import os,time; os.close(1); os.close(2); time.sleep(60)",
            ],
            **kw,
        )
        children.append(p)
        return p

    def trace(frame, event, arg):
        nonlocal fired
        if (
            not fired
            and event == "line"
            and frame.f_code is function.__code__
            and frame.f_lineno == lineno
        ):
            fired = True
            os.kill(os.getpid(), sig)
        return trace

    monkeypatch.setattr(mac.subprocess, "Popen", spawn)
    previous_trace = sys.gettrace()
    started = time.monotonic()
    try:
        sys.settrace(trace)
        with pytest.raises(mac.BuildError, match="cancelled"):
            mac.tool("fixture", ["FAKE-NOT-PRODUCT"], env={}, timeout=60)
        assert fired and time.monotonic() - started < 5
        assert children[0].poll() is not None
        assert children[0].stdout.closed and children[0].stderr.closed
        assert mac._cancellation is None
    finally:
        sys.settrace(previous_trace)
        for p in children:
            if p.poll() is None:
                mac.stop_group(p, grace=1)
            p.stdout.close()
            p.stderr.close()


@pytest.mark.skipif(
    sys.platform != "darwin", reason="owned native library, never executed"
)
@pytest.mark.parametrize("link_mode", ["-dynamiclib", "-bundle"])
def test_owned_tiny_library_empty_entitlements(tmp_path, link_mode):
    arch = "arm64" if os.uname().machine == "arm64" else "x86_64"
    target = next(t for t, a in mac.TARGETS.items() if a == arch)
    plan = mac.normalize({}, target)
    binary = tmp_path / "owned-library"
    mac.tool(
        "fixture_compile",
        ["xcrun", "clang", "-arch", arch, link_mode, "-x", "c", "-o", str(binary), "-"],
        input_data=b"int fixture_value(void) { return 1; }\n",
        timeout=30,
    )
    # Empty profile: no library privilege grants, no keychain and no execution.
    mac.sign(binary, plan, {}, ROOT)
    mac.arches(binary, plan, {})
    mac.signature(binary, plan, {}, ROOT, native_library=True)


def macho_header(kind, *, byteorder="little", bits=64):
    magic = 0xFEEDFACF if bits == 64 else 0xFEEDFACE
    return (
        magic.to_bytes(4, byteorder)
        + bytes(8)
        + kind.to_bytes(4, byteorder)
        + bytes(16 if bits == 64 else 12)
    )


@pytest.mark.parametrize("kind", [2, 6, 8])
@pytest.mark.parametrize("byteorder", ["little", "big"])
@pytest.mark.parametrize("bits", [32, 64])
def test_macho_type_comes_from_native_header(tmp_path, kind, byteorder, bits):
    path = tmp_path / "misleading.exe"
    path.write_bytes(macho_header(kind, byteorder=byteorder, bits=bits))
    assert mac.macho_filetype(path) == kind


@pytest.mark.parametrize(
    "data",
    [b"fake", macho_header(1), macho_header(6)[:16], b"\xca\xfe\xba\xbe" + bytes(28)],
)
def test_unknown_or_fat_macho_type_is_rejected(tmp_path, data):
    path = tmp_path / "fake.so"
    path.write_bytes(data)
    with pytest.raises(mac.BuildError, match="macho_type_invalid"):
        mac.macho_filetype(path)


@pytest.mark.parametrize("mode", ["adhoc", "formal"])
@pytest.mark.parametrize("kind", [2, 6, 8])
@pytest.mark.parametrize("valid", [True, False])
def test_actual_code_type_entitlement_profile_is_exact(
    tmp_path, monkeypatch, mode, kind, valid
):
    cert = b"owned fake certificate bytes"
    inputs = {**FORMAL, "APPLE_SIGNING_IDENTITY": hashlib.sha1(cert).hexdigest()}
    plan = mac.normalize(inputs if mode == "formal" else {}, TARGET)
    binary = tmp_path / "misleading.so"
    binary.write_bytes(macho_header(kind))
    expected = (
        {k: True for k in mac.ADHOC_KEYS} if mode == "adhoc" and kind == 2 else {}
    )
    reported = (
        expected if valid else ({} if expected else {k: True for k in mac.ADHOC_KEYS})
    )
    stages = []

    def fake_tool(stage, cmd, **kwargs):
        stages.append(stage)
        if stage == "signature_metadata":
            return b"", (
                b"Signature=adhoc\n"
                if mode == "adhoc"
                else b"TeamIdentifier=FAKE123456\nTimestamp=fake\nflags=0x10000(runtime)\n"
            )
        if stage == "certificate_verify":
            Path(cmd[-2] + "0").write_bytes(cert)
        if stage == "entitlements_verify":
            return plistlib.dumps(reported) if reported else b"", b""
        return b"", b""

    monkeypatch.setattr(mac, "tool", fake_tool)
    if valid:
        mac.signature(
            binary, plan, {}, ROOT, backend=kind == 2, native_library=kind in {6, 8}
        )
    else:
        with pytest.raises(mac.BuildError, match="entitlements_invalid"):
            mac.signature(
                binary, plan, {}, ROOT, backend=kind == 2, native_library=kind in {6, 8}
            )
    if mode == "formal":
        assert "developer_id_verify" in stages and "certificate_verify" in stages


@pytest.mark.parametrize(
    "kind,backend,library", [(6, True, False), (8, True, False), (2, False, True)]
)
def test_main_and_library_context_cannot_be_swapped(
    tmp_path, monkeypatch, kind, backend, library
):
    path = tmp_path / "fake-code"
    path.write_bytes(macho_header(kind))
    monkeypatch.setattr(
        mac, "tool", lambda *a, **k: pytest.fail("wrong-kind reached codesign")
    )
    with pytest.raises(mac.BuildError, match="macho_type_invalid"):
        mac.signature(
            path,
            mac.normalize({}, TARGET),
            {},
            ROOT,
            backend=backend,
            native_library=library,
        )
