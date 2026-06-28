#!/usr/bin/env python3
"""Build the V3 desktop bundle: PyInstaller backend sidecar + Tauri app (#40/#41).

Run from the repo root (with the build + v3 deps installed):

    uv sync --extra build --extra v3
    uv run python scripts/build.py

Steps:
  1. PyInstaller → a single `taskpaw-backend` executable (agent|hub by arg).
  2. Copy it to src-tauri/binaries/taskpaw-backend-<target-triple>[.exe] — the
     Tauri `externalBin` sidecar (resolved next to the app at runtime, main.rs).
  3. `tauri build` → the platform installer (.dmg/.app on macOS, .msi/.exe on
     Windows) under src-tauri/target/release/bundle/.

`--skip-tauri` stops after step 2 (useful where the Tauri CLI/toolchain isn't
present — e.g. quick sidecar-only checks).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_TAURI = ROOT / "taskpaw_v3" / "src-tauri"
SPEC = ROOT / "taskpaw_v3" / "packaging" / "taskpaw-backend.spec"
EXE_EXT = ".exe" if os.name == "nt" else ""


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(map(str, cmd)), flush=True)
    subprocess.run(cmd, check=True, **kw)


def target_triple() -> str:
    out = subprocess.run(["rustc", "-vV"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("host:"):
            return line.split(":", 1)[1].strip()
    raise SystemExit("could not determine rust target triple (is rustc installed?)")


def build_backend() -> Path:
    """PyInstaller → build/backend/taskpaw-backend[.exe]."""
    dist = ROOT / "build" / "backend"
    run([sys.executable, "-m", "PyInstaller", str(SPEC),
         "--distpath", str(dist), "--workpath", str(ROOT / "build" / "pyi"), "-y"], cwd=ROOT)
    built = dist / f"taskpaw-backend{EXE_EXT}"
    if not built.exists():
        raise SystemExit(f"PyInstaller did not produce {built}")
    return built


def place_sidecar(built: Path) -> Path:
    """Copy the backend to the Tauri externalBin path with the target-triple suffix."""
    triple = target_triple()
    bin_dir = SRC_TAURI / "binaries"
    bin_dir.mkdir(parents=True, exist_ok=True)
    sidecar = bin_dir / f"taskpaw-backend-{triple}{EXE_EXT}"
    shutil.copy2(built, sidecar)
    os.chmod(sidecar, 0o755)
    print(f"sidecar -> {sidecar}", flush=True)
    return sidecar


def build_tauri() -> None:
    # `install` (not `ci`): the UI package-lock.json is gitignored, matching the
    # existing frontend CI job.
    run(["npm", "--prefix", str(ROOT / "taskpaw_v3" / "ui"), "install"], cwd=ROOT)
    # npx fetches the Tauri CLI v2 if not present; beforeBuildCommand builds the UI.
    run(["npx", "--yes", "@tauri-apps/cli@^2", "build"], cwd=SRC_TAURI)
    print("bundle -> " + str(SRC_TAURI / "target" / "release" / "bundle"), flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the V3 desktop bundle.")
    ap.add_argument("--skip-tauri", action="store_true",
                    help="stop after building + placing the backend sidecar")
    args = ap.parse_args(argv)

    place_sidecar(build_backend())
    if args.skip_tauri:
        print("skipped tauri build (--skip-tauri)")
        return 0
    build_tauri()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
