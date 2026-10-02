# V01：已合并区域的隔离验收准备

本页只准备 R01、R02、R03、R05、R15 的证据和人工验收记录，**未执行以下验收，所有案例初始状态均为 pending**。完整 [V01 / #240](https://github.com/AlvinShenSSW/Taskpaw/issues/240) 仍待完成。源码、自动测试、CI 构建与安装后的真机结果是不同证据。

源码基线：[04686df53f933b03fab856a965a4055c66466568](https://github.com/AlvinShenSSW/Taskpaw/commit/04686df53f933b03fab856a965a4055c66466568)。下列五个 squash commit 均包含在该基线。这里不包含未合并 PR #251、#252、#253 或其他待合并修复；也不接纳、复制或推广私有 `41ba56b`。安装、签名、跨机、GPU、真实通知、长期运行仍需最终产物、指定机器和单独授权。

## 证据矩阵

下表是已核对的历史 PR 元数据：列出的检查均为 COMPLETED / SUCCESS，属于**对应 PR head**，不是 squash commit 或上述基线的新检查。后续 main、构建产物及人工运行必须各自记录 SHA 和运行编号，不互换身份。

| 区域 / PR | 已合并 squash | 当时 PR head | 当时自动检查 |
| --- | --- | --- | --- |
| R01 / [#241](https://github.com/AlvinShenSSW/Taskpaw/pull/241) | [e247c3c](https://github.com/AlvinShenSSW/Taskpaw/commit/e247c3c6948a29c009cdb8b5bad55cc0c164090a) | [b1f27db](https://github.com/AlvinShenSSW/Taskpaw/commit/b1f27db9d7954611d7ef2fafb8a7b8211c605f1d) | [CI 36891432222](https://github.com/AlvinShenSSW/Taskpaw/actions/runs/36891432222)：9 项，含 Windows Python→Rust 凭据互操作 |
| R02 / [#242](https://github.com/AlvinShenSSW/Taskpaw/pull/242) | [be5136d](https://github.com/AlvinShenSSW/Taskpaw/commit/be5136d2630d85b9ea85e8fb2c4cb11d2823ea34) | [f260ec2](https://github.com/AlvinShenSSW/Taskpaw/commit/f260ec2964545b96de25cb69dd740851560857bf) | [CI 36889119017](https://github.com/AlvinShenSSW/Taskpaw/actions/runs/36889119017)：8 项 |
| R03 / [#243](https://github.com/AlvinShenSSW/Taskpaw/pull/243) | [ded7815](https://github.com/AlvinShenSSW/Taskpaw/commit/ded7815b46c918f6f4563a9e7f57a56c92a2f6d1) | [a4d4fc3](https://github.com/AlvinShenSSW/Taskpaw/commit/a4d4fc3a3e1821987ed0510281cc81cb4e66a165) | [CI 36952474427](https://github.com/AlvinShenSSW/Taskpaw/actions/runs/36952474427)：9 项，含凭据互操作 |
| R05 / [#244](https://github.com/AlvinShenSSW/Taskpaw/pull/244) | [80441e0](https://github.com/AlvinShenSSW/Taskpaw/commit/80441e01699ddddeee4c7bf63248266036a4545a) | [682407a](https://github.com/AlvinShenSSW/Taskpaw/commit/682407a98966e9663fe02a9e98cb069cb3356409) | [CI 36954218819](https://github.com/AlvinShenSSW/Taskpaw/actions/runs/36954218819)：9 项，含凭据互操作 |
| R15 / [#246](https://github.com/AlvinShenSSW/Taskpaw/pull/246) | [7c1e174](https://github.com/AlvinShenSSW/Taskpaw/commit/7c1e174cebd42fe1217535a99d072c02720a6c11) | [3b515f6](https://github.com/AlvinShenSSW/Taskpaw/commit/3b515f6869bb2753711f2778866dabd9a9871a52) | [CI 36905174413](https://github.com/AlvinShenSSW/Taskpaw/actions/runs/36905174413)：8 项；[扫描 36905174435](https://github.com/AlvinShenSSW/Taskpaw/actions/runs/36905174435)：1 项 |

| 区域 | 源码 / 已有测试中的具体证据 | 自动证据不覆盖的边界 |
| --- | --- | --- |
| R01 | [控制 guard](../../taskpaw_v3/core/control.py)；[路由测试](../../taskpaw_v3/tests/test_control_routes.py)的 `test_agent_every_mutation_rejects_without_state_change`、`test_hub_every_mutation_rejects_without_state_change`；[凭据测试](../../taskpaw_v3/tests/test_control_credentials.py)的 `test_each_boot_fresh_key_rejects_static_env_and_old_file` | 实际安装目录 ACL、真机 WebView、原生窗口操作未由这些测试验收 |
| R02 | [重定向拒绝](../../taskpaw_v3/core/http.py)；[HTTP 测试](../../taskpaw_v3/tests/test_http_redirects.py)的 `test_real_cross_port_redirect_never_requests_target`、`test_redirect_keeps_status_and_ack_until_direct_recovery`、`test_redirect_at_attempt_cap_has_one_safe_dead_letter_alert` | 假端点/TLS 不证明现场代理、LAN 或真实 OpenClaw 配置兼容 |
| R03 | [迁移工具](../../taskpaw_v3/hub/server/outbox_migration.py)；[迁移测试](../../taskpaw_v3/tests/test_outbox_migration.py)的 `test_preview_is_readonly_apply_preserves_values_and_double_start`、`test_complete_backup_includes_committed_wal_and_restores_all_tables` | 临时 SQLite 不证明真实历史数据、目录保护或恢复后的通知效果 |
| R05 | [Agent 状态](../../taskpaw_v3/agent/state.py)、[状态存储](../../taskpaw_v3/core/state.py)、[Hub poller](../../taskpaw_v3/hub/server/poller.py)；[游标测试](../../taskpaw_v3/tests/test_event_cursor.py)的 `test_unverified_counter_does_not_fallback`、`test_unoffered_ack_inside_current_range_retains_queue`、`test_actual_legacy_agent_status_only` | 模拟重启不证明现场历史完整、双方记录同时回滚可识别或事件正文持久重放 |
| R15 | [扫描器](../../scripts/dependency_scan.py)、[工作流](../../.github/workflows/dependency-scan.yml)、[例外清单](../security/dependency-exceptions.json)；[扫描测试](../../tests/test_dependency_scan.py)的 `test_complete_multiversion_coverage_and_identity_mismatch`、`test_tool_error_and_malformed_reports_never_findings`、`test_expiry_is_exclusive_and_new_advisory_stays_actionable` | 某时刻的扫描不证明当前无新公告、最终安装包内容或产品不存在漏洞 |

历史 [审计跟进索引](../audits/2026-10-02-audit-follow-up.md) 是其标注时间的快照，不在本页改写为当前 tracker。已有 [部署](deployment.md)、[控制契约](../specs/2026-10-01-local-control-auth-design.md)、[outbox 迁移契约](../specs/2026-10-02-outbox-timestamp-migration.md)、[游标恢复](event-cursor-recovery.md)、[依赖扫描](dependency-security.md)提供详细语义；引用操作指南不代表本页授权执行。

## 每个未来案例的共同前提

只在一次性 VM，或明确指定且没有生产 TaskPaw 数据/服务的独立 OS 账户中安排人工验收。**仅覆盖 HOME/APPDATA 不足以证明隔离**；不得在共享桌面上试启动正常 backend、setup 或 sidecar。先记录实际解析的配置、凭据、状态、数据库、日志路径，证明全部属于本次私有目录；不明确则保持 pending 并停止。

仅使用新建合成数据、专用测试实例、owned 回环假端点及已确认空闲的端口；不导入真实用户配置/DB，不复用常规服务端口，不连接外部 Agent、交易/媒体进程或真实通知地址。操作员先形成含明确路径、端点与资源所有权的操作清单，得到该案例的授权后执行。本页不提供可直接触发真实服务的启动/停止、迁移或证书命令。

每次重复使用新夹具或明确恢复的 VM 快照；记录操作前后的计数、哈希和实际耗时。不凭“看起来正常”填写 passed：缺前提/未运行写 pending；不符合预期写 failed 并停止；有完整观察与清理证据才可由操作员签认该次结果。没有已确定的耗时预算时，只记录方法、时长和分布，不编造达标阈值。

## 五个未来案例

### R01：本地控制凭据与来源（pending）

前提：隔离 Agent/Hub 各有独立私有目录、当次凭据描述文件和合成配置；无真实受管任务。以下认证矩阵针对**受保护操作**，不泛化到开放 GET/HEAD ping 或合法预检；预检成功不授权后续修改。

1. 记录操作前配置/状态。分别发送无凭据、错误凭据、网络读取/轮询 token、重复 Authorization 的受保护请求：预期401、无修改。带正确凭据但非法/重复 Origin：预期403、无修改；拒绝必须先于正文/参数验证及任何副作用。
2. 用当次正确控制凭据和受信来源（或无 Origin 的受信本地工具）只修改一个合成对象：预期一次有效修改。Agent/Hub 凭据、网络 token 不能互相代用。
3. 经批准重启仅本案例实例，验证新凭据、旧凭据失效；UI 的401应清空相应凭据，不能自动重放失败修改。单独记录实际描述文件权限/ACL、原生 UI 观察，未运行项仍 pending。

保留：HTTP 状态、合成对象前后摘要、权限结果、旧/新凭据是否有效的布尔结果；不保存 token 或完整描述文件。失败时停止后续修改。清理：关闭本案例实例/连接，确认仅其资源释放，移除其临时凭据和目录或还原 VM。

### R02：出站重定向拒绝（pending）

前提：同一隔离环境内的假 source/target、合成 Agent 状态/事件及 fake 通知接收器，记录每端请求计数；所有地址回环且无真实凭据。

1. 对状态、事件、影片代理、通知分别提供301/302/303/307/308，包括同源/相对 Location。预期 source 收到请求，重定向 target 零请求，凭据不被转发；诊断只呈现固定失败和状态，不泄露 Location、上游 reason/body 或 Authorization。
2. 比较事件失败前后 Hub ack/历史；失败不能推进 ack。未确认通知进入既有重试/死信策略：尝试达到上限时遵守现有死信处理，**不承诺无限保留或永远重试**。
3. 在选定的重试上限前切换为直接服务的 fake 成功响应，观察恢复及所确认合成数据；另以独立夹具观察到达上限后的既有死信结果。健康假 Agent 的轮询仍能继续。

保留：状态、source/target 计数、ack/行状态/attempts 摘要和恢复耗时；原始重定向字段不得进入公开记录。target 收到请求即失败并停止。清理：关闭 owned 连接、假端点与测试实例，保留脱敏摘要后清理合成 DB/目录。

### R03：旧 outbox 分类、备份与恢复（pending）

前提：仅在副本中生成合成旧 DB，含 aware、naive、坏 JSON/时间与健康行；原始夹具只读保留。写操作前确认所有 owned Hub/连接离线，不使用已安装 Hub DB。

1. 只读 preview，比较 DB 字节/表数据和备份数量：预期无写入。未知旧时区的 naive 行不能从当前机器时区猜测；DST 歧义/不存在时间须隔离，健康 aware 时间保留原瞬间。
2. 在新副本上、具备合成时区来源记录后 apply：预期写前完整 WAL-aware 私有备份，分类/转换事务一致；ID、payload、attempts 等非时间字段按契约保留，坏行隔离，健康行可继续。重复 apply 无新增无效备份/迁移；解除隔离只能显式 retry，仍坏的行继续隔离。
3. 在另一 owned 副本演练备份失败/写失败，核对无半迁移；所有连接关闭后恢复完整备份、处理该副本旧 WAL/SHM，比较全部表。手动改值前另存完整快照，apply 自动备份不包含“手动修改前”的旧值。

保留：预览固定 reason/ID、表摘要、前后哈希、备份检查及恢复比较，DB/备份不进入公开日志。恢复发生在发送之后可能重放通知；交付是 at-least-once，不是 exactly-once，真实外部通知对账未验收。失败时保留副本停止；清理只关闭 owned 连接并处理已核对的副本，绝不向真实 DB 恢复或把转换队列交给 V2。

### R05：事件身份、游标与恢复选择（pending）

前提：新的合成 Agent 身份/事件流、状态及 high-water、专用 Hub 注册；无旧现场配对。离线操作前确认相关 owned Agent/Hub 已退出并释放租约。

1. 记录可信状态后验证保留身份/事件流的正常重启；缺失、空白、损坏、不一致或身份不符的证据应明确拒绝，不自动从1继续。故障文件保留；备份失败不能静默修复。
2. 根据证据**只选择适用的一条替代路径，不能依次执行恢复命令**：完整可信旧单份计数器用 migrate；同身份/同流可信幸存记录用 recover；无法证明连续性时，退休合成旧注册并 initialize 新身份/新流/不同注册。不能手降 ID，也不能把同时回滚的两份记录当作连续性证据。
3. 用已验证离线报告核对 Hub 采纳前提和历史下界；未提供过的 ack 应拒绝并保留队列。无当前支持的游标证明时只读状态；受支持事件接口404不得改请求裸 `/events`。旧历史/outbox 不作为“最高曾发 ID”的证明。

保留：合成身份/水位摘要、拒绝状态、故障备份清单、所选路径及注册变更；不承诺事件正文持久重放或 exactly-once。身份/水位不明即停止，不拼接猜测恢复。清理仅 owned 实例/连接/注册和目录，保留诊断副本后还原 VM；不删除真实状态或退休真实注册。

### R15：扫描与最终产物证据核对（pending）

前提：明确 SHA 的已保存扫描报告和锁/策略输入；无需本页启动扫描器、安装工具或修改锁。

1. 核对输入/锁/政策哈希、工具版本、报告/公告库 UTC 时间和精确 inventory/coverage；缺失、部分覆盖、工具/数据错误不是通过。
2. 区分 exit0 的 clean 与 accepted_exceptions；逐项核对例外 ID、owner、适用范围和排他到期时间。exit1 是未豁免可操作发现，exit2 是扫描/配置/覆盖错误；错误或 yank 不能靠例外豁免。依赖新公告可能改变结论，旧报告不宣称“当前安全”。
3. 分别记录最终 sidecar archive/TOC 与 UI 产物证据是否存在、对应源码/锁及散列。声明的依赖组不能证明最终包内容；尚无最终产物则记 pending。新的实时扫描按[现有独立流程](dependency-security.md)另行授权，安装包、原生依赖兼容和正式发布安全仍未验收。

保留：脱敏 summary、coverage/例外结果和输入/报告/产物哈希，不发布原始敏感 stderr。缺证据即 pending，有错误/不匹配即 failed。清理仅本次保存的公开/脱敏报告副本；不自动升级、修锁、发通知或部署。

## 脱敏记录模板

复制以下空表记录**一个案例的一次运行**，初始状态不改为 passed；重复运行保留独立记录，不覆盖上次失败。

| 字段 | 待填写值 |
| --- | --- |
| 案例 / 重复序号 / 状态 | 待填写 / 待填写 / pending（未执行） |
| 操作员别名 / 授权范围及记录 | 待填写；不记录私人账号或凭据 |
| 开始 / 结束 UTC；计时方法 / 观察时长 | 待填写；延迟、重试、重复/丢失计数只记实测 |
| OS 版本 / CPU 架构 / Agent或Hub角色 | 待填写 |
| 一次性 VM 或独立账户证明 / owned 资源清单 | 待填写；公开版只用夹具别名和 repo-relative 路径 |
| 源码 SHA / PR head检查或main检查 SHA及run | 待填写；分别记录，不互代 |
| 安装版本 / build target / 产物 SHA256 / 签名状态 | pending；未安装/未核验不得从源码推断 |
| 合成配置摘要 / fake 端点别名 / 操作前快照 | 待填写；无 token、真实地址、用户配置或 DB payload |
| 操作顺序 / 预期 / 实际 / 差异 | 待填写；私有操作清单中的明确 owned 路径另行保管 |
| 脱敏证据位置 / 哈希 / 可复核结果 | 待填写；无原始凭据/响应敏感字段/DB备份 |
| 清理：owned 进程/连接/端点释放与目录处置 | pending；只处理记录的资源，禁止广泛 kill/delete |
| 最终 passed/failed/pending / 依据 / 签认 | pending；失败或资源缺失原因待填写 |

发布记录前移除控制描述文件、token/Authorization、LLM key、真实主机/IP、用户名/本机绝对路径及原始 payload、Location/reason。不得用“token 的哈希”代替脱敏。敏感诊断/备份如需保留，只放在获授权的私有隔离目录；公开记录保留必要状态/数量与非敏感文件哈希。

清理先核对所有权、关闭仅本次创建的连接/进程/端点，记录释放结果；留存脱敏证据后仅处理确认属于夹具的路径，或还原一次性 VM。清理失败必须记录，不继续真实部署。完成本页材料或全部自动检查，都不能关闭 V01 中未执行的原生、跨机和长期验收项。
