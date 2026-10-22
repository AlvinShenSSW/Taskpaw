# F02-P1 设计：确认入库后的归档、回执与精确清理（阶段 1 单机闭环）

- Issue：Filmpaw#47（F02），父 EPIC Taskpaw#271；阶段 1 子集。
- 基线：Taskpaw `88fea70`（M01 完成），Filmpaw `acbb1b5`（main）。分支均为 `afk/claude-f02-47`。
- 设计原则 P0：个人家庭内网、单用户、提效。收拢设计，复用已有、已在真实 NAS 上跑过的机制，不新造框架。
- 版本：v3（辩论第 1 轮 D01–D13、第 2 轮 N01–N06 处置见附录）。

## 1. 要解决的问题

M01 之后，一部影片在 Taskpaw 中有了 final manifest（Miko 完整后处理产物，位于
`<work>/scrape/<attempt>/out/<film_dir>/`）。阶段 1 闭环还缺最后一段：

操作者一键「确认入库」→ 整包写入 NAS 演员目录（不覆盖）→ 全文件 hash 校验 → 持久回执 →
自动精确删除 INBOX 原片与 Taskpaw 工作副本，并回收该影片在工作盘上的全部私有目录。

T01 已经有这一段的状态模型（`approval_store.py`：`put_approval_context` / `approve` /
`begin_archive` / `accept_archive_receipt` / `begin_cleanup` / `record_cleanup_result`），但没有任何
运行时代码调用它；Filmpaw 已有完整的 `TransferService`（独占 staging、复制 + SHA-256、同句柄
no-replace 提交、崩溃核对、操作日志），但它是「移动」语义，绑定 UI 的 batch 目录设置，且外部进程
拿不到端口和会话凭据。

## 2. 冻结的契约（issue contract）

- **C1 一键确认**：`confirm(film_id, performer_id)` 一次调用完成「组装审批上下文 + 批准」；之后归档与
  清理全自动。前提：scrape 阶段 `succeeded`，归档阶段 `pending` 或 `failed`，且该影片没有未结的归档操作
  （D02；由 `put_approval_context` 在存储事务内强制，N05）。缺少 Filmpaw / 目标已存在同名目录 / 演员目录失效 / 来源成员身份或大小不符时，拒绝并给出可读
  原因，不产生任何 NAS 写入。
- **C2 归档只写新目录、不覆盖**：Filmpaw 在演员目录下新建 `<film_dir>`；同名已存在（含大小写不同）
  在复制前拒绝，提交使用 no-replace rename；从不删除或修改 NAS 既有内容。归档时 Filmpaw 核对演员目录
  路径与身份仍等于审批时的目标证明（D07）。
- **C3 整包精确一致**：Filmpaw 复制的**文件**集合必须恰好等于 final manifest 的条目（相对路径集合），
  并且目标侧每个文件的 SHA-256 与 manifest 一致，才允许提交。多/少/变化 → 失败，不提交。
- **C4 持久回执与按操作键核对**：Filmpaw 任务以 `request_id = archive operation_id` 持久化；响应丢失或
  超时后，Taskpaw 按操作键查询，不重复提交、不靠「路径存在」判定成功。只有 Filmpaw 报告 `completed`、
  回执目标路径 = 审批目标、逐文件 hash = manifest，Taskpaw 才 `accept_archive_receipt`。
- **C5 回执之后才清理**：`begin_cleanup` 要求已接受的回执（T01 已强制）。清理只删除审批绑定的成员：
  input 范围 = input manifest 的 INBOX 原片；workcopy 范围 = final manifest 条目（scrape 输出）。
  NAS 目标根从不进入清理列表（T01 `formal_target_cleanup_forbidden` 已强制）。
- **C6 精确删除**：每个成员以独占共享方式（只允许读，拒绝写/删/改名）打开，在**同一句柄**上核对文件身份、
  大小、SHA-256，然后在同一句柄上设置删除处置。被占用 → `blocked_writing`；身份不符 → `blocked_identity`；
  内容/大小不符 → `blocked_changed`；已不存在 → `already_absent`。永不按路径重新 unlink。逐成员精确性
  只在删除句柄上判定一次（D05/D12）。
- **C7 清理失败只重试清理**：未完成成员（`blocked_writing` / `failed` / `unknown`）在之后的调度轮次中
  重试，复用同一个清理操作，不重新复制、刮削或占用 GPU。`blocked_changed` / `blocked_identity` 永不自动
  删除，显示为「待清理（来源已变化）」，由操作者手动处理。
