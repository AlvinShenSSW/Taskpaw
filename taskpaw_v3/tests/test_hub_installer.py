"""Exercise the shipped installation entry without installed apps or services."""

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

INSTALLER = Path(__file__).resolve().parents[2] / "scripts/Install Hub.command"


def test_install_entry_is_a_standalone_executable_shell_command():
    content = INSTALLER.read_bytes()
    assert content.startswith(b"#!/bin/sh\n")
    if os.name != "nt":  # Windows does not preserve POSIX executable mode bits.
        assert INSTALLER.stat().st_mode & 0o100


class InstallerFixture:
    def __init__(self, root):
        self.home = root / "home with spaces"
        self.home.mkdir()
        self.system_apps = root / "system Applications"
        self.system_apps.mkdir()
        self.bin = root / "fixture bin"
        self.bin.mkdir()
        self.log = root / "calls.log"
        self.release = root / "release backend"
        self.command = root / "Install Hub.command"
        content = INSTALLER.read_bytes()
        source = b'"/Applications/TaskPaw Hub.app"'
        assert content.count(source) == 1
        # Relocate only the copied fixture's fixed global app path. Production
        # has no test environment override, and no real Applications access.
        destination = f'"{self.system_apps}/TaskPaw Hub.app"'.encode()
        self.command.write_bytes(content.replace(source, destination))
        self.env = {
            "HOME": str(self.home),
            "PATH": str(self.bin),
            "LC_ALL": "C",
            "TEST_LOG": str(self.log),
            "TEST_INSTALL_EXIT": "0",
            "TEST_OPEN_EXIT": "0",
            "TEST_XATTR_EXIT": "0",
        }
        # #357: clearing the installed app's quarantine is recorded, not real
        self._executable(
            self.bin / "xattr",
            """#!/bin/sh
printf 'xattr\n' >> "$TEST_LOG"
printf 'arg:%s\n' "$@" >> "$TEST_LOG"
if [ "$TEST_XATTR_EXIT" -ne 0 ]; then
    printf 'fixture-xattr-failure\n' >&2
fi
exit "$TEST_XATTR_EXIT"
""",
        )
        self._executable(
            self.bin / "open",
            """#!/bin/sh
printf 'open\n' >> "$TEST_LOG"
printf 'arg:%s\n' "$@" >> "$TEST_LOG"
printf 'open-done\n' >> "$TEST_LOG"
exit "$TEST_OPEN_EXIT"
""",
        )

    @staticmethod
    def _executable(path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o700)

    def add_app(self, *, user=False):
        base = self.home / "Applications" if user else self.system_apps
        app = base / "TaskPaw Hub.app"
        self._executable(
            app / "Contents/MacOS/taskpaw-backend",
            """#!/bin/sh
printf 'backend:%s\n' "$0" >> "$TEST_LOG"
printf 'arg:%s\n' "$@" >> "$TEST_LOG"
if [ -n "${TEST_RELEASE:-}" ]; then
    while [ ! -f "$TEST_RELEASE" ]; do /bin/sleep 0.01; done
fi
printf 'backend-done\n' >> "$TEST_LOG"
if [ "$TEST_INSTALL_EXIT" -ne 0 ]; then
    printf 'fixture-install-failure\n' >&2
fi
exit "$TEST_INSTALL_EXIT"
""",
        )
        return app

    def run(self, *args):
        return subprocess.run(
            ["/bin/sh", str(self.command), *args],
            env=self.env,
            cwd=self.command.parent,
            input="",
            capture_output=True,
            text=True,
            timeout=5,
        )

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    @staticmethod
    def expected(app):
        return [
            "xattr",
            "arg:-dr",
            "arg:com.apple.quarantine",
            f"arg:{app}",
            f"backend:{app}/Contents/MacOS/taskpaw-backend",
            "arg:hub-service",
            "arg:install",
            "backend-done",
            "open",
            f"arg:{app}",
            "open-done",
        ]


