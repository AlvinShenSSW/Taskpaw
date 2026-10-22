// 媒体处理中心 V3: the status vocabulary (build spec §3) and how a backend row maps onto it.
// Pure functions only; the components and tests share them.
import type {
  FilmRow, FilmTiming, LtxTier, PackageRow, RestoreParams, RestoreTiming, StepKey, TargetPreflight, WorkflowFilms,
  WorkflowStep,
} from "../../api";
import { LTX_STAGES, plainText, readLtxProgress, record, type LtxSnapshot } from "../ltxProgress.helpers";
import {
  readBasicProgress, readScanProgress, readSplitNote, type BasicSnapshot, type ScanSnapshot, type SplitNote,
} from "../restorePhase.helpers";

export type ChipTone = "neutral" | "green" | "blue" | "amber" | "red";
export type StatusKey =
  | "pending" | "receiving" | "postprocess_error" | "inbox" | "restore" | "encode" | "denoise" | "asr" | "translate"
  | "return" | "scrape" | "match" | "waiting" | "checking" | "ready" | "multi" | "nomatch" | "offline" | "permission" | "duplicate"
  | "unknown" | "archiving" | "cleanup" | "completed" | "skipped" | "stopping" | "failed" | "intake_attention";

export const STATUS_META: Record<StatusKey, [string, ChipTone]> = {
  pending: ["待处理", "amber"],
  receiving: ["等待收件检查", "blue"],
  postprocess_error: ["成品封面待处理", "amber"],
  inbox: ["待领取", "blue"],
  // Not in the V3 vocabulary (U1c): Jasna runs but no LTX stage yet (loading, mosaic scan, BasicVSR++).
  restore: ["修复中", "green"],
  encode: ["LTX · Encode", "green"],
  denoise: ["LTX · Denoise", "green"],
  asr: ["语音识别", "blue"],
  translate: ["字幕翻译", "blue"],
  return: ["回传成品", "blue"],
  scrape: ["Miko 完整后处理", "blue"],
  match: ["演员匹配", "blue"],
  // Not in the V3 vocabulary: a queued asr / translate / return / scrape stage that is not running
  // (paused, or its component is off); the backend's note says why. Never 「正在处理」.
  waiting: ["等待处理", "blue"],
  // Not in the V3 vocabulary: a film waiting for 确认入库 whose target preflight has not answered
  // yet (not polled now, or still loading); never 「待确认」 before the check (IR05).
  checking: ["检查中", "blue"],
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
  // Not in the V3 vocabulary: a local stage whose stop the runner has not confirmed yet (`unknown`).
  // Never 「本机任务已停止」 and never retryable until the backend says so (`can_retry`).
  stopping: ["正在确认停止", "blue"],
  // Not in the V3 vocabulary: a local stage that failed and can be retried (§4.4 last row).
  failed: ["处理失败", "red"],
  intake_attention: ["收件需要处理", "amber"],
};

export const REVIEW_STATES: StatusKey[] = ["checking", "ready", "multi", "nomatch", "offline", "permission", "duplicate"];
export const EXCEPTION_STATES: StatusKey[] = [
  "postprocess_error", "offline", "permission", "duplicate", "unknown", "cleanup", "stopping", "failed", "intake_attention",
];
export const ACTIVE_STATES: StatusKey[] = ["restore", "encode", "denoise", "asr", "translate", "return", "scrape", "match"];

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

