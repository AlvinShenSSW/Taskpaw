// 数据分析 page shell, filters, KPI row and 产量趋势 (EPIC 2026-10-31 §8, §10; C3 #390).
// The fixture follows §7 field for field with invented machines and films. The clock is fixed and the
// zone is Asia/Shanghai for this file, so local-day buckets are deterministic.
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, HttpError, type WorkflowAnalytics } from "../api";
import { AnalyticsPage, ANALYTICS_REFETCH_MS } from "../components/media/analytics/AnalyticsPage";
import { ANALYTICS_CSS } from "../components/media/analytics/analyticsStyles";
import { MEDIA_CSS } from "../components/media/mediaStyles";
import SAMPLE from "./fixtures/analytics-sample.json";

const FIXTURE = SAMPLE as WorkflowAnalytics;
const NOW = Date.parse("2026-10-31T06:00:00Z"); // 14:00 in Shanghai
const WEEK_START = Date.parse("2026-10-24T16:00:00Z"); // local 10-25 00:00
const in7d = (iso: string) => { const t = Date.parse(iso); return t >= WEEK_START && t < NOW; };
// Node re-reads TZ on assignment; restored after this file (delete: assigning undefined stores "undefined")
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

function mount(body: WorkflowAnalytics | Error = FIXTURE) {
  const spy = vi.spyOn(api, "workflowAnalytics");
  if (body instanceof Error) spy.mockRejectedValue(body);
  else spy.mockResolvedValue(body);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={qc}><div className="mw"><AnalyticsPage /></div></QueryClientProvider>);
  return { spy, qc };
}
const tile = (label: string) => screen.getByRole("group", { name: new RegExp(`^${label}`) });
const range = (label: string) => fireEvent.click(within(screen.getByRole("radiogroup", { name: "时段" })).getByRole("radio", { name: label }));
const production = () => screen.getByRole("region", { name: "产量趋势" });

