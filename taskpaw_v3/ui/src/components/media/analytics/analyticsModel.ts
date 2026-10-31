// The 数据分析 model (EPIC 2026-10-31 §8.2): a pure, typed port of the approved preview's
// rangeDef / compute / bucketize / stats / slices (design-system/taskpaw-v3/preview/media-analytics-v1.html).
// The backend sends per-film facts and the browser aggregates (D2); buckets are the controller's
// local time (§7.1). No React here.
import type { AnalyticsFailure, AnalyticsFilm, AnalyticsMachine, AnalyticsSpan, WorkflowAnalytics } from "../../../api";
import { DAY, fHM, fMD, fMDHM, fWk, fYM, HOUR, pad } from "./analyticsFormat";

export type RangeKey = "24h" | "7d" | "30d" | "90d" | "all";
export type ResFilter = "all" | "4K" | "1080p";
export type BucketKind = "h" | "d" | "w" | "m";
export const ALL_MACHINES = "all";

export const RANGES: ReadonlyArray<readonly [RangeKey, string]> = [
  ["24h", "24 小时"], ["7d", "7 天"], ["30d", "30 天"], ["90d", "90 天"], ["all", "全部"],
];
export const RESOLUTIONS: ReadonlyArray<readonly [ResFilter, string]> = [["all", "全部"], ["4K", "4K"], ["1080p", "1080P"]];
const RANGE_NAME: Record<RangeKey, string> = { "24h": "过去 24 小时", "7d": "过去 7 天", "30d": "过去 30 天", "90d": "过去 90 天", all: "全部记录" };
const UNIT: Record<BucketKind, string> = { h: "每小时", d: "每天", w: "每周", m: "每月" };

export const sum = (a: number[]) => a.reduce((x, y) => x + y, 0);
export const avg = (a: number[]) => (a.length ? sum(a) / a.length : null);
export const amin = (a: number[]) => (a.length ? Math.min(...a) : null);
export const amax = (a: number[]) => (a.length ? Math.max(...a) : null);
export function quantile(a: number[], q: number): number | null {
  if (!a.length) return null;
  const s = [...a].sort((x, y) => x - y), i = (s.length - 1) * q, lo = Math.floor(i), hi = Math.ceil(i);
  return s[lo] + (s[hi] - s[lo]) * (i - lo);
}
export interface Stats { n: number; min: number; p25: number; p50: number; p75: number; max: number; avg: number }
export function stats(vals: number[]): Stats | null {
  if (!vals.length) return null;
  return {
    n: vals.length, min: amin(vals)!, p25: quantile(vals, 0.25)!, p50: quantile(vals, 0.5)!,
    p75: quantile(vals, 0.75)!, max: amax(vals)!, avg: avg(vals)!,
  };
}
const nums = (a: Array<number | null | undefined>) => a.filter((v): v is number => v != null && Number.isFinite(v));

// ---- time ranges and buckets (local time) ----
const localDay = (t: number) => { const d = new Date(t); return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime(); };
const addDays = (t: number, n: number) => { const d = new Date(t); d.setDate(d.getDate() + n); return d.getTime(); };
const floorHour = (t: number) => { const d = new Date(t); d.setMinutes(0, 0, 0); return d.getTime(); };

export interface RangeDef {
  key: RangeKey;
  kind: BucketKind;
  edges: number[]; // n + 1 bucket edges, epoch ms, local calendar units
  n: number;
  start: number;
  end: number; // exclusive; the bucket holding `now` is the last one
  len: number;
  // D14 / C3 AC11: max(range start, coverage.archives); for 全部 exactly coverage.archives
  effectiveStart: number;
  prevStart: number;
  prevEnd: number;
  hasPrev: boolean;
  name: string;
  unit: string;
}

