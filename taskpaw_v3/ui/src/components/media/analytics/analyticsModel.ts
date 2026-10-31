// The 数据分析 model (EPIC 2026-10-31 §8.2): a pure, typed port of the approved preview's
// rangeDef / compute / bucketize / stats / slices (design-system/taskpaw-v3/preview/media-analytics-v1.html).
// The backend sends per-film facts and the browser aggregates (D2); buckets are the controller's
// local time (§7.1). No React here.
import type {
  AnalyticsFailure, AnalyticsFailureType, AnalyticsFilm, AnalyticsInbox, AnalyticsMachine, AnalyticsSpan, AnalyticsStateReason, WorkflowAnalytics,
} from "../../../api";
import { DAY, fHM, fMD, fMDHM, fSpan, fWk, fYM, HOUR, pad } from "./analyticsFormat";

export type RangeKey = "24h" | "7d" | "30d" | "90d" | "all";
export type ResFilter = "all" | "4K" | "1080p";
export type BucketKind = "h" | "d" | "w" | "m";
export const ALL_MACHINES = "all";

export const RANGES: ReadonlyArray<readonly [RangeKey, string]> = [
  ["24h", "24 小时"], ["7d", "7 天"], ["30d", "30 天"], ["90d", "90 天"], ["all", "全部"],
];
export const RESOLUTIONS: ReadonlyArray<readonly [ResFilter, string]> = [["all", "全部"], ["4K", "4K"], ["1080p", "1080P"]];
// Resolution series of every per-resolution chart: 4K = --c1 blue, 1080P = --c2 orange (OD7); films whose
// input probes gave no resolution (§7 `res: null`) only show when there are some.
export interface ResSeries { key: string; label: string; color: string; res: AnalyticsFilm["res"] }
const RES_SERIES: ResSeries[] = [
  { key: "4K", label: "4K", color: "var(--c1)", res: "4K" },
  { key: "1080p", label: "1080P", color: "var(--c2)", res: "1080p" },
  { key: "none", label: "未知分辨率", color: "var(--c-queue)", res: null },
];
export const shownResSeries = (res: ResFilter, films: AnalyticsFilm[]) =>
  RES_SERIES.filter((s) => (res === "all" ? s.res !== null || films.some((f) => f.res === null) : s.res === res));
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
  machine: string; // the machine filter: ALL_MACHINES, a GPU worker id or the controller id
  cur: AnalyticsFilm[];
  rangeFilms: AnalyticsFilm[]; // films in range of the resolution filter on every machine (energy attribution)
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
  const rangeFilms = all || ctrl ? cur : data.films.filter((f) => resOk(f.res) && within(filmTime(f), R.start, R.end)
    && (cover === null || filmTime(f) >= cover));
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
    R, now, cover, controller, ctrl, machine: filters.machine, cur, rangeFilms, prev, prevOk, prevPartial, failures, failuresPrev,
    visStart, visEnd, slices, uncov, coverDays: cover === null ? 0 : Math.ceil((now - cover) / DAY), fleet,
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
 *  `partial` = measured films with `energy_partial` (§7.1: some attempt lacked energy): their readings are
 *  summed but the total is incomplete. `kwh` is null when no film has a reading. The KPI and C5's 用电
 *  panel share this rule. */
export interface EnergyReading { kwh: number | null; measured: number; missing: number; partial: number }
export function energyReading(films: AnalyticsFilm[], ctrl: boolean): EnergyReading {
  const read = films.filter((f) => hasEnergyReading(f, ctrl));
  return {
    kwh: read.length ? sum(read.map((f) => filmKwh(f, ctrl))) : null,
    measured: read.length, missing: films.length - read.length, partial: read.filter((f) => f.energy_partial).length,
  };
}
/** Every film of the period has a full reading: only then may two periods be compared. */
export const energyComplete = (r: EnergyReading | null) => r !== null && r.kwh !== null && r.missing === 0 && r.partial === 0;

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
  // periods are fully measured (no missing, no partial film), so a gap never reads as a reduction
  energy: number; prevEnergy: number | null; energyPerFilm: number | null;
  energyMeasured: number; energyMissing: number; energyPartial: number;
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
  const comparable = energyComplete(en) && energyComplete(enPrev);
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
    energy: en.kwh ?? 0, prevEnergy: comparable ? enPrev!.kwh : null,
    energyPerFilm: en.kwh !== null ? en.kwh / en.measured : null,
    energyMeasured: en.measured, energyMissing: en.missing, energyPartial: en.partial,
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
  for (const f of data.films) {
    if (!within(filmTime(f), R.start, R.end)) continue;
    if (f.worker) active.add(f.worker);
    for (const [w] of f.restore_wh) active.add(w); // a failed attempt's energy is data in range too
  }
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

// ---- 各 GPU 机器 (§8.4 item 3; C4 AC1, AC2) ----
/** What a machine's chip says: 已退休, else the live state, with 新加入 (D14) in place of 在线 / 确认中 / 未启用. */
export type MachineStateKey = "online" | "paused" | "new" | "unknown" | "off" | "offline" | "retired";
export const MACHINE_STATE_TEXT: Record<MachineStateKey, string> = {
  online: "在线", paused: "停领", offline: "离线", new: "新加入", retired: "已退休", unknown: "确认中", off: "未启用",
};
/** What a 停领 is: the reason codes of §7 `state_reason` (§6.2). */
export const PAUSE_REASON_TEXT: Record<AnalyticsMachine["state_reason"], string> = {
  "": "停领", admission: "主控停领", worker: "工作机暂停领取", global: "主控暂停新领取", version: "版本不兼容",
};
export function machineStateKey(m: AnalyticsMachine, isNew: boolean): MachineStateKey {
  if (m.retired) return "retired";
  if (m.state_now === "offline" || m.state_now === "paused") return m.state_now;
  return isNew ? "new" : m.state_now;
}

