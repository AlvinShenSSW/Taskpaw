"""Hermetic regression fixtures for actual audit schemas and coverage failures."""

from __future__ import annotations

import copy
import io
import json
import os
import sys
import time
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from scripts import dependency_scan as scan  # noqa: E402


@pytest.fixture
def rust_items():
    return [
        {
            "name": "glib",
            "version": "0.18.5",
            "source": scan.REGISTRY_SOURCE,
            "checksum": "a" * 64,
        },
        {
            "name": "proc-macro-error",
            "version": "1.0.4",
            "source": scan.REGISTRY_SOURCE,
            "checksum": "b" * 64,
        },
    ]


def index_bytes(items, yanked=False):
    return (
        b"\n".join(
            json.dumps(
                {
                    "name": item["name"],
                    "vers": item["version"],
                    "cksum": item["checksum"],
                    "yanked": yanked,
                }
            ).encode()
            for item in items
        )
        + b"\n"
    )


def fake_fetch(items, yanked=False):
    def fetch(url, limit):
        if url.endswith("config.json"):
            data = b'{"dl":"https://static.crates.io/crates","api":"https://crates.io"}'
        else:
            data = index_bytes(
                [
                    item
                    for item in items
                    if url == scan.INDEX_URL + scan.index_path(item["name"])
                ],
                yanked,
            )
        assert len(data) <= limit
        return data, {
            "url": url,
            "status": 200,
            "bytes": len(data),
            "sha256": scan.sha256(data),
            "fetched_utc": "2026-10-01T00:00:00+00:00",
        }

    return fetch


def rust_report(items):
    def warning(item, advisory_id, kind, aliases=()):
        return {
            "kind": kind,
            "package": {**item},
            "advisory": {"id": advisory_id, "aliases": list(aliases)},
            "versions": {"patched": []},
        }

    return {
        "database": {
            "advisory-count": 1278,
            "last-commit": "c" * 40,
            "last-updated": scan.utc_now(),
        },
        "lockfile": {"dependency-count": len(items) + 1},
        "settings": {
            "ignore": [],
            "severity": None,
            "target_arch": [],
            "target_os": [],
            "informational_warnings": scan.CATEGORIES.copy(),
        },
        "vulnerabilities": {"found": False, "count": 0, "list": []},
        "warnings": {
            "unsound": [
                warning(
                    items[0], "RUSTSEC-2024-0429", "unsound", ["GHSA-wrw7-89jp-8q8g"]
                )
            ],
            "unmaintained": [warning(items[1], "RUSTSEC-2024-0370", "unmaintained")],
        },
    }


def result(code, report=None):
    return {
        "exit": code,
        "stdout": json.dumps(report).encode() if report else b"",
        "stderr": b"",
    }


def test_real_silent_cargo_shape_requires_independent_coverage(rust_items):
    # Matches the critic's real interface shape: exit1, empty stderr, valid JSON,
    # fresh DB, zero vulnerabilities, only two accepted GTK warnings. No skip field.
    report = rust_report(rust_items)
    with pytest.raises(scan.ScanError, match="coverage"):
        scan.parse_rust(report, rust_items, result(1), None)
    coverage = scan.registry_coverage(rust_items, fake_fetch(rust_items))
    findings = scan.normalize_findings(
        scan.parse_rust(report, rust_items, result(1), coverage)
    )
    policy = scan.load_json(
        (ROOT / "docs/security/dependency-exceptions.json").read_bytes()
    )
    actionable, accepted = scan.apply_exceptions(policy, findings, date(2026, 10, 1))
    assert scan.overall_status([], actionable, accepted) == (0, "accepted_exceptions")
    assert scan.overall_status(["registry failed"], actionable, accepted) == (
        2,
        "scan_error",
    )
    assert coverage["native_cargo_yank_success_certified"] is False


