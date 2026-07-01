import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HubDashboard } from "../views/HubDashboard";
import { theme } from "../theme";
import "../i18n";

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
