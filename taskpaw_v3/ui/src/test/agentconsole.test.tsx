import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AgentConsole } from "../views/AgentConsole";
import { theme } from "../theme";
import "../i18n";

// Minimal /control/status payload with two monitors.
const STATUS = {
  machine: "box1",
  monitors: {
    // Names distinct from type_ids (the type renders as a chip in the same row).
    "lada-main": { state: "running", type_id: "lada" },
    downloads: { state: "idle", type_id: "folder_watch" },
  },
};

// Route fetch by path: status resolves; everything else (plugins/events) is empty
// so the views don't hang or error.
function stubFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      const body =
        url.includes("/control/status") ? STATUS
        : url.includes("/control/plugins") ? { plugins: [], presets: [] }
        : { events: [] };
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    }),
  );
}

const renderConsole = () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <ThemeProvider theme={theme}>
      <QueryClientProvider client={qc}>
        <AgentConsole />
      </QueryClientProvider>
    </ThemeProvider>,
  );
};

// Agent Console polish (#92).
describe("AgentConsole", () => {
  beforeEach(stubFetch);

  it("lists the machine's monitors once status loads", async () => {
    renderConsole();
    // `downloads` is unique (unselected row); `lada-main` also appears in the
    // detail-pane heading once selected, so assert it shows up at least once.
    expect(await screen.findByText("downloads")).toBeInTheDocument();
    expect(screen.getAllByText("lada-main").length).toBeGreaterThan(0);
  });

  it("marks the selected monitor row with aria-pressed", async () => {
    renderConsole();
    await screen.findByText("downloads");
    // The first monitor is auto-selected; scope to that row.
    const selected = document.querySelector('[aria-pressed="true"]');
    expect(selected).toBeTruthy();
    expect(within(selected as HTMLElement).getByText("lada-main")).toBeInTheDocument();
  });

  it("shows an 'updated' freshness timestamp", async () => {
    renderConsole();
    await waitFor(() =>
      expect(screen.getAllByText(/Updated|更新于/).length).toBeGreaterThan(0),
    );
  });

  const stubStatus = (monitors: Record<string, unknown>) =>
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      const body = url.includes("/control/status") ? { machine: "box1", monitors }
        : url.includes("/control/plugins") ? { plugins: [], presets: [] } : { events: [] };
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    }));

  it("renders a single monitor as a full-width hero, no rail (#134)", async () => {
    stubStatus({ "lada-main": { state: "running", type_id: "lada" } });
    renderConsole();
    await screen.findByText("lada-main");
    // The hero is not a selectable rail list → no aria-pressed row.
    expect(document.querySelector('[aria-pressed="true"]')).toBeNull();
  });

  it("uses a horizontal pill selector for multiple monitors, and swaps the hero (#135)", async () => {
    renderConsole(); // 2 monitors (lada-main auto-selected)
    await screen.findByText("downloads");
    // A trailing "+ Add monitor" pill exists.
    expect(screen.getByRole("button", { name: /Add monitor|添加监控/ })).toBeInTheDocument();
    // Selecting the "downloads" pill marks it current (paired with aria-pressed).
    fireEvent.click(screen.getByText("downloads"));
    const sel = document.querySelector('[aria-pressed="true"]');
    expect(within(sel as HTMLElement).getByText("downloads")).toBeInTheDocument();
  });

  it("shows an empty-state Add CTA when there are no monitors (#134)", async () => {
    stubStatus({});
    renderConsole();
    await screen.findByText(/No monitors yet|还没有监控/);
    expect(screen.getByRole("button", { name: /Add|添加/ })).toBeInTheDocument();
  });

  it("shows an inline recent-events panel on the monitor dashboard (#136)", async () => {
    renderConsole(); // monitors tab (default), 2 monitors → hero + inline events
    await screen.findByText("downloads");
    // The hero carries an inline "Recent events" panel beside the metrics.
    expect(screen.getByText(/^Recent events$|^最近事件$/)).toBeInTheDocument();
  });

  it("scopes the inline events panel to the selected monitor (#130)", async () => {
    renderConsole(); // lada-main auto-selected
    await screen.findByText("downloads");
    // The inline panel fetches /control/events filtered to the current monitor.
    await waitFor(() => {
      const calls = (fetch as unknown as { mock: { calls: unknown[][] } }).mock.calls;
      const urls = calls.map((c) => String(c[0]));
      expect(urls.some((u) => /\/control\/events\?.*monitor=lada-main/.test(u))).toBe(true);
    });
  });

  // #145: auth-disabled banner is driven by /control/config { auth_disabled }.
  const stubConfig = (authDisabled: boolean) =>
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        const body =
          url.includes("/control/status") ? STATUS
          : url.includes("/control/config") ? { monitors: [], auth_disabled: authDisabled }
          : url.includes("/control/plugins") ? { plugins: [], presets: [] }
          : { events: [] };
        return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
      }),
    );

  // Language is non-deterministic in tests (i18n detector), so match either locale.
  const BANNER = /Auth is disabled|鉴权已禁用/;

  it("shows the auth-disabled banner when the API has no token (#145)", async () => {
    stubConfig(true);
    renderConsole();
    expect(await screen.findByText(BANNER)).toBeInTheDocument();
  });

  it("hides the auth-disabled banner when a token is set (#145)", async () => {
    stubConfig(false);
    renderConsole();
    await screen.findByText("downloads"); // console loaded
    expect(screen.queryByText(BANNER)).not.toBeInTheDocument();
  });
});