// The fixed eleven steps in order; the backend sends every one (film and package rows alike), so
// a missing key only reads as not started.
export function lifecycle(steps: WorkflowStep[] | null | undefined): WorkflowStep[] {
  const byKey = new Map((steps ?? []).map((s) => [s.key, s]));
  return STEP_KEYS.map((key): WorkflowStep => byKey.get(key) ?? { key, state: "pending", note: null, progress: null });
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

// The local GPU restore is running (any phase).
export const isRestoring = (status: StatusKey) => status === "restore" || status === "encode" || status === "denoise";

// ── the running 修复 step (U1c) ─────────────────────────────────────────
// starting: no console line yet (Jasna loads its models); working: console lines, no known bar;
// split: the auto split logged its plan; scan → basic (BasicVSR++ spans) → ltx: Jasna's bars.
export type RestorePhase = "starting" | "working" | "split" | "scan" | "basic" | "ltx";
export interface RestoreLive {
  phase: RestorePhase;
  startedAt: number | null; // epoch ms
  console: string | null;
  split: SplitNote | null;
  scan: ScanSnapshot | null;
  basic: BasicSnapshot | null;
  ltx: LtxSnapshot | null;
  metrics: Record<string, unknown>; // the step's parse state, for the shared progress cards
  params: RestoreParams | null;
  timing: RestoreTiming | null; // U1e: the film's phase times so far
}

export function restoreLive(step: WorkflowStep | undefined): RestoreLive | null {
  if (!step || step.key !== "restore" || step.state !== "running") return null;
  const metrics = record(step.ltx) ? step.ltx : {};
  const ltx = readLtxProgress(metrics);
  const scan = ltx ? null : readScanProgress(metrics);
  const basic = ltx || scan ? null : readBasicProgress(metrics);
  const split = readSplitNote(metrics);
  const console = plainText(step.console, 300)?.trim() || null;
  const started = typeof step.started_at === "string" ? Date.parse(step.started_at) : NaN;
  const phase: RestorePhase = ltx ? "ltx" : scan ? "scan" : basic ? "basic" : split ? "split" : console ? "working" : "starting";
  const params = readParams(step.params);
  return {
    phase, startedAt: Number.isFinite(started) ? started : null, console, split, scan, basic, ltx, metrics, params,
    timing: readRestoreTiming(step.timing),
  };
}

// ── 用时 (U1e): how long a film took, read defensively ──────────────────
const seconds = (v: unknown) => (typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : null);

export function readRestoreTiming(value: unknown): RestoreTiming | null {
  if (!record(value)) return null;
  const stages: Record<string, number> = {};
  if (record(value.ltx_stages)) {
    for (const [name, v] of Object.entries(value.ltx_stages)) {
      const s = seconds(v);
      if (s !== null && name.length <= 32) stages[name] = s;
    }
  }
  return {
    scan: seconds(value.scan) ?? 0, basicvsr: seconds(value.basicvsr) ?? 0, ltx: seconds(value.ltx) ?? 0,
    speed_test: seconds(value.speed_test) ?? 0, ltx_stages: stages, ltx_runs: int(value.ltx_runs) ?? 0,
    wall: seconds(value.wall) ?? 0,
  };
}

export function readFilmTiming(value: unknown): FilmTiming | null {
  if (!record(value)) return null;
  const stages: Record<string, number> = {};
  if (record(value.stages)) {
    for (const [key, v] of Object.entries(value.stages)) {
      const s = seconds(v);
      if (s !== null) stages[key] = s;
    }
  }
  return {
    stages, restore_breakdown: readRestoreTiming(value.restore_breakdown),
    total_restore_to_translate: seconds(value.total_restore_to_translate), queue_wait: seconds(value.queue_wait),
  };
}

// Whether a restore timing has any LTX time to show (a reused or BasicVSR-only film has none).
export const hasLtxTime = (t: RestoreTiming | null): t is RestoreTiming => !!t && (t.ltx > 0 || t.ltx_runs > 0);

export const LTX_STAGE_ZH: Record<(typeof LTX_STAGES)[number], string> = {
  scan: "扫描", encode: "编码", denoise: "去噪", compose: "合成",
};

// The four LTX stages in Jasna's order (a stage that never ran reads 0), then any other name.
export function ltxStageTimes(t: RestoreTiming): Array<[string, string, number]> {
  const rows: Array<[string, string, number]> = LTX_STAGES.map((s) => [s, LTX_STAGE_ZH[s], t.ltx_stages[s] ?? 0]);
  for (const [name, v] of Object.entries(t.ltx_stages)) {
    if (!(LTX_STAGES as readonly string[]).includes(name)) rows.push([name, "", v]);
  }
  return rows;
}

// The 用时 block's stage rows, in lifecycle order: only stages that finished with a time.
export const TIMING_STAGES: Array<[string, string]> = [
  ["restore", "修复"], ["asr", "识别"], ["translate", "翻译"], ["return", "回传"], ["scrape", "整理"],
];

// The step line's duration under a finished step (U1e); the board's stage keys match the step keys.
export function stepDurations(timing: FilmTiming | null | undefined): Partial<Record<StepKey, number>> {
  const out: Partial<Record<StepKey, number>> = {};
  for (const key of STEP_KEYS) {
    const v = timing?.stages[key];
    if (typeof v === "number") out[key] = v;
  }
  return out;
}

// 「1:05」: hours and minutes (the queue card's 全程).
export function fmtHoursMinutes(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value < 0) return "—";
  const minutes = Math.round(value / 60);
  return `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}`;
}

