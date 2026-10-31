// 数据分析 model (EPIC 2026-10-31 §8.2, §10; C3 #390 AC5/AC6/AC11). Every expected time is built with
// local-time constructors, so the assertions hold in any time zone (buckets are local, §7.1).
import { describe, expect, it } from "vitest";
import type { AnalyticsFailure, AnalyticsFilm, AnalyticsMachine, WorkflowAnalytics } from "../api";
import {
  bucketIndex, bucketTitle, bucketize, compute, delta, energyReading, isNewMachine, kpiFigures, machineGroups, prevBucketIndex, presentAt,
  recordedPresence,
  querySince, rangeDef, sliceValues, stats, type Filters,
} from "../components/media/analytics/analyticsModel";

const H = 3_600_000, DAY = 24 * H;
const local = (y: number, mo: number, d: number, h = 0, mi = 0) => new Date(y, mo - 1, d, h, mi).getTime();
const iso = (t: number) => new Date(t).toISOString();
const NOW = local(2026, 10, 31, 14, 20);
const CTRL = "ctrl-1";

const film = (at: number, o: Partial<AnalyticsFilm> = {}): AnalyticsFilm => ({
  code: `ZZT-${Math.round(at / 1000) % 100000}`, archived_at: iso(at), created_at: iso(at - 30 * H), res: "4K",
  duration_s: 7200, input_bytes: 1e10, worker: "gpu-a", queue_s: 3600, restore_s: 4 * 3600, asr_s: 600, translate_s: 60,
  translated: true, cues: null, subtitles: { generated_ja: 1 }, scrape_s: 120, archive_s: 60, lead_s: 30 * 3600,
  restore_wh: [["gpu-a", 800, 14400]], controller_wh: 50, energy_partial: false, target: "performer", actor: "Actor Test",
  second: false, marks: [], ...o,
});
const failure = (at: number, o: Partial<AnalyticsFailure> = {}): AnalyticsFailure => ({
  at: iso(at), code: "ZZT-0", stage: "restore", machine: "gpu-a", severity: "fail", type: "ltx_failed", exhausted: false,
  note: "Jasna LTX 失败（退出码 1）", outcome: "retried_ok", ...o,
});
const machine = (id: string, o: Partial<AnalyticsMachine> = {}): AnalyticsMachine => ({
  id, name: id, kind: "gpu", model: "RTX 5060 Ti 16GB", first_seen: iso(local(2026, 9, 1)), retired: false,
  state_now: "online", state_reason: "", state_since: null, ...o,
});
const data = (o: Partial<WorkflowAnalytics> = {}): WorkflowAnalytics => ({
  server_time: iso(NOW), since: null,
  coverage: { archives: iso(local(2026, 10, 10, 9)), energy: null, cues: null, devices: null, outages: null },
  machines: [machine(CTRL, { name: "本机", kind: "controller", model: "RTX 5060 8GB" }), machine("gpu-a"), machine("gpu-b")],
  films: [], failures: [], devices: { available: true, reason: "", spans: [] }, outages: [], inbox: null, ...o,
});
const F: Filters = { machine: "all", res: "all", compare: false };

