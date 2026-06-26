# TaskPaw V3 — 架构设计文档

**作者**: Claude (spec-planner 视角) — Opus 4.8
**日期**: 2026-06-27
**状态**: 草案，待 operator 评审
**目标读者**: 明天开始实施 V3 的开发者（人 或 implementation-pilot / afk）

---

## 0. 一句话目标

把 TaskPaw 从一个"AI 批处理任务完成通知器"升级为一个**通用的本地服务监控 + 告警平台**，
界面采用 MDCx 同款 **Tauri 壳 + Web 前端**，并首先支撑一个真实新场景：
**监控 moomoo (MQT) 自动股票交易服务器**。

---

## 1. 背景与现状

### 1.1 V2 是什么（三层架构）

```
被监控机 (taskpaw.py agent, :5678)            Mac Mini
  ├─ 本地轮询 Lada / ComfyUI / 文件夹 / 进程 ──HTTP poll──► Hub (taskpaw_hub.py)
  └─ REST: /ping /status /events                            ├─ SQLite 历史
                                                            └─ POST ──► OpenClaw (:18789)
MacSubs (macsubs.py, :5679) 同协议 ──poll────────────────────┘
```

### 1.2 V2 值得保留的资产

- 事件队列 + 单调 `id` 去重（`taskpaw.py:195-272`），跨重启持久化到 `state.json`
- 可选 Bearer Token 鉴权，401 不清空队列（防攻击者冲走事件）
- 配置原子写 + `from_dict` 容错迁移（未知字段过滤）
- Hub 的 SQLite + WAL + 7 天滚动清理
- `/ping /status /events` 这套 agent↔Hub 协议本身是干净、可扩展的

### 1.3 V2 的根本局限（"勉强可用、场景受限"的根因）

1. **范式错配**：所有 Watcher 语义都是"任务从运行到结束 → 通知完成"。无法表达"一个常驻服务现在是否健康"。
2. **加新监控类型摩擦极高**：要同时改 `WatcherConfig` dataclass 硬编码字段（`taskpaw.py:82-130`）、`WATCHER_CLASS_MAP` 静态字典（`taskpaw.py:1707-1713`）、以及 ~700 行手写表单 `_open_watcher_editor()`（`taskpaw.py:2554-2835`）。
3. **god-object**：`LadaWatcher`(567 行，managed/passive 两套几乎不共享)、`ComfyUIWatcher`(308 行状态机)、`TaskPawApp`(1000+ 行)。
4. **UI 栈是瓶颈**：tkinter 手搓界面，是"勉强可用"体感的主要来源；且 ui-ux-pro-max 等设计工具完全用不上（它面向 Web/移动端）。
5. **已知遗留 bug**：`taskpaw_hub.py` 写 status 文件处 `os` 未导入（写 status.md 会崩）；`macsubs.py:35` OpenRouter key 是中文占位符。V3 重写时一并消除。

---

## 2. V3 设计原则

1. **监控即插件**：每种监控类型是一个自描述插件（声明自己的配置 schema + 运行逻辑），注册到 registry。新增类型 = 写一个类 + 注册，不碰 UI、不碰核心 dataclass。
2. **配置 schema 驱动 UI**：前端表单由插件 schema 自动生成，杜绝手写 700 行对话框。
3. **健康 + 事件双范式**：既支持"任务完成"（批处理），也支持"存活/健康/阈值/日志命中"（常驻服务）。
4. **消费而非重造**：被监控系统（如 MQT）已有的心跳/告警/日志，agent 优先读取转发，不重复实现监控逻辑。
5. **协议向后兼容**：V3 Hub 能继续轮询 V2 agent 的 `/status /events`；事件 schema 加版本号平滑演进。
6. **Web 优先 UI，桌面套壳**：抄 MDCx 的 Tauri + 同源 FastAPI 模式，一套前端同时服务桌面壳和局域网浏览器。

---

## 3. V3 技术栈（对齐 MDCx）

