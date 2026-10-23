# #299 Zero-config setup: first-run role wizard and every setting in the UI (frozen contract)

Date 2026-10-23 · Base `afk/claude-p2-271` c552278 · EPIC #271 · Home LAN, single user, Windows + macOS, smallest complete change. P2-4 R13–R16 + §9 (retained-state guards, ffprobe on PATH, CX24 staging normalisation) and OD7 R11 (controller runs WhisperJAV; workers need only Jasna) still apply. Visual spec: the operator-confirmed preview boards (Main, Onboard-Network, Onboard-Controller, Onboard-Worker, Onboard-Done, Settings-Workflow, Hub-Settings). Chinese copy, V3 tokens (`design-system/taskpaw-v3/MASTER.md`), no slogan headings, 390 px.

**Operator requirement:** from installing the package to real use — 媒体主控, GPU 工作机, plain TaskPaw agent, Mac Hub — the operator never touches a config file.

## 1. Requirements

| # | Requirement | Board · gap |
|---|---|---|
| R1 | **True first run, no dialog.** When `service._main` scaffolds agent.yaml it writes `agent.setup.json` `{v:1,status:"pending",step:"role",server_id}` (atomic). The backend auto-calls `initialize_state(origin="new_pairing")` only if: marker pending, its `server_id` equals the config's, and no primary/anchor/`.fault-*` file. Anything else (marker absent/done, leftover state) keeps the existing native confirmation dialog for `initialization_required`. | Main · gap 5 |
| R2 | **Wizard gate.** Agent role ready + `GET /control/setup` = `pending` → App shows `SetupWizard` instead of AgentConsole; absent/done → console as today. | Main |
| R3 | **选择角色.** Three cards. GPU 工作机 disabled with 「本机显卡不足 16 GB」 when nvidia-smi reports < 16 GB; no GPU → enabled with warning 「未检测到 NVIDIA 显卡」. 媒体主控 shows the detected GPU. | Main |
| R4 | **网络与访问.** One LAN switch, forced on for 主控/工作机. On → `lan_access:true`, `bind_host` = chosen address; off → `127.0.0.1`. Address auto-detected, NIC selectable under 高级. Empty token → generate `tp_` + 32 base62 on entering the step; 显示/复制/重新生成 (重新生成 warns 「已配对的设备需要重新配对」). Ports under 高级. Saves via `PATCH /control/config`. A changed host:port is test-bound first: 「此地址不属于本机」/「端口已被占用」. | Onboard-Network · gap 1 |
| R5 | **Address self-heal.** At startup, `lan_access` on and `bind_host` no longer local → save the primary private address (`save_yaml(backup=True)`) and use it; 网络与访问 shows 「本机地址已从 A 变为 B；Hub 和工作机需更新地址，建议在路由器里保留固定地址」. Wildcard/public still refused. | risk |
| R6 | **Windows firewall.** On 完成 with LAN on: one UAC prompt adds the inbound rule (private profile) for `bind_port` and, if 允许 GPU 工作机接入, `worker_api_port` (§4). Declined/failed never blocks: 「未添加防火墙规则，其他设备可能连不上本机；可在 设置 → 网络与访问 重试」. Public profile warned. macOS: 「首次监听时 macOS 会询问是否允许传入连接，请点“允许”」. | Onboard-Network/Done |
| R7 | **主控设置.** INBOX + 工作目录 (`data_dir`) with exists/missing, free space, drive; 「创建此目录」 for missing. Tools detected (§5) each with 更改. 允许 GPU 工作机接入 note per OD7 R11; Jasna shown only while worker access is off. Saves via `PUT /control/workflow/settings` (all P2-4 guards). | Onboard-Controller |
| R8 | **GPU 工作机设置.** GPU card (name, VRAM, driver, power limit), Jasna detection, LTX preset (`自动：<detected>` + catalogue), 暂存目录 with free space, 暂存上限 GB (warn when > free). **D1:** 加入 needs a running worker host: step **3a** saves role + fields and asks 「保存并启动工作机（约 10 秒）」 (restart), then **3b** resumes on 加入主控. 下一步 disabled until joined (「加入主控后才能继续」). | Onboard-Worker |
| R9 | **Pairing errors.** Local checks first: malformed → 「配对码格式不对，应以 TP1: 开头」; non-LAN host → existing message; unreachable → existing message; controller 401 → 「配对码无效、已使用或已过期（15 分钟内有效，只能用一次）。请在主控 设备 → 添加设备 重新生成，再粘贴一次。」 **D2:** the controller answers 401 for every bad code by design. The code is never echoed, stored or logged. | Onboard-Worker |
| R10 | **完成.** Summary rows from the saved config, each with 修改 (back to its step). Token as `tp_••••••` + `api_token_hint` (last 4 chars, only if ≥ 16 chars). Button: firewall step if needed → `POST /control/setup/complete` → 立即重启; nothing to restart → 「开始使用」. 普通 Agent: 角色 → 网络 → 完成. | Onboard-Done |
| R11 | **立即重启** (§3) wherever `restart_required` shows (ConfigSection, 媒体工作流, 功耗与主机监控, wizard, Hub settings) via one `RestartBanner` with 稍后/立即重启. Busy → 「暂不能重启：…」. Outside the shell → 「请退出并重新打开 TaskPaw」. | Settings-Workflow, Hub-Settings · gap 2 |
| R12 | **Settings sections:** 通用 (language, machine name, LLM), 网络与访问 (R4–R6), 媒体工作流, 功耗与主机监控 (`power.enabled` live; `host_metrics` restart), 关于 — side nav on desktop, select at 390 px. No visible copy names agent.yaml. PowerPage: 「功耗估算已关闭（设置 → 功耗与主机监控）」 with a link; `configHint` copy likewise. | gap 3 |
| R13 | **媒体工作流** gains 重新检测, per-tool rows (path, version/source, 更改) and directory facts. **D3:** the board's separate 数据目录 row is dropped (only `data_dir` = 工作目录 exists). | Settings-Workflow |
| R14 | **Hub settings.** OpenClaw 启用/地址/令牌 (masked)/测试发送 (「已送达 · HH:MM · 用时 x 秒」 or 无法连接/令牌错误/超时/HTTP n). 主控连接 reuses the servers API + `polling_token`. 高级: 轮询间隔 15/30/60 s, write status.md + new `status_md_path` (null → `<data_dir>/status.md`), 日志保留天数 0–3650, 局域网只读接口 (switch + token). | Hub-Settings · gap 4 |
| R15 | **Hub storage.** Store `config` overrides (the `polling_token` pattern); hub.yaml never rewritten. At startup `effective_hub_config(yaml, store)` is validated; invalid keys ignored (log names the key, never the value). Live: `openclaw_enabled`, `openclaw_token`, `openclaw_url` (poller gets a getter, **D4**), `polling_token`; everything else restart. | Hub-Settings |
| R16 | **Validation and secrets.** Per-field Chinese errors, never echoing values. 允许 GPU 工作机接入 or worker role with a loopback bind → 422 「需要先开启局域网访问」. Tokens only in generate/reveal responses with `Cache-Control: no-store`. `"***"` round-trip unchanged. Agent saves via `update_config` (`.bak`, unknown keys kept). Task logs record field names only. | all |

