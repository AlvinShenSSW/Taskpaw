// 数据分析 model for 各 GPU 机器, 本机（主控）处理 and 耗时分布 (EPIC 2026-10-31 §8.4 items 3–5; C4 #391
// AC1–AC4). Every expected time is built with local-time constructors (buckets are local, §7.1).
import { describe, expect, it } from "vitest";
import type { AnalyticsFailure, AnalyticsFilm, AnalyticsMachine, AnalyticsSpan, WorkflowAnalytics } from "../api";
import {
  compute, controllerFigures, devicesCoverRange, durationFigures, machineDays, machineRows, modelAverage, modelGroups, modelThroughput,
  outageFigures, rangeDef, sortMachineRows, type Filters,
} from "../components/media/analytics/analyticsModel";

const H = 3_600_000, DAY = 24 * H;
const local = (y: number, mo: number, d: number, h = 0, mi = 0) => new Date(y, mo - 1, d, h, mi).getTime();
const iso = (t: number) => new Date(t).toISOString();
const NOW = local(2026, 10, 31, 14, 20);
const COVER = local(2026, 10, 10, 9);
const CTRL = "ctrl-1";
let seq = 0;

const film = (at: number, o: Partial<AnalyticsFilm> = {}): AnalyticsFilm => ({
  code: `ZZM-${(seq += 1)}`, archived_at: iso(at), created_at: iso(at - 30 * H), res: "4K",
  duration_s: 7200, input_bytes: 1e10, worker: "gpu-a", queue_s: 3600, restore_s: 4 * 3600, asr_s: 600, translate_s: 60,
  translated: true, cues: null, subtitles: { generated_ja: 1 }, scrape_s: 120, archive_s: 60, lead_s: 30 * 3600,
  restore_wh: [["gpu-a", 800, 14400]], controller_wh: 50, energy_partial: false, target: "performer", actor: "Actor Test",
  second: false, marks: [], ...o,
});
const failure = (at: number, o: Partial<AnalyticsFailure> = {}): AnalyticsFailure => ({
  at: iso(at), code: "ZZM-0", stage: "restore", machine: "gpu-a", severity: "fail", type: "ltx_failed", exhausted: false,
  note: "Jasna LTX 失败（退出码 1）", outcome: "retried_ok", ...o,
});
const machine = (id: string, o: Partial<AnalyticsMachine> = {}): AnalyticsMachine => ({
  id, name: id, kind: "gpu", model: "RTX 5060 Ti 16GB", first_seen: iso(local(2026, 9, 1)), retired: false,
  state_now: "online", state_reason: "", state_since: null, ...o,
});
const span = (m: string, state: AnalyticsSpan["state"], s: number, e: number, open = false): AnalyticsSpan =>
  ({ machine: m, state, reason: "", start: iso(s), end: iso(e), open });
const data = (o: Partial<WorkflowAnalytics> = {}): WorkflowAnalytics => ({
  server_time: iso(NOW), since: null,
  coverage: { archives: iso(COVER), energy: null, cues: null, devices: null, outages: null },
  machines: [machine(CTRL, { name: "本机", kind: "controller", model: "RTX 5060 8GB" }), machine("gpu-a"), machine("gpu-b")],
  films: [], failures: [], devices: { available: true, reason: "", spans: [] }, outages: [], inbox: null, ...o,
});
const F: Filters = { machine: "all", res: "all", compare: false };
const WEEK = rangeDef("7d", NOW, COVER);
const AT = local(2026, 10, 28, 10);