- **C8 归档失败有终态**：Filmpaw 报告 `failed` / `needs_review` / `stopped` → Taskpaw 以新命令
  `fail_archive` 记录失败，归档阶段 `failed`；**不自动重试**，操作者在原因消除后重新确认（新审批）。
  Filmpaw 未运行或暂时拒绝（409 且按操作键查询仍为 404）→ 操作保持未结，显示「等待 Filmpaw」，不失败、
  不重复提交（D06）。
- **C9 重启后可继续**：归档/清理适配器主体是持久的固定 principal（每次启动换新凭据），因此未完成的归档/
  清理操作在重启后由同一主体继续，T01「恰好一个 cleanup_adapter」检查成立（D11）。
- **C10 工作盘回收**：workcopy 清理验证完成后，删除该影片自己的 Taskpaw 私有目录（restore/asr/translate
  attempt 目录、return 包、scrape attempt）。一个 INBOX 包可含多部影片，共享同一个 intake snapshot 与包
  记录，因此**包级**回收只在该包全部影片都完成对应清理后进行：全部 `cleanup_workcopy` verified → 删除
  intake snapshot；全部 `cleanup_input` verified → 先自底向上 `rmdir` 已空的 INBOX 包目录，再退役包记录
  （D04/D10/N02/N03）。
- **C11 不碰真实数据**：测试只用临时目录模拟 INBOX、工作区与 NAS；任何在测试外启动 Filmpaw 服务的检查
  都设置临时 `FILMPAW_DB`。

## 3. Filmpaw 侧（Filmpaw-claude-f02）

### 3.1 工作流归档 = TransferService 的「复制模式」

不另写归档引擎。在 `TransferService` 上加一个工作流入口，复用 preflight / copy / verify / commit /
recover 全部现有代码：

- `create_workflow_task(operation_id, package_dir, performer_id, expected_target, files)`：
  - `package_dir` 为绝对本地路径（`.../out/<film_dir>`），其父目录作为 source 父目录（也作为 batch 身份），
    `<film_dir>` 为唯一 name；目标为 performer 的 `unc_path`。绑定构造复用 `_read_binding` 的演员/嵌套
    检查，但不读 UI 的 `last_local_dir` 设置（只有 `submit` / `reserve_cleanup` 读它，D13）。
  - `expected_target = {target_path, directory_identity}`：与当前 `unc_path` 及 `_directory(target)`
    不一致 → 409，不建任务（D07）。
  - 幂等：同一 `operation_id` 再次调用返回既有任务（参数不同 → 409）。
  - 任务带 `workflow = {"keep_source": True, "expected": {rel_path: [size, sha256]}}`。
- preflight：`_manifest(source)` 中 `kind == "file"` 的条目集合必须与 `expected` 的键集合完全一致，
  大小一致（C3，D13）。
- `_copy` 之后的 `_verify(staging)`：每个文件的 `sha256` 必须等于 `expected`（C3）。
- `_move_item`：`keep_source` 时提交后不进入 `cleaning`，不调用 `_cleanup`，条目直接 `completed`，消息
  「已归档；来源由 Taskpaw 精确清理」（D13）。
- `_reconcile`：`keep_source` 任务在 `committing` 状态恢复时，目标按 staging 身份核对完整即 `completed`，
  不检查来源是否残留。
- 回执：`keep_source` 任务完成后在公开载荷中附 `receipt = {task_id, target_path, directory_identity,
  files: [{path, size, sha256}]}`。`directory_identity` 为提交目录身份（rename 保留身份 = staging 身份），
  格式与 `_directory` 相同（`[st_dev, st_ino]`）。

### 3.2 路由（`routes_workflow.py`，前缀 `/api/workflow/archive`）

- `GET  /target?performer_id=&film_dir=` → `{performer_id, target_path, directory_identity, absent}`：
  演员目录 `_directory` 身份，`<film_dir>` 是否不存在（大小写不敏感，忽略 `.filmpaw-transfer-*`
  staging）。
- `POST /` `{operation_id, package_dir, performer_id, expected_target, files}` → 202 + 任务视图。
- `GET  /{operation_id}` → 任务视图（完成时含 `receipt`）；不存在 → 404。
- 会话守卫扩展到 `/api/workflow/archive*` 的所有方法（与 `/api/archive` 相同）。

### 3.3 端点发现

