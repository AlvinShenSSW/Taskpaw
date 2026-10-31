// 数据分析 chart primitives and panel parts (EPIC 2026-10-31 §8.2, §8.5; C3 #390 AC8).
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { niceTicks, useChartWidth } from "../components/media/analytics/chartKit";
import { ChartTooltipProvider, ColumnChart, Sparkline } from "../components/media/analytics/charts";
import { AnalyticsPanel, DataTable, EstimateChip, KpiTile } from "../components/media/analytics/AnalyticsParts";

const SERIES = [
  { key: "4K", label: "4K", color: "var(--c1)", vals: [2, 0, 3, 1] },
  { key: "1080p", label: "1080P", color: "var(--c2)", vals: [1, 0, 2, 4] },
];
const tip = (i: number) => ({
  title: `第 ${i + 1} 天`,
  rows: [...SERIES.map((s) => ({ color: s.color, value: `${s.vals[i]} 部`, label: s.label })), { value: `${SERIES[0].vals[i] + SERIES[1].vals[i]} 部`, label: "合计" }],
});

beforeEach(() => { vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(640); });
afterEach(() => { vi.restoreAllMocks(); vi.useRealTimers(); vi.unstubAllGlobals(); });

const chart = (extra: Partial<Parameters<typeof ColumnChart>[0]> = {}) => render(
  <ChartTooltipProvider>
    <ColumnChart n={4} series={SERIES} tick={(i) => `D${i + 1}`} tip={tip} labelMax label="产量趋势：每个时段完成的影片数" {...extra} />
  </ChartTooltipProvider>,
);
const items = () => Array.from(document.querySelectorAll<SVGGElement>("[data-rove]"));

describe("ColumnChart", () => {
  it("draws one stacked column per bucket, axis ticks, bucket labels and the max label", () => {
    chart();
    const svg = screen.getByRole("img", { name: "产量趋势：每个时段完成的影片数" });
    expect(items()).toHaveLength(4);
    // bucket 2 is empty: no segment; bucket 4 has two segments
    expect(items()[1].querySelectorAll(".mk")).toHaveLength(0);
    expect(items()[3].querySelectorAll(".mk")).toHaveLength(2);
    expect(within(svg as unknown as HTMLElement).getByText("D1")).toBeInTheDocument();
    expect(svg.querySelector(".dlabel")?.textContent).toBe("5");
    expect(niceTicks(5, 4)).toEqual([0, 2, 4, 6]);
  });

  it("only the first column is in the tab order; arrows, Home and End rove; the tooltip follows focus; Escape hides it", () => {
    chart();
    expect(items().map((g) => g.getAttribute("tabindex"))).toEqual(["0", "-1", "-1", "-1"]);
    act(() => items()[0].focus());
    const tt = screen.getByRole("tooltip");
    expect(tt).toHaveTextContent("第 1 天");
    expect(tt).toHaveTextContent("3 部合计");
    fireEvent.keyDown(items()[0], { key: "ArrowRight" });
    expect(items()[1]).toHaveFocus();
    expect(items().map((g) => g.getAttribute("tabindex"))).toEqual(["-1", "0", "-1", "-1"]);
    expect(screen.getByRole("tooltip")).toHaveTextContent("第 2 天");
    fireEvent.keyDown(items()[1], { key: "End" });
    expect(items()[3]).toHaveFocus();
    fireEvent.keyDown(items()[3], { key: "ArrowRight" });
    expect(items()[3]).toHaveFocus();
    fireEvent.keyDown(items()[3], { key: "Home" });
    expect(items()[0]).toHaveFocus();
    fireEvent.keyDown(items()[0], { key: "ArrowLeft" });
    expect(items()[0]).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).toBeNull();
    act(() => items()[0].blur());
  });

  it("shows the tooltip on pointer hover and hides it on leave", () => {
    chart();
    fireEvent.pointerEnter(items()[2], { clientX: 100, clientY: 80 });
    expect(screen.getByRole("tooltip")).toHaveTextContent("第 3 天");
    fireEvent.pointerLeave(items()[2]);
    expect(screen.queryByRole("tooltip")).toBeNull();
  });

  it("draws the previous-period line, the uncovered band with 无记录 and nothing at zero width", () => {
    const { unmount } = chart({ prev: [1, null, 2, 2], uncov: 0.5 });
    expect(document.querySelector(".prev-line")?.getAttribute("d")).toMatch(/^M[\d.]+,[\d.]+M[\d.]+,[\d.]+L/);
    expect(document.querySelector(".uncov")).not.toBeNull();
    expect(screen.getByText("无记录")).toBeInTheDocument();
    unmount();
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(0);
    chart();
    expect(screen.queryByRole("img")).toBeNull();
  });
});