describe("rangeDef", () => {
  it("7 天: seven local days ending with today, previous period right before", () => {
    const R = rangeDef("7d", NOW, local(2026, 10, 10));
    expect(R.kind).toBe("d");
    expect(R.n).toBe(7);
    expect(R.start).toBe(local(2026, 10, 25));
    expect(R.end).toBe(local(2026, 11, 1));
    expect(R.edges[3]).toBe(local(2026, 10, 28));
    expect(R.prevStart).toBe(local(2026, 10, 18));
    expect(R.prevEnd).toBe(R.start);
    expect(R.unit).toBe("每天");
    expect(querySince(R)).toBe(new Date(local(2026, 10, 18)).toISOString());
  });

  it("24 小时: 24 hourly buckets, the last one holds now; since is stable within the hour", () => {
    const R = rangeDef("24h", NOW, null);
    expect(R.kind).toBe("h");
    expect(R.n).toBe(24);
    expect(R.end).toBe(local(2026, 10, 31, 15));
    expect(R.start).toBe(local(2026, 10, 30, 15));
    expect(bucketIndex(R, NOW)).toBe(23);
    expect(querySince(rangeDef("24h", local(2026, 10, 31, 14, 59), null))).toBe(querySince(R));
    expect(querySince(rangeDef("24h", local(2026, 10, 31, 15, 1), null))).not.toBe(querySince(R));
  });

  it("30 天 daily and 90 天 weekly, both ending tomorrow at local midnight", () => {
    const m = rangeDef("30d", NOW, null);
    expect([m.kind, m.n, m.start, m.end]).toEqual(["d", 30, local(2026, 10, 2), local(2026, 11, 1)]);
    const q = rangeDef("90d", NOW, null);
    expect([q.kind, q.n, q.end, q.unit]).toEqual(["w", 13, local(2026, 11, 1), "每周"]);
    expect(q.edges[1] - q.edges[0]).toBeGreaterThanOrEqual(7 * DAY - H);
  });

  it("全部 starts at coverage.archives: daily ≤ 45 days, weekly > 45, monthly > 120; no previous period", () => {
    const cover = local(2026, 10, 10, 9, 30);
    const d = rangeDef("all", NOW, cover);
    expect([d.kind, d.start, d.effectiveStart, d.hasPrev]).toEqual(["d", local(2026, 10, 10), cover, false]);
    expect(d.end).toBe(local(2026, 11, 1));
    expect(querySince(d)).toBeUndefined();
    expect(rangeDef("all", NOW, local(2026, 9, 1)).kind).toBe("w");
    const m = rangeDef("all", NOW, local(2026, 3, 14));
    expect([m.kind, m.start, m.end]).toEqual(["m", local(2026, 3, 1), local(2026, 11, 1)]);
    expect(m.n).toBe(8);
  });

  it("effective start = max(range start, coverage.archives)", () => {
    expect(rangeDef("30d", NOW, local(2026, 10, 10, 9)).effectiveStart).toBe(local(2026, 10, 10, 9));
    expect(rangeDef("7d", NOW, local(2026, 10, 10, 9)).effectiveStart).toBe(local(2026, 10, 25));
  });
});

describe("buckets", () => {
  const R = rangeDef("7d", NOW, null);
  it("indexes by local day, end-exclusive", () => {
    expect(bucketIndex(R, R.start)).toBe(0);
    expect(bucketIndex(R, local(2026, 10, 25, 23, 59))).toBe(0);
    expect(bucketIndex(R, local(2026, 10, 26))).toBe(1);
    expect(bucketIndex(R, R.end - 1)).toBe(6);
    expect(bucketIndex(R, R.end)).toBe(-1);
    expect(bucketIndex(R, R.start - 1)).toBe(-1);
  });
  it("aligns a previous-period time onto the same position", () => {
    expect(prevBucketIndex(R, local(2026, 10, 19, 12))).toBe(1);
    expect(prevBucketIndex(rangeDef("all", NOW, null), local(2026, 10, 19, 12))).toBe(-1);
  });
  it("titles say 截至 for the bucket holding now", () => {
    expect(bucketTitle(R, 6, NOW)).toBe("10-31 周六（截至 14:20）");
    expect(bucketTitle(R, 0, NOW)).toBe("10-25 周日");
    expect(bucketTitle(rangeDef("24h", NOW, null), 23, NOW)).toBe("10-31 14:00–15:00");
  });
  it("bucketizes counts, sums and the previous period", () => {
    const A = compute(data({ films: [film(local(2026, 10, 26, 8)), film(local(2026, 10, 26, 20)), film(local(2026, 10, 19, 9))] }), R, F, NOW);
    expect(bucketize(A, A.cur)).toEqual([0, 2, 0, 0, 0, 0, 0]);
    expect(bucketize(A, A.cur, (f) => f.duration_s ?? 0)[1]).toBe(14400);
    expect(bucketize(A, A.prev!, undefined, true)).toEqual([0, 1, 0, 0, 0, 0, 0]);
  });
});