`main.py` 把实际端口传给 `create_app`；lifespan 在取得库会话锁之后，原子写
`<db 目录>/workflow-endpoint.json = {port, pid, token, db_path}`，退出时删除（只删 pid 为自己的文件）。
只在会话凭据已配置时写。`token` 即现有会话凭据：家庭单用户，文件位于用户自己的 `%APPDATA%\Filmpaw`
（P0 已接受）。Taskpaw 发出的请求不带 Origin，现有守卫允许。

不在 P1 范围：F01 索引接线（阶段 2/T05）、受管子进程隔离与每主机熔断（现有线程模型已在真实 NAS 使用，
阶段 2 视需要）、失败任务在 NAS 上残留的 `.filmpaw-transfer-*` 暂存目录自动回收（现状由 Filmpaw 报告
位置）、schema 变化（回执存于现有 `archive_tasks.payload`，无迁移）。

## 4. Taskpaw 侧（Taskpaw-claude-f02）

### 4.1 T01 补充（最小）

- `grant_component`：角色为 `archive_adapter` / `cleanup_adapter` 的既有 principal 再授权时，允许
  `boot_id` 变化（更新 `principals.boot_id`）。读 `principals.boot_id` 的地方中，这两个角色只受
  `approval_store._adapter`（非空）与 attempt 记录影响（D11 已核对）。
- 读 `component_generation(principal_id) -> int`（当前最大 generation，无则 0），运行时据此发 `max+1`；
  授权与保存之间崩溃无需持久化 generation（D11）。
- `fail_archive(auth, meta, ArchiveFailure{operation_id, expected_version, error_code, evidence})`：
  调用者须为操作 owner；状态 `admitted|running|unknown` → `failed`，操作 resolved，归档阶段与 attempt
  `failed`（C8）。
- 读 `film_workflow(film_id)`：当前审批（id、binding、过期时间）、归档操作（状态、版本）、回执 id、两个
  清理操作及逐成员状态（D09）。`list_jobs` 的阶段集合加入 `archive` / `cleanup_input` /
  `cleanup_workcopy`（对应 Literal 一并扩展）。
- `put_approval_context`：在事务内要求归档阶段 `pending|failed` 且无未结归档操作，否则
  `precondition_failed`（N05）。
- `begin_cleanup`（input 范围）：当本审批已有被接受的归档回执时，`_approved(allow_source_change=True)`
  且跳过 `_require_source_admissible`——归档后 INBOX 原片被删/被换会被 intake 扫描标记，若仍要求「来源可准入」
  则清理永远无法开始；逐成员精确性由删除句柄判定（N04）。
- 读 `package_films(package_id)`：同包各影片的 `cleanup_input` / `cleanup_workcopy` 阶段状态（N02/N03）。
- `retire_archived_package(package_id)`：要求同包全部影片 `cleanup_input` 已 succeeded，再调用
  `_retire_intake_package`（N03）。
- 清理重试无需新命令：`record_cleanup_result` 已允许对非终态成员再次记录（`deleted`/`already_absent`
  不可再改）。

### 4.2 文件身份统一

新 `file_identity.native(path, root_id) -> NativeIdentity`：win32 用 `intake_files.WindowsFiles`（与
input manifest 相同格式：`{volume:016x}` + 16 字节 hex），其他平台 `posix_inode`（`st_dev` / `st_ino`）。
scrape 的 final 条目、确认时的观察、删除时的核对共用它（D08）。`scrape_step._final` 为每个 final 条目填
`native_identity`；final manifest hash 随之变化，无测试依赖其为 None。

### 4.3 新模块

- `filmpaw_client.py`：读 `workflow-endpoint.json`（目录取 `WorkflowSettings.filmpaw_data_dir`，默认
  `%APPDATA%\Filmpaw`），校验 pid 存活；`urllib` 调用 §3.2 三个端点，带 `X-Filmpaw-Session`，有限超时。
  文件不存在/进程不在/连接失败 → `FilmpawUnavailable`。
- `file_identity.py`：`native`、`check(path, member) -> status`（只核对身份 + 大小，不读内容）、
  `delete_exact(path, member) -> status`（C6：Windows `CreateFileW(DELETE|GENERIC_READ, FILE_SHARE_READ)`
  + `GetFileInformationByHandleEx(FileIdInfo)` + 同句柄 hash + `SetFileInformationByHandle
  (FileDispositionInfo)`；非 Windows 为 inode 核对后 unlink，仅测试/开发）。