@pytest.mark.parametrize("failure", ["config", "crate", "partial-version"])
def test_registry_individual_and_partial_failures(failure, rust_items):
    items = rust_items + [{**rust_items[0], "version": "0.18.4", "checksum": "d" * 64}]
    base = fake_fetch(items)

    def fetch(url, limit):
        if (failure == "config" and url.endswith("config.json")) or (
            failure == "crate" and url.endswith("proc-macro-error")
        ):
            raise scan.ScanError("public index request failed")
        if failure == "partial-version" and url.endswith("glib"):
            return fake_fetch(rust_items)(url, limit)
        return base(url, limit)

    with pytest.raises(scan.ScanError):
        scan.registry_coverage(items, fetch)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "checksum",
        "case",
        "yanked-type",
        "schema",
        "json",
        "duplicate-key",
        "line-size",
    ],
)
def test_bad_index_records_fail_closed(mutation, rust_items):
    items = [rust_items[0]]
    record = {"name": "glib", "vers": "0.18.5", "cksum": "a" * 64, "yanked": False}
    if mutation == "checksum":
        record["cksum"] = "e" * 64
    if mutation == "case":
        record["name"] = "GLib"
    if mutation == "yanked-type":
        record["yanked"] = "false"
    if mutation == "schema":
        record["v"] = 3
    data = json.dumps(record).encode() + b"\n"
    if mutation == "missing":
        data = data.replace(b"0.18.5", b"0.18.4")
    if mutation == "duplicate":
        data += data
    if mutation == "json":
        data = b'{"truncated":'
    if mutation == "duplicate-key":
        data = data.replace(b'"yanked": false', b'"yanked": false, "yanked": true')
    if mutation == "line-size":
        data = b" " * (scan.LINE_LIMIT + 1) + data
    with pytest.raises(scan.ScanError):
        scan.parse_index(data, items)


@pytest.mark.parametrize(
    "name,path",
    [
        ("a", "1/a"),
        ("ab", "2/ab"),
        ("Abc", "3/a/abc"),
        ("Inflector", "in/fl/inflector"),
        ("quick-xml", "qu/ic/quick-xml"),
    ],
)
def test_index_layout_preserves_metadata_case(name, path):
    assert scan.index_path(name) == path
    item = {
        "name": name,
        "version": "1.0.0",
        "checksum": "1" * 64,
        "source": scan.REGISTRY_SOURCE,
    }
    assert scan.parse_index(index_bytes([item]), [item])[0]["name"] == name


def test_real_yanked_record_is_actionable_outside_gtk_exceptions():
    # Actual public Inflector index record captured during design; not claimed
    # to be a TaskPaw dependency. The test-owned lock uses its exact metadata.
    item = {
        "name": "Inflector",
        "version": "0.3.2",
        "checksum": "8dc0de8dc5c3b8e8a7fe2868553c407e5cd4a897d67df7553f2fe7f038b058c3",
        "source": scan.REGISTRY_SOURCE,
    }
    data = index_bytes([item], True)
    parsed = scan.parse_index(data, [item])
    assert parsed[0]["yanked"] is True
    coverage = scan.registry_coverage([item], fake_fetch([item], True))
    findings = scan.validate_coverage(coverage, [item])
    actionable, accepted = scan.apply_exceptions(
        {"schema_version": 1, "exceptions": []}, findings, date(2026, 10, 1)
    )
    assert scan.overall_status([], actionable, accepted) == (1, "actionable_findings")
    assert findings[0]["classification"] == "yanked"


def test_complete_multiversion_coverage_and_identity_mismatch(rust_items):
    items = [
        rust_items[0],
        {**rust_items[0], "version": "0.18.4", "checksum": "d" * 64},
    ]
    coverage = scan.registry_coverage(items, fake_fetch(items))
    assert coverage["covered_count"] == 2 and len(coverage["requests"]) == 2
    assert scan.validate_coverage(coverage, items) == []
    coverage["identities"][0]["checksum"] = "f" * 64
    with pytest.raises(scan.ScanError, match="partial"):
        scan.validate_coverage(coverage, items)


