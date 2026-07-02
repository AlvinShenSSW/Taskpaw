"""Shared process enumeration for monitor plugins (psutil).

Public so plugins don't reach into each other's privates: `process` and
`dev_activity` both use this. `scan_matches` does ONE `process_iter` sweep and
tests many precompiled patterns per process (O(processes), not O(patterns ×
processes)).
"""

from __future__ import annotations

import re

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


# Cap the per-tool subtree walk so a pathological/looping tree can't wedge a check.
_MAX_SUBTREE = 500


def _cpu_seconds(cpu_times) -> float:
    """user+system CPU seconds from a psutil cpu_times tuple, or 0.0 if unavailable."""
    try:
        return float(cpu_times.user) + float(cpu_times.system)
    except (AttributeError, TypeError, ValueError):
        return 0.0


def scan_activity(
    patterns: dict[str, "re.Pattern[str]"],
) -> dict[str, dict]:
    """One sweep → per-tool observed CPU. For each precompiled `{tool: regex}` this
    finds the matching ROOT processes (name/cmdline) and sums `cpu_times` (user+system
    seconds) over each root's process SUBTREE, so a CLI's tool-execution children count
    too. Returns `{tool: {"present": bool, "cpu_seconds": float}}`.

    Pure external observation (no writes to / no impact on the tools) and same-user
    process info needs no root on macOS. Raises RuntimeError if psutil is unavailable
    (caller degrades); per-process races are swallowed.
    """
    result: dict[str, dict] = {
        k: {"present": False, "cpu_seconds": 0.0} for k in patterns
    }
    if not patterns:
        return result
    if psutil is None:
        raise RuntimeError("psutil not available")

    # One sweep: collect (name, cmdline, cpu_seconds) per pid + a ppid→children index.
    info: dict[int, dict] = {}
    children: dict[int, list[int]] = {}
    for proc in psutil.process_iter(["pid", "ppid", "name", "cmdline", "cpu_times"]):
        try:
            d = proc.info
            pid = d.get("pid")
            if pid is None:
                continue
            info[pid] = {
                "name": d.get("name") or "",
                "cmd": " ".join(d.get("cmdline") or []),
                "cpu": _cpu_seconds(d.get("cpu_times")),
            }
            children.setdefault(d.get("ppid"), []).append(pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    # Roots per tool: every process whose name/cmdline matches the tool's regex.
    roots: dict[str, list[int]] = {k: [] for k in patterns}
    for pid, meta in info.items():
        for key, rx in patterns.items():
            if rx.search(meta["name"]) or (meta["cmd"] and rx.search(meta["cmd"])):
                roots[key].append(pid)

    # Sum cpu over the UNION of each tool's roots' subtrees, counting every pid ONCE —
    # so a matching descendant (e.g. `claude` → `claude-worker`) isn't double-counted
    # through both its own root and its parent's subtree (Codex 外门). BFS is bounded +
    # cycle-safe via the shared `seen` set.
    for key, root_pids in roots.items():
        if not root_pids:
            continue
        result[key]["present"] = True
        seen: set[int] = set()
        queue = list(root_pids)
        while queue and len(seen) < _MAX_SUBTREE:
            cur = queue.pop()
            if cur in seen or cur not in info:
                continue
            seen.add(cur)
            result[key]["cpu_seconds"] += info[cur]["cpu"]
            queue.extend(children.get(cur, ()))
    return result


def cpu_percents(
    prev: dict[str, float],
    prev_mono: float,
    sample: dict[str, dict],
    now_mono: float,
) -> tuple[dict[str, float], dict[str, float]]:
    """Turn two `scan_activity` cpu_seconds snapshots into a per-tool CPU percent.

    `pct = 100 * max(0, cur - prev) / elapsed` — the first sample (no prev, or
    elapsed<=0) yields 0.0, and a drop (a busy child exited between samples) clamps to
    0 rather than going negative. Returns `(percents, new_prev)`; a tool with no root
    (`present` False) is omitted from `percents` (observation unavailable)."""
    elapsed = now_mono - prev_mono
    percents: dict[str, float] = {}
    new_prev: dict[str, float] = {}
    for tool, s in sample.items():
        if not s.get("present"):
            continue
        cur = float(s.get("cpu_seconds", 0.0))
        new_prev[tool] = cur
        if tool in prev and elapsed > 0:
            percents[tool] = 100.0 * max(0.0, cur - prev[tool]) / elapsed
    return percents, new_prev