// ── 本片参数 (U1c): the effective launch, read defensively ──────────────
const int = (v: unknown) => (typeof v === "number" && Number.isSafeInteger(v) && v >= 0 ? v : null);
const text = (v: unknown, limit = 128) => plainText(v, limit)?.trim() || null;

export function readParams(value: unknown): RestoreParams | null {
  if (!record(value)) return null;
  const argv = text(value.argv, 4096);
  if (!argv) return null;
  const tier = TIER_ORDER.find((t) => t === value.ltx_model) ?? null;
  const overlap = value.overlap === "on" || value.overlap === "off" ? value.overlap : "auto";
  return {
    ltx_model: tier, model_name: text(value.model_name), fast: value.fast === true,
    precision: value.fast === true ? "nvfp4" : "int8", auto_split: int(value.auto_split) ?? 0,
    large_canvas: value.large_canvas === true, overlap, codec: text(value.codec),
    source_codec: text(value.source_codec), cq: int(value.cq),
    match_source_bitrate: value.match_source_bitrate === true, maxrate_bps: int(value.maxrate_bps),
    source_bitrate_bps: int(value.source_bitrate_bps), detection_model: text(value.detection_model),
    clip_size: int(value.clip_size), temporal_overlap: int(value.temporal_overlap), tier: text(value.tier) ?? "1080p",
    source_width: int(value.source_width), source_height: int(value.source_height), gpu: text(value.gpu),
    gpu_vram_mib: int(value.gpu_vram_mib), preset: text(value.preset) ?? "",
    preset_label: text(value.preset_label) ?? text(value.preset) ?? "", preset_auto: value.preset_auto === true, argv,
  };
}

// 「预设 RTX 5070 Ti · 16 GB（自动）」: the header of the 本片参数 block; empty without a preset.
export const presetText = (p: RestoreParams) =>
  p.preset ? `预设 ${p.preset_label || p.preset}${p.preset_auto ? "（自动）" : ""}` : "";

const mbps = (bps: number) => `${(bps / 1e6).toFixed(1)} Mbps`;
const ON_OFF = (on: boolean) => (on ? "开" : "关");
const OVERLAP: Record<RestoreParams["overlap"], string> = { auto: "自动", on: "开", off: "关" };

export function capText(p: RestoreParams): string {
  if (p.maxrate_bps !== null) {
    return `${mbps(p.maxrate_bps)}${p.source_bitrate_bps !== null ? `（原片 ${mbps(p.source_bitrate_bps)}）` : ""}`;
  }
  return p.match_source_bitrate ? "未读到原片码率，按 Jasna 默认上限" : "按 Jasna 默认上限";
}

// 编码: with the auto split on, Jasna smart-renders the split's spans in the source codec, but only
// once it planned a split; skipped / no mosaics restores the whole video in the configured codec.
export function codecText(p: RestoreParams, split: SplitNote | null): string {
  const cq = `CQ ${p.cq ?? "—"}`;
  const configured = (p.codec ?? "—").toUpperCase();
  if (!p.source_codec) return `${configured} · ${cq}`;
  const source = p.source_codec.toUpperCase();
  if (!split) return `${configured} · ${cq}（若自动拆分则沿用原片 ${source}）`;
  return split.kind === "planned" ? `${source} · ${cq}（自动拆分沿用原片编码）` : `${configured} · ${cq}`;
}