describe("compute", () => {
  const films = [
    film(local(2026, 10, 27, 10), { worker: "gpu-a", res: "4K" }),
    film(local(2026, 10, 28, 10), { worker: "gpu-b", res: "1080p" }),
    film(local(2026, 10, 29, 10), { worker: null, res: null, restore_s: null, restore_wh: [] }),
    film(local(2026, 10, 20, 10), { worker: "gpu-a" }),
  ];
  it("keeps the range and filters by machine and resolution locally", () => {
    const d = data({ films });
    const R = rangeDef("7d", NOW, Date.parse(d.coverage.archives!));
    expect(compute(d, R, F, NOW).cur).toHaveLength(3);
    expect(compute(d, R, { ...F, machine: "gpu-a" }, NOW).cur).toHaveLength(1);
    expect(compute(d, R, { ...F, machine: CTRL }, NOW).cur).toHaveLength(3); // 本机 post-processes every film
    expect(compute(d, R, { ...F, res: "1080p" }, NOW).cur.map((f) => f.worker)).toEqual(["gpu-b"]);
    expect(compute(d, R, { ...F, machine: CTRL }, NOW).fleet).toEqual([]);
    expect(compute(d, R, F, NOW).fleet.map((m) => m.id)).toEqual(["gpu-a", "gpu-b"]);
  });

  it("covered previous period, partial previous period and the uncovered fraction", () => {
    const d = data({ films });
    const cover = Date.parse(d.coverage.archives!);
    const week = compute(d, rangeDef("7d", NOW, cover), F, NOW);
    expect([week.prevOk, week.prevPartial, week.uncov]).toEqual([true, false, 0]);
    expect(week.prev).toHaveLength(1);
    const month = compute(d, rangeDef("30d", NOW, cover), F, NOW);
    expect(month.prevOk).toBe(false);
    expect(month.prev).toBeNull();
    expect(month.uncov).toBeCloseTo((cover - local(2026, 10, 2)) / (local(2026, 11, 1) - local(2026, 10, 2)), 6);
    expect(month.coverDays).toBe(Math.ceil((NOW - cover) / DAY));
    const late = data({ films, coverage: { ...d.coverage, archives: iso(local(2026, 10, 21)) } });
    const w2 = compute(late, rangeDef("7d", NOW, local(2026, 10, 21)), F, NOW);
    expect([w2.prevOk, w2.prevPartial]).toEqual([false, true]);
  });

  it("全部 has no uncovered band and no previous period (AC11)", () => {
    const d = data({ films });
    const A = compute(d, rangeDef("all", NOW, Date.parse(d.coverage.archives!)), F, NOW);
    expect([A.uncov, A.prevOk, A.visStart]).toEqual([0, false, Date.parse(d.coverage.archives!)]);
    expect(A.cur).toHaveLength(4);
  });

  it("takes a failure's resolution from the archived film with its code; unknown codes stay", () => {
    const d = data({
      films: [film(local(2026, 10, 27, 10), { code: "ZZT-4K", res: "4K" })],
      failures: [failure(local(2026, 10, 27, 1), { code: "ZZT-4K" }), failure(local(2026, 10, 27, 2), { code: "ZZT-NEW" }),
        failure(local(2026, 10, 9), { code: "ZZT-OLD" })],
    });
    const R = rangeDef("7d", NOW, Date.parse(d.coverage.archives!));
    expect(compute(d, R, F, NOW).failures.map((f) => f.code)).toEqual(["ZZT-4K", "ZZT-NEW"]);
    expect(compute(d, R, { ...F, res: "1080p" }, NOW).failures.map((f) => f.code)).toEqual(["ZZT-NEW"]);
    expect(compute(d, rangeDef("30d", NOW, Date.parse(d.coverage.archives!)), F, NOW).failures).toHaveLength(2); // before coverage: out
  });

  it("slices are null before the first archive", () => {
    const d = data();
    const A = compute(d, rangeDef("30d", NOW, Date.parse(d.coverage.archives!)), F, NOW);
    const vals = sliceValues(A, () => 1);
    expect(vals).toHaveLength(12);
    expect(vals[0]).toBeNull();
    expect(vals[11]).toBe(1);
  });
});

describe("stats", () => {
  it("min / quartiles / median / max / avg", () => {
    expect(stats([4, 1, 3, 2, 10])).toEqual({ n: 5, min: 1, p25: 2, p50: 3, p75: 4, max: 10, avg: 4 });
    expect(stats([2, 4])!.p25).toBe(2.5);
    expect(stats([])).toBeNull();
  });
});

describe("D14 「新加入」", () => {
  const cover = local(2026, 10, 10, 9);
  it("is new just past the first 10 % of the effective range, not just before", () => {
    const R = rangeDef("7d", NOW, cover);
    const edge = R.effectiveStart + 0.1 * (NOW - R.effectiveStart);
    expect(isNewMachine(machine("x", { first_seen: iso(edge + 1000) }), R, NOW)).toBe(true);
    expect(isNewMachine(machine("x", { first_seen: iso(edge - 1000) }), R, NOW)).toBe(false);
    expect(isNewMachine(machine("x", { first_seen: null }), R, NOW)).toBe(false);
  });
  it("measures from coverage.archives when the range starts before it, and for 全部", () => {
    for (const key of ["30d", "all"] as const) {
      const R = rangeDef(key, NOW, cover);
      expect(R.effectiveStart).toBe(cover);
      const edge = cover + 0.1 * (NOW - cover);
      expect(isNewMachine(machine("x", { first_seen: iso(edge + 60_000) }), R, NOW)).toBe(true);
      expect(isNewMachine(machine("x", { first_seen: iso(edge - 60_000) }), R, NOW)).toBe(false);
    }
  });
});

