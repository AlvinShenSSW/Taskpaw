import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { api, clearControlCredentials, hasControlCredential, setDevControlCredential } from "../api";
import { App } from "../App";
vi.mock("../views/AgentConsole", () => ({ AgentConsole: function Console() {
  useQuery({ queryKey: ["agentStatus"], queryFn: api.agentStatus, refetchInterval: 10, retry: false });
  return <button onClick={() => { void api.stopMonitor("task").catch(() => {}); }}>Stop fake task</button>;
} }));
vi.mock("../views/HubDashboard", () => ({ HubDashboard: () => <div>Hub fake dashboard</div> }));
beforeEach(() => { clearControlCredentials(); setDevControlCredential("agent", "fake-app-key"); });
describe("App credential expiry", () => {
  it("unmounts the role view, clears cached data and stops interval refetch after 401", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response("{}", { status: 401 }))));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    qc.setQueryData(["agentConfig"], { private: "fake-cache" });
    render(<QueryClientProvider client={qc}><App /></QueryClientProvider>);
    await screen.findByLabelText(/控制令牌|Control token/);
    expect(screen.queryByText("Stop fake task")).not.toBeInTheDocument();
    expect(hasControlCredential("agent")).toBe(false);
    await waitFor(() => expect(qc.getQueryCache().getAll()).toHaveLength(0));
    await new Promise(done => setTimeout(done, 40));
    expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("does not retry or replay an expired mutation", async () => {
    vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve(new Response("{}", { status: url.includes("/stop?") ? 401 : 200 }))));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={qc}><App /></QueryClientProvider>);
    fireEvent.click(screen.getByText("Stop fake task"));
    await screen.findByLabelText(/控制令牌|Control token/);
    expect(vi.mocked(fetch).mock.calls.filter(c => String(c[0]).includes("/stop?"))).toHaveLength(1);
    await expect(api.stopMonitor("task")).rejects.toThrow();
    expect(vi.mocked(fetch).mock.calls.filter(c => String(c[0]).includes("/stop?"))).toHaveLength(1);
  });
});
