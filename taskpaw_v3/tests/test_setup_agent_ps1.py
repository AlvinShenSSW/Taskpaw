"""Run only an isolated copy of Windows setup, never installation or a service."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "setup-agent.ps1"
PUBLISH = "import os,sys; os.replace(sys.argv[1],sys.argv[2])"
STAGES = ["sync", "preview", "yaml", "publication", "bootstrap", "launch"]
OLD_MONITORS = b"# existing fixture\nmonitors: []\n"
OLD_AGENT = b"# existing agent fixture; keep unchanged\n"

# Every uv dispatch is a native Python child. Publication executes the exact
# admitted Python code with real os.replace; no mocked publication result.
FAKE_UV = r"""
import json, os, pathlib, subprocess, sys

args = sys.argv[1:]
root = pathlib.Path(os.environ["APPDATA"]).resolve() / "TaskPaw"
known = {
    ("sync", "--extra", "v3"): "sync",
    ("run", "python", "-m", "taskpaw_v3.migrate"): "preview",
    ("run", "python", "-m", "taskpaw_v3.migrate", "--yaml"): "yaml",
    ("run", "python", "-m", "taskpaw_v3.bootstrap", "agent"): "bootstrap",
    ("run", "python", "-m", "taskpaw_v3.agent"): "launch",
}
stage = known.get(tuple(args))
if args[:3] == ["run", "python", "-c"]:
    assert len(args) == 6 and args[3] == "import os,sys; os.replace(sys.argv[1],sys.argv[2])"
    source, target = map(pathlib.Path, args[4:])
    assert source.resolve().parent == target.resolve().parent == root
    assert source.name.endswith(".tmp") and target.name == "monitors.yaml"
    stage = "publication"
assert stage is not None, "Refused unexpected uv invocation"
with open(os.environ["TEST_CALLS"], "a", encoding="utf-8") as out:
    out.write(json.dumps({"stage": stage, "args": args, "pid": os.getpid()}) + "\n")

if stage == "publication":
    result = subprocess.run([sys.executable, "-c", args[3], *args[4:]], capture_output=True, timeout=10)
    pathlib.Path(os.environ["TEST_PUBLICATION"]).write_text(json.dumps({
        "code": result.returncode, "source_exists": source.exists(),
        "target_is_file": target.is_file(), "stderr": result.stderr.decode(errors="replace"),
    }), encoding="utf-8")
    raise SystemExit(result.returncode)

if stage == os.environ.get("TEST_FAIL_STAGE"):
    if stage == "yaml": print("monitors:\n  - partial-failure-fixture", flush=True)
    raise SystemExit(23)
if stage == "preview": print("owned read-only migration preview")
if stage == "yaml": print("monitors: []\n# complete migration fixture")
if stage == "bootstrap":
    target = root / "agent.yaml"
    if not target.exists(): target.write_text("# complete bootstrap fixture\n", encoding="utf-8")
