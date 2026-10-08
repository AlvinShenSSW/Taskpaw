# V3 事件计数器与离线恢复

Agent 的事件 ID 必须在重启后继续递增。`agent.state.json` 和独立的
`agent.state.highwater.json` 保存相同的身份、事件流和下一 ID；每次发出事件前，
先持久化预留 ID，再把事件放入内存队列。运行中的 Agent 持有 OS 排他租约，离线工具
不能同时改写它。Hub 运行期间也持有数据库维护租约，阻止离线游标采纳。

文件缺失、空白、损坏、两份不一致、配置身份不匹配或计数器耗尽时，Agent 显式停止，
不会自动从 1 开始。原始故障内容保存为旁边唯一命名的 `.fault-*` 文件；备份失败也会
拒绝恢复。不要删除这些证据，也不要手动降低计数器。两份文件一起回滚到旧备份无法
自动识别；仅凭 Hub 已收到的最大 ID，也不能证明 Agent 从未发出过更大的 ID。

## 首次安装

第一次启动会创建平台默认的 `agent.yaml`。3.9.12 起 macOS 和 Windows 打包版
Agent 在事件状态尚未初始化时显示原生确认框：确认这是从未配对的新端点，或该
端点所有旧 Hub 注册均已禁用后，选择创建新配对。
只有两份状态文件均不存在、且没有故障备份证据时，应用内初始化才可执行；损坏或
部分缺失的状态不会被此入口重置。取消不会初始化，成功后应用只重试启动一次。
它会生成新的逻辑 Agent 身份和事件流。真正全新的 Hub 注册可
自动绑定，不需要额外采纳；不要复用旧注册名/历史配对来表示丢失证据后的新设备。

其他平台、无界面服务或自定义后端使用下面的离线命令：先退出应用及其 backend，
确认旧注册已禁用，再执行 `initialize` 并重新打开应用。

源码安装可显式一次完成配置和事件初始化：

```sh
python -m taskpaw_v3.bootstrap agent --initialize-events --run
```

可同时使用既有 `--preset moomoo`、`--bind-host` 参数，配置修改先于状态初始化。
`--force` 和 presets 不会重置已有可靠状态的身份/计数器；已验证状态再次初始化会拒绝。
自动化安装脚本只创建 YAML 时，仍须先执行明确的事件初始化步骤。打包用户使用下面
的 sidecar，不需要安装 Python、uv 或下载源码。


## macOS / Windows 旧版升级

旧版只有 `{"next_event_id": N}` 的完整事件文件时，打包版 Agent 会说明需要一次
事件状态升级。确认文件一直由旧版正常使用、没有被手动替换或从备份回滚后，可在
原生确认框执行迁移。迁移使用与离线工具相同的排他锁和故障备份，保留配置、Agent
身份和下一事件 ID，生成新的事件流与独立水位文件，然后只重试启动一次。
正常升级不使用初始化入口，也不需要为每台 Agent 删除身份或重新登记 Hub。

已迁移且验证一致的状态正常启动，不会每次升级都再次迁移。未知版本、损坏、身份
不匹配或部分写入仍须按下表核查，不能从启动确认框强制恢复或重置。自定义后端和
附加到既有后端的模式不会执行应用内迁移。新 Hub 的旧注册可能仍需下述离线采纳。

启动错误使用固定错误类别传给桌面端，区分迁移、初始化、状态损坏、配置问题、端口
占用和绑定地址失效；详细日志位置会随提示显示。错误帧不包含密钥、原始异常或可
执行命令。Windows 日志位于 `%APPDATA%\TaskPaw\taskpaw-backend-agent.log`。
macOS 安装包验收覆盖旧计数器迁移、迁移后启动及退出再启动，而非仅预先初始化
一份新状态后检查后端；Windows 原生安装包仍须单独验证这些路径。


## 选择恢复操作

先停止 Agent，运行 `inspect`。以下操作是按故障类型选择的替代方案，不应依次执行：

| 状态 | 操作与前提 |
|---|---|
| 无历史配对，或全部证据丢失 | 禁用所有旧注册，`initialize --confirm-new-pairing`；新身份、新流、ID 从 1 开始，建立不同名称的新 Hub 注册，旧历史保留 |
| 升级前单份 `{"next_event_id": N}` 完整且可信 | `migrate --confirm-intact-legacy-counter`；保留身份和 N，生成独立锚与流，不降低 ID |
| 有可信同身份/同流的幸存记录 | `recover --confirm-surviving-record-intact`；选最高预留水位、保留故障备份，可能跳过尚未发布的 ID |
| 两份记录均已验证 | 正常重启；需要 Hub 采纳时 `export --output REPORT` 导出非密钥报告 |