@pytest.mark.parametrize(
    "status,encoding,data,length",
    [
        (304, "identity", b"{}", "2"),
        (200, "gzip", b"{}", "2"),
        (200, "identity", b"{}", "3"),
        (200, "identity", b"{}", "99999999"),
    ],
)
def test_http_status_compression_truncation_and_bounds(
    monkeypatch, status, encoding, data, length
):
    class Response(io.BytesIO):
        headers = {"Content-Encoding": encoding, "Content-Length": length}

        def geturl(self):
            return scan.INDEX_URL + "config.json"

    response = Response(data)
    response.status = status
    monkeypatch.setattr(
        scan.urllib.request,
        "build_opener",
        lambda *args: SimpleNamespace(open=lambda *args, **kwargs: response),
    )
    with pytest.raises(scan.ScanError):
        scan.fetch_index(scan.INDEX_URL + "config.json", scan.CONFIG_LIMIT)


def test_http_redirect_and_no_ambient_proxy(monkeypatch):
    captured = []

    def build(*handlers):
        captured.extend(handlers)
        raise OSError("synthetic unavailable public endpoint")

    monkeypatch.setenv("HTTPS_PROXY", "http://secret.invalid")
    monkeypatch.setattr(scan.urllib.request, "build_opener", build)
    with pytest.raises(OSError):
        scan.fetch_index(scan.INDEX_URL + "config.json", scan.CONFIG_LIMIT)
    assert (
        next(
            x for x in captured if isinstance(x, scan.urllib.request.ProxyHandler)
        ).proxies
        == {}
    )
    with pytest.raises(scan.ScanError, match="redirect"):
        scan.NoRedirect().redirect_request(
            None, None, 302, "", {}, "https://secret.invalid"
        )


@pytest.mark.skipif(
    scan.tomllib is None,
    reason="owned TOML policy execution uses the mandated Python 3.12 scanner",
)
@pytest.mark.parametrize(
    "section,key,value",
    [
        ("advisories", "ignore", ["RUSTSEC-2026-0194"]),
        ("advisories", "severity_threshold", "high"),
        ("target", "os", "windows"),
        ("target", "arch", "x86_64"),
        ("yanked", "enabled", False),
        ("yanked", "update_index", False),
        ("database", "fetch", False),
        ("database", "stale", True),
        ("database", "url", "https://other.invalid/db"),
    ],
)
def test_owned_policy_tampering_is_scan_error(tmp_path, section, key, value):
    path = tmp_path / "audit.toml"
    db = tmp_path / "db"
    scan.write_policy(path, db)
    policy = scan.audit_policy(db)
    policy[section][key] = value
    path.write_text(
        "\n".join(
            f"[{section}]\n"
            + "\n".join(f"{key} = {json.dumps(value)}" for key, value in fields.items())
            for section, fields in policy.items()
        )
    )
    with pytest.raises(scan.ScanError, match="policy drift"):
        scan.validate_policy(path, db)


@pytest.mark.parametrize(
    "key,value",
    [
        ("ignore", ["RUSTSEC-2026-0194", "RUSTSEC-2026-0195"]),
        ("severity", "high"),
        ("target_os", ["windows"]),
        ("target_arch", ["x86_64"]),
        ("informational_warnings", ["notice"]),
    ],
)
def test_inherited_suppressed_report_is_rejected(rust_items, key, value):
    report = rust_report(rust_items)
    report["settings"][key] = value
    with pytest.raises(scan.ScanError):
        scan.validate_rust_settings(report)


def test_owned_environment_does_not_inherit_policy_or_credentials(
    tmp_path, monkeypatch
):
    project = tmp_path / "ambient-project"
    (project / ".cargo").mkdir(parents=True)
    (project / ".cargo/audit.toml").write_text(
        '[advisories]\nignore=["RUSTSEC-2026-0194"]\n[yanked]\nenabled=false\n'
    )
    home = tmp_path / "ambient-cargo-home"
    home.mkdir()
    (home / "audit.toml").write_text(
        '[database]\nfetch=false\nurl="https://other.invalid"\n'
    )
    for key, value in {
        "CARGO_HOME": str(home),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "SECRET",
        "HTTPS_PROXY": "SECRET",
        "NPM_TOKEN": "SECRET",
        "GIT_ASKPASS": "SECRET",
        "CARGO_REGISTRIES_CRATES_IO_INDEX": "SECRET",
    }.items():
        monkeypatch.setenv(key, value)
    context = tmp_path / "owned"
    context.mkdir()
    env = scan.create_context(context)
    assert env["CARGO_HOME"] == str(context / "cargo-home")
    assert env["GIT_CONFIG_COUNT"] == "0" and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "SECRET" not in json.dumps(env)
    assert "HTTPS_PROXY" not in env and "GIT_CONFIG_KEY_0" not in env
    assert Path(env["GIT_CONFIG_GLOBAL"]).read_text() == ""
    assert (context / "cwd").is_dir() and not (
        context / "cwd/.cargo/audit.toml"
    ).exists()
    # Neither real configuration nor the controlled ambient files is an input.
    assert (home / "audit.toml").read_text().startswith("[database]")