describe("数据分析 page", () => {
  it("shows the title, the data start, 最后刷新 and 「每分钟自动刷新」, and asks for one range length before 7 天", async () => {
    const { spy, qc } = mount();
    expect(await screen.findByRole("heading", { level: 1, name: "数据分析" })).toBeInTheDocument();
    expect(await screen.findByText(/数据自 2026-10-10 起/)).toBeInTheDocument();
    expect(screen.getByText("每分钟自动刷新")).toBeInTheDocument();
    expect(screen.getByText("10-31 14:00:00")).toBeInTheDocument();
    expect(spy).toHaveBeenCalledWith("2026-10-17T16:00:00.000Z");
    const query = qc.getQueryCache().find({ queryKey: ["mediaAnalytics", "2026-10-17T16:00:00.000Z"] });
    expect(query).toBeDefined();
    expect((query!.options as { refetchInterval?: unknown }).refetchInterval).toBe(60000);
    expect(ANALYTICS_REFETCH_MS).toBe(60000);
  });

  it("KPI row: films, durations, 估算 energy, 失败率 (fail only, exhausted transients included) and GPU 在线 N / M", async () => {
    mount();
    await screen.findByRole("group", { name: /^完成影片/ });
    const films = FIXTURE.films.filter((f) => in7d(f.archived_at));
    const k4 = films.filter((f) => f.res === "4K").length, k1 = films.filter((f) => f.res === "1080p").length;
    expect(tile("完成影片")).toHaveTextContent(`${films.length}部`);
    expect(tile("完成影片")).toHaveTextContent(`4K ${k4} · 1080P ${k1}`);
    expect(tile("平均全程时长")).toHaveTextContent("入库→归档");
    expect(within(tile("总用电")).getByText("估算")).toBeInTheDocument();
    const failures = FIXTURE.failures.filter((f) => in7d(f.at));
    const fails = failures.filter((f) => f.severity === "fail").length;
    expect(fails).toBe(9);
    expect(failures.some((f) => f.exhausted && f.severity === "fail")).toBe(true);
    const rate = (fails / (fails + films.length)) * 100;
    expect(tile("失败率")).toHaveTextContent(`${(Math.round(rate * 10) / 10).toFixed(1)}%`);
    expect(tile("失败率")).toHaveTextContent(`失败 ${fails} 次 · 警告 ${failures.length - fails} 次`);
    const gpu = tile("GPU 在线");
    expect(gpu).toHaveTextContent("4 / 5");
    expect(gpu).toHaveTextContent("可领取 3 台");
    expect(gpu).toHaveTextContent("Birch 离线 5 小时");
    expect(gpu).toHaveTextContent("Cedar 停领 3 小时");
    // deltas are always shown, as in the approved preview; 对比上一周期 only adds chart overlays
    expect(screen.getByRole("switch", { name: "对比上一周期" })).toHaveAttribute("aria-checked", "false");
    const prevFilms = FIXTURE.films.filter((f) => { const t = Date.parse(f.archived_at); return t >= WEEK_START - 7 * 86_400_000 && t < WEEK_START; }).length;
    const pct = Math.round(((films.length - prevFilms) / prevFilms) * 100);
    expect(within(tile("完成影片")).getByText(`${pct > 0 ? "+" : "−"}${Math.abs(pct)}%`)).toBeInTheDocument();
    for (const label of ["完成影片", "平均全程时长", "平均修复时长", "总用电", "失败率"]) {
      expect(tile(label).querySelector(".delta")).not.toBeNull();
    }
    expect(production().querySelector(".prev-line")).toBeNull();
    expect(within(production()).queryByText("上一周期")).toBeNull();
  });

  it("对比上一周期 adds deltas and the previous-period line; 全部 disables it and asks for all history", async () => {
    const { spy } = mount();
    await screen.findByRole("group", { name: /^完成影片/ });
    fireEvent.click(screen.getByRole("switch", { name: "对比上一周期" }));
    expect(screen.getAllByText("较上一周期").length).toBeGreaterThan(0);
    expect(within(production()).getByText("上一周期")).toBeInTheDocument();
    expect(production().querySelector(".prev-line")).not.toBeNull();
    range("全部");
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith(undefined));
    await waitFor(() => expect(within(tile("完成影片")).getByText("全部时段没有上一周期")).toBeInTheDocument());
    const sw = screen.getByRole("switch", { name: "对比上一周期" });
    expect(sw).toBeDisabled();
    expect(sw).toHaveAttribute("aria-checked", "false");
    await waitFor(() => expect(tile("完成影片")).toHaveTextContent(`${FIXTURE.films.length}部`));
    expect(screen.queryByText(/所选时段早于数据记录起点/)).toBeNull();
  });

  it("a range before the first archive refetches with a floored since and shows the uncovered notice and band", async () => {
    const { spy } = mount();
    await screen.findByRole("group", { name: /^完成影片/ });
    range("30 天");
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith("2026-09-01T16:00:00.000Z"));
    expect(await screen.findByText("所选时段早于数据记录起点（10-10），只统计 21 天。图中灰色区域没有记录。")).toBeInTheDocument();
    expect(within(tile("完成影片")).getByText("上一周期没有记录")).toBeInTheDocument(); // with compare still off
    expect(within(production()).getByText("无记录")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("switch", { name: "对比上一周期" }));
    expect(within(production()).getByText("上一周期没有完整记录，暂时无法对比。")).toBeInTheDocument();
    expect(screen.getAllByText("上一周期没有记录").length).toBeGreaterThan(0);
  });

  it("machine and resolution filters recompute locally without a refetch and announce the summary", async () => {
    const { spy } = mount();
    await screen.findByRole("group", { name: /^完成影片/ });
    const calls = spy.mock.calls.length;
    const select = screen.getByRole("combobox", { name: "机器" });
    const groups = within(select).getAllByRole("group");
    expect(groups.map((g) => g.getAttribute("label"))).toEqual(["RTX 5060 Ti 16GB × 4", "RTX 5070 Ti 16GB × 1", "主控"]);
    expect(within(select).getByRole("option", { name: "Ember（新加入）" })).toBeInTheDocument();
    expect(within(select).queryByRole("option", { name: /Fern/ })).toBeNull(); // retired, no data in 7 天
    fireEvent.change(select, { target: { value: "w-gpu-dune" } });
    const dune = FIXTURE.films.filter((f) => in7d(f.archived_at) && f.worker === "w-gpu-dune").length;
    expect(tile("完成影片")).toHaveTextContent(`${dune}部`);
    expect(screen.getByText(`已更新：过去 7 天 · Dune · 全部分辨率 · 完成 ${dune} 部`)).toBeInTheDocument();
    fireEvent.change(select, { target: { value: "w-ctrl-0001" } });
    expect(within(tile("平均修复时长")).getByText("本机不做修复")).toBeInTheDocument();
    expect(tile("本机状态")).toHaveTextContent("在线");
    fireEvent.change(select, { target: { value: "all" } });
    fireEvent.click(within(screen.getByRole("radiogroup", { name: "分辨率" })).getByRole("radio", { name: "4K" }));
    const k4 = FIXTURE.films.filter((f) => in7d(f.archived_at) && f.res === "4K").length;
    expect(tile("完成影片")).toHaveTextContent(`${k4}部`);
    expect(spy.mock.calls.length).toBe(calls);
  });

  it("全部 lists a retired machine with data as 「（已退休）」", async () => {
    mount();
    await screen.findByRole("group", { name: /^完成影片/ });
    range("全部");
    expect(await screen.findByRole("option", { name: "Fern（已退休）" })).toBeInTheDocument();
  });

  it("产量趋势: stacked columns with a keyboard tooltip, and a table view with every bucket", async () => {
    mount();
    await screen.findByRole("group", { name: /^完成影片/ });
    const films = FIXTURE.films.filter((f) => in7d(f.archived_at));
    expect(production()).toHaveTextContent(`每天完成归档的影片数，按分辨率分层 · 过去 7 天共 ${films.length} 部`);
    const cols = production().querySelectorAll<SVGGElement>("[data-rove]");
    expect(cols).toHaveLength(7);
    act(() => cols[0].focus());
    const day0 = films.filter((f) => Date.parse(f.archived_at) < WEEK_START + 86_400_000);
    expect(screen.getByRole("tooltip")).toHaveTextContent("10-25 周日");
    expect(screen.getByRole("tooltip")).toHaveTextContent(`${day0.length} 部合计`);
    fireEvent.keyDown(cols[0], { key: "End" });
    expect(screen.getByRole("tooltip")).toHaveTextContent("10-31 周六（截至 14:00）");
    act(() => cols[6].blur());
    fireEvent.click(within(production()).getByRole("radio", { name: "表格" }));
    const table = within(production()).getByRole("table", { name: "产量趋势" });
    const header = within(table).getAllByRole("columnheader").map((c) => c.textContent);
    expect(header).toEqual(["时段", "4K", "1080P", "未知分辨率", "合计"]);
    expect(within(table).getAllByRole("row")).toHaveLength(8);
  });

  it("dims the previous render while a new range loads", async () => {
    const { spy } = mount();
    await screen.findByRole("group", { name: /^完成影片/ });
    let release: (v: WorkflowAnalytics) => void = () => {};
    spy.mockImplementation(() => new Promise((r) => { release = r; }));
    range("90 天");
    expect(document.querySelector(".an-sections")).toHaveClass("busy");
    expect(tile("完成影片")).toBeInTheDocument();
    await act(async () => { release(FIXTURE); });
    await waitFor(() => expect(document.querySelector(".an-sections")).not.toHaveClass("busy"));
  });
});

