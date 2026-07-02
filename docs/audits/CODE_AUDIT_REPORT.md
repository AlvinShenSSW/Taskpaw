# TaskPaw 项目代码审计报告

> 审计日期：2026-05-06  
> 审计范围：`taskpaw.py` (Windows Agent)、`taskpaw_hub.py` (macOS Hub)、`macsubs.py` (macOS 字幕微服务)  
> 审计依据：源代码逐行分析 + `BUG_AUDIT.md` 交叉验证

---

## 📋 审计范围

| 文件 | 行数 | 说明 |
|------|------|------|
| `taskpaw.py` | 2,656 | Windows Agent（主程序） |
| `taskpaw_hub.py` | 1,461 | macOS Hub（中央监控） |
| `macsubs.py` | 449 | macOS 字幕翻译微服务 |
| `BUG_AUDIT.md` | 162 | 内部已知 Bug 清单 |
| `README.md` / `OPENCLAW_INTEGRATION.md` | 301 | 文档 |
| `build.bat` / `build_hub.sh` / `TaskPaw.spec` | 133 | 构建脚本 |

---

## 🔴 严重问题（Critical）

### 1. `save_config()` 非原子写入 — 配置损坏风险 ✅ FIXED

**位置：** `taskpaw.py:167-171`

**状态：** 已修复（P0）

**修复内容：** 使用 `*.tmp` + `os.replace()` 原子替换，避免写入中断导致配置损坏：

```python
def save_config(cfg: AppConfig):
    tmp = CONFIG_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg.to_dict(), f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG_FILE)
```

---

### 2. HTTP API 无任何认证，绑定 `0.0.0.0`

**位置：** `taskpaw.py:301`

**代码：**

```python
self.server = ThreadingHTTPServer(("0.0.0.0", self.port), APIRequestHandler)
```

**问题：** 局域网内任意设备可访问 `/status`、`/events`、`/ping`，暴露机器名、运行状态、监控列表、所有历史事件。在公共 WiFi 环境下风险极高。

**修复建议：** 方案 A：绑定 `127.0.0.1` + 通过 Tailscale/ZeroTier 访问；方案 B：增加 `Authorization: Bearer <token>` 头校验。

---

### 3. `CustomCmdWatcher` 命令注入漏洞

**位置：** `taskpaw.py:1427-1434`

**代码：**

```python
result = subprocess.run(
    self.cfg.custom_command,
    shell=True,
    capture_output=True, text=True, timeout=30,
    creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
)
```

**问题：** `shell=True` + 用户可控输入 = 任意命令执行。恶意配置文件可执行 `del /s /q C:\` 或下载木马。虽然需要本地 GUI 访问，但一旦配置文件被篡改（通过社工、其他漏洞），即获得完整 shell。

**修复建议：** 改为 `shlex.split()` + `shell=False`：

```python
cmd = shlex.split(self.cfg.custom_command)
result = subprocess.run(cmd, shell=False, ...)
```

（代码中已导入 `shlex`，line 24，却未在此使用。）

---

### 4. `taskpaw_hub.py` 外键约束从未启用

**位置：** `taskpaw_hub.py:77-82`

**代码：**

```python
self._conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=10)
self._conn.execute("PRAGMA journal_mode=WAL")
self._conn.execute("PRAGMA busy_timeout=5000")
```

**问题：** 创建了 `FOREIGN KEY` 约束（`servers.id` → `status_log.server_id`），但从未执行 `PRAGMA foreign_keys=ON`。SQLite 默认关闭外键，因此 `DELETE FROM servers WHERE id=1` 后，关联的 `status_log` 孤儿记录不会被级联清理。

**修复建议：** 连接后立即执行：

```python
self._conn.execute("PRAGMA foreign_keys=ON")
```

---

### 5. Hub 状态文件非原子写入

**位置：** `taskpaw_hub.py:576`

**代码：**

```python
STATUS_FILE.write_text("\n".join(lines), encoding="utf-8")
```

**问题：** `status.md` 被 OpenClaw 持续读取，半写状态可能导致 OpenClaw 读到损坏内容。

**修复建议：**

```python
tmp = STATUS_FILE.with_suffix(".md.tmp")
tmp.write_text("\n".join(lines), encoding="utf-8")
os.replace(tmp, STATUS_FILE)
```

---

### 6. `macsubs.py` 全局状态字典无锁保护

**位置：** `macsubs.py:43-54, 184-202`

**代码：**

```python
_current_status = { ... }  # 全局变量，被 HTTP handler 线程和主线程同时读写
```

**问题：** `update_status()` 在主线程中覆写整个 `_current_status` dict，而 `TaskPawHandler.do_GET()` 中的 `_current_status.copy()` 在 HTTP handler 线程执行。虽 CPython 的 GIL 降低了风险，但非原子操作仍可能导致不一致状态（如读到一半新一半旧的数据）。

**修复建议：** 增加 `threading.Lock()`：

```python
_status_lock = threading.Lock()

