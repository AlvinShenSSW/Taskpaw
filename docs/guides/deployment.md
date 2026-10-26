# TaskPaw V3 deployment

Three roles, deployed independently. **Agents** observe a machine and expose a
read API; the **Hub** polls all agents and drives notifications. A machine can be
both (e.g. the Mac is the Hub *and* an agent for itself).

Hub, Windows Agent, Mac Agent, Windows media controllers and GPU workers may
run **different application versions**. Release and upgrade only the components
affected by a change. A Hub-only update does not require upgrading Agents, and
an unrelated controller update must not stop a compatible worker taking films.
The version copies inside one installer still agree; installations on different
machines need not. Upgrade busy Agents only at an operator-selected idle point.

Compatibility follows the workflow protocol and actual feature requirements,
not equal application version strings. A genuinely incompatible feature may
require a documented minimum build (for example a future feature requiring
4.0), but the message must identify the feature and affected endpoint. An
upgrade notice must not cancel existing work. Optional readings, such as
temperatures absent from an older Agent, remain unavailable without blocking
the rest of monitoring.

| Role          | Config file | Run command                         | Listens |
|---------------|-------------|-------------------------------------|---------|
| Agent         | `agent.yaml`| `python -m taskpaw_v3.agent`        | `bind_port` 5680 (LAN), control 5681 (loopback) |
| Hub           | `hub.yaml`  | `python -m taskpaw_v3.hub run`      | `bind_port` 5690 (read), control 5691 (loopback) |

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

## 多机媒体工作流部署