/** The buckets of a range, as the preview's `rangeDef`; `archives` = coverage.archives (ms) or null. */
export function rangeDef(key: RangeKey, now: number, archives: number | null): RangeDef {
  const edges: number[] = [];
  let kind: BucketKind;
  const today = localDay(now);
  if (key === "24h") {
    kind = "h";
    const end = floorHour(now) + HOUR;
    for (let i = 24; i >= 0; i--) edges.push(end - i * HOUR);
  } else if (key === "7d" || key === "30d") {
    kind = "d";
    const days = key === "7d" ? 7 : 30;
    for (let i = 1 - days; i <= 1; i++) edges.push(addDays(today, i));
  } else if (key === "90d") {
    kind = "w";
    const end = addDays(today, 1);
    for (let i = 13; i >= 0; i--) edges.push(addDays(end, -7 * i));
  } else {
    // 全部: from the first archive; monthly once the span exceeds 120 days, weekly over 45
    const cover = archives ?? today;
    const span = now - cover;
    if (span > 120 * DAY) {
      kind = "m";
      const d0 = new Date(cover);
      const y = d0.getFullYear();
      let m = d0.getMonth();
      edges.push(new Date(y, m, 1).getTime());
      while (edges[edges.length - 1] <= now) { m += 1; edges.push(new Date(y, m, 1).getTime()); }
    } else {
      kind = span > 45 * DAY ? "w" : "d";
      const step = kind === "w" ? 7 : 1;
      for (let t = localDay(cover); ; t = addDays(t, step)) { edges.push(t); if (t > now) break; }
    }
  }
  const n = edges.length - 1, start = edges[0], end = edges[n], len = end - start;
  const hasPrev = key !== "all";
  const effectiveStart = key === "all" ? (archives ?? start) : Math.max(start, archives ?? start);
  return { key, kind, edges, n, start, end, len, effectiveStart, prevStart: start - len, prevEnd: start, hasPrev, name: RANGE_NAME[key], unit: UNIT[kind] };
}

/** The bucket holding `t`, or -1 outside the range. */
export function bucketIndex(R: RangeDef, t: number): number {
  if (t < R.start || t >= R.end) return -1;
  let lo = 0, hi = R.n - 1;
  while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (R.edges[mid] <= t) lo = mid; else hi = mid - 1; }
  return lo;
}
/** The current-period bucket aligned with a previous-period time (one range length later). */
export const prevBucketIndex = (R: RangeDef, t: number) => (R.hasPrev ? bucketIndex(R, t + R.len) : -1);

export function bucketShort(R: RangeDef, i: number): string {
  const s = R.edges[i];
  return R.kind === "h" ? fHM(s) : R.kind === "m" ? `${pad(new Date(s).getMonth() + 1)}月` : fMD(s);
}
export function bucketTitle(R: RangeDef, i: number, now: number): string {
  const s = R.edges[i], e = R.edges[i + 1], open = e > now;
  if (R.kind === "h") return `${fMD(s)} ${fHM(s)}–${fHM(e)}`;
  if (R.kind === "d") return `${fMD(s)} ${fWk(s)}${open ? `（截至 ${fHM(now)}）` : ""}`;
  if (R.kind === "m") return `${fYM(s)}${open ? "（截至今天）" : ""}`;
  return `${fMD(s)} – ${fMD(addDays(e, -1))}${open ? "（截至今天）" : ""}`;
}

/** The query's `since`: one range length before the range (对比上一周期 needs no refetch); none for 全部. */
export const querySince = (R: RangeDef) => (R.hasPrev ? new Date(R.prevStart).toISOString() : undefined);

// ---- filters and the one aggregate every section reads ----
export interface Filters { machine: string; res: ResFilter; compare: boolean }

export interface Analysis {
  R: RangeDef;
  now: number;
  cover: number | null; // coverage.archives
  controller: AnalyticsMachine | null;
  ctrl: boolean; // the 本机 filter
  cur: AnalyticsFilm[];
  prev: AnalyticsFilm[] | null; // null when the previous period is not covered
  prevOk: boolean;
  prevPartial: boolean; // the previous period starts before coverage.archives but overlaps it
  failures: AnalyticsFailure[];
  failuresPrev: AnalyticsFailure[] | null;
  visStart: number;
  visEnd: number;
  slices: Array<[number, number]>; // 12 sparkline slices
  uncov: number; // fraction of the range before coverage.archives (0 for 全部, AC11)
  coverDays: number;
  fleet: AnalyticsMachine[]; // GPU machines in scope of the machine filter
}

const parsed = new WeakMap<object, number>();
const timeOf = (row: object, iso: string) => {
  let t = parsed.get(row);
  if (t === undefined) { t = Date.parse(iso); parsed.set(row, t); }
  return t;
};
export const filmTime = (f: AnalyticsFilm) => timeOf(f, f.archived_at);
export const failureTime = (f: AnalyticsFailure) => timeOf(f, f.at);
const within = (t: number, s: number, e: number) => t >= s && t < e;

