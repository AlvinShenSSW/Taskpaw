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
  agent: {
    monitors: "Monitors", events: "Events",
    monitorsTitle: "{{machine}} — monitors",
    noMonitors: "No monitors yet — add one to start watching this machine.",
    selectPrompt: "Select or add a monitor.",
    recentEvents: "{{machine}} — recent events",
    autoManaged: "Auto-managed system monitor — always on.",
    stoppedHint: "Stopped — click Start to run it, or Edit config to change settings.",
    addMonitor: "Add monitor", editMonitor: "Edit “{{name}}”",
    saveChanges: "Save changes", noSelectableTypes: "No selectable monitor types.",
    loadingConfig: "Loading config…",
    deleteTitle: "Delete monitor “{{name}}”?",
    deleteBody: "This removes it from this agent's config. It can't be undone.",
    unreachable: "Agent unreachable: {{error}}",
    updated: "Updated {{time}}",
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
  settings: {
    title: "Settings", language: "Language", languageHint: "Choose the interface language.",
    about: "About",
    aboutBody:
      "TaskPaw is a lightweight monitoring companion for your machines. It watches " +
      "local tasks and services — LADA video restore, ComfyUI, folders, processes — " +
      "and surfaces their status, progress, and events in one place. Run an agent on " +
      "each machine and aggregate them on a Hub.",
    author: "Author: 304",
    copyright: "© 2026 304. All rights reserved.",
    config: "Agent configuration",
    configHint: "Edit this machine's settings instead of hand-editing agent.yaml. Port/host changes apply after a restart.",
    machine: "Machine name", bindHost: "Network bind host", bindPort: "Network port",
    controlHost: "Control host (loopback)", controlPort: "Control port",
    apiToken: "API token", apiTokenHint: "Leave blank to keep the current token.",
    save: "Save", saved: "Saved.", restartNeeded: "Saved — restart the agent for port/host changes to take effect.",
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
  agent: {
    monitors: "监控", events: "事件",
    monitorsTitle: "{{machine}} — 监控",
    noMonitors: "还没有监控 —— 添加一个开始监视这台机器。",
    selectPrompt: "选择或添加一个监控。",
    recentEvents: "{{machine}} — 最近事件",
    autoManaged: "系统自动管理的监控 —— 始终开启。",
    stoppedHint: "已停止 —— 点击「启动」运行,或「编辑配置」修改设置。",
    addMonitor: "添加监控", editMonitor: "编辑「{{name}}」",
    saveChanges: "保存更改", noSelectableTypes: "没有可选的监控类型。",
    loadingConfig: "正在加载配置…",
    deleteTitle: "删除监控「{{name}}」?",
    deleteBody: "这会把它从本 agent 的配置中移除,无法撤销。",
    unreachable: "无法连接 Agent:{{error}}",
    updated: "更新于 {{time}}",
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
  settings: {
    title: "设置", language: "语言", languageHint: "选择界面语言。",
    about: "关于",
    aboutBody:
      "TaskPaw 是一款轻量的机器监控助手。它盯着本机的任务与服务 —— LADA 视频修复、" +
      "ComfyUI、文件夹、进程 —— 把状态、进度和事件集中呈现。每台机器跑一个 agent," +
      "再用 Hub 汇总。",
    author: "发起人:304",
    copyright: "© 2026 304. 保留所有权利。",
    config: "Agent 配置",
    configHint: "在这里改本机设置,不用手编 agent.yaml。端口/主机的改动需重启 agent 后生效。",
    machine: "机器名称", bindHost: "网络绑定地址", bindPort: "网络端口",
    controlHost: "控制地址(回环)", controlPort: "控制端口",
    apiToken: "API 令牌", apiTokenHint: "留空则保留当前令牌。",
    save: "保存", saved: "已保存。", restartNeeded: "已保存 —— 端口/主机改动需重启 agent 才生效。",
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
