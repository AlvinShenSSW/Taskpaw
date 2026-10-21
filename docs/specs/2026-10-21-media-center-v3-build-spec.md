# Media Center (媒体处理中心) — V3 build spec

Sources:
- the approved design `D:/WORKSPACE/Taskpaw/design-system/taskpaw-v3/pages/media-workflow.md`;
- the prototype `preview/media-workflow-v3.html`: the V1 base layer is lines 9–122 and the V3 overlay lines 124–188. The overlay replaces the shell, overview, devices, local and settings pages.

Screenshots are in `C:/Users/304/AppData/Local/Temp/claude/d--WORKSPACE-Filmpaw/4ae57e4b-811b-4fbf-aac5-cf70da7fb0d7/scratchpad/v3-shots/`:
- desktop: `d-*.png`, including `d-drawer-F02/F03` and `d-confirm-F08/F09/F10`;
- 390px: `m-*.png`, clipped at about 500px. The 390px rules come from the CSS media queries.

When code and prototype conflict, the prototype and the md are authoritative, with the operator decisions at the end taking precedence.

## 1. Shell

- **The tab.** It is named 「媒体处理中心」; the prototype's 「影片工作流」 and the 「新增工作区」 chip are dropped. The nine internal pages exist only inside this tab, and the existing tabs keep working.
  - Phase 1: it stays the AgentConsole tab.
  - Later: the same `<MediaWorkspace/>` is mounted as a HubDashboard tab.
  - Do not add the prototype's outer 本机监控 tab.
- **Scope banner** (`.scope-banner`):
  - Text `TaskPaw / 媒体处理中心 / {page}`.
  - Background `#22C55E06`, bottom border `#223C33`, 11px `#B5CDBF`, padding 9px 25px.
  - At ≤900px it holds the 「工作区导航」 button: menu icon, ghost, 36px high.
- **Left rail.**
  - Size: 205px wide (180px at ≤1080px); background `#0E1727`; right border `#263650`; padding 22px 13px.
  - Heading: 「媒体处理中心」.
  - Nav items:
    - Base: min-height 46px, padding 10px 13px, radius 8, 13px `#A8B5C7`, 19px icon.
    - Hover: background `#FFFFFF05`, text `#F8FAFC`.
    - Current page (`aria-current=page`): background `#22C55E10`, border `#22C55E36`, text `#9BE7B5`.
  - Count badges appear only on 待确认 and 异常:
    - Base: mono 11px/20px, min-width 22px, background `#263650`, text `#CEDBEA`, radius 5.
    - On the active item: background `#22C55E19`, text `#A8EDBE`.
- **Nav items, in order:**

  | Key | Label | MUI icon |
  |---|---|---|
  | overview | 概览 | GridView |
  | queue | 影片队列 | Movie |
  | review | 待确认 | FactCheck |
  | exceptions | 异常 | WarningAmber |
  | devices | 设备 | DesktopWindows |
  | local | 本机 | Computer |
  | libraries | 片库与规则 | FolderOpen |
  | bridge | Mac 信息桥 | Lan |
  | settings | 设置 / 迁移 | Tune |

  No emoji.
- **Content area:** padding 27px (24px at ≤900px, 22px 15px at ≤600px); max-width 1650px.
- **Page state** lives in component state or zustand. On navigation, focus `<main tabIndex=-1>` and scroll to the top.
- **≤900px:**
  - The rail is hidden.
  - 「工作区导航」 opens a Dialog titled 「工作台导航」.
    - Close: a 44px icon button, aria 「关闭主导航」.
    - Width `min(380px, 100% - 24px)`; border `#415875`; radius 12; background `#101A2C`; backdrop `#030812CC`; items 49px.
    - Choosing an item closes the dialog. Keep the focus trap, Escape to close, and return focus to the trigger.
- **Omit:**
  - the demo bar and the mode switch;
  - every 演示/FIXTURE/静态快照/虚构 marker;
  - the footer notes;
  - 「演示场景切换」;
  - the overview's integration notice;
  - the remote and ordinary Agent mode pages.