def update_status(...):
    global _current_status
    try:
        sys_info = get_mac_system_info()
        with _status_lock:
            _current_status = { ... }
    except Exception as e:
        print(f"update_status error: {e}")
```

---

## 🟠 高危问题（High）

### 7. Hub 轮询逻辑漂移（Polling Drift）

**位置：** `taskpaw_hub.py:340-363`

**代码：**

```python
def run(self):
    while self.running:
        try:
            self.poll_all_servers()
            self.poll_count += 1
            time.sleep(1)
        except Exception as e:
            logger.error(f"Error in polling loop: {e}")
            time.sleep(1)

def poll_all_servers(self):
    if self.poll_count % poll_interval != 0:
        return
```

**问题：** 使用 `poll_count % 60` 判断轮询时机，但 `time.sleep(1)` 加上每次轮询执行耗时，实际间隔会漂移。运行数天后，轮询间隔可能变成 70s+，导致 Hub 判断服务器离线。

**修复建议：** 使用 `time.monotonic()` 计算下次轮询时间：

```python
def run(self):
    next_poll = time.monotonic()
    while self.running:
        now = time.monotonic()
        if now >= next_poll:
            try:
                self.poll_all_servers()
            except Exception as e:
                logger.error(f"Error in polling loop: {e}")
            next_poll = now + poll_interval
        time.sleep(1)
```

---

### 8. Hub 日志修剪逻辑错误 — 旧日志永不被删除 ✅ FIXED

**位置：** `taskpaw_hub.py:305`

**状态：** 已修复（P0）

**修复内容：** 改用 SQLite 原生 `datetime()` 函数比较，修复字典序导致的永远匹配不到记录的问题：

```python
cursor.execute(
    "DELETE FROM status_log WHERE timestamp < datetime('now', '-7 days', 'localtime')"
)
```

---

### 9. Hub 数据库操作无回滚

**位置：** `taskpaw_hub.py:162-171, 192-212`

**问题：** `add_server()`、`store_status()`、`store_event()` 等操作在 `commit()` 前若抛出异常，不会执行 `rollback()`，连接处于未决事务状态，后续操作可能锁死。

**修复建议：** 每个数据库写操作包在 try/except 中：

```python
def add_server(self, server: Server) -> int:
    with self._lock:
        cursor = self._conn.cursor()
        try:
            cursor.execute(
                "INSERT INTO servers (name, ip, port, enabled) VALUES (?, ?, ?, ?)",
                (server.name, server.ip, server.port, int(server.enabled)),
            )
            self._conn.commit()
            return cursor.lastrowid
        except Exception:
            self._conn.rollback()
            raise
```

---

### 10. `taskpaw.py` 进程名子串匹配导致误判

**位置：** `taskpaw.py:935, 1404`

**代码：**

```python
return name.lower() in result.stdout.lower()
```

**问题：** 进程名 `python` 会匹配 `pythonw.exe`、`python3.10.exe`、甚至路径中的 `C:\not_python_but_this.exe`。

**修复建议：** 解析 `tasklist` 的 CSV 输出或使用 `psutil.process_iter()`：

```python
def _check_process(self, name: str) -> bool:
    try:
        for proc in psutil.process_iter(['name']):
            if proc.info['name'] and proc.info['name'].lower() == name.lower():
                return True
        return False
    except Exception:
        return False
```

---

### 11. `macsubs.py` 异常静默吞掉

**位置：** `macsubs.py:180-181, 201-202`

**代码：**

```python
except Exception:
    pass
```

**问题：** `get_mac_system_info()` 和 `update_status()` 的所有异常都被静默吞掉。如果系统信息收集持续失败，用户永远不知道原因。

**修复建议：** 至少记录 debug 日志：

```python
except Exception as e:
    print(f"[DEBUG] get_mac_system_info failed: {e}")
```

---

## 🟡 中危问题（Medium）

### 12. `dist/` 目录包含未修复的旧版本 ✅ FIXED

**状态：** 已修复（P0）

**修复内容：** 已删除 `dist/` 和 `build/` 目录，避免误分发旧版本。下次打包前请重新执行 `build.bat` / `build_hub.sh`。

---

### 13. FolderWatcher 0 字节文件误判

**位置：** `taskpaw.py:1316-1325`

**问题：** 新创建的 0 字节文件在第一次 poll 时即被记录大小为 0，后续若文件一直未写入（如下载失败），会在 `stable_seconds` 轮后触发"文件已完成"通知。

**修复建议：** 在 `FolderWatcher.run()` 中增加：

```python
if size == 0:
    continue
