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
  echo "==> Scaffolding moomoo agent config (four life-signs preset)…"
  uv run python -m taskpaw_v3.bootstrap agent --preset moomoo
else
  echo "==> Existing config found, leaving it untouched: $CFG"
fi

echo "==> Starting moomoo agent (Ctrl-C to stop)…"
exec uv run python -m taskpaw_v3.agent
