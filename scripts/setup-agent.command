#!/usr/bin/env bash
# Double-click on macOS to set up + start a TaskPaw V3 agent (monitors this Mac).
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

echo "==> Installing V3 dependencies (uv sync --extra v3)…"
uv sync --extra v3

echo "==> Scaffolding agent config…"
uv run python -m taskpaw_v3.bootstrap agent
# host_metrics alone already reports CPU/mem/disk/net. Edit the config printed
# above (server_id/machine, and bind_host if the Hub is on another machine)
# before exposing it to a remote Hub.

echo "==> Starting agent (Ctrl-C to stop)…"
exec uv run python -m taskpaw_v3.agent
