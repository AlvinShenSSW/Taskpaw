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
