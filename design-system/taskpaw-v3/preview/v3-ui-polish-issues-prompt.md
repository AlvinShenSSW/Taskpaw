# Prompt — 把 V3 UI/UX 精修拆成多个 issue 并创建

> 用法：把本文件**全部内容**复制粘贴到 VS Code 里的 Claude（在 TaskPaw 仓库根目录运行）。
> 它会读设计稿与设计文档，按下面的清单创建一组 GitHub issue（含新功能「引导式添加监控向导」的完整功能需求）。

---

你是在 TaskPaw 仓库里工作的工程助手。请把下面这批 V3 前端 UI/UX 精修工作**拆成多个 GitHub issue 并创建**。先读资料、再按规范产出，不要直接写实现代码。

## 1) 先读这些（务必全部读完再动手）

- 设计稿（高保真交互预览，是这批工作的视觉基准）：
  `design-system/taskpaw-v3/preview/v3-ui-polish-preview.html`
  —— 在浏览器打开，注意顶栏 **EN / 中** 切换、左栏「+ 添加」打开的三步向导、表单样式、Hub 机群卡片。
- 改动清单（预览 → 代码的逐项映射，含后端依赖与工作量）：
  `design-system/taskpaw-v3/preview/v3-ui-polish-changelist.md`
- 设计系统（颜色/字体/间距/反模式，**硬约束**）：
  `design-system/taskpaw-v3/MASTER.md`、`design-system/taskpaw-v3/pages/agent-console.md`、`design-system/taskpaw-v3/pages/hub-dashboard.md`
- 现有 UI 实现与契约：`taskpaw_v3/ui/`（重点：`src/theme.ts`、`src/App.tsx`、`src/views/AgentConsole.tsx`、`src/views/HubDashboard.tsx`、`src/components/SchemaForm.tsx`、`src/components/PathWidget.tsx`、`src/components/StatusDot.tsx`、`src/api.ts`、`src/i18n.ts`）
- V3 UI 原始 spec：`docs/specs/2026-06-27-v3-ui-19.md`
- 硬规则与协作约定：`docs/constitution.md`、`AGENTS.md`、`CLAUDE.md`
- 仓库自带视觉精修知识库：`skill/ui-ux-pro-max/SKILL.md`（实现时按其 §1–§4 检查项自查）

## 2) 全局约束（写进每个 issue 的前提，别违反）

- V2 冻结，仅在 `taskpaw_v3/` 内做 V3 新工作；遵守 `docs/constitution.md`。
- 沿用现有设计系统 token，**不另起配色**；禁止 emoji 当图标（用 SVG/MUI icon）。
- 可访问性硬线：对比度 ≥4.5:1、可见 focus、状态不靠颜色单独表达、`prefers-reduced-motion` 降级、过渡 150–300ms。
- **中文是一等公民**：CJK 字体回退；中文下关闭只适合拉丁字母的 `text-transform:uppercase` 与大 `letter-spacing`。
- 每个行为变更都要带测试（前端 `npm test`/vitest；后端 `uv run pytest`）。CI 红时不开 PR。
- **不要 commit/push、不要部署**，除非操作者明确要求（CLAUDE.md 约定）。

## 3) 每个 issue 用统一模板

```
标题: [V3-UI] <简洁动词短语>
标签: v3, ui, frontend|backend, enhancement, （可加 P0/P1/P2）
## 背景 / 动机
## 功能需求（编号列出，可测）
## 验收标准（勾选项）
## 涉及文件 / 组件
## 设计稿 & 设计文档引用（给出仓库内路径）
## 依赖（blocked by #，blocks #）
## 测试计划（要新增/修改哪些测试）
## 范围之外（Out of scope）
```

## 4) 要创建的 issue 清单（按此拆分，标题/依赖照写）

> 顺序即建议落地顺序；请在 issue 正文里用 `Blocked by`/`Blocks` 串好依赖。