describe("machine rows (AC1, AC2)", () => {
  const d = data({
    machines: [
      machine(CTRL, { name: "本机", kind: "controller", model: "RTX 5060 8GB" }),
      machine("gpu-a", { name: "Aurora" }), machine("gpu-b", { name: "Birch" }),
      machine("gpu-c", { name: "Cedar", model: "RTX 5070 Ti 16GB" }),
      machine("gpu-new", { name: "Newbie", first_seen: iso(local(2026, 10, 30)) }),
      machine("gpu-old", { name: "Oldie", retired: true }), machine("gpu-gone", { name: "Gone", retired: true }),
    ],
    films: [
      // Birch only failed on this film: its energy counts for Birch, not for Aurora
      film(AT, { worker: "gpu-a", res: "4K", restore_s: 4 * 3600, duration_s: 7200, restore_wh: [["gpu-a", 800, 14400], ["gpu-b", 100, 1800]] }),
      film(AT + H, { worker: "gpu-a", res: "1080p", restore_s: 2 * 3600, duration_s: 3600, restore_wh: [["gpu-a", 400, 7200]] }),
      film(AT + 2 * H, { worker: "gpu-old", restore_s: 3600, restore_wh: [["gpu-old", 250, 3600]] }),
      film(AT + 3 * H, { worker: "gpu-new", restore_wh: [["gpu-new", 100, 3600]] }),
      film(AT + 4 * H, { worker: "gpu-new", res: "1080p", restore_wh: [["gpu-new", 100, 3600]] }),
      film(local(2026, 10, 20), { worker: "gpu-gone", restore_wh: [["gpu-gone", 100, 3600]] }), // previous week
      film(AT + 5 * H, { worker: null, restore_s: null, restore_wh: [] }),
    ],
    failures: [
      failure(AT, { machine: "gpu-a" }),
      failure(AT, { machine: "gpu-a", severity: "warn", type: "interrupted" }),
      failure(AT, { machine: "gpu-b", type: "transient", exhausted: true, outcome: "needs_action" }),
    ],
  });
  const A = compute(d, WEEK, F, NOW);
  const rows = machineRows(d, A);
  const row = (id: string) => rows.find((r) => r.m.id === id)!;

  it("lists the active fleet plus retired machines with data in range; 本机 only when it restored", () => {
    expect(rows.map((r) => r.m.id).sort()).toEqual(["gpu-a", "gpu-b", "gpu-c", "gpu-new", "gpu-old"]);
  });

  it("counts, restore hours, efficiency and 失败 (severity fail only)", () => {
    const a = row("gpu-a");
    expect([a.n4, a.n1, a.nNone, a.total]).toEqual([1, 1, 0, 2]);
    expect([a.a4, a.a1, a.min, a.max]).toEqual([4, 2, 2, 4]);
    expect(a.eff).toBe(2); // 6 h restore for 3 h of film
    expect(a.fails).toBe(1);
    expect(row("gpu-b").fails).toBe(1); // an exhausted transient is a failure
    expect(row("gpu-c")).toMatchObject({ total: 0, a4: null, power: null, kwh: null, kwhPartial: 0, fails: 0, eff: null });
  });

  it("平均功率 = Σwh × 3600 ÷ Σseconds from restore_wh, a machine with only failed attempts included", () => {
    expect(row("gpu-a").power).toBeCloseTo((1200 * 3600) / 21600, 6);
    expect(row("gpu-a").kwh).toBeCloseTo(1.2, 6);
    expect(row("gpu-b").total).toBe(0);
    expect(row("gpu-b").power).toBeCloseTo(200, 6);
    expect(row("gpu-b").kwh).toBeCloseTo(0.1, 6);
  });

  it("用电 is never 0 kWh without a reading; films whose restore lacked energy mark it partial", () => {
    const films = [
      film(AT, { worker: "gpu-a", restore_wh: [["gpu-a", 600, 7200]] }),
      film(AT + H, { worker: "gpu-a", restore_wh: [] }), // restored, no reading at all
      film(AT + 2 * H, { worker: "gpu-a", restore_wh: [["gpu-a", 300, 3600]], energy_partial: true }),
      film(AT + 3 * H, { worker: "gpu-b", restore_wh: [] }),
    ];
    const e = data({ films });
    const rs = machineRows(e, compute(e, WEEK, F, NOW));
    const a = rs.find((r) => r.m.id === "gpu-a")!, b = rs.find((r) => r.m.id === "gpu-b")!;
    expect(a.kwh).toBeCloseTo(0.9, 6);
    expect(a.kwhPartial).toBe(2);
    expect(a.power).toBeCloseTo((900 * 3600) / 10800, 6); // seconds only from attempts with a reading
    expect([b.kwh, b.kwhPartial, b.power]).toEqual([null, 1, null]);
  });

  it("marks 新加入 by the D14 rule and keeps the state key", () => {
    expect(row("gpu-new").isNew).toBe(true);
    expect(row("gpu-new").state).toBe("new");
    expect(row("gpu-old").state).toBe("retired");
    expect(row("gpu-a").state).toBe("online");
  });

  it("a machine filter keeps that machine only; the 本机 filter has no rows unless 本机 restored (D11)", () => {
    const one = compute(d, WEEK, { ...F, machine: "gpu-b" }, NOW);
    const r = machineRows(d, one);
    expect(r.map((x) => x.m.id)).toEqual(["gpu-b"]);
    expect(r[0].power).toBeCloseTo(200, 6); // its failed attempt on gpu-a's film still counts
    expect(machineRows(d, compute(d, WEEK, { ...F, machine: CTRL }, NOW))).toEqual([]);
    const withCtrl = data({ ...d, films: [...d.films, film(AT, { worker: CTRL, restore_wh: [[CTRL, 300, 10800]] })] });
    const withCtrlRows = machineRows(withCtrl, compute(withCtrl, WEEK, F, NOW));
    expect(withCtrlRows.map((x) => x.m.id)).toContain(CTRL);
    // 本机's model sorts before the GPU models by name, but its group goes last
    expect(modelGroups(withCtrlRows).map((g) => g.model)).toEqual(["RTX 5060 Ti 16GB", "RTX 5070 Ti 16GB", "RTX 5060 8GB"]);
    const ctrlOnly = machineRows(withCtrl, compute(withCtrl, WEEK, { ...F, machine: CTRL }, NOW));
    expect(ctrlOnly.map((x) => [x.m.id, x.total, x.power])).toEqual([[CTRL, 1, 100]]);
  });

  it("groups by model with the same-model average tick excluding only 新加入 machines", () => {
    const groups = modelGroups(rows);
    expect(groups.map((g) => [g.model, g.rows.length])).toEqual([["RTX 5060 Ti 16GB", 4], ["RTX 5070 Ti 16GB", 1]]);
    expect(modelAverage(groups[0])).toBeCloseTo((2 + 0 + 1) / 3, 6); // Newbie's 2 films left out
    expect(modelAverage(groups[1])).toBeNull(); // one machine: nothing to compare
  });

  it("sorts within a group by name by default, then by the chosen column; empty values last", () => {
    const g = modelGroups(rows)[0].rows;
    expect(sortMachineRows(g, { key: "model", dir: 1 }).map((r) => r.m.name)).toEqual(["Aurora", "Birch", "Newbie", "Oldie"]);
    expect(sortMachineRows(g, { key: "total", dir: -1 }).map((r) => r.m.name)).toEqual(["Aurora", "Newbie", "Oldie", "Birch"]);
    expect(sortMachineRows(g, { key: "a4", dir: 1 }).map((r) => r.m.name).at(-1)).toBe("Birch");
    expect(sortMachineRows(g, { key: "st", dir: 1 }).map((r) => r.state)).toEqual(["online", "online", "new", "retired"]);
  });
});

