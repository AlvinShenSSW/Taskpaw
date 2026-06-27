"""Smoke tests — the minimal, cross-platform safety net the review gates build on.

These do NOT import the modules (importing taskpaw.py would pull in tkinter / a
GUI backend and run module-level setup). Instead they byte-compile each source
file, which catches syntax/indentation regressions on every platform and in CI
without needing a display server or the optional `tray` extras.

Real behavioural unit tests (event-id monotonicity, config round-trip, folder
stability, etc.) should be added per-issue as the V3 work lands.
"""

from __future__ import annotations

import py_compile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# The three V2 entry points. Kept explicit (not a glob) so a new top-level
# script must be added here deliberately.
SOURCE_FILES = [
    "taskpaw.py",
    "taskpaw_hub.py",
    "macsubs.py",
]


@pytest.mark.parametrize("rel_path", SOURCE_FILES)
def test_source_byte_compiles(rel_path: str) -> None:
    """Each top-level module must byte-compile (syntax gate)."""
    src = REPO_ROOT / rel_path
    assert src.is_file(), f"expected source file missing: {rel_path}"
    # doraise=True turns a compile failure into PyCompileError -> test failure.
    py_compile.compile(str(src), doraise=True)


def test_source_files_present() -> None:
    """Guard against an accidental rename/delete of a tracked entry point."""
    missing = [p for p in SOURCE_FILES if not (REPO_ROOT / p).is_file()]
    assert not missing, f"missing tracked source files: {missing}"