## 2. Tokens

Scope these to the workspace: a `mediaTokens` constant used through sx, or a nested ThemeProvider. Do not retune the global theme.

**Colours:**

| Token | Hex |
|---|---|
| bg | `#0B1120` |
| surface | `#111A2E` |
| low | `#0D1626` |
| line | `#263650` |
| text | `#F8FAFC` |
| muted | `#A8B5C7` |
| green | `#22C55E` |
| green-light | `#97E8B4` |
| blue | `#75D2FA` |
| amber | `#FBC76A` |
| red | `#FDA4AF` |
| on-green | `#06210F` |

**Panel:**
- 1px line, radius 12, background `linear-gradient(170deg, #111A2E, #0D1626)`.
- Head: padding 18px 20px, bottom border, h2 17/600, subtitle 11px muted.
- Body: padding 20px.

**Buttons:**
- Base: min-height 44, padding 9px 14px, radius 8, 12px/500, background `#17243A`, border `#344861`, text `#E1EAF5`. Hover: background `#20324B`, border `#4C6584`.
- **Primary:** background and border `#22C55E`, text `#06210F`, weight 650. Hover `#4BD37E`.
- **Ghost:** transparent.
- **Danger:** text `#FECDD3`, border `#8C4052`, background `#4C213233`.
- **Small:** 11px, padding 8px 11px.
- **Icon:** 44×44.
- **Link:** blue, 12px.
- **Disabled:** opacity .52.

**Filter pill:**
- Min-height 44, padding 7px 13px, radius 7, border `#33455D`, muted 11px.
- Pressed: border `#48805D`, background `#22C55E0D`, text `#A0E8B9`.

**Input / select:**
- Background `#0D1728`, border `#3B4D66`, radius 8, min-height 44, 12px `#E0E9F5`, placeholder `#99ABC1`.
- Search: 245px wide (100% at ≤600px), left icon, padding-left 38px.

**In-page tabs:** 45px high, padding 8px 18px, 2px bottom border. Pressed: border `#22C55E`, text `#97E8B4`.

**Chips:** radius 5, padding 3px 8px, 10px. The text always carries the state.

| Variant | Background | Border | Text |
|---|---|---|---|
| neutral | `#1B2940` | `#344861` | `#C8D7E9` |
| green | `#22C55E0E` | `#22C55E42` | `#97E8B4` |
| blue | `#38BDF80C` | `#38BDF83D` | `#75D2FA` |
| amber | `#F59E0B0D` | `#F59E0B42` | `#FBC76A` |
| red | `#FB71850D` | `#FB718548` | `#FDA4AF` |

**Notices:** radius 8, padding 13px 16px, 12px.

| Variant | Border | Background | Text |
|---|---|---|---|
| info | `#3D536D` | `#1A2B414D` | `#C0D4E9` |
| amber | `#6A5229` | `#60451C20` | `#F9D492` |
| green | `#315641` | `#1B542420` | `#A7E9BC` |
| red | `#6E3544` | `#59233120` | `#FEC0C9` |

**Progress:** track `#27364B`, fill `linear-gradient(90deg, #169A4C, #39D373)`, height 5px in rows and 8px in the detail.

**Type:**
- h1: 29/1.3, letter-spacing -.7px, weight 600 (25px at ≤600px).
- Eyebrow: 10px, letter-spacing 1.4px, muted.
- Page description: 12px muted, max-width 650.
- h2: 17px. Film title: 14px in rows, 15px in cards.
- Meta: 10px. Body: 12–14px.
- Numbers: Fira Code, tabular. Stat number: mono 31px/500.
- Sans stack: `"Fira Sans","Noto Sans SC",system-ui,"Microsoft YaHei"`.

**Covers** (radius 7–9):

| Place | Size |
|---|---|
| row | 60×80 (48×64 at ≤600px) |
| card | 61×82 |
| peek | 48×64 |
| review card | 88×117 (66×88 at ≤1200px) |
| drawer | 116×154 |
| confirm | 108×144 |

