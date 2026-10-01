"""Build-only native Mac packaging. Never import/unlock signing credentials.

Runtime verification requires a genuinely disposable/exclusive host. Context
flags are assertions by the trusted controller, not OS isolation mechanisms.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import hashlib
import http.client
import json
import math
import os
import platform
import plistlib
import re
import secrets
import selectors
import shlex
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import uuid
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

TARGETS = {"aarch64-apple-darwin": "arm64", "x86_64-apple-darwin": "x86_64"}
FORMAL_KEYS = (
    "APPLE_SIGNING_IDENTITY",
    "APPLE_TEAM_ID",
    "TASKPAW_MACOS_SIGNING_KEYCHAIN",
    "TASKPAW_MACOS_NOTARY_PROFILE",
)
ADHOC_KEYS = {
    "com.apple.security.cs.disable-library-validation",
    "com.apple.security.cs.allow-dyld-environment-variables",
    "com.apple.security.cs.allow-unsigned-executable-memory",
}
MAGICS = {
    b"\xfe\xed\xfa\xce",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xcf",
    b"\xcf\xfa\xed\xfe",
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
}
TAIL_LIMIT = 65536
_cancellation = None


class BuildError(RuntimeError):
    """Only fixed codes / validated field names enter public diagnostics."""


class _Cancellation:
    """Signals only record requests; guarded call sites decide when to raise."""

    def __init__(self):
        self.requested = False

    def checkpoint(self):
        if self.requested:
            raise BuildError("macos_build_cancelled")

    def request(self, signum, frame):
        self.requested = True


@contextlib.contextmanager
def cancellation_scope():
    global _cancellation
    if _cancellation is not None:
        yield _cancellation
        return
    state = _Cancellation()
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}
    installed = []
    try:
        for sig in previous:
            signal.signal(sig, state.request)
            installed.append(sig)
        _cancellation = state
        yield state
    finally:
        for sig in installed:
            signal.signal(sig, previous[sig])
        _cancellation = None
        state.checkpoint()


def cancellation_checkpoint():
    if _cancellation is not None:
        _cancellation.checkpoint()


@contextlib.contextmanager
def owned_child(cmd, *, stage, grace=1, reject_forced=False, **kwargs):
    with cancellation_scope() as cancellation:
        cancellation.checkpoint()
        proc = None
        try:
            # Do not block signals: a blocked mask would also reach exec'd children.
            # The handler records requests until Popen's returned object is owned.
            try:
                proc = subprocess.Popen(cmd, start_new_session=True, **kwargs)
            except OSError:
                raise BuildError(stage + "_unavailable") from None
            cancellation.checkpoint()
            yield proc
        finally:
            if proc is not None:
                try:
                    forced = stop_group(proc, grace=grace)
                finally:
                    for stream in (proc.stdin, proc.stdout, proc.stderr):
                        if stream is not None:
                            stream.close()
                if forced and reject_forced:
                    raise BuildError("macos_smoke_forced_cleanup")
            cancellation.checkpoint()


@dataclass(frozen=True)
class Plan:
    target: str
    mode: str
    identity: str = ""
    team: str = ""
    keychain: str = ""
    profile: str = ""
    bundles: frozenset[str] = frozenset({"app", "dmg"})

    @property
    def arch(self):
        return TARGETS[self.target]

    def bundle_root(self, root: Path):
        return root / "taskpaw_v3/src-tauri/target" / self.target / "release/bundle"

    def entitlements(self, root: Path, backend=True):
        name = (
            "macos-adhoc-entitlements.plist"
            if backend and self.mode == "adhoc"
            else "macos-release-entitlements.plist"
        )
        return root / "taskpaw_v3/src-tauri" / name


def child_env(env: Mapping[str, str], *, runtime=False):
    result = {
        k: v
        for k, v in env.items()
        if not k.startswith(("APPLE_", "TASKPAW_MACOS_", "TASKPAW_PYI_"))
    }
    if runtime:
        result = {
            k: v
            for k, v in result.items()
            if not k.startswith(("TASKPAW_LLM_", "DYLD_", "PYTHON"))
        }
    return result


def normalize(env: Mapping[str, str], target: str) -> Plan:
    values = {key: str(env.get(key, "")).strip() for key in FORMAL_KEYS}
    unsupported = sorted(
        key
        for key, value in env.items()
        if key.startswith("APPLE_") and key not in FORMAL_KEYS and value.strip()
    )
    if unsupported:
        raise BuildError(
            "macos_legacy_credentials_unsupported: " + ",".join(unsupported)
        )
    identity, team, keychain, profile = (values[k] for k in FORMAL_KEYS)
    if identity == "-" and any((team, keychain, profile)):
        raise BuildError("macos_signing_configuration_conflict")
    if identity == "-":
        identity = ""
    supplied = (identity, team, keychain, profile)
    if any(supplied) and not all(supplied):
        missing = [key for key, value in zip(FORMAL_KEYS, supplied) if not value]
        raise BuildError("macos_signing_configuration_incomplete: " + ",".join(missing))
    if all(supplied):
        if (
            not re.fullmatch(r"[0-9A-Fa-f]{40}", identity)
            or not re.fullmatch(r"[A-Z0-9]{10}", team)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", profile)
        ):
            raise BuildError("macos_signing_configuration_invalid")
        path = Path(keychain)
        if (
            not path.is_absolute()
            or ".." in path.parts
            or any(c in keychain for c in "\0\r\n")
        ):
            raise BuildError("macos_signing_configuration_invalid")
    if target not in TARGETS:
        raise BuildError("macos_native_target_invalid")
    raw = env.get("TASKPAW_BUNDLE_TARGETS", "").strip()
    bundles = (
        frozenset(p.strip() for p in raw.split(","))
        if raw
        else frozenset({"app", "dmg"})
    )
    if not bundles or not bundles <= {"app", "dmg"}:
        raise BuildError("macos_bundle_targets_invalid")
    return Plan(
        target,
        "formal" if all(supplied) else "adhoc",
        identity.upper(),
        team,
        keychain,
        profile,
        bundles,
    )


def stop_group(proc, *, grace=10):
    """Only a group created by our own start_new_session Popen is addressed."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        proc.wait(timeout=5)
        return False
    except PermissionError:
        # Darwin may briefly report EPERM while the owned group is tearing down.
        # It is not proof of absence: keep probing and require eventual ESRCH.
        print("macos stage=child_cleanup_recheck", flush=True)
    forced = False
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        proc.poll()
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            proc.wait(timeout=1)
            return forced
        except PermissionError:
            # Retry only until the bounded deadline, never accept EPERM as gone.
            time.sleep(0.05)
            continue
        time.sleep(0.05)
    forced = True
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        forced = False
    except PermissionError:
        raise BuildError("macos_child_cleanup_failed") from None
    proc.wait(timeout=5)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.killpg(proc.pid, 0)
        except ProcessLookupError:
            return forced
        except PermissionError:
            time.sleep(0.05)
            continue  # Still not absent; bounded retry.
        time.sleep(0.05)
    raise BuildError("macos_child_cleanup_failed")


