"""Headless agent service entrypoint (no UI).

Loads agent.yaml from the platform config dir and runs the agent until a stop
signal. The interactive (Tauri) mode reuses run_agent() directly instead.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.config import AgentConfig, load_yaml
from agent.server.launcher import run_agent


def default_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home())) / "TaskPaw"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "TaskPaw"
    else:
        base = Path("/etc/taskpaw")
    return base / "agent.yaml"


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    path = default_config_path()
    if not path.exists():
        print(f"No agent config at {path}", file=sys.stderr)
        return 1
    config: AgentConfig = load_yaml(AgentConfig, path)  # type: ignore[assignment]
    run_agent(config, block=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