- `approval_flow.py`：`confirm(film_id, performer_id)`（C1）：
  1. 读 `film_workflow`；scrape 未成功、归档阶段不是 `pending`/`failed`、或有未结归档操作 → 拒绝。
  2. `filmpaw.target(performer_id, film_dir)`；`absent=False` → 拒绝「目标已存在同名目录」。
  3. 组装 `ApprovalBinding`：input/final 修订与 hash，`postprocess_digest`，
     `actor_ids = (uuid5(NS, "filmpaw-performer:<id>"),)`，`TargetIdentity`（`root_id` 同上 uuid5，
     `host_identity` = UNC 主机名（本地路径为 `local`），`export_identity` = 目标证明的 `target_path`，
     `native_identity` = 目标证明的目录身份，`relative_path = film_dir`，`expected_absent=True`；C4 的
     「回执目标路径 = 审批目标」即 `receipt.target_path == export_identity / film_dir`，N06），
     input_cleanup = input 条目，workcopy_cleanup = final 条目（D01）。
  4. 五个观察 EvidenceRef 由 archive 适配器主体产生：metadata / candidate 记录片号与所选演员；target 记录
     Filmpaw 目标证明；两个 cleanup 观察对每个成员做 `check`（身份 + 大小）——任一不符 → 该证据 `failed`
     → 上下文 `blocked`，拒绝确认（D12：确认时不读内容）。
  5. `put_approval_context` → `approve`，有效期 10 年（单用户；避免 Filmpaw 长期未运行导致回执到达后
     无法开始清理，D03）。
- `archive_step.py`：调度器每轮：
  - 阶段 `pending|failed` 且**当前审批**没有任何归档操作 → `begin_archive`（操作者）→ 提交；当前审批已有
    失败操作 → 不动，等操作者重新确认（新审批）（C8，D06，N01）。
  - 对未结操作：`filmpaw.get(operation_id)`：
    - 404 → 尚未提交，提交（同一操作键，幂等）；提交返回 409 → 视为暂时拒绝，等下一轮；
    - `queued` / `running` → 等下一轮；
    - `completed` 且回执满足 C4 → `accept_archive_receipt`；
    - `failed` / `needs_review` / `stopped` → `fail_archive`（error_code 带 Filmpaw 消息）；
    - Filmpaw 不可用 → 保持，原因「等待 Filmpaw 运行」。
- `cleanup_step.py`：归档成功后，对 `cleanup_input` / `cleanup_workcopy` 各自：
  - 无操作：`SourceObservation.items` = 审批绑定的成员原样；writer 证据 = 逐成员尝试独占共享打开（不存在视为
    无写者，被占用 → 本轮不准入，下一轮再试）；content / identity / membership 证据记录「绑定自 manifest，
    逐成员在删除句柄上核对」；shared_references 证据记录 `_shared_sources` 将由存储核对 → `begin_cleanup`
    （D05：准入不要求成员当下精确一致，精确性归 `delete_exact`）。
  - 有未结操作：对非终态成员 `delete_exact` → `record_cleanup_result`；全部 `deleted/already_absent`
    → 阶段成功。
  - workcopy verified → 回收该影片自己的私有目录（路径来自该影片各阶段 attempt 记录与 return 约定位置，
    `rmtree`，与 scrape 回收旧 attempt 的既有做法一致）；若 `package_films` 显示同包全部 workcopy 已完成，
    再删除 intake snapshot。
  - input verified → 若同包全部 input 已完成：先自底向上 `rmdir` 已空的 INBOX 包目录（非空即失败、保留），
    再 `retire_archived_package`。
- `local_dispatch` / `local_host`：把 archive / cleanup 步骤挂入现有轮询；`off_reasons` 显示等待原因。

### 4.4 不在 P1 范围

审批 UI（T06-P1，基于 `confirm` 与 Filmpaw 演员搜索 `GET /api/performers`）；多节点；受管子进程隔离；
source_invalidated 后的 workcopy-only 清理路径；`blocked_changed` 的操作者「放弃清理」按钮（T06）。

## 5. 测试计划（TDD）

Filmpaw（`tests/test_workflow_archive.py`，沿用 `test_transfer.py` 的假 NAS 目录与 `_checkpoint` 注入）：
- 成功：多卷 + NFO + SRT + 图片 + folder.jpg 整包到新目录，来源保留，回执逐文件 hash 与目录身份正确。
- 同名目标（含大小写）→ 复制前拒绝；提交前出现同名 → 不覆盖。
- 文件集合多一个/少一个/内容 hash 不符 → 不提交。
- 目标演员目录身份/路径与审批时不同 → 409，不建任务。
- 幂等：同一 operation_id 返回同一任务；参数不同 → 409；`GET` 未知 → 404。
- 崩溃于 `after_commit` → 恢复后 `completed`，回执可查，不重复复制。
- 端点文件：写入/退出删除；路由需要会话凭据。

