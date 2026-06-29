import { describe, expect, it, vi, beforeAll } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "../App";
import { theme } from "../theme";
import "../i18n";

// The role views fetch over the network; in jsdom there's no server, so stub fetch
// to fail fast. The views render their unreachable state — the app shell (top bar)
// renders regardless, which is what these tests cover.
beforeAll(() => {
  vi.stubGlobal("fetch", vi.fn(() => Promise.reject(new Error("offline"))));
});

const renderApp = () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <ThemeProvider theme={theme}>
      <QueryClientProvider client={qc}>
        <App />
      </QueryClientProvider>
    </ThemeProvider>,
  );
};

// App shell & top bar (#91).
describe("App shell", () => {
  it("uses an SVG paw mark, not the 🐾 emoji", () => {
    const { container } = renderApp();
    expect(container.textContent ?? "").not.toContain("🐾");
    expect(container.querySelector("svg")).toBeInTheDocument(); // the paw logo
  });

  it("shows a health badge with text (status not color-only)", () => {
    renderApp();
    expect(screen.getAllByText(/在线|Online/).length).toBeGreaterThan(0);
  });

  it("offers a segmented role switcher with correct aria-selected", () => {
    renderApp();
    // Scope to the role switcher tablist (the role views render their own inner
    // Monitors/Events/Settings tabs too).
    const roleStrip = screen.getByRole("tablist", { name: /Agent.*Hub|Hub.*Agent/ });
    const tabs = within(roleStrip).getAllByRole("tab");
    expect(tabs.length).toBe(2);
    // Default role is agent → its tab is selected, the hub tab is not.
    const agentTab = within(roleStrip).getByRole("tab", { name: /Agent/ });
    const hubTab = within(roleStrip).getByRole("tab", { name: /Hub/ });
    expect(agentTab).toHaveAttribute("aria-selected", "true");
    expect(hubTab).toHaveAttribute("aria-selected", "false");

    // Switching updates the selection.
    fireEvent.click(hubTab);
    expect(within(roleStrip).getByRole("tab", { name: /Hub/ })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });
});
