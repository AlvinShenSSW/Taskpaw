# TaskPaw V3 — 架构设计文档

**作者**: Claude (spec-planner 视角) — Opus 4.8
**日期**: 2026-06-27
**状态**: 草案 v2（已纳入 Codex 外门第 1 轮评审，15 条全部处置；待 Kimi 终审）
**目标读者**: 实施 V3 的开发者（人 或 implementation-pilot / afk）

---

## 0. 一句话目标

把 TaskPaw 从"AI 批处理任务完成通知器"升级为**通用的本地服务监控 + 可靠告警平台**，
界面采用 MDCx 同款 **Tauri 壳 + Web 前端**，并首先支撑一个真实新场景：
**监控 moomoo (MQT) 自动股票交易服务器**。

> **核心纪律（来自评审）**：先把*事件可靠投递、远程安全边界、moomoo 信号语义、插件生命周期*这四个契约定死，
> 再做"漂亮的壳和表单"。否则会先做出好看的 UI，回头发现告警链路和 moomoo preset 不可靠。
> 因此 **第一阶段 headless 优先，Tauri 壳后置**（见 §10）。

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

- 事件队列 + 单调 `id`（`taskpaw.py:195-272`），跨重启持久化到 `state.json`
- 可选 Bearer Token 鉴权，401 不清空队列
- 配置原子写 + `from_dict` 容错迁移
- Hub 的 SQLite + WAL + 7 天滚动清理
- `/ping /status /events` 这套 agent↔Hub 协议骨架

### 1.3 V2 的根本局限

1. **范式错配**：所有 Watcher 语义是"任务从运行到结束→通知完成"，无法表达"常驻服务是否健康"。
2. **加新监控类型摩擦极高**：要同改 `WatcherConfig`（`taskpaw.py:82-130`）、`WATCHER_CLASS_MAP`（`taskpaw.py:1707-1713`）、~700 行手写表单 `_open_watcher_editor()`（`taskpaw.py:2554-2835`）。
3. **god-object**：`LadaWatcher`(567 行)、`ComfyUIWatcher`(308 行)、`TaskPawApp`(1000+ 行)。
4. **UI 栈是瓶颈**：tkinter 手搓；ui-ux-pro-max 等工具用不上。
5. **事件投递不可靠**：`/events` 是"取走即清空"。Hub 拉到事件后、写库/转发 OpenClaw 前崩溃→事件**永久丢失**。对交易告警是阻断级缺陷。
6. **已知遗留 bug**：`taskpaw_hub.py` 写 status 文件处 `os` 未导入；`macsubs.py:35` OpenRouter key 是占位符。V3 一并消除。

---

## 2. V3 设计原则

1. **监控即插件**：每种监控是自描述插件（声明配置 schema + 生命周期 + 运行逻辑），注册到 registry。新增 = 写类 + 注册，不碰 UI 与核心数据结构。
2. **配置 schema 驱动 UI**：前端表单由插件 schema 自动生成；但**限定 schema 子集**（见 §4.3），不支持任意 JSON Schema。
3. **健康 + 事件双范式**：既支持"任务完成"，也支持"存活/健康/阈值/日志命中"。
4. **可靠投递优先**：事件有持久 outbox、cursor/ack、幂等键、重试、死信（见 §6）。"取走即清空"在 V3 native 协议中废止。
5. **消费而非重造**：被监控系统已有的心跳/告警/日志，agent 优先读取转发。
6. **安全边界显式**：桌面 UI 的 loopback 信任**不外推**到 agent 的跨机 API；远程暴露必须显式开启 + 鉴权（见 §3.2）。
7. **协议向后兼容**：V3 Hub 能继续轮询 V2 agent；事件 schema 加版本 + LegacyEventAdapter。
8. **agent 最小化**：交易服务器上的 agent 不携带 Hub 的 dashboard/聚合/OpenClaw token；两个产物共享 core（见 §7）。

---

## 3. 技术栈与安全模型

### 3.1 技术栈（对齐 MDCx）

