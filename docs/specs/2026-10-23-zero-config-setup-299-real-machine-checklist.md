# #299 Zero-config setup — real-machine checklist

Companion to the frozen contract [2026-10-23-zero-config-setup-299.md](2026-10-23-zero-config-setup-299.md).
Everything below needs a packaged build on real hardware: CI and the unit tests fake ShellExecute,
netsh, PowerShell, nvidia-smi, the NICs and the shell's window rebuild. Use a spare machine or a
fresh Windows user — never the operator's working agent.yaml, hub.yaml or event state.

## 1. First run (R1, R2)

- [ ] Fresh install, no `%APPDATA%\TaskPaw`: the app opens straight into 「首次启动 · 选择角色」 with
      **no** native 「事件状态尚未初始化」 dialog; `agent.setup.json` holds `status: pending`.
- [ ] Delete only `agent.state.json` + its highwater file on an install whose marker is `done`: the
      existing confirmation dialog appears (no silent re-initialisation).
- [ ] Upgrade an existing agent (agent.yaml present, no marker): the console opens as before.
- [ ] Kill the backend right after the very first start (marker `pending`, no state yet), reopen: the
      existing confirmation dialog appears — only the run that scaffolded agent.yaml initializes silently
      (§12 IR4); the wizard still resumes after confirming.

## 2. Roles (R3, R7, R8, R10)

- [ ] 普通 Agent: 角色 → 网络 → 完成 → 「开始使用」 without a restart when nothing changed.
- [ ] macOS (fresh user): 媒体主控 and GPU 工作机 are disabled with 「仅支持 Windows」; the 普通 Agent path
      completes; 设置 · 媒体工作流 offers only 不参与 (§12 OD-W).
- [ ] 媒体主控 on the RTX 5060 8 GB box: 工作机 card disabled with 「本机显卡不足 16 GB」; the controller card
      shows the GPU; INBOX / 工作目录 suggestions sit on the fixed drive with most free space; ffprobe,
      Jasna (version from the PE resource), WhisperJAV and Miko detected; suggested folders are created on
      下一步; 完成 shows every row, 修改 returns to its step.
- [ ] GPU 工作机 on the 16 GB box: GPU card shows name, VRAM, driver, power limit; 「保存并启动工作机（约 10 秒）」
      restarts and the new window resumes at 加入主控; a malformed code says 「配对码格式不对…」 without a
      request; an expired code shows the 15-minute message; after joining, 下一步 → 完成.

## 3. Network and firewall (R4–R6)

- [ ] 媒体主控: the token is generated on entering 网络与访问; 显示 / 复制 / 重新生成 (warning) work.
- [ ] GPU 工作机 and 普通 Agent: 网络与访问 asks for 「粘贴主控的访问令牌」 (粘贴 reads the clipboard);
      a mangled paste is refused; after pasting the 主控's token, the 主控's 功耗 page reads the worker's
      power and the Hub polls the machine with its one polling token (§12 IR2).
- [ ] Hub 设置 → 主控连接: with several polled agents and none chosen it asks 「选择主控」; 更换主控 edits the
      chosen one, never the first in the list.
- [ ] A typed port already in use → 「端口已被占用」; a foreign address → 「此地址不属于本机」; nothing saved.
- [ ] 完成 with LAN on: exactly **one** UAC prompt under the TaskPaw publisher; afterwards
      `Get-NetFirewallRule -DisplayName 'TaskPaw Agent'` shows an inbound, private-profile rule for the
      backend exe and 5680 (+ 5682 with GPU workers). Answer 「否」 once: the wizard continues with
      「未添加防火墙规则…」 and 设置 → 网络与访问 offers 添加入站规则.
- [ ] With the TaskPaw window in front, 添加入站规则 brings the UAC prompt to the front (it is owned by the
      TaskPaw window). Put another app in front right after clicking: if the prompt only blinks in the
      taskbar, after 5 s the page says 「请在任务栏找到 Windows 管理员确认窗口」; leave it unanswered for 2
      minutes: 「管理员确认未完成（超时）…」, and a second click meanwhile opens no second prompt (§12 IR3).
- [ ] On a network marked Public the public-profile warning shows; with Wi-Fi and Ethernet on different
      profiles the warning follows the adapter that owns 本机地址 (§12 IR7).
- [ ] Change the DHCP lease (or the reservation) so the saved address disappears; restart: the agent binds
      the new primary address, `agent.yaml.bak` holds the old file, 网络与访问 shows 「本机地址已从 A 变为 B…」.
- [ ] On a machine with WSL / Hyper-V / VirtualBox / Tailscale: start TaskPaw with the network cable out
      (or Wi-Fi off) — agent.yaml is not rewritten and no vEthernet / 192.168.56.x / 100.x address is ever
      saved; the start fails with the bind-address dialog; plug back in and reopen: it binds (or heals to)
      the real LAN address (§12 IR1).
- [ ] macOS: the 「首次监听时 macOS 会询问…」 note shows; the system prompt appears on first LAN listen.

## 4. 立即重启 (§3, R11)

- [ ] 设置 → 网络与访问 change the port → banner → 立即重启: the window is replaced within ~10 s (no close
      confirmation), the new port is live, the old backend tree is gone (Task Manager: no stray
      `taskpaw-backend` / llm-worker).
- [ ] While a local Jasna/ASR run holds the GPU: 「暂不能重启：本机正在运行：…」, nothing restarts.
- [ ] Quit the app while a restart is in progress: no orphan backend remains.
- [ ] Maximize (or move and resize) the window, then 立即重启: the new window opens maximized / at the same
      place and size (§12 IR9). A second 立即重启 click or a save during the restart answers 「正在重启」.
- [ ] macOS: after 立即重启 and after quitting, `ps -o pid,pgid,command` shows no process left in the old
      backend's group (§12 IR6).
- [ ] Hub (macOS): change 轮询间隔 → 立即重启 rebuilds the Hub window; OpenClaw address/token changes apply
      without a restart; 测试发送 reports 已送达 / 令牌错误 / 无法连接.

## 5. Settings without files (R12–R16)

- [ ] No visible copy names agent.yaml or hub.yaml; 功耗 page with power off links to 功耗与主机监控.
- [ ] 设置 at 390 px: the section select replaces the side nav; no horizontal scroll in any step.
- [ ] Turning 允许 GPU 工作机接入 on with LAN off → 「需要先开启局域网访问」.
- [ ] hub.yaml is byte-identical after any Hub settings save.
