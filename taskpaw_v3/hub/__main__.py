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
import sys
from pathlib import Path

from taskpaw_v3.core.config import HubConfig, load_yaml
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
        # No --config and none at the default path → use the default data_dir db.
        return HubStore(db_path_for(HubConfig()))
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
        print(f"error: would operate on {db}, but an older hub.db exists at {legacy}.\n"
              f"  Move it:   mv '{legacy}' '{db}'\n"
              f"  or set data_dir, or pass --db {db} to target it explicitly.",
              file=sys.stderr)
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
        print(f"{s['id']:>3}  {s['name']:<20} {addr:<24} {'yes' if s['enabled'] else 'no'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m taskpaw_v3.hub",
                                 description="Run the TaskPaw V3 Hub and manage polled agents.")
    ap.add_argument("--config", default=None, help="path to hub.yaml (default: platform location)")
    ap.add_argument("--db", default=None,
                    help="path to hub.db (default: HubConfig.data_dir/hub.db)")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("run", help="start the Hub (poller + API)")
    sub.add_parser("list-servers", help="list registered agents")

    p_add = sub.add_parser("add-server", help="register an agent to poll")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--ip", required=True, help="agent LAN IP (its bind_host)")
    p_add.add_argument("--port", type=_port, default=5680, help="agent bind_port (default 5680)")
    p_add.add_argument("--disabled", action="store_true", help="register but don't poll yet")

    for name in ("enable-server", "disable-server", "remove-server"):
        p = sub.add_parser(name)
        p.add_argument("--id", type=int, required=True)

    args = ap.parse_args(argv)
    cmd = args.cmd or "run"

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
                sid = store.add_server(args.name, args.ip, args.port, enabled=not args.disabled)
            except Exception as e:
                print(f"error: could not add server (duplicate name?): {e}", file=sys.stderr)
                return 2
            print(f"added agent #{sid}: {args.name} @ {args.ip}:{args.port}"
                  f"{' (disabled)' if args.disabled else ''}")
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
    logging.basicConfig(level=logging.INFO)   # only when run as a script (Kimi)
    raise SystemExit(main())