def test_invalid_cargo_home_is_rejected_before_tool(tmp_path):
    context = tmp_path / "owned"
    context.mkdir()
    env = scan.create_context(context)
    home = Path(env["CARGO_HOME"])
    home.rmdir()
    home.write_text("not a directory")
    runner = scan.Runner(ROOT, context, env, tmp_path)
    if scan.tomllib is None:
        pytest.skip("Rust lock TOML parsing is a Python 3.12 scanner gate")
    with pytest.raises(scan.ScanError, match="CARGO_HOME"):
        scan.scan_rust(runner, "does-not-exist")


def test_python_marker_union_profiles_and_multiple_versions():
    lock = {
        "version": 1,
        "package": [
            {
                "name": "taskpaw",
                "version": "1.0",
                "source": {"virtual": "."},
                "dependencies": [{"name": "base"}],
                "optional-dependencies": {
                    "tray": [{"name": "image", "marker": "sys_platform == 'win32'"}]
                },
                "dev-dependencies": {"dev": [{"name": "base"}]},
            },
            {
                "name": "base",
                "version": "1.0",
                "source": {"registry": "https://pypi.org/simple"},
            },
            {
                "name": "image",
                "version": "1.0",
                "source": {"registry": "https://pypi.org/simple"},
            },
            {
                "name": "image",
                "version": "2.0",
                "source": {"registry": "https://pypi.org/simple"},
            },
        ],
    }
    inventory = scan.python_inventory(lock)
    assert len(inventory) == 3
    assert inventory[0]["profiles"] == ["base-runtime-candidate", "development"]
    assert all(
        "optional-tray-runtime-candidate" in x["profiles"]
        for x in inventory
        if x["name"] == "image"
    )
    assert sorted(len(x) for x in scan.python_batches(inventory)) == [1, 2]
    lock["package"][1]["source"] = {"git": "https://invalid"}
    with pytest.raises(scan.ScanError, match="source"):
        scan.python_inventory(lock)


def test_python_audit_identity_coverage_and_alias_dedup():
    expected = [
        {
            "name": "pillow",
            "version": "10.4.0",
            "profiles": ["optional-tray-runtime-candidate"],
        }
    ]
    vuln = {
        "id": "PYSEC-2024-1",
        "aliases": ["GHSA-aaaa-bbbb-cccc", "CVE-2024-12345"],
        "fix_versions": ["12.3.0"],
    }
    report = {
        "dependencies": [
            {
                "name": "Pillow",
                "version": "10.4.0",
                "vulns": [
                    vuln,
                    {
                        **vuln,
                        "id": "GHSA-aaaa-bbbb-cccc",
                        "aliases": ["PYSEC-2024-1", "CVE-2024-12345"],
                    },
                ],
            }
        ],
        "fixes": [],
    }
    findings = scan.parse_python(report, expected, result(1))
    assert len(findings) == 2 and len(scan.normalize_findings(findings)) == 1
    report["dependencies"][0]["skip_reason"] = "unavailable"
    with pytest.raises(scan.ScanError, match="skipped"):
        scan.parse_python(report, expected, result(1))
    with pytest.raises(scan.ScanError, match="coverage"):
        scan.parse_python({"dependencies": [], "fixes": []}, expected, result(0))


