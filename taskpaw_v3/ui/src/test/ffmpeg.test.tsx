import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { api, type FfmpegStatus, type PluginInfo } from "../api";
import { FfmpegReminder } from "../components/FfmpegReminder";
import { ffmpegState } from "../components/FfmpegReminder.helpers";
import { MonitorWizard } from "../views/MonitorWizard";
import { theme } from "../theme";
import i18n from "../i18n";

const SCRIPT = "& {\n    Write-Output 'server script'\n}";
const EXE = "C:\\WhisperJAV\\Scripts\\whisperjav.exe";
const FOUND = "C:\\WhisperJAV\\Library\\bin\\ffmpeg.exe";
const base: FfmpegStatus = {
  on_path: null, bundled: null, effective: null, exe_ok: true,
  saved_path_ok: null, pending_restart: false, candidates: [],
  platform: "windows", error: false, script: SCRIPT,
};
const reply = (body: unknown) => ({ ok: true, json: async () => body }) as Response;
function stubStatus(status: Partial<FfmpegStatus> = {}) {
  const fetcher = vi.fn(async (url: string) => {
    if (new URL(url).pathname === "/control/ffmpeg") return reply({ ...base, ...status });
    if (new URL(url).pathname === "/control/monitors") return reply({ ok: true });
    throw new Error(`Unexpected URL: ${url}`);
  });
  vi.stubGlobal("fetch", fetcher);
  return fetcher;
}
const wrap = (ui: React.ReactNode) => render(<ThemeProvider theme={theme}>{ui}</ThemeProvider>);
const tick = (ms = 400) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });
const clipboard = vi.fn();

