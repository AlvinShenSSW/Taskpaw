# TaskPaw V3 — 架构设计文档

**作者**: Claude (spec-planner 视角) — Opus 4.8
**日期**: 2026-06-27
**状态**: 草案 v4（Codex 15 条 + Kimi 15 条 + 文件实证；§9 九项 operator 决策全定；仅余 moomoo 事实勘测，见附录 B → 可作为 #0–#6 实施基础）
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
5. **事件投递有理论丢失窗口**：`/events` 是"取走即清空"，Hub 取走后落库前崩溃可丢事件。**实战数月未发生**；operator 定为"只优化不重写"（§6 采纳 clear-on-ack 闭合此窗口）。
6. **已知遗留 bug**：`taskpaw_hub.py` 写 status 文件处 `os` 未导入；`macsubs.py:35` OpenRouter key 是占位符。V3 一并消除。
7. **退出不干净（恼人）**：Windows app 点窗口 X 是 `withdraw()` 缩进托盘而非退出（`taskpaw.py:1880,2839-2848`），用户以为关了、进程仍在后台，要手动 kill。残留进程占着端口 5678 + 单实例 mutex（`taskpaw.py:2911-2916`）→ 重启实例拿不到端口、Hub 看到假 Stopped。**根因是点 X 缩进托盘 + 停机不干净**；V3 用 MDCx 壳模式根治——**X 关壳连带杀后端，彻底退出、绝不残留**，UI 功能全保留（operator 定，见 §7.1）。

---

## 2. V3 设计原则

1. **监控即插件**：每种监控是自描述插件（声明配置 schema + 生命周期 + 运行逻辑），注册到 registry。新增 = 写类 + 注册，不碰 UI 与核心数据结构。
2. **配置 schema 驱动 UI**：前端表单由插件 schema 自动生成；但**限定 schema 子集**（见 §4.3），不支持任意 JSON Schema。
3. **健康 + 事件双范式**：既支持"任务完成"，也支持"存活/健康/阈值/日志命中"。
4. **通信稳则不动**：agent↔Hub 通信稳定数月，**保留 poll+/events+单调id+Hub去重**，做三项向后兼容优化（clear-on-ack、Hub 转发重试+死信、事件附加字段，见 §6）。不引入 push、不重写协议。
5. **消费而非重造**：被监控系统已有的心跳/告警/日志，agent 优先读取转发。
6. **安全边界显式**：桌面 UI 的 loopback 信任**不外推**到 agent 的跨机 API；远程暴露必须显式开启 + 鉴权（见 §3.2）。
7. **协议向后兼容**：V3 Hub 能继续轮询 V2 agent；事件 schema 加版本 + LegacyEventAdapter。
8. **agent 有本地控制 UI、但不含 Hub 聚合**：agent **保留本机操作能力**（启停 Lada、设监听目录、ComfyUI 配置等，数月稳定，**不删**），但不携带 Hub 的 dashboard/跨机聚合/OpenClaw token；与 Hub 共享 core（见 §7）。moomoo 例外：只装服务、不装 UI。
9. **跨平台 agent**：agent 必须同时支持 **macOS 与 Windows**（V2 是 Windows 本位）。进程/系统信息走 `psutil`（已在依赖），路径/进程管理器/打包按平台分支（见 §3.0）。Hub 固定 macOS。
10. **X = 彻底退出（operator 定）**：交互式 agent 用 MDCx 壳模式——**点 X 关壳连带杀后端子进程**（停所有监控线程 + 托管子进程 lada-cli + 释放端口），**绝不残留、无托盘**。代价是关窗即停监控；要 7×24 走 headless 服务模式（moomoo 即此）。**UI 功能全保留**（见 §1.3#7、§7.1）。

---

## 3. 技术栈与安全模型

### 3.0 部署拓扑（operator 明确）

```
                      ┌────────── Mac #母体 ──────────┐
                      │  TaskPaw Hub（聚合+SQLite+UI） │
                      │     └── POST ──► OpenClaw      │
                      └──▲──────────▲──────────▲───────┘
       局域网         │          │          │
   ┌──────────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
   │ Win: Lada    │ │ Win:     │ │ Mac #2:  │ │ ...       │
   │              │ │ ComfyUI  │ │ moomoo   │ │           │
   └──────────────┘ └──────────┘ └──────────┘ └──────────┘
```
（MacSubs 不再纳入 V3 监控。）