@pytest.fixture
def installer(tmp_path):
    if os.name == "nt" or not Path("/bin/sh").is_file():
        pytest.skip("shell execution requires POSIX /bin/sh")
    return InstallerFixture(tmp_path)


def test_missing_app_fails_with_install_location_and_no_launch(installer):
    result = installer.run()
    assert result.returncode != 0
    assert "Applications" in result.stdout + result.stderr
    assert installer.calls() == []


def test_nonexecutable_backend_is_not_launched(installer):
    app = installer.add_app()
    (app / "Contents/MacOS/taskpaw-backend").chmod(0o600)
    result = installer.run()
    assert result.returncode != 0
    assert "Applications" in result.stdout + result.stderr
    assert installer.calls() == []


def test_install_failure_preserves_status_and_does_not_open(installer):
    app = installer.add_app()
    installer.env["TEST_INSTALL_EXIT"] = "37"
    result = installer.run()
    assert result.returncode == 37
    assert "fixture-install-failure" in result.stdout + result.stderr
    assert installer.calls() == installer.expected(app)[:8]


@pytest.mark.parametrize("user", [False, True], ids=["system-app", "user-app"])
def test_install_then_open_exact_app_with_spaces(installer, user):
    app = installer.add_app(user=user)
    result = installer.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert installer.calls() == installer.expected(app)


def test_system_app_is_preferred_when_both_locations_exist(installer):
    app = installer.add_app()
    installer.add_app(user=True)
    result = installer.run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert installer.calls() == installer.expected(app)


def test_repeated_invocation_delegates_each_install_to_manager(installer):
    app = installer.add_app(user=True)
    for _ in range(2):
        result = installer.run()
        assert result.returncode == 0, result.stdout + result.stderr
    assert installer.calls() == installer.expected(app) * 2


def test_open_waits_for_backend_to_complete(installer):
    app = installer.add_app()
    installer.env["TEST_RELEASE"] = str(installer.release)
    process = subprocess.Popen(
        ["/bin/sh", str(installer.command)],
        cwd=installer.command.parent,
        env=installer.env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 3
        while not installer.calls() and time.monotonic() < deadline:
            if process.poll() is not None:
                break
            time.sleep(0.01)
        assert installer.calls(), "installer never invoked the backend"
        assert process.poll() is None, "installer did not wait for the backend"
        assert "open" not in installer.calls()
    finally:
        installer.release.touch()
        try:
            stdout, stderr = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
            raise
    assert process.returncode == 0, stdout + stderr
    assert installer.calls() == installer.expected(app)


def test_action_arguments_cannot_turn_installer_into_service_control(installer):
    installer.add_app()
    result = installer.run("stop")
    assert result.returncode != 0
    assert installer.calls() == []


def test_open_failure_is_reported_without_retrying_install(installer):
    app = installer.add_app()
    installer.env["TEST_OPEN_EXIT"] = "19"
    result = installer.run()
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "TaskPaw Hub.app" in output
    assert any(word in output.lower() for word in ("open", "打开", "启动"))
    assert installer.calls() == installer.expected(app)


def test_quarantine_is_cleared_on_the_exact_app_before_any_launch(installer):
    # #357: one Gatekeeper approval (this command), not three
    app = installer.add_app(user=True)
    installer.add_app()  # the system app wins; only it is touched
    result = installer.run()
    assert result.returncode == 0, result.stdout + result.stderr
    calls = installer.calls()
    assert calls[:4] == [
        "xattr",
        "arg:-dr",
        "arg:com.apple.quarantine",
        f"arg:{installer.system_apps}/TaskPaw Hub.app",
    ]
    assert f"arg:{app}" not in calls


def test_quarantine_failure_is_reported_and_install_continues(installer):
    app = installer.add_app()
    installer.env["TEST_XATTR_EXIT"] = "1"
    result = installer.run()
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "fixture-xattr-failure" in output
    assert "隐私与安全性" in output  # where macOS will still ask
    assert installer.calls() == installer.expected(app)
