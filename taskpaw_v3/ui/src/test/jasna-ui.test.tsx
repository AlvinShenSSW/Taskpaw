import { beforeEach, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { theme } from "../theme";
import { MonitorWizard } from "../views/MonitorWizard";
import { MonitorMetrics } from "../components/MonitorMetrics";
import { LtxDownload } from "../components/LtxDownload";
import { api, type PluginInfo } from "../api";
import catalog from "./fixtures/jasna-catalog.json";
import i18n from "../i18n";

beforeEach(() => { vi.restoreAllMocks(); void i18n.changeLanguage("zh-CN"); });
const field = (id: string) => document.getElementById(`root_${id}`) as HTMLInputElement;
function select(label: string, option: string) {
  fireEvent.mouseDown(screen.getByRole("combobox", { name: label }));
  fireEvent.click(screen.getByRole("option", { name: option }));
}
function wizard(mode: "edit" | "add" = "edit") {
  return render(<ThemeProvider theme={theme}><MonitorWizard mode={mode} existingType="jasna" name="J"
    existingConfig={mode === "edit" ? { name: "J", clip_size_1080p: 75, clip_size_4k: 55, cq: 27, ltx_large_canvas: true } : undefined}
    plugins={[catalog as PluginInfo]} presets={[]} onClose={vi.fn()} onDone={vi.fn()} onError={vi.fn()} /></ThemeProvider>);
}

it("opening edit preserves settings; both selectors fill both tiers and save manual edits", async () => {
  const update = vi.spyOn(api, "updateMonitor").mockResolvedValue({ ok: true } as never);
  wizard();
  expect(field("clip_size_4k")).toHaveValue(55);
  expect(field("ltx_large_canvas")).toBeChecked();
  fireEvent.change(field("cq"), { target: { value: "29" } });
  select("修复模型", "LTX");
  expect(field("clip_size_4k")).toHaveValue(55);
  select("设备型号", "RTX 5060 Ti · 16 GB");
  expect(field("clip_size_1080p")).toHaveValue(180);
  expect(field("clip_size_4k")).toHaveValue(90);
  expect(field("cq")).toHaveValue(29);
  expect(field("ltx_large_canvas")).not.toBeChecked();
  fireEvent.change(field("clip_size_4k"), { target: { value: "72" } });
  fireEvent.click(field("ltx_large_canvas"));
  fireEvent.change(field("cq"), { target: { value: "25" } });
  expect(field("clip_size_4k")).toHaveValue(72);
  fireEvent.submit(field("cq").closest("form")!);
  await waitFor(() => expect(update).toHaveBeenCalled());
  expect(update.mock.calls[0][1].config).toMatchObject({ clip_size_1080p: 180, clip_size_4k: 72, ltx_1080p: true, ltx_4k: true, ltx_large_canvas: true, cq: 25 });
}, 15000);

it("LTX quality is a select of the three zh tiers; a migrated high config keeps high (#283, #285)", async () => {
  const update = vi.spyOn(api, "updateMonitor").mockResolvedValue({ ok: true } as never);
  render(<ThemeProvider theme={theme}><MonitorWizard mode="edit" existingType="jasna" name="J"
    existingConfig={{ name: "J", ltx_model: "high" }}
    plugins={[catalog as PluginInfo]} presets={[]} onClose={vi.fn()} onDone={vi.fn()} onError={vi.fn()} /></ThemeProvider>);
  const box = screen.getByRole("combobox", { name: "LTX 画质" });
  expect(box).toHaveTextContent("高画质（慢约 2.5 倍）");
  fireEvent.mouseDown(box);
  expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([
    "标准画质（默认，最快）",
    "良好画质（慢约 1.5 倍）",
    "高画质（慢约 2.5 倍）",
  ]);
  fireEvent.keyDown(screen.getByRole("listbox"), { key: "Escape" });
  fireEvent.submit(field("cq").closest("form")!);
  await waitFor(() => expect(update).toHaveBeenCalled());
  expect(update.mock.calls[0][1].config).toMatchObject({ ltx_model: "high", match_source_bitrate: true, cq: 20 });
  expect(update.mock.calls[0][1].config).not.toHaveProperty("ltx_best");
}, 15000);

it("output bitrate follows the source: a zh tickbox next to CQ, on by default (#285)", () => {
  wizard();
  const box = field("match_source_bitrate");
  expect(box).toBeChecked();
  expect(screen.getByLabelText("输出码率跟随原片")).toBe(box);
  expect(field("cq")).toHaveValue(27); // a saved cq is kept
});

it("8GB preset keeps 4K on unet; changing GPU merges current fields and reapply is explicit", () => {
  wizard(); select("修复模型", "LTX"); select("设备型号", "RTX 5060 · 8 GB");
  expect(field("ltx_1080p")).toBeChecked();
  expect(field("ltx_4k")).not.toBeChecked();
  expect(field("unet4x_4k")).toBeChecked();
  fireEvent.change(field("cq"), { target: { value: "30" } });
  select("设备型号", "RTX 4090 · 24 GB");
  expect(field("ltx_fast")).not.toBeChecked();
  expect(field("clip_size_4k")).toHaveValue(180);
  expect(field("cq")).toHaveValue(30);
  fireEvent.change(field("clip_size_4k"), { target: { value: "90" } });
  fireEvent.click(screen.getByText("重新填入预设"));
  expect(field("clip_size_4k")).toHaveValue(180);
}, 15000);

it("VR switch applies once and leaves manual VR tuning editable", () => {
  wizard(); select("修复模型", "unet-4x"); select("设备型号", "RTX 5090 · 32 GB");
  fireEvent.click(field("vr_8k"));
  expect(field("clip_size_4k")).toHaveValue(30);
  expect(field("clip_size_4k")).not.toBeDisabled();
  fireEvent.change(field("clip_size_4k"), { target: { value: "45" } });
  fireEvent.change(field("cq"), { target: { value: "26" } });
  expect(field("clip_size_4k")).toHaveValue(45);
  fireEvent.click(field("vr_8k"));
  expect(field("clip_size_4k")).toHaveValue(180);
}, 15000);

it("LTX preset permits the independent VR switch and restores both LTX tiers when cleared", () => {
  wizard(); select("修复模型", "LTX"); select("设备型号", "RTX 4090 · 24 GB");
  const vr = screen.getByRole("checkbox", { name: "8K VR" });
  expect(vr).toHaveAttribute("id", "root_vr_8k");
  expect(vr).not.toBeDisabled();
  expect(screen.queryByText(/输入 y/)).not.toBeInTheDocument();
  fireEvent.click(vr);
  expect(field("ltx_1080p")).not.toBeChecked();
  expect(field("ltx_4k")).not.toBeChecked();
  expect(field("clip_size_4k")).toHaveValue(30);
  expect(field("clip_size_4k")).not.toBeDisabled();
  fireEvent.change(field("clip_size_4k"), { target: { value: "48" } });
  fireEvent.change(field("cq"), { target: { value: "26" } });
  expect(field("clip_size_4k")).toHaveValue(48);
  fireEvent.click(field("vr_8k"));
  expect(field("ltx_1080p")).toBeChecked();
  expect(field("ltx_4k")).toBeChecked();
  expect(field("clip_size_4k")).toHaveValue(180);
  expect(field("cq")).toHaveValue(26);
}, 15000);

it("stage 100% never becomes whole-film 100%; stage card resets for BasicVSR++", () => {
  const { rerender } = render(<MonitorMetrics metrics={{ current_file: "a.mp4", ltx_stage: "3/4 denoise", percent: 100, eta: "04:57" }} />);
  expect(screen.getByText("阶段 3 / 4")).toBeInTheDocument();
  expect(screen.getByText("当前 LTX 本轮预计剩余").parentElement).toHaveTextContent("04:57");
  expect(screen.getByRole("progressbar")).toHaveAccessibleName("denoise · 第 3 / 4 阶段进度");
  rerender(<MonitorMetrics metrics={{ current_file: "b.mp4", percent: 5 }} />);
  expect(screen.queryByTestId("ltx-progress")).not.toBeInTheDocument();
  rerender(<MonitorMetrics metrics={{ ltx_stage: "9/4 scan", percent: NaN }} />);
  expect(screen.queryByTestId("ltx-progress")).not.toBeInTheDocument();
});

const prompt = { state: "awaiting", request_id: "r1", total_bytes: 9e9, files: ["model.enc"], destination: "C:/Jasna/model_weights/ltx-restore" };
it("LTX stage card remains visible with the subtitle pipeline, without whole-restore percent", () => {
  render(<MonitorMetrics metrics={{ current_file: "a.mp4", film: "a.mp4", ltx_stage: "3/4 denoise", percent: 100,
    steps: [{ key: "restore", state: "active" }, { key: "asr", state: "pending" }, { key: "translate", state: "pending" }],
    films: [], films_more: 0 }} />);
  expect(screen.getByTestId("pipeline-progress")).toBeInTheDocument();
  expect(screen.getByTestId("ltx-progress")).toBeInTheDocument();
  expect(screen.getAllByText("100%")).toHaveLength(1);
});

it("add review/back preserves both presets and subsequent manual tuning", async () => {
  const add = vi.spyOn(api, "addMonitor").mockResolvedValue({ ok: true } as never);
  wizard("add");
  fireEvent.click(screen.getByText("Jasna"));
  fireEvent.click(screen.getByText("继续"));
  expect(field("ltx_large_canvas")).not.toBeChecked();
  fireEvent.change(field("name"), { target: { value: "mixed" } });
  select("修复模型", "普通（BasicVSR++）"); select("设备型号", "RTX 5070 Ti · 16 GB");
  fireEvent.change(field("clip_size_4k"), { target: { value: "75" } });
  fireEvent.submit(field("name").closest("form")!);
  fireEvent.click(screen.getByText("返回"));
  expect(field("clip_size_4k")).toHaveValue(75);
  expect(field("clip_size_1080p")).toHaveValue(180);
  fireEvent.submit(field("name").closest("form")!);
  fireEvent.click(screen.getByText("添加监控"));
  await waitFor(() => expect(add).toHaveBeenCalled());
  expect(add.mock.calls[0][0].config).toMatchObject({ name: "mixed", clip_size_4k: 75, ltx_1080p: false, unet4x_1080p: false });
}, 15000);

it("download accepts only displayed request; paused/error can retry; Hub is read-only", async () => {
  const respond = vi.spyOn(api, "respondPrompt").mockResolvedValue({ ok: true });
  const changed = vi.fn();
  const { rerender } = render(<LtxDownload value={prompt} name="J" onChanged={changed} />);
  fireEvent.click(screen.getByText("暂不下载"));
  await waitFor(() => expect(changed).toHaveBeenCalled());
  expect(respond).toHaveBeenCalledWith("J", "r1", false);
  expect(screen.getByText("下载并继续")).toBeDisabled();
  rerender(<LtxDownload value={{ ...prompt, state: "paused", request_id: "r2" }} name="J" />);
  fireEvent.click(screen.getByText("下载并继续"));
  await waitFor(() => expect(respond).toHaveBeenCalledWith("J", "r2", true));
  rerender(<LtxDownload value={{ ...prompt, state: "error", request_id: "r3", error: "disk full" }} />);
  expect(screen.getByText("disk full")).toBeInTheDocument();
  expect(screen.queryByRole("button")).not.toBeInTheDocument();
  rerender(<LtxDownload value={{ ...prompt, state: "downloading", downloaded_bytes: 4.5e9 }} name="J" />);
  expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");
});
