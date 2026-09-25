import i18n from "i18next";
import { initReactI18next } from "react-i18next";

// V3 UI internationalization (#78). Default locale is Simplified Chinese; English
// is the other option. The visible selector lives in the Settings tab (#79); the
// choice persists in localStorage so it survives reloads. Backend-sourced schema
// field labels (plugin `description=`) stay English for now — out of scope (#78).

export type Lang = "zh-CN" | "en";
export const LANGS: { value: Lang; label: string }[] = [
  { value: "zh-CN", label: "中文" },
  { value: "en", label: "English" },
];
const STORE_KEY = "taskpaw.lang";

const en = {
  app: { agent: "Agent Console", hub: "Hub Dashboard", settings: "Settings", openSettings: "Settings", online: "Online" },
  common: {
    add: "Add", start: "Start", stop: "Stop", editConfig: "Edit config", delete: "Delete",
    cancel: "Cancel", loading: "Loading…", updating: "updating…", type: "Type", level: "Level",
    allLevels: "All levels", show: "show", hide: "hide", save: "Save",
  },
  state: {
    ok: "ok", idle: "idle", running: "running", degraded: "degraded",
    error: "error", stopped: "stopped", unknown: "unknown", disabled: "disabled",
    enabled: "enabled",
  },
  ai: {
    busy: "Running AI · {{tools}}", waiting: "Waiting for input", idle: "AI idle",
    presentOnly: "AI present · not reported", none: "No AI activity",
    presentUnreported: "present · not reported", unknown: "not reported",
    ago: "{{s}}s ago",
    duty: "busy {{busy}}/{{win}} min · {{pct}}%",
    tool: { busy: "busy", idle: "idle", waiting: "waiting" },
  },
  agent: {
    monitors: "Monitors", events: "Events",
    monitorsTitle: "{{machine}} — monitors",
    noMonitors: "No monitors yet — add one to start watching this machine.",
    recentEvents: "{{machine}} — recent events",
    recentEventsShort: "Recent events",
    autoManaged: "Auto-managed system monitor — always on.",
    stoppedHint: "Stopped — click Start to run it, or Edit config to change settings.",
    addMonitor: "Add monitor", editMonitor: "Edit “{{name}}”",
    saveChanges: "Save changes", noSelectableTypes: "No selectable monitor types.",
    loadingConfig: "Loading config…",
    deleteTitle: "Delete monitor “{{name}}”?",
    deleteBody: "This removes it from this agent's config. It can't be undone.",
    unreachable: "Agent unreachable: {{error}}",
    authDisabled:
      "Auth is disabled — no API token is set, so /status and /events accept any request. The bind guard keeps this loopback-only; set a token in Settings to require a Bearer token or to bind a LAN address.",
    updated: "Updated {{time}}",
    lastEvent: "Last event {{time}}",
  },
  wizard: {
    add: "Add a monitor", s1: "Choose service", s2: "Configure", s3: "Review",
    s1desc: "Pick the kind of service you want to watch on this machine.",
    continue: "Continue", back: "Back", review: "Review", addBtn: "Add monitor",
    saveBtn: "Save changes", svctype: "Service type",
    adapt: "Fields adapt to the {{name}} schema. Required fields are marked.",
    recap: "On add, the monitor is created and you land on its detail pane (Start / Edit config).",
    presetCreates: "This bundle creates {{count}} monitors:",
    closeTitle: "Discard this monitor?",
    closeBody: "Your entries will be lost.",
    discard: "Discard",
  },
  services: {
    lada: "Subtitle/translation pipeline — files, fps, GPU.",
    jasna: "Jasna video restore — per-file queue, unet-4x by resolution, GPU, optional AV-translate subtitles.",
    avsubs: "AV translate — a Simplified Chinese .srt named after each video in a library folder (the Japanese .ja.srt is only an intermediate, removed once translated); videos that already have subtitles are skipped and an existing subtitle is never overwritten; GPU shared with Jasna.",
    comfyui: "Image render queue depth and progress.",
    moomoo: "Trading server — life-signs heartbeat.",
    folder_watch: "Alert when files arrive or go idle.",
    process: "Is a named process alive on this host?",
    heartbeat: "Generic liveness ping with a max age.",
    tcp_check: "Probe a host:port for reachability.",
    state_file: "Read status from a JSON/state file.",
    custom_cmd: "Run a command and parse its output.",
  },
  hub: {
    fleet: "Fleet", manage: "Manage", events: "Events",
    server: "Server", allServers: "All servers",
    fleetTitle: "{{machine}} — fleet ({{count}} {{unit}})",
    agent: "agent", agents: "agents",
    noAgents: "No agents registered yet.",
    selfMonitor: "Hub host (self-monitor)",
    eventHistory: "event history",
    unreachable: "Hub unreachable: {{error}}",
    fleetHealth: "Fleet health",
    healthOk: "healthy", healthDegraded: "degraded", healthOffline: "offline",
    online: "online", offline: "offline",
    lastSeen: "last seen {{time}}", lastSeenNever: "never polled",
    machineMonitors: "monitors", machineEvents: "recent events",
    noMonitors: "No monitors reported.",
    cpu: "CPU", mem: "MEM",
    manageAgents: "Manage agents",
    mName: "Name", mIp: "IP / host", mPort: "Port",
    pollingToken: "Polling token", pollingTokenHint: "Must match each agent's API token. Save blank to clear it (unauthenticated polling).",
    clearToken: "Clear",
    deleteAgentTitle: 'Remove agent "{{name}}"?',
    deleteAgentBody: "The Hub will stop polling it and forget its history. This can't be undone.",
  },
  events: {
    none: "No events yet — they appear here as monitors report activity.",
    nowProcessing: "Now processing", currentFile: "current file",
    queue: "queue", queueDone: "{{done}} / {{total}} done", queueLeft: " · {{n}} left",
    vram: "vram", fps: "fps", eta: "ETA",
  },
  // #189 per-film pipeline view (PipelineProgress).
  pipeline: {
    step: { restore: "Restore", asr: "Transcribe", translate: "Translate" },
    stepOf: "Step {{k}} of {{n}}",
    upNext: "Up next", lastFinished: "Last finished",
    done: "Done", doneIn: "Done · {{d}}", failed: "Failed", skipped: "Skipped",
    queued: "Queued", notStarted: "Not started", after: "{{step}} must finish first",
    running: "In progress", left: "{{d}} left", elapsed: "{{t}} elapsed",
    waitGpu: "Waiting for GPU", waitGpuHeld: "Waiting for GPU ({{holder}})",
    waited: "waited {{t}}",
    sec: "{{n}} s", min: "{{n}} min", hmin: "{{h}} h {{m}} min",
    panel: { restore: "Restore · Jasna", asr: "Transcribe · WhisperJAV", translate: "Translate" },
    tile: {
      speed: "Speed", frames: "Frames", elapsed: "Elapsed", eta: "ETA", scene: "Scene", phase: "Phase",
      batches: "Batches", cues: "Translated",
    },
    cues: "{{done}} / {{total}} cues",
    gpuHeld: "GPU in use by “{{holder}}”",
    gpuHeldHint:
      "It starts here once that task finishes the GPU work on its current file — the two take turns per file and never share VRAM.",
    gpuFreeHint: "The GPU is about to free up — it starts on the next check.",
    translateQueued: "Translation queued — another film is being translated first.",
    q: {
      restored: "Restored {{a}} / {{b}}", subs: "Subtitles {{a}} / {{b}}",
      inProgress: " · in progress", nFailed: " · {{n}} failed", nSkipped: " · {{n}} skipped",
      done: "Done {{n}}", translating: "Translating {{n}}", failed: "Failed {{n}}",
      skipped: "Skipped {{n}}", queued: "Queued {{n}}", preDone: "Already subtitled {{n}}",
      legend: "Dark green = restored and subtitled; light green = in progress (restore or subtitles not finished yet)",
    },
    films: "Films", more: "{{n}} more",
    row: {
      done: "Done", failed: "Failed", skipped: "Skipped", pending: "Queued",
      queued: "Waiting to translate", waiting_gpu: "Waiting for GPU", active: "In progress",
      restore: "Restoring", asr: "Transcribing",
      translate: "Translating · in the background, no GPU",
      subsOnly: "Queued · already restored, subtitles only",
    },
  },
  settings: {
    title: "Settings", language: "Language", languageHint: "Choose the interface language.",
    about: "About",
    aboutBody:
      "TaskPaw is a lightweight companion for monitoring a fleet of your own " +
      "machines. Run an agent on each box and aggregate them on a Hub — watch " +
      "CPU / RAM / GPU / VRAM, long-running task progress (LADA / Jasna 4K video " +
      "restore with live percentage and ETA; AV-translate subtitles, after each " +
      "restore or as a standalone task over a whole library; " +
      "ComfyUI queues), processes, folders, and " +
      "services, with their status and events in one place. It even observes your " +
      "AI coding tools — Claude Code, Codex, Kimi — so you can tell at a glance " +
      "which machines are busy. Privacy-first: it reports state only, never your " +
      "content.",
    author: "Designed & developed by 304",
    copyright: "© 2026 304. All rights reserved.",
    config: "Agent configuration",
    configHint: "Edit this machine's settings instead of hand-editing agent.yaml. Port/host changes apply after a restart.",
    machine: "Machine name", bindHost: "Network bind host", bindPort: "Network port",
    controlHost: "Control host (loopback)", controlPort: "Control port",
    apiToken: "API token", apiTokenHint: "Leave blank to keep the current token.",
    save: "Save", saved: "Saved.", restartNeeded: "Saved — restart the agent for port/host changes to take effect.",
    llm: "LLM API",
    llmHint: "One OpenAI-compatible LLM endpoint for this agent (default xAI Grok; OpenRouter or a local Ollama work too). Changes apply immediately, no restart needed.",
    llmApiBase: "API base URL", llmModel: "Model", llmApiKey: "API key",
    llmApiKeyHint: "The TASKPAW_LLM_API_KEY environment variable takes precedence over this key. Leave blank to keep the stored key.",
    llmApiKeyEnv: "Provided by the TASKPAW_LLM_API_KEY environment variable — change it there.",
    llmSave: "Save LLM settings", llmClear: "Clear key", llmTest: "Test connection", llmTesting: "Testing…",
    llmTestOk: "Connected — {{model}} replied in {{latency}} ms.",
    llmTestTruncated: "The reply was truncated, but the connection works.",
    llmTestFail: "Connection failed: {{error}}",
    llmSaved: "LLM settings saved.", llmCleared: "Stored API key cleared.",
  },
};

