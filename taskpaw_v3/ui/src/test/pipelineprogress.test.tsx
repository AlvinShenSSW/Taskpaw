import { afterEach, describe, expect, it } from "vitest";
import { cleanup, render, screen, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { MonitorMetrics } from "../components/MonitorMetrics";
import { readPipeline } from "../components/pipelineProgress.helpers";
import { setLang } from "../i18n";
import { theme } from "../theme";

// #189 AV 翻译 progress redesign — PipelineProgress (per-film 修复 → 识别 → 翻译
// stepper, active-step panel, queue card, batch list) rendered by MonitorMetrics
// whenever `metrics.steps` is a usable array. Fixtures follow the metric shapes in
// docs/specs/2026-09-25-189-progress-redesign-design.md ("Plugins — metrics").

const wrap = (ui: React.ReactNode) =>
  render(<ThemeProvider theme={theme}>{ui}</ThemeProvider>);
// Unmount before switching back to zh so no mounted tree re-renders outside act().
const reset = () => {
  cleanup();
  setLang("zh-CN");
};
const pipe = () => screen.getByTestId("pipeline-progress");
const stepper = () => screen.getByTestId("pipeline-stepper");

const GAUGES = {
  cpu_pct: 41, mem_pct: 55, mem_used_mb: 9000, mem_total_mb: 16384,
  gpu_pct: 89, gpu_mem_used_mb: 3700, gpu_mem_total_mb: 8192,
};

// Jasna, AV 翻译 on, board ② — restoring (capture on).
const RESTORE = {
  ...GAUGES,
  current_file: "SDAB-312.mp4", percent: 69, fps: 157, eta: "7:18", elapsed: "39:12",
  processed_frames: 36703, remaining_frames: 207454,
  queue_completed: 0, queue_total: 1, queue_failed: 0, queue_remaining: 1, queue_restored: 0,
  phase: "restore",
  subs_total: 1, subs_completed: 0, subs_failed: 0, subs_skipped: 0, subs_remaining: 1,
  subs_translating: 0,
  film: "SDAB-312.mp4",
  steps: [
    { key: "restore", state: "active", percent: 69, eta_s: 438 },
    { key: "asr", state: "pending" },
    { key: "translate", state: "pending" },
  ],
  films: [{ name: "SDAB-312.mp4", steps: { restore: "active", asr: "pending", translate: "pending" },
            status: "active", percent: 69, eta_s: 438 }],
  films_more: 0,
};

// Board ③ — transcribing (qwen scene progress).
const ASR = {
  ...GAUGES,
  current_file: "SDAB-312-破解.mp4",
  queue_completed: 0, queue_total: 1, queue_failed: 0, queue_remaining: 1, queue_restored: 1,
  phase: "subs", subs_total: 1, subs_completed: 0, subs_failed: 0, subs_skipped: 0,
  subs_remaining: 1, subs_translating: 0,
  film: "SDAB-312.mp4",
  steps: [
    { key: "restore", state: "done", duration_s: 3420 },
    { key: "asr", state: "active", percent: 43, eta_s: 360, elapsed_s: 250,
      phase: 5, phase_n: 8, scene: 120, scenes: 276 },
    { key: "translate", state: "pending" },
  ],
  films: [], films_more: 0,
};

// Board ④ — translating (model shown).
const TRANSLATE = {
  ...GAUGES,
  queue_completed: 0, queue_total: 1, queue_failed: 0, queue_remaining: 1, queue_restored: 1,
  phase: "translate", subs_total: 1, subs_completed: 0, subs_failed: 0, subs_skipped: 0,
  subs_remaining: 1, subs_translating: 1,
  film: "SDAB-312.mp4",
  model: "grok-4.3 · api.x.ai",
  steps: [
    { key: "restore", state: "done", duration_s: 3420 },
    { key: "asr", state: "done", duration_s: 600 },
    { key: "translate", state: "active", percent: 56, eta_s: 60, elapsed_s: 92,
      model: "grok-4.3 · api.x.ai", batches_done: 9, batches_total: 16,
      cues_done: 360, cues_total: 620 },
  ],
};

// Board ⑤ — standalone avsubs task waiting for the GPU.
const AVSUBS_WAIT = {
  cpu_pct: 12, mem_pct: 40, gpu_pct: 88, gpu_mem_used_mb: 6758, gpu_mem_total_mb: 8192,
  queue_completed: 130, queue_total: 158, queue_failed: 1, queue_skipped: 0,
  queue_remaining: 27, queue_pre_done: 118, phase: "waiting_gpu", subs_translating: 2,
  film: "2024/ABC-123.mp4",
  steps: [
    { key: "asr", state: "waiting_gpu", holder: "Jasna", waited_s: 200 },
    { key: "translate", state: "pending" },
  ],
  films: [], films_more: 0,
};

// Board ⑥ — a Jasna batch: one film translating while the next restores.
const BATCH = {
  ...GAUGES,
  current_file: "SDAB-313.mp4", percent: 34, fps: 157, eta: "38:00",
  queue_completed: 2, queue_total: 6, queue_failed: 0, queue_remaining: 4, queue_restored: 4,
  phase: "restore", subs_total: 6, subs_completed: 2, subs_failed: 0, subs_skipped: 0,
  subs_remaining: 4, subs_translating: 1, model: "grok-4.3 · api.x.ai",
  film: "SDAB-313.mp4",
  steps: [
    { key: "restore", state: "active", percent: 34, eta_s: 2280 },
    { key: "asr", state: "pending" },
    { key: "translate", state: "pending" },
  ],
  films: [
    { name: "SDAB-310.mp4", steps: { restore: "done", asr: "done", translate: "done" },
      status: "done", duration_s: 4120 },
    { name: "SDAB-311.mp4", steps: { restore: "done", asr: "done", translate: "done" },
      status: "done", duration_s: 4265 },
    { name: "SDAB-312.mp4", steps: { restore: "done", asr: "done", translate: "active" },
      status: "active", percent: 56, eta_s: 60 },
    { name: "SDAB-313.mp4", steps: { restore: "active", asr: "pending", translate: "pending" },
      status: "active", percent: 34, eta_s: 2280 },
    { name: "SDAB-314.mp4", steps: { restore: "pending", asr: "pending", translate: "pending" },
      status: "pending" },
    { name: "SDAB-298.mp4", steps: { restore: "done", asr: "pending", translate: "pending" },
      status: "pending" },
  ],
  films_more: 5,
};

describe("PipelineProgress — stepper (#189)", () => {
  afterEach(reset);

  it("active restore: ring + percent + ETA, later steps pending with their number", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={RESTORE} />);
    const s = within(stepper());
    expect(s.getByText("修复")).toBeInTheDocument();
    expect(s.getByText("69% · 约剩 8 分")).toBeInTheDocument(); // ceil(438/60)
    expect(s.getByText("2")).toBeInTheDocument();                // pending number
    expect(s.getByText("3")).toBeInTheDocument();
    expect(s.getByText("等待修复完成")).toBeInTheDocument();
    expect(s.getByText("等待识别完成")).toBeInTheDocument();
    expect(within(pipe()).getByText("第 1 步 / 共 3 步")).toBeInTheDocument();
    expect(within(pipe()).getByText("正在处理")).toBeInTheDocument();
  });

  it("done step shows ✓ + its duration; the header counts the first unfinished step", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={ASR} />);
    const s = within(stepper());
    expect(s.getByText("完成 · 57 分")).toBeInTheDocument();
    expect(s.getByTestId("step-done-icon")).toBeInTheDocument();
    expect(s.getByText("43% · 约剩 6 分")).toBeInTheDocument();
    expect(within(pipe()).getByText("第 2 步 / 共 3 步")).toBeInTheDocument();
  });

  it("hour-scale durations and ETAs use the 小时 + 分 format", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={{ ...ASR, steps: [
      { key: "restore", state: "done", duration_s: 6300 },
      { key: "asr", state: "active", percent: 10, eta_s: 3601 },
      { key: "translate", state: "pending" },
    ] }} />);
    const s = within(stepper());
    expect(s.getByText("完成 · 1 小时 45 分")).toBeInTheDocument();       // round(6300/60) = 105
    expect(s.getByText("10% · 约剩 1 小时 01 分")).toBeInTheDocument();  // ceil(3601/60) = 61
  });

  it("done without a duration shows ✓ alone (a translation finished between polls)", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={{ ...TRANSLATE, steps: [
      { key: "restore", state: "done", duration_s: 3420 },
      { key: "asr", state: "done", duration_s: 600 },
      { key: "translate", state: "done" },
    ] }} />);
    const s = within(stepper());
    expect(s.getAllByTestId("step-done-icon")).toHaveLength(3);
    expect(s.getByText("完成")).toBeInTheDocument();
    // Every step terminal → k = n, and the header says "last finished".
    expect(within(pipe()).getByText("第 3 步 / 共 3 步")).toBeInTheDocument();
    expect(within(pipe()).getByText("最近完成")).toBeInTheDocument();
  });

  it("failed is red-labelled, skipped grey-labelled (never colour alone)", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={{ ...ASR, steps: [
      { key: "restore", state: "done", duration_s: 30 },
      { key: "asr", state: "failed" },
      { key: "translate", state: "skipped" },
    ] }} />);
    const s = within(stepper());
    expect(s.getByText("完成 · 30 秒")).toBeInTheDocument();
    expect(s.getByText("失败")).toBeInTheDocument();
    expect(s.getByText("已跳过")).toBeInTheDocument();
    expect(s.getByTestId("step-failed-icon")).toBeInTheDocument();
    expect(s.getByTestId("step-skipped-icon")).toBeInTheDocument();
    expect(within(pipe()).getByText("第 3 步 / 共 3 步")).toBeInTheDocument();
  });

  it("queued translation reads 排队中 and gets its own panel", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={{ ...TRANSLATE, steps: [
      { key: "restore", state: "done", duration_s: 3420 },
      { key: "asr", state: "done", duration_s: 600 },
      { key: "translate", state: "queued" },
    ] }} />);
    expect(within(stepper()).getByText("排队中")).toBeInTheDocument();
    expect(within(stepper()).getByTestId("step-queued-icon")).toBeInTheDocument();
    expect(within(pipe()).getByText(/翻译排队中/)).toBeInTheDocument();
  });

  it("waiting_gpu names the holder in full-width parentheses + waited time", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={AVSUBS_WAIT} />);
    const s = within(stepper());
    // No extra space after the full-width "）" (it carries its own gap).
    expect(s.getByText("等待 GPU（Jasna）· 已等 3:20")).toBeInTheDocument();
    expect(s.getByTestId("step-waiting-icon")).toBeInTheDocument();
    expect(within(pipe()).getByText("GPU 正由「Jasna」使用")).toBeInTheDocument();
    expect(within(pipe()).getByText("下一个文件")).toBeInTheDocument();
    expect(within(pipe()).getByText("2024/ABC-123.mp4")).toBeInTheDocument();
    expect(within(pipe()).getByText("第 1 步 / 共 2 步")).toBeInTheDocument();
  });

  it("waiting_gpu with holder \"\" omits the parentheses (lease reserved for this run)", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={{ ...AVSUBS_WAIT, steps: [
      { key: "asr", state: "waiting_gpu", holder: "", waited_s: 5 },
      { key: "translate", state: "pending" },
    ] }} />);
    const s = within(stepper());
    expect(s.getByText("等待 GPU · 已等 0:05")).toBeInTheDocument();
    expect(pipe().textContent).not.toMatch(/（|「/);
    expect(within(pipe()).getByText(/下一次检查/)).toBeInTheDocument();
  });

  it("waiting_gpu without waited_s shows the bare wait label (en)", () => {
    setLang("en");
    wrap(<MonitorMetrics metrics={{ ...AVSUBS_WAIT, steps: [
      { key: "asr", state: "waiting_gpu", holder: "Jasna" },
      { key: "translate", state: "pending" },
    ] }} />);
    expect(within(stepper()).getByText("Waiting for GPU (Jasna)")).toBeInTheDocument();
    expect(within(pipe()).getByText("Step 1 of 2")).toBeInTheDocument();
  });
});

