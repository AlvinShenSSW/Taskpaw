import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { LtxProgress } from "../components/LtxProgress";
import { MonitorMetrics } from "../components/MonitorMetrics";
import { theme } from "../theme";
import { setLang } from "../i18n";

// Original console values recorded on 2026-10-16 with Jasna 0.11.1, also
// retained in test_jasna.py. In particular, Compose's two ETAs disagree:
// the UI must reproduce the observation instead of recalculating either.
const RECORDED = [
  { stage_index: 1, stage_name: "scan", percent: 45, percent_text: "45", current: "136", total: "300",
    rate_value: "133.25", rate_unit: "frame/s", elapsed: "00:01", eta: "00:01",
    raw_line: "LTX 1/4 scan:  45%|████▌     | 136/300 [00:01<00:01, 133.25frame/s]" },
  { stage_index: 2, stage_name: "encode", percent: 52, percent_text: "52", current: "156", total: "300",
    rate_value: "5.62", rate_unit: "frame/s", elapsed: "00:27", eta: "00:25", run_eta: "12:12",
    raw_line: "LTX 2/4 encode:  52%|█████▏    | 156/300 [00:27<00:25,  5.62frame/s, run 12:12 left]" },
  { stage_index: 3, stage_name: "denoise", percent: 100, percent_text: "100", current: "120", total: "120",
    rate_value: "2.34", rate_unit: "s/step", elapsed: "04:41", eta: "00:00", run_eta: "04:57",
    raw_line: "LTX 3/4 denoise: 100%|██████████| 120/120 [04:41<00:00,  2.34s/step, run 04:57 left]" },
  { stage_index: 4, stage_name: "compose", percent: 10, percent_text: "10", current: "30", total: "300",
    rate_value: "1.37", rate_unit: "s/frame", elapsed: "00:40", eta: "03:38", run_eta: "03:20",
    raw_line: "LTX 4/4 compose:  10%|█         | 30/300 [00:40<03:38,  1.37s/frame, run 03:20 left]" },
];
const snapshot = (fields: Record<string, unknown> = {}) => ({
  mode: "restore", stage_total: 4, ...RECORDED[1], ...fields,
});
const metrics = (fields: Record<string, unknown> = {}) => ({
  current_file: "video.mp4", ltx_progress: snapshot(fields),
});
const wrap = (ui: React.ReactNode) => render(<ThemeProvider theme={theme}>{ui}</ThemeProvider>);
const card = () => within(screen.getByTestId("ltx-progress"));
const metric = (label: string) => card().getByText(label).parentElement!;
const disclose = () => fireEvent.click(screen.getByTestId("ltx-console-toggle"));