- **Hub 固定一台 macOS**（母体）。
- **每个 agent 独占一台机器，与 Hub 必为异机**（operator 明确："一个平台一台"）。moomoo 的 Mac 是**独立于 Hub 的另一台 Mac**；Lada/ComfyUI 各在自己的 Windows 机。
- 因此 **所有 agent 对 Hub 都是远程**，但 **均在同一局域网**（operator 定）→ Hub 按内网 IP 轮询 + Bearer，**不需要 Tailscale/VPN**。**无 loopback/同机情形，不引入 push。**
- **agent 跨平台**：macOS 或 Windows（当前：moomoo=macOS、Lada/ComfyUI=Windows）。
- **moomoo 实勘**：本机 moomoo 由 **pm2 管理**（PM2 God Daemon → `orchestrator` online），OpenD 是 **`moomoo_OpenD.app`**（非 Linux `./OpenD`）。→ 体征③ 用 **tcp 11111 探测**（跨平台稳）；体征①② 的进程管理器/作业名**做成可配**（这台 Mac 是 pm2，他机可能 launchd），#0 逐机确认。

### 3.1 技术栈（对齐 MDCx）

| 层 | 技术 | 来源/理由 |
|---|---|---|
| 桌面壳（仅 Hub UI） | **Tauri v2** | 抄 `mdcx/src-tauri/`，锁死无 IPC |
| 前端 | **React 19 + Vite + MUI + TanStack Router/Query + Zustand** | MDCx 用 Rspack，V3 建议 **Vite**（更稳）；其余照抄 |
| 前后端通信 | 后端**同源** serve 前端 + REST `/api/v1` + WebSocket `/ws` | 抄 MDCx |
| 后端 | **Python FastAPI + uvicorn** | 复用现有 Python 监控逻辑 |
| 打包 | **后端**（agent + hub，跨平台）：PyInstaller。**Tauri 客户端**（agent 控制台 + hub 仪表盘，共享前端）：`cargo tauri build`（macOS + Windows）。交互式 agent = 壳+后端打一起（X 退出，§7.1）；headless（moomoo）= 后端注册 OS 服务、不打 UI | **需自建 `scripts/build.py`**。MDCx 的 `build.py` 实为 PyInstaller 打包 `main.py --onefile`，只能参考流程不能照抄（Kimi P2#5） |
| 握手 | 后端 stdout 输出一行 readiness JSON，壳解析 | 抄 MDCx |

**MDCx 安全约束 → V3 验收标准**（不是"抄 MDCx"一句带过，逐条作为 #5 验收项）：
- `withGlobalTauri=false`，capabilities 为空，UI 无任何 Tauri IPC/FS 权限
- webview init script **仅在 loopback origin** 注入 api key；导航离开 loopback 即失效
- 后端 stdout **只**输出一行 readiness JSON；其余日志走 stderr，不污染握手
- api key 经 env 传入，**不走 argv、不进日志**
- 壳对后端 `base_url` 做 loopback 校验后才加载

### 3.2 网络与安全模型

> **operator 约束（重要）**：agent↔Hub 的 **Hub 轮询 + `/ping /status /events` + Bearer** 通信机制**已稳定运行数月，不大改、只优化**。
> 因此 **V3 保留现有 poll 模型**，**不**引入 push/webhook（那本身就是大改通信）。下方 Codex P1#3 提的"push 备选"与 Kimi P1#4"push bootstrap"**因不引入 push 而不适用**（保留备查，将来若上公网再议）。

在"保留 poll"前提下做安全优化：
- **agent 对 Hub/网络只暴露 `/ping /status /events`（读为主）**，bind 局域网网卡 + Bearer。本机**控制 API**（启停 Lada、改配置）走**独立 loopback 端口**（`control_addr`），由本机 UI 客户端访问，**不对网络/Hub 暴露**。
- 传输面：**同一局域网 + per-agent Bearer**（沿用现有，无需 VPN）。局域网视为受信；**不对公网/WAN 暴露**任何端口。
- 沿用 V2 Bearer 鉴权与 401 不清队列语义。
- 桌面壳的"loopback 信任"仅用于 Hub 本机 UI；CORS 只对 Hub UI 放开，agent 不开放 CORS。
- secret/token 经**环境变量(优先)/ 本地配置文件**取得，不走 argv、不进日志，**且绝不提交到 git/GitHub**：真实 token/api key 一律放 env 或被 gitignore 的本地配置，仓库只含 `*.example.*` 空模板(`.gitignore` 覆盖 `.env`/`config.json`/`agent.yaml`/`hub.yaml`)。

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
- **事件出口**：`emit(...)` 是插件唯一出口；去重（`dedupe_key`）/持久化/限流由 supervisor + 现有事件队列（§6）保证。

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
| **`webhook_in`** | both | 本机被监控脚本→agent 入站 POST（仅本地，非 agent→Hub） | 新增（可选） |
| **`host_metrics`** | service | 本机主机健康：CPU / 内存 / GPU / 网络 / 磁盘基础指标 + 阈值告警（psutil；GPU 跨平台需平台分支，见 §5.3） | 新增 |

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
**信号路径**：**#13 实测确认**，活跃仓在 `~/Documents/Workspace/moomoo/`（**非** `~/mqt`），心跳在
`~/Documents/Workspace/moomoo/runtime/orchestrator_heartbeat.json`。⚠️ 同机另有多个 worktree
（`moomoo-wt-298 / -wt-716 / -linux`）各带一份**陈旧** `orchestrator_heartbeat.json` + `ecosystem.config.js`——
**必须指向活跃主仓路径，忽略 worktree 副本**。路径全部可配。

