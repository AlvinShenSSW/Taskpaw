// #336: the 功耗 sensor line — CPU / GPU temperature and fan RPM from LibreHardwareMonitor.
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "@mui/material/styles";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type AgentConfigView, type MachineSensors, type PowerView } from "../api";
import { PowerPage } from "../components/power/PowerPage";
import { INSTALL_CMD, SETUP_CMD } from "../components/power/LhmGuide";
import { SensorLine } from "../components/power/SensorLine";
import { theme } from "../theme";
import { POWER_SETTINGS, powerView } from "./fixtures/power";

const LHM: MachineSensors = {
  at: 1792920000, source: "lhm", cpu_c: 68.4, gpu_c: 40.2,
  fans: [{ name: "CPU Fan", rpm: 1218 }, { name: "SYS Fan1", rpm: 2554 }],
};

function sensorView(overrides: Partial<PowerView> = {}): PowerView {
  return { ...powerView(), sensors: LHM, sensors_age_s: 4, ...overrides };
}

const clients: QueryClient[] = [];
function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  clients.push(qc);
  return render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}><PowerPage /></QueryClientProvider></ThemeProvider>);
}
const tick = async (ms = 20) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
const line = (id = "local") => document.querySelector(`[data-machine="${id}"] .m-sensors`) as HTMLElement;
const meta = (id = "local") => line(id).querySelector(".meta");
const only = () => document.querySelector(".m-sensors") as HTMLElement;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date", "performance"] });
  vi.spyOn(api, "workflowFilms").mockResolvedValue({ state: "disabled" } as never);
  vi.spyOn(api, "config").mockResolvedValue({ monitors: [], power: POWER_SETTINGS } as unknown as AgentConfigView);
});
afterEach(() => {
  cleanup(); clients.splice(0).forEach((c) => c.clear()); vi.useRealTimers(); vi.restoreAllMocks();
});

describe("sensor line states", () => {
  it("shows CPU, GPU and every fan with the source and age", () => {
    render(<SensorLine machine="GPU-02" sensors={LHM} ageS={4} receivedMs={1000} nowMs={1000} failed={false} />);
    expect(only()).toHaveTextContent(/CPU\s*68\s*°C/);
    expect(only()).toHaveTextContent(/GPU\s*40\s*°C/);
    expect(only()).toHaveTextContent(/风扇\s*CPU Fan\s*1218\s*RPM\s*\/\s*SYS Fan1\s*2554\s*RPM/);
    expect(only().querySelector(".meta")).toHaveTextContent("LHM · 4 秒前");
    expect(only()).not.toHaveClass("stale");
    expect(only()).not.toHaveTextContent("未运行");
  });

  it("does not render items without a value", () => {
    render(<SensorLine machine="GPU-02" sensors={{ ...LHM, cpu_c: null, fans: [] }} ageS={1} receivedMs={0} nowMs={0} failed={false} />);
    expect(only()).not.toHaveTextContent("CPU");
    expect(only()).not.toHaveTextContent("风扇");
    expect(only()).toHaveTextContent(/GPU\s*40\s*°C/);
  });

  it("shows GPU from nvidia-smi and says LHM is not running", () => {
    const sensors: MachineSensors = { at: 1, source: "nvidia-smi", cpu_c: null, gpu_c: 37, fans: [] };
    render(<SensorLine machine="GPU-02" sensors={sensors} ageS={5} receivedMs={0} nowMs={0} failed={false} />);
    expect(only()).toHaveTextContent(/GPU\s*37\s*°C/);
    expect(only().querySelector(".unavailable")).toHaveTextContent("CPU / 风扇：LHM 未运行");
    expect(only().querySelector(".meta")).toHaveTextContent("nvidia-smi · 5 秒前");
  });

  it("says LHM is not running when there is no reading at all", () => {
    const sensors: MachineSensors = { at: 1, source: null, cpu_c: null, gpu_c: null, fans: [] };
    render(<SensorLine machine="GPU-02" sensors={sensors} ageS={2} receivedMs={0} nowMs={0} failed={false} />);
    expect(only()).not.toHaveTextContent("°C");
    expect(only().querySelector(".unavailable")).toHaveTextContent("CPU / 风扇：LHM 未运行");
  });

  it.each([undefined, null])("asks to upgrade an agent that sends no sensors (%s)", (sensors) => {
    render(<SensorLine machine="GPU-02" sensors={sensors} ageS={3} receivedMs={0} nowMs={0} failed={false} />);
    expect(only()).toHaveTextContent("传感器未提供 · 需升级 TaskPaw");
    expect(only().querySelector(".meta")).toBeNull();
  });

  it("turns amber after 30 seconds and keeps the values", () => {
    render(<SensorLine machine="GPU-02" sensors={LHM} ageS={95} receivedMs={0} nowMs={0} failed={false} />);
    expect(only()).toHaveClass("stale");
    expect(only()).toHaveTextContent(/CPU\s*68\s*°C/);
    expect(only().querySelector(".meta")).toHaveTextContent("已过期 · 最后读数 95 秒前");
  });

  it("shows a lost connection through the same stale path", () => {
    render(<SensorLine machine="GPU-02" sensors={LHM} ageS={2} receivedMs={0} nowMs={0} failed />);
    expect(only()).toHaveClass("stale");
    expect(only().querySelector(".meta")).toHaveTextContent("已过期 · 最后读数 2 秒前");
  });

  it.each([[null, 0], [3, undefined]])("says the sample time is unknown without an age (%s, %s)", (ageS, receivedMs) => {
    render(<SensorLine machine="GPU-02" sensors={LHM} ageS={ageS} receivedMs={receivedMs} nowMs={0} failed={false} />);
    expect(only()).not.toHaveClass("stale");
    expect(only().querySelector(".meta")).toHaveTextContent("LHM · 采样时间未知");
  });
});