describe("PipelineProgress — active-step panel (#189)", () => {
  afterEach(reset);

  it("restore panel: percent bar + speed / elapsed / ETA from the capture keys", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={RESTORE} />);
    const p = within(screen.getByTestId("pipeline-panel"));
    expect(p.getByText("修复 · Jasna")).toBeInTheDocument();
    expect(p.getByText("69%")).toBeInTheDocument();
    expect(p.getByRole("progressbar")).toBeInTheDocument();
    expect(p.getByText("速度")).toBeInTheDocument();
    expect(p.getByText("157 fps")).toBeInTheDocument();
    expect(p.getByText("帧")).toBeInTheDocument();
    expect(p.getByText("36703 / 244157")).toBeInTheDocument(); // processed / (processed + remaining)
    expect(p.getByText("已用")).toBeInTheDocument();
    expect(p.getByText("39:12")).toBeInTheDocument();
    expect(p.getByText("约剩")).toBeInTheDocument();
    expect(p.getByText("8 分")).toBeInTheDocument();
  });

  it("restore panel (en): Frames + ETA tiles, hour-scale ETA, pending wording", () => {
    setLang("en");
    wrap(<MonitorMetrics metrics={{ ...RESTORE, steps: [
      { key: "restore", state: "active", percent: 69, eta_s: 6300 },
      { key: "asr", state: "pending" },
      { key: "translate", state: "pending" },
    ] }} />);
    const p = within(screen.getByTestId("pipeline-panel"));
    expect(p.getByText("Frames")).toBeInTheDocument();
    expect(p.getByText("36703 / 244157")).toBeInTheDocument();
    expect(p.getByText("ETA")).toBeInTheDocument();
    expect(p.getByText("1 h 45 min")).toBeInTheDocument(); // ceil(6300/60) = 105 min
    const s = within(stepper());
    expect(s.getByText("69% · 1 h 45 min left")).toBeInTheDocument();
    expect(s.getByText("Restore must finish first")).toBeInTheDocument();
    expect(s.getByText("Transcribe must finish first")).toBeInTheDocument();
  });

  it("restore panel: processed frames alone when remaining_frames is absent", () => {
    setLang("zh-CN");
    const noRemaining = Object.fromEntries(Object.entries(RESTORE).filter(
      ([k]) => k !== "remaining_frames"));
    wrap(<MonitorMetrics metrics={noRemaining} />);
    const p = within(screen.getByTestId("pipeline-panel"));
    expect(p.getByText("帧")).toBeInTheDocument();
    expect(p.getByText("36703")).toBeInTheDocument();
  });

  it("asr panel formats 场景 i/N (zh) when the scene is known", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={ASR} />);
    const p = within(screen.getByTestId("pipeline-panel"));
    expect(p.getByText("识别 · WhisperJAV")).toBeInTheDocument();
    expect(p.getByText("场景")).toBeInTheDocument();
    expect(p.getByText("120 / 276")).toBeInTheDocument();
    expect(p.queryByText("阶段")).toBeNull();
    expect(p.getByText("4:10")).toBeInTheDocument(); // elapsed_s 250
    expect(p.getByText("6 分")).toBeInTheDocument();  // eta_s 360
  });

  it("asr panel formats Scene i/N (en)", () => {
    setLang("en");
    wrap(<MonitorMetrics metrics={ASR} />);
    const p = within(screen.getByTestId("pipeline-panel"));
    expect(p.getByText("Transcribe · WhisperJAV")).toBeInTheDocument();
    expect(p.getByText("Scene")).toBeInTheDocument();
    expect(p.getByText("120 / 276")).toBeInTheDocument();
    expect(within(stepper()).getByText("43% · 6 min left")).toBeInTheDocument();
  });

  it("asr panel falls back to 阶段 k/8 without a scene (zh + en)", () => {
    const phaseOnly = { ...ASR, steps: [
      { key: "restore", state: "done", duration_s: 3420 },
      { key: "asr", state: "active", percent: 7, elapsed_s: 70, phase: 2, phase_n: 8,
        scene: null, scenes: null },
      { key: "translate", state: "pending" },
    ] };
    setLang("zh-CN");
    const { unmount } = wrap(<MonitorMetrics metrics={phaseOnly} />);
    let p = within(screen.getByTestId("pipeline-panel"));
    expect(p.getByText("阶段")).toBeInTheDocument();
    expect(p.getByText("2 / 8")).toBeInTheDocument();
    expect(p.queryByText("场景")).toBeNull();
    // No ETA yet → the stepper shows the percent alone.
    expect(within(stepper()).getByText("7%")).toBeInTheDocument();
    unmount();
    setLang("en");
    wrap(<MonitorMetrics metrics={phaseOnly} />);
    p = within(screen.getByTestId("pipeline-panel"));
    expect(p.getByText("Phase")).toBeInTheDocument();
    expect(p.getByText("2 / 8")).toBeInTheDocument();
  });

  it("asr without a percent (non-qwen engine) shows elapsed in the stepper", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={{ ...ASR, steps: [
      { key: "restore", state: "done" },
      { key: "asr", state: "active", elapsed_s: 245, phase: null, percent: null, eta_s: null },
      { key: "translate", state: "pending" },
    ] }} />);
    expect(within(stepper()).getByText("已用 4:05")).toBeInTheDocument();
    const p = within(screen.getByTestId("pipeline-panel"));
    expect(p.queryByRole("progressbar")).toBeNull();
    expect(p.queryByText("阶段")).toBeNull();
  });

  it("translate panel shows the model label, batches, cues, elapsed and ETA", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={TRANSLATE} />);
    const p = within(screen.getByTestId("pipeline-panel"));
    expect(p.getByText("翻译 · grok-4.3 · api.x.ai")).toBeInTheDocument();
    expect(p.getByText("56%")).toBeInTheDocument();
    expect(p.getByText("批次")).toBeInTheDocument();
    expect(p.getByText("9 / 16")).toBeInTheDocument();
    expect(p.getByText("已译")).toBeInTheDocument();
    expect(p.getByText("360 / 620 句")).toBeInTheDocument();
    expect(p.getByText("1:32")).toBeInTheDocument();
    expect(p.getByText("1 分")).toBeInTheDocument();
    expect(within(pipe()).getByText("第 3 步 / 共 3 步")).toBeInTheDocument();
  });

  it("translate panel falls back to the top-level model (en)", () => {
    setLang("en");
    const steps = [
      { key: "asr", state: "done" },
      { key: "translate", state: "active", percent: 10, cues_done: 40, cues_total: 400 },
    ];
    wrap(<MonitorMetrics metrics={{ film: "x.mp4", model: "gpt-x · api.example.com", steps }} />);
    const p = within(screen.getByTestId("pipeline-panel"));
    expect(p.getByText("Translate · gpt-x · api.example.com")).toBeInTheDocument();
    expect(p.getByText("40 / 400 cues")).toBeInTheDocument();
  });
});