### 5.1 四项生命体征（唯一监控范围）

| # | 体征 | type_id | 配置 | 告警 |
|---|---|---|---|---|
| # | 体征 | type_id | 配置（#13 实测值） | 告警 |
|---|---|---|---|---|
| ① | **pm2 守护存活** | `process` | God Daemon 进程：`pgrep 'PM2.*God'`（实测进程名 `PM2 v7.0.1: God Daemon (~/.pm2)`）。**绝不用 `pm2 ping`**（会拉起守护、破坏只读）。管理器类型可配（本机 pm2；他机若 launchd 则 `launchctl`） | 不在 = 体系失去自愈根，所有进程不再自动重启 |
| ② | **orchestrator 进程在跑** | `process` | pm2 作业名 **`orchestrator`**（实测 pm_id 22、online；脚本 `moomoo/strategy_orchestrator.py`）。作业名可配 | 非 online（errored/crash-loop/stopped） |
| ③ | **OpenD 网关在线** | `tcp_check` | **`127.0.0.1:11111` LISTEN**（实测：`.env` `FUTU_OPEND_PORT=11111` 未改；进程 `moomoo_OpenD.app` 监听 loopback）。仅本地可探→agent 须与 OpenD 同机 | 非 LISTEN = 交易瘫痪 |
| ④ | **orchestrator 心跳新鲜** | `heartbeat` | `~/Documents/Workspace/moomoo/runtime/orchestrator_heartbeat.json`（实测 `-rw-r--r--` 可读）。按 `next_check_due_utc` + grace 判定 | 超 due+grace 未更新 = HUNG |

**⚠️ ④ 心跳判定有状态语义（#13 关键发现，不能用裸 mtime）**：心跳含 `status` 字段，可为
`cycling` / `hibernating` 等。**`hibernating` 时 `interval_min: 0`、`next_check_due_utc` 可能在数天后**
（实测有一例 due 在 +2 天、`wake_utc` 同步），此时心跳"陈旧"**不是**告警。正确判据 = `now > next_check_due_utc + grace`
且 **非 hibernating**。grace 取值（实测自 `moomoo/scripts/cron/check_orchestrator_heartbeat.py`）：
orchestrator 自带 grace（cycling 300s / hibernation·startup 900s，已含在 `next_check_due_utc`）+
外部 watcher 额外 `WATCHER_EXTRA_GRACE_SEC=300s` + 重启宽限 `STARTUP_GRACE_SEC=600s`（pm2 uptime<600s 跳过）。

说明：
- ①②是进程检查、③是端口探测、④读全局可读的心跳文件 → **无权限障碍**（之前的 `rw-------` 顾虑只涉及交易层文件，现已不在范围内）。
- **moomoo 已有自愈** `orch-watchdog`（pm2 作业，`cron_restart '*/10 * * * *'`，每 10 分钟巡检心跳并重启）。**TaskPaw 的角色是向 operator 告警，与 moomoo 自愈互补**，不替代、不重启。
- **跨平台/跨机注意**：OpenD 在 macOS 是 `moomoo_OpenD.app`、Linux 是 `./OpenD`，进程名不同 → ③ 用 **tcp 探测**最稳。管理器本机 pm2、他机可能 launchd → ①② 把"管理器类型 + 作业名"做成配置项。
- 四项中 ①②④ 用 `process`/`heartbeat`，③ 用 `tcp_check`；**moomoo preset 只需 process + heartbeat + tcp_check 三种插件**。`heartbeat` 插件须支持读 JSON 的 `status`/`next_check_due_utc` 字段（不只 mtime）。
- ④ 与 ② 互补：进程在跑 ≠ 在干活；心跳过期能抓"进程活着但卡死"。

### 5.2 部署与可达性

moomoo agent 跑在**独立的一台 macOS**（非 Linux，非 Hub 那台 Mac）→ 对 Hub 是远程。
按 §3.2：**保留现有 Hub 轮询模型**，moomoo agent 在同一局域网暴露 `/ping /status /events` + Bearer，Hub 按内网 IP 轮询——与现存 Windows agent 完全一致。无 push、无 VPN、无 loopback。

---

## 5b. 主机健康监控（Hub 母体 + 每台 agent 机器）

**范围（operator 明确）**：除了业务监控（Lada / ComfyUI / moomoo），还要监控**承载机本身的健康**——
即 **Hub 母体 Mac** 与 **Mac 开发 agent** 两台机器（以及任何 agent 机器）。要监控的信息**很简单**，只两类：

