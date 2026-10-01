"""Standalone Claude/Codex activity writer; never stores prompt/content fields.

Explicit --state and appended notify arguments retain the four-field JSON API.
Automatic documented hook inputs also commit bounded hashed session/turn/child
facts to <JSON>.activity-v2.sqlite3, then atomically publish the legacy projection.
The rich monitor reduces independent subjects; Stop/SubagentStop are continuable
attempts, not proof of completion. Missing IDs/expired unresolved activity is
unknown, never inferred complete from receipt ordering. Claude prompt_id requires
v2.1.196+ and is absent before first input. Codex turn_id is event-specific.
Both official PermissionRequest inputs lack tool_use_id; no tool-name pairing.
SessionEnd/Interrupt legacy JSON mapping is unchanged; rich finality is scoped.

Default ~/.taskpaw/agent-activity.json; --path selects a per-tool destination.
Copied absolute scripts remain standalone (stdlib SQLite, optional existing
psutil for direct-parent identity only). CLI reclamation defaults to300s and
keeps unresolved coverage after expiry; reader freshness may be shorter. JSON
watermarks gate old handle evidence without asserting task completion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

DEFAULT_PATH = "~/.taskpaw/agent-activity.json"

# Claude Code hook event -> activity state.
_CLAUDE_EVENT_STATE = {
    "UserPromptSubmit": "busy",
    "SessionStart": "busy",
    "SubagentStart": "busy",
    "PreToolUse": "busy",
    "PostToolUse": "busy",
    "Notification": "waiting",
    "PermissionRequest": "waiting",
    "Stop": "idle",
    "SubagentStop": "idle",
    "SessionEnd": "idle",
}


_CODEX_EVENT_STATE = {
    **dict.fromkeys(
        (
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PreCompact",
            "PostCompact",
            "SubagentStart",
            "SubagentStop",
        ),
        "busy",
    ),
    "PermissionRequest": "waiting",
    **dict.fromkeys(("Stop", "Interrupt", "SessionEnd"), "idle"),
}


def state_from_stdin(
    raw: str, tool: str = "claude"
) -> tuple[Optional[str], Optional[str]]:
    """Map a Claude Code hook payload to (state, session_id). Best-effort."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None, None
    if not isinstance(data, dict):
        return None, None
    event = data.get("hook_event_name") or data.get("hookEventName") or ""
    session = data.get("session_id") or data.get("sessionId")
    mapping = _CODEX_EVENT_STATE if tool == "codex" else _CLAUDE_EVENT_STATE
    return mapping.get(str(event)), (session if isinstance(session, str) else None)


def write_activity(
    path: str,
    tool: str,
    state: str,
    session: Optional[str] = None,
    ts: Optional[float] = None,
) -> Path:
    """Atomically write the activity file (tmp in same dir + os.replace)."""
    return _write_projection(
        path, tool, state, session, time.time() if ts is None else ts
    )


def _write_projection(
    path: str, tool: str, state: str, session: Optional[str], ts: float, **extra
) -> Path:
    p = Path(path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tool": tool,
        "state": state,
        "session": session or "",
        "ts": ts,
        **extra,
    }
    fd, name = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=p.parent)
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)
    return p


# Private bounded evidence cache. No transcript, prompt, argv or payload columns.
_APP_ID = 0x54504132
_VERSION = 1
_RETENTION = 86400.0
_WRITER_FRESHNESS = 300.0
_FACT_CAP = 2048
_SUMMARY_CAP = 256
_TOOL_CAP = 64
_TOOL_OVERFLOW = 1
_STORE_OVERFLOW = 2
_COLUMNS = "id,tool,session,turn,actor,unit,kind,ts,pid,created"
_SCOPE = ("tool", "session", "turn", "actor", "pid", "created")


class ActivityStoreError(OSError):
    """Fixed sanitized failure; callers never expose SQLite/path exceptions."""


class _CapacityRefused(Exception):
    """Normal admission backpressure, distinct from a failed transaction."""


def sidecar_path(path: str | Path) -> Path:
    return Path(str(Path(path).expanduser()) + ".activity-v2.sqlite3")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _identity_field(data: dict, name: str) -> str:
    value = data.get(name)
    return (
        _hash(value)
        if isinstance(value, str) and 0 < len(value.encode("utf-8")) <= 256
        else ""
    )