export const isFail = (f: AnalyticsFailure) => f.severity === "fail";
export const failureRate = (fails: number, archived: number) => (fails + archived ? (fails / (fails + archived)) * 100 : null);

export function controllerOf(data: WorkflowAnalytics): AnalyticsMachine | null {
  return data.machines.find((m) => m.kind === "controller") ?? null;
}

export function compute(data: WorkflowAnalytics, R: RangeDef, filters: Filters, now: number): Analysis {
  const cover = data.coverage.archives ? Date.parse(data.coverage.archives) : null;
  const controller = controllerOf(data);
  const ctrl = controller !== null && filters.machine === controller.id;
  const all = filters.machine === ALL_MACHINES;
  const resOk = (r: AnalyticsFilm["res"] | undefined) => filters.res === "all" || r === filters.res;
  // every film passes through 本机's post-processing, so the 本机 filter keeps all of them (preview)
  const pool = data.films.filter((f) => resOk(f.res) && (all || ctrl || f.worker === filters.machine)
    && (cover === null || filmTime(f) >= cover));
  const cur = pool.filter((f) => within(filmTime(f), R.start, R.end));
  const prevOk = R.hasPrev && cover !== null && R.prevStart >= cover;
  const prevPartial = R.hasPrev && cover !== null && !prevOk && R.prevEnd > cover;
  const prev = prevOk ? pool.filter((f) => within(filmTime(f), R.prevStart, R.prevEnd)) : null;
  // failures carry no resolution: take it from an archived film with the same code, keep unknown ones
  const resByCode = new Map(data.films.map((f) => [f.code, f.res] as const));
  const failPool = data.failures.filter((f) => (all || f.machine === filters.machine)
    && (!resByCode.has(f.code) || resOk(resByCode.get(f.code))) && (cover === null || failureTime(f) >= cover));
  const failures = failPool.filter((f) => within(failureTime(f), R.start, R.end));
  const failuresPrev = prevOk ? failPool.filter((f) => within(failureTime(f), R.prevStart, R.prevEnd)) : null;
  const visStart = Math.max(R.start, cover ?? R.start), visEnd = Math.min(R.end, now);
  const sw = (visEnd - R.start) / 12;
  const slices = Array.from({ length: 12 }, (_, i) => [R.start + i * sw, R.start + (i + 1) * sw] as [number, number]);
  const uncov = R.key !== "all" && cover !== null && R.start < cover ? Math.min(1, (cover - R.start) / R.len) : 0;
  const gpus = data.machines.filter((m) => m.kind === "gpu");
  const fleet = ctrl ? [] : all ? gpus.filter((m) => !m.retired) : gpus.filter((m) => m.id === filters.machine);
  return {
    R, now, cover, controller, ctrl, cur, prev, prevOk, prevPartial, failures, failuresPrev, visStart, visEnd, slices, uncov,
    coverDays: cover === null ? 0 : Math.ceil((now - cover) / DAY), fleet,
  };
}

/** Counts (or `val` sums) per bucket; `prev` aligns previous-period films onto the current buckets. */
export function bucketize(A: Analysis, films: AnalyticsFilm[], val?: (f: AnalyticsFilm) => number, prev = false): number[] {
  const out = new Array<number>(A.R.n).fill(0);
  for (const f of films) {
    const i = prev ? prevBucketIndex(A.R, filmTime(f)) : bucketIndex(A.R, filmTime(f));
    if (i >= 0) out[i] += val ? val(f) : 1;
  }
  return out;
}
/** One value per sparkline slice; null for a slice that ends before the first archive. */
export function sliceValues(A: Analysis, fn: (s: number, e: number) => number | null): Array<number | null> {
  return A.slices.map(([s, e]) => (A.cover === null || e <= A.cover ? null : fn(s, e)));
}
export function sliceLabels(A: Analysis): string[] {
  return A.slices.map(([s, e]) => (A.R.kind === "h" ? `${fMDHM(s)}–${fHM(e)}` : `${fMD(s)} – ${fMD(e)}`));
}
export const filmsIn = (films: AnalyticsFilm[], s: number, e: number) => films.filter((f) => within(filmTime(f), s, e));

