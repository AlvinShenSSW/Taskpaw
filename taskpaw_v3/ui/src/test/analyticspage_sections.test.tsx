// 数据分析 sections 各 GPU 机器, 本机（主控）处理 and 耗时分布 on the rendered page (EPIC 2026-10-31 §8.4
// items 3–5, §8.6; C4 #391 AC1–AC4, AC6). Same fixture, fixed clock and Asia/Shanghai zone as
// analyticspage.test.tsx, so local-day buckets are deterministic.
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api, type WorkflowAnalytics } from "../api";
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
const region = (name: string) => screen.getByRole("region", { name });
const machines = () => region("各 GPU 机器");
const ctrl = () => region("本机（主控）处理");
const durations = () => region("耗时分布");
const range = async (label: string) => {
  fireEvent.click(within(screen.getByRole("radiogroup", { name: "时段" })).getByRole("radio", { name: label }));
  await waitFor(() => expect(document.querySelector(".an-sections")).not.toHaveClass("busy"));
};
const pick = (value: string) => fireEvent.change(screen.getByRole("combobox", { name: "机器" }), { target: { value } });
const view = (panel: HTMLElement, label: "图表" | "表格") => fireEvent.click(within(panel).getByRole("radio", { name: label }));
const machineTable = () => within(machines()).getAllByRole("table")[0];
const rowOf = (name: string) => within(machineTable()).getByRole("row", { name: `只看 ${name}` });