export interface MachineRow {
  m: AnalyticsMachine;
  isNew: boolean;
  state: MachineStateKey;
  films: AnalyticsFilm[]; // films this machine restored, archived in range
  n4: number; n1: number; nNone: number; total: number;
  a4: number | null; a1: number | null; min: number | null; max: number | null; // restore hours
  power: number | null; // W: Σwh × 3600 ÷ Σseconds over its restore_wh entries (§7.1)
  kwh: number | null; // its restore_wh entries, failed attempts on any film included; null without any reading
  kwhPartial: number; // films it restored whose restore reading is missing or energy_partial (the sum is incomplete)
  fails: number; // severity fail only
  eff: number | null; // restore hours per hour of film
  days: number; // online (+ 停领) days when device history covers the range, else calendar days present
}

/** Device history covers the range: recorded and available from before the range's effective start. */
export function devicesCoverRange(data: WorkflowAnalytics, A: Analysis): boolean {
  return data.devices.available && data.coverage.devices !== null && Date.parse(data.coverage.devices) <= A.visStart;
}

/** A machine's days in range for 各机型实测产能: online + 停领 span time when `covered`, else time since it joined. */
export function machineDays(data: WorkflowAnalytics, A: Analysis, m: AnalyticsMachine, covered: boolean): number {
  const s = A.visStart, e = A.visEnd;
  if (!covered) return Math.max(0, e - Math.max(s, m.first_seen ? Date.parse(m.first_seen) : s)) / DAY;
  let ms = 0;
  for (const sp of data.devices.spans) {
    if (sp.machine !== m.id || sp.state === "offline") continue;
    // an open span ends at its last recorded tick: time after it is unrecorded, never online (§6.2)
    const a = Math.max(s, Date.parse(sp.start)), b = Math.min(e, Date.parse(sp.end));
    if (b > a) ms += b - a;
  }
  return ms / DAY;
}

/**
 * The machine table's rows: the GPU fleet in scope, retired GPU machines with data in range, and 本机 only when
 * it restored a film in range (D11). Under the 本机 filter that is 本机 alone, or nothing.
 */
export function machineRows(data: WorkflowAnalytics, A: Analysis): MachineRow[] {
  const ctrlId = A.controller?.id;
  const ctrlRestored = ctrlId !== undefined && A.cur.some((f) => f.worker === ctrlId);
  let ms: AnalyticsMachine[];
  if (A.ctrl) ms = ctrlRestored ? [A.controller!] : [];
  else if (A.machine !== ALL_MACHINES) ms = A.fleet;
  else {
    // retired machines exactly as the machine select lists them (data in range), so a row click always filters
    const listed = new Set(machineGroups(data, A.R, A.now).flatMap((g) => g.options.map((o) => o.id)));
    const retired = data.machines.filter((m) => m.kind === "gpu" && m.retired && listed.has(m.id));
    ms = [...A.fleet, ...retired, ...(ctrlRestored ? [A.controller!] : [])];
  }
  const covered = devicesCoverRange(data, A);
  const restoreH = (fs: AnalyticsFilm[]) => nums(fs.map((f) => hours(f.restore_s)));
  return ms.map((m) => {
    const films = A.cur.filter((f) => f.worker === m.id);
    const all = restoreH(films);
    let wh = 0, secs = 0;
    const fed = new Set<AnalyticsFilm>(); // films it put restore energy into, failed attempts included
    for (const f of A.rangeFilms) for (const [w, e, s] of f.restore_wh) if (w === m.id) { wh += e; secs += s; fed.add(f); }
    const timed = films.filter((f) => f.restore_s != null && f.duration_s);
    const filmH = sum(timed.map((f) => f.duration_s! / 3600));
    const isNew = isNewMachine(m, A.R, A.now);
    return {
      m, isNew, state: machineStateKey(m, isNew), films,
      n4: films.filter((f) => f.res === "4K").length, n1: films.filter((f) => f.res === "1080p").length,
      nNone: films.filter((f) => f.res === null).length, total: films.length,
      a4: avg(restoreH(films.filter((f) => f.res === "4K"))), a1: avg(restoreH(films.filter((f) => f.res === "1080p"))),
      min: amin(all), max: amax(all),
      // restore_wh holds only attempts with an energy reading, so both sums stay honest (never 0 for "no reading")
      power: secs > 0 ? (wh * 3600) / secs : null, kwh: fed.size ? wh / 1000 : null,
      // incomplete: a film it restored without its reading, or any film it fed that is energy_partial (C3's rule)
      kwhPartial: new Set([...films, ...fed].filter((f) => f.energy_partial || !fed.has(f))).size,
      fails: A.failures.filter((f) => f.machine === m.id && isFail(f)).length,
      eff: filmH > 0 ? sum(timed.map((f) => f.restore_s! / 3600)) / filmH : null,
      days: machineDays(data, A, m, covered),
    };
  });
}