// ---- per-film figures ----
export const hours = (s: number | null) => (s == null ? null : s / 3600);
export const restoreKwh = (f: AnalyticsFilm) => sum(f.restore_wh.map((e) => e[1])) / 1000;
/** A film's estimated energy: 本机 filter = 本机's post-processing only, else restore + post-processing. */
export const filmKwh = (f: AnalyticsFilm, ctrl: boolean) => (ctrl ? 0 : restoreKwh(f)) + (f.controller_wh ?? 0) / 1000;
/** Whether a film carries any energy evidence (under the 本机 filter: 本机's post-processing reading). */
export const hasEnergyReading = (f: AnalyticsFilm, ctrl: boolean) => f.controller_wh != null || (!ctrl && f.restore_wh.length > 0);
/** Estimated energy of the films with a reading; films without one count as missing, never as 0 kWh.
 *  `kwh` is null when no film has a reading. The KPI and C5's 用电 panel share this rule. */
export function energyReading(films: AnalyticsFilm[], ctrl: boolean): { kwh: number | null; measured: number; missing: number } {
  const read = films.filter((f) => hasEnergyReading(f, ctrl));
  return { kwh: read.length ? sum(read.map((f) => filmKwh(f, ctrl))) : null, measured: read.length, missing: films.length - read.length };
}

/** D14: a machine is 「新加入」 iff it first booted after the first 10 % of the effective range. */
export function isNewMachine(m: AnalyticsMachine, R: RangeDef, now: number): boolean {
  if (!m.first_seen) return false;
  const eff = R.effectiveStart;
  return Date.parse(m.first_seen) > eff + 0.1 * (now - eff);
}

/** Like presentAt, but null when no span of `ids` covers `t` (a recording gap, before the first span,
 *  after the last recorded tick): unrecorded time is 无记录, never 0 online. */
export function recordedPresence(spans: AnalyticsSpan[], ids: Set<string>, t: number): number | null {
  const covered = spans.some((s) => {
    if (!ids.has(s.machine)) return false;
    const a = Date.parse(s.start), b = Date.parse(s.end);
    return t >= a && (t < b || (s.open && t <= b));
  });
  return covered ? presentAt(spans, ids, t) : null;
}

/** How many of `ids` a device span shows online or 停领 at `t` (unrecorded time counts as neither). */
export function presentAt(spans: AnalyticsSpan[], ids: Set<string>, t: number): number {
  const seen = new Set<string>();
  for (const s of spans) {
    if (!ids.has(s.machine) || s.state === "offline") continue;
    const a = Date.parse(s.start), b = Date.parse(s.end);
    if (t >= a && (t < b || (s.open && t <= b))) seen.add(s.machine);
  }
  return seen.size;
}

// ---- KPI figures (§8.4 item 1) ----
export interface KpiFigures {
  films: number; films4k: number; films1080: number;
  prevFilms: number | null;
  lead: number[]; prevLeadAvg: number | null; // hours
  restore: number[]; restore4k: number[]; restore1080: number[]; prevRestoreAvg: number | null;
  ctrlRestored: boolean; // 本机 restored a film in range (D11)
  // kWh, estimated, over films with a reading only (energyReading); prevEnergy is null unless both
  // periods are fully measured, so a missing reading never reads as a reduction
  energy: number; prevEnergy: number | null; energyPerFilm: number | null;
  energyMeasured: number; energyMissing: number;
  fails: number; warns: number; failRate: number | null; prevFailRate: number | null;
}
/** Restore hours of `films`; under the 本机 filter only the films 本机 itself restored (D11). */
export function restoreHours(A: Analysis, films: AnalyticsFilm[]): number[] {
  const restored = A.ctrl ? films.filter((f) => f.worker === A.controller?.id) : films;
  return nums(restored.map((f) => hours(f.restore_s)));
}
/** 全程 (入库 → 归档) hours of `films`. */
export const leadHours = (films: AnalyticsFilm[]) => nums(films.map((f) => hours(f.lead_s)));

