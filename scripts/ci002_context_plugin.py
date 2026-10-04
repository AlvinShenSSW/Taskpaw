"""Temporary metadata-only observer; never copied into the product checkout."""
import builtins
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time

import pytest

TARGET = "test_reinstall_after_external_edit_revokes_whole_file_restore"
BOUNDARIES = ("read_bytes", "backup", "private_dir", "atomic_write", "verify_writer")
MAX_RECORDS, MAX_CHAIN, MAX_FRAMES = 256, 3, 32
CASES, REPORTS = {}, []
COLLECTION = {}
STAGES = {396: "preflight_settings_read", 399: "preflight_undo_read",
          419: "preflight_verify_writer", 449: "backup", 450: "settings_atomic_write",
          469: "undo_private_dir", 470: "undo_atomic_write", 480: "undo_private_dir",
          481: "undo_atomic_write", 482: "final_settings_read_validate",
          483: "final_verify_writer", 514: "message_publication", 515: "message_publication",
          518: "message_publication", 519: "message_publication", 520: "message_publication",
          523: "postbackup_sanitize", 525: "postbackup_sanitize"}


def key_for(item):
    if getattr(item, "originalname", None) != TARGET or not hasattr(item, "callspec"):
        return None
    params = item.callspec.params
    if params.get("changed_path") != "writer":
        return None
    if params.get("tool") not in ("claude", "codex") or type(params.get("existing")) is not bool:
        raise ValueError("unexpected_fixed_target")
    return params["tool"] + ("-existing" if params["existing"] else "-new")


def state_snapshot(code):
    monitoring = getattr(sys, "monitoring", None)
    masks = None if monitoring is None else tuple(
        (monitoring.get_tool(i), monitoring.get_events(i), monitoring.get_local_events(i, code))
        for i in range(6))
    return sys.gettrace(), threading.gettrace(), masks


def unchanged(before, after):
    return before[0] is after[0] and before[1] is after[1] and before[2] == after[2]


def coverage_metadata(config):
    import coverage
    plugin = config.pluginmanager.getplugin("_cov")
    controller = getattr(plugin, "cov_controller", None)
    cov = getattr(controller, "cov", None)
    collector = getattr(cov, "_collector", None)
    core = getattr(collector, "core", None)
    trace_class = getattr(core, "tracer_class", None)
    name = getattr(trace_class, "__name__", None)
    if name is None:
        tracers = getattr(collector, "tracers", ())
        name = type(tracers[0]).__name__ if tracers else None
    return {"version": coverage.__version__, "core": name if name in
            ("CTracer", "PyTracer", "SysMonitor") else "unknown",
            "branch": getattr(getattr(cov, "config", None), "branch", None)}


def observe_third(module, original_main, record, args, kwargs, modules, approved):
    """Pure boundary control: exactly one original call, no tracing setters or I/O."""
    saved = {name: getattr(module, name) for name in BOUNDARIES}
    had_print = "print" in vars(module)
    original_print = vars(module).get("print", builtins.print)
    owner = threading.get_ident()
    started = time.monotonic()
    before = None

    def invalid():
        record["observer_valid"] = False
        record["observer_errors"] += 1

    def add(exc, boundary):
        if threading.get_ident() != owner:
            return
        try:
            if len(record["events"]) >= MAX_RECORDS:
                record["dropped_records"] += 1
                return
            cursor, stage = sys._getframe(), "third_install_other"
            for _ in range(MAX_FRAMES):
                if cursor is None:
                    break
                if cursor.f_code is original_main.__code__:
                    stage = STAGES.get(cursor.f_lineno, "third_install_other")
                    break
                cursor = cursor.f_back
            rows, seen, current, frames_left = [], set(), exc, MAX_FRAMES
            truncated = False
            for _ in range(MAX_CHAIN):
                if current is None or id(current) in seen:
                    break
                seen.add(id(current))
                name = approved.get(type(current))
                if name is None:
                    name = "OSErrorSubclass" if isinstance(current, OSError) else (
                        "sqlite3.ErrorSubclass" if isinstance(current, sqlite3.Error) else "unlisted_exception")
                row = {"class": name, "frames": []}
                for field in ("errno", "winerror", "sqlite_errorcode"):
                    value = getattr(current, field, None)
                    if type(value) is int:
                        row[field] = value
                tb = current.__traceback__
                while tb is not None and frames_left:
                    frames_left -= 1
                    code = tb.tb_frame.f_code
                    label = modules.get(code.co_filename)
                    if label is not None:
                        function = code.co_name
                        if not function.isidentifier() and function not in ("<lambda>", "<genexpr>"):
                            function = "internal"
                        row["frames"].append({"module": label, "function": function[:64], "line": tb.tb_lineno})
                    tb = tb.tb_next
                truncated |= tb is not None
                rows.append(row)
                current = current.__cause__ if current.__cause__ is not None else current.__context__
            truncated |= current is not None and id(current) not in seen
            if truncated:
                record["truncated_events"] += 1
            record["events"].append({"boundary": boundary, "stage": stage,
                "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
                "exceptions": rows, "truncated": truncated})
        except Exception:
            invalid()

    def wrapper(original, boundary):
        def transparent(*call_args, **call_kwargs):
            try:
                return original(*call_args, **call_kwargs)
            except tuple(approved):
                add(sys.exception(), boundary)
                raise
        return transparent

    try:
        try:
            before = state_snapshot(original_main.__code__)
            record["prior_trace_present"] = before[0] is not None
            for name, original in saved.items():
                setattr(module, name, wrapper(original, name))
            module.print = wrapper(original_print, "print")
        except Exception:
            invalid()
        result = original_main(*args, **kwargs)
        record["third_result"] = result if type(result) is int else None
        return result
    finally:
        try:
            for name, original in saved.items():
                setattr(module, name, original)
            if had_print:
                module.print = original_print
            elif "print" in vars(module):
                delattr(module, "print")
            record["bindings_restored"] = all(getattr(module, n) is v for n, v in saved.items())
            record["print_restored"] = ("print" in vars(module)) == had_print and (
                not had_print or module.print is original_print)
            record["tracing_unchanged"] = before is not None and unchanged(before, state_snapshot(original_main.__code__))
            if not all(record[k] for k in ("bindings_restored", "print_restored", "tracing_unchanged")):
                invalid()
        except Exception:
            invalid()


