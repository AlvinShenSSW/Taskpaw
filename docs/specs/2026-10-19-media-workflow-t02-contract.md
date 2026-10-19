# T02：普通本机 INBOX 收件与不可变输入快照实施合同

日期：2026-10-19。状态：初始范围冻结，等待独立设计 debate；不是实现许可或联调通过记录。

## 1. 规格复述与当前基线

[Taskpaw #273](https://github.com/AlvinShenSSW/Taskpaw/issues/273) 要求用户只把影片目录放入唯一 Windows 主控的普通本机 INBOX，后台持续发现、判断操作性就绪、建立可验证工作副本及 manifest。没有投递程序、ready 文件、提交按钮或逐片风险 checkbox。正常流程的另一次人工操作仍只是后续 T06 的最终确认入库。

上位依据：`docs/specs/2026-10-19-media-workflow-epic.md`、`2026-10-19-media-workflow-architecture.md` 第 5、6、9 节、`2026-10-19-media-workflow-development-plan.md` 的 T02/W2 和跨包不变量；T01 接口依据 `docs/specs/2026-10-19-media-workflow-t01-contract.md`。Taskpaw 调查基线 `885ead2503aecb538705f72fa3299d74e20d2b24`。本草稿还读取了 T01 工作树中尚未完成全部检查与独立门的当前实现；它是接口候选，不能称为已交付依赖。T01 通过并固定实际 revision 后才能实施最终 schema 迁移和真实联调。

issue REST 正文已核对；当前 comments 为空。首次 GraphQL 请求网络超时，随后 REST 成功，不把超时当作不存在。读取时仅有与本功能无关的 open PR #256；没有据此改动其代码。

### 1.1 已有实现与缺口

| 证据位置 | 已有行为 | T02 不能依赖的假设 / 必需增量 |
|---|---|---|
| `taskpaw_v3/monitors/plugins/folder.py:45,75,118` | 内存保存文件大小与稳定起点，达到时间发完成事件 | 非持久影片包；无成员关系、原生身份、整包 hash 或工作副本证明；旧 monitor 不改语义 |
| `taskpaw_v3/monitors/plugins/jasna.py:347,386,2322` | pixel-count 档位、只取首视频宽高；probe 失败回 1080p | 不能作为 workflow 授权；1440p 被分到旧 4K 档也不是 profile 证明 |
| `taskpaw_v3/workflow/models.py:275,310,331,370,504` | `NativeIdentity/MediaProbe/ManifestItem/ReadinessEvidence/InputManifest`；完整 hash、字幕映射与 workcopy 对应校验 | 原始媒体字段缺 rotation、field order、frame rate、SAR、视频流选择；发现、稳定状态和目录上下文未持久化 |
| `taskpaw_v3/workflow/film_store.py:207,233` | intake 角色可 `create_film/put_input_manifest`；新 input revision 建阶段、使 context 失效 | 只能在完整新 manifest 到达后更新；不能及时记录“旧源已变化但新包未就绪” |
| `taskpaw_v3/workflow/read_store.py:19` 与 `models.py:1408` | `get_film` 返回 FilmRecord、stages、context | 不公开完整 InputManifest；intake 不得借访问 `_conn` 填补读取缺口 |
| `taskpaw_v3/workflow/schema.py`、`store_core.py` | 单 FileLease、锁内短 SQLite 事务、角色/CAS/幂等 | 无 discovery/source generation/收件 work operation 持久表；T02 需显式增量 |
| `taskpaw_v3/core/state.py:31` | OS FileLease | 复用单 owner 思路；不能由文件存在推断服务归属 |
| `taskpaw_v3/hub/server/upstream_worker.py:654` | Windows 原生受管进程、Job、身份和退出证明 | 可复用既有受管启动原语；不是现成媒体收件器，不能用 best-effort post-start JobKeeper 替代证明 |

## 2. 冻结范围与正常用户路径

本轮只有 Windows 主控本机收件：配置一个明确的受控 INBOX 和一个不重叠的私有 work root。真实机器路径只存在操作者私有配置；公开例子使用 `<INBOX>/Actor/Film/files`、`<WORK>/intake/...`。不预填用户路径。remote workdir、Miko 暂存、NAS 成品均不是收件入口。

普通 TaskPaw 默认无此服务；只有明确启用 workflow 的 controller 角色可构造并启动 IntakeService。计算 Agent 和普通翻译机不扫描、不下载模型、不修改 monitor。T02 交付可显式构造的 Python service/store API 和脱敏查询；T05 装配生产服务、T06 装配 UI。不得为了展示队列临时打开 LAN 控制、在 import 时启动线程或挂载现有 Hub app。

T02 可以读 INBOX、在自己的 work root 建临时及已验证副本、写 workflow 状态；不能写入、重命名、移动或删除 INBOX 中的任何对象，不能写 NAS、执行 GPU/字幕/Miko 或发入库批准。失败私有临时副本只在本 operation 原生 owner 已收束且路径/身份确属受控临时目录时回收；已经发布的副本交后续精确清理合同。

日常状态：`receiving` → `waiting_stability` → `snapshotting` → `operational_ready`；不完整/仍写回到 receiving，关系歧义是 `needs_attention`，缺工具/未知身份/失败为 `blocked`，磁盘不足 `waiting_capacity`，退出证明失败 `unknown_owner`。状态必须附固定 reason、观察时间、最后成功补扫与 generation，不把异常显示为空队列或 idle。T06 映射为“收件中、待补全、待人工、已就绪”；T02 不实现页面。

## 3. 发现、包边界与持久身份

### 3.1 监听与补扫

Windows 目录通知只作脏标记，不能作完整成员清单。使用固定本机 root 的 `ReadDirectoryChangesW` 后台 owner；启动、重连、事件缓冲丢失/异常以及定时周期都执行完整有界补扫。通知风暴按包合并，队列有上限；溢出后持久标记需补扫，不能丢掉变化后仍允许旧批准。通知实现的取消和 join 必须有测试；原生读取不能阻塞主控 event loop。[Microsoft 文档](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-readdirectorychangesw) 给出该通知接口，完整性仍由本方案的补扫与快照检查承担。

后台默认 policy：稳定窗口 60 秒、补扫 30 秒、最多 4096 成员/包、目录深度 8、最多一个快照/hash operation、最多一个 probe、最多 256 个内存脏包提示。数值必须通过类型化范围校验并持久化 policy revision；超限显示具体原因，不丢文件。时间调度使用 monotonic；重启后重新开始稳定窗口，不能用关机时间抵扣。目录不可读或部分枚举不能作为完整空目录、不能标记所有成员消失。

配置只接受能确认本机原生身份及受支持文件系统语义的 root；UNC、映射网络盘、reparse root、与 work root/其他数据根重叠均不作为本轮默认 INBOX。Windows 上通过 SMB 暴露给其他机器的本机目录仍从本机路径收件，用户动作不变。配置缺失只报告未配置，不创建假根、不跟随回退路径。

### 3.2 package 与 Film 的关系

`package_id` 表示一次目录上下文收件身份；`film_id` 是后续独立影片工作单元。持久记录 root ID、目录 native identity、首次发现 session、当前受控相对路径、generation、完整观察摘要、film 列表。目录路径、演员名、番号都不能作为主键。相同 native 目录的 rename 更新路径并重新观察，不新建 film；同一路径被不同 native 目录替换则新 package/film，旧历史保留。无法证明身份相同则冲突待核对，不凭同名合并。

- 常规 `Actor/Film/files`：保留 actor 相对父上下文和 Film 目录边界，默认整包一个 film；里面明确连续分卷属于一个 film 的多个 main_video，保留原卷序及每卷字幕。
- 单目录多个不同影片：只在可证实的文件名/NFO/明确子目录归属下拆成多个 film。共享附件作为来源引用保留；不能将第一张封面、首个 NFO 自动分给全部影片，也不能每个视频无条件拆包。歧义停在 needs_attention，T06 后续提供关联修正；不增加正常片的确认动作。
- `CD1/CD2` 等明确卷序可识别；重复序号、编号缺口、不同 stem 的无规则视频不猜序。未来未出现的末卷无法证明缺失，属于第 8 节局限。
- SRT 以精确 stem、卷序和显式语言标签关联。单视频目录内唯一无标签 SRT 可确认 video 关系，但语言无法确定时不伪造 `zh`；现 T01 要求语言，因此待修正或由明确有证据的语言检测策略处理。多个候选/语言冲突要待人工。只保留原件和关系，不翻译、不改字幕文本。
- NFO 解析有字节上限、拒绝外部实体和网络解析；错误不是忽略原件，而是保留并报告。图片、NFO、字幕及允许附件全部有 member ID/hash。未知附件不静默遗漏，待 policy/人工归属；隐藏文件也参与成员观察。明确临时后缀/临时属性意味着尚未就绪；不能简单把它们过滤后称整包完整。
- 保留目录级 `parent_context`、group membership 与 shared source refs，供 T04/M01 决定多视频目录 `folder.jpg`；T02 不生成或修改封面。

相同 package 的重复通知/重启/相同 fingerprint 不增 film、不增 revision。已发布包有变化：先 dirty，再新 generation；内容及所有绑定最终与原 snapshot 完全一致时可以记录核对结论，但旧批准不能自动恢复。新目录投递相同内容创建独立 film，并给出去重候选；同番号/同标题不同内容始终独立。整包摘要只找候选，必须比较完整角色、成员、关系及内容，不能单凭视频 hash 静默丢弃新附件。已归档 package 的重复事件保留原 receipt，不重传；真实改动进入需核对，不自动把旧 receipt 当作新 revision 的归档。

## 4. Windows 操作性快照与私有副本

稳定观察签名至少含完整成员集合、相对路径、native identity、size、mtime/可获得的 change 证据、角色与关系的版本化结果。稳定只允许进入下一步，不直接生成就绪。

一个受管 snapshot operation 执行：

1. 取得预先登记的 root/package native 身份，枚举完整成员与目录祖先；逐级拒绝链接/reparse、ADS、大小写冲突、路径逃逸和不支持身份。拒绝任意 URL/协议路径。保留所有将读取对象的原生句柄及父目录身份，不能检查路径后不再约束对象就重新打开。
2. 对成员取得只读、允许其他读但不允许共享写/删除的 Windows handle，覆盖完整读取/复制期间。共享冲突为 receiving；权限/身份不明为 blocked。有共享约束并非未来不会续拷的证明。Microsoft 的 [CreateFileW 共享语义](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew) 规定打开冲突及句柄存续期；具体目录/父级约束仍须用原生测试证明，不能用 Python `open()` 或 `exists()` 冒充。
3. 在 `<WORK>/intake/<operation_id>/partial` 使用排他创建的新文件，逐块读取每个已持有的源对象、计算 SHA-256、复制字节；不 hardlink、不把原文件作为工作副本。达到 configured 字节/成员/剩余磁盘上限立即保留原因并停止。所有文件 flush 后重新完整读取工作副本校验 hash/size。原生身份、成员集合前后均需一致；枚举失败或变化本次不发布。
4. 在已校验私有副本上执行固定 ffprobe，无用户自由 argv、无 shell。source handles 保持至最终来源复核完成；probe 不需要重新读取不受控源路径。图片/NFO/SRT 原件不重写。
5. 在工作根同卷把私有 partial 原子改名为不可变快照目录，保存 completion metadata 和完整 payload digest。通过短 DB CAS 事务提交 snapshot、input manifest、目录上下文、copy proof 与 readiness，且核对操作 generation/配置/包身份仍 current。父目录替换、成员事件或 policy 变更均拒绝过期发布。
6. 一旦 source handles 释放，未来写入仍可能发生。继续监听/补扫；新变化先关闭旧准入。不声称目录通知可捕获所有未来变更的瞬时点；最终 archive/cleanup 的实际来源复核仍由后续阶段承担。

copy/hash/probe 均运行在可终止且能证明收束的本机受管 owner 内；不在 SQLite 事务或 async event loop 做阻塞 I/O。可窄复用现有 `upstream_worker` 的 Windows 受管启动原语，新 helper 只接受固定 intake/probe 消息；不设计通用 shell 服务，不复制另一套任意进程框架。child 不打开 workflow DB，结果先是证据，只有父 store 能提交。固定 ffprobe 对本地私有文件禁用不必要协议，避免媒体 playlist/NFO 引导网络或任意路径读取；不支持的媒体容器进入 blocked。

每个 snapshot 使用一个固定 helper、一个受管 Job；所有源 handles 保持到最终来源复核。令本包需 probe 的主视频数为 V（V ≤ 4096），源码/frozen 固定启动层数预算 B=4。每视频至多创建一个 ffprobe，operation 内不重试、不另开版本探测进程；版本来源须由该固定运行或已验证工具 metadata 给出。intake 自己维护单调的原生 Job accounting：累计创建总数和保留 birth HANDLE 数各 ≤ B+V ≤ 4100，同时 active ≤ B+1 ≤ 5；非法类型、倒退、不一致或越界均停止后续 probe 并保留不确定性。B 是需要 source/frozen 原生验收证明的产品预算，不假称所有启动方式已满足。不得调用既有 helper 限制累计 16 的 `_accounting`、`capture_members` 或 `drained`。

固定 helper 对每个 probe 使用 suspended CreateProcess，禁止 breakaway；发送 operation/item/单调 sequence/PID/creation FILETIME。父进程打开该 birth 的 HANDLE，核验 PID、creation time、同一 Job、当前 item/sequence 及预算，并将已观察 owner 持久化后才 ACK；helper 只在匹配 ACK 后 resume，一次 sequence 不可启动两次。父一直保留该 probe HANDLE 到 operation 收束，因此短命 probe 也不会落在两次 census 之间而丢失身份。ACK 缺失/不匹配、打开或身份失败必须终止本 Job 并核对，不能继续新 probe。协议仅允许固定 probe，不接受用户自由 argv。

intake 只从底层 API 取得 accounting/members/identity/wait/close，并保留独立的已观察身份集合。固定启动层可能在采样前退出，故不强求历史 total 等于 retained HANDLE 数；但已观察 PID 的打开/身份/等待/关闭失败永远 sticky，不能因随后 PID 不存在或 Job 空而抹掉。退出时停止并等待 helper、每个已记录 probe 和其它已观察成员，随后证明 Job active=0、I/O owners joined、自己持有的所有 HANDLE 均关闭且结果已持久化。缺一项则保留唯一 intake 槽位及工作目录为 unknown_owner。不得把等待超时当作退出事实，也不得改变现有 upstream/activity 消费者的原生策略。

默认单 item probe deadline 30 秒、整个 copy/hash operation hard deadline 6 小时、无字节进展 deadline 120 秒，均为有界 policy 可调。超时、停止、输出超过限制后先终止并等待原生 owner/所有已观察 child 与 I/O owner；无退出证明时持久 unknown_owner、保留 workdir 和槽位，不能另开第二个 operation 或删除仍用的临时文件。退出之后才能宣称取消完成。父进程崩溃后的旧 owner 不因新 boot 而被释放；不能恢复身份则阻断并由 T08 核对。固定启动方式见第 10 节。不能用 asyncio timeout 返回假装原生 I/O 已停止。

## 5. 原始媒体信息、FB-04 与 profile 分离

现有 `MediaProbe(width,height,duration_ms,video_streams,audio_streams)` 不够。最小扩充为每个主视频记录选定 video stream index、coded width/height、可展示 width/height、SAR/DAR 有理数、avg/r frame-rate 有理数、field_order、rotation/display matrix 观察、codec/pixel format、duration 及来源字段、音频/字幕流摘要、attached-picture 标志、tool/version 与 probe 原始 payload digest。可缺省字段必须显式 unknown；`0/0` 不是合法 fps，bool 不是 int，冲突的 duration/rotation 不默默择一。封面流不当主视频，多真实视频流无法确定主流则待人工。

固定生成命令形态：`<ffprobe> -v error -show_format -show_streams -of json <private-workcopy-video>`，实现应以固定 `-show_entries` 收窄字段和 bounded stdout；不加 `-show_packets/-show_frames` 产生无界输出。JSON 至少拒绝重复键、超长/过多流、NaN/Infinity；缺失字段不赋假值。[ffprobe 官方文档](https://ffmpeg.org/ffprobe.html) 支持 JSON、format/stream 信息及字段选择；probe 成功只证明本次解析结果，不是完整解码或外部复制完成证明。

`MediaFacts` 与 `ProcessingEligibility` 是两种记录：前者事实不可被 profile 改写，后者含 policy/profile ID+revision、decision、reason，不能来自文件名或 GPU 手填标签。工作副本验证成功可以在查询中显示 snapshot_ready，但只有必需原始字段已知且匹配明确 workflow profile 的 generation 才能成为可领取输入。

FB-04 首版冻结的表示与拒绝规则：

| 实测媒体 | 本层处理 |
|---|---|
| progressive、无旋转、方形像素，尺寸不超过 1920×1080（含 720p） | 可匹配显式定义的 ≤1080 profile 范围；仍须匹配 fps/codec/tool 要求，不能据此声称任意 8GB GPU 可跑 |
| progressive、无旋转、方形像素，超过上述范围但不超过 3840×2160（含 1440p） | 候选较大 profile 范围；须明确 profile 允许该实际尺寸，不能把“旧 Jasna 4K 档”当成能力授权 |
| 旋转、非方形像素、隔行、多个主流、未知 field_order、超过范围、VR | 保留原始事实，默认 profile_required / needs_attention；只有后续明确验证的 profile 才放行，不自动转码、去隔行、裁剪或转正 |
| probe 失败/缺主视频/未知宽高或关键字段 | blocked，不回退 1080p |

上述范围定义可表达的 profile 匹配边界，不内置未经验证的 GPU 能力声明。T02 没有默认批准的 GPU profile；配置的可用 profile 必须绑定明确 policy/revision/tool 和原始媒体约束。没有已批准可用 profile 时只能完成收件证据、不能开 compute 准入。T03 最终仍按 worker 实测能力、tool/model/profile/config 和 GPU owner 再检查，T02 不提供硬件授权。所有分卷均须匹配；一卷未知不能只看首卷放行。T01 的 compute claim 消费新增事实/eligibility 门槛，避免其它调用者绕过 service 直接 claim。

## 6. 必需持久模型、公开 API 与 T01 增量

### 6.1 单 writer 与数据职责

沿用 WorkflowStore 的单 FileLease/连接/事务锁。T02 repository mixin 由 store 内部执行 SQL；`intake.py`、watcher、worker 只能调用公开 API，不能访问 `_conn` 或另开可写连接。长 I/O 在事务外，事务只做绑定、CAS、审计和状态提交。

最小新增表（具体 DDL 在 T01 最终基线上冻结，不提前改其当前 schema）：

- `intake_roots`：root ID、native identity、配置 revision、受限私有路径、work root ID、policy revision、enabled、owner component。仅本地主控 operator 配置；同 authority 只有一个 enabled 人工 input root。
- `intake_packages`：package ID、root/native 目录 identity key、当前 relative context、generation、version、state/reason、观察签名、stable 观察时段、成员/分组摘要；identity 唯一约束负责重复发现。
- `intake_films`：package/film ID、分组 key、当前 generation、上下文/共享引用；不按标题去重。每个 Film 的 source admissibility 为 `unverified/ready/dirty/missing/unknown`，指向已批准输入 generation/hash。
- `intake_operations`：operation ID、package/generation、owner boot/原生归属证据、state、受控 workcopy ref、结果 digest、publication_allowed。进行中或 unknown 的同 package 不能新开；全局并发额度持久核对。
- `intake_snapshots`：已完成成员清单、完整媒体 facts、成员关系、工作副本证明、输入 revision/hash、发布命令 request ID；不可变。payload 可用现有 manifest/evidence 表存，避免双份可变内容权威。

观察/通知幂等、所有变化有审计。只存 native identity 的可重建证据，不持久 HANDLE 数字作为所有权。旧 snapshot 和 receipt 永不 cascade 删除。迁移采用正式 schema version 增量、单 writer、backup＋事务 DDL；不改号假装 v1 仍同 schema。未知/损坏库拒绝，不静默建新库。

### 6.2 公开接口草案

以下都是有限本地 Python API；命令统一 `(auth: AuthProof, meta: CommandMeta, command: StrictDTO) -> CommandResult`，每事务重验角色/epoch/撤销/CAS/幂等，结果通过 typed query 读取。新增 DTO 禁止 arbitrary dict 替代必需字段。

| 方法 / command | 最小字段与职责 |
|---|---|
| `configure_intake_root / IntakeRootPut` | operator；root/workroot ID、expected version、受限路径+已核对 native identity、policy、enabled。不远程接任意路径 |
| `observe_intake / IntakeObservation` | intake_adapter；root+config revision、package 候选 UUID、目录 identity、complete/partial 观察、member/path signature、source时间、触发原因。同 identity 去重；新包预分配 Film ID；变化在本事务先关闭关联 film 准入并 invalidates context，不能等新 hash |
| `begin_intake_snapshot / IntakeSnapshotAdmission` | intake_adapter；package ID、expected version、generation、operation ID、owner boot；检查稳定/关系/配额，持久 operation 后才建 workdir/启动 helper |
| `record_intake_owner / IntakeOwnerObservation` | intake_adapter；operation/version/boot、原生 owner 及 sticky uncertainty。事实不能由新的 clean 报告抹掉 |
| `finish_intake_snapshot / IntakeSnapshotResult` | intake_adapter；operation/version/generation、结果类别、typed ExitProof、完整 candidate manifests/context/media/workcopy/readiness 或固定错误；当前绑定和 owner 退出证明通过后原子发布所有涉及 films；stale 结果仅记录、不建可领取阶段 |
| `cancel_intake_snapshot / IntakeCancel` | intake_adapter 或 operator；operation/version/reason；关闭 publication，保持 owner，完成赢得事务则返回已提交事实，不声称已撤销 |
| `resolve_intake_grouping / IntakeGroupingPut` | operator；package/generation/expected version、精确成员→film/卷/字幕/共享关系。仅异常修正，不用于正常逐片 ready；更新只失效旧证据，不能直接标 ready |
| `get_intake / list_intake` | 脱敏 typed view；operator/observer 或本 root intake 身份。固定分页，展示原始信息、state/reason/时间与证据摘要，不返回任意私有绝对路径 |
| `get_input_manifest(auth, film_id, revision)` | 新增授权读取完整 InputManifest。operator、所属 intake、以及自己已领取对应 input 的 worker/adapter 可读其执行必需内容；observer 只读摘要；私有 root 实际路径由本地受限 resolver，不从 LAN 返回 |

`finish_intake_snapshot` 在同一 store 事务复用/抽出 `create_film/put_input_manifest` 的内部纯 SQL 实现，不嵌套调用两个各自开事务的公共方法。多影片共享包必须原子发布分组与各 manifest，避免部分包成为 ready、剩余附件无引用。幂等键在启动工作前持久预留，回复丢失用同键查询/重放；不是把文件副本再做一遍。

### 6.3 来源变化的立即失效消费者

观察到真实来源变化、仍写、root identity 不明或完整补扫发现缺失时，`observe_intake` 先在事务中置相关 film dirty/unknown，递增 generation，取消旧快照 operation 的发布资格并使 approval context invalidated。partial/无法枚举只记录 unknown，不误报确定删除。随后才做重新枚举/hash；旧批准不因内容后来恢复而自动有效。

以下新操作必须在同事务检查 source generation admissibility，不能只比现有 current_input_revision：新 `claim`、`put_approval_context/approve`、`begin_archive`、`begin_cleanup(input)`；未生成完整新 revision 也须拒绝。已经发生的结果、归档 receipt、清理事实必须继续按原 operation 记录，不能借 dirty 抹除外部提交；旧 GPU/process owner 也不释放。运行中的旧 stage 可以记录其 attempt 事实，但不能作为新 generation 的依赖。已经准入的外部写入出现变化后转待核对，不自动重传或以“取消”声称不存在副作用。

已归档 film 检出来源变化：保留 receipt 和原批准历史，input 为 cleanup_pending；不自动新建 archive 任务。独立且符合自身绑定的受控成品副本清理不应被无关 input 变化永久阻塞，这部分须与现有 T01 scope 门槛协同，而不是粗暴给所有 cleanup 加同一 dirty 拒绝。真正删除仍由 F02 完成，T02 不出删除许可。

这些是 T02 完成 issue AC 必不可少的 T01 facade/DDL 消费者增量；在 T01 本轮冻结修复结束前只写此规划，不擅自插入 T01 修复批次。本合同冻结这些公开命令和必要 schema/consumer 增量；具体严格 DTO 字段按此约束实现。若不能提供这些入口，T02 不可用 `_conn` 绕过或假称集成完成。

## 7. 恢复、重试与清理边界

- 每次启动先恢复 operation/unknown owner、核验 root/config/authority，再启动收件；重启不把 snapshotting 变成 ready，不自动释放 worker槽或删除临时文件。
- DB 尚未提交前进程死：copy 目录留 orphan candidate，不能自动当 manifest；由已有 operation ID 核对/收束后重做。快照目录 rename 完成但 DB 尚未发布：重新核验 completion digest、全部内容/身份和 source generation 后可用相同 request ID 完成提交；无法核对则保留待处理。DB 已提交、回复丢失：公开 query 返回原结果，不重复 Film/阶段/副本。
- 稳定窗口/证据不跨策略变化沿用；磁盘满不影响其它已就绪包的读取。持续变动的一个包不能饿死其余包；队列公平有界，补扫页可续，但只有完整 package enumeration 才形成成员证据。
- 源文件被用户删掉/移动不授权回收其它路径。rename 原生身份已证明时可以更新当前位置并要求新 generation；不扩展旧 cleanup 路径。hardlink/shared source 引用完整保留，不能因为某一 film 完成就认为所有引用可删。
- 完整 NAS receipt 后，普通 INBOX 在同一原生对象/成员/hash/写占用复核齐全时可以由 F02 自动精确清理。T02 的 ordinary_directory 模式不是永久禁删标记；外部拖入前的原件、整个 actor/root、后来新增对象不属于删除范围。

## 8. 验收、技术假设与风险

| AC | 必须观察到的结果 |
|---|---|
| A01 自动收件 | 真实临时普通 INBOX 放入 Actor/Film 包，无 ready/额外提交；通知与周期补扫最终发现。关闭通知/注入溢出后可补偿，普通/remote 模式不构造收件器 |
| A02 就绪与写占用 | 实际 Windows writer 持续写、共享写 handle、copy 暂停/恢复、成员替换/删除/迟到/隐藏临时成员时不错误发布当前 snapshot；稳定＋probe＋整包 hash＋workcopy 复核齐全后自动就绪 |
| A03 原生路径身份 | symlink/junction/reparse、父目录替换、大小写冲突、ADS、escape、hardlink/sharedsource、原路径重新放不同 native 对象；不能读出允许根或把新对象当旧源。原生 fixture 不用真实 NAS |
| A04 包关系 | 单片、多卷、多语言 SRT、已有 NFO/图片、中文/空格、同目录多影片和共享附件；精确映射与 parent_context 保持；歧义保留全部对象并待人工 |
| A05 持久去重 | 重启、重复事件、相同 request 重放、同目录 rename 不重复 Film；新目录同内容仅候选；同番号不同版本不同 ID；提交前/后 crash 不能双发布 |
| A06 变化立刻失效 | 已有 ready/approval 时触发源变化，在新 hash 完成之前，经公开 API claim/approve/archive/input cleanup 被拒；receipt 保留、unknown owner 不释放、旧结果不能推进新 generation；workcopy 独立范围不被误杀 |
| A07 媒体事实 | 真正 ffprobe 的小合成视频/fixtures 覆盖 progressive 720/1080/1440/2160、rotation、interlaced、SAR、attached picture、多流、损坏/缺 tool/超时；raw facts 不被旧 tier 映射覆盖，未知/无 profile 不可领 |
| A08 完整读取与副本 | 源中途变化、工作副本截断/hash 错、部分 copy、disk full、数据库 commit/rename 故障；均无 ready；worker只读已验证副本，原件内容/名字未被修改 |
| A09 有界 owner | 真正本地原生阻塞 I/O 与 probe 子进程 timeout/stop，父 API 继续响应；同包至少 17 次真实顺序 probe、短命 probe、source/frozen 额外启动层不因累计16限制误报 unknown；验证 B=4、V=4096 上限模型与4097拒绝、无 ACK 不 resume/不重复 sequence、中途失败时 source handles 保持及正确收束。成功收束有全部已知 birth 退出＋Job空＋I/O join＋HANDLE关闭；注入 open/identity/wait/close/query 失败即使随后 PID 消失仍 unknown，保留目录且不重用槽 |
| A10 schema/API | 正式前版库 backup/迁移故障/重开、单 writer 竞争、权限/撤销、CAS/幂等、分页/脱敏；任何正常路径不通过测试直写 SQL 注入身份/ready 状态 |
| A11 静止截断局限 | 构造仍可解析且可完整读取的短媒体文件，长期无 writer；记录它可能通过操作性准入，随后追加/迟到附件必须触发失效。不能把“没有发现写者”断言为外部原片完整 |
| A12 旧功能 | folder/Jasna 手动行为、旧 /ping/status/events、loopback 控制、普通 TaskPaw 配置、已有翻译机保持；本 issue 不启 GPU、不写 NAS、不删 source |

重要假设：native API 的共享约束适用于实际受支持的本机文件系统；它不能证明未来没有写入。hash 不能检测尚未存在的外部字节/卷/附件。ffprobe 不是整片解码验证。媒体 profile 对 LTX/GPU 的真实适用性由 T03/T04 验证，本轮只冻结表示与拒绝门槛。上述风险不转化为额外用户正常步骤，也不把普通输入永久禁清理。

风险优先级：长时间静止截断（可达、来源完整性影响高，接受的效率假设＋真实失败测试）；并发替换/迟到附件（可达、误绑定影响高，同 handle/native identity＋generation CAS）；媒体分类（可达、错误 GPU 准入影响高，事实/profile分离）；磁盘容量（常见、可恢复，先留容量＋waiting_capacity）；原生 owner 无法收束（少见、影响高，unknown 保留，不用扩大 timeout 隐藏）。

## 9. 文件职责、执行面与交付顺序

| 文件 | 拟变更职责 |
|---|---|
| `taskpaw_v3/workflow/intake.py` | 显式 IntakeService 生命周期、通知/补扫/公平调度、受限公开 store 调用 |
| `taskpaw_v3/workflow/manifest.py` | 纯包划分/角色/字幕卷序/context、完整摘要、重复候选；不做 DB/子进程 |
| `taskpaw_v3/workflow/intake_models.py` | discovery/policy/operation/snapshot/query 类型；与既有 models 保持严格字段边界 |
| `taskpaw_v3/workflow/intake_store.py` | 新表状态/CAS/幂等/原子多 film 发布与查询，纳入唯一 store facade |
| `taskpaw_v3/workflow/intake_native.py` | Windows watcher、身份、共享读句柄和受控 copy/hash；固定 helper owner 与 ffprobe 边界，可按实现拆小文件但不得泛化进程框架 |
| `taskpaw_v3/workflow/models.py` | 必需 raw media/上下文与 source validity DTO 增量，通知全部消费者 |
| `taskpaw_v3/workflow/schema.py`、`store.py`、`auth.py` | 增量 schema/migration、facade、固定角色命令；不改 HubStore |
| `taskpaw_v3/workflow/film_store.py`、`approval_store.py`、`read_store.py` | 纯 SQL 发布复用、source generation 准入与事实保留、完整 input 授权读取 |
| `taskpaw_v3/tests/test_workflow_intake*.py`、`test_workflow_manifest.py`、`fixtures/workflow/intake/` | 各 AC 的真实临时 DB/文件/native/子进程、虚构包与媒体 fixtures；旧 workflow 测试做新增字段兼容回归 |
| `docs/guides/media-intake.md` | 唯一普通入口、异常原因、剩余技术局限与恢复说明；不写真实路径 |

只读消费者：T03/T04 工作副本领取，M01 目录上下文，F02 来源/共享引用，T06 状态与异常关联，T05 配置/安装服务，T08 恢复；列表不授权提前改它们。生产启动/安装/模型下载、watcher 的现有 Agent/Hub mount、跨机文件传输、真实NAS/用户媒体均不在本轮自动执行面。

后续实现测试命令（项目根，使用 driver 认可的独立私有 TEMP/basetemp）：`uv run pytest taskpaw_v3/tests/test_workflow_intake.py taskpaw_v3/tests/test_workflow_manifest.py`；新增 native/state test 文件同批纳入，然后现有全部 workflow 与 folder/Jasna 回归，最后 `uv run pytest`、ruff check/format、mypy、lock check。原生命令以测试负责创建的临时 root 和虚构 DB 为唯一输入；不启动配置中的 Agent/Hub。

合成媒体由固定 ffmpeg lavfi 小样本命令生成到测试临时目录（例如 `ffmpeg -f lavfi -i testsrc2=size=1280x720:rate=24 -t 1 -c:v libx264 <tmp>/sample.mp4`），没有真实影片/网络输入；codec/build 不可用必须显式报告，不能把纯 JSON mock 当 native probe 已通过。checked-in fixture 可保存小型生成媒体、生成命令和校验摘要；不提交整片或私人路径。snapshot 生成器为固定 intake worker，输入为 root/operation/profile 与句柄绑定，输出仅受控 workdir 和严格结果；DB 生成器仍是 WorkflowStore create/migrate。

实施顺序：先 driver/critic 冻结本草稿及最小 T01 API 增量 → 等 T01 前置检查通过 → models/manifest 与 store tests-first → Windows native owner/worker tests-first → 显式 service 集成与公开 API 全路径 → 真实临时 fixtures 与回归 → 独立实现审阅。初始草稿不消耗 review repair cycle；后续 review-driven 内容修订先由 driver 分类预留，作者不自修。

## 10. 已冻结的初始决策与移交

本次只冻结设计，没有运行任何服务、产品测试、GPU、真实文件收件或 NAS 操作；上述 AC 都是待验证要求。T01/F01 测试不能移作 T02 通过证据。唯一外部 implementation gate 按操作者要求是 Claude Opus；design gate 仍 off。

1. 第 5 节允许表达 ≤1080 / 更大 progressive 的媒体范围；没有任何 GPU/model profile 因宽高或设备名称自动获批。T02 默认只生成完整原始事实及 snapshot_ready，只有明确受控配置的已批准 profile 才满足 compute 输入门槛；T03/T04 负责硬件、工具和模型核验。保留720p/1440p实际事实，不复用旧pixel-tier fallback。
2. 第 6 节 T01 facade/DDL 的最小增量属于 T02。独立 T02 分支建立在固定 T01 交付 revision 上，schema 正式升版；不把这些改动塞进正在修复的 T01。来源变化阻断新消费，却保留已有结果/owner/receipt；独立 workcopy 清理按其自己的绑定核对。
3. 原生启动窄复用 `taskpaw_v3/hub/server/upstream_worker.py` 的 `_WindowsProcess.start/resume/terminate_owned/poll/wait/close` 和其底层 Job/HANDLE/stdin/stdout 原语；现有 `integrations/activity_verifier.py` 仅证明可以窄接入，不证明其 census 适用于多影片 intake。intake 按第 4 节自行维护 accounting、成员和收束策略，明确不调用 `_accounting/capture_members/drained` 的累计16限制，不改 upstream HTTP、writer verification 或其测试/运行语义。新获得的 probe HANDLE 由 intake 独立持有及关闭，不混入旧 helper 的成员上限。任何成员采样/句柄/IO 证明失败不得被之后一次成功抹掉。
4. 唯一新增固定 backend role 是 `workflow-intake`，在 `taskpaw_v3/packaging/backend_main.py` 的普通 Agent/Hub 配置加载之前分派到 `taskpaw_v3.workflow.intake_worker.main`；源码形态是 `[sys.executable, '-m', 'taskpaw_v3.workflow.intake_worker']`，frozen 形态是 `[sys.executable, 'workflow-intake']`。参数不是任意命令或模块，受限请求从父拥有的 stdin 进入；worker 不读默认配置/DB，不启动服务。仅把 `backend_main.py` 及对应 `test_packaging.py` 加入本 issue 的允许编辑面；现有 spec 已 `collect_submodules('taskpaw_v3')`，除实际构建证据要求外不扩展打包配置。
5. 构建产物必须以明确 owned 临时根和虚构包测试同一固定 role 的 source/frozen 行为、ffprobe 子进程退出、原生 owner 和 stdout 预算，不能从现有普通 backend smoke 推断已通过。命令为 `uv run python scripts/build.py --skip-tauri`，随后专用 frozen intake pytest 明确传入刚构建 EXE；不安装/启动普通服务。

初始文件职责另增 `taskpaw_v3/workflow/intake_worker.py`（固定消息入口、只在受控工作根执行 copy/hash/probe）和 `taskpaw_v3/tests/test_workflow_intake_frozen.py`（刚构建 EXE 的专用验收）。根目录配置/worker 凭据仍由 T05 管理；本轮不改用户已安装运行环境。

driver 已读当前 T01/store、upstream `_WindowsProcess`、activity_verifier、backend_main 和 PyInstaller spec 来冻结上述接入边界；原生 API 的具体行为仍按 A02/A03/A09 的真实临时场景验证。CreateFileW、ReadDirectoryChangesW 及 ffprobe 文档已核对，不把文档语义当作实际 pipeline 已通过。

下一步：独立 critic 对本冻结初始合同做 adversarial debate；只有该轮无 open P1 / load-bearing unknown 后才发 tests-first 实施许可。T01 的最终 revision 未就绪时，先停在设计，不绕过依赖。
