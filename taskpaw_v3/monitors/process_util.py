"""Shared process enumeration for monitor plugins (psutil).

Public so plugins don't reach into each other's privates: `process` and
`dev_activity` both use this. `scan_matches` does ONE `process_iter` sweep and
tests many precompiled patterns per process (O(processes), not O(patterns ×
processes)).
"""

from __future__ import annotations

import math
import ntpath
import os
import re
import sys

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None


def scan_matches(
    patterns: dict[str, "re.Pattern[str]"], search_cmdline: bool = True
) -> dict[str, bool]:
    """One sweep → {key: matched} for each precompiled regex in `patterns`.

    Raises RuntimeError if psutil is unavailable (caller decides how to degrade).
    """
    found = {k: False for k in patterns}
    if not patterns:
        return found
    if psutil is None:
        raise RuntimeError("psutil not available")
    fields = ["name", "cmdline"] if search_cmdline else ["name"]
    for proc in psutil.process_iter(fields):
        if all(found.values()):
            break  # every pattern already matched — stop early
        try:
            info = proc.info
            name = info.get("name") or ""
            cmd = " ".join(info.get("cmdline") or []) if search_cmdline else ""
            for key, rx in patterns.items():
                if found[key]:
                    continue
                if rx.search(name) or (cmd and rx.search(cmd)):
                    found[key] = True
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            # Process vanished / inaccessible mid-iteration — skip, don't let a
            # transient race degrade a healthy scan.
            continue
    return found


def scan_one(rx: "re.Pattern[str]", search_cmdline: bool = True) -> bool:
    """True if any running process matches the precompiled regex (one sweep)."""
    return scan_matches({"_": rx}, search_cmdline)["_"]


# Activity observation is deliberately separate from generic full-command regexes.
WINDOWS = sys.platform == "win32"
_MAX_SUBTREE = 500
_MAX_PROCESSES = 8192
_MAX_ANCESTORS = 32


def _basename(value: str) -> str:
    name = ntpath.basename(value) if WINDOWS else os.path.basename(value)
    return name.lower().removesuffix(".exe") if WINDOWS else name


def _identity(d: dict, patterns: dict) -> str | None:
    exe = d.get("exe") or ""
    argv = d.get("cmdline") or []
    identity = _basename(exe or (argv[0] if argv else "") or d.get("name") or "")
    # Desktop apps/helpers cannot borrow a CLI identity from argv or name.
    bundle_path = exe.replace("\\", "/").lower()
    if "/claude.app/" in bundle_path or (
        "/chatgpt.app/" in bundle_path and identity != "codex"
    ):
        return None
    if identity in {"claude", "codex", "kimi"}:
        return identity
    for candidate in [argv[0] if argv else "", d.get("name") or ""]:
        tool = _basename(candidate)
        if tool in {"claude", "codex", "kimi"}:
            return tool
    # Interpreter launchers expose their entry point after argv[0]. Only these
    # exact script/module identities are accepted; never scan other arguments.
    launcher = _basename(argv[0]) if argv else ""
    python = re.fullmatch(r"python(?:\d+(?:\.\d+)?)?", launcher)
    if (launcher in {"node", "nodejs"} or python) and len(argv) > 1:
        script = _basename(argv[1])
        if script in {"claude", "codex", "kimi"}:
            return script
        if script == "kimi-cli" or ("/" + argv[1].replace("\\", "/")).endswith(
            "/@moonshot-ai/kimi-code/dist/main.mjs"
        ):
            return "kimi"
        if python and argv[1:3] == ["-m", "kimi_cli"]:
            return "kimi"
    editor_names = {"code", "Code", "Visual Studio Code", "Code Helper"}
    editor_names.update(f"Code Helper ({r})" for r in ("GPU", "Plugin", "Renderer"))
    if WINDOWS:
        editor_names = {n.lower() for n in editor_names}
    is_editor = identity in editor_names
    # macOS's main executable is Electron; require the actual VS Code bundle.
    if identity == "Electron" and "/Visual Studio Code.app/Contents/" in exe:
        is_editor = True
    if is_editor and exe and identity.startswith("Code Helper"):
        is_editor = "/Visual Studio Code.app/Contents/" in exe
    if is_editor:
        return "vscode"
    for candidate in [exe, argv[0] if argv else "", d.get("name") or ""]:
        for tool, rx in patterns.items():
            if tool != "vscode" and rx is not None and rx.search(_basename(candidate)):
                return tool
    return None


def common_host(roots: list[dict]) -> str:
    hosts = {r["host"] for r in roots}
    if not hosts or "unknown" in hosts:
        return "unknown"
    return next(iter(hosts)) if len(hosts) == 1 else "mixed"


def _host(pid: int, records: dict[int, dict], identities: dict[int, str]) -> str:
    seen = {pid}
    for _ in range(_MAX_ANCESTORS):
        child = records[pid]
        parent = child.get("ppid")
        if parent == 0:
            return "other"
        if parent in seen or parent not in records:
            return "unknown"
        ancestor = records[parent]
        if (
            ancestor["created"] is None
            or child["created"] is None
            or ancestor["created"] > child["created"]
        ):
            return "unknown"
        if identities.get(parent) == "vscode":
            return "vscode"
        seen.add(parent)
        pid = parent
    return "unknown"