// The labelled rows of the drawer's 「本片参数」 block.
export function paramRows(p: RestoreParams, split: SplitNote | null = null): Array<[string, string]> {
  const quality = p.ltx_model ? TIER_SHORT[p.ltx_model] : "—";
  const size = p.source_width !== null && p.source_height !== null ? `${p.source_width}×${p.source_height} · ` : "";
  return [
    ["画质", `${quality}（${p.model_name ?? "—"}）`],
    ["快速模式", p.precision],
    ["自动拆分", p.auto_split > 0 ? `${p.auto_split}%` : "关"],
    ["大画布", ON_OFF(p.large_canvas)],
    ["减少重叠", OVERLAP[p.overlap]],
    ["编码", codecText(p, split)],
    ["码率上限", capText(p)],
    ["检测", p.detection_model ?? "—"],
    ["片段 / 重叠", `${p.clip_size ?? "—"} / ${p.temporal_overlap ?? "—"}`],
    ["输入", `${size}${p.tier} 档`],
    ["GPU", [p.gpu, fmtVram(p.gpu_vram_mib)].filter(Boolean).join(" ") || "—"],
  ];
}

// The queue card's one line: 「标准 · nvfp4 · 上限 12.2 Mbps」.
export function paramSummary(p: RestoreParams): string {
  const cap = p.maxrate_bps !== null ? `上限 ${mbps(p.maxrate_bps)}` : "默认码率上限";
  return [p.ltx_model ? TIER_SHORT[p.ltx_model] : null, p.precision, cap].filter(Boolean).join(" · ");
}