| 层 | 技术 | 来源/理由 |
|---|---|---|
| 桌面壳（仅 Hub UI） | **Tauri v2** | 抄 `mdcx/src-tauri/`，锁死无 IPC |
| 前端 | **React 19 + Vite + MUI + TanStack Router/Query + Zustand** | MDCx 用 Rspack，V3 建议 **Vite**（更稳）；其余照抄 |
| 前后端通信 | 后端**同源** serve 前端 + REST `/api/v1` + WebSocket `/ws` | 抄 MDCx |
| 后端 | **Python FastAPI + uvicorn** | 复用现有 Python 监控逻辑 |
| 打包 | Tauri build（壳）+ PyInstaller（后端） | 抄 `mdcx/scripts/build.py` |
| 握手 | 后端 stdout 输出一行 readiness JSON，壳解析 | 抄 MDCx |

**MDCx 安全约束 → V3 验收标准**（不是"抄 MDCx"一句带过，逐条作为 #5 验收项）：
- `withGlobalTauri=false`，capabilities 为空，UI 无任何 Tauri IPC/FS 权限
- webview init script **仅在 loopback origin** 注入 api key；导航离开 loopback 即失效
- 后端 stdout **只**输出一行 readiness JSON；其余日志走 stderr，不污染握手
- api key 经 env 传入，**不走 argv、不进日志**
- 壳对后端 `base_url` 做 loopback 校验后才加载

### 3.2 网络与安全模型（评审 P1#3 —— 必须先定）

桌面壳的"loopback 天然安全"**只适用于 Hub 本机 UI**。agent 要被 Hub 跨机轮询，不能套用。
moomoo agent 跑在**生产交易机**上，暴露面风险最高。强制规则：

- **agent 默认只 bind `127.0.0.1`**；远程暴露需显式配置，且**只**开放 `/ping /status /events`，**不**带任何管理 UI / 写接口。
- **传输面三选一**（§9 决策点 2）：① Tailscale/WireGuard 私网 + per-agent Bearer；② **agent 主动 push 到 Hub 入站 webhook**（agent 完全不监听公网，最契合交易机）；③ mTLS。
- agent↔Hub 沿用 V2 Bearer；**移除 V2 的 `0.0.0.0` 绑定默认**。
- Hub 入站 webhook（若选 push 模式）必须：HMAC 签名 + 重放窗口（时间戳+nonce）+ 限流 + 幂等键 `(server_id, event_id)`。
- CORS 仅对 Hub 本机 UI 放开；agent 不开放 CORS。

---

## 4. 核心抽象：Monitor 插件系统

### 4.1 实例与生命周期（评审 P1#4 —— `run/health` 太薄，补 supervisor）

```python
class MonitorPlugin(ABC):
    type_id: str
    display_name: str
    category: Literal["task", "service"]

    @classmethod
    def config_model(cls) -> type[BaseModel]: ...   # Pydantic，服务端权威校验
    @classmethod
    def json_schema(cls) -> dict: ...               # 前端表单（由 model 导出）
    @classmethod
    def ui_schema(cls) -> dict: ...                  # 控件提示（见 §4.3）
    config_version: int                              # 配置迁移用

    @abstractmethod
    def create(self, cfg: BaseModel) -> "MonitorInstance": ...

class MonitorInstance(ABC):
    instance_id: str
    def start(self, emit: EventEmitter): ...          # 由 supervisor 调用
    def stop(self, timeout: float): ...
    def snapshot(self) -> MonitorStatus: ...          # 运行态健康（不是 health(cfg)）
    def reconfigure(self, cfg: BaseModel): ...
```

**Supervisor 负责**（核心，不在插件里）：线程崩溃自动重启（带退避上限）、`poll` 超时、启动失败上报、
事件统一过队列（带 `dedupe_key`、`severity`、`source_offset`）、backpressure、资源上限（见 §4.4）。
`emit(level, title, message, data, dedupe_key)` 是插件唯一出口；去重/持久化/限流由 supervisor + outbox（§6）保证。

### 4.2 V3 内置 Monitor 类型