def npm_fixture():
    def package(version, dev=False):
        return {
            "version": version,
            "dev": dev,
            "resolved": "https://registry.npmjs.org/example/-/example-1.0.0.tgz",
            "integrity": "sha512-test",
        }

    lock = {
        "lockfileVersion": 3,
        "packages": {
            "": {"dependencies": {"example": "1"}},
            "node_modules/example": package("1.0.0"),
            "node_modules/dev": package("1.0.0", True),
        },
    }
    via = {"url": "https://github.com/advisories/GHSA-aaaa-bbbb-cccc"}
    report = {
        "auditReportVersion": 2,
        "metadata": {"dependencies": {"total": 2}, "vulnerabilities": {"total": 2}},
        "vulnerabilities": {
            "example": {
                "name": "example",
                "nodes": ["node_modules/example"],
                "via": [via],
            },
            "dev": {"name": "dev", "nodes": ["node_modules/dev"], "via": ["example"]},
        },
    }
    return lock, report


def test_npm_recursive_alias_paths_dev_and_runtime_counts():
    lock, report = npm_fixture()
    inventory = scan.npm_inventory(lock)
    findings = scan.normalize_findings(scan.parse_npm(report, inventory, result(1)))
    assert len(findings) == 2 and len({x["advisory_id"] for x in findings}) == 1
    assert {tuple(x["profiles"]) for x in findings} == {
        ("development",),
        ("production-candidate",),
    }
    assert {x["paths"][0] for x in findings} == {
        "node_modules/example",
        "node_modules/dev",
    }
    with pytest.raises(scan.ScanError, match="dev node"):
        scan.parse_npm(report, inventory, result(1), True)
    report["vulnerabilities"]["example"]["via"] = ["dev"]
    with pytest.raises(scan.ScanError, match="cyclic"):
        scan.parse_npm(report, inventory, result(1))


@pytest.mark.parametrize(
    "code,report", [(0, {"error": {"message": "SECRET"}}), (1, {}), (2, {}), (0, None)]
)
def test_tool_error_and_malformed_reports_never_findings(code, report):
    with pytest.raises(scan.ScanError):
        scan.result_json(result(code, report))


def test_exit_one_is_findings_only_when_consistent():
    scan.check_exit(result(1), True)
    scan.check_exit(result(0), False)
    with pytest.raises(scan.ScanError):
        scan.check_exit(result(1), False)
    with pytest.raises(scan.ScanError):
        scan.check_exit(result(0), True)


@pytest.mark.parametrize(
    "change",
    [
        "expired",
        "future",
        "unbounded",
        "wildcard-version",
        "wrong-version",
        "wrong-id",
        "empty-owner",
        "duplicate",
        "unknown-field",
        "wrong-alias",
    ],
)
def test_exception_exact_dates_and_stale_boundaries(change, rust_items):
    policy = scan.load_json(
        (ROOT / "docs/security/dependency-exceptions.json").read_bytes()
    )
    report = rust_report(rust_items)
    coverage = scan.registry_coverage(rust_items, fake_fetch(rust_items))
    findings = scan.normalize_findings(
        scan.parse_rust(report, rust_items, result(1), coverage)
    )
    entry = policy["exceptions"][0]
    if change == "expired":
        entry["expires_on"] = "2026-10-01"
    if change == "future":
        entry["reviewed_on"] = "2026-10-02"
    if change == "unbounded":
        entry["expires_on"] = "2026-11-02"
    if change == "wildcard-version":
        entry["versions"] = ["*"]
    if change == "wrong-version":
        entry["versions"] = ["0.18.4"]
    if change == "wrong-id":
        entry["advisory_id"] = "RUSTSEC-2024-9999"
    if change == "empty-owner":
        entry["owner"] = " "
    if change == "duplicate":
        policy["exceptions"].append(copy.deepcopy(entry))
    if change == "unknown-field":
        entry["ignore_errors"] = True
    if change == "wrong-alias":
        entry["aliases"] = []
    with pytest.raises(scan.ScanError):
        scan.apply_exceptions(policy, findings, date(2026, 10, 1))


