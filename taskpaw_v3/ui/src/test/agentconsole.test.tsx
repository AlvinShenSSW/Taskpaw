import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
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

  it("marks the selected monitor row with aria-current", async () => {
    renderConsole();
    await screen.findByText("downloads");
    // The first monitor is auto-selected; scope to that row.
    const selected = document.querySelector('[aria-current="true"]');
    expect(selected).toBeTruthy();
    expect(within(selected as HTMLElement).getByText("lada-main")).toBeInTheDocument();
  });

  it("shows an 'updated' freshness timestamp", async () => {
    renderConsole();
    await waitFor(() =>
      expect(screen.getAllByText(/Updated|更新于/).length).toBeGreaterThan(0),
    );
  });
});