| type_id | category | 用途 | 取代/新增 |
|---|---|---|---|
| `process` | service/task | 进程存活（名/PID/pm2 状态） | 合并 V2 Lada+Process 的重复 `_check_process` |
| `folder` | task | 文件稳定即完成 | 迁移 V2 |
| `comfyui` | task | ComfyUI 队列 | 迁移（拆 ErrorDetector） |
| `lada` | task | lada-cli 进度/完成 | 迁移（拆 managed/passive + ProgressParser） |
| `custom_cmd` | both | 跑命令看 exit code | 迁移 |
| **`heartbeat`** | service | 读 JSON 字段/文件 mtime，超时→告警 | 新增 |
| **`tail_jsonl`** | service | tail JSONL，按字段匹配（含 partial-line 容错） | 新增 |
| **`log_pattern`** | service | tail 文本，正则命中（限长+预编译） | 新增 |
| **`tcp_check`** | service | 探 host:port LISTEN | 新增 |
| **`http_health`** | service | GET 端点看状态码/JSON 字段 | 新增 |
| **`state_file`** | service | 监视 JSON 字段变化/阈值/mtime | 新增 |
| **`webhook_in`** | both | 暴露入站 webhook，被监控脚本主动 POST | 新增（push 模式） |

### 4.3 schema 驱动 UI 的边界（评审 P2#6）

不支持任意 JSON Schema 全量。约定每个插件提供四件套：
`pydantic model + 导出的 json_schema 子集 + ui_schema + config_version`。

- 渲染用 **RJSF + MUI**（或自建子集），仅支持：string/number/bool/enum/数组、条件显隐（`dependencies`）。
- **secret 字段**（token/密码）单独 `widget: password`，单独存储 + 脱敏，**不**随 `/status` 返回。
- 特殊控件：路径选择、正则在线测试、枚举动态加载，用 ui_schema `widget` 指定。
- 保存前服务端用 pydantic **二次校验**；错误结构化回显到对应字段。
- 配置升级走 `config_version` + 每插件 migration 函数。

### 4.4 资源与并发约束（评审 P2#11 —— 落到机制）

每个 monitor 配置统一含：`poll_interval / timeout / max_events_per_minute / max_line_bytes`。
- tail 类按 **inode + offset** 跟踪，处理日志轮转/inode 替换/慢磁盘；JSONL **忽略最后半行**直到下次补全。
- 正则**预编译** + 行长上限，防灾难性回溯。
- 事件风暴：超 `max_events_per_minute` 折叠为一条"N 起同类事件"摘要。
- agent 暴露自监控指标（各 monitor 存活、队列深度、丢弃计数）。

---

## 5. moomoo (MQT) 监控集成规格

**部署**：在 moomoo 交易服务器上跑 TaskPaw V3 agent（已确认）。agent **只读消费** MQT 已有产物，不改 MQT。
**信号路径**以 `moomoo/trading/paths.py` 为准（约 `~/mqt/runtime/`），全部做成可配。

> **评审 P1#2 / P2#5 修正**：经核实 `runtime/health_alert_state.json` 是 `{alert_type: timestamp}` 的
> **冷却 map**（实测 `{"anthropic_api": 1781322736.38926}`），**不是**告警事件日志，会因 cooldown 不更新而漏报、
> 多进程写同文件丢更新。**降级为旁路信号**。真正的告警/成交事件源是下表中的 jsonl 产物。

### 5.1 三层监控（按可靠性分层）

**第一层 · 基础存活**
| 项 | type_id | 配置 | 告警 |
|---|---|---|---|
| 编排器进程 | `process` | pm2 名 `orchestrator` 的 status（区分 online/errored/crash-loop/stopped） | 非 online |
| 编排器心跳 | `heartbeat` | `runtime/orchestrator_heartbeat.json`，按 `next_check_due_utc` **+ watchdog grace** 判定 | 超 due+grace 未更新 = HUNG |
| 冻结事件 | `tail_jsonl` | `runtime/freeze_incidents.jsonl`（watchdog 真实事件日志） | 新增行→告警 |

**第二层 · 交易可用性**
| 项 | type_id | 配置 | 告警/说明 |
|---|---|---|---|
| OpenD 端口 | `tcp_check` | `127.0.0.1:11111` | 非 LISTEN=交易瘫痪。**注意：LISTEN ≠ 已登录/已解锁/订阅可用**，仅作粗判 |
| OpenD 深度健康 | `tail_jsonl`/`state_file` | 优先消费 MQT health_monitor 分类（OPEND_DOWN/OPEND_KICKED/TRADE_AUTH） | 经 `health_alert_state.json` cooldown 旁路 + 实际日志交叉确认 |
| 上下文泄漏 | `tail_jsonl` | `orchestrator_log.jsonl` 中 `event=="runtime_health"` 行（**是 jsonl 事件，不是独立文件**），算 `ctx_created-ctx_closed` | >2 连续多周期 |

