import { describe, expect, it, vi, beforeEach } from "vitest";
import { act, fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HubDashboard } from "../views/HubDashboard";
import { theme } from "../theme";
import i18n from "../i18n";

// Four machines: two healthy, one online-but-degraded (a monitor in alert), one
// offline → counts 2 / 1 / 1 (distinct, so the tally assertions are meaningful).
// `self` carries host metrics so the tile path is exercised.
const STATUS = {
  machine: "hub-box",
  servers: [
    {
      id: 1, name: "render-01", ip: "10.0.0.1", port: 8765, enabled: 1,
      online: true, last_seen: "2026-06-29T10:00:00Z",
      snapshot: { machine: "render-01", monitors: {
        // lada emits cpu_pct/mem_pct too — must NOT be mistaken for the host (Kimi #113).
        "lada-main": { state: "running", type_id: "lada", metrics: { cpu_pct: 99, mem_pct: 99 } },
        "render-01-host": { state: "ok", type_id: "host_metrics", metrics: { cpu_pct: 37, mem_pct: 72 } },
      } },
    },
    {
      id: 2, name: "render-02", ip: "10.0.0.2", port: 8765, enabled: 1,
      online: true, last_seen: "2026-06-29T10:00:00Z",
      // "error" (not "alert") — health must treat all failure states as degraded.
      snapshot: { machine: "render-02", monitors: { gpu: { state: "error" } } },
    },
    {
      // Disabled server: backend forces online=false; counts as offline health.
      id: 3, name: "render-03", ip: "10.0.0.3", port: 8765, enabled: 0,
      online: false, last_seen: null, snapshot: null,
    },
    {
      id: 4, name: "render-04", ip: "10.0.0.4", port: 8765, enabled: 1,
      online: true, last_seen: "2026-06-29T10:00:00Z",
      // Legacy agent: monitors carry NO type_id → hostMetrics falls back to a
      // cpu_pct/mem_pct key-scan (Kimi #113).
      snapshot: { machine: "render-04", monitors: { host: { state: "ok", metrics: { cpu_pct: 55 } } } },
    },
  ],
  acks: {},
  self: { "hub-host": { state: "ok", metrics: { cpu_pct: 42, mem_pct: 61 } } },
};

function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      const body = url.includes("/status") ? STATUS : { events: [] };
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    }),
  );
}

const renderHub = () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <ThemeProvider theme={theme}>
      <QueryClientProvider client={qc}>
        <HubDashboard />
      </QueryClientProvider>
    </ThemeProvider>,
  );
};

