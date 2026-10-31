// 数据分析 sections 用电, 设备在线, 异常与报警 and 片源与片库 on the rendered page (EPIC 2026-10-31 §8.4
// items 6–9, §8.6, D6, D9, D16; C5 #392 AC1–AC4). Same fixture, fixed clock and Asia/Shanghai zone as
// analyticspage.test.tsx, so local-day buckets are deterministic.
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, type AnalyticsFilm, type WorkflowAnalytics } from "../api";
import { AnalyticsPage } from "../components/media/analytics/AnalyticsPage";
import SAMPLE from "./fixtures/analytics-sample.json";

const FIXTURE = SAMPLE as WorkflowAnalytics;
const NOW = Date.parse("2026-10-31T06:00:00Z"); // 14:00 in Shanghai
const WEEK_START = Date.parse("2026-10-24T16:00:00Z"); // local 10-25 00:00
const in7d = (iso: string) => { const t = Date.parse(iso); return t >= WEEK_START && t < NOW; };
const WEEK = FIXTURE.films.filter((f) => in7d(f.archived_at));
const env = (globalThis as unknown as { process: { env: Record<string, string | undefined> } }).process.env;
const zone = env.TZ;

beforeAll(() => { env.TZ = "Asia/Shanghai"; });
afterAll(() => { if (zone === undefined) delete env.TZ; else env.TZ = zone; });
beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(800);
});
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); });

async function mount(body: WorkflowAnalytics = FIXTURE) {
  const spy = vi.spyOn(api, "workflowAnalytics").mockResolvedValue(body);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={qc}><div className="mw"><AnalyticsPage /></div></QueryClientProvider>);
  await screen.findByRole("group", { name: /^完成影片/ });
  return spy;
}
const region = (name: string | RegExp) => screen.getByRole("region", { name });
const energy = () => region(/^用电/); // the heading carries the 「估算」 / 「部分」 badges
const uptime = () => region("设备在线");
const alerts = () => region("异常与报警");
const sources = () => region("片源与片库");
const range = async (label: string) => {
  fireEvent.click(within(screen.getByRole("radiogroup", { name: "时段" })).getByRole("radio", { name: label }));
  await waitFor(() => expect(document.querySelector(".an-sections")).not.toHaveClass("busy"));
};
const pick = (value: string) => fireEvent.change(screen.getByRole("combobox", { name: "机器" }), { target: { value } });
const view = (panel: HTMLElement, label: "图表" | "表格") => fireEvent.click(within(panel).getByRole("radio", { name: label }));
const roveItems = (el: Element) => Array.from(el.querySelectorAll<SVGGElement>("[data-rove]"));
const tipOf = (g: SVGGElement) => { act(() => g.focus()); const t = screen.getByRole("tooltip").textContent ?? ""; act(() => g.blur()); return t; };

// the C3 energy rule, restated: a film with no reading is missing, never 0 kWh
const hasReading = (f: AnalyticsFilm) => f.controller_wh != null || f.restore_wh.length > 0;
const filmKwh = (f: AnalyticsFilm) => (f.restore_wh.reduce((a, e) => a + e[1], 0) + (f.controller_wh ?? 0)) / 1000;
const kwh = (v: number) => (v >= 100 ? String(Math.round(v)) : v >= 10 ? (Math.round(v * 10) / 10).toFixed(1) : v.toFixed(2));
// the fixture's week has partial readings but no film without any: drop one film's only reading
const GAP: WorkflowAnalytics = { ...FIXTURE, films: FIXTURE.films.map((f) => (f.code === "ZZB-151" ? { ...f, controller_wh: null } : f)) };
const GAP_WEEK = GAP.films.filter((f) => in7d(f.archived_at));

