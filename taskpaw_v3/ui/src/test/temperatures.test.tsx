// #323: sensor readings are independent of power/history and age while requests stall.
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider } from "@mui/material/styles";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type AgentConfigView, type PowerView } from "../api";
import { PowerPage } from "../components/power/PowerPage";
import { theme } from "../theme";
import { POWER_SETTINGS, powerView } from "./fixtures/power";

function temperatures(overrides = {}) {
  return {
    sampled_at: 1792920000, age_s: 2, stale: false,
    cpu: { celsius: null, label: "CPU", source: null, reason: "no_reader" },
    gpu: { celsius: 42, label: "NVIDIA GPU core (highest readable)", source: "nvidia-smi", reason: null, readable: 1, total: 1 },
    board: { celsius: null, label: "Board / chassis", source: null, reason: "no_reader" },
    ...overrides,
  };
}

function thermalView(overrides = {}): PowerView {
  const view = powerView();
  return { ...view, temperatures: temperatures(), ...overrides } as PowerView;
}

const clients: QueryClient[] = [];
function mount(client?: QueryClient) {
  const qc = client ?? new QueryClient({ defaultOptions: { queries: { retry: false, gcTime: Infinity } } });
  if (!clients.includes(qc)) clients.push(qc);
  return { client: qc, ...render(<ThemeProvider theme={theme}><QueryClientProvider client={qc}><PowerPage /></QueryClientProvider></ThemeProvider>) };
}
const tick = async (ms = 20) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };
const row = (id = "local") => document.querySelector(`[data-machine="${id}"]`) as HTMLElement;
const sensors = (id = "local") => row(id).querySelector(".m-temperatures") as HTMLElement;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date", "performance"] });
  vi.spyOn(api, "workflowFilms").mockResolvedValue({ state: "disabled" } as never);
  vi.spyOn(api, "config").mockResolvedValue({ monitors: [], power: POWER_SETTINGS } as unknown as AgentConfigView);
});
afterEach(() => {
  cleanup(); clients.splice(0).forEach((c) => c.clear()); vi.useRealTimers(); vi.restoreAllMocks();
});

