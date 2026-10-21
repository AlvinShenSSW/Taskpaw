// U1c: the running 修复 step always shows it is alive — 已用 ticking from the stage start, the
// phase (马赛克扫描 → BasicVSR++ → the Jasna monitor's LtxProgress) and the latest console line.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import type { ReactNode } from "react";
import { MonitorMetrics } from "../components/MonitorMetrics";
import { elapsedClock, filmStatus, heartbeatText, restoreLive, restoreStep, type WorkItem } from "../components/media/mediaModel";
import { StageBlock } from "../components/media/parts";
import { RestoreLivePanel } from "../components/media/RestoreLivePanel";
import { setLang } from "../i18n";
import { theme } from "../theme";
import {
  BASIC_LINE, LTX_LINE, RESTORE_PHASES, restoreSteps, row, SCAN_LINE, type RestorePhaseKey,
} from "./fixtures/mediaWorkspace";

const wrap = (ui: ReactNode) => render(<ThemeProvider theme={theme}><div className="mw">{ui}</div></ThemeProvider>);
const film = (phase: RestorePhaseKey) =>
  row({ film_id: "abf-401", title: "浮光之间", stage: "restore", status: "running", steps: restoreSteps(phase) });
const item = (phase: RestorePhaseKey): WorkItem => {
  const r = film(phase);
  return { kind: "film", id: r.film_id, row: r, status: filmStatus(r) };
};
const step = (phase: RestorePhaseKey) => restoreStep(film(phase))!;

beforeEach(() => {
  setLang("zh-CN");
  vi.useFakeTimers({ toFake: ["setInterval", "clearInterval", "Date"] });
  vi.setSystemTime(new Date("2026-10-22T08:01:05Z")); // 65 s after the stage start
});
afterEach(() => vi.useRealTimers());

describe("restoreLive", () => {
  it.each([
    ["starting", "starting"], ["working", "working"], ["scan", "scan"], ["split", "split"], ["basic", "basic"], ["ltx", "ltx"],
  ] as const)("reads the %s fixture as the %s phase", (phase, expected) => {
    expect(restoreLive(step(phase))?.phase).toBe(expected);
  });

  it("is only for a running restore step and never invents a start", () => {
    expect(restoreLive({ ...step("scan"), state: "pending" })).toBeNull();
    expect(restoreLive({ ...step("scan"), key: "asr" })).toBeNull();
    const live = restoreLive({ ...step("scan"), started_at: "not a time" })!;
    expect(live.startedAt).toBeNull();
    expect(heartbeatText(live, Date.now())).toBe("修复中");
  });

  it("strips terminal controls from the console line", () => {
    expect(restoreLive({ ...step("working"), console: "\u001b[33mloading\u001b[0m" })?.console).toBe("loading");
  });

  it("formats 已用 as mm:ss, then h:mm:ss", () => {
    expect(elapsedClock(65_000)).toBe("01:05");
    expect(elapsedClock(-5_000)).toBe("00:00");
    expect(elapsedClock(3_725_000)).toBe("1:02:05");
  });

  it("maps a running restore without an LTX stage onto 修复中", () => {
    expect(item("scan").status).toBe("restore");
    expect(item("basic").status).toBe("restore");
    expect(item("ltx").status).toBe("encode");
  });
});

describe("queue card / overview stage block", () => {
  it("before any line: 正在启动 Jasna（加载模型）, ticking client-side", () => {
    wrap(<StageBlock item={item("starting")} />);
    expect(screen.getByTestId("restore-heartbeat")).toHaveTextContent("正在启动 Jasna（加载模型） · 已用 01:05");
    expect(screen.getByText("等待 Jasna 的第一行输出")).toBeInTheDocument();
    act(() => { vi.advanceTimersByTime(2000); });
    expect(screen.getByTestId("restore-heartbeat")).toHaveTextContent("已用 01:07");
  });

  it("console lines without a bar: 修复中 and the latest line", () => {
    wrap(<StageBlock item={item("working")} />);
    expect(screen.getByTestId("restore-heartbeat")).toHaveTextContent("修复中 · 已用 01:05");
    expect(screen.getByText("Loading LTX weights (fast) …")).toBeInTheDocument();
  });

  it("the mosaic scan: percent, bar, samples and its remaining time", () => {
    wrap(<StageBlock item={item("scan")} />);
    expect(screen.getByText("扫描马赛克")).toBeInTheDocument();
    const bar = screen.getByRole("progressbar", { name: "浮光之间 扫描马赛克 本阶段进度" });
    expect(bar).toHaveAttribute("aria-valuenow", "12");
    expect(screen.getByText("600 / 5000 采样")).toBeInTheDocument();
    expect(screen.getByText("剩余 04:53")).toBeInTheDocument();
    expect(screen.getByTestId("restore-heartbeat")).toHaveTextContent("修复中 · 已用 01:05");
  });

  it("the split plan is a phase of its own", () => {
    wrap(<StageBlock item={item("split")} />);
    expect(screen.getByText("已完成自动分段，正在切分片段")).toBeInTheDocument();
  });

  it("BasicVSR++ spans: their percent, frames and speed", () => {
    wrap(<StageBlock item={item("basic")} />);
    const bar = screen.getByRole("progressbar", { name: "浮光之间 BasicVSR++ 修复 本片段进度" });
    expect(bar).toHaveAttribute("aria-valuenow", "15");
    expect(screen.getByText("36703 / 244157 帧")).toBeInTheDocument();
    expect(screen.getByText("112.3 fps")).toBeInTheDocument();
    expect(screen.getByTestId("restore-heartbeat")).toHaveTextContent("修复中 · 已用 01:05");
  });

  it("LTX keeps its stage block and gains the heartbeat", () => {
    wrap(<StageBlock item={item("ltx")} />);
    expect(screen.getByRole("progressbar", { name: "浮光之间 Encode / 编码 本阶段进度" })).toHaveAttribute("aria-valuenow", "52");
    expect(screen.getByText("156 / 300 帧")).toBeInTheDocument();
    expect(screen.getByTestId("restore-heartbeat")).toHaveTextContent("修复中 · 已用 01:05");
  });
});