const zh: typeof en = {
  app: { agent: "Agent 控制台", hub: "Hub 仪表盘", settings: "设置", openSettings: "设置", online: "在线" },
  common: {
    add: "添加", start: "启动", stop: "停止", editConfig: "编辑配置", delete: "删除",
    cancel: "取消", loading: "加载中…", updating: "更新中…", type: "类型", level: "级别",
    allLevels: "全部级别", show: "显示", hide: "隐藏", save: "保存",
  },
  state: {
    ok: "正常", idle: "空闲", running: "运行中", degraded: "降级",
    error: "错误", stopped: "已停止", unknown: "未知", disabled: "已禁用", enabled: "已启用",
  },
  ai: {
    busy: "在跑 AI · {{tools}}", waiting: "等待输入", idle: "AI 空闲",
    presentOnly: "AI 在场 · 未上报", none: "无 AI 活动",
    presentUnreported: "在场 · 未上报", unknown: "未上报",
    ago: "{{s}}s 前",
    duty: "忙 {{busy}}/{{win}} 分 · {{pct}}%",
    tool: { busy: "忙", idle: "空闲", waiting: "等待" },
  },
  agent: {
    monitors: "监控", events: "事件",
    monitorsTitle: "{{machine}} — 监控",
    noMonitors: "还没有监控 —— 添加一个开始监视这台机器。",
    recentEvents: "{{machine}} — 最近事件",
    recentEventsShort: "最近事件",
    autoManaged: "系统自动管理的监控 —— 始终开启。",
    stoppedHint: "已停止 —— 点击「启动」运行,或「编辑配置」修改设置。",
    addMonitor: "添加监控", editMonitor: "编辑「{{name}}」",
    saveChanges: "保存更改", noSelectableTypes: "没有可选的监控类型。",
    loadingConfig: "正在加载配置…",
    deleteTitle: "删除监控「{{name}}」?",
    deleteBody: "这会把它从本 agent 的配置中移除,无法撤销。",
    unreachable: "无法连接 Agent:{{error}}",
    authDisabled:
      "鉴权已禁用——未设置 API 令牌,/status 与 /events 接受任意请求。绑定守卫已将其限制在仅回环地址;请在「设置」中设置令牌以启用 Bearer 鉴权,或用于绑定 LAN 地址。",
    updated: "更新于 {{time}}",
    lastEvent: "最近事件 {{time}}",
  },
  wizard: {
    add: "添加监控项", s1: "选择服务", s2: "配置", s3: "复核",
    s1desc: "选择你想在这台机器上监控的服务类型。",
    continue: "继续", back: "返回", review: "复核", addBtn: "添加监控",
    saveBtn: "保存更改", svctype: "服务类型",
    adapt: "字段会随所选的 {{name}} schema 变化,必填项已标注。",
    recap: "添加后会创建该监控项,并自动跳转到它的详情页(启动 / 编辑配置)。",
    presetCreates: "该套件会创建 {{count}} 个监控:",
    closeTitle: "放弃这个监控?",
    closeBody: "你填写的内容会丢失。",
    discard: "放弃",
  },
  services: {
    lada: "字幕/翻译流水线 —— 文件、帧率、GPU。",
    jasna: "Jasna 视频修复 —— 逐文件队列、按分辨率启用 unet-4x、GPU、可选 AV 翻译字幕。",
    avsubs: "AV 翻译 —— 为片库文件夹里的每个视频生成与视频同名的简体中文 .srt（日文 .ja.srt 只是中间产物，翻译完成后删除）；已有字幕的视频跳过，已有的字幕绝不覆盖；与 Jasna 轮流使用 GPU。",
    comfyui: "图像渲染队列深度与进度。",
    moomoo: "交易服务 —— 生命体征心跳。",
    folder_watch: "文件到达或长时间空闲时告警。",
    process: "指定进程在本机是否存活?",
    heartbeat: "通用存活心跳,带最大时延。",
    tcp_check: "探测 host:port 可达性。",
    state_file: "从 JSON/状态文件读取状态。",
    custom_cmd: "运行一条命令并解析其输出。",
  },
  hub: {
    fleet: "机群", manage: "管理", events: "事件",
    server: "服务器", allServers: "全部服务器",
    fleetTitle: "{{machine}} — 机群({{count}} {{unit}})",
    agent: "台", agents: "台",
    noAgents: "还没有注册任何 agent。",
    selfMonitor: "Hub 主机(自监控)",
    eventHistory: "事件历史",
    unreachable: "无法连接 Hub:{{error}}",
    fleetHealth: "机群健康",
    healthOk: "正常", healthDegraded: "降级", healthOffline: "离线",
    online: "在线", offline: "离线",
    lastSeen: "最后心跳 {{time}}", lastSeenNever: "从未轮询",
    machineMonitors: "监控项", machineEvents: "最近事件",
    noMonitors: "未上报监控项。",
    cpu: "CPU", mem: "内存",
    manageAgents: "管理 agent",
    mName: "名称", mIp: "IP / 主机", mPort: "端口",
    pollingToken: "轮询令牌", pollingTokenHint: "需与各 agent 的 API 令牌一致。保存空值 = 清除（不鉴权轮询）。",
    clearToken: "清除",
    deleteAgentTitle: "删除 agent「{{name}}」?",
    deleteAgentBody: "Hub 会停止轮询它并清除其历史记录，无法撤销。",
  },
  events: {
    none: "暂无事件 —— 监控产生活动时会显示在这里。",
    nowProcessing: "正在处理", currentFile: "当前文件",
    queue: "队列", queueDone: "{{done}} / {{total}} 完成", queueLeft: " · 剩 {{n}}",
    vram: "显存", fps: "帧率", eta: "预计剩余",
  },
  pipeline: {
    step: { restore: "修复", asr: "识别", translate: "翻译" },
    stepOf: "第 {{k}} 步 / 共 {{n}} 步",
    upNext: "下一个文件", lastFinished: "最近完成",
    done: "完成", doneIn: "完成 · {{d}}", failed: "失败", skipped: "已跳过",
    queued: "排队中", notStarted: "待开始", after: "等待{{step}}完成",
    running: "进行中", left: "约剩 {{d}}", elapsed: "已用 {{t}}",
    waitGpu: "等待 GPU", waitGpuHeld: "等待 GPU（{{holder}}）",
    waited: "已等 {{t}}",
    sec: "{{n}} 秒", min: "{{n}} 分", hmin: "{{h}} 小时 {{m}} 分",
    panel: { restore: "修复 · Jasna", asr: "识别 · WhisperJAV", translate: "翻译" },
    tile: {
      speed: "速度", frames: "帧", elapsed: "已用", eta: "约剩", scene: "场景", phase: "阶段",
      batches: "批次", cues: "已译",
    },
    cues: "{{done}} / {{total}} 句",
    gpuHeld: "GPU 正由「{{holder}}」使用",
    gpuHeldHint:
      "对方做完当前这部片子的 GPU 工作后，就轮到这里；两边按文件轮流，不会同时占用显存。",
    gpuFreeHint: "GPU 即将空出，下一次检查就会开始。",
    translateQueued: "翻译排队中 —— 先翻译前面的片子。",
    q: {
      restored: "修复 {{a}} / {{b}}", subs: "字幕 {{a}} / {{b}}",
      inProgress: " · 进行中", nFailed: " · 失败 {{n}}", nSkipped: " · 跳过 {{n}}",
      done: "完成 {{n}}", translating: "翻译中 {{n}}", failed: "失败 {{n}}",
      skipped: "跳过 {{n}}", queued: "排队 {{n}}", preDone: "已有字幕 {{n}}",
      legend: "深绿 = 修复和字幕都完成；浅绿 = 进行中（修复或字幕还没结束）",
    },
    films: "影片", more: "还有 {{n}} 部",
    row: {
      done: "完成", failed: "失败", skipped: "跳过", pending: "排队",
      queued: "等待翻译", waiting_gpu: "等待 GPU", active: "进行中",
      restore: "修复中", asr: "识别中", translate: "翻译中 · 后台进行，不占 GPU",
      subsOnly: "排队 · 已修复过，只补字幕",
    },
  },
  settings: {
    title: "设置", language: "语言", languageHint: "选择界面语言。",
    about: "关于",
    aboutBody:
      "TaskPaw 是一款轻量的机器监控助手,为「一人多机」而生。在每台机器上运行一个 " +
      "agent,由 Hub 统一汇总 —— 实时掌握 CPU / 内存 / GPU / 显存,长任务进度(LADA / " +
      "Jasna 4K 视频修复的实时百分比与预估时间;AV 翻译字幕,可在每次修复后生成,也可作为独立任务" +
      "处理整个片库;ComfyUI 队列),以及进程、" +
      "文件夹与服务的" +
      "状态和事件。它还能观测 Claude Code、Codex、Kimi 等 AI 编程工具的忙碌 / 空闲," +
      "让你一眼看清整个机队谁在干活。隐私优先:只上报状态,绝不读取任何内容。",
    author: "由 304 独立设计与开发",
    copyright: "© 2026 304. 保留所有权利。",
    config: "Agent 配置",
    configHint: "在这里改本机设置,不用手编 agent.yaml。端口/主机的改动需重启 agent 后生效。",
    machine: "机器名称", bindHost: "网络绑定地址", bindPort: "网络端口",
    controlHost: "控制地址(回环)", controlPort: "控制端口",
    apiToken: "API 令牌", apiTokenHint: "留空则保留当前令牌。",
    save: "保存", saved: "已保存。", restartNeeded: "已保存 —— 端口/主机改动需重启 agent 才生效。",
    llm: "LLM API",
    llmHint: "本机 agent 统一使用的 OpenAI 兼容大模型接口(默认 xAI Grok,也可用 OpenRouter 或本地 Ollama)。修改即时生效,无需重启。",
    llmApiBase: "API 地址", llmModel: "模型", llmApiKey: "API 密钥",
    llmApiKeyHint: "环境变量 TASKPAW_LLM_API_KEY 优先于此处的密钥。留空则保留已保存的密钥。",
    llmApiKeyEnv: "密钥由环境变量 TASKPAW_LLM_API_KEY 提供,如需更改请修改该环境变量。",
    llmSave: "保存 LLM 设置", llmClear: "清除密钥", llmTest: "测试连接", llmTesting: "测试中…",
    llmTestOk: "连接成功 —— {{model}} 用时 {{latency}} 毫秒响应。",
    llmTestTruncated: "回复被截断,但连接正常。",
    llmTestFail: "连接失败:{{error}}",
    llmSaved: "LLM 设置已保存。", llmCleared: "已清除保存的 API 密钥。",
  },
};