describe("PipelineProgress — queue card (#189)", () => {
  afterEach(reset);

  it("Jasna: fully-done count + 修复 a/b · 字幕 chips (in progress)", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={ASR} />);
    const q = within(screen.getByTestId("pipeline-queue"));
    expect(q.getByText(/0 \/ 1 完成/)).toBeInTheDocument();
    expect(q.getByText("修复 1 / 1")).toBeInTheDocument();
    expect(q.getByText("字幕 0 / 1 · 进行中")).toBeInTheDocument();
    expect(q.queryByText(/已有字幕/)).toBeNull();
  });

  it("Jasna batch: light in-progress segment = restored − fully done", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={BATCH} />);
    const q = within(screen.getByTestId("pipeline-queue"));
    expect(q.getByText(/2 \/ 6 完成/)).toBeInTheDocument();
    expect(q.getByText("修复 4 / 6")).toBeInTheDocument();
    expect(q.getByText("字幕 2 / 6 · 进行中")).toBeInTheDocument();
    expect(q.getByTestId("seg-done")).toHaveStyle({ width: `${(2 / 6) * 100}%` });
    expect(q.getByTestId("seg-progress")).toHaveStyle({ width: `${(2 / 6) * 100}%` });
    expect(q.queryByTestId("seg-failed")).toBeNull();
    expect(q.getByText(/浅绿/)).toBeInTheDocument(); // legend
  });

  it("Jasna: failed restores + failed/skipped subtitles are chips, not raw tiles", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={{ ...BATCH, queue_failed: 1, subs_failed: 1, subs_skipped: 2,
      subs_translating: 0 }} />);
    const q = within(screen.getByTestId("pipeline-queue"));
    expect(q.getByText("失败 1")).toBeInTheDocument();
    expect(q.getByText("字幕 2 / 6 · 失败 1 · 跳过 2")).toBeInTheDocument();
  });

  it("avsubs: 完成/翻译中/失败/跳过/排队/已有字幕 chips + red failed segment", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={AVSUBS_WAIT} />);
    const q = within(screen.getByTestId("pipeline-queue"));
    expect(q.getByText(/130 \/ 158 完成/)).toBeInTheDocument();
    expect(q.getByText("完成 12")).toBeInTheDocument();     // 130 − 118 pre-done
    expect(q.getByText("翻译中 2")).toBeInTheDocument();
    expect(q.getByText("失败 1")).toBeInTheDocument();
    expect(q.getByText("跳过 0")).toBeInTheDocument();
    expect(q.getByText("排队 25")).toBeInTheDocument();     // 27 remaining − 2 translating
    expect(q.getByText("已有字幕 118")).toBeInTheDocument();
    expect(q.getByTestId("seg-failed")).toHaveStyle({ width: `${(1 / 158) * 100}%` });
    expect(q.queryByText(/^修复/)).toBeNull();
  });

  it("avsubs chips in English", () => {
    setLang("en");
    wrap(<MonitorMetrics metrics={AVSUBS_WAIT} />);
    const q = within(screen.getByTestId("pipeline-queue"));
    expect(q.getByText("Done 12")).toBeInTheDocument();
    expect(q.getByText("Already subtitled 118")).toBeInTheDocument();
    expect(within(stepper()).getByText("Waiting for GPU (Jasna) · waited 3:20")).toBeInTheDocument();
  });
});