export interface ModelGroup { model: string; rows: MachineRow[] }
/** Rows grouped by GPU model (the latest probe, D11); unprobed machines, then 本机's group, last. */
export function modelGroups(rows: MachineRow[]): ModelGroup[] {
  const by = new Map<string, MachineRow[]>();
  for (const r of rows) { const k = r.m.model ?? UNPROBED; by.set(k, [...(by.get(k) ?? []), r]); }
  const rank = ([k, rs]: [string, MachineRow[]]) => (rs.some((r) => r.m.kind === "controller") ? 2 : k === UNPROBED ? 1 : 0);
  return [...by.entries()].sort((a, b) => rank(a) - rank(b) || a[0].localeCompare(b[0])).map(([model, rs]) => ({ model, rows: rs }));
}
/** The same-model average films per machine; 新加入 machines (D14) are left out; null below two machines. */
export function modelAverage(g: ModelGroup): number | null {
  const w = g.rows.filter((r) => !r.isNew);
  return w.length > 1 ? avg(w.map((r) => r.total)) : null;
}

export type MachineSortKey = "name" | "st" | "total" | "a4" | "a1" | "min" | "power" | "kwh" | "fails" | "eff";
export interface MachineSort { key: MachineSortKey | "model"; dir: 1 | -1 }
const STATE_ORDER: Record<MachineStateKey, number> = { online: 0, paused: 1, new: 2, unknown: 3, off: 4, offline: 5, retired: 6 };
/** Preview `sortRows`: "model" (the default) and "name" sort by name; empty values sort last either way. */
export function sortMachineRows(rows: MachineRow[], sort: MachineSort): MachineRow[] {
  const val = (r: MachineRow): string | number | null =>
    sort.key === "model" || sort.key === "name" ? r.m.name : sort.key === "st" ? STATE_ORDER[r.state] : r[sort.key];
  return [...rows].sort((a, b) => {
    const x = val(a), y = val(b);
    if (x == null || y == null) return x == null && y == null ? 0 : x == null ? 1 : -1;
    const c = typeof x === "string" ? x.localeCompare(y as string) : x - (y as number);
    return c * sort.dir || a.m.name.localeCompare(b.m.name);
  });
}

export interface ModelThroughput {
  model: string; count: number; newCount: number; days: number;
  p4: number | null; p1: number | null; pt: number | null; // films per machine per (online) day
  a4: number | null; a1: number | null; // restore hours
}
/** 各机型实测产能: every machine of a model, 新加入 included (D14), normalised by its days in range. */
export function modelThroughput(groups: ModelGroup[]): ModelThroughput[] {
  return groups.map((g) => {
    const days = sum(g.rows.map((r) => r.days));
    const per = (n: number) => (days > 0 ? n / days : null);
    const films = g.rows.flatMap((r) => r.films);
    const h = (res: AnalyticsFilm["res"]) => avg(nums(films.filter((f) => f.res === res).map((f) => hours(f.restore_s))));
    return {
      model: g.model, count: g.rows.length, newCount: g.rows.filter((r) => r.isNew).length, days,
      p4: per(sum(g.rows.map((r) => r.n4))), p1: per(sum(g.rows.map((r) => r.n1))), pt: per(sum(g.rows.map((r) => r.total))),
      a4: h("4K"), a1: h("1080p"),
    };
  });
}

// ---- 本机（主控）处理 (§8.4 item 4; C4 AC3) ----
export interface StageFigure { n: number; avg: number | null; max: number | null } // minutes
export interface ControllerFigures {
  asr: StageFigure; translate: StageFigure; scrape: StageFigure; archive: StageFigure;
  translated: number;
  cues: number | null; // line total of translated films; null before any line count was recorded
  cuesSince: number | null; // coverage.cues when it falls inside the range (「自 MM-DD 起统计行数」)
  reusedZh: number; noSpeech: number; // films with ASR decision reused_zh / no_speech > 0 (§7.1)
  targets: { performer: number; category: number; new_actor: number; manual: number; second: number };
}
const minutesOf = (vals: Array<number | null>) => nums(vals.map((s) => (s == null ? null : s / 60)));
const stage = (mins: number[], n = mins.length): StageFigure => ({ n, avg: avg(mins), max: amax(mins) });

export function controllerFigures(data: WorkflowAnalytics, A: Analysis): ControllerFigures {
  const c = A.cur, tr = c.filter((f) => f.translated === true); // null = unknown, never 无需翻译 (§7.1)
  const cuesCover = data.coverage.cues ? Date.parse(data.coverage.cues) : null;
  const count = (k: AnalyticsFilm["target"]) => c.filter((f) => f.target === k).length;
  return {
    asr: stage(minutesOf(c.map((f) => f.asr_s))),
    translate: stage(minutesOf(tr.map((f) => f.translate_s)), tr.length),
    scrape: stage(minutesOf(c.map((f) => f.scrape_s))),
    archive: stage(minutesOf(c.map((f) => f.archive_s)), c.length), // every film here was archived
    translated: tr.length,
    cues: cuesCover === null ? null : sum(tr.map((f) => f.cues ?? 0)),
    cuesSince: cuesCover !== null && cuesCover > A.visStart ? cuesCover : null,
    reusedZh: c.filter((f) => (f.subtitles.reused_zh ?? 0) > 0).length,
    noSpeech: c.filter((f) => (f.subtitles.no_speech ?? 0) > 0).length,
    targets: {
      performer: count("performer"), category: count("category"), new_actor: count("new_actor"), manual: count("manual"),
      second: c.filter((f) => f.second).length,
    },
  };
}

