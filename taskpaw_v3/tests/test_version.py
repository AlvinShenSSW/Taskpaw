"""The six version files never drift (#173, AC 8).

`taskpaw_v3.__version__` is the single source of truth for the V3 app version; the
desktop bundle (`tauri.conf.json`, `Cargo.toml`, `Cargo.lock`) and the UI package
(`package.json`, `package-lock.json`) carry their own copies because their build
tools cannot import Python. This test asserts all six agree, so a release bump that
misses one is caught by `uv run pytest` instead of by a mislabelled installer.

Parsed with regexes rather than `tomllib` (3.11+) / a JSON round-trip so the test
runs on the project's minimum 3.10 and never rewrites the files it reads.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from taskpaw_v3 import __version__

V3 = Path(__file__).resolve().parents[1]
SRC_TAURI = V3 / "src-tauri"
UI = V3 / "ui"


def _tauri_conf_version() -> str:
    return str(
        json.loads((SRC_TAURI / "tauri.conf.json").read_text(encoding="utf-8"))[
            "version"
        ]
    )


def _cargo_toml_version() -> str:
    """The `version` of the `[package]` table (the first table in the file)."""
    text = (SRC_TAURI / "Cargo.toml").read_text(encoding="utf-8")
    package = text.split("\n[", 1)[0] if text.startswith("[package]") else ""
    m = re.search(r'^version\s*=\s*"([^"]+)"', package, re.MULTILINE)
    assert m, "no version in Cargo.toml [package]"
    return m.group(1)


def _cargo_lock_version() -> str:
    """The `[[package]]` entry whose `name` is `taskpaw` (the desktop shell crate)."""
    text = (SRC_TAURI / "Cargo.lock").read_text(encoding="utf-8")
    for block in text.split("[[package]]"):
        m = re.search(r'^name\s*=\s*"taskpaw"\s*$', block, re.MULTILINE)
        if m:
            v = re.search(r'^version\s*=\s*"([^"]+)"', block, re.MULTILINE)
            assert v, "the taskpaw entry in Cargo.lock has no version"
            return v.group(1)
    raise AssertionError('no [[package]] named "taskpaw" in Cargo.lock')


def _package_json_version() -> str:
    return str(json.loads((UI / "package.json").read_text(encoding="utf-8"))["version"])


def _package_lock_versions() -> tuple[str, str]:
    """Both copies npm keeps: the top level and the root workspace `packages[""]`."""
    data = json.loads((UI / "package-lock.json").read_text(encoding="utf-8"))
    return str(data["version"]), str(data["packages"][""]["version"])


SOURCES = {
    "src-tauri/tauri.conf.json": _tauri_conf_version,
    "src-tauri/Cargo.toml": _cargo_toml_version,
    "src-tauri/Cargo.lock ([[package]] taskpaw)": _cargo_lock_version,
    "ui/package.json": _package_json_version,
    "ui/package-lock.json (version)": lambda: _package_lock_versions()[0],
    'ui/package-lock.json (packages[""])': lambda: _package_lock_versions()[1],
}


@pytest.mark.parametrize("source", sorted(SOURCES))
def test_version_matches_python_source_of_truth(source):
    assert SOURCES[source]() == __version__, (
        f"{source} disagrees with taskpaw_v3.__version__ ({__version__}) — "
        "bump all six together"
    )


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), __version__