1. **机器存活**（没挂）。
2. **基础资源指标**：CPU / 内存 / GPU / 网络（+ 磁盘）占用。

### 5b.1 用 `host_metrics` 插件（service 类）

新增内置插件 `host_metrics`（§4.2），每台 agent 注册一个本机实例，采样并随 `/status` 上报：

| 指标 | 来源 | 备注 |
|---|---|---|
| CPU % | `psutil.cpu_percent` | 跨平台稳定 |
| 内存 % | `psutil.virtual_memory` | 跨平台稳定 |
| 网络吞吐 | `psutil.net_io_counters`（取差分 → bytes/s 收/发） | 跨平台稳定 |
| 磁盘 % | `psutil.disk_usage` | 跨平台稳定 |
| **GPU %** | **operator 定**：**macOS 忽略 GPU**（字段恒 `n/a`）；**Windows 采集**——**直接复用 V2 已实现的 `_get_gpu_info()`（`taskpaw.py:541`，`nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total`）** | 已是成熟代码，非勘测项；非 N 卡机取不到时标 `unavailable`，不阻塞其余指标 |

- **阈值告警**：每指标可配阈值（默认例：CPU>90% 持续 3 周期、内存>90%、磁盘>90% → `warn`/`alert`）。默认只报不致命。
- **机器存活**：无需额外探针——agent 可被 Hub 轮询到即视为存活；连续 N 次轮询失败由 Hub 侧判离线告警（沿用现有机制）。`host_metrics` 实例在跑本身即"主机活着且 agent 在工作"的证据。
- 复用 V2 已有的 `_get_cpu_memory()`（psutil，`taskpaw.py:516`）与 `_get_gpu_info()`（nvidia-smi，`taskpaw.py:541`），扩展出网络/磁盘。**GPU 无需勘测——V2 已能取到。**

### 5b.2 Hub 母体的自监控（特例）

Hub 是聚合方、与 agent 必为异机（§3.0），但**它自己的主机健康也要监控**。
做法：**Hub 内置一个 `host_metrics` 自监控实例**（监控 Hub 所在 Mac 本机），直接喂进 Hub 自己的仪表盘——
**不需要在 Hub 上再装一个独立 agent**。这是"Hub 看自己"，不破坏"一平台一机"的拓扑约束。

> 实质：`host_metrics` 是唯一一个 **Hub 与 agent 都会运行**的插件。其余监控仍只在 agent 侧。

### 5b.3 部署

- **Mac 开发 agent**：本就跑 agent，加一个 `host_metrics` 实例即可（连同它承载的 Lada/ComfyUI 业务监控）。
- **Hub 母体**：Hub 后端启动时自动起一个本机 `host_metrics` 自监控（§5b.2）。
- 指标采样低频（与 poll 同档，默认 10s），资源开销可忽略（§4.4 资源上限同样适用）。

### 5c. 开发 agent 活动监控（optional）：VSCode 里 Claude / Codex 在跑任务还是 idle

**operator 定为 optional**：仅在**开发 agent 机器**（如 Mac 开发机）上，监控其 VSCode 里跑的
**Claude Code / Codex 当前是 busy（正在跑任务）还是 idle（等输入）**。

**可行且可靠**——不靠猜 CPU，而是让这两个 CLI 自己上报事件（已实证两者都支持）：

| 工具 | 上报机制 | busy→idle 信号 |
|---|---|---|
| **Claude Code** | `hooks`（settings.json） | `UserPromptSubmit`/`SessionStart` → busy；`Stop` → idle；`Notification`（等待输入/权限）→ waiting |
| **Codex** | `config.toml` 的 `notify = [程序, 事件]`（事件如 `turn-ended`），亦有 `hooks` | turn 开始→busy；`turn-ended` → idle |

**实现**：一个极小的包装脚本被 hook/notify 调用，原子写一个状态文件
`~/.taskpaw/agent-activity.json`（`{tool, state: busy|idle|waiting, session, ts}`）。
TaskPaw 用**已有的 `state_file` 插件**读它（无需新插件类型）：状态变化 → 事件；
可选 watchdog（busy 持续过久 / 长时间无更新）→ 告警。

- **零侵入**：纯读状态文件 + 用户自己在 Claude/Codex 配置里挂 hook/notify；TaskPaw 不进 VSCode、不读会话内容。
- **隐私**：只上报 busy/idle 与时间戳，**不读 prompt/代码/会话**。
- **范围**：仅开发 agent 机；moomoo/Hub 不需要。纯 optional，不阻塞主链路。

---

## 6. 事件投递：保留现有协议 + 针对性优化

> **operator 约束**：agent↔Hub 通信稳定数月，**不大改、只优化**。
> 故 V3 **保留** poll + `/events` + 单调 `id` + Hub `last_event_ids` 去重这套机制。
> 下述评审的"大改"建议（Codex P1#1 cursor/ack/outbox 重写、Kimi P1#2 V2 桥接、Kimi P1#4 push bootstrap）**降级为可选优化或不采纳**，理由：现有机制已含单调 id + 持久 next_event_id + Hub 去重，实战未现丢事件。