describe("current machine temperatures", () => {
  it("shows only measured GPU temperature beside unchanged watts and energy", async () => {
    vi.spyOn(api, "power").mockResolvedValue(thermalView());
    mount(); await tick();
    expect(sensors()).toHaveTextContent("NVIDIA GPU 核心最高");
    expect(sensors()).toHaveTextContent("42 °C");
    expect(sensors()).toHaveTextContent("1 / 1 NVIDIA GPU 可读");
    expect(sensors()).toHaveTextContent("nvidia-smi");
    expect(within(sensors()).queryByText("CPU")).not.toBeInTheDocument();
    expect(within(sensors()).queryByText("主板 / 机箱")).not.toBeInTheDocument();
    expect(sensors()).not.toHaveTextContent("不可用");
    expect(sensors()).not.toHaveTextContent("估算");
    expect(sensors()).not.toHaveTextContent("0 °C");
    expect(screen.getByTestId("stat-all")).toHaveTextContent("519W · 3 台");
    expect(screen.getByTestId("stat-energy")).toHaveTextContent("7.3度 · 今日");
    expect(screen.getByText(/功耗和能耗为估算/)).toHaveTextContent("温度来自实际可读传感器");
    expect(screen.queryByText(/全部为估算/)).not.toBeInTheDocument();
  });

  it("shows local power-off and peer partial GPU coverage independently of history state", async () => {
    const view = thermalView({ power: null });
    const gpu = { ...temperatures().gpu, celsius: 67, readable: 1, total: 2 };
    view.peers = view.peers!.map((p, i) => i === 0 ? {
      ...p, state: "not_found", history: null, stale: true, power: null,
      temperatures: temperatures({ gpu }),
    } : p);
    vi.spyOn(api, "power").mockResolvedValue(view);
    mount(); await tick();
    expect(row()).toHaveTextContent("未开启功耗估算");
    expect(sensors()).toHaveTextContent("42 °C");
    expect(sensors("peer:GPU-02")).toHaveTextContent("67 °C");
    expect(sensors("peer:GPU-02")).toHaveTextContent("1 / 2 NVIDIA GPU 可读");
    expect(sensors("peer:GPU-02")).toHaveTextContent("可读部分");
    expect(sensors("peer:GPU-02")).not.toHaveTextContent("已过期");
    expect(sensors("peer:GPU-02")).not.toHaveTextContent("CPU");
    expect(sensors("peer:GPU-02")).not.toHaveTextContent("主板 / 机箱");
    expect(sensors("peer:GPU-02")).not.toHaveTextContent("不可用");
    expect(sensors("peer:GPU-04")).toHaveTextContent("温度未提供 · 需升级 TaskPaw");
  });

  it("does not borrow a range query's temperatures, even if that history request fails", async () => {
    vi.spyOn(api, "power").mockImplementation(async (range) => {
      if (range === "7d") return thermalView({ temperatures: temperatures({ gpu: { ...temperatures().gpu, celsius: 99 } }) });
      if (range === "30d") throw new Error("history failed");
      return thermalView();
    });
    mount(); await tick();
    fireEvent.click(screen.getByRole("button", { name: "7 天" })); await tick();
    expect(sensors()).toHaveTextContent("42 °C"); expect(sensors()).not.toHaveTextContent("99 °C");
    fireEvent.click(screen.getByRole("button", { name: "30 天" })); await tick();
    expect(sensors()).toHaveTextContent("42 °C"); expect(sensors()).not.toHaveTextContent("已过期");
  });

  it("marks a disconnected peer's retained sample stale independently of a fresh local sample", async () => {
    const view = thermalView();
    view.peers = view.peers!.map((p, i) => i === 0 ? {
      ...p, state: "unreachable", power: null, temperatures: temperatures({ stale: true }),
    } : p);
    vi.spyOn(api, "power").mockResolvedValue(view);
    mount(); await tick();
    expect(sensors()).not.toHaveTextContent("已过期");
    expect(sensors("peer:GPU-02")).toHaveTextContent("42 °C · 已过期");
  });

  it("clears earlier temperatures when a successful response no longer provides them", async () => {
    const power = vi.spyOn(api, "power").mockResolvedValue(thermalView());
    mount(); await tick(); expect(sensors()).toHaveTextContent("42 °C");
    power.mockResolvedValue(powerView()); await tick(5000);
    expect(sensors()).toHaveTextContent("温度未提供 · 需升级 TaskPaw");
    expect(sensors()).not.toHaveTextContent("42");
  });

  it("ages through a pending poll at the exact 30 second boundary", async () => {
    vi.spyOn(api, "power").mockResolvedValueOnce(thermalView({ temperatures: temperatures({ age_s: 25 }) }))
      .mockImplementation(() => new Promise(() => {}));
    mount(); await tick();
    expect(sensors()).not.toHaveTextContent("已过期");
    await tick(5000);
    expect(sensors()).toHaveTextContent("42 °C · 已过期");
    expect(sensors()).toHaveTextContent("最后读数");
  });

  it("includes browser request latency before deciding whether a response is current", async () => {
    let resolve!: (v: PowerView) => void;
    vi.spyOn(api, "power").mockImplementation(() => new Promise((r) => { resolve = r; }));
    mount(); await tick(4000);
    await act(async () => { resolve(thermalView({ temperatures: temperatures({ age_s: 27 }) })); }); await tick();
    expect(sensors()).toHaveTextContent("已过期");
  });

  it("keeps failed day readings visible but stale immediately without live watts", async () => {
    vi.spyOn(api, "power").mockResolvedValueOnce(thermalView()).mockRejectedValue(new Error("disconnected"));
    mount(); await tick(); await tick(5000);
    expect(sensors()).toHaveTextContent("42 °C · 已过期");
    expect(screen.getByText(/无法读取功耗与温度/)).toBeInTheDocument();
    expect(row().querySelector(".m-pow")).not.toHaveTextContent("286");
    expect(screen.getByTestId("stat-all")).toHaveTextContent("0W · 0 台");
  });

  it("keeps the same sample aging through identical replies, range changes and remounts", async () => {
    const power = vi.spyOn(api, "power").mockResolvedValue(thermalView({ temperatures: temperatures({ age_s: 20 }) }));
    const first = mount(); await tick(); await tick(6000);
    fireEvent.click(screen.getByRole("button", { name: "7 天" })); await tick();
    first.unmount(); await tick(5000);
    power.mockImplementation(() => new Promise(() => {}));
    mount(first.client); await tick();
    expect(sensors()).toHaveTextContent("42 °C · 已过期");
  });

  it("allows a genuinely new sample to restore freshness", async () => {
    const power = vi.spyOn(api, "power").mockResolvedValue(thermalView({ temperatures: temperatures({ age_s: 35 }) }));
    mount(); await tick(); expect(sensors()).toHaveTextContent("已过期");
    power.mockResolvedValue(thermalView({ temperatures: temperatures({ sampled_at: 1792920005, age_s: 1 }) }));
    await tick(5000); expect(sensors()).not.toHaveTextContent("已过期");
  });

  it.each([null, -1, NaN, Infinity])("does not show a live reading for invalid age %s", async (age) => {
    vi.spyOn(api, "power").mockResolvedValue(thermalView({ temperatures: temperatures({ age_s: age }) }));
    mount(); await tick(); expect(sensors()).toHaveTextContent("已过期");
  });

  it("does not present a negative sampling timestamp as current", async () => {
    vi.spyOn(api, "power").mockResolvedValue(thermalView({ temperatures: temperatures({ sampled_at: -1 }) }));
    mount(); await tick(); expect(sensors()).toHaveTextContent("已过期");
  });

  it.each([null, "", "   ", 1])("does not present a sensor reading without its actual source (%s)", async (source) => {
    vi.spyOn(api, "power").mockResolvedValue(thermalView({ temperatures: temperatures({
      gpu: { ...temperatures().gpu, source },
    }) }));
    mount(); await tick(); expect(sensors()).not.toHaveTextContent("42 °C");
    expect(sensors()).toHaveTextContent("不可用");
  });

  it.each([NaN, Infinity, -Infinity, 201, "42"])("rejects invalid sensor values %s", async (value) => {
    vi.spyOn(api, "power").mockResolvedValue(thermalView({ temperatures: temperatures({ gpu: { ...temperatures().gpu, celsius: value } }) }));
    mount(); await tick(); expect(sensors()).not.toHaveTextContent("°C"); expect(sensors()).toHaveTextContent("不可用");
  });

  it("does not call a GPU reading current without valid sensor coverage", async () => {
    vi.spyOn(api, "power").mockResolvedValue(thermalView({ temperatures: temperatures({
      gpu: { ...temperatures().gpu, readable: 2, total: 1 },
    }) }));
    mount(); await tick(); expect(sensors()).not.toHaveTextContent("42 °C");
    expect(sensors()).not.toHaveTextContent("2 / 1");
  });

  it.each(["pending", "unsupported_platform", "provider_missing", "permission_denied", "timeout", "query_failed", "invalid_reading", "no_sensor"])(
    "shows provider failure %s without a guessed zero or old value", async (reason) => {
      vi.spyOn(api, "power").mockResolvedValue(thermalView({ temperatures: temperatures({
        sampled_at: null, age_s: null, stale: true, gpu: { ...temperatures().gpu, celsius: null, readable: 0, total: 0, reason },
      }) }));
      mount(); await tick(); expect(sensors()).not.toHaveTextContent("°C"); expect(sensors()).toHaveTextContent("不可用");
    });

  it.each(["local", "peer:GPU-02"])("hides CPU and board readings from %s even when supplied", async (machine) => {
    const measured = temperatures({
      cpu: { celsius: 65, label: "CPU package sensor", source: "verified-reader", reason: null },
      board: { celsius: 35, label: "System board sensor", source: "verified-reader", reason: null },
    });
    const view = thermalView({ temperatures: measured });
    view.peers = view.peers!.map((p, i) => i === 0 ? { ...p, temperatures: measured } : p);
    vi.spyOn(api, "power").mockResolvedValue(view);
    mount(); await tick();
    expect(sensors(machine)).toHaveTextContent("42 °C");
    expect(sensors(machine)).toHaveTextContent("nvidia-smi");
    expect(sensors(machine)).not.toHaveTextContent("CPU package sensor");
    expect(sensors(machine)).not.toHaveTextContent("System board sensor");
    expect(sensors(machine)).not.toHaveTextContent("65 °C");
    expect(sensors(machine)).not.toHaveTextContent("35 °C");
  });
});