确认开关是操作者对旧配对退休或幸存记录完整性的声明，软件不能从“文件不存在”证明
设备全新，也不能从未知/同时回滚的记录恢复真实最高已发 ID。无法证明连续性时必须
走新身份和新注册路径。故障文件不要复制覆盖回正常状态；保留原始证据供核查。

在停止的 Hub 上，先通过现有应用禁用待采纳注册，核对实际注册 ID 和数据库位置。
`event-cursor` 查看绑定/已收到水位；`adopt-event-cursor` 读取 Agent 的已验证报告，
拒绝身份变化、低于历史/outbox/确认水位的计数器以及不明旧注册上的新配对报告。
采纳保留历史和 outbox，原始损坏的 ack 配置保留为数据库诊断记录。采纳后重新启动
Hub、启用注册。Agent 停止时的报告是连续性证据，Hub 历史只是已收到下界。

## macOS 打包命令

使用实际安装的 Agent/Hub sidecar。以下是正常 `.app` 安装位置示例；安装位置不同
则修改应用路径。若 `Contents/MacOS/taskpaw-backend` 不存在，通过 Finder 的
“显示包内容”找到 `Contents/Resources` 或带目标平台后缀的实际 sidecar 文件。

```sh
taskpawAgentBackend="/Applications/TaskPaw Agent.app/Contents/MacOS/taskpaw-backend"
taskpawHubBackend="/Applications/TaskPaw Hub.app/Contents/MacOS/taskpaw-backend"
taskpawAgentConfig="$HOME/Library/Application Support/TaskPaw/agent.yaml"
taskpawStateReport="$HOME/Desktop/taskpaw-event-state-report.json"
"$taskpawAgentBackend" agent-state --config "$taskpawAgentConfig" inspect
# 仅选择与已核实状态相符的一条：
"$taskpawAgentBackend" agent-state --config "$taskpawAgentConfig" initialize --confirm-new-pairing
"$taskpawAgentBackend" agent-state --config "$taskpawAgentConfig" migrate --confirm-intact-legacy-counter
"$taskpawAgentBackend" agent-state --config "$taskpawAgentConfig" recover --confirm-surviving-record-intact
"$taskpawAgentBackend" agent-state --config "$taskpawAgentConfig" export --output "$taskpawStateReport"
# 在停止的 Hub 上，用实际数据库和注册 ID；先把报告复制到此机器：
"$taskpawHubBackend" hub-cursor --db "$HOME/.taskpaw-hub/hub.db" event-cursor --id 1
"$taskpawHubBackend" hub-cursor --db "$HOME/.taskpaw-hub/hub.db" adopt-event-cursor --id 1 --state-report "$taskpawStateReport"
```

## Windows 打包命令

从应用快捷方式的“打开文件所在的位置”进入安装目录并打开 PowerShell。不要假定
统一的 Program Files/LocalAppData 安装前缀。sidecar 通常与应用 EXE 相邻；如果名称
带目标平台后缀，将变量改为那个实际文件。

```powershell
$taskpawAgentBackend = Join-Path (Get-Location) 'taskpaw-backend.exe'
$taskpawAgentConfig = Join-Path $env:APPDATA 'TaskPaw\agent.yaml'
$taskpawStateReport = Join-Path $env:USERPROFILE 'Desktop\taskpaw-event-state-report.json'
& $taskpawAgentBackend agent-state --config $taskpawAgentConfig inspect
# 仅选择与已核实状态相符的一条：
& $taskpawAgentBackend agent-state --config $taskpawAgentConfig initialize --confirm-new-pairing
& $taskpawAgentBackend agent-state --config $taskpawAgentConfig migrate --confirm-intact-legacy-counter
& $taskpawAgentBackend agent-state --config $taskpawAgentConfig recover --confirm-surviving-record-intact
& $taskpawAgentBackend agent-state --config $taskpawAgentConfig export --output $taskpawStateReport
# 在 Hub 安装目录，用实际数据库和注册 ID，先复制报告：
$taskpawHubBackend = Join-Path (Get-Location) 'taskpaw-backend.exe'
$taskpawHubDb = Join-Path $env:USERPROFILE '.taskpaw-hub\hub.db'
& $taskpawHubBackend hub-cursor --db $taskpawHubDb event-cursor --id 1
& $taskpawHubBackend hub-cursor --db $taskpawHubDb adopt-event-cursor --id 1 --state-report $taskpawStateReport
```