| 层 | 技术 | 来源/理由 |
|---|---|---|
| 桌面壳 | **Tauri v2** (Rust) | 抄 `mdcx/src-tauri/`，锁死无 IPC，只靠后端 HTTP；二进制小 |
| 前端 | **React 19 + Vite + MUI + TanStack Router/Query + Zustand** | MDCx 用 Rspack，V3 建议 **Vite**（更成熟、社区大）；其余照抄 |
| 前后端通信 | 后端**同源** serve 前端静态 + REST `/api/v1` + WebSocket `/ws` 实时推送 | 抄 MDCx；CORS 因 loopback 天然安全 |
| 后端 | **Python FastAPI + uvicorn** | 复用现有 Python 监控逻辑；agent 与 Hub 都是 FastAPI app |
| 打包 | Tauri build (壳) + PyInstaller (后端) | 抄 `mdcx/scripts/build.py` |
| 握手 | 后端 stdout 输出一行 `{"event":"ready","base_url":...,"ws_url":...}`，壳解析 | 抄 MDCx readiness 契约 |
| 鉴权 | 启动随机生成 API key，壳通过 webview init script 注入 localStorage（仅 loopback） | 抄 MDCx；保留 V2 的 Bearer 用于 agent↔Hub |

> **说明**：V3 的 agent 和 Hub 共用同一个 FastAPI + React 代码库，通过"角色"区分（agent 模式只跑监控+暴露 `/events`；Hub 模式跑聚合+转发+完整 dashboard）。也可拆两个产物，明天评审时定（见 §9 决策点）。

---

## 4. 核心抽象：Monitor 插件系统

### 4.1 插件接口（替代 V2 的 BaseWatcher + WatcherConfig 硬编码）

```python
class MonitorPlugin(ABC):
    type_id: str                       # "heartbeat" / "log_pattern" / "tcp_check" ...
    display_name: str
    category: Literal["task", "service"]   # 批处理 vs 常驻服务

    @classmethod
    @abstractmethod
    def config_schema(cls) -> dict:    # JSON Schema，前端据此自动生成表单
        ...

    @abstractmethod
    def run(self, cfg: dict, emit: EventEmitter, stop: threading.Event):
        # 轮询循环；通过 emit(level, message, data) 发事件；stop.wait(interval)
        ...

    def health(self, cfg: dict) -> MonitorStatus:   # 当前健康快照（给 /status）
        ...
```

注册：

```python
@register_monitor
class HeartbeatMonitor(MonitorPlugin): ...
```

`registry` 取代 `WATCHER_CLASS_MAP`；`config_schema()` 取代 `WatcherConfig` 的硬编码字段 + 手写表单。
配置存储改为 `{"type_id": "...", "name": "...", "config": { ...任意插件字段... }}`，核心不再认识具体字段。

### 4.2 V3 内置 Monitor 类型

| type_id | category | 用途 | 取代/新增 |
|---|---|---|---|
| `process` | service/task | 进程存活（按名/PID） | 合并 V2 Lada+Process 的 `_check_process` 重复逻辑 |
| `folder` | task | 文件稳定即完成 | 迁移 V2 FolderWatcher |
| `comfyui` | task | ComfyUI 队列 | 迁移 V2（拆出 ErrorDetector） |
| `lada` | task | lada-cli 进度/完成 | 迁移 V2（拆 managed/passive + ProgressParser） |
| `custom_cmd` | both | 跑命令看 exit code | 迁移 V2 |
| **`heartbeat`** | service | 读 JSON/文件 mtime，超时未更新→告警 | **新增**（MQT 核心需求） |
| **`log_pattern`** | service | tail 文件，正则/JSON 字段命中→事件 | **新增** |
| **`tcp_check`** | service | 探 host:port 是否 LISTEN | **新增**（OpenD 11111） |
| **`http_health`** | service | GET 健康端点，看状态码/JSON 字段 | **新增** |
| **`state_file`** | service | 监视 JSON 状态文件特定字段变化/阈值 | **新增**（health_alert_state.json、bucket_state.json） |
| **`webhook_in`** | both | 暴露入站 webhook，被监控脚本主动 POST 事件 | **新增**（push 模式，最灵活） |

---

## 5. moomoo (MQT) 监控集成规格

**部署方式**：在 moomoo 交易服务器上跑 TaskPaw V3 agent（已确认）。agent **只消费 MQT 已有信号**，不改 MQT 代码。