function initialLang(): Lang {
  const saved = (typeof localStorage !== "undefined" && localStorage.getItem(STORE_KEY)) as Lang | null;
  return saved === "en" || saved === "zh-CN" ? saved : "zh-CN"; // default Chinese
}

// Tell the Tauri shell the current UI language so the native close-confirm
// dialog follows it (#108). No-op in the browser/dev (no shell): guarded by the
// injected __TASKPAW__, then a dynamic import so the bundle doesn't hard-require
// the Tauri API (same pattern as PathWidget). Fire-and-forget.
function syncLangToShell(l: Lang): void {
  if (typeof window === "undefined" || !window.__TASKPAW__) return;
  import("@tauri-apps/api/core")
    .then(({ invoke }) => invoke("set_ui_lang", { lang: l }))
    .catch(() => {
      /* not in a Tauri shell, or command unavailable — ignore */
    });
}

const lng = initialLang();
i18n.use(initReactI18next).init({
  resources: { en: { translation: en }, "zh-CN": { translation: zh } },
  lng,
  fallbackLng: "en",
  interpolation: { escapeValue: false }, // React already escapes
});
if (typeof document !== "undefined") document.documentElement.lang = lng;
syncLangToShell(lng); // report the initial language to the shell (#108)

export function setLang(l: Lang): void {
  try {
    localStorage.setItem(STORE_KEY, l);
  } catch {
    /* storage may be unavailable — still switch for this session */
  }
  i18n.changeLanguage(l);
  if (typeof document !== "undefined") document.documentElement.lang = l;
  syncLangToShell(l); // keep the shell's close dialog in the chosen language (#108)
}

export function currentLang(): Lang {
  return (i18n.language as Lang) === "en" ? "en" : "zh-CN";
}

export default i18n;
