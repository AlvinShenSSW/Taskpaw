// LaneChart, the 设备在线 timeline primitive (EPIC 2026-10-31 §8.2, §8.5; C5 #392 AC5): one row per lane with
// its label and right-hand value, coloured state segments on a shared time axis, a summary item per lane and
// one item per segment in the roving order, the tooltip on focus, and a text placeholder for an empty lane.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { ChartTooltipProvider, LaneChart, type Lane } from "../components/media/analytics/charts";

beforeEach(() => { vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(600); });
afterEach(() => { vi.restoreAllMocks(); });

const items = () => Array.from(document.querySelectorAll<SVGGElement>("[data-rove]"));
const H = 3_600_000, T0 = Date.UTC(2026, 9, 30), T1 = T0 + 24 * H;
const lanes: Lane[] = [
  {
    key: "a", label: "Aurora", from: T0, to: T1, value: "在线 96%",
    tip: () => ({ title: "Aurora", rows: [{ value: "96%", label: "在线率" }] }),
    segs: [
      { key: "a1", s: T0, e: T0 + 12 * H, color: "var(--lane-on)", tip: () => ({ title: "Aurora", rows: [{ value: "在线" }] }) },
      { key: "a2", s: T0 + 12 * H, e: T0 + 13 * H, color: "var(--st-red)", tip: () => ({ title: "Aurora", rows: [{ value: "离线" }] }) },
      { key: "a3", s: T0 + 13 * H, e: T1, color: "var(--lane-on)" },
    ],
  },
  { key: "b", label: "Ember", from: T0 + 18 * H, to: T1, value: "在线 100%", segs: [{ key: "b1", s: T0 + 18 * H, e: T1, color: "var(--lane-on)" }] },
  { key: "c", label: "Fern", from: T1, to: T1, segs: [], empty: "10-31 加入" },
];
const ticks = [0, 6, 12, 18].map((h) => ({ t: T0 + h * H, label: `${h}:00` }));
const wrap = () => render(<ChartTooltipProvider><LaneChart start={T0} end={T1} lanes={lanes} ticks={ticks} label="设备在线时间轴" /></ChartTooltipProvider>);

describe("LaneChart", () => {
  it("draws one labelled row per lane with its value, segments in their colours and a placeholder for an empty lane", () => {
    wrap();
    const svg = screen.getByRole("img", { name: "设备在线时间轴" });
    expect(Array.from(svg.querySelectorAll(".rlabel")).map((t) => t.textContent)).toEqual(["Aurora", "Ember", "Fern"]);
    expect(Array.from(svg.querySelectorAll(".vt1")).map((t) => t.textContent)).toEqual(["在线 96%", "在线 100%"]);
    expect(svg).toHaveTextContent("10-31 加入");
    const marks = Array.from(svg.querySelectorAll<SVGRectElement>("rect.mk"));
    expect(marks.map((m) => m.style.fill)).toEqual(["var(--lane-on)", "var(--st-red)", "var(--lane-on)", "var(--lane-on)"]);
    // label 132 px + value 84 px → a 384 px plot; Ember starts 18 h into the day
    expect(Number(marks[3].getAttribute("x"))).toBeCloseTo(132 + 384 * 0.75, 0);
    expect(Number(marks[1].getAttribute("width"))).toBeCloseTo(384 / 24, 0);
    expect(svg.querySelectorAll(".tick").length).toBeGreaterThanOrEqual(4);
  });

  it("roves over each lane's summary then its segments; the tooltip follows focus and Escape hides it", () => {
    wrap();
    const g = items();
    expect(g).toHaveLength(6); // Aurora: summary + 3 segments; Ember: summary + 1; Fern (empty): none
    expect(g.map((x) => x.getAttribute("tabindex"))).toEqual(["0", ...g.slice(1).map(() => "-1")]);
    act(() => g[0].focus());
    expect(screen.getByRole("tooltip")).toHaveTextContent("在线率");
    fireEvent.keyDown(g[0], { key: "ArrowRight" });
    fireEvent.keyDown(g[1], { key: "ArrowRight" });
    expect(g[2]).toHaveFocus();
    expect(screen.getByRole("tooltip")).toHaveTextContent("离线");
    fireEvent.keyDown(g[2], { key: "End" });
    expect(g[g.length - 1]).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByRole("tooltip")).toBeNull();
  });
});