describe("用电 (AC1)", () => {
  it("估算 badge, 部分 note, per-machine attribution incl. failed attempts elsewhere, 本机后处理 and the top-5 table", async () => {
    await mount(GAP);
    const measured = GAP_WEEK.filter(hasReading);
    const total = measured.reduce((a, f) => a + filmKwh(f), 0);
    expect(energy()).toHaveTextContent(`GPU 修复 + 本机后处理的估算用电，按归档完成时间计入 · 过去 7 天共 ${kwh(total)} kWh`);
    const head = within(energy()).getByRole("heading", { level: 2 });
    expect(head).toHaveTextContent("估算");
    expect(head).toHaveTextContent("部分");
    const missing = GAP_WEEK.length - measured.length, partial = measured.filter((f) => f.energy_partial).length;
    expect(missing).toBeGreaterThan(0);
    expect(partial).toBeGreaterThan(0);
    expect(within(energy()).getByText(`${missing + partial} 部用电记录不全，只合计已有的记录`)).toBeInTheDocument();

    const bars = within(energy()).getByRole("img", { name: "按机器的估算用电" });
    const labels = Array.from(bars.querySelectorAll(".rlabel")).map((t) => t.textContent);
    expect(labels[labels.length - 1]).toBe("本机（后处理）");
    // Dune restored none of these films alone but holds the failed 50 Wh attempts on films restored elsewhere
    const dune = GAP_WEEK.flatMap((f) => f.restore_wh).filter(([w]) => w === "w-gpu-dune").reduce((a, e) => a + e[1], 0) / 1000;
    const duneRow = roveItems(bars)[labels.indexOf("Dune")];
    expect(duneRow.querySelector(".dlabel")?.textContent).toContain(kwh(dune));
    const post = GAP_WEEK.reduce((a, f) => a + (f.controller_wh ?? 0), 0) / 1000;
    expect(roveItems(bars)[labels.length - 1].querySelector(".dlabel")?.textContent).toContain(kwh(post));

    const top = within(energy()).getByRole("table", { name: "用电最多的影片" });
    const codes = within(top).getAllByRole("row").slice(1).map((r) => r.querySelector("td")?.textContent);
    const expected = [...measured].sort((a, b) => filmKwh(b) - filmKwh(a)).slice(0, 5).map((f) => f.code);
    expect(codes.map((c) => c?.replace("（部分）", ""))).toEqual(expected);
  });

  it("per-bucket columns with a tooltip that names films without a reading", async () => {
    await mount(GAP);
    const chart = within(energy()).getByRole("img", { name: "每个时段的估算用电" });
    const cols = roveItems(chart);
    expect(cols).toHaveLength(7);
    const missingFilm = GAP_WEEK.find((f) => !hasReading(f))!;
    const day = new Date(missingFilm.archived_at).getDate();
    const i = cols.findIndex((g) => tipOf(g).includes(`10-${day}`));
    expect(tipOf(cols[i])).toMatch(/1 部无用电记录/);
  });

  it("a bucket whose films all lack a reading shows 「—」 / 「无用电记录」, not 0 kWh (C5-X3)", async () => {
    const strip = new Set(["ZZA-150", "ZZB-151"]); // every film archived on local 10-26
    await mount({ ...FIXTURE, films: FIXTURE.films.map((f) => (strip.has(f.code) ? { ...f, restore_wh: [], controller_wh: null } : f)) });
    const cols = roveItems(within(energy()).getByRole("img", { name: "每个时段的估算用电" }));
    const tip = tipOf(cols.find((g) => tipOf(g).includes("10-26"))!);
    expect(tip).toContain("无用电记录");
    expect(tip).not.toContain("0.00 kWh");
    view(energy(), "表格");
    const row = within(within(energy()).getByRole("table", { name: "用电" })).getByRole("row", { name: /^10-26/ });
    expect(row.querySelectorAll("td")[1].textContent).toBe("—");
  });

  it("对比上一周期: no previous line while either period's readings are incomplete", async () => {
    await mount();
    fireEvent.click(screen.getByRole("switch", { name: "对比上一周期" }));
    expect(within(energy()).getByText("用电记录不全，暂时无法对比")).toBeInTheDocument();
    expect(within(energy()).getByRole("img", { name: "每个时段的估算用电" }).querySelector(".prev-line")).toBeNull();
  });

  it("the 本机 filter shows only 本机's post-processing; the table view lists buckets, groups and the top 5", async () => {
    await mount();
    view(energy(), "表格");
    expect(within(energy()).getByRole("table", { name: "用电" }).querySelectorAll("tbody tr")).toHaveLength(7);
    expect(within(energy()).getByRole("table", { name: "用电分组" })).toHaveTextContent("本机（后处理）");
    expect(within(energy()).getByRole("table", { name: "用电最多的影片" }).querySelectorAll("tbody tr")).toHaveLength(5);
    pick("w-ctrl-0001");
    expect(energy()).toHaveTextContent("本机后处理的估算用电");
    const groups = within(energy()).getByRole("table", { name: "用电分组" });
    expect(groups).not.toHaveTextContent("Dune");
    expect(groups).toHaveTextContent("本机（后处理）");
  });
});