describe("各 GPU 机器 (AC1, AC2)", () => {
  it("groups machines by model with the same-model average that leaves 新加入 out", async () => {
    await mount();
    const groups = within(machineTable()).getAllByRole("columnheader").filter((th) => th.getAttribute("scope") === "colgroup");
    expect(groups.map((g) => g.textContent)).toEqual([
      // Birch 10, Cedar 4, Dune 6 → 6.7; Ember (新加入) is not averaged in
      "RTX 5060 Ti 16GB × 4同型号平均每台 6.7 部",
      "RTX 5070 Ti 16GB × 1只有一台，无同型号比较",
    ]);
    expect(machineTable().querySelectorAll(".avg-mk")).toHaveLength(4);
    expect(within(machines()).getByText("同型号平均（每台）")).toBeInTheDocument();
    expect(within(machineTable()).queryByRole("row", { name: "只看 本机" })).toBeNull(); // 本机 did not restore in 7 天
    expect(within(machineTable()).queryByRole("row", { name: /Fern/ })).toBeNull(); // retired, no data in range
  });

  it("state chips with notes: 离线 / 停领 with the reason / 新加入 with its join time", async () => {
    await mount();
    expect(rowOf("Birch")).toHaveTextContent("离线10-31 09:10 起");
    expect(rowOf("Cedar")).toHaveTextContent("停领主控停领 · 10-31 11:00 起");
    expect(rowOf("Ember")).toHaveTextContent("新加入10-30 17:00 加入");
    expect(rowOf("Dune")).toHaveTextContent("在线");
  });

  it("sortable columns announce aria-sort; rows re-order inside their model group", async () => {
    await mount();
    const header = (label: string) => within(machineTable()).getByRole("button", { name: label }).closest("th")!;
    expect(machineTable().querySelectorAll("th[aria-sort]")).toHaveLength(0);
    const names = () => within(machineTable()).getAllByRole("row").map((r) => r.getAttribute("aria-label")).filter(Boolean);
    expect(names()).toEqual(["只看 Birch", "只看 Cedar", "只看 Dune", "只看 Ember", "只看 Aurora"]);
    fireEvent.click(within(header("修复影片")).getByRole("button"));
    expect(header("修复影片")).toHaveAttribute("aria-sort", "descending");
    expect(names()).toEqual(["只看 Birch", "只看 Dune", "只看 Cedar", "只看 Ember", "只看 Aurora"]);
    fireEvent.click(within(header("修复影片")).getByRole("button"));
    expect(header("修复影片")).toHaveAttribute("aria-sort", "ascending");
    expect(names()).toEqual(["只看 Ember", "只看 Cedar", "只看 Dune", "只看 Birch", "只看 Aurora"]);
    fireEvent.click(within(header("机器")).getByRole("button"));
    expect(header("机器")).toHaveAttribute("aria-sort", "ascending");
    expect(header("修复影片")).not.toHaveAttribute("aria-sort");
  });

  it("平均功率 and 用电 count every restore attempt of the machine, failed ones on other films included", async () => {
    await mount();
    const dune = WEEK.flatMap((f) => f.restore_wh).filter(([w]) => w === "w-gpu-dune");
    const wh = dune.reduce((a, e) => a + e[1], 0), secs = dune.reduce((a, e) => a + e[2], 0);
    expect(dune.length).toBeGreaterThan(WEEK.filter((f) => f.worker === "w-gpu-dune").length); // it failed on others' films
    expect(rowOf("Dune")).toHaveTextContent(`${Math.round((wh * 3600) / secs)} W`);
    expect(rowOf("Dune")).toHaveTextContent(`${(wh / 1000).toFixed(2)} kWh`);
    // 失败: severity fail only (Dune's warnings in range do not count)
    const duneFails = FIXTURE.failures.filter((f) => in7d(f.at) && f.machine === "w-gpu-dune" && f.severity === "fail").length;
    expect(duneFails).toBe(0);
    expect(within(rowOf("Dune")).getAllByRole("cell")[8]).toHaveTextContent(/^0$/);
    expect(within(rowOf("Birch")).getAllByRole("cell")[8]).toHaveTextContent(/^3$/);
  });

  it("a row click filters to that machine; 查看全部 GPU 机器 resets", async () => {
    await mount();
    fireEvent.click(rowOf("Dune"));
    const dune = WEEK.filter((f) => f.worker === "w-gpu-dune").length;
    expect(screen.getByRole("group", { name: /^完成影片/ })).toHaveTextContent(`${dune}部`);
    expect(screen.getByRole("combobox", { name: "机器" })).toHaveValue("w-gpu-dune");
    expect(within(machineTable()).getAllByRole("row", { name: /^只看/ })).toHaveLength(1);
    fireEvent.click(within(machines()).getByRole("button", { name: "查看全部 GPU 机器" }));
    expect(screen.getByRole("combobox", { name: "机器" })).toHaveValue("all");
    fireEvent.keyDown(rowOf("Cedar"), { key: "Enter" });
    expect(screen.getByRole("combobox", { name: "机器" })).toHaveValue("w-gpu-cedar");
  });

  it("the 本机 filter shows the preview notice; 本机 joins the table only in a range where it restored", async () => {
    await mount();
    pick("w-ctrl-0001");
    expect(within(machines()).getByText(/已筛选「本机」。主控只做 ASR、翻译、刮削和归档，不做修复，这里没有 GPU 数据。/)).toBeInTheDocument();
    expect(within(machines()).queryByRole("table")).toBeNull();
    fireEvent.click(within(machines()).getByRole("button", { name: "查看全部 GPU 机器" }));
    await range("30 天");
    expect(rowOf("本机")).toHaveTextContent("RTX 5060 8GB");
    expect(within(machineTable()).getAllByRole("columnheader").some((th) => th.textContent?.startsWith("RTX 5060 8GB × 1"))).toBe(true);
  });

  it("各机型实测产能 keeps 新加入, and says 「未扣除离线时间」 unless device history covers the range", async () => {
    await mount();
    const tp = () => within(machines()).getByRole("table", { name: "各机型实测产能" });
    expect(within(machines()).getByText(/未扣除离线时间/)).toBeInTheDocument();
    expect(within(tp()).getByRole("row", { name: /RTX 5060 Ti 16GB/ })).toHaveTextContent("4（含 1 台新加入）");
    await range("24 小时"); // devices recorded since 10-28: covers the last 24 hours
    expect(within(machines()).queryByText(/未扣除离线时间/)).toBeNull();
    expect(within(machines()).getByText(/按在线时长折算/)).toBeInTheDocument();
  });

  it("small multiples: one per machine, all on one y-axis", async () => {
    await mount();
    const minis = within(machines()).getAllByRole("img", { name: /每个时段修复量$/ });
    expect(minis).toHaveLength(5);
    const top = (svg: HTMLElement) => Array.from(svg.querySelectorAll("text.tick")).filter((t) => t.getAttribute("text-anchor") === "end").map((t) => t.textContent).at(-1);
    expect(new Set(minis.map(top)).size).toBe(1);
    expect(within(machines()).getByText(/所有小图用同一个纵轴（最高 \d+ 部）/)).toBeInTheDocument();
  });

  it("table view lists every number, the throughput table and per-bucket counts", async () => {
    await mount();
    view(machines(), "表格");
    const t = within(machines()).getByRole("table", { name: "各 GPU 机器" });
    expect(within(t).getAllByRole("columnheader").map((c) => c.textContent)).toEqual([
      "机型", "机器", "状态", "4K", "1080P", "未知分辨率", "合计", "同型号平均", "平均修复 4K", "平均修复 1080P", "最短", "最长", "平均功率", "用电 kWh", "失败", "修复效率",
    ]);
    expect(within(t).getAllByRole("row")).toHaveLength(6);
    expect(within(t).getByRole("row", { name: /Cedar/ })).toHaveTextContent("停领（主控停领 · 10-31 11:00 起）");
    expect(within(machines()).getByRole("table", { name: "各机型实测产能" })).toBeInTheDocument();
    const per = within(machines()).getByRole("table", { name: "每台机器每个时段的修复量" });
    expect(within(per).getAllByRole("row")).toHaveLength(8);
  });
});