### 6.1 保留的现有机制（不改）

- Hub 轮询 agent `/events`，响应 `{"events":[...]}`，每条带单调 `id`（`taskpaw.py` 已持久化到 `state.json`）。
- Hub 按 `last_event_ids[server]` 去重，只收 `id >` 上次的事件（`taskpaw_hub.py` 已实现）。
- Bearer 鉴权，401 不清队列。

### 6.2 采纳的优化（operator 定：三项全做，均向后兼容）

1. **clear-on-ack（采纳）**：agent 由"取走即清空"改为"Hub 确认收到（下次轮询带上已收 max-id）后再裁剪队列"。同一 `/events` 端点，仅改裁剪时机，闭合"Hub 取走后、落库前崩溃"的丢失窗口。**不改协议形状**。
2. **Hub→OpenClaw 转发重试（outbox，Hub 内部）**：转发失败进 `delivery_state: pending/failed/dead_letter` + 退避重试 + 死信，失败不只 log。**纯 Hub 内部，不碰 agent↔Hub 线协议**。死信判定：连续 10 次或超 24h；死信生成一条本地高优告警（不经 OpenClaw）；保留 7 天。同时修掉 V2 写 status 文件处 `os` 未导入的 bug。
3. **事件字段增富（附加，不破坏）**：在现有 `{id,time,machine/stage,monitor,message}` 上**附加可选字段** `level/title/data`，老消费者忽略即可。**不**改 endpoint、不强制 schema 版本。

### 6.3 事件字段（附加式，非协议破坏）

现有字段保留；新增可选字段：

```json
{
  "id": 1234, "time": "...", "machine": "moomoo-prod",
  "monitor": "orchestrator-hb", "message": "next_check_due 12 min overdue",
  "level": "alert",                 // 新增可选: info | warn | alert | done
  "title": "Orchestrator HUNG",     // 新增可选
  "data": { "cycle_count": 8123 }   // 新增可选
}
```
Hub 与 OpenClaw 不认识新字段时按老逻辑处理 → 天然兼容。

### 6.3 字段命名兼容（保持现状）

现有 V2 agent 事件字段（`machine/monitor/message`）Hub 已稳定兼容——**保持现状**，不强制统一 schema。新增的 `level/title/data` 为可选附加（§6.2），不影响旧字段。（MacSubs 已不再纳入监控，其 `stage/current_file` 兼容无需考虑。）

### 6.4 Hub 存储与 OpenClaw sink（优化项）

- Hub `events` 表**可选**加列承接新字段：`level, title, payload_json, delivery_state`（缺省为空，兼容旧行）。
- `OpenClawSink`：默认仍发 `{"text": rendered}`（不假设 OpenClaw 能吃结构化）；配合 §6.2 优化 2 的转发重试。

---

## 7. 仓库结构（monorepo）

**运行形态（operator 定）**：
- **交互式 agent**（Lada/ComfyUI）= **Tauri 壳 + 后端**（壳 spawn 后端，**X 退出连带杀后端**，§7.1）。监控逻辑与 UI 同一应用生命周期、干净启停。
- **headless 服务模式**（moomoo / 想 7×24 常驻）= 同一后端、不带壳，注册 OS 服务。
- **Hub** = 后端跑成常驻服务 + Tauri 仪表盘客户端。

```
taskpaw_v3/                  # monorepo
├── core/                    # 共享：events/config/auth/protocol/schema
├── monitors/                # 插件 + registry + supervisor
├── agent/                   # taskpaw-agent 后端 —— 监控 + 本机控制 API + /events
│   └── server/{app.py,launcher.py,service.py}   # launcher=壳spawn入口; service=OS服务入口
├── hub/                     # taskpaw-hub 后端 —— 聚合/SQLite/OpenClaw + Hub API
│   └── server/{app.py,launcher.py,service.py}
│   └── poller.py / store.py / openclaw.py
├── ui/                      # 共享 React 前端（agent 控制台视图 + hub 仪表盘视图，按角色路由）
├── src-tauri/               # 共享 Tauri 壳（连本地后端；agent 控制台 / hub 仪表盘各一窗口）
├── scripts/build.py         # 打包：后端(agent/hub) + Tauri 客户端
└── docs/specs/
```

- **agent 保留全部本地操作 UI**（启停 Lada、设监听目录、ComfyUI 配置等，过去数月稳定，**不删**），UI 调本地控制 API。
- **agent 不含** Hub 的跨机聚合 / OpenClaw token / SQLite 仪表盘。
- **moomoo 例外**：headless 服务模式、**不装 UI**（读为主四体征），暴露面最小。

