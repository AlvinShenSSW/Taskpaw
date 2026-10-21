// 媒体处理中心 V3: the status vocabulary (build spec §3) and how a backend row maps onto it.
// Pure functions only; the components and tests share them.
import type {
  FilmRow, LtxTier, PackageRow, StepKey, TargetPreflight, WorkflowFilms, WorkflowStep,
} from "../../api";
import { readLtxProgress, type LtxSnapshot } from "../ltxProgress.helpers";

export type ChipTone = "neutral" | "green" | "blue" | "amber" | "red";
export type StatusKey =
  | "pending" | "receiving" | "postprocess_error" | "inbox" | "encode" | "denoise" | "asr" | "translate"
  | "return" | "scrape" | "match" | "ready" | "multi" | "nomatch" | "offline" | "permission" | "duplicate"
  | "unknown" | "archiving" | "cleanup" | "completed" | "skipped" | "stopped" | "failed" | "intake_attention";

export const STATUS_META: Record<StatusKey, [string, ChipTone]> = {
  pending: ["待处理", "amber"],
  receiving: ["等待收件检查", "blue"],
  postprocess_error: ["成品封面待处理", "amber"],
  inbox: ["待领取", "blue"],
  encode: ["LTX · Encode", "green"],
  denoise: ["LTX · Denoise", "green"],
  asr: ["语音识别", "blue"],
  translate: ["字幕翻译", "blue"],
  return: ["回传成品", "blue"],
  scrape: ["Miko 完整后处理", "blue"],
  match: ["演员匹配", "blue"],
  ready: ["待确认", "amber"],
  multi: ["多个目标候选", "amber"],
  nomatch: ["暂无演员匹配", "amber"],
  offline: ["目标 NAS 离线", "red"],
  permission: ["目标权限不足", "red"],
  duplicate: ["入库目标冲突", "red"],
  unknown: ["入库结果未知", "red"],
  // Not in the V3 vocabulary: a confirmed film between 「确认入库」 and its receipt (§6 「已确认，等待归档」).
  archiving: ["已确认，等待归档", "blue"],
  cleanup: ["已入库 · 待清理", "amber"],
  completed: ["已完成", "green"],
  skipped: ["已跳过 · 保留来源", "neutral"],
  stopped: ["本机任务已停止", "amber"],
  // Not in the V3 vocabulary: a local stage that failed and can be retried (§4.4 last row).
  failed: ["处理失败", "red"],
  intake_attention: ["收件需要处理", "amber"],
};

export const REVIEW_STATES: StatusKey[] = ["ready", "multi", "nomatch", "offline", "permission", "duplicate"];
export const EXCEPTION_STATES: StatusKey[] = [
  "postprocess_error", "offline", "permission", "duplicate", "unknown", "cleanup", "stopped", "failed", "intake_attention",
];
export const ACTIVE_STATES: StatusKey[] = ["encode", "denoise", "asr", "translate", "return", "scrape", "match"];

// ── tiers (U1 V2) ───────────────────────────────────────────────────────
export const TIER_ORDER: LtxTier[] = ["standard", "good", "high"];
export const TIER_SHORT: Record<LtxTier, string> = { standard: "标准", good: "良好", high: "高" };
export const TIER_LONG: Record<LtxTier, string> = { standard: "标准画质", good: "良好画质", high: "高画质" };
export const tierLong = (tier: LtxTier | null | undefined) => (tier ? TIER_LONG[tier] : "无需修复");

// ── lifecycle step line (H2 R5 + U1 V3) ─────────────────────────────────
export const STEP_KEYS: StepKey[] =
  ["intake", "plan", "restore", "asr", "translate", "return", "scrape", "confirm", "archive", "cleanup", "done"];
export const STEP_LABEL: Record<StepKey, string> = {
  intake: "入库扫描", plan: "待处理", restore: "修复", asr: "识别", translate: "翻译", return: "回传",
  scrape: "整理", confirm: "待确认", archive: "归档", cleanup: "清理", done: "完成",
};