describe("drawer 修复 section", () => {
  it("starting: the heartbeat and what Jasna is doing", () => {
    wrap(<RestoreLivePanel step={step("starting")} />);
    const beat = screen.getByTestId("restore-heartbeat");
    expect(beat).toHaveTextContent("正在启动 Jasna（加载模型） · 已用 01:05");
    expect(beat).toHaveTextContent("Jasna 正在加载模型");
    expect(screen.queryByTestId("restore-console-toggle")).not.toBeInTheDocument();
  });

  it("working: the latest console line in a selectable 最新输出 fold", () => {
    wrap(<RestoreLivePanel step={step("working")} />);
    expect(screen.getByTestId("restore-console-toggle")).toHaveTextContent("最新输出");
    expect(screen.getByTestId("restore-console-raw")).toHaveTextContent("Loading LTX weights (fast) …");
  });

  it("scan: the shared scan card, its console fold, no duplicate 最新输出", () => {
    wrap(<RestoreLivePanel step={step("scan")} />);
    const card = screen.getByTestId("scan-progress");
    expect(within(card).getByText("Mosaic scan")).toBeInTheDocument();
    expect(within(card).getByText("12%")).toBeInTheDocument();
    expect(within(card).getByText("600 / 5000")).toBeInTheDocument();
    expect(within(card).getByText("04:53")).toBeInTheDocument();
    expect(within(card).getByText("每秒采样数")).toBeInTheDocument();
    expect(within(card).getByTestId("scan-console-raw").textContent).toBe(SCAN_LINE);
    expect(screen.queryByTestId("restore-console-toggle")).not.toBeInTheDocument();
    expect(screen.getByTestId("restore-heartbeat")).toHaveTextContent("扫描马赛克");
  });

  it("BasicVSR++: the shared card and the console line, the split plan as a note", () => {
    wrap(<RestoreLivePanel step={step("basic")} />);
    const card = screen.getByTestId("basic-progress");
    expect(within(card).getByText("Processing video")).toBeInTheDocument();
    expect(within(card).getByText("15%")).toBeInTheDocument();
    expect(within(card).getByText("36703 / 244157")).toBeInTheDocument();
    expect(screen.getByTestId("restore-console-raw").textContent).toBe(BASIC_LINE);
    expect(screen.getByTestId("restore-heartbeat")).toHaveTextContent("已自动分段：大马赛克片段用 LTX，其余用 BasicVSR++");
  });

  it("LTX: exactly the Jasna monitor's LtxProgress, plus a differing latest line", () => {
    wrap(<RestoreLivePanel step={step("ltx")} />);
    const card = screen.getByTestId("ltx-progress");
    expect(within(card).getByRole("list", { name: "LTX 阶段位置" })).toBeInTheDocument();
    expect(within(card).getByText("12:12")).toBeInTheDocument();
    expect(within(card).getByTestId("ltx-console-raw").textContent).toBe(LTX_LINE);
    expect(screen.queryByTestId("scan-progress")).not.toBeInTheDocument();
    expect(screen.getByTestId("restore-console-raw")).toHaveTextContent("WARNING: VRAM 92% used");
    expect(screen.getByTestId("restore-heartbeat")).toHaveTextContent("LTX 2/4 encode");
    act(() => { vi.advanceTimersByTime(60_000); });
    expect(screen.getByTestId("restore-heartbeat")).toHaveTextContent("已用 02:05");
  });
});

describe("Jasna monitor", () => {
  it("shows the mosaic scan with the same card, never as the file's percent", () => {
    const metrics = { ...RESTORE_PHASES.scan, console: undefined, gpu_pct: 80 };
    wrap(<MonitorMetrics metrics={metrics} />);
    const card = screen.getByTestId("scan-progress");
    expect(within(card).getByText("ABF-401.mp4")).toBeInTheDocument();
    expect(within(card).getByText("12%")).toBeInTheDocument();
    expect(screen.queryByText("scan progress")).not.toBeInTheDocument();
    expect(screen.queryByTestId("ltx-progress")).not.toBeInTheDocument();
  });
});
