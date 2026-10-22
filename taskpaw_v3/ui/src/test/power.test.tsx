// PWR-3 「功耗」 tab (approved preview power-v3.html) over the backend's shapes: the local machine plus
// peers, every figure an estimate, no slogan heading; the 估算参数 dialog with the calibration helper
// and the peer list. 每片能耗 reads the media workflow's contract fixture.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { api, HttpError, type AgentConfigView, type LivePower, type PowerHistory, type WorkflowFilms } from "../api";
import { PowerPage } from "../components/power/PowerPage";
import { PowerParams } from "../components/power/PowerParams";
import { chartSeries, machinesOf, stateChip } from "../components/power/powerView";
import { fmtKwh, fmtKwhShort, predictFromRates } from "../components/power/powerModel";
import { EnergyBlock, PredictLine } from "../components/power/PowerParts";
import { theme } from "../theme";
import { setLang } from "../i18n";
import contract from "./fixtures/workflow-contract.json";
import { POWER_SETTINGS, powerView } from "./fixtures/power";

const FILMS = (contract as unknown as { films: WorkflowFilms }).films;

const wrap = (ui: ReactNode) => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}>{ui}</QueryClientProvider></ThemeProvider>);
};

function mockApi() {
  const power = vi.spyOn(api, "power").mockImplementation(async (range) => powerView(range));
  vi.spyOn(api, "workflowFilms").mockResolvedValue(FILMS);
  vi.spyOn(api, "config").mockResolvedValue({ monitors: [], power: POWER_SETTINGS } as unknown as AgentConfigView);
  const update = vi.spyOn(api, "updateConfig").mockResolvedValue({ ok: true, restart_required: false } as never);
  return { power, update };
}

beforeEach(() => { setLang("zh-CN"); });
afterEach(() => { vi.restoreAllMocks(); });

