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
  app: { agent: "Agent Console", hub: "Hub Dashboard", settings: "Settings", openSettings: "Settings" },
  common: {
    add: "Add", start: "Start", stop: "Stop", editConfig: "Edit config", delete: "Delete",
    cancel: "Cancel", loading: "Loading…", updating: "updating…", type: "Type", level: "Level",
    allLevels: "All levels",
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
  },
  hub: {
    fleet: "Fleet", events: "Events",
    fleetTitle: "{{machine}} — fleet ({{count}} {{unit}})",
    agent: "agent", agents: "agents",
    noAgents: "No agents registered yet.",
    selfMonitor: "Hub host (self-monitor)",
    eventHistory: "event history",
    unreachable: "Hub unreachable: {{error}}",
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
  },
};

const zh: typeof en = {
  app: { agent: "Agent 控制台", hub: "Hub 仪表盘", settings: "设置", openSettings: "设置" },
  common: {
    add: "添加", start: "启动", stop: "停止", editConfig: "编辑配置", delete: "删除",
    cancel: "取消", loading: "加载中…", updating: "更新中…", type: "类型", level: "级别",
    allLevels: "全部级别",
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
  },
  hub: {
    fleet: "机群", events: "事件",
    fleetTitle: "{{machine}} — 机群({{count}} {{unit}})",
    agent: "台", agents: "台",
    noAgents: "还没有注册任何 agent。",
    selfMonitor: "Hub 主机(自监控)",
    eventHistory: "事件历史",
    unreachable: "无法连接 Hub:{{error}}",
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
  },
};

function initialLang(): Lang {
  const saved = (typeof localStorage !== "undefined" && localStorage.getItem(STORE_KEY)) as Lang | null;
  return saved === "en" || saved === "zh-CN" ? saved : "zh-CN"; // default Chinese
}

const lng = initialLang();
i18n.use(initReactI18next).init({
  resources: { en: { translation: en }, "zh-CN": { translation: zh } },
  lng,
  fallbackLng: "en",
  interpolation: { escapeValue: false }, // React already escapes
});
if (typeof document !== "undefined") document.documentElement.lang = lng;

export function setLang(l: Lang): void {
  try {
    localStorage.setItem(STORE_KEY, l);
  } catch {
    /* storage may be unavailable — still switch for this session */
  }
  i18n.changeLanguage(l);
  if (typeof document !== "undefined") document.documentElement.lang = l;
}

export function currentLang(): Lang {
  return (i18n.language as Lang) === "en" ? "en" : "zh-CN";
}

export default i18n;