def test_expiry_is_exclusive_and_new_advisory_stays_actionable(rust_items):
    policy = scan.load_json(
        (ROOT / "docs/security/dependency-exceptions.json").read_bytes()
    )
    findings = scan.normalize_findings(
        scan.parse_rust(
            rust_report(rust_items),
            rust_items,
            result(1),
            scan.registry_coverage(rust_items, fake_fetch(rust_items)),
        )
    )
    with pytest.raises(scan.ScanError, match="expired"):
        scan.apply_exceptions(policy, findings, date(2026, 10, 31))
    new = scan.finding("rust", "glib", "0.18.5", "RUSTSEC-2026-9999")
    actionable, accepted = scan.apply_exceptions(
        policy, findings + [new], date(2026, 10, 1)
    )
    assert actionable == [new] and len(accepted) == 2


def test_command_deadline_and_output_limit_reap_owned_children(tmp_path, monkeypatch):
    start = time.monotonic()
    with pytest.raises(scan.ScanError, match="deadline"):
        scan.run_command(
            [sys.executable, "-c", "import time; time.sleep(20)"],
            tmp_path,
            dict(os.environ),
            timeout=0.1,
        )
    assert time.monotonic() - start < 3
    monkeypatch.setattr(scan, "STDOUT_LIMIT", 100)
    with pytest.raises(scan.ScanError, match="output limit"):
        scan.run_command(
            [sys.executable, "-c", "print('x' * 200)"], tmp_path, dict(os.environ)
        )
    with pytest.raises(scan.ScanError, match="could not start"):
        scan.run_command([str(tmp_path / "missing")], tmp_path, {})


def test_output_does_not_overwrite_inputs_or_source(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    with pytest.raises(scan.ScanError):
        scan.output_directory(root, root / "scripts")
    directory = scan.output_directory(root, root / "build/dependency-scan")
    assert (directory / ".taskpaw-dependency-scan").is_file()
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "file").write_text("private")
    with pytest.raises(scan.ScanError):
        scan.output_directory(root, unrelated)


def test_input_mutation_remains_error_and_other_scans_run(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    for path in scan.INPUTS:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}")
    (root / scan.INPUTS[-1]).write_text('{"schema_version":1,"exceptions":[]}')
    output = scan.output_directory(root, tmp_path / "artifacts")
    called = []

    def python(runner):
        called.append("python")
        (root / "uv.lock").write_text("changed")
        raise scan.ScanError("public advisory endpoint unavailable")

    def npm(runner):
        called.append("npm")
        return []

    def rust(runner, tool):
        called.append("rust")
        return []

    monkeypatch.setattr(scan, "scan_python", python)
    monkeypatch.setattr(scan, "scan_npm", npm)
    monkeypatch.setattr(scan, "scan_rust", rust)
    monkeypatch.setattr(
        scan.Runner, "check", lambda *args: {"stdout": b"a" * 40, "exit": 0}
    )
    assert scan.scan(root, output) == 2 and called == ["python", "npm", "rust"]
    report = scan.load_json((output / "summary.json").read_bytes())
    assert any(x["reason"] == "scan inputs mutated" for x in report["errors"])
    assert str(tmp_path) not in (output / "summary.json").read_text()


def test_workflow_permissions_and_manual_periodic_scan():
    text = (ROOT / ".github/workflows/dependency-scan.yml").read_text()
    assert (
        "workflow_dispatch:" in text and "schedule:" in text and "pull_request:" in text
    )
    assert (
        "pull_request_target" not in text
        and "contents: read" in text
        and "persist-credentials: false" in text
    )
    assert (
        "secrets." not in text
        and "continue-on-error" not in text
        and "|| true" not in text
    )
    assert "timeout-minutes: 30" in text and "retention-days: 30" in text


def test_registry_window_concurrency_and_total_response_budget(monkeypatch, rust_items):
    import threading

    items = [{**rust_items[0], "name": f"crate{n}"} for n in range(24)]
    active, peak = 0, 0
    guard = threading.Lock()
    base = fake_fetch(items)

    def fetch(url, limit):
        nonlocal active, peak
        if url.endswith("config.json"):
            return base(url, limit)
        with guard:
            active += 1
            peak = max(peak, active)
        time.sleep(0.005)
        value = base(url, limit)
        with guard:
            active -= 1
        return value

    coverage = scan.registry_coverage(items, fetch)
    assert 1 < peak <= 8 and coverage["covered_count"] == 24
    monkeypatch.setattr(scan, "INDEX_TOTAL_LIMIT", 4)
    budget = scan.IndexBudget()
    budget.consume(4)
    with pytest.raises(scan.ScanError, match="aggregate"):
        budget.consume(1)
    assert budget.bytes == 4
    with pytest.raises(scan.ScanError, match="aggregate"):
        scan.registry_coverage(items, fetch)