describe("设备在线 (AC2, §8.6)", () => {
  it("one lane per machine in range plus 本机, 在线率 over recorded time, and the coverage notice", async () => {
    await mount();
    const chart = within(uptime()).getByRole("img", { name: "设备在线时间轴" });
    expect(Array.from(chart.querySelectorAll(".rlabel")).map((t) => t.textContent)).toEqual(["Aurora", "Birch", "Cedar", "Dune", "Ember", "本机"]);
    expect(uptime()).toHaveTextContent("设备在线记录从 10-28 08:00 开始，之前的时间显示为「无记录」");
    // Birch: 76 h 28 min recorded (a 1.5 h gap is unrecorded, not offline), 71 h 38 min online → 94 %
    const values = Array.from(chart.querySelectorAll(".vt1")).map((t) => t.textContent);
    expect(values[1]).toBe("在线 94%");
    expect(uptime()).toHaveTextContent("在线率 = （在线 + 停领）÷ 有记录的时间");
  });

  it("停领 tooltips name the reason, gaps say 无记录, a 新加入 lane starts at first_seen", async () => {
    await mount();
    const chart = within(uptime()).getByRole("img", { name: "设备在线时间轴" });
    const tips = roveItems(chart).map(tipOf);
    expect(tips.some((t) => t.includes("停领") && t.includes("主控停领"))).toBe(true); // Cedar (admission)
    expect(tips.some((t) => t.includes("停领") && t.includes("主控暂停新领取"))).toBe(true); // global pause
    expect(tips.some((t) => t.includes("主控未运行或未收到心跳，无记录"))).toBe(true);
    expect(tips.some((t) => t.includes("Ember") && t.includes("10-30 17:00 加入"))).toBe(true);
    const marks = (lane: number) => Array.from(chart.querySelectorAll("g.lane")[lane].querySelectorAll<SVGRectElement>("rect.mk"));
    expect(Number(marks(4)[0].getAttribute("x"))).toBeGreaterThan(Number(marks(0)[0].getAttribute("x")) + 100);
  });

  it("legend states carry text and an icon; the table lists every span with its state text", async () => {
    await mount();
    const legend = uptime().querySelector(".lgd")!;
    for (const s of ["在线", "停领", "离线", "无记录"]) expect(legend).toHaveTextContent(s);
    expect(legend.querySelectorAll(".lgd-ic svg").length).toBe(4);
    view(uptime(), "表格");
    const spans = within(uptime()).getByRole("table", { name: "离线与停领记录" });
    expect(spans).toHaveTextContent("停领（主控停领）");
    expect(spans).toHaveTextContent("离线");
    const rates = within(uptime()).getByRole("table", { name: "在线率" });
    expect(within(rates).getByRole("row", { name: /^Birch/ })).toHaveTextContent("94%");
    expect(rates.querySelector("thead")).toHaveTextContent("无记录");
  });

  it("「仍在继续」 only for a fresh open span matching the live state; otherwise 「最后记录」 (C5-X11)", async () => {
    // Birch's open offline span is fresh, but Birch reports online now; Cedar's 停领 (admission) still matches
    await mount({ ...FIXTURE, machines: FIXTURE.machines.map((m) => (m.id === "w-gpu-birch" ? { ...m, state_now: "online" as const } : m)) });
    const chart = within(uptime()).getByRole("img", { name: "设备在线时间轴" });
    const tips = roveItems(chart).map(tipOf);
    const birch = tips.filter((t) => t.startsWith("Birch") && t.includes("离线"));
    expect(birch.length).toBeGreaterThan(0);
    for (const t of birch) expect(t).not.toContain("仍在继续");
    expect(birch.some((t) => t.includes("最后记录 10-31 13:59"))).toBe(true);
    expect(tips.some((t) => t.startsWith("Cedar") && t.includes("仍在继续"))).toBe(true);
    view(uptime(), "表格");
    const spans = within(uptime()).getByRole("table", { name: "离线与停领记录" });
    const row = (name: string) => within(spans).getAllByRole("row").filter((r) => r.querySelector("td")?.textContent === name).pop()!;
    expect(row("Birch")).not.toHaveTextContent("仍在继续");
    expect(row("Birch")).toHaveTextContent("最后记录 10-31 13:59");
    expect(row("Cedar")).toHaveTextContent("仍在继续");
    expect(alerts()).not.toHaveTextContent("仍离线"); // the same rule in 异常与报警
  });

  it("devices.available = false: 「设备记录不可用：<reason>」; no device history yet: its own notice", async () => {
    await mount({ ...FIXTURE, devices: { available: false, reason: "analytics.db 无法打开", spans: [] } });
    expect(uptime()).toHaveTextContent("设备记录不可用：analytics.db 无法打开");
    await range("24 小时");
    expect(uptime()).toHaveTextContent("设备记录不可用：analytics.db 无法打开");
  });

  it("no device span yet", async () => {
    await mount({ ...FIXTURE, coverage: { ...FIXTURE.coverage, devices: null }, devices: { available: true, reason: "", spans: [] } });
    expect(uptime()).toHaveTextContent("还没有设备在线记录");
    const values = Array.from(within(uptime()).getByRole("img", { name: "设备在线时间轴" }).querySelectorAll(".vt1")).map((t) => t.textContent);
    expect(new Set(values)).toEqual(new Set(["—"]));
  });

  it("filters: one GPU machine alone; 本机 alone", async () => {
    await mount();
    pick("w-gpu-cedar");
    expect(Array.from(within(uptime()).getByRole("img", { name: "设备在线时间轴" }).querySelectorAll(".rlabel")).map((t) => t.textContent)).toEqual(["Cedar"]);
    pick("w-ctrl-0001");
    expect(Array.from(within(uptime()).getByRole("img", { name: "设备在线时间轴" }).querySelectorAll(".rlabel")).map((t) => t.textContent)).toEqual(["本机"]);
  });
});

