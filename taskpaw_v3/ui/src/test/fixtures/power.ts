// 功耗 tab fixture data in the backend's shapes (PWR-1 `/control/power`, PWR-3 peers). Shared by the
// tests and the screenshot harness; nothing here is rendered as-is by the shipped UI.
import type { LivePower, PowerHistory, PowerPeerRow, PowerRange, PowerView } from "../../api";

const STEP: Record<PowerRange, [number, number]> = { "24h": [96, 900], "7d": [168, 3600], "30d": [120, 21600] };
export const NOW = Date.UTC(2026, 9, 22, 8, 30) / 1000;

// A deterministic busy / idle pattern: `busy` W while working, `idle` W otherwise; `gapAt` points have no samples.
export function history(range: PowerRange, busy: number, idle: number, seed: number, gapAt: number[] = []): PowerHistory {
  const [n, step] = STEP[range];
  const first = Math.floor(NOW / step) * step - (n - 1) * step;
  const t = Array.from({ length: n }, (_, i) => first + i * step);
  const working = (i: number) => ((i + seed) % 23) < 13;
  const avg = t.map((_, i) => (gapAt.includes(i) ? null : Math.round((working(i) ? busy : idle) + ((i * 37 + seed) % 11) - 5)));
  const wh = avg.map((w) => (w == null ? 0 : (w * step) / 3600));
  const act = (k: "restore" | "asr" | "translate" | "idle" | "other") => avg.map((w, i) => {
    if (w == null) return 0;
    const kind = working(i) ? (seed % 3 === 0 ? "translate" : i % 5 === 0 ? "asr" : "restore") : "idle";
    return kind === k ? wh[i] : 0;
  });
  const activity = { restore: act("restore"), asr: act("asr"), translate: act("translate"), idle: act("idle"), other: act("other") };
  const sum = (xs: number[]) => xs.reduce((a, b) => a + b, 0);
  return {
    range, step_s: step, estimated: true, t, covered_s: avg.map((w) => (w == null ? 0 : step)), avg_w: avg, wh,
    gpu_wh: wh.map((v) => v * 0.7), cpu_wh: wh.map((v) => v * 0.18), other_wh: wh.map((v) => v * 0.12),
    activity_wh: activity,
    totals: { wh: sum(wh), activity_wh: {
      restore: sum(activity.restore), asr: sum(activity.asr), translate: sum(activity.translate), idle: sum(activity.idle), other: 0,
    } },
  };
}

export const LOCAL_LIVE: LivePower = {
  w: 286, gpu_w: 210, cpu_w: 46, other_w: 30, estimated: true,
  today_wh: 4550, week_wh: 20400, month_wh: 114000, activity: "restore",
};
export const GPU02_LIVE: LivePower = {
  w: 172, gpu_w: 118, cpu_w: 24, other_w: 30, estimated: true,
  today_wh: 1920, week_wh: 9630, month_wh: 54900, activity: "asr",
};
export const TRANS_LIVE: LivePower = {
  w: 61, gpu_w: null, cpu_w: 33, other_w: 28, estimated: true,
  today_wh: 830, week_wh: 4180, month_wh: 24200, activity: "translate",
};

function peer(name: string, address: string, state: string, live: LivePower | null, h: PowerHistory | null): PowerPeerRow {
  return {
    name, address, state, error: state === "ok" ? null : state, machine: name, version: state === "no_power" ? "3.9.10" : "3.9.18",
    os: "Windows-11", power: live, history: h, age_s: state === "pending" ? null : 2.1, stale: state === "pending",
  };
}

export function powerView(range: PowerRange = "24h"): PowerView {
  return {
    power: LOCAL_LIVE,
    history: history(range, 280, 60, 1, range === "24h" ? [40, 41, 42] : []),
    machine: { name: "HOME-PC", os: "Windows" },
    peers: [
      peer("GPU-02", "192.168.1.20", "ok", GPU02_LIVE, history(range, 170, 52, 7)),
      peer("翻译机", "192.168.1.30:5680", "ok", TRANS_LIVE, history(range, 62, 40, 3)),
      peer("GPU-04", "192.168.1.40", "no_power", null, null),
      peer("GPU-05", "gpu-05.local", "unreachable", null, null),
    ],
  };
}

export const POWER_SETTINGS = {
  enabled: true, tdp_w: null, idle_w: null, other_w: 30, psu_efficiency: null, cpu_source: "estimate" as const,
  peers: [
    { name: "GPU-02", address: "192.168.1.20" }, { name: "翻译机", address: "192.168.1.30:5680" },
    { name: "GPU-04", address: "192.168.1.40" }, { name: "GPU-05", address: "gpu-05.local" },
  ],
};