The image is the real poster; without one, show a placeholder reading 「无封面」.

**Modal:**
- Border `#3B506C`, radius 14, background `#101A2C`, width `min(760px, 100% - 32px)`, max-height `100dvh - 40px`, shadow `0 25px 100px #000A`, backdrop `#030812BA` with blur 4.
- Sticky head: padding 20/24.
- Body: padding 24, scrolls.
- Foot: background `#0E1727`, top border, 10px note on the left (max-width 310), actions on the right.

**Drawer:** right side, `min(660px, 100%)`, radius `14px 0 0 14px`.

**Toast:** MUI Snackbar at the bottom centre; background `#22364A`, border `#4D6A81`, radius 9, 12px `#ECF6FF`, 4.8 s, aria-live polite.

**A11y:**
- Focus ring: `outline: 2px solid #75D2FA; outline-offset: 4px`.
- Skip link 「跳至主要内容」.
- Controls at least 44px.
- Dialogs: focus trap, Escape to close, focus returns to the trigger, focus goes to the title on open.
- Respect prefers-reduced-motion.
- No auto-advancing fake progress.
- Show controller, API, admission and GPU-owner health as separate indicators.

**Breakpoints:**

| Width | Changes |
|---|---|
| ≥1600 | queue 4 columns; padding 36 |
| ≤1200 | queue 2 columns; two-column layout `1.4fr / minmax(270px, 1fr)` |
| ≤960/900 | rail hidden; single column |
| ≤600 | stats 2×2; flow strip 3 columns; grids single column; heading actions move below |

## 3. Status vocabulary

Each status has a label and a chip variant:

| Status | Label | Chip |
|---|---|---|
| receiving | 等待收件检查 | blue |
| postprocess_error | 成品封面待处理 | amber |
| inbox | 待领取 | blue |
| encode | LTX · Encode | green |
| denoise | LTX · Denoise | green |
| asr | 语音识别 | blue (not in V3; small deviation) |
| translate | 字幕翻译 | blue |
| return | 回传成品 | blue |
| scrape | Miko 完整后处理 | blue |
| match | 演员匹配 | blue |
| ready | 待确认 | amber |
| multi | 多个目标候选 | amber |
| nomatch | 暂无演员匹配 | amber |
| offline | 目标 NAS 离线 | red |
| permission | 目标权限不足 | red |
| duplicate | 入库目标冲突 | red |
| unknown | 入库结果未知 | red |
| cleanup | 已入库 · 待清理 | amber |
| completed | 已完成 | green |
| skipped | 已跳过 · 保留来源 | neutral |
| stopped | 本机任务已停止 | amber |
| intake_attention | 收件需要处理 | amber (operator addition, for INBOX packages needing attention) |

Extra chips:
- 「人工接管 · 来源保留」 blue;
- 「输入变化 · 禁止清理」 red, for kept `blocked_changed` / `blocked_identity`;
- 「自动处理已暂停 · 仍待核对」 amber.

Groups:
- **Review:** ready, multi, nomatch, offline, permission, duplicate.
- **Exceptions:** postprocess_error, offline, permission, duplicate, unknown, cleanup, stopped, intake_attention.
- **Active:** encode, denoise, asr, translate, return, scrape, match.

**Stage block (non-LTX):** one line plus a 10px note 「此阶段不提供影片总体百分比」.

| Stage | Line |
|---|---|
| translate | 中文翻译 · {done} / {total} 条字幕 |
| return | 成品包回传 |
| scrape | Miko 正在整理元数据、字幕与最终封面 |
| receiving | 后台检查收件内容，稍后自动继续 |
| postprocess_error | 最终封面尚未通过检查 |
| inbox | the waiting reason |

**Stage block (LTX):**
- A green label 「Encode / 编码」 or 「Denoise / 去噪」, and mono 「{p}% 本阶段」.
- A progressbar with aria 「{title} {stage} 本阶段进度」.
- Mono 10px: 「{cur} / {total} 帧|步」 and 「{rate} frame/s|s/step」.
- Never show a film-overall percentage.