describe("异常与报警 (AC3, D5, D6, D16)", () => {
  it("the burst banner names the machine, count and span; burst rows stay highlighted", async () => {
    await mount();
    const banner = alerts().querySelector(".notice.red")!;
    expect(banner).toHaveTextContent("Birch 10-31 08:20–09:00 出现 3 次「应用程序控制拦截」");
    expect(banner).toHaveTextContent("已自动改派 3 部");
    const list = within(alerts()).getByRole("table", { name: "最近的异常与报警" });
    expect(list.querySelectorAll("tbody tr.hl")).toHaveLength(3);
  });

  it("burst outcome counts are films (部), the heading counts attempts (次) (C5-X7)", async () => {
    // two of the three faults hit the same film: 3 attempts, 2 films
    const failures = FIXTURE.failures.map((f) => (f.code === "ZZE-085" ? { ...f, code: "ZZD-084" } : f));
    await mount({ ...FIXTURE, failures });
    const banner = alerts().querySelector(".notice.red")!;
    expect(banner).toHaveTextContent("出现 3 次「应用程序控制拦截」");
    expect(banner).toHaveTextContent("已自动改派 2 部");
  });

  it("an older, recovered burst keeps its highlighted rows without the banner", async () => {
    const shift = (iso: string) => new Date(Date.parse(iso) - 3 * 86_400_000).toISOString();
    const failures = FIXTURE.failures.map((f) => (f.type === "machine_fault" ? { ...f, at: shift(f.at) } : f));
    await mount({ ...FIXTURE, failures });
    expect(alerts().querySelector(".notice.red")).toBeNull();
    expect(within(alerts()).getByRole("table", { name: "最近的异常与报警" }).querySelectorAll("tbody tr.hl")).toHaveLength(3);
  });

  it("by type with fail / warn icons, the exhausted labels, offline and outage warnings; no CPU type or toggle", async () => {
    await mount();
    const types = within(alerts()).getByRole("img", { name: "按类型的异常次数" });
    const labels = Array.from(types.querySelectorAll(".rlabel")).map((t) => t.textContent);
    for (const l of ["应用程序控制拦截", "临时失败（重试已用尽）", "翻译暂停（重试已用尽）", "缺少 ffprobe", "刮削白名单", "其他失败", "运行中断", "已停止", "翻译暂停", "临时失败（自动重试）", "设备离线", "翻译服务中断"]) {
      expect(labels).toContain(l);
    }
    expect(labels.indexOf("应用程序控制拦截")).toBeLessThan(labels.indexOf("运行中断")); // failures before warnings
    expect(types.querySelectorAll("[data-rove] svg").length).toBe(labels.length); // every row has its icon
    expect(alerts()).not.toHaveTextContent(/CPU/);
    expect(within(alerts()).queryByRole("switch")).toBeNull();
    const perMachine = within(alerts()).getByRole("img", { name: "各机器失败次数" });
    const rows = roveItems(perMachine).map((g) => [g.querySelector(".rlabel")?.textContent, g.querySelector(".dlabel")?.textContent]);
    expect(rows).toContainEqual(["Birch", "3"]);
    expect(rows).toContainEqual(["Dune", "0"]); // a stop and an interruption are warnings
  });

  it("merged list newest first with outcome labels; 14 rows, then 「显示更多」; a filter change resets the rows, not the view", async () => {
    await mount();
    const list = () => within(alerts()).getByRole("table", { name: "最近的异常与报警" });
    const rows = () => Array.from(list().querySelectorAll("tbody tr"));
    expect(rows()).toHaveLength(14);
    expect(rows()[0]).toHaveTextContent("10-31 13:30");
    expect(rows()[0]).toHaveTextContent("翻译服务中断");
    expect(rows()[0]).toHaveTextContent("尚未恢复");
    for (const o of ["已自动改派", "需要处理", "重试中", "已重试成功", "仍离线"]) expect(list()).toHaveTextContent(o);
    expect(alerts()).toHaveTextContent("显示 14 条，共 18 条");
    fireEvent.click(within(alerts()).getByRole("button", { name: "显示更多" }));
    expect(rows()).toHaveLength(18);
    expect(within(alerts()).queryByRole("button", { name: "显示更多" })).toBeNull();
    expect(list()).toHaveTextContent("已恢复"); // Dune came back online right after its offline span
    await range("30 天"); // a new filter starts at 14 rows again
    expect(rows()).toHaveLength(14);
    await range("7 天"); // C5-X8: and coming back to the first filter does not bring its old expansion back
    expect(rows()).toHaveLength(14);
    await range("30 天");
    view(alerts(), "表格");
    fireEvent.click(within(alerts()).getByRole("button", { name: "显示更多" }));
    await range("7 天"); // C5-X4: the chosen 表格 view survives a filter change
    expect(within(alerts()).getByRole("table", { name: "按类型" })).toBeInTheDocument();
    expect(rows()).toHaveLength(14);
  });

  it("a GPU filter keeps that machine's events; table view lists types and machines", async () => {
    await mount();
    pick("w-gpu-birch");
    const list = within(alerts()).getByRole("table", { name: "最近的异常与报警" });
    const machines = Array.from(list.querySelectorAll("tbody tr")).map((r) => r.querySelectorAll("td")[1].textContent);
    expect(new Set(machines)).toEqual(new Set(["Birch"]));
    expect(list).not.toHaveTextContent("翻译服务中断");
    view(alerts(), "表格");
    expect(within(alerts()).getByRole("table", { name: "按类型" })).toHaveTextContent("应用程序控制拦截");
    expect(within(alerts()).getByRole("table", { name: "各机器失败次数" })).toHaveTextContent("Birch");
  });
});