describe("各机型实测产能 (AC1)", () => {
  const machines = [machine(CTRL, { kind: "controller", name: "本机" }), machine("gpu-a"), machine("gpu-new", { first_seen: iso(local(2026, 10, 30)) })];
  const films = [film(AT), film(AT + H, { res: "1080p" }), film(AT + 2 * H, { worker: "gpu-new", restore_wh: [["gpu-new", 1, 1]] })];

  it("without device history covering the range: calendar days present, 新加入 included", () => {
    const d = data({ machines, films });
    const A = compute(d, WEEK, F, NOW);
    expect(devicesCoverRange(d, A)).toBe(false);
    const rows = machineRows(d, A);
    const span7 = (NOW - local(2026, 10, 25)) / DAY, spanNew = (NOW - local(2026, 10, 30)) / DAY;
    expect(rows.find((r) => r.m.id === "gpu-a")!.days).toBeCloseTo(span7, 6);
    expect(rows.find((r) => r.m.id === "gpu-new")!.days).toBeCloseTo(spanNew, 6);
    const [tp] = modelThroughput(modelGroups(rows));
    expect([tp.count, tp.newCount]).toEqual([2, 1]);
    expect(tp.pt).toBeCloseTo(3 / (span7 + spanNew), 6);
    expect(tp.p4).toBeCloseTo(2 / (span7 + spanNew), 6);
    expect(tp.p1).toBeCloseTo(1 / (span7 + spanNew), 6);
    expect([tp.a4, tp.a1]).toEqual([4, 4]);
  });

  it("with device history covering the range: online + 停领 days; offline and gaps left out", () => {
    const spans = [
      span("gpu-a", "online", local(2026, 10, 26), local(2026, 10, 27)),
      span("gpu-a", "paused", local(2026, 10, 27), local(2026, 10, 27, 12)),
      span("gpu-a", "offline", local(2026, 10, 27, 12), local(2026, 10, 28)),
      span("gpu-a", "online", local(2026, 10, 30), local(2026, 10, 31, 14), true), // open: runs to now
      span("gpu-a", "online", local(2026, 10, 20), local(2026, 10, 25, 12)), // half a day inside
      span("gpu-b", "online", local(2026, 10, 26), local(2026, 10, 27)),
    ];
    const d = data({ machines, films, coverage: { ...data().coverage, devices: iso(local(2026, 10, 20)) }, devices: { available: true, reason: "", spans } });
    const A = compute(d, WEEK, F, NOW);
    expect(devicesCoverRange(d, A)).toBe(true);
    expect(machineDays(d, A, machines[1], true)).toBeCloseTo(1 + 0.5 + (NOW - local(2026, 10, 30)) / DAY + 0.5, 6);
    expect(machineDays(d, A, machines[2], true)).toBe(0);
    const unavailable = data({ ...d, devices: { available: false, reason: "坏了", spans } });
    expect(devicesCoverRange(unavailable, compute(unavailable, WEEK, F, NOW))).toBe(false);
    const late = data({ ...d, coverage: { ...d.coverage, devices: iso(local(2026, 10, 28)) } });
    expect(devicesCoverRange(late, compute(late, WEEK, F, NOW))).toBe(false);
  });
});

