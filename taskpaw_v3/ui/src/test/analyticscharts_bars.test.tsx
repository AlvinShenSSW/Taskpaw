// 数据分析 chart primitives added for 各 GPU 机器 / 本机（主控）处理 / 耗时分布 (EPIC 2026-10-31 §8.2, §8.5;
// C4 #391 AC5): HBarChart, RangeChart, ShareBar, StripChart, plus MachineStateChip.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import ErrorOutlineIcon from "@mui/icons-material/ErrorOutline";
import { ChartTooltipProvider, HBarChart, RangeChart, ShareBar, StripChart } from "../components/media/analytics/charts";
import { MachineStateChip } from "../components/media/analytics/AnalyticsParts";

beforeEach(() => { vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(600); });
afterEach(() => { vi.restoreAllMocks(); });

const items = (root: ParentNode = document) => Array.from(root.querySelectorAll<SVGGElement>("[data-rove]"));
const wrap = (node: React.ReactNode) => render(<ChartTooltipProvider>{node}</ChartTooltipProvider>);

/** Shared keyboard contract: one tab stop, arrows / Home / End rove, the tooltip follows focus, Escape hides. */
function expectRoving(expectFirst: string, expectSecond: string) {
  const g = items();
  expect(g.map((x) => x.getAttribute("tabindex"))).toEqual(["0", ...g.slice(1).map(() => "-1")]);
  act(() => g[0].focus());
  expect(screen.getByRole("tooltip")).toHaveTextContent(expectFirst);
  fireEvent.keyDown(g[0], { key: "ArrowDown" });
  expect(g[1]).toHaveFocus();
  expect(screen.getByRole("tooltip")).toHaveTextContent(expectSecond);
  fireEvent.keyDown(g[1], { key: "End" });
  expect(g[g.length - 1]).toHaveFocus();
  fireEvent.keyDown(g[g.length - 1], { key: "Home" });
  expect(g[0]).toHaveFocus();
  fireEvent.keyDown(document, { key: "Escape" });
  expect(screen.queryByRole("tooltip")).toBeNull();
  act(() => g[0].blur());
}

describe("HBarChart", () => {
  const rows = [
    { key: "a", label: "主片库（演员目录）", segs: [{ v: 6, color: "var(--c1)" }], value: "6 部", tip: () => ({ title: "主片库（演员目录）", rows: [{ value: "6 部" }] }) },
    { key: "b", label: "分类目录", segs: [{ v: 2, color: "var(--c1)" }, { v: 1, color: "var(--c2)" }], value: "3 部", tip: () => ({ title: "分类目录", rows: [{ value: "3 部" }] }) },
    { key: "c", label: "手动目录", segs: [{ v: 0, color: "var(--c1)" }], value: "0 部", icon: ErrorOutlineIcon, iconColor: "var(--red)" },
  ];

  it("draws a labelled row per item with stacked segments, value text and an optional icon", () => {
    wrap(<HBarChart rows={rows} label="归档去向" />);
    const svg = screen.getByRole("img", { name: "归档去向" });
    expect(items()).toHaveLength(3);
    expect(items()[0].querySelector(".rlabel")?.textContent).toBe("主片库（演员目录）");
    expect(items()[0].querySelectorAll(".mk")).toHaveLength(1);
    expect(items()[1].querySelectorAll(".mk")).toHaveLength(2);
    expect(items()[2].querySelectorAll(".mk")).toHaveLength(0); // a zero row draws no bar
    expect(Array.from(svg.querySelectorAll(".dlabel")).map((t) => t.textContent)).toEqual(["6 部", "3 部", "0 部"]);
    expect(items()[2].querySelector("svg")).not.toBeNull(); // the icon
    // the longest bar spans the plot: label width 110, value width 64 → 426 px for 6
    const full = Number(items()[0].querySelector(".mk")!.getAttribute("d")!.match(/H([\d.]+)/)![1]);
    expect(full).toBeGreaterThan(400);
  });

  it("scales to a given max (a share chart tops out at 100)", () => {
    wrap(<HBarChart rows={[{ key: "x", label: "4K", segs: [{ v: 50, color: "var(--c1)" }], value: "50%" }]} label="标识占比" max={100} />);
    const end = Number(items()[0].querySelector(".mk")!.getAttribute("d")!.match(/H([\d.]+)/)![1]);
    expect(end).toBeLessThan(110 + 426 * 0.5 + 1);
  });

  it("roves by keyboard with the tooltip", () => {
    wrap(<HBarChart rows={rows.slice(0, 2)} label="归档去向" />);
    expectRoving("主片库（演员目录）", "分类目录");
  });
});