## 4. Pages

### 4.1 概览

**Heading:**
- eyebrow `WORKFLOW / 全局工作流`
- h1 「让每一部影片，顺利归位。」
- p 「从人工投片到验证入库，持续处理；只在演员与目标路径需要您决定时停下。」
- Actions:
  - ghost 「暂停新领取」 (pause icon), which toggles to 「恢复新领取」 (play icon); toasts 「已暂停新的领取；所有在途任务继续。」 and 「已恢复新任务领取。」
  - primary 「待确认 {n} →」.

**Paused notice (amber):** 「主控已暂停新领取」 / 「在途修复、翻译、回传与已确认入库继续运行。」

**Four stats:**

| Stat | Value | Sub-line |
|---|---|---|
| 正在处理 | {n} 部影片 | 「{k} 部 LTX · 其余步骤分别计数」 |
| 等待您的确认 | {n} 部影片 (amber) | 「按影片核对演员与最终路径」 |
| 需要处理 | {n} 项异常 | 「含入库未知、来源清理待重试」 |
| 计算设备 | 1 / 1 在线 | the GPU name and VRAM |

**Flow strip:** six buttons, min-height 84; name 10px; number mono 22px, amber for 待确认.

| Button | Counts |
|---|---|
| 收件 | inbox + receiving |
| 修复 / 字幕 | encode, denoise, asr, translate |
| 回传 / 刮削 | return, scrape, match |
| 待确认 | the review group |
| 入库 / 清理 | unknown, cleanup |
| 完成 | completed |

Clicking a button opens 影片队列 filtered to that stage.

**Left column:**
- Panel 「正在处理」 with subtitle 「当前阶段与设备 · 不合成影片总体百分比」 and link 「全部队列 →」. It shows 4 active rows.
- Panel 「安全抵达之后，再清理来源」 with subtitle 「正式片库永不作为清理目标」. Five step pills: 确认目标与清单 → 不覆盖提交 → 完整校验与回执 → 来源身份复核 → 清理清单内原片.

**Right column:**
- 「下一步，需要您」 with subtitle 「图文核对 · 一部一确认」 and chip 「{n} 待确认」.
  - Two peeks, each with a 「核对入库 →」 button.
  - Empty: 「待确认已处理」.
- 「设备协作」: one row for the local GPU.

### 4.2 影片队列

**Heading:**
- eyebrow `FILM QUEUE / 影片队列`
- h1 「每部影片，一条完整记录。」
- p 「修复、字幕、回传与归档共享影片身份；原始输入与处理后的成品包分别记录。」
- Action: ghost 「收件规则」 (opens settings).

**Toolbar:**
- Stage select (aria 「按影片阶段筛选」): 全部阶段 / 收件待领取 / 修复 / 字幕 / 回传 / 刮削 / 匹配 / 待确认 / 入库 / 来源清理 / 已完成 / 已跳过.
- Resolution select: 全部分辨率 / 4K / 1080p.
- Chip 「{n} 部」.
- Search 「搜索影片、演员或编号」: title, id and actor, case-insensitive, and it keeps the caret.

**Card:**
- Cover 61×82; title; 「{id} · {res}」; actor; chips; stage block.
- The lifecycle step line sits under the stage block (operator decision).
- Foot: 「本机」 or 「INBOX」, plus ghost small 「查看详情」.

**Packages:**
- In progress: 等待收件检查 cards with intake progress and 「打开文件夹」, and no drawer.
- Needing attention: on 异常.

**Empty state:** 「没有符合条件的影片」 / 「试试更改阶段、分辨率或搜索词。」

### 4.3 待确认

**Heading:**
- eyebrow `HUMAN REVIEW / 待确认`
- h1 「最后一步，由您确认。」
- p 「看最终封面、演员与目标路径，确认一次即可入库。完成后自动清理本次收件来源。」