export interface OutageItem { start: number; s: number; e: number; ms: number; open: boolean; paused: boolean }
export interface OutageFigures {
  items: OutageItem[]; // overlapping the range; s/e clipped to it, ms the whole outage (an open one up to now)
  totalMs: number; longestMs: number;
  since: number | null; // coverage.outages when the range starts before it (「翻译服务中断记录从 MM-DD 起」)
  uncovUntil: number | null; // the grey band runs from the range start to here; the range end without any record
}
/** 翻译服务中断 (D7): the task-log outages, covered from coverage.outages (the oldest day scanned). */
export function outageFigures(data: WorkflowAnalytics, A: Analysis): OutageFigures {
  const R = A.R, cover = data.coverage.outages ? Date.parse(data.coverage.outages) : null;
  const items: OutageItem[] = [];
  if (cover !== null) {
    for (const o of data.outages) {
      const start = Date.parse(o.start), end = o.end ? Date.parse(o.end) : A.now;
      if (end <= R.start || start >= Math.min(R.end, A.now)) continue;
      items.push({ start, s: Math.max(start, R.start), e: Math.min(end, R.end), ms: end - start, open: o.end === null, paused: o.paused });
    }
  }
  const late = cover !== null && cover > R.start;
  return {
    items, totalMs: sum(items.map((i) => i.ms)), longestMs: amax(items.map((i) => i.ms)) ?? 0,
    since: late ? cover : null, uncovUntil: cover === null ? R.end : late ? cover : null,
  };
}

// ---- 耗时分布 (§8.4 item 5; C4 AC4) ----
export interface DurationRow { key: string; label: string; st: Stats | null }
export interface ShareStage { key: "queue" | "restore" | "asr" | "translate" | "scrape" | "archive"; label: string; hours: number }
export interface DurationFigures { hours: DurationRow[]; minutes: DurationRow[]; share: ShareStage[] }
/**
 * Stage durations of the films in range. A stage a film did not have (null) is left out, never 0 (§7.1);
 * restore follows the 本机 rule of `restoreHours`. The share is each stage's average in hours.
 */
export function durationFigures(A: Analysis): DurationFigures {
  const c = A.cur, tr = c.filter((f) => f.translated === true); // null = unknown, never 无需翻译 (§7.1)
  const queue = nums(c.map((f) => hours(f.queue_s))), lead = leadHours(c);
  const r4 = restoreHours(A, c.filter((f) => f.res === "4K")), r1 = restoreHours(A, c.filter((f) => f.res === "1080p"));
  const hoursRows: DurationRow[] = [{ key: "queue", label: "排队等待", st: stats(queue) }];
  if (r4.length) hoursRows.push({ key: "restore4k", label: "修复 4K", st: stats(r4) });
  if (r1.length) hoursRows.push({ key: "restore1080", label: "修复 1080P", st: stats(r1) });
  const rNone = restoreHours(A, c.filter((f) => f.res === null)); // as 产量趋势's 未知分辨率 series
  if (rNone.length) hoursRows.push({ key: "restoreNone", label: "修复 未知分辨率", st: stats(rNone) });
  hoursRows.push({ key: "lead", label: "全程", st: stats(lead) });
  const asr = minutesOf(c.map((f) => f.asr_s)), trans = minutesOf(tr.map((f) => f.translate_s));
  const scrape = minutesOf(c.map((f) => f.scrape_s)), archive = minutesOf(c.map((f) => f.archive_s));
  const h = (mins: number[]) => (avg(mins) ?? 0) / 60;
  return {
    hours: hoursRows,
    minutes: [
      { key: "asr", label: "ASR", st: stats(asr) }, { key: "translate", label: "翻译", st: stats(trans) },
      { key: "scrape", label: "刮削", st: stats(scrape) }, { key: "archive", label: "归档", st: stats(archive) },
    ],
    share: [
      { key: "queue", label: "排队等待", hours: avg(queue) ?? 0 },
      { key: "restore", label: "修复（LTX）", hours: avg(restoreHours(A, c)) ?? 0 },
      { key: "asr", label: "ASR", hours: h(asr) }, { key: "translate", label: "翻译", hours: h(trans) },
      { key: "scrape", label: "刮削", hours: h(scrape) }, { key: "archive", label: "归档", hours: h(archive) },
    ],
  };
}