describe("KPI honesty (Codex r1)", () => {
  it("总用电 sums measured films only, marks 「部分」 with the missing count and does not compare (C3-X2)", async () => {
    const at = ["2026-10-30T01:00:00Z", "2026-10-30T02:00:00Z"];
    const unmeasured = at.map((t, i) => ({ ...FIXTURE.films[FIXTURE.films.length - 1], code: `ZZN-90${i}`, archived_at: t,
      restore_wh: [] as WorkflowAnalytics["films"][number]["restore_wh"], controller_wh: null }));
    // only missing readings here (no partial film), so the note keeps 「N 部无用电记录」
    const films = [...FIXTURE.films.map((f) => ({ ...f, energy_partial: false })), ...unmeasured]
      .sort((a, b) => a.archived_at.localeCompare(b.archived_at));
    mount({ ...FIXTURE, films });
    await screen.findByRole("group", { name: /^完成影片/ });
    const measured = FIXTURE.films.filter((f) => in7d(f.archived_at));
    const kwh = measured.reduce((a, f) => a + f.restore_wh.reduce((x, e) => x + e[1], 0) + (f.controller_wh ?? 0), 0) / 1000;
    const energy = tile("总用电");
    expect(energy).toHaveTextContent(`${kwh >= 10 ? (Math.round(kwh * 10) / 10).toFixed(1) : kwh.toFixed(2)}kWh`);
    expect(within(energy).getByText("部分")).toBeInTheDocument();
    expect(energy).toHaveTextContent("2 部无用电记录");
    expect(energy).toHaveTextContent(`每片平均 ${(kwh / measured.length).toFixed(2)} kWh`);
    expect(within(energy).getByText("上一周期无可比数据")).toBeInTheDocument();
    expect(within(tile("完成影片")).queryByText("部分")).toBeNull();
  });

  it("a partial energy reading marks 「部分」 with 「用电记录不全」 and does not compare (C3-X5)", async () => {
    // every film in range is measured, two of them only partly; none missing
    const partial = new Set(FIXTURE.films.filter((f) => in7d(f.archived_at)).slice(0, 2).map((f) => f.code));
    const films = FIXTURE.films.map((f) => (in7d(f.archived_at)
      ? { ...f, controller_wh: f.controller_wh ?? 50, energy_partial: partial.has(f.code) }
      : { ...f, energy_partial: false }));
    mount({ ...FIXTURE, films });
    await screen.findByRole("group", { name: /^完成影片/ });
    const energy = tile("总用电");
    expect(within(energy).getByText("部分")).toBeInTheDocument();
    expect(energy).toHaveTextContent("2 部用电记录不全");
    expect(energy).not.toHaveTextContent("无用电记录");
    expect(within(energy).getByText("上一周期无可比数据")).toBeInTheDocument();
  });

  it("the GPU spark's latest point is the live state, not a drop past the last recorded tick (C3-X3)", async () => {
    mount();
    await screen.findByRole("group", { name: /^完成影片/ });
    const hits = tile("GPU 在线").querySelectorAll(".spark-hit");
    expect(hits).toHaveLength(12);
    fireEvent.pointerEnter(hits[11], { clientX: 5, clientY: 5 });
    expect(screen.getByRole("tooltip")).toHaveTextContent("4 台在线");
    fireEvent.pointerEnter(hits[0], { clientX: 5, clientY: 5 }); // before device history began
    expect(screen.getByRole("tooltip")).toHaveTextContent("无记录");
  });
});