describe("KPI figures", () => {
  const cover = local(2026, 10, 10, 9);
  const films = [
    film(local(2026, 10, 27, 10), { restore_s: 2 * 3600, lead_s: 10 * 3600, restore_wh: [["gpu-a", 1000, 7200], ["gpu-b", 200, 900]], controller_wh: 100 }),
    film(local(2026, 10, 28, 10), { res: "1080p", restore_s: 4 * 3600, lead_s: 20 * 3600, restore_wh: [["gpu-a", 400, 3600]], controller_wh: 60 }),
    film(local(2026, 10, 29, 10), { worker: CTRL, res: "1080p", restore_s: 6 * 3600, restore_wh: [[CTRL, 300, 21600]], controller_wh: 40 }),
  ];
  const failures = [
    failure(local(2026, 10, 27, 1)),
    failure(local(2026, 10, 27, 2), { severity: "warn", type: "interrupted", machine: CTRL }),
    failure(local(2026, 10, 27, 3), { severity: "warn", type: "transient", outcome: "retrying" }),
    failure(local(2026, 10, 27, 4), { severity: "fail", type: "transient", exhausted: true, outcome: "needs_action", machine: CTRL }),
    failure(local(2026, 10, 27, 5), { severity: "fail", type: "translate_paused", exhausted: true, outcome: "needs_action", machine: CTRL }),
  ];
  const d = data({ films, failures, coverage: { archives: iso(cover), energy: null, cues: null, devices: null, outages: null } });
  const R = rangeDef("7d", NOW, cover);

  it("失败率 counts severity fail only, which includes exhausted transients (D12)", () => {
    const k = kpiFigures(compute(d, R, F, NOW));
    expect([k.fails, k.warns]).toEqual([3, 2]);
    expect(k.failRate).toBeCloseTo((3 / (3 + 3)) * 100, 6);
    expect(k.prevFailRate).toBeNull(); // nothing archived or failed in the previous week
  });

  it("films, durations and estimated energy (restore + 本机 post-processing)", () => {
    const k = kpiFigures(compute(d, R, F, NOW));
    expect([k.films, k.films4k, k.films1080, k.prevFilms]).toEqual([3, 1, 2, 0]);
    expect(k.restore).toEqual([2, 4, 6]);
    expect(k.lead).toEqual([10, 20, 30]);
    expect(k.energy).toBeCloseTo((1000 + 200 + 100 + 400 + 60 + 300 + 40) / 1000, 6);
    expect(k.ctrlRestored).toBe(true);
  });

  it("the 本机 filter: post-processing energy only, restore only of films 本机 restored, its failures", () => {
    const k = kpiFigures(compute(d, R, { ...F, machine: CTRL }, NOW));
    expect(k.energy).toBeCloseTo(0.2, 6);
    expect(k.restore).toEqual([6]);
    expect([k.fails, k.warns]).toEqual([2, 1]);
    const noCtrl = data({ ...d, films: films.slice(0, 2) });
    expect(kpiFigures(compute(noCtrl, R, { ...F, machine: CTRL }, NOW)).ctrlRestored).toBe(false);
  });
});

describe("delta", () => {
  const cover = local(2026, 10, 10, 9);
  const d = data();
  const week = compute(d, rangeDef("7d", NOW, cover), F, NOW);
  it("percent, percentage points and the flat states", () => {
    expect(delta(week, 12, 10, "up")).toEqual({ tone: "good", dir: "up", text: "+20%", vs: true });
    expect(delta(week, 8, 10, "up")).toMatchObject({ tone: "bad", text: "−20%" });
    expect(delta(week, 12, 10, "none").tone).toBe("flat");
    expect(delta(week, 3.5, 2, "down", true)).toMatchObject({ tone: "bad", text: "+1.5 个百分点" });
    expect(delta(week, 10.05, 10, "up").text).toBe("与上一周期持平");
    expect(delta(week, 5, 0, "up").text).toBe("上一周期无可比数据");
    expect(delta(week, null, 3, "up").text).toBe("上一周期无可比数据");
    expect(delta(compute(d, rangeDef("all", NOW, cover), F, NOW), 1, 1, "up").text).toBe("全部时段没有上一周期");
    expect(delta(compute(d, rangeDef("30d", NOW, cover), F, NOW), 1, 1, "up").text).toBe("上一周期没有记录");
    const late = data({ coverage: { ...d.coverage, archives: iso(local(2026, 10, 21)) } });
    const partial = compute(late, rangeDef("7d", NOW, local(2026, 10, 21)), F, NOW);
    expect(delta(partial, 1, 1, "up").text).toBe("上一周期记录不全");
  });
});

