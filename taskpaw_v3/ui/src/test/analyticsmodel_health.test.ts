// 数据分析 model for 用电, 设备在线, 异常与报警 and 片源与片库 (EPIC 2026-10-31 §8.4 items 6–9, D9, D14, D16;
// C5 #392 AC1–AC4). Every expected time is built with local-time constructors (buckets are local, §7.1).
import { describe, expect, it } from "vitest";
import type { AnalyticsFailure, AnalyticsFilm, AnalyticsMachine, AnalyticsSpan, WorkflowAnalytics } from "../api";
import {
  alertFigures, compute, controllerFigures, durationFigures, energyFigures, failureBursts, histogram, inboxFigures, rangeDef, sourceFigures, uptimeLanes, type Filters,
} from "../components/media/analytics/analyticsModel";

const MIN = 60_000, H = 60 * MIN, DAY = 24 * H;
const local = (y: number, mo: number, d: number, h = 0, mi = 0) => new Date(y, mo - 1, d, h, mi).getTime();
const iso = (t: number) => new Date(t).toISOString();
const NOW = local(2026, 10, 31, 14, 20);
const COVER = local(2026, 10, 10, 9);
const CTRL = "ctrl-1";
let seq = 0;

const film = (at: number, o: Partial<AnalyticsFilm> = {}): AnalyticsFilm => ({
  code: `ZZH-${(seq += 1)}`, archived_at: iso(at), created_at: iso(at - 30 * H), res: "4K",
  duration_s: 7200, input_bytes: 1e10, worker: "gpu-a", queue_s: 3600, restore_s: 4 * 3600, asr_s: 600, translate_s: 60,
  translated: true, cues: null, subtitles: { generated_ja: 1 }, scrape_s: 120, archive_s: 60, lead_s: 30 * 3600,
  restore_wh: [["gpu-a", 800, 14400]], controller_wh: 50, energy_partial: false, target: "performer", actor: "Actor Test",
  second: false, marks: [], ...o,
});
const failure = (at: number, o: Partial<AnalyticsFailure> = {}): AnalyticsFailure => ({
  at: iso(at), code: "ZZH-0", stage: "restore", machine: "gpu-a", severity: "fail", type: "ltx_failed", exhausted: false,
  note: "Jasna LTX 失败（退出码 1）", outcome: "retried_ok", ...o,
});
const machine = (id: string, o: Partial<AnalyticsMachine> = {}): AnalyticsMachine => ({
  id, name: id, kind: "gpu", model: "RTX 5060 Ti 16GB", first_seen: iso(local(2026, 9, 1)), retired: false,
  state_now: "online", state_reason: "", state_since: null, ...o,
});
const span = (m: string, state: AnalyticsSpan["state"], s: number, e: number, o: Partial<AnalyticsSpan> = {}): AnalyticsSpan =>
  ({ machine: m, state, reason: "", start: iso(s), end: iso(e), open: false, ...o });
const data = (o: Partial<WorkflowAnalytics> = {}): WorkflowAnalytics => ({
  server_time: iso(NOW), since: null,
  coverage: { archives: iso(COVER), energy: null, cues: null, devices: null, outages: null },
  machines: [machine(CTRL, { name: "本机", kind: "controller", model: "RTX 5060 8GB" }), machine("gpu-a"), machine("gpu-b")],
  films: [], failures: [], devices: { available: true, reason: "", spans: [] }, outages: [], inbox: null, ...o,
});
const ALL: Filters = { machine: "all", res: "all", compare: false };
const analyse = (d: WorkflowAnalytics, f: Partial<Filters> = {}, key: Parameters<typeof rangeDef>[0] = "7d") =>
  compute(d, rangeDef(key, NOW, d.coverage.archives ? Date.parse(d.coverage.archives) : null), { ...ALL, ...f }, NOW);

