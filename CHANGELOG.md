# TaskPaw 修改日志

> 持续更新；不同批次按时间倒序排列。

---

## V3 3.3.1 — LLM API 出厂默认改为 xAI 直连（#181）

- `llm_api_base` 默认 `https://api.x.ai/v1`,`llm_model` 默认 `grok-4.3`(owner 在 3.3.0 上实测通过的
  组合),不再默认 OpenRouter。只改默认值:已保存过设置的 `agent.yaml` 不受影响;OpenRouter、本地
  Ollama 等 OpenAI 兼容端点仍可在设置页填写。示例配置、设置页占位符与提示、README 同步。

---

## V3 3.3.0 — 全局 LLM API 设置 + 可终止的 llm-worker（#178）

- **新增 agent 级 LLM API 设置。** `agent.yaml` 多了 `llm_api_base`（默认
  `https://openrouter.ai/api/v1`）、`llm_model`（默认 `x-ai/grok-4.1-fast`）、`llm_api_key`
  三个键；设置页新增「LLM API」卡片,可保存、清除 key、测试连接。三个字段**即时生效**,不需要重启。
  key 的优先级:环境变量 `TASKPAW_LLM_API_KEY` > `agent.yaml`;界面与 `/control/config` 一律
  打码为 `***`,并附 `llm_api_key_source`(env / config / none);留空或 `***` 保留原值,`null`
  显式清除;来自环境变量的 key 永远不写回 YAML(宪法 §2)。
- **`taskpaw_v3/core/llm.py`**:同步的 OpenAI 兼容 `chat()`(标准库 `urllib`,不跟随重定向,
  `Authorization` 只发给原始地址),完整校验响应信封,错误统一为 `LLMError(auth / rate_limit /
  refusal / network / bad_response)`,报错文本是固定字符串,绝不包含 key、提示词或响应正文。
- **`taskpaw_v3/core/llm_worker.py` + 打包角色 `taskpaw-backend llm-worker`**:stdin/stdout
  JSON 行协议的 LLM 请求子进程,设置只从子进程环境变量读取。取消 = 关闭它的 stdin:worker 自带的
  stdin 监视线程在 EOF 时立刻 `os._exit(0)`,即使主线程正阻塞在 HTTP 里,所以父进程死亡也不会遗留
  worker;Windows 上再加一层 kill-on-close Job Object 兜底。这是 #177「AV 翻译」的翻译线程能在
  5 秒停止预算内退出的前提。
- 新增控制命令 `llm_test` / 路由 `POST /control/llm-test`:用当前表单值测试连接,不持久化。
- 版本 3.2.1 → 3.3.0(六处版本文件同步)。

---

## V3 3.2.1 — Jasna 输出改用 hvc1 标签（macOS 可预览）

- **修复:Jasna 产出的 MP4 在 macOS 上无法预览。** ffmpeg 给 MP4 里的 HEVC 默认写
  `hev1` 采样条目名,苹果的 AVFoundation 只认 `hvc1`,于是 Finder 缩略图、QuickLook、
  QuickTime 和 Safari 一律当成不支持的格式。发布环节现在把暂存文件 stsd 里的那 4 个字节
  从 `hev1` 改写成 `hvc1`,然后再原子改名为最终文件。
- **只改元数据,不重封装。** 采样数据、`hvcC` 参数集和所有字节偏移原样不动,文件大小不变,
  解码结果逐帧一致——等价于 `ffmpeg -c copy -tag:v hvc1`,但不需要重写几十 GB。非苹果播放器
  两种标签都认。只有当采样条目确实是 `hev1` 且带 `hvcC`(参数集在盒内)时才改写;文件结构看不懂
  就原样发布并记一条警告,绝不会因为改标签丢掉已完成的视频。