```

---

### 14. Hub 无 IP/端口校验

**位置：** `taskpaw_hub.py:1047-1060`

**问题：** 添加服务器对话框仅检查 `port` 是否为整数，不检查范围（1-65535），也不验证 IP 格式。可输入 `99999`、`abc` 等无效值。

**修复建议：**

```python
import ipaddress

def validate_server(ip: str, port: int):
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        raise ValueError("Invalid IP address")
    if not (1 <= port <= 65535):
        raise ValueError("Port must be between 1 and 65535")
```

---

### 15. Hub 事件去重状态不持久化

**位置：** `taskpaw_hub.py:338`

**代码：**

```python
self.last_event_ids = {}  # 内存中，Hub 重启后丢失
```

**问题：** 重启 Hub 后，所有历史事件会被重新读取并重复发送给 OpenClaw。

**修复建议：** 将 `last_event_ids` 持久化到 SQLite `config` 表，启动时读取，关闭时保存。

---

### 16. `macsubs.py` 固定重试间隔

**位置：** `macsubs.py:265-281`

**问题：** API 调用失败后固定等待 3 秒重试 3 次，对 OpenRouter 的限流不友好。

**修复建议：** 使用指数退避（exponential backoff）：

```python
import random

delay = 3 * (2 ** attempt) + random.uniform(0, 1)
time.sleep(delay)
```

---

### 17. macsubs 与 Hub 端口不匹配

**位置：** `macsubs.py:40` (5679) vs `taskpaw_hub.py:67` (默认 5678)

**问题：** 按照默认配置，Hub 永远连不上 MacSubs，除非手动修改端口。

**修复建议：** 统一为 5678（Mac 上两者不会同时运行），或在文档中明确标注。

---

### 18. ComfyUI 空闲确认状态机边界双发

**位置：** `taskpaw.py:~1044-1060` (Claude 补充)

**问题：** 当队列从 running→empty 的边界，idle_confirm 计数器可能因边界条件而 double-fire，导致过早或重复通知。

**修复建议：** 在递增 `idle_count` 前检查 `was_processing` 状态。

---

### 19. ComfyUI JSON 解析失败静默吞掉

**位置：** `taskpaw.py:~1175, ~1198` (Claude 补充)

**问题：** `json.loads()` 失败时（如 ComfyUI 返回 HTML 错误页而非 JSON），异常被 bare `except` 吞掉，用户看不到任何错误信息。

**修复建议：** 捕获 `json.JSONDecodeError` 并记录响应体前 200 字符。

---

### 20. Hub 修剪后从不执行 VACUUM

**位置：** `taskpaw_hub.py:299-308` (Claude 补充)

**问题：** 即使日志修剪正确执行，`DELETE` 操作后从不运行 `VACUUM`，SQLite 数据库文件只增不减。

**修复建议：** 在 `prune_old_status_logs()` 的末尾定期执行 `VACUUM`（例如每周一次，通过计数器控制频率）。

---

### 21. macsubs `/events` 永远返回空队列

**位置：** `macsubs.py:127-128` (Claude 补充)

**问题：** `/events` 端点硬编码返回 `{"events": []}`，字幕完成事件永远不会传播到 Hub/OpenClaw。

**修复建议：** 实现事件队列机制，在字幕处理完成/出错时添加事件。

---

### 22. macsubs 事件缺少 `id` 字段导致 Hub 去重拒绝

**位置：** `taskpaw_hub.py:450-457` + `macsubs.py:127` (Claude 补充)

**问题：** Hub 的事件去重逻辑依赖 `e.get("id", -1) > last_id`，但 macsubs 的事件根本不存在 `id` 字段，因此即使 `/events` 有数据，也全被过滤掉。

**修复建议：** macsubs 事件必须包含从 1 开始单调递增的 `id`，并持久化到文件以防重启丢失。

---

### 23. 监控类型枚举漂移

**位置：** `macsubs.py:119` (Claude 补充)

**问题：** macsubs 上报 `"type": "macsubs"`，但 `OPENCLAW_INTEGRATION.md` 仅定义了 `lada / comfyui / folder / process / custom`。OpenClaw 无法识别 `"macsubs"` 类型。

**修复建议：** 统一使用 `"custom"` 类型，或更新文档扩展枚举值。

---

### 24. 三机时区契约未定义

**位置：** 跨文件 (Claude 补充)

**问题：** Windows Agent、macOS Hub、macsubs 三台机器使用各自的本地时间，事件时间戳对比时可能产生歧义（特别是跨时区场景）。

**修复建议：** 统一使用 UTC 存储，本地时间仅在展示层转换。

---

### 25. TaskPaw → OpenClaw Webhook 负载未定义 Schema

**位置：** `taskpaw_hub.py:471-493, 495-537` (Claude 补充)

**问题：** POST 到 OpenClaw 的 JSON payload 没有文档化的字段列表、类型、版本号，OpenClaw 只能硬编码解析。

**修复建议：** 在 `OPENCLAW_INTEGRATION.md` 中定义 webhook payload schema（字段、类型、版本）。

---

## 🟢 低危问题（Low）

### 18. 日志无轮转

**位置：** `taskpaw.py:49-56`、`taskpaw_hub.py:50-57`

**问题：** 使用固定 `FileHandler`，长期运行后日志文件可能增长到数百 MB。

**修复建议：** 使用 `RotatingFileHandler(maxBytes=10_000_000, backupCount=5)`。

---

### 19. Bare `except` 过多

**位置：** `taskpaw.py:1638-1639, 2063-2064, 2620-2622` 等

**问题：** 多处 `except:` 或 `except Exception:` 后无任何处理或仅 `pass`，隐藏了真正的错误。

**修复建议：** 捕获具体异常并记录 debug 日志：

```python
except (tk.TclError, RuntimeError) as e:
    log.debug(f"UI callback failed during shutdown: {e}")