export function kpiFigures(A: Analysis): KpiFigures {
  const { cur, prev, ctrl } = A;
  const restoreH = (fs: AnalyticsFilm[]) => restoreHours(A, fs);
  const leadH = leadHours;
  const en = energyReading(cur, ctrl);
  const enPrev = prev ? energyReading(prev, ctrl) : null;
  const comparable = en.missing === 0 && en.kwh !== null && enPrev !== null && enPrev.missing === 0 && enPrev.kwh !== null;
  const fails = A.failures.filter(isFail).length;
  const prevFails = A.failuresPrev ? A.failuresPrev.filter(isFail).length : null;
  return {
    films: cur.length,
    films4k: cur.filter((f) => f.res === "4K").length,
    films1080: cur.filter((f) => f.res === "1080p").length,
    prevFilms: prev ? prev.length : null,
    lead: leadH(cur), prevLeadAvg: prev ? avg(leadH(prev)) : null,
    restore: restoreH(cur),
    restore4k: restoreH(cur.filter((f) => f.res === "4K")),
    restore1080: restoreH(cur.filter((f) => f.res === "1080p")),
    prevRestoreAvg: prev ? avg(restoreH(prev)) : null,
    ctrlRestored: A.controller !== null && cur.some((f) => f.worker === A.controller!.id),
    energy: en.kwh ?? 0, prevEnergy: comparable ? enPrev.kwh : null,
    energyPerFilm: en.kwh !== null ? en.kwh / en.measured : null,
    energyMeasured: en.measured, energyMissing: en.missing,
    fails, warns: A.failures.length - fails,
    failRate: failureRate(fails, cur.length),
    prevFailRate: prev && prevFails !== null ? failureRate(prevFails, prev.length) : null,
  };
}

// ---- delta vs the previous period (preview `dlt`) ----
export interface Delta { tone: "good" | "bad" | "flat"; dir: "up" | "down" | "flat"; text: string; vs: boolean }
export function delta(A: Analysis, cur: number | null, prev: number | null, good: "up" | "down" | "none", pp = false): Delta {
  const flat = (text: string): Delta => ({ tone: "flat", dir: "flat", text, vs: false });
  if (!A.R.hasPrev) return flat("全部时段没有上一周期");
  if (!A.prevOk) return flat(A.prevPartial ? "上一周期记录不全" : "上一周期没有记录");
  if (cur == null || prev == null || (prev === 0 && !pp)) return flat("上一周期无可比数据");
  const d = pp ? cur - prev : ((cur - prev) / prev) * 100;
  if (pp ? Math.abs(d) < 0.05 : Math.abs(d) < 1) return flat("与上一周期持平");
  const text = `${d > 0 ? "+" : "−"}${pp ? `${Math.abs(d).toFixed(1)} 个百分点` : `${Math.abs(Math.round(d))}%`}`;
  const dir = d > 0 ? "up" : "down";
  return { tone: good === "none" ? "flat" : dir === good ? "good" : "bad", dir, text, vs: true };
}

// ---- the machine select (§8.3) ----
export interface MachineOption { id: string; label: string }
export interface MachineGroup { label: string; options: MachineOption[] }
const UNPROBED = "未识别型号";
/** GPU machines grouped by model, plus retired ones with data in range 「（已退休）」. */
export function machineGroups(data: WorkflowAnalytics, R: RangeDef, now: number): MachineGroup[] {
  const active = new Set<string>();
  for (const f of data.films) if (f.worker && within(filmTime(f), R.start, R.end)) active.add(f.worker);
  for (const f of data.failures) if (within(failureTime(f), R.start, R.end)) active.add(f.machine);
  const byModel = new Map<string, AnalyticsMachine[]>();
  for (const m of data.machines) {
    if (m.kind !== "gpu" || (m.retired && !active.has(m.id))) continue;
    const key = m.model ?? UNPROBED;
    byModel.set(key, [...(byModel.get(key) ?? []), m]);
  }
  const order = ([a]: [string, unknown], [b]: [string, unknown]) => Number(a === UNPROBED) - Number(b === UNPROBED) || a.localeCompare(b);
  return [...byModel.entries()].sort(order).map(([model, ms]) => ({
    label: `${model} × ${ms.length}`,
    options: [...ms].sort((a, b) => a.name.localeCompare(b.name)).map((m) => ({
      id: m.id, label: `${m.name}${m.retired ? "（已退休）" : isNewMachine(m, R, now) ? "（新加入）" : ""}`,
    })),
  }));
}
export const controllerLabel = (m: AnalyticsMachine) => (m.name === "本机" ? "本机" : `本机（${m.name}）`);
export function machineName(data: WorkflowAnalytics, id: string): string {
  if (id === ALL_MACHINES) return "全部 GPU 机器";
  const m = data.machines.find((x) => x.id === id);
  return !m ? id : m.kind === "controller" ? "本机" : m.name;
}