def _producer_identity() -> tuple[int, float] | None:
    # Only our direct parent. Helper parents stay unbound; no ancestry/cmdline read.
    try:
        import psutil
    except ImportError:
        return None
    try:
        pid = os.getppid()
        created = psutil.Process(pid).create_time()
        if (
            pid == os.getppid()
            and psutil.Process(pid).create_time() == created
            and math.isfinite(created)
        ):
            return pid, created
    except (psutil.Error, OSError):
        return None
    return None


def hook_fact(
    raw: str, tool: str, ts: float, producer: tuple[int, float] | None = None
) -> dict | None:
    """Read only documented bounded identity/event fields; receipt ts is not order."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return None
    if (
        not isinstance(data, dict)
        or not isinstance(tool, str)
        or not 0 < len(tool.encode("utf-8")) <= 256
    ):
        return None
    event = data.get("hook_event_name") or data.get("hookEventName")
    mapping = _CODEX_EVENT_STATE if tool == "codex" else _CLAUDE_EVENT_STATE
    if event not in mapping and event != "SubagentStart":
        return None
    session = _identity_field(data, "session_id") or _identity_field(data, "sessionId")
    turn = _identity_field(data, "turn_id" if tool == "codex" else "prompt_id")
    actor = (
        _identity_field(data, "agent_id")
        if str(event).startswith("Subagent")
        else "parent"
    )
    unit = _identity_field(data, "tool_use_id")
    if event in ("Stop", "SubagentStop"):
        kind = "stop_attempt"
    elif event == "Interrupt" and tool == "codex":
        kind = "interrupt"
    elif event == "SessionEnd":
        kind = "session_end"
    elif event == "SessionStart":
        kind = "presence"
    elif event == "PermissionRequest" or event == "Notification":
        kind = "waiting"
    else:
        kind = "busy"
    if not session or (
        event not in ("SessionStart", "SessionEnd") and (not turn or not actor)
    ):
        kind = "unknown"
    pid, created = producer if producer else (0, 0.0)
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid < 0
        or not math.isfinite(created)
        or created < 0
    ):
        pid, created = 0, 0.0
    fact = dict(
        tool=tool,
        session=session,
        turn=turn,
        actor=actor,
        unit=unit,
        kind=kind,
        ts=ts,
        pid=pid,
        created=created,
    )
    # Repeated identical callback cannot renew its lease. Event/unit distinguish facts.
    fact["id"] = _hash(
        json.dumps(
            [tool, session, turn, actor, unit, kind, event, pid, created],
            separators=(",", ":"),
        )
    )
    return fact


def _regular(path: Path) -> os.stat_result | None:
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ActivityStoreError("unsafe activity store")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ActivityStoreError("unsafe activity store")
    if sys.platform != "win32" and (
        info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise ActivityStoreError("unsafe activity store")
    return info


def _open_store(path: Path, *, writable: bool) -> sqlite3.Connection | None:
    conn = None
    try:
        before = _regular(path)
        fresh = False
        if before is None:
            if not writable:
                return None
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if os.name != "nt":
                path.parent.chmod(0o700)
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                before = _regular(path)
            else:
                before = os.fstat(fd)
                os.close(fd)
                fresh = True
        uri = path.absolute().as_uri() + ("?mode=rw" if writable else "?mode=ro")
        conn = sqlite3.connect(uri, uri=True, timeout=0.1)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=100")
        after = _regular(path)
        if (
            before is None
            or after is None
            or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise ActivityStoreError("activity store changed")
        if not fresh and (
            conn.execute("PRAGMA application_id").fetchone()[0] != _APP_ID
            or conn.execute("PRAGMA user_version").fetchone()[0] != _VERSION
        ):
            raise ActivityStoreError("unsupported activity store")
        if writable:
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=FULL")
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            conn.execute(f"PRAGMA max_page_count={8 * 1024 * 1024 // page_size}")
        if fresh:
            conn.executescript(f"""
                PRAGMA application_id={_APP_ID};
                PRAGMA user_version={_VERSION};
                CREATE TABLE tools(tool TEXT PRIMARY KEY, watermark REAL, overflow INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE facts(id TEXT PRIMARY KEY, tool TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL,
                  actor TEXT NOT NULL, unit TEXT NOT NULL, kind TEXT NOT NULL, ts REAL NOT NULL, pid INTEGER NOT NULL, created REAL NOT NULL);
                CREATE TABLE summaries(tool TEXT NOT NULL, session TEXT NOT NULL, turn TEXT NOT NULL, actor TEXT NOT NULL,
                  pid INTEGER NOT NULL, created REAL NOT NULL, reason INTEGER NOT NULL, PRIMARY KEY(tool,session,turn,actor,pid,created));
            """)
        # Fail closed instead of reading unbounded/foreign data.
        if (
            conn.execute("PRAGMA page_count").fetchone()[0]
            * conn.execute("PRAGMA page_size").fetchone()[0]
            > 8 * 1024 * 1024
        ):
            raise ActivityStoreError("activity store limited")
        for table, cap in (
            ("tools", _TOOL_CAP),
            ("facts", _FACT_CAP),
            ("summaries", _SUMMARY_CAP),
        ):
            if conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] > cap:
                raise ActivityStoreError("activity store limited")
        return conn
    except (OSError, sqlite3.Error, ValueError):
        if conn is not None:
            conn.close()
        raise ActivityStoreError("activity store unavailable") from None


def _scope(row: dict) -> tuple:
    return tuple(row[k] for k in _SCOPE)


def _interrupted(row: dict, finals: list[dict]) -> bool:
    return row["actor"] == "parent" and any(
        f["kind"] == "interrupt" and _scope(f) == _scope(row) for f in finals
    )


def _retain_unknown(conn: sqlite3.Connection, row: dict) -> None:
    if not row["session"] or not row["turn"] or not row["actor"]:
        conn.execute(
            "UPDATE tools SET overflow=overflow|? WHERE tool=?",
            (_TOOL_OVERFLOW, row["tool"]),
        )
        return
    old = conn.execute(
        "SELECT reason FROM summaries WHERE tool=? AND session=? AND turn=? AND actor=? AND pid=? AND created=?",
        _scope(row),
    ).fetchone()
    reason = (
        2 if row["kind"] == "stop_attempt" else 4 if row["kind"] == "unknown" else 1
    )
    if old is not None:
        conn.execute(
            "UPDATE summaries SET reason=reason|? WHERE tool=? AND session=? AND turn=? AND actor=? AND pid=? AND created=?",
            (reason, *_scope(row)),
        )
        return
    if conn.execute("SELECT count(*) FROM summaries").fetchone()[0] >= _SUMMARY_CAP:
        conn.execute(
            "UPDATE tools SET overflow=overflow|? WHERE tool=?",
            (_TOOL_OVERFLOW, row["tool"]),
        )
        return
    conn.execute("INSERT INTO summaries VALUES(?,?,?,?,?,?,?)", (*_scope(row), reason))


def _expire(
    conn: sqlite3.Connection, now: float, threshold: float, finals: list[dict]
) -> None:
    for value in conn.execute(
        f"SELECT {_COLUMNS} FROM facts WHERE ts < ?", (now - threshold,)
    ).fetchall():
        row = dict(value)
        if row["kind"] == "interrupt":
            if now - row["ts"] <= _RETENTION:
                continue
        elif row["kind"] not in ("presence", "session_end") and not _interrupted(
            row, finals
        ):
            _retain_unknown(conn, row)
        conn.execute("DELETE FROM facts WHERE id=?", (row["id"],))


def _retain_refused(conn: sqlite3.Connection, fact: dict) -> None:
    """Remember normal backpressure without deleting/admitting an old fact."""
    if (
        conn.execute("SELECT 1 FROM tools WHERE tool=?", (fact["tool"],)).fetchone()
        is None
    ):
        if conn.execute("SELECT count(*) FROM tools").fetchone()[0] >= _TOOL_CAP:
            # The refused tool has no row. Carry a fixed store-wide bit in one
            # existing row instead of inventing a65th identity or losing coverage.
            conn.execute(
                "UPDATE tools SET overflow=overflow|? WHERE tool=(SELECT tool FROM tools ORDER BY tool LIMIT 1)",
                (_STORE_OVERFLOW,),
            )
            return
        conn.execute("INSERT INTO tools(tool) VALUES(?)", (fact["tool"],))
    _retain_unknown(conn, {**fact, "kind": "unknown"})


def _admit_fact(conn: sqlite3.Connection, fact: dict, freshness: float) -> None:
    now = fact["ts"]
    if (
        conn.execute("SELECT 1 FROM tools WHERE tool=?", (fact["tool"],)).fetchone()
        is None
    ):
        if conn.execute("SELECT count(*) FROM tools").fetchone()[0] >= _TOOL_CAP:
            raise _CapacityRefused
        conn.execute("INSERT INTO tools(tool) VALUES(?)", (fact["tool"],))
    finals = [
        dict(r)
        for r in conn.execute(f"SELECT {_COLUMNS} FROM facts WHERE kind='interrupt'")
    ]
    if fact["kind"] == "interrupt":
        finals.append(fact)
    _expire(conn, now, _RETENTION, finals)
    if conn.execute("SELECT 1 FROM facts WHERE id=?", (fact["id"],)).fetchone() is None:
        if conn.execute("SELECT count(*) FROM facts").fetchone()[0] >= _FACT_CAP:
            _expire(conn, now, freshness, finals)
            if conn.execute("SELECT count(*) FROM facts").fetchone()[0] >= _FACT_CAP:
                # Redundant progress under a retained final is resolved, not
                # fresh active. Keep the actual terminal tombstone for24h.
                for value in conn.execute(f"SELECT {_COLUMNS} FROM facts").fetchall():
                    row = dict(value)
                    if row["kind"] != "interrupt" and _interrupted(row, finals):
                        conn.execute("DELETE FROM facts WHERE id=?", (row["id"],))
        if conn.execute("SELECT count(*) FROM facts").fetchone()[0] >= _FACT_CAP:
            raise _CapacityRefused
        conn.execute(
            f"INSERT INTO facts({_COLUMNS}) VALUES(?,?,?,?,?,?,?,?,?,?)",
            tuple(fact[k] for k in _COLUMNS.split(",")),
        )
    if (
        conn.execute(
            "SELECT count(DISTINCT session) FROM facts WHERE session!=''"
        ).fetchone()[0]
        > 64
        or conn.execute(
            "SELECT count(*) FROM (SELECT DISTINCT session,turn FROM facts WHERE turn!='')"
        ).fetchone()[0]
        > 256
    ):
        raise _CapacityRefused
    if fact["kind"] == "interrupt":
        conn.execute(
            "DELETE FROM summaries WHERE tool=? AND session=? AND turn=? AND actor=? AND pid=? AND created=?",
            _scope(fact),
        )
    if fact["kind"] in ("interrupt", "session_end", "stop_attempt"):
        conn.execute(
            "UPDATE tools SET watermark=CASE WHEN watermark IS NULL OR watermark<? THEN ? ELSE watermark END WHERE tool=?",
            (now, now, fact["tool"]),
        )


def publish_fact(
    path: str | Path, fact: dict, *, freshness: float = _WRITER_FRESHNESS
) -> None:
    """Atomic publication or bounded durable coverage for normal refusal.

    CLI reclamation uses300s. A shorter reader TTL cannot authorize earlier CLI
    deletion. SQL/I/O/commit failures roll back the entire original snapshot;
    normal capacity refusal keeps old facts and commits only unknown coverage.
    """
    conn = _open_store(sidecar_path(path), writable=True)
    assert conn is not None
    refused = False
    try:
        now = fact["ts"]
        if (
            not isinstance(now, (int, float))
            or isinstance(now, bool)
            or not math.isfinite(now)
            or not 0 < freshness <= _RETENTION
        ):
            raise ActivityStoreError("invalid activity fact")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("SAVEPOINT admission")
        try:
            _admit_fact(conn, fact, freshness)
        except _CapacityRefused:
            conn.execute("ROLLBACK TO admission")
            _retain_refused(conn, fact)
            refused = True
        conn.execute("RELEASE admission")
        conn.commit()
    except (OSError, sqlite3.Error, ValueError, KeyError):
        conn.rollback()
        raise ActivityStoreError("activity store write failed") from None
    finally:
        conn.close()
    if refused:
        # The incoming fact was not recorded. Report nonzero/false projection,
        # even though the separate unknown coverage was atomically committed.
        raise ActivityStoreError("activity store limited")


def read_facts(path: str | Path, tool: str) -> dict:
    """Bounded read-only snapshot; a missing sidecar is distinct from unavailable."""
    conn = _open_store(sidecar_path(path), writable=False)
    if conn is None:
        return {"facts": [], "summaries": [], "overflow": False, "watermark": None}
    try:
        conn.execute("BEGIN")
        # A refused65th tool has no metadata row; read the fixed store-wide
        # coverage bit before filtering by the requested tool.
        global_overflow = (
            conn.execute(
                "SELECT 1 FROM tools WHERE overflow & ? != 0 LIMIT 1",
                (_STORE_OVERFLOW,),
            ).fetchone()
            is not None
        )
        meta = conn.execute(
            "SELECT watermark,overflow FROM tools WHERE tool=?", (tool,)
        ).fetchone()
        return {
            "facts": [
                dict(r)
                for r in conn.execute(
                    f"SELECT {_COLUMNS} FROM facts WHERE tool=?", (tool,)
                )
            ],
            "summaries": [
                dict(r)
                for r in conn.execute("SELECT * FROM summaries WHERE tool=?", (tool,))
            ],
            "overflow": global_overflow or bool(meta and meta["overflow"]),
            "watermark": meta["watermark"] if meta else None,
        }
    except (sqlite3.Error, ValueError):
        raise ActivityStoreError("activity store read failed") from None
    finally:
        conn.rollback()
        conn.close()


def publish_watermark(path: str | Path, tool: str, ts: float) -> None:
    """Legacy idle remains four fields but keeps its negative file-evidence gate."""
    fact = dict(
        id=_hash(json.dumps([tool, "watermark"])),
        tool=tool,
        session="",
        turn="",
        actor="",
        unit="",
        kind="presence",
        ts=ts,
        pid=0,
        created=0.0,
    )
    publish_fact(path, fact)
    conn = _open_store(sidecar_path(path), writable=True)
    assert conn is not None
    try:
        with conn:
            conn.execute(
                "UPDATE tools SET watermark=CASE WHEN watermark IS NULL OR watermark<? THEN ? ELSE watermark END WHERE tool=?",
                (ts, ts, tool),
            )
    except sqlite3.Error:
        raise ActivityStoreError("activity watermark failed") from None
    finally:
        conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Write dev-agent activity state.")
    ap.add_argument("--tool", default="agent", help="agent label (claude|codex|...)")
    ap.add_argument(
        "--state",
        default=None,
        help="busy|idle|waiting; omit to auto-detect from stdin hook payload",
    )
    ap.add_argument("--session", default=None, help="optional session id")
    ap.add_argument(
        "--path", default=DEFAULT_PATH, help=f"output file (default {DEFAULT_PATH})"
    )
    # parse_known_args (not parse_args): a Codex `notify` program is invoked with
    # its event JSON appended as a trailing argv, which strict parsing would reject
    # (exit 2 → nothing written). A hook shim must tolerate extra args and never
    # break the host's notify/hook chain (#168). The trade-off — a mistyped flag is
    # silently ignored rather than erroring — is acceptable for a fire-and-forget
    # writer whose contract is "never disrupt the caller".
    ap.add_argument("--taskpaw-hook-id", default=None)
    args, _ = ap.parse_known_args(argv)

    started = time.time()
    state, session = args.state, args.session
    fact = None
    if state is None and not sys.stdin.isatty():
        raw = sys.stdin.read()
        detected, sess = state_from_stdin(raw, args.tool)
        fact = hook_fact(raw, args.tool, started, _producer_identity())
        state = detected
        session = session or sess
    if state is None:
        # Unknown event / nothing to record — succeed quietly so we never break
        # the host hook chain.
        return 0

    committed = True
    if fact is not None:
        try:
            publish_fact(args.path, fact)
        except ActivityStoreError:
            committed = False
            print("activity writer: fact write failed", file=sys.stderr)
    try:
        if fact is not None:
            _write_projection(
                args.path,
                args.tool,
                state,
                session,
                started,
                activity_schema=2,
                fact_id=fact["id"],
                fact_committed=committed,
            )
        else:
            write_activity(args.path, args.tool, state, session, started)
            if state == "idle":
                publish_watermark(args.path, args.tool, started)
    except OSError:
        print("activity writer: state write failed", file=sys.stderr)
        return 1
    return 0 if committed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