describe("本机（主控）处理 (AC3, AC6)", () => {
  it("ASR / 翻译 / 刮削 / 归档 tiles with the line total, its coverage note and the two subtitle chips", async () => {
    await mount();
    const translated = WEEK.filter((f) => f.translated).length;
    const cues = WEEK.reduce((a, f) => a + (f.cues ?? 0), 0);
    const tile = (label: string) => within(ctrl()).getByRole("group", { name: label });
    expect(tile("翻译")).toHaveTextContent(`${translated}片`);
    expect(tile("翻译")).toHaveTextContent(`共 ${(Math.round(cues / 1000) / 10).toFixed(1)} 万行字幕`);
    expect(tile("翻译")).toHaveTextContent("自 10-28 起统计行数");
    const reused = WEEK.filter((f) => (f.subtitles.reused_zh ?? 0) > 0).length;
    const silent = WEEK.filter((f) => (f.subtitles.no_speech ?? 0) > 0).length;
    expect(within(tile("翻译")).getByText(`${reused} 片已有中文字幕`)).toHaveClass("chip");
    expect(within(tile("翻译")).getByText(`${silent} 片无语音`)).toHaveClass("chip");
    // 无需翻译 films are not all 无语音 (most already had Chinese subtitles)
    expect(WEEK.filter((f) => !f.translated).length).not.toBe(silent);
    expect(ctrl()).not.toHaveTextContent(/deepseek|100%/); // no model-share chip (D8)
    expect(tile("ASR")).toHaveTextContent(`${WEEK.filter((f) => f.asr_s != null).length}片`);
    expect(tile("刮削")).toHaveTextContent("平均");
    expect(tile("归档")).toHaveTextContent(`${WEEK.length}片`);
    await range("24 小时"); // line counts cover the whole range: no note
    expect(within(ctrl()).queryByText(/统计行数/)).toBeNull();
  });

  it("归档去向 bars for the five destinations", async () => {
    await mount();
    const chart = within(ctrl()).getByRole("img", { name: "归档去向" });
    const rows = Array.from(chart.querySelectorAll(".rlabel")).map((t) => t.textContent);
    expect(rows).toEqual(["主片库（演员目录）", "分类目录", "新建演员", "手动目录", "第二目标（额外复制）"]);
    const performer = WEEK.filter((f) => f.target === "performer").length;
    expect(chart.querySelectorAll(".dlabel")[0].textContent).toBe(`${performer} 部`);
  });

  it("翻译服务中断 strip; the uncovered band and 「翻译服务中断记录从 MM-DD 起」 when the range starts earlier", async () => {
    await mount();
    const strip = within(ctrl()).getByRole("img", { name: "翻译服务中断时间轴" });
    expect(strip.querySelectorAll("[data-rove]")).toHaveLength(3);
    expect(within(ctrl()).getByText(/^中断 3 次 · 共 /)).toBeInTheDocument();
    expect(strip.querySelector(".uncov")).toBeNull();
    expect(within(ctrl()).queryByText(/翻译服务中断记录从/)).toBeNull();
    act(() => (strip.querySelectorAll("[data-rove]")[2] as SVGGElement).focus());
    expect(screen.getByRole("tooltip")).toHaveTextContent("尚未恢复");
    act(() => (strip.querySelectorAll("[data-rove]")[2] as SVGGElement).blur());
    await range("90 天");
    expect(within(ctrl()).getByText("翻译服务中断记录从 10-02 起")).toBeInTheDocument();
    expect(within(ctrl()).getByRole("img", { name: "翻译服务中断时间轴" }).querySelector(".uncov")).not.toBeNull();
  });

  it("a GPU machine filter shows 「翻译服务属于本机」 instead of the strip", async () => {
    await mount();
    pick("w-gpu-dune");
    expect(within(ctrl()).getByText("翻译服务属于本机；选择「全部 GPU 机器」或「本机」时显示。")).toBeInTheDocument();
    expect(within(ctrl()).queryByRole("img", { name: "翻译服务中断时间轴" })).toBeNull();
    expect(ctrl()).toHaveTextContent("Dune 修复的影片在本机完成的后处理");
    pick("w-ctrl-0001");
    expect(within(ctrl()).getByRole("img", { name: "翻译服务中断时间轴" })).toBeInTheDocument();
  });

  it("table view: stages, subtitles, destinations and outages", async () => {
    await mount();
    view(ctrl(), "表格");
    expect(within(ctrl()).getAllByRole("row", { name: /^(ASR|翻译|刮削|归档)/ }).length).toBeGreaterThanOrEqual(4);
    expect(within(ctrl()).getByRole("table", { name: "归档去向" })).toBeInTheDocument();
    const out = within(ctrl()).getByRole("table", { name: "翻译服务中断" });
    expect(within(out).getAllByRole("row")).toHaveLength(4);
    expect(out).toHaveTextContent("尚未恢复");
    pick("w-gpu-dune");
    expect(within(ctrl()).queryByRole("table", { name: "翻译服务中断" })).toBeNull();
  });
});