describe("energyFigures (用电, AC1)", () => {
  const day = (d: number, h = 10) => local(2026, 10, d, h);
  const films = () => [
    film(day(30), { code: "ZZH-A", restore_wh: [["gpu-a", 800, 14400]], controller_wh: 50 }),
    // restored on gpu-b after a failed attempt on gpu-a: that attempt's energy is gpu-a's (§7.1)
    film(day(30, 12), { code: "ZZH-B", worker: "gpu-b", res: "1080p", restore_wh: [["gpu-b", 600, 10800], ["gpu-a", 100, 1800]], controller_wh: 40 }),
    film(day(29), { code: "ZZH-C", restore_wh: [], controller_wh: null }), // no reading at all
    film(day(28), { code: "ZZH-D", worker: "gpu-b", restore_wh: [["gpu-b", 300, 5400]], controller_wh: 30, energy_partial: true }),
  ];

  it("attributes restore_wh per worker (failed attempts on other machines included) and 本机后处理 from controller_wh", () => {
    const e = energyFigures(data({ films: films() }), analyse(data({ films: films() })), "all");
    const by = Object.fromEntries(e.byMachine.map((g) => [g.label, g]));
    expect(by["gpu-a"].kwh).toBeCloseTo(0.9); // 800 + 100 Wh
    expect(by["gpu-b"].kwh).toBeCloseTo(0.9); // 600 + 300 Wh
    expect(by["gpu-b"].partial).toBe(true); // ZZH-D lacked energy on some attempt
    expect(by["本机（后处理）"].kwh).toBeCloseTo(0.12);
    expect(by["本机（后处理）"].partial).toBe(true); // ZZH-C has no controller reading
    expect(e.byMachine[e.byMachine.length - 1].label).toBe("本机（后处理）");
  });

  it("the per-machine breakdown lists every restore_wh contributor, so groups + 本机后处理 sum to the total (C5-X2)", () => {
    // 本机 only contributed a failed restore attempt: it is not a machine-table row, but its energy is in the total
    const fs = [film(day(30), { restore_wh: [["gpu-a", 800, 14400], [CTRL, 200, 3600]], controller_wh: 50 }), film(day(29), { worker: "gpu-b", restore_wh: [["gpu-b", 400, 7200]], controller_wh: 20 })];
    const d = data({ films: fs });
    for (const machineFilter of ["all", "gpu-a"]) {
      const e = energyFigures(d, analyse(d, { machine: machineFilter }), "all");
      const groups = e.byMachine.reduce((a, g) => a + (g.kwh ?? 0), 0);
      expect(groups).toBeCloseTo(e.reading.kwh!);
    }
    const all = energyFigures(d, analyse(d), "all");
    expect(all.byMachine.map((g) => g.label)).toEqual(["gpu-a", "gpu-b", "本机（修复）", "本机（后处理）"]);
    expect(all.byMachine.find((g) => g.label === "本机（修复）")!.kwh).toBeCloseTo(0.2);
  });

  it("sums measured films only: a film without a reading is missing, never 0 kWh; partial ones are flagged", () => {
    const d = data({ films: films() });
    const e = energyFigures(d, analyse(d), "all");
    expect(e.reading.measured).toBe(3);
    expect(e.reading.missing).toBe(1);
    expect(e.reading.partial).toBe(1);
    expect(e.reading.kwh).toBeCloseTo(0.85 + 0.74 + 0.33);
    expect(e.perFilm).toBeCloseTo((0.85 + 0.74 + 0.33) / 3);
    const i29 = e.buckets.findIndex((b) => b.missing === 1);
    // archived films but no reading at all: unknown (null), not 0 kWh; an empty bucket is a true 0 (C5-X3)
    expect(e.buckets[i29]).toEqual({ kwh: null, films: 1, missing: 1, partial: 0 });
    expect(e.buckets.find((b) => b.films === 0)).toEqual({ kwh: 0, films: 0, missing: 0, partial: 0 });
    expect(e.buckets.find((b) => b.partial === 1)!.kwh).toBeCloseTo(0.33);
  });

  it("most / top 5 rank measured films (partial ones flagged); least only among complete readings", () => {
    const d = data({ films: [...films(), film(day(27), { code: "ZZH-E", restore_wh: [["gpu-a", 100, 1800]], controller_wh: 10, energy_partial: true })] });
    const e = energyFigures(d, analyse(d), "all");
    expect(e.most?.code).toBe("ZZH-A");
    expect(e.least?.code).toBe("ZZH-B"); // ZZH-E (0.11 kWh) is partial: a lower bound cannot be the least
    expect(e.top.map((f) => f.code)).toEqual(["ZZH-A", "ZZH-B", "ZZH-D", "ZZH-E"]);
  });

  it("per resolution: average over measured films of each shown resolution", () => {
    const d = data({ films: films() });
    const e = energyFigures(d, analyse(d), "all");
    const k4 = e.byRes.find((r) => r.series.key === "4K")!, k1 = e.byRes.find((r) => r.series.key === "1080p")!;
    expect(k4.films).toBe(3); // ZZH-A, ZZH-C (no reading), ZZH-D
    expect(k4.measured).toBe(2);
    expect(k4.avg).toBeCloseTo((0.85 + 0.33) / 2);
    expect(k1.avg).toBeCloseTo(0.74);
    expect(energyFigures(d, analyse(d, { res: "4K" }), "4K").byRes.map((r) => r.series.key)).toEqual(["4K"]);
  });

  it("the previous-period line only when both periods are fully measured (C3's honesty rule)", () => {
    const prevFilm = film(local(2026, 10, 20, 10), { code: "ZZH-P" });
    const complete = data({ films: [film(day(30)), prevFilm] });
    const e = energyFigures(complete, analyse(complete, { compare: true }), "all");
    expect(e.prev).not.toBeNull();
    expect(e.prev!.reduce((a, b) => a + b, 0)).toBeCloseTo(0.85);
    const gap = data({ films: [film(day(30)), film(day(29), { restore_wh: [], controller_wh: null }), prevFilm] });
    expect(energyFigures(gap, analyse(gap, { compare: true }), "all").prev).toBeNull();
    const partialPrev = data({ films: [film(day(30)), { ...prevFilm, energy_partial: true }] });
    expect(energyFigures(partialPrev, analyse(partialPrev, { compare: true }), "all").prev).toBeNull();
  });

  it("the 本机 filter: only 本机's post-processing, per film and per group", () => {
    const d = data({ films: films() });
    const e = energyFigures(d, analyse(d, { machine: CTRL }), "all");
    expect(e.byMachine.map((g) => g.label)).toEqual(["本机（后处理）"]);
    expect(e.reading.kwh).toBeCloseTo(0.12);
    expect(e.most?.code).toBe("ZZH-A");
  });
});

