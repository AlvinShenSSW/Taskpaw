"""moomoo (MQT) four-life-signs preset (V3 design §5.1; facts from #13 recon).

Operator scope: monitor only the four life-signs' liveness — the trading layer is
MQT's own concern. The preset emits monitor specs ({type_id, config}) that
build_supervisor() turns into running monitors. All paths/names/ports are
overridable; defaults are the #13-confirmed values for the current moomoo Mac.

#13-confirmed defaults:
- pm2 God Daemon: process `PM2 .* God Daemon`
- orchestrator:   pm2 job `orchestrator` → script `strategy_orchestrator.py`
- OpenD:          TCP 127.0.0.1:11111 (loopback)
- heartbeat:      ~/Documents/Workspace/moomoo/runtime/orchestrator_heartbeat.json
                  status-aware (hibernating ≠ stale); grace per recon
"""

from __future__ import annotations

import os
from typing import Any

# Default heartbeat path (#13). Override per-machine via heartbeat_path.
DEFAULT_HEARTBEAT = os.path.expanduser(
    "~/Documents/Workspace/moomoo/runtime/orchestrator_heartbeat.json"
)
# grace = orchestrator self-grace (in next_check_due_utc) + watcher extra 300s +
# startup 600s. We add the watcher's extra grace on top of the file's due time.
DEFAULT_GRACE_SECONDS = 300.0
OPEND_PORT = 11111


def moomoo_preset(
    *,
    god_daemon_pattern: str = r"PM2 .*God Daemon",
    orchestrator_pattern: str = r"strategy_orchestrator\.py",
    opend_host: str = "127.0.0.1",
    opend_port: int = OPEND_PORT,
    heartbeat_path: str = DEFAULT_HEARTBEAT,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    poll_interval: float = 30.0,
) -> list[dict[str, Any]]:
    """Return the four monitor specs for the moomoo agent."""
    return [
        {  # ① process-manager daemon alive (loses self-healing if down)
            "type_id": "process",
            "config": {
                "name": "moomoo-pm2-daemon",
                "pattern": god_daemon_pattern,
                "poll_interval": poll_interval,
            },
        },
        {  # ② orchestrator process running (not online ⇒ no trading loop)
            "type_id": "process",
            "config": {
                "name": "moomoo-orchestrator",
                "pattern": orchestrator_pattern,
                "poll_interval": poll_interval,
            },
        },
        {  # ③ OpenD gateway listening (down ⇒ trading paralysed)
            "type_id": "tcp_check",
            "config": {
                "name": "moomoo-opend",
                "host": opend_host,
                "port": opend_port,
                "poll_interval": poll_interval,
            },
        },
        {  # ④ orchestrator heartbeat fresh (HUNG = alive but stuck)
            "type_id": "heartbeat",
            "config": {
                "name": "moomoo-orchestrator-heartbeat",
                "path": heartbeat_path,
                "grace_seconds": grace_seconds,
                "poll_interval": poll_interval,
            },
        },
    ]
