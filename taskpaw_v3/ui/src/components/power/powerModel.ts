// PWR-2/PWR-3: formatting and pure helpers shared by 媒体处理中心's power additions and the 功耗 tab.
// The backend reports W and Wh; the UI shows 度 (kWh). Every figure is an estimate (估算).
import type {
  EnergyRate, FilmEnergy, LivePower, LtxTier, PlanPrediction, PowerActivity, PowerHistory, Resolution,
} from "../../api";

export const kwh = (wh: number | null | undefined) => (wh == null ? null : wh / 1000);
// 「0.84」 below 10 度, a short form for the compact lines
export function fmtKwh(wh: number | null | undefined): string {
  const v = kwh(wh);
  if (v === null) return "—";
  return v < 0.01 ? "<0.01" : v.toFixed(2);
}
export function fmtKwhShort(wh: number | null | undefined): string {
  const v = kwh(wh);
  if (v === null) return "—";
  return v >= 1 ? v.toFixed(1) : v < 0.01 ? "<0.01" : v.toFixed(2);
}
// table cells: 3 significant-ish digits
export function fmtKwhCell(wh: number | null | undefined): string {
  const v = kwh(wh);
  if (v === null) return "—";
  return v >= 100 ? v.toFixed(0) : v >= 10 ? v.toFixed(1) : v.toFixed(2);
}
export const fmtW = (w: number | null | undefined) => (w == null ? "—" : String(Math.round(w)));

export interface Split { gpu: number; cpu: number; other: number; total: number; noGpu: boolean }
export function splitOf(p: Pick<LivePower, "w" | "gpu_w" | "cpu_w" | "other_w"> | null | undefined): Split | null {
  if (!p) return null;
  const gpu = p.gpu_w ?? 0, cpu = p.cpu_w ?? 0, other = p.other_w ?? 0;
  return { gpu, cpu, other, total: gpu + cpu + other || p.w, noGpu: p.gpu_w == null };
}
export function sumSplits(list: Array<Split | null>): Split {
  return list.reduce<Split>((a, s) => (s ? { gpu: a.gpu + s.gpu, cpu: a.cpu + s.cpu, other: a.other + s.other,
    total: a.total + s.total, noGpu: a.noGpu && s.noGpu } : a), { gpu: 0, cpu: 0, other: 0, total: 0, noGpu: true });
}

// 「h:mm:ss」 and 「h:mm」 from seconds
export function clock(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 3600)}:${String(Math.floor((s % 3600) / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}
export function clockHM(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const m = Math.max(0, Math.round(seconds / 60));
  return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`;
}

// The film's stages as the 能耗 block lists them (PW04: GPU stages own the machine; the others
// count the machine's estimate while they run).
export const ENERGY_STAGES: Array<[string, string, string]> = [
  ["restore", "修复", "LTX"],
  ["asr", "识别", "语音识别"],
  ["translate", "翻译", "字幕翻译"],
  ["return", "回传", "成品回传"],
  ["scrape", "刮削", "Miko 整理"],
];

export const ACTIVITY_LABEL: Record<PowerActivity, [string, string]> = {
  restore: ["Jasna 修复", "LTX 编码 / 去噪 · GPU 独占整机"],
  asr: ["语音识别", "Whisper · GPU 独占整机"],
  translate: ["翻译", "字幕翻译"],
  idle: ["空闲", "在线、无任务"],
  other: ["其他", "主控编排、回传与整理"],
};
export const ACTIVITY_ORDER: PowerActivity[] = ["restore", "asr", "translate", "idle", "other"];
export const activityNow = (a: string | null | undefined) =>
  a && a in ACTIVITY_LABEL ? ACTIVITY_LABEL[a as PowerActivity][0] : null;

// Series colours: the preview's validated categorical order (dark surface, adjacent CVD ΔE ≥ 8.3).
export const MACHINE_COLORS = ["#3987e5", "#d95926", "#9085e9", "#c98500", "#d55181", "#2aa5b8"];
export const machineColor = (i: number) => MACHINE_COLORS[i % MACHINE_COLORS.length];

export const TIER_SHORT_ALL: Record<LtxTier | "none", string> = { standard: "标准", good: "良好", high: "高", none: "无需修复" };

// PW13 for a film already in the queue: the rate of its machine × resolution × tier times its length.
export function predictFromRates(
  rates: EnergyRate[] | undefined, machine: string | null | undefined, resolution: Resolution | null | undefined,
  tier: LtxTier | "none", durationMs: number | null | undefined,
): number | null {
  if (!rates || !resolution || !durationMs) return null;
  const rate = rates.find((r) => r.resolution === resolution && r.tier === tier && (!machine || r.machine === machine));
  return rate?.wh_per_hour == null ? null : (rate.wh_per_hour * durationMs) / 3_600_000;
}
export const predictionOf = (p: PlanPrediction | null | undefined, tier: LtxTier | "none") => (p ? p[tier] : null);

// The share of 修复 in a film's energy, in whole percent.
export function restoreShare(e: FilmEnergy | null | undefined): number | null {
  const restore = e?.stages.restore?.wh;
  return e && restore != null && e.total_wh > 0 ? Math.round((restore / e.total_wh) * 100) : null;
}

// One point of a stacked series: null is a gap (never drawn as zero power).
export function seriesOf(h: PowerHistory | null | undefined): Array<number | null> {
  return h ? h.avg_w : [];
}