describe("功耗 tab", () => {
  it("has a compact title row, the plug notice and the four stats", async () => {
    mockApi();
    wrap(<PowerPage />);
    expect(await screen.findByRole("heading", { level: 1, name: "功耗" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);  // no slogan heading (decision 7)
    expect(screen.getByText("整机实测请以智能插座为准；此处为估算，用于看分布和每片能耗。")).toBeInTheDocument();
    const all = await screen.findByTestId("stat-all");
    // local 286 + GPU-02 172 + 翻译机 61; peers without power are not summed
    expect(all).toHaveTextContent("519W · 3 台");
    expect(screen.getByTestId("stat-energy")).toHaveTextContent(`${fmtKwhShort(4550 + 1920 + 830)}度 · 今日`);
    expect(screen.getByTestId("stat-jasna")).toHaveTextContent("458W · GPU 机器");
    expect(screen.getByTestId("stat-jasna")).toHaveTextContent("2 台 GPU 机器 · 2 台在工作");
    expect(screen.getByTestId("stat-film")).toHaveTextContent("度 / 部");
    expect(screen.getAllByText("估算").length).toBeGreaterThanOrEqual(4);
  });

  it("lists this machine and every peer with its state, live split and today", async () => {
    mockApi();
    wrap(<PowerPage />);
    const local = await waitFor(() => {
      const el = document.querySelector<HTMLElement>('[data-machine="local"]');
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    expect(within(local).getByRole("heading", { name: "HOME-PC" })).toBeInTheDocument();
    expect(local).toHaveTextContent("本机");
    expect(local).toHaveTextContent("Jasna 修复");
    expect(local).toHaveTextContent("286W · 估算");
    expect(local).toHaveTextContent("GPU 210CPU 46其他 30");
    expect(local).toHaveTextContent(`${fmtKwh(4550)}度`);
    const old = document.querySelector('[data-machine="peer:GPU-04"]') as HTMLElement;
    expect(within(old).getAllByText("无功耗数据（需升级 TaskPaw）").length).toBeGreaterThan(0);
    expect(document.querySelector('[data-machine="peer:GPU-05"]')).toHaveTextContent("离线");
    // the translation machine has no NVIDIA GPU
    expect(document.querySelector('[data-machine="peer:翻译机"]')).toHaveTextContent("GPU —");
    // PW08: a Mac running only the Hub has no row, and the tab says so
    expect(screen.getByText(/只运行 Hub 的 Mac 没有 TaskPaw Agent，暂不计入/)).toBeInTheDocument();
  });

  it("stacks every machine in the chart, toggles series and keeps at least one", async () => {
    mockApi();
    wrap(<PowerPage />);
    const chart = await screen.findByTestId("power-chart");
    const svg = within(chart).getByRole("img");
    expect(svg.getAttribute("aria-label")).toMatch(/^24 小时功耗按机器堆叠面积图/);
    expect(svg.querySelectorAll("path").length).toBeGreaterThanOrEqual(3);
    expect(svg.querySelectorAll("[data-gap]").length).toBe(0);  // a gap of one machine only breaks its own area
    const legend = screen.getByRole("group", { name: "显示或隐藏机器" });
    const buttons = within(legend).getAllByRole("button");
    expect(buttons.map((b) => b.textContent)).toEqual(["HOME-PC", "GPU-02", "翻译机"]);
    fireEvent.click(buttons[1]);
    expect(buttons[1]).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText("显示 2 / 3 台")).toBeInTheDocument();
    fireEvent.click(buttons[2]);
    fireEvent.click(buttons[0]);  // the last one stays
    expect(buttons[0]).toHaveAttribute("aria-pressed", "true");
    expect(await screen.findByText("至少保留一台机器。")).toBeInTheDocument();
    // keyboard: the newest point, then one back
    fireEvent.focus(chart);
    fireEvent.keyDown(chart, { key: "ArrowLeft" });
    const tip = document.querySelector(".chart-tip") as HTMLElement;
    expect(tip).toHaveTextContent("合计（估算）");
    expect(tip).toHaveTextContent("HOME-PC");
  });

  it("switches the range: the chart, the selected energy column and the activity totals follow", async () => {
    const { power } = mockApi();
    wrap(<PowerPage />);
    await screen.findByTestId("power-chart");
    const range = screen.getByRole("group", { name: "时间范围" });
    expect(within(range).getByRole("button", { name: "24 小时" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("columnheader", { name: "今日" })).toHaveClass("sel");
    fireEvent.click(within(range).getByRole("button", { name: "7 天" }));
    await waitFor(() => expect(power).toHaveBeenCalledWith("7d"));
    await waitFor(() => expect(screen.getByRole("columnheader", { name: "本周" })).toHaveClass("sel"));
    expect(screen.getByText(/^最近 7 天 · 共约/)).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "占比（本周）" })).toBeInTheDocument();
    expect(document.querySelectorAll("[data-use]")).toHaveLength(5);
  });

  it("a peer's malformed history never breaks the tab (Codex r2)", async () => {
    const view = (range: "24h" | "7d" | "30d") => {
      const v = powerView(range);
      const bad = { t: [0], avg_w: [100] } as unknown as PowerHistory; // no totals, no activity_wh
      return { ...v, peers: (v.peers ?? []).map((p, i) => (i === 0 ? { ...p, history: bad } : p)) };
    };
    vi.spyOn(api, "power").mockImplementation(async (range) => view(range));
    vi.spyOn(api, "workflowFilms").mockResolvedValue(FILMS);
    vi.spyOn(api, "config").mockResolvedValue({ monitors: [], power: POWER_SETTINGS } as unknown as AgentConfigView);
    wrap(<PowerPage />);
    await screen.findByTestId("power-chart");
    expect(document.querySelectorAll("[data-use]")).toHaveLength(5);
    expect(screen.getAllByText("GPU-02").length).toBeGreaterThan(0);
  });

  it("an offline peer's energy for the running period stays in the totals, marked 数据过期 (Codex r3)", async () => {
    const view = (range: "24h" | "7d" | "30d") => {
      const v = powerView(range);
      const energy = { today_wh: 1920, week_wh: 9630, month_wh: 54900 };
      return {
        ...v,
        peers: (v.peers ?? []).map((p) => (p.name === "GPU-02"
          ? { ...p, state: "unreachable", power: null, energy, energy_stale: true } : p)),
      };
    };
    vi.spyOn(api, "power").mockImplementation(async (range) => view(range));
    vi.spyOn(api, "workflowFilms").mockResolvedValue(FILMS);
    vi.spyOn(api, "config").mockResolvedValue({ monitors: [], power: POWER_SETTINGS } as unknown as AgentConfigView);
    wrap(<PowerPage />);
    const all = await screen.findByTestId("stat-all");
    expect(all).toHaveTextContent("347W · 2 台");  // live W excludes the offline peer
    const energy = screen.getByTestId("stat-energy");
    expect(energy).toHaveTextContent(`${fmtKwhShort(4550 + 1920 + 830)}度 · 今日`);
    expect(energy).toHaveTextContent(`本周约 ${fmtKwhShort(20400 + 9630 + 4180)} 度`);
    expect(energy).toHaveTextContent("含 1 台数据过期机器的最近读数");
    const row = screen.getByRole("row", { name: /GPU-02/ });
    expect(row).toHaveTextContent("数据过期");
    expect(row).toHaveTextContent("1.92");
    const foot = document.querySelector("tfoot") as HTMLElement;
    expect(foot).toHaveTextContent("含过期数据");
    expect(foot).toHaveTextContent("7.30");  // 4.55 + 1.92 + 0.83
  });

  it("an alias of the same machine is listed and not counted (Codex r3)", async () => {
    const view = (range: "24h" | "7d" | "30d") => {
      const v = powerView(range);
      const alias = { ...v.peers![0], name: "GPU-02 (wifi)", state: "duplicate", duplicate_of: "GPU-02", power: null,
        energy: null, history: null };
      return { ...v, peers: [...v.peers!, alias] };
    };
    vi.spyOn(api, "power").mockImplementation(async (range) => view(range));
    vi.spyOn(api, "workflowFilms").mockResolvedValue(FILMS);
    vi.spyOn(api, "config").mockResolvedValue({ monitors: [], power: POWER_SETTINGS } as unknown as AgentConfigView);
    wrap(<PowerPage />);
    const all = await screen.findByTestId("stat-all");
    expect(all).toHaveTextContent("519W · 3 台");
    const dup = document.querySelector('[data-machine="peer:GPU-02 (wifi)"]') as HTMLElement;
    expect(dup).toHaveTextContent("与 GPU-02 是同一台机器，不重复计入");
    expect(screen.getByTestId("stat-energy")).toHaveTextContent(`${fmtKwhShort(4550 + 1920 + 830)}度 · 今日`);
  });

  it("lists each finished film's energy from the media workflow", async () => {
    mockApi();
    const open = vi.fn();
    wrap(<PowerPage onOpenMedia={open} />);
    const table = await screen.findByTestId("films-energy");
    const review = FILMS.films.find((f) => f.film_id === "film-review")!;
    expect(table).toHaveTextContent(`约 ${fmtKwh(review.energy!.total_wh)} 度`);
    expect(screen.getByText("历史不足（每组至少 3 部）")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "在影片队列中查看" }));
    expect(open).toHaveBeenCalled();
  });

  it("says when this machine does not estimate power", async () => {
    vi.spyOn(api, "power").mockRejectedValue(new HttpError("/control/power → 404", 404));
    vi.spyOn(api, "workflowFilms").mockResolvedValue(FILMS);
    vi.spyOn(api, "config").mockResolvedValue({ monitors: [] } as unknown as AgentConfigView);
    wrap(<PowerPage />);
    expect(await screen.findByText(/本机没有运行功耗估算/)).toBeInTheDocument();
  });
});