@pytest.mark.parametrize("inherit_pipes", [False, True])
def test_owned_descendants_are_reaped_even_after_root_exits(tmp_path, inherit_pipes):
    marker = tmp_path / "orphan-marker"
    child = f"import time; from pathlib import Path; time.sleep(0.4); Path({str(marker)!r}).write_text('orphan')"
    redirect = (
        ""
        if inherit_pipes
        else ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL"
    )
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}]{redirect})"
    if inherit_pipes:
        with pytest.raises(scan.ScanError, match="deadline"):
            scan.run_command(
                [sys.executable, "-c", parent], tmp_path, dict(os.environ), timeout=0.15
            )
    else:
        value = scan.run_command(
            [sys.executable, "-c", parent], tmp_path, dict(os.environ), timeout=2
        )
        assert value["exit"] == 0
    time.sleep(0.45)
    assert not marker.exists(), "owned descendant survived wrapper cleanup"


def test_critic_seven_inherited_ignores_cannot_bypass_postscan_policy(rust_items):
    report = rust_report(rust_items)
    report["settings"]["ignore"] = [
        "RUSTSEC-2026-0195",
        "RUSTSEC-2026-0194",
        "RUSTSEC-2025-0081",
        "RUSTSEC-2025-0075",
        "RUSTSEC-2025-0080",
        "RUSTSEC-2025-0100",
        "RUSTSEC-2025-0098",
    ]
    coverage = scan.registry_coverage(rust_items, fake_fetch(rust_items))
    with pytest.raises(scan.ScanError, match="suppression"):
        scan.parse_rust(report, rust_items, result(1), coverage)


def test_failed_command_evidence_is_retained_without_stderr_secret(
    tmp_path, monkeypatch
):
    context = tmp_path / "context"
    context.mkdir()
    env = scan.create_context(context)
    env["TEST_PRIVATE_DIAGNOSTIC"] = "SECRET" * 30
    output = tmp_path / "output"
    output.mkdir()
    runner = scan.Runner(ROOT, context, env, output)
    monkeypatch.setattr(scan, "STDERR_LIMIT", 100)
    with pytest.raises(scan.ScanError, match="output limit"):
        runner.run(
            "synthetic-bounded-tool",
            [
                sys.executable,
                "-c",
                "import os,sys;sys.stderr.write(os.environ['TEST_PRIVATE_DIAGNOSTIC'])",
            ],
            context,
        )
    record = scan.load_json((output / "commands.json").read_bytes())[0]
    assert record["error"] == "command output limit exceeded"
    assert "SECRET" not in (output / "commands.json").read_text()


def test_nested_unsupported_tool_schema_remains_overall_error(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    for path in scan.INPUTS:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}")
    (root / scan.INPUTS[-1]).write_text('{"schema_version":1,"exceptions":[]}')
    output = scan.output_directory(root, tmp_path / "artifacts")
    lock, report = npm_fixture()
    report["vulnerabilities"]["example"] = None
    monkeypatch.setattr(scan, "scan_python", lambda runner: [])
    monkeypatch.setattr(
        scan,
        "scan_npm",
        lambda runner: scan.parse_npm(report, scan.npm_inventory(lock), result(1)),
    )
    monkeypatch.setattr(scan, "scan_rust", lambda runner, tool: [])
    monkeypatch.setattr(
        scan.Runner, "check", lambda *args: {"stdout": b"a" * 40, "exit": 0}
    )
    assert scan.scan(root, output) == 2
    summary = scan.load_json((output / "summary.json").read_bytes())
    assert (
        summary["status"] == "scan_error" and summary["errors"][0]["ecosystem"] == "npm"
    )