- 同一问题 Lada 在 [ladaapp/lada@ed2f09e](https://github.com/ladaapp/lada/commit/ed2f09ec3a717756b316188889b32a9a2b8c29af)
  中在写入端修掉了;Jasna 是冻结的二进制、CLI 也没有设置 codec tag 的开关,所以 TaskPaw 在
  发布环节补上。已有的旧输出可以用同一个函数原地补标签。

---

## V3 3.2.0 — Jasna 任务类型 (#173)

- **新增 `jasna` 任务类型（Jasna 视频修复）**。托管模式下 TaskPaw **逐个文件**启动
  `jasna.exe`（不用文件夹批处理），因此可以断点续跑（已有最终输出 `<原名>_restored.mp4`
  的文件直接跳过）、逐文件重试，并能按文件分别给参数。被动模式仍然只监视已在运行的进程。
- **按分辨率分档**：ffprobe 探测每个视频的分辨率，像素数 > 1920×1080×1.5（约 3.1 MP，即
  2560×1440 及以上）归为 **4K 档**，其余为 **1080p 档**；两档各自有片段长度
  （默认 90 / 60）和一个 **unet-4x 二次修复勾选框：1080p 默认开、4K 默认关**（8 GB 显存放不下
  4K 的 unet-4x）。
- **失败重试与自动降级**：带 unet-4x 的启动失败会自动不带 unet-4x 重跑；若重跑成功，告警一次
  （未在 Jasna 图形界面激活赞助者授权，或显存不足）并在本次运行中**仅对该档位**关闭 unet-4x。
  普通启动失败重试一次后告警并跳过该文件；连续 3 个文件失败则中止整批（状态 `degraded`）。
- **输出原子发布**：Jasna 先写暂存名 `<原名>_restored.tmp.mp4`，只有退出码 0 才改名为最终名，
  被杀/崩溃的一次运行不会留下被误认为“已完成”的文件。
- **队列指标与 lada 对齐**：`queue_completed/total/remaining`（新增 `queue_failed`）、`current_file`、
  捕获模式下的 `percent/elapsed/processed_frames/remaining_frames/eta/fps`，以及 CPU/内存/GPU；
  `status.md` 按 lada 的格式渲染 jasna 快照，[docs/guides/openclaw-integration.md](docs/guides/openclaw-integration.md)
  的字段对照表已含 jasna。
- **所有字段都有中文标题与说明**（向导/配置表单），新增服务图标，“关于”文案同时提到 LADA / Jasna。
- **托管 Jasna 不会开机自启**（需手动点“启动”），`jasna_capture_progress` 默认关（每个文件自己开一个
  控制台窗口显示进度）。
- **版本号 3.1.0 → 3.2.0**（`taskpaw_v3/__init__.py`、`tauri.conf.json`、`Cargo.toml`、`Cargo.lock`、
  `ui/package.json`、`ui/package-lock.json` 六处），并新增 `taskpaw_v3/tests/test_version.py` 断言六处一致，
  以后不会再漏改。
- **Lada 不受影响**：`lada` 任务类型、它的配置与行为完全未改，也没有任何自动迁移；
  两个类型可以共存。

---

## V3 未发布 — Mac 打包 parity + OpenClaw 富指标 (afk/mac-parity)

- **OpenClaw:`status.md` 恢复 V2 的富指标**,`status_log` 的 `status_json` 也带全字段 ——
  CPU%、内存(新增 `mem_used_mb`/`mem_total_mb`,已用/总量 GB)、GPU%、显存、LADA 队列
  (`queue_completed/total/remaining` + `current_file`)、ComfyUI(`running`/`pending`)。
  新增 **[docs/guides/openclaw-integration.md](docs/guides/openclaw-integration.md)**:如何从
  `hub.db`/`status.md` 读取,字段对照表,以及"用 `state` 不用 `enabled` 判断运行"等规则。
- **修复:运行中的监控不再错显 `disabled`。** 一个正在跑(有实时 `state`/`metrics`)但配置
  `enabled:false` 的监控,`status.md` 现按实时状态渲染,与数据库/UI 一致;仅未启动的桩显示
  `disabled`。无 `type_id` 的旧 agent 主机按 `disk_pct` 识别,CPU/GPU/显存照常输出。
- **Hub 面板:下钻显示每个监控的完整指标**(CPU/GPU/显存/队列/fps 仪表),与 Agent 控制台一致。
- **status.md 加固**:所有名字/状态/文件名 sanitize(控制字符→空格、限长),防止行注入;
  数值 NaN/inf 过滤;错误态不再被陈旧指标掩盖。
- **打包 / 桌面外壳(Mac)**:端口冲突等启动失败**不再崩溃**(友好原生弹窗 + 干净退出,先杀后端
  防孤儿);后端日志按角色写 `~/Library/Logs/TaskPaw/taskpaw-backend-<role>.log`(append + 滚动
  + `O_NOFOLLOW`);真实品牌图标 `.icns`;全新安装的 `machine` 默认取系统友好电脑名
  (macOS `ComputerName` / Windows `%COMPUTERNAME%`)而非网络 hostname。
- 遗留硬化项(非阻塞)记入 issue #127;Windows `.exe` 构建见 issue #126。

---

## V3 未发布 — Hub OpenClaw 兼容 (#38)

- **破坏性变更(仅早期 V3):Hub 的 SQLite 库 + `status.md` 现位于 `HubConfig.data_dir`
  (默认 `~/.taskpaw-hub/`),不再在 `hub.yaml` 旁边。** 对齐 V2 位置,使 OpenClaw 脚本
  直接读 `~/.taskpaw-hub/{hub.db,status.md}` 无需改动。若你跑过会在 `hub.yaml` 旁建
  `hub.db` 的早期 V3 构建,Hub 会**拒绝启动**(管理 CLI / bootstrap 也拒绝注册),
  而非静默从空库开始。处理:
  ```
  mv ~/Library/Application\ Support/TaskPaw/hub.db ~/.taskpaw-hub/hub.db   # macOS
  ```
  或在 `hub.yaml` 设 `data_dir` 指向旧目录,或用 `--db <path>` 显式指定。
- Hub 每轮写 `status.md`(V2 Markdown 格式)+ 每次成功轮询记一行
  `status_log(server_id, timestamp, status_json)`,OpenClaw 脚本零改动。打开 V2 `hub.db`
  时就地迁移(status_log 列、events 保留为 `events_v2_legacy`)。

---

## v2.7 — Claude 第二轮修复（2026-05-06）

> 由 Claude (Kate) 在 Kimi 完成 P0/P1/P2 修复后追加  
> 解决 Codex 独立审计发现的关键回归 + Claude 自己的补充发现  
> 对应审计报告：`CODEX_AUDIT_FINDINGS.md` + Claude 原始 `BUG_AUDIT.md`

### 🔴 P0 — 关键事件传递修复

| # | 问题 | 文件 | 行号 | 改动摘要 |
|---|------|------|------|----------|
| 1 | Windows 事件无 `id` 字段且 `/events` 返回裸列表（应返回 `{"events": [...]}` 包装），Hub 静默丢弃所有事件 | `taskpaw.py` | 210–272, 285–303 | 增加单调 `id`，响应改为 `{"events": [...]}`，计数器持久化到 `%APPDATA%\TaskPaw\state.json`（原子写） |
| 2 | MacSubs `_next_event_id` 仅在内存，重启后归 1；Hub 已持久化 `last_event_ids` 会过滤掉所有新事件 | `macsubs.py` | 58–132 | 持久化 `_next_event_id` 到 `~/Documents/MacSubs/.event_state.json`；启动加载，递增后保存 |

**影响：** 此次修复前，Lada / ComfyUI / 文件夹监控的完成通知**实际上从未到达 OpenClaw**（Hub 解析出错或 ID 过滤拒绝），且队列每次轮询会被清空，事件永久丢失。这是 Codex 审计第 1 项发现。

### 🟠 P1 — 认证与健壮性

| # | 问题 | 文件 | 行号 | 改动摘要 |
|---|------|------|------|----------|
| 3 | HTTP API 三处均无认证 | `taskpaw.py` / `macsubs.py` / `taskpaw_hub.py` | 多处 | 可选 Bearer Token：空字符串 = 无认证（默认，保持原行为）；设值后 `/status` 与 `/events` 强制校验 `Authorization: Bearer <token>`；`/ping` 始终开放 |
| 4 | FolderWatcher 把 0 字节文件计入稳定计数，失败下载会触发"完成"通知 | `taskpaw.py` | ~1408 | 在循环中 `if size == 0: continue` |
| 5 | MacSubs 主监控循环 `except: pass` 静默吞异常 | `macsubs.py` | ~520 | 改为 log + `update_status("error", …)` + `add_event("error", …)` + 2s 退避 |

**Token 配置点：**

| 组件 | 配置位置 |
|------|---------|
| TaskPaw (Windows) | `Settings` 标签页 → "API Token" 字段（`%APPDATA%\TaskPaw\config.json` 中 `api_token`） |
| MacSubs (Mac) | 环境变量 `MACSUBS_API_TOKEN`（在 launchd plist 或 shell env 中设置） |
| Hub (Mac) | Hub `Settings` 标签页 → "Polling Auth" 区段 → "Token" 字段（SQLite `config` 表 `polling_token`） |

三处 token 必须保持一致。任一处为空即代表"该方向不强制认证"。失败认证返回 401 且**不**清空事件队列，避免攻击者通过错误 token 轮询冲走待发事件。

### 🎁 顺手修复（一并改了）

| # | 问题 | 文件 | 行号 | 改动摘要 |
|---|------|------|------|----------|
| 6 | `WatcherConfig` 加载时遇到未知字段（如旧版残留或未来新字段）会抛 `TypeError`，整个配置回退默认（Codex 第 8 项） | `taskpaw.py` | 147–178 | 加载前过滤未知键并记录 info 日志；单条 watcher 解析失败不影响其它 |
| 7 | API Port 仅校验下限（≥1），上限 65535 未限制（Codex 第 7 项） | `taskpaw.py` | 2188–2204 | 强制 `1 ≤ port ≤ 65535`；无效值在 UI 显示红色错误并保留原值 |
| 8 | 双开导致端口竞争 → Hub 看到错误的 watcher 状态（实战发现） | `taskpaw.py` | 2834–2925 | Windows 命名 mutex 单实例强制：第二次启动检测到已有实例 → 弹提示框后静默退出。不影响首次启动正常流程。stdlib only（ctypes）；非 Windows 平台为空操作 |

**多实例 bug 复盘：** 在 SnowLeopard 上排查 ComfyUI 状态显示 "Stopped" 的过程中发现：用户启动了两个 TaskPaw 实例（一个来自 Windows 启动文件夹、一个手动启动），其中一个绑定到 5678 端口，另一个静默失败（`watcher_status` 字典保持空）。Hub `curl /status` 走第一个实例 → 永远返回 "Stopped"；用户在第二个实例的 UI 里点的 Start 实际只更新了那个实例的内存状态。耗时约 1 小时定位。修复方案：进程启动最早期取 `CreateMutexW`，竞争失败立即退出。

### 🔄 版本号

- `APP_VERSION`：`2.5.0` → `2.7.0`（跳过 2.6 与 Kimi 的批次错开）。

### 📌 仍未修复（建议下一批次）

| 项 | 说明 | 来源 |
|----|------|------|
| MacSubs `5679` vs Hub 默认 `5678` 端口不一致 | 决定：要么改代码统一为 5678，要么在 `DEPLOYMENT_GUIDE.md` 明确文档化 | Codex 第 5 项 |
| 三机时区契约未明确 | Hub / Windows / Mac 各用本地时间字符串，建议统一以 Hub 本地时间为权威，文档化 | Claude 原始发现 |
| TaskPaw → OpenClaw webhook payload schema 未版本化 | 需要明确字段名、类型、版本号 | Claude 原始发现 |

### 🔁 重新打包提示

1. 在 Windows 上：`python -m py_compile taskpaw.py` 确认语法，然后 `build.bat`
2. 在 Mac 上：`python3 -m py_compile macsubs.py taskpaw_hub.py`，然后 `build_hub.sh`
3. 启用 token 时**先**改 Hub，再改各 Windows agent，再改 MacSubs；任一处不一致会出现 401，但不崩溃，下一轮询恢复

---

## v2.5 — Kimi 第一轮修复（2026-05-06）

> 由 Kimi Code CLI 生成于 2026-05-06  
> 对应审计报告：`CODE_AUDIT_REPORT.md`

---

## ✅ 已完成（P0 + P1 + P2）

### P0 — 关键安全与稳定性修复（2026-05-06）

| # | 问题 | 文件 | 行号 | 改动摘要 |
|---|------|------|------|----------|
| 1 | `save_config()` 非原子写入 | `taskpaw.py` | 167-171 | `.tmp` → `os.replace()` |
| 2 | `CustomCmdWatcher` 命令注入 | `taskpaw.py` | 1429-1437 | `shlex.split()` + `shell=False` |
| 3 | Hub 外键约束未启用 | `taskpaw_hub.py` | 82 | `PRAGMA foreign_keys=ON` |
| 4 | Hub 日志修剪 SQL 错误 | `taskpaw_hub.py` | 305 | `datetime('now', '-7 days', 'localtime')` |
| 5 | Hub `status.md` 非原子写入 | `taskpaw_hub.py` | 575-577 | `.md.tmp` → `os.replace()` |
| 6 | `dist/` / `build/` 旧版本 | 目录级 | — | 已删除，待重新打包 |

### P1 — 轮询与线程安全（2026-05-06）

| # | 问题 | 文件 | 行号 | 改动摘要 |
|---|------|------|------|----------|
| 7 | Hub 轮询漂移 | `taskpaw_hub.py` | 369-384 | `time.monotonic()` 驱动 |
| 8 | 数据库操作无回滚 | `taskpaw_hub.py` | 163-352 | 7 个写操作加 `try/except + rollback` |
| 9 | macsubs 状态无锁 | `macsubs.py` | 22, 76-77, 191-192 | `_status_lock` 保护读写 |

### P2 — 精确匹配、日志、事件与 Schema（2026-05-06）

| # | 问题 | 文件 | 行号 | 改动摘要 |
|---|------|------|------|----------|
| 10 | 进程名子串匹配 → 精确匹配 | `taskpaw.py` | 931-966, 1426-1461 | `psutil.process_iter(['name'])` 精确匹配，回退 CSV 解析 |
| 11 | ComfyUI 边界双发 | `taskpaw.py` | ~1056 | `idle_count` 只在 `was_processing` 时递增 |
| 12 | ComfyUI JSON 解析失败静默 | `taskpaw.py` | 1175-1225 | 捕获 `json.JSONDecodeError`，记录响应体前 200 字符 |
| 13 | 日志无轮转 | `taskpaw.py` / `taskpaw_hub.py` | 49-56 / 50-57 | `FileHandler` → `RotatingFileHandler(10MB, 5 backups)` |
| 14 | Bare except 清理 | `taskpaw.py` / `taskpaw_hub.py` / `macsubs.py` | 多处 | 捕获具体异常并记录 debug |
| 15 | Hub 修剪后无 VACUUM | `taskpaw_hub.py` | 336-346 | 每 10 次实际删除后执行 `VACUUM` |
| 16 | Hub IP/端口无校验 | `taskpaw_hub.py` | 1133-1138, 1252-1257 | `ipaddress.ip_address()` + 端口 1-65535 |
| 17 | Hub 事件去重不持久 | `taskpaw_hub.py` | 380-643 | 启动加载 / 停止保存 / 新事件后即时保存 `last_event_ids` |
| 18 | macsubs `/events` 空队列 | `macsubs.py` | 64-89, 162-163 | 新增 `add_event()` / `get_and_clear_events()`，线程安全 FIFO |
| 19 | macsubs 事件缺少 `id` | `macsubs.py` | 64-89 | 单调递增 `id`，队列上限 100 条 |
| 20 | Schema 漂移 | `macsubs.py` | 153 | `"type": "macsubs"` → `"type": "custom"` |
| 21 | 依赖版本未锁定 | `requirements.txt` | 5-10 | 增加 `<major+1` 上限 |

---

## ⏳ 待执行（待 Claude 评审）

| # | 问题 | 文件 | 建议方案 | 风险说明 |
|---|------|------|----------|----------|
| **7** | HTTP API 无认证，绑定 `0.0.0.0` | `taskpaw.py` ~301 | **方案 A（推荐）**：保持 `0.0.0.0`，增加 `Authorization: Bearer <token>` 头校验 | 绑定 `127.0.0.1` 会切断 Hub 远程轮询，破坏架构 |

**给 Claude 的问题：**
- 是否接受方案 A（共享 token 认证）？
- token 应复用 OpenClaw 的 token 还是独立配置？
- 是否需要在 `taskpaw_hub.py` 的 `poll_server()` 中同步增加 token 发送逻辑？

---

## 📝 待 Claude 补充评审的原始发现

以下 9 项来自 Claude 的审计补充，尚未修复，供评审后决定是否纳入下一批次：

| # | 问题 | 文件 | 严重程度 |
|---|------|------|----------|
| 22 | ComfyUI idle-confirm 状态机边界双发（Claude 原始发现） | `taskpaw.py` ~1044 | 中 |
| 23 | ComfyUI `json.loads()` 失败静默（Claude 原始发现） | `taskpaw.py` ~1175 | 高 |
| 24 | Hub VACUUM 从不执行（Claude 原始发现） | `taskpaw_hub.py` ~305 | 中 |
| 25 | macsubs `/events` 永远返回 `[]`（Claude 原始发现） | `macsubs.py` 127-128 | 高 |
| 26 | macsubs 事件缺少 `id` 字段（Claude 原始发现） | `macsubs.py` + `taskpaw_hub.py` ~451 | 高 |
| 27 | 监控类型枚举漂移（Claude 原始发现） | `macsubs.py` 119 + 文档 | 低 |
| 28 | 三机时区契约未定义（Claude 原始发现） | 跨文件 | 低 |
| 29 | Webhook payload schema 未文档化（Claude 原始发现） | `taskpaw_hub.py` + 文档 | 低 |
| 30 | `dist/` 旧版本未清理（Claude 原始发现 → Kimi 已修） | 目录级 | 高 |

> 注：项 22-26 已包含在 Kimi 的 P2 修复中。项 27-29 仍未修复，待评估。项 30 已在 P0 修复。

---

## 🔄 重新打包检查清单

当准备好重新打包 `.exe` 时：

- [ ] 确认第 7 项（HTTP API 认证）已处理或明确跳过
- [ ] 运行 `build.bat`（Windows）
- [ ] 运行 `build_hub.sh`（macOS）
- [ ] 验证新的 `dist/TaskPaw.exe` 和 `dist/TaskPawHub` 存在
- [ ] 运行基本功能测试（启动、添加监控、轮询、事件通知）