The media workflow (EPIC #271, phase 2) runs on several machines at home. With the
packaged app you edit **no** yaml file: the first-run wizard writes everything, and
the two settings that need a restart end with a 立即重启 button. Everything below
is for one user on a trusted home LAN.

### Roles

| Role | Machine | Runs |
|------|---------|------|
| 媒体主控 (controller) | Windows only | 媒体处理中心, the INBOX watcher, local ASR (WhisperJAV), translate, Miko scrape and Filmpaw ingest. It dispatches restore and never restores itself (OD1, OD7). Any GPU works, e.g. the RTX 5060 8 GB. |
| GPU 工作机 (worker) | Windows only, GPU of **16 GB or more** | Jasna restore, 1080p and 4K, and nothing else (OD7). A card under 16 GB never restores (OD5): the wizard disables the card with 「本机显卡不足 16 GB」. |
| Mac Hub | macOS | Only the Hub: the read-only 媒体处理 tab, notifications, and the **single** consumer of each Agent's `/events` (see the section below). The Mac is never a controller or a worker: those wizard cards read 「仅支持 Windows」. |

Windows is the only controller and worker platform. The reference fleet is one 5060
8 GB controller plus four 16 GB workers (3 × 5060 Ti, 1 × 5070 Ti).

### Ports and firewall

| Port | What | Reachable from | Firewall rule the wizard creates |
|------|------|----------------|----------------------------------|
| 5680 | Agent read API (`/status`, `/workflow/*`) | LAN: the Hub, the controller (power and status of a worker) | The rule 「TaskPaw Agent」: inbound, **private profile only**, scoped to the TaskPaw backend program, with port 5680. Added on every Windows machine with 局域网访问 on. |
| 5681 | Agent control API (the app's own UI) | loopback only | None. Never opened. |
| 5682 | Worker API (`/worker/v1`: pairing, heartbeat, claim, transfer) | LAN: workers → controller | Controller only, and only while 允许 GPU 工作机接入 is on. The same 「TaskPaw Agent」 rule gains port 5682 (5680 + 5682). Workers listen on 5680 only. |
| 5690 | Hub read API | LAN | None: the wizard is Windows-only and the Hub runs on the Mac. macOS asks once to allow incoming connections; click 允许. |
| 5691 | Hub control API | loopback only | None. Never opened. |

The rule is added by **one** UAC prompt on 完成, under the TaskPaw publisher. If you
answer 「否」 nothing breaks: 设置 → 网络与访问 then offers 添加入站规则 to retry. If
the machine's network is marked Public the wizard warns: switch it to Private, as
the rule does not apply to Public.

### Zero-config steps per role

Install the packaged TaskPaw app, open it, and the wizard 「首次启动 · 选择角色」
opens by itself. No file is edited on any machine. **Set up the controller first**:
the workers need its 访问令牌 and its 配对码.

**媒体主控 (Windows)**

1. 角色: pick 媒体主控. The card shows the detected GPU.
2. 网络与访问: 局域网访问 is forced on. The 访问令牌 (the one token for the whole
   house) is generated for you. 复制 it; you paste it on every other machine.
   (生成新令牌 is only for the very first machine; 重新生成 makes every other
   TaskPaw and the Hub need the new token.)
3. The controller step: INBOX and 工作目录 are suggested on the fixed drive with the
   most free space, and ffprobe, Jasna, WhisperJAV and Miko are detected. Turn on
   **允许 GPU 工作机接入** (under 高级 the 工作机接口端口 stays 5682).
4. 完成: review the rows (修改 returns to a step), accept the UAC prompt, then
   立即重启 TaskPaw.

**GPU 工作机 (Windows, 16 GB or more)**

1. 角色: pick GPU 工作机.
2. 网络与访问: click 粘贴主控的访问令牌 and paste the controller's token.
3. The worker step shows the GPU, VRAM, driver and power limit. Set 暂存目录 and
   暂存上限. Click 「保存并启动工作机（约 10 秒）」; the window restarts and resumes at
   加入主控 (leave the page open until then).
4. 加入主控: paste the 配对码 made on the controller (next section). Wait for
   「已加入主控」, then 下一步 → 完成 (UAC prompt, 立即重启 if offered).

**Mac Hub**

The Hub has no wizard. Open the app on the Mac, register the controller as a polled
server (its LAN address, port 5680, polled with the same 访问令牌), then pick it in
设置 → 主控连接 (选择主控 / 更换主控). Keep the Mac the only `/events` consumer.

### Pairing and 核验并启用

1. On the controller: 媒体处理中心 → 设备 → 添加设备. Enter a name and the worker's
   address, then 生成配对码. The code is valid for 15 minutes and works once.
2. Paste it on the worker (step 4 above). If it fails with 「配对码无效、已使用或已过期」,
   generate a new one (重新配对 on an enrolled worker does the same).
3. The worker appears on 设备 as 待核验. Open 核验并启用, compare the probe with the
   last one, leave 修复 on and confirm. Only after this does the controller send
   it films. A worker can be paused with 停领 and resumed with 恢复.

### 4K needs a re-核验 after this update

Before this update a worker advertised at most 1080p, so every worker you already
approved holds a **1080p** profile. A 16 GB worker now probes 3840×2160, and 4K
films go only to a worker whose approved profile says so.

1. Update only the controller/workers that lack the required 4K capability,
   after their active work finishes; unrelated Agents and the Mac Hub need not
   change. Different builds with compatible workflow protocols can cooperate.
2. On each 16 GB worker the device page shows 待核验 again (the proposed max is
   above the approved max). Click 核验并启用 once per worker.
3. After the update every worker re-posts its probe with the 4K max, and the controller
   de-verifies it: **every** worker shows 待核验 and takes **no** film, 1080p included,
   until you click 核验并启用. Films wait in 待处理 with 「未核验」. Re-核验 each worker once
   after updating and work resumes, including 4K on 16 GB cards. 「4K 需重新核验」 shows only
   briefly, before the worker has re-posted its probe. 「显存不足 4K」 means the worker truly
   cannot take 4K.

A card under 16 GB is refused before any profile check and cannot be approved.

### Operations

- **失联 (OD2).** A worker that stops answering shows 失联 N 分钟, and its films show
  设备失联 with 「后台重连中」. Nothing is moved automatically. Open the film:
  - 改派 moves a film that has **not started** to another machine or to 自动 (the
    new machine downloads the source again).
  - A film that already started needs 判定已停止 first. It is offered only after both
    channels have been dead for 5 minutes, and you confirm the machine is really off
    or the task stopped. The stage counts as a free retry and anything the worker
    sends later is discarded. If the machine is still running, wait for it instead.
- **Stop one film (OD3).** The only per-film stop is 停止并重新排队 in the film
  drawer: the worker kills the Jasna process tree at its next heartbeat and the
  film goes back to 待处理 without counting as a failure.
- **Tool or capability change (OD4).** A new Jasna, a changed GPU preset or
  model, or any capability change puts the worker back to 待核验: click 核验并启用
  again. The same applies to a 4K change.
- **Changing a setting that needs a restart** shows a banner: 稍后 or 立即重启. It
  refuses while a local Jasna or ASR run holds the GPU.

### Troubleshooting

- **401 or 令牌不一致.** The 访问令牌 differs between machines. Copy it from the
  controller's 设置 → 网络与访问 and use 粘贴主控的访问令牌 on the other machines;
  re-register or edit the Hub's polled server with the same token. A bad 配对码 also
  answers 401, by design: generate a fresh one.
- **The controller or a worker changed address (DHCP).** On start the app rebinds to
  the new address in the same /24 subnet and shows 「本机地址已从 A 变为 B…」 in 设置 →
  网络与访问. The other machines do not follow: update the Hub's server address and
  the worker's 配对 (重新配对 if it cannot reach the controller). Best fix: reserve a
  fixed address in your router.
- **工作流协议不兼容.** New work requires the controller's supported workflow
  protocol. The device row shows the worker and supported protocol numbers;
  check which endpoint needs a compatible build. Application versions may
  differ, and neither unrelated machines nor the Mac Hub need matching builds.
  A missing current-boot heartbeat is an unverified state: wait for the heartbeat
  rather than upgrading. Existing claims can still replay, renew, upload and
  finish; the compatibility check does not cancel them.
- **Older controllers.** This policy is implemented by 3.10.5 and later
  controllers. Older controller binaries keep their previous same-build gate;
  pre-3.10.2 controllers can also reject newer pair/boot fields. Update the
  controller to adopt the policy. Actual 3.10.2 worker source has been exercised
  against the updated controller; this is not a promise that every historical
  or future protocol is compatible.

> **Legacy / manual path.** The yaml-based steps from here on (hand-written
> `agent.yaml` and `hub.yaml`, bootstrap scripts) still work and are kept for
> developer machines. For the media workflow use the wizard above.

---

## One-click setup (developer scripts, legacy)

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
python -m taskpaw_v3.hub run        # read API :5690; local control :5691
```

Manage agents anytime: `list-servers`, `enable-server --id N`,
`disable-server --id N`, `remove-server --id N`.

> `polling_token` must equal each agent's `api_token`. If the agents are open
> (empty token), leave it empty.

---

### Media workflow monitoring and event ownership

The Mac Hub is a read-only media progress monitor. Its **媒体处理** tab shows the
workflow of the enabled Windows Agent that answers `/workflow/films`, using the
same polling token as other Agent reads. Workflow routes and images require the
Agent's existing Bearer `api_token`; disabled workflows answer 404. Operate the
workflow from the Windows Agent's **媒体处理中心** console. The Mac view cannot
enqueue, confirm, retry, pause, download models or open remote folders.

**The Mac Hub must be the only consumer of each Agent's `/events` queue.** An
acknowledgement removes those events for every reader. Do not attach another Hub
or diagnostic script that consumes `/events`. A future Windows controller Hub
must read `/status` and `/workflow/*` only and must not poll `/events`. Cursor
lineage checks protect pairing and restart continuity; they do not provide
separate queues for multiple consumers.

Workflow events share the existing Agent event queue and Hub outbox. OpenClaw
messages use the `TaskPaw 媒体 | ` prefix, and `status.md` includes a **媒体处理**
section. Workflow notification markers survive ordinary Agent restarts; this
does not add durable payload storage to the Agent's in-memory event queue.

## Existing V2 Hub queue

Before starting V3 against an existing Hub DB, stop the old Hub and perform a
read-only queue preview:

```bash
python -m taskpaw_v3.hub.server.outbox_migration --db hub.db
```

V2 did not record its source timezone. V3 normalizes aware timestamps but retains
unknown naive or malformed rows in persistent quarantine, allowing healthy Agent
polling and notifications to continue. Supply a legacy zone only when it is known
from trustworthy records; never infer it from today's host offset. Apply/retry
operations require an offline Hub and create a complete WAL-aware private backup
before their writes. That backup reflects the DB at apply/retry start, **after**
any earlier manual edits; snapshot separately before editing to retain old values.

See [the outbox migration contract](../specs/2026-10-02-outbox-timestamp-migration.md)
for preview/apply/retry commands, DST/Windows TZif handling, reason codes, failure
rollback and safe restoration. Do not hand a normalized queue back to V2; restore
a pre-conversion snapshot for downgrade. Restoring after sends can replay notices
and requires operator reconciliation.

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
same data through the authenticated loopback control listener on port 5691.

The Hub refuses HTTP 301/302/303/307/308 redirects for Agent status, events,
film lists and OpenClaw notifications, including relative or same-origin
redirects. Configure the Agent's directly serving host/port and the final
OpenClaw hook URL; a proxy must serve the response directly rather than redirect
the client. Refusal reports the HTTP status without forwarding the Bearer or
logging the redirect address/reason. A failed events fetch does not advance the
Hub's acknowledgement, and an unconfirmed notification keeps the existing
outbox retry/dead-letter policy. Agents without supported durable cursor proof remain status-only. A cursor-
verified events fetch that returns 404 fails without a bare `/events` fallback. Local automated tests verify this policy; they do not
establish compatibility with your LAN proxy configuration.

The Hub also writes `~/.taskpaw-hub/hub.db` (SQLite `status_log`) and
`~/.taskpaw-hub/status.md` each poll for OpenClaw to read without an API — see
**[openclaw-integration.md](openclaw-integration.md)** for the schema, the metric
field reference, and the `state`-not-`enabled` rule.

## 传感器（可选）

The 功耗 page shows each Windows machine's CPU and GPU temperature and its fan
speeds when LibreHardwareMonitor (LHM) runs on that machine. Without LHM an
NVIDIA GPU's temperature still comes from `nvidia-smi`, and CPU and fans show
「LHM 未运行」. Board and chassis temperatures are not shown.

On each machine, once:

1. Install LibreHardwareMonitor and run it as administrator. Install PawnIO
   when LHM asks for it; without the driver LHM cannot read the CPU or the
   motherboard fans.
2. Options: enable **Run on Windows startup**, **Start minimized** and
   **Minimize to tray**.
3. Options → **Remote Web Server**: **Run**, port **8085**, listen on
   **127.0.0.1** (loopback only; nothing is opened to the LAN).

TaskPaw reads it automatically every 5 s; there is nothing to configure in
TaskPaw. A reading older than 30 s is shown as 已过期, and a machine running an
older TaskPaw shows 「传感器未提供 · 需升级 TaskPaw」.

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

## Local UI and HTTP management

Agent control uses `control_host/control_port` (default `127.0.0.1:5681`); Hub
control uses the same fields (default `127.0.0.1:5691`). Only `127.0.0.1` or `::1`
is accepted. Hub port 5690 is read-only: move existing HTTP management requests
to the control listener. Offline Hub commands such as `add-server` and
`remove-server` still access the local database and do not need HTTP credentials.

After both ports are claimed, each backend creates a fresh random credential in
`agent.control.json` or `hub.control.json` next to the YAML it actually loaded.
The config directory must be private; unsafe files, links or permissions fail
startup. On POSIX the directory belongs to the service account and the descriptor
is mode 0600. On Windows, every directory owner/modifying principal must be the
current user, SYSTEM, Administrators, or the exact TrustedInstaller service SID.
Directory `OWNER_RIGHTS` applies only to that already-verified owner. Unknown
owners and Everyone write access still fail. The descriptor itself must remain
owned by the current user, with a protected non-inherited ACL allowing only the
current user and SYSTEM; neither TrustedInstaller nor `OWNER_RIGHTS` broadens
file access. Select a private user config directory with a trusted parent chain.
For example, CI's unknown-owner D: runner root stays rejected; its interop
fixture creates a new private directory in Python's user Temp on C: instead,
without altering existing parent ACLs.
Keep these local files out of backups or shared folders. The credential is not
stored in YAML and is independent of `api_token`, `polling_token` and LLM keys.
`TASKPAW_CONTROL_TOKEN`, `TASKPAW_UI_TOKEN` and `VITE_TASKPAW_TOKEN` cannot supply
control credentials.

Tauri reads the protected descriptor for its own backend automatically. When
explicitly attaching to an existing backend, set the nonsecret
`TASKPAW_CONTROL_CREDENTIAL_FILE` path if the config location is custom.
`TASKPAW_UI_BASE`, if supplied, must equal the descriptor's endpoint. Direct
Vite development on port 5173 asks for each role's endpoint and credential.
Copy `base_url` from the current descriptor into that role's endpoint field
(defaults: Agent 5681, Hub 5691), then enter its credential in the password
field; read both locally with a trusted editor. Only a canonical numeric
loopback origin is accepted before any request sends the credential.
`VITE_TASKPAW_BASE` does not provide an automatic endpoint fallback. The UI keeps it in memory and
clears it on reload or a 401. After a backend restart, reopen the desktop or
re-enter the new development credential; failed mutations are never replayed.

For HTTP scripts, read the current protected descriptor for every operation
with `read_control_descriptor`, construct Authorization in Python memory and
disable redirects. Do not put a token in a command argument or print the JSON.
The [local control contract](../specs/2026-10-01-local-control-auth-design.md)
contains Agent stop and Hub CRUD examples that print only HTTP status.

## Monitor changes and partial failures

The YAML configuration is the saved desired state; monitor status reports the
actual lifecycle separately. Add, Start, Edit and Delete save a candidate before
changing runtime. If runtime apply then fails, that candidate stays saved: use
Start/Edit or Stop/Delete to reconcile it. A deleted monitor whose cleanup is
unfinished remains visible and stoppable until its existing owner is released.
Retry does not create another instance while initialization or cleanup is pending.

Standalone Stop requests local cancellation before disk or plugin validation. If
it reports **stopped but not saved**, restarting the agent can run a passive
monitor whose saved `enabled` is still true. Retry Stop to save `enabled: false`.
A timeout means the API stopped waiting, not that all resources were released.
A combined Edit containing `enabled: false` must first validate its entire config;
validation timeout means the edit was not applied and did not request Stop. Use
standalone Stop independently; configuration retry waits for that validator to
exit. No late validator result is applied automatically.

Manual Start preserves the saved enable policy: the default `enabled: false`
stays false, and an existing explicit true stays true. R07 confirms worker exit
and the current plugin stop acknowledgement; full descendant/GPU cleanup and
native Windows file-lock/disk-exhaustion acceptance remain separate, unverified
platform work.