**第三层 · 交易事件**
| 项 | type_id | 配置 | 说明 |
|---|---|---|---|
| 确认成交 | `tail_jsonl` | `runtime/confirmed_fill_ledger.jsonl`（**真实成交**，不是 auto_decisions 的 `PLACED`） | `PLACED` 在 `executions[]` 内，仅"已提交/接受"，非确认成交 |
| 下单/跳过/止损 intent | `tail_jsonl` | `auto_decisions.jsonl`，区分 `PLACED` / `SKIPPED_PREFLIGHT_*` / 止损 intent | 区分语义，避免把"提交"当"成交" |
| 每日亏损熔断 | `state_file`/`tail_jsonl` | MQT `BRAIN_DAILY_LOSS_HALT` 信号 | 触发→高优告警 |

> MQT 自身已有 `pending_fill_notify.json` / `notified_fill_ids.json` 成交通知管线；
> agent 只 tail `confirmed_fill_ledger.jsonl` 做镜像通知，**不**写入 MQT 的通知状态文件，避免双写竞争。

### 5.2 远端可达性

交易机是远程 Linux。按 §3.2，**首选 agent 主动 push 到 Hub webhook**（交易机不监听公网）。
若用拉模式，需 Tailscale/VPN，agent 只 bind 私网网卡 + Bearer。§9 决策点 2 定。

---

## 6. 事件可靠投递与 schema v2（评审 P1#1 / P2#8 / P3#13）

### 6.1 投递契约（废止"取走即清空"）

- agent 侧：事件写**本地持久日志**（SQLite/append-only），保留游标；`/events?after=<cursor>` 返回 > cursor 的事件，**不删除**。
- Hub 侧：按 `(server_id, event_id)` **幂等**写入；处理成功后推进该 server 的 cursor（ack 语义）。
- OpenClaw 转发：经 Hub **outbox 表**（`delivery_state: pending/sent/failed`）+ 指数退避重试 + 死信；失败**不**丢事件、不只 log error。
- push 模式下：agent→Hub webhook 自带幂等键，Hub 同样 outbox 化。

### 6.2 事件 schema v2

```json
{
  "schema": "taskpaw.event/2",
  "id": 1234, "ts": "2026-06-27T12:00:00Z",
  "server": "moomoo-prod",
  "monitor": { "type": "heartbeat", "name": "orchestrator-hb" },
  "level": "alert",                 // info | warn | alert | done
  "title": "Orchestrator HUNG",
  "message": "next_check_due 12 min overdue",
  "dedupe_key": "moomoo-prod/orchestrator-hb/hung",
  "data": { "cycle_count": 8123 }
}
```

### 6.3 LegacyEventAdapter（兼容映射 —— V2 并非统一旧 schema）

实测两种旧格式不同，需分别映射：
- TaskPaw V2 `taskpaw.py`：`{id, time, machine, monitor, message}`
- MacSubs `macsubs.py`：`{id, time, stage, current_file, message}`

→ 统一映射到 v2（`machine/stage→server/monitor`，`message→message`，补 `level=info/done`，`schema` 缺失即判 legacy）。

### 6.4 Hub 存储与 OpenClaw sink

- Hub `events` 表新增列：`source_event_id, schema, level, title, payload_json, delivery_state`。
- `OpenClawSink`：**默认仍发 `{"text": rendered}`**（不假设 OpenClaw 能吃结构化）；可配置附带 `event` 字段或选 v1/v2；带能力探测。

---

## 7. 仓库结构（评审 P2#7 —— monorepo，但拆两个产物）

