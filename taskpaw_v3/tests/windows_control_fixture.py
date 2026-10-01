"""Windows CI fixtures made by the production credential writer, never a daemon.

Only the non-secret output path travels in TASKPAW_TEST_CONTROL_FIXTURES.
Secure and broad_acl fixtures must run; unavailable privileged cases are listed
as explicit skips. No descriptor contents or token are printed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from taskpaw_v3.core import control_file as files


def _private_directory(path: Path) -> None:
    import ctypes
    from ctypes import wintypes as wt

    _, user_text = files._user_sid()
    sd = ctypes.c_void_p()
    files._win_ok(
        files._sddl(
            f"O:{user_text}D:P(A;;FA;;;{user_text})(A;;FA;;;SY)",
            1,
            ctypes.byref(sd),
            None,
        )
    )
    try:
        sa = files._SA(ctypes.sizeof(files._SA), sd, False)
        create = files._signature(
            files._kernel,
            "CreateDirectoryW",
            [wt.LPCWSTR, ctypes.POINTER(files._SA)],
            wt.BOOL,
        )
        files._win_ok(create(str(path), ctypes.byref(sa)), "fixture_directory_failed")
    finally:
        files._local_free(sd)


def _security_change(
    path: Path,
    *,
    dacl: str | None = None,
    null_dacl: bool = False,
    owner_sid: str | None = None,
) -> None:
    import ctypes
    from ctypes import wintypes as wt

    set_security = files._signature(
        files._advapi,
        "SetSecurityInfo",
        [
            wt.HANDLE,
            wt.DWORD,
            wt.DWORD,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ],
        wt.DWORD,
    )
    get_dacl = files._signature(
        files._advapi,
        "GetSecurityDescriptorDacl",
        [
            ctypes.c_void_p,
            ctypes.POINTER(wt.BOOL),
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(wt.BOOL),
        ],
        wt.BOOL,
    )
    handle = files._create(
        str(path),
        0x20000 | 0x40000 | 0x80000,
        7,
        None,
        3,
        0x200000 | (0x2000000 if path.is_dir() else 0x80),
        None,
    )
    files._win_ok(handle != files._INVALID, "fixture_security_failed")
    sd, acl, owner = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    try:
        flags = 0
        if dacl is not None:
            present, defaulted = wt.BOOL(), wt.BOOL()
            files._win_ok(files._sddl(dacl, 1, ctypes.byref(sd), None))
            files._win_ok(
                get_dacl(
                    sd,
                    ctypes.byref(present),
                    ctypes.byref(acl),
                    ctypes.byref(defaulted),
                )
            )
            flags |= 4 | 0x80000000
        elif null_dacl:
            flags |= 4 | 0x80000000
        if owner_sid is not None:
            files._win_ok(files._string_sid(owner_sid, ctypes.byref(owner)))
            flags |= 1
        result = set_security(handle, 1, flags, owner, None, acl, None)
        files._win_ok(result == 0, "fixture_security_failed")
    finally:
        files._close(handle)
        if sd:
            files._local_free(sd)
        if owner:
            files._local_free(owner)


def generate(root: Path) -> dict:
    if os.name != "nt":
        raise RuntimeError("Windows-only credential fixtures")
    _private_directory(root)
    descriptor = files.ControlDescriptor(
        1,
        "agent",
        "http://127.0.0.1:5681",
        "0123456789abcdef0123456789abcdef",
        "fake-python-rust-interop-token",
    )
    cases = []

    def published(name: str) -> Path:
        directory = root / name
        _private_directory(directory)
        lease = files.CredentialLease(directory, "agent.control.json")
        try:
            lease.publish(descriptor)
        finally:
            lease.close()
        return directory / "agent.control.json"

    def record(name: str, path: Path, expect: str, reason: str | None = None):
        case = {"name": name, "path": str(path), "expect": expect}
        if reason is not None:
            case["reason"] = reason
        cases.append(case)

    secure = published("secure")
    record("secure", secure, "accept")
    _, owner = files._user_sid()
    private = f"D:P(A;;FA;;;{owner})(A;;FA;;;SY)"
    broad = published("broad_acl")
    _security_change(broad, dacl=private + "(A;;FR;;;WD)")
    record("broad_acl", broad, "reject")
    null = published("null_acl")
    _security_change(null, null_dacl=True)
    record("null_acl", null, "reject")
    parent = published("unsafe_parent")
    _security_change(parent.parent, dacl=private + "(A;;0x2;;;WD)")
    record("unsafe_parent", parent, "reject")
    wrong = published("wrong_owner")
    try:
        _security_change(wrong, owner_sid="S-1-5-18")
    except files.ControlCredentialError:
        record("wrong_owner", wrong, "skip", "owner_privilege_unavailable")
    else:
        record("wrong_owner", wrong, "reject")
    for name, sid in (
        ("trusted_system_parent", "S-1-5-18"),
        ("trusted_admins_parent", "S-1-5-32-544"),
    ):
        trusted = published(name)
        try:
            _security_change(trusted.parent, owner_sid=sid)
        except files.ControlCredentialError:
            record(name, trusted, "skip", "owner_privilege_unavailable")
        else:
            # Open and publish again after the actual owner mutation, so this
            # checks the production writer's final-directory policy as well.
            lease = files.CredentialLease(trusted.parent, "agent.control.json")
            try:
                lease.publish(descriptor)
            finally:
                lease.close()
            record(name, trusted, "accept")
    for name, directory in (("reparse_file", False), ("reparse_parent", True)):
        link = root / (name + ".link")
        try:
            link.symlink_to(
                secure.parent if directory else secure, target_is_directory=directory
            )
        except OSError:
            record(name, link, "skip", "symlink_privilege_unavailable")
        else:
            record(name, link / "agent.control.json" if directory else link, "reject")
    manifest = {"version": 1, "cases": cases}
    tmp = root / "manifest.json.tmp"
    tmp.write_text(json.dumps(manifest), encoding="utf-8")
    os.replace(tmp, root / "manifest.json")
    return manifest


def main() -> None:
    # Never print a path/error supplied by the environment or a secret body.
    value = os.environ.get("TASKPAW_TEST_CONTROL_FIXTURES", "")
    if not value or not Path(value).is_absolute() or ".." in Path(value).parts:
        raise SystemExit("A safe fixture output path is required")
    try:
        generate(Path(value))
    except (OSError, ValueError):
        raise SystemExit("Windows credential fixture generation failed") from None
    print("Windows credential fixtures created")


if __name__ == "__main__":
    main()
