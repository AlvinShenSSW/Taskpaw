// U1c: the running 修复 step always shows it is alive — 已用 ticking from the stage start, the
// phase (马赛克扫描 → BasicVSR++ → the Jasna monitor's LtxProgress) and the latest console line.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import type { ReactNode } from "react";
import { MonitorMetrics } from "../components/MonitorMetrics";
import { elapsedClock, filmStatus, heartbeatText, readParams, restoreLive, restoreStep, type WorkItem } from "../components/media/mediaModel";
import { StageBlock } from "../components/media/parts";
import { RestoreLivePanel } from "../components/media/RestoreLivePanel";
import { setLang } from "../i18n";
import { theme } from "../theme";
import {
  BASIC_LINE, LTX_LINE, PARAMS, RESTORE_PHASES, restoreSteps, row, SCAN_LINE, type RestorePhaseKey,
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

describe("本片参数", () => {
  const param = (label: string) => document.querySelector(`[data-param="${label}"]`)?.textContent;

  it("the drawer lists this film's effective launch and the full command", () => {
    wrap(<RestoreLivePanel step={step("ltx")} />);
    const block = screen.getByTestId("restore-params");
    expect(within(block).getByText("本片参数")).toBeInTheDocument();
    expect(within(block).getByText("预设 RTX 5070 Ti · 16 GB（自动）")).toBeInTheDocument();
    expect(param("画质")).toBe("画质标准（ltx）");
    expect(param("快速模式")).toBe("快速模式nvfp4");
    expect(param("自动拆分")).toBe("自动拆分12%");
    expect(param("大画布")).toBe("大画布关");
    expect(param("减少重叠")).toBe("减少重叠自动");
    expect(param("编码")).toBe("编码HEVC · CQ 20");
    expect(param("码率上限")).toBe("码率上限12.2 Mbps（原片 12.2 Mbps）");
    expect(param("检测")).toBe("检测rfdetr-v6");
    expect(param("片段 / 重叠")).toBe("片段 / 重叠180 / 8");
    expect(param("输入")).toBe("输入1920×1080 · 1080p 档");
    expect(param("GPU")).toBe("GPUNVIDIA GeForce RTX 5070 Ti 16 GB");
    expect(within(block).getByTestId("restore-argv-toggle")).toHaveTextContent("完整命令");
    expect(within(block).getByTestId("restore-argv-raw").textContent).toBe(PARAMS.argv);
  });

  it("names a configured preset without the 自动 mark, and falls back to the id", () => {
    const named = { ...PARAMS, preset: "5060-8", preset_label: "RTX 5060 · 8 GB", preset_auto: false };
    wrap(<RestoreLivePanel step={{ ...step("ltx"), params: named }} />);
    expect(within(screen.getByTestId("restore-params")).getByText("预设 RTX 5060 · 8 GB")).toBeInTheDocument();
    expect(readParams({ ...PARAMS, preset_label: undefined, preset_auto: undefined })).toMatchObject(
      { preset: "5070ti-16", preset_label: "5070ti-16", preset_auto: false });
  });

  it("says so when the source bitrate could not be read", () => {
    const params = { ...PARAMS, ltx_model: "high" as const, model_name: "ltx-8step", fast: false, maxrate_bps: null, source_bitrate_bps: null };
    wrap(<RestoreLivePanel step={{ ...step("starting"), params }} />);
    expect(param("码率上限")).toBe("码率上限未读到原片码率，按 Jasna 默认上限");
    expect(param("画质")).toBe("画质高（ltx-8step）");
    expect(param("快速模式")).toBe("快速模式int8");
  });

  it("drops a payload without a command and strips terminal controls", () => {
    expect(restoreLive({ ...step("scan"), params: { ...PARAMS, argv: "" } })?.params).toBeNull();
    expect(restoreLive({ ...step("scan"), params: { ...PARAMS, gpu: "[31mRTX[0m" } })?.params?.gpu).toBe("RTX");
  });

  it.each(["starting", "scan", "basic", "ltx"] as const)("the %s queue card shows the one-line summary", (phase) => {
    wrap(<StageBlock item={item(phase)} />);
    expect(screen.getByTestId("restore-params")).toHaveTextContent("标准 · nvfp4 · 上限 12.2 Mbps");
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