// ---- 用电 (§8.4 item 6; C5 AC1) ----
// kWh: 0 for a bucket without archives; null when it has archives but none carries a reading (unknown, not 0)
export interface EnergyBucket { kwh: number | null; films: number; missing: number; partial: number }
export interface EnergyGroup { key: string; label: string; kwh: number | null; partial: boolean }
export interface EnergyFigures {
  reading: EnergyReading; // the films in range, by C3's rule (energyReading)
  buckets: EnergyBucket[]; // kWh of the films with a reading, and how many had none / only part of one
  prev: number[] | null; // the aligned previous period, only when both periods are fully measured
  perFilm: number | null;
  most: AnalyticsFilm | null; // ranked among films with a reading (a partial sum is a lower bound, still high)
  least: AnalyticsFilm | null; // among complete readings only: a lower bound cannot claim to be the least
  top: AnalyticsFilm[]; // the 5 highest readings
  byRes: Array<{ series: ResSeries; avg: number | null; films: number; measured: number }>;
  byMachine: EnergyGroup[]; // restore energy per contributing machine (restore_wh of the films in range), then 本机's post-processing
}
export function energyFigures(data: WorkflowAnalytics, A: Analysis, res: ResFilter): EnergyFigures {
  const { cur, ctrl } = A;
  const kwhOf = (f: AnalyticsFilm) => filmKwh(f, ctrl);
  const reading = energyReading(cur, ctrl);
  const buckets = Array.from({ length: A.R.n }, (): EnergyBucket => ({ kwh: 0, films: 0, missing: 0, partial: 0 }));
  for (const f of cur) {
    const b = buckets[bucketIndex(A.R, filmTime(f))];
    if (!b) continue;
    b.films += 1;
    if (!hasEnergyReading(f, ctrl)) b.missing += 1;
    else { b.kwh = (b.kwh ?? 0) + kwhOf(f); if (f.energy_partial) b.partial += 1; }
  }
  for (const b of buckets) if (b.films > 0 && b.missing === b.films) b.kwh = null;
  const comparable = energyComplete(reading) && energyComplete(A.prev ? energyReading(A.prev, ctrl) : null);
  const ranked = cur.filter((f) => hasEnergyReading(f, ctrl)).sort((a, b) => kwhOf(b) - kwhOf(a));
  const complete = ranked.filter((f) => !f.energy_partial);
  const byRes = shownResSeries(res, cur).map((series) => {
    const films = cur.filter((f) => f.res === series.res), read = films.filter((f) => hasEnergyReading(f, ctrl));
    return { series, films: films.length, measured: read.length, avg: avg(read.map(kwhOf)) };
  });
  // The breakdown splits exactly the films the total counts: every restore_wh contributor (failed attempts
  // included, 本机 too when it restored) and every restoring worker without a reading (「—」), so the groups
  // plus 本机's post-processing sum to the total. Under the 本机 filter only the post-processing counts.
  // Per group, C4's rule: incomplete when a film it fed is energy_partial or a film it restored lacks its reading.
  const machines: EnergyGroup[] = [];
  if (!ctrl) {
    const fed = new Map<string, Set<AnalyticsFilm>>(), wh = new Map<string, number>();
    for (const f of cur) {
      for (const [w, e] of f.restore_wh) {
        fed.set(w, (fed.get(w) ?? new Set()).add(f));
        wh.set(w, (wh.get(w) ?? 0) + e);
      }
    }
    const ids = new Set([...fed.keys(), ...cur.map((f) => f.worker).filter((w): w is string => w !== null)]);
    const isCtrl = (id: string) => id === A.controller?.id;
    const order = [...ids].sort((a, b) => Number(isCtrl(a)) - Number(isCtrl(b)) || machineName(data, a).localeCompare(machineName(data, b)));
    for (const id of order) {
      const own = fed.get(id) ?? new Set<AnalyticsFilm>();
      const touched = new Set([...own, ...cur.filter((f) => f.worker === id)]);
      machines.push({
        key: id, label: isCtrl(id) ? "本机（修复）" : machineName(data, id), kwh: own.size ? wh.get(id)! / 1000 : null,
        partial: [...touched].some((f) => f.energy_partial || !own.has(f)),
      });
    }
  }
  const post = cur.filter((f) => f.controller_wh != null);
  machines.push({
    key: "post", label: "本机（后处理）", kwh: post.length ? sum(post.map((f) => f.controller_wh!)) / 1000 : null,
    partial: cur.some((f) => f.controller_wh == null || f.energy_partial),
  });
  return {
    reading, buckets, prev: comparable ? bucketize(A, A.prev!, kwhOf, true) : null,
    perFilm: reading.kwh !== null ? reading.kwh / reading.measured : null,
    most: ranked[0] ?? null, least: complete[complete.length - 1] ?? null, top: ranked.slice(0, 5), byRes, byMachine: machines,
  };
}

// ---- 设备在线 (§8.4 item 7; C5 AC2) ----
export type LaneState = "online" | "paused" | "offline" | "none"; // none = 无记录 (no span: 主控未运行或未收到心跳)
export interface LaneSegment { s: number; e: number; state: LaneState; reason: AnalyticsStateReason; open: boolean }
export interface UptimeLane {
  m: AnalyticsMachine;
  isNew: boolean;
  from: number; // the range start, or first_seen for a 新加入 machine (D14)
  to: number; // now, or the range end
  segs: LaneSegment[]; // contiguous from `from` to `to`; gaps (and time after the last recorded tick) are 无记录
  online: number; paused: number; offline: number; none: number; // ms
  rate: number | null; // 在线率 %: (online + 停领) ÷ recorded time; null without any record
}
const overlaps = (s: number, e: number, a: number, b: number) => s < b && e > a;
// an open span ends at its last recorded tick: time after it is unrecorded (§6.2, as machineDays / C4-X3)
const spanEnd = (sp: AnalyticsSpan) => Date.parse(sp.end);

/** The machines of the 设备在线 panel: the GPU fleet in scope (retired ones only with a span in range), then 本机. */
function laneMachines(data: WorkflowAnalytics, A: Analysis): AnalyticsMachine[] {
  if (A.ctrl) return A.controller ? [A.controller] : [];
  if (A.machine !== ALL_MACHINES) return A.fleet;
  const byName = (a: AnalyticsMachine, b: AnalyticsMachine) => a.name.localeCompare(b.name);
  const seen = (m: AnalyticsMachine) => data.devices.spans.some((sp) => sp.machine === m.id
    && overlaps(Date.parse(sp.start), spanEnd(sp), A.R.start, A.visEnd));
  const retired = data.machines.filter((m) => m.kind === "gpu" && m.retired && seen(m)).sort(byName);
  return [...[...A.fleet].sort(byName), ...retired, ...(A.controller ? [A.controller] : [])];
}