describe("sensor age on the page", () => {
  it("adds the seconds since the browser received the response to the controller's age", async () => {
    vi.spyOn(api, "power").mockResolvedValueOnce(sensorView({ sensors_age_s: 25 }))
      .mockImplementation(() => new Promise(() => {}));
    mount(); await tick();
    expect(meta()).toHaveTextContent("LHM · 25 秒前");
    expect(line()).not.toHaveClass("stale");
    await tick(4000);
    expect(meta()).toHaveTextContent("LHM · 29 秒前");
    await tick(2000);
    expect(line()).toHaveClass("stale");
    expect(meta()).toHaveTextContent("已过期 · 最后读数 31 秒前");
  });

  it("restarts from a new response's age", async () => {
    const power = vi.spyOn(api, "power").mockResolvedValue(sensorView({ sensors_age_s: 40 }));
    mount(); await tick();
    expect(line()).toHaveClass("stale");
    power.mockResolvedValue(sensorView({ sensors_age_s: 1 }));
    await tick(5000);
    expect(line()).not.toHaveClass("stale");
    expect(meta()).toHaveTextContent("LHM · 1 秒前");
  });

  it("maps each peer's own sensors and age, and an old peer asks to upgrade", async () => {
    const view = sensorView();
    view.peers = view.peers!.map((p, i) => i === 0
      ? { ...p, sensors: { at: 1, source: "nvidia-smi", cpu_c: null, gpu_c: 74, fans: [] }, sensors_age_s: 6 }
      : i === 2 ? { ...p, sensors: null, sensors_age_s: 2 } : p);
    vi.spyOn(api, "power").mockResolvedValue(view);
    mount(); await tick();
    expect(line()).toHaveTextContent(/CPU\s*68\s*°C/);
    expect(line("peer:GPU-02")).toHaveTextContent(/GPU\s*74\s*°C/);
    expect(line("peer:GPU-02")).toHaveTextContent("LHM 未运行");
    expect(line("peer:GPU-02").querySelector(".meta")).toHaveTextContent("nvidia-smi · 6 秒前");
    expect(line("peer:GPU-04")).toHaveTextContent("传感器未提供 · 需升级 TaskPaw");
  });

  it("marks every line stale when the power view cannot be read", async () => {
    vi.spyOn(api, "power").mockResolvedValueOnce(sensorView({ sensors_age_s: 2 })).mockRejectedValue(new Error("disconnected"));
    mount(); await tick();
    expect(line()).not.toHaveClass("stale");
    await tick(5000);
    expect(line()).toHaveClass("stale");
    expect(line()).toHaveTextContent(/CPU\s*68\s*°C/);
    expect(meta()).toHaveTextContent(/已过期 · 最后读数 \d+ 秒前/);
  });
});

// #345: the inline LibreHardwareMonitor install guide. jsdom does not provide the clipboard API.
Object.defineProperty(navigator, "clipboard", { configurable: true, get: () => undefined });
const NO_LHM: MachineSensors = { at: 1, source: "nvidia-smi", cpu_c: null, gpu_c: 37, fans: [] };
const guideLink = () => document.querySelector(".guide-link") as HTMLButtonElement | null;
const guide = () => document.querySelector(".m-guide") as HTMLElement | null;
const pres = () => [...document.querySelectorAll(".m-guide pre")].map((p) => p.textContent);
const copyButtons = () => screen.getAllByRole("button", { name: /复制/ });
const selected = () => window.getSelection()?.toString();