环境变量缺失或配置自定义了目录时，使用服务报告的实际配置和 `data_dir` 路径，不要
猜测默认位置。任何命令都必须显式选择配置/现有 DB。报告只含身份/计数器证据，不含
Bearer/LLM 密钥；通过既有本地文件工作流转移。offline routes 不启动监听器、监控器
或 readiness，不支持任意 Python 模块或服务器管理命令。

源码工具使用相同参数：`python -m taskpaw_v3.agent.state --config PATH …` 与
`python -m taskpaw_v3.hub --db DB event-cursor/adopt-event-cursor …`。

## 协议与混合版本

`/ping`、`/status`、`/events` 和原事件必填字段不变。可选 `event_cursor` 包含
`version: 1`、逻辑 `server_id`、`stream_id`、`boot_id`、`resume_floor`、
`offered_highwater`、`next_event_id` 和 `durable`。新 Hub 每次用当前 status 准入后
携带可选 `cursor_stream`、`cursor_boot`；stale/partial/重复证明会在裁剪前拒绝。
Agent Bearer 检查先于任何 ack/proof 处理。

数字 ack 只允许到本次启动前的可靠预留下界或本 boot 真正提供过的最大 ID。
错误/过大/重复 ack 返回 409，不修改队列、事件历史或计数器。确认仍在 Hub 的
SQLite 事件落库、可用 outbox 入队之后持久化；重放沿用现有去重。

| Agent / Hub | 行为 |
|---|---|
| 新可靠 / 新 | 正常确认与重启恢复；升级前已有注册需要一次离线采纳 |
| 新可靠 / 旧 | 仍接受原数字 ack 和不带 ack 请求，无须新增证明；旧 clear-on-read 响应丢失边界保留 |
| 新不可信 / 任意 | 启动显式拒绝，在恢复或新配对前不发布事件 |
| 旧 / 新 | 继续 status 采样，事件通道显式暂停，不发破坏性 `/events` 或 404 fallback；升级并采纳后恢复 |
| 旧 / 旧 | 行为不变，计数器重置风险仍在，需至少升级一端 |

Hub `/status` 的注册项增加 `event_channel`，显示 `ready/paused`、具体原因与恢复提示。
混合版本的事件暂停是可见降级，恢复可用性需要操作者步骤，不是完整事件兼容。
暂停不删除历史或 pending/failed/dead_letter outbox；既有 outbox 投递仍可继续。

此机制不把事件正文变成磁盘队列，不承诺 exactly-once，也不修复旧 clear-on-read 的
响应丢失窗口。OS 租约和 fsync/atomic replace 面向本地文件系统；同时丢失/回滚所有
锚、网络文件系统或硬件破坏的连续性仍须人工证明或新配对。

## 完整批次中的坏事件与已消费下界

新 Hub 对大小/结构都在限额内且 cursor proof 可靠的完整响应逐项准入。坏 ID、
非有限数、不可编码字符串、重复或乱序项保留 metadata-only quarantine receipt；
有效邻项按唯一 ID 递增入库。事件、active outbox、receipt、永久 consumed floor
及 ack 在一个 SQLite 事务中提交，然后才更新内存 ack，并在下一请求确认本批
proof 的 `offered_highwater`。事务失败完全回滚；不对部分前缀取 max ID 确认。
全坏批次也可有证据地完成，不永久重放 poison item；超限/坏 envelope 整批拒绝。

receipt 有七天/每注册 256 条/全库 4096 条限额，裁剪保留计数/原因/时间摘要。
永久 consumed floor 不随它或历史清理而降低，离线采纳必须满足该下界。不要删除新
metadata 或降低 ack 来绕过准入；损坏仍要求已验证的离线恢复，不自动 reset。
此行为仍要求 producer 已交付 ID 单调：确认高水位后未来才出现的更低 ID 不能恢复；
预留但未交付的间隙合法，不要求连续 ID。详见
[upstream 隔离与固定限额](upstream-isolation.md)。