describe("uptimeLanes (设备在线, AC2)", () => {
  const T0 = local(2026, 10, 28, 0);
  const spans = [
    span("gpu-a", "online", T0, T0 + 10 * H),
    // 2 h gap: unrecorded (主控未运行)
    span("gpu-a", "paused", T0 + 12 * H, T0 + 14 * H, { reason: "worker" }),
    span("gpu-a", "offline", T0 + 14 * H, T0 + 15 * H),
    span("gpu-a", "online", T0 + 15 * H, NOW - 30_000, { open: true }),
  ];

  it("splits a lane into recorded states and 无记录 gaps; an open span ends at its last recorded tick (C4-X3)", () => {
    const d = data({ coverage: { ...data().coverage, devices: iso(T0) }, devices: { available: true, reason: "", spans } });
    const A = analyse(d);
    const lane = uptimeLanes(d, A).find((l) => l.m.id === "gpu-a")!;
    expect(lane.from).toBe(A.R.start);
    expect(lane.to).toBe(NOW);
    expect(lane.segs.map((s) => s.state)).toEqual(["none", "online", "none", "paused", "offline", "online", "none"]);
    expect(lane.segs[0]).toMatchObject({ s: A.R.start, e: T0 });
    expect(lane.segs[3].reason).toBe("worker");
    expect(lane.segs[5]).toMatchObject({ e: NOW - 30_000, open: true });
    expect(lane.segs[6]).toMatchObject({ s: NOW - 30_000, e: NOW }); // after the last tick: unrecorded, never online
    expect(lane.offline).toBe(H);
    expect(lane.paused).toBe(2 * H);
    expect(lane.none).toBe(T0 - A.R.start + 2 * H + 30_000);
    const recorded = NOW - 30_000 - T0 - 2 * H;
    expect(lane.online).toBe(recorded - 3 * H);
    // 在线率 = (online + 停领) ÷ recorded time; unrecorded time is listed apart, never counted as offline
    expect(lane.rate).toBeCloseTo(((recorded - H) / recorded) * 100);
  });

  it("a 新加入 machine's lane starts at first_seen (D14); others start at the range start", () => {
    const joined = local(2026, 10, 30, 9);
    const d = data({ machines: [...data().machines, machine("gpu-new", { first_seen: iso(joined) })] });
    const A = analyse(d);
    const lanes = uptimeLanes(d, A);
    expect(lanes.find((l) => l.m.id === "gpu-new")).toMatchObject({ isNew: true, from: joined });
    expect(lanes.find((l) => l.m.id === "gpu-b")).toMatchObject({ isNew: false, from: A.R.start });
  });

  it("without any span every lane is 无记录 and has no 在线率", () => {
    const d = data();
    const lane = uptimeLanes(d, analyse(d))[0];
    expect(lane.segs.map((s) => s.state)).toEqual(["none"]);
    expect(lane.rate).toBeNull();
  });

  it("lanes per filter: the fleet plus 本机 (last); one GPU machine alone; 本机 alone; retired only with a span in range", () => {
    const d = data({
      machines: [...data().machines, machine("gpu-old", { retired: true }), machine("gpu-gone", { retired: true })],
      devices: { available: true, reason: "", spans: [span("gpu-old", "online", T0, T0 + H)] },
    });
    expect(uptimeLanes(d, analyse(d)).map((l) => l.m.id)).toEqual(["gpu-a", "gpu-b", "gpu-old", CTRL]);
    expect(uptimeLanes(d, analyse(d, { machine: "gpu-b" })).map((l) => l.m.id)).toEqual(["gpu-b"]);
    expect(uptimeLanes(d, analyse(d, { machine: CTRL })).map((l) => l.m.id)).toEqual([CTRL]);
  });
});

