# TaskPaw V3 — 架构设计文档

**作者**: Claude (spec-planner 视角) — Opus 4.8
**日期**: 2026-06-27
**状态**: 草案 v3（已纳入 Codex 外门 15 条 + Kimi 终审 15 条 + 文件实证；待 operator brief 确认 #0 三项后定稿）
**目标读者**: 实施 V3 的开发者（人 或 implementation-pilot / afk）

---

## 0. 一句话目标

把 TaskPaw 从"AI 批处理任务完成通知器"升级为**通用的本地服务监控 + 可靠告警平台**，
界面采用 MDCx 同款 **Tauri 壳 + Web 前端**，并首先支撑一个真实新场景：
**监控 moomoo (MQT) 自动股票交易服务器**。

> **核心纪律（来自评审）**：先把*事件可靠投递、远程安全边界、moomoo 信号语义、插件生命周期*这四个契约定死，
> 再做"漂亮的壳和表单"。否则会先做出好看的 UI，回头发现告警链路和 moomoo preset 不可靠。
> 因此 **第一阶段 headless 优先，Tauri 壳后置**（见 §10）。
>
> **moomoo 范围（operator 明确）**：只监控**四项生命体征存活**（pm2 守护进程 / orchestrator 进程 / OpenD 网关 / orchestrator 心跳）。
> 交易层监测 MQT 自身已完善，**TaskPaw 不介入**。这大幅简化了 moomoo preset（仅需 process+heartbeat+tcp_check 三种插件，见 §5）。

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
9. **跨平台 agent**：agent 必须同时支持 **macOS 与 Windows**（V2 是 Windows 本位）。进程/系统信息走 `psutil`（已在依赖），路径/进程管理器/打包按平台分支（见 §3.0）。Hub 固定 macOS。

---

## 3. 技术栈与安全模型

### 3.0 部署拓扑（operator 明确）

```
                    ┌─────────────────── Mac（母体）─────────────────┐
                    │  TaskPaw Hub（聚合 + SQLite + Web UI/Tauri）    │
                    │       └── POST ──► OpenClaw (:18789)            │
                    └───▲───────────▲──────────────▲─────────────────┘
        agent(Windows) │           │ agent(macOS)  │ agent(macOS，可与 Hub 同机)
        Lada / ComfyUI │           │ moomoo 四体征 │ MacSubs(迁移后)
```

- **Hub 固定在 macOS**（母体信息接收端）。
- **agent 跨平台**：可在 macOS 或 Windows。当前实例：moomoo=macOS、Lada/ComfyUI=Windows。
- **agent 可与 Hub 同机**（loopback，如 MacSubs 现状 / moomoo 若在同一台 Mac）**或异机**（如 Windows 机器 → Mac Hub）。两种都要支持，决定走 loopback 还是远程鉴权/ push（见 §3.2）。
- **moomoo 实勘（本机）**：经 `pm2 list`/`ps` 确认本机 moomoo 由 **pm2 管理**（PM2 God Daemon → `orchestrator` online），OpenD 是 **`moomoo_OpenD.app`**（非 Linux `./OpenD` 二进制）。→ 体征③ 用 **tcp 11111 探测**（跨平台稳），体征①② 的进程管理器/进程名**做成可配**（本机 pm2；他机可能 launchd），#0 逐机确认。

### 3.1 技术栈（对齐 MDCx）

| 层 | 技术 | 来源/理由 |
|---|---|---|
| 桌面壳（仅 Hub UI） | **Tauri v2** | 抄 `mdcx/src-tauri/`，锁死无 IPC |
| 前端 | **React 19 + Vite + MUI + TanStack Router/Query + Zustand** | MDCx 用 Rspack，V3 建议 **Vite**（更稳）；其余照抄 |
| 前后端通信 | 后端**同源** serve 前端 + REST `/api/v1` + WebSocket `/ws` | 抄 MDCx |
| 后端 | **Python FastAPI + uvicorn** | 复用现有 Python 监控逻辑 |
| 打包 | **Hub**（macOS）：`cargo tauri build` + PyInstaller。**agent**：PyInstaller，**macOS + Windows 双产物**（无壳、headless） | **需自建 `scripts/build.py`**（跨平台 agent + Hub）。MDCx 的 `build.py` 实为 PyInstaller 打包 `main.py --onefile`，只能参考流程不能照抄（Kimi P2#5） |
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