def scan_activity(patterns: dict[str, re.Pattern[str] | None]) -> dict[str, dict]:
    """One bounded sweep. Paths/argv are discarded after identity classification.

    `cpus` is keyed by (pid, creation time), with nearest AI root ownership.
    Inaccessible/truncated data is unavailable, never evidence of zero CPU.
    """
    result: dict[str, dict] = {
        t: {
            "present": False,
            "cpu_seconds": 0.0,
            "cpus": {},
            "roots": [],
            "complete": True,
            "limited": False,
            "errors": [],
        }
        for t in patterns
    }
    if psutil is None:
        raise RuntimeError("unavailable")
    records: dict[int, dict] = {}
    identities: dict[int, str] = {}
    errors: list[str] = []
    limited = False
    fields = ["pid", "ppid", "exe", "name", "cmdline", "create_time", "cpu_times"]
    try:
        for index, proc in enumerate(psutil.process_iter(fields)):
            if index >= _MAX_PROCESSES:
                limited = True
                break
            try:
                d = proc.info
                pid = d.get("pid")
                if not isinstance(pid, int):
                    continue
                created = d.get("create_time")
                if not isinstance(created, (int, float)) or not math.isfinite(created):
                    created = None
                cpu_times = d.get("cpu_times")
                cpu = None
                if cpu_times is not None:
                    value = float(cpu_times.user) + float(cpu_times.system)
                    if math.isfinite(value) and value >= 0:
                        cpu = value
                records[pid] = {
                    "ppid": d.get("ppid"),
                    "created": created,
                    "cpu": cpu,
                    "process": proc if cpu is None else None,
                }
                tool = _identity(d, patterns)
                if tool:
                    identities[pid] = tool
            except psutil.AccessDenied:
                errors.append("denied")
            except (psutil.NoSuchProcess, psutil.ZombieProcess):
                continue
    except (OSError, psutil.AccessDenied):
        errors.append("denied")
    children: dict[int, list[int]] = {}
    for pid, d in records.items():
        children.setdefault(d["ppid"], []).append(pid)
    for pid, tool in identities.items():
        if tool not in result:
            continue
        out = result[tool]
        out["present"] = True
        if tool == "vscode":
            continue
        root = {
            "pid": pid,
            "created": records[pid]["created"],
            "host": _host(pid, records, identities),
        }
        out["roots"].append(root)
        seen: set[int] = set()
        queue = [pid]
        while queue and len(seen) <= _MAX_SUBTREE:
            cur = queue.pop()
            if cur in seen:
                continue
            # Nested AI roots own their subtree; editor core never belongs to AI.
            if cur != pid and cur in identities:
                continue
            seen.add(cur)
            d = records[cur]
            exited = False
            if cur != pid and d["cpu"] is None:
                # attrs substitutes None for both denied and zombie CPU reads.
                # Only confirmed descendant exit races can be omitted safely.
                try:
                    exited = d["process"].status() == psutil.STATUS_ZOMBIE
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    exited = True
                except psutil.AccessDenied:
                    exited = False
            if not exited and (d["created"] is None or d["cpu"] is None):
                out["complete"] = False
                out["errors"].append("unavailable")
            elif not exited:
                out["cpus"][(cur, d["created"])] = (d["cpu"], (pid, root["created"]))
                out["cpu_seconds"] += d["cpu"]
            for child in children.get(cur, []):
                c = records[child]
                if (
                    d["created"] is None
                    or c["created"] is None
                    or c["created"] < d["created"]
                ):
                    out["complete"] = False
                    continue
                queue.append(child)
        if queue:
            out["limited"] = True
            out["complete"] = False
    for out in result.values():
        out["errors"] = list(dict.fromkeys(out["errors"] + errors))
        out["limited"] |= limited
        out["complete"] &= not limited and not errors
    return result


def cpu_percents(
    prev: dict, prev_mono: float, sample: dict[str, dict], now_mono: float
) -> tuple[dict[str, float], dict]:
    """Only identities in consecutive complete samples contribute to a delta."""
    elapsed = now_mono - prev_mono
    percents: dict[str, float] = {}
    new_prev: dict = {}
    for tool, s in sample.items():
        s["root_cpu"] = {}
        if not s.get("present") or not s.get("complete") or tool == "vscode":
            continue
        cpus = s["cpus"]
        new_prev[tool] = cpus
        old = prev.get(tool, {})
        if elapsed <= 0:
            continue
        # A newly created root cannot inherit the lifetime CPU of its children.
        roots = {(r["pid"], r["created"]) for r in s["roots"]}
        valid_roots = roots & old.keys() & cpus.keys()
        s["cpu_complete"] = roots == valid_roots
        for identity, (cpu, root) in cpus.items():
            if identity in old and root in valid_roots and old[identity][1] == root:
                delta = max(0.0, cpu - old[identity][0]) * 100.0 / elapsed
                s["root_cpu"][root] = s["root_cpu"].get(root, 0.0) + delta
        if s["root_cpu"]:
            percents[tool] = sum(s["root_cpu"].values())
    return percents, new_prev