**Info notice:** 「入库不会覆盖或删除已有片库。来源暂时无法清理时会保留，并显示“已入库 · 待清理”。」

**Pills:** 全部待确认 / 可核对入库 / 需要先处理, plus 「{n} 部影片 · 每次单独确认」.

**Review card:**
- 2 columns, cover 88×117, title, resolution chip, 「{id} · {actor}」, status chip.
- Target: 「{performer source_label} / {actor}」 or 「尚未选定目标」.
- One line:

  | Status | Line |
  |---|---|
  | ready | 最终成品包 {n} 个文件 · Miko 已完成 |
  | multi | {n} 个演员 / 目录候选，必须明确选择 |
  | nomatch | 可人工指定目标或接管，来源会保留 |
  | offline | 目标 NAS 离线，禁止提交 |
  | permission | 目标目录不可写，禁止提交 |
  | duplicate | 目标存在同名包，禁止覆盖 |

- Button: 「图文核对入库」 (primary) for ready, otherwise 「查看并处理」.

**Empty state:** 「当前没有待确认影片」 / 「已确认的影片可在队列中查看入库回执与清理结果。」

### 4.4 异常

**Heading:**
- eyebrow `EXCEPTIONS / 异常中心`
- h1 「停在可恢复的位置。」
- p 「失败不会变成重复入库；“已入库”与“原片已清理”分别记录。跳过与人工接管都会保留来源。」

**Pills:** 所有异常 / 入库结果未知 / 仅需清理来源.

**Card:** a 39×39 amber symbol box; title and chip; 「**{title}** · {id} · {res}」; a description; actions.

| Status | Title | Description | Action |
|---|---|---|---|
| postprocess_error | 最终封面尚未准备好 | 已有 NFO 或封面不能跳过本次后处理。 | 重试后处理 (existing retry stage) |
| offline | 目标 NAS 暂时离线 | 来源保留，稍后重试。 | 重试 |
| permission | 目标暂不可写 | 检查权限后重试，来源保留。 | 重试 |
| duplicate | 目标已有同名影片包 | 不会覆盖已有片源，请核对目标。 | 查看冲突 |
| unknown | 入库结果待核对 | 后台先检查目标和记录，暂不重新上传或清理来源。 | 核对结果 (an explanation dialog; the backend reconciles automatically) |
| cleanup | 已入库 · 来源待清理 | {kept reason} or 「仅重试来源清理，不重复上传。」 | 重试清理 (explanation dialog; the finalizer retries automatically) |
| stopped | 本机任务已停止 | 原始来源和已完成结果保留。 | 重试 |
| intake_attention | 收件需要处理 | the intake reason labels and file names | 打开文件夹 |
| failed local stage | the stage label | the attempt note | 重试 (retry stage) |

**Dialogs:**
- **「先核对入库结果」**
  - Subtitle: 「结果未知期间：禁止重复上传，禁止来源清理」.
  - Facts:
    - 本地记录: 「提交意图存在，最终回执未收到」
    - 必须核对: 「目标影片包、文件身份、完整校验与入库事务一致」
    - 不允许: 「盲目重传、覆盖同名包、先删来源」
- **「仅重试来源清理」**
  - Subtitle: 「不重新修复、不重新刮削、不重复上传」.
  - A receipt box.
  - Amber note: 「不会遍历删除收件目录，也不会把 NAS 正式片库当作清理根。」

**Empty state:** 「当前筛选没有异常」 / 「处理结果会保留在影片详情的事件记录中。」

### 4.5 本机 (reduced)

**Heading:**
- eyebrow `LOCAL AGENT / 本机`
- h1 「主控电脑，也是您的本机 Agent。」
- p 「{machine} · Windows · {GPU} {VRAM}。这一页只控制本机任务。」

**Notice:** 「本机兼任主控与 GPU Agent，不需要另一套窗口或第二份设备卡。」

**Device card:** name and GPU; chip 在线; metrics 显存容量 and 任务资格 (4K 优先 at ≥16 GB, otherwise 仅 1080p).