**push 模式 bootstrap（评审 Kimi P1#4）**：
- `server_id` 由 operator 在 agent 配置文件中**显式指定**（如 `moomoo-prod`），并在 Hub 注册表**预登记**；Hub 拒绝未登记的 server_id。
- `hub_url` + HMAC 密钥经 agent 本地配置文件/env 下发，**不走 argv、不进日志**。
- HMAC 密钥支持轮换：**双密钥宽限期**（新旧并存一段时间）。
- 首次 push 时 Hub 校验 `server_id + HMAC` 才接收。

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

**Supervisor 契约**（核心，不在插件里。评审 Kimi P1#3 —— 接口/崩溃检测此前未定义）：

```python
class EventEmitter(Protocol):
    def __call__(self, level: str, title: str, message: str,
                 data: dict | None = None, dedupe_key: str | None = None): ...

class Supervisor:
    def register(self, plugin: MonitorPlugin, cfg: BaseModel): ...
    def on_instance_error(self, instance_id: str, exc: Exception): ...  # 实例内部捕获异常后回调
    def snapshot(self) -> dict: ...      # 各实例存活/队列深度/丢弃计数
```

- **线程模型**：每个 `MonitorInstance` 跑独立 `threading.Thread`；supervisor 守护线程按 `is_alive()` 检测线程已死（业务异常通常只杀线程、不通知 supervisor）。
- **退避**：指数退避，最小 5s、最大 5min；连续失败 5 次进入 `DEGRADED` 并发告警。
- **reconfigure 语义**：默认 `stop()` → 替换配置 → `create()`+`start()`；实例声明支持热更新时由实例自行处理。
- **事件出口**：`emit(...)` 是插件唯一出口；去重（`dedupe_key`）/持久化/限流由 supervisor + outbox（§6）保证。

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
- **secret 字段**（token/密码）单独 `widget: password`，单独加密/隔离存储。`/status` 与配置摘要中以 `"***"` 占位（评审 Kimi P2#7）；编辑表单经独立 `GET /api/v1/monitors/{id}/config` 取完整配置；保存时若回传仍是 `***` 则**保留原值**，避免误存占位符。
- 特殊控件：路径选择、正则在线测试、枚举动态加载，用 ui_schema `widget` 指定。
- 保存前服务端用 pydantic **二次校验**；错误结构化回显到对应字段。
- 配置升级走 `config_version` + 每插件 migration 函数。

### 4.4 资源与并发约束（评审 P2#11 —— 落到机制）

每个 monitor 配置统一含以下参数（默认值，评审 Kimi P3#13）：

| 参数 | 默认 | 说明 |
|---|---|---|
| `poll_interval` | 10s | 最小 1s |
| `timeout` | 30s | 命令/HTTP 探针超时 |
| `max_events_per_minute` | 60 | 超出折叠为"N 起同类"摘要 |
| `max_line_bytes` | 1MB | tail 单行上限 |

- tail 类按 **inode + offset** 跟踪，处理日志轮转/inode 替换/慢磁盘；JSONL **忽略最后半行**直到下次补全。
- 正则**预编译** + 行长上限，防灾难性回溯。
- 事件风暴：超 `max_events_per_minute` 折叠为一条"N 起同类事件"摘要。
- agent 暴露自监控指标（各 monitor 存活、队列深度、丢弃计数）。

---

## 5. moomoo (MQT) 监控集成规格

**范围（operator 明确）**：moomoo 只做**四项生命体征存活监控**。交易层监测（成交/盈亏/止损/熔断）MQT 自身已很完善，**TaskPaw 不介入**。
**部署**：在 moomoo 交易服务器上跑 TaskPaw V3 agent。agent **只读** MQT 产物，不改 MQT。
**信号路径**以 `moomoo/trading/paths.py` 为准（约 `~/mqt/runtime/`），全部可配。

### 5.1 四项生命体征（唯一监控范围）

| # | 体征 | type_id | 配置 | 告警 |
|---|---|---|---|---|
| ① | **进程管理器守护存活** | `process` | 进程管理器 daemon（本机 pm2：`pm2 ping`/God Daemon 进程；他机若 launchd 则 `launchctl`）。**管理器类型可配** | 不在 = 体系失去自愈根，所有进程不再自动重启 |
| ② | **orchestrator 进程在跑** | `process` | 进程名/管理器作业名（**可配**，默认 `orchestrator`，#0 确认本机 `ecosystem.config.js` 或 launchd 作业名）status | 非 online（errored/crash-loop/stopped） |
| ③ | **OpenD 网关在线** | `tcp_check` | `127.0.0.1:11111` LISTEN | 非 LISTEN = 交易瘫痪 |
| ④ | **orchestrator 心跳新鲜** | `heartbeat` | `runtime/orchestrator_heartbeat.json`，按 `next_check_due_utc` **+ watchdog grace** 判定（实测 `-rw-r--r--`，可读） | 超 due+grace 未更新 = HUNG（编排器卡死但进程还在） |

