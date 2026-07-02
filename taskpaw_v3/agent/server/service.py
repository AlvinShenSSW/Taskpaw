"""Headless agent service entrypoint (no UI).

Loads agent.yaml from the platform config dir and runs the agent until a stop
signal. The interactive (Tauri) mode reuses run_agent() directly instead.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from taskpaw_v3.agent.server.launcher import run_agent
from taskpaw_v3.core.config import AgentConfig, load_yaml


def default_config_path() -> Path:
    if sys.platform == "win32":
        # `or` (not get-default): APPDATA can be set to an empty string.
        base = Path(os.environ.get("APPDATA") or Path.home()) / "TaskPaw"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "TaskPaw"
    else:
        base = Path("/etc/taskpaw")
    return base / "agent.yaml"


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    path = default_config_path()
    if not path.exists():
        # Self-initialize a default config so a fresh install / first run "just
        # works" (host_metrics baseline on loopback) instead of exiting and
        # leaving the packaged UI with no backend (#40 Codex). Edit afterwards.
        from taskpaw_v3 import bootstrap

        try:
            bootstrap.scaffold("agent")
        except OSError as e:
            # e.g. Linux /etc/taskpaw without root — fail cleanly, don't crash (Kimi).
            print(
                f"No agent config at {path} and could not auto-create it: {e}\n"
                f"  Create it manually (see taskpaw_v3/examples/agent.example.yaml) "
                f"or run with write access to that directory.",
                file=sys.stderr,
            )
            return 1
        logging.getLogger("taskpaw.agent").info(
            "created default agent config at %s", path
        )
    config: AgentConfig = load_yaml(AgentConfig, path)  # type: ignore[assignment]
    # Persist the monotonic event-id counter next to the config.
    state_path = path.with_name("agent.state.json")
    # Pass the config path so the control API can persist add/remove/enable (#57).
    run_agent(config, state_path=state_path, config_path=path, block=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
