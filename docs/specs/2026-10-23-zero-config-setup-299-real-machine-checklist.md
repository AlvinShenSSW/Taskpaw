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

## 2. Roles (R3, R7, R8, R10)

- [ ] 普通 Agent: 角色 → 网络 → 完成 → 「开始使用」 without a restart when nothing changed.
- [ ] 媒体主控 on the RTX 5060 8 GB box: 工作机 card disabled with 「本机显卡不足 16 GB」; the controller card
      shows the GPU; INBOX / 工作目录 suggestions sit on the fixed drive with most free space; ffprobe,
      Jasna (version from the PE resource), WhisperJAV and Miko detected; suggested folders are created on
      下一步; 完成 shows every row, 修改 returns to its step.
- [ ] GPU 工作机 on the 16 GB box: GPU card shows name, VRAM, driver, power limit; 「保存并启动工作机（约 10 秒）」
      restarts and the new window resumes at 加入主控; a malformed code says 「配对码格式不对…」 without a
      request; an expired code shows the 15-minute message; after joining, 下一步 → 完成.

## 3. Network and firewall (R4–R6)

- [ ] The token is generated on entering 网络与访问; 显示 / 复制 / 重新生成 (warning) work.
- [ ] A typed port already in use → 「端口已被占用」; a foreign address → 「此地址不属于本机」; nothing saved.
- [ ] 完成 with LAN on: exactly **one** UAC prompt under the TaskPaw publisher; afterwards
      `Get-NetFirewallRule -DisplayName 'TaskPaw Agent'` shows an inbound, private-profile rule for the
      backend exe and 5680 (+ 5682 with GPU workers). Answer 「否」 once: the wizard continues with
      「未添加防火墙规则…」 and 设置 → 网络与访问 offers 添加入站规则.
- [ ] On a network marked Public the public-profile warning shows.
- [ ] Change the DHCP lease (or the reservation) so the saved address disappears; restart: the agent binds
      the new primary address, `agent.yaml.bak` holds the old file, 网络与访问 shows 「本机地址已从 A 变为 B…」.
- [ ] macOS: the 「首次监听时 macOS 会询问…」 note shows; the system prompt appears on first LAN listen.

## 4. 立即重启 (§3, R11)

- [ ] 设置 → 网络与访问 change the port → banner → 立即重启: the window is replaced within ~10 s (no close
      confirmation), the new port is live, the old backend tree is gone (Task Manager: no stray
      `taskpaw-backend` / llm-worker).
- [ ] While a local Jasna/ASR run holds the GPU: 「暂不能重启：本机正在运行：…」, nothing restarts.
- [ ] Quit the app while a restart is in progress: no orphan backend remains.
- [ ] Hub (macOS): change 轮询间隔 → 立即重启 rebuilds the Hub window; OpenClaw address/token changes apply
      without a restart; 测试发送 reports 已送达 / 令牌错误 / 无法连接.

## 5. Settings without files (R12–R16)

- [ ] No visible copy names agent.yaml or hub.yaml; 功耗 page with power off links to 功耗与主机监控.
- [ ] 设置 at 390 px: the section select replaces the side nav; no horizontal scroll in any step.
- [ ] Turning 允许 GPU 工作机接入 on with LAN off → 「需要先开启局域网访问」.
- [ ] hub.yaml is byte-identical after any Hub settings save.