"""

WRAPPER = r"""
$ErrorActionPreference = 'Stop'
$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
function uv {
    & $env:TEST_PYTHON $env:TEST_FAKE_UV @args
    $nativeExit = $LASTEXITCODE
    Set-Variable -Scope Global -Name LASTEXITCODE -Value $nativeExit
}
if ((Get-Command uv).CommandType -ne 'Function') { throw 'Unsafe uv dispatch' }
function Read-Host {
    param([string]$Prompt)
    [IO.File]::AppendAllText($env:TEST_PROMPTS, $Prompt + [Environment]::NewLine)
    return ''
}
if ($env:TEST_PARTIAL_WRITE -eq '1') {
    function Out-File {
        [CmdletBinding()]
        param([Parameter(ValueFromPipeline=$true)]$InputObject,
              [Parameter(Position=0)][string]$FilePath,
              [string]$LiteralPath, [string]$Encoding)
        process {
            if (-not $LiteralPath -or -not $LiteralPath.EndsWith('.tmp')) {
                throw 'Refused partial write outside owned staging file'
            }
            [IO.File]::WriteAllText($LiteralPath, 'PARTIAL STAGING FIXTURE')
            throw 'Owned staging write failed'
        }
    }
}
$held = $null
try {
    if ($env:TEST_HOLD_TARGET -eq '1') {
        $target = Join-Path (Join-Path $env:APPDATA 'TaskPaw') 'monitors.yaml'
        $held = [IO.File]::Open($target, 'Open', 'Read', 'None')
    }
    & $env:TEST_COPIED_SETUP
} finally {
    if ($held) { $held.Dispose() }
}
"""


@pytest.fixture(scope="module")
def powershell() -> str:
    # Windows must actually test the advertised 5.1 entrypoint, even if a custom
    # pwsh override is present. The explicit override is for non-Windows fixtures.
    if sys.platform == "win32":
        executable = shutil.which("powershell")
        assert executable, "Windows PowerShell5.1 is required; do not skip native CI"
    else:
        executable = os.environ.get("TEST_TASKPAW_PWSH")
        if not executable:
            pytest.skip("Windows5.1 required; no private local PowerShell selected")
    assert Path(executable).is_file(), "Selected fixture PowerShell is unavailable"
    version = (
        subprocess.run(
            [
                executable,
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$PSVersionTable.PSVersion.ToString()",
            ],
            capture_output=True,
            timeout=10,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    if sys.platform == "win32":
        assert version.startswith("5.1."), f"Required Windows5.1, got {version}"
    else:
        assert version.startswith("7."), (
            f"Expected private local PowerShell7, got {version}"
        )
    return executable


def run_setup(
    tmp_path: Path,
    powershell: str,
    *,
    existing: bool = False,
    failure: str = "",
    partial_write: bool = False,
    collision: bool = False,
    hold_target: bool = False,
) -> tuple[subprocess.CompletedProcess[str], list[dict], Path, Path]:
    root = tmp_path / "隔离 repo with spaces"
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    copied = scripts / "setup-agent.ps1"
    shutil.copyfile(SCRIPT, copied)
    appdata = tmp_path / "临时 App Data"
    appdata.mkdir(exist_ok=True)
    config = appdata / "TaskPaw"
    if existing:
        config.mkdir(exist_ok=True)
        (config / "monitors.yaml").write_bytes(OLD_MONITORS)
        (config / "agent.yaml").write_bytes(OLD_AGENT)
    if collision:
        destination = config / "monitors.yaml"
        destination.mkdir(parents=True)
        (destination / "sentinel").write_text("owned directory collision")
    fake = root / "fake_uv.py"
    fake.write_text(FAKE_UV, encoding="utf-8")
    wrapper = root / "wrapper.ps1"
    # BOM makes Unicode script content safe in the Windows5.1 parser.
    wrapper.write_text(WRAPPER, encoding="utf-8-sig")
    calls_path, prompts = root / "calls.jsonl", root / "prompts.txt"
    calls_path.write_text("")
    env = os.environ.copy()
    env.update(
        APPDATA=str(appdata),
        TEST_PYTHON=sys.executable,
        TEST_FAKE_UV=str(fake),
        TEST_COPIED_SETUP=str(copied),
        TEST_CALLS=str(calls_path),
        TEST_PROMPTS=str(prompts),
        TEST_PUBLICATION=str(root / "publication.json"),
        TEST_FAIL_STAGE=failure,
        TEST_PARTIAL_WRITE="1" if partial_write else "0",
        TEST_HOLD_TARGET="1" if hold_target else "0",
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
    )
    command = [powershell, "-NoLogo", "-NoProfile", "-NonInteractive"]
    if sys.platform == "win32":
        command += ["-ExecutionPolicy", "Bypass"]
    command += ["-File", str(wrapper)]
    with subprocess.Popen(
        command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ) as child:
        try:
            stdout, stderr = child.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            # Only this owned fixture process tree may be killed, never by name.
            descendants = psutil.Process(child.pid).children(recursive=True)
            for process in descendants:
                process.kill()
            child.kill()
            child.communicate(timeout=5)
            psutil.wait_procs(descendants, timeout=5)
            pytest.fail("Owned PowerShell setup fixture exceeded timeout")
        result = subprocess.CompletedProcess(
            command,
            child.returncode,
            stdout.decode("utf-8-sig", errors="replace"),
            stderr.decode("utf-8-sig", errors="replace"),
        )
    calls = [json.loads(line) for line in calls_path.read_text().splitlines()]
    assert all(not psutil.pid_exists(call["pid"]) for call in calls), (
        "Owned fake uv child leaked"
    )
    return result, calls, config, root


def assert_complete(config: Path) -> None:
    text = (config / "monitors.yaml").read_text(encoding="utf-8-sig")
    assert (
        text.replace("\r\n", "\n").strip()
        == "monitors: []\n# complete migration fixture"
    )
    assert not list(config.glob("*.tmp"))


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("stage", ["sync", "preview", "yaml", "bootstrap", "launch"])
def test_native_failure_stops_at_failed_stage(tmp_path, powershell, existing, stage):
    result, calls, config, root = run_setup(
        tmp_path, powershell, existing=existing, failure=stage
    )
    assert result.returncode != 0, result.stdout + result.stderr
    assert [call["stage"] for call in calls] == STAGES[: STAGES.index(stage) + 1]
    assert "23" in result.stderr
    assert stage in result.stderr.lower()
    assert (root / "prompts.txt").exists() == (stage == "launch")
    assert ("ACTION NEEDED" in result.stdout) == (stage == "launch")
    if stage in {"sync", "preview", "yaml"}:
        if existing:
            assert (config / "monitors.yaml").read_bytes() == OLD_MONITORS
        else:
            assert not (config / "monitors.yaml").exists()
    else:
        assert_complete(config)
    if existing:
        assert (config / "agent.yaml").read_bytes() == OLD_AGENT
    elif stage != "launch":
        assert not (config / "agent.yaml").exists()
    assert not list(config.glob("*.tmp"))


@pytest.mark.parametrize("existing", [False, True])
def test_failed_partial_migration_never_publishes(tmp_path, powershell, existing):
    result, calls, config, root = run_setup(
        tmp_path, powershell, existing=existing, failure="yaml"
    )
    if existing:
        assert (config / "monitors.yaml").read_bytes() == OLD_MONITORS
    else:
        assert not (config / "monitors.yaml").exists()
    assert [call["stage"] for call in calls] == STAGES[:3]
    assert result.returncode != 0
    assert not (root / "prompts.txt").exists()


@pytest.mark.parametrize("existing", [False, True])
def test_partial_staging_write_does_not_publish(tmp_path, powershell, existing):
    result, calls, config, root = run_setup(
        tmp_path, powershell, existing=existing, partial_write=True
    )
    assert result.returncode != 0
    assert "Owned staging write failed" in result.stderr
    assert [call["stage"] for call in calls] == STAGES[:3]
    assert not (root / "prompts.txt").exists()
    assert "ACTION NEEDED" not in result.stdout
    assert not list(config.glob("*.tmp"))
    if existing:
        assert (config / "monitors.yaml").read_bytes() == OLD_MONITORS
        assert (config / "agent.yaml").read_bytes() == OLD_AGENT
    else:
        assert not (config / "monitors.yaml").exists()


def test_real_publication_collision_stops_and_cleans_staging(tmp_path, powershell):
    result, calls, config, root = run_setup(tmp_path, powershell, collision=True)
    assert result.returncode != 0
    assert [call["stage"] for call in calls] == STAGES[:4]
    assert "publication" in result.stderr.lower()
    publication = json.loads((root / "publication.json").read_text())
    assert publication["code"] != 0 and publication["source_exists"]
    assert (
        config / "monitors.yaml" / "sentinel"
    ).read_text() == "owned directory collision"
    assert not list(config.glob("*.tmp"))
    assert not (root / "prompts.txt").exists()
    assert not (config / "agent.yaml").exists()


@pytest.mark.skipif(
    sys.platform != "win32", reason="Actual Windows file-share rejection required"
)
def test_real_windows_lock_preserves_old_destination(tmp_path, powershell):
    result, calls, config, root = run_setup(
        tmp_path, powershell, existing=True, hold_target=True
    )
    assert result.returncode != 0
    assert [call["stage"] for call in calls] == STAGES[:4]
    assert "publication" in result.stderr.lower()
    assert (config / "monitors.yaml").read_bytes() == OLD_MONITORS
    assert (config / "agent.yaml").read_bytes() == OLD_AGENT
    assert not list(config.glob("*.tmp"))
    assert not (root / "prompts.txt").exists()
    assert json.loads((root / "publication.json").read_text())["code"] != 0


@pytest.mark.parametrize("existing", [False, True])
def test_success_preserves_arguments_config_and_repeatability(
    tmp_path, powershell, existing
):
    for run in range(2):
        result, calls, config, root = run_setup(
            tmp_path, powershell, existing=existing and run == 0
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert [call["stage"] for call in calls] == STAGES
        assert_complete(config)
        assert "ACTION NEEDED" in result.stdout
        assert (root / "prompts.txt").exists()
        assert calls[3]["args"][:4] == ["run", "python", "-c", PUBLISH]
        assert json.loads((root / "publication.json").read_text())["code"] == 0
        assert (config / "agent.yaml").read_bytes() == (
            OLD_AGENT if existing else b"# complete bootstrap fixture\n"
        )