describe("machine select groups", () => {
  it("groups GPU machines by model, lists retired ones only with data in range, marks 新加入", () => {
    const d = data({
      machines: [
        machine(CTRL, { kind: "controller", name: "本机" }),
        machine("b", { name: "Birch" }), machine("a", { name: "Aurora", model: "RTX 5070 Ti 16GB" }),
        machine("n", { name: "Newbie", first_seen: iso(local(2026, 10, 30)) }),
        machine("r1", { name: "Gone", retired: true }), machine("r2", { name: "Old", retired: true }),
        machine("u", { name: "Unprobed", model: null }),
      ],
      films: [film(local(2026, 10, 27), { worker: "r1" })],
    });
    const R = rangeDef("7d", NOW, local(2026, 10, 10));
    expect(machineGroups(d, R, NOW)).toEqual([
      { label: "RTX 5060 Ti 16GB × 3", options: [{ id: "b", label: "Birch" }, { id: "r1", label: "Gone（已退休）" }, { id: "n", label: "Newbie（新加入）" }] },
      { label: "RTX 5070 Ti 16GB × 1", options: [{ id: "a", label: "Aurora" }] },
      { label: "未识别型号 × 1", options: [{ id: "u", label: "Unprobed" }] },
    ]);
  });
});

describe("device presence", () => {
  it("counts online or 停领 machines at a time; offline and unrecorded count as neither", () => {
    const span = (machine: string, state: "online" | "paused" | "offline", s: number, e: number, open = false) =>
      ({ machine, state, reason: "" as const, start: iso(s), end: iso(e), open });
    const spans = [
      span("a", "online", local(2026, 10, 30), local(2026, 10, 31, 14, 19), true),
      span("b", "paused", local(2026, 10, 30), local(2026, 10, 31)),
      span("c", "offline", local(2026, 10, 30), local(2026, 10, 31)),
    ];
    const ids = new Set(["a", "b", "c", "d"]);
    expect(presentAt(spans, ids, local(2026, 10, 30, 12))).toBe(2);
    expect(presentAt(spans, ids, local(2026, 10, 31, 12))).toBe(1);
    expect(presentAt(spans, ids, local(2026, 10, 31, 14, 19))).toBe(1);
    expect(presentAt(spans, ids, local(2026, 10, 29))).toBe(0);
  });
});

describe("energy readings (C3-X2): a film without energy evidence is not 0 kWh", () => {
  const cover = local(2026, 10, 10, 9);
  const R = rangeDef("7d", NOW, cover);
  const none = { restore_wh: [] as AnalyticsFilm["restore_wh"], controller_wh: null };
  const measured = [
    film(local(2026, 10, 27, 10), { restore_wh: [["gpu-a", 1000, 7200]], controller_wh: 100 }),
    film(local(2026, 10, 28, 10), { restore_wh: [], controller_wh: 60 }), // post-processing only: still a reading
  ];
  const prevWeek = [film(local(2026, 10, 20, 10), { restore_wh: [["gpu-a", 900, 7200]], controller_wh: 100 })];

  it("sums and averages measured films only and counts the missing ones", () => {
    const fs = [...measured, film(local(2026, 10, 29, 10), none), film(local(2026, 10, 30, 10), none)];
    const all = energyReading(fs, false), ctrl = energyReading(fs, true);
    expect([all.measured, all.missing, ctrl.measured, ctrl.missing]).toEqual([2, 2, 2, 2]);
    expect(all.kwh).toBeCloseTo((1000 + 100 + 60) / 1000, 9);
    expect(ctrl.kwh).toBeCloseTo(0.16, 9);
    expect(energyReading([film(local(2026, 10, 29, 10), none)], false)).toEqual({ kwh: null, measured: 0, missing: 1, partial: 0 });
    expect(energyReading([], false)).toEqual({ kwh: null, measured: 0, missing: 0, partial: 0 });
  });

  it("the KPI averages over measured films and compares only fully measured periods", () => {
    const d = (films: AnalyticsFilm[]) => data({ films, coverage: { archives: iso(cover), energy: null, cues: null, devices: null, outages: null } });
    const partial = kpiFigures(compute(d([...measured, film(local(2026, 10, 29, 10), none), ...prevWeek]), R, F, NOW));
    expect(partial.energy).toBeCloseTo(1.16, 6);
    expect(partial.energyPerFilm).toBeCloseTo(0.58, 6);
    expect([partial.energyMeasured, partial.energyMissing]).toEqual([2, 1]);
    expect(partial.prevEnergy).toBeNull(); // current period is partly unmeasured: no delta
    const full = kpiFigures(compute(d([...measured, ...prevWeek]), R, F, NOW));
    expect(full.prevEnergy).toBeCloseTo(1.0, 6);
    const prevGap = kpiFigures(compute(d([...measured, ...prevWeek, film(local(2026, 10, 21, 10), none)]), R, F, NOW));
    expect(prevGap.prevEnergy).toBeNull();
  });
});

