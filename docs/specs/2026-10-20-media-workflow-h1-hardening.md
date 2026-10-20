# H1：阶段 1 加固切片

- 父 EPIC：Taskpaw#271，阶段 1 收尾。基线 `b1f1212`（T06-P1）。分支 `afk/claude-h1-271`。
- 原则 P0：只修真实家用一次运行就会碰到的问题；每项都是对已有代码的最小修正，不加新机制。
- 来源：各子任务记录中 Deferred 的 P2（claude-phase.md）。

## 冻结契约

- **H1-1 全量测试不再固定变红**：`test_workflow_intake_service.py::test_stop_and_public_scan_admission_are_one_atomic_boundary`
  在机器负载下稳定失败（T02 C6-TEST01）。先判定是测试的时序假设错误还是 stop/扫描准入边界真有竞态：
  测试问题 → 改测试的同步方式（不靠固定 sleep/超时）；真竞态 → 在 IntakeService 修边界。全量测试在负载下
  连续 5 次通过。
- **H1-2 Windows 文件占用视为暂时性**：本地阶段与 return/scrape 在 rmtree / 复制 / 链接时遇到
  PermissionError 或共享冲突（WinError 5 / 32 / 33，杀毒或索引临时占用）时，按 transient 失败分类（走已有自动
  重试），不再 needs_attention。其他权限错误保持原分类。
- **H1-3 日文非 SRT 字幕不冲突**：输入里有 `.ass` / `.ssa` / `.vtt` 日文字幕、同时 ASR 又生成日文 SRT 时，
  scrape 交给最终包的两个日文字幕使用不同文件名（保留原扩展名，例如 `<stem>.ja.ass` 与 `<stem>.ja.srt`），
  不再因 duplicate_item_id 失败；也不把原格式错标为 `.srt`。
- **H1-4 回收意图不丢**：finalizer 启动（第一次 run_once）时，对清理阶段已成功、但工作目录或包仍未回收的影片
  补写回收标记（来自 film_board / 已成功的 cleanup 阶段），之后由现有的标记重试完成（修 F02 r3 P2 崩溃窗口）。
- **H1-5 return 步骤响应停止**：return 步骤在复制/哈希大文件时检查取消事件（ReturnContext 已有 cancel），停止时
  不留下被当作成功的半成品（与 restore/asr 的取消语义一致），满足宪法的干净退出规则。

## 测试

每项先写会失败的测试（RED），修复后通过（GREEN）；全量 Python 测试 + UI 测试通过。

## 不在范围

其余 Deferred 项（见各子任务记录）；待操作者决定的两项：M01 CX-M2（folder_cnword）、F02 r3 并发 ADS 竞态。

## v2（辩论第 1 轮 D01–D09，优先于上文）

- **H1-1（D01）**：边界本身正确（scan_once 持 `_state_lock` 时已准入；stop 的超时在阻塞前计算）。只改测试：
  去掉 `stop_done.wait(0.2)`/`stop_started`，stopper 启动后即放行；walk 中等待 `stop_done` 并记录
  `kwargs["cancelled"]()`；断言 `outcomes == [False]`、`seen == [True]`、随后 `stop(timeout=2)` 成功且
  `not scan_once()`。负载下连续通过。
- **H1-2（D02/D03/D04）**：Windows 共享冲突在 open/copyfile 上是 `PermissionError(errno=13, winerror=None)`。
  executors.py 新增 `classify_exception(exc)`：`PermissionError` → transient，其余 → needs_attention；
  用于 local_dispatch 两处兜底（执行器异常 / 阶段异常），以及 exec_common.workcopy_ref 与 exec_asr 中把 OSError
  一律变成 attention 的两处（PermissionError 改为 StageFailure transient）。重试无退避：总时长约 10–15 秒外加
  已有的 7.5 秒改名等待，家用可接受，记录在案。
- **H1-3（D05/D06）**：`_place_japanese` 用输入字幕自己的扩展名，casefold 的已用名集合，冲突时
  `<stem>.ja.<n><ext>`；生成的 SRT 与重用/保留的输入字幕都不再同名。输入的 zh/其他 `.ass` 不进最终包 → Deferred。
- **H1-4（D07/D08）**：回收标记改为在 `record_cleanup_result` **之前**写入（`_reclaim` 在阶段未成功时本就返回
  False），无需启动回填扫描。
- **H1-5（D09）**：return 的 `_build` / `_complete` 每个成员前、`_Busy` 等待循环中检查 `context.cancel` →
  `ExecOutcome.failed("cancelled")`（免费重试类）；`stage_results.sha256_file` / `check_file` 增加可选 `cancel`，
  每 1 MiB 检查一次。回执被接受之后不再检查取消。scrape 的哈希不在范围。
