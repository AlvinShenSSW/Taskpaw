"""CI-only, nonsecret diagnostics for native credential directory rejection.

Inspect temporary fixture directories using the production HANDLE access/share
flags. Never open a credential file, print a SID/path, or change an existing ACL.
Component indices identify the offending ancestor without exposing user names.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def inspect_directory(path: Path) -> dict:
    import ctypes
    from ctypes import wintypes as wt

    from taskpaw_v3.core import control_file as files

    user, _ = files._user_sid()
    identities = {
        user: "current",
        files._named_sid("S-1-5-18"): "system",
        files._named_sid("S-1-5-32-544"): "administrators",
    }
    # Fixed public identities only. Never stringify an arbitrary SID or user.
    for name, sid in {
        "everyone": "S-1-1-0",
        "creator_owner": "S-1-3-0",
        "creator_group": "S-1-3-1",
        "owner_rights": "S-1-3-4",
        "authenticated_users": "S-1-5-11",
        "builtin_users": "S-1-5-32-545",
        "all_application_packages": "S-1-15-2-1",
        "all_restricted_application_packages": "S-1-15-2-2",
        "trusted_installer": "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
    }.items():
        identities[files._named_sid(sid)] = name
    result: dict = {"drive": path.drive, "components": []}
    handles = []
    components = [Path(path.anchor)]
    for part in path.parts[1:]:
        components.append(components[-1] / part)
    try:
        for index, component in enumerate(components):
            item = {"index": index, "final": index == len(components) - 1}
            result["components"].append(item)
            ctypes.set_last_error(0)
            handle = files._create(
                str(component),
                files._READ_CONTROL | 0x80,
                3,
                None,
                3,
                0x00200000 | 0x02000000,
                None,
            )
            item["create_error"] = ctypes.get_last_error()
            if handle == files._INVALID:
                item["stage"] = "CreateFileW"
                break
            handles.append(handle)
            tag = files._Tag()
            ctypes.set_last_error(0)
            ok = files._get_tag(handle, 9, ctypes.byref(tag), ctypes.sizeof(tag))
            item.update(
                tag_ok=bool(ok),
                tag_error=ctypes.get_last_error(),
                attributes=tag.FileAttributes,
                reparse_tag=tag.ReparseTag,
                file_type=files._file_type(handle),
            )
            owner, dacl, sd = files._PVOID(), files._PVOID(), files._PVOID()
            code = files._get_security(
                handle,
                1,
                5,
                ctypes.byref(owner),
                None,
                ctypes.byref(dacl),
                None,
                ctypes.byref(sd),
            )
            item["security_error"] = code
            if code:
                item["stage"] = "GetSecurityInfo"
                break
            try:
                item["owner"] = identities.get(files._sid_bytes(owner), "other")
                control, revision = wt.WORD(), wt.DWORD()
                ok = files._get_control(
                    sd, ctypes.byref(control), ctypes.byref(revision)
                )
                item.update(
                    control_ok=bool(ok), control=control.value, null_dacl=not bool(dacl)
                )
                if dacl:
                    acl = ctypes.cast(dacl, ctypes.POINTER(files._ACL)).contents
                    item.update(
                        acl_revision=acl.revision,
                        acl_size=acl.size,
                        ace_count=acl.count,
                    )
                    entries = []
                    item["aces"] = entries
                    for number in range(min(acl.count, 256)):
                        address = files._PVOID()
                        if not files._get_ace(dacl, number, ctypes.byref(address)):
                            entries.append(
                                {
                                    "index": number,
                                    "get_ace_error": ctypes.get_last_error(),
                                }
                            )
                            break
                        ace = ctypes.cast(address, ctypes.POINTER(files._ACE)).contents
                        entry = {
                            "index": number,
                            "kind": ace.kind,
                            "flags": ace.flags,
                            "size": ace.size,
                            "mask": ace.mask,
                        }
                        entries.append(entry)
                        if ace.kind in {0, 1} and ace.size >= 16:
                            sid = files._sid_bytes(files._PVOID(address.value + 8))
                            entry["principal"] = identities.get(sid, "other")
                            mask = (
                                files._PARENT_UNSAFE
                                if item["final"]
                                else files._DIR_UNSAFE
                            )
                            entry["unsafe_mask"] = ace.mask & mask
            finally:
                if sd:
                    files._local_free(sd)
            try:
                files._security(handle, user, directory=True, final=item["final"])
                item["production_security"] = "accept"
            except files.ControlCredentialError as error:
                item["production_security"] = str(error)  # fixed code only
            # Continue after a policy rejection: show every ancestor/final ACL.
        try:
            pinned = files._WindowsDirectory(path)
            pinned.close()
            result["production_directory"] = "accept"
        except files.ControlCredentialError as error:
            result["production_directory"] = str(error)
    finally:
        for handle in reversed(handles):
            files._close(handle)
    return result


def main() -> None:
    if sys.platform != "win32":
        print(json.dumps({"platform": "non_windows", "status": "skip"}))
        return
    from taskpaw_v3.tests.windows_control_fixture import _private_directory

    # Cover the standard pytest drive and the dedicated interop runner drive.
    for label, base in (
        ("python_temp", None),
        ("runner_temp", os.environ.get("RUNNER_TEMP")),
    ):
        with tempfile.TemporaryDirectory(
            prefix="taskpaw-acl-diagnostic-", dir=base
        ) as temporary:
            ordinary = Path(temporary)
            print(
                json.dumps({"case": label + "_ordinary", **inspect_directory(ordinary)})
            )
            private = ordinary / "private"
            _private_directory(private)
            print(
                json.dumps({"case": label + "_private", **inspect_directory(private)})
            )


if __name__ == "__main__":
    main()