## 2. API changes (control API, loopback, control token)

- `GET /control/setup` → `{status, step, role}`; `PUT /control/setup {step, role}` (forward-only state machine, else 422); `POST /control/setup/complete` (needs role; for 工作机 a confirmed identity).
- `GET /control/network` → `{lan_access, bind_host, bind_port, control_port, addresses[{ip,nic,primary}], address_changed_from, token_set, api_token_hint, firewall{platform, rule: present|absent|unknown, profile}}`.
- `POST /control/network/token {action: generate|reveal}` → `{token}`.
- `POST /control/network/firewall` → `{result: added|declined|failed|unsupported}`.
- `GET /control/workflow/detect` → `{gpu, tools{ffprobe,jasna,whisperjav,miko}, suggested{inbox,data_dir,staging}}` (cached 60 s; `?refresh=1`).
- `POST /control/workflow/settings/directory/inspect {path}` → `{exists, free_bytes, volume, local}` (POST keeps the path out of access logs).
- `GET /control/restart` → `{required, fields[], blocked}`; `POST /control/restart {boot_id}` → 202, or 409 `restart_busy`/`operation_busy`.
- `AgentConfig.lan_access: bool = False`.
- Hub: `GET/PUT /config/settings` (saved, running, restart_required, token masked), `POST /config/openclaw/test`, `POST /restart`; `HubConfig.status_md_path`, `lan_access`.