describe("PipelineProgress — batch list (#189)", () => {
  afterEach(reset);

  it("lists every film row with its step chips, status and time + 还有 N 部", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={BATCH} />);
    const list = within(screen.getByTestId("pipeline-films"));
    const rows = list.getAllByTestId("film-row");
    expect(rows).toHaveLength(6);
    expect(within(rows[0]).getByText("SDAB-310.mp4")).toBeInTheDocument();
    expect(within(rows[0]).getByText("1:08:40")).toBeInTheDocument();
    expect(within(rows[0]).getByText("完成")).toBeInTheDocument();
    expect(within(rows[2]).getByText("翻译 56%")).toBeInTheDocument();
    expect(within(rows[2]).getByText(/^翻译中/)).toBeInTheDocument();
    expect(within(rows[2]).getByText("约剩 1 分")).toBeInTheDocument();
    expect(within(rows[3]).getByText("修复 34%")).toBeInTheDocument();
    expect(within(rows[3]).getByText("修复中")).toBeInTheDocument();
    expect(within(rows[3]).getByText("约剩 38 分")).toBeInTheDocument();
    expect(within(rows[4]).getByText("排队")).toBeInTheDocument();
    expect(within(rows[5]).getByText("排队 · 已修复过，只补字幕")).toBeInTheDocument();
    expect(list.getByText("还有 5 部")).toBeInTheDocument();
  });

  it("row statuses for failed / skipped / waiting_gpu / queued (en) + N more", () => {
    setLang("en");
    wrap(<MonitorMetrics metrics={{ ...BATCH, films_more: 2, films: [
      { name: "a.mp4", steps: { restore: "failed", asr: "skipped", translate: "skipped" }, status: "failed" },
      { name: "b.mp4", steps: { restore: "done", asr: "done", translate: "skipped" }, status: "skipped" },
      { name: "c.mp4", steps: { restore: "waiting_gpu", asr: "pending", translate: "pending" }, status: "waiting_gpu" },
      { name: "d.mp4", steps: { restore: "done", asr: "done", translate: "queued" }, status: "queued" },
    ] }} />);
    const rows = screen.getAllByTestId("film-row");
    expect(within(rows[0]).getByText("Failed")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Skipped")).toBeInTheDocument();
    expect(within(rows[2]).getByText("Waiting for GPU")).toBeInTheDocument();
    expect(within(rows[3]).getByText("Waiting to translate")).toBeInTheDocument();
    expect(screen.getByText("2 more")).toBeInTheDocument();
  });

  it("no films → no batch list", () => {
    wrap(<MonitorMetrics metrics={ASR} />);
    expect(screen.queryByTestId("pipeline-films")).toBeNull();
  });
});