export function uptimeLanes(data: WorkflowAnalytics, A: Analysis): UptimeLane[] {
  const to = A.visEnd;
  return laneMachines(data, A).map((m) => {
    const isNew = isNewMachine(m, A.R, A.now);
    const from = isNew && m.first_seen ? Math.max(A.R.start, Date.parse(m.first_seen)) : A.R.start;
    const segs: LaneSegment[] = [];
    const tot = { online: 0, paused: 0, offline: 0, none: 0 };
    const push = (s: number, e: number, state: LaneState, reason: AnalyticsStateReason = "", open = false) => {
      if (e <= s) return;
      segs.push({ s, e, state, reason, open });
      tot[state] += e - s;
    };
    if (to > from) {
      const spans = data.devices.spans.filter((sp) => sp.machine === m.id).sort((a, b) => Date.parse(a.start) - Date.parse(b.start));
      let at = from;
      for (const sp of spans) {
        const a = Math.max(at, Date.parse(sp.start)), b = Math.min(to, spanEnd(sp));
        if (b <= a) continue;
        push(at, a, "none");
        push(a, b, sp.state, sp.reason, sp.open);
        at = b;
      }
      push(at, to, "none");
    }
    const recorded = tot.online + tot.paused + tot.offline;
    return { m, isNew, from, to, segs, ...tot, rate: recorded > 0 ? ((tot.online + tot.paused) / recorded) * 100 : null };
  });
}

// ---- 异常与报警 (§8.4 item 8, §7.2, §7.3, D5, D6, D16; C5 AC3) ----
export type Severity = AnalyticsFailure["severity"];
const FAILURE_TYPE: Record<AnalyticsFailureType, { label: string; hint: string }> = {
  machine_fault: { label: "应用程序控制拦截", hint: "WinError 4551 · 机器故障，自动停领并改派" },
  missing_model: { label: "缺少模型文件", hint: "工作机缺少 LTX 模型文件，需要补齐" },
  missing_ffprobe: { label: "缺少 ffprobe", hint: "工作机缺少 ffprobe，需要补装" },
  ltx_failed: { label: "LTX 失败", hint: "修复进程异常退出" },
  scrape_whitelist: { label: "刮削白名单", hint: "刮削遇到白名单以外的文件" },
  other: { label: "其他失败", hint: "其他失败，见说明" },
  interrupted: { label: "运行中断", hint: "运行被中断（如重启），自动重新运行" },
  stopped: { label: "已停止", hint: "手动停止或判定已停止，已重新排队" },
  translate_paused: { label: "翻译暂停", hint: "翻译服务暂停，恢复后自动继续" },
  transient: { label: "临时失败（自动重试）", hint: "临时错误，自动重试" },
};
const EXHAUSTED: Partial<Record<AnalyticsFailureType, { label: string; hint: string }>> = {
  transient: { label: "临时失败（重试已用尽）", hint: "临时错误的自动重试已用尽，需要处理" },
  translate_paused: { label: "翻译暂停（重试已用尽）", hint: "翻译暂停的自动重试已用尽，需要处理" },
};
const CLIENT_TYPE = {
  offline: { label: "设备离线", hint: "工作机失联（超过 60 秒没有心跳）" },
  outage: { label: "翻译服务中断", hint: "翻译服务无响应，恢复后自动继续" },
} as const;
const failureType = (f: AnalyticsFailure) => (f.exhausted && EXHAUSTED[f.type]) || FAILURE_TYPE[f.type];
const typeKey = (f: AnalyticsFailure) => `${f.type}${f.exhausted && EXHAUSTED[f.type] ? ":x" : ""}`;
const TYPE_ORDER = [
  "machine_fault", "missing_model", "missing_ffprobe", "ltx_failed", "scrape_whitelist", "transient:x", "translate_paused:x", "other",
  "interrupted", "stopped", "translate_paused", "transient", "offline", "outage",
];

export type OutcomeTone = "green" | "blue" | "amber" | "red" | "neutral";
const OUTCOME: Record<AnalyticsFailure["outcome"], [string, OutcomeTone]> = {
  retried_ok: ["已重试成功", "green"], reassigned: ["已自动改派", "blue"], retrying: ["重试中", "neutral"], needs_action: ["需要处理", "amber"],
};

export interface Burst { machine: string; rows: AnalyticsFailure[]; start: number; end: number; banner: boolean }
const BURST_MIN = 3;
/**
 * D16: ≥ 3 machine_fault failures on one machine within any 1 h window; windows that share a failure merge
 * into one burst. Its rows are always highlighted; the banner only when it ended in the last 24 h or has a
 * needs_action row. Newest burst first.
 */