## 3. Restart design

- **Backend:** `POST /control/restart` takes the settings admission lock; refuses when another save holds it (`operation_busy`), when `gpu_lease.holder()` is set (「本机正在运行：{label}」), or when a worker's `worker_busy(...)` is true. Remote work, queued films and crash-safe stages don't block. Success → 202, then `shutdown.shutdown()`, and `service.main` returns **exit code 75**. Hub likewise (only a concurrent save blocks).
- **Shell:** a watcher thread `try_wait()`s the owned child every 250 ms. Exit 75 → drop the old Job (stragglers die), respawn the same command, assign a new Job, read readiness (30 s; failure → existing startup-error path), load the new descriptor, rebuild the `main` window with the new init script (destroy, not close, so the close confirmation doesn't fire). Other exit codes as today.
- `init_script` adds `restart: true`; the UI offers the button only when it sees that flag. No new IPC permission; capabilities unchanged.
- **UI:** overlay 「正在重启 TaskPaw…」; if the window isn't replaced after 45 s → 「重启失败，请退出并重新打开 TaskPaw」.

## 4. Firewall design

Fixed backend route `taskpaw-backend agent-firewall allow --ports P[,P]` — frozen builds on Windows only, integer ports 1–65535, at most 2. It runs `%SystemRoot%\System32\netsh.exe advfirewall firewall delete rule name="TaskPaw Agent"` then `add rule name="TaskPaw Agent" dir=in action=allow protocol=TCP localport=P profile=private program="<backend exe>"`. Started via `ShellExecuteExW("runas", SW_HIDE)`, waits ≤ 120 s; error 1223 = declined. One UAC prompt under the TaskPaw publisher. Status read without elevation via one fixed `powershell -NoProfile -NonInteractive` call (`Get-NetFirewallRule` / `Get-NetConnectionProfile` → JSON; not localised), 8 s timeout, cached 30 s.

## 5. Detection rules

Every probe: fixed argv, `CREATE_NO_WINDOW`, stdin null, timeout, kill tree on timeout, ≤ 4 KB output parsed by regex, nothing logged; network paths never probed.
- **ffprobe:** saved path → `shutil.which` → `ffmpeg.bundled_ffmpeg_dir(whisperjav)`; version from `-version` (3 s). The only tool binary ever executed.
- **Jasna:** saved path → monitor configs (`jasna_exe_path`) → `C:\Jasna\jasna.exe`, `%ProgramFiles%\Jasna`, `%LOCALAPPDATA%\Programs\Jasna` → `which("jasna")`; version from the PE file-version resource only (never executed), else 「已找到」.
- **WhisperJAV:** saved path → monitor configs → `C:\WhisperJAV\Scripts\whisperjav.exe`, `%LOCALAPPDATA%\Programs\WhisperJAV\Scripts` → PATH; existence only.
- **Miko:** root containing `mdcx/cmd/postprocess_one.py` in saved root, `<data_dir>\engines\MDCX304`, `<config_dir>\engines\MDCX304`; Python `<root>\.venv\Scripts\python.exe` (macOS `.venv/bin/python`); profile `%APPDATA%\com.mdcx.desktop\config.json` (macOS `~/Library/Application Support/com.mdcx.desktop/config.json`) → 三项齐全 / partial / off.
- **GPU:** `LocalGpu`'s nvidia-smi query + `driver_version,power.limit` (5 s).
- **Directory suggestions:** fixed local drive with most free space → `TaskpawInbox`, `TaskpawWork`, `TaskpawStaging` (macOS: under home). A suggested directory is created when the step is saved; a typed path is never created silently. **D5:** 「本地 NVMe」 → 「本地磁盘」.

## 6. Wizard state machine

普通 Agent `role → network → done`; 主控 `role → network → controller → done`; 工作机 `role → network → worker_fields → worker_join → done`. Each 下一步 saves the step's settings through the real writers, then the step to `agent.setup.json`. 上一步/修改 go back losslessly. After any restart (quit, crash, planned 3a) the wizard resumes at the saved step, re-reading the form from the saved config. Changing the role re-saves the workflow under the P2-4 guards.

## 7. UI per board

`views/SetupWizard.tsx` + one file per step; 4-step stepper (3 for 普通 Agent); states per step: loading, detected, missing, field errors, saving, refused. Settings-Workflow: section nav, RestartBanner, detection rows. Hub-Settings: `components/HubSettings.tsx` in `Settings role="hub"`. Fixtures at 390 px for every board.

## 8. Test plan

- Backend unit: first-run marker matrix (fresh, crash after scaffold, marker done, fault file, server_id mismatch); setup transitions; token generate/reveal never logged; bind probe; self-heal; firewall argv whitelist + 1223 mapping (ShellExecute faked); detection with fake trees + fake runner (timeout; Jasna never executed); restart guard matrix; exit 75; Hub override validation, live vs restart keys, OpenClaw test outcomes; 422 for LAN-off with worker access.
- Wizard e2e with fakes: each role fresh install → done, incl. resume after a restart at every step and the worker 3a→3b path.
- UI fixtures: every board state incl. busy/refused restart, pairing errors, firewall declined, 390 px.
- Rust: `restart_supervisor` unit with fake child + spawner — exit 75 respawns once and rebuilds; other exits do nothing; failed respawn readiness → `fatal_startup`; Job reassigned on Windows; `init_script` carries `restart`.
- Event-driven tests and injected clocks only; no host disk/wall-clock dependence.

## 9. Out of scope

Linux; headless-service restarts; draining/forcing a restart while busy; a Hub first-run wizard; re-running the wizard from settings; macOS firewall automation; DHCP reservation; multi-NIC binding; developer `scripts/setup-*` (only the final hint changes to 「打开 TaskPaw → 设置」).

## 10. Risks

1. A saved setting that cannot start leaves no UI — mitigated by the save-time bind/port probe, self-heal, existing startup dialogs.
2. IP change breaks Hub/worker addresses — copy recommends a DHCP reservation.
3. Program-scoped firewall rule goes stale if the install folder moves — status shows it; 重试 re-adds.
4. Reveal sends the token to the local UI — loopback + control-token gated, `no-store`.
5. Window rebuild loses in-page state — wizard resumes from disk.

## 11. Commits

1. `feat(agent)`: first-run marker, silent init, setup API. 2. `feat(agent)`: network & access (lan_access, detection, self-heal, token, bind probe, firewall helper). 3. `feat(agent)`: tool/GPU/directory detection. 4. `feat(agent,hub)`: restart endpoint (exit 75); Hub settings overrides + OpenClaw test. 5. `feat(shell)`: exit-75 supervisor, window rebuild, `restart` flag. 6. `feat(ui)`: settings sections, 网络与访问, 功耗与主机监控, RestartBanner, copy fixes. 7. `feat(ui)`: SetupWizard. 8. `feat(ui)`: Hub settings. 9. `docs`: this contract + real-machine checklist.

### Code facts behind the contract
`startup_code` sends `initialization_required` only with no state and no fault file; the shell shows `Action::Initialize.confirmation()` and `recover_once` allows one restart. No restart command exists (`generate_handler![set_ui_lang]` only); on Windows `terminate_child` hard-kills after 5 s. `init_script` fixes the control token for the window's life and `api.ts` deletes it after reading, so a restart needs a new window. `guard_bind_exposure` refuses wildcard binds and requires a token for any non-loopback bind. `power` is live; `host_metrics` and `workflow` are not. Worker `join` returns 409 unless the running role is worker; a controller 401 doesn't say why. The Hub has no hub.yaml writer; `openclaw_enabled`/`openclaw_token` are already live store overrides; `openclaw_url` is fixed at Poller construction.