describe("数据分析 states", () => {
  it("503: 媒体流水线未运行 with the backend reason", async () => {
    mount(new HttpError("工作流未启用", 503));
    expect(await screen.findByText("媒体流水线未运行：工作流未启用")).toBeInTheDocument();
  });

  it("409: 数据分析只在主控上", async () => {
    mount(new HttpError("数据分析只在主控上", 409));
    expect(await screen.findByText("数据分析只在主控上")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: /^完成影片/ })).toBeNull();
  });

  it("other errors say so with the message", async () => {
    mount(new HttpError("/control/workflow/analytics → 500", 500));
    expect(await screen.findByText(/无法读取数据分析/)).toBeInTheDocument();
  });

  it("no archive yet: 「还没有归档完成的影片」 and no data start", async () => {
    mount({ ...FIXTURE, coverage: { ...FIXTURE.coverage, archives: null }, films: [], failures: [] });
    expect(await screen.findByText("还没有归档完成的影片")).toBeInTheDocument();
    expect(tile("完成影片")).toHaveTextContent("0部");
    expect(screen.queryByText(/数据自/)).toBeNull();
  });
});

describe("OD1 guard: no progress against a total, no remaining count, no forecast", () => {
  const BANNED: Array<[string, RegExp]> = [
    ["progress percentage", /已完成\s*\d+(\.\d+)?\s*%|完成\s*\d+(\.\d+)?\s*%/],
    ["进度", /进度/],
    ["剩余", /剩余/],
    ["预计", /预计/],
    ["ETA", /\bETA\b/i],
    ["完成度", /完成度/],
    ["completion time", /将于.*完成|预计完成/],
  ];
  const texts = () => {
    const el = document.body;
    const attrs = Array.from(el.querySelectorAll("*")).flatMap((n) => ["aria-label", "title", "label"].map((a) => n.getAttribute(a) ?? ""));
    return [el.textContent ?? "", ...attrs].join("\n");
  };
  const assertClean = () => { const all = texts(); for (const [name, re] of BANNED) expect(all, name).not.toMatch(re); };

  it("holds on every range, filter and view, and keeps the 「GPU 在线 N / M」 device-count tile", async () => {
    mount();
    await screen.findByRole("group", { name: /^完成影片/ });
    expect(tile("GPU 在线")).toHaveTextContent(/\d+ \/ \d+/);
    fireEvent.click(screen.getByRole("switch", { name: "对比上一周期" }));
    for (const label of ["24 小时", "7 天", "30 天", "90 天", "全部"]) {
      range(label);
      await waitFor(() => expect(document.querySelector(".an-sections")).not.toHaveClass("busy"));
      for (const view of ["表格", "图表"]) {
        for (const group of screen.getAllByRole("radiogroup", { name: /显示方式$/ })) fireEvent.click(within(group).getByRole("radio", { name: view }));
        const cols = document.querySelectorAll<SVGGElement>("[data-rove]");
        if (cols.length) act(() => cols[cols.length - 1].focus());
        assertClean();
      }
    }
    const select = screen.getByRole("combobox", { name: "机器" });
    for (const value of ["w-ctrl-0001", "w-gpu-ember"]) { fireEvent.change(select, { target: { value } }); assertClean(); }
    expect(screen.getAllByText(/共 \d+ 部/).length).toBeGreaterThan(0); // totals of what happened stay allowed
  });
});

describe("sticky filter bar (approved preview)", () => {
  it("sticks below the app's sticky app bar, and .mw-root clips without becoming a scroll container", () => {
    expect(ANALYTICS_CSS).toMatch(/\.mw \.an \.filterbar\{[^}]*position:sticky;top:64px/);
    expect(ANALYTICS_CSS).toMatch(/@media\(max-width:900px\)\{\.mw \.an \.filterbar\{position:static\}/);
    // overflow:hidden makes .mw-root the sticky scroll container and the bar never sticks; clip does not
    const root = MEDIA_CSS.match(/\.mw-root\{[^}]*\}/)![0];
    expect(root).toMatch(/overflow:clip/);
    expect(root.lastIndexOf("overflow:clip")).toBeGreaterThan(root.lastIndexOf("overflow:hidden"));
  });
});
