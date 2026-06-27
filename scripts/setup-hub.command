#!/usr/bin/env bash
# Double-click on macOS to set up + start the TaskPaw V3 Hub.
# Edit the AGENTS line below with the agents this Hub should poll, then run.
set -euo pipefail

# Repo root = two levels up from this script (scripts/ -> repo).
cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

# --- EDIT ME: the agents to poll, as repeated --agent name,ip[,port] ---------
AGENTS=(
  --agent mac-self,127.0.0.1
  # --agent moomoo,192.168.1.50
)

echo "==> Installing V3 dependencies (uv sync --extra v3)…"
uv sync --extra v3

echo "==> Scaffolding config + registering agents…"
uv run python -m taskpaw_v3.bootstrap hub "${AGENTS[@]}"

echo "==> Starting Hub (Ctrl-C to stop)…"
exec uv run python -m taskpaw_v3.hub run