说明：
- ①②是进程检查、③是端口探测、④读全局可读的心跳文件 → **无权限障碍**（之前的 `rw-------` 顾虑只涉及交易层文件，现已不在范围内）。
- **跨平台/跨机注意**：OpenD 在 macOS 是 `moomoo_OpenD.app`、Linux 是 `./OpenD` 二进制，进程名不同 → ③ 用 **tcp 探测**而非进程名最稳。进程管理器本机是 pm2，他机可能是 launchd → ①② 把"管理器类型 + 作业名"做成配置项。
- 四项中 ①②④ 用 `process`/`heartbeat`，③ 用 `tcp_check`；**moomoo preset 只需 process + heartbeat + tcp_check 三种插件**。
- ④ 与 ② 互补：进程在跑 ≠ 在干活；心跳过期能抓"进程活着但卡死"。

### 5.2 部署与可达性

moomoo agent 跑在 **macOS**（非 Linux）。两种情形（§3.0）：
- **与 Hub 同机**（moomoo 若就在 Hub 那台 Mac）：loopback，最简单，无需远程鉴权。
- **异机**（独立 Mac）：按 §3.2，agent push 到 Hub webhook 或 Tailscale/VPN + Bearer。

Windows 上的 Lada/ComfyUI agent 则是异机 → Mac Hub，走远程鉴权或 push。§9 决策点 2 统一定。
moomoo 是否与 Hub 同机，由 #0 确认。

---

## 6. 事件可靠投递与 schema v2（评审 P1#1 / P2#8 / P3#13）

### 6.1 投递契约（废止"取走即清空"）

- agent 侧：事件写**本地持久日志**（SQLite/append-only），保留游标；`/events?after=<cursor>` 返回 > cursor 的事件，**不删除**。
- Hub 侧：按 `(server_id, event_id)` **幂等**写入；处理成功后推进该 server 的 cursor（ack 语义）。
- OpenClaw 转发：经 Hub **outbox 表**（`delivery_state: pending/sent/failed/dead_letter`）+ 指数退避重试 + 死信；失败**不**丢事件、不只 log error。
- push 模式下：agent→Hub webhook 自带幂等键，Hub 同样 outbox 化。

**死信队列（评审 Kimi P2#8）**：
- 判定：连续 10 次失败 **或** 超 24h 未投递成功 → `delivery_state=dead_letter`。
- 表列：`event_id, server_id, payload_json, failed_at, last_error, delivery_state`。
- 通知 operator：死信本身生成一条高优本地告警（绕过 OpenClaw，避免"告警链路坏了却用它报告"）。
- 保留 7 天（可配）。

**V2 / MacSubs 兼容策略（评审 Kimi P1#2 —— cursor/ack 与旧"取走即清空"冲突）**：
V2 agent（`taskpaw.py:262-267`）与 MacSubs（`macsubs.py:123-128`）的 `/events` 是 `get_and_clear`，**无 cursor 接口**，Hub 无法 ack。明确分级：
- **短期**：V2/MacSubs 视为"不可靠旧源"，Hub 仍 best-effort 接收（不保证 exactly-once）；**V3 新监控（含 moomoo）必须用 V3 agent**，享受 cursor/ack。
- **中期**：为 V2 agent 写只读**桥接进程**，把内存队列转成 V3 cursor/ack 协议。
- 文档明确不对旧源承诺可靠投递；交易告警等关键链路一律走 V3 agent。

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

统一映射到 v2（评审 Kimi P3#15 补全规则）：
- `machine/stage → server`，`monitor → monitor`，`current_file → data.current_file`（MacSubs 字段不丢）。
- `title` = `monitor` + message 首句；`dedupe_key` = `hash(server/monitor/message)`。
- `level`：任务完成→`done`，错误→`alert`，其余→`info`。
- `schema` 缺失即判 legacy。

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