describe("failureBursts (D16)", () => {
  const fault = (at: number, o: Partial<AnalyticsFailure> = {}) =>
    failure(at, { type: "machine_fault", note: "[WinError 4551]", outcome: "reassigned", ...o });
  const T = NOW - 3 * H;

  it("needs ≥ 3 machine_fault failures on one machine within 1 h", () => {
    expect(failureBursts([fault(T), fault(T + 20 * MIN)], NOW)).toEqual([]);
    expect(failureBursts([fault(T), fault(T + 40 * MIN), fault(T + 80 * MIN)], NOW)).toEqual([]); // spread over > 1 h
    expect(failureBursts([fault(T), fault(T + 20 * MIN), fault(T + 30 * MIN, { machine: "gpu-b" }), fault(T + 40 * MIN, { machine: "gpu-b" })], NOW)).toEqual([]);
    expect(failureBursts([fault(T), fault(T + 20 * MIN, { type: "ltx_failed" }), fault(T + 40 * MIN)], NOW)).toEqual([]);
    const b = failureBursts([fault(T), fault(T + 30 * MIN), fault(T + 60 * MIN), fault(T + 5 * H)], NOW);
    expect(b).toHaveLength(1);
    expect(b[0]).toMatchObject({ machine: "gpu-a", start: T, end: T + 60 * MIN, banner: true });
    expect(b[0].rows).toHaveLength(3);
  });

  it("overlapping windows merge into one burst", () => {
    const b = failureBursts([0, 20, 40, 70, 90].map((m) => fault(T + m * MIN)), NOW);
    expect(b).toHaveLength(1);
    expect(b[0].rows).toHaveLength(5);
  });

  it("a burst is a union of qualifying windows that share failures, not of faults merely ≤ 1 h apart (C5-X9)", () => {
    // 0/20/40 and 100/120/140: two windows of 3, no 3-in-1 h window links them (40 → 100 is only 2 faults)
    const two = failureBursts([0, 20, 40, 100, 120, 140].map((m) => fault(T + m * MIN)), NOW);
    expect(two.map((b) => b.rows.length)).toEqual([3, 3]);
    expect(two.map((b) => [b.start, b.end])).toEqual([[T + 100 * MIN, T + 140 * MIN], [T, T + 40 * MIN]]); // newest first
    // 0/20/40/70: windows {0,20,40} and {20,40,70} share faults → one burst of 4
    const chain = failureBursts([0, 20, 40, 70].map((m) => fault(T + m * MIN)), NOW);
    expect(chain.map((b) => b.rows.length)).toEqual([4]);
    // an old recovered window is not pulled into a recent banner through a 2-fault bridge
    const old = NOW - 3 * DAY;
    const split = failureBursts([0, 20, 40, 100, 120, 140].map((m) => fault(old + m * MIN, m >= 100 ? { outcome: "needs_action" } : {})), NOW);
    expect(split.map((b) => b.banner)).toEqual([true, false]);
  });

  it("the banner only for a burst that ended in the last 24 h or has a needs_action row", () => {
    const old = NOW - 3 * DAY;
    const recovered = failureBursts([fault(old), fault(old + 10 * MIN), fault(old + 20 * MIN)], NOW);
    expect(recovered).toHaveLength(1);
    expect(recovered[0].banner).toBe(false);
    const open = failureBursts([fault(old), fault(old + 10 * MIN, { outcome: "needs_action" }), fault(old + 20 * MIN)], NOW);
    expect(open[0].banner).toBe(true);
    const edge = NOW - DAY + MIN;
    expect(failureBursts([fault(edge - 20 * MIN), fault(edge - 10 * MIN), fault(edge)], NOW)[0].banner).toBe(true);
  });
});