**Panel 「本机任务」:** the current film row. Empty: 「本机当前空闲」, plus, under 16 GB, 「全局队列中的 4K 影片不会派给这台 8 GB 设备。」

**Panel 「两个控制范围」:** static text.

**Not in phase 1:** 排空 and 停止本机任务, which would need dispatcher stop semantics.

### 4.6 Later pages: 设备, 片库与规则, Mac 信息桥, 设置 / 迁移

These stay in the rail, marked 「后续」. Each opens a page with its heading plus an EmptyState 「此功能将在多机工作流阶段提供」 and one line on its purpose.

- **片库与规则** also shows the true 「入库与来源清理规则」 panel with four rules: 输入与成品分开建清单 / 先验证完整入库，再清理 / 只重试未完成的清理 / 正式片库永不成为清理根.
- **设置** also shows the notice 「只投到主控的一个普通收件目录。INBOX 为目录别名……」, plus read-only INBOX, work directory and tool paths.

## 5. Drawer (660px)

**Head:** 「影片详情」 / `{id}` / a close button (aria 「关闭对话框」).

**Hero:**
- Cover 116×154.
- h2 24px title.
- Chips: status, extras, resolution.
- Actor.
- 「来源：主控收件目录 INBOX · 本机」.

The lifecycle step line sits under the hero (operator decision).

**Stage section:**
- **LTX:** the Jasna monitor's full `LtxProgress`: four stages, counters, rate, stage elapsed and remaining, run ETA, and the raw line in a fold. Note: 「由阶段日志提供；不包含翻译、回传、刮削与入库。」 No fake 100%; show 「—」 when a value is unobserved.
- **Missing LTX models:** the `LtxDownload` component with an `onRespond` prop, using H3 M1.
- **Other stages:** an info notice.

**Final-output panel:**
- Title 「本次 Miko 最终成品」 / 「Miko 完整后处理」.
- Subtitle-line: 「字幕：已验证的非空中文字幕。」 / 「本片无语音，不宣称已完成中文字幕。」 / 「字幕成果待后处理核对。」
- The package list.

**Facts** (label column 125px):

| Fact | Value |
|---|---|
| 目标 | the target or 「尚未确定目标」 |
| 原始来源 | 「主控 INBOX / {pkg} · 本次收件 {n} 项」 |
| 入库结果 | the receipt or 「尚未入库」 |
| 来源清理 | its state |

**Fold 「查看后台检查记录」:** 收件检查, 成品版本, 来源版本, 清理边界 (「只处理本次输入清单；新增文件、其他影片、父目录和 NAS 正式片库均不删除。」), plus the files and inputs tables.

**Foot:** note 「阶段结果与来源清理分别记录。」, ghost 「打开文件夹」, and a primary button that depends on the state:
- review state: 「查看并确认入库」;
- exception: its action;
- otherwise: 「关闭」.

## 6. 图文确认 dialog (760px)

**Head:** 「确认影片与入库目标」 / 「入库完成后自动清理本次收件来源」.

**Top section:**
- Cover 108px.
- 「{id} · MIKO 最终封面」 above an h3 20px title.
- Chips: resolution, 「本次后处理已完成」.
- 「演员：**{actor | 待选择}**」.
- 「修复成品 · 中文字幕已核对」 or 「修复成品 · 无语音，不加中文字幕标识」.

**Candidates:**
- Exactly one: pre-selected (operator decision).
- Several: no default. Show 「选择演员与目标」 with 2-column buttons (aria-pressed; border `#3C506A`, background `#0D1728`, min-height 75; selected `#45885D` with background `#22C55E0A`).
- None: a manual search box. Never auto-create an actor folder.

**Block notice** (amber), by condition:

