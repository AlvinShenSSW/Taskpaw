"""Explicit local hook setup. Never runs host CLIs, unrelated hooks or trust actions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from taskpaw_v3.integrations.activity_writer import (
    _CLAUDE_EVENT_STATE,
    _CODEX_EVENT_STATE,
    read_facts,
)
from taskpaw_v3.monitors.session_activity import safe_path

WINDOWS = sys.platform == "win32"
WRITER = Path(__file__).with_name("activity_writer.py").absolute()


class SetupError(Exception):
    """Only fixed, sanitized diagnostics are carried in this exception."""


def marker(tool: str) -> str:
    return f"taskpaw-ai-activity-v1-{tool}"


def owned(handler: dict, tool: str) -> bool:
    if handler.get("type") != "command" or not isinstance(handler.get("command"), str):
        return False
    try:
        words = shlex.split(handler["command"])
    except ValueError:
        return False
    return any(
        a == "--taskpaw-hook-id" and b == marker(tool) for a, b in zip(words, words[1:])
    )


def render_command(
    python: str, writer: str, tool: str, output: str, *, windows: bool = False
) -> str:
    paths = [python, writer, output]
    if windows:
        paths = [p.replace("\\", "/") for p in paths]
    return shlex.join(
        [
            paths[0],
            paths[1],
            "--tool",
            tool,
            "--path",
            paths[2],
            "--taskpaw-hook-id",
            marker(tool),
        ]
    )


def shell_for(tool: str) -> str:
    if WINDOWS and tool == "codex":
        raise SetupError("Windows Codex hook dispatch not verified")
    if WINDOWS:
        override = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
        candidates = []
        if override:
            candidates.append(Path(override))
        else:
            roots = []
            git = shutil.which("git.exe")
            if git:
                roots.append(Path(git).parent.parent)
            for variable, suffix in (
                ("ProgramFiles", "Git"),
                ("LOCALAPPDATA", "Programs/Git"),
            ):
                directory = os.environ.get(variable)
                if directory:
                    roots.append(Path(directory) / suffix)
            candidates.extend(
                root / relative
                for root in roots
                for relative in ("bin/bash.exe", "usr/bin/bash.exe")
            )
        for candidate in candidates:
            if "system32" in str(candidate.resolve()).replace("\\", "/").lower().split(
                "/"
            ):
                continue
            if candidate.is_file():
                return str(candidate)
        raise SetupError("Git Bash missing; set CLAUDE_CODE_GIT_BASH_PATH")
    shell = shutil.which("bash" if tool == "claude" else "sh")
    if not shell:
        raise SetupError("required shell missing")
    return shell


def _pairs(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SetupError("duplicate JSON key")
        result[key] = value
    return result


def read_bytes(path: Path) -> bytes | None:
    if not safe_path(path):
        raise SetupError("symlink or reparse target refused")
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise SetupError("unsupported target file type")
        return path.read_bytes()
    except FileNotFoundError:
        return None


def decode(raw: bytes | None) -> dict:
    try:
        data = {} if raw is None else json.loads(raw, object_pairs_hook=_pairs)
    except (ValueError, UnicodeError):
        raise SetupError("invalid settings JSON") from None
    if not isinstance(data, dict):
        raise SetupError("settings must be a JSON object")
    return data


def settings(raw: bytes | None) -> dict:
    data = decode(raw)
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        raise SetupError("invalid hooks object")
    for groups in hooks.values():
        if not isinstance(groups, list):
            raise SetupError("invalid hook groups")
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise SetupError("invalid hook group")
            if "matcher" in group and not isinstance(group["matcher"], str):
                raise SetupError("invalid hook matcher")
            for handler in group["hooks"]:
                if not isinstance(handler, dict) or not isinstance(
                    handler.get("type"), str
                ):
                    raise SetupError("invalid hook handler")
                if handler["type"] == "command" and not isinstance(
                    handler.get("command"), str
                ):
                    raise SetupError("invalid hook command")
    return data


def digest(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def private_dir(path: Path) -> None:
    if not safe_path(path):
        raise SetupError("unsafe local storage directory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not WINDOWS:
        path.chmod(0o700)


def backup(tool: str, raw: bytes | None, state_dir: Path) -> Path | None:
    if raw is None:
        return None
    directory = state_dir / "hook-setup/backups"
    private_dir(directory.parent)
    private_dir(directory)
    path = (
        directory
        / f"{tool}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex}.json.bak"
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return path


def atomic_write(path: Path, raw: bytes | None, expected: bytes | None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if raw is None:
        if digest(read_bytes(path)) != digest(expected):
            raise SetupError(
                "settings changed concurrently; retry with host settings edits paused"
            )
        path.unlink(missing_ok=True)
        return
    mode = stat.S_IMODE(path.stat().st_mode) if expected is not None else 0o600
    fd, name = tempfile.mkstemp(prefix=".taskpaw-", suffix=".tmp", dir=path.parent)
    temp = Path(name)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.chmod(temp, mode)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if digest(read_bytes(path)) != digest(expected):
            raise SetupError(
                "settings changed concurrently; retry with host settings edits paused"
            )
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def encode(data: dict) -> bytes:
    return (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def required(tool: str, command: str) -> dict[str, dict]:
    events = _CLAUDE_EVENT_STATE if tool == "claude" else _CODEX_EVENT_STATE
    return {
        event: {
            **({"matcher": "permission_prompt"} if event == "Notification" else {}),
            "hooks": [{"type": "command", "command": command, "timeout": 3}],
        }
        for event in events
    }


def validate_installed(data: dict, tool: str, command: str) -> None:
    if data.get("disableAllHooks") or data.get("disabled"):
        raise SetupError("hooks disabled; enable them before checking")
    expected = required(tool, command)
    seen: dict[str, int] = {}
    for event, groups in data.get("hooks", {}).items():
        for group in groups:
            for handler in group["hooks"]:
                if not owned(handler, tool):
                    continue
                if (
                    event not in expected
                    or handler != expected[event]["hooks"][0]
                    or {k: v for k, v in group.items() if k != "hooks"}
                    != {k: v for k, v in expected[event].items() if k != "hooks"}
                ):
                    raise SetupError("edited TaskPaw handler; reinstall before check")
                seen[event] = seen.get(event, 0) + 1
    if any(seen.get(event) != 1 for event in expected):
        raise SetupError("missing or duplicate TaskPaw handlers; reinstall")


def verify_writer(command: str, tool: str, state_dir: Path) -> None:
    """Only canonical generated argv is executed, with isolated check output."""
    shell = shell_for(tool)
    try:
        args = shlex.split(command)
    except ValueError:
        raise SetupError("invalid generated command; reinstall") from None
    if (
        len(args) != 8
        or args[2:5] != ["--tool", tool, "--path"]
        or args[6:] != ["--taskpaw-hook-id", marker(tool)]
        or shlex.join(args) != command
    ):
        raise SetupError("invalid generated command; reinstall")
    for value in args[:2]:
        if not Path(value).is_absolute() or not Path(value).is_file():
            raise SetupError("interpreter or writer missing")
    if not os.access(args[0], os.X_OK):
        raise SetupError("interpreter is not executable")
    private_dir(state_dir)
    with tempfile.TemporaryDirectory(
        prefix=".activity-check-", dir=state_dir
    ) as directory:
        output = Path(directory) / "state.json"
        args[5] = str(output).replace("\\", "/") if WINDOWS else str(output)
        nonce = uuid.uuid4().hex
        for event, state in (
            ("UserPromptSubmit", "busy"),
            ("PermissionRequest", "waiting"),
            ("Stop", "idle"),
        ):
            output.unlink(missing_ok=True)
            started = time.time()
            try:
                # exec replaces the shell, so timeout kills the writer itself.
                completed = subprocess.run(
                    [shell, "-c", "exec " + shlex.join(args)],
                    input=json.dumps(
                        {
                            "hook_event_name": event,
                            "session_id": nonce,
                            "prompt_id" if tool == "claude" else "turn_id": nonce,
                        }
                    ),
                    text=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise SetupError("writer check timed out") from None
            if completed.returncode != 0:
                raise SetupError("writer command failed")
            data = decode(read_bytes(output))
            ts = data.get("ts")
            if (
                data.get("tool") != tool
                or data.get("state") != state
                or data.get("session") != nonce
                or not isinstance(ts, (float, int))
                or isinstance(ts, bool)
                or not started <= ts <= time.time() + 5
            ):
                raise SetupError("writer output missing, stale or incorrect")
            if (
                data.get("activity_schema") != 2
                or data.get("fact_committed") is not True
                or not any(
                    row["id"] == data.get("fact_id")
                    for row in read_facts(output, tool)["facts"]
                )
            ):
                raise SetupError("writer fact output missing or incorrect")


def remove_owned(data: dict, tool: str, created: list) -> None:
    hooks = data.get("hooks", {})
    for event, groups in list(hooks.items()):
        for group in groups:
            group["hooks"] = [h for h in group["hooks"] if not owned(h, tool)]
        # Indices/fields cannot prove who created an empty group after edits.
        # Selective cleanup preserves all empty scaffolding, including user groups.


def reconcile(data: dict, tool: str, command: str, record: dict) -> tuple[dict, list]:
    try:
        validate_installed(data, tool, command)
        return data, record.get("groups", [])
    except SetupError:
        pass  # Reconciliation repairs missing/edited owned entries only.
    remove_owned(data, tool, record.get("groups", []))
    hooks = data.setdefault("hooks", {})
    groups_created = []
    for event, group in required(tool, command).items():
        groups = hooks.setdefault(event, [])
        groups_created.append(
            {
                "event": event,
                "index": len(groups),
                "fields": {k: v for k, v in group.items() if k != "hooks"},
            }
        )
        groups.append(group)
    return data, groups_created


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "check", "uninstall"))
    parser.add_argument("--tool", choices=("claude", "codex", "all"), default="all")
    parser.add_argument("--home", type=Path, default=None)
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--python", default=None)
    parser.add_argument("--writer", default=None)
    args = parser.parse_args(argv)
    if args.action != "install" and (args.python or args.writer):
        parser.error("--python and --writer are install options")
    home = Path(os.path.realpath((args.home or Path.home()).expanduser()))
    state_dir = Path(
        os.path.realpath((args.state_dir or home / ".taskpaw").expanduser())
    )
    tools = ("claude", "codex") if args.tool == "all" else (args.tool,)
    prepared = []
    current_tool = "all"
    try:
        # Preflight every requested tool before any settings edits.
        for tool in tools:
            current_tool = tool
            if args.action != "uninstall":
                shell_for(tool)
            if (
                tool == "codex"
                and args.action != "uninstall"
                and os.environ.get("CODEX_HOME")
                and Path(os.path.realpath(Path(os.environ["CODEX_HOME"]).expanduser()))
                != Path(os.path.realpath(home / ".codex"))
            ):
                raise SetupError(
                    "nondefault CODEX_HOME: use manual configuration; default installer cannot verify wiring"
                )
            path = Path(os.path.realpath(home / f".{tool}")) / (
                "settings.json" if tool == "claude" else "hooks.json"
            )
            raw = read_bytes(path)
            data = settings(raw)
            record_path = state_dir / "hook-setup" / f"{tool}.json"
            record_raw = read_bytes(record_path)
            record = decode(record_raw)
            if record and record.get("target") != str(path):
                raise SetupError(
                    "undo record target differs; use the original home/state directory"
                )
            command = render_command(
                str(Path(args.python or sys.executable).absolute()),
                str(Path(args.writer or WRITER).absolute()),
                tool,
                str(state_dir / f"agent-activity-{tool}.json"),
                windows=WINDOWS,
            )
            if args.action == "check":
                command = record.get("command", command)
                validate_installed(data, tool, command)
                verify_writer(command, tool, state_dir)
            elif args.action == "install":
                if data.get("disableAllHooks") or data.get("disabled"):
                    raise SetupError("hooks disabled; enable them before installation")
                verify_writer(command, tool, state_dir)
            prepared.append(
                (tool, path, raw, data, record_path, record_raw, record, command)
            )
    except (SetupError, OSError) as exc:
        print(
            f"{current_tool}: {exc if isinstance(exc, SetupError) else 'local I/O failed'}"
        )
        return 1
    failed = False
    for tool, path, raw, data, record_path, record_raw, record, command in prepared:
        saved = None
        updated: bytes | None
        try:
            if args.action == "install":
                before = encode(data)
                for groups in data.get("hooks", {}).values():
                    if any(
                        "activity_writer.py" in h.get("command", "")
                        and not owned(h, tool)
                        for g in groups
                        for h in g["hooks"]
                    ):
                        print(
                            f"{tool}: existing unmarked TaskPaw hook preserved; duplicate writes possible"
                        )
                        break
                data, groups = reconcile(data, tool, command, record)
                updated = encode(data)
                if updated != before:
                    saved = backup(tool, raw, state_dir)
                    atomic_write(path, updated, raw)
                    new_record = {
                        **record,
                        "target": str(path),
                        "last_hash": digest(updated),
                        "groups": groups,
                        "command": command,
                    }
                    if not record:
                        new_record.update(
                            original_exists=raw is not None,
                            baseline=str(saved) if saved else None,
                            baseline_hash=digest(raw),
                        )
                    elif digest(raw) != record.get("last_hash"):
                        # External edits revoke whole-file restoration, including
                        # deletion of a file that was originally absent.
                        for key in ("original_exists", "baseline", "baseline_hash"):
                            new_record.pop(key, None)
                    private_dir(record_path.parent)
                    atomic_write(record_path, encode(new_record), record_raw)
                elif not record:
                    # Settings already correct, but a lost/failed undo record
                    # cannot invent original absence or adopt an old backup.
                    new_record = {
                        "target": str(path),
                        "last_hash": digest(raw),
                        "groups": groups,
                        "command": command,
                    }
                    private_dir(record_path.parent)
                    atomic_write(record_path, encode(new_record), record_raw)
                validate_installed(settings(read_bytes(path)), tool, command)
                verify_writer(command, tool, state_dir)
            elif args.action == "uninstall":
                updated = raw
                restore = "original_exists" in record and digest(raw) == record.get(
                    "last_hash"
                )
                if restore and record.get("original_exists"):
                    try:
                        baseline = record.get("baseline")
                        if not isinstance(baseline, str):
                            raise SetupError("baseline unavailable")
                        updated = read_bytes(Path(baseline))
                        if updated is None or digest(updated) != record.get(
                            "baseline_hash"
                        ):
                            raise SetupError("baseline unavailable")
                    except (OSError, SetupError):
                        restore = False
                        updated = raw
                        print(f"{tool}: baseline unavailable; selective uninstall")
                elif restore:
                    updated = None
                if not restore:
                    before = encode(data)
                    remove_owned(data, tool, record.get("groups", []))
                    if encode(data) != before:
                        updated = encode(data)
                if updated != raw:
                    saved = backup(tool, raw, state_dir)
                    atomic_write(path, updated, raw)
                if record_raw is not None:
                    atomic_write(record_path, None, record_raw)
            print(
                f"{tool}: {args.action} succeeded"
                + (f"; backup: {saved}" if saved else "")
            )
            if tool == "codex" and args.action != "uninstall":
                print(
                    "writer verified; Codex trust/dispatch not verified. Open interactive Codex, run /hooks, review and trust the new TaskPaw definitions; repeat after changing commands."
                )
        except (SetupError, OSError) as exc:
            failed = True
            print(
                f"{tool}: {exc if isinstance(exc, SetupError) else 'local I/O failed'}"
                + (f"; backup retained: {saved}" if saved else "")
            )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
