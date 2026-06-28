#!/usr/bin/env bash
# Double-click on the moomoo (MQT trading) Mac to set up + start its agent.
# First run fills the four life-signs monitors from the built-in moomoo preset
# (#13 real values), so no editing is needed. Re-runs just start the agent and
# never clobber your edits. If the heartbeat path / OpenD port differ on this
# box, edit ~/Library/Application Support/TaskPaw/agent.yaml after the first run.
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"

echo "==> Installing V3 dependencies (uv sync --extra v3)…"
uv sync --extra v3

CFG="$HOME/Library/Application Support/TaskPaw/agent.yaml"
if [ ! -f "$CFG" ]; then
  # The Hub is a separate Mac, so the agent must bind a LAN IP (not loopback).
  LANIP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
  if [ -n "$LANIP" ]; then
    echo "==> Scaffolding moomoo agent (preset + bind_host $LANIP)…"
    uv run python -m taskpaw_v3.bootstrap agent --preset moomoo --bind-host "$LANIP"
    echo "    Register on the Hub:  python -m taskpaw_v3.hub add-server --name moomoo --ip $LANIP"
  else
    echo "==> Could not auto-detect a LAN IP. Scaffolding with loopback bind…"
    uv run python -m taskpaw_v3.bootstrap agent --preset moomoo
    echo "    NOTE: a Hub on another machine can't reach 127.0.0.1 — edit bind_host"
    echo "          in $CFG to this Mac's LAN IP, then register it on the Hub."
  fi
else
  echo "==> Existing config found, leaving it untouched: $CFG"
fi

echo "==> Starting moomoo agent (Ctrl-C to stop)…"
exec uv run python -m taskpaw_v3.agent