describe("MonitorMetrics with / without steps (#189 D13)", () => {
  afterEach(reset);

  // Keys shown by the pipeline view (queue chips / restore panel) must not ALSO
  // appear as raw tiles when `steps` is present.
  const HIDDEN_TILE_LABELS = [
    "queue completed", "queue total", "queue failed", "queue remaining", "queue restored",
    "queue skipped", "queue pre done", "phase", "subs total", "subs completed", "subs failed",
    "subs skipped", "subs remaining", "subs translating", "film", "steps", "films",
    "films more", "model", "elapsed", "processed frames", "remaining frames",
  ];

  it("with steps: pipeline replaces banner/queue bar/fps-ETA tiles; hidden keys get no tile", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={{ ...RESTORE, queue_skipped: 0, queue_pre_done: 0,
      model: "m · h", custom_metric: 7 }} />);
    expect(pipe()).toBeInTheDocument();
    for (const label of HIDDEN_TILE_LABELS) {
      expect(screen.queryByText(label)).toBeNull();
    }
    expect(screen.queryByText("当前文件")).toBeNull(); // old banner caption
    expect(screen.queryByText("帧率")).toBeNull();     // old fps tile
    expect(screen.queryByText("预计剩余")).toBeNull(); // old ETA tile
    // Gauges + VRAM bar stay; an unrelated unknown key still becomes a tile.
    expect(screen.getByText("GPU")).toBeInTheDocument();
    expect(screen.getByText("CPU")).toBeInTheDocument();
    expect(screen.getByText("MEM")).toBeInTheDocument();
    expect(screen.getByText("显存")).toBeInTheDocument();
    expect(screen.getByText("custom metric")).toBeInTheDocument();
  });

  it("without steps: today's banner, queue bar, fps/ETA and raw tiles (AV-off / Lada)", () => {
    setLang("zh-CN");
    const noSteps = Object.fromEntries(Object.entries(RESTORE).filter(
      ([k]) => !["steps", "films", "films_more", "film"].includes(k)));
    wrap(<MonitorMetrics metrics={noSteps} />);
    expect(screen.queryByTestId("pipeline-progress")).toBeNull();
    expect(screen.getByText("正在处理")).toBeInTheDocument();
    expect(screen.getByText("当前文件")).toBeInTheDocument();
    expect(screen.getByText("SDAB-312.mp4")).toBeInTheDocument();
    expect(screen.getByText(/0 \/ 1 完成/)).toBeInTheDocument();
    expect(screen.getByText("帧率")).toBeInTheDocument();
    expect(screen.getByText("预计剩余")).toBeInTheDocument();
    for (const label of ["phase", "subs total", "queue failed", "elapsed", "processed frames"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    expect(screen.queryByText("queue restored")).toBeNull(); // a known key since #189
  });

  it("without steps: queue_restored / queue_pre_done never become raw tiles (empty tracker)", () => {
    setLang("zh-CN");
    // AV-on Jasna with nothing pending / avsubs with every video already subtitled:
    // the tracker is empty, so no `steps`, but the queue counters are still emitted.
    wrap(<MonitorMetrics metrics={{ ...GAUGES, queue_completed: 3, queue_total: 3,
      queue_remaining: 0, queue_restored: 3, queue_pre_done: 3, custom_metric: 7 }} />);
    expect(screen.queryByTestId("pipeline-progress")).toBeNull();
    expect(screen.getByText(/3 \/ 3 完成/)).toBeInTheDocument();
    expect(screen.queryByText("queue restored")).toBeNull();
    expect(screen.queryByText("queue pre done")).toBeNull();
    expect(screen.getByText("custom metric")).toBeInTheDocument();
  });
});

