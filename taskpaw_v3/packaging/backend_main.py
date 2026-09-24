"""Single bundled-backend entry point (#40/#41).

The Tauri shell spawns ONE backend executable and tells it which role to run:

    taskpaw-backend agent   # headless agent (reads agent.yaml)
    taskpaw-backend hub      # headless hub (reads hub.yaml)
    taskpaw-backend llm-worker  # LLM request sidecar (#178; settings via env)

PyInstaller bundles this module into `taskpaw-backend`; the shell resolves that
sidecar next to the app and runs it with the role. Falls back to `agent`.

Contract: the ONLY supported argument is the role. The bundled backend reads its
config from the platform config dir (agent.yaml / hub.yaml) — it intentionally
takes no flags. For richer CLI (custom --config/--db, server admin) run the
modules directly: `python -m taskpaw_v3.agent` / `python -m taskpaw_v3.hub`.
"""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    role = argv[0].lower() if argv else "agent"
    if role == "hub":
        from taskpaw_v3.hub.server.service import main as hub_main

        return hub_main()
    if role == "agent":
        from taskpaw_v3.agent.server.service import main as agent_main

        return agent_main()
    if role == "llm-worker":
        # The terminable child that runs chat() for #177 (#178): spawned by the
        # agent itself via core.llm_worker.worker_argv(), never by the shell.
        from taskpaw_v3.core.llm_worker import main as llm_worker_main

        return llm_worker_main()
    print(
        f"unknown backend role: {role!r} (expected 'agent', 'hub' or 'llm-worker')",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