### 5.1 推荐监控配置（开箱即用模板 "moomoo preset"）

| 监控项 | type_id | 关键配置 | 告警条件 |
|---|---|---|---|
| 编排器存活 | `process` | match `strategy_orchestrator.py` | 进程消失 |
| 编排器心跳 | `heartbeat` | file `runtime/orchestrator_heartbeat.json`, field `next_check_due_utc` | 超 now+10min 未更新 = HUNG |
| OpenD 网关 | `tcp_check` | `127.0.0.1:11111` | 非 LISTEN = 交易瘫痪 |
| 成交事件 | `log_pattern` | tail `runtime/auto_decisions.jsonl`, json `status=="PLACED"` | 命中→推送"已下单" |
| 错误/止损 | `log_pattern` | tail `runtime/orchestrator_log.jsonl`, `event=="connection_error"` 或止损关键词 | 命中→告警 |
| MQT 自身告警 | `state_file` | watch `runtime/health_alert_state.json` 新增告警键 | 任意新告警→转发（OPEND_DOWN/PM2_CRASH/LLM_PROVIDERS_EXHAUSTED 等） |
| 上下文泄漏 | `log_pattern` | `runtime/runtime_health` 行，计算 `ctx_created-ctx_closed` | >2 连续多周期 |

> MQT 的 `health_monitor.py` 已定义十余种告警并写入 `health_alert_state.json`（带冷却）。
> V3 用一个 `state_file` 监控订阅这个文件就能"白嫖"全部告警语义，再经 Hub 转给 OpenClaw。
> 路径以 `moomoo/trading/paths.py` 为准（可能是 `~/mqt/runtime/`），配置里做成可填。

### 5.2 远端可达性（Hub 视角）

moomoo 服务器是 Linux 远程机。agent 在其上暴露 `/events`，Hub 跨网轮询。
若服务器在公网/异网段：评审时定走 VPN/反向隧道/还是 Hub 改"被 agent push"（见 §9）。

---

## 6. 事件 schema v2 与 OpenClaw 契约

V2 给 OpenClaw 发的是裸 `{"text": "..."}`，无版本、无结构。V3 改为：

```json
{
  "schema": "taskpaw.event/2",
  "id": 1234,
  "ts": "2026-06-27T12:00:00Z",
  "server": "moomoo-prod",
  "monitor": { "type": "heartbeat", "name": "orchestrator-hb" },
  "level": "alert",                     // info | warn | alert | done
  "title": "Orchestrator HUNG",
  "message": "next_check_due 12 min overdue",
  "data": { "cycle_count": 8123 }       // 插件自定义结构化负载
}
```

- Hub 转发时保留结构，OpenClaw 可按 `level`/`monitor.type` 路由。
- 兼容：Hub 收到无 `schema` 字段的 V2 事件时按旧格式处理。

---

## 7. 仓库结构（V3）

```
taskpaw-v3/
├── src-tauri/                 # Tauri 壳（抄 mdcx/src-tauri 改名）
│   ├── tauri.conf.json
│   ├── src/lib.rs             # spawn 后端 + 解析 readiness + 注入 apiKey
│   └── ui-shell/index.html    # loading 屏
├── ui/                        # React 前端
│   ├── vite.config.ts
│   └── src/{routes,client,store,hooks}/
├── taskpaw/                   # Python 后端（agent + hub 共用）
│   ├── server/{app.py,launcher.py}      # FastAPI + readiness JSON
│   ├── monitors/              # 每个插件一个文件 + registry.py
│   ├── core/{events.py,config.py,auth.py}   # 复用 V2 事件队列/鉴权/原子写
│   └── hub/{poller.py,store.py,openclaw.py}  # 仅 Hub 角色加载
├── scripts/build.py           # PyInstaller 打包（抄 mdcx）
└── docs/specs/                # 设计文档
```

---

## 8. 迁移与兼容

1. **协议兼容优先**：先让 V3 Hub 能轮询现存 V2 agent（不破坏在用的 Lada/ComfyUI 机器）。
2. **配置迁移器**：读 V2 `config.json`（`WatcherConfig` 数组）→ 转成 V3 `{type_id, name, config}`。每种旧类型写一个 mapper。
3. **灰度**：V3 agent 先在 moomoo 这台新机上跑（绿地，无历史包袱），验证插件系统 + 新监控类型；ComfyUI/Lada 机器后续再换。

