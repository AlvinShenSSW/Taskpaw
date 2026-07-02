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

Dev note: the Tauri `externalBin` makes ANY cargo build (incl. `cargo tauri dev`
/ `cargo check`) require the sidecar to exist first. Run `python scripts/build.py
--skip-tauri` once on a clean checkout before `cargo tauri dev` (the release
workflow runs this script, so the sidecar is always present before it builds).
The before*Command hooks are intentionally empty (their cwd is ambiguous across
Tauri versions, see #50) — so for dev, start Vite yourself in another terminal:
`npm --prefix taskpaw_v3/ui run dev`, then `cargo tauri dev` from src-tauri.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_TAURI = ROOT / "taskpaw_v3" / "src-tauri"
SPEC = ROOT / "taskpaw_v3" / "packaging" / "taskpaw-backend.spec"
EXE_EXT = ".exe" if os.name == "nt" else ""
# Pinned for reproducible bundles (Kimi).
TAURI_CLI = "@tauri-apps/cli@2.11.3"


def run(cmd: list[str], **kw) -> None:
    print("+", " ".join(map(str, cmd)), flush=True)
    # Resolve the program (npm/npx are .cmd shims on Windows, found via PATHEXT)
    # so subprocess locates them WITHOUT shell=True (constitution §2) (#50).
    prog = shutil.which(cmd[0]) or cmd[0]
    subprocess.run([prog, *cmd[1:]], check=True, **kw)


def target_triple() -> str:
    # Prefer rustc's host triple (authoritative). Fall back to a platform-derived
    # triple so --skip-tauri (sidecar only) works WITHOUT the Rust toolchain (Codex).
    try:
        out = subprocess.run(["rustc", "-vV"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if line.startswith("host:"):
                return line.split(":", 1)[1].strip()
    except FileNotFoundError:
        pass
    import platform

    mach = platform.machine().lower()
    arch = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "arm64": "aarch64",
        "aarch64": "aarch64",
    }.get(mach, mach)
    if sys.platform == "darwin":
        return f"{arch}-apple-darwin"
    if sys.platform == "win32":
        return f"{arch}-pc-windows-msvc"
    return f"{arch}-unknown-linux-gnu"


def build_backend() -> Path:
    """PyInstaller → build/backend/taskpaw-backend[.exe]."""
    dist = ROOT / "build" / "backend"
    run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            str(SPEC),
            "--distpath",
            str(dist),
            "--workpath",
            str(ROOT / "build" / "pyi"),
            "-y",
        ],
        cwd=ROOT,
    )
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
    ui = ROOT / "taskpaw_v3" / "ui"
    # Build the UI HERE with an explicit absolute --prefix (cwd-independent), then
    # drop tauri's beforeBuildCommand — so the release bundle never depends on
    # Tauri's hook working directory (which differs from frontendDist's base; see
    # #50 where the relative-prefix hook broke the build). `ci` = reproducible.
    run(["npm", "--prefix", str(ui), "ci"], cwd=ROOT)
    run(["npm", "--prefix", str(ui), "run", "build"], cwd=ROOT)
    # Role-specific identifier + name so the agent and hub installers don't
    # overwrite each other on one machine (Kimi). Role from TASKPAW_BUILD_ROLE
    # (also baked into the binary via option_env! in main.rs).
    role = os.environ.get("TASKPAW_BUILD_ROLE", "agent").strip().lower()
    if role not in ("agent", "hub"):
        role = "agent"
    cfg = {
        "identifier": f"com.taskpaw.app.{role}",
        "productName": f"TaskPaw {role.capitalize()}",
    }
    # Stamp the release version from the tag (TASKPAW_BUILD_VERSION, leading 'v'
    # stripped) so a v3.1.0 tag doesn't ship "3.0.0" installers (Kimi). Unset
    # (e.g. workflow_dispatch) → keep tauri.conf.json's version.
    ver = os.environ.get("TASKPAW_BUILD_VERSION", "").strip().lstrip("vV")
    if ver:
        cfg["version"] = ver
    overrides = json.dumps(cfg)
    # --ci: never prompt (headless runners would hang). Pin the CLI for
    # reproducible bundles; beforeBuildCommand builds the UI.
    cmd = ["npx", "--yes", TAURI_CLI, "build", "--ci", "--config", overrides]
    # Optional target restriction (#54 bundle smoke): e.g. TASKPAW_BUNDLE_TARGETS=deb
    # builds only a .deb on a Linux PR smoke (no AppImage tooling), without
    # affecting release.yml (which leaves it unset → tauri.conf "all").
    targets = os.environ.get("TASKPAW_BUNDLE_TARGETS", "").strip()
    if targets:
        cmd += ["--bundles", targets]
    run(cmd, cwd=SRC_TAURI)
    print("bundle -> " + str(SRC_TAURI / "target" / "release" / "bundle"), flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the V3 desktop bundle.")
    ap.add_argument(
        "--skip-tauri",
        action="store_true",
        help="stop after building + placing the backend sidecar",
    )
    args = ap.parse_args(argv)

    place_sidecar(build_backend())
    if args.skip_tauri:
        print("skipped tauri build (--skip-tauri)")
        return 0
    build_tauri()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