def new_record(key):
    return {"case": key, "main_calls": 0, "third_call_seen": False, "third_result": None,
            "events": [], "dropped_records": 0, "truncated_events": 0,
            "observer_valid": True, "observer_errors": 0, "bindings_restored": False,
            "print_restored": False, "tracing_unchanged": False, "main_restored": False}


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    key = key_for(item)
    if key is None:
        yield
        return
    module, _ = item.funcargs["setup"]
    product = Path(os.environ["TASKPAW_CI002_PRODUCT"]).resolve()
    assert Path(module.__file__).resolve() == product / "taskpaw_v3/integrations/activity_setup.py"
    from taskpaw_v3.integrations import activity_writer
    assert Path(activity_writer.__file__).resolve() == product / "taskpaw_v3/integrations/activity_writer.py"
    original = module.main
    modules = {original.__code__.co_filename: "activity_setup",
               activity_writer.read_facts.__code__.co_filename: "activity_writer"}
    for library in (sys.modules[Path.__module__], tempfile, shutil, subprocess, os):
        filename = getattr(library, "__file__", None)
        if filename:
            modules[str(Path(filename).resolve())] = library.__name__
    approved = {getattr(builtins, name): name for name in
        ("OSError", "FileNotFoundError", "PermissionError", "FileExistsError",
         "NotADirectoryError", "IsADirectoryError", "TimeoutError", "InterruptedError",
         "BlockingIOError", "ChildProcessError", "ConnectionError", "BrokenPipeError")}
    approved.update({getattr(sqlite3, name): "sqlite3." + name for name in
        ("Error", "InterfaceError", "DatabaseError", "DataError", "OperationalError",
         "IntegrityError", "InternalError", "ProgrammingError", "NotSupportedError")})
    approved[module.SetupError] = "SetupError"
    approved[activity_writer.ActivityStoreError] = "ActivityStoreError"
    record = new_record(key)
    try:
        record["coverage"] = coverage_metadata(item.config)
    except Exception:
        record["coverage"] = {"version": "unknown", "core": "unknown", "branch": None}
        record["observer_valid"] = False
        record["observer_errors"] += 1
    CASES[key] = record

    def main(*args, **kwargs):
        record["main_calls"] += 1
        if record["main_calls"] != 3:
            return original(*args, **kwargs)
        record["third_call_seen"] = True
        return observe_third(module, original, record, args, kwargs, modules, approved)

    module.main = main
    try:
        yield
    finally:
        module.main = original
        record["main_restored"] = module.main is original


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    key = key_for(item)
    if key is not None or report.failed:
        REPORTS.append({"case": key or "other", "phase": report.when, "outcome": report.outcome})


def pytest_collection_finish(session):
    target_indices = {}
    digest = hashlib.sha256()
    for index, item in enumerate(session.items):
        digest.update(item.nodeid.encode("utf-8") + b"\0")
        key = key_for(item)
        if key is not None:
            assert key not in target_indices
            target_indices[key] = index
    COLLECTION.update({"count": len(session.items), "expected_count": 4000,
        "count_matches": len(session.items) == 4000, "order_sha256": digest.hexdigest(),
        "targets": target_indices, "targets_match": set(target_indices) ==
        {"claude-new", "claude-existing", "codex-new", "codex-existing"},
        "historical_order_comparison_available": False})


def pytest_sessionfinish(session, exitstatus):
    payload = {"schema": 2, "exitstatus": int(exitstatus), "collection": COLLECTION,
        "cases": list(CASES.values()), "reports": REPORTS,
        "acceptance": False, "instrumentation": "transparent_boundaries_with_coverage"}
    encoded = (json.dumps(payload, indent=2) + "\n").encode("utf-8")
    # Fixed cap: an oversized receipt preserves only an explicit invalid result.
    if len(encoded) > 2 * 1024 * 1024:
        encoded = b'{"schema":2,"observer_valid":false,"receipt_oversize":true,"acceptance":false}\n'
    Path(os.environ["TASKPAW_CI002_RECEIPT"]).write_bytes(encoded)