describe("PipelineProgress — malformed input never crashes (#189)", () => {
  afterEach(reset);

  it("steps not an array → today's rendering (raw tile), no pipeline", () => {
    wrap(<MonitorMetrics metrics={{ ...GAUGES, steps: "abc", phase: "restore" }} />);
    expect(screen.queryByTestId("pipeline-progress")).toBeNull();
    expect(screen.getByText("steps")).toBeInTheDocument();
    expect(screen.getByText("phase")).toBeInTheDocument();
  });

  it("steps with no usable entry → today's rendering", () => {
    wrap(<MonitorMetrics metrics={{ ...GAUGES, steps: [null, 1, "x", { key: 3 },
      { key: "asr" }, { key: "asr", state: "bogus" }, []] }} />);
    expect(screen.queryByTestId("pipeline-progress")).toBeNull();
    expect(screen.getByText("GPU")).toBeInTheDocument();
  });

  it("an empty steps array → today's rendering", () => {
    wrap(<MonitorMetrics metrics={{ ...GAUGES, steps: [], queue_completed: 1, queue_total: 2 }} />);
    expect(screen.queryByTestId("pipeline-progress")).toBeNull();
    expect(screen.getByText(/1 \/ 2/)).toBeInTheDocument();
  });

  it("bad entries are skipped, wrong-typed fields ignored", () => {
    setLang("zh-CN");
    wrap(<MonitorMetrics metrics={{
      film: 42, current_file: "fallback.mp4",
      steps: [
        "junk",
        { key: "restore", state: "active", percent: "69", eta_s: Number.NaN, holder: 5,
          elapsed_s: Infinity, duration_s: "x" },
        { key: "asr", state: "waiting_gpu", holder: 7, waited_s: "long" },
      ],
      films: [null, 3, { name: 5 }, { name: "ok.mp4", steps: "x", status: 7, percent: "a",
        eta_s: null, duration_s: {} }, { name: "odd.mp4", steps: { restore: 1, asr: "bogus" },
        status: "bogus" }],
      films_more: "lots",
      queue_total: "1", queue_completed: null,
    }} />);
    expect(within(pipe()).getByText("fallback.mp4")).toBeInTheDocument();
    expect(within(pipe()).getByText("第 1 步 / 共 2 步")).toBeInTheDocument();
    expect(within(stepper()).getByText("进行中")).toBeInTheDocument();
    expect(within(stepper()).getByText("等待 GPU")).toBeInTheDocument();
    const rows = screen.getAllByTestId("film-row");
    expect(rows).toHaveLength(2);
    expect(within(rows[0]).getByText("ok.mp4")).toBeInTheDocument();
    expect(within(rows[1]).getByText("odd.mp4")).toBeInTheDocument();
    expect(screen.queryByText(/还有/)).toBeNull();
    expect(screen.queryByTestId("pipeline-queue")).toBeNull();
  });

  it("an unknown step key is labelled with the raw key", () => {
    wrap(<MonitorMetrics metrics={{ film: "f.mp4", steps: [{ key: "mux", state: "active" }] }} />);
    expect(within(stepper()).getByText("mux")).toBeInTheDocument();
  });

  it("readPipeline: kind from the step keys (restore ⇒ jasna), null when unusable", () => {
    expect(readPipeline({ steps: [{ key: "restore", state: "done" }] })?.kind).toBe("jasna");
    expect(readPipeline({ steps: [{ key: "asr", state: "done" }], queue_restored: 3 })?.kind)
      .toBe("avsubs");
    expect(readPipeline({})).toBeNull();
    expect(readPipeline({ steps: {} })).toBeNull();
    expect(readPipeline({ steps: [{ key: "", state: "done" }] })).toBeNull();
    const p = readPipeline({ steps: [{ key: "asr", state: "active", scene: 0, scenes: 5,
      percent: 250 }], films_more: -3 });
    expect(p?.steps[0].percent).toBe(100);   // clamped for the bar
    expect(p?.filmsMore).toBe(0);
  });
});
