# T04-P1 设计：本机逐片 LTX、字幕与回传（v4）

日期：2026-10-20。Issue：[#275](https://github.com/AlvinShenSSW/Taskpaw/issues/275)（阶段 1 子集）。
基线：`afk/claude-t08a-279` @ `ffd1ae4`（T03-P1 本机调度 + T08-P1a 恢复加固）。
设计原则 P0：家庭内网、单用户、提效。只做在主控本机（RTX 5060 8 GB，仅 1080p）跑通真实处理所需的最小部分。

v2 修正辩论第 1 轮的 D01–D10，v3 修正第 2 轮的 D11–D16，v4 修正第 3 轮的 D17–D21（处置见文末附录）。

## 1. 目标

为 T03-P1 的调度框架提供三个真实执行器，再加上本机"回传"步骤：
- **restore**：用 Jasna LTX 逐卷修复主视频；
- **asr**：判断能否复用已有字幕，不能复用就用 WhisperJAV 识别日语；
- **translate**：调用现有 Translator 翻成中文；
- **return**：在本机把成品整理成经 hash 核验的回传包，写好交给 M01 的 `return-manifest.json`，并登记 T01 回执。

不在本阶段：
- 远端回传、同卡翻译服务占卡准入（验收 6）；
- 多通道并行调度。T01 规定计算阶段要绑定同一块 GPU 资源、一次只能有一个 owner，所以并行需要 T01 先修订，留到阶段 2。

真实推理质量待实机（验收 5，交 T09）。

## 2. 冻结的 issue 契约（T04-P1）

| ID | 要求 | 对应 #275 验收 |
|---|---|---|
| E1 | restore 按卷序处理每个主视频，输出 `<原名>-破解.mp4`。成功同时要求：退出码 0、输出存在、ffprobe 读到视频流（记录宽、高、时长、视频流数、音频流数）、时长与输入相差 ≤ 2 秒或 2%、记录 sha256。输入已是修复成品（沿用 Jasna 插件现有的命名规则：`-破解`、`_restored` 等后缀）时不再修复，记为 `reused`。是否要取 `gpu_lease` 在领取前按影片判断：执行器提供 `needs_gpu(manifest)`，只有所有卷都是修复成品时才返回 False；只要有一卷需要修复，就取 lease（D19）。多卷中只要有一卷是 `applied`，整片就记为 `applied`。缺少 Jasna 或 LTX 模型、LTX 非 0 退出，都为 `needs_attention`：沿用 Jasna 现有规则，失败的 LTX 不自动重跑；中断和停止仍可免费重试 | 1、3、8 |
| E2 | asr 逐个主视频决定字幕来源，判定见 §3.4：已有中文 → `reused_zh`；已有日语 → `source_ja`；否则运行 WhisperJAV → `generated_ja` 或 `no_speech`。无语音不写空字幕文件。标签为中文但内容无法判定为中文，或无法解码 → `needs_attention`。`en` 等其他语言字幕保留为附件，从不计为中文 | 1、4、8 |
| E3 | translate 只处理 `source_ja` / `generated_ja`，输出 `<修复后名>.zh.srt`，并记录仍保留日语的句数；其他情况作为空操作成功结束（策略没有可跳过的阶段）。没有可用的 LLM 配置 → `needs_attention`；翻译失败或暂停 → `transient`。已有的有效字幕不被覆盖 | 1、4 |
| E4 | 每个阶段把产物与事实写入本 attempt 工作目录下的 `stage-result.json`，文件的 sha256 写入成功证据的 `payload_digest`。后续阶段只读当前成功 attempt 的结果，并先核对 hash。阶段成功后，删除同一 job 其他 attempt 的工作目录 | 2、3 |
| E5 | return 有独立的领取和核销路径。每个 boot 授予一个 receiver_adapter 身份，领取时不带 worker_id 和 resource_id。回传包目录为 `work/return/<film_id>-r<input_revision>/`：修复后视频用硬链接（失败则复制），字幕、NFO、图片、附件一律复制，原始 input 不动。逐个核对 hash 后写 `return-manifest.json`，以 receiver 身份签证据，调用 `accept_artifact_receipt`（`durable_reference` = 本 attempt 的 operation_id），再核销。重启时：完整的包重新核对 hash 后复用；`.partial` 目录删除后重建。两种情况都不会重跑 GPU | 3、9 |
| E6 | `return-manifest.json` 是交给 M01 的接口，包含：影片与输入版本、各阶段 attempt、每个文件的角色/相对路径/大小/sha256/所属视频/语言/卷序/媒体探测，以及**整部影片级**的事实（restoration：`applied` 或 `reused`，以及实际分辨率；subtitles：`present`+语言、或 `no_speech`、仍保留日语句数）。各卷字幕结论混合（部分中文、部分无语音以外的失败）→ `needs_attention`。已有 NFO 也照样交给 M01；父目录与分卷关系保留 | 8、9 |
| E7 | 单一调度线程。等 GPU 时继续看后面的任务，因此不需要 GPU 的阶段（translate、return）不会被手动 GPU 任务挡住。所有"放弃排队"统一经过一个 `_leave_queue()`：只在即将运行非 GPU 阶段时或停止时 withdraw；其他未持有 lease 的路径（领取被拒、本机 GPU 未登记、本轮结束）都保留排队位置（D14、D18） | 效率 |
| E8 | 受控子进程的输出按 `\r` 或 `\n` 分行，先解码再截断到 4096 字符，截断后继续读管道。Jasna 进度经 `parse_jasna_progress_line` 解析后放进 `status()`，分开显示阶段、百分比和 ETA | 5 |
| E9 | 启用工作流且已配置 Jasna、WhisperJAV 路径后，launcher 自动注册三个执行器，不需要手动添加 Jasna 监控；重复启动保持幂等 | 7 |
| E10 | 测试：用假的 Jasna、WhisperJAV、LLM（真实子进程或注入）覆盖成功、失败分类、复用判断、编码、无语音、回传重建与复用、return 崩溃后的重启核销；真实工具运行为待实机 | 5 |

待实机（交 T09）：
- 真实 Jasna LTX 在 5060 上跑 1080p，包括首次编译 TensorRT 引擎；
- WhisperJAV 识别效果；
- LLM 翻译质量；
- 磁盘占用。

已知局限：`stage-result.json` 已写好、但核销尚未提交时进程退出，attempt 会按"中断"处理，该阶段从头重跑（P0 下可接受）。

## 3. 组件

### 3.1 配置与注册

- `WorkflowSettings` 增加 `jasna_exe_path`、`whisperjav_exe_path`、`ltx_gpu`（默认 `"5060-8"`）。
- 预设取 `preset_catalogue()` 中 model 为 ltx、gpu 为该值的 profile，用其 `standard` 参数构造 `JasnaConfig`（`jasna_exe_path` 留空，以跳过插件的目录校验）。
- launcher 调用 `start_local_workflow(config.workflow, …, executors=default_executors(config.workflow))`。缺少某个 exe 时，不注册对应执行器。

### 3.2 阶段结果台账 `stage_results.py`

```text
work/<attempt_id>/stage-result.json
{ schema: 1, film_id, input_revision, stage, attempt_id,
  videos: [ { video_item_id, volume_ordinal,
              restore?: {decision: applied|reused, path, sha256, size, width, height, duration_ms, video_streams, audio_streams},
              subtitle?: {decision, ja?: {path, sha256, origin}, zh?: {path, sha256, origin}, kept_ja?} } ] }
```

- 路径都相对于工作根目录。
- `ExecOutcome` 增加 `result_digest`，调度在核销成功时把它写进证据的 `payload_digest`。
- 读取路径：`get_film` 取该阶段当前的 `attempt_id` → `list_attempts` 中它的成功证据 digest → 读文件并核对 sha256。不一致时为 `needs_attention`。

### 3.3 执行器

- **RestoreExecutor**（uses_gpu）：
  - 输入路径为工作根目录下 manifest `readiness.workcopy.items` 中对应的相对路径。
  - 参数：`build_argv(cfg, exe, input, out_tmp, "1080p", …, work_dir, fast_ok, False)`，其中 `fast_ok = ltx_fast and fast_supported(query_gpus())`。
  - 开工前检查 `ltx_missing_files(exe_dir, best, fast_ok)`。
  - 成功后依次执行 `retag_hevc_hvc1`、`os.replace` 和 ffprobe 校验。
- **AsrExecutor**（uses_gpu）：用 `whisperjav.build_argv` + `asr_env` 启动，结果由 `read_outcome` 解析。
- **TranslateExecutor**（不用 GPU）：
  - `Translator(checkpoint_dir=work/translate-checkpoints)`，逐句提交。
  - 等待结果时轮询取消事件；取消时调用 `cancel()` 并 join 后才返回。
- 所有执行器对 `cancel` 都作出响应。

### 3.4 字幕内容判定

1. **解码**：依次尝试 UTF-8（含 BOM）、UTF-16（有 BOM 时）、GB18030；都失败则视为无法解码。
2. **解析**：`srt.load`。只判定 SRT；`.ass`、`.vtt` 作为附件保留，不参与复用。
3. **统计**：整个文件内汉字和假名的数量；CJK 总数 < 20 时视为内容不足。
4. **内容判定**：
   - 假名占 CJK 的比例 ≤ 0.05 → 中文；
   - ≥ 0.2 → 日语；
   - 其他 → 未定。
5. **按标签决定结果**（D13）：
   - **标为中文**：
     - 内容为中文 → `reused_zh`；
     - 否则，若至少一半的字幕句中有一行是不含假名、且至少 2 个汉字的文字 → 按中日双语中文字幕 `reused_zh`（D21）；
     - 否则（判为日语、未定、内容不足或无法解码）→ `needs_attention`。
   - **标为日语**：内容为日语 → `source_ja`；其余情况运行 ASR。
   - **其他标签或无字幕** → 运行 ASR。

### 3.5 回传（ReturnStep）与调度改动

- **ReturnStep 不放进 `rt.executors`**，不会发布 return 能力。
- 调度每轮在计算阶段之后检查 pending 的 return job，使用 `list_jobs(auth, status, stages)`：它由 `list_compute_jobs` 推广而来，`PendingComputeJob.stage` 同步放宽。
- **领取与核销**：
  - 使用本 boot 的 receiver 身份领取，`AttemptFile` 记录 `assignment_id`。
  - 回执证据由 receiver 身份签名，并把每个 item_id 写进 `summary.item_ids`。
  - 核销时由 operator 调用 `resolve_assignment`。领取时没有 resource，所以 T01 不要求提供 stopped owner。
- **新增 operator 只读查询 `list_open_assignments(auth)`**，返回所有未核销的 assignment。
  - `_resolve` 和 `settle_previous` 改用它，不再只看 worker 自己的 assignment。重启时，旧 boot 的 return assignment 同样按"中断"核销并重试。
  - **核销范围只限本运行时自己的 assignment**：`worker_id` 为本机 worker；或 stage 为 return，且 owner 是本运行时授予的 receiver 身份。这样以后 M01、远端节点在同一个库里的工作，不会被主控重启误核销（D12）。
  - receiver 身份的 component_id 固定为 `LocalIdentity.receiver_component_id`。加载旧身份文件时若缺这个字段，就生成一个并原子写回，之后每次启动都相同（D17）。
  - `list_open_assignments` 关联 principals 表，返回每个 assignment 的 `owner_component_id`。不改 schema。
- **return 不启动续租线程**：租约长达 24 小时，回传几分钟就完成，而且 renew 必须用 receiver 身份（D11）。
- **新产物的 item_id** 为 `uuid5(film_id, 相对路径)`，重建时保持稳定。
- **复用已完成的包**：`return-manifest.json` 中不写 return 自己的 attempt 和 operation id；复用时重新核对每个文件的 hash，再重新计算 manifest 的 hash 写入回执（D16）。旧 input revision 的包目录，以及该 revision 各 job 的 attempt 工作目录，在新 revision 回传成功后一并删除（D20）。
- **目录改名**：在有限次数内重试 `PermissionError`（杀毒或索引程序占用句柄），仍失败则为 `transient`。

## 4. 不做的事

- 不做多通道并行（需要先修订 T01，阶段 2）。
- 不做 TLS 分块传输、新的执行器协议、出生计数之类的机制。
- 不在流水线中自动下载 LTX 模型。
- 不重写现有插件，只复用其中的纯函数。

## 5. 测试计划

| 测试 | 覆盖 |
|---|---|
| 假 Jasna（打印 `\r` 进度、按参数写输出）：成功、非 0 退出 → needs_attention 不重试、输出缺失、时长不符；输入已是 `-破解` → reused 且不启动子进程 | E1、E8 |
| 模型或 exe 缺失 → needs_attention，不启动子进程 | E1 |
| 字幕判定矩阵：简中、繁中、GB18030、UTF-16、中日双语（同一句内两行）、汉字很多的日语、标中文实为日语 → needs_attention、标中文内容不足 → needs_attention、英文、无字幕 → 假 WhisperJAV、无语音 | E2 |
| 翻译：成功、无 LLM 配置、失败；reused_zh / no_speech 时为空操作 | E3 |
| stage-result hash 被篡改 → needs_attention；成功后旧 attempt 工作目录被删除 | E4 |
| 回传：完整包、hash 核对、receiver 证据、回执与核销；return 中途"崩溃"后重启：assignment 被核销，`.partial` 重建，完整包复用；原始 input 字节不变 | E5、E6 |
| 各卷字幕结论混合 → needs_attention | E6 |
| 手动 GPU 任务占卡时，另一部影片的 translate 和 return 仍会执行；工作流在 `gpu_lease` 中的排队位置跨轮保留，手动任务释放后由工作流接手 | E7 |
| 重启核销不触碰非本运行时的 assignment（注入一个其他 component 的 scrape assignment） | D12 |
| 端到端（原生）：INBOX 投片 → 三个阶段 → 回传包 + `return-manifest.json` | E9、E10 |

## 附录：辩论第 1 轮处置

| ID | 级别 | 处置 |
|---|---|---|
| D01 双通道被 T01 的 GPU 单 owner 规则阻止 | P1 | 取消多通道；单线程，等 GPU 时继续看下一个任务（E7） |
| D02 return 能领取，但调度看不到、无法核销或恢复 | P1 | 独立路径、`list_open_assignments`、receiver 签名证据（§3.5） |
| D03 每个 attempt 的 operation_id 都不同，重启后无法复用回传包 | P2 | 包目录按 film_id + input_revision 命名（E5） |
| D04 LTX 非 0 退出会被重跑两次 | P2 | 改为 needs_attention（E1） |
| D05 中日文阈值重叠；未考虑编码 | P2 | 重写判定规则（§3.4） |
| D06 M01 需要整片级事实；已修复的输入会被重修 | P2 | 整片汇总、流数量、reused（E1、E6） |
| D07 硬链接与改名 | minor | 只对视频硬链接；改名有限重试 |
| D08 截断与进度解析 | minor | 先解码再截断；解析 Jasna 进度（E8） |
| D09 复用细节 | 记录 | 写入 §3.1、§3.3、E3、E9 |
| D10 崩溃窗口与磁盘 | minor | 写成已知局限；删除旧 attempt 目录（E4） |
| D11 return 续租需要 receiver 身份 | minor | return 不启动续租线程（§3.5） |
| D12 重启核销范围过宽 | P2 | 只核销本运行时自己的 assignment（§3.5） |
| D13 双语字幕和标签/内容不符的规则有误 | P2 | 重写 §3.4 第 5 步 |
| D14 每轮 withdraw 丢失 GPU 排队位置 | P2 | 等待时保留位置，运行非 GPU 阶段前才 withdraw（E7） |
| D15 reused 仍取 GPU；多卷混合 | minor | reused 不取 lease；有一卷 applied 即整片 applied（E1） |
| D16 复用的包 manifest 与 attempt | minor | manifest 中不写 return 自身 id；复用时重算；删除旧 revision 的包（§3.5） |
| D17 receiver component_id 不能跨重启，也读不到 | P2 | 写入 LocalIdentity 并补写旧文件；`list_open_assignments` 返回 `owner_component_id`（§3.5） |
| D18 其他路径仍会丢失排队位置 | minor | 统一 `_leave_queue()`（E7） |
| D19 reused 是否取 GPU 要在领取前判断 | minor | `needs_gpu(manifest)`（E1） |
| D20 旧 revision 的 attempt 目录泄漏 | minor | 与旧包目录一并删除（§3.5） |
| D21 双语规则对短中文句过严 | minor | 阈值改为 2 个汉字（§3.4） |