// The fixed eleven steps in order. A backend without the 待处理 step gets one: waiting while the
// film is unplanned, done once intake is done and the film has moved on.
export function lifecycle(steps: WorkflowStep[] | null | undefined, group?: string): WorkflowStep[] {
  const byKey = new Map((steps ?? []).map((s) => [s.key, s]));
  return STEP_KEYS.map((key): WorkflowStep => {
    const sent = byKey.get(key);
    if (sent) return sent;
    if (key === "plan") {
      const intakeDone = byKey.get("intake")?.state === "done";
      const state = group === "pending" ? "waiting" : intakeDone ? "done" : "pending";
      return { key, state, note: null, progress: null };
    }
    return { key, state: "pending", note: null, progress: null };
  });
}

// The highlighted step: the first one running, waiting or failed; else the next not started; else the last.
export function currentStep(steps: WorkflowStep[]): number {
  const active = steps.findIndex((s) => s.state === "running" || s.state === "waiting" || s.state === "failed");
  if (active >= 0) return active;
  const next = steps.findIndex((s) => s.state === "pending");
  return next >= 0 ? next : steps.length - 1;
}

// ── status derivation ───────────────────────────────────────────────────
const LOCAL_STAGES = new Set(["restore", "asr", "translate", "return"]);

export function restoreStep(row: { steps?: WorkflowStep[] | null }): WorkflowStep | undefined {
  return (row.steps ?? []).find((s) => s.key === "restore");
}

export function ltxOf(row: { steps?: WorkflowStep[] | null }): LtxSnapshot | null {
  const ltx = restoreStep(row)?.ltx;
  return ltx && typeof ltx === "object" ? readLtxProgress(ltx) : null;
}

// What the preflight says about a film waiting for 确认入库: a target error, else the candidate count.
export function reviewStatus(preflight: TargetPreflight | null | undefined): StatusKey {
  if (!preflight) return "ready";
  if (preflight.error_code) return preflight.error_code;
  const n = preflight.candidates?.length;
  if (n === 0) return "nomatch";
  if (n !== undefined && n > 1) return "multi";
  return "ready";
}

export function filmStatus(row: FilmRow, preflight?: TargetPreflight | null): StatusKey {
  const stage = row.stage ?? "";
  switch (row.group) {
    case "pending": return "pending";
    case "done": return "completed";
    case "confirm": return reviewStatus(preflight);
    case "archiving": return stage === "archive" && row.status === "unknown" ? "unknown" : "archiving";
    case "attention":
      if (stage === "scrape") return "postprocess_error";
      if (stage.startsWith("cleanup")) return "cleanup";
      if (stage === "intake") return "intake_attention";
      if (stage === "archive") return row.status === "unknown" ? "unknown" : "failed";
      if (LOCAL_STAGES.has(stage) && row.status === "unknown") return "stopped";
      return "failed";
    default: // active
      if (stage === "restore") {
        if (row.status !== "running") return "inbox";
        return ltxOf(row)?.stage_name === "denoise" ? "denoise" : "encode";
      }
      if (stage === "asr" || stage === "translate" || stage === "return" || stage === "scrape") return stage;
      if (stage === "archive") return "archiving";
      return "receiving";
  }
}

export const packageStatus = (pkg: PackageRow): StatusKey =>
  pkg.group === "attention" ? "intake_attention" : "receiving";

// One entry of the workspace: a film, or an INBOX package that is not a film yet.
export type WorkItem =
  | { kind: "film"; id: string; row: FilmRow; status: StatusKey }
  | { kind: "package"; id: string; pkg: PackageRow; status: StatusKey };

export function workItems(data: WorkflowFilms | undefined, preflights: Record<string, TargetPreflight | null>): WorkItem[] {
  if (!data) return [];
  return [
    ...(data.films ?? []).map((row): WorkItem =>
      ({ kind: "film", id: row.film_id, row, status: filmStatus(row, preflights[row.film_id]) })),
    ...(data.packages ?? []).map((pkg): WorkItem =>
      ({ kind: "package", id: `pkg:${pkg.package_id}`, pkg, status: packageStatus(pkg) })),
  ];
}

