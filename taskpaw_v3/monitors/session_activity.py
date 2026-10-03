"""Bounded session metadata observation. Session contents are never opened."""

from __future__ import annotations

import math
import os
import stat
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING

import psutil

from taskpaw_v3.monitors.process_util import common_host

if TYPE_CHECKING:
    from taskpaw_v3.monitors.plugins.dev_activity import DevActivityConfig

WINDOWS = sys.platform == "win32"


def safe_path(path: Path) -> bool:
    """Reject symlinks and Windows reparse points in every existing component."""
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            return False
    return True


def eligible(tool: str, path: Path) -> bool:
    name = path.name.lower() if WINDOWS else path.name
    return name.endswith(".jsonl") and (tool != "codex" or name.startswith("rollout-"))


class SessionActivity:
    def __init__(
        self, cfg: DevActivityConfig, stop_event: threading.Event | None = None
    ):
        self.cfg = cfg
        self.stop_event = stop_event or threading.Event()
        self.closed = False
        self.watermarks: dict[str, float] = {}
        self.completed: dict[str, tuple[float, frozenset]] = {}
        self.priority_offset: dict[str, int] = {}
        home = Path.home()
        defaults = {
            "claude": [home / ".claude/projects"],
            "codex": [home / ".codex/sessions"],
            "kimi": [home / ".kimi-code/sessions", home / ".kimi/sessions"],
        }
        self.roots = {
            t: [
                Path(os.path.realpath(Path(p).expanduser()))
                for p in cfg.session_roots.get(t, defaults.get(t, []))
            ]
            for t in dict.fromkeys(cfg.tools)
            if t != "vscode"
        }
        self.candidates: dict[str, dict[Path, tuple[float, int, float]]] = {
            t: {} for t in self.roots
        }
        self.queues: dict[str, deque] = {}
        self.cursors: dict[str, tuple] = {}
        self.next_scan: dict[str, float] = {}
        self.cycle_errors: dict[str, list[str]] = {}
        self.cycle_limited: dict[str, bool] = {}
        self.handle_offset = 0

    def close(self) -> None:
        self.closed = True
        for cursor, _ in self.cursors.values():
            cursor.close()
        self.cursors.clear()
        self.queues.clear()

    def _metadata(
        self, tool: str, path: Path, now: float, mono: float, errors: list[str]
    ) -> tuple[float, int, float] | None:
        try:
            if self.closed or self.stop_event.is_set():
                return None
            if not eligible(tool, path) or not safe_path(path):
                return None
            info = path.stat(follow_symlinks=False)
            if self.stop_event.is_set():
                return None
            if not stat.S_ISREG(info.st_mode):
                return None
            age = now - info.st_mtime
            if not math.isfinite(age) or age < -5:
                errors.append("invalid_time")
                return None
            return max(0.0, age), info.st_mtime_ns, mono
        except FileNotFoundError:
            return None
        except PermissionError:
            errors.append("denied")
        except OSError:
            errors.append("unavailable")
        return None

    def _discover(
        self, tool: str, now: float, mono: float, deadline: float
    ) -> tuple[list[str], bool, bool]:
        cache = self.candidates[tool]
        errors: list[str] = []
        current_date = set(time.strftime("%Y %m %d", time.localtime(now)).split())
        active_parents = {p.parent for p in cache}
        if self.closed or self.stop_event.is_set():
            return [], False, False
        for path, previous in list(cache.items()):
            candidate_errors: list[str] = []
            item = self._metadata(tool, path, now, mono, candidate_errors)
            errors.extend(candidate_errors)
            if item is not None:
                cache[path] = item
            elif not candidate_errors or mono - previous[2] >= 600:
                cache.pop(path, None)
            else:
                # Keep identity for retry, but never use an unvalidated age.
                cache[path] = (math.inf, previous[1], previous[2])
        if tool not in self.queues and mono >= self.next_scan.get(tool, 0):
            roots = self.roots[tool]
            active = sorted(active_parents - set(roots))
            capacity = 256 - len(roots)
            offset = self.priority_offset.get(tool, 0) % max(1, len(active))
            active = active[offset:] + active[:offset]
            if len(active) > capacity:
                self.priority_offset[tool] = offset + 1
            # Keep root discovery alive when the cached priority set fills the
            # queue; rotate equal-priority omissions across completed cycles.
            seeds = [*active[:capacity], *roots]
            self.queues[tool] = deque(
                (
                    p,
                    min(
                        len(p.parts) - len(r.parts)
                        for r in roots
                        if p.is_relative_to(r)
                    ),
                )
                for p in seeds[:256]
            )
            self.cycle_errors[tool] = []
            self.cycle_limited[tool] = len(active) > capacity
        entries = directories = 0
        queue = self.queues.get(tool)
        limited = False
        while queue is not None and (queue or tool in self.cursors):
            if self.closed or self.stop_event.is_set():
                break
            if entries >= 512 or directories >= 64 or time.monotonic() >= deadline:
                # Yield without exclusions; the saved cursor keeps this incomplete.
                break
            if tool not in self.cursors:
                path, depth = queue.popleft()
                try:
                    if not safe_path(path) or not path.is_dir():
                        if path.exists():
                            errors.append("invalid_root")
                        continue
                    cursor = os.scandir(path)
                    if self.closed or self.stop_event.is_set():
                        cursor.close()
                        break
                    self.cursors[tool] = (cursor, depth)
                    directories += 1
                except FileNotFoundError:
                    continue
                except PermissionError:
                    errors.append("denied")
                    continue
                except OSError:
                    errors.append("unavailable")
                    continue
            cursor, depth = self.cursors[tool]
            try:
                entry = next(cursor)
            except StopIteration:
                cursor.close()
                del self.cursors[tool]
                continue
            except OSError:
                errors.append("unavailable")
                cursor.close()
                del self.cursors[tool]
                continue
            if self.closed or self.stop_event.is_set():
                break
            entries += 1
            path = Path(entry.path)
            try:
                info = entry.stat(follow_symlinks=False)
                if self.closed or self.stop_event.is_set():
                    break
                if (
                    stat.S_ISLNK(info.st_mode)
                    or getattr(info, "st_file_attributes", 0) & 0x400
                ):
                    continue
                if stat.S_ISDIR(info.st_mode):
                    priority = path in active_parents or path.name in current_date
                    if depth >= 8:
                        self.cycle_limited[tool] = True
                    else:
                        if len(queue) >= 256:
                            self.cycle_limited[tool] = True
                            if not priority:
                                continue
                            # Priority must be admitted before overflow rejection.
                            lower = next(
                                (
                                    i
                                    for i in range(len(queue) - 1, -1, -1)
                                    if queue[i][0] not in active_parents
                                    and queue[i][0].name not in current_date
                                ),
                                None,
                            )
                            if lower is None:
                                lower = self.priority_offset.get(tool, 0) % len(queue)
                                self.priority_offset[tool] = lower + 1
                            del queue[lower]
                        if priority:
                            queue.appendleft((path, depth + 1))
                        else:
                            queue.append((path, depth + 1))
                elif stat.S_ISREG(info.st_mode) and eligible(tool, path):
                    item = self._metadata(tool, path, now, mono, errors)
                    if item is not None:
                        cache[path] = item
                        if len(cache) > self.cfg.session_max_files:
                            oldest = min(cache, key=lambda p: (cache[p][1], str(p)))
                            del cache[oldest]
            except FileNotFoundError:
                continue
            except PermissionError:
                errors.append("denied")
            except OSError:
                errors.append("unavailable")
        self.cycle_errors.setdefault(tool, []).extend(errors)
        self.cycle_errors[tool] = list(dict.fromkeys(self.cycle_errors[tool]))
        complete = queue is None or (not queue and tool not in self.cursors)
        if complete and queue is not None:
            del self.queues[tool]
            self.next_scan[tool] = mono + self.cfg.session_scan_interval_seconds
        limited |= self.cycle_limited.get(tool, False)
        errors = list(dict.fromkeys(errors + self.cycle_errors.get(tool, [])))
        return errors, limited, complete and not errors and not limited

    def sample(
        self, snapshot: dict[str, dict], skip: set[str], now: float
    ) -> dict[str, dict]:
        if self.closed or self.stop_event.is_set():
            return {}
        mono = time.monotonic()
        deadline = mono + 0.1
        result: dict[str, dict] = {}
        pending: list[tuple[str, dict]] = []
        for tool, roots in self.roots.items():
            if self.stop_event.is_set():
                return {}
            data = snapshot.get(tool, {})
            if tool in skip or not data.get("present") or not data.get("roots"):
                continue
            live_roots = [
                r
                for r in data["roots"]
                if isinstance(r.get("created"), (int, float))
                and not isinstance(r["created"], bool)
                and math.isfinite(r["created"])
            ]
            identity_unavailable = len(live_roots) != len(data["roots"])
            errors, limited, complete = (
                self._discover(tool, now, mono, deadline)
                if roots and live_roots
                else ([], False, True)
            )
            if identity_unavailable:
                errors.append("unavailable")
                complete = False
            root_ids = frozenset((r["pid"], r["created"]) for r in live_roots)
            if complete and data.get("complete", False):
                self.completed[tool] = (mono, root_ids)
            elif errors or limited or identity_unavailable:
                self.completed.pop(tool, None)
            previous = self.completed.get(tool)
            if (
                previous is not None
                and previous[1] == root_ids
                and mono - previous[0]
                <= max(
                    2 * self.cfg.session_scan_interval_seconds,
                    2 * self.cfg.poll_interval,
                )
                and not errors
                and not limited
            ):
                complete = True
            elif previous is not None:
                self.completed.pop(tool, None)
            age = (
                min((v[0] for v in self.candidates[tool].values()), default=math.inf)
                if live_roots
                else math.inf
            )
            result[tool] = {
                "state": None,
                "age_s": None,
                "host": "unknown" if identity_unavailable else common_host(live_roots),
                "vscode_state": None,
                "errors": errors,
                "limited": limited,
                "complete": complete and data.get("complete", False),
                "mtime_age": age,
                "write_age": min(
                    (
                        v[0]
                        for v in self.candidates[tool].values()
                        if v[1] / 1e9 > self.watermarks.get(tool, -math.inf)
                    ),
                    default=math.inf,
                ),
                "positive_handles": [],
            }
            if not WINDOWS and roots:
                pending.extend((tool, r) for r in live_roots)
        if pending:
            start = self.handle_offset % len(pending)
            ordered = pending[start:] + pending[:start]
            self.handle_offset = (start + 16) % len(pending)
            for tool, root in ordered[16:]:
                result[tool]["limited"] = True
                result[tool]["complete"] = False
            for tool, root in ordered[:16]:
                if self.stop_event.is_set():
                    return {}
                out = result[tool]
                try:
                    proc = psutil.Process(root["pid"])
                    if root["created"] is None or proc.create_time() != root["created"]:
                        out["complete"] = False
                        continue
                    positives = []
                    opened_files = proc.open_files()
                    if self.stop_event.is_set():
                        return {}
                    for index, opened in enumerate(opened_files):
                        if self.stop_event.is_set():
                            return {}
                        if index >= 256:
                            out["limited"] = True
                            out["complete"] = False
                            break
                        path = Path(os.path.abspath(opened.path))
                        # Resolve directory aliases, retaining the leaf for no-follow checks.
                        path = Path(os.path.realpath(path.parent)) / path.name
                        if not any(path.is_relative_to(r) for r in self.roots[tool]):
                            continue
                        item = self._metadata(tool, path, now, mono, out["errors"])
                        if item is not None and item[1] / 1e9 > self.watermarks.get(
                            tool, -math.inf
                        ):
                            positives.append((item[0], root["host"]))
                    if self.stop_event.is_set():
                        return {}
                    # Revalidate identity after the native call; never reuse positives.
                    if psutil.Process(root["pid"]).create_time() == root["created"]:
                        out["positive_handles"].extend(positives)
                    else:
                        out["complete"] = False
                except (psutil.NoSuchProcess, psutil.ZombieProcess):
                    out["complete"] = False
                except (psutil.AccessDenied, PermissionError):
                    out["errors"].append("denied")
                    out["complete"] = False
                except (OSError, NotImplementedError):
                    out["errors"].append("unavailable")
                    out["complete"] = False
        for out in result.values():
            handles = out.pop("positive_handles")
            age = out.pop("mtime_age")
            write_age = out.pop("write_age")
            if handles:
                age, _ = min(handles)
                out["state"] = "busy"
                out["host"] = common_host([{"host": host} for _, host in handles])
                if any(host == "vscode" for _, host in handles):
                    out["vscode_state"] = "busy"
            elif write_age <= self.cfg.session_busy_seconds:
                age = write_age
                out["state"] = "busy"
            elif (
                age <= self.cfg.session_idle_seconds
                and out["complete"]
                and not out["errors"]
            ):
                out["state"] = "idle"
            if out["state"] is not None:
                out["age_s"] = age
                if out["host"] == "vscode":
                    out["vscode_state"] = out["state"]
            out["errors"] = list(dict.fromkeys(out["errors"]))
        return result
