"""Hub CLI: run the Hub, and manage the agents it polls.

    python -m taskpaw_v3.hub run                 # start the Hub (poller + API)
    python -m taskpaw_v3.hub list-servers
    python -m taskpaw_v3.hub add-server  --name moomoo --ip 192.168.1.50 [--port 5680] [--disabled]
    python -m taskpaw_v3.hub enable-server  --id 1
    python -m taskpaw_v3.hub disable-server --id 1
    python -m taskpaw_v3.hub remove-server  --id 1

The agent list lives in the Hub's SQLite store (not hub.yaml), so these
subcommands open the DB directly. `--config` / `--db` override the default
platform locations.
"""

from __future__ import annotations

import argparse
import json
import sys
from contextlib import nullcontext
from pathlib import Path

from taskpaw_v3.core.config import HubConfig, load_yaml
from taskpaw_v3.core.state import (
    FileLease,
    StateError,
    StateRecord,
    db_lease_path,
    strict_json,
)
from taskpaw_v3.hub.server.service import (
    db_path_for,
    default_config_path,
    legacy_db_conflict,
    run_from_config,
)
from taskpaw_v3.hub.server.store import HubStore


def _port(value: str) -> int:
    """argparse type: a valid TCP port (1–65535), else a parse error."""
    try:
        p = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"port must be an integer, got {value!r}")
    if not (1 <= p <= 65535):
        raise argparse.ArgumentTypeError(f"port must be 1–65535, got {p}")
    return p


