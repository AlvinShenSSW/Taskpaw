# TaskPaw V3 — UI/UX 精修改动清单（预览 → 代码映射）

> 配套预览：`v3-ui-polish-preview.html`（暗色全应用 + 中/EN 切换 + 添加向导）。
> 本清单把预览里的每处改动映射到 `taskpaw_v3/ui/` 的具体文件，并标出后端依赖与工作量，方便逐项落地。
> 约定：🟢 纯前端可独立完成；🟡 需要少量后端/契约配合；分级 S/M/L = 小/中/大。

---

## 0. 总体方向

- 沿用现有设计系统 token（暗色 OLED、navy slate + 绿色 accent、Fira Code/Sans），**不另起配色**。
- 科技感通过「克制的细节」实现：蓝图网格背景、HUD 边角刻线、顶栏发光下划线、状态点/仪表盘辉光——全部走 `prefers-reduced-motion` 降级。
- 新增**中文一等公民**支持：CJK 字体回退 + 针对中文关闭只适合拉丁字母的大写/宽字距。

---

## 1. 主题与全局样式 — `src/theme.ts` 🟢 M

| 改动 | 说明 |
|---|---|
| 背景加深 | `background.default` `#0F172A → #0B1120`，与卡片 `paper` 拉开层次。 |
| **CJK 字体栈** | `typography.fontFamily` 改为 `'"Fira Sans","Noto Sans SC",system-ui,"PingFang SC","Microsoft YaHei",sans-serif'`；mono 同理加 `"Noto Sans SC"` 回退。`index.html` 的 Google Fonts 链接追加 `Noto+Sans+SC`。 |
| 蓝图网格背景 | 通过 `MuiCssBaseline.styleOverrides.body` 注入两层 `linear-gradient` 网格 + 一层 `radial-gradient` 冷光。 |
| `starting` 状态色 | `statusColors` 增加 `starting:"#38BDF8"`（用于启动中 pulse）。 |
| 卡片渐变 + HUD 角 | `MuiCard` styleOverrides：`background:linear-gradient(...)` + `::before/::after` 角标（或封装一个 `<HudCard>`）。 |

> CJK 细节见 §8。

---

## 2. 应用外壳 / 顶栏 — `src/App.tsx` 🟡 S–M

| 改动 | 说明 |
|---|---|
| **去掉 🐾 emoji** | 第 42 行 `🐾 TaskPaw` 换成内联 SVG 爪印 logo（预览里有现成路径）。**这是 MASTER.md 明令禁止的 emoji-as-icon，优先修。** |
| 顶栏健康徽章 | 右侧加「中枢可达 · 鉴权正常」pill（带发光状态点）。🟡 数据源：agent 端可由对 Hub 的连通性/Bearer 校验得出；若暂无该状态，先静态占位或读 `acks` 新鲜度。 |
| 角色切换做成 segmented control | 现有 `Tabs` 换成预览里的 `.seg` 胶囊样式（仅 dev/无注入角色时显示，逻辑不变）。 |
| 顶栏发光下划线 | `AppBar` 底部 `::after` 渐变线（纯样式）。 |
| 语言切换 | i18n 已支持 zh（见 `i18n.ts`），已在 Settings 里。可选：顶栏加一个 EN/中 快捷切换。 |

---

## 3. 状态点 — `src/components/StatusDot.tsx` 🟢 S

- 增加 `live`（running/ok/starting）时的**脉冲圈**（`::after` + keyframes），并 `@media (prefers-reduced-motion: reduce)` 关闭动画。
- 加 `box-shadow:0 0 8px currentColor` 辉光。
- 保持「点 + 文字标签」双编码（color-not-only，a11y 已满足）。

---

## 4. Agent 控制台 — `src/views/AgentConsole.tsx` 🟢 S–M

| 改动 | 说明 |
|---|---|
| 左栏行尾显示 last-event 时间 | `mrow` 右侧 mono 时间戳（数据已有 `last`/事件时间）。 |
| 选中行强调 | 选中态加 `border-left` 绿色 + 浅绿底（预览已做）。 |
| 骨架屏替代「Loading…」 | `status.isLoading` 时渲染 skeleton 行，而非纯文字。 |
| 主详情区 last-updated | 状态头右侧加 mono「更新于 HH:MM:SS」。 |

---

## 5. ⭐ 添加监控 = 引导式向导（最大块） — 新建 `src/views/MonitorWizard.tsx`，替换 `AgentConsole.tsx` 里的 `MonitorDialog` 🟡 L

当前 `MonitorDialog`：一个对话框里先 `<select>` 选类型，再塞 `SchemaForm`。改为三步向导：

1. **选择服务**（step 1）：服务卡片墙。
   - 数据源：`api.plugins()` 返回的 `plugins`（过滤 `system===true`）**＋** `presets`（moomoo 就是一个 preset，`PresetInfo`）。
   - 每张卡：图标 + `display_name` + 一行描述。🟡 **图标**：`PluginInfo` 目前无 icon 字段；前端按 `type_id` 建一张图标映射表（预览里已画好 lada/comfyui/folder/process/heartbeat/tcp/state_file/custom + moomoo）。后续可考虑后端给每个插件加 `icon` 字段。
   - 卡描述同理：可前端按 type_id 兜底，或后端给 `description`。