describe("recorded presence (C3-X3): no record is null, never 0 online", () => {
  const span = (machine: string, state: "online" | "paused" | "offline", s: number, e: number, open = false) =>
    ({ machine, state, reason: "" as const, start: iso(s), end: iso(e), open });
  const spans = [
    span("a", "online", local(2026, 10, 30), local(2026, 10, 30, 15)),
    span("a", "online", local(2026, 10, 30, 16, 30), local(2026, 10, 31, 13, 59), true),
    span("b", "offline", local(2026, 10, 30), local(2026, 10, 30, 15)),
  ];
  const ids = new Set(["a", "b"]);
  it("counts within recorded time, null in a gap, before the first span and after the last tick", () => {
    expect(recordedPresence(spans, ids, local(2026, 10, 30, 12))).toBe(1);
    expect(recordedPresence(spans, ids, local(2026, 10, 30, 15, 30))).toBeNull();
    expect(recordedPresence(spans, ids, local(2026, 10, 29, 12))).toBeNull();
    expect(recordedPresence(spans, ids, local(2026, 10, 31, 13, 59))).toBe(1);
    expect(recordedPresence(spans, ids, local(2026, 10, 31, 14, 10))).toBeNull();
    expect(recordedPresence([span("b", "offline", local(2026, 10, 30), local(2026, 10, 31))], ids, local(2026, 10, 30, 12))).toBe(0);
  });
});

describe("partial energy readings (C3-X5)", () => {
  const cover = local(2026, 10, 10, 9);
  const R = rangeDef("7d", NOW, cover);
  const d = (films: AnalyticsFilm[]) => data({ films, coverage: { archives: iso(cover), energy: null, cues: null, devices: null, outages: null } });
  const full = film(local(2026, 10, 27, 10), { restore_wh: [["gpu-a", 1000, 7200]], controller_wh: 100 });
  const partial = film(local(2026, 10, 28, 10), { restore_wh: [["gpu-a", 100, 900]], controller_wh: null, energy_partial: true });
  const prevWeek = [film(local(2026, 10, 20, 10), { restore_wh: [["gpu-a", 900, 7200]], controller_wh: 100 })];

  it("counts partial films apart and still sums their readings", () => {
    const r = energyReading([full, partial], false);
    expect([r.measured, r.missing, r.partial]).toEqual([2, 0, 1]);
    expect(r.kwh).toBeCloseTo(1.2, 9);
    const ctrl = energyReading([full, partial], true); // 本机: partial has no controller reading
    expect([ctrl.measured, ctrl.missing, ctrl.partial]).toEqual([1, 1, 0]);
  });

  it("a partial period never compares (no fake reduction), in either period", () => {
    const cur = kpiFigures(compute(d([full, partial, ...prevWeek]), R, F, NOW));
    expect([cur.energyMeasured, cur.energyMissing, cur.energyPartial]).toEqual([2, 0, 1]);
    expect(cur.prevEnergy).toBeNull();
    const prevPartial = kpiFigures(compute(d([full, { ...prevWeek[0], energy_partial: true }]), R, F, NOW));
    expect(prevPartial.prevEnergy).toBeNull();
    expect(kpiFigures(compute(d([full, ...prevWeek]), R, F, NOW)).prevEnergy).toBeCloseTo(1.0, 9);
  });
});
