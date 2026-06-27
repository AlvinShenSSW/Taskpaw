"""Run the headless agent: `python -m taskpaw_v3.agent`.

Thin alias for the agent service entrypoint (loads agent.yaml from the platform
config dir and runs until a stop signal).
"""

from __future__ import annotations

from taskpaw_v3.agent.server.service import main

if __name__ == "__main__":
    raise SystemExit(main())
