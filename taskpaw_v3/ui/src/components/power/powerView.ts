// PWR-3: the 功耗 tab's pure view model — machines from the control view, chart series, ranges.
import type { LivePower, MachineTemperatures, PeerEnergy, PowerHistory, PowerPeerRow, PowerRange, PowerView } from "../../api";
import { ACTIVITY_ORDER, machineColor } from "./powerModel";

export interface ChartSeries { id: string; name: string; color: string; values: Array<number | null> }

export const RANGE: Record<PowerRange, { label: string; agg: string; tickEvery: number; fmt: (t: number) => string; tick: (t: number) => string }> = {
  "24h": { label: "24 小时", agg: "每点 15 分钟平均", tickEvery: 16, fmt: hm, tick: hm },
  "7d": { label: "7 天", agg: "每点 1 小时平均", tickEvery: 24, fmt: (t) => `${md(t)} ${hm(t).slice(0, 2)}:00`, tick: md },
  "30d": { label: "30 天", agg: "每点 6 小时平均", tickEvery: 20, fmt: (t) => `${md(t)} ${hm(t).slice(0, 2)}:00 起 6 小时`, tick: md },
};
function hm(t: number) {
  const d = new Date(t * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}
function md(t: number) {
  const d = new Date(t * 1000);
  return `${d.getMonth() + 1}/${d.getDate()}`;
}
export const rangeLabel = (r: PowerRange) => RANGE[r].label;
export const rangeAgg = (r: PowerRange) => RANGE[r].agg;

export function peakOf(series: ChartSeries[], n: number): number {
  let max = 0;
  for (let i = 0; i < n; i++) max = Math.max(max, series.reduce((a, s) => a + (s.values[i] ?? 0), 0));
  return max;
}

export interface Machine {
  id: string; name: string; hw: string; local: boolean; state: string; stale: boolean; color: string;
  live: LivePower | null; day: PowerHistory | null; hist: PowerHistory | null;
  // the period energy that counts in the totals: live, or a peer's last reading while its period lasts
  energy?: PeerEnergy | null; energyStale?: boolean; duplicateOf?: string | null;
  // P2-4 R11: an enrolled GPU worker that joined the list by itself (工作机 · 自动加入)
  auto?: boolean;
  temperatures?: MachineTemperatures | null;
}

const STATE_TEXT: Record<string, [string, "green" | "amber" | "red" | "neutral"]> = {
  ok: ["在线", "green"],
  off: ["未开启功耗估算", "amber"],
  no_power: ["无功耗数据（需升级 TaskPaw）", "amber"],
  not_found: ["无功耗数据（需升级 TaskPaw）", "amber"],
  pending: ["检查中", "neutral"],
  unreachable: ["连不上（检查网络 / 防火墙）", "red"],
  timeout: ["连不上（检查网络 / 防火墙）", "red"],
  invalid_address: ["地址无效或为公网地址", "red"],
  self: ["就是本机，不重复计入", "amber"],
};
export function stateChip(m: Pick<Machine, "name" | "state" | "stale" | "duplicateOf">): [string, "green" | "amber" | "red" | "neutral"] {
  if (m.state === "unauthorized") return [`令牌不一致：在 ${m.name} 的 设置 → 网络与访问 粘贴主控的访问令牌`, "red"];
  if (m.state === "duplicate") return [`与 ${m.duplicateOf ?? "另一项"} 是同一台机器，不重复计入`, "amber"];
  if (m.state === "ok" && m.stale) return ["数据过期", "amber"];
  return STATE_TEXT[m.state] ?? ["离线", "red"];
}

// A peer's history is data from another machine: the backend validates it, and the page still never
// trusts it. Anything without the arrays and totals the page reads is unavailable (null), never drawn.
function sane(h: PowerHistory | null | undefined): PowerHistory | null {
  if (!h || !Array.isArray(h.t) || !Array.isArray(h.avg_w) || h.avg_w.length !== h.t.length) return null;
  if (typeof h.totals?.wh !== "number" || typeof h.totals.activity_wh !== "object" || h.totals.activity_wh === null) return null;
  return h;
}

const energyOf = (p: Pick<LivePower, "today_wh" | "week_wh" | "month_wh">): PeerEnergy =>
  ({ today_wh: p.today_wh, week_wh: p.week_wh, month_wh: p.month_wh });

function peerMachine(p: PowerPeerRow, ranged: PowerPeerRow | undefined, i: number): Machine {
  const fresh = p.state === "ok" && !p.stale;
  const counted = p.state !== "duplicate" && p.state !== "self";
  return {
    duplicateOf: p.duplicate_of ?? null, auto: p.source === "workflow",
    energy: counted ? (p.energy ?? (fresh && p.power ? energyOf(p.power) : null)) : null,
    energyStale: p.energy_stale ?? !fresh,
    id: `peer:${p.name}`, name: p.name, local: false, state: p.state, stale: p.stale, color: machineColor(i),
    hw: [p.address, p.version ? `TaskPaw ${p.version}` : null].filter(Boolean).join(" · "),
    live: fresh ? p.power : null, day: sane(p.history), hist: sane(ranged?.history),
    temperatures: p.temperatures,
  };
}

export function machinesOf(day: PowerView | undefined, ranged: PowerView | undefined): Machine[] {
  if (!day) return [];
  const local: Machine = {
    id: "local", name: day.machine?.name || "本机", local: true, state: day.power ? "ok" : "off", stale: false,
    color: machineColor(0), hw: ["本机 Agent", day.machine?.os].filter(Boolean).join(" · "),
    live: day.power, day: day.history, hist: ranged?.history ?? null,
    // the retained totals count with sampling off (power null); the live figures are the same numbers when on
    energy: day.power ? energyOf(day.power) : day.energy ?? null, energyStale: false,
    temperatures: day.temperatures,
  };
  const peers = (day.peers ?? []).map((p, i) => peerMachine(p, ranged?.peers?.find((r) => r.name === p.name), i + 1));
  return [local, ...peers];
}

// Align every machine's points on the local machine's time axis (same range, same step), by timestamp
// only: a point whose bucket start the local axis does not have is a gap (null), never moved to the
// same index (a peer whose clock or range start differs would otherwise be drawn at the wrong time).
export function chartSeries(machines: Machine[]): { series: ChartSeries[]; times: number[] } {
  const base = machines.find((m) => m.hist)?.hist;
  if (!base) return { series: [], times: [] };
  const times = base.t;
  const series = machines.filter((m) => m.hist).map((m) => {
    const h = m.hist as PowerHistory;
    const at = new Map(h.t.map((t, i) => [t, h.avg_w[i]] as const));
    return { id: m.id, name: m.name, color: m.color, values: times.map((t) => at.get(t) ?? null) };
  });
  return { series, times };
}

// Wh per activity over the displayed window only: the same timestamps the chart draws (the first
// machine's axis), for every machine. A history's cached `totals` also cover points an offline peer
// no longer shows, so they are never used; a missing or non-finite point counts as nothing.
export function activityTotals(machines: Machine[]): Record<string, number> {
  const sums = Object.fromEntries(ACTIVITY_ORDER.map((k) => [k, 0])) as Record<string, number>;
  const window = new Set(chartSeries(machines).times);
  for (const m of machines) {
    const h = m.hist;
    if (!h) continue;
    for (const k of ACTIVITY_ORDER) {
      const wh = h.activity_wh?.[k];
      if (!Array.isArray(wh)) continue;
      h.t.forEach((t, i) => {
        const v = wh[i];
        if (window.has(t) && typeof v === "number" && Number.isFinite(v)) sums[k] += v;
      });
    }
  }
  return sums;
}

// PW09 defaults per platform: tdp / idle / other W.
export function platformDefaults(os: string | undefined) {
  return os === "Darwin" ? { tdp: 20, idle: 3, other: 5 } : { tdp: 65, idle: 10, other: 35 };
}

