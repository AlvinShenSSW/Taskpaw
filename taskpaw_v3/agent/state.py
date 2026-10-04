"""Explicit offline event-lineage operations; also reachable from the sidecar."""

from __future__ import annotations

import argparse
import json
import stat
import sys
from dataclasses import asdict
from pathlib import Path

import yaml

from taskpaw_v3.core.config import AgentConfig, load_yaml, save_yaml
from taskpaw_v3.core.state import (
    FileLease,
    StateError,
    StateRecord,
    atomic_json,
    backup_sources,
    integer,
    new_lineage_id,
    read_record,
    state_paths,
    strict_json,
    write_pair,
)


def _desktop_source(path: Path) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise StateError("desktop_state_requires_regular_records")
    return path.read_bytes()


def main(argv: list[str] | None = None, *, desktop_config: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    desktop = desktop_config is not None
    if desktop:
        parser.set_defaults(config=desktop_config)
    else:
        parser.add_argument("--config", required=True, type=Path)
    commands = parser.add_subparsers(dest="command", required=True)
    if not desktop:
        commands.add_parser("inspect")
    for verb, flag in (
        ("initialize", "--confirm-new-pairing"),
        ("migrate", "--confirm-intact-legacy-counter"),
        ("recover", "--confirm-surviving-record-intact"),
    ):
        if desktop and verb == "recover":
            continue
        commands.add_parser(verb).add_argument(flag, action="store_true", required=True)
    if not desktop:
        commands.add_parser("export").add_argument("--output", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)
    if not args.config.is_file():
        print(
            "config_missing: launch the installed Agent once to scaffold its config",
            file=sys.stderr,
        )
        return 2
    try:
        config: AgentConfig = load_yaml(AgentConfig, args.config)  # type: ignore[assignment]
        path = args.config.with_name("agent.state.json")
        primary, anchor, lock = state_paths(path)
        with FileLease(lock):
            desktop_snapshot = None
            if desktop:
                config_raw = args.config.read_bytes()
                config = load_yaml(AgentConfig, args.config)  # type: ignore[assignment]
                desktop_snapshot = (
                    config_raw,
                    _desktop_source(primary),
                    _desktop_source(anchor),
                )
            records = []
            errors = []
            for source in (primary, anchor):
                try:
                    records.append(read_record(source))
                except StateError as exc:
                    errors.append(exc.reason)
            trusted = (
                len(records) == 2
                and records[0] == records[1]
                and records[0].server_id == config.server_id
            )
            if args.command == "inspect":
                print(
                    json.dumps(
                        {
                            "state": "verified" if trusted else "recovery_required",
                            "reasons": errors
                            or ([] if trusted else ["state_records_disagree"]),
                            "record": asdict(records[0]) if trusted else None,
                        }
                    )
                )
                return 0
            backups: tuple[Path, ...] = ()
            if args.command == "export":
                if args.output.resolve() in {
                    p.resolve() for p in (primary, anchor, lock, args.config)
                }:
                    raise StateError("invalid_report_output")
                if not trusted:
                    raise StateError("recovery_required")
                atomic_json(
                    args.output,
                    {
                        "report_version": 1,
                        "verified": True,
                        "record": asdict(records[0]),
                    },
                )
                print(f"verified state report: {args.output}")
                return 0
            if args.command == "initialize":
                if desktop and (
                    desktop_snapshot is None
                    or desktop_snapshot[1:] != (None, None)
                    or any(
                        any(p.parent.glob(p.name + ".fault-*"))
                        for p in (primary, anchor)
                    )
                ):
                    raise StateError("desktop_initialization_requires_absent_state")
                if trusted:
                    raise StateError("state_already_verified")
                backups = backup_sources(path)
                config.server_id = "agent-" + new_lineage_id()
                record = StateRecord(
                    2, config.server_id, new_lineage_id(), "new_pairing", 1
                )
            elif args.command == "migrate":
                if anchor.exists() or (desktop and anchor.is_symlink()):
                    raise StateError("legacy_migration_requires_single_counter")
                raw = strict_json(primary.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or set(raw) != {"next_event_id"}:
                    raise StateError("invalid_legacy_counter")
                nxt = integer(raw["next_event_id"])
                backups = backup_sources(path)
                record = StateRecord(
                    2, config.server_id, new_lineage_id(), "legacy_migration", nxt
                )
            else:
                if not records or any(r.server_id != config.server_id for r in records):
                    raise StateError("no_intact_continuing_lineage")
                if any(
                    (r.server_id, r.stream_id, r.lineage_origin)
                    != (
                        records[0].server_id,
                        records[0].stream_id,
                        records[0].lineage_origin,
                    )
                    for r in records
                ):
                    raise StateError("state_identity_mismatch")
                record = max(records, key=lambda r: r.next_event_id)
                backups = backup_sources(path)
            if desktop:
                if desktop_snapshot != (
                    args.config.read_bytes(),
                    _desktop_source(primary),
                    _desktop_source(anchor),
                ):
                    raise StateError("state_changed_during_operation", backups)
                if args.command == "initialize" and any(
                    any(p.parent.glob(p.name + ".fault-*")) for p in (primary, anchor)
                ):
                    raise StateError("desktop_initialization_requires_absent_state")
            write_pair(path, record)
            if args.command == "initialize":
                if desktop and (
                    desktop_snapshot is None
                    or args.config.read_bytes() != desktop_snapshot[0]
                ):
                    raise StateError("state_changed_during_operation", backups)
                save_yaml(config, args.config)
            if desktop:
                print(json.dumps({"result": args.command}))
                return 0
            print(
                json.dumps(
                    {
                        "result": args.command,
                        "server_id": record.server_id,
                        "stream_id": record.stream_id,
                        "next_event_id": record.next_event_id,
                        "backups": [str(p) for p in backups],
                    }
                )
            )
            return 0
    except (StateError, OSError, ValueError, TypeError, yaml.YAMLError) as exc:
        reason = exc.reason if isinstance(exc, StateError) else "state_operation_failed"
        print(reason, file=sys.stderr)
        if isinstance(exc, StateError) and not desktop:
            for backup in exc.backups:
                print(f"fault backup: {backup}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