def _store(args) -> HubStore:
    if args.db:
        return HubStore(Path(args.db).expanduser())
    # Target the SAME db the running hub uses: HubConfig.data_dir/hub.db.
    cfg_path = Path(args.config).expanduser() if args.config else default_config_path()
    if not cfg_path.exists():
        if args.config:
            # Explicitly requested config is missing → fail, don't silently target
            # the default db (operator could manage the wrong store) (Kimi).
            print(f"error: hub config not found: {cfg_path}", file=sys.stderr)
            raise SystemExit(2)
        # No --config and none at the default path → use the default data_dir db,
        # but still honor the legacy guard (a config-adjacent hub.db may exist
        # even without a hub.yaml) (Kimi).
        db = db_path_for(HubConfig())
        legacy = legacy_db_conflict(default_config_path(), db)
        if legacy:
            print(
                f"error: would operate on {db}, but an older hub.db exists at "
                f"{legacy}.\n  Move it, set data_dir, or pass --db to target one.",
                file=sys.stderr,
            )
            raise SystemExit(2)
        return HubStore(db)
    try:
        config: HubConfig = load_yaml(HubConfig, cfg_path)  # type: ignore[assignment]
    except Exception as e:
        # A malformed config must NOT silently target a different db than the
        # running hub — surface it and exit (#38 review).
        print(f"error: cannot read hub config {cfg_path}: {e}", file=sys.stderr)
        raise SystemExit(2)
    db = db_path_for(config)
    # FATAL (consistent with `run`): don't let the operator edit a new empty db
    # while a real legacy one sits beside the config — they'd manage a db the hub
    # refuses to start on. Pass --db to target a specific db explicitly (Kimi).
    legacy = legacy_db_conflict(cfg_path, db)
    if legacy:
        print(
            f"error: would operate on {db}, but an older hub.db exists at {legacy}.\n"
            f"  Move it:   mv '{legacy}' '{db}'\n"
            f"  or set data_dir, or pass --db {db} to target it explicitly.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return HubStore(db)


def _print_servers(store: HubStore) -> None:
    servers = store.list_servers()
    if not servers:
        print("(no agents registered)")
        return
    print(f"{'id':>3}  {'name':<20} {'address':<24} enabled")
    for s in servers:
        addr = f"{s['ip']}:{s['port']}"
        print(
            f"{s['id']:>3}  {s['name']:<20} {addr:<24} {'yes' if s['enabled'] else 'no'}"
        )


def _cursor_command(args) -> int:
    if not args.db or not Path(args.db).expanduser().is_file():
        print("cursor operation requires an explicit existing --db", file=sys.stderr)
        return 2
    db = Path(args.db).expanduser().resolve()
    try:
        guard = (
            FileLease(db_lease_path(db))
            if args.cmd == "adopt-event-cursor"
            else nullcontext()
        )
        with guard:
            store = HubStore(db)
            try:
                server = store.get_server(args.id)
                if server is None:
                    raise StateError("unknown_server")
                binding = store.get_event_cursor(args.id)
                diagnostic = None
                try:
                    acks = store.read_acks()
                except StateError:
                    diagnostic = store.get_config("last_event_ids")
                    acks = {}
                floor = store.event_floor(args.id, acks)
                if args.cmd == "event-cursor":
                    print(
                        json.dumps(
                            {
                                "binding": binding,
                                "received_floor": floor,
                                "ack_store_invalid": diagnostic is not None,
                            }
                        )
                    )
                    return 0
                if server["enabled"]:
                    raise StateError("cursor_adoption_requires_disabled_server")
                report = strict_json(
                    Path(args.state_report).read_text(encoding="utf-8")
                )
                if (
                    not isinstance(report, dict)
                    or set(report) != {"report_version", "verified", "record"}
                    or type(report["report_version"]) is not int
                    or report["report_version"] != 1
                    or report["verified"] is not True
                ):
                    raise StateError("invalid_state_report")
                record = StateRecord.parse(report["record"])
                identity = {
                    "server_id": record.server_id,
                    "stream_id": record.stream_id,
                }
                if binding["identity"] is not None and binding["identity"] != identity:
                    raise StateError("state_identity_changed")
                if (
                    record.lineage_origin == "new_pairing"
                    and binding["state"] != "fresh"
                    and binding["identity"] != identity
                ):
                    raise StateError("new_pairing_requires_new_registration")
                if record.next_event_id <= floor:
                    raise StateError("cursor_floor_regressed")
                acks[args.id] = floor
                store.commit_event_cursor(
                    args.id,
                    {
                        "state": "bound",
                        "identity": identity,
                        "boot_id": None,
                        "resume_floor": None,
                    },
                    acks,
                    require_disabled=True,
                    diagnostic=diagnostic,
                )
                print(
                    json.dumps(
                        {
                            "result": "adopted",
                            "server_id": args.id,
                            "ack": floor,
                            "retained_events": len(store.recent_events(args.id)),
                        }
                    )
                )
                return 0
            finally:
                store.close()
    except (ValueError, OSError) as exc:
        print(
            exc.reason if isinstance(exc, StateError) else "cursor_operation_failed",
            file=sys.stderr,
        )
        return 2


def main(
    argv: list[str] | None = None,
    *,
    allowed_commands: frozenset[str] | None = None,
    require_explicit_db: bool = False,
) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m taskpaw_v3.hub",
        description="Run the TaskPaw V3 Hub and manage polled agents.",
    )
    ap.add_argument(
        "--config", default=None, help="path to hub.yaml (default: platform location)"
    )
    ap.add_argument(
        "--db", default=None, help="path to hub.db (default: HubConfig.data_dir/hub.db)"
    )
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("run", help="start the Hub (poller + API)")
    sub.add_parser("list-servers", help="list registered agents")

    p_add = sub.add_parser("add-server", help="register an agent to poll")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--ip", required=True, help="agent LAN IP (its bind_host)")
    p_add.add_argument(
        "--port", type=_port, default=5680, help="agent bind_port (default 5680)"
    )
    p_add.add_argument(
        "--disabled", action="store_true", help="register but don't poll yet"
    )

    for name in ("enable-server", "disable-server", "remove-server"):
        p = sub.add_parser(name)
        p.add_argument("--id", type=int, required=True)

    for name in ("event-cursor", "adopt-event-cursor"):
        command = sub.add_parser(name)
        command.add_argument("--id", type=int, required=True)
        if name == "adopt-event-cursor":
            command.add_argument("--state-report", required=True)
    args = ap.parse_args(argv)
    cmd = args.cmd or "run"
    if allowed_commands is not None and (
        args.cmd not in allowed_commands or (require_explicit_db and not args.db)
    ):
        print(
            "unsupported offline cursor command or missing explicit --db",
            file=sys.stderr,
        )
        return 2
    if cmd in ("event-cursor", "adopt-event-cursor"):
        return _cursor_command(args)

    if cmd == "run":
        return run_from_config(
            Path(args.config).expanduser() if args.config else None,
            Path(args.db).expanduser() if args.db else None,
        )

    store = _store(args)
    try:
        if cmd == "list-servers":
            _print_servers(store)
        elif cmd == "add-server":
            try:
                sid = store.add_server(
                    args.name, args.ip, args.port, enabled=not args.disabled
                )
            except Exception as e:
                print(
                    f"error: could not add server (duplicate name?): {e}",
                    file=sys.stderr,
                )
                return 2
            print(
                f"added agent #{sid}: {args.name} @ {args.ip}:{args.port}"
                f"{' (disabled)' if args.disabled else ''}"
            )
        elif cmd in ("enable-server", "disable-server"):
            ok = store.set_server_enabled(args.id, cmd == "enable-server")
            print(f"{'updated' if ok else 'no such server id'}: #{args.id}")
            if not ok:
                return 2
        elif cmd == "remove-server":
            ok = store.remove_server(args.id)
            print(f"{'removed' if ok else 'no such server id'}: #{args.id}")
            if not ok:
                return 2
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO)  # only when run as a script (Kimi)
    raise SystemExit(main())