**agent 配置模型（评审 Kimi P2#9 —— agent 无 UI，配置需明确）**：
- 本地配置文件启动（跨平台路径）：macOS `~/Library/Application Support/TaskPaw/agent.yaml`、Windows `%APPDATA%/TaskPaw/agent.yaml`、Linux `/etc/taskpaw/agent.yaml`。
- 字段：`server_id`、`hub_url`（push 模式）、`api_token`/HMAC、`mode: push|poll`、`monitors[]`。
- `server_id` 由 operator 显式指定并在 Hub 预登记（见 §3.2 bootstrap）。
- 配置经 SSH/Ansible/运维脚本下发；agent **不**提供远程管理接口。

---

## 8. 迁移矩阵（评审 P2#10）

| 来源 | 内容 | 迁移动作 |
|---|---|---|
| V2 agent `APPDATA/TaskPaw/config.json`（仅 Windows） | watcher 数组 | 每旧类型 mapper → V3 `{type_id,name,config}`；V3 agent 跨平台，新机用 §7 平台路径 |
| V2 `state.json` | `_next_event_id` | 迁为 V3 agent 事件游标起点 |
| Hub `~/.taskpaw-hub/hub.db` | servers/events/config/last_event_ids | 表结构升级（加 §6.4 列），保留历史 |
| MacSubs | 伪 agent（无统一 config） | 显式登记为一个 server + 端口 |
| OpenClaw token / openclaw_enabled | Hub config | 平移 |
| 端口 | V2 agent 5678 / MacSubs 5679 | **V3 agent 默认 5680**，与 V2/MacSubs 不冲突；启动时端口占用检测，若 V2 在跑则退出并提示迁移（Kimi P3#14） |

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
- **#1 协议与可靠投递**：事件 schema v2 + agent 持久事件日志 + cursor/ack 单测 + Hub 幂等 + OpenClaw outbox/重试/死信单测 + LegacyEventAdapter（§6）。
- **#2 headless 最小闭环**：FastAPI agent + Hub（**先无 Tauri、无花哨前端**）+ 鉴权 + cursor/ack 端到端 + push/poll 双模式打通 → 入库 → OpenClaw。
- **#2.5 安全验收**（评审 Kimi P2#10 —— 独立 issue 防遗漏）：覆盖 §3.1/§3.2 全部安全项（loopback 校验、token 不进日志、CORS/origin 拒绝、agent 远程禁 UI、secret 不入 `/status`、webhook HMAC+重放拒绝、push bootstrap 校验）。
- **#3 插件 supervisor + schema 子集**：`MonitorPlugin/MonitorInstance` + Supervisor 契约（§4.1：崩溃重启/超时/退避/DEGRADED）+ pydantic/json_schema/ui_schema + 资源上限（§4.4）。先做 moomoo 所需的 **`process` / `heartbeat` / `tcp_check`** 三种打通。
- **#4 moomoo preset + 端到端验证**：内置四项生命体征模板（§5.1），在交易机（或 `moomoo_distill` 副本）实跑，制造 pm2 停 / orchestrator 崩 / OpenD 关闭 / 心跳过期，确认告警达 OpenClaw。
- **#5 UI / Tauri**：React + MUI（schema 驱动表单）+ Tauri 壳 + §3.1 安全验收项。每页先跑 ui-ux-pro-max `--design-system`。
- **#6 V2 迁移与剩余监控**：迁 folder/comfyui/lada/custom_cmd 为插件 + §8 迁移器 + 灰度切换。

依赖序：#0 → #1 → #2 → #2.5 → #3 → #4 →（#5 ∥ #6）。Tauri 不阻塞监控链路验证。

---

## 11. 测试计划

- **协议/可靠性**：cursor/ack、`(server_id,event_id)` 幂等、Hub 崩溃重启不丢事件、outbox 重试与死信、v1/v2 与 MacSubs 兼容映射、鉴权 401 不影响游标。
- **插件系统**：注册/发现、pydantic+json_schema 双校验、配置往返与 `config_version` 迁移、未知 type 容错、supervisor 崩溃重启/超时。
- **新监控单测**：heartbeat 用临时文件 mtime 模拟超时（含 grace）；tail_jsonl 喂 partial-line/轮转/inode 替换样本；log_pattern 正则限长；tcp_check 起临时 socket；state_file 模拟字段变化。
- **资源**：事件风暴折叠、max_line_bytes、正则回溯上限。
- **安全（评审 P3#14）**：HTTP/WS 鉴权、CORS/origin 拒绝非 loopback、token 不进日志、agent 远程禁 UI、secret 不入 `/status`、webhook HMAC+重放拒绝。
- **桌面壳**：Tauri readiness 失败页、后端子进程退出清理、WebSocket 重连、Playwright 基本冒烟。
- **迁移器**：V2 config / Hub DB / MacSubs / token 各样本断言；只读预览正确。
- **moomoo 实跑（四项体征）**：停 pm2 守护进程、停 orchestrator、关 OpenD(11111)、心跳过期 → 四项各自告警送达 OpenClaw。

