"""Explicit, per-user macOS Hub installation; the dashboard never calls this.

Only the frozen ``hub-service`` command uses this module. launchd owns the
foreground runtime; installation commands never detach a Python child. All
paths/argv are fixed, and failure to establish a stopped installation preserves
its files. No Hub configuration or database is removed or rolled back.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import logging.handlers
import math
import os
import plistlib
import re
import selectors
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import psutil

from taskpaw_v3 import __version__
from taskpaw_v3.core.control_file import (
    ControlCredentialError,
    _check_darwin_acl,
    _PosixDirectory,
)
from taskpaw_v3.core.state import (
    FileLease,
    StateError,
    atomic_json,
    db_lease_path,
    strict_json,
)

LABEL = "com.taskpaw.hub.v3"
COMMAND_TIMEOUT = 10.0
START_TIMEOUT = 45.0
STOP_TIMEOUT = 45.0
LOG_BYTES = 5 * 1024 * 1024
COMMANDS = frozenset(
    {"install", "start", "stop", "status", "restart", "uninstall", "run"}
)


class ServiceError(ValueError):
    """A fixed, non-secret diagnostic suitable for the installer UI."""


def _command(argv: list[str], *, timeout: float = COMMAND_TIMEOUT) -> int:
    try:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        ).returncode
    except subprocess.TimeoutExpired:
        raise ServiceError("command_timeout") from None
    except OSError:
        raise ServiceError("command_unavailable") from None


def _query(argv: list[str]) -> str:
    """Bound the legacy launchctl evidence read, including its owned child."""
    child = subprocess.Popen(
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    assert child.stdout is not None
    data = bytearray()
    deadline = time.monotonic() + COMMAND_TIMEOUT
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise ServiceError("service_registration_unknown")
                chunk = os.read(child.stdout.fileno(), 4096)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 16384:
                    raise ServiceError("service_registration_unknown")
        if child.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
            raise ServiceError("service_registration_unknown")
        return data.decode("utf-8", errors="strict")
    except (UnicodeError, subprocess.TimeoutExpired):
        raise ServiceError("service_registration_unknown") from None
    finally:
        child.stdout.close()
        if child.poll() is None:
            child.kill()  # Only this just-created query process, never a service PID.
            try:
                child.wait(timeout=COMMAND_TIMEOUT)
            except subprocess.TimeoutExpired:
                raise ServiceError("service_registration_unknown") from None


def _directory(path: Path, *, create: bool = False) -> None:
    if create and not path.exists() and not path.is_symlink():
        _directory(path.parent, create=True)
        path.mkdir(mode=0o700)
    try:
        guard = _PosixDirectory(path)
        guard.close()
    except ControlCredentialError:
        raise ServiceError("unsafe_service_directory") from None


def _file(path: Path, *, executable: bool = False) -> None:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW") | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or info.st_mode & 0o077
                or (executable and not info.st_mode & stat.S_IXUSR)
            ):
                raise ServiceError("unsafe_service_file")
            _check_darwin_acl(fd, directory=False)
        finally:
            os.close(fd)
    except (OSError, ControlCredentialError):
        raise ServiceError("service_file_unavailable") from None


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json(path: Path) -> dict:
    _file(path)
    if path.stat().st_size > 16384:
        raise ServiceError("invalid_service_record")
    try:
        value = strict_json(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError):
        raise ServiceError("invalid_service_record") from None
    if not isinstance(value, dict):
        raise ServiceError("invalid_service_record")
    return value


def _atomic_bytes(path: Path, value: bytes, mode: int = 0o600) -> None:
    # Parent is private and all existing targets have already passed ownership.
    pending = path.with_name(path.name + ".tmp")
    fd = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(value)
            output.flush()
            os.fsync(output.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


class _LogStream(io.TextIOBase):
    """Bound both Python logging and third-party stdout/stderr writes."""

    def __init__(self, handler: logging.handlers.RotatingFileHandler):
        self.handler = handler

    def write(self, text: str) -> int:
        # A single hostile/accidental huge print must not defeat file rotation.
        for offset in range(0, len(text), 8192):
            self.handler.handle(
                logging.LogRecord(
                    "taskpaw.hub",
                    logging.INFO,
                    "",
                    0,
                    text[offset : offset + 8192].rstrip("\n"),
                    (),
                    None,
                )
            )
        return len(text)

    def flush(self) -> None:
        self.handler.flush()


class Manager:
    def __init__(self, home: Path, source: Path):
        self.home = Path(os.path.abspath(home))
        self.source = Path(os.path.abspath(source))
        self.root = self.home / "Library/Application Support/TaskPaw/HubService"
        self.runtime = self.root / "taskpaw-backend"
        self.previous = self.root / "taskpaw-backend.previous"
        self.staged = self.root / "taskpaw-backend.next"
        self.manifest = self.root / "installation.json"
        self.journal = self.root / "upgrade.json"
        self.record = self.root / "runtime.json"
        self.lock = self.root / "operation.lock"
        self.run_lock = self.root / "runtime.lock"
        self.temp = self.root / "tmp"
        self.log = self.root / "hub.log"
        self.plist = self.home / "Library/LaunchAgents" / (LABEL + ".plist")
        self.domain = f"gui/{os.getuid()}"
        self.target = f"{self.domain}/{LABEL}"

    def _launch(self, *args: str) -> int:
        return _command(["/bin/launchctl", *args], timeout=COMMAND_TIMEOUT)

    def _loaded(self) -> bool:
        if self._launch("print", self.domain) != 0:
            raise ServiceError("login_session_unavailable")
        result = self._launch("print", self.target)
        if result == 0:
            return True
        # launchctl's documented error decoder identifies 113 as "Could not
        # find specified service". Other failures never mean absent.
        if result == 113:
            return False
        raise ServiceError("service_registration_unknown")

    def _verify_registration(self) -> None:
        # `print` is explicitly not an API. The documented legacy `list` format
        # is only positive evidence when these complete fields are recognized.
        # Format/domain changes fail closed; no PID is parsed or acted upon.
        if (
            _query(["/bin/launchctl", "manageruid"]).strip() != str(os.getuid())
            or _query(["/bin/launchctl", "managername"]).strip() != "Aqua"
        ):
            raise ServiceError("service_registration_unknown")
        text = _query(["/bin/launchctl", "list", LABEL])
        if not text.startswith("{\n") or not text.endswith("};\n"):
            raise ServiceError("service_registration_unknown")
        for key, expected, optional in (
            ("Label", LABEL, False),
            ("LimitLoadToSessionType", "Aqua", False),
            ("Program", str(self.runtime), True),
        ):
            lines = re.findall(r'^[ \t]*"' + key + r'".*$', text, re.MULTILINE)
            if optional and not lines:
                continue
            if lines != [f'\t"{key}" = "{expected}";']:
                raise ServiceError("service_registration_conflict")
        blocks = re.findall(
            r'^\t"ProgramArguments" = \(\n((?:\t\t"[^"\r\n]*";\n)+)\t\);$',
            text,
            re.MULTILINE,
        )
        expected_args = "".join(
            f'\t\t"{arg}";\n' for arg in (str(self.runtime), "hub-service", "run")
        )
        if len(
            re.findall(r'^[ \t]*"ProgramArguments"', text, re.MULTILINE)
        ) != 1 or blocks != [expected_args]:
            raise ServiceError("service_registration_conflict")

    def _plist_bytes(self) -> bytes:
        return plistlib.dumps(
            {
                "Label": LABEL,
                "AssociatedBundleIdentifiers": "com.taskpaw.app.hub",
                "ProgramArguments": [str(self.runtime), "hub-service", "run"],
                "RunAtLoad": True,
                "KeepAlive": True,
                "ThrottleInterval": 15,
                "ExitTimeOut": 30,
                "Umask": 0o077,
                "WorkingDirectory": str(self.root),
                "EnvironmentVariables": {
                    "HOME": str(self.home),
                    "TMPDIR": str(self.temp),
                },
                # Python/uvicorn logs and readiness stdout are rotated inside run().
                # Native bootloader diagnostics go to launchd's normal logging path.
            },
            sort_keys=True,
        )

    def _manifest(self, digest: str | None) -> dict:
        return {
            "schema": 1,
            "uid": os.getuid(),
            "label": LABEL,
            "sha256": digest,
            "plist_sha256": hashlib.sha256(self._plist_bytes()).hexdigest()
            if digest
            else None,
        }

    def _valid_manifest(self, value: dict) -> None:
        if not isinstance(value, dict):
            raise ServiceError("invalid_installation")
        digest = value.get("sha256")
        if digest is not None and (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise ServiceError("invalid_installation")
        if value != self._manifest(digest):
            raise ServiceError("invalid_installation")

    def _ownership(self) -> dict:
        _directory(self.root)
        value = _json(self.manifest)
        self._valid_manifest(value)
        for path in (
            self.lock,
            self.run_lock,
            self.record,
            self.journal,
            self.log,
            self.log.with_name("hub.log.1"),
        ):
            if path.exists() or path.is_symlink():
                _file(path)
        if self.temp.exists() or self.temp.is_symlink():
            _directory(self.temp)
        if value["sha256"]:
            _file(self.runtime, executable=True)
            _directory(self.plist.parent)
            _file(self.plist)
            if (
                _digest(self.runtime) != value["sha256"]
                or _digest(self.plist) != value["plist_sha256"]
            ):
                raise ServiceError("installation_modified")
        elif any(p.exists() or p.is_symlink() for p in (self.runtime, self.plist)):
            raise ServiceError("unowned_service_files")
        return value

    def _verify_source(self) -> None:
        apps = (
            Path("/Applications/TaskPaw Hub.app"),
            self.home / "Applications/TaskPaw Hub.app",
        )
        app = next(
            (p for p in apps if self.source == p / "Contents/MacOS/taskpaw-backend"),
            None,
        )
        if app is None:
            raise ServiceError("hub_app_required")
        for path in (
            app,
            app / "Contents",
            self.source.parent,
            self.source,
            app / "Contents/Info.plist",
        ):
            if path.is_symlink():
                raise ServiceError("hub_app_required")
        if app.parent.is_symlink():
            raise ServiceError("hub_app_required")
        try:
            info_path = app / "Contents/Info.plist"
            if (
                info_path.stat().st_size > 65536
                or plistlib.loads(info_path.read_bytes()).get("CFBundleIdentifier")
                != "com.taskpaw.app.hub"
            ):
                raise ServiceError("hub_app_required")
        except (OSError, ValueError, AttributeError):
            raise ServiceError("hub_app_required") from None
        if (
            _command(
                ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                timeout=COMMAND_TIMEOUT,
            )
            != 0
        ):
            raise ServiceError("signature_invalid")

    def _processes(self) -> list[tuple[int, float]]:
        found = []
        names = {str(self.runtime), str(self.previous), str(self.staged)}
        for proc in psutil.process_iter(
            ["pid", "exe", "cmdline", "uids", "create_time"]
        ):
            info = proc.info
            if info["uids"] is None or info["uids"].real != os.getuid():
                continue
            argv = info["cmdline"] or []
            if info["exe"] not in names and (not argv or argv[0] not in names):
                continue
            # Exclude the explicit installer itself, including its onefile parent.
            if "hub-service" in argv and "run" not in argv:
                continue
            if info["create_time"] is None:
                raise ServiceError("process_observation_failed")
            found.append((info["pid"], info["create_time"]))
            try:
                found.extend(
                    (child.pid, child.create_time())
                    for child in proc.children(recursive=True)
                )
            except psutil.NoSuchProcess:
                # The leader exited; its original identity remains in the set.
                continue
            except psutil.AccessDenied:
                raise ServiceError("process_observation_failed") from None
        return found

    @staticmethod
    def _alive(identity: tuple[int, float]) -> bool:
        try:
            return psutil.Process(identity[0]).create_time() == identity[1]
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            raise ServiceError("process_observation_failed") from None

    def _runtime_record(self) -> dict | None:
        if not self.record.exists() and not self.record.is_symlink():
            return None
        record = _json(self.record)
        if (
            set(record)
            != {"schema", "pid", "created", "sha256", "state", "db", "ports"}
            or record["schema"] != 1
            or type(record["pid"]) is not int
            or record["pid"] <= 0
            or type(record["created"]) not in (float, int)
            or not math.isfinite(record["created"])
            or record["state"] not in {"starting", "running", "stopped"}
            or not isinstance(record["sha256"], str)
            or len(record["sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in record["sha256"])
            or (
                record["db"] is not None
                and (
                    not isinstance(record["db"], str)
                    or not Path(record["db"]).is_absolute()
                )
            )
            or not isinstance(record["ports"], list)
            or len(record["ports"]) not in (0, 2)
            or any(
                not isinstance(pair, list)
                or len(pair) != 2
                or not isinstance(pair[0], str)
                or type(pair[1]) is not int
                or not 1 <= pair[1] <= 65535
                for pair in record["ports"]
            )
        ):
            raise ServiceError("invalid_runtime_record")
        return record

    def _ready(self) -> bool:
        record = self._runtime_record()
        if (
            not record
            or record.get("state") != "running"
            or record.get("sha256") != _digest(self.runtime)
        ):
            return False
        identity = (record.get("pid"), record.get("created"))
        return identity in self._processes()

    def _resources_released(self, record: dict | None) -> bool:
        from taskpaw_v3.core.net import port_available

        try:
            with FileLease(self.run_lock):
                if record and record.get("db"):
                    db = Path(record["db"])
                    if not db.is_absolute():
                        raise ServiceError("invalid_runtime_record")
                    with FileLease(db_lease_path(db)):
                        if not all(
                            port_available(host, port) for host, port in record["ports"]
                        ):
                            return False
            return True
        except StateError:
            return False

    def _stop(self) -> None:
        record = self._runtime_record()
        identities = self._processes()
        if self._loaded():
            self._verify_registration()
            if self._launch("bootout", self.target) != 0:
                raise ServiceError("stop_failed")
        deadline = time.monotonic() + STOP_TIMEOUT
        while True:
            if (
                not self._processes()
                and not any(self._alive(p) for p in identities)
                and self._resources_released(record)
            ):
                if self._loaded():
                    raise ServiceError("stop_incomplete")
                return
            if time.monotonic() >= deadline:
                raise ServiceError("stop_incomplete")
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    def _start(self) -> None:
        if not self._loaded():
            if self._launch("bootstrap", self.domain, str(self.plist)) != 0:
                raise ServiceError("start_failed")
        self._verify_registration()
        deadline = time.monotonic() + START_TIMEOUT
        while not self._ready():
            if time.monotonic() >= deadline:
                raise ServiceError("start_timeout")
            time.sleep(min(0.1, max(0, deadline - time.monotonic())))

    def _recover(self, *, restart_old: bool = True) -> None:
        """An interrupted upgrade has only two admitted runtimes and manifests."""
        journal = _json(self.journal)
        if (
            set(journal) != {"old", "new", "was_loaded", "phase"}
            or type(journal["was_loaded"]) is not bool
            or journal["phase"] not in ("prepared", "committed")
        ):
            raise ServiceError("invalid_upgrade_record")
        old, new = journal["old"], journal["new"]
        self._valid_manifest(old)
        self._valid_manifest(new)
        current = _json(self.manifest)
        if current not in (old, new):
            raise ServiceError("installation_modified")
        for path in (self.runtime, self.previous, self.staged):
            if path.exists() or path.is_symlink():
                _file(path, executable=True)
                if _digest(path) not in {old["sha256"], new["sha256"]}:
                    raise ServiceError("installation_modified")
        if self.plist.exists() or self.plist.is_symlink():
            _file(self.plist)
            if self.plist.read_bytes() != self._plist_bytes():
                raise ServiceError("installation_modified")
        if journal["phase"] == "committed":
            # Success is durable before deleting the only old executable.
            # Cleanup after this point moves forward and never stops the new job.
            if current != new or _digest(self.runtime) != new["sha256"]:
                raise ServiceError("installation_modified")
            self._ownership()
            if self._loaded():
                self._verify_registration()
            self.staged.unlink(missing_ok=True)
            self.previous.unlink(missing_ok=True)
            self.journal.unlink()
            return
        self._stop()
        if old["sha256"]:
            if self.previous.exists():
                if _digest(self.previous) != old["sha256"]:
                    raise ServiceError("previous_runtime_unavailable")
                os.replace(self.previous, self.runtime)
            elif not self.runtime.exists() or _digest(self.runtime) != old["sha256"]:
                raise ServiceError("previous_runtime_unavailable")
            _atomic_bytes(self.plist, self._plist_bytes())
        else:
            self.runtime.unlink(missing_ok=True)
            self.plist.unlink(missing_ok=True)
        atomic_json(self.manifest, old)
        self.staged.unlink(missing_ok=True)
        self.previous.unlink(missing_ok=True)
        self.journal.unlink()
        if restart_old and old["sha256"] and journal["was_loaded"]:
            self._start()

    def _install(self, current: dict) -> None:
        digest = _digest(self.source)
        if current["sha256"] == digest:
            self._start()
            return
        if (
            self.staged.exists()
            or self.staged.is_symlink()
            or self.previous.exists()
            or self.previous.is_symlink()
        ):
            raise ServiceError("unowned_upgrade_files")
        with self.source.open("rb") as source, self.staged.open("xb") as staged:
            try:
                os.chmod(self.staged, 0o700)
                shutil.copyfileobj(source, staged, 1024 * 1024)
                staged.flush()
                os.fsync(staged.fileno())
            except OSError:
                self.staged.unlink()
                raise
        if (
            _digest(self.staged) != digest
            or _command(
                ["/usr/bin/codesign", "--verify", "--strict", str(self.staged)],
                timeout=COMMAND_TIMEOUT,
            )
            != 0
        ):
            self.staged.unlink()
            raise ServiceError("signature_invalid")
        new = self._manifest(digest)
        was_loaded = self._loaded()
        journal = {
            "old": current,
            "new": new,
            "was_loaded": was_loaded,
            "phase": "prepared",
        }
        atomic_json(self.journal, journal)
        try:
            self._stop()
        except ServiceError:
            self.journal.unlink()
            self.staged.unlink()
            raise
        try:
            if current["sha256"]:
                os.replace(self.runtime, self.previous)
            os.replace(self.staged, self.runtime)
            _atomic_bytes(self.plist, self._plist_bytes())
            atomic_json(self.manifest, new)
            self._start()
        except (ServiceError, OSError):
            try:
                self._recover()
            except (ServiceError, OSError):
                raise ServiceError("upgrade_incomplete") from None
            raise ServiceError("upgrade_failed_rolled_back") from None
        journal["phase"] = "committed"
        atomic_json(self.journal, journal)
        self.previous.unlink(missing_ok=True)
        self.journal.unlink()

    def execute(self, action: str) -> dict:
        if action not in COMMANDS - {"run"}:
            raise ServiceError("invalid_service_command")
        if action == "install":
            self._verify_source()
        if not self.root.exists() and not self.root.is_symlink():
            if action != "install":
                return {"ok": True, "state": "not_installed"}
            if self._loaded() or self.plist.exists() or self.plist.is_symlink():
                raise ServiceError("unowned_registration")
            _directory(self.root, create=True)
            atomic_json(self.manifest, self._manifest(None))
        _directory(self.root)
        if self.lock.exists() or self.lock.is_symlink():
            _file(self.lock)
        try:
            lease = FileLease(self.lock).acquire()
        except StateError:
            raise ServiceError("operation_busy") from None
        try:
            if self.journal.exists() or self.journal.is_symlink():
                if action == "status":
                    return {"ok": False, "state": "upgrade_incomplete"}
                self._recover(restart_old=action not in {"stop", "uninstall"})
            current = self._ownership()
            if not current["sha256"]:
                if self._loaded():
                    raise ServiceError("unowned_registration")
                if action != "install":
                    return {"ok": True, "state": "not_installed"}
            if action == "install":
                _directory(self.temp, create=True)
                _directory(self.plist.parent, create=True)
                self._install(current)
            elif action in {"stop", "restart", "uninstall"}:
                self._stop()
                if action == "uninstall":
                    self.runtime.unlink()
                    self.plist.unlink()
                    self.record.unlink(missing_ok=True)
                    atomic_json(self.manifest, self._manifest(None))
                    return {"ok": True, "state": "not_installed"}
                if action == "restart":
                    self._start()
            elif action == "start":
                self._start()
            loaded = self._loaded()
            if loaded:
                self._verify_registration()
            return {
                "ok": True,
                "state": "running"
                if loaded and self._ready()
                else "starting"
                if loaded
                else "stopped",
                "version": __version__,
            }
        finally:
            lease.close()

    def run(self) -> int:
        from taskpaw_v3.hub.server.service import run_from_config

        current = self._ownership()
        if not current["sha256"] or Path(sys.executable) != self.runtime:
            raise ServiceError("managed_runtime_required")
        _directory(self.temp)
        for path in (self.log, self.log.with_name("hub.log.1")):
            if path.exists() or path.is_symlink():
                _file(path)
        with FileLease(self.run_lock):
            handler = logging.handlers.RotatingFileHandler(
                self.log, maxBytes=LOG_BYTES, backupCount=1, encoding="utf-8"
            )
            os.chmod(self.log, 0o600)
            stream = _LogStream(handler)
            stdout, stderr = sys.stdout, sys.stderr
            logger = logging.getLogger()
            old_handlers, old_level = logger.handlers[:], logger.level
            record = {
                "schema": 1,
                "pid": os.getpid(),
                "created": psutil.Process().create_time(),
                "sha256": current["sha256"],
                "state": "starting",
                "db": None,
                "ports": [],
            }

            def ready(config, db):
                record.update(
                    state="running",
                    db=str(db.resolve()),
                    ports=[
                        [config.bind_host, config.bind_port],
                        [config.control_host, config.control_port],
                    ],
                )
                atomic_json(self.record, record)

            try:
                atomic_json(self.record, record)
                sys.stdout = sys.stderr = stream
                logger.handlers = [logging.StreamHandler(stream)]
                logger.setLevel(logging.INFO)
                return run_from_config(reclaim_stale=False, on_ready=ready)
            finally:
                record["state"] = "stopped"
                try:
                    atomic_json(self.record, record)
                finally:
                    sys.stdout, sys.stderr = stdout, stderr
                    logger.handlers, logger.level = old_handlers, old_level
                    handler.close()


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        print(json.dumps({"ok": False, "error": "packaged_macos_hub_required"}))
        return 2
    if len(args) != 1 or args[0] not in COMMANDS:
        print(json.dumps({"ok": False, "error": "invalid_service_command"}))
        return 2
    try:
        manager = Manager(Path.home(), Path(sys.executable))
        if args[0] == "run":
            return manager.run()
        result = manager.execute(args[0])
        print(json.dumps(result))
        return 0 if result["ok"] else 1
    except (ServiceError, OSError, StateError, psutil.Error) as exc:
        error = (
            str(exc) if isinstance(exc, ServiceError) else "service_operation_failed"
        )
        print(json.dumps({"ok": False, "error": error}))
        return 1