describe("Sparkline", () => {
  it("draws the line with the latest point marked and a pointer tooltip per slice", () => {
    render(<ChartTooltipProvider><Sparkline vals={[1, null, 3, 2]} labels={["a", "b", "c", "d"]} fmt={(v) => `${v} 部`} /></ChartTooltipProvider>);
    const svg = screen.getByRole("img", { name: "近期趋势" });
    expect(svg.querySelector(".spark-line")?.getAttribute("d")).toContain("M");
    expect(svg.querySelectorAll("circle")).toHaveLength(1);
    const hits = svg.querySelectorAll(".spark-hit");
    expect(hits).toHaveLength(4);
    fireEvent.pointerEnter(hits[1], { clientX: 5, clientY: 5 });
    expect(screen.getByRole("tooltip")).toHaveTextContent("无记录");
    fireEvent.pointerEnter(hits[3], { clientX: 5, clientY: 5 });
    expect(screen.getByRole("tooltip")).toHaveTextContent("2 部");
  });

  it("says so when there are not enough records", () => {
    render(<ChartTooltipProvider><Sparkline vals={[null, null]} labels={["a", "b"]} fmt={String} /></ChartTooltipProvider>);
    expect(screen.getByText("没有足够记录")).toBeInTheDocument();
  });
});

describe("useChartWidth", () => {
  it("measures on mount and follows resizes after a 120 ms debounce", () => {
    vi.useFakeTimers();
    let notify: (w: number) => void = () => {};
    vi.stubGlobal("ResizeObserver", class {
      constructor(cb: ResizeObserverCallback) { notify = (w) => cb([{ contentRect: { width: w } } as ResizeObserverEntry], this as unknown as ResizeObserver); }
      observe() {}
      disconnect() {}
    });
    function Probe() { const [ref, w] = useChartWidth(); return <div ref={ref}>宽 {w}</div>; }
    render(<Probe />);
    expect(screen.getByText("宽 640")).toBeInTheDocument();
    act(() => notify(500));
    act(() => { vi.advanceTimersByTime(100); });
    expect(screen.getByText("宽 640")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(30); });
    expect(screen.getByText("宽 500")).toBeInTheDocument();
  });
});

describe("panel parts", () => {
  it("AnalyticsPanel toggles between 图表 and 表格 as a radio group", () => {
    render(<AnalyticsPanel id="prod" title="产量趋势" desc="说明" chart={() => <p>图</p>} table={() => <p>表</p>} />);
    const group = screen.getByRole("radiogroup", { name: "产量趋势 显示方式" });
    expect(screen.getByRole("region", { name: "产量趋势" })).toBeInTheDocument();
    expect(screen.getByText("图")).toBeInTheDocument();
    fireEvent.click(within(group).getByRole("radio", { name: "表格" }));
    expect(within(group).getByRole("radio", { name: "表格" })).toHaveAttribute("aria-checked", "true");
    expect(screen.getByText("表")).toBeInTheDocument();
    expect(screen.queryByText("图")).toBeNull();
  });

  it("DataTable has a caption, numeric columns and an empty row", () => {
    const { rerender } = render(<DataTable caption="产量趋势" cols={[{ label: "时段" }, { label: "合计", num: true }]} rows={[["10-30", 4]]} />);
    const table = screen.getByRole("table", { name: "产量趋势" });
    expect(within(table).getByRole("columnheader", { name: "合计" })).toHaveClass("num");
    expect(within(table).getByRole("cell", { name: "4" })).toHaveClass("num");
    rerender(<DataTable caption="产量趋势" cols={[{ label: "时段" }]} rows={[]} />);
    expect(screen.getByText("这个时段没有数据")).toBeInTheDocument();
  });

  it("KpiTile shows value, unit, delta, note and an optional tag", () => {
    render(<ChartTooltipProvider>
      <KpiTile label="总用电" tag={<EstimateChip />} value="12.5" unit="kWh" note="每片平均 0.60 kWh"
        delta={{ tone: "flat", dir: "up", text: "+12%", vs: true }} spark={{ vals: [1, 2], labels: ["a", "b"], fmt: String }} />
    </ChartTooltipProvider>);
    const tile = screen.getByText("总用电").closest(".kpi") as HTMLElement;
    expect(within(tile).getByText("估算")).toBeInTheDocument();
    expect(tile).toHaveTextContent("12.5kWh");
    expect(tile).toHaveTextContent("+12%较上一周期");
    expect(within(tile).getByText("每片平均 0.60 kWh")).toBeInTheDocument();
    expect(within(tile).getByRole("img", { name: "近期趋势" })).toBeInTheDocument();
  });
});