**agent 配置模型（评审 Kimi P2#9）**：
- 后端服务读本地配置（跨平台路径）：macOS `~/Library/Application Support/TaskPaw/agent.yaml`、Windows `%APPDATA%/TaskPaw/agent.yaml`、Linux `/etc/taskpaw/agent.yaml`。
- 字段：`server_id`、`bind_addr`（局域网网卡，对 Hub 暴露 `/ping /status /events`）、`control_addr`（loopback，对本机 UI 暴露控制 API）、`api_token`、`monitors[]`。
- 配置也可由本机 UI 客户端改写（→ 写回配置 + 热生效），或运维脚本下发。

### 7.1 退出行为与生命周期（operator 定：X = 彻底退出）

operator 明确：**Windows agent 点 X = 彻底退出，监控也一起停，绝不残留**（最直觉）。
V2 痛点根因是"点 X 缩进托盘 + 停机不干净"；V3 直接用 **MDCx 壳模式**根治：

- **交互式 agent（有 UI，如 Lada/ComfyUI）= Tauri 壳 spawn 后端子进程**。点 **X 关壳 → 连带杀后端子进程** → supervisor `stop()` 所有 MonitorInstance（停线程）+ **terminate 托管子进程（lada-cli）** + 释放端口 → **进程彻底结束**。**无托盘、无残留、不用任务管理器抓进程。**
- **取舍（operator 已认）**：关窗 = 停监控 → 关窗后不再收 Lada 完成通知。**需要监控时开着窗口**即可。
- **想要 7×24 常驻** 的场景走 **headless 服务模式**（同一后端、不带 Tauri 壳，注册 Windows Service/launchd，UI 按需打开连它）——**moomoo 即此模式**（读为主四体征、launchd 常驻）。即"X=退出"是交互式默认，"常驻服务"是可选模式，二者同一份后端代码。
- **单实例**：保留命名 mutex/端口检查；因 X 干净停机，不再"残留占端口→新实例拿不到→Hub 假 Stopped"（§1.3#7）。
- **Hub**：Hub 是母体、宜常驻。Hub 后端跑成 launchd 服务（始终聚合），Tauri 仪表盘作客户端按需开关；**关 Hub 仪表盘窗口不停聚合**。（与交互式 agent 的"X=退出"相反，因 Hub 角色不同——§9 决策点确认。）

---

## 8. 迁移矩阵（评审 P2#10）

| 来源 | 内容 | 迁移动作 |
|---|---|---|
| V2 agent `APPDATA/TaskPaw/config.json`（仅 Windows） | watcher 数组 | 每旧类型 mapper → V3 `{type_id,name,config}`；V3 agent 跨平台，新机用 §7 平台路径 |
| V2 `state.json` | `_next_event_id` | 迁为 V3 agent 事件游标起点 |
| Hub `~/.taskpaw-hub/hub.db` | servers/events/config/last_event_ids | 表结构升级（加 §6.4 列），保留历史 |
| MacSubs | —— | **不再纳入 V3 监控**（operator 定）；从 Hub 服务器列表移除，端口 5679 释放 |
| OpenClaw token / openclaw_enabled | Hub config | 平移 |
| 端口 | V2 agent 5678 / MacSubs 5679 | **V3 agent 默认 5680**，与 V2/MacSubs 不冲突；启动时端口占用检测，若 V2 在跑则退出并提示迁移（Kimi P3#14） |

迁移器**先只读预览**（diff）后再写入。

---

## 9. operator 决策（均已定）

| # | 决策 | 结论 |
|---|---|---|
| 1 | 重写策略 | **全新结构 + 移植成熟逻辑**（monorepo，V2 暂并存）。tkinter→Tauri+React+FastAPI 无法原地改。 |
| 2 | 传输面/网络 | **均在同一局域网** → Hub 按内网 IP 轮询 + Bearer，**不需 Tailscale/VPN**，不引入 push。 |
| 3 | 通信优化范围 | **三项全做**：clear-on-ack、Hub→OpenClaw 转发重试+死信、事件附加字段（§6）。 |
| 4 | MacSubs | **不再纳入 V3 监控**；从 Hub 列表移除，5679 释放。 |
| 5 | 前端 bundler | **Vite**（比 Rspack 稳）。 |
| 6 | V2 去留 | tkinter V2 **冻结维护到 V3 上线再弃用**。 |
| 7 | moomoo 范围 | **仅四项体征**（distill 不纳入）。 |
| 8 | Lada/ComfyUI | **先功能对齐 V2，再谈 UI 改进**。 |
| 9 | agent 退出 | 交互式 **X = 彻底退出**（§7.1）；7×24 走 headless 服务模式。 |

剩余待 #0 勘测确认的事实项见 §附录 B（moomoo 进程管理器/作业名/心跳路径/OpenD 端口）。

---

## 10. 实施路线（评审 P2#9 / P3#12 重排：先验证告警链路，Tauri 后置）

