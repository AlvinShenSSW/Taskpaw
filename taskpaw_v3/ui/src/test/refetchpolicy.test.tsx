// #379 item 7: the production QueryClient has no default refetchInterval. Static queries
// load once; live queries keep the cadence they declare themselves.
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClientProvider } from "@tanstack/react-query";
import { ControlCredentialError } from "../api";
import { createQueryClient } from "../queryClient";
import { theme } from "../theme";
import { AgentHome } from "../views/SetupWizard";

const STATUS = { machine: "box1", monitors: { "lada-main": { state: "running", type_id: "lada" }, downloads: { state: "idle", type_id: "folder_watch" } } };

afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });

describe("refetch policy", () => {
  it("has no default interval and still refuses to retry a credential error", () => {
    const defaults = createQueryClient().getDefaultOptions().queries;
    expect(defaults?.refetchInterval).toBeUndefined();
    const retry = defaults?.retry as (count: number, error: Error) => boolean;
    expect(retry(0, new ControlCredentialError())).toBe(false);
    expect(retry(0, new Error("x"))).toBe(true);
    expect(retry(1, new Error("x"))).toBe(false);
  });

  it("loads static agent queries once and keeps the live ones at 5 s", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn((url: string) => {
      const body = url.includes("/control/setup") ? { status: "done" }
        : url.includes("/control/status") ? STATUS
        : url.includes("/control/plugins") ? { plugins: [], presets: [] }
        : {};
      return Promise.resolve({ ok: true, json: () => Promise.resolve(body) });
    });
    vi.stubGlobal("fetch", fetcher);
    render(<ThemeProvider theme={theme}><QueryClientProvider client={createQueryClient()}><AgentHome /></QueryClientProvider></ThemeProvider>);
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    await act(async () => { await vi.advanceTimersByTimeAsync(15000); });
    const count = (path: string) => fetcher.mock.calls.filter(([url]) => new URL(url).pathname === path).length;
    expect(count("/control/setup")).toBe(1);
    expect(count("/control/plugins")).toBe(1);
    expect(count("/control/status")).toBeGreaterThanOrEqual(4);
    expect(count("/control/config")).toBeGreaterThanOrEqual(4);
  });
});
