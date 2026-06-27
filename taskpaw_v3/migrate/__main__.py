"""Read-only V2→V3 migration preview CLI (design §8).

    python -m taskpaw_v3.migrate [CONFIG_JSON] [--state STATE_JSON] [--yaml]

Prints the migration plan for a V2 `config.json`: the V3 monitors it would
create, any warnings (skipped / changed-semantics watchers), and the event-id
cursor. It NEVER writes anything — copy the `--yaml` block into your agent config
yourself once the plan looks right.

Default CONFIG path is the V2 location: %APPDATA%/TaskPaw/config.json on Windows,
~/TaskPaw/config.json elsewhere (state.json is looked up alongside it).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

import yaml

from taskpaw_v3.migrate.migrator import plan_migration


def _default_config_path() -> Path:
    base = Path(os.environ.get("APPDATA", Path.home())) / "TaskPaw"
    return base / "config.json"


def _render_text(plan, config_path: Path) -> str:
    lines = [
        f"V2→V3 migration preview  (read-only)",
        f"  source : {config_path}",
        f"  machine: {plan.machine_name or '(unset)'}",
        f"  cursor : {plan.cursor}  (V3 next event id)",
        "",
        f"Monitors ({len(plan.monitors)}):",
    ]
    if not plan.monitors:
        lines.append("  (none)")
    for m in plan.monitors:
        flag = "" if m.enabled else "  [DISABLED — excluded from runtime]"
        lines.append(f"  • {m.type_id:<10} {m.name}{flag}")
        lines.append(f"      from V2 {m.source_type!r}  config={json.dumps(m.config, ensure_ascii=False)}")
    lines.append("")
    runnable = plan.to_runtime_monitors()
    lines.append(f"Runnable (enabled) monitors: {len(runnable)}")
    if plan.warnings:
        lines.append("")
        lines.append(f"Warnings ({len(plan.warnings)}):")
        for w in plan.warnings:
            lines.append(f"  ! [{w.source_type}] {w.name}: {w.reason}")
    else:
        lines.append("")
        lines.append("No warnings — clean migration.")
    return "\n".join(lines)


def _render_yaml(plan) -> str:
    """The `monitors:` block to paste into an agent config (enabled only)."""
    block = {"monitors": plan.to_runtime_monitors()}
    return yaml.safe_dump(block, sort_keys=False, allow_unicode=True)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m taskpaw_v3.migrate",
                                 description="Preview a V2→V3 migration (read-only).")
    ap.add_argument("config", nargs="?", default=None,
                    help="path to V2 config.json (default: V2 standard location)")
    ap.add_argument("--state", default=None,
                    help="path to V2 state.json (default: alongside config.json)")
    ap.add_argument("--yaml", action="store_true",
                    help="emit only the agent `monitors:` YAML block (enabled monitors)")
    args = ap.parse_args(argv)

    config_path = Path(args.config).expanduser() if args.config else _default_config_path()
    if not config_path.exists():
        print(f"error: V2 config not found: {config_path}", file=sys.stderr)
        print("       pass the path explicitly: python -m taskpaw_v3.migrate /path/to/config.json",
              file=sys.stderr)
        return 2

    state_path = Path(args.state).expanduser() if args.state else config_path.with_name("state.json")
    plan = plan_migration(config_path, state_path)

    if args.yaml:
        sys.stdout.write(_render_yaml(plan))
    else:
        print(_render_text(plan, config_path))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
