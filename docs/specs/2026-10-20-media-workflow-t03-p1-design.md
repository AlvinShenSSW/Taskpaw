# T03-P1 设计：主控本机单节点领取与执行归属（v3）

日期：2026-10-20。Issue：[#274](https://github.com/AlvinShenSSW/Taskpaw/issues/274)（阶段 1 子集）。
基线：`afk/media-t02-273` @ `8c6c1d9`（T01 + T02，已评审，冻结不改语义；本设计只在 T02 增加一条运维命令和一条只读查询）。
设计原则 P0：操作者个人在家庭内网使用、单用户、目标是提效。只保留防误操作的数据安全，不做多用户/对抗性设计。

v2 修正辩论第 1 轮的 D01–D12；v3 修正第 2 轮的 D13–D17（处置见文末附录）。

## 1. 目标

让主控这台机器（RTX 5060 8 GB）在启用工作流后，把 T02 收件产生的影片按阶段**自动领取并执行**，
同一时刻只让一个工作流 GPU 阶段占卡，并与已有的手动 Jasna/字幕任务共用同一个 GPU 仲裁；
重启（包括关闭应用造成的强制结束）后能确定上一次的执行已经结束，再继续。
真实 LTX/ASR/翻译执行器由 T04 提供；本 issue 提供执行框架和测试用合成执行器。

不在本阶段：多机领取、远端 Agent、设备增删改/停领/退役、配对 UI、第六节点、同卡翻译 HTTP 服务的占用探测
（→ 阶段 2 的 T03 余项）。

## 2. 冻结的 issue 契约（T03-P1）

| ID | 要求 | 对应 #274 验收 |
|---|---|---|
| C1 | 本机能力由 GPU 显存决定：显存 < 15360 MiB 只批准 ≤1920×1080 的 profile；≥ 15360 MiB 批准 ≤3840×2160。8 GB 机器永不领取 4K；这类影片保持 pending，并给出可读原因「没有可接 4K 的设备」 | 1 |
| C2 | 领取在 GPU 子进程启动前完成；只有当前 attempt 的结果能推进阶段；重复/过期结果被拒（复用 T01 CAS，不新增机制） | 2 |
| C3 | 工作流子进程以挂起方式创建、放进 KILL_ON_JOB_CLOSE 的 Job 后才恢复，整棵进程树都在 Job 内。Agent 进程退出（正常或强制）即 Job 关闭、树内进程结束。重启时把上一 boot 遗留的执行和收件操作核销后继续；同一阶段不会有两个执行。因重启核销的 attempt 不消耗自动重试次数 | 3 |
| C4 | 调度在独立线程运行，不阻塞 Agent 的 HTTP/心跳/就绪；工作流启动失败不影响 Agent 其余功能。`stop()` 终止在途子进程、等待 Job 收束后再核销 GPU owner，结果如实记录 | 4 |
| C5 | 工作流 GPU 阶段与手动 Jasna/字幕任务使用同一个进程内 `gpu_lease`，任何时刻最多一个 GPU 持有者；工作流等待时不会长时间挡住手动任务 | 6（仅本机手动任务部分） |
| C6 | 自动重试：`transient` 失败最多自动重试 2 次；`permission`、`needs_attention` 不自动重试，阶段停在 failed 并显示原因 | 3、T08 分类子集 |
| C7 | 默认关闭：只有配置 `workflow.enabled=true` 且指定收件根目录时才启动；升级不会自动启用 | EPIC 约束 |
| C8 | 系统时钟回拨（含自动校时）不会让工作流永久不可用 | 稳定性（D10） |
| C9 | 测试：假时钟、真实 SQLite、真实受控子进程（Job 收束）、重启核销（执行与收件）、GPU 仲裁竞争；五机模拟与实机属于阶段 2 / T09 | 5 |

待实机（交 T09）：真实 5060 上 LTX 与手动 Jasna 交替占卡；关闭应用后 Job 内进程全部结束、显存释放。

## 3. 组件

新增模块都在 `taskpaw_v3/workflow/`。对 T01/T02 的改动只有两处，均为新增：
- `read_store.py`：只读查询 `list_pending_compute_jobs`（§3.4）。
- `intake_store.py`：运维命令 `settle_abandoned_intake`（§3.6）。

### 3.1 `local_runtime.py`：本机工作流运行时（自举）

T01 的领取前置条件要求完整的 worker 身份。主控本机不需要人工配对，`LocalRuntime` 在启动时以幂等方式自动完成：

1. **身份文件先落盘（D11）**：数据目录默认 `%APPDATA%\Taskpaw\workflow\`，测试时注入临时目录。
   - 首次启动先生成 authority id、operator/本机 worker/组件的凭据 id 与密钥、worker/device id，以及每条初始化命令的固定幂等键。
   - 这些内容原子写入 `local-identity.json`（写临时文件后 `os.replace`），然后才执行任何 store 命令。
   - 某一步中途崩溃，重启时用同一身份和同一幂等键重放。
   - P0：家庭单用户，密钥以用户目录下的普通文件保存，不加密。
2. **打开 store**：
   - 首次用 `WorkflowStore.create(...)`：凭据有效期 10 年（T01 不设上限，已验证），`lease_seconds=86400`（D04，休眠不会让 attempt 过期）；`TlsBinding` 用固定占位指纹，因为 P1 不启动 HTTPS worker API，本机调度直接调用 store。
   - 之后用 `WorkflowStore.open`。
   - 两种情况都传入 §3.7 的单调时钟（C8）。
3. **确保本机 worker**，每一步先读当前状态，已完成就跳过：
   1. `register_worker`（地址 127.0.0.1）
   2. `issue_pairing`
   3. `complete_pairing`
   4. `record_capabilities`：
      - probe 来自 §3.8 的 GPU 查询（型号、UUID、显存）和 MachineGuid；
      - 批准的 profile 由已注册执行器按 C1 生成；
      - 没有执行器、查不到 GPU 或模型未就绪时，只记录 probe、不批准（D08），worker 停在 pending_validation，并给出可读原因。
   5. `update_worker(participation=True, admission=enabled)`
   - 能力变化（换卡、执行器版本变化）时写新的 capability revision；已领取的 attempt 不受影响（T01 已保证）。
4. **每次启动的 boot 处理**：
   1. 生成新 `boot_id` 并 `record_boot`。T01 会把旧 boot 遗留的 assignment、owner、阶段、attempt 自动标为 unknown，所以不需要单独调用 `mark_unknown`（D09）。
   2. 执行 §3.5 的执行核销。
   3. 再次 `record_boot`（同一 boot），这时没有未核销的 assignment，boot 变为 reconciled。
   4. 为本次 boot 授予一个新的 intake_adapter 组件主体：component_id 不变，principal 和 boot_id 是新的（D01）。
   5. 执行 §3.6 的收件核销。

### 3.2 `executors.py`：执行器接口

```python
class StageExecutor(Protocol):
    stage: Literal["restore", "asr", "translate"]
    uses_gpu: bool                                   # 只决定是否取 gpu_lease
    def tools(self) -> ToolVersions: ...              # 进入 profile 与 ClaimRequest
    def max_resolution(self, vram_mib: int) -> tuple[int, int]: ...   # C1
    def run(self, job: JobContext) -> ExecOutcome: ...
```

- `JobContext` 提供：输入 manifest、本 attempt 的私有工作目录、`launch(argv, cwd)`（§3.3）、取消事件、进度回调。
- `ExecOutcome` 有两种：`succeeded(evidence)`；`failed(error_class, message)`，其中 `error_class ∈ {transient, permission, needs_attention, cancelled}`（C6）。
- 按 T01 规则（D12），所有计算阶段领取时都带本机 GPU 绑定的 `resource_id`，包括不用 GPU 的翻译阶段；T01 owner 照常登记，`uses_gpu=False` 只是不取 `gpu_lease`。
- 失败的 `finish_attempt` 同样带 worker 主体生成的 passed 证据，以及 stopped 状态的 owner。
- P1 只带 `SyntheticExecutor`（测试用，运行一个会再起子进程的 Python 程序）；T04 注册真实执行器。

### 3.3 `owned_process.py`：受控子进程（D02）

复用 `hub/server/upstream_worker._WindowsAPI`（T02 的 `intake_owner`/`intake_probe_native` 已在用）：
- `create_job` + `configure_job`（KILL_ON_JOB_CLOSE）；
- `create_process(..., CREATE_SUSPENDED)` → `assign` → `resume`。这样进程在执行任何代码之前已在 Job 内，它的所有后代也都在 Job 内。
- `owned_process.py` 内子类化 `_WindowsAPI`，覆盖 `create_process` 增加 `cwd` 参数（做法同 `intake_owner._CreationAPI`），不改 `upstream_worker.py`（D15）。
- 输出捕获：调用方给出可继承的 stdout/stderr 管道，三个 std 句柄都放进 `handle_list`；由读线程持续读走输出并逐行回调进度，避免管道写满卡住子进程（沿用 `intake_probe_native.SuspendedProbe` 的管道与读线程写法）。
- 进程身份 = `identity(handle, job)` 返回的 `pid:创建时间`，写入 `LocalOwnerEvidence.process_identity`。
- `terminate()` 调 `terminate_job`；`wait(deadline)` 等到 `active(job) == 0` 后关闭句柄，返回是否已收束。

不做 Job 出生计数、conhost 核算之类的原生账目：挂起创建 + KILL_ON_JOB_CLOSE + 活动进程数为 0 已足以证明收束。
非 Windows（开发/CI）退化为进程组 + kill，仅用于跨平台单元测试；原生收束测试只在 Windows 运行。

### 3.4 `local_dispatch.py`：调度线程

单线程循环（默认每 5 s；收件产生新影片时立即唤醒）：

1. 调用 `list_pending_compute_jobs()`（D06）。返回当前 input revision 中 status=pending、依赖阶段为 succeeded 或 skipped 的 restore/asr/translate job，按影片创建时间排序。其余可领取条件由 `claim` 自己检查。
2. 对每个 job：
   - 找对应执行器；
   - 用 manifest 中的媒体探测分辨率对照本机 profile。超出就记下原因（C1）并跳过；原因由 `status()` 对外提供，给 T06 显示。
3. 需要 GPU 的 job 调用 `gpu_lease.try_acquire(RUN, poll=5, label="影片工作流：<片名>")`（D07）：
   - `RUN = ("workflow", runtime_instance_id)` 在整个运行期间固定，等待期间每轮都用同一个 RunId 重试；
   - 本轮没有候选 job 时调用 `withdraw(RUN)`，不让空等的 waiter 拿到预留窗口。
4. 执行：
   1. `claim`；失败时立即 `release(RUN)`。
   2. 建工作目录，调用执行器 `run()`。
   3. 子进程启动后 `renew`，写入 `LocalOwnerEvidence(state=running)`。
   4. 续租线程每 1 小时续一次（`lease_seconds=86400`）。
5. 结束，顺序保证「先确认进程树结束，再放卡」（D17）：
   1. Job 收束；
   2. 以 operator 身份调用一次 `resolve_assignment(outcome, local_owner=stopped, evidence)`。T01 在阶段未终结时会同时写入阶段和 attempt 的结果，并核销 assignment 和 GPU owner（`film_store.py:976-987`），因此不另调 `finish_attempt`。租约是否过期不影响这一步，D04 的特殊分支随之取消；
   3. `gpu_lease.release(RUN)`。
6. 失败处理：`transient` 且该阶段由真实执行失败的 attempt 少于 3 次 → `retry_stage`；否则停在 failed，原因可读（C6）。

`stop()` 的步骤：
1. 设置取消事件；
2. 执行器终止 Job；
3. 等待收束，上限 30 s。超时就保留 owner、不放卡，界面显示「停止未确认」；Agent 退出时 Job 关闭，下次启动按 §3.5 核销。
4. 核销，结果为 `failed(cancelled)`，不消耗重试次数；
5. 线程退出。

若 store 处于 `authority_unavailable` 等不可恢复状态，调度线程停止循环，并在 `status()` 中给出原因，不忙等。

### 3.5 执行核销（重启后）

**收束依据：**
- 上一 boot 的子进程都在上一个 Agent 进程持有的 KILL_ON_JOB_CLOSE Job 里。新 boot 能打开 store，说明 `WorkflowStore` 的文件锁已被释放（`core/state.py` 的 OS FileLease，锁文件句柄与 Job 句柄都不可继承），也就是旧 Agent 进程已退出。旧 Job 因此已关闭，树内进程已被系统结束或正在被结束。
- **逐个核对（D13）**：对每个待核销 owner，按记录的 `process_identity`（pid + 创建时间）打开进程并比对创建时间。
  - 进程不存在，或 pid 已被其他进程复用（创建时间不同），才视为已结束；
  - 仍是同一进程（例如停止超时、卡在驱动里）时，等待最多 30 s。仍未退出就保留 owner 为 unknown，阶段保持阻断，界面显示「上次的任务进程仍在运行」；本机不再开新的 GPU 任务，直到该进程结束后下一轮调度再核对。

**核销步骤**，对本机 worker 每个未核销的 assignment：
- 读取存储的 owner 身份（`get_worker().assignments`），以 `resolve_assignment(local_owner=stopped)` 核销。
- outcome 规则：阶段业务结果已成功提交时为 succeeded，否则为 failed，原因写「上一次运行被中断」。
- 中断造成的 failed 直接 `retry_stage`，不计入 C6 的重试次数（D05）。

### 3.6 收件核销（D01）

T02 在打开 store 时会把未完成的收件操作标为 unknown，而 `begin_intake_snapshot` 在存在任何未核销操作时拒绝所有新快照。T02 契约把核销留给了 T08。在 Windows 上关闭应用会强制结束后台，所以阶段 1 必须补上这一步，否则一次关闭就会让收件永久卡死。

新增运维命令 `settle_abandoned_intake(operator)`：
- **适用条件**：收件操作的 `owner_boot_id` 不是当前 intake_adapter 的 boot，且 `unresolved=1`。
- **执行内容**：
  - 把操作标为 `cancelled`、`unresolved=0`、`publication_allowed=0`；
  - 把对应的包置回 `waiting_stability`，清空 `active_operation_id` 和 `stable_since`，下一轮扫描重新判断稳定并重新快照；
  - 用旧操作 id 建的私有工作副本目录尽力删除，删不掉只记日志，因为新快照使用新目录。
- **收束依据**：收件 helper 的 Job 同样是 KILL_ON_JOB_CLOSE（`upstream_worker.py:515`），持有者是已退出的旧 Agent 进程，依据与 §3.5 相同。
- **边界**：不改变 T02 的其它语义。同一 boot 内的 unknown（helper 在本进程内失控）仍按 T02 规则保持阻断。

### 3.7 单调时钟（C8，D10）

T01 的 `_observe_time` 只要发现 `now < max_observed_at` 就把 authority 永久置为 `recovery_required`，且没有重置命令。系统自动校时向回调哪怕一点点也会触发。

`LocalRuntime` 给 store 注入 `MonotonicWallClock`：
- 返回 `max(真实 UTC 时间, 上次返回值)`；
- 初始下限取 `workflow.db` 中的 `authority.max_observed_at`，在打开 store 前用只读连接读取。

因此 store 永远看不到时间倒退。代价是回拨期间时间戳暂时停在高水位，对家用场景可以接受。T01 不需要修改。

同一个时钟实例也注入 `IntakeService`（D14）。T02 收件 owner 内部仍有少量直接取系统时间的位置（`intake_owner.py:176,652`、`intake_build.py:69`），回拨窗口内正在进行的快照可能因 `observation_time_invalid` 失败并停在本 boot 的 unknown；这是已知局限：回拨期间收件暂停，最坏情况重启后由 §3.6 核销恢复，不会永久卡死。

### 3.8 GPU 查询（D03）

新增 `nvidia-smi --query-gpu=name,uuid,memory.total --format=csv,noheader,nounits`（带超时、隐藏窗口，shell=False），取第 0 张卡：
- `gpu_identity` = UUID；
- `device_identity` = 注册表 `MachineGuid`；
- `gpu_model` = name；
- `vram_mib` = memory.total。

这几项在重启之间保持稳定，所以物理绑定也保持不变。查询失败时不批准能力（D08）。

### 3.9 Agent 进程挂载

`agent/server/launcher.py::run_agent` 在 supervisor 启动、就绪信号发出**之后**（D08），若 `config.workflow.enabled` 为真，就在后台线程里依次执行：
1. 创建 `LocalRuntime`；
2. 启动 T02 `IntakeService`，收件根目录取自配置，以本 boot 的 intake_adapter 身份运行，注入 §3.7 的时钟；
3. 启动 `LocalDispatcher`。

任一步失败只记录原因并在状态中显示，不影响 Agent 的其它功能（C4）。`_stop()` 中按相反顺序停止。
工作流与 Jasna 监控在同一进程，因此共享 `gpu_lease` 单例（C5）。

`AgentConfig` 增加可选的 `workflow: {enabled: false, inbox_root: null, data_dir: null}`，缺省即关闭（C7）。

## 4. 不做的事（P0 收拢）

- 不启用 HTTPS worker API，不做配对 UI，不做凭据轮换（本机自举，有效期十年）。
- 不做 nvidia-smi 外部占用探测或显存回落等待（D16）；重启交接靠 §3.5 的进程核对，旧进程拖慢退出导致的显存不足按普通 transient 失败重试。
- 不做 Job 出生计数、conhost 核算等原生账目。

## 5. 测试计划

| 测试 | 覆盖 |
|---|---|
| runtime 首启和再启幂等：两次启动只有一个 worker、一份能力；换显存产生新 capability revision | §3.1、C1 |
| 首启在任意一步之后"崩溃"（丢弃对象），重启后重放到完成，身份不变 | §3.1 D11 |
| 无执行器或 GPU 查询失败时：worker 停在 pending_validation，原因可读，Agent 不受影响 | §3.1 D08、C4 |
| 8 GB 本机遇到 4K 影片：不领取，原因可读；注入 16 GB 时 1080p 和 4K 都可领 | C1 |
| 合成执行器成功路径：pending → running → succeeded，GPU owner 核销，`gpu_lease` 释放 | C2、C4 |
| 手动任务持有 `gpu_lease` 时调度不领取；释放后领取；调度空闲时已 withdraw，不占预留窗口 | C5 |
| 执行中"崩溃"（丢弃 runtime，不调 stop；重开 store、新 boot）：旧 attempt 被核销，新 attempt 执行，不并行，重试次数不变 | C3 |
| 重启时记录的进程仍存活（测试中保持一个同身份的进程）：owner 保持 unknown、不开新 GPU 任务；进程结束后下一轮核销并继续；pid 复用（创建时间不同）视为已结束 | C3、D13 |
| 受控子进程在指定 cwd 运行，持续输出大量行时不阻塞，进度逐行回调 | §3.3、D15 |
| 收件快照中"崩溃"后重启：旧操作 cancelled，包重新快照并产出影片，之后的新包不受阻 | C3、D01 |
| Job 收束（Windows 原生）：子进程再起孙进程，关闭 Job 后两者都结束；`active == 0` | C3、D02 |
| 重试分类：transient 自动重试两次后停住；permission 不重试；cancelled 不计次数 | C6 |
| `stop()` 在执行中：子进程结束，owner 核销，结果为 failed(cancelled) | C4 |
| 租约过期后（假时钟推进）执行结束：以 resolve 核销，结果正确，GPU 可再领取 | D04、D17 |
| 时钟回拨 1 秒和 1 小时：工作流继续可领取，authority 仍为 active | C8 |
| 配置缺省关闭：`run_agent` 未配置时不创建 workflow.db | C7 |

测试数据目录和 pytest `--basetemp` 一律放 `%TEMP%`（用正斜杠路径），不写用户目录。

## 附录：辩论第 1 轮处置

| ID | 级别 | 处置 |
|---|---|---|
| D01 收件操作在强制结束后永久阻断 | P1 | 修复：§3.6、每 boot 新 intake 主体 |
| D02 Job 辅助函数在进程启动后才放入 Job | P1 | 修复：§3.3 挂起创建 |
| D03 `query_gpus` 无型号/UUID | P1 | 修复：§3.8 |
| D10 时钟回拨永久停用 | P1（由作者上调） | 修复：§3.7 |
| D04 休眠导致租约过期 | P2 | 修复：`lease_seconds=86400` + stale 处理 |
| D05 重启核销消耗重试次数、旧显存未释放 | P2 | 修复：不计次数；显存问题由 D13 进程核对取代 |
| D06 待领取查询的过滤条件错误 | P2 | 修复：§3.4 第 1 步 |
| D07 RunId/withdraw/claim 失败后未 release | minor | 修复：§3.4 第 3、4 步 |
| D08 自举严格失败会拖住启动 | minor | 修复：§3.1 第 3 步、§3.9 |
| D09 多余的 mark_unknown | minor | 删除 |
| D11 身份文件晚于命令写入 | minor | 修复：§3.1 第 1 步 |
| D12 resource_id 与失败证据规则 | minor | 写入 §3.2 |
| D13 停止超时后重启，仍存活的旧进程被当成已结束 | P2 | 修复：§3.5 按 pid+创建时间逐个核对 |
| D14 收件仍用系统时钟 | minor | 修复：注入同一时钟；剩余位置记为已知局限 |
| D15 `create_process` 无 cwd，输出管道未说明 | minor | 修复：§3.3 子类化 + 读线程 |
| D16 显存回落等待属过度设计 | minor | 删除 |
| D17 finish 后 resolve 写成可选 | minor | 修复：只调一次 operator `resolve_assignment` |