export const itemTitle = (item: WorkItem) => (item.kind === "film" ? item.row.title || item.row.name : item.pkg.name);
// 「{id}」: the film number when it is known and differs from the title.
export function itemNumber(item: WorkItem): string | null {
  if (item.kind === "package") return null;
  const id = item.row.number || (item.row.title ? item.row.name : null);
  return id && id !== itemTitle(item) ? id : null;
}
export const itemActors = (item: WorkItem) => (item.kind === "film" ? (item.row.actors ?? []).join("、") : "");
export const itemResolution = (item: WorkItem) => (item.kind === "film" ? item.row.resolution ?? null : null);

// ── flow strip and queue stage filter (§4.1, §4.2) ──────────────────────
export type StageFilter =
  | "all" | "inbox" | "pending" | "processing" | "preparing" | "review" | "archive" | "completed" | "skipped";
export const STAGE_OPTIONS: Array<[StageFilter, string]> = [
  ["all", "全部阶段"], ["inbox", "收件待领取"], ["processing", "修复 / 字幕"], ["preparing", "回传 / 刮削 / 匹配"],
  ["review", "待确认"], ["archive", "入库 / 来源清理"], ["completed", "已完成"], ["skipped", "已跳过"],
];
export function matchesStage(status: StatusKey, filter: StageFilter): boolean {
  switch (filter) {
    case "all": return true;
    case "inbox": return status === "inbox" || status === "receiving";
    case "pending": return status === "pending";
    case "processing": return ["encode", "denoise", "asr", "translate"].includes(status);
    case "preparing": return ["return", "scrape", "match"].includes(status);
    case "review": return REVIEW_STATES.includes(status);
    case "archive": return ["archiving", "unknown", "cleanup"].includes(status);
    case "completed": return status === "completed";
    case "skipped": return status === "skipped";
  }
}

// ── copy for one status ─────────────────────────────────────────────────
export const STAGE_NAME: Record<string, string> = {
  intake: "入库扫描", restore: "修复", asr: "语音识别", translate: "翻译", return: "回传", scrape: "刮削",
  archive: "归档", cleanup_input: "INBOX 清理", cleanup_workcopy: "工作副本清理",
};
export const stageName = (stage: string | null | undefined) => (stage ? STAGE_NAME[stage] ?? stage : "");

// The translate step's 「{done} / {total}」 when its progress carries the counts.
function countsOf(progress: unknown): [number, number] | null {
  if (progress && typeof progress === "object") {
    const o = progress as Record<string, unknown>;
    const done = o.completed ?? o.done;
    if (typeof done === "number" && typeof o.total === "number") return [done, o.total];
  }
  if (typeof progress === "string") {
    const m = /(\d+)\s*\/\s*(\d+)/.exec(progress);
    if (m) return [Number(m[1]), Number(m[2])];
  }
  return null;
}

// The one line of a non-LTX stage block (§3).
export function stageLine(item: WorkItem): string {
  if (item.kind === "package") {
    return item.status === "receiving" ? "后台检查收件内容，稍后自动继续" : STATUS_META[item.status][0];
  }
  const row = item.row;
  const step = (row.steps ?? []).find((s) => s.key === row.stage);
  switch (item.status) {
    case "translate": {
      const c = countsOf(step?.progress);
      return c ? `中文翻译 · ${c[0]} / ${c[1]} 条字幕` : "中文翻译";
    }
    case "return": return "成品包回传";
    case "scrape": return "Miko 正在整理元数据、字幕与最终封面";
    case "receiving": return "后台检查收件内容，稍后自动继续";
    case "postprocess_error": return "最终封面尚未通过检查";
    case "inbox": return row.note || "等待设备领取";
    case "asr": return row.note ? `语音识别 · ${row.note}` : "语音识别";
    default: return row.note || STATUS_META[item.status][0];
  }
}

// The review card line (§4.3).
export function reviewLine(status: StatusKey, packageFiles: number | null, candidates: number | null): string {
  switch (status) {
    case "ready": return packageFiles ? `最终成品包 ${packageFiles} 个文件 · Miko 已完成` : "Miko 已完成";
    case "multi": return `${candidates ?? 2} 个演员 / 目录候选，必须明确选择`;
    case "nomatch": return "可人工指定目标或接管，来源会保留";
    case "offline": return "目标 NAS 离线，禁止提交";
    case "permission": return "目标目录不可写，禁止提交";
    case "duplicate": return "目标存在同名包，禁止覆盖";
    default: return STATUS_META[status][0];
  }
}