> 原序（壳→插件→新监控→事件→moomoo）会先固化错误的 emit API、写出无稳定语义的 monitor、moomoo 验证太晚。重排为：

- **#0 勘测与决策**：moomoo 信号实地勘测（确认各 jsonl/json 字段与路径）+ 敲定 §3.2 网络/安全模型 + §9 决策点。产出更新本文。
- **#1 通信优化（保留协议，三项全做）**：现有 poll+/events+去重回归测试打底；clear-on-ack；Hub→OpenClaw 转发重试/死信 + 修 `os` 未导入 bug；事件附加字段 `level/title/data`（§6）。**不重写协议、不引 push**。
- **#2 后端最小闭环 + 干净停机**：FastAPI agent + Hub 后端（**先无 UI**）+ Bearer + 现有 poll 端到端 → 入库 → OpenClaw。实现优雅停机原语（SIGTERM/壳信号 → 停所有线程 + 杀托管子进程 lada-cli + 释放端口），供 #5 的"X 退出"与 headless 服务两种模式复用。控制 API 走 loopback。
- **#2.5 安全验收**（评审 Kimi P2#10 —— 独立 issue 防遗漏）：覆盖 §3.1/§3.2 安全项（agent 对网络只暴露 `/ping/status/events`、控制 API 仅 loopback、token 不进日志、Hub UI 的 CORS/origin、secret 不入 `/status`、Bearer 401 不清队列）。
- **#3 插件 supervisor + schema 子集**：`MonitorPlugin/MonitorInstance` + Supervisor 契约（§4.1：崩溃重启/超时/退避/DEGRADED）+ pydantic/json_schema/ui_schema + 资源上限（§4.4）。先做 moomoo 所需的 **`process` / `heartbeat` / `tcp_check`** 三种打通。
- **#4 moomoo preset + 端到端验证**：内置四项生命体征模板（§5.1），在交易机（或 `moomoo_distill` 副本）实跑，制造 pm2 停 / orchestrator 崩 / OpenD 关闭 / 心跳过期，确认告警达 OpenClaw。
- **#5 UI / Tauri（共享前端，双角色）**：React + MUI（schema 驱动表单）+ Tauri 壳，连本地后端。**agent 控制台**（启停 Lada、设监听目录、ComfyUI 配置等——对齐并改进 V2 现有功能，不删）+ **Hub 仪表盘**（多机状态汇总）。**交互式 agent 接线 MDCx 壳模式：壳 spawn 后端、X 关壳连带杀后端（彻底退出、无残留）**；moomoo/Hub 走 headless 服务。每页先跑 ui-ux-pro-max `--design-system`。
- **#6 V2 迁移与剩余监控**：迁 folder/comfyui/lada/custom_cmd 为插件 + §8 迁移器 + 灰度切换 + **各机安装 V3 服务并下线 V2 GUI app**（消除托盘残留）。

依赖序：#0 → #1 → #2 → #2.5 → #3 → #4 →（#5 ∥ #6）。Tauri 不阻塞监控链路验证。

---

## 11. 测试计划

- **协议/可靠性（优化项）**：现有 poll+去重回归不破坏；clear-on-ack（Hub 取走后崩溃不丢）；Hub→OpenClaw 转发重试/死信；附加字段向后兼容（老消费者忽略）；鉴权 401 不清队列。
- **插件系统**：注册/发现、pydantic+json_schema 双校验、配置往返与 `config_version` 迁移、未知 type 容错、supervisor 崩溃重启/超时。
- **新监控单测**：heartbeat 用临时文件 mtime 模拟超时（含 grace）；tail_jsonl 喂 partial-line/轮转/inode 替换样本；log_pattern 正则限长；tcp_check 起临时 socket；state_file 模拟字段变化。
- **资源**：事件风暴折叠、max_line_bytes、正则回溯上限。
- **安全（评审 P3#14）**：HTTP/WS 鉴权、Hub UI 的 CORS/origin 校验、token 不进日志、agent 网络面只读 `/ping/status/events`、控制 API 仅 loopback 可达、secret 不入 `/status`、Bearer 401 不清队列。
- **生命周期（修复 V2 残留，operator 重点）**：交互式 agent 点 **X → Tauri 壳杀后端子进程 → 所有监控线程退出 + lada-cli 被 terminate + 端口释放 + 进程彻底结束（无残留、无僵尸，任务管理器里干净）**；headless 服务模式 stop/SIGTERM 同样干净；Windows Service/launchd 安装-启动-停止-卸载冒烟。
- **桌面壳**：Tauri readiness 失败页、**壳退出杀后端子进程**、WebSocket 重连、Playwright 基本冒烟。
- **迁移器**：V2 config / Hub DB / token 各样本断言；只读预览正确。
- **moomoo 实跑（四项体征）**：停 pm2 守护进程、停 orchestrator、关 OpenD(11111)、心跳过期 → 四项各自告警送达 OpenClaw。