// 「已用」: mm:ss under an hour, else h:mm:ss; a clock ahead of the start reads 00:00.
export function elapsedClock(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (n: number) => String(n).padStart(2, "0");
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`;
}

export const SPLIT_LABEL: Record<SplitNote["kind"], string> = {
  planned: "已完成自动分段，正在切分片段",
  skipped: "未自动分段 · 整片使用 LTX",
  no_mosaics: "未发现马赛克 · 整片使用 LTX",
};
// The same fact once a later phase runs.
export const SPLIT_NOTE: Record<SplitNote["kind"], string> = {
  ...SPLIT_LABEL,
  planned: "已自动分段：大马赛克片段用 LTX，其余用 BasicVSR++",
};

// 「修复中 · 已用 mm:ss」, or before Jasna printed anything 「正在启动 Jasna（加载模型）」.
export function heartbeatText(live: RestoreLive, now: number): string {
  const head = live.phase === "starting" ? "正在启动 Jasna（加载模型）" : "修复中";
  return live.startedAt === null ? head : `${head} · 已用 ${elapsedClock(now - live.startedAt)}`;
}

// What the preflight says about a film waiting for 确认入库: a target error, else the candidate count.
// `missing` (the chosen performer record is gone) asks for a new choice, as no match does.
// No preflight yet reads 「检查中」, not ready.
export function reviewStatus(preflight: TargetPreflight | null | undefined): StatusKey {
  if (!preflight) return "checking";
  const code = preflight.error_code;
  if (code) return code === "missing" ? "nomatch" : code;
  const n = preflight.candidates?.items.length;
  if (n === 0) return "nomatch";
  if (n !== undefined && n > 1) return "multi";
  return "ready";
}

export function filmStatus(row: FilmRow, preflight?: TargetPreflight | null): StatusKey {
  const stage = row.stage ?? "";
  switch (row.group) {
    case "planning": return "pending";
    case "done": return "completed";
    case "confirm": return reviewStatus(preflight);
    case "archiving": return stage === "archive" && row.status === "unknown" ? "unknown" : "archiving";
    case "attention":
      if (stage === "scrape") return "postprocess_error";
      if (stage.startsWith("cleanup")) return "cleanup";
      if (stage === "intake") return "intake_attention";
      if (stage === "archive") return row.status === "unknown" ? "unknown" : "failed";
      if (LOCAL_STAGES.has(stage) && row.status === "unknown") return "stopping";
      return "failed";
    default: // active
      if (stage === "restore") {
        if (row.status !== "running") return "inbox";
        const ltx = ltxOf(row);
        if (!ltx) return "restore"; // U1c: loading, the mosaic scan or BasicVSR++
        return ltx.stage_name === "denoise" ? "denoise" : "encode";
      }
      if (stage === "asr" || stage === "translate" || stage === "return" || stage === "scrape") {
        return row.status === "running" ? stage : "waiting";
      }
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
export function matchesStage(item: WorkItem, filter: StageFilter): boolean {
  const status = item.status;
  // a waiting stage sits in the bucket of the stage it waits for
  if (status === "waiting" && item.kind === "film") {
    if (filter === "processing") return item.row.stage === "asr" || item.row.stage === "translate";
    if (filter === "preparing") return item.row.stage === "return" || item.row.stage === "scrape";
  }
  switch (filter) {
    case "all": return true;
    case "inbox": return status === "inbox" || status === "receiving";
    case "pending": return status === "pending";
    case "processing": return ["restore", "encode", "denoise", "asr", "translate"].includes(status);
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

// "none": nothing to press (the stop is still being confirmed, or the backend offers no retry).
export type ExceptionAction = "retry-scrape" | "retry" | "review" | "reconcile" | "cleanup" | "open-package" | "none";
// Title, description and the one action of an exception card (§4.4).
export function exceptionInfo(item: WorkItem, intakeReasons: (pkg: PackageRow) => string): [string, string, string, ExceptionAction] {
  const note = item.kind === "film" ? item.row.note : null;
  // 重试 only where the backend names a stage to retry (`can_retry`, board `retry_stage`)
  const canRetry = item.kind === "film" && item.row.can_retry;
  switch (item.status) {
    case "postprocess_error":
      return ["最终封面尚未准备好", "已有 NFO 或封面不能跳过本次后处理。", canRetry ? "重试后处理" : "", canRetry ? "retry-scrape" : "none"];
    case "offline": return ["目标 NAS 暂时离线", "来源保留，稍后重试。", "重试", "review"];
    case "permission": return ["目标暂不可写", "检查权限后重试，来源保留。", "重试", "review"];
    case "duplicate": return ["目标已有同名影片包", "不会覆盖已有片源，请核对目标。", "查看冲突", "review"];
    case "unknown": return ["入库结果待核对", "后台先检查目标和记录，暂不重新上传或清理来源。", "核对结果", "reconcile"];
    case "cleanup": return ["已入库 · 来源待清理", note || "仅重试来源清理，不重复上传。", "重试清理", "cleanup"];
    case "stopping": return ["正在确认停止", "等待本机任务确认停止；原始来源和已完成结果保留，确认前不能重试。", "", "none"];
    case "intake_attention":
      return ["收件需要处理", item.kind === "package" ? intakeReasons(item.pkg) : note || "", "打开文件夹", "open-package"];
    default: {
      const stage = item.kind === "film" ? stageName(item.row.retry_stage || item.row.stage) : "";
      return [stage ? `${stage}失败` : "处理失败", note || (canRetry ? "原始来源保留，可重试本阶段。" : "原始来源保留。"),
        canRetry ? "重试" : "", canRetry ? "retry" : "none"];
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
  const gpu = [m.gpu, fmtVram(m.vram_mib)].filter(Boolean).join(" ");
  return gpu ? `${m.name} · ${gpu}` : m.name;
}

// VRAM in whole GB, as the backend's reasons spell it (8151 MiB → 8 GB).
export const vramGb = (mib: number | null | undefined) => (mib ? Math.round(mib / 1024) : null);
export const fmtVram = (mib: number | null | undefined) => (vramGb(mib) === null ? "" : `${vramGb(mib)} GB`);
export const gpuLabel = (gpu: WorkflowFilms["gpu"] | undefined) =>
  [gpu?.name, fmtVram(gpu?.vram_mib)].filter(Boolean).join(" ");
// What the workflow restores on this card, as the backend states it (`gpu.max_output`): phase 1 caps
// every card at 1080p, so a 16 GB card is no 4K device. Unknown reads as the safe 1080p.
export const outputLimit = (gpu: WorkflowFilms["gpu"] | undefined): string =>
  `修复输出上限 ${gpu?.max_output === "4K" ? "4K" : "1080p"}`;

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