Taskpaw：
- `file_identity`：删除成功；被占用 → `blocked_writing`；替换为同名新文件 → `blocked_identity`；内容改变
  → `blocked_changed`；不存在 → `already_absent`。
- T01：`fail_archive` 后阶段 `failed`，重新确认（新审批）后可再归档；同 principal 新 boot 再授权后恰好一个
  cleanup_adapter，且能继续记录旧操作；归档进行中再次确认被拒绝。
- 端到端（假 Filmpaw HTTP 服务：内存回执 + 真实目录复制）：确认 → 归档 → 回执 → 两个范围清理全部完成，
  INBOX 原片与包目录、scrape 输出及全部私有工作目录消失，NAS 目录完整，intake 包退役（不成为待处理）；
  注入「响应丢失」→ 按操作键核对，只复制一次；注入清理时文件被占用 → 下一轮重试成功，归档次数不变；
  INBOX 原片被替换 → 保留并显示待清理，其余成员照常删除；INBOX 原片已被手动删除 → `already_absent`，
  清理完成。
- 真实 Filmpaw 服务联调一次（临时 `FILMPAW_DB` + 临时目录假 NAS）。

## 6. 待实机（交给 T09 / 操作者）

真实 SMB NAS 上的身份语义、权限/空间不足、断网；真实 INBOX 原片删除。

## 附录：辩论第 1 轮处置

| ID | 级别 | 处置 |
|---|---|---|
| D01 | P1 | 采纳：root_id / actor_ids 用 uuid5；FinalManifest 无演员字段，actor = 所选演员 |
| D02 | P1 | 采纳：C1 增加阶段与未结操作前提 |
| D03 | P1 | 采纳：审批有效期 10 年 |
| D04 | P1 | 采纳：C10 回收全部私有工作目录 |
| D05 | P1 | 采纳：准入证据基于绑定成员 + 无写者，精确性归删除句柄 |
| D06 | P2 | 采纳：只对无归档操作的审批自动准入；409+404 = 等待；`stopped` → 失败；NAS 残留暂存目录不在 P1 |
| D07 | P2 | 采纳：POST 带 expected_target，Filmpaw 核对；统一 `_directory` 格式 |
| D08 | P2 | 采纳：`file_identity.native` 跨平台统一 |
| D09 | P2 | 采纳：`film_workflow` 读；`list_jobs` 阶段扩展 |
| D10 | P2 | 采纳：input 清理后退役包并 rmdir 空目录 |
| D11 | minor | 采纳：固定 principal 持久化于 LocalIdentity；`component_generation` 读 |
| D12 | minor | 采纳：确认/准入只核身份 + 大小，删除句柄上 hash 一次 |
| D13 | minor | 采纳：只比文件条目；keep_source 不进 cleaning；`last_local_dir` 绕过足够 |

## 附录：辩论第 2 轮处置

| ID | 级别 | 处置 |
|---|---|---|
| N01 | P1 | 采纳：归档准入条件为 `pending|failed` 且当前审批无归档操作 |
| N02 | P1 | 采纳：intake snapshot 按包回收，同包全部 workcopy 完成后才删 |
| N03 | P1 | 采纳：包退役在同包全部 input 完成后，且先 rmdir 再退役 |
| N04 | P2 | 采纳：input 清理在有回执时允许来源变化、跳过来源可准入检查 |
| N05 | minor | 采纳：D02 前提在 `put_approval_context` 事务内强制 |
| N06 | minor | 采纳：host/export 字段取值与 C4 路径比较明确 |

## 附录：辩论第 3 轮（无新 P1，设计冻结）

实现时按以下方式落实（不改契约）：

| ID | 级别 | 落实 |
|---|---|---|
| V3-1 | P2 | N04 是有意的 T01 契约变化：改写 `test_archive_fact_survives_dirty_source_and_independent_workcopy_can_clean`，期望回执后 input 清理可准入，并断言逐成员删除结果仍精确、`_shared_sources` 仍拒绝共享原片 |
| V3-2 | P2 | 存储内只强制「无未结归档操作」（足以关闭 D02）；`pending\|failed` 阶段检查留在 `approval_flow.confirm` |
| V3-3 | P2 | 包级回收只计当前输入修订仍链接到该包的影片（与 `_invalidate_intake_dependents` 同一连接）；等待时显示「包内仍有未入库影片」 |