describe("片源与片库 (AC4, AC5, D9)", () => {
  it("INBOX card: a meter with values, 已用 / 可用 text, 待处理, 平均每天腾出, 现在还可放入 估算 — and no projection", async () => {
    await mount();
    const meter = within(sources()).getByRole("meter", { name: "INBOX 已用空间" });
    expect(meter).toHaveAttribute("aria-valuemin", "0");
    expect(meter).toHaveAttribute("aria-valuemax", "1000");
    expect(meter).toHaveAttribute("aria-valuenow", "590");
    expect(meter.getAttribute("aria-valuetext")).toBe("已用 590 GB，可用 410 GB，共 1 TB");
    const card = within(sources()).getByRole("group", { name: /INBOX 空间/ });
    expect(card).toHaveTextContent("当前 · 不随时段筛选");
    expect(card).toHaveTextContent("已用 590 GB · 可用 410 GB（共 1 TB）");
    expect(card).toHaveTextContent("待处理影片占用320GB");
    expect(card).toHaveTextContent("23 部");
    expect(card).toHaveTextContent("平均每天腾出160GB");
    expect(card).toHaveTextContent("现在还可放入约 31部");
    expect(card).toHaveTextContent("估算");
    expect(card).not.toHaveTextContent(/剩余|1 天后/);
    act(() => meter.focus());
    expect(screen.getByRole("tooltip")).toHaveTextContent("其他文件");
    expect(screen.getByRole("tooltip")).toHaveTextContent("可用");
  });

  it("histograms, top 8 actors and marks share from the mark codes", async () => {
    await mount();
    expect(roveItems(within(sources()).getByRole("img", { name: "输入大小分布" }))).toHaveLength(9);
    expect(roveItems(within(sources()).getByRole("img", { name: "片长分布" }))).toHaveLength(6);
    const actors = within(sources()).getByRole("img", { name: "归档最多的演员" });
    const counts = new Map<string, number>();
    for (const f of WEEK) if (f.actor) counts.set(f.actor, (counts.get(f.actor) ?? 0) + 1);
    const top = [...counts.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])).slice(0, 8);
    expect(Array.from(actors.querySelectorAll(".rlabel")).map((t) => t.textContent)).toEqual(top.map(([n]) => n));
    const marks = within(sources()).getByRole("img", { name: "标识占比" });
    expect(Array.from(marks.querySelectorAll(".rlabel")).map((t) => t.textContent)).toEqual(["中文字幕", "无码", "4K", "修复"]);
    const zh = WEEK.filter((f) => f.marks.includes("chinese_subtitles")).length;
    expect(marks.querySelectorAll(".dlabel")[0].textContent).toBe(`${Math.round((zh / WEEK.length) * 100)}%`);
  });

  it("table view keeps the INBOX card and lists every bin, actor and mark", async () => {
    await mount();
    view(sources(), "表格");
    expect(within(sources()).getByRole("meter")).toBeInTheDocument();
    expect(within(sources()).getByRole("table", { name: "输入大小" }).querySelectorAll("tbody tr")).toHaveLength(9);
    expect(within(sources()).getByRole("table", { name: "片长" }).querySelectorAll("tbody tr")).toHaveLength(6);
    expect(within(sources()).getByRole("table", { name: "标识占比" })).toHaveTextContent("中文字幕");
  });

  it("without an INBOX root the card says so; with no archive yet the card stays and the film panels say so", async () => {
    await mount({ ...FIXTURE, inbox: null, coverage: { ...FIXTURE.coverage, archives: null }, films: [] });
    expect(sources()).toHaveTextContent("没有配置 INBOX 目录，无法读取空间");
    expect(within(sources()).getAllByText("还没有归档完成的影片").length).toBeGreaterThan(0);
    expect(within(energy()).getByText("还没有归档完成的影片")).toBeInTheDocument();
  });
});
