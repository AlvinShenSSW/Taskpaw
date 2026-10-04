"""Child-only exception observer; no product behavior or trace chaining."""

import atexit
import hashlib
import json
import os
import sys

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


def observe():
    target = os.environ.get("TASKPAW_CI004_CHILD")
    if not target or sys.argv[0] != target:
        return
    marker = os.environ.get("TASKPAW_CI004_MARKER", "")
    if len(marker) != 32 or any(c not in "0123456789abcdef" for c in marker):
        return
    state = {"valid": False, "errors": 0, "dropped": 0, "events": []}

    def emit():
        try:
            state["valid"] &= sys.gettrace() is trace
            sys.stderr.write(
                "TASKPAW_CI004_"
                + marker
                + ":"
                + json.dumps(state, separators=(",", ":"))
                + "\n"
            )
        except Exception:
            # A missing emission is invalidated by the parent; never print raw errors.
            return

    def trace(frame, event, arg):
        if frame.f_code.co_filename != target:
            return None
        frame.f_trace_lines = False
        frame.f_trace_opcodes = False
        if event == "exception":
            try:
                cls, exc, _ = arg
                name, function = cls.__name__, frame.f_code.co_name
                if (
                    cls.__module__ not in ("builtins", "sqlite3", "__main__")
                    or name not in CLASSES
                    or function not in FUNCTIONS
                ):
                    state["errors"] += 1
                elif len(state["events"]) == 64:
                    state["dropped"] += 1
                else:
                    record = {
                        "class": name,
                        "function": function,
                        "line": frame.f_lineno,
                    }
                    for field in ("errno", "winerror", "sqlite_errorcode"):
                        value = getattr(exc, field, None)
                        if value is not None and (
                            type(value) is not int or not -(2**31) <= value < 2**31
                        ):
                            state["errors"] += 1
                            value = None
                        record[field] = value
                    state["events"].append(record)
            except Exception:
                state["errors"] += 1
        return trace

    atexit.register(emit)
    try:
        if sys.gettrace() is not None:
            state["errors"] += 1
            return
        with open(target, "rb") as source:
            digest = hashlib.sha256(source.read()).hexdigest()
        if digest != os.environ.get("TASKPAW_CI004_WRITER_SHA"):
            state["errors"] += 1
            return
        state["valid"] = True
        sys.settrace(trace)
    except Exception:
        state["errors"] += 1


observe()