export type ExceptionAction = "retry-scrape" | "retry" | "review" | "reconcile" | "cleanup" | "open-package";
// Title, description and the one action of an exception card (§4.4).
export function exceptionInfo(item: WorkItem, intakeReasons: (pkg: PackageRow) => string): [string, string, string, ExceptionAction] {
  const note = item.kind === "film" ? item.row.note : null;
  switch (item.status) {
    case "postprocess_error": return ["最终封面尚未准备好", "已有 NFO 或封面不能跳过本次后处理。", "重试后处理", "retry-scrape"];
    case "offline": return ["目标 NAS 暂时离线", "来源保留，稍后重试。", "重试", "review"];
    case "permission": return ["目标暂不可写", "检查权限后重试，来源保留。", "重试", "review"];
    case "duplicate": return ["目标已有同名影片包", "不会覆盖已有片源，请核对目标。", "查看冲突", "review"];
    case "unknown": return ["入库结果待核对", "后台先检查目标和记录，暂不重新上传或清理来源。", "核对结果", "reconcile"];
    case "cleanup": return ["已入库 · 来源待清理", note || "仅重试来源清理，不重复上传。", "重试清理", "cleanup"];
    case "stopped": return ["本机任务已停止", "原始来源和已完成结果保留。", "重试", "retry"];
    case "intake_attention":
      return ["收件需要处理", item.kind === "package" ? intakeReasons(item.pkg) : note || "", "打开文件夹", "open-package"];
    default: {
      const stage = item.kind === "film" ? stageName(item.row.retry_stage || item.row.stage) : "";
      return [stage ? `${stage}失败` : "处理失败", note || "原始来源保留，可重试本阶段。", "重试", "retry"];
    }
  }
}

// ── formatting ──────────────────────────────────────────────────────────
export function fmtBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let v = n;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${i === 0 ? v : v.toFixed(1)} ${units[i]}`;
}

export function fmtDuration(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return "—";
  const s = Math.round(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}:${String(m).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}
export const fmtDurationMs = (ms: number | null | undefined) => fmtDuration(ms == null ? null : ms / 1000);

// 「本机 · RTX 5070 Ti 16 GB」 from a plan machine (backend `planning.machines`).
export function machineLabel(m: { name: string; gpu?: string | null; vram_mib?: number | null } | undefined, fallback = "本机"): string {
  if (!m) return fallback;
  const gpu = [m.gpu, m.vram_mib ? `${Math.round(m.vram_mib / 1024)} GB` : null].filter(Boolean).join(" ");
  return gpu ? `${m.name} · ${gpu}` : m.name;
}

export const fmtVram = (gb: number | null | undefined) => (gb == null ? "" : `${gb} GB`);
export const gpuLabel = (gpu: WorkflowFilms["gpu"]) =>
  [gpu?.name, fmtVram(gpu?.vram_gb)].filter(Boolean).join(" ");

// The package file note of the 图文确认 list (§6).
export function packageNote(role: string, path: string): string {
  const lower = path.toLowerCase();
  if (role === "folder_cover" || lower.endsWith("folder.jpg")) return "多视频 / 分卷目录封面 · 与最终 poster 一致";
  if (role === "main_video") return "修复成品";
  if (role.endsWith("subtitle")) return lower.endsWith(".zh.srt") ? "中文字幕 · 已验证非空" : "字幕";
  if (role === "nfo") return "本次 Miko 元数据";
  if (role === "poster") return "本次 Miko 最终封面";
  return role;
}

// Cleanup state of the drawer's Facts row.
export const CLEANUP_STATE: Record<string, string> = {
  pending: "入库完成后自动检查并清理本次来源", ready: "入库完成后自动检查并清理本次来源", running: "正在清理本次来源",
  succeeded: "本次来源已清理", skipped: "已跳过 · 保留来源", failed: "清理失败，来源保留",
  blocked: "来源已变化，禁止清理", unknown: "已入库 · 待清理",
};