describe("RangeChart", () => {
  const st = { n: 4, min: 1, p25: 2, p50: 3, p75: 4, max: 8, avg: 3.5 };
  const rows = [
    { key: "queue", label: "排队等待", color: "var(--c-queue)", st },
    { key: "restore4k", label: "修复 4K", color: "var(--c1)", st: { ...st, n: 2, max: 12 } },
    { key: "none", label: "全程", color: "var(--c-total)", st: null },
  ];

  it("draws the min–max hairline, the p25–p75 band and the average dot per row; a row without data says so", () => {
    wrap(<RangeChart rows={rows} fmt={(v) => `${v} 小时`} label="按小时计的阶段耗时分布" />);
    expect(screen.getByRole("img", { name: "按小时计的阶段耗时分布" })).toBeInTheDocument();
    const [q, , none] = items();
    expect(q.querySelector("line.mk")).not.toBeNull();
    expect(q.querySelector("rect.mk")).not.toBeNull();
    expect(q.querySelector("circle.mk")).not.toBeNull();
    expect(q).toHaveTextContent("平均 3.5 小时");
    expect(q).toHaveTextContent("最长 8 小时");
    expect(none).toHaveTextContent("没有数据");
    expect(none.querySelector(".mk")).toBeNull();
    // one shared axis: the 12 h maximum of 修复 4K sets the scale (0 / 5 / 10 / 15) for 排队等待 too
    const ticks = Array.from(document.querySelectorAll("text.tick")).map((t) => t.textContent).filter((t) => t !== "没有数据");
    expect(ticks).toEqual(["0", "5", "10", "15"]);
  });

  it("tooltip lists 平均 / 中位数 / 中间一半 / 最短 / 最长 and roves", () => {
    wrap(<RangeChart rows={rows} fmt={(v) => `${v} 小时`} label="耗时" />);
    act(() => items()[0].focus());
    const tt = screen.getByRole("tooltip");
    expect(tt).toHaveTextContent("排队等待 · 4 部");
    for (const t of ["3.5 小时平均", "3 小时中位数", "2 小时 – 4 小时中间一半（p25–p75）", "1 小时最短", "8 小时最长"]) expect(tt).toHaveTextContent(t);
    act(() => items()[0].blur());
    expectRoving("排队等待", "修复 4K · 2 部");
  });
});

describe("ShareBar", () => {
  const segs = [
    { key: "queue", label: "排队等待", v: 6, color: "var(--c-queue)", tip: () => ({ title: "平均每部片", rows: [{ value: "60%", label: "排队等待" }] }) },
    { key: "restore", label: "修复", v: 4, color: "var(--c1)", tip: () => ({ title: "平均每部片", rows: [{ value: "40%", label: "修复" }] }) },
    { key: "asr", label: "ASR", v: 0, color: "var(--c3)" },
  ];

  it("splits the width by share, leaves zero stages out and roves", () => {
    wrap(<ShareBar segs={segs} label="各阶段平均耗时占比" />);
    expect(screen.getByRole("img", { name: "各阶段平均耗时占比" })).toBeInTheDocument();
    expect(items()).toHaveLength(2);
    const w0 = Number(items()[0].querySelector("rect.mk")!.getAttribute("width"));
    expect(w0).toBeCloseTo((600 - 2) * 0.6, 0);
    expectRoving("排队等待", "修复");
  });

  it("says so when there is nothing to share", () => {
    wrap(<ShareBar segs={segs.map((s) => ({ ...s, v: 0 }))} label="占比" />);
    expect(screen.getByText("这个时段没有数据")).toBeInTheDocument();
    expect(screen.queryByRole("img")).toBeNull();
  });
});

describe("StripChart", () => {
  const ticks = Array.from({ length: 10 }, (_, i) => ({ t: i * 10, label: `T${i}` }));
  const stripItems = [
    { key: "a", s: 40, e: 45, tip: () => ({ title: "翻译服务中断", rows: [{ value: "5 分钟", label: "中断时长" }] }) },
    { key: "b", s: 80, e: 80.2, tip: () => ({ title: "翻译暂停", rows: [{ value: "1 分钟", label: "中断时长" }] }) },
  ];

  it("places items on the time axis over a track, with an uncovered band and thinned ticks", () => {
    wrap(<StripChart start={0} end={100} items={stripItems} ticks={ticks} uncovUntil={30} trackFrom={30} trackTo={90} label="翻译服务中断时间轴" />);
    const svg = screen.getByRole("img", { name: "翻译服务中断时间轴" });
    expect(items()).toHaveLength(2);
    const x = (t: number) => (t / 100) * (600 - 4);
    expect(Number(items()[0].querySelector("rect.mk")!.getAttribute("x"))).toBeCloseTo(x(40), 3);
    expect(Number(items()[1].querySelector("rect.mk")!.getAttribute("width"))).toBe(3); // minimum visible width
    expect(Number(svg.querySelector(".uncov")!.getAttribute("width"))).toBeCloseTo(x(30), 3);
    const track = svg.querySelector(".strip-track")!;
    expect(Number(track.getAttribute("x"))).toBeCloseTo(x(30), 3);
    expect(Number(track.getAttribute("width"))).toBeCloseTo(x(90) - x(30), 3);
    // ticks closer than 58 px are dropped: 10 units = 59.6 px here, so all stay; at 300 px every other one
    expect(svg.querySelectorAll("text.tick")).toHaveLength(10);
    expectRoving("翻译服务中断", "翻译暂停");
  });

  it("has no band when the whole range is covered, and thins ticks on a narrow width", () => {
    vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(300);
    wrap(<StripChart start={0} end={100} items={[]} ticks={ticks} label="中断" />);
    const svg = screen.getByRole("img", { name: "中断" });
    expect(svg.querySelector(".uncov")).toBeNull();
    expect(svg.querySelectorAll("text.tick")).toHaveLength(5);
    expect(items()).toHaveLength(0);
  });
});

describe("MachineStateChip", () => {
  it("says the state in text with an icon, never colour alone", () => {
    const { rerender } = render(<MachineStateChip state="online" />);
    const cases: Array<[Parameters<typeof MachineStateChip>[0]["state"], string, string]> = [
      ["online", "在线", "green"], ["paused", "停领", "amber"], ["offline", "离线", "red"], ["new", "新加入", "blue"],
      ["retired", "已退休", ""], ["unknown", "确认中", ""], ["off", "未启用", ""],
    ];
    for (const [state, text, tone] of cases) {
      rerender(<MachineStateChip state={state} />);
      const chip = screen.getByText(text).closest(".chip")!;
      expect(chip.querySelector("svg")).not.toBeNull();
      if (tone) expect(chip).toHaveClass(tone);
    }
  });
});
