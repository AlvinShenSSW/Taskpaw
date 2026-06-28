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
    except Exception as e:
        # These are REQUIRED runtime libs — fail the build rather than ship a
        # backend that crashes on import at runtime (Kimi).
        raise SystemExit(f"packaging error: cannot collect required package {pkg!r}: {e}")
hiddenimports += collect_submodules("taskpaw_v3")
# Bundle the example config templates that bootstrap.scaffold() reads on first
# run. collect_submodules grabs .py only, not data files, so without these the
# packaged backend crashes with FileNotFoundError on a no-config launch (#53).
# Use explicit REPO_ROOT paths (not collect_data_files, which resolves the
# package via sys.path at spec-eval time and is fragile if taskpaw_v3 isn't
# importable). The dest "taskpaw_v3/examples" matches EXAMPLES =
# Path(__file__).parent/"examples" inside the onefile extraction tree. Check
# BOTH role templates so a deleted/renamed one fails the build, not a user's
# first run (Codex+Kimi).
_examples_src = os.path.join(REPO_ROOT, "taskpaw_v3", "examples")
for _name in ("agent.example.yaml", "hub.example.yaml"):
    _path = os.path.join(_examples_src, _name)
    if not os.path.exists(_path):
        raise SystemExit(f"packaging error: missing {_path} to bundle (#53)")
    datas.append((_path, "taskpaw_v3/examples"))

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