describe("alertFigures (异常与报警, AC3)", () => {
  const T = local(2026, 10, 30, 10);
  const fails = [
    failure(T, { type: "machine_fault", note: "[WinError 4551]", outcome: "reassigned" }),
    failure(T + 10 * MIN, { type: "machine_fault", note: "[WinError 4551]", outcome: "reassigned" }),
    failure(T + 20 * MIN, { type: "machine_fault", note: "[WinError 4551]", outcome: "needs_action" }),
    failure(T + 2 * H, { machine: CTRL, stage: "return", type: "transient", severity: "fail", exhausted: true, note: "[WinError 5]", outcome: "needs_action" }),
    failure(T + 3 * H, { machine: CTRL, stage: "translate", type: "translate_paused", severity: "warn", note: "翻译暂停：无响应", outcome: "retrying" }),
    failure(T + 4 * H, { machine: "gpu-b", type: "interrupted", severity: "warn", note: "上一次运行被中断", outcome: "retried_ok" }),
  ];
  const devices = {
    available: true, reason: "", spans: [
      span("gpu-b", "offline", T - 5 * H, T - 4 * H), span("gpu-b", "online", T - 4 * H, T - 3 * H), // seen back online: 已恢复
      span("gpu-b", "offline", T - H, T - 30 * MIN), // closed by a restart / stall, recovery never observed (§6.2)
      span("gpu-a", "offline", T + 5 * H, NOW - 30_000, { open: true }),
    ],
  };
  const outages = [{ start: iso(T + 6 * H), end: iso(T + 7 * H), paused: false }];
  const d = data({ failures: fails, devices, outages, coverage: { ...data().coverage, devices: iso(T - DAY), outages: iso(T - 2 * DAY) } });

  it("types by §7.3 with fail / warn severity and the 「（重试已用尽）」 label; offline spans and outages are warnings", () => {
    const a = alertFigures(d, analyse(d));
    const t = Object.fromEntries(a.types.map((x) => [x.label, x]));
    expect(t["应用程序控制拦截"]).toMatchObject({ n: 3, severity: "fail" });
    expect(t["临时失败（重试已用尽）"]).toMatchObject({ n: 1, severity: "fail" });
    expect(t["翻译暂停"]).toMatchObject({ n: 1, severity: "warn" });
    expect(t["运行中断"]).toMatchObject({ n: 1, severity: "warn" });
    expect(t["设备离线"]).toMatchObject({ n: 3, severity: "warn" });
    expect(t["翻译服务中断"]).toMatchObject({ n: 1, severity: "warn" });
    expect(a.types.map((x) => x.severity)).toEqual([...a.types.filter((x) => x.severity === "fail"), ...a.types.filter((x) => x.severity === "warn")].map((x) => x.severity));
    expect(a.types.some((x) => /CPU/.test(x.label))).toBe(false); // D6
    expect(a.fails).toBe(4);
    expect(a.warns).toBe(2);
  });

  it("merges failures, offline spans and outages newest first with §7.2 outcome labels; burst rows flagged", () => {
    const a = alertFigures(d, analyse(d));
    expect(a.events.map((e) => e.at)).toEqual([...a.events.map((e) => e.at)].sort((x, y) => y - x));
    expect(a.events[0]).toMatchObject({ kind: "outage", label: "翻译服务中断", outcome: "已自动恢复" });
    expect(a.events.find((e) => e.kind === "offline" && e.machine === "gpu-a")).toMatchObject({ outcome: "仍离线" });
    const gpuB = a.events.filter((e) => e.kind === "offline" && e.machine === "gpu-b").sort((x, y) => x.at - y.at);
    expect(gpuB.map((e) => e.outcome)).toEqual(["已恢复", "记录中断"]); // C5-X1: a closed span alone is no recovery
    const labels = a.events.filter((e) => e.kind === "failure").map((e) => e.outcome);
    expect(new Set(labels)).toEqual(new Set(["已自动改派", "需要处理", "重试中", "已重试成功"]));
    expect(a.events.filter((e) => e.burst)).toHaveLength(3);
    expect(a.bursts).toHaveLength(1);
  });

  it("recovery needs the chronologically next span: one starting a tick before a zero-length offline span is not it (C5-X5)", () => {
    const at = local(2026, 10, 30, 12);
    const d2 = data({
      devices: { available: true, reason: "", spans: [span("gpu-b", "online", at - 20_000, at - 10_000), span("gpu-b", "offline", at, at)] },
      coverage: { ...data().coverage, devices: iso(at - DAY) },
    });
    expect(alertFigures(d2, analyse(d2)).events.find((e) => e.kind === "offline")).toMatchObject({ outcome: "记录中断" });
    const d3 = data({
      devices: { available: true, reason: "", spans: [span("gpu-b", "offline", at, at), span("gpu-b", "online", at + 20_000, at + H)] },
      coverage: { ...data().coverage, devices: iso(at - DAY) },
    });
    expect(alertFigures(d3, analyse(d3)).events.find((e) => e.kind === "offline")).toMatchObject({ outcome: "已恢复" });
  });

  it("a year of device history: outcomes match the naive next-span rule (C5-X6, OD2)", () => {
    // 5 workers × 365 days × 8 spans, one closed offline span a day: back online at once on even days, after a gap on odd
    const start = local(2025, 10, 31), spans: AnalyticsSpan[] = [];
    for (let w = 0; w < 5; w++) {
      for (let dd = 0; dd < 365; dd++) {
        const b = start + dd * DAY, id = `gpu-${w}`;
        spans.push(span(id, "online", b, b + 3 * H), span(id, "paused", b + 3 * H, b + 4 * H), span(id, "online", b + 4 * H, b + 8 * H),
          span(id, "offline", b + 8 * H, b + 9 * H), span(id, "online", b + (dd % 2 ? 10 : 9) * H, b + 15 * H),
          span(id, "online", b + 15 * H, b + 20 * H), span(id, "paused", b + 20 * H, b + 22 * H), span(id, "online", b + 22 * H, b + 24 * H));
      }
    }
    const machines = [machine(CTRL, { name: "本机", kind: "controller" }), ...[0, 1, 2, 3, 4].map((w) => machine(`gpu-${w}`))];
    const d = data({ machines, devices: { available: true, reason: "", spans }, coverage: { ...data().coverage, archives: iso(start), devices: iso(start) } });
    const A = compute(d, rangeDef("all", NOW, start), ALL, NOW);
    const t0 = performance.now();
    const events = alertFigures(d, A).events.filter((e) => e.kind === "offline");
    const ms = performance.now() - t0;
    // the reference: a plain linear search for the earliest span of the machine starting at or after the end
    const own = new Map([0, 1, 2, 3, 4].map((w) => [`gpu-${w}`, spans.filter((n) => n.machine === `gpu-${w}`).map((n) => ({ n, t: Date.parse(n.start) }))]));
    const naive = (sp: AnalyticsSpan) => {
      const end = Date.parse(sp.end);
      let next: { n: AnalyticsSpan; t: number } | null = null;
      for (const c of own.get(sp.machine)!) if (c.n !== sp && c.t >= end && (next === null || c.t < next.t)) next = c;
      return next && next.t - end <= 30_000 && next.n.state !== "offline" ? "已恢复" : "记录中断";
    };
    const offline = spans.filter((sp) => sp.state === "offline");
    expect(events).toHaveLength(1825);
    const key = (m: string, at: number, o: string) => `${m}|${at}|${o}`;
    expect(events.map((e) => key(e.machine, e.at, e.outcome)).sort())
      .toEqual(offline.map((sp) => key(sp.machine, Date.parse(sp.start), naive(sp))).sort());
    expect(events.filter((e) => e.outcome === "已恢复")).toHaveLength(5 * 183);
    // loose: about 13 ms alone with the per-machine index, about 485 ms with the earlier per-span rescan
    expect(ms).toBeLessThan(200);
  });

  it("failures per machine count severity fail only; fleet machines without failures show 0", () => {
    const a = alertFigures(d, analyse(d));
    expect(Object.fromEntries(a.perMachine.map((m) => [m.label, m.n]))).toEqual({ "gpu-a": 3, "gpu-b": 0, "本机": 1 });
  });

  it("a GPU filter drops outages and other machines; the 本机 filter keeps outages", () => {
    const g = alertFigures(d, analyse(d, { machine: "gpu-b" }));
    expect(new Set(g.events.map((e) => e.machine))).toEqual(new Set(["gpu-b"]));
    expect(g.events.some((e) => e.kind === "outage")).toBe(false);
    const c = alertFigures(d, analyse(d, { machine: CTRL }));
    expect(c.events.some((e) => e.kind === "outage")).toBe(true);
    expect(c.perMachine.map((m) => m.label)).toEqual(["本机"]);
  });
});