beforeEach(() => { setLang("zh-CN"); });
afterEach(() => { cleanup(); setLang("zh-CN"); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe("LTX console card (#269)", () => {
  it.each(RECORDED)("preserves the recorded $stage_name observation", observation => {
    wrap(<MonitorMetrics metrics={metrics(observation)} />);
    expect(card().getByRole("heading", { name: new RegExp(observation.stage_name) })).toBeInTheDocument();
    expect(card().getByText(`${observation.current} / ${observation.total}`)).toBeInTheDocument();
    expect(metric("阶段速率")).toHaveTextContent(`${observation.rate_value} ${observation.rate_unit}`);
    expect(metric("本阶段已用")).toHaveTextContent(observation.elapsed);
    expect(metric("本阶段剩余")).toHaveTextContent(observation.eta);
    expect(screen.getByRole("progressbar", { name: new RegExp(observation.stage_name) }))
      .toHaveAttribute("aria-valuenow", String(observation.percent));
    expect(screen.getByTestId("ltx-console-raw")).not.toBeVisible();
    disclose();
    expect(screen.getByTestId("ltx-console-raw").textContent).toBe(observation.raw_line);
    expect(card().queryByText(/fps/)).not.toBeInTheDocument();
    if (observation.run_eta) expect(metric("当前 LTX 本轮预计剩余")).toHaveTextContent(observation.run_eta);
  });

  it.each(["frame/s", "s/frame", "s/step", "step/s", "it/s", "s/it"])("keeps numeric spelling and %s without conversion", unit => {
    wrap(<LtxProgress metrics={metrics({ percent: 62.5, percent_text: "62.50", current: "0050", total: "1e2",
      rate_value: "002.340", rate_unit: unit })} />);
    expect(card().getByText("62.50%")).toBeInTheDocument();
    expect(card().getByText("0050 / 1e2")).toBeInTheDocument();
    expect(metric("阶段速率")).toHaveTextContent(`002.340 ${unit}`);
  });

  it("labels speed-test observations and never marks unobserved earlier stages completed", () => {
    const { rerender } = wrap(<LtxProgress metrics={metrics({ mode: "speed_test", ...RECORDED[3] })} />);
    expect(card().getByText("LTX 测速")).toBeInTheDocument();
    const stages = screen.getByRole("list", { name: "LTX 阶段位置" });
    expect(within(stages).getAllByRole("listitem")).toHaveLength(4);
    expect(stages.querySelectorAll('[aria-current="step"]')).toHaveLength(1);
    expect(stages.querySelector('[aria-current="step"]')).toHaveTextContent("compose");
    expect(stages.querySelector(".Mui-completed")).toBeNull();
    expect(within(stages).queryByText(/已完成/)).not.toBeInTheDocument();
    rerender(<ThemeProvider theme={theme}><LtxProgress metrics={metrics({ stage_index: 1, stage_name: "scan" })} /></ThemeProvider>);
    expect(card().queryByText("LTX 测速")).not.toBeInTheDocument();
    expect(stages.querySelector('[aria-current="step"]')).toHaveTextContent("scan");
  });

  it("shows unknown stages literally without guessing the four-stage order or count unit", () => {
    wrap(<MonitorMetrics metrics={metrics({ stage_index: 5, stage_total: 6, stage_name: "blend pass" })} />);
    expect(card().getByRole("heading", { name: "blend pass" })).toBeInTheDocument();
    expect(card().getByText("阶段 5 / 6")).toBeInTheDocument();
    expect(card().getByText("计数")).toBeInTheDocument();
    expect(card().queryByRole("list")).not.toBeInTheDocument();
  });

  it("replaces partial snapshots without borrowing legacy or preceding-stage numbers", () => {
    const legacy = { ltx_stage: "2/4 encode", percent: 92, fps: 999, eta: "98:00", elapsed: "97:00", processed_frames: 999 };
    const { rerender } = wrap(<MonitorMetrics metrics={{ ...metrics(), ...legacy }} />);
    const partial = { mode: "restore", stage_index: 3, stage_total: 4, stage_name: "denoise", raw_line: "LTX 3/4 denoise:" };
    rerender(<ThemeProvider theme={theme}><MonitorMetrics metrics={{ ltx_progress: partial, ...legacy }} /></ThemeProvider>);
    expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
    for (const label of ["阶段速率", "本阶段已用", "本阶段剩余", "当前 LTX 本轮预计剩余"]) {
      expect(metric(label)).toHaveTextContent("—");
    }
    expect(card().getByText("— / —")).toBeInTheDocument();
    expect(screen.queryByText(/98:00|97:00|999|92%|5\.62/)).not.toBeInTheDocument();
    expect(card().getByText("最近一次控制台更新")).toBeInTheDocument();
  });

  it("ignores malformed optional fields without hiding a valid stage", () => {
    wrap(<LtxProgress metrics={metrics({ percent: Infinity, percent_text: "100", current: "NaN", total: "-1",
      rate_value: "Infinity", rate_unit: "fps", elapsed: "yesterday", eta: "00:99", run_eta: {}, raw_line: null })} />);
    expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
    expect(card().getByText("— / —")).toBeInTheDocument();
    expect(metric("阶段速率")).toHaveTextContent("—");
    expect(metric("本阶段剩余")).toHaveTextContent("—");
    disclose();
    expect(card().getByRole("button", { name: "复制当前行" })).toBeDisabled();
  });

  it.each([null, [], { mode: "restore", stage_index: 9, stage_total: 4, stage_name: "scan" },
    { mode: "other", stage_index: 1, stage_total: 4, stage_name: "scan" }])("does not resurrect legacy data behind an invalid structured snapshot (%j)", value => {
    wrap(<LtxProgress metrics={{ ltx_progress: value, ltx_stage: "2/4 encode", percent: 99 }} />);
    expect(screen.queryByTestId("ltx-progress")).not.toBeInTheDocument();
  });

  it("supports old Agents with unknown new fields and switches away cleanly", () => {
    const { rerender } = wrap(<MonitorMetrics metrics={{ current_file: "a.mp4", ltx_stage: "2/4 encode", percent: 52, fps: 5.62, eta: "12:12" }} />);
    expect(metric("阶段速率")).toHaveTextContent("5.62 frame/s");
    expect(metric("当前 LTX 本轮预计剩余")).toHaveTextContent("12:12");
    expect(metric("本阶段剩余")).toHaveTextContent("—");
    expect(card().getByText("— / —")).toBeInTheDocument();
    rerender(<ThemeProvider theme={theme}><MonitorMetrics metrics={{ current_file: "b.mp4", percent: 5, fps: 8 }} /></ThemeProvider>);
    expect(screen.queryByTestId("ltx-progress")).not.toBeInTheDocument();
    expect(screen.getByText("5%")).toBeInTheDocument();
  });

  it.each(["en", "zh-CN"] as const)("localizes stage, labels, unknowns and copy feedback (%s)", lang => {
    setLang(lang);
    wrap(<LtxProgress metrics={metrics({ stage_index: 3, stage_name: "denoise", mode: "speed_test" })} />);
    expect(card().getByText(lang === "en" ? "LTX speed test" : "LTX 测速")).toBeInTheDocument();
    expect(card().getByRole("heading")).toHaveTextContent(lang === "en" ? "denoise Denoise" : "denoise 去噪");
    expect(card().getByText(lang === "en" ? "Last console update" : "最近一次控制台更新")).toBeInTheDocument();
    expect(screen.getByRole("progressbar")).toHaveAccessibleName(new RegExp("denoise"));
    disclose();
    expect(card().getByRole("button", { name: lang === "en" ? "Copy current line" : "复制当前行" })).toBeEnabled();
  });

  it("copies bounded plain text with ANSI/OSC/control sequences removed", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    const raw = "\u001b[32mLTX\u001b[0m \u001b]8;;https://example.invalid\u0007plain\u001b]8;;\u001b\\\u0000 <b>literal</b> " + "x".repeat(4200);
    wrap(<LtxProgress metrics={metrics({ raw_line: raw })} />);
    disclose();
    const pre = screen.getByTestId("ltx-console-raw");
    expect(pre.textContent).toMatch(/^LTX plain <b>literal<\/b>/);
    expect(pre.textContent!.length).toBeLessThanOrEqual(4096);
    expect(pre.querySelector("b")).toBeNull();
    fireEvent.click(card().getByRole("button", { name: "复制当前行" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(pre.textContent));
    expect(card().getByRole("status")).toHaveTextContent("已复制当前进度行");
  });

  it.each(["unavailable", "denied"])("keeps raw text selectable when clipboard is %s", async kind => {
    vi.stubGlobal("navigator", kind === "unavailable" ? {} : { clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) } });
    wrap(<LtxProgress metrics={metrics()} />);
    disclose();
    fireEvent.click(card().getByRole("button", { name: "复制当前行" }));
    await waitFor(() => expect(card().getByRole("status")).toHaveTextContent("无法自动复制，请选择原始进度文本复制。"));
    expect(screen.getByTestId("ltx-console-raw")).toBeVisible();
    expect(screen.getByTestId("ltx-console-raw")).toHaveAttribute("tabindex", "0");
  });
});

describe("LTX pipeline scope in Agent and Hub", () => {
  it.each(["agent", "hub"] as const)("isolates stage numbers and ETA while retaining queue/subtitle values (%s)", kind => {
    const input = { ...metrics(RECORDED[2]), ltx_stage: "3/4 denoise", percent: 100, fps: 2.34,
      eta: "04:57", elapsed: "04:41", processed_frames: 120, remaining_frames: 0,
      queue_completed: 2, queue_total: 8, queue_restored: 3, queue_remaining: 6,
      film: "video.mp4", steps: [{ key: "restore", state: "active", percent: 100, eta_s: 297, elapsed_s: 281 },
        { key: "asr", state: "pending" }, { key: "translate", state: "pending" }],
      films: [{ name: "video.mp4", status: "active", steps: { restore: "active", asr: "pending", translate: "pending" }, percent: 100, eta_s: 297 },
        { name: "previous.mp4", status: "active", steps: { restore: "done", asr: "active", translate: "pending" }, percent: 43, eta_s: 360 }],
      films_more: 0,
    };
    wrap(<MonitorMetrics metrics={input} taskType="jasna" filmSource={kind === "hub" ? { kind: "hub", serverId: 1 } : undefined} />);
    const pipeline = within(screen.getByTestId("pipeline-progress"));
    expect(screen.getAllByText("100%")).toHaveLength(1);
    expect(pipeline.queryByText(/04:57|4:41|约剩 5 分|100%/)).not.toBeInTheDocument();
    expect(pipeline.getByText(/2.*8/)).toBeInTheDocument();
    expect(pipeline.getByText(/43%/)).toBeInTheDocument();
    expect(input.steps[0]).toMatchObject({ percent: 100, eta_s: 297, elapsed_s: 281 });
  });
});

describe("LTX269-F1: invalid structured data still owns legacy metrics", () => {
  const invalidSnapshots = [
    null,
    { mode: "other", stage_index: 2, stage_total: 4, stage_name: "encode" },
    { mode: "restore", stage_index: 9, stage_total: 4, stage_name: "encode" },
  ];
  const legacy = { current_file: "video.mp4", ltx_stage: "2/4 encode", percent: 99,
    fps: 5.62, eta: "12:12", elapsed: "11:11", processed_frames: 999, remaining_frames: 1,
    queue_completed: 2, queue_total: 8, queue_remaining: 6 };

  it.each(invalidSnapshots)("suppresses generic stage numbers for %j, then retains BasicVSR++ progress", invalid => {
    const { rerender } = wrap(<MonitorMetrics metrics={{ ...legacy, ltx_progress: invalid }} />);
    expect(screen.queryByTestId("ltx-progress")).not.toBeInTheDocument();
    for (const stale of ["99%", "12:12", "11:11", "5.6", "processed frames", "remaining frames"]) {
      expect(screen.queryByText(stale)).not.toBeInTheDocument();
    }
    expect(screen.getByText(/2.*8/)).toBeInTheDocument();
    // Absence, unlike an invalid owned property, admits ordinary film telemetry.
    rerender(<ThemeProvider theme={theme}><MonitorMetrics metrics={{ current_file: "plain.mp4", percent: 25, fps: 5.62, eta: "12:12" }} /></ThemeProvider>);
    expect(screen.getByText("25%")).toBeInTheDocument();
    expect(screen.getByText("12:12")).toBeInTheDocument();
    expect(screen.getByText("5.6")).toBeInTheDocument();
  });

  it.each(invalidSnapshots)("suppresses restore-step/row legacy estimates for %j without hiding subtitle or queue progress", invalid => {
    wrap(<MonitorMetrics metrics={{ ...legacy, ltx_progress: invalid, film: "video.mp4",
      steps: [{ key: "restore", state: "active", percent: 99, eta_s: 732, elapsed_s: 671 },
        { key: "asr", state: "pending" }, { key: "translate", state: "pending" }],
      films: [{ name: "video.mp4", status: "active", steps: { restore: "active", asr: "pending", translate: "pending" }, percent: 99, eta_s: 732 },
        { name: "previous.mp4", status: "active", steps: { restore: "done", asr: "active", translate: "pending" }, percent: 43, eta_s: 360 }],
      films_more: 0,
    }} />);
    expect(screen.queryByTestId("ltx-progress")).not.toBeInTheDocument();
    const pipeline = screen.getByTestId("pipeline-progress");
    expect(pipeline).not.toHaveTextContent(/99%|12:12|11:11|约剩 13 分|5\.6/);
    expect(within(pipeline).getByText(/43%/)).toBeInTheDocument();
    expect(within(pipeline).getByText(/2.*8/)).toBeInTheDocument();
  });
});