2. **配置**（step 2）：进入所选服务的表单（见 §6），顶部有「返回」回到 step 1。
3. **复核**（step 3）：键值复核表 + 「添加监控」提交。提交后自动选中新建项（沿用现有 `onDone(savedName)` 逻辑）。

- 步骤指示器：序号圆点 + 连接线，active/done 态（预览已实现）。
- 保留现有契约：`api.addMonitor({type_id, config})`、edit 模式仍可直接进 step 2（编辑时类型锁定）。

---

## 6. 配置表单重做 — `src/components/SchemaForm.tsx`（+ 自定义 widgets/templates）🟡 M–L

当前用 `@rjsf/mui` 的 `<Form>`，默认布局较糙。三选一（建议 B）：

- **A** 仅靠 MUI 主题覆盖 `MuiTextField`/`MuiFormLabel`：标签上置、聚焦绿光、间距统一。改动小但可控度有限。
- **B（建议）** 提供自定义 RJSF `FieldTemplate` + `ObjectFieldTemplate`，落地预览里的字段样式（label 在上、helper 在下、必填 `*`、行内错误态、两列栅格 `full` 跨列）。
- **C** 用现成 widgets 增强：
  - `PasswordWidget`（新）：密码框 + show/hide 切换（预览已示范）。当前 `PathWidget.tsx` 已实现路径 Browse（Tauri 原生选择器），保留。
- 校验体验保持现状思路（`liveValidate=false`、提交时聚焦首个错误），但错误改为**就近字段**展示而非仅顶部 summary（ui-ux-pro-max `error-placement`）。

---

## 7. Hub 仪表盘 — `src/views/HubDashboard.tsx` 🟡 M（含后端依赖）

| 改动 | 前端/后端 | 说明 |
|---|---|---|
| 机群健康汇总条 | 🟡 | 顶部「N 台 · 正常/降级/离线」计数。判定可由 `acks[server_id]` 最后心跳新鲜度推出（无需新接口）。 |
| 自动刷新 | 🟢 | `useQuery(hubStatus)` 加 `refetchInterval:5000`（Agent 端已有，Hub 端当前**没有**）。 |
| 机器卡片可点下钻 | 🟢 | 卡片 hover 抬升、选中高亮；点击展开该机详情（其 monitors + 最近事件）。 |
| **卡片内 CPU/MEM 迷你条** | 🔴🟡 **后端依赖** | `HubStatus.servers` 目前只有 `{id,name,ip,port,enabled}`，**无 per-machine 指标**。要显示需 Hub 在 `/status` 暴露每个 agent 的最近快照（poller 已在轮询 agent，需落地"最近状态"并返回）。**未做前先只显示 在线/离线 + last-seen。** |
| 自监控改 tiles | 🟢 | `self` 的 metrics 现在是 `JSON.stringify` 进 `<pre>`（第 102–106 行），改为指标 tile（复用 `MonitorMetrics` 的 Tile）。 |

---

## 8. ⭐ 中文排版处理（贯穿）— `src/theme.ts` + 各组件 + `src/i18n.ts` 🟢 M

中文是一等公民，现有 Fira Code/Sans 不含 CJK 字形，需专门处理：

- **字体回退**：见 §1（Noto Sans SC + 系统中文字体）。
- **关闭只适合拉丁字母的样式**：`overline`/`glab`/tile 标签当前用 `text-transform:uppercase` + 大 `letter-spacing`，对中文应关闭（大写对中文无效、宽字距让中文松散）。做法：在根节点 `lang="zh"` 时通过 CSS/`sx` 条件关闭（预览用 `html[lang="zh"]` 选择器示范）。MUI 侧可在 theme 里对相关 variant 用 `:lang(zh)` 覆盖。
- **行高**：中文正文行高略调高（1.55 → 1.7）。
- **数字仍用 tabular mono**：时间戳/指标/计数保持 Fira Code 等宽数字，不受中文影响。
- **i18n 文案**：`i18n.ts` 已有 zh；本轮新增的向导/健康徽章/服务描述等需补 key。注意中文标点（、，：（）)与半角混排。

---

## 9. 建议落地顺序

1. **快赢**：§2 去 emoji + SVG logo、§3 状态点 pulse、§7 自监控 tiles、§4 骨架屏。（🟢 S，半天内可见效）
2. **主题层**：§1 token/字体/网格 + §8 CJK。（影响全局，先定调）
3. **向导 + 表单**：§5 + §6（最大块，建议拆成两个 PR：先表单模板，再向导壳）。
4. **Hub 深化**：§7 自动刷新 + 健康汇总（acks）→ 下钻；**per-machine 指标另开后端 issue**。

> 每条行为变更都要带测试（仓库规则：`uv run pytest`；前端 `npm test`）。本清单不含 commit/push（按 CLAUDE.md 约定，需显式要求）。