```

---

### 20. `requirements.txt` 未锁定版本

**位置：** `requirements.txt`

```text
pystray>=0.19.0
Pillow>=10.0.0
psutil>=5.9.0
```

**问题：** 使用 `>=` 可能导致未来依赖升级引入破坏性变更。

**修复建议：** 锁定主版本：

```text
pystray>=0.19.0,<1.0.0
Pillow>=10.0.0,<11.0.0
psutil>=5.9.0,<6.0.0
```

---

## 📊 修复状态汇总

| 文件 | 严重 Bug | 已修复 | 未修复 | 修复率 |
|------|---------|--------|--------|--------|
| `taskpaw.py` | 6 | 4 | 2 | 67% |
| `taskpaw_hub.py` | 6 | 0 | 6 | 0% |
| `macsubs.py` | 3 | 0 | 3 | 0% |

> 注：统计基于 `BUG_AUDIT.md` 中列出的 Critical + High 级别问题。

---

## ✅ 已正确修复的要点（值得肯定）

1. **`ThreadingHTTPServer` 替代 `HTTPServer`**（taskpaw.py:301）— 消除了单线程阻塞导致的假死问题。
2. **`psutil` 替代 `wmic`**（taskpaw.py:401-424）— 消除了 Windows 11 上 `wmic` 挂起导致的 watcher 线程冻结。
3. **`RLock` 保护共享状态**（taskpaw.py:1624）— `watcher_status` / `watchers` 字典现在线程安全。
4. **`_stop_watcher` 增加 `join(timeout=5)`**（taskpaw.py:2194-2197）— 消除了僵尸进程问题。
5. **`root.winfo_exists()` 检查**（taskpaw.py:2079, 2161）— 关闭窗口后不再向 Tk 派发无效任务。
6. **Lada 进度捕获机制**（taskpaw.py:709-800）— 新增了完整的 tqdm 输出解析和 Tk 输出窗口。

---

## 🎯 优先修复建议

### 第一优先级（立即修复）

1. **`save_config()` 原子写入** — 防止配置丢失
2. **`CustomCmdWatcher` 移除 `shell=True`** — 消除命令注入
3. **Hub `PRAGMA foreign_keys=ON`** — 启用外键约束
4. **Hub 日志修剪 SQL 修复** — 解决数据库无限膨胀

### 第二优先级（本周修复）

5. **HTTP API 增加认证或绑定 `127.0.0.1`** — 安全加固
6. **Hub 轮询漂移修复** — 使用 `time.monotonic()`
7. **删除 `dist/` 中的旧版本** — 防止误分发
8. **`macsubs.py` 增加状态锁** — 线程安全

### 第三优先级（后续优化）

9. 日志轮转、Bare except 清理、依赖版本锁定、IP/端口校验、事件去重持久化等。

---

## 📁 文件完整性检查

| 检查项 | 结果 |
|--------|------|
| 硬编码密码/API Key | ✅ 仅发现 `macsubs.py` 中的中文占位符 |
| `eval()` / `exec()` / `pickle` | ✅ 未使用 |
| 反序列化漏洞 | ✅ 未发现 |
| 路径遍历漏洞 | ⚠️ 用户可控路径无校验（中危） |
| SSRF | ⚠️ ComfyUI URL 用户可控，但仅限局域网（低危） |

---

## 📌 审计结论

TaskPaw V2 相比 V1 已经修复了导致程序假死的核心架构问题（线程、HTTP Server、进程查询），代码质量有明显提升。但当前仍有 **6 个严重问题未修复**，主要集中在：

- **配置安全**：原子写入、命令注入
- **数据层**：Hub 的外键、日志修剪、轮询漂移

**建议按上述优先级分批次修复，并在修复后清理 `dist/` 目录重新打包。**
