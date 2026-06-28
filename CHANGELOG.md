# TaskPaw 修改日志

> 持续更新；不同批次按时间倒序排列。

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