### #A [V3-UI] 主题层 & 中文排版基础（theme.ts / index.html / i18n）🟢
- 需求：背景加深并与卡片分层；`typography.fontFamily` 与 mono 加 `Noto Sans SC` 等 CJK 回退；`index.html` Google Fonts 追加 `Noto+Sans+SC`；`MuiCssBaseline` 注入蓝图网格 + 冷光背景；`statusColors` 增加 `starting`；卡片渐变底 + HUD 角（可封装 `HudCard`）。中文（`lang=zh`）下关闭 `overline`/标签的大写与宽字距、正文行高 1.55→1.7。
- 验收：中英切换下排版均正常；网格/辉光在 reduced-motion 下不刺眼；数字仍用 tabular mono。
- 参照：MASTER.md 配色/字体；改动清单 §1、§8。

### #B [V3-UI] 共享视觉基元（StatusDot / Skeleton / HudCard）🟢
- 需求：`StatusDot` 给 live（running/ok/starting）态加脉冲圈 + 辉光，`prefers-reduced-motion` 关动画，保留「点+文字」双编码；通用骨架行组件；`HudCard` 角标封装（若 #A 未做）。
- 依赖：Blocked by #A。
- 参照：改动清单 §3。

### #C [V3-UI] 应用外壳 & 顶栏精修（App.tsx）🟡
- 需求：**去掉 `🐾` emoji，换内联 SVG 爪印 logo**（设计系统禁止 emoji-as-icon，优先级最高）；角色切换改 segmented control（仅 dev/无注入角色时显示，逻辑不变）；右侧加「中枢可达 · 鉴权正常」健康徽章（数据源：对 Hub 连通性/Bearer 校验，或先读 `api` 现状/占位）；AppBar 底部发光下划线。
- 依赖：Blocked by #A、#B。
- 参照：预览顶栏；改动清单 §2。

### #D [V3-UI] Agent 控制台精修（AgentConsole.tsx）🟢
- 需求：左栏行尾显示 last-event 时间；选中行 accent 左边框强调；`status.isLoading` 用骨架行替代「Loading…」；主详情区状态头加 mono「最后更新 HH:MM:SS」。
- 依赖：Blocked by #B。
- 参照：pages/agent-console.md；改动清单 §4。

### #E ⭐ [V3-UI] 引导式「添加监控」向导（新功能）— MonitorWizard 🟡（最大块）
> 这是**之前不存在的新功能**：把现在「一个对话框里下拉选类型再填表单」改成分步引导。功能需求见 §5（完整列在该 issue 正文）。
- 依赖：Blocked by #B、#F；Blocks（无）。
- 参照：预览里「+ 添加」流程；pages/agent-console.md 的 EmptyState/ConfigForm；改动清单 §5。

### #F [V3-UI] 配置表单重做（SchemaForm 模板 + PasswordWidget）🟡
- 需求：为 `@rjsf/mui` 提供自定义 `FieldTemplate` + `ObjectFieldTemplate`，落地预览字段样式（label 在上、helper 在下、必填 `*`、行内错误就近显示、两列栅格 full 跨列、聚焦绿光）；新增 `PasswordWidget`（show/hide）；保留现有 `PathWidget`（Tauri 原生选择器）；保留 `liveValidate=false`+提交聚焦首错，但错误改为就近字段展示。
- 依赖：Blocked by #A。Blocks #E。
- 参照：改动清单 §6；ui-ux-pro-max §8 Forms。

### #G [V3-UI] Hub 仪表盘精修（HubDashboard.tsx）🟡
- 需求：顶部机群健康汇总条（N 台 · 正常/降级/离线，判定由 `HubStatus.acks` 最后心跳新鲜度推出，无需新接口）；`useQuery(hubStatus)` 加 `refetchInterval:5000`；机器卡片 hover 抬升 + 可点下钻该机详情（其 monitors + 最近事件）；自监控 `self` 的 metrics 从 `JSON.stringify(<pre>)` 改为指标 tile（复用 `MonitorMetrics` 的 Tile）。卡片**暂只显示 在线/离线 + last-seen**（per-machine CPU/MEM 见 #H）。
- 依赖：Blocked by #A、#B。
- 参照：pages/hub-dashboard.md；改动清单 §7。