describe("本机（主控）处理 figures (AC3)", () => {
  const films = [
    film(AT, { translated: true, cues: 1000, subtitles: { generated_ja: 1 }, asr_s: 600, translate_s: 60, scrape_s: 120, archive_s: 30 }),
    film(AT + H, { translated: false, cues: null, subtitles: { reused_zh: 1 }, asr_s: 1200, translate_s: null, target: "category", actor: null }),
    film(AT + 2 * H, { translated: false, subtitles: { no_speech: 1 }, asr_s: null, target: "new_actor", second: true }),
    film(AT + 3 * H, { translated: true, cues: 500, subtitles: { generated_ja: 2, no_speech: 1 }, translate_s: 120, target: "manual", actor: null }),
    film(AT + 4 * H, { translated: false, subtitles: {}, archive_s: null }), // 无需翻译 without a known reason: neither chip
  ];

  it("translated films, line total, 已有中文字幕 / 无语音 from subtitles only, never from 无需翻译", () => {
    const d = data({ films, coverage: { ...data().coverage, cues: iso(local(2026, 10, 27)) } });
    const c = controllerFigures(d, compute(d, WEEK, F, NOW));
    expect(c.translated).toBe(2);
    expect(c.cues).toBe(1500);
    expect(c.cuesSince).toBe(local(2026, 10, 27)); // coverage.cues inside the range
    expect([c.reusedZh, c.noSpeech]).toEqual([1, 2]);
    expect(c.asr).toEqual({ n: 4, avg: (10 + 20 + 10 + 10) / 4, max: 20 });
    expect(c.translate).toEqual({ n: 2, avg: 1.5, max: 2 });
    expect(c.archive).toMatchObject({ n: 5, max: 1 });
    expect(c.targets).toEqual({ performer: 2, category: 1, new_actor: 1, manual: 1, second: 1 });
    const early = data({ ...d, coverage: { ...d.coverage, cues: iso(local(2026, 10, 1)) } });
    expect(controllerFigures(early, compute(early, WEEK, F, NOW)).cuesSince).toBeNull();
    const none = data({ ...d, coverage: { ...d.coverage, cues: null } });
    expect(controllerFigures(none, compute(none, WEEK, F, NOW)).cues).toBeNull();
  });

  it("翻译服务中断: outages overlapping the range, clipped, the open one to now; uncovered before coverage.outages", () => {
    const outages = [
      { start: iso(local(2026, 10, 20, 10)), end: iso(local(2026, 10, 20, 11)), paused: false },
      { start: iso(local(2026, 10, 24, 23, 50)), end: iso(local(2026, 10, 25, 0, 20)), paused: false },
      { start: iso(local(2026, 10, 28, 10)), end: iso(local(2026, 10, 28, 10, 30)), paused: true },
      { start: iso(local(2026, 10, 31, 14)), end: null, paused: false },
    ];
    const d = data({ outages, coverage: { ...data().coverage, outages: iso(local(2026, 10, 27)) } });
    const o = outageFigures(d, compute(d, WEEK, F, NOW));
    expect(o.items.map((x) => [x.s, x.e, x.open])).toEqual([
      [WEEK.start, local(2026, 10, 25, 0, 20), false],
      [local(2026, 10, 28, 10), local(2026, 10, 28, 10, 30), false],
      [local(2026, 10, 31, 14), NOW, true],
    ]);
    expect(o.items[0].ms).toBe(30 * 60_000); // whole outage, not only the part inside the range
    expect(o.totalMs).toBe(30 * 60_000 + 30 * 60_000 + 20 * 60_000);
    expect(o.longestMs).toBe(30 * 60_000);
    expect(o.since).toBe(local(2026, 10, 27));
    expect(o.uncovUntil).toBe(local(2026, 10, 27));
    const early = data({ ...d, coverage: { ...d.coverage, outages: iso(local(2026, 10, 1)) } });
    const e = outageFigures(early, compute(early, WEEK, F, NOW));
    expect([e.since, e.uncovUntil]).toEqual([null, null]);
    const none = data({ ...d, coverage: { ...d.coverage, outages: null } });
    const n = outageFigures(none, compute(none, WEEK, F, NOW));
    expect([n.since, n.uncovUntil, n.items.length]).toEqual([null, WEEK.end, 0]);
  });
});

