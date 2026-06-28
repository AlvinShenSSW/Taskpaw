"""Single bundled-backend entry point (#40/#41).

The Tauri shell spawns ONE backend executable and tells it which role to run:

    taskpaw-backend agent   # headless agent (reads agent.yaml)
    taskpaw-backend hub      # headless hub (reads hub.yaml)

PyInstaller bundles this module into `taskpaw-backend`; the shell resolves that
sidecar next to the app and runs it with the role. Falls back to `agent`.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    role = argv[0].lower() if argv else "agent"
    if role == "hub":
        from taskpaw_v3.hub.server.service import main as hub_main
        return hub_main()
    if role in ("agent", ""):
        from taskpaw_v3.agent.server.service import main as agent_main
        return agent_main()
    print(f"unknown backend role: {role!r} (expected 'agent' or 'hub')", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