beforeEach(async () => {
  vi.useFakeTimers();
  await i18n.changeLanguage("zh-CN");
  clipboard.mockReset().mockResolvedValue(undefined);
  vi.spyOn(navigator, "clipboard", "get").mockReturnValue({ writeText: clipboard } as unknown as Clipboard);
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// jsdom does not provide the clipboard API.
Object.defineProperty(navigator, "clipboard", { configurable: true, get: () => undefined });

describe("FFmpeg reminder states (#204)", () => {
  const cases: Array<[string, Partial<FfmpegStatus>, string, string, string]> = [
    ["path", { on_path: FOUND, effective: FOUND }, "status", "已找到 FFmpeg：", "FFmpeg found:"],
    ["bundled", { bundled: FOUND, effective: FOUND }, "status", "将自动使用 WhisperJAV 自带的 FFmpeg：", "WhisperJAV's bundled FFmpeg will be used automatically:"],
    ["restart", { pending_restart: true, saved_path_ok: FOUND }, "status", "FFmpeg 已加入 PATH", "FFmpeg has been added to PATH"],
    ["neutral", { exe_ok: false }, "status", "填写 whisperjav.exe 的完整路径后", "Enter the full path to whisperjav.exe"],
    ["windows", {}, "alert", "AV 翻译识别（WhisperJAV）需要 FFmpeg", "AV speech recognition (WhisperJAV) needs FFmpeg"],
    ["other", { platform: "other", exe_ok: false, script: null }, "status", "请安装 FFmpeg 并将它加入 PATH", "Install FFmpeg and add it to PATH"],
    ["error", { error: true, effective: FOUND, on_path: FOUND }, "status", "无法检查 FFmpeg", "Unable to check FFmpeg"],
  ];
  for (const lang of ["zh-CN", "en"]) {
    it.each(cases)(`${lang}: %s state, role and script visibility`, async (state, status, role, zh, en) => {
      await i18n.changeLanguage(lang);
      stubStatus(status);
      wrap(<FfmpegReminder whisperjav={EXE} />);
      await tick();
      expect(screen.getByRole(role)).toHaveTextContent(lang === "en" ? en : zh);
      expect(screen.queryByRole(role === "alert" ? "status" : "alert")).not.toBeInTheDocument();
      const copy = screen.queryByRole("button", { name: lang === "en" ? "Copy script" : "复制脚本" });
      if (state === "windows") {
        expect(copy).toHaveAttribute("type", "button");
        expect(copy).toHaveStyle({ minHeight: "40px" });
        const script = screen.getByRole("region", { name: lang === "en" ? "PowerShell setup script" : "PowerShell 设置脚本" });
        expect(script.textContent).toBe(SCRIPT);
        expect(script).toHaveStyle({ overflowX: "auto" });
        expect(script).not.toHaveAttribute("contenteditable");
        expect(screen.getByRole("alert")).toHaveTextContent(lang === "en" ? "current Windows user only" : "只修改当前 Windows 用户的设置");
        expect(screen.getByRole("alert")).toHaveTextContent(lang === "en" ? "normal PowerShell window" : "普通 PowerShell 窗口");
        expect(screen.getByRole("alert")).toHaveTextContent(".ps1");
        expect(screen.getByRole("alert")).toHaveTextContent(lang === "en" ? "Start menu" : "再从开始菜单重新打开");
      } else {
        expect(copy).not.toBeInTheDocument();
        expect(screen.queryByRole("region")).not.toBeInTheDocument();
      }
      if (state === "error") expect(screen.getByRole("status")).toHaveTextContent("README");
      if (state === "path" || state === "bundled") {
        expect(screen.getByRole("status")).toHaveTextContent(FOUND);
        expect(screen.getByRole("status")).toHaveStyle({ overflowWrap: "anywhere", minWidth: 0 });
      }
    });

    it(`${lang}: candidate hint and clipboard success/failure`, async () => {
      await i18n.changeLanguage(lang);
      stubStatus({ candidates: [{ dir: "C:\\found", exists: true }, { dir: "C:\\absent", exists: false }] });
      wrap(<FfmpegReminder whisperjav={EXE} />);
      await tick();
      expect(screen.getByRole("alert")).toHaveTextContent(lang === "en" ? "Found ffmpeg.exe in C:\\found" : "已在 C:\\found 找到 ffmpeg.exe");
      expect(screen.getByRole("alert")).not.toHaveTextContent("C:\\absent");
      const copy = screen.getByRole("button", { name: lang === "en" ? "Copy script" : "复制脚本" });
      await act(async () => { fireEvent.click(copy); });
      expect(clipboard).toHaveBeenCalledWith(SCRIPT);
      expect(copy).toHaveTextContent(lang === "en" ? "Copied" : "已复制");
      clipboard.mockRejectedValueOnce(new Error("denied"));
      await act(async () => { fireEvent.click(copy); });
      expect(screen.getByRole("alert")).toHaveTextContent(lang === "en" ? "Copy failed; select the script and copy it manually" : "复制失败，请手动选中复制");
    });
  }

  it("applies the frozen precedence and trusts exe_ok, without client path validation", () => {
    expect(ffmpegState({ ...base, error: true, on_path: FOUND, effective: FOUND })).toBe("error");
    expect(ffmpegState({ ...base, on_path: FOUND, bundled: FOUND, effective: FOUND, pending_restart: true })).toBe("path");
    expect(ffmpegState({ ...base, bundled: FOUND, effective: FOUND, pending_restart: true })).toBe("bundled");
    expect(ffmpegState({ ...base, exe_ok: false, pending_restart: true })).toBe("restart");
    expect(ffmpegState({ ...base, exe_ok: false })).toBe("neutral");
    expect(ffmpegState({ ...base, exe_ok: false, platform: "other" })).toBe("other");
    expect(ffmpegState(base)).toBe("windows");
  });

  it("debounces 400 ms, encodes the live path, and lets the newest response win", async () => {
    const pending: Array<(value: Response) => void> = [];
    const fetcher = vi.fn((url: string) => {
      expect(new URL(url).pathname).toBe("/control/ffmpeg");
      return new Promise<Response>((resolve) => pending.push(resolve));
    });
    vi.stubGlobal("fetch", fetcher);
    const view = wrap(<FfmpegReminder whisperjav="first" />);
    await tick(399);
    expect(fetcher).not.toHaveBeenCalled();
    await tick(1);
    view.rerender(<ThemeProvider theme={theme}><FfmpegReminder whisperjav="typing" /></ThemeProvider>);
    await tick(200);
    const newest = "C:\\A & B\\Scripts\\whisperjav.exe";
    view.rerender(<ThemeProvider theme={theme}><FfmpegReminder whisperjav={newest} /></ThemeProvider>);
    await tick(399);
    expect(fetcher).toHaveBeenCalledTimes(1);
    await tick(1);
    expect(new URL(fetcher.mock.calls[1][0]).searchParams.get("whisperjav")).toBe(newest);
    await act(async () => { pending[1](reply({ ...base, on_path: "new", effective: "new" })); });
    await act(async () => { pending[0](reply({ ...base, error: true })); });
    expect(screen.getByRole("status")).toHaveTextContent("已找到 FFmpeg：new");
    expect(fetcher).toHaveBeenCalledTimes(2);
  });

  it.each(["zh-CN", "en"])("%s: request failure points to README without inventing a script", async (lang) => {
    await i18n.changeLanguage(lang);
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      expect(new URL(url).pathname).toBe("/control/ffmpeg");
      throw new Error("offline");
    }));
    wrap(<FfmpegReminder whisperjav={EXE} />);
    await tick();
    expect(screen.getByRole("status")).toHaveTextContent(lang === "en" ? "Unable to check FFmpeg" : "无法检查 FFmpeg");
    expect(screen.getByRole("status")).toHaveTextContent("FFmpeg（AV 翻译识别需要）");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("supports the optional API parameter", async () => {
    const fetcher = stubStatus();
    await api.ffmpeg();
    expect(new URL(fetcher.mock.calls[0][0]).pathname).toBe("/control/ffmpeg");
    expect(new URL(fetcher.mock.calls[0][0]).search).toBe("");
  });

  it("keeps translation keys in parity", () => {
    expect(Object.keys(i18n.getResource("zh-CN", "translation", "ffmpeg")).sort())
      .toEqual(Object.keys(i18n.getResource("en", "translation", "ffmpeg")).sort());
  });
});

function plugin(type_id: string): PluginInfo {
  return {
    type_id, display_name: type_id, category: "task", config_version: 1, system: false,
    json_schema: { type: "object", properties: {
      name: { type: "string", title: "Name" },
      whisperjav_exe_path: { type: "string", title: "WhisperJAV path" },
      av_translate: { type: "boolean", title: "AV translate", default: false },
    } }, ui_schema: {},
  };
}
const props = { plugins: [plugin("avsubs"), plugin("jasna"), plugin("lada")], presets: [], onClose: vi.fn(), onDone: vi.fn(), onError: vi.fn() };
function choose(id: string) {
  fireEvent.click(screen.getByRole("button", { name: new RegExp(`^${id}`) }));
  fireEvent.click(screen.getByRole("button", { name: "继续" }));
}

describe("wizard FFmpeg integration", () => {
  it("avsubs always shows above Review; copy does not submit, Review still submits", async () => {
    stubStatus();
    wrap(<MonitorWizard mode="add" {...props} />);
    choose("avsubs");
    await tick();
    const reminder = screen.getByRole("alert");
    const submit = screen.getByRole("button", { name: "复核" });
    expect(reminder.compareDocumentPosition(submit) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(submit).toHaveAttribute("type", "submit");
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "复制脚本" })); });
    expect(submit).toBeInTheDocument();
    await act(async () => { fireEvent.click(submit); });
    expect(screen.getByRole("button", { name: "添加监控" })).toBeInTheDocument();
  });

  it("Jasna follows live AV toggle and resets on service change; other types never show", async () => {
    const fetcher = stubStatus();
    wrap(<MonitorWizard mode="add" {...props} />);
    choose("jasna");
    await tick();
    expect(fetcher).not.toHaveBeenCalled();
    const av = screen.getByRole("checkbox");
    fireEvent.click(av);
    await tick();
    expect(screen.getByRole("alert")).toBeInTheDocument();
    fireEvent.click(av);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    fireEvent.click(av);
    await tick();
    fireEvent.click(screen.getByRole("button", { name: "返回" }));
    choose("lada");
    fireEvent.click(screen.getByRole("checkbox"));
    await tick();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "返回" }));
    choose("jasna");
    await tick();
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("edit Jasna with AV enabled checks the seeded path without interaction", async () => {
    const fetcher = stubStatus({ bundled: FOUND, effective: FOUND });
    wrap(<MonitorWizard mode="edit" name="saved" existingType="jasna"
      existingConfig={{ name: "saved", av_translate: true, whisperjav_exe_path: EXE }} {...props} />);
    await tick();
    expect(screen.getByRole("status")).toHaveTextContent("将自动使用");
    expect(new URL(fetcher.mock.calls[0][0]).searchParams.get("whisperjav")).toBe(EXE);
  });

  it.each(["avsubs", "jasna"])("N3-1: %s edit saves the NEW typed path after the status update", async (type) => {
    const saved: unknown[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const parsed = new URL(url);
      if (parsed.pathname === "/control/ffmpeg") return reply({ ...base, effective: parsed.searchParams.get("whisperjav"), on_path: parsed.searchParams.get("whisperjav") });
      if (parsed.pathname === "/control/monitors") { saved.push(JSON.parse(init!.body as string)); return reply({ ok: true }); }
      throw new Error(`Unexpected URL: ${url}`);
    }));
    wrap(<MonitorWizard mode="edit" name="saved" existingType={type}
      existingConfig={{ name: "saved", av_translate: true, whisperjav_exe_path: EXE }} {...props} />);
    await tick();
    const input = screen.getByRole("textbox", { name: /whisperjav/i });
    const newest = "D:\\new\\Scripts\\whisperjav.exe";
    fireEvent.change(input, { target: { value: newest } });
    await tick(450);
    expect(screen.getByRole("status")).toHaveTextContent(newest);
    expect(input).toHaveValue(newest);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "保存更改" })); });
    expect(saved).toEqual([{ config: expect.objectContaining({ whisperjav_exe_path: newest }) }]);
  });
});