describe("LHM install guide", () => {
  it.each([NO_LHM, { ...NO_LHM, source: null, gpu_c: null }])("offers the steps when LHM is not running (%#)", (sensors) => {
    render(<SensorLine machine="GPU-02" sensors={sensors as MachineSensors} ageS={5} receivedMs={0} nowMs={0} failed={false} />);
    expect(guideLink()).toHaveTextContent("安装步骤");
    expect(guideLink()).toHaveAttribute("type", "button");
    expect(guideLink()).toHaveAttribute("aria-expanded", "false");
    expect(guideLink()!.parentElement).toBe(only());
    expect(guide()).toBeNull();
  });

  it.each([LHM, null, undefined])("offers no steps with LHM running or without sensors (%#)", (sensors) => {
    render(<SensorLine machine="GPU-02" sensors={sensors} ageS={5} receivedMs={0} nowMs={0} failed={false} />);
    expect(guideLink()).toBeNull();
  });

  it("toggles the guide right after the line with the machine name and both commands", () => {
    render(<SensorLine machine="GPU-02" sensors={NO_LHM} ageS={5} receivedMs={0} nowMs={0} failed={false} />);
    fireEvent.click(guideLink()!);
    expect(guideLink()).toHaveTextContent("收起安装步骤");
    expect(guideLink()).toHaveAttribute("aria-expanded", "true");
    expect(only().nextElementSibling).toBe(guide());
    expect(guide()!.querySelector(".lead")).toHaveTextContent("在 GPU-02 上装一次 LibreHardwareMonitor，这一行就会显示 CPU 温度和风扇转速。");
    expect(guide()!.querySelector("ol li b")).toHaveTextContent("GPU-02");
    expect(guide()!.querySelectorAll("ol li")).toHaveLength(3);
    expect(pres()).toEqual([INSTALL_CMD, SETUP_CMD]);
    expect(guide()!.querySelector(".note")).toHaveTextContent("只需做一次；TaskPaw 不会自动安装或远程推送。");
    fireEvent.click(guideLink()!);
    expect(guideLink()).toHaveTextContent(/^安装步骤$/);
    expect(guide()).toBeNull();
  });

  it("keeps the commands verbatim, backslashes and angle brackets included", () => {
    expect(INSTALL_CMD).toBe("winget install --id LibreHardwareMonitor.LibreHardwareMonitor -e --accept-source-agreements");
    expect(SETUP_CMD).toContain('"$env:LOCALAPPDATA\\Microsoft\\WinGet\\Packages","$env:ProgramFiles\\WinGet\\Packages","$env:ProgramFiles\\LibreHardwareMonitor"');
    expect(SETUP_CMD).toContain('-User "$env:USERDOMAIN\\$env:USERNAME"');
    expect(SETUP_CMD).toContain('<add key="listenerIp" value="127.0.0.1" />');
    expect(SETUP_CMD).toContain('New-NetFirewallRule -DisplayName "LibreHardwareMonitor 8085 LAN block" -Direction Inbound -Protocol TCP -LocalPort 8085 -Action Block');
    expect(SETUP_CMD.split("\n")).toHaveLength(14);
    expect(SETUP_CMD).not.toMatch(/&lt;|&gt;|^\s|\s$/);
  });

  it("copies a command and confirms for 1.5 seconds", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.spyOn(navigator, "clipboard", "get").mockReturnValue({ writeText } as unknown as Clipboard);
    render(<SensorLine machine="GPU-02" sensors={NO_LHM} ageS={5} receivedMs={0} nowMs={0} failed={false} />);
    fireEvent.click(guideLink()!);
    await act(async () => { fireEvent.click(copyButtons()[1]); });
    expect(writeText).toHaveBeenCalledWith(SETUP_CMD);
    expect(copyButtons().map((b) => b.textContent)).toEqual(["复制", "已复制"]);
    await tick(1400);
    expect(copyButtons()[1]).toHaveTextContent("已复制");
    await tick(100);
    expect(copyButtons()[1]).toHaveTextContent(/^复制$/);
  });

  it("selects the command text when the clipboard is unavailable", async () => {
    render(<SensorLine machine="GPU-02" sensors={NO_LHM} ageS={5} receivedMs={0} nowMs={0} failed={false} />);
    fireEvent.click(guideLink()!);
    await act(async () => { fireEvent.click(copyButtons()[0]); });
    expect(selected()).toBe(INSTALL_CMD);
    expect(copyButtons()[0]).toHaveTextContent(/^复制$/);
  });

  it("selects the command text when the clipboard refuses", async () => {
    const writeText = vi.fn().mockRejectedValue(new Error("denied"));
    vi.spyOn(navigator, "clipboard", "get").mockReturnValue({ writeText } as unknown as Clipboard);
    render(<SensorLine machine="GPU-02" sensors={NO_LHM} ageS={5} receivedMs={0} nowMs={0} failed={false} />);
    fireEvent.click(guideLink()!);
    await act(async () => { fireEvent.click(copyButtons()[1]); });
    expect(writeText).toHaveBeenCalledWith(SETUP_CMD);
    expect(selected()).toBe(SETUP_CMD);
    expect(copyButtons()[1]).toHaveTextContent(/^复制$/);
  });

  it("names each machine row in its own guide on the page", async () => {
    const view = sensorView();
    view.peers = view.peers!.map((p, i) => i === 0 ? { ...p, sensors: NO_LHM, sensors_age_s: 6 } : p);
    vi.spyOn(api, "power").mockResolvedValue(view);
    mount(); await tick();
    expect(line().querySelector(".guide-link")).toBeNull();
    fireEvent.click(line("peer:GPU-02").querySelector(".guide-link")!);
    const row = document.querySelector('[data-machine="peer:GPU-02"]')!;
    expect(row.querySelector(":scope > .m-guide .lead")).toHaveTextContent("在 GPU-02 上装一次 LibreHardwareMonitor");
  });
});