describe("耗时分布 figures (AC4)", () => {
  const films = [
    film(AT, { res: "4K", queue_s: 2 * 3600, restore_s: 4 * 3600, lead_s: 10 * 3600, asr_s: 600, translate_s: 60, scrape_s: 120, archive_s: 60 }),
    film(AT + H, { res: "4K", queue_s: null, restore_s: 6 * 3600, lead_s: 20 * 3600, translated: false, translate_s: null }),
    film(AT + 2 * H, { res: "1080p", queue_s: 4 * 3600, restore_s: 2 * 3600, lead_s: 30 * 3600 }),
  ];
  const d = data({ films });

  it("hour and minute strips per stage; a missing stage is left out, never 0", () => {
    const f = durationFigures(compute(d, WEEK, F, NOW));
    expect(f.hours.map((r) => [r.key, r.st?.n])).toEqual([["queue", 2], ["restore4k", 2], ["restore1080", 1], ["lead", 3]]);
    expect(f.hours[1].st).toMatchObject({ min: 4, max: 6, avg: 5 });
    expect(f.minutes.map((r) => [r.key, r.st?.n])).toEqual([["asr", 3], ["translate", 2], ["scrape", 3], ["archive", 3]]);
    expect(f.minutes[0].st?.avg).toBe(10);
  });

  it("time share: each stage's average in hours", () => {
    const f = durationFigures(compute(d, WEEK, F, NOW));
    expect(f.share.map((s) => s.key)).toEqual(["queue", "restore", "asr", "translate", "scrape", "archive"]);
    expect(f.share[0].hours).toBe(3);
    expect(f.share[1].hours).toBe(4);
    expect(f.share[2].hours).toBeCloseTo(10 / 60, 6);
  });

  it("the 4K filter drops the 1080P strip; the 本机 filter drops restore unless 本机 restored", () => {
    expect(durationFigures(compute(d, WEEK, { ...F, res: "4K" }, NOW)).hours.map((r) => r.key)).toEqual(["queue", "restore4k", "lead"]);
    const ctrl = durationFigures(compute(d, WEEK, { ...F, machine: CTRL }, NOW));
    expect(ctrl.hours.map((r) => r.key)).toEqual(["queue", "lead"]);
    expect(ctrl.share.find((s) => s.key === "restore")!.hours).toBe(0);
  });
});
