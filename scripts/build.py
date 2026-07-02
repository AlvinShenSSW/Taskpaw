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
    # (e.g. workflow_dispatch) → fall back to tauri.conf.json's version. Always set it
    # in cfg so the ad-hoc DMG filename (_adhoc_finalize_macos) matches the app version
    # even when the env var is absent.
    ver = os.environ.get("TASKPAW_BUILD_VERSION", "").strip().lstrip("vV")
    if not ver:
        ver = json.loads((SRC_TAURI / "tauri.conf.json").read_text())["version"]
    cfg["version"] = ver
    # macOS: ad-hoc sign local/unsigned builds so the .app + .dmg aren't rejected as
    # "damaged" on Apple Silicon (an unsigned/inconsistently-signed bundle fails
    # Gatekeeper). ONLY when no release identity is configured — a real
    # APPLE_SIGNING_IDENTITY (release.yml #49) must win so Developer-ID signing +
    # notarization still happen. Kept out of tauri.conf so the release path is
    # untouched.
    adhoc = (
        sys.platform == "darwin"
        and not os.environ.get("APPLE_SIGNING_IDENTITY", "").strip()
    )
    if adhoc:
        cfg["bundle"] = {"macOS": {"signingIdentity": "-"}}
    overrides = json.dumps(cfg)
    # --ci: never prompt (headless runners would hang). Pin the CLI for
    # reproducible bundles; beforeBuildCommand builds the UI.
    cmd = ["npx", "--yes", TAURI_CLI, "build", "--ci", "--config", overrides]
    # Optional target restriction (#54 bundle smoke): e.g. TASKPAW_BUNDLE_TARGETS=deb
    # builds only a .deb on a Linux PR smoke (no AppImage tooling), without
    # affecting release.yml (which leaves it unset → tauri.conf "all").
    targets = os.environ.get("TASKPAW_BUNDLE_TARGETS", "").strip()
    if adhoc:
        # Build the .app ONLY (Tauri deletes the .app right after it makes the DMG, and
        # would ad-hoc-sign the sidecar WITHOUT the library-validation entitlement it
        # needs to load its bundled libpython on another mac). We post-process the .app
        # and build the DMG ourselves (_adhoc_finalize_macos).
        run(cmd + ["--bundles", "app"], cwd=SRC_TAURI)
        _adhoc_finalize_macos(cfg, targets)
    else:
        if targets:
            cmd += ["--bundles", targets]
        run(cmd, cwd=SRC_TAURI)
    print("bundle -> " + str(SRC_TAURI / "target" / "release" / "bundle"), flush=True)


def _adhoc_finalize_macos(cfg: dict, targets: str) -> None:
    """After an ad-hoc `--bundles app` build: re-sign the PyInstaller sidecar with the
    library-validation-disabling entitlement, re-seal the .app, then build the DMG.

    Why: the onefile backend extracts its bundled libpython at runtime and dlopen()s it;
    that dylib's code-signature Team ID differs from the ad-hoc exe, so macOS library
    validation refuses it ("different Team IDs") and the backend never starts on any mac
    but the build host. The entitlement lets the process load its own differently-signed
    libraries. Tauri's own signing can't carry this (it doesn't apply the app
    entitlements to the nested sidecar), so we do it here.
    """
    bundle_dir = SRC_TAURI / "target" / "release" / "bundle"
    macos_dir = bundle_dir / "macos"
    # Select THIS role's app by productName — `--bundles app` doesn't delete the .app,
    # so a prior role's bundle (e.g. "TaskPaw Agent.app") can still sit alongside it and
    # a naive sorted()[0] would grab the wrong one.
    app = macos_dir / f"{cfg['productName']}.app"
    if not app.is_dir():
        raise SystemExit(f"ad-hoc build produced no {app.name} under {macos_dir}")
    entitlements = SRC_TAURI / "macos-adhoc-entitlements.plist"

    sidecars = [p for p in app.rglob("taskpaw-backend*") if p.is_file()]
    if not sidecars:
        raise SystemExit(f"no taskpaw-backend sidecar found inside {app}")
    for side in sidecars:
        # Re-sign the sidecar ad-hoc WITH the entitlement (disable library validation).
        run(
            [
                "codesign",
                "--force",
                "--sign",
                "-",
                "--entitlements",
                str(entitlements),
                "--timestamp=none",
                str(side),
            ]
        )
    # Re-seal the app WITHOUT --deep, so the sidecar's fresh entitlement survives (a
    # deep re-sign would re-sign the sidecar and strip it). Tauri already ad-hoc-signed
    # the other nested code (frameworks/helpers); this just re-computes the outer seal
    # over the now-entitled sidecar.
    run(["codesign", "--force", "--sign", "-", str(app)])
    run(["codesign", "--verify", "--strict", str(app)])

    # Build the DMG ourselves (Tauri would have; we deferred it). UDZO = compressed.
    if targets and "dmg" not in {t.strip() for t in targets.split(",")}:
        return  # caller only wanted the .app (e.g. a smoke build)
    dmg_dir = bundle_dir / "dmg"
    dmg_dir.mkdir(parents=True, exist_ok=True)
    triple = target_triple()
    arch = "aarch64" if triple.startswith("aarch64") else "x64"
    ver = cfg.get("version") or "3.0.0"
    dmg = dmg_dir / f"{cfg['productName']}_{ver}_{arch}.dmg"
    if dmg.exists():
        dmg.unlink()
    # Stage the .app + an /Applications symlink so the DMG is drag-to-install.
    staging = bundle_dir / "_dmg_staging"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    shutil.copytree(app, staging / app.name, symlinks=True)
    os.symlink("/Applications", staging / "Applications")
    run(
        [
            "hdiutil",
            "create",
            "-volname",
            cfg["productName"],
            "-srcfolder",
            str(staging),
            "-ov",
            "-format",
            "UDZO",
            str(dmg),
        ]
    )
    shutil.rmtree(staging)
    print(f"dmg -> {dmg}", flush=True)


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