describe("耗时分布 (AC4)", () => {
  it("hour and minute strips with a stats tooltip, and the time-share bar", async () => {
    await mount();
    const hoursChart = within(durations()).getByRole("img", { name: "按小时计的阶段耗时分布" });
    const labels = Array.from(hoursChart.querySelectorAll(".rlabel")).map((t) => t.textContent);
    expect(labels).toEqual(["排队等待", "修复 4K", "修复 1080P", "全程"]);
    const minutes = within(durations()).getByRole("img", { name: "按分钟计的阶段耗时分布" });
    expect(Array.from(minutes.querySelectorAll(".rlabel")).map((t) => t.textContent)).toEqual(["ASR", "翻译", "刮削", "归档"]);
    const queued = WEEK.filter((f) => f.queue_s != null).length;
    act(() => (hoursChart.querySelector("[data-rove]") as SVGGElement).focus());
    expect(screen.getByRole("tooltip")).toHaveTextContent(`排队等待 · ${queued} 部`);
    expect(screen.getByRole("tooltip")).toHaveTextContent("中位数");
    act(() => (hoursChart.querySelector("[data-rove]") as SVGGElement).blur());
    const share = within(durations()).getByRole("img", { name: "各阶段平均耗时占比" });
    expect(share.querySelectorAll("[data-rove]").length).toBe(6);
    expect(within(durations()).getByText("一部片的时间都花在哪")).toBeInTheDocument();
  });

  it("the 4K filter drops the 1080P strip", async () => {
    await mount();
    fireEvent.click(within(screen.getByRole("radiogroup", { name: "分辨率" })).getByRole("radio", { name: "4K" }));
    const labels = Array.from(within(durations()).getByRole("img", { name: "按小时计的阶段耗时分布" }).querySelectorAll(".rlabel")).map((t) => t.textContent);
    expect(labels).toEqual(["排队等待", "修复 4K", "全程"]);
  });

  it("table view: min / p25 / median / p75 / max / avg per stage, and the share table", async () => {
    await mount();
    view(durations(), "表格");
    const t = within(durations()).getByRole("table", { name: "耗时分布" });
    expect(within(t).getAllByRole("columnheader").map((c) => c.textContent)).toEqual(["阶段", "影片", "最短", "p25", "中位数", "p75", "最长", "平均"]);
    expect(within(t).getAllByRole("row")).toHaveLength(9);
    const share = within(durations()).getByRole("table", { name: "时间占比" });
    expect(within(share).getAllByRole("row")).toHaveLength(7);
  });
});

describe("no archive yet (§8.6)", () => {
  it("each C4 panel says 「还没有归档完成的影片」", async () => {
    await mount({ ...FIXTURE, coverage: { ...FIXTURE.coverage, archives: null }, films: [], failures: [] });
    for (const panel of [machines(), ctrl(), durations()]) expect(within(panel).getByText("还没有归档完成的影片")).toBeInTheDocument();
  });
});
