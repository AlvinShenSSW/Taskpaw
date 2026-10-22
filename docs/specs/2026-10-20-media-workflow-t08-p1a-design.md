# T08-P1a 设计：本机调度的故障恢复加固（v2）

日期：2026-10-20。Issue：[#279](https://github.com/AlvinShenSSW/Taskpaw/issues/279) 阶段 1 子集（"各阶段独立有限重试、故障恢复"）。
基线：`afk/claude-t03-274` @ `435a6d5`（T03-P1，内部评审 + Codex 外门已通过）。
设计原则 P0：家庭内网、单用户、提效；只做最小修复。

## 1. 目标

T03-P1 评审中延后的 P2 指出：几种少见的时序或环境故障会让本机调度"卡住直到重启"，有一种甚至永久卡住。
本 issue 只修这些已被证实的路径，不新增机制。

## 2. 冻结契约（T08-P1a）

| ID | 要求 | 来源 |
|---|---|---|
| R1 | 阶段结束时，先停止并 join 续租线程，再读取 assignment 版本并核销；续租与核销不会互相造成 `cas_conflict` | CX2 / IR02 |
| R2 | 领取成功后的准备步骤（建 Job、建工作目录）失败时，按 `needs_attention` 失败正常核销，释放 owner 与 GPU，界面可见原因；不会让 assignment 一直 running | CX4 / IR02 |
| R3 | 应自动重试的失败（`transient` 未超次数、`cancelled`、`exec_interrupted`）若在"核销已提交、重试未提交"之间进程退出，下次启动时补做重试；不会永久停在 failed | CX3 |
| R4 | `local-identity.json` 与 attempt 文件用已有的 `core/state.atomic_json`（fsync + 原子替换）写入；读取时 attempt 文件损坏视为"未记录子进程"（旧 Job 已随旧进程关闭）并记日志，不阻断启动 | IR03 |
| R5 | `LocalWorkflowHost.stop()` 在启动线程、调度线程或收件服务未确认停止时（含收件停止抛异常），不关闭 store，状态记为 `stop_unconfirmed` | CX5 |
| R6 | 测试：每条要求一条回归测试；修复前失败、修复后通过（能表达的情况下） | — |

不在本 issue：跨机 unknown 核对、UNC 熔断、升级迁移中的恢复（阶段 2 的 T08 余项）。

## 3. 做法

- **R1**：在 `_execute` 包住 `executor.run` 的 `finally` 中执行 `stop_renew.set(); renewer.join()`，让"停止未确认"这条提前返回的路径也被覆盖。续租线程用 `stop.wait()` 睡眠，join 只会等正在进行的那一次续租结束。join 期间调度线程不持有任何锁，不会死锁（D01）。
- **R2**：在 try 之前先构造 `_Running`，其中 `processes: OwnedJob | None = None`；`OwnedJob()` 与 `workdir.mkdir` 放进 try。
  - 异常转成 `ExecOutcome.failed("needs_attention", ...)`，停机期间发生则转成 `cancelled`，之后走原有的核销流程。
  - `processes` 为 None 时说明什么也没启动，视为已收束，跳过 terminate/settle（D02）。
  - owner 已在 try 之前创建并续租登记，核销照常使用 `running.owner`；GPU 在 `_resolve` 的 finally 中释放。
- **R3**：新增只读查询 `list_compute_jobs(auth, status)`，并把 `list_pending_compute_jobs` 改成它的特例，不保留两份重复逻辑。调度线程启动时执行一次 `reconcile_retries()`：
  - 对象是当前 input revision 中 status=failed 的计算阶段；
  - 取它最近一次 attempt 的证据 kind，按 `_after_failure` 的同一规则判断是否应重试；
  - 应重试的就 `retry_stage`。
  
  判断规则只有一处实现，`_after_failure` 与启动补做共用，且一律依据证据 kind 判断（D03）；匹配方式为 `any(e.kind == ...)`，不取 `evidence[0]`：
  - `exec_interrupted`：重试，不计次数；
  - `exec_failed:cancelled`：重试，不计次数；
  - `exec_failed:transient`：已失败的 transient attempt 数 ≤ `MAX_AUTO_RETRIES` 时重试；
  - 其他（`permission`、`needs_attention`）：不重试。

  "最近一次 attempt" 取 `list_attempts` 按 attempt_number 排序后的最后一个。`reconcile_retries()` 在 `_loop` 的 while 之前运行，每个阶段单独捕获 `WorkflowError`，并整体捕获 `Exception` 只记日志，确保调度线程不会因此退出（D04）。`list_compute_jobs` 的 status 用绑定参数，依赖过滤保持不变。
- **R4**：`write_json_atomic` 删除，改用 `core/state.atomic_json`。`read_attempt` 捕获 `ValueError`（含 `JSONDecodeError`、`UnicodeDecodeError`）、`KeyError`、`TypeError`，返回 `None` 并记一条 warning。之后 `operation_id` 改从 store 读取，原有回退路径照常工作（D05）。
- **R5**：`LocalDispatcher.stop()` 改为返回线程是否已退出。`LocalWorkflowHost.stop()` 在以下任一情况下保留 runtime、不关闭 store，状态记为 `stop_unconfirmed` 并记 error 日志（D06）：
  - 启动线程仍存活；
  - `dispatcher.stop()` 返回 False；
  - `intake.stop()` 返回 False 或抛出异常。
  
  理由：关闭 store 要等待 store 锁，而收件停止线程可能正持有或等待这把锁，关闭会让 launcher 的停机卡住。进程退出时，OS 会释放文件锁并关闭 kill-on-close Job。

## 4. 测试

| 测试 | 覆盖 |
|---|---|
| 续租线程在核销前一刻完成一次续租（注入），核销仍成功 | R1 |
| 工作目录不可创建，或 `OwnedJob()` 本身抛异常（分别注入）：阶段 failed，原因可读，没有未核销 assignment，GPU 已释放 | R2 |
| 核销后、重试前"崩溃"（注入在 retry_stage 前抛 BaseException），分别覆盖 transient、cancelled 和重启核销的 interrupted：重启后阶段回到 pending 并完成；超次数的 transient 和 permission 不补做 | R3 |
| attempt 文件被截断成半个 JSON：重启核销成功，boot reconciled | R4 |
| 收件停止返回 False、收件停止抛异常、调度线程未退出（分别注入）：host 状态 `stop_unconfirmed`，store 未关闭 | R5 |

## 附录：辩论第 1 轮处置

| ID | 级别 | 处置 |
|---|---|---|
| D01 join 是否死锁 | 无问题 | 改为在 finally 中 join |
| D02 `processes` 可能未绑定 | P1（作者上调：按原设计实现会在目标路径上 NameError） | 修复：§3 R2 |
| D03 重试规则以证据 kind 为准 | minor | 写入 §3 R3 |
| D04 reconcile 异常不能杀死线程 | minor | 写入 §3 R3 |
| D05 读取时的异常范围 | minor | 写入 §3 R4 |
| D06 停机判定条件补全 | minor | 写入 §3 R5 |
| D07 补充测试 | minor | 写入 §4 |