describe("HubDashboard (#95)", () => {
  beforeEach(stubFetch);

  it("tallies fleet health from online + snapshot (ok / degraded / offline)", async () => {
    renderHub();
    const summary = await screen.findByLabelText(/Fleet health|机群健康/);
    // 2 healthy, 1 degraded, 1 offline. Each count is scoped to its labelled row.
    const row = (re: RegExp) => within(summary).getByText(re).closest("p") as HTMLElement;
    expect(within(row(/healthy|正常/)).getByText("2")).toBeInTheDocument();
    expect(within(row(/degraded|降级/)).getByText("1")).toBeInTheDocument();
    expect(within(row(/offline|离线/)).getByText("1")).toBeInTheDocument();
    // Status conveyed by a labelled dot, not color alone (a11y §1): one per count.
    expect(within(summary).getAllByLabelText(/status:/).length).toBe(3);
  });

  // Each machine is now a full-width row (a Card), not a click-to-expand button (#131).
  const rowOf = async (name: string) =>
    (await screen.findByText(name)).closest(".MuiCard-root") as HTMLElement;

  it("shows CPU/MEM mini-bars for a live machine that reports host metrics (#113)", async () => {
    renderHub();
    const card = await rowOf("render-01");
    // The host_metrics monitor (37/72) drives the bars — NOT the lada monitor that
    // also reports cpu_pct/mem_pct (99) (Kimi #113 attribution fix).
    expect(within(card).getByText("37%")).toBeInTheDocument();
    expect(within(card).getByText("72%")).toBeInTheDocument();
    expect(within(card).queryByText("99%")).not.toBeInTheDocument();
  });

  it("falls back to a key-scan for a legacy agent with no type_id (#113)", async () => {
    renderHub();
    // render-04's monitor has no type_id but reports cpu_pct → bar still renders.
    const card = await rowOf("render-04");
    expect(within(card).getByText("55%")).toBeInTheDocument();
  });

  it("omits mini-bars for an offline machine with no metrics (#113)", async () => {
    renderHub();
    const card = await rowOf("render-03");
    expect(within(card).queryByText(/%$/)).not.toBeInTheDocument();
  });

  it("labels a disabled server distinctly from a merely-offline one", async () => {
    renderHub();
    // render-03 is enabled:0 → its chip reads "disabled", not just "offline".
    const card = await rowOf("render-03");
    expect(within(card).getByText(/disabled|已禁用/)).toBeInTheDocument();
  });

  it("renders the self-monitor as metric tiles, not raw JSON", async () => {
    renderHub();
    await screen.findByText("hub-host");
    // Scope to the self-monitor card (its overline label) so card mini-bars don't
    // satisfy this — verifies the self monitor specifically renders gauges, not raw JSON.
    const selfCard = screen.getByText(/self-monitor|自监控/).closest(".MuiCard-root") as HTMLElement;
    expect(within(selfCard).getByText(/CPU/i)).toBeInTheDocument();
    expect(within(selfCard).queryByText(/"cpu_pct"/)).not.toBeInTheDocument();
  });

  it("keeps agent management on its own Manage tab, not the Fleet page (#132)", async () => {
    renderHub();
    await screen.findByLabelText(/Fleet health|机群健康/); // fleet loaded
    // The Fleet (dashboard) page is observation-only — no agent manager here.
    expect(screen.queryByText(/Manage agents|管理 agent/)).not.toBeInTheDocument();
    // Switching to the Manage tab reveals the CRUD manager.
    fireEvent.click(screen.getByRole("tab", { name: /^Manage$|^管理$/ }));
    expect(await screen.findByText(/Manage agents|管理 agent/)).toBeInTheDocument();
  });

  it("filters the events feed by server on the Events tab (#133)", async () => {
    const fetchMock = vi.fn((url: string) => {
      const body = url.includes("/status") ? STATUS : { events: [] };
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    });
    vi.stubGlobal("fetch", fetchMock);
    renderHub();
    fireEvent.click(screen.getByRole("tab", { name: /^Events$|^事件$/ }));
    // pick a specific server in the new filter → the query gains ?server=1
    fireEvent.mouseDown(await screen.findByRole("combobox", { name: /Server|服务器/i }));
    fireEvent.click(await screen.findByRole("option", { name: "render-01" }));
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(
        ([u]) => String(u).includes("/events") && String(u).includes("server=1"),
      )).toBe(true),
    );
  });

  it("shows each machine's monitors inline, flush, with no click-to-expand (#131)", async () => {
    renderHub();
    // render-02's monitor is listed directly on its row — no expand needed.
    const card = await rowOf("render-02");
    expect(within(card).getByText("gpu")).toBeInTheDocument();
    // No drill-down affordance: the row is not an expandable button.
    expect(within(card).queryByRole("button", { name: /render-02/ })).not.toBeInTheDocument();
  });
});