| Condition | Text |
|---|---|
| unknown | 入库结果待核对，暂不能重新上传。 |
| not ready | 影片尚未完成准备，稍后自动继续。 |
| missing poster or package | 最终封面或文件包尚未准备好，请先处理。 |
| revision changed | 成品或来源已更新，请重新查看后确认。 |
| offline | 目标 NAS 暂时离线，请稍后重试。 |
| permission | 目标暂不可写，请检查权限后重试。 |
| duplicate | 目标已有同名影片包，不能覆盖；请打开文件夹核对。 |
| no choice yet | 请选择演员与目标路径。 |

**Target box:**
- Border `#345C47`, background `#0B1727`, radius 9.
- 「入库到」.
- Path in mono 11px on `#142B24` / `#284C3B` / `#A7E8BC`: `{target_path}\{film dir}`.
- 「保留所有已有片源；同名冲突不会覆盖。」

**Fold `<details open>` 「最终文件包 · {n} 个文件」:**

| File | Note |
|---|---|
| video | 修复成品 |
| .zh.srt | 中文字幕 · 已验证非空 |
| nfo | 本次 Miko 元数据 |
| poster | 本次 Miko 最终封面 |
| folder.jpg | 多视频 / 分卷目录封面 · 与最终 poster 一致 |

**Fold source box** (amber):
- Summary: 「入库后清理：主控 INBOX / {pkg} · {n} 个来源文件」.
- The file list, then 「只处理本次收件来源；若文件发生变化或仍被占用，将保留并显示待清理。」

**Foot:**
- Note: 「一次确认即可，剩余步骤由后台完成。」
- Ghost 「打开文件夹」.
- Primary **「确认入库」**: no checkbox, no risk statement.

**After confirming:** 「已确认，等待归档」, then the result 「入库完成」 or 「已入库 · 待清理」 with a receipt box and 「继续下一部」.

## 7. Backend additions needed (U1)

1. H3 M1/M2/M3, in flight.
2. Rows and detail gain:
   - `resolution` ("4K" | "1080p" | null), from the input media probes;
   - `actors` and `number` (NFO, final or input);
   - `receipt_id`;
   - `revisions` (final and input revision plus hash).
3. Target preflight `GET /control/workflow/films/{id}/target?performer_id=` → `{target_path, absent, error_code: offline | permission | duplicate | null}`, wrapping `filmpaw.target`. Its candidates field: `performers(q = NFO actor)` → 0 / 1 / many.
4. `/films` also returns:
   - `dispatch.state`, `dispatch.reason` and `paused`;
   - the GPU name and VRAM.
5. `POST /control/workflow/pause {paused: bool}`: a dispatcher flag that stops new claims while in-flight work continues. The finalizer keeps running.

## 8. Operator decisions (2026-10-21)

1. A unique performer candidate is pre-selected (V3). Several candidates are never pre-selected; no match needs a manual choice. This supersedes T06-P1 U3/U4.
2. The LTX detail uses the Jasna monitor's full `LtxProgress` inside the V3 drawer's stage section.
3. The four later pages stay in the rail, marked 「后续」.
4. The lifecycle step line stays, because the whole lifecycle must be observable: in queue cards under the stage block, and in the drawer under the hero.
5. INBOX packages needing attention show on 异常 with the amber chip 「收件需要处理」 and the intake reason labels.
6. Name: 「媒体处理中心」. Phase 1 mounts it in AgentConsole; it moves to the Hub later.
7. **No slogan headings** (operator, 2026-10-21: 「每个tab的类似每部影片，一条完整记录。这样的废话就不要占空间了」 — filler text like 「每部影片，一条完整记录。」 on every tab must not take up space).
   - Every page drops the slogan h1 and the description paragraph, for example 「让每一部影片，顺利归位。」, 「每部影片，一条完整记录。」, 「最后一步，由您确认。」, 「停在可恢复的位置。」 and 「先安排，再开工。」.
   - The eyebrow line goes too.
   - Each page keeps one compact title row: the page name, as in the rail label, at about 20px, with the page's actions on the right and the toolbar directly below.
   - Functional notices (amber or info notices that state a rule or a state) stay. Purely descriptive copy goes.
