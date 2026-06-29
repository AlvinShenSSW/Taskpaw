import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
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
        "lada-main": { state: "running" },
        "render-01-host": { state: "ok", metrics: { cpu_pct: 37, mem_pct: 72 } },
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
      snapshot: { machine: "render-04", monitors: { "lada-main": { state: "ok" } } },
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

  it("shows CPU/MEM mini-bars for a live machine that reports host metrics (#113)", async () => {
    renderHub();
    const card = (await screen.findByText("render-01")).closest("button")!;
    // host_metrics → cpu_pct 37 / mem_pct 72 render as labelled % on the card face.
    expect(within(card).getByText("37%")).toBeInTheDocument();
    expect(within(card).getByText("72%")).toBeInTheDocument();
  });

  it("omits mini-bars for an offline machine with no metrics (#113)", async () => {
    renderHub();
    const card = (await screen.findByText("render-03")).closest("button")!;
    expect(within(card).queryByText(/%$/)).not.toBeInTheDocument();
  });

  it("labels a disabled server distinctly from a merely-offline one", async () => {
    renderHub();
    // render-03 is enabled:0 → its chip reads "disabled", not just "offline".
    const card = (await screen.findByText("render-03")).closest("button")!;
    expect(within(card).getByText(/disabled|已禁用/)).toBeInTheDocument();
  });

  it("renders the self-monitor as metric tiles, not raw JSON", async () => {
    renderHub();
    await screen.findByText("hub-host");
    // The host metrics render as labelled tiles/gauges (CPU appears for the self
    // monitor and the #113 card mini-bars), never a raw JSON.stringify blob.
    expect(screen.getAllByText(/CPU/i).length).toBeGreaterThan(0);
    expect(screen.queryByText(/"cpu_pct"/)).not.toBeInTheDocument();
  });

  it("drills down into a machine's monitors when its card is clicked", async () => {
    renderHub();
    const card = (await screen.findByText("render-02")).closest("button")!;
    expect(card.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(card);
    expect(card.getAttribute("aria-expanded")).toBe("true");
    // The expanded detail lists that machine's monitor from its snapshot.
    expect(await screen.findByText("gpu")).toBeInTheDocument();
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