---

## 12. 风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| 在生产交易机跑额外 agent 影响交易进程 | 高 | agent 只读、低频、资源上限、最小产物、只 bind 局域网且不暴露公网；先在 distill 副本验证 |
| agent 读不到 MQT 文件（权限） | 低 | 四项体征仅用进程/端口检查 + 全局可读心跳文件；已无 `rw-------` 依赖（交易层不监控） |
| V3 打包脚本需重新设计（不能照抄 MDCx build.py） | 低 | #5 新建 `scripts/build.py`：`cargo tauri build` + PyInstaller 打 hub 入口 |
| moomoo 内部产物格式随 MQT 演进漂移 | 中 | 只读 + 路径/字段全可配；优先消费稳定的 jsonl 事件日志；#0 勘测锁定 |
| Tauri/Rust 工具链是新增复杂度 | 中 | headless 优先（#2-#4 不依赖壳）；壳直接移植 MDCx |
| 事件投递优化引入回归 | 低 | 保留现有协议，三项优化均向后兼容；clear-on-ack 仅改裁剪时机、有回归测试兜底 |
| schema→UI 自动表单覆盖不全 | 低 | 限定 schema 子集 + ui_schema widget + 复杂控件自定义回退 |
| V2/V3 并跑端口/状态冲突 | 中 | 不同默认端口 + 迁移只读预览 + 互斥启动 |

---

## 13. 后续

§9 决策已全定 → 据 §10 建 issue（#0–#6）→ `/afk` 或 `/afk codex` 逐个自治实现 + 双评审。**#0 勘测已完成（issue #13，✅ 附录 B 已填）→ 实现可从 #1 起。**

---

## 附录 A · 评审记录

> **范围变更注记（评审后，operator 决定）**：moomoo 监控收窄为**四项生命体征存活**（§5），交易层不再监控。
> 因此下述评审中涉及交易层信号语义的若干条（Codex P1#2 关于 `health_alert_state.json`、P2#5 成交/熔断信号；
> Kimi P1#1/P2#6 及 rw------- 权限发现）**已成历史背景**——结论本身正确，但相关信号不再在实现范围内。
> 保留记录以备将来扩展交易层监控时复用。四项体征所需信号（pm2/进程/端口/心跳）无任何权限或语义障碍。
>
> **通信决策注记（评审后，operator 决定）**：agent↔Hub 的 poll+/events 通信稳定数月，定为**只优化不重写**。
> 因此 **Codex P1#1（cursor/ack/outbox 重写）降级为 §6.2 可选优化**；**Kimi P1#2（V2 桥接）、P1#4（push bootstrap）不采纳**（不引入 push）；
> **P2#8 死信、P3#15 字段映射**保留为 Hub 内部/附加式优化。详见 §6。评审结论技术上仍正确，是 operator 基于生产稳定性的取舍。

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

## 附录 B · #0 勘测（✅ 已完成，issue #13，2026-06-27）

moomoo 机实测（`AlvintekiMini`，macOS arm64）。详见 issue #13 评论；结论已并入 §5.1。

| # | 事实项 | 实测结果 |
|---|---|---|
| 1 | 进程管理器 + 作业名 | **pm2** v7.0.1（Homebrew）；orchestrator 作业名 **`orchestrator`**（pm_id 22, online） |
| 2 | 心跳路径 + grace | `~/Documents/Workspace/moomoo/runtime/orchestrator_heartbeat.json`；grace = orchestrator 自带（cycling 300s/hibernation·startup 900s，含在 `next_check_due_utc`）+ watcher 额外 300s + 重启 600s；watchdog `*/10 * * * *` |
| 3 | OpenD 端口 | **11111**（`.env` `FUTU_OPEND_PORT=11111` 未改），`moomoo_OpenD.app` 监听 **`127.0.0.1:11111`**（仅 loopback） |
| 4 | pm2 守护探测 | God Daemon 进程 `PM2 v7.0.1: God Daemon (~/.pm2)`，用 `pgrep 'PM2.*God'`（**非** `pm2 ping`） |

**勘测发现的两个关键约束（已并入 §5.1）**：
1. **心跳有 `status` 语义**：`hibernating` 时 `next_check_due_utc` 可在数天后，陈旧≠告警 → `heartbeat` 插件须读 `status`/`next_check_due_utc`，不能用裸 mtime。
2. **活跃路径是 `~/Documents/Workspace/moomoo/`（非 `~/mqt`）**，且同机有多个带陈旧心跳的 worktree → 必须指向活跃主仓、忽略副本。
3. **moomoo 自带 `orch-watchdog`** 每 10 分钟自愈 → TaskPaw 只告警、不重启（互补）。

（GPU：复用 V2 nvidia-smi，非勘测项，见 §5b.1；Windows 机无需额外勘测。交易层信号不在范围。）
