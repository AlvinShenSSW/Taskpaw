# TaskPaw V3 deployment

Three roles, deployed independently. **Agents** observe a machine and expose a
read API; the **Hub** polls all agents and drives notifications. A machine can be
both (e.g. the Mac is the Hub *and* an agent for itself).

| Role          | Config file | Run command                         | Listens |
|---------------|-------------|-------------------------------------|---------|
| Agent         | `agent.yaml`| `python -m taskpaw_v3.agent`        | `bind_port` 5680 (LAN), control 5681 (loopback) |
| Hub           | `hub.yaml`  | `python -m taskpaw_v3.hub run`      | `bind_port` 5690 |

Config locations per OS:

| OS      | config dir |
|---------|-----------|
| macOS   | `~/Library/Application Support/TaskPaw/` |
| Windows | `%APPDATA%\TaskPaw\` |
| Linux   | `/etc/taskpaw/` |

Examples to copy: [`taskpaw_v3/examples/agent.example.yaml`](../../taskpaw_v3/examples/agent.example.yaml),
[`hub.example.yaml`](../../taskpaw_v3/examples/hub.example.yaml).

Prereq on every machine: Python 3.10+ and the project installed **with the V3
dependency extra** — the V3 runtime imports `pydantic`, `PyYAML`, `fastapi`, and
`uvicorn`, which live in the optional `v3` extra (a base install omits them and
`python -m taskpaw_v3.*` fails on import):

```bash
uv sync --extra v3            # then prefix commands with `uv run`, e.g. uv run python -m taskpaw_v3.hub run
# or, with pip:
pip install -e '.[v3]'
```

Commands below assume that environment is active (or prefixed with `uv run`).

---

## One-click setup (recommended)

Don't hand-type the steps — use the wrappers in `scripts/`, which install deps,
scaffold the config, register agents, and start the service:

| Machine             | Do this |
|---------------------|---------|
| **Mac Hub**         | edit the `AGENTS=(…)` list at the top of `scripts/setup-hub.command`, then **double-click** it (or `bash scripts/setup-hub.command`) |
| **Mac agent** (self)| **double-click** `scripts/setup-agent.command` |
| **moomoo Mac** (MQT trading box) | **double-click** `scripts/setup-agent-moomoo.command` — fills the four life-signs preset, zero edits |
| **Windows Lada/ComfyUI box** | right-click `scripts\setup-agent.ps1` → **Run with PowerShell** (migrates the V2 config, scaffolds, then starts) |

> Topology: **moomoo is a Mac** (the MQT trading server — pm2 / orchestrator /
> OpenD / heartbeat). The **Windows** box(es) run **Lada / ComfyUI** (GPU) and are
> the ones with a V2 config to migrate. The **Hub** is a separate Mac.

> First double-click on macOS may be blocked by Gatekeeper — right-click → Open
> once, or `chmod +x scripts/*.command` (already executable in the repo).

Under the hood these call the cross-platform bootstrapper, which you can also run
directly:

```bash
python -m taskpaw_v3.bootstrap agent [--run]
python -m taskpaw_v3.bootstrap hub --agent moomoo,192.168.1.50 --agent mac,127.0.0.1 [--run]
```

It copies the example config into place (never clobbering an existing one unless
`--force`), registers the listed agents, and with `--run` launches the service.
The manual steps below are the fallback / reference.

---

## 1. Mac Hub machine

The Hub polls agents and remembers them in its own SQLite DB (`hub.db`, created
next to `hub.yaml`) — the agent list is **not** in the yaml.

```bash
# 1. config
mkdir -p ~/Library/Application\ Support/TaskPaw
cp taskpaw_v3/examples/hub.example.yaml ~/Library/Application\ Support/TaskPaw/hub.yaml
#    edit: machine name, poll_interval, polling_token (if agents use api_token)

# 2. register the agents it should poll (ip = each agent's LAN bind_host)
python -m taskpaw_v3.hub add-server --name moomoo   --ip 192.168.1.50 --port 5680
python -m taskpaw_v3.hub add-server --name mac-self --ip 127.0.0.1     --port 5680
python -m taskpaw_v3.hub list-servers

# 3. run
python -m taskpaw_v3.hub run        # Hub API on 127.0.0.1:5690
```

Manage agents anytime: `list-servers`, `enable-server --id N`,
`disable-server --id N`, `remove-server --id N`.

> `polling_token` must equal each agent's `api_token`. If the agents are open
> (empty token), leave it empty.

---

## 2. Mac agent machine (self-monitor)

If a Mac (e.g. the Hub's own box) should monitor itself, run an agent on it.
`host_metrics: true` already gives CPU/mem/disk/net with **no** monitors
configured.

```bash
mkdir -p ~/Library/Application\ Support/TaskPaw
cp taskpaw_v3/examples/agent.example.yaml ~/Library/Application\ Support/TaskPaw/agent.yaml
#    edit: server_id, machine
#    if the Hub is on a DIFFERENT machine, set bind_host to this Mac's LAN IP;
#    if Hub + agent are the SAME Mac, bind_host: 127.0.0.1 is fine (register it
#    in the Hub as --ip 127.0.0.1).

python -m taskpaw_v3.agent             # read API on bind_host:5680
```

macOS ignores GPU by design (host_metrics reports CPU/mem/disk/net only).

To monitor the dev-agent Claude/Codex activity on a dev Mac, see
[dev-agent-activity.md](dev-agent-activity.md) and add a `state_file` monitor.

---

## 3. moomoo Mac (MQT trading server)

A **Mac**, not Windows. Monitors the four life-signs (pm2 God Daemon /
`orchestrator` process / OpenD `:11111` / orchestrator heartbeat). The
`--preset moomoo` defaults are the #13-confirmed real values, so it needs no
edits unless this box's heartbeat path or OpenD port differ.

```bash
python -m taskpaw_v3.bootstrap agent --preset moomoo     # writes the 4-monitor agent.yaml
python -m taskpaw_v3.agent
```

- TaskPaw only **alerts**; moomoo's own `orch-watchdog` does the self-healing.
- If the Hub is on another Mac, set `bind_host` to this Mac's LAN IP and register
  it: `add-server --name moomoo --ip <that IP>`.

---

## 4. Windows Lada / ComfyUI box (GPU)

Where the V2 `taskpaw.py` ran — so this is the machine with a config to migrate,
and where GPU/VRAM monitoring lives.

```powershell
# 1. migrate the existing V2 config to V3 monitors (read-only preview first)
python -m taskpaw_v3.migrate                      # prints the plan
python -m taskpaw_v3.migrate --yaml > monitors.yaml

# 2. config
mkdir %APPDATA%\TaskPaw
copy taskpaw_v3\examples\agent.example.yaml %APPDATA%\TaskPaw\agent.yaml
#    edit server_id/machine, set bind_host to this box's LAN IP, and replace the
#    `monitors:` block with the one from monitors.yaml

# 3. run
python -m taskpaw_v3.agent
```

- `host_metrics: true` reports **GPU + VRAM** on Windows (via `nvidia-smi`).
- Set `bind_host` to this box's LAN IP so the Mac Hub can reach it, then register
  it on the Hub: `add-server --name <this box> --ip <that IP>`.
- Keep everything on a trusted LAN / Tailscale — never expose `bind_host` to the
  public internet (constitution §2). Use `api_token` + matching Hub
  `polling_token` if the network isn't fully trusted.

---

## Verify the fleet

```bash
curl -s http://127.0.0.1:5690/status | python -m json.tool   # on the Hub
```

`servers` lists each registered agent; the Hub polls every `poll_interval` and
fans completion events to OpenClaw if enabled. The Tauri UI (#19) renders the
same data — point it at the Hub for the dashboard.

## Running as a background service

These are plain foreground processes. For always-on operation wrap each in your
OS service manager — `launchd` (macOS), a Scheduled Task / NSSM (Windows), or
`systemd` (Linux) — invoking the same `python -m …` command. The processes
handle SIGTERM for a graceful shutdown.

## Logs & troubleshooting

The packaged desktop app (Tauri shell) has no console, so it routes the bundled
backend's stderr — where the logs go — to a file per OS:

| OS      | Backend log file |
| ------- | ---------------- |
| macOS   | `~/Library/Logs/TaskPaw/taskpaw-backend-<role>.log` |
| Windows | `%APPDATA%\TaskPaw\taskpaw-backend-<role>.log` |
| Linux   | inherited stderr (run under `systemd`/terminal, read via `journalctl`) |

`<role>` is `agent` or `hub`, so running both on one account keeps separate logs.
The file appends across relaunches; at each launch, if it already exceeds ~5 MB it
is rolled to `…-<role>.log.1` (one backup). (Dev builds — `cargo tauri dev` —
inherit stderr to the terminal instead.)

If the UI loads but shows no data, tail that file first — a backend that exits on
a bad config or a port clash reports it there.

> **macOS residual risk (orphan backend).** When you quit normally (window close
> / Cmd-Q) the shell reaps the backend. But if the shell is *hard*-killed
> (SIGKILL, OOM, force-quit), macOS has no `PR_SET_PDEATHSIG` equivalent (Linux
> does, Windows uses a Job Object). The backend is a long-running server, so it
> does **not** exit on its own — it gets reparented to `launchd` and keeps running,
> holding its port. Find it with `pgrep -fl taskpaw-backend` and clear the right one
> with `pkill -f "taskpaw-backend.*<role>"` (e.g. `…agent`) before relaunching — a
> relaunch that can't bind its port is the symptom.
