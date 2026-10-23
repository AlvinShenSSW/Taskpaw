"""#299 §5: tool, GPU and directory detection — fake trees under tmp_path and
fake runners only. Jasna and WhisperJAV are never executed, no GPU is read,
no network path is touched and this host's disks never decide a test."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from taskpaw_v3.agent.server import detect
from taskpaw_v3.agent.server.admin import MonitorAdmin
from taskpaw_v3.agent.server.app import create_control_app
from taskpaw_v3.core.config import AgentConfig, WorkflowSettings, load_yaml
from taskpaw_v3.monitors.registry import PluginRegistry

TOKEN = "control-token"
GPU_LINE = "NVIDIA GeForce RTX 5070 Ti, 16303, 581.57, 300.00\n"


class Runner:
    """Records every argv; answers ffprobe and nvidia-smi from a table."""

    def __init__(self, answers: dict[str, str | None] | None = None):
        self.calls: list[tuple[list[str], float]] = []
        self.answers = answers or {}

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        name = Path(argv[0]).name
        return self.answers.get(name)


def touch(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    return str(path)


def detector(tmp_path, runner=None, **kw):
    kw.setdefault("platform", "win32")
    kw.setdefault("env", {"APPDATA": str(tmp_path / "appdata")})
    kw.setdefault("home", tmp_path / "home")
    kw.setdefault("which", lambda name: None)
    kw.setdefault("network", lambda path: path.startswith("\\\\"))
    kw.setdefault("drives", lambda: [])
    kw.setdefault("candidates", {"jasna": [], "whisperjav": []})
    kw.setdefault("version_of", lambda path: None)
    return detect.Detector(run=runner or Runner(), **kw)


# ── ffprobe: saved → PATH → bundled; the only tool executed ─────────────
def test_ffprobe_prefers_the_saved_path_then_path_then_bundled(tmp_path):
    saved = touch(tmp_path / "tools" / "ffprobe.exe")
    on_path = touch(tmp_path / "path" / "ffprobe.exe")
    runner = Runner({"ffprobe.exe": "ffprobe version 8.0.1-essentials_build Copyright"})
    d = detector(tmp_path, runner, which=lambda n: on_path if n == "ffprobe" else None)
    found = d.ffprobe(WorkflowSettings(ffprobe_path=saved))
    assert found == {
        "found": True,
        "path": saved,
        "source": "saved",
        "version": "8.0.1",
    }
    assert runner.calls == [([saved, "-version"], 3.0)]
    found = d.ffprobe(WorkflowSettings(ffprobe_path=str(tmp_path / "gone.exe")))
    assert found["source"] == "path" and found["path"] == on_path


def test_ffprobe_falls_back_to_whisperjavs_bundled_folder(tmp_path):
    whisper = tmp_path / "WhisperJAV" / "Scripts" / "whisperjav.exe"
    bundled = touch(tmp_path / "WhisperJAV" / "Library" / "bin" / "ffprobe.exe")
    d = detector(tmp_path)
    found = d.ffprobe(WorkflowSettings(whisperjav_exe_path=str(whisper)))
    assert found["source"] == "bundled" and Path(found["path"]) == Path(bundled)
    assert found["version"] is None  # the fake runner printed nothing


def test_ffprobe_missing(tmp_path):
    runner = Runner()
    assert detector(tmp_path, runner).ffprobe(WorkflowSettings())["found"] is False
    assert runner.calls == []


# ── Jasna: version from the PE resource, never executed ─────────────────
def test_jasna_search_order_and_version_without_running_it(tmp_path):
    saved = touch(tmp_path / "saved" / "jasna.exe")
    monitor = touch(tmp_path / "monitor" / "jasna.exe")
    fixed = touch(tmp_path / "Jasna" / "jasna.exe")
    runner = Runner()
    versions = []
    d = detector(
        tmp_path,
        runner,
        candidates={"jasna": [fixed], "whisperjav": []},
        version_of=lambda path: versions.append(path) or "0.11.3",
    )
    monitors = [{"type_id": "jasna", "config": {"jasna_exe_path": monitor}}]
    assert (
        d.jasna(WorkflowSettings(jasna_exe_path=saved), monitors)["source"] == "saved"
    )
    assert d.jasna(WorkflowSettings(), monitors) == {
        "found": True,
        "path": monitor,
        "source": "monitor",
        "version": "0.11.3",
    }
    assert d.jasna(WorkflowSettings(), [])["source"] == "default"
    assert runner.calls == []  # Jasna is never executed
    assert versions == [saved, monitor, fixed]
    d2 = detector(tmp_path, which=lambda n: monitor if n == "jasna" else None)
    found = d2.jasna(WorkflowSettings(), [])
    assert found["source"] == "path" and found["version"] is None  # 「已找到」


def test_whisperjav_is_only_looked_for(tmp_path):
    fixed = touch(tmp_path / "WhisperJAV" / "Scripts" / "whisperjav.exe")
    runner = Runner()
    d = detector(tmp_path, runner, candidates={"jasna": [], "whisperjav": [fixed]})
    assert d.whisperjav(WorkflowSettings(), []) == {
        "found": True,
        "path": fixed,
        "source": "default",
        "version": None,
    }
    assert runner.calls == []


def test_network_paths_are_never_probed(tmp_path):
    seen = []

    def network(path):
        seen.append(path)
        return path.startswith("\\\\")

    d = detector(tmp_path, network=network)
    unc = r"\\nas\share\jasna.exe"
    assert d.jasna(WorkflowSettings(jasna_exe_path=unc), [])["found"] is False
    assert unc in seen


def test_default_candidates_follow_the_contract():
    env = {"ProgramFiles": r"C:\PF", "LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
    found = detect.default_candidates(env, "win32")
    assert found["jasna"] == [
        r"C:\Jasna\jasna.exe",
        r"C:\PF\Jasna\jasna.exe",
        r"C:\Users\u\AppData\Local\Programs\Jasna\jasna.exe",
    ]
    assert found["whisperjav"] == [
        r"C:\WhisperJAV\Scripts\whisperjav.exe",
        r"C:\Users\u\AppData\Local\Programs\WhisperJAV\Scripts\whisperjav.exe",
    ]
    assert detect.default_candidates(env, "darwin") == {"jasna": [], "whisperjav": []}


# ── Miko ────────────────────────────────────────────────────────────────
def test_miko_complete_partial_off(tmp_path):
    config_dir = tmp_path / "cfg"
    root = config_dir / "engines" / "MDCX304"
    touch(root / "mdcx" / "cmd" / "postprocess_one.py")
    python = touch(root / ".venv" / "Scripts" / "python.exe")
    d = detector(tmp_path)
    assert d.miko(WorkflowSettings(), config_dir) == {
        "status": "partial",
        "root": str(root),
        "python": python,
        "profile": None,
    }
    profile = touch(tmp_path / "appdata" / "com.mdcx.desktop" / "config.json")
    assert d.miko(WorkflowSettings(), config_dir)["status"] == "complete"
    assert d.miko(WorkflowSettings(), config_dir)["profile"] == profile
    assert detector(tmp_path / "none").miko(WorkflowSettings(), None)["status"] == "off"
    mac = detector(tmp_path, platform="darwin", home=tmp_path / "mac")
    touch(
        tmp_path
        / "mac"
        / "Library"
        / "Application Support"
        / "com.mdcx.desktop"
        / "config.json"
    )
    assert mac.miko(WorkflowSettings(), None)["status"] == "partial"


# ── GPU ─────────────────────────────────────────────────────────────────
def test_gpu_reads_name_vram_driver_and_power_limit(tmp_path):
    runner = Runner({"nvidia-smi": GPU_LINE})
    gpu = detector(tmp_path, runner).gpu()
    assert gpu == {
        "name": "NVIDIA GeForce RTX 5070 Ti",
        "vram_mib": 16303,
        "driver": "581.57",
        "power_limit_w": 300,
        "worker_ok": True,
    }
    assert runner.calls == [
        (
            [
                "nvidia-smi",
                "--query-gpu=name,memory.total,driver_version,power.limit",
                "--format=csv,noheader,nounits",
            ],
            5.0,
        )
    ]
    small = Runner({"nvidia-smi": "NVIDIA GeForce RTX 5060, 8151, 581.57, [N/A]\n"})
    gpu = detector(tmp_path, small).gpu()
    assert gpu["worker_ok"] is False and gpu["power_limit_w"] is None
    for out in (None, "", "garbage", "a, b, c, d", "x, 0, 1, 2"):
        assert detector(tmp_path, Runner({"nvidia-smi": out})).gpu() is None


# ── suggestions and directory facts ─────────────────────────────────────
def test_suggestions_pick_the_fixed_drive_with_most_space(tmp_path):
    d = detector(tmp_path, drives=lambda: [("C:\\", 10), ("D:\\", 900), ("E:\\", 50)])
    assert d.suggested() == {
        "inbox": "D:\\TaskpawInbox",
        "data_dir": "D:\\TaskpawWork",
        "staging": "D:\\TaskpawStaging",
    }
    assert detector(tmp_path).suggested() == {
        "inbox": None,
        "data_dir": None,
        "staging": None,
    }
    mac = detector(tmp_path, platform="darwin", home=Path("/Users/u"))
    assert mac.suggested()["inbox"] == str(Path("/Users/u") / "TaskpawInbox")


def test_inspect_directory(tmp_path):
    from taskpaw_v3.agent.server.workflow_settings import Refused

    existing = tmp_path / "work"
    existing.mkdir()
    facts = detect.inspect_directory(str(existing), network=lambda p: False)
    assert facts["exists"] is True and facts["local"] is True
    assert isinstance(facts["free_bytes"], int) and facts["volume"]
    missing = detect.inspect_directory(
        str(tmp_path / "new" / "deeper"), network=lambda p: False
    )
    assert missing["exists"] is False and isinstance(missing["free_bytes"], int)
    for bad in ("", "relative\\x", "\\\\nas\\share", str(tmp_path / ".." / "x"), 3):
        with pytest.raises(Refused):
            detect.inspect_directory(bad)
    remote = detect.inspect_directory(str(existing), network=lambda p: True)
    assert remote == {
        "exists": None,
        "free_bytes": None,
        "volume": remote["volume"],
        "local": False,
    }


# ── the probe runner ────────────────────────────────────────────────────
def test_run_probe_reads_4kb_and_kills_on_timeout(tmp_path):
    out = detect.run_probe([sys.executable, "-c", "print('x' * 10000)"], 20)
    assert out is not None and len(out) == 4096
    assert (
        detect.run_probe([sys.executable, "-c", "import sys; sys.exit(3)"], 20) is None
    )
    assert detect.run_probe([str(tmp_path / "missing.exe")], 1) is None
    slow = [sys.executable, "-c", "import time; time.sleep(30)"]
    assert detect.run_probe(slow, 0.5) is None


def test_run_probe_closes_stdin(tmp_path):
    code = "import sys; print(repr(sys.stdin.read()))"
    assert detect.run_probe([sys.executable, "-c", code], 20).strip() == "''"


# ── the routes ──────────────────────────────────────────────────────────
class World:
    def __init__(self, tmp_path: Path, runner: Runner):
        self.path = tmp_path / "agent.yaml"
        self.path.write_text(
            yaml.safe_dump({"server_id": "s", "machine": "m", "host_metrics": False}),
            encoding="utf-8",
        )
        self.config = load_yaml(AgentConfig, self.path)
        self.admin = MonitorAdmin(self.config, None, PluginRegistry(), self.path)
        self.now = [100.0]
        self.made = 0

        def make():
            self.made += 1
            return detector(tmp_path, runner, drives=lambda: [("D:\\", 5)])

        app = create_control_app(
            self.config,
            admin=self.admin,
            control_token=TOKEN,
            control_active=lambda: True,
        )
        # rebuild the detect routes with fakes (the app registers real ones first)
        app.router.routes = [
            r
            for r in app.router.routes
            if getattr(r, "path", "") != "/control/workflow/detect"
        ]
        detect.register_detect_routes(
            app,
            self.admin,
            tmp_path,
            self.admin.desired_workflow,
            detector=make,
            clock=lambda: self.now[0],
        )
        self.client = TestClient(app, base_url="http://127.0.0.1")
        self.h = {"Authorization": f"Bearer {TOKEN}"}


def test_detect_is_cached_for_60_seconds(tmp_path):
    runner = Runner({"nvidia-smi": GPU_LINE})
    world = World(tmp_path, runner)
    first = world.client.get("/control/workflow/detect", headers=world.h).json()
    assert first["gpu"]["worker_ok"] is True
    assert first["suggested"]["inbox"] == "D:\\TaskpawInbox"
    assert set(first["tools"]) == {"ffprobe", "jasna", "whisperjav", "miko"}
    world.client.get("/control/workflow/detect", headers=world.h)
    assert world.made == 1
    world.client.get("/control/workflow/detect?refresh=1", headers=world.h)
    assert world.made == 2
    world.now[0] += 60
    world.client.get("/control/workflow/detect", headers=world.h)
    assert world.made == 3


def test_the_inspect_route_never_echoes_the_path(tmp_path):
    world = World(tmp_path, Runner())
    res = world.client.post(
        "/control/workflow/settings/directory/inspect",
        json={"path": "\\\\secret-nas\\share"},
        headers=world.h,
    )
    assert res.status_code == 422 and "secret-nas" not in res.text
    res = world.client.post(
        "/control/workflow/settings/directory/inspect",
        json={"path": str(tmp_path)},
        headers=world.h,
    )
    assert res.status_code == 200 and res.json()["exists"] is True


def test_no_probe_inherits_a_console_window(monkeypatch):
    seen = {}

    class Done:
        returncode = 0

        def communicate(self, timeout):
            return b"ok", b""

    def popen(argv, **kwargs):
        seen.update(kwargs)
        return Done()

    monkeypatch.setattr(subprocess, "Popen", popen)
    assert detect.run_probe(["x"], 1) == "ok"
    assert seen["stdin"] is subprocess.DEVNULL
    if sys.platform == "win32":
        assert seen["creationflags"] & 0x08000000