```
taskpaw-v3/                  # monorepo
├── core/                    # 共享：events/outbox/config/auth/protocol/schema
├── monitors/                # 插件 + registry + supervisor
├── agent/                   # 入口①: taskpaw-agent —— 最小化，默认无 UI，只 /ping /status /events(+push)
│   └── server/{app.py,launcher.py}
├── hub/                     # 入口②: taskpaw-hub —— 聚合/SQLite/OpenClaw/完整 Web UI
│   ├── server/{app.py,launcher.py}
│   ├── poller.py / store.py / openclaw.py
│   ├── src-tauri/           # Tauri 壳（仅 Hub）
│   └── ui/                  # React 前端（仅 Hub）
├── scripts/build.py         # 分别打包 agent / hub
└── docs/specs/
```

agent 产物**不含** Hub 的 dashboard/聚合/OpenClaw token/Tauri/前端 —— 交易机暴露面最小。

---

## 8. 迁移矩阵（评审 P2#10）

| 来源 | 内容 | 迁移动作 |
|---|---|---|
| V2 agent `APPDATA/TaskPaw/config.json` | watcher 数组 | 每旧类型 mapper → V3 `{type_id,name,config}` |
| V2 `state.json` | `_next_event_id` | 迁为 V3 agent 事件游标起点 |
| Hub `~/.taskpaw-hub/hub.db` | servers/events/config/last_event_ids | 表结构升级（加 §6.4 列），保留历史 |
| MacSubs | 伪 agent（无统一 config） | 显式登记为一个 server + 端口 |
| OpenClaw token / openclaw_enabled | Hub config | 平移 |
| 端口 | V2 agent 5678 / MacSubs 5679 | **V3 用不同默认端口或互斥启动**，防 V2/V3 并跑冲突 |

迁移器**先只读预览**（diff）后再写入。

---

## 9. 待 operator 决策点（评审后更新倾向）

1. **agent 与 Hub** → **倾向：monorepo + 两个产物**（评审 P2#7）。待确认。
2. **moomoo 远端可达性** → **倾向：agent push 到 Hub webhook**（交易机不监听公网，评审 P1#3）。待确认。
3. **前端 bundler**：Vite（推荐）vs Rspack。
4. **V2 去留**：tkinter 版冻结维护 vs V3 即弃用。
5. **issue 拆分**：按下面 §10 重排版本。

---

## 10. 实施路线（评审 P2#9 / P3#12 重排：先验证告警链路，Tauri 后置）

> 原序（壳→插件→新监控→事件→moomoo）会先固化错误的 emit API、写出无稳定语义的 monitor、moomoo 验证太晚。重排为：

- **#0 勘测与决策**：moomoo 信号实地勘测（确认各 jsonl/json 字段与路径）+ 敲定 §3.2 网络/安全模型 + §9 决策点。产出更新本文。
- **#1 协议与可靠投递**：事件 schema v2 + agent 持久事件日志 + cursor/ack + Hub 幂等 + OpenClaw outbox/重试/死信 + LegacyEventAdapter（§6）。
- **#2 headless 最小闭环**：FastAPI agent + Hub（**先无 Tauri、无花哨前端**），打通 push/poll → 入库 → OpenClaw。
- **#3 插件 supervisor + schema 子集**：`MonitorPlugin/MonitorInstance` + supervisor（崩溃重启/超时/资源上限）+ pydantic/json_schema/ui_schema（§4）。先迁 `process/heartbeat/tail_jsonl/tcp_check` 四种打通。
- **#4 moomoo preset + 端到端验证**：内置三层模板，在交易机（或 `moomoo_distill` 副本）实跑，制造 OpenD 关闭 / 心跳过期 / 成交，确认告警达 OpenClaw。
- **#5 UI / Tauri**：React + MUI（schema 驱动表单）+ Tauri 壳 + §3.1 安全验收项。每页先跑 ui-ux-pro-max `--design-system`。
- **#6 V2 迁移与剩余监控**：迁 folder/comfyui/lada/custom_cmd 为插件 + §8 迁移器 + 灰度切换。

依赖序：#0 → #1 → #2 → #3 → #4 →（#5 ∥ #6）。Tauri 不阻塞监控链路验证。

---

## 11. 测试计划

