// PWR-3: the 功耗 tab's pure view model — machines from the control view, chart series, ranges.
import type { LivePower, PowerHistory, PowerPeerRow, PowerRange, PowerView } from "../../api";
import { machineColor } from "./powerModel";

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
}

const STATE_TEXT: Record<string, [string, "green" | "amber" | "red" | "neutral"]> = {
  ok: ["在线", "green"],
  off: ["未开启功耗估算", "amber"],
  no_power: ["无功耗数据（需升级 TaskPaw）", "amber"],
  not_found: ["无功耗数据（需升级 TaskPaw）", "amber"],
  pending: ["检查中", "neutral"],
  unauthorized: ["API Token 不一致", "red"],
  invalid_address: ["地址无效或为公网地址", "red"],
};
export function stateChip(m: Pick<Machine, "state" | "stale">): [string, "green" | "amber" | "red" | "neutral"] {
  if (m.state === "ok" && m.stale) return ["数据过期", "amber"];
  return STATE_TEXT[m.state] ?? ["离线", "red"];
}

function peerMachine(p: PowerPeerRow, ranged: PowerPeerRow | undefined, i: number): Machine {
  return {
    id: `peer:${p.name}`, name: p.name, local: false, state: p.state, stale: p.stale, color: machineColor(i),
    hw: [p.address, p.version ? `TaskPaw ${p.version}` : null].filter(Boolean).join(" · "),
    live: p.state === "ok" && !p.stale ? p.power : null, day: p.history, hist: ranged?.history ?? null,
  };
}

export function machinesOf(day: PowerView | undefined, ranged: PowerView | undefined): Machine[] {
  if (!day) return [];
  const local: Machine = {
    id: "local", name: day.machine?.name || "本机", local: true, state: day.power ? "ok" : "off", stale: false,
    color: machineColor(0), hw: ["本机 Agent", day.machine?.os].filter(Boolean).join(" · "),
    live: day.power, day: day.history, hist: ranged?.history ?? null,
  };
  const peers = (day.peers ?? []).map((p, i) => peerMachine(p, ranged?.peers?.find((r) => r.name === p.name), i + 1));
  return [local, ...peers];
}

// Align every machine's points on the local machine's time axis (same range, same step).
export function chartSeries(machines: Machine[]): { series: ChartSeries[]; times: number[] } {
  const base = machines.find((m) => m.hist)?.hist;
  if (!base) return { series: [], times: [] };
  const times = base.t;
  const series = machines.filter((m) => m.hist).map((m) => {
    const h = m.hist as PowerHistory;
    const at = new Map(h.t.map((t, i) => [t, h.avg_w[i]] as const));
    const byIndex = h.t.length === times.length;
    return {
      id: m.id, name: m.name, color: m.color,
      values: times.map((t, i) => (at.has(t) ? at.get(t) ?? null : byIndex ? h.avg_w[i] ?? null : null)),
    };
  });
  return { series, times };
}

// PW09 defaults per platform: tdp / idle / other W.
export function platformDefaults(os: string | undefined) {
  return os === "Darwin" ? { tdp: 20, idle: 3, other: 5 } : { tdp: 65, idle: 10, other: 35 };
}