---

## 9. 待 operator 决策点（明天评审先定）

1. **agent 与 Hub：单产物双模式 还是 两个产物？**（影响打包与代码组织）
2. **moomoo 远端可达性**：Hub 拉（需网络可达/隧道）还是 agent 推（Hub 加入站 webhook）？
3. **前端 bundler**：Vite（推荐，稳）还是照搬 MDCx 的 Rspack（快、统一）？
4. **V2 是否保留**：tkinter 版冻结维护，还是 V3 出来即弃用？
5. **issue 拆分粒度**：按下面 §10 的 6 个 issue，还是合并/再拆？

---

## 10. 实施路线（建议拆成 GitHub issue，供 /afk 逐个自治推进）

> afk 是 issue 驱动、瀑布式、带 CTO + Codex 外门 + Kimi 终审双评审。每个 issue 独立一条分支。

- **#1 脚手架**：Tauri 壳 + React + FastAPI 同源 + readiness 握手 + apiKey 注入（移植 MDCx 模板，跑通空壳）。
- **#2 Monitor 插件系统**：`MonitorPlugin` ABC + registry + JSON Schema 配置 + 前端 schema 驱动表单。先迁 `process` 一种打通闭环。
- **#3 迁移现有监控**：folder / comfyui / lada / custom_cmd 迁为插件（拆 god-object，去重 `_check_process`）+ V2 配置迁移器。
- **#4 新监控类型**：heartbeat / log_pattern / tcp_check / http_health / state_file / webhook_in。
- **#5 事件 schema v2 + Hub/OpenClaw 契约**：结构化事件 + 版本兼容 + Hub 转发更新 + status 文件 bug 修复。
- **#6 moomoo preset + 端到端验证**：内置 moomoo 监控模板，在交易服务器上实跑，验证 7 项信号 → OpenClaw 告警链路。

依赖序：#1 → #2 → (#3 ∥ #4) → #5 → #6。

---

## 11. 测试计划要点

- **插件系统**：注册/发现、schema 校验、配置往返序列化、未知 type 容错。
- **新监控单测**：heartbeat 用临时文件 mtime 模拟超时；log_pattern 喂样本 jsonl 断言命中；tcp_check 起临时 socket；state_file 模拟 health_alert_state.json 新增键。
- **事件管线**：去重、单调 id 跨重启、v2/v1 兼容、鉴权 401 不清队列。
- **迁移器**：V2 config.json 样本 → V3 配置，逐类型断言。
- **集成冒烟**：壳启动 → 后端 readiness → 前端连通 → 加一个 process 监控 → 触发事件 → /events 可取。
- **moomoo 实跑**：在交易服务器（或 `moomoo_distill` 测试副本）上，制造 OpenD 关闭 / 心跳过期，确认告警送达。

---

## 12. 风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| Tauri/Rust 工具链对当前以 Python 为主的项目是新增复杂度 | 中 | 直接移植 MDCx 已跑通的壳；Web-only 模式可不依赖壳先开发 |
| moomoo 监控误读 MQT 内部格式（路径/字段随 MQT 演进漂移） | 中 | 只读 + 路径/字段全部可配；优先消费稳定的 health_alert_state.json |
| 在生产交易机上跑额外 agent 影响交易进程 | 高 | agent 只读、低频、资源上限；先在 distill/测试副本验证 |
| 插件 schema→UI 自动表单覆盖不了复杂控件 | 低 | schema 支持 `widget` 提示；复杂类型回退自定义组件 |
| V3 重写工期 | 中 | 绿地灰度（先只上 moomoo），V2 继续服务存量机器 |

---

## 13. 后续

评审通过 → 据 §9 决策更新本文 → 据 §10 建 6 个 issue → `/afk`（Claude 驱动）或 `/afk codex` 逐个自治实现 + 双评审。
UI 实现阶段对每个页面先跑 `python3 skill/ui-ux-pro-max/scripts/search.py "<query>" --design-system` 拿设计系统。
