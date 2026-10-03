#!/usr/bin/env python3
"""Read-only known-advisory scans. Tooling entry point requires Python 3.12.

No package remediation, app import, build script, or native exception suppression.
The independent crates.io checker is required because cargo-audit 0.22.2 JSON
cannot prove its native index/yank pass completed.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import shutil
import signal
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Importable by the project's Python 3.10 test suite.
    tomllib = None

ROOT = Path(__file__).resolve().parents[1]
PIP_AUDIT_VERSION = "2.10.1"
CARGO_AUDIT_VERSION = "0.22.2"
RUSTSEC_URL = "https://github.com/RustSec/advisory-db.git"
REGISTRY_SOURCE = "registry+https://github.com/rust-lang/crates.io-index"
SPARSE_SOURCE = "sparse+https://index.crates.io/"
INDEX_URL = "https://index.crates.io/"
SCAN_TIMEOUT = 300
STDOUT_LIMIT = 16 * 1024 * 1024
STDERR_LIMIT = 1024 * 1024
INDEX_LIMIT = 8 * 1024 * 1024
CONFIG_LIMIT = 64 * 1024
INDEX_TOTAL_LIMIT = 256 * 1024 * 1024
LINE_LIMIT = 1024 * 1024
INPUTS = (
    "pyproject.toml",
    "uv.lock",
    "taskpaw_v3/ui/package.json",
    "taskpaw_v3/ui/package-lock.json",
    "taskpaw_v3/src-tauri/Cargo.toml",
    "taskpaw_v3/src-tauri/Cargo.lock",
    "docs/security/dependency-exceptions.json",
)
CATEGORIES = ["unmaintained", "unsound", "notice"]
ID_RE = re.compile(
    r"(?:GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}|RUSTSEC-\d{4}-\d{4}|CVE-\d{4}-\d{4,}|PYSEC-\d{4}-\d+)"
)


class ScanError(Exception):
    """A configuration, execution, schema or coverage error; never waivable."""

    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result


def require(condition, message):
    if not condition:
        raise ScanError(message)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def unique_object(pairs):
    obj = {}
    for key, value in pairs:
        require(key not in obj, "duplicate JSON key")
        obj[key] = value
    return obj


def load_json(data):
    try:
        return json.loads(data, object_pairs_hook=unique_object)
    except (ValueError, UnicodeError) as exc:
        raise ScanError("invalid JSON") from exc


def load_toml(path):
    require(tomllib is not None, "scanner execution requires Python 3.12")
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError, OSError) as exc:
        raise ScanError("invalid or unreadable TOML input") from exc


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def atomic_text(path, text):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def canonical_name(name):
    require(
        isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name),
        "invalid Python package name",
    )
    return re.sub(r"[-_.]+", "-", name).lower()


def safe_url(url, hosts=None):
    require(isinstance(url, str), "invalid source URL")
    parsed = urllib.parse.urlsplit(url)
    require(
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and parsed.username is None
        and parsed.password is None
        and not parsed.fragment,
        "nonpublic or credential-bearing source URL",
    )
    if hosts is not None:
        require(
            parsed.hostname in hosts and parsed.port in (None, 443),
            "unexpected public source host",
        )
    return url


def executable(name):
    found = shutil.which(name)
    require(found is not None, f"required executable unavailable: {name}")
    return str(Path(found).absolute())


def child_environment(context):
    # Do not read or forward user configuration, tokens, proxy or Git injection.
    env = {
        key: os.environ[key]
        for key in ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "PATHEXT")
        if key in os.environ
    }
    env.update(
        {
            "CARGO_HOME": str(context / "cargo-home"),
            "UV_CACHE_DIR": str(context / "uv-cache"),
            "UV_TOOL_DIR": str(context / "uv-tools"),
            "UV_PYTHON_INSTALL_DIR": str(context / "python"),
            "UV_NO_CONFIG": "1",
            "RUSTUP_TOOLCHAIN": "1.96.0",
            "GIT_CONFIG_GLOBAL": str(context / "empty-git-config"),
            "GIT_CONFIG_SYSTEM": str(context / "empty-git-config"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_COUNT": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CEILING_DIRECTORIES": str(context),
            "XDG_CONFIG_HOME": str(context / "xdg"),
            "TMPDIR": str(context),
            "TEMP": str(context),
            "TMP": str(context),
            "NPM_CONFIG_USERCONFIG": str(context / "empty-npmrc"),
            "NPM_CONFIG_GLOBALCONFIG": str(context / "empty-global-npmrc"),
            "NPM_CONFIG_CACHE": str(context / "npm-cache"),
            "NPM_CONFIG_REGISTRY": "https://registry.npmjs.org/",
            "NPM_CONFIG_IGNORE_SCRIPTS": "true",
            "NPM_CONFIG_UPDATE_NOTIFIER": "false",
            "PIP_CONFIG_FILE": os.devnull,
            "PIP_INDEX_URL": "https://pypi.org/simple",
            "PYTHONNOUSERSITE": "1",
            "LC_ALL": "C.UTF-8" if sys.platform != "darwin" else "en_US.UTF-8",
        }
    )
    return env


def create_context(path):
    for name in ("cargo-home", "xdg", "cwd", "npm"):
        (path / name).mkdir()
    for name in ("empty-git-config", "empty-npmrc", "empty-global-npmrc"):
        (path / name).write_text("", encoding="utf-8")
    return child_environment(path)


def windows_job():
    """An unnamed job owns only our subprocess tree; no breakaway is permitted.

    Win32 Job Objects / JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE:
    https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
    """
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64),
            ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD),
            ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t),
            ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits),
            ("io", ctypes.c_uint64 * 6),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process_memory", ctypes.c_size_t),
            ("peak_job_memory", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    for name, args, result in [
        ("CreateJobObjectW", [ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
        (
            "SetInformationJobObject",
            [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD],
            wintypes.BOOL,
        ),
        ("AssignProcessToJobObject", [wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
        ("TerminateJobObject", [wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
        ("CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
    ]:
        function = getattr(kernel, name)
        function.argtypes, function.restype = args, result
    handle = kernel.CreateJobObjectW(None, None)
    require(bool(handle), "cannot create owned process job")
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel.SetInformationJobObject(
        handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        kernel.CloseHandle(handle)
        raise ScanError("cannot set owned process job limits")
    return kernel, handle


def close_job(job):
    if job is not None:
        kernel, handle = job
        require(bool(kernel.CloseHandle(handle)), "cannot close owned process job")


def terminate_owned(proc):
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # The owned process/group already exited.
    else:
        job = getattr(proc, "_dependency_scan_job", None)
        if job is not None:
            kernel, handle = job
            require(
                bool(kernel.TerminateJobObject(handle, 1)),
                "cannot terminate owned process job",
            )
        elif proc.poll() is None:
            proc.kill()
    proc.wait(timeout=10)


def run_command(argv, cwd, env, timeout=SCAN_TIMEOUT):
    """Drain bounded pipes concurrently; hard deadline kills only our process group."""
    started = time.monotonic()
    job = windows_job()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            start_new_session=os.name == "posix",
        )
    except OSError as exc:
        close_job(job)
        raise ScanError(f"could not start {Path(argv[0]).name}") from exc
    if job is not None:
        kernel, handle = job
        if not kernel.AssignProcessToJobObject(handle, int(proc._handle)):
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)
            close_job(job)
            raise ScanError("cannot assign owned process job")
        proc._dependency_scan_job = job
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()

    def drain(stream, buf, limit):
        try:
            while chunk := stream.read(65536):
                if len(buf) + len(chunk) > limit:
                    overflow.set()
                    return
                buf.extend(chunk)
        finally:
            stream.close()

    threads = [
        threading.Thread(target=drain, args=(stream, buf, limit), daemon=True)
        for stream, buf, limit in zip(
            (proc.stdout, proc.stderr), buffers, (STDOUT_LIMIT, STDERR_LIMIT)
        )
    ]
    for thread in threads:
        thread.start()
    failed = None
    while proc.poll() is None or any(thread.is_alive() for thread in threads):
        if overflow.is_set():
            failed = "command output limit exceeded"
            break
        if time.monotonic() - started >= timeout:
            failed = "command deadline exceeded"
            break
        time.sleep(0.02)
    # Also reap descendants after a successful root exit; a child may have
    # redirected its pipes and otherwise remain invisible to the drain loop.
    terminate_owned(proc)
    for thread in threads:
        thread.join(timeout=1)
    close_job(job)
    require(
        not any(thread.is_alive() for thread in threads), "command pipe did not close"
    )
    result = {
        "exit": proc.returncode,
        "stdout": bytes(buffers[0]),
        "stderr": bytes(buffers[1]),
        "seconds": round(time.monotonic() - started, 3),
    }
    if overflow.is_set() or failed:
        raise ScanError(
            "command output limit exceeded" if overflow.is_set() else failed, result
        )
    return result


def result_json(result):
    require(result["exit"] in (0, 1), "unexpected audit exit code")
    report = load_json(result["stdout"])
    require(
        isinstance(report, dict) and "error" not in report,
        "audit error or unsupported report",
    )
    return report


def check_exit(result, has_findings):
    require(result["exit"] == (1 if has_findings else 0), "audit exit/result mismatch")


def python_inventory(lock):
    require(
        lock.get("version") == 1 and isinstance(lock.get("package"), list),
        "unsupported uv lock schema",
    )
    packages = {}
    roots = []
    for item in lock["package"]:
        name, version, source = (
            canonical_name(item["name"]),
            item["version"],
            item.get("source"),
        )
        require(
            isinstance(version, str) and re.fullmatch(r"[A-Za-z0-9.+!-]+", version),
            "invalid Python locked version",
        )
        if source == {"virtual": "."} and name == "taskpaw":
            roots.append(item)
            continue
        require(
            source == {"registry": "https://pypi.org/simple"},
            "unsupported Python locked source",
        )
        key = (name, version)
        require(key not in packages, "duplicate Python locked identity")
        packages[key] = item
    require(
        len(roots) == 1 and packages, "missing explicit virtual Python root/inventory"
    )
    labels = {key: set() for key in packages}
    by_name = {}
    for key in packages:
        by_name.setdefault(key[0], []).append(key)

    def resolve(edge):
        keys = by_name.get(canonical_name(edge["name"]), [])
        if "version" in edge:
            keys = [key for key in keys if key[1] == edge["version"]]
        require(bool(keys), "unresolved Python locked graph edge")
        return keys

    def visit(edges, profile, seen):
        for edge in edges:
            for key in resolve(edge):
                labels[key].add(profile)
                if key not in seen:
                    seen.add(key)
                    visit(packages[key].get("dependencies", []), profile, seen)

    root = roots[0]
    visit(root.get("dependencies", []), "base-runtime-candidate", set())
    for group, edges in root.get("optional-dependencies", {}).items():
        visit(
            edges,
            "build-tooling"
            if group == "build"
            else f"optional-{group}-runtime-candidate",
            set(),
        )
    for edges in root.get("dev-dependencies", {}).values():
        visit(edges, "development", set())
    for item in packages.values():
        for edge in item.get("dependencies", []):
            resolve(edge)
    return [
        {
            "name": key[0],
            "version": key[1],
            "source": "https://pypi.org/simple",
            "profiles": sorted(labels[key]) or ["unknown/potential"],
            "markers": [
                edge.get("marker")
                for edge in packages[key].get("dependencies", [])
                if edge.get("marker")
            ],
        }
        for key in sorted(packages)
    ]


def python_batches(inventory):
    batches = []
    for item in inventory:
        for batch in batches:
            if all(other["name"] != item["name"] for other in batch):
                batch.append(item)
                break
        else:
            batches.append([item])
    return batches


def finding(
    ecosystem,
    package,
    version,
    advisory_id,
    classification="vulnerability",
    aliases=(),
    **extra,
):
    require(
        ID_RE.fullmatch(advisory_id) is not None or classification == "yanked",
        "unsupported advisory identity",
    )
    require(
        isinstance(aliases, list | tuple)
        and all(isinstance(x, str) and ID_RE.fullmatch(x) for x in aliases),
        "invalid advisory aliases",
    )
    return {
        "ecosystem": ecosystem,
        "package": package,
        "version": version,
        "advisory_id": advisory_id,
        "aliases": sorted(set(aliases) - {advisory_id}),
        "classification": classification,
        "exposure": "dependency graph/source candidate; packaged contents and exploitability unverified",
        **extra,
    }


def parse_python(report, expected, result):
    require(
        isinstance(report.get("dependencies"), list)
        and isinstance(report.get("fixes"), list),
        "unsupported pip-audit JSON schema",
    )
    required = {(item["name"], item["version"]) for item in expected}
    seen, findings = set(), []
    for dep in report["dependencies"]:
        require(
            isinstance(dep, dict) and "skip_reason" not in dep and "error" not in dep,
            "pip-audit skipped/failed identity",
        )
        key = (canonical_name(dep["name"]), dep["version"])
        require(
            key in required and key not in seen and isinstance(dep.get("vulns"), list),
            "Python audited identity mismatch",
        )
        seen.add(key)
        for vuln in dep["vulns"]:
            require(
                isinstance(vuln, dict) and isinstance(vuln.get("fix_versions"), list),
                "invalid Python advisory schema",
            )
            findings.append(
                finding(
                    "python",
                    *key,
                    vuln["id"],
                    aliases=vuln.get("aliases", []),
                    fix_versions=vuln["fix_versions"],
                    source_url="https://pypi.org/project/" + key[0] + "/",
                    profiles=next(
                        item["profiles"]
                        for item in expected
                        if (item["name"], item["version"]) == key
                    ),
                )
            )
    require(seen == required, "incomplete Python advisory coverage")
    check_exit(result, bool(findings))
    return findings


def npm_inventory(lock):
    require(
        lock.get("lockfileVersion") == 3
        and isinstance(lock.get("packages"), dict)
        and "" in lock["packages"],
        "unsupported npm lock schema",
    )
    inventory = []
    for path, item in lock["packages"].items():
        if path == "":
            continue
        require(
            isinstance(path, str)
            and re.fullmatch(
                r"(?:node_modules/(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+/)*node_modules/(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+",
                path,
            ),
            "invalid npm locked path",
        )
        require(
            isinstance(item.get("version"), str) and not item.get("link"),
            "unsupported npm locked identity",
        )
        safe_url(item.get("resolved"), {"registry.npmjs.org"})
        require(isinstance(item.get("integrity"), str), "npm locked integrity missing")
        name = path.rsplit("node_modules/", 1)[1]
        profiles = ["development"] if item.get("dev") else ["production-candidate"]
        if item.get("optional") or item.get("devOptional"):
            profiles.append("optional/platform")
        inventory.append(
            {
                "path": path,
                "name": name,
                "version": item["version"],
                "profiles": profiles,
                "platforms": {key: item[key] for key in ("os", "cpu") if key in item},
            }
        )
    require(bool(inventory), "empty npm inventory")
    paths = {item["path"] for item in inventory}
    for path, item in lock["packages"].items():
        for dep in item.get("dependencies", {}):
            parent = path
            while True:
                candidate = (parent + "/" if parent else "") + "node_modules/" + dep
                if candidate in paths:
                    break
                if not parent:
                    raise ScanError("unresolved npm locked graph edge")
                parent = (
                    parent.rsplit("/node_modules/", 1)[0]
                    if "/node_modules/" in parent
                    else ""
                )
    return inventory


def parse_npm(report, inventory, result, production=False):
    require(
        report.get("auditReportVersion") == 2
        and isinstance(report.get("vulnerabilities"), dict),
        "unsupported npm audit schema",
    )
    metadata = report.get("metadata", {})
    require(
        metadata.get("dependencies", {}).get("total") == len(inventory),
        "npm audit inventory count mismatch",
    )
    vulnerabilities = report["vulnerabilities"]
    require(
        metadata.get("vulnerabilities", {}).get("total") == len(vulnerabilities),
        "npm audit finding count mismatch",
    )
    paths = {item["path"]: item for item in inventory}
    findings = []

    def advisories(name, ancestors):
        require(
            name in vulnerabilities and name not in ancestors,
            "unresolved/cyclic npm advisory reference",
        )
        value = vulnerabilities[name]
        require(
            isinstance(value.get("via"), list) and value.get("name") == name,
            "invalid npm vulnerability schema",
        )
        out = []
        for via in value["via"]:
            if isinstance(via, str):
                out.extend(advisories(via, ancestors | {name}))
            else:
                require(isinstance(via, dict), "invalid npm advisory")
                url = safe_url(via.get("url"), {"github.com"})
                advisory_id = url.rsplit("/", 1)[-1]
                require(
                    ID_RE.fullmatch(advisory_id) is not None,
                    "unsupported npm advisory URL",
                )
                out.append((advisory_id, url))
        require(bool(out), "npm affected node has no actionable advisory")
        return out

    for name, value in vulnerabilities.items():
        require(
            isinstance(value.get("nodes"), list) and value["nodes"],
            "npm affected node paths missing",
        )
        for path in value["nodes"]:
            require(
                path in paths and paths[path]["name"] == name,
                "npm affected identity not in lock",
            )
            item = paths[path]
            require(
                not production or "production-candidate" in item["profiles"],
                "dev node in npm production report",
            )
            for advisory_id, url in advisories(name, set()):
                findings.append(
                    finding(
                        "npm",
                        name,
                        item["version"],
                        advisory_id,
                        source_url=url,
                        paths=[path],
                        profiles=item["profiles"],
                        fix_available=value.get("fixAvailable"),
                    )
                )
    check_exit(result, bool(vulnerabilities))
    return findings


def rust_inventory(lock):
    require(
        lock.get("version") in (3, 4) and isinstance(lock.get("package"), list),
        "unsupported Cargo lock schema",
    )
    inventory, roots = [], []
    seen = set()
    for package in lock["package"]:
        if "source" not in package:
            require(
                package.get("name") == "taskpaw",
                "unsupported nonregistry Cargo package",
            )
            roots.append(package)
            continue
        name, version, source, checksum = (
            package.get(key) for key in ("name", "version", "source", "checksum")
        )
        require(
            source in (REGISTRY_SOURCE, SPARSE_SOURCE),
            "unsupported Cargo registry/git/path source",
        )
        require(
            isinstance(name, str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name),
            "invalid Cargo crate name",
        )
        require(
            isinstance(version, str)
            and re.fullmatch(
                r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][A-Za-z0-9.+-]+)?", version
            ),
            "invalid Cargo version",
        )
        require(
            isinstance(checksum, str) and re.fullmatch(r"[a-f0-9]{64}", checksum),
            "invalid Cargo checksum",
        )
        key = (source, name, version, checksum)
        require(key not in seen, "duplicate Cargo identity")
        seen.add(key)
        inventory.append(dict(zip(("source", "name", "version", "checksum"), key)))
    require(len(roots) == 1 and inventory, "missing Cargo root/inventory")
    return sorted(inventory, key=lambda item: (item["name"], item["version"]))


def registry_identity(item):
    return tuple(item[key] for key in ("source", "name", "version", "checksum"))


def index_path(name):
    require(
        isinstance(name, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", name),
        "invalid index crate name",
    )
    lower = name.lower()
    if len(lower) < 3:
        return f"{len(lower)}/{lower}"
    if len(lower) == 3:
        return f"3/{lower[0]}/{lower}"
    return f"{lower[:2]}/{lower[2:4]}/{lower}"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ScanError("registry redirect rejected")


def fetch_index(url, limit, budget=None):
    require(url.startswith(INDEX_URL), "unexpected index URL")
    safe_url(url, {"index.crates.io"})
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    request = urllib.request.Request(
        url,
        headers={
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            "User-Agent": "TaskPaw-dependency-scan/1",
        },
    )
    start = time.monotonic()
    try:
        with opener.open(request, timeout=10) as response:
            require(
                response.status == 200 and response.geturl() == url,
                "index HTTP status/source mismatch",
            )
            require(
                response.headers.get("Content-Encoding", "identity").lower()
                in ("identity", ""),
                "compressed index response rejected",
            )
            expected_length = response.headers.get("Content-Length")
            require(
                expected_length is None
                or (expected_length.isdigit() and int(expected_length) <= limit),
                "index response size invalid",
            )
            chunks, count = [], 0
            while chunk := response.read(65536):
                count += len(chunk)
                require(count <= limit, "index response size limit exceeded")
                require(
                    time.monotonic() - start <= 15, "index request deadline exceeded"
                )
                if budget is not None:
                    budget.consume(len(chunk))
                chunks.append(chunk)
            require(time.monotonic() - start <= 15, "index request deadline exceeded")
            require(
                expected_length is None or count == int(expected_length),
                "truncated index response",
            )
            data = b"".join(chunks)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise ScanError("public index request failed") from exc
    return data, {
        "url": url,
        "status": 200,
        "bytes": len(data),
        "sha256": sha256(data),
        "fetched_utc": utc_now(),
    }


def parse_index(data, required):
    require(bool(data) and len(data) <= INDEX_LIMIT, "empty/oversized index response")
    records = {}
    name = required[0]["name"]
    for line in data.splitlines():
        if not line.strip():
            continue
        require(len(line) <= LINE_LIMIT, "index line size limit exceeded")
        record = load_json(line)
        require(
            isinstance(record, dict)
            and record.get("v", 1) in (1, 2)
            and type(record.get("v", 1)) is int,
            "unsupported index schema",
        )
        require(
            record.get("name") == name
            and isinstance(record.get("vers"), str)
            and isinstance(record.get("cksum"), str)
            and re.fullmatch(r"[a-f0-9]{64}", record["cksum"])
            and type(record.get("yanked")) is bool,
            "invalid index record identity/yank schema",
        )
        version = record["vers"]
        require(version not in records, "duplicate index version")
        records[version] = record
    covered = []
    for item in required:
        require(item["version"] in records, "locked version missing from index")
        record = records[item["version"]]
        require(record["cksum"] == item["checksum"], "locked/index checksum mismatch")
        covered.append({**item, "yanked": record["yanked"]})
    return covered


class IndexBudget:
    """Shared bound for accepted registry response bytes, including in-flight reads."""

    def __init__(self):
        self.bytes = 0
        self.lock = threading.Lock()

    def consume(self, count):
        with self.lock:
            require(
                self.bytes + count <= INDEX_TOTAL_LIMIT,
                "aggregate index response limit exceeded",
            )
            self.bytes += count


def registry_coverage(inventory, fetch=fetch_index):
    budget = IndexBudget()
    get = (
        (lambda url, limit: fetch_index(url, limit, budget))
        if fetch is fetch_index
        else fetch
    )
    config, config_proof = get(INDEX_URL + "config.json", CONFIG_LIMIT)
    value = load_json(config)
    require(
        isinstance(value, dict) and value.get("auth-required", False) is False,
        "index authentication policy invalid",
    )
    safe_url(value.get("dl"), {"crates.io", "static.crates.io"})
    if "api" in value:
        require(value["api"] == "https://crates.io", "unexpected index API source")
    groups = {}
    for item in inventory:
        groups.setdefault(item["name"], []).append(item)
    coverage, requests, total = [], [config_proof], len(config)
    require(total <= INDEX_TOTAL_LIMIT, "aggregate index response limit exceeded")
    remaining = iter(groups.items())
    # Keep only eight futures in flight; never queue/buffer the entire registry.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        pending = {}

        def submit_next():
            entry = next(remaining, None)
            if entry is not None:
                name, items = entry
                pending[pool.submit(get, INDEX_URL + index_path(name), INDEX_LIMIT)] = (
                    items
                )

        for _ in range(8):
            submit_next()
        try:
            while pending:
                done, _ = concurrent.futures.wait(
                    pending, return_when=concurrent.futures.FIRST_COMPLETED
                )
                for future in done:
                    items = pending.pop(future)
                    data, proof = future.result()
                    total += len(data)
                    require(
                        total <= INDEX_TOTAL_LIMIT,
                        "aggregate index response limit exceeded",
                    )
                    coverage.extend(parse_index(data, items))
                    requests.append(proof)
                    submit_next()
        finally:
            for future in pending:
                future.cancel()
    expected = {registry_identity(item) for item in inventory}
    actual = {registry_identity(item) for item in coverage}
    require(
        expected == actual and len(coverage) == len(expected),
        "incomplete registry identity coverage",
    )
    identity_hash = sha256(json.dumps(sorted(expected)).encode())
    return {
        "schema_version": 1,
        "complete": True,
        "expected_count": len(expected),
        "covered_count": len(coverage),
        "expected_set_sha256": identity_hash,
        "covered_set_sha256": identity_hash,
        "identities": sorted(
            coverage, key=lambda item: (item["name"], item["version"])
        ),
        "requests": requests,
        "config_sha256": config_proof["sha256"],
        "native_cargo_yank_success_certified": False,
        "limits": {
            "concurrency": 8,
            "worker_seconds": SCAN_TIMEOUT,
            "socket_seconds": 10,
            "request_seconds": 15,
            "file_bytes": INDEX_LIMIT,
            "aggregate_bytes": INDEX_TOTAL_LIMIT,
            "line_bytes": LINE_LIMIT,
        },
    }


def validate_coverage(coverage, inventory):
    require(
        isinstance(coverage, dict)
        and coverage.get("schema_version") == 1
        and coverage.get("complete") is True,
        "registry coverage unavailable/incomplete",
    )
    expected = {registry_identity(item) for item in inventory}
    items = coverage.get("identities")
    require(
        isinstance(items, list)
        and all(type(item.get("yanked")) is bool for item in items),
        "invalid registry coverage results",
    )
    require(
        len(items) == len(expected)
        and {registry_identity(item) for item in items} == expected
        and coverage.get("expected_count") == len(expected)
        and coverage.get("covered_count") == len(expected),
        "partial registry identity coverage",
    )
    expected_hash = sha256(json.dumps(sorted(expected)).encode())
    require(
        coverage.get("expected_set_sha256") == expected_hash
        and coverage.get("covered_set_sha256") == expected_hash,
        "registry coverage set hash mismatch",
    )
    proofs = coverage.get("requests")
    require(
        isinstance(proofs, list)
        and len(proofs) == len({item["name"] for item in inventory}) + 1,
        "registry request proofs missing",
    )
    urls = {proof.get("url") for proof in proofs}
    require(
        urls
        == {INDEX_URL + "config.json"}
        | {INDEX_URL + index_path(item["name"]) for item in inventory},
        "registry proof sources incomplete",
    )
    for proof in proofs:
        require(
            proof.get("status") == 200
            and isinstance(proof.get("bytes"), int)
            and proof["bytes"] > 0
            and re.fullmatch(r"[a-f0-9]{64}", proof.get("sha256", ""))
            and isinstance(proof.get("fetched_utc"), str),
            "invalid registry proof",
        )
    return [
        finding(
            "rust",
            item["name"],
            item["version"],
            f"YANKED:{item['name']}@{item['version']}",
            "yanked",
            source_url=INDEX_URL + index_path(item["name"]),
            checksum=item["checksum"],
        )
        for item in items
        if item["yanked"]
    ]


def audit_policy(db):
    return {
        "advisories": {"ignore": [], "informational_warnings": CATEGORIES},
        "database": {
            "path": str(db),
            "url": RUSTSEC_URL,
            "fetch": True,
            "stale": False,
        },
        "output": {
            "deny": ["warnings", "unmaintained", "unsound", "yanked"],
            "format": "json",
            "quiet": False,
            "show_tree": False,
        },
        "target": {},
        "yanked": {"enabled": True, "update_index": True},
    }


def write_policy(path, db):
    expected = audit_policy(db)
    lines = []
    for section, values in expected.items():
        lines.append(f"[{section}]")
        for key, value in values.items():
            lines.append(f"{key} = {json.dumps(value)}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")
    validate_policy(path, db)
    return sha256(path.read_bytes())


def validate_policy(path, db):
    require(
        path.is_file()
        and not path.is_symlink()
        and load_toml(path) == audit_policy(db),
        "owned cargo-audit policy drift",
    )


def validate_rust_settings(report):
    settings = report.get("settings")
    require(
        isinstance(settings, dict)
        and set(settings)
        == {"ignore", "severity", "target_arch", "target_os", "informational_warnings"},
        "unsupported cargo-audit settings schema",
    )
    require(
        settings["ignore"] == []
        and settings["severity"] is None
        and settings["target_arch"] == []
        and settings["target_os"] == [],
        "cargo-audit suppression/filter policy detected",
    )
    require(
        isinstance(settings["informational_warnings"], list)
        and len(settings["informational_warnings"]) == 3
        and set(settings["informational_warnings"]) == set(CATEGORIES),
        "cargo-audit informational policy drift",
    )


def parse_rust(report, inventory, result, coverage):
    validate_rust_settings(report)
    require(
        report.get("lockfile", {}).get("dependency-count") == len(inventory) + 1,
        "cargo-audit lock count mismatch",
    )
    db = report.get("database", {})
    require(
        isinstance(db.get("advisory-count"), int)
        and db["advisory-count"] > 0
        and re.fullmatch(r"[a-f0-9]{40}", db.get("last-commit", "")),
        "cargo-audit database provenance missing",
    )
    try:
        stamp = datetime.fromisoformat(db["last-updated"])
        require(
            stamp.tzinfo is not None
            and (datetime.now(timezone.utc) - stamp).total_seconds() <= 90 * 86400,
            "stale RustSec database",
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise ScanError("invalid RustSec date") from exc
    vulns, warnings = report.get("vulnerabilities"), report.get("warnings")
    require(
        isinstance(vulns, dict)
        and isinstance(vulns.get("list"), list)
        and type(vulns.get("found")) is bool
        and vulns.get("count") == len(vulns["list"])
        and vulns["found"] == bool(vulns["list"]),
        "unsupported cargo vulnerability schema",
    )
    require(
        isinstance(warnings, dict)
        and set(warnings) <= {"unmaintained", "unsound", "notice", "yanked"}
        and all(isinstance(items, list) for items in warnings.values()),
        "unsupported cargo warning schema",
    )
    locked = {(item["name"], item["version"]): item for item in inventory}
    findings = []
    for category, items in [("vulnerability", vulns["list"]), *warnings.items()]:
        for value in items:
            package = value.get("package", {})
            key = (package.get("name"), package.get("version"))
            require(
                key in locked
                and package.get("source") == locked[key]["source"]
                and package.get("checksum") == locked[key]["checksum"],
                "cargo reported package outside frozen lock",
            )
            if category == "yanked":
                findings.append(
                    finding(
                        "rust",
                        *key,
                        f"YANKED:{key[0]}@{key[1]}",
                        "yanked",
                        source_url=INDEX_URL + index_path(key[0]),
                    )
                )
            else:
                advisory = value.get("advisory", {})
                findings.append(
                    finding(
                        "rust",
                        *key,
                        advisory["id"],
                        category,
                        aliases=advisory.get("aliases", []),
                        source_url="https://rustsec.org/advisories/"
                        + advisory["id"]
                        + ".html",
                        fix_versions=value.get("versions", {}).get("patched", []),
                        profiles=["unknown/potential; includes platform/build graph"],
                    )
                )
    check_exit(result, bool(findings))
    return findings + validate_coverage(coverage, inventory)


def normalize_findings(findings):
    # Alias connected components are ecosystem-scoped, independent of severity/package.
    components = []
    for item in findings:
        ids = {item["advisory_id"], *item["aliases"]}
        overlap = [
            group
            for group in components
            if group[0] == item["ecosystem"] and group[1] & ids
        ]
        for group in overlap:
            ids |= group[1]
            components.remove(group)
        components.append((item["ecosystem"], ids))
    merged = {}
    for item in findings:
        ids = next(
            ids
            for eco, ids in components
            if eco == item["ecosystem"] and item["advisory_id"] in ids
        )
        preferred = "RUSTSEC-" if item["ecosystem"] == "rust" else "GHSA-"
        canonical = next(
            iter(sorted(x for x in ids if x.startswith(preferred))), min(ids)
        )
        key = (
            item["ecosystem"],
            item["package"],
            item["version"],
            item["classification"],
            canonical,
        )
        value = merged.setdefault(
            key,
            {
                **item,
                "advisory_id": canonical,
                "aliases": sorted(ids - {canonical}),
                "paths": [],
                "profiles": [],
            },
        )
        value["paths"] = sorted(set(value["paths"]) | set(item.get("paths", [])))
        value["profiles"] = sorted(
            set(value["profiles"]) | set(item.get("profiles", []))
        )
    return list(merged.values())


def apply_exceptions(document, findings, today):
    fields = {
        "ecosystem",
        "package",
        "versions",
        "advisory_id",
        "aliases",
        "classification",
        "source_url",
        "exposure",
        "reason",
        "owner",
        "reviewed_on",
        "expires_on",
        "removal_conditions",
    }
    require(
        isinstance(document, dict)
        and set(document) == {"schema_version", "exceptions"}
        and type(document["schema_version"]) is int
        and document["schema_version"] == 1
        and isinstance(document["exceptions"], list),
        "invalid exception document schema",
    )
    accepted, seen = [], set()
    for entry in document["exceptions"]:
        require(
            isinstance(entry, dict) and set(entry) == fields,
            "invalid exception entry fields",
        )
        require(
            entry["ecosystem"] in ("python", "npm", "rust")
            and entry["classification"]
            in ("vulnerability", "unsound", "unmaintained", "notice"),
            "unsupported exception classification",
        )
        require(
            isinstance(entry["package"], str)
            and re.fullmatch(
                r"(?:@[A-Za-z0-9_.-]+/)?[A-Za-z0-9_.-]+", entry["package"]
            ),
            "invalid exception package",
        )
        require(
            isinstance(entry["versions"], list)
            and bool(entry["versions"])
            and len(set(entry["versions"])) == len(entry["versions"])
            and all(
                isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9.!+-]+", v)
                for v in entry["versions"]
            ),
            "exception requires exact versions",
        )
        require(
            isinstance(entry["advisory_id"], str)
            and ID_RE.fullmatch(entry["advisory_id"]) is not None,
            "exception requires exact advisory ID",
        )
        require(
            isinstance(entry["aliases"], list)
            and len(set(entry["aliases"])) == len(entry["aliases"])
            and all(
                isinstance(v, str) and ID_RE.fullmatch(v) for v in entry["aliases"]
            ),
            "invalid exception aliases",
        )
        for field in ("exposure", "reason", "owner"):
            require(
                isinstance(entry[field], str) and bool(entry[field].strip()),
                "empty exception responsibility/rationale",
            )
        require(
            isinstance(entry["removal_conditions"], list)
            and entry["removal_conditions"]
            and all(
                isinstance(v, str) and v.strip() for v in entry["removal_conditions"]
            ),
            "missing exception removal conditions",
        )
        safe_url(entry["source_url"])
        require(
            all(
                isinstance(entry[key], str)
                and re.fullmatch(r"\d{4}-\d{2}-\d{2}", entry[key])
                for key in ("reviewed_on", "expires_on")
            ),
            "invalid exception UTC date format",
        )
        try:
            reviewed, expiry = (
                date.fromisoformat(entry[key]) for key in ("reviewed_on", "expires_on")
            )
        except (TypeError, ValueError) as exc:
            raise ScanError("invalid exception UTC date") from exc
        require(
            reviewed <= today < expiry and 0 < (expiry - reviewed).days <= 30,
            "expired/future/unbounded exception",
        )
        matches = [
            item
            for item in findings
            if item["ecosystem"] == entry["ecosystem"]
            and item["package"] == entry["package"]
            and item["version"] in entry["versions"]
            and item["advisory_id"] == entry["advisory_id"]
            and item["classification"] == entry["classification"]
            and set(item["aliases"]) == set(entry["aliases"])
        ]
        require(
            matches and {item["version"] for item in matches} == set(entry["versions"]),
            "stale unused or ambiguous exception",
        )
        for item in matches:
            key = (
                item["ecosystem"],
                item["package"],
                item["version"],
                item["advisory_id"],
                item["classification"],
            )
            require(key not in seen, "duplicate exception match")
            seen.add(key)
            accepted.append(
                {
                    **item,
                    "owner": entry["owner"],
                    "expires_on": entry["expires_on"],
                    "reason": entry["reason"],
                    "removal_conditions": entry["removal_conditions"],
                }
            )
    actionable = [
        item
        for item in findings
        if (
            item["ecosystem"],
            item["package"],
            item["version"],
            item["advisory_id"],
            item["classification"],
        )
        not in seen
    ]
    return actionable, accepted


def overall_status(errors, actionable, accepted):
    if errors:
        return 2, "scan_error"
    if actionable:
        return 1, "actionable_findings"
    return 0, "accepted_exceptions" if accepted else "clean"


def hash_inputs(root):
    try:
        return {name: sha256((root / name).read_bytes()) for name in INPUTS}
    except OSError as exc:
        raise ScanError("required input missing/unreadable") from exc


def output_directory(root, output):
    absolute = output.absolute()
    require(absolute == absolute.resolve(), "symlink output path rejected")
    if absolute.is_relative_to(root):
        require(
            absolute.is_relative_to(root / "build" / "dependency-scan"),
            "output must use dedicated build/dependency-scan directory",
        )
    require(
        not any((root / name).is_relative_to(absolute) for name in INPUTS),
        "output contains scan input",
    )
    marker = absolute / ".taskpaw-dependency-scan"
    if absolute.exists():
        require(
            absolute.is_dir() and (not any(absolute.iterdir()) or marker.is_file()),
            "output is not a dedicated artifact directory",
        )
    absolute.mkdir(parents=True, exist_ok=True)
    marker.write_text("TaskPaw scan artifacts only\n", encoding="utf-8")
    return absolute


def scrub(value, root, context):
    if isinstance(value, str):
        clean = value.replace(str(root), "<repo>").replace(str(context), "<scan-temp>")
        if clean.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", clean):
            return "<tool>/" + Path(clean).name
        return clean
    if isinstance(value, list):
        return [scrub(item, root, context) for item in value]
    if isinstance(value, dict):
        return {key: scrub(item, root, context) for key, item in value.items()}
    return value


class Runner:
    def __init__(self, root, context, env, output):
        self.root, self.context, self.env, self.output = root, context, env, output
        self.commands = []

    def run(self, name, argv, cwd):
        error = None
        try:
            result = run_command(argv, cwd, self.env)
        except ScanError as exc:
            error = exc
            result = exc.result or {
                "exit": None,
                "stdout": b"",
                "stderr": b"",
                "seconds": None,
            }
        record = {
            "name": name,
            "argv": scrub(argv, self.root, self.context),
            "cwd": scrub(str(cwd), self.root, self.context),
            "exit": result["exit"],
            "seconds": result["seconds"],
            "stdout_bytes": len(result["stdout"]),
            "stderr_bytes": len(result["stderr"]),
            "stderr_sha256": sha256(result["stderr"]),
        }
        if error:
            record["error"] = str(error)
        self.commands.append(record)
        atomic_json(self.output / "commands.json", self.commands)
        if error:
            raise error
        return result

    def audit(self, name, argv, cwd):
        result = self.run(name, argv, cwd)
        report = result_json(result)
        atomic_json(
            self.output / f"{name}-raw.json", scrub(report, self.root, self.context)
        )
        return result, report

    def check(self, name, argv, cwd):
        result = self.run(name, argv, cwd)
        require(result["exit"] == 0, f"{name} failed")
        return result


def scan_python(runner):
    inventory = python_inventory(load_toml(runner.root / "uv.lock"))
    atomic_json(runner.output / "python-inventory.json", inventory)
    # uv check runs against an isolated copy, never the project environment.
    directory = runner.context / "python-project"
    directory.mkdir()
    for name in ("pyproject.toml", "uv.lock", "README.md"):
        shutil.copyfile(runner.root / name, directory / name)
    uv = executable("uv")
    runner.check(
        "python-lock-check", [uv, "lock", "--check", "--python", "3.12"], directory
    )
    findings = []
    for number, batch in enumerate(python_batches(inventory)):
        pins = runner.context / f"python-pins-{number}.txt"
        pins.write_text(
            "".join(f"{item['name']}=={item['version']}\n" for item in batch),
            encoding="utf-8",
        )
        result, report = runner.audit(
            f"python-{number}",
            [
                uv,
                "tool",
                "run",
                "--python",
                "3.12",
                "--from",
                f"pip-audit=={PIP_AUDIT_VERSION}",
                "pip-audit",
                "--requirement",
                str(pins),
                "--no-deps",
                "--disable-pip",
                "--strict",
                "--aliases",
                "on",
                "--format",
                "json",
                "--progress-spinner",
                "off",
            ],
            directory,
        )
        findings.extend(parse_python(report, batch, result))
    atomic_json(
        runner.output / "python-coverage.json",
        {
            "complete": True,
            "identities": inventory,
            "identity_count": len(inventory),
            "pip_audit_version": PIP_AUDIT_VERSION,
        },
    )
    return findings


def scan_npm(runner):
    inventory = npm_inventory(
        load_json((runner.root / "taskpaw_v3/ui/package-lock.json").read_bytes())
    )
    atomic_json(runner.output / "npm-inventory.json", inventory)
    directory = runner.context / "npm"
    for name in ("package.json", "package-lock.json"):
        shutil.copyfile(runner.root / "taskpaw_v3/ui" / name, directory / name)
    npm = executable("npm")
    node = (
        runner.check("node-version", [executable("node"), "--version"], directory)[
            "stdout"
        ]
        .decode()
        .strip()
    )
    require(re.fullmatch(r"v22\.\d+\.\d+", node), "dependency scan requires Node 22")
    runner.check("npm-version", [npm, "--version"], directory)
    runner.check(
        "npm-lock-check",
        [
            npm,
            "ci",
            "--ignore-scripts",
            "--no-audit",
            "--registry=https://registry.npmjs.org/",
        ],
        directory,
    )
    findings = []
    for production in (False, True):
        args = [
            npm,
            "audit",
            "--package-lock-only",
            "--ignore-scripts",
            "--json",
            "--audit-level=low",
            "--registry=https://registry.npmjs.org/",
        ]
        if production:
            args.append("--omit=dev")
        result, report = runner.audit(
            "npm-production" if production else "npm-full", args, directory
        )
        findings.extend(parse_npm(report, inventory, result, production))
    return findings


def metadata_roles(report, inventory):
    packages, resolution = report.get("packages"), report.get("resolve")
    require(
        isinstance(packages, list)
        and isinstance(resolution, dict)
        and isinstance(resolution.get("nodes"), list),
        "Cargo metadata resolution missing",
    )
    expected = {(item["name"], item["version"], item["source"]) for item in inventory}
    actual = {
        (item["name"], item["version"], item["source"])
        for item in packages
        if item.get("source") is not None
    }
    require(expected == actual, "Cargo metadata lock inventory mismatch")
    annotations = {
        item["id"]: {
            "name": item["name"],
            "version": item["version"],
            "roles": set(),
            "targets": set(),
        }
        for item in packages
    }
    for node in resolution["nodes"]:
        require(node["id"] in annotations, "unknown Cargo metadata node")
        for dep in node.get("deps", []):
            require(
                dep["pkg"] in annotations and isinstance(dep.get("dep_kinds"), list),
                "unresolved Cargo metadata edge",
            )
            for kind in dep["dep_kinds"]:
                annotations[dep["pkg"]]["roles"].add(kind.get("kind") or "normal")
                if kind.get("target"):
                    annotations[dep["pkg"]]["targets"].add(kind["target"])
    return [
        {
            **item,
            "roles": sorted(item["roles"]) or ["unknown/potential"],
            "targets": sorted(item["targets"]),
            "exposure": "locked dependency-edge role, not a frozen artifact SBOM",
        }
        for item in annotations.values()
    ]


def verify_database(db, report, runner):
    require(db.is_dir() and not db.is_symlink(), "fresh RustSec database missing")
    git_dir = db / ".git"
    require(
        git_dir.is_dir() and not git_dir.is_symlink(),
        "RustSec repository provenance missing",
    )
    origin = (
        runner.check(
            "rustsec-origin",
            [
                executable("git"),
                "-C",
                str(db),
                "config",
                "--local",
                "--get",
                "remote.origin.url",
            ],
            runner.context / "cwd",
        )["stdout"]
        .decode()
        .strip()
    )
    head = (
        runner.check(
            "rustsec-head",
            [executable("git"), "-C", str(db), "rev-parse", "HEAD"],
            runner.context / "cwd",
        )["stdout"]
        .decode()
        .strip()
    )
    require(
        origin == RUSTSEC_URL and head == report["database"]["last-commit"],
        "RustSec source/revision drift",
    )
    lines = (git_dir / "FETCH_HEAD").read_text(encoding="utf-8").splitlines()
    require(
        any(line == head + "\t\t" + RUSTSEC_URL for line in lines),
        "fresh RustSec FETCH_HEAD missing/mismatched",
    )
    return {
        "origin": origin,
        "head": head,
        "fetch_head_sha256": sha256((git_dir / "FETCH_HEAD").read_bytes()),
        "fresh_context": True,
    }


def scan_rust(runner, cargo_audit):
    inventory = rust_inventory(
        load_toml(runner.root / "taskpaw_v3/src-tauri/Cargo.lock")
    )
    atomic_json(runner.output / "rust-inventory.json", inventory)
    cwd = runner.context / "cwd"
    home = Path(runner.env["CARGO_HOME"])
    require(
        home.is_dir() and not home.is_symlink() and home.is_relative_to(runner.context),
        "invalid owned CARGO_HOME",
    )
    lock = cwd / "Cargo.lock"
    shutil.copyfile(runner.root / "taskpaw_v3/src-tauri/Cargo.lock", lock)
    lock_hash = sha256(lock.read_bytes())
    shutil.copyfile(runner.root / "taskpaw_v3/src-tauri/Cargo.toml", cwd / "Cargo.toml")
    (cwd / "src").mkdir()
    (cwd / "src/main.rs").write_text("fn main() {}\n", encoding="utf-8")
    cargo = executable("cargo")
    version = (
        runner.check("cargo-version", [cargo, "--version"], cwd)["stdout"]
        .decode()
        .strip()
    )
    require(
        version.startswith("cargo 1.96.0 "),
        "dependency scan requires Rust/Cargo 1.96.0",
    )
    metadata = runner.check(
        "cargo-metadata", [cargo, "metadata", "--locked", "--format-version", "1"], cwd
    )
    atomic_json(
        runner.output / "rust-roles.json",
        metadata_roles(load_json(metadata["stdout"]), inventory),
    )
    db = runner.context / "rustsec-db"
    require(not db.exists(), "RustSec database must start absent")
    policy = cwd / ".cargo/audit.toml"
    policy.parent.mkdir()
    policy_hash = write_policy(policy, db)
    tool = (
        str(Path(cargo_audit).absolute()) if cargo_audit else executable("cargo-audit")
    )
    require(Path(tool).is_file(), "cargo-audit executable missing")
    version = (
        runner.check("cargo-audit-version", [tool, "--version"], cwd)["stdout"]
        .decode()
        .strip()
    )
    require(
        version == f"cargo-audit {CARGO_AUDIT_VERSION}", "cargo-audit version mismatch"
    )
    # The worker's report is mandatory even if cargo-audit returns valid JSON.
    worker_result = runner.run(
        "rust-registry-worker",
        [sys.executable, str(Path(__file__).resolve()), "--registry-worker", str(lock)],
        cwd,
    )
    require(worker_result["exit"] == 0, "independent registry worker failed")
    coverage = load_json(worker_result["stdout"])
    validate_coverage(coverage, inventory)
    atomic_json(runner.output / "rust-registry-coverage.json", coverage)
    args = [
        tool,
        "audit",
        "--file",
        str(lock),
        "--db",
        str(db),
        "--url",
        RUSTSEC_URL,
        "--json",
        "--deny",
        "warnings",
    ]
    result, report = runner.audit("rust-advisories", args, cwd)
    validate_policy(policy, db)
    require(
        sha256(policy.read_bytes()) == policy_hash
        and sha256(lock.read_bytes()) == lock_hash,
        "owned Rust policy/lock mutated",
    )
    validate_rust_settings(report)
    provenance = verify_database(db, report, runner)
    atomic_json(
        runner.output / "rust-policy-proof.json",
        scrub(
            {
                "policy": audit_policy(db),
                "policy_sha256": policy_hash,
                "cwd": str(cwd),
                "cargo_home": str(home),
                "argv": args,
                "tool_version": version,
                "effective_settings": report["settings"],
                "database": provenance,
            },
            runner.root,
            runner.context,
        ),
    )
    return parse_rust(report, inventory, result, coverage)


def scan(root, output, cargo_audit=None):
    errors, findings, accepted, actionable = [], [], [], []
    initial = hash_inputs(root)
    with tempfile.TemporaryDirectory(prefix="taskpaw-dependency-scan-") as directory:
        context = Path(directory)
        env = create_context(context)
        runner = Runner(root, context, env, output)
        for ecosystem, operation in (
            ("python", lambda: scan_python(runner)),
            ("npm", lambda: scan_npm(runner)),
            ("rust", lambda: scan_rust(runner, cargo_audit)),
        ):
            try:
                findings.extend(operation())
            except (
                ScanError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
                AttributeError,
                subprocess.SubprocessError,
            ) as exc:
                # Fixed messages contain no environment, raw diagnostics or credentials.
                errors.append(
                    {
                        "ecosystem": ecosystem,
                        "reason": str(exc)
                        if isinstance(exc, ScanError)
                        else f"{type(exc).__name__}: scan input/tool schema or IO failed",
                    }
                )
        try:
            require(initial == hash_inputs(root), "scan inputs mutated")
            normalized = normalize_findings(findings)
            actionable, accepted = apply_exceptions(
                load_json((root / INPUTS[-1]).read_bytes()),
                normalized,
                datetime.now(timezone.utc).date(),
            )
        except (ScanError, OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(
                {
                    "ecosystem": "policy",
                    "reason": str(exc)
                    if isinstance(exc, ScanError)
                    else "exception input invalid",
                }
            )
            normalized, actionable = (
                normalize_findings(findings),
                normalize_findings(findings),
            )
        try:
            commit = (
                runner.check(
                    "git-revision",
                    [executable("git"), "-C", str(root), "rev-parse", "HEAD"],
                    context,
                )["stdout"]
                .decode()
                .strip()
            )
            require(
                re.fullmatch(r"[a-f0-9]{40}", commit), "invalid repository revision"
            )
        except (ScanError, OSError, ValueError, subprocess.SubprocessError) as exc:
            commit = "unknown"
            errors.append(
                {
                    "ecosystem": "provenance",
                    "reason": str(exc)
                    if isinstance(exc, ScanError)
                    else "repository revision unavailable",
                }
            )
        code, status = overall_status(errors, actionable, accepted)
        summary = {
            "schema_version": 1,
            "status": status,
            "exit_code": code,
            "scanned_utc": utc_now(),
            "commit": commit,
            "input_sha256": initial,
            "errors": errors,
            "raw_finding_records": len(findings),
            "normalized_package_advisory_records": len(normalized),
            "affected_counts": {
                eco: {
                    "package_names": len(
                        {
                            item["package"]
                            for item in normalized
                            if item["ecosystem"] == eco
                        }
                    ),
                    "package_versions": len(
                        {
                            (item["package"], item["version"])
                            for item in normalized
                            if item["ecosystem"] == eco
                        }
                    ),
                    "installation_paths": len(
                        {
                            path
                            for item in normalized
                            if item["ecosystem"] == eco
                            for path in item.get("paths", [])
                        }
                    ),
                }
                for eco in ("python", "npm", "rust")
            },
            "unique_advisories": {
                eco: len(
                    {
                        item["advisory_id"]
                        for item in normalized
                        if item["ecosystem"] == eco
                    }
                )
                for eco in ("python", "npm", "rust")
            },
            "actionable": actionable,
            "accepted_exceptions": accepted,
            "limits": {
                "command_seconds": SCAN_TIMEOUT,
                "stdout_bytes": STDOUT_LIMIT,
                "stderr_bytes": STDERR_LIMIT,
            },
            "boundary": "known-advisory identity scan; declared graph candidates are not frozen executable/bundle SBOMs; native/manual validation remains separate",
        }
        summary = scrub(summary, root, context)
        atomic_json(output / "summary.json", summary)
        text = f"# Dependency scan: {status}\n\nUTC: {summary['scanned_utc']} · commit: {commit}\n\nExit {code}; {len(actionable)} actionable package/advisory records; {len(accepted)} exact accepted exceptions.\n\n"
        for item in accepted:
            text += f"- Accepted {item['advisory_id']} / {item['package']} {item['version']}: {item['owner']}, expires {item['expires_on']} UTC (exclusive).\n"
        for item in actionable:
            text += f"- Actionable {item['advisory_id']} / {item['package']} {item['version']}: {item['classification']}; update the exact dependency and rerun.\n"
        for item in errors:
            text += f"- Scan error ({item['ecosystem']}): {item['reason']}. No exception can waive this.\n"
        text += "\n" + summary["boundary"] + "\n"
        atomic_text(output / "summary.md", text)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "build/dependency-scan"
    )
    parser.add_argument(
        "--cargo-audit", help="absolute path to installed pinned cargo-audit"
    )
    parser.add_argument("--registry-worker", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        require(
            sys.version_info[:2] == (3, 12), "scanner execution requires Python 3.12"
        )
        if args.registry_worker:
            inventory = rust_inventory(load_toml(args.registry_worker))
            print(json.dumps(registry_coverage(inventory)))
            return 0
        output = output_directory(ROOT, args.output_dir)
        return scan(ROOT, output, args.cargo_audit)
    except (
        ScanError,
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        subprocess.SubprocessError,
    ) as exc:
        reason = str(exc) if isinstance(exc, ScanError) else "scanner input/IO failed"
        print(f"scan_error: {reason}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