describe("hubStatus auto-refresh (#95)", () => {
  it("re-polls /status on a 5s interval", async () => {
    vi.useFakeTimers();
    try {
      const fetchMock = vi.fn((url: string) => {
        const body = url.includes("/status") ? STATUS : { events: [] };
        return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
      });
      vi.stubGlobal("fetch", fetchMock);
      renderHub();
      // Let the initial query settle.
      await vi.advanceTimersByTimeAsync(0);
      const initial = fetchMock.mock.calls.filter(([u]) => String(u).includes("/status")).length;
      expect(initial).toBeGreaterThanOrEqual(1);
      // After ~5s the refetchInterval fires at least once more.
      await vi.advanceTimersByTimeAsync(5100);
      const after = fetchMock.mock.calls.filter(([u]) => String(u).includes("/status")).length;
      expect(after).toBeGreaterThan(initial);
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("#210 actual Hub task cards", () => {
  const snapshotFilms = [{ name: "snapshot-only.mp4", steps: { asr: "future" }, status: "future" }];
  const pipeline = { film: "live.mp4", model: "model-fixture", cpu_pct: 42,
    steps: [{ key: "restore", state: "done" }, { key: "asr", state: "done" },
      { key: "translate", state: "active", percent: 43, model: "model-fixture" }], films: snapshotFilms };
  const fleet = (type: unknown, metrics: Record<string, unknown>, version: unknown = __APP_VERSION__, online = true, enabled = 1) => ({
    machine: "hub", self: {}, acks: {}, servers: [{ id: 1, name: "machine-fixture", ip: "10.0.0.1", port: 5680, online, enabled,
      snapshot: { version, machine: "agent", monitors: { "task-fixture": { state: "running", type_id: type, metrics } } } }],
  });
  function transport(status: unknown, code = 404, error = "film_list_unavailable") {
    const mock = vi.fn((input: string) => Promise.resolve({ ok: input.endsWith("/status"), status: input.endsWith("/status") ? 200 : code,
      json: async () => input.endsWith("/status") ? status : { error, detail: "never display raw upstream" } }));
    vi.stubGlobal("fetch", mock); return mock;
  }
  it.each(["jasna", "avsubs"])("renders %s stepper, model, icon and safe tiles", async type => {
    transport(fleet(type, { ...pipeline, bad: null, nested: {}, steps: type === "jasna" ? pipeline.steps : pipeline.steps.slice(1) }));
    renderHub(); await screen.findByText("task-fixture");
    expect(screen.getByTestId("pipeline-stepper")).toBeInTheDocument();
    expect(screen.getAllByText(/model-fixture/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/43%/).length).toBeGreaterThan(0);
    expect(screen.getByText(type === "jasna" ? "Jasna" : /AV translate|AV 翻译/).closest(".MuiChip-root")).not.toBeNull();
    expect(screen.getByText("task-fixture").parentElement?.querySelector("svg")).not.toBeNull();
    expect(screen.queryByText("bad")).toBeNull(); expect(screen.queryByText("nested")).toBeNull();
    expect(await screen.findByText("snapshot-only.mp4")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Start|Stop|启动|停止/ })).toBeNull();
  });
  it.each([
    ["avsubs", {}, "films"], ["avsubs", { queue_pre_done: 1 }, "films"],
    ["jasna", { ...pipeline, steps: [{ key: "restore", state: "future" }, { key: "asr", state: "active" }] }, "run-films"],
    ["jasna", { films: snapshotFilms, steps: "bad" }, "run-films"],
    ["jasna", { queue_total: 1 }, null], ["other", pipeline, null], [undefined, pipeline, null],
  ])("selects typed endpoint without inferred identity %#", async (type, metrics, resource) => {
    const mock = transport(fleet(type, metrics)); renderHub(); await screen.findByText("task-fixture");
    if (resource) await waitFor(() => expect(mock.mock.calls.some(([u]) => u.includes(`/servers/1/monitors/${resource}?`))).toBe(true));
    else expect(mock.mock.calls).toHaveLength(1);
  });
  it.each([[false, 1], [true, 0], [false, 0]])("keeps offline/disabled header-only (%s/%s)", async (online, enabled) => {
    const mock = transport(fleet("avsubs", pipeline, __APP_VERSION__, online, enabled)); renderHub();
    await screen.findByText("machine-fixture"); expect(screen.queryByText("task-fixture")).toBeNull();
    expect(screen.queryByText("snapshot-only.mp4")).toBeNull(); expect(screen.queryByTestId("pipeline-stepper")).toBeNull();
    expect(mock.mock.calls).toHaveLength(1);
  });
  it.each([[404, "film_list_unavailable"], [409, "agent_disabled"], [503, "agent_offline"], [502, "agent_auth_failed"], [504, "agent_timeout"]])("shows compact singleton and localized reason for online error %s", async (status, code) => {
    transport(fleet("avsubs", { films: snapshotFilms }), status as number, code as string); renderHub();
    await screen.findByText("snapshot-only.mp4");
    await waitFor(() => expect(screen.queryByText(/Loading film list|正在读取影片/)).toBeNull());
    expect(screen.getAllByRole("status").length).toBeGreaterThan(0);
    expect(screen.queryByText("never display raw upstream")).toBeNull();
  });
  it.each(["999.0.0", __APP_VERSION__, "0.0.1", "malformed", "", null, 123])("warns only for a valid newer version (%s)", async version => {
    transport(fleet(undefined, {}, version)); renderHub(); await screen.findByText("machine-fixture");
    if (typeof version === "string" && version) expect(screen.getByLabelText(`Agent v${version}`)).toHaveTextContent(`v${version}`);
    expect(screen.queryAllByRole("alert")).toHaveLength(version === "999.0.0" ? 1 : 0);
    if (version === "999.0.0") expect(screen.getByRole("alert")).toHaveTextContent(/Update the Hub|请更新 Hub/);
  });
  it.each(["offline", "disabled", "tab", "removed"])("stops film polling and drops late feedback on %s; reconnect starts fresh", async transition => {
    vi.useFakeTimers();
    let status = fleet("avsubs", { films: snapshotFilms });
    let resolve!: (value: { ok: boolean; status: number; json: () => Promise<unknown> }) => void;
    let delayed = false;
    const calls: string[] = [];
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      calls.push(url);
      if (url.endsWith("/status")) return Promise.resolve({ ok: true, json: async () => status });
      if (delayed) return new Promise(r => { resolve = r; });
      return Promise.resolve({ ok: false, status: 404, json: async () => ({ error: "film_list_unavailable" }) });
    }));
    const view = renderHub();
    try {
      await act(async () => { await vi.advanceTimersByTimeAsync(50); });
      expect(screen.getByText("snapshot-only.mp4")).toBeInTheDocument();
      delayed = true;
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(resolve).toBeTypeOf("function");
      if (transition === "offline") status = fleet("avsubs", { films: snapshotFilms }, __APP_VERSION__, false);
      if (transition === "disabled") status = fleet("avsubs", { films: snapshotFilms }, __APP_VERSION__, true, 0);
      if (transition === "removed") status = { ...status, servers: [] };
      if (transition === "tab") fireEvent.click(screen.getByRole("tab", { name: /^Manage$|^管理$/ }));
      await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
      expect(screen.queryByText("task-fixture")).toBeNull();
      expect(screen.queryByText("snapshot-only.mp4")).toBeNull();
      const count = calls.filter(u => u.includes("/monitors/films")).length;
      await act(async () => {
        resolve({ ok: false, status: 504, json: async () => ({ error: "agent_timeout" }) });
        await vi.advanceTimersByTimeAsync(10000);
      });
      expect(calls.filter(u => u.includes("/monitors/films"))).toHaveLength(count);
      expect(screen.queryByText(/Film list request timed out|读取影片列表超时/)).toBeNull();
      delayed = false; status = fleet("avsubs", { films: snapshotFilms });
      if (transition === "tab") fireEvent.click(screen.getByRole("tab", { name: /^Fleet$|^机群$/ }));
      await act(async () => { await vi.advanceTimersByTimeAsync(5100); });
      expect(screen.getByText("snapshot-only.mp4")).toBeInTheDocument();
      const last = calls.filter(u => u.includes("/monitors/films")).at(-1)!;
      expect(new URL(last).searchParams.has("page")).toBe(false);
      expect(calls.filter(u => u.includes("/monitors/films")).length).toBeGreaterThan(count);
    } finally { view.unmount(); vi.useRealTimers(); }
  });
  it.each(["en", "zh-CN"])("localizes actual type labels and handles unknown/invalid ids (%s)", async lang => {
    await i18n.changeLanguage(lang);
    const status = fleet("avsubs", {});
    status.servers[0].snapshot.monitors = { ...status.servers[0].snapshot.monitors, ...Object.fromEntries([
      ["task-fixture", "avsubs"], ["unknown-task", "future-plugin"], ["invalid-task", 42],
    ].map(([name, type]) => [name, { state: "running", type_id: type, metrics: {} }])) };
    transport(status); const view = renderHub();
    try {
      expect(await screen.findByText(lang === "en" ? "AV translate" : "AV 翻译")).toBeInTheDocument();
      expect(screen.getByText("future-plugin")).toBeInTheDocument();
      expect(screen.getByText("invalid-task").parentElement?.querySelector("svg")).toBeNull();
    } finally { view.unmount(); await i18n.changeLanguage("zh-CN"); }
  });

});
