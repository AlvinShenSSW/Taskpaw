# PyInstaller spec for the bundled backend (taskpaw-backend) (#40/#41).
# Build:  pyinstaller taskpaw_v3/packaging/taskpaw-backend.spec  (run from repo root)
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

# SPECPATH is taskpaw_v3/packaging/ → repo root is two levels up.
REPO_ROOT = os.path.abspath(os.path.join(SPECPATH, "..", ".."))

datas, binaries, hiddenimports = [], [], []
# These libs ship data files / dynamically-imported submodules PyInstaller's
# static analysis misses — collect them wholesale.
for pkg in ("uvicorn", "fastapi", "starlette", "pydantic", "pydantic_core",
            "yaml", "psutil", "anyio", "click", "h11"):
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except ImportError as e:
        # Don't silently ship a backend missing a required lib — surface it.
        print(f"WARNING: collect_all({pkg!r}) failed: {e}")
hiddenimports += collect_submodules("taskpaw_v3")

a = Analysis(
    [os.path.join(SPECPATH, "backend_main.py")],
    pathex=[REPO_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter"],   # headless backend — never needs Tk
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="taskpaw-backend",
    console=True,           # a service binary; the Tauri shell owns the window
    debug=False,
    strip=False,
    upx=False,
)
