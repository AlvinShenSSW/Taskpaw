# T01：持久媒体工作流与授权契约

状态：实施规格初稿，待独立设计审阅；不是已实现、已验证或已部署声明。

所属 [T01 #272](https://github.com/AlvinShenSSW/Taskpaw/issues/272)，父项 [EPIC #271](https://github.com/AlvinShenSSW/Taskpaw/issues/271)。代码审计基线为 `885ead2503aecb538705f72fa3299d74e20d2b24`。产品范围以[架构](2026-10-19-media-workflow-architecture.md)、[开发计划](2026-10-19-media-workflow-development-plan.md)及本规格的明确细化为准；安全与兼容性遵守[constitution](../constitution.md)。

## 1. 目标与冻结范围

T01 交付一个可重启恢复的、具有版本的工作流账本及其类型化 API。它回答某一影片版本由谁处理、哪些结果被接纳、批准覆盖什么、哪些外部操作仍未知，以及哪些精确来源获得了清理授权。它不执行调度算法、模型、媒体探测、刮削、文件复制或删除。

本次允许新增 `taskpaw_v3/workflow/`、对应测试和契约样例。新 API 只有独立 app factory，**不挂载到现有 Agent/Hub app，不启动端口，不添加默认服务，不修改普通安装的配置**。后续 T03/T05 负责运行时装配，T02/T04/M01/F01/F02 负责真实证明的产生与验证。永久保存事实、接纳证明与实际执行必须可区分，不能以测试 fixture 声称真实 NAS 入库完成。

冻结的产品约束：

- Windows 是唯一工作流 authority；Mac 及普通监控主体只读。所有普通及工作流 Hub 记录迁移属于 T05，不能因工作流 enrollment 而过滤原监控记录。
- 普通 TaskPaw、仅翻译机器保持原用途。`workflow_participation=false` 是默认；监控注册、GPU 探测、翻译端点发现和监控 token 都不授予执行权。
- 默认普通本机 INBOX，经操作性就绪及不可变快照准入；不要求 sender commit、ready 文件或逐片技术风险 checkbox。长期中断但可解析的复制仍可能误判，T01 不把操作性就绪宣称为复制完成证明。
- 正常末端一次确认，同时绑定归档及精确 input/工作副本清理范围。整包归档验证和持久 receipt 之后，未变且复核通过的普通来源可自动清理；变化、仍写、未知才阻断相关清理。
- NAS 正式库只增、不覆盖、不删除；超时或失联不等于副作用未发生；unknown 不自动重新领取或生成新复制 operation。
- 不承诺跨组件 exactly-once、LTX 片内续算、真实 GPU 原生停止证明或 NAS 原子提交。T01 保存并校验这些证明的契约关系。

## 2. 代码依据与实施选择

| 现有依据（基线行号） | T01 决定 |
|---|---|
| [HubStore](../../taskpaw_v3/hub/server/store.py)，59–80：SQLite、WAL、RLock 和事务；430–458：`remove_server` 删除历史/outbox | 新增独立 `workflow.db`，不修改监控库 schema，不调用 destructive removal；监控关联仅为非所有权引用 |
| [Hub app](../../taskpaw_v3/hub/server/app.py)，345–376：网络只读与本地 control 分离 | 独立 worker/local app factory；旧 `/ping`、`/status`、`/events` 和控制端口不增加执行权限 |
| [auth.py](../../taskpaw_v3/core/auth.py)，14–23：空 token 允许旧兼容路径 | 新身份域空凭据必须拒绝，不能继承该短路 |
| [gpu_lease.py](../../taskpaw_v3/core/gpu_lease.py)，62–121：进程内 holder/generation | 中央持久 owner 与现有本地 lease 分开；T03/T04 再接入两者，T01 不修改本地 GPU 行为 |
| [state.py](../../taskpaw_v3/core/state.py)，31–74：原生文件锁 | 复用 `FileLease` 原语持有工作流库单 owner；关闭前停止准入、等当前事务退出 |
| [outbox_migration.py](../../taskpaw_v3/hub/server/outbox_migration.py)，281–339；[迁移测试](../../taskpaw_v3/tests/test_outbox_migration.py)，426–518 | 沿用先备份、事务变更、失败保留、版本拒绝的模式；不把 outbox 专用迁移函数直接扩成通用框架 |
| [protocol.py](../../taskpaw_v3/core/protocol.py)，`EventQueue` | 工作流审计使用独立 ID/游标域，不改变现有 event-id/ack 契约 |

独立数据库避免工作流退役误删普通监控历史，也避免向已有 Hub 初始化路径隐式加入执行功能。两个数据库没有联合原子事务；后续通知集成从工作流审计按稳定 ID 幂等消费，不能声称一次提交同时覆盖两个库。

## 3. 通用类型、边界与编码

所有公开 DTO 放在 `workflow/models.py`，使用现有 Pydantic 依赖，`extra="forbid"`。下列名称是并行实现之间的共享契约。所有写入 DTO 包含 `schema_version=1`；未知版本拒绝，不能猜测降级。

| 类型 | 定义 |
|---|---|
| `Id` | UUID 的规范字符串；film、worker、device、attempt、operation 等各自具有独立字段，不能互换为路径/名字/IP |
| `Digest` | 小写 SHA-256 十六进制，64 字符 |
| `UtcTime` | 有时区时间，序列化为 UTC；store 使用注入的 Hub 时钟生成权威时间，不接受调用者提供 `now` |
| `Revision` | 正整数；比较使用整数，不使用时间戳代替 |
| `RelativePath` | `/` 分段、非空；禁止绝对路径、盘符、UNC、空段、`.`、`..`、NUL、ADS 冒号和 Windows 保留名/尾随点或空格；包内规范化后大小写冲突拒绝 |
| `NativeIdentity` | `scheme`、`root_id`、`volume_or_export_id`、`object_id`、`observed_at`、`evidence_id`；不可用时显式为 null，不能用路径/hash 伪造稳定原生身份 |
| `EvidenceRef` | `evidence_id`、`kind`、`producer_id`、`producer_version`、`observed_at`、`received_at`、`payload_digest` 及有界类型化摘要；本版本不接收任意 URL/脚本 |
| `CommandMeta` | `request_id`、`idempotency_key`、`authority_id`、`authority_epoch`；需要 CAS 的命令另有 `expected_version` |
| `CommandResult` | `request_id`、稳定 `result_id`、实体 ID、提交后 version、状态、审计序号；重放返回原结果，不重新生成 ID/时间 |

规范摘要使用 UTF-8 JSON、按 key 排序、紧凑分隔符、禁止 NaN/Infinity；只包含 DTO 定义的规范内容。先验证再规范化；不静默修正有歧义路径。集合字段按定义的稳定 key 排序，顺序有业务意义的分卷字段保留显式 ordinal。`manifest_hash` 不把自己、接收时间或数据库版本算入内容摘要；来源身份、角色、关联、处理策略、就绪证据摘要必须进入摘要。

统一边界：HTTP body 上限 2 MiB，manifest 最多 4096 项，每个路径最多 1024 字符，每项证据摘要最多 16 KiB，普通字符串最多 2048 字符，错误说明最多 2048 字符。分页 limit 为 1–200。超限明确拒绝，不截断后接纳。大日志和原始探测输出不进入账本 DTO，后续以受控证据引用提供。secret 不进入通用 DTO 的 repr、审计、请求结果或幂等摘要。

## 4. 记录模型

### 4.1 影片、快照、成果与阶段

| DTO | 必需字段/语义 |
|---|---|
| `FilmCreate` / `FilmRecord` | `film_id`、`intake_session_id`、`display_name`、`policy`；record 增加当前 input/final revision、当前 approval context revision/有效状态、`version`、创建时间。`display_name` 不是唯一键 |
| `FilmPolicy` | `policy_revision`、需要的阶段、字幕要求、允许复用/跳过的阶段及证据种类、需要的图片角色、`folder_cover_required`。Miko 完整后处理、归档验证及清理准入不能被设置为可跳过 |
| `ManifestItem` | `item_id`、`role`、`relative_path`、`size_bytes`、`sha256`、`source_root_id`、`native_identity`、`parent_item_id`、`volume_ordinal`、`video_item_id`、`language`；不适用关系为 null，存在的引用必须指向本包有效项 |
| `InputManifest` | `film_id`、单调 `revision`、`items`、`readiness`、`policy_revision`、`manifest_hash`；不可变。原始 input 与工作副本成员映射必须保留 |
| `ReadinessEvidence` | `mode="ordinary_directory"`、策略版本、稳定观察开始/结束、成员集合摘要、写占用结果、逐视频媒体探测摘要、全量读取 hash、已验证工作副本 manifest/hash、源复核证据。结果枚举 `passed/failed/unknown`，只有全部必要结果 passed 才准入 |
| `FinalManifest` | `film_id`、revision、`input_revision/hash`、items、实际输出相对位置/受控 root、工具/model/profile 版本、字幕/修复实际结果、`postprocess`、manifest hash |
| `PostprocessEvidence` | Miko attempt/版本/配置摘要、完整流程结果、逐项结果、最终 NFO/封面及必要图片 item ID/hash、实际水印标识、旧标识图处理结果、适用 folder.jpg 的 item ID/hash 和父目录/分卷上下文；缺失与不适用必须区分 |
| `StageRecord` | 稳定 `job_id`、film ID、`stage`、输入 revision/hash、依赖 job ID、`status`、当前 attempt ID、version |
| `AttemptRecord` | attempt ID、job ID、单调 attempt number、工具/model/profile/config 版本、输入 hash、owner principal、worker ID（本地 component 为 null）/boot、epoch/fence、开始/结束时间、结果/错误类别、external operation/evidence 引用；旧 attempt 不覆盖 |
| `ArtifactReceipt` | receiver ID、operation/attempt、输入和输出 manifest hash、完整接收结果、逐项大小/hash 核验结果、durable reference、接纳时间；发送端的“上传结束”不能代替接收端结果 |

`role` 固定为 `main_video/source_subtitle/generated_subtitle/nfo/poster/fanart/thumb/folder_cover/attachment`。`attachment` 仍须被影片策略允许；不能成为任意文件读取/执行入口。最终源路径和目标位置以受限 root ID＋相对路径表达，root 的真实解析留给后续适配器。

`stage` 固定为 `intake/restore/asr/translate/return/scrape/archive/cleanup_input/cleanup_workcopy`。T01 不用自由文本注册可执行阶段。待确认不是额外外部执行阶段；是 scrape 及最终包通过后、归档授权产生前的流程状态。

`create_film` 建立接收记录；`put_input_manifest` 在就绪证明通过时原子建立该 revision 的固定阶段行，并把 intake 标为 succeeded。job ID 由 store 生成并持久化，`get_film` 返回这些 ID；重放不再生成。`claim` 仅用于 restore/asr/translate/return/scrape：compute worker 只能领取前三种，receiver adapter 领取 return，scrape adapter 领取 scrape。本地 adapter 以已登记 principal 和 component boot 为 owner，不伪装 GPU worker。archive/cleanup 的 attempt/operation 分别由专门 admission 方法建立。`put_final_manifest` 保存 scrape attempt 的候选最终包，只有同一当前 attempt 的成功证明接纳后它才成为可批准的最终包；仅写入一个 manifest 不推进阶段。

### 4.2 设备、资格与资源

| DTO | 必需字段/语义 |
|---|---|
| `WorkerRegistration` / `WorkerRecord` | device ID、worker ID、可选 monitoring reference、名称、地址/端口及 network revision、participation、角色、authorization revision、enrollment state、运行准入状态、当前 boot ID、version |
| `CapabilityRevision` | 独立 `declared`、`probed`、`approved` 记录；各有 revision、来源、时间、内容摘要。approved 引用确切 probe revision、工具/model/profile 版本和允许阶段；声明型号/显存本身不授予能力 |
| `PhysicalGpuBinding` | controller 接纳的 physical resource ID、device 身份证据、硬件 GPU 身份证据、对应 capability revision；GPU 型号、总显存和 PCI 索引不能单独充当稳定身份 |
| `PairingRecord` | pairing ID、预定 worker/device ID、一次性 secret digest、expires_at、consumed_at、消费后的 credential ID、request digest；只允许完成该身份的登记 |
| `CredentialRecord` | credential ID、worker 或 local principal ID、随机 secret digest、generation、角色/scopes、issued/expired/revoked 时间；没有监控 token 隐式转换 |
| `AssignmentRecord` | assignment ID、job/attempt、owner principal、worker ID（本地 component 为 null）/boot、epoch、单调 fencing token、physical resource ID（非 GPU 阶段可 null）、领取/续约/截止时间、状态、当前 local owner evidence、version |
| `ResourceOwnerRecord` | physical resource ID、assignment/attempt、owner generation、`reserved/running/unknown`、已接纳停止/结果核对证据；只有明确核销操作能移除活跃 owner |

工作流 enrollment 为 `pending_pairing/pending_validation/verified/retired`；运行准入为 `disabled/enabled/draining/revoked`，二者分开。`pending_validation` 可以保存；领取要求 participation=true、verified、enabled、有效凭据、当前 boot 已核对及 profile/物理身份通过。`draining` 阻止新领取但不撤销在途结果的提交资格。`revoked` 阻止原凭据继续调用；核对由当前授权的本地主体或重新配对的受限身份提供，不能复活旧 token。

普通重启保留 worker ID、增加 boot 记录；旧 boot 未核销 assignment 保留 unknown，核对前不恢复新领取。退役后重入产生新 worker ID/凭据；必须重新绑定已核验物理资源，旧 unknown owner 继续阻断同卡。不能证明物理身份时该节点不具备 GPU claim 资格。声明、探测和授权不等于对恶意主机的硬件远程证明；可信探测渠道及实际硬件身份来源由 T03 验证。

### 4.3 审批、归档与清理

`ApprovalBinding` 包含 film ID、input revision/hash、final revision/hash、policy revision、Miko/postprocess evidence digest、metadata revision、actor/candidate selection revision、target root ID、实际 host/export/root native identity、target relative path、expected-absent 条件，以及 input/workcopy 两份精确 cleanup manifest/hash。任何绑定变化使其不能授权新操作。

`ApprovalContext` 是可与批准比较的持久当前事实，包含 `film_id`、store 分配的单调 `context_revision`、完整 `ApprovalBinding`、`context_hash`，以及 metadata、actor/candidate、target/root identity、input/workcopy cleanup 范围各自的类型化观察证据（来源/版本/观察与接纳时间/结果/摘要）。不可变 context 行保存历史，FilmRecord 指向当前 revision；有效状态由 store 依据当前 manifest 和证据推导为 ready、blocked 或 invalidated，不接纳客户端自称 ready。必要观察为 failed/unknown 时可以保存新 context 来阻断旧授权，但不能批准；旧身份不能伪装成当前已核验观察。context hash 覆盖完整 binding 及规范证据摘要，不只覆盖 final manifest。

只有本地主控 operator 可通过 `put_approval_context` 接纳 F01 或其他已授权本地适配器事实；compute worker、observer 和普通监控主体不得更新。命令 CAS 同时检查 film version 和 expected context revision（0 表示尚无 context），在同一事务内验证来源 input/final revision/hash、保存下一 revision、切换 current 指针、使所有引用旧 context 的授权不能用于新操作并写审计。新元数据、候选、root identity 或清理范围即便不改变 final manifest，也必须经过这个入口形成可比较的新事实。真实 NAS/文件系统观察仍由 F01/F02 负责；T01 不自行探测。

context 更新只使旧授权失效，不创建 approval、不恢复领取、不清理来源，也不要求新的技术 checkbox。相同幂等请求返回原 context；不同有效更新创建新 revision，不能通过把旧 binding 再写回来复活旧批准。`put_input_manifest` 或 `put_final_manifest` 改变所引用版本时，同事务将当前 context 标为 invalidated；必须重新接纳匹配当前版本的 context 后才可批准。既有 operation/receipt 及已归档事实始终保留。

`ApprovalRecord` 包含 approval ID、所引用 current context revision/hash、完整 binding/binding hash、operator principal、Hub issued_at/expires_at、epoch、撤回时间及原因。`approve` 在事务内要求 context 是当前 ready revision，传入 binding 与其逐字段相等，才能生成不可变批准。expires_at 必须明确且晚于 issued_at；默认有效时长由后续主控策略提供，本层不隐式无限授权。input 和 workcopy 清理不产生第二个正常人工确认动作。

`ArchiveOperation` 包含 operation ID、approval ID、binding hash、attempt、receiver/adapter ID、目标身份、`admitted/running/unknown/verified/failed`、精确 owned target items、错误和核对引用。

`ArchiveReceipt` 包含 receipt ID、operation/approval ID、Filmpaw producer/版本、目标 root/目录原生身份、final manifest hash、逐项大小/hash 核验、最终提交标记、durable reference、接纳时间。只有角色允许的归档适配器事实可被接纳；普通计算 worker 不能自报归档成功。T01 校验字段、来源角色、operation 与清单关系，真实 NAS 核验属于 F02。

`CleanupIntent` 包含 operation ID、approval ID、archive receipt ID、`scope=input|workcopy`、被批准 cleanup manifest/hash、当前 source observation、逐项原生身份/hash/成员/写占用复核、version。每项 `CleanupItemResult` 为 `pending/deleted/already_absent/blocked_changed/blocked_writing/blocked_identity/failed/unknown`，并保留 evidence/错误及时间。

共享来源以 root ID＋原生对象身份建立引用索引；其他影片仍需要该来源时拒绝删除授权。只有各引用影片均已有有效归档事实且当前批准覆盖该精确来源，才可认为不再需要原件；不能通过相同路径/文件名合并不同对象。对身份不可比较的共享关系保守阻断，并保存原因。

## 5. 状态转换与故障结果

### 5.1 阶段与 assignment

| 当前状态 | 允许转换 | 必要条件 |
|---|---|---|
| pending | running | 前置有效、当前 input、有效资格、CAS 成功；生成新 attempt/assignment |
| pending / blocked | skipped | 仅 policy 明确允许的复用/跳过阶段，有原因及内容证明；保留 input，不伪造 receipt |
| running | succeeded | 当前身份/attempt/fence/input，类型化完成证明齐全；需要 receiver receipt 的阶段必须先被接纳 |
| running | failed | 有明确失败且无未核销副作用/owner；否则 unknown |
| running | unknown | 失联、租约到期、超时、取消/退出不明、master/boot 不一致 |
| unknown | succeeded / failed / blocked | 授权 reconcile，有具体结果/停止证明；旧 token 的迟到消息不能直接推进 |
| failed / blocked | pending | 显式重试/依赖重新验证；旧 attempt 保留，下次运行新 attempt |
| succeeded / skipped | 不覆盖原事实 | 新输入/策略使其成为失效历史，建立新 revision 的阶段状态 |

lease 到期操作只标 unknown，绝不清空 resource owner。`resolve_assignment` 独立核销资源归属：GPU 阶段即使业务结果成功，也必须有本地受管子进程 owner 已退出的证明才能释放资源。后续 NAS/input housekeeping 不占用已释放 GPU。

基本依赖为 intake → restore → asr → translate → return → scrape → archive → 两种 cleanup。可按 policy 有证据复用 restore/asr/translate；return 的核验、Miko 完整后处理、archive 验证不能用 skipped 绕过。任一必须字幕/图片或 folder 证据缺失，流程为 blocked，不能生成待确认的完整成品。

### 5.2 操作授权与事实

新 `begin_archive`、`begin_cleanup` 分别在写事务内读取 FilmRecord 的 current approval context，检查：当前主体权限、有效 authority/epoch、批准未过期未撤回、context 为当前 ready revision、approval 引用的 context revision/hash 与其相等、全部 binding 与当前 context 及当前 manifest 逐字段一致、必要前置证明、没有同范围未核销 operation。缺 context、context blocked/invalidated 或版本不符一律拒绝，不能只将请求 binding 与旧 approval 自身比较。通过后原子写入 operation/intent，才可由未来适配器执行。

**到期按 operation admission 解释。** 排队到期但尚未准入必须拒绝。到期后允许记录准入时有效的既有 operation 的结果/receipt 事实，不能新发 copy/cleanup 授权，也不能把重复请求变成新 operation。此前 archive 已准入不代表后来的 cleanup 也已准入；cleanup 仍检查其当时的批准有效性。

撤回、绑定变化立即阻止新操作。已有操作被要求停止/核对；T01 不声称数据库更新能撤回已发出的文件写入。实际发生或未知副作用及 receipt 永久保留；失效版本的 receipt 可作为历史事实接纳，但不能推进当前新版本或授权其清理。既有 operation 的事实提交仍要求当前有效的提交/核对主体，不能以原 approval 替代身份认证。

来源新增/变化/仍写/未知使受影响 input 的 cleanup 保持 pending/blocked；既有 archive receipt 不清除。工作副本清理独立判定，不能借 input 失败扩大 workcopy 范围。删除后回复丢失需按原 intent 核对同对象已不存在；不能搜索同名新文件。F02 必须在实际验证与删除间保持同一 native 对象约束，T01 的记录校验不能替代该操作保证。

## 6. SQLite、事务、幂等与生命周期

数据库使用 `application_id` 和 schema version 双检查，WAL、foreign_keys=ON、bounded busy_timeout、`synchronous=FULL`；数据库要求本机受控数据目录，不放 NAS/SMB。逻辑实体至少覆盖 authority、films、manifests/items/source references、stages、attempts、assignments、workers/capability revisions、pairings/credentials、physical resources/owners、approval_contexts、approvals、operations、receipts、cleanup items、request results、audit。允许把不可变详情存为规范 JSON，但 CAS/唯一性/权限关键信息必须有可查询列。

必须具备的数据库约束：

- film ID、worker ID、attempt ID、operation ID 全局唯一；manifest `(film_id, kind, revision)` 唯一。
- approval context `(film_id, context_revision)` 唯一；film 的 current context 指针、有效状态及 version 同事务变更；approval 引用不可变 context revision/hash，不能级联删除历史。
- stage `(film_id, input_revision, stage)` 唯一；一个 job 最多一个未核销 assignment；一个 physical resource 最多一个活跃 owner，unknown 仍算活跃。
- `(principal_id, command_kind, idempotency_key)` 唯一；archive receipt 的 operation/producer 关系唯一，冲突内容不可覆盖。
- 所有历史 FK 禁止级联删除身份、attempt、批准、receipt、审计；retire 为 tombstone。

写入顺序固定：`BEGIN IMMEDIATE` → 校验 authority 与当前 credential/membership/scopes → 查幂等记录 → 对比请求摘要/目标身份 → 校验 CAS/业务前置 → 条件更新与 insert → 写审计及稳定结果 → commit。任一步异常 rollback；返回成功只能发生在 commit 之后。幂等重放仍校验当前调用者身份；同键同请求返回原结果，同键不同请求 `idempotency_conflict`。秘密不进入摘要，配对使用 secret digest 等固定身份字段。拒绝请求不悄悄更改业务状态。

CAS 使用 `UPDATE ... WHERE version=? AND ...` 并检查影响行数，不能只先 SELECT 再无条件 UPDATE；唯一索引是最后防线。线程锁只保护连接使用，不代替数据库约束。测试须实际运行竞争命令，并验证一个提交及其余冲突/同一重放。

数据库时钟为 Hub 注入的 UTC wall clock，期限判断只由它做出；worker 的本地等待由后续本地 monotonic 时钟计量，不上传 monotonic 数值当跨机期限。发现权威时钟回退时暂停新期限型授权并要求恢复核对，不能把已过期批准重新变为有效；持久记录最大已观察 authority time 用于检测。T01 不自动调整系统时钟。

`WorkflowStore.create` 仅对显式新建且不存在的目标创建库；`open` 遇到缺失、零字节、损坏、不兼容或残缺 schema 都拒绝，不能重建空库或新 authority。数据库 native lease 与初始化/迁移在 API 可用之前取得，任何失败关闭连接并释放已取得资源。首次 schema 版本为 1；只测试新库创建的事务 DDL/commit 故障，不虚构已发布的 v0 产品迁移。失败创建留下的 owned 空库/残缺文件不能由 open 自动续建，需显式检查和处理后重新 create。未来升级必须先有含已提交 WAL 的完整快照、检查备份、事务修改和最后的版本记录；不自动 downgrade，也不把当前测试称为真实生产迁移。

关闭先拒绝新命令，等待正在执行的事务结束，再关连接和 lease；不能在仍有 writer 时释放 owner。DB 备份恢复不得自动激活更旧 epoch 或遗忘已签发 operation；T05/T08 接入时必须先核对远端最大 epoch、在途操作和唯一 authority，再激活。T01 保存 `inactive/active/recovery_required` 状态，正常原库重启保留其状态；显式恢复导入必须标记 recovery_required，本层不提供副本激活入口。T01 不能仅从一个结构合法的旧 DB 判断它是否未经申报被复制/回滚，实际恢复工具及远端 epoch fencing 由 T05/T08 完成，不能把本机 file lock 当成跨机器单主证明。

## 7. 身份、配对与传输

新执行域使用随机高熵凭据，数据库仅保存摘要，认证使用常量时间比较；没有空凭据模式。受限角色固定为 `operator/compute_worker/intake_adapter/receiver_adapter/scrape_adapter/archive_adapter/cleanup_adapter/observer`。API 主体由认证层产生，不能从业务 body 的 `role`、`principal_id` 或 worker 自报 scopes 接纳。

本地 bootstrap 由调用方显式提供 authority、operator credential digest 及证书 binding；不读取生产配置、不从 monitor token 推导、不自动添加普通节点。后续服务从受保护环境/本地配置取得原凭据。T01 不写 credential 文件，不在日志/错误/响应中回显 secret。

初始 operator 通过 `grant_component` 持久登记受限 component principal/credential，允许角色仅为 intake_adapter、receiver_adapter、scrape_adapter、archive_adapter、cleanup_adapter、observer；不能传自定义 scopes、创建第二 operator 或绕过 worker 配对。grant 包含 component/principal/credential ID、boot ID、generation、secret digest、固定角色及明确 credential expires_at；凭据在受保护调用方先生成并保存，只传摘要进入 store。operator 可用 `revoke_credential` 撤销这些凭据，保留其 operation/receipt；普通 worker 撤销同时撤销其全部当前 credential。完整流程测试必须经 bootstrap → authenticate → grant_component → authenticate 获取适配器身份，不能直接 SQL 填入主体后声称公开 API 闭环。

`authenticate(credential_id, secret)` 比较持久摘要并返回私有 `AuthProof`；它携带 credential ID/generation 及认证证明，repr 不包含秘密，不允许从 HTTP body 构造。每个 store 事务仍按其对应当前记录复验 digest、generation、到期、撤销和角色，不把旧 AuthProof 当永久 capability。正常业务 DTO 中不存在可自授的 principal/scopes。

配对流程：operator 先登记 worker，并用随机一次性 secret 的摘要创建限时 ticket；worker 本地先持久保存随机新 credential，再通过已绑定 TLS 提交 ticket 与该 credential。pairing 认证入口接收一次性 secret 和新 credential 的 SecretStr，仅认证层计算摘要，产生只可用于 `complete_pairing` 的私有 `PairingProof`。服务器事务核验 ticket、目标 worker/device、期限及未消费状态，写 credential digest 并消费 ticket。相同完成请求在有效新 credential 证明下可返回同一登记结果，不重发或存储明文 secret；不同请求重用 ticket 拒绝。合法已完成重放使用新 credential 认证，不能只凭过期/已消费 ticket 取得结果。ticket 发出/凭据保存由后续向导负责，T01 测试通过内存 secret 注入，不能把 secret 落入 fixture 输出。

credential 撤销/轮换与资格修改在写事务内重新检查，避免 HTTP 认证通过后到实际 claim 之间的撤销竞争。普通 retirement 要求没有在途/unknown，保留 tombstone；离线隔离可立即撤销未来资格但保留 owner，不能假扮已经退休。observer 和旧监控 token 不具有任何变更权限。

### 7.1 FB-01：新执行通道冻结为 HTTPS

新 LAN worker 协议要求 HTTPS，并绑定主控证书身份；不提供隐式明文回退。`TlsBinding` 为 authority ID、证书 SHA-256 fingerprint、binding revision。worker 配对前取得可信 binding，连接时验证证书/名称及已批准绑定；首次绑定交付和证书轮换由 T03/T05 实现，不能靠 HTTP 回复或未核验 TOFU 自动接受新证书。

T01 worker app factory 必须接收非空有效 `TlsBinding`，拒绝 `http` ASGI 请求；不信任 `X-Forwarded-Proto` 等客户端头替代实际 scheme，不带通用反向代理信任开关。factory 不建立 socket，因此这个单元门槛不等于完成了 TLS 部署：T03/T05 运行入口必须真正配置 TLS，并实现客户端验证后才发送凭据。单元测试使用 HTTPS TestClient 只能证明 app 拒绝规则。现有网络监控协议保持原状，不能借此给旧 `/control` 开 LAN。

### 7.2 FB-05：到期与撤回

第 5.2 节是唯一 expiry 语义。store 必须把“新授权”与“记录已发生事实”作为不同方法；后者不能返回新的执行许可。默认有效时长不是 T01 固定产品策略，批准命令必须传明确 expires_at，经 Hub 时钟验证。正常流程一次确认；到期后的重新批准属于异常恢复，不新增逐片技术 checkbox。

## 8. 最小 Python API 与 HTTP 映射

公开方法均同步、有限 SQL 工作，不等待 worker/NAS，也不发网络请求。类型均来自 models；返回 record 是值快照，不泄露连接/可变内部对象。`AuthProof` 是认证层私有对象（credential ID＋认证证据），不从 JSON 反序列化；每条命令重新在事务内验证其对应记录。

```python
WorkflowStore.create(path: Path, bootstrap: AuthorityBootstrap, *, clock: Clock) -> WorkflowStore
WorkflowStore.open(path: Path, *, clock: Clock) -> WorkflowStore
WorkflowStore.close() -> None
authenticate(store: WorkflowStore, credential_id: Id, secret: SecretStr) -> AuthProof
authenticate_pairing(store: WorkflowStore, pairing_id: Id,
                     ticket_secret: SecretStr, new_secret: SecretStr) -> PairingProof

# Every mutating method below takes (auth: AuthProof, meta: CommandMeta, command: DTO).
grant_component(..., command: ComponentGrant) -> CommandResult
revoke_credential(..., command: CredentialRevocation) -> CommandResult
register_worker(..., command: WorkerRegistration) -> CommandResult
update_worker(..., command: WorkerUpdate) -> CommandResult
record_capabilities(..., command: CapabilityUpdate) -> CommandResult
issue_pairing(..., command: PairingIssue) -> CommandResult
complete_pairing(..., command: PairingComplete) -> CommandResult
record_boot(..., command: BootObservation) -> CommandResult
revoke_worker(..., command: WorkerRevocation) -> CommandResult
retire_worker(..., command: WorkerRetirement) -> CommandResult

create_film(..., command: FilmCreate) -> CommandResult
put_input_manifest(..., command: InputManifestPut) -> CommandResult
put_final_manifest(..., command: FinalManifestPut) -> CommandResult
claim(..., command: ClaimRequest) -> CommandResult
renew(..., command: AssignmentRenewal) -> CommandResult
finish_attempt(..., command: AttemptResult) -> CommandResult
mark_unknown(..., command: UnknownObservation) -> CommandResult
resolve_assignment(..., command: AssignmentResolution) -> CommandResult
retry_stage(..., command: StageRetry) -> CommandResult
skip_stage(..., command: StageSkip) -> CommandResult
accept_artifact_receipt(..., command: ArtifactReceipt) -> CommandResult

put_approval_context(..., command: ApprovalContextPut) -> CommandResult
approve(..., command: ApprovalCreate) -> CommandResult
revoke_approval(..., command: ApprovalRevocation) -> CommandResult
begin_archive(..., command: ArchiveAdmission) -> CommandResult
accept_archive_receipt(..., command: ArchiveReceipt) -> CommandResult
begin_cleanup(..., command: CleanupAdmission) -> CommandResult
record_cleanup_result(..., command: CleanupResult) -> CommandResult

get_film(auth: AuthProof, film_id: Id) -> FilmView
get_worker(auth: AuthProof, worker_id: Id) -> WorkerView
get_operation(auth: AuthProof, operation_id: Id) -> OperationView
list_audit(auth: AuthProof, after: int, limit: int = 100) -> AuditPage
```

上面 `...` 只代表固定的 `auth, meta` 两参数，不表示可传任意 kwargs。`complete_pairing` 是唯一例外：首次 auth 为 PairingProof，已完成重放为匹配新 credential 的 AuthProof；其余方法拒绝 PairingProof。认证函数属于 auth 模块，不能授权业务状态推进。`CommandMeta` 在 HTTP 是 body 的 `meta`，业务 DTO 为 `command`；wrapper 禁止多余字段。除首次 create/registration 外，变更目标必须包含明确 ID 与 expected_version。首次命令的 caller 预先生成 UUID，配合幂等键处理回复丢失。

关键命令字段固定如下，不能由不同作者自行猜测：

| DTO | 业务字段（除已列公共字段） |
|---|---|
| `ComponentGrant/CredentialRevocation` | component/principal/credential ID、boot ID、generation、固定受限角色、secret digest、expires_at / credential ID、expected_version、原因；仅 operator 调用 |
| `WorkerUpdate` | worker ID、expected_version、network revision/地址、participation、运行准入状态；不能修改 worker/device 身份或注入 approved 能力 |
| `CapabilityUpdate` | worker ID、expected_version、完整 capability revision、physical binding；compute worker 只可报 probe/declaration，approved 仅 operator |
| `PairingIssue/Complete` | pairing ID、worker/device ID、expires_at、ticket digest / 对应 ticket 认证证据、新 credential ID/digest、generation；明文只在认证入口使用 |
| `BootObservation` | worker ID、expected_version、新 boot ID、本地已知 epoch/assignment/owner 摘要；不能声明没有任务就释放旧 owner |
| `WorkerRevocation/Retirement` | worker ID、expected_version、原因；撤销可保留 unknown，retire 必须全部归属收束 |
| `InputManifestPut/FinalManifestPut` | film ID、expected_version、对应完整 manifest；新 revision 只能单调增加 |
| `ClaimRequest` | job ID、expected_version、attempt ID、operation ID、worker ID（component 为 null）、boot ID、profile/config revision、physical resource ID（component 为 null）；owner principal 取自 auth，compute 的 GPU/profile 字段必须完整。本版本只领取明确 job，不做“选最合适下一个”的调度算法 |
| `AssignmentRenewal` | assignment ID、expected_version、worker/boot/attempt、epoch/fence、owner evidence；deadline 由 Hub lease policy 产生，客户端不指定 |
| `AttemptResult` | attempt/assignment/job、expected_version、worker/boot/epoch/fence、input hash、outcome、类型化结果/evidence；不得直接改 archive/cleanup 状态 |
| `UnknownObservation/AssignmentResolution` | assignment/attempt、expected_version、原因/核对 outcome、外部 operation、local owner 及停止/结果证据；resolution 仅可信 reconcile 主体 |
| `StageRetry/StageSkip` | job、expected_version、原因、对应 policy/input 和复用证据；retry 不复用旧 attempt |
| `ApprovalContextPut` | film ID、expected_version、expected_context_revision（首次为 0）、完整 binding 和各类观察证据；context revision/hash 由 store 生成；仅 operator 调用 |
| `ApprovalCreate/Revocation` | film、expected_version、approval ID、current context revision/hash、完整 binding、expires_at / approval ID、撤回原因 |
| `ArchiveAdmission` | operation ID、approval ID、binding hash、film expected_version、adapter ID |
| `CleanupAdmission` | operation ID、approval ID、archive receipt ID、scope、精确 cleanup manifest/hash、当前 source observation、expected_version |
| `CleanupResult` | operation ID、expected_version、逐项结果；只有原 intent 的 item ID 可出现，不得增加路径 |

`Clock` 提供 UTC `now()`；lease policy 是构造时显式注入的有界正整数秒，不依赖客户端时间。`AuthorityBootstrap` 含 authority ID、初始 epoch、初始 operator credential digest、TLS binding、lease policy；只对显式新库使用。初始普通 Hub/Agent 不调用它。

提供两种独立 factory：

```python
create_worker_app(store: WorkflowStore, *, tls: TlsBinding) -> FastAPI
create_local_app(store: WorkflowStore, *, control_token: str,
                 control_active: Callable[[], bool], operator_credential_id: Id) -> FastAPI
```

worker factory 仅公开 `/v1/workflow/enrollment/complete`、`/boot`、`/claims`、`/assignments/{id}/renew`、`/attempts/{id}/result` 及被授权的自身任务查询。pairing 是独立一次性认证入口，其余用 worker credential；route ID 与 body ID 不同拒绝。没有 `/control`、批准、archive/cleanup 或通用命令路由。

local factory 使用现有 `add_control_guard` 的 origin/loopback 控制模型，固定映射到 bootstrap 的 operator credential，再由 store 验证当前 operator 记录；factory 初始化必须验证 control_token 对应这个 operator credential，无空 token 模式。只实现 component grant/revoke、设备登记/变更/能力批准/撤销/退役、影片/operation/审计查询、审批上下文更新（`PUT /v1/workflow/films/{id}/approval-context`）、批准/撤回、显式 retry/skip/reconcile 的具体路由。`get_film` 的 operator 视图返回 current context revision/hash、内容及有效状态，供后续一次确认使用；上下文更新本身不是确认。它不接受来自网络 body 的角色切换。适配器写入方法先通过直接 Python API 契约测试，M01/F02/T05 集成时再装配其受限进程通道；不能暂时把所有 adapter 权限给 compute worker。

读取权限同样限定：compute worker 只读自己的 assignment/operation 和关联影片的执行字段，不读取其它节点、批准清理范围或凭据摘要；operator 可读完整脱敏视图；observer 仅读脱敏状态/审计；component 仅读本角色处理 operation 所需的包与绑定。查询分页不返回原始 secret、credential/ticket digest 或任意私有配置。HTTP worker 表面没有全局审计/worker 列表。

错误统一为 `{error: {code, request_id, entity_id?, retryable}}`，不泄露 secret、SQL、真实私有路径或回溯。固定主要错误码：`unauthorized`(401)、`forbidden`(403)、`transport_required`(403)、`not_found`(404)、`cas_conflict/idempotency_conflict/stale_attempt/stale_manifest/owner_unresolved/binding_changed`(409)、`invalid_request`(422)、`approval_expired/approval_revoked/precondition_failed`(409)、`authority_unavailable`(503)。未知副作用是持久业务状态，不以 `retryable=true` 暗示可直接重放实际动作。

## 9. 实施文件、顺序和并行边界

| 文件 | 允许变更及职责 |
|---|---|
| `taskpaw_v3/workflow/__init__.py` | 显式导出公共 API；import 无 I/O/自动建库 |
| `taskpaw_v3/workflow/models.py` | 本规格 DTO、规范摘要、字段边界；不导入 store/API |
| `taskpaw_v3/workflow/schema.py` | DDL、schema/application ID、事务迁移与备份；不懂 HTTP |
| `taskpaw_v3/workflow/store.py` | 事务、CAS、状态/授权门槛、只读查询、审计及生命周期 |
| `taskpaw_v3/workflow/auth.py` | secret 摘要/验证、AuthProof、scope 矩阵与 pairing 认证；最终撤销检查在 store 事务内 |
| `taskpaw_v3/workflow/api.py` | 两种未挂载 app factory、body/transport/auth 边界与错误映射；无 scheduler/executor |
| `taskpaw_v3/tests/test_workflow_models.py` | DTO/摘要/路径/包关系 round-trip |
| `taskpaw_v3/tests/test_workflow_store.py` | 流程、CAS、幂等、owner、审批/receipt/cleanup 门槛 |
| `taskpaw_v3/tests/test_workflow_migrations.py` | 版本、备份/失败/崩溃、单 owner、关闭恢复 |
| `taskpaw_v3/tests/test_workflow_auth.py` | 配对、权限、撤销竞争、普通主体隔离 |
| `taskpaw_v3/tests/test_workflow_api.py` | 真实 ASGI factory、HTTPS 拒绝/允许、local guard、旧协议回归 |
| `taskpaw_v3/tests/fixtures/workflow/` | 虚构、无秘密的版本化下游包/回执样例，按 schema 校验 |

先冻结 models 名称/序列化和命令字段，编写能体现拒绝路径的测试。schema/store 作者与 API/auth 作者可据此并行，但 store 独占事务与身份再校验实现，API 作者不得在 route 中另造状态机。models 的变更须通知所有消费者；不以额外字段悄悄绕过当前合同。整合后再做完整回归与独立实现评审。

既有 HubStore、protocol、GPU lease、控制启动器、UI、打包和跨仓库产品代码在 T01 只读；若实现发现必须改变它们，应先向 driver 报告具体消费者及因果需要，不能按文件邻近扩改。

## 10. 验收、命令与执行面

| 编号 | 必需测试及可观察结果 | issue AC |
|---|---|---|
| T01-A01 | 临时库完整 film→stage→approval→receipt→两种 cleanup 后重开，ID、审计、事实与 pending 完整；不存在默认生产目录 I/O | 1、6 |
| T01-A02 | schema/data/version/commit 故障注入；实际隔离子进程在未提交与已提交点退出后重开；无半迁移/半结果；未知版本、损坏库、未知非空库拒绝 | 1、6 |
| T01-A03 | 同 job 双 claim、同卡跨 worker/new identity claim、并发 stale CAS；只一个 owner；同键同请求同结果、同键异请求冲突；回复丢失重放不重复生成 operation | 2、8 |
| T01-A04 | wrong attempt/fence/boot/epoch/input、旧 token 和旧 manifest 结果不推进；lease 到期 unknown 仍占 owner；只有核销证明释放 | 2、5、8 |
| T01-A05 | 未批准/到期/撤回/绑定逐字段改变拒绝新 archive/cleanup；先准入后到期仍可记既有事实，不额外发授权；撤回与 claim/准入竞态不会越过事务权限检查 | 3 |
| T01-A06 | mandatory skip、缺字幕/Miko/图片/folder、只有上传完成或缺完整 receiver/archive receipt 全部拒绝；input 与 workcopy 范围不能混用 | 4、11 |
| T01-A07 | 一次性配对过期/并发消费/丢回复/不同 credential 重放；空、监控、observer、错误、撤销主体无写权；secret 不进结果/日志/数据库正文 | 3、7、9 |
| T01-A08 | 第六节点/4090/任意型号可登记；声明与实测冲突、无 profile、未配对/未参与、重启后未核对都不领取；retire 保留 assignment/receipt/audit 及监控关联 | 7、8、9 |
| T01-A09 | 普通 INBOX 不含 sender/ready 也可准入；来源变更只阻断依赖范围，archive receipt 保留；未变且复核齐全有 cleanup intent；共享来源未满足条件拒绝 | 10 |
| T01-A10 | factory 未挂载、不启动网络/GPU/服务；worker app 拒绝 HTTP 与 spoofed forwarding header；worker 无 local/approval/cleanup route；旧监控 token/协议及控制隔离回归保持 | 6、9 |
| T01-A11 | 权威时钟回退阻断新授权，不重新激活过期 approval；单 owner 竞争、关闭时在途事务、重开恢复不释放未知 owner | 1、5、8 |
| T01-A12 | 下游 fixtures round-trip 并在改变成员/元数据/目标/清理/处理证据后被拒绝；完整必要证据一次批准，不要求额外 checkbox | 6、10、11 |
| T01-A13（D01） | 通过公开 grant/authenticate/put_approval_context/approve API 建立 M1/C1/R1 批准，保持 final manifest 不变，分别经 context CAS 更新 metadata、candidate selection、root/host/export/native identity、target relative path/expected-absent、input/workcopy cleanup manifest；每项均使旧批准的新 archive/cleanup 准入拒绝。验证无 context/unknown context 拒绝、worker/observer 更新拒绝、stale CAS 无副作用、幂等重放不重复更新、manifest 改变使 context invalidated；重开保留 current 与旧事实，更新不自动批准且既有 receipt 不删除。不得直接 SQL 改候选以替代正常调用路径 | 2、3、10 |

测试使用 repo 的 `tmp_path` 与现有隔离 fixtures；显式注入时钟、策略、虚构 root/身份。Windows 检查使用 driver 已验证且拥有的私有临时根设置 TEMP/TMP 和独立 pytest basetemp，避免系统临时目录祖先 ACL 与控制凭据安全检查冲突；保留原失败证据，不修改生产 ACL、不关闭检查或跳过断言。真实 sqlite、真实 ASGI 和隔离子进程可证明本层契约；外部 adapter 的虚构回执不能代替五机 LAN、TLS socket、GPU、Windows native 删除或 NAS 故障验证。模型测试不读取实际视频、生产环境凭据或用户配置。

实施期间的检查命令（仓库根）：

```text
uv run pytest taskpaw_v3/tests/test_workflow_models.py taskpaw_v3/tests/test_workflow_store.py taskpaw_v3/tests/test_workflow_migrations.py taskpaw_v3/tests/test_workflow_auth.py taskpaw_v3/tests/test_workflow_api.py
uv run pytest
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run python -m py_compile taskpaw.py taskpaw_hub.py macsubs.py
```

允许测试命令只生成拥有的临时 DB、WAL、lock、备份、fixture 派生值和常规测试缓存；sqlite/测试代码是其生成器，输入为虚构 DTO 与临时路径。`WorkflowStore.create` 是后续唯一显式新库生成入口，输入为 path/bootstrap，输出为该路径的库及伴随文件，不自动生成 YAML、凭据文件或媒体目录。本层没有生产启动命令；不得为手动 smoke 启动实际 Agent/Hub 或挂载真实 NAS。

## 11. 风险、假设与移交

| 风险/假设 | 概率与影响 | 控制及责任 |
|---|---|---|
| 真实适配器证明不完整或误报 | 集成时可能；错误授权风险高 | T01 严格类型/角色/绑定、缺证据拒绝；实际证明方法与原生故障验证由 T02/T04/M01/F02 完成 |
| 新 worker 不能稳定识别旧物理 GPU | 设备重装时可能；双跑风险高 | 无可信 identity/profile 不准入；旧 owner 不随 ID/IP/退役消失；T03 冻结硬件探测依据 |
| TLS 客户端/证书轮换装配错误 | 后续部署可能；凭据泄漏风险高 | HTTPS＋绑定主控身份，无明文回退；T01 只证明 factory 门槛，T03/T05 负责实际信任部署 |
| 拷贝长期静止后继续写入 | 已接受的操作性就绪局限；来源清理风险 | 保存就绪/快照证据和实际变更，来源重新核验；不宣称绝对完成证明，也不永久禁止普通来源清理 |
| 备份恢复回退 epoch 或漏已发生外部提交 | 恢复时可能；重复执行风险高 | 恢复库不能自动激活；保留 operation/receipt，T05/T08 先跨组件 reconcile |
| 审批到期导致稍后 cleanup 等待 | 可发生；属于异常恢复成本 | 明确每次新 operation 的准入检查；保留归档事实，不自动延长授权，后续 UI 提供重新批准 |

本规格选择的是工作流内的协议边界，不改变旧 Hub 的普通 LAN 监控合同，不提前决定 T03 调度算法、T05 安装器端口/证书存放或 T06 默认审批时长。不实现通用 job shell、自动执行发现的翻译服务、真实组件迁移、升级/发布或 NAS 写入。

移交顺序：独立 critic 审查本规格与 T01 AC → driver 分类并冻结结论 → 如配置要求则独立外部设计审阅 → tests-first 实施 → 当前 revision 的完整检查与独立实现外门。初稿本身不构成实现许可；本文件未记录任何尚未运行的测试为通过。