export function failureBursts(failures: AnalyticsFailure[], now: number): Burst[] {
  const by = new Map<string, AnalyticsFailure[]>();
  for (const f of failures) {
    if (f.type !== "machine_fault" || !isFail(f)) continue;
    const list = by.get(f.machine);
    if (list) list.push(f); else by.set(f.machine, [f]);
  }
  const out: Burst[] = [];
  for (const [machine, fs] of by) {
    const s = [...fs].sort((a, b) => failureTime(a) - failureTime(b)), t = s.map(failureTime);
    // qualifying windows [i, j]: ≥ 3 faults within 1 h (two pointers over the sorted times); windows that share
    // a failure (index overlap) merge into one burst — faults merely ≤ 1 h apart do not link two windows
    let cur: [number, number] | null = null;
    const flush = () => {
      if (!cur) return;
      const rows = s.slice(cur[0], cur[1] + 1), start = t[cur[0]], end = t[cur[1]];
      out.push({ machine, rows, start, end, banner: end >= now - DAY || rows.some((r) => r.outcome === "needs_action") });
      cur = null;
    };
    for (let i = 0, j = 0; i < s.length; i++) {
      if (j < i) j = i;
      while (j + 1 < s.length && t[j + 1] - t[i] <= HOUR) j++;
      if (j - i + 1 < BURST_MIN) continue;
      if (cur && i <= cur[1]) cur[1] = j;
      else { flush(); cur = [i, j]; }
    }
    flush();
  }
  return out.sort((a, b) => b.end - a.end);
}

export interface AlertEvent {
  key: string; at: number; machine: string; kind: "failure" | "offline" | "outage";
  label: string; severity: Severity; code: string | null; outcome: string; tone: OutcomeTone; note: string; burst: boolean;
}
export interface AlertType { key: string; label: string; severity: Severity; hint: string; n: number }
export interface AlertFigures {
  events: AlertEvent[]; // failures and warnings, offline spans and outages, newest first
  types: AlertType[]; // types that occurred: failures first, then warnings
  perMachine: Array<{ id: string; label: string; n: number }>; // severity fail only
  bursts: Burst[];
  fails: number; warns: number; offline: number; outages: number; // warns = failure rows of severity warn
}

const TICK = 30_000; // the recorder's tick (§6.2)
/**
 * Whether a closed offline span ended in a recovery: the same machine's chronologically next span (start ≥ its
 * end, within a tick) is online or 停领. The recorder also closes spans on a controller restart or a stall
 * (§6.2); then nothing says the machine came back, and the outcome is 「记录中断」, not 「已恢复」. The spans
 * are indexed per machine by start once, so a year of history is one sort plus a binary search per span (OD2).
 */
function recoveryIndex(spans: AnalyticsSpan[]): (sp: AnalyticsSpan) => boolean {
  const by = new Map<string, Array<{ sp: AnalyticsSpan; t: number }>>();
  for (const sp of spans) {
    const list = by.get(sp.machine);
    if (list) list.push({ sp, t: Date.parse(sp.start) }); else by.set(sp.machine, [{ sp, t: Date.parse(sp.start) }]);
  }
  for (const list of by.values()) list.sort((x, y) => x.t - y.t);
  return (sp) => {
    const list = by.get(sp.machine) ?? [], end = spanEnd(sp);
    let lo = 0, hi = list.length;
    while (lo < hi) { const mid = (lo + hi) >> 1; if (list[mid].t < end) lo = mid + 1; else hi = mid; }
    while (lo < list.length && list[lo].sp === sp) lo += 1; // a zero-length span starts at its own end
    const next = list[lo];
    return next !== undefined && next.t - end <= TICK && next.sp.state !== "offline";
  };
}

export function alertFigures(data: WorkflowAnalytics, A: Analysis): AlertFigures {
  const ctrlId = A.controller?.id;
  const bursts = failureBursts(A.failures, A.now);
  const inBurst = new Set(bursts.flatMap((b) => b.rows));
  const events: AlertEvent[] = [];
  const types = new Map<string, AlertType>();
  const count = (key: string, label: string, hint: string, severity: Severity) => {
    const t = types.get(key) ?? { key, label, hint, severity, n: 0 };
    t.n += 1;
    if (severity === "fail") t.severity = "fail";
    types.set(key, t);
  };
  A.failures.forEach((f, i) => {
    const ty = failureType(f), [outcome, tone] = OUTCOME[f.outcome];
    count(typeKey(f), ty.label, ty.hint, f.severity);
    events.push({
      key: `f${i}`, at: failureTime(f), machine: f.machine, kind: "failure", label: ty.label, severity: f.severity, code: f.code,
      outcome, tone, note: f.note || ty.hint, burst: inBurst.has(f),
    });
  });
  // offline spans of the machines in scope; outages belong to 本机 (hidden under a GPU-machine filter)
  const scope = A.ctrl ? new Set(ctrlId ? [ctrlId] : []) : A.machine === ALL_MACHINES ? null : new Set([A.machine]);
  const end = Math.min(A.R.end, A.now);
  let offline = 0;
  const recovered = recoveryIndex(data.devices.spans);
  data.devices.spans.forEach((sp, i) => {
    const s = Date.parse(sp.start), e = spanEnd(sp);
    if (sp.state !== "offline" || (scope && !scope.has(sp.machine)) || !overlaps(s, e, A.R.start, end)) return;
    offline += 1;
    const back = !sp.open && recovered(sp);
    count("offline", CLIENT_TYPE.offline.label, CLIENT_TYPE.offline.hint, "warn");
    events.push({
      key: `d${i}`, at: s, machine: sp.machine, kind: "offline", label: CLIENT_TYPE.offline.label, severity: "warn", code: null,
      outcome: sp.open ? "仍离线" : back ? "已恢复" : "记录中断", tone: sp.open ? "red" : "neutral",
      note: `离线 ${fSpan(e - s)}${sp.open ? "，至今" : back ? "" : "，之后没有记录"}`, burst: false,
    });
  });
  const outages = scope === null || A.ctrl ? outageFigures(data, A).items : [];
  outages.forEach((o, i) => {
    count("outage", CLIENT_TYPE.outage.label, CLIENT_TYPE.outage.hint, "warn");
    events.push({
      key: `o${i}`, at: o.start, machine: ctrlId ?? "", kind: "outage", label: CLIENT_TYPE.outage.label, severity: "warn", code: null,
      outcome: o.open ? "尚未恢复" : "已自动恢复", tone: o.open ? "amber" : "neutral",
      note: `${o.paused ? "翻译暂停" : "翻译服务无响应"} · ${fSpan(o.ms)}${o.open ? "，至今" : ""}`, burst: false,
    });
  });
  events.sort((a, b) => b.at - a.at || a.key.localeCompare(b.key));
  const rank = (t: AlertType) => (t.severity === "fail" ? 0 : 1) * 100 + TYPE_ORDER.indexOf(t.key);
  // failures per machine: the fleet in scope (zero included), any other machine that failed, then 本机
  const fails = A.failures.filter(isFail);
  const byName = (a: string, b: string) => machineName(data, a).localeCompare(machineName(data, b));
  let ids: string[];
  if (A.ctrl) ids = ctrlId ? [ctrlId] : [];
  else if (A.machine !== ALL_MACHINES) ids = [A.machine];
  else {
    const fleet = A.fleet.map((m) => m.id).sort(byName);
    const others = [...new Set(fails.map((f) => f.machine))].filter((id) => id !== ctrlId && !fleet.includes(id)).sort(byName);
    ids = [...fleet, ...others, ...(ctrlId ? [ctrlId] : [])];
  }
  return {
    events, types: [...types.values()].sort((a, b) => rank(a) - rank(b)),
    perMachine: ids.map((id) => ({ id, label: machineName(data, id), n: fails.filter((f) => f.machine === id).length })),
    bursts, fails: fails.length, warns: A.failures.length - fails.length, offline, outages: outages.length,
  };
}