- **协议/可靠性**：cursor/ack、`(server_id,event_id)` 幂等、Hub 崩溃重启不丢事件、outbox 重试与死信、v1/v2 与 MacSubs 兼容映射、鉴权 401 不影响游标。
- **插件系统**：注册/发现、pydantic+json_schema 双校验、配置往返与 `config_version` 迁移、未知 type 容错、supervisor 崩溃重启/超时。
- **新监控单测**：heartbeat 用临时文件 mtime 模拟超时（含 grace）；tail_jsonl 喂 partial-line/轮转/inode 替换样本；log_pattern 正则限长；tcp_check 起临时 socket；state_file 模拟字段变化。
- **资源**：事件风暴折叠、max_line_bytes、正则回溯上限。
- **安全（评审 P3#14）**：HTTP/WS 鉴权、CORS/origin 拒绝非 loopback、token 不进日志、agent 远程禁 UI、secret 不入 `/status`、webhook HMAC+重放拒绝。
- **桌面壳**：Tauri readiness 失败页、后端子进程退出清理、WebSocket 重连、Playwright 基本冒烟。
- **迁移器**：V2 config / Hub DB / MacSubs / token 各样本断言；只读预览正确。
- **moomoo 实跑**：OpenD 关闭、心跳过期、`confirmed_fill_ledger` 新增、`freeze_incidents` 新增 → 告警送达。

---

## 12. 风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| 在生产交易机跑额外 agent 影响交易进程 | 高 | agent 只读、低频、资源上限、最小产物、默认 loopback；先在 distill 副本验证 |
| moomoo 内部产物格式随 MQT 演进漂移 | 中 | 只读 + 路径/字段全可配；优先消费稳定的 jsonl 事件日志；#0 勘测锁定 |
| Tauri/Rust 工具链是新增复杂度 | 中 | headless 优先（#2-#4 不依赖壳）；壳直接移植 MDCx |
| 事件可靠投递实现复杂 | 中 | #1 单独成 issue 先打牢，后续监控复用 |
| schema→UI 自动表单覆盖不全 | 低 | 限定 schema 子集 + ui_schema widget + 复杂控件自定义回退 |
| V2/V3 并跑端口/状态冲突 | 中 | 不同默认端口 + 迁移只读预览 + 互斥启动 |

---

## 13. 后续

评审通过 → 据 §9 决策更新本文 → 据 §10 建 issue（#0–#6）→ `/afk` 或 `/afk codex` 逐个自治实现 + 双评审。

---

## 附录 A · 评审记录

**Codex 外门（第 1 轮，2026-06-27，model_reasoning_effort=high）**
- 内置 `review`（代码 diff）：CLEAN —— 纯文档无可执行代码，无可操作正确性问题（预期）。
- 架构专项评审：15 条（P1×4 / P2×7 / P3×4），**逐条核实后全部成立**，已纳入 v2：
  - P1#1 事件可靠投递 → §1.2/§6 新增 outbox/cursor/ack/幂等/死信。
  - P1#2 `health_alert_state.json` 误读 → 实测确认是 cooldown map，§5.1 降级为旁路，改用 `freeze_incidents.jsonl` 等真实事件源。
  - P1#3 远程安全边界 → §3.2 新增网络/安全模型；P3#15 MDCx 安全约束转为 §3.1 验收标准。
  - P1#4 插件生命周期太薄 → §4.1 补 `MonitorInstance` + supervisor。
  - P2#5 moomoo 信号误报 → §5.1 三层重写（pm2 status / heartbeat+grace / confirmed_fill_ledger / runtime_health 为 jsonl 事件 / TCP LISTEN 仅粗判）。
  - P2#6 schema 边界 → §4.3 限定子集 + pydantic/ui_schema/secret。
  - P2#7 单产物双模式 → §7 拆两个产物。
  - P2#8 schema 兼容/存储 → §6.3 LegacyEventAdapter + §6.4 Hub 表列。
  - P2#9 issue 序 → §10 重排（headless 优先）。
  - P2#10 迁移 → §8 迁移矩阵。
  - P2#11 资源/并发 → §4.4 落机制。
  - P3#12 更简单第一阶段 → §10 headless 优先采纳。
  - P3#13 OpenClaw 双格式 → §6.4 OpenClawSink。
  - P3#14 测试安全/浏览器 → §11 补充。
- **Kimi 终审**：待运行。