def tool(stage: str, cmd, *, env=None, cwd=None, timeout=60, input_data=None):
    """Drain both pipes, keep bounded tails, never print upstream output/argv."""
    if env is None:
        env = child_env(os.environ)
    print("macos stage=" + stage, flush=True)
    with owned_child(
        cmd,
        stage=stage,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as proc:
        out, err = bytearray(), bytearray()
        with selectors.DefaultSelector() as selector:
            cancellation_checkpoint()
            selector.register(proc.stdout, selectors.EVENT_READ, out)
            cancellation_checkpoint()
            selector.register(proc.stderr, selectors.EVENT_READ, err)
            cancellation_checkpoint()
            if input_data is not None:
                proc.stdin.write(input_data)
                proc.stdin.close()
            deadline = time.monotonic() + timeout
            while selector.get_map():
                cancellation_checkpoint()
                if time.monotonic() >= deadline:
                    raise BuildError(stage + "_timeout")
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        key.data.extend(chunk)
                        del key.data[:-TAIL_LIMIT]
            while proc.poll() is None:
                cancellation_checkpoint()
                if time.monotonic() >= deadline:
                    raise BuildError(stage + "_timeout")
                time.sleep(0.05)
            cancellation_checkpoint()
            rc = proc.returncode
            if rc:
                raise BuildError(stage + "_failed")
            return bytes(out), bytes(err)


def native_target(env, *, skip_tauri=False):
    machine = platform.machine().lower()
    arch = "arm64" if machine in {"arm64", "aarch64"} else machine
    uname = os.uname().machine.lower()
    if arch not in TARGETS.values() or uname != arch:
        raise BuildError("macos_native_architecture_mismatch")
    translated = subprocess.run(
        ["sysctl", "-in", "sysctl.proc_translated"],
        capture_output=True,
        text=True,
        check=False,
        env=child_env(env),
    )
    if translated.returncode == 0 and translated.stdout.strip() == "1":
        raise BuildError("macos_rosetta_unsupported")
    target = env.get("TASKPAW_BUILD_TARGET", "").strip() or next(
        t for t, a in TARGETS.items() if a == arch
    )
    if TARGETS.get(target) != arch:
        raise BuildError("macos_native_target_mismatch")
    try:
        data, _ = tool("rust_host", ["rustc", "-vV"], env=child_env(env))
    except BuildError as exc:
        if not skip_tauri or str(exc) != "rust_host_unavailable":
            raise
    else:
        match = re.search(rb"^host: ([^\r\n]+)$", data, re.MULTILINE)
        if not match or match.group(1).decode() != target:
            raise BuildError("macos_rust_host_mismatch")
    return target


def boot_uuid():
    data, _ = tool("smoke_boot_context", ["sysctl", "-n", "kern.bootsessionuuid"])
    try:
        return str(uuid.UUID(data.decode().strip()))
    except (ValueError, UnicodeError):
        raise BuildError("macos_smoke_isolation_required") from None


def darwin_private_acl(fd):
    """Check effective grants on the same fd, using public Darwin ACL APIs."""
    try:
        libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
        ptr = ctypes.c_void_p
        signatures = {
            "acl_get_fd_np": ([ctypes.c_int, ctypes.c_int], ptr),
            "acl_valid": ([ptr], ctypes.c_int),
            "acl_free": ([ptr], ctypes.c_int),
            "acl_get_entry": ([ptr, ctypes.c_int, ctypes.POINTER(ptr)], ctypes.c_int),
            "acl_get_tag_type": ([ptr, ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
            "acl_get_permset_mask_np": (
                [ptr, ctypes.POINTER(ctypes.c_uint64)],
                ctypes.c_int,
            ),
            "acl_get_flagset_np": ([ptr, ctypes.POINTER(ptr)], ctypes.c_int),
            "acl_get_flag_np": ([ptr, ctypes.c_uint32], ctypes.c_int),
            "acl_get_qualifier": ([ptr], ptr),
            "mbr_uid_to_uuid": ([ctypes.c_uint32, ptr], ctypes.c_int),
        }
        for name, (args, result) in signatures.items():
            function = getattr(libc, name)
            function.argtypes, function.restype = args, result
        ctypes.set_errno(0)
        acl = libc.acl_get_fd_np(fd, 0x100)  # ACL_TYPE_EXTENDED
        if not acl:
            if ctypes.get_errno() == errno.ENOENT:
                return
            raise ValueError
        try:
            if libc.acl_valid(acl) != 0:
                raise ValueError
            owner = ctypes.create_string_buffer(16)
            if libc.mbr_uid_to_uuid(os.getuid(), owner) != 0:
                raise ValueError
            for index in range(129):
                entry = ptr()
                ctypes.set_errno(0)
                rc = libc.acl_get_entry(acl, index, ctypes.byref(entry))
                if rc == -1 and ctypes.get_errno() == errno.EINVAL:
                    return
                if rc != 0 or not entry.value or index == 128:
                    raise ValueError
                tag, mask, flagset = ctypes.c_int(), ctypes.c_uint64(), ptr()
                if (
                    libc.acl_get_tag_type(entry, ctypes.byref(tag)) != 0
                    or libc.acl_get_permset_mask_np(entry, ctypes.byref(mask)) != 0
                    or libc.acl_get_flagset_np(entry, ctypes.byref(flagset)) != 0
                    or not flagset.value
                ):
                    raise ValueError
                flags = 0
                for bit in range(32):
                    present = libc.acl_get_flag_np(flagset, 1 << bit)
                    if present not in (0, 1):
                        raise ValueError
                    flags |= present << bit
                if tag.value not in (1, 2) or mask.value & ~0x103FFE or flags & ~0x1F0:
                    raise ValueError
                # DENY and inherit-only entries add no effective file access.
                # Metadata-only grants are harmless; data/modify/security grants
                # must identify exactly this process user, never one of its groups.
                if tag.value == 1 and not flags & 0x100 and mask.value & 0x353E:
                    qualifier = libc.acl_get_qualifier(entry)
                    if not qualifier:
                        raise ValueError
                    try:
                        if ctypes.string_at(qualifier, 16) != owner.raw:
                            raise ValueError
                    finally:
                        libc.acl_free(qualifier)
        finally:
            libc.acl_free(acl)
    except (OSError, AttributeError, ValueError):
        raise BuildError("macos_private_file_invalid") from None


def private_file(path: Path, *, outside: Path | None = None):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = None
    try:
        if (
            not path.is_absolute()
            or path.is_symlink()
            or (outside and path.resolve().is_relative_to(outside.resolve()))
        ):
            raise BuildError("macos_private_file_invalid")
        fd = os.open(path, flags)
        st = os.fstat(fd)
        if (
            not stat.S_ISREG(st.st_mode)
            or st.st_uid != os.getuid()
            or stat.S_IMODE(st.st_mode) != 0o600
            or st.st_nlink != 1
        ):
            raise BuildError("macos_private_file_invalid")
        if sys.platform == "darwin":
            darwin_private_acl(fd)
        return fd
    except (OSError, BuildError):
        if fd is not None:
            os.close(fd)
        raise BuildError("macos_private_file_invalid") from None


class Isolation:
    def __init__(self, plan: Plan, env: Mapping[str, str], root: Path):
        self.plan, self.env, self.root = plan, dict(env), root
        self.fd = None
        self.original = None

    def require(self):
        kind = self.env.get("TASKPAW_MACOS_SMOKE_ISOLATION", "")
        if kind == "github-hosted-fresh":
            valid = (
                self.env.get("GITHUB_ACTIONS") == "true"
                and self.env.get("RUNNER_ENVIRONMENT") == "github-hosted"
                and self.env.get("RUNNER_OS") == "macOS"
                and self.env.get("RUNNER_ARCH")
                == ("ARM64" if self.plan.arch == "arm64" else "X64")
                and self.env.get("GITHUB_JOB") == "bundle"
                and bool(re.fullmatch(r"[0-9]+", self.env.get("GITHUB_RUN_ID", "")))
            )
            if not valid:
                raise BuildError("macos_smoke_isolation_required")
        elif kind == "disposable-native":
            try:
                path = Path(self.env.get("TASKPAW_MACOS_SMOKE_ATTESTATION", ""))
                fd = private_file(path, outside=self.root)
                with os.fdopen(fd, "rb") as f:
                    raw = f.read(16385)
                if len(raw) > 16384:
                    raise ValueError
                record = json.loads(raw)
                expected = {
                    "version",
                    "kind",
                    "session_id",
                    "boot_session_uuid",
                    "target",
                    "dedicated",
                    "clean",
                    "disposable",
                    "no_real_taskpaw",
                    "no_independent_taskpaw_launches",
                }
                if (
                    not isinstance(record, dict)
                    or set(record) != expected
                    or type(record["version"]) is not int
                    or record["version"] != 1
                    or record["kind"] != kind
                    or record["target"] != self.plan.target
                    or str(uuid.UUID(record["boot_session_uuid"])) != boot_uuid()
                ):
                    raise ValueError
                uuid.UUID(record["session_id"])
                if not all(
                    record[k] is True
                    for k in (
                        "dedicated",
                        "clean",
                        "disposable",
                        "no_real_taskpaw",
                        "no_independent_taskpaw_launches",
                    )
                ):
                    raise ValueError
                if self.original is not None and raw != self.original:
                    raise ValueError
                self.original = raw
            except (OSError, ValueError, TypeError, KeyError, BuildError):
                raise BuildError("macos_smoke_isolation_required") from None
        else:
            raise BuildError("macos_smoke_isolation_required")
        if self.fd is None:
            import fcntl

            lock = Path(tempfile.gettempdir()) / f"taskpaw-r13-smoke-{os.getuid()}.lock"
            try:
                fd = os.open(
                    lock, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600
                )
                st = os.fstat(fd)
                if (
                    not stat.S_ISREG(st.st_mode)
                    or st.st_uid != os.getuid()
                    or st.st_nlink != 1
                    or stat.S_IMODE(st.st_mode) != 0o600
                ):
                    raise OSError
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if "fd" in locals():
                    os.close(fd)
                raise BuildError("macos_smoke_isolation_required") from None
            self.fd = fd

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        self.require()
        return self

    def __exit__(self, *args):
        self.close()


def validate_profiles(plan, root):
    for backend in (False, True):
        try:
            record = plistlib.loads(plan.entitlements(root, backend).read_bytes())
        except (OSError, ValueError, plistlib.InvalidFileException):
            raise BuildError("macos_entitlements_invalid") from None
        expected = ADHOC_KEYS if backend and plan.mode == "adhoc" else set()
        if (
            not isinstance(record, dict)
            or set(record) != expected
            or not all(v is True for v in record.values())
        ):
            raise BuildError("macos_entitlements_invalid")


def arches(path, plan, env):
    out, _ = tool("architecture_verify", ["lipo", "-archs", str(path)], env=env)
    if out.decode().strip().split() != [plan.arch]:
        raise BuildError("macos_artifact_architecture_mismatch")


def sign(path, plan, env, root, *, backend=False, dmg=False):
    cmd = [
        "codesign",
        "--sign",
        plan.identity if plan.mode == "formal" else "-",
        "--force",
    ]
    if plan.mode == "formal":
        cmd += ["--keychain", plan.keychain, "--timestamp"]
        if not dmg:
            cmd += ["--options", "runtime"]
    else:
        cmd += ["--timestamp=none"]
    if not dmg:
        cmd += ["--entitlements", str(plan.entitlements(root, backend))]
    tool("code_sign", cmd + [str(path)], env=env)


def macho_filetype(path):
    # Native builds contain thin headers; never infer code kind from its filename.
    with Path(path).open("rb") as f:
        header = f.read(32)
    magic = header[:4]
    big = {b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf"}
    little = {b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe"}
    minimum = 32 if magic in {b"\xfe\xed\xfa\xcf", b"\xcf\xfa\xed\xfe"} else 28
    if magic not in big | little or len(header) < minimum:
        raise BuildError("macos_macho_type_invalid")
    kind = int.from_bytes(header[12:16], "big" if magic in big else "little")
    if kind not in {2, 6, 8}:  # MH_EXECUTE, MH_DYLIB, MH_BUNDLE (Apple loader.h).
        raise BuildError("macos_macho_type_invalid")
    return kind


def signature(path, plan, env, root, *, backend=False, dmg=False, native_library=False):
    if backend or native_library:
        kind = macho_filetype(path)
        if (native_library and kind not in {6, 8}) or (backend and kind != 2):
            raise BuildError("macos_macho_type_invalid")
    tool("signature_verify", ["codesign", "--verify", "--strict", str(path)], env=env)
    _, display = tool(
        "signature_metadata",
        ["codesign", "--display", "--verbose=4", str(path)],
        env=env,
    )
    if plan.mode == "adhoc":
        if b"Signature=adhoc" not in display:
            raise BuildError("macos_adhoc_signature_expected")
    else:
        team = re.search(rb"^TeamIdentifier=([^\r\n]+)", display, re.MULTILINE)
        if (
            not team
            or team.group(1).decode() != plan.team
            or not re.search(rb"^Timestamp=(?!none$).+", display, re.MULTILINE)
        ):
            raise BuildError("macos_signing_identity_mismatch")
        if not dmg and b"(runtime)" not in display:
            raise BuildError("macos_hardened_runtime_missing")
        requirement = (
            "=anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.13] exists and certificate leaf[subject.OU] = "
            + plan.team
        )
        tool(
            "developer_id_verify",
            ["codesign", "--verify", "--strict", "-R", requirement, str(path)],
            env=env,
        )
        with tempfile.TemporaryDirectory(prefix="taskpaw-signature-") as tmp:
            prefix = Path(tmp) / "cert"
            tool(
                "certificate_verify",
                [
                    "codesign",
                    "--display",
                    "--extract-certificates",
                    str(prefix),
                    str(path),
                ],
                env=env,
            )
            if (
                hashlib.sha1(Path(str(prefix) + "0").read_bytes()).hexdigest().upper()
                != plan.identity
            ):
                raise BuildError("macos_signing_identity_mismatch")
    if not dmg:
        out, _ = tool(
            "entitlements_verify",
            ["codesign", "--display", "--entitlements", "-", "--xml", str(path)],
            env=env,
        )
        try:
            ents = plistlib.loads(out) if out.strip() else {}
        except (ValueError, plistlib.InvalidFileException):
            raise BuildError("macos_entitlements_invalid") from None
        expected = plistlib.loads(plan.entitlements(root, backend).read_bytes())
        if ents != expected:
            raise BuildError("macos_entitlements_invalid")


def preflight(plan, env, root):
    validate_profiles(plan, root)
    if plan.mode == "adhoc":
        return
    fd = private_file(Path(plan.keychain), outside=root)
    os.close(fd)
    out, _ = tool(
        "keychain_context", ["security", "list-keychains", "-d", "user"], env=env
    )
    if Path(plan.keychain).resolve() not in {
        Path(p).resolve() for p in shlex.split(out.decode())
    }:
        raise BuildError("macos_signing_preflight_failed")
    out, _ = tool(
        "keychain_identity",
        ["security", "find-identity", "-v", "-p", "codesigning", plan.keychain],
        env=env,
    )
    if not re.search(plan.identity.encode() + rb' "Developer ID Application:', out):
        raise BuildError("macos_signing_preflight_failed")
    with tempfile.TemporaryDirectory(prefix="taskpaw-signing-probe-") as tmp:
        probe = Path(tmp) / "probe"
        tool(
            "signing_probe_compile",
            ["xcrun", "clang", "-arch", plan.arch, "-x", "c", "-o", str(probe), "-"],
            env=env,
            timeout=30,
            input_data=b"int main(void) { return 0; }\n",
        )
        sign(probe, plan, env, root)
        signature(probe, plan, env, root)
    tool(
        "notary_profile_verify",
        [
            "xcrun",
            "notarytool",
            "history",
            "--keychain-profile",
            plan.profile,
            "--keychain",
            plan.keychain,
            "--output-format",
            "json",
        ],
        env=env,
    )


def archive_entries(sidecar):
    try:
        from PyInstaller.archive.readers import CArchiveReader
        from PyInstaller.loader.pyimod01_archive import ArchiveReadError
    except ImportError:
        raise BuildError("macos_archive_reader_unavailable") from None
    try:
        reader = CArchiveReader(str(sidecar))
    except (OSError, ValueError, ArchiveReadError):
        raise BuildError("macos_archive_invalid") from None
    if len(reader.toc) > 10000:
        raise BuildError("macos_archive_limit")
    total = 0
    for name, entry in reader.toc.items():
        if (
            Path(name).is_absolute()
            or ".." in Path(name).parts
            or "\0" in name
            or "\\" in name
        ):
            raise BuildError("macos_archive_name_invalid")
        offset, length, size, compressed, code = entry
        if (
            size < 0
            or size > 512 * 1024 * 1024
            or length < 0
            or length > 512 * 1024 * 1024 + 65536
            or offset < 0
            or reader._start_offset + offset + length > Path(sidecar).stat().st_size
        ):
            raise BuildError("macos_archive_limit")
        # Read within the actual CArchive, bounding decompression before allocation.
        with open(sidecar, "rb") as f:
            f.seek(reader._start_offset + offset)
            data = f.read(length)
        if len(data) != length:
            raise BuildError("macos_archive_invalid")
        if compressed:
            decoder = zlib.decompressobj()
            data = decoder.decompress(data, size + 1)
            if not decoder.eof or decoder.unconsumed_tail:
                raise BuildError("macos_archive_invalid")
        if len(data) != size:
            raise BuildError("macos_archive_invalid")
        total += len(data)
        if total > 1024 * 1024 * 1024:
            raise BuildError("macos_archive_limit")
        macho = data[:4] in MAGICS
        if code == "b" and not macho:
            raise BuildError("macos_archive_binary_invalid")
        if code == "b" or macho:
            yield name, data


def verify_archive(sidecar, plan, env, root):
    required = set()
    count = 0
    try:
        with tempfile.TemporaryDirectory(prefix="taskpaw-native-inspection-") as tmp:
            for name, data in archive_entries(sidecar):
                lower = name.lower()
                if Path(name).name == "Python" or Path(name).name.startswith(
                    "libpython"
                ):
                    required.add("python")
                if "pydantic_core/" in lower and "_pydantic_core" in lower:
                    required.add("pydantic")
                if "psutil/" in lower and "_psutil" in lower:
                    required.add("psutil")
                path = Path(tmp) / str(count)
                path.write_bytes(data)
                path.chmod(0o600)
                arches(path, plan, env)
                kind = macho_filetype(path)
                signature(
                    path,
                    plan,
                    env,
                    root,
                    backend=kind == 2,
                    native_library=kind in {6, 8},
                )
                count += 1
    except (OSError, ValueError, zlib.error):
        raise BuildError("macos_archive_invalid") from None
    if required != {"python", "pydantic", "psutil"}:
        raise BuildError("macos_archive_required_extension_missing")
    return count


def app_code(app):
    leaves, bundles = [], []
    for path in app.rglob("*"):
        if path.is_symlink():
            if not path.resolve().is_relative_to(app.resolve()):
                raise BuildError("macos_bundle_symlink_invalid")
            continue
        if path.is_dir() and path.suffix in {".app", ".framework", ".xpc"}:
            bundles.append(path)
        if path.is_file():
            with path.open("rb") as f:
                if f.read(4) in MAGICS:
                    leaves.append(path)
    sidecars = [p for p in leaves if p.name.startswith("taskpaw-backend")]
    if len(sidecars) != 1:
        raise BuildError("macos_bundle_sidecar_invalid")
    return (
        sorted(leaves, key=lambda p: len(p.parts), reverse=True),
        sorted(bundles, key=lambda p: len(p.parts), reverse=True),
        sidecars[0],
    )


def verify_app(app, plan, env, root):
    leaves, bundles, sidecar = app_code(app)
    for path in leaves:
        arches(path, plan, env)
        signature(path, plan, env, root, backend=path == sidecar)
    for path in [*bundles, app]:
        signature(path, plan, env, root)
    return sidecar, verify_archive(sidecar, plan, env, root)


def ready_line(line, base):
    try:
        data = json.loads(line)
        return (
            isinstance(data, dict)
            and data.get("taskpaw_ready") is True
            and data.get("role") == "agent"
            and data.get("base_url") == base
        )
    except (ValueError, UnicodeError):
        return False


def metrics_ok(record, version):
    if (
        not isinstance(record, dict)
        or record.get("server_id") != "release-smoke"
        or record.get("machine") != "release-smoke"
        or record.get("version") != version
    ):
        return False
    monitors = record.get("monitors", {})
    monitor = (
        monitors.get("release-smoke-metrics", {}) if isinstance(monitors, dict) else {}
    )
    if not isinstance(monitor, dict):
        return False
    metrics = monitor.get("metrics", {})
    if not isinstance(metrics, dict):
        return False
    return monitor.get("state") in {"ok", "degraded"} and all(
        type(metrics.get(k)) in {int, float} and math.isfinite(metrics[k])
        for k in ("cpu_pct", "mem_pct", "disk_pct", "net_in_bps", "net_out_bps")
    )


def smoke(sidecar, plan, isolation, env, version):
    # This is intentionally before HOME/ports or any backend Popen.
    isolation.require()
    with tempfile.TemporaryDirectory(prefix="taskpaw-release-smoke-") as tmp:
        home = Path(tmp) / "HOME"
        config = home / "Library/Application Support/TaskPaw/agent.yaml"
        config.parent.mkdir(parents=True, mode=0o700)
        extraction = Path(tmp) / "extraction"
        extraction.mkdir(mode=0o700)
        token = secrets.token_urlsafe(32)
        reservations = [socket.socket(), socket.socket()]
        try:
            for sock in reservations:
                sock.bind(("127.0.0.1", 0))
            net_port, ctl_port = (sock.getsockname()[1] for sock in reservations)
        finally:
            for sock in reservations:
                sock.close()
        payload = {
            "server_id": "release-smoke",
            "machine": "release-smoke",
            "bind_host": "127.0.0.1",
            "bind_port": net_port,
            "control_host": "127.0.0.1",
            "control_port": ctl_port,
            "api_token": token,
            "host_metrics": False,
            "monitors": [
                {
                    "type_id": "host_metrics",
                    "name": "release-smoke-metrics",
                    "config": {
                        "name": "release-smoke-metrics",
                        "poll_interval": 1,
                        "disk_path": str(home),
                    },
                }
            ],
        }
        # JSON is valid YAML; no secret is ever an argv input.
        config.write_text(json.dumps(payload))
        config.chmod(0o600)
        runtime_env = child_env(env, runtime=True)
        runtime_env.update(
            HOME=str(home), TMPDIR=str(extraction), PATH="/usr/bin:/bin:/usr/sbin:/sbin"
        )
        isolation.require()
        try:
            with owned_child(
                [str(sidecar), "agent"],
                stage="smoke",
                grace=10,
                reject_forced=True,
                env=runtime_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as proc:
                base = f"http://127.0.0.1:{ctl_port}"
                success = False
                pending = bytearray()
                with selectors.DefaultSelector() as selector:
                    cancellation_checkpoint()
                    selector.register(proc.stdout, selectors.EVENT_READ, "out")
                    cancellation_checkpoint()
                    selector.register(proc.stderr, selectors.EVENT_READ, "err")
                    cancellation_checkpoint()
                    ready = False
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline and proc.poll() is None:
                        cancellation_checkpoint()
                        for key, _ in selector.select(0.1):
                            chunk = os.read(key.fileobj.fileno(), 8192)
                            if not chunk:
                                selector.unregister(key.fileobj)
                            elif key.data == "out":
                                pending.extend(chunk)
                                if len(pending) > TAIL_LIMIT:
                                    raise BuildError("macos_smoke_output_limit")
                                while b"\n" in pending:
                                    line, _, remaining = pending.partition(b"\n")
                                    pending[:] = remaining
                                    if len(line) > 16384:
                                        raise BuildError("macos_smoke_output_limit")
                                    ready = ready or ready_line(line, base)
                                if len(pending) > 16384:
                                    raise BuildError("macos_smoke_output_limit")
                        if ready:
                            connection = http.client.HTTPConnection(
                                "127.0.0.1", net_port, timeout=2
                            )
                            try:
                                connection.request(
                                    "GET",
                                    "/status",
                                    headers={"Authorization": "Bearer " + token},
                                )
                                response = connection.getresponse()
                                body = response.read(1024 * 1024 + 1)
                                if (
                                    response.status == 200
                                    and len(body) <= 1024 * 1024
                                    and metrics_ok(json.loads(body), version)
                                ):
                                    success = True
                                    break
                            except (OSError, ValueError, http.client.HTTPException):
                                # Expected while the isolated fixture is still starting.
                                time.sleep(0.05)
                            finally:
                                connection.close()
                    if not success:
                        raise BuildError("macos_smoke_readiness_failed")
        finally:
            for port in (net_port, ctl_port):
                try:
                    with socket.socket() as check:
                        check.bind(("127.0.0.1", port))
                except OSError:
                    raise BuildError("macos_smoke_listener_cleanup_failed") from None
    print("macos packaged readiness and native metrics passed", flush=True)


def notarize(path, plan, env):
    out, _ = tool(
        "notarization",
        [
            "xcrun",
            "notarytool",
            "submit",
            str(path),
            "--keychain-profile",
            plan.profile,
            "--keychain",
            plan.keychain,
            "--output-format",
            "json",
            "--wait",
            "--timeout",
            "20m",
        ],
        env=env,
        timeout=1500,
    )
    try:
        record = json.loads(out)
        if record.get("status") != "Accepted":
            raise ValueError
    except (ValueError, AttributeError):
        raise BuildError("macos_notarization_not_accepted") from None
    # Submission-ID/hash traceability is deferred; never claim it is complete.


def staple(path, env):
    for action in ("staple", "validate"):
        tool(
            "notary_ticket_" + action, ["xcrun", "stapler", action, str(path)], env=env
        )


def zip_app(app, path, env):
    tool("app_zip", ["ditto", "-c", "-k", "--keepParent", str(app), str(path)], env=env)


def verify_zip(path, plan, isolation, env, root, version, product_name):
    with tempfile.TemporaryDirectory(prefix="taskpaw-zip-verification-") as tmp:
        dest = Path(tmp) / "TaskPaw smoke 測試"
        dest.mkdir(mode=0o700)
        tool("zip_extract", ["ditto", "-x", "-k", str(path), str(dest)], env=env)
        sidecar, _ = verify_app(dest / (product_name + ".app"), plan, env, root)
        if not os.access(sidecar, os.X_OK):
            raise BuildError("macos_archive_executable_mode_missing")
        smoke(sidecar, plan, isolation, env, version)


def verify_dmg(path, plan, isolation, env, root, version, product_name):
    with tempfile.TemporaryDirectory(prefix="taskpaw-dmg-verification-") as tmp:
        mount = Path(tmp) / "mount"
        mount.mkdir(mode=0o700)
        try:
            tool(
                "dmg_mount",
                [
                    "hdiutil",
                    "attach",
                    "-readonly",
                    "-nobrowse",
                    "-plist",
                    "-mountpoint",
                    str(mount),
                    str(path),
                ],
                env=env,
            )
            app = Path(tmp) / "TaskPaw smoke 測試" / (product_name + ".app")
            app.parent.mkdir(mode=0o700)
            shutil.copytree(mount / (product_name + ".app"), app, symlinks=True)
            sidecar, _ = verify_app(app, plan, env, root)
            smoke(sidecar, plan, isolation, env, version)
        finally:
            # Also covers attach failure after a successful mount operation.
            if os.path.ismount(mount):
                tool("dmg_unmount", ["hdiutil", "detach", str(mount)], env=env)


def finalize(plan, isolation, env, root, cfg):
    bundle = plan.bundle_root(root)
    app = bundle / "macos" / (cfg["productName"] + ".app")
    if not app.is_dir():
        raise BuildError("macos_app_missing")
    artifacts = []
    with tempfile.TemporaryDirectory(prefix="taskpaw-finalization-") as tmp:
        work = Path(tmp)
        staged_app = work / app.name
        shutil.copytree(app, staged_app, symlinks=True)
        leaves, bundles, sidecar = app_code(staged_app)
        for path in leaves:
            sign(path, plan, env, root, backend=path == sidecar)
        for path in [*bundles, staged_app]:
            sign(path, plan, env, root)
        sidecar, count = verify_app(staged_app, plan, env, root)
        smoke(sidecar, plan, isolation, env, cfg["version"])
        if plan.mode == "formal":
            submitted_zip = work / "submitted-app.zip"
            zip_app(staged_app, submitted_zip, env)
            notarize(submitted_zip, plan, env)
            staple(staged_app, env)
            verify_app(staged_app, plan, env, root)
        stem = (
            cfg["productName"]
            + "_"
            + cfg["version"]
            + "_"
            + ("aarch64" if plan.arch == "arm64" else "x64")
        )
        if "app" in plan.bundles:
            zipped = work / (stem + ".app.zip")
            zip_app(staged_app, zipped, env)
            verify_zip(
                zipped, plan, isolation, env, root, cfg["version"], cfg["productName"]
            )
            artifacts.append((zipped, bundle / "macos" / zipped.name))
        if "dmg" in plan.bundles:
            staging = work / "dmg-staging"
            staging.mkdir()
            shutil.copytree(staged_app, staging / app.name, symlinks=True)
            (staging / "Applications").symlink_to("/Applications")
            dmg = work / (stem + ".dmg")
            tool(
                "dmg_create",
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
                ],
                env=env,
            )
            if plan.mode == "formal":
                sign(dmg, plan, env, root, dmg=True)
                signature(dmg, plan, env, root, dmg=True)
                notarize(dmg, plan, env)
                staple(dmg, env)
            verify_dmg(
                dmg, plan, isolation, env, root, cfg["version"], cfg["productName"]
            )
            artifacts.append((dmg, bundle / "dmg" / dmg.name))
        for source, dest in artifacts:
            dest.parent.mkdir(parents=True, exist_ok=True)
            pending = dest.with_suffix(dest.suffix + ".tmp")
            shutil.copy2(source, pending)
            os.replace(pending, dest)
        # Preserve the final signed/stapled local app only after required checks.
        shutil.rmtree(app)
        shutil.copytree(staged_app, app, symlinks=True)
        summary = {
            "schema_version": 1,
            "role": cfg["identifier"].rsplit(".", 1)[-1],
            "version": cfg["version"],
            "target": plan.target,
            "mode": plan.mode,
            "native_entry_count": count,
            "runtime_verified": True,
            "deliverables_verified": True,
            "notarization_accepted": plan.mode == "formal",
            "staple_verified": plan.mode == "formal",
            "artifacts": [
                {
                    "name": dest.name,
                    "sha256": hashlib.sha256(dest.read_bytes()).hexdigest(),
                }
                for _, dest in artifacts
            ],
        }
        commit, _ = tool(
            "source_revision", ["git", "rev-parse", "HEAD"], env=env, cwd=root
        )
        summary["source_commit"] = commit.decode().strip()
        report = bundle / "macos-verification.json"
        pending = report.with_suffix(".tmp")
        pending.write_text(json.dumps(summary, sort_keys=True) + "\n")
        os.replace(pending, report)
    print("macos verified build complete (not clean-download acceptance)", flush=True)


@contextlib.contextmanager
def build_session(plan, env, root, *, skip_tauri=False):
    isolation = Isolation(plan, env, root)
    with cancellation_scope():
        try:
            if not skip_tauri:
                isolation.require()
            yield isolation
        finally:
            isolation.close()
