"""Target-only transparent subprocess observer; receipt contains no raw output."""

import hashlib
import json
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

TARGET = "taskpaw_v3/tests/test_activity.py::test_i216_copied_absolute_writer_concurrent_sessions"
# Fixed constants; do not import sitecustomize (which installs the observer).
FUNCTIONS = frozenset(
    """
state_from_stdin write_activity _write_projection sidecar_path _hash
_identity_field _producer_identity hook_fact _regular _open_store _check_snapshot
_validate_fact _session_end_id _witness _same_fact _covered _read_link _resolve_link
_collapse_verified _proof_for _projection_matches _scope _interrupted
_retain_unknown _expire _retain_refused _admit_fact publish_fact publish_hook
_confirm_session_ends read_facts publish_watermark main <module>
""".split()
)
CLASSES = frozenset(
    """
ActivityStoreError _CapacityRefused OSError FileNotFoundError FileExistsError
PermissionError IsADirectoryError NotADirectoryError ValueError TypeError KeyError
ImportError ModuleNotFoundError RecursionError OverflowError AssertionError
Error DatabaseError OperationalError IntegrityError ProgrammingError
InterfaceError DataError NotSupportedError SystemExit StopIteration
""".split()
)
STATE = {"errors": 0, "children": [], "reports": [], "restored": False}
LOCK = threading.Lock()


def child_metadata(stderr, marker):
    """Strict schema; never serialize untrusted child strings."""
    prefix = "TASKPAW_CI004_" + marker + ":"
    lines = stderr.splitlines()
    tagged = [line[len(prefix) :] for line in lines if line.startswith(prefix)]
    categories = set()
    for line in lines:
        if line.startswith(prefix):
            continue
        categories.add(
            {
                "activity writer: state write failed": "state_write_failed",
                "activity writer: fact write failed": "fact_write_failed",
            }.get(line, "other_stderr")
        )
    trace = None
    if marker:
        if len(tagged) != 1 or len(tagged[0]) > 24000:
            raise ValueError("invalid_trace")
        trace = json.loads(tagged[0])
        if type(trace) is not dict or set(trace) != {
            "valid",
            "errors",
            "dropped",
            "events",
        }:
            raise ValueError("invalid_trace")
        if type(trace["valid"]) is not bool or any(
            type(trace[k]) is not int or not 0 <= trace[k] <= 10000
            for k in ("errors", "dropped")
        ):
            raise ValueError("invalid_trace")
        if type(trace["events"]) is not list or len(trace["events"]) > 64:
            raise ValueError("invalid_trace")
        for event in trace["events"]:
            if type(event) is not dict or set(event) != {
                "class",
                "function",
                "line",
                "errno",
                "winerror",
                "sqlite_errorcode",
            }:
                raise ValueError("invalid_trace")
            if (
                event["class"] not in CLASSES
                or event["function"] not in FUNCTIONS
                or type(event["line"]) is not int
                or not 1 <= event["line"] <= 1105
            ):
                raise ValueError("invalid_trace")
            for field in ("errno", "winerror", "sqlite_errorcode"):
                value = event[field]
                if value is not None and (
                    type(value) is not int or not -(2**31) <= value < 2**31
                ):
                    raise ValueError("invalid_trace")
    return sorted(categories), trace


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    if item.nodeid != TARGET:
        yield
        return
    original = subprocess.run
    temp = item.funcargs["tmp_path"]
    writer, path = temp / "writer-copy.py", temp / "state.json"
    expected_sha = os.environ["TASKPAW_CI004_WRITER_SHA"]
    tracing = os.environ["TASKPAW_CI004_TRACE"] == "1"

    def run(*args, **kwargs):
        index = None
        marker = uuid.uuid4().hex if tracing else ""
        record = {
            "index": None,
            "returncode": None,
            "exception": None,
            "stderr_categories": [],
            "trace": None,
        }
        try:
            expected = [
                sys.executable,
                str(writer),
                "--tool",
                "codex",
                "--path",
                str(path),
            ]
            if (
                len(args) != 1
                or args[0] != expected
                or set(kwargs)
                != {"input", "text", "capture_output", "timeout", "check"}
            ):
                raise ValueError("unexpected_call")
            if (
                kwargs["text"] is not True
                or kwargs["capture_output"] is not True
                or kwargs["timeout"] != 5
                or kwargs["check"] is not False
            ):
                raise ValueError("unexpected_flags")
            for candidate in range(13):
                if kwargs["input"] == json.dumps(
                    {
                        "session_id": "fake-" + str(candidate),
                        "turn_id": "fake-turn",
                        "hook_event_name": "UserPromptSubmit",
                    }
                ):
                    index = candidate
                    break
            if (
                index is None
                or not writer.is_absolute()
                or writer.parent != temp
                or hashlib.sha256(writer.read_bytes()).hexdigest() != expected_sha
            ):
                raise ValueError("unexpected_input")
            record["index"] = index
        except Exception:
            with LOCK:
                STATE["errors"] += 1
            return original(*args, **kwargs)
        options = dict(kwargs)
        # Restore the child's original PYTHONPATH; parent-only plugin imports
        # must not change the copied writer's sibling/package imports.
        env = dict(os.environ)
        if env["TASKPAW_CI004_HAD_PYTHONPATH"] == "1":
            env["PYTHONPATH"] = env["TASKPAW_CI004_ORIGINAL_PYTHONPATH"]
        else:
            env.pop("PYTHONPATH", None)
        if tracing:
            child = str(Path(__file__).parent / "ci004_child")
            prior = env.get("PYTHONPATH")
            env["PYTHONPATH"] = child + (os.pathsep + prior if prior else "")
            env.update(TASKPAW_CI004_CHILD=str(writer), TASKPAW_CI004_MARKER=marker)
        options["env"] = env
        try:
            result = original(*args, **options)
            record["returncode"] = result.returncode
            try:
                record["stderr_categories"], record["trace"] = child_metadata(
                    result.stderr, marker
                )
            except Exception:
                with LOCK:
                    STATE["errors"] += 1
            return result
        except BaseException as exc:
            record["exception"] = (
                "timeout"
                if isinstance(exc, subprocess.TimeoutExpired)
                else "other_exception"
            )
            raise
        finally:
            with LOCK:
                STATE["children"].append(record)

    subprocess.run = run
    try:
        yield
    finally:
        STATE["restored"] = subprocess.run is run
        subprocess.run = original


def pytest_collection_finish(session):
    STATE["collection_exact"] = [item.nodeid for item in session.items] == [TARGET]


def pytest_runtest_logreport(report):
    if report.nodeid == TARGET:
        STATE["reports"].append({"phase": report.when, "outcome": report.outcome})


def pytest_sessionfinish(session, exitstatus):
    STATE["exitcode"] = int(exitstatus)
    STATE["children"].sort(key=lambda row: -1 if row["index"] is None else row["index"])
    path = Path(os.environ["TASKPAW_CI004_RECEIPT"])
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(STATE, separators=(",", ":")) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)