---

## 12. 风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| 在生产交易机跑额外 agent 影响交易进程 | 高 | agent 只读、低频、资源上限、最小产物、默认 loopback；先在 distill 副本验证 |
| agent 读不到 MQT 文件（权限） | 低 | 四项体征仅用进程/端口检查 + 全局可读心跳文件；已无 `rw-------` 依赖（交易层不监控） |
| V3 打包脚本需重新设计（不能照抄 MDCx build.py） | 低 | #5 新建 `scripts/build.py`：`cargo tauri build` + PyInstaller 打 hub 入口 |
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

> **范围变更注记（评审后，operator 决定）**：moomoo 监控收窄为**四项生命体征存活**（§5），交易层不再监控。
> 因此下述评审中涉及交易层信号语义的若干条（Codex P1#2 关于 `health_alert_state.json`、P2#5 成交/熔断信号；
> Kimi P1#1/P2#6 及 rw------- 权限发现）**已成历史背景**——结论本身正确，但相关信号不再在实现范围内。
> 保留记录以备将来扩展交易层监控时复用。四项体征所需信号（pm2/进程/端口/心跳）无任何权限或语义障碍。

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

**Kimi 终审（2026-06-27，`kimi -p`，交叉验证了 moomoo 源码与 mdcx）**
- 终审意见："不直接通过，需修订后再次 brief 确认"。15 条（P1×4/P2×6/P3×5），**逐条经文件实证后全部成立**，已纳入 v3：
  - P1#1 §5.1 第二层"消费 health_monitor 分类"不可行 → 实证 health_monitor 只发邮件 + append `freeze_incidents.jsonl`；改用 `orchestrator_log.jsonl` 的 `strategy_error/entry_blocked_health/startup_health_check` 事件。
  - P1#2 V2 取走即清空 与 cursor/ack 冲突 → §6 新增 V2/MacSubs 兼容分级（旧源不可靠 best-effort，关键链路走 V3 agent）。
  - P1#3 supervisor 接口未定义 → §4.1 补 `EventEmitter`/`Supervisor` 契约、`is_alive()` 检测、退避、reconfigure 语义。
  - P1#4 push bootstrap 缺失 → §3.2 补 server_id 预登记 + HMAC 下发/轮换。
  - P2#5 MDCx build.py 错位 → §3.1/§12 改为自建 build 脚本。
  - P2#6 daily-loss-halt 信号源 → 实证 `brain_guard_state.json` halted/halt_reason；§5.1 改正。
  - P2#7 secret 的 /status 回填 → §4.3 占位 `***` + 独立取配置端点 + 未变更保留原值。
  - P2#8 死信细节 → §6.4 补判定/表列/通知/保留。
  - P2#9 agent 配置模型 → §7 补 agent.yaml。
  - P2#10 issue 边界 + 独立安全 issue → §10 收紧 #1/#2 + 新增 #2.5。
  - P3#11 pm2 名可配 / P3#12 ctx 泄漏=连续3周期>2 / P3#13 资源默认值表 / P3#14 端口 5680+占用检测 / P3#15 LegacyEventAdapter 映射 → 均已补。
- **Kimi 额外发现（Codex 漏，本文新增）**：MQT 的 `brain_guard_state.json`、`health_alert_state.json` 为 `-rw-------`，agent 需同用户/授读 → §5 权限说明 + §12 风险。

---

## 附录 B · 定稿前必办（#0 勘测阶段三项，Kimi 终审硬性前置）

1. **moomoo 四项体征 + 拓扑实勘**：确认 (a) moomoo 是否与 Hub 同机（决定 loopback vs 远程）；(b) 进程管理器类型（本机已确认 pm2，他机可能 launchd）与作业名（`ecosystem.config.js`/launchd）；(c) `orchestrator_heartbeat.json` 路径与 grace；(d) OpenD 端口（默认 11111，可能被 `.env` 改）。（交易层信号已不在范围。）
2. **兼容与 bootstrap 决策**：定 V2/V3 兼容策略 + server_id/HMAC/push bootstrap 机制（§3.2/§6/§7/§8）。
3. **supervisor 与资源参数**：定 Supervisor 接口与 monitor 默认资源参数（§4）。

三项闭合后，本架构稿可作为 #1–#6 的实施基础。