// ---- 片源与片库 (§8.4 item 9, D9; C5 AC4) ----
export interface Histogram { labels: string[]; counts: number[]; missing: number }
/** Bins split at `edges`, the last one open-ended (「40+」); the first starts at 0 or, `openLow`, is 「<edge」. */
export function histogram(vals: Array<number | null>, edges: number[], fmt: (v: number) => string, openLow = false): Histogram {
  const labels = [
    openLow ? `<${fmt(edges[0])}` : `0–${fmt(edges[0])}`,
    ...edges.slice(1).map((e, i) => `${fmt(edges[i])}–${fmt(e)}`),
    `${fmt(edges[edges.length - 1])}+`,
  ];
  const counts = labels.map(() => 0);
  let missing = 0;
  for (const v of vals) {
    if (v == null || !Number.isFinite(v)) { missing += 1; continue; }
    const i = edges.findIndex((e) => v < e);
    counts[i < 0 ? edges.length : i] += 1;
  }
  return { labels, counts, missing };
}
export const GB = 1e9; // decimal, as the drive is sold (the 1 TB INBOX)
const SIZE_EDGES = [5, 10, 15, 20, 25, 30, 35, 40]; // GB
const LENGTH_EDGES = [1.5, 2, 2.5, 3, 3.5]; // hours
export const MARKS: ReadonlyArray<readonly [string, string]> = [
  ["chinese_subtitles", "中文字幕"], ["uncensored", "无码"], ["4k", "4K"], ["restored", "修复"],
];
export interface SourceFigures {
  size: Histogram; duration: Histogram;
  actors: Array<[string, number]>; // the 8 actors with most films archived into their folder
  marks: Array<{ key: string; label: string; n: number }>;
  total: number;
}
export function sourceFigures(A: Analysis): SourceFigures {
  const c = A.cur, by = new Map<string, number>();
  for (const f of c) if (f.actor) by.set(f.actor, (by.get(f.actor) ?? 0) + 1);
  return {
    size: histogram(c.map((f) => (f.input_bytes == null ? null : f.input_bytes / GB)), SIZE_EDGES, String),
    duration: histogram(c.map((f) => (f.duration_s == null ? null : f.duration_s / 3600)), LENGTH_EDGES, (v) => v.toFixed(1), true),
    actors: [...by.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 8),
    marks: MARKS.map(([key, label]) => ({ key, label, n: c.filter((f) => f.marks.includes(key)).length })),
    total: c.length,
  };
}
export interface InboxFigures {
  total: number; free: number; used: number; pending: number; other: number; pendingFilms: number;
  freedPerDay: number; avgInput: number;
  canNow: number | null; // 现在还可放入约 N 部 (估算): free space ÷ the 7-day average size; no projection (D9)
}
export function inboxFigures(x: AnalyticsInbox | null): InboxFigures | null {
  if (!x) return null;
  return {
    total: x.total_bytes, free: x.free_bytes, used: x.used_bytes, pending: x.pending_bytes,
    other: Math.max(0, x.used_bytes - x.pending_bytes), pendingFilms: x.pending_films,
    freedPerDay: x.freed_bytes_per_day_7d, avgInput: x.avg_input_bytes_7d,
    canNow: x.avg_input_bytes_7d > 0 ? Math.floor(x.free_bytes / x.avg_input_bytes_7d) : null,
  };
}
