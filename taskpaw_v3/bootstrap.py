"""One-shot setup for a V3 Hub or agent — no hand-typed config.

    python -m taskpaw_v3.bootstrap agent [--run]
    python -m taskpaw_v3.bootstrap hub --agent moomoo,192.168.1.50 --agent mac,127.0.0.1 [--run]

It copies the bundled example config into the platform config dir (without
clobbering an existing one unless --force), optionally registers the agents the
Hub should poll, and optionally launches the service. The double-clickable
wrappers in `scripts/` call this so the operator never touches a command line.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from taskpaw_v3.agent.server import service as agent_service
from taskpaw_v3.hub.server import service as hub_service

EXAMPLES = Path(__file__).resolve().parent / "examples"


def _friendly_machine_name() -> str:
    """The human-facing computer name to seed a fresh agent's `machine` — what the
    user calls this box, not the network hostname. On macOS that's the OS
    `ComputerName` (e.g. "ThunderPig"); on Windows `%COMPUTERNAME%`. Both differ
    from socket.gethostname(), which returns the network host (macOS gives
    'Mac.localdomain' -> 'Mac'). Falls back to the short hostname when the friendly
    name is unavailable/blank (e.g. Linux, or scutil missing)."""
    import socket

    if sys.platform == "darwin":
        try:
            import subprocess

            # shell=False (list argv) per constitution §2 — no shell injection.
            # errors="replace" so a ComputerName with non-UTF-8 bytes can't raise
            # UnicodeDecodeError (a ValueError, not caught below) and crash scaffold;
            # broad except is deliberate — any failure falls back to the hostname.
            out = subprocess.run(
                ["scutil", "--get", "ComputerName"],
                capture_output=True, text=True, timeout=2, errors="replace",
            )
            name = out.stdout.strip()
            if name:
                return name
        except (OSError, subprocess.SubprocessError) as e:
            # Not silent (constitution §4): note why, then fall back to the hostname.
            print(f"taskpaw: could not read macOS ComputerName ({e}); using hostname",
                  file=sys.stderr)
    elif sys.platform == "win32":
        name = (os.environ.get("COMPUTERNAME") or "").strip()
        if name:
            return name
    # Guard the split result too: gethostname() == "." would yield "" (Kimi).
    return (socket.gethostname() or "agent").split(".")[0] or "agent"


def scaffold(role: str, force: bool = False) -> tuple[Path, bool]:
    """Copy the example config for `role` to its platform path.

    Returns (path, created). created=False means a config was already there and
    was left untouched (unless force=True).
    """
    if role == "agent":
        dst, src = agent_service.default_config_path(), EXAMPLES / "agent.example.yaml"
    elif role == "hub":
        dst, src = hub_service.default_config_path(), EXAMPLES / "hub.example.yaml"
    else:
        raise ValueError(f"unknown role: {role!r}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and not force:
        return dst, False
    text = Path(src).read_text(encoding="utf-8")
    if role == "agent":
        # Give each fresh agent a UNIQUE identity so multiple LAN installs don't
        # all advertise "my-agent" (display/log collisions) (Kimi). `machine` is the
        # human display name (the OS friendly computer name, e.g. "ThunderPig") so
        # the Hub and the auto host_metrics monitor ("<machine>-host") read the way
        # the user names the box; `server_id` is a slug of it + a short uuid (a
        # stable, space-free technical id). Targeted line replace keeps the comments.
        import json
        import re
        import uuid
        friendly = _friendly_machine_name()
        slug = re.sub(r"[^A-Za-z0-9]+", "-", friendly).strip("-").lower() or "agent"
        # `machine` is a free-form OS name that can contain YAML metacharacters
        # (spaces, '#', ':', "'s", non-ASCII) — emit it as a quoted scalar so a name
        # like "Studio #1" or "我的电脑" can't corrupt/invalidate the config (Codex,
        # Kimi). json.dumps yields a YAML-valid double-quoted string; ensure_ascii=
        # False keeps unicode names readable in the file. `server_id` is already a
        # [a-z0-9-] slug, so it needs no quoting.
        text = text.replace("server_id: my-agent",
                            f"server_id: {slug}-{uuid.uuid4().hex[:6]}")
        text = text.replace("machine: my-machine",
                            f"machine: {json.dumps(friendly, ensure_ascii=False)}")
    # Atomic write (repo invariant: configs are reader-visible state) — a tmp file
    # in the same dir + fsync + os.replace, so an interrupted/power-lost bootstrap
    # never leaves a truncated config the service would read.
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, dst)
    return dst, True


def apply_agent_edits(config_path: Path, preset: str | None = None,
                      bind_host: str | None = None) -> int:
    """Apply post-scaffold edits to an agent.yaml (atomic save_yaml).

    - preset="moomoo": set machine/server_id and inject the four life-signs
      (real #13 defaults) → zero hand-editing.
    - bind_host: set the LAN address the Hub polls (a separate Hub can't reach a
      loopback bind — Codex).
    Returns the monitor count after editing.
    """
    from taskpaw_v3.core.config import AgentConfig, load_yaml, save_yaml
    from taskpaw_v3.monitors.presets.moomoo import moomoo_preset

    cfg: AgentConfig = load_yaml(AgentConfig, config_path)  # type: ignore[assignment]
    if preset == "moomoo":
        cfg.machine = "moomoo"
        cfg.server_id = "moomoo-prod"
        cfg.monitors = moomoo_preset()
    if bind_host:
        cfg.bind_host = bind_host
    save_yaml(cfg, config_path)
    return len(cfg.monitors)


def _parse_agent_spec(spec: str) -> tuple[str, str, int]:
    """`name,ip[,port]` → (name, ip, port). Raises ValueError on bad input."""
    parts = [p.strip() for p in spec.split(",")]
    if len(parts) < 2 or not parts[0] or not parts[1]:
        raise ValueError(f"agent spec must be 'name,ip[,port]', got {spec!r}")
    name, ip = parts[0], parts[1]
    port = 5680
    if len(parts) >= 3 and parts[2]:
        port = int(parts[2])
        if not (1 <= port <= 65535):
            raise ValueError(f"port must be 1–65535, got {port}")
    return name, ip, port


def register_agents(specs: list[str]) -> list[str]:
    """Register agent specs into the Hub's store. Skips duplicates (by name).
    Returns human-readable lines describing what happened."""
    from taskpaw_v3.core.config import HubConfig, load_yaml
    from taskpaw_v3.hub.server.store import HubStore

    parsed = [_parse_agent_spec(s) for s in specs]  # validate all before opening DB
    # Open the SAME db the running hub uses (HubConfig.data_dir/hub.db), not the
    # config dir — else registered agents land in a db the hub never reads (Kimi).
    cfg_path = hub_service.default_config_path()
    if cfg_path.exists():
        cfg: HubConfig = load_yaml(HubConfig, cfg_path)  # type: ignore[assignment]
    else:
        cfg = HubConfig()
    db = hub_service.db_path_for(cfg)
    legacy = hub_service.legacy_db_conflict(cfg_path, db)
    if legacy:
        # FATAL (consistent with the hub `run` guard): don't register into a new
        # empty db while a real legacy one exists beside the config (Kimi).
        raise RuntimeError(
            f"would register into {db}, but an older hub.db exists at {legacy}. "
            f"Move it (mv '{legacy}' '{db}') or set data_dir first.")
    store = HubStore(db)
    lines: list[str] = []
    try:
        # Inside the try so a failing list_servers() (corrupt/locked db) still
        # closes the connection via finally (Kimi).
        existing = {s["name"] for s in store.list_servers()}
        for name, ip, port in parsed:
            if name in existing:
                lines.append(f"  · {name} already registered — skipped")
                continue
            store.add_server(name, ip, port)
            existing.add(name)
            lines.append(f"  + {name} @ {ip}:{port}")
    finally:
        store.close()
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m taskpaw_v3.bootstrap",
                                 description="Scaffold + launch a V3 Hub or agent.")
    ap.add_argument("role", choices=["agent", "hub"])
    ap.add_argument("--force", action="store_true", help="overwrite an existing config")
    ap.add_argument("--run", action="store_true", help="launch the service after setup")
    ap.add_argument("--agent", action="append", default=[], metavar="name,ip[,port]",
                    help="(hub only, repeatable) register an agent to poll")
    ap.add_argument("--preset", choices=["moomoo"], default=None,
                    help="(agent only) fill monitors from a built-in preset")
    ap.add_argument("--bind-host", default=None, metavar="IP",
                    help="(agent only) LAN address the Hub polls (default loopback)")
    args = ap.parse_args(argv)

    if args.agent and args.role != "hub":
        print("error: --agent is only valid for the hub role", file=sys.stderr)
        return 2
    if (args.preset or args.bind_host) and args.role != "agent":
        print("error: --preset/--bind-host are only valid for the agent role", file=sys.stderr)
        return 2

    try:
        path, created = scaffold(args.role, force=args.force)
    except OSError as e:
        print(f"error: could not write config: {e}", file=sys.stderr)
        return 1
    print(f"{'created' if created else 'kept existing'} config: {path}")
    if not created:
        print("  (use --force to overwrite with the example)")

    if args.preset or args.bind_host:
        if not created and not args.force:
            print(f"error: {path} already exists — refusing to edit it "
                  f"(preset/bind-host); re-run with --force", file=sys.stderr)
            return 2
        n = apply_agent_edits(path, preset=args.preset, bind_host=args.bind_host)
        if args.preset:
            print(f"applied {args.preset} preset: {n} monitors (machine=moomoo)")
        if args.bind_host:
            print(f"set bind_host = {args.bind_host} (Hub polls this address)")

    if args.role == "hub" and args.agent:
        try:
            lines = register_agents(args.agent)
        except Exception as e:
            # incl. bad spec (ValueError), legacy conflict (RuntimeError), and
            # malformed/unreadable hub.yaml (yaml/OS errors) — clean exit, no
            # traceback (Kimi).
            print(f"error: {e}", file=sys.stderr)
            return 2
        print("registered agents:")
        print("\n".join(lines))

    print()
    if args.run:
        print(f"starting {args.role}…  (Ctrl-C to stop)")
        if args.role == "agent":
            return agent_service.main()
        return hub_service.main()

    # Not running — tell the operator exactly what to do next.
    if args.role == "agent":
        if args.preset:
            print(f"next: {path} is ready (preset monitors set). Start it:")
            print("      python -m taskpaw_v3.agent")
        else:
            print(f"next: edit {path} (server_id/machine, bind_host for LAN, monitors),")
            print("      then start it:  python -m taskpaw_v3.agent")
    else:
        print(f"next: edit {path} if needed, register agents with")
        print("      python -m taskpaw_v3.bootstrap hub --agent name,ip  (or `hub add-server`),")
        print("      then start it:  python -m taskpaw_v3.hub run")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