### #H [V3-BE] Hub /status 暴露 per-agent 最近快照（后端，解锁机群指标）🔴
- 背景：`HubStatus.servers` 现仅 `{id,name,ip,port,enabled}`，无每台机器的实时指标；poller 已在轮询 agent，需落地「最近一次状态快照」并在 `/status` 返回。
- 需求：每个 server 附最近 `state`/关键 metrics（cpu/mem 等）与 `last_seen`；定义返回契约；不破坏现有 OpenClaw/事件契约。
- 依赖：Blocks #G 的卡片迷你指标条。
- 参照：`taskpaw_v3/hub/server/poller.py`、`store.py`、`app.py`；改动清单 §7。

## 5) #E 向导的完整功能需求（请原样写进 #E 正文）

**目标**：新增引导式添加流程，支持单个监控，也支持一个「服务」一次性创建多个监控项（preset），这是当前缺失的能力。

1. **入口**：Agent 控制台左栏「+ 添加」按钮，以及无监控时空状态的主 CTA，均打开向导（模态或全屏抽屉）；ESC/点遮罩可关闭，关闭前若已填写则二次确认。
2. **Step 1 选择服务**：网格卡片，数据来自 `api.plugins()` 返回的 `plugins`（过滤 `system===true`）**与** `presets`（如 moomoo 是 preset，会创建 4 项 life-sign 监控）。每卡显示：图标、`display_name`、一行描述。图标按 `type_id`/preset id 建前端映射表（预览已含 lada/comfyui/folder/process/heartbeat/tcp/state_file/custom + moomoo）；描述缺失时前端兜底。选中卡片高亮，「继续」启用；键盘可达、focus 可见。
3. **Step 2 配置**：渲染所选项的表单——plugin 用其 `json_schema`（经重做后的 SchemaForm，#F）；preset 用其 `monitors[].config` 预填（可能是多个监控的合并/分步表单）。顶部「返回」回 Step 1。必填校验、错误就近显示；路径字段用 `PathWidget`，密码用 `PasswordWidget`，存量密钥显示 `***` 不回显真值。
4. **Step 3 复核**：键值复核表，密码脱敏；点「添加监控」提交：plugin → `api.addMonitor({type_id, config})`；preset → 对其每个 monitor 批量 `addMonitor`（任一失败要可见报错并允许重试）。
5. **成功/失败**：成功后关闭向导并**自动选中新建项**（复用现有 `onDone(savedName)` 落到详情页 Start/Edit）；失败显示后端 `detail` 文案，不静默。
6. **编辑模式复用**：从现有监控点「编辑配置」直接进 Step 2，**类型锁定**、`name` 只读，跳过 Step 1，Step 3 可选。
7. **步骤指示器 & 动效**：序号圆点 + 连接线，active/done 态；过渡 150–300ms、可中断、`prefers-reduced-motion` 降级；模态从触发源弹出（scale+fade）。
8. **i18n**：所有文案走 `i18n.ts`（中英），新增 key；中文标点与排版按 §2 处理。
9. **a11y**：步骤切换后焦点移到新步首个可交互元素；服务卡是真正可聚焦按钮；表单标签关联控件。

**验收标准（示例）**：选 Lada → 填表 → 复核 → 新建并自动选中；选 moomoo preset → 一次创建 4 个监控；中文模式全流程文案/排版正确；reduced-motion 下无动画；键盘可全程操作。

**范围之外**：插件 icon 字段下沉到后端（先前端映射）；preset 的高级分步表单优化。

## 6) 产出与创建方式

1. 在 `docs/specs/2026-06-29-v3-ui-polish.md` 写一份**总览**（背景 + issue 列表 + 依赖图 + 落地顺序），作为 tracking 文档。
2. 用 `gh issue create` 逐个创建上面的 #A–#H（标题、标签、正文按模板；正文里用 `Blocked by`/`Blocks` 串依赖，可先创建再补 issue 号）。若 `gh` 未认证或受限，则改为在 `docs/specs/` 下每个 issue 落一个 `*.md` 规格文件，并在总览里列出待创建清单 + 现成的 `gh issue create` 命令。
3. 不要开始写任何前端实现代码；不要 commit/push。完成后回报：创建了哪些 issue（号/链接）、依赖关系、建议的第一张要做的 issue。
