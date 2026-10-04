"""Temporary diagnostic pytest plugin. Never installed into product checkout."""
import builtins
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest

TARGET = "test_reinstall_after_external_edit_revokes_whole_file_restore"
PRODUCT = pathlib.Path(os.environ["TASKPAW_CI002_PRODUCT"]).resolve()
OUTPUT = pathlib.Path(os.environ["TASKPAW_CI002_RECEIPT"]).resolve()
MAX_RECORDS = 256
MAX_CHAIN = 3
MAX_STACK = 32
CASES = {}
REPORTS = []
ALLOWED_CLASSES = {
    getattr(builtins, name): name
    for name in ("OSError", "FileNotFoundError", "PermissionError", "FileExistsError",
                 "NotADirectoryError", "IsADirectoryError", "TimeoutError",
                 "InterruptedError", "BlockingIOError", "ChildProcessError",
                 "ConnectionError", "BrokenPipeError")
}
ALLOWED_CLASSES.update({getattr(sqlite3, name): "sqlite3." + name for name in
                       ("Error", "InterfaceError", "DatabaseError", "DataError",
                        "OperationalError", "IntegrityError", "InternalError",
                        "ProgrammingError", "NotSupportedError")})
SETUP = PRODUCT / "taskpaw_v3/integrations/activity_setup.py"
WRITER = PRODUCT / "taskpaw_v3/integrations/activity_writer.py"
MODULES = {str(SETUP): "activity_setup", str(WRITER): "activity_writer"}
for module in (pathlib, tempfile, shutil, subprocess, os):
    if getattr(module, "__file__", None):
        MODULES[str(pathlib.Path(module.__file__).resolve())] = module.__name__
# Known immutable setup.main call sites; no argument/path/locals inspection.
STAGES = {396: "preflight_settings_read", 399: "preflight_undo_read",
          419: "preflight_verify_writer", 423: "preflight_sanitize",
          449: "backup", 450: "settings_atomic_write", 469: "undo_private_dir",
          470: "undo_atomic_write", 480: "undo_private_dir",
          481: "undo_atomic_write", 482: "final_settings_read_validate",
          483: "final_verify_writer", 523: "postbackup_sanitize"}


def key_for(item):
    assert item.originalname == TARGET
    params = item.callspec.params
    assert params["changed_path"] == "writer"
    assert params["tool"] in ("claude", "codex") and type(params["existing"]) is bool
    return params["tool"] + ("-existing" if params["existing"] else "-new")


def metadata(exc):
    kind = ALLOWED_CLASSES.get(type(exc))
    if kind is None:
        kind = "OSErrorSubclass" if isinstance(exc, OSError) else "sqlite3.ErrorSubclass"
    out = {"class": kind}
    for field in ("errno", "winerror", "sqlite_errorcode"):
        value = getattr(exc, field, None)
        if type(value) is int:
            out[field] = value
    return out


def chain(exc):
    out, seen, current = [], set(), exc
    for _ in range(MAX_CHAIN):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        # Traverse suppressed context too, but record only approved exception types.
        if isinstance(current, (OSError, sqlite3.Error)) or type(current) in ALLOWED_CLASSES:
            out.append(metadata(current))
        current = current.__cause__ if current.__cause__ is not None else current.__context__
    return out


def trace_for(record):
    begun = time.monotonic()

    def add(frame, event, exc=None):
        if len(record["trace"]) >= MAX_RECORDS:
            record["dropped_records"] += 1
            return
        module = MODULES.get(frame.f_code.co_filename)
        if module is None:
            return
        stage, cursor = "third_install_other", frame
        for _ in range(MAX_STACK):
            if cursor is None:
                break
            if MODULES.get(cursor.f_code.co_filename) == "activity_setup" and cursor.f_code.co_name == "main":
                stage = STAGES.get(cursor.f_lineno, "third_install_other")
                break
            cursor = cursor.f_back
        operation = frame.f_code.co_name
        # co_name comes only from immutable product or standard-library code.
        if not operation.isidentifier() and operation not in ("<lambda>", "<genexpr>"):
            operation = "internal"
        row = {"event": event, "module": module, "function": operation[:64],
               "line": frame.f_lineno, "stage": stage,
               "elapsed_ms": round((time.monotonic() - begun) * 1000, 3)}
        if exc is not None:
            row["exceptions"] = chain(exc)
        record["trace"].append(row)

    def tracer(frame, event, arg):
        # Only this calling thread and the third setup.main invocation are traced.
        if frame.f_code.co_filename not in MODULES:
            return None
        frame.f_trace_lines = MODULES.get(frame.f_code.co_filename) == "activity_setup"
        frame.f_trace_opcodes = False
        if event == "line" and frame.f_code.co_name == "main" and frame.f_lineno in STAGES:
            add(frame, "stage")
        elif event == "exception":
            exc = arg[1]
            if isinstance(exc, (OSError, sqlite3.Error)) or type(exc) in ALLOWED_CLASSES:
                add(frame, "exception", exc)
        return tracer

    return tracer


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_call(item):
    key = key_for(item)
    setup_module, _ = item.funcargs["setup"]
    assert pathlib.Path(setup_module.__file__).resolve() == SETUP
    from taskpaw_v3.integrations import activity_writer
    assert pathlib.Path(activity_writer.__file__).resolve() == WRITER
    ALLOWED_CLASSES[setup_module.SetupError] = "SetupError"
    ALLOWED_CLASSES[activity_writer.ActivityStoreError] = "ActivityStoreError"
    original = setup_module.main
    setup_code_path = original.__code__.co_filename
    assert pathlib.Path(setup_code_path).resolve() == SETUP
    MODULES[setup_code_path] = "activity_setup"
    MODULES[activity_writer.read_facts.__code__.co_filename] = "activity_writer"
    record = {"case": key, "main_calls": 0, "third_call_seen": False,
              "third_result": None, "trace": [], "dropped_records": 0,
              "trace_restored": False, "main_restored": False,
              "prior_trace_present": None}
    CASES[key] = record

    def traced_main(*args, **kwargs):
        record["main_calls"] += 1
        if record["main_calls"] != 3:
            return original(*args, **kwargs)
        record["third_call_seen"] = True
        prior = sys.gettrace()
        record["prior_trace_present"] = prior is not None
        # Coverage is disabled; an unexpected trace makes this diagnostic invalid.
        if prior is not None:
            record["instrumentation_unavailable"] = "preexisting_trace"
            return original(*args, **kwargs)
        try:
            sys.settrace(trace_for(record))
            result = original(*args, **kwargs)
            record["third_result"] = result if type(result) is int else None
            return result
        finally:
            sys.settrace(prior)
            record["trace_restored"] = sys.gettrace() is prior

    setup_module.main = traced_main
    try:
        yield
    finally:
        setup_module.main = original
        record["main_restored"] = setup_module.main is original


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    REPORTS.append({"case": key_for(item), "phase": report.when, "outcome": report.outcome})


def pytest_collection_finish(session):
    assert len(session.items) == 4 and {key_for(item) for item in session.items} == {
        "claude-new", "claude-existing", "codex-new", "codex-existing"}


def pytest_sessionfinish(session, exitstatus):
    payload = {"schema": 1, "round": int(os.environ["TASKPAW_CI002_ROUND"]),
               "exitstatus": int(exitstatus), "cases": list(CASES.values()), "reports": REPORTS,
               "limits": "Third installer call only; parent process only; bounded metadata without messages, filenames, arguments or locals. No-reproduction does not close CI002."}
    OUTPUT.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