describe("translated is three-valued (§7.1 null rule, driver addendum)", () => {
  it("null is unknown: neither counted as translated nor as 无需翻译, and its time stays out of 翻译", () => {
    const T = local(2026, 10, 30, 10);
    const d = data({
      films: [
        film(T, { translated: true, translate_s: 60 }),
        film(T + MIN, { translated: false, translate_s: null, subtitles: { reused_zh: 1 } }),
        film(T + 2 * MIN, { translated: null, translate_s: 600, subtitles: {} }),
      ],
    });
    const A = analyse(d);
    const c = controllerFigures(d, A);
    expect(c.translated).toBe(1);
    expect(c.translate.n).toBe(1);
    expect(c.translate.avg).toBeCloseTo(1); // minutes: the unknown film's 10 min is not mixed in
    expect(c.reusedZh).toBe(1); // 已有中文字幕 comes from the ASR decision, never from translated === false/null
    expect(durationFigures(A).minutes.find((r) => r.key === "translate")!.st!.n).toBe(1);
  });
});

describe("片源与片库 (AC4, D9)", () => {
  it("histogram: closed bins plus open-ended edges, films without a value counted apart", () => {
    const h = histogram([1, 4.9, 5, 41, null, 60], [5, 10, 40], (v) => String(v));
    expect(h.labels).toEqual(["0–5", "5–10", "10–40", "40+"]);
    expect(h.counts).toEqual([2, 1, 0, 2]);
    expect(h.missing).toBe(1);
    const o = histogram([1.2, 1.5, 3.6], [1.5, 2], (v) => v.toFixed(1), true);
    expect(o.labels).toEqual(["<1.5", "1.5–2.0", "2.0+"]);
    expect(o.counts).toEqual([1, 1, 1]);
  });

  it("size / length histograms, top 8 actors and marks share from the mark codes", () => {
    const T = local(2026, 10, 30, 10);
    const actors = ["Actor A", "Actor A", "Actor A", "Actor B", "Actor B", "Actor C", "Actor D", "Actor E", "Actor F", "Actor G", "Actor H", "Actor I"];
    const films = actors.map((a, i) => film(T + i * MIN, { actor: a, input_bytes: (i + 1) * 4e9, duration_s: 5400 + i * 900, marks: i < 3 ? ["chinese_subtitles", "4k", "restored"] : ["uncensored"] }));
    films.push(film(T + 20 * MIN, { actor: null, target: "category", input_bytes: null, duration_s: null }));
    const d = data({ films });
    const s = sourceFigures(analyse(d));
    expect(s.size.counts.reduce((a, b) => a + b, 0)).toBe(12);
    expect(s.size.missing).toBe(1);
    expect(s.size.labels[s.size.labels.length - 1]).toBe("40+");
    expect(s.duration.missing).toBe(1);
    expect(s.actors).toHaveLength(8);
    expect(s.actors.slice(0, 3)).toEqual([["Actor A", 3], ["Actor B", 2], ["Actor C", 1]]);
    expect(s.marks.map((m) => [m.label, m.n])).toEqual([["中文字幕", 3], ["无码", 9], ["4K", 3], ["修复", 3]]);
    expect(s.total).toBe(13);
  });

  it("INBOX: 其他 = used − pending (≥ 0), 现在还可放入 = free ÷ 7-day average size; no projection field", () => {
    const x = inboxFigures({ total_bytes: 1e12, free_bytes: 4.1e11, used_bytes: 5.9e11, pending_bytes: 3.2e11, pending_films: 23, freed_bytes_per_day_7d: 1.6e11, avg_input_bytes_7d: 1.3e10 })!;
    expect(x).toEqual({ total: 1e12, free: 4.1e11, used: 5.9e11, pending: 3.2e11, other: 2.7e11, pendingFilms: 23, freedPerDay: 1.6e11, avgInput: 1.3e10, canNow: 31 });
    expect(inboxFigures({ total_bytes: 1e12, free_bytes: 1e11, used_bytes: 9e11, pending_bytes: 9.5e11, pending_films: 1, freed_bytes_per_day_7d: 0, avg_input_bytes_7d: 0 })).toMatchObject({ other: 0, canNow: null });
    expect(inboxFigures(null)).toBeNull();
  });
});
