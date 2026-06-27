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
import shutil
import subprocess
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


def _is_bash_script(path: Path) -> bool:
    """Only treat a .sh as bash if its shebang says so (zsh/dash/sh differ)."""
    try:
        first = path.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
    except OSError:
        return False
    return bool(first) and first[0].startswith("#!") and "bash" in first[0]


BASH_SCRIPTS = sorted(
    str(p.relative_to(REPO_ROOT))
    for p in REPO_ROOT.glob("scripts/**/*.sh")
    if _is_bash_script(p)
)


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize("rel_path", BASH_SCRIPTS or ["<none>"])
def test_shell_script_parses(rel_path: str) -> None:
    """Each bash script (by shebang) must parse under `bash -n` (syntax gate)."""
    if rel_path == "<none>":
        pytest.skip("no bash scripts to check")
    script = REPO_ROOT / rel_path
    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{rel_path} failed bash -n:\n{result.stderr}"