describe("估算参数", () => {
  const openDialog = async () => {
    const m = mockApi();
    wrap(<PowerPage />);
    await screen.findByTestId("power-chart");
    const button = screen.getByRole("button", { name: "估算参数" });
    await waitFor(() => expect(button).not.toBeDisabled());
    fireEvent.click(button);
    return { ...m, dialog: await screen.findByRole("dialog") };
  };

  it("edits this machine's parameters with measured CPU disabled", async () => {
    const { dialog, update } = await openDialog();
    expect(within(dialog).getByRole("heading", { name: "估算参数" })).toBeInTheDocument();
    expect(within(dialog).getByLabelText("其他功耗基线")).toHaveValue(30);
    expect(within(dialog).getByRole("option", { name: "实测（暂不支持）" })).toBeDisabled();
    const source = within(dialog).getByLabelText("CPU 功耗来源");
    fireEvent.change(source, { target: { value: "none" } });
    expect(within(dialog).getByLabelText("CPU TDP")).toBeDisabled();
    fireEvent.change(source, { target: { value: "estimate" } });
    fireEvent.change(within(dialog).getByLabelText("CPU TDP"), { target: { value: "125" } });
    fireEvent.change(within(dialog).getByLabelText("电源效率"), { target: { value: "0.9" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    expect(update.mock.calls[0][0]).toEqual({
      power: {
        other_w: 30, psu_efficiency: 0.9, cpu_source: "estimate", tdp_w: 125, idle_w: null, peers: POWER_SETTINGS.peers,
      },
    });
    expect(await screen.findByText("估算参数已保存，实时功耗已按新参数估算。")).toBeInTheDocument();
  });

  it("focuses the title once on open and keeps focus in an input across typing and a parent rerender (IR01)", async () => {
    const qc = new QueryClient();
    const tree = (live: LivePower) => (
      <ThemeProvider theme={theme}><QueryClientProvider client={qc}>
        <PowerParams open onClose={() => {}} settings={POWER_SETTINGS} live={live} machineName="HOME-PC" os="Windows" onSaved={() => {}} />
      </QueryClientProvider></ThemeProvider>
    );
    const r = render(tree({ w: 286, gpu_w: 210, cpu_w: 46, other_w: 30 } as LivePower));
    await waitFor(() => expect(screen.getByRole("heading", { name: "估算参数" })).toHaveFocus());
    const plug = screen.getByLabelText("插座读数");
    plug.focus();
    fireEvent.change(plug, { target: { value: "3" } });
    expect(plug).toHaveFocus();
    r.rerender(tree({ w: 290, gpu_w: 212, cpu_w: 48, other_w: 30 } as LivePower));  // the 5 s poll
    expect(plug).toHaveFocus();
  });

  it("calibrates the baseline from the plug reading in the browser", async () => {
    const { dialog } = await openDialog();
    const fill = within(dialog).getByRole("button", { name: "填入基线" });
    expect(fill).toBeDisabled();
    fireEvent.change(within(dialog).getByLabelText("插座读数"), { target: { value: "320" } });
    // 320 × 100% − GPU 210 − CPU 46 = 64
    expect(within(dialog).getByText("建议其他基线：320 × 100% − GPU 210 − CPU 46 = 64 W")).toBeInTheDocument();
    fireEvent.click(fill);
    expect(within(dialog).getByLabelText("其他功耗基线")).toHaveValue(64);
    fireEvent.change(within(dialog).getByLabelText("插座读数"), { target: { value: "100" } });
    expect(within(dialog).getByText(/插座读数低于当前 GPU \+ CPU 估算/)).toBeInTheDocument();
  });

  it("adds and removes peers by name and address", async () => {
    const { dialog, update } = await openDialog();
    const list = within(dialog).getByRole("list", { name: "其他机器" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(4);
    fireEvent.click(within(dialog).getByRole("button", { name: "移除 GPU-05" }));
    fireEvent.change(within(dialog).getByLabelText("名称"), { target: { value: "GPU-03" } });
    fireEvent.change(within(dialog).getByLabelText("地址"), { target: { value: "http://x" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "添加" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("地址格式");
    fireEvent.change(within(dialog).getByLabelText("地址"), { target: { value: "192.168.1.31:5680" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "添加" }));
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    const sent = (update.mock.calls[0][0] as { power: { peers: unknown[] } }).power.peers;
    expect(sent).toEqual([...POWER_SETTINGS.peers.slice(0, 3), { name: "GPU-03", address: "192.168.1.31:5680" }]);
  });

  it("saves a typed peer that was not added yet, and blocks the save on an incomplete one (IR07)", async () => {
    const { dialog, update } = await openDialog();
    fireEvent.change(within(dialog).getByLabelText("名称"), { target: { value: "GPU-09" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));
    expect(within(dialog).getByRole("alert")).toHaveTextContent("请填写名称和地址。");
    expect(update).not.toHaveBeenCalled();
    fireEvent.change(within(dialog).getByLabelText("地址"), { target: { value: "192.168.1.39" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "保存" }));
    await waitFor(() => expect(update).toHaveBeenCalled());
    const sent = (update.mock.calls[0][0] as { power: { peers: unknown[] } }).power.peers;
    expect(sent).toEqual([...POWER_SETTINGS.peers, { name: "GPU-09", address: "192.168.1.39" }]);
  });
});

describe("power view model", () => {
  it("aligns every machine on the local time axis and keeps gaps as null", () => {
    const machines = machinesOf(powerView("24h"), powerView("24h"));
    const { series, times } = chartSeries(machines);
    expect(times).toHaveLength(96);
    expect(series.map((s) => s.name)).toEqual(["HOME-PC", "GPU-02", "翻译机"]);
    expect(series[0].values[40]).toBeNull();
    expect(stateChip({ state: "ok", stale: true })[0]).toBe("数据过期");
    expect(stateChip({ state: "no_power", stale: false })[0]).toBe("无功耗数据（需升级 TaskPaw）");
    expect(stateChip({ state: "self", stale: false })[0]).toBe("就是本机，不重复计入");
    expect(stateChip({ state: "duplicate", stale: false, duplicateOf: "GPU-02" })[0]).toBe("与 GPU-02 是同一台机器，不重复计入");
  });

  it("aggregates by timestamp only: an unmatched peer point is a gap, never shifted by index", () => {
    const hist = (t: number[], avg_w: Array<number | null>) => ({ t, avg_w }) as unknown as PowerHistory;
    const m = (id: string, h: PowerHistory) => ({
      id, name: id, hw: "", local: id === "local", state: "ok", stale: false, color: "#000", live: null, day: null, hist: h,
    });
    const { series, times } = chartSeries([m("local", hist([900, 1800], [5, 6])), m("peer", hist([0, 900], [10, 20]))]);
    expect(times).toEqual([900, 1800]);
    expect(series[1].values).toEqual([20, null]);
  });

  it("predicts from the rates only with enough history", () => {
    const rates = [{ machine: "m", resolution: "4K" as const, tier: "standard" as const, films: 3, wh_per_hour: 640 }];
    expect(predictFromRates(rates, "m", "4K", "standard", 7_200_000)).toBe(1280);
    expect(predictFromRates(rates, "m", "4K", "high", 7_200_000)).toBeNull();
    expect(predictFromRates(rates, "m", "1080p", "standard", 7_200_000)).toBeNull();
    expect(fmtKwh(4)).toBe("<0.01");
  });
});

describe("media power parts", () => {
  const prediction = {
    machine: "m", resolution: "4K" as const, min_films: 3, films: { standard: 5, good: 4, high: 3, none: 0 },
    standard: 1400, good: 2300, high: 5300, none: null, estimated: true as const,
  };

  it("预计耗电 highlights the chosen tier and names the source", () => {
    render(<div className="mw"><PredictLine prediction={prediction} tier="high" needsGpu id="p" /></div>);
    const line = document.getElementById("p") as HTMLElement;
    expect(line).toHaveTextContent("预计耗电：标准 ~1.4 度 · 良好 ~2.3 · 高 ~5.3（已选）（按本机历史）");
    expect(line.querySelector(".t.on")).toHaveAttribute("data-t", "high");
  });

  it("a film without restore predicts only 识别 and 翻译, or 历史不足", () => {
    render(<div className="mw"><PredictLine prediction={{ ...prediction, none: 50 }} tier={null} needsGpu={false} id="a" />
      <PredictLine prediction={prediction} tier={null} needsGpu={false} id="b" /></div>);
    expect(document.getElementById("a")).toHaveTextContent("预计耗电：约 0.05 度（仅识别与翻译 · 估算）");
    expect(document.getElementById("b")).toHaveTextContent("预计耗电：历史不足（仅识别与翻译 · 估算）");
  });

  it("an interrupted attempt makes the film 「部分」", () => {
    const stage = { wh: 900, gpu_wh: 700, cpu_wh: 120, other_wh: 80, seconds: 12000, avg_w: 270, peak_w: 305,
      machine: "local", attempts: 2, partial: true, running: false };
    render(<div className="mw"><EnergyBlock filmId="f" machine="本机" predicted={null} runSeconds={14400}
      energy={{ stages: { restore: stage }, total_wh: 900, partial: true, running: false, estimated: true }} /></div>);
    const block = screen.getByTestId("film-energy");
    expect(block).toHaveTextContent("本片约0.90度全程 4:00 · 修复占 100% · 部分（有尝试被中断，未计入）");
    expect(block.querySelector('[data-energy="restore"]')).toHaveTextContent("部分");
    expect(block.querySelector('[data-energy="restore"]')).toHaveTextContent("2 次尝试");
  });
});
