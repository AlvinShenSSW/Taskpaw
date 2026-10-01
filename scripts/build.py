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
    stage = kw.pop("mac_stage", None)
    if stage:
        macos_tools().tool(stage, cmd, **kw)
        return
    kw.setdefault(
        "env",
        {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("APPLE_", "TASKPAW_MACOS_", "TASKPAW_PYI_"))
        },
    )
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


def build_backend(plan=None) -> Path:
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
        **(
            {"env": pyinstaller_env(plan), "mac_stage": "pyinstaller", "timeout": 5400}
            if plan
            else {}
        ),
    )
    built = dist / f"taskpaw-backend{EXE_EXT}"
    if not built.exists():
        raise SystemExit(f"PyInstaller did not produce {built}")
    return built


def place_sidecar(built: Path, plan=None) -> Path:
    """Copy the backend to the Tauri externalBin path with the target-triple suffix."""
    triple = plan.target if plan else target_triple()
    bin_dir = SRC_TAURI / "binaries"
    bin_dir.mkdir(parents=True, exist_ok=True)
    sidecar = bin_dir / f"taskpaw-backend-{triple}{EXE_EXT}"
    shutil.copy2(built, sidecar)
    os.chmod(sidecar, 0o755)
    print(f"sidecar -> {sidecar}", flush=True)
    return sidecar


def macos_tools():
    # The direct script's sys.path starts in scripts/, while pytest uses repo root.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from scripts import macos_release

    return macos_release


def pyinstaller_env(plan):
    env = macos_tools().child_env(os.environ)
    env["TASKPAW_PYI_TARGET_ARCH"] = plan.arch
    env["TASKPAW_PYI_ENTITLEMENTS_FILE"] = str(plan.entitlements(ROOT))
    if plan.mode == "formal":
        env["TASKPAW_PYI_CODESIGN_IDENTITY"] = plan.identity
    return env


def bundle_config():
    role = os.environ.get("TASKPAW_BUILD_ROLE", "agent").strip().lower()
    if role not in ("agent", "hub"):
        role = "agent"
    ver = os.environ.get("TASKPAW_BUILD_VERSION", "").strip().lstrip("vV")
    if not ver:
        ver = json.loads((SRC_TAURI / "tauri.conf.json").read_text())["version"]
    return {
        "identifier": f"com.taskpaw.app.{role}",
        "productName": f"TaskPaw {role.capitalize()}",
        "version": ver,
    }


def build_tauri(plan=None, isolation=None) -> None:
    ui = ROOT / "taskpaw_v3" / "ui"
    cfg = bundle_config()
    kwargs = {}
    if sys.platform == "darwin":
        if plan is None or isolation is None:
            raise macos_tools().BuildError("macos_smoke_isolation_required")
        isolation.require()
        kwargs = {
            "env": macos_tools().child_env(os.environ),
            "mac_stage": "tauri",
            "timeout": 5400,
        }
        bundle = plan.bundle_root(ROOT)
        # Only generated artifacts for this role, never supplied user paths.
        for folder in (bundle / "macos", bundle / "dmg"):
            if folder.is_dir():
                for old in folder.glob(cfg["productName"] + "*"):
                    if old.suffix in {".app", ".dmg", ".zip"}:
                        if old.is_dir() and not old.is_symlink():
                            shutil.rmtree(old)
                        else:
                            old.unlink()
        (bundle / "macos-verification.json").unlink(missing_ok=True)
    run(["npm", "--prefix", str(ui), "ci"], cwd=ROOT, **kwargs)
    run(["npm", "--prefix", str(ui), "run", "build"], cwd=ROOT, **kwargs)
    cmd = ["npx", "--yes", TAURI_CLI, "build", "--ci", "--config", json.dumps(cfg)]
    if plan:
        cmd += ["--no-sign", "--bundles", "app", "--target", plan.target]
    else:
        targets = os.environ.get("TASKPAW_BUNDLE_TARGETS", "").strip()
        if targets:
            cmd += ["--bundles", targets]
    run(cmd, cwd=SRC_TAURI, **kwargs)
    if plan:
        macos_tools().finalize(plan, isolation, kwargs["env"], ROOT, cfg)
    else:
        print(
            "bundle -> " + str(SRC_TAURI / "target" / "release" / "bundle"), flush=True
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the V3 desktop bundle.")
    ap.add_argument(
        "--skip-tauri",
        action="store_true",
        help="stop after building + placing the backend sidecar",
    )
    args = ap.parse_args(argv)

    if sys.platform == "darwin":
        mac = macos_tools()
        try:
            plan = mac.normalize(
                os.environ, mac.native_target(os.environ, skip_tauri=args.skip_tauri)
            )
            with mac.build_session(
                plan, os.environ, ROOT, skip_tauri=args.skip_tauri
            ) as isolation:
                mac.preflight(plan, mac.child_env(os.environ), ROOT)
                built = build_backend(plan)
                mac.arches(built, plan, mac.child_env(os.environ))
                mac.signature(
                    built, plan, mac.child_env(os.environ), ROOT, backend=True
                )
                mac.verify_archive(built, plan, mac.child_env(os.environ), ROOT)
                place_sidecar(built, plan)
                if not args.skip_tauri:
                    build_tauri(plan, isolation)
            return 0
        except (mac.BuildError, OSError, ValueError, subprocess.SubprocessError) as exc:
            message = (
                str(exc) if isinstance(exc, mac.BuildError) else "macos_build_failed"
            )
            print(message, file=sys.stderr)
            return 1
    place_sidecar(build_backend())
    if args.skip_tauri:
        print("skipped tauri build (--skip-tauri)")
        return 0
    build_tauri()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
