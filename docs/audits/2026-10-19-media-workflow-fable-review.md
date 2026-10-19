# 持续影片工作流：Fable 独立设计审阅

日期：2026-10-19。关联 [EPIC #271](https://github.com/AlvinShenSSW/Taskpaw/issues/271)。本记录是设计审阅与处置，不是开发、部署或真实片源操作报告。

## 结论

Fable 返回 **SOUND WITH CONCERNS**：总体设计可行，未提出 P1 阻断；共 1 项 P2、5 项 minor。六项建议均保留在下表，不能解释为问题已实现修复或无条件批准上线。

作者为 Codex；独立审阅模型请求和响应均为 `claude-fable-5`，effort 为 `medium`。调用于 05:41:27 UTC 开始、05:47:42 UTC 完成，耗时约 6 分 15 秒。审阅工具仅有 Read、Grep、Glob。回执一致性检查结果为 `consistent=true`、`reviewsComplete=true`、`allRequiredApproved=true`，响应模型身份为 `verified`。

## 实际覆盖

Fable 报告完整阅读 [EPIC](../specs/2026-10-19-media-workflow-epic.md)、[架构与契约](../specs/2026-10-19-media-workflow-architecture.md)、[开发计划](../specs/2026-10-19-media-workflow-development-plan.md)、[UI/UX 规范](../../design-system/taskpaw-v3/pages/media-workflow.md)、[V3 原型](../../design-system/taskpaw-v3/preview/media-workflow-v3.html)，以及 T01–T09、M01、F01、F02 全部十二条 issue 的正文和验收条件。原型检查针对页面、状态和交互代码，不是逐字审查重复 CSS/SVG。

已核对的重点包括：普通本机 INBOX 自动收件与一次确认；普通监控设备保留原用法；主控同时作为 Agent；全 Hub 迁移而非仅五机迁移；动态设备准入及退役；Miko 完整后处理、真实字幕状态、旧图刷新及 `folder.jpg`；NAS 只追加、未知写入核对、持久回执后精确清理；Mac 只读桥及唯一通知权威。Fable 未发现这些要求在方案、UI 和相关 issue 间矛盾。

Fable 直接检查了 TaskPaw 源码及 Miko/Filmpaw 的十八份源码镜像，核实了删除设备会清历史、进程内 GPU 锁、各组件 Python 约束、Miko 图片标识与目录封面、Filmpaw 数据库独占与原生对象绑定清理等事实。

## 发现与处置

| ID | 级别 | 发现 | 当前处置及后续位置 |
| --- | --- | --- | --- |
| FB-01 | P2 | 新 LAN 执行协议未明确传输保密性/完整性，以及普通请求的重放边界。 | Deferred。现有宪法采用 LAN＋每 Agent Bearer 信任边界，不构成当前设计的 P1；T01/T03 冻结执行协议时应明确 TLS/等价保护，或明确记录受信局域网假设。不得据此声称已实现加密。 |
| FB-02 | minor | 开发计划八项既有 issue 表后仍写“以上四项”。 | Deferred。以表中八项及各 issue 的明确依赖为准；后续计划修订时校正文句，不能借文字误差跳过依赖。 |
| FB-03 | minor | 122 项原型检查结果未作为六份冻结材料中的独立证据交给 Fable。 | Deferred。作者确有浏览器报告与截图，本记录列出摘要和指纹；Fable 没有执行或独立核验这些检查。T06 实现时仍需重新验证，不把本次结果当产品测试通过。 |
| FB-04 | minor | 成功探测到 720p、1440p、隔行等素材时，1080p/4K 分类映射尚未明确。 | Deferred。T01/T02/T03/T04 冻结 profile 时应确定范围或显式转异常；缺少适用已验证 profile 不得派发。不能自行将其他素材降为 1080p。 |
| FB-05 | minor | 审批有效期到期时，排队中与已开始归档的处理语义尚未明确。 | Deferred。T01 已规定过期/撤回批准不能归档或清理；T01/F02 还应冻结准入、执行中、回执后及 cleanup 的具体语义。到期不能抹掉持久回执或以失败为由重复入库。Fable 的“只在准入检查有效期”是建议，需与既有验收协调，尚未采纳为契约。 |
| FB-06 | minor | 最终确认字段未明确显示视频时长/体积，可作为中断复制残留短片的人工线索。 | Deferred。建议 T06 在确认明细展示已有 manifest 数据，无额外勾选；它只能帮助识别异常，不能证明发送端复制完整。 |

这些是设计阶段保留建议，本轮没有因低级别建议修改已经审阅的六份冻结材料，也没有新增实现或另买一轮空审阅。无未分类项、无开放的已确认 P1；结构性 P2 仍需在后续实施/合并边界明确处理。

## 已发布的十二条子任务

以下均为设计待开发任务，已关联到父 EPIC，未宣称实现完成：

| 工作 | Issue |
| --- | --- |
| T01 持久工作流与授权协议 | [Taskpaw #272](https://github.com/AlvinShenSSW/Taskpaw/issues/272) |
| T02 普通 INBOX 与快照准入 | [Taskpaw #273](https://github.com/AlvinShenSSW/Taskpaw/issues/273) |
| T03 设备管理与调度 | [Taskpaw #274](https://github.com/AlvinShenSSW/Taskpaw/issues/274) |
| T04 LTX、ASR、字幕与回传 | [Taskpaw #275](https://github.com/AlvinShenSSW/Taskpaw/issues/275) |
| T05 统一安装与迁移 | [Taskpaw #276](https://github.com/AlvinShenSSW/Taskpaw/issues/276) |
| T06 TaskPaw 工作区与确认体验 | [Taskpaw #277](https://github.com/AlvinShenSSW/Taskpaw/issues/277) |
| T07 Mac 只读信息桥 | [Taskpaw #278](https://github.com/AlvinShenSSW/Taskpaw/issues/278) |
| T08 跨组件恢复与未知结果核对 | [Taskpaw #279](https://github.com/AlvinShenSSW/Taskpaw/issues/279) |
| T09 安装、五机验收与回滚 | [Taskpaw #280](https://github.com/AlvinShenSSW/Taskpaw/issues/280) |
| M01 完整 Miko 后处理 | [MDCX304 #296](https://github.com/AlvinShenSSW/MDCX304/issues/296) |
| F01 多 NAS 演员位置候选 | [Filmpaw #46](https://github.com/AlvinShenSSW/Filmpaw/issues/46) |
| F02 安全归档与精确清理 | [Filmpaw #47](https://github.com/AlvinShenSSW/Filmpaw/issues/47) |

## 证据与限制

作者的独立无头 Edge 检查：**122 项通过，0 JavaScript 异常，0 HTTP 请求**，覆盖 1366px/390px、三种使用模式、设备生命周期、一次确认、最终封面/目录封面、审批失效和来源清理异常。静态检查 **103 项通过**，包括本地链接、JavaScript 语法、十二条 issue 结构与依赖。它们只验证离线原型与设计结构，尚未运行真实五机、GPU、字幕、Miko 或 NAS 流水线。

| 证据 | SHA-256 |
| --- | --- |
| EPIC | `e33481eda5b7bc11d26d4848383350334e70fe60d8c73c0aee3e80a40ade4f66` |
| 架构 | `ca57512b8e730f018863f3450e6bd3ac44a6258232fa4fb3f92f5e59735724f2` |
| 开发计划 | `dbea4cb037bde3b945a6398e9518f4409a19f420618a21f28e804dc31fbee872` |
| UI/UX 规范 | `44ee77802fd9bbe85d20be5551165981f3f2a28b75a71c2a55a32c6070a63ecf` |
| V3 HTML | `3a9e0099eeeea92301727045c54c1768c90e4521aedce18c9fd8a9220aae1905` |
| 十二条 issue 草案 | `89d7a1a0544cec6b90486976d3fce3311a07a2ce1e5d81aadeb34d57dd950292` |
| 浏览器检查 JSON | `466333bb185ee025477aac53c7469aab884b472cfa4dd447165fa7b5df148acd` |
| 静态检查 JSON | `7f27a6bbb1fd422d0779d7ac5f550c991ab4225ff7dfdb1a38fc241e2977e2b9` |
| Fable 最终审阅原文 | `4950552adf82a743da23057dd86f57e7e389c51ade1e306ed1f39df7a076d827` |

根代理核对六份材料在审阅前后完全未变，TaskPaw HEAD 为 `3fc956bcaa75dad5d0b1e52e45876dea2e57850c`。指纹由本机工具计算，Fable 本身不能运行 hash 命令。

Fable 读取本机 Miko 仓库被拒一次，随后使用源码镜像；镜像未含 `mdcx/core/web.py` 的旧图分支，该处未由 Fable 独立验证。根代理已直接核对源码，确有保留旧 poster 即返回的分支。Fable 也未核验 GitHub 既有 issue 的实时状态、同 tree 声明或执行浏览器测试；这些限制保留，不以独立审阅结论代替。

根代理在交付前另行通过 GitHub 核对 #52、#225、#237、#236、#234、#233、#232、#240，八项当时均为 OPEN。这是作者的实时查询结果，不追记为 Fable 执行过的检查。

此前 Kimi K3 审过 V2，结论为 SOUND WITH CONCERNS；其托管发送端包装建议已被用户明确选择普通本机 INBOX 的需求替代，依赖映射歧义在本版已明确。当前设计结论以本次 Fable 冻结版本为准。
