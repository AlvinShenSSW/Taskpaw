# Run on the Windows Lada / ComfyUI (GPU) box to set up + start a TaskPaw V3 agent.
#   Right-click → "Run with PowerShell", or:  powershell -ExecutionPolicy Bypass -File scripts\setup-agent.ps1
# Migrates the existing V2 (taskpaw.py) config to V3 monitors, scaffolds
# agent.yaml, and starts. (This is NOT moomoo — moomoo is a Mac; use
# setup-agent-moomoo.command there.)
$ErrorActionPreference = "Stop"

# Repo root = parent of this script's folder.
Set-Location (Split-Path -Parent $PSScriptRoot)
$env:Path = "$HOME\.local\bin;" + $env:Path

Write-Host "==> Installing V3 dependencies (uv sync --extra v3)..."
uv sync --extra v3

$cfgDir = Join-Path $env:APPDATA "TaskPaw"
New-Item -ItemType Directory -Force -Path $cfgDir | Out-Null

Write-Host "==> Migrating V2 config -> V3 monitors (read-only preview)..."
uv run python -m taskpaw_v3.migrate
Write-Host ""
Write-Host "    Writing the migrated monitors block to $cfgDir\monitors.yaml"
uv run python -m taskpaw_v3.migrate --yaml | Out-File -Encoding utf8 (Join-Path $cfgDir "monitors.yaml")

Write-Host "==> Scaffolding agent config..."
uv run python -m taskpaw_v3.bootstrap agent

Write-Host ""
Write-Host "ACTION NEEDED before the agent is useful:"
Write-Host "  1. Edit $cfgDir\agent.yaml -> set server_id/machine and bind_host = this box's LAN IP"
Write-Host "  2. Replace its 'monitors: []' with the contents of $cfgDir\monitors.yaml"
Write-Host "  3. On the Mac Hub:  python -m taskpaw_v3.hub add-server --name <this box> --ip <that LAN IP>"
Write-Host ""
Read-Host "Press Enter to start the agent now (Ctrl-C later to stop)"

uv run python -m taskpaw_v3.agent
