"""Smoke tests — the minimal, cross-platform safety net the review gates build on.

These do NOT import the modules (importing taskpaw.py would pull in tkinter / a
GUI backend and run module-level setup). Instead they byte-compile each source
file, which catches syntax/indentation regressions on every platform and in CI
without needing a display server or the optional `tray` extras.

Real behavioural unit tests (event-id monotonicity, config round-trip, folder
stability, etc.) should be added per-issue as the V3 work lands.
"""

from __future__ import annotations

import platform
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


def _resolve_functional_bash() -> str | None:
    """Return a `bash` executable that actually runs a command, else None.

    Guards Linux/macOS runners that somehow lack a working bash, and — crucially —
    returns the *same* resolved path the test then invokes, so the probe and the
    real call can't hit different executables under unusual PATH ordering. (On
    Windows the exe `shutil.which` finds, often Git Bash, and the one
    `subprocess.run(["bash", ...])` launches via CreateProcess,
    `C:\\Windows\\System32\\bash.exe` — the WSL stub — can differ; that case is
    handled by the explicit Windows skip below.)
    """
    exe = shutil.which("bash")
    if exe is None:
        return None
    try:
        ok = subprocess.run(
            [exe, "-c", "exit 0"], capture_output=True, timeout=30
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return None
    return exe if ok else None


# Short-circuit on Windows: the test is skipped there anyway, so don't spawn the
# WSL-launcher bash at import time (avoids the "install a distribution" notice +
# collection latency).
_FUNCTIONAL_BASH = None if platform.system() == "Windows" else _resolve_functional_bash()


# These are POSIX (macOS/Linux) setup scripts — never executed on Windows. A
# Windows runner's `bash` is unreliable here: `subprocess.run(["bash", ...])`
# resolves to the WSL launcher `System32\bash.exe`, which with no distro prints an
# "install a distribution" notice and exits non-zero for every call (and even Git
# Bash mishandles the Windows-path argument). Syntax-check them where a real POSIX
# bash exists; skip on Windows and where bash is non-functional.
@pytest.mark.skipif(
    platform.system() == "Windows" or _FUNCTIONAL_BASH is None,
    reason="POSIX-only scripts; bash -n unreliable on Windows / no functional bash",
)
@pytest.mark.parametrize("rel_path", BASH_SCRIPTS or ["<none>"])
def test_shell_script_parses(rel_path: str) -> None:
    """Each bash script (by shebang) must parse under `bash -n` (syntax gate)."""
    if rel_path == "<none>":
        pytest.skip("no bash scripts to check")
    script = REPO_ROOT / rel_path
    result = subprocess.run(
        [_FUNCTIONAL_BASH, "-n", str(script)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"{rel_path} failed bash -n:\n{result.stderr}"
