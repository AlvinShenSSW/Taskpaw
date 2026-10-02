import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { App } from "../App";
import { setLang } from "../i18n";
import { clearControlCredentials, ControlCredentialError, setDevControlCredential, type ControlRole } from "../api";

const clients: QueryClient[] = [];
function show(role: ControlRole = "agent", retry = false) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: retry ? (count, error) => !(error instanceof ControlCredentialError) && count < 1 : false } } });
  clients.push(qc);
  const view = render(<QueryClientProvider client={qc}><App /></QueryClientProvider>);
  const roles = screen.getByRole("tablist", { name: /Agent.*Hub/ });
  fireEvent.click(within(roles).getByRole("tab", { name: role === "agent" ? /Agent/ : /Hub/ }));
  return { ...view, qc, roles };
}
beforeEach(() => setLang("en"));
afterEach(() => {
  const roles = screen.queryByRole("tablist", { name: /Agent.*Hub/ });
  if (roles) fireEvent.click(within(roles).getByRole("tab", { name: /Agent/ }));
  cleanup();
  clients.splice(0).forEach(qc => qc.clear());
  vi.unstubAllGlobals();
  vi.useRealTimers();
  setLang("zh-CN");
});

const badge = () => screen.getByRole("status", { name: "Local API connection" });
const statusFor = (role: ControlRole) => role === "agent" ? { machine: "fixture-agent", monitors: {} } : { machine: "fixture-hub", servers: [], self: {}, acks: {} };
const response = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const tick = async (ms = 0) => { await act(async () => { await vi.advanceTimersByTimeAsync(ms); }); };

describe("connection lifecycle and ownership", () => {
  it.each(["en", "zh-CN"] as const)("localizes the accessible connection state (%s)", async language => {
    setLang(language);
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    show();
    const connection = screen.getByRole("status", { name: language === "en" ? "Local API connection" : "本地 API 连接" });
    await waitFor(() => expect(connection).toHaveTextContent(language === "en" ? "Connection failed" : "连接失败"));
    expect(connection).not.toHaveTextContent(/Online|在线/);
  });

  it.each(["agent", "hub"] as const)("uses only the %s status query and adds no transport owner", async role => {
    const urlPath = role === "agent" ? "/control/status" : "/status";
    const fetcher = vi.fn((url: string) => Promise.resolve(response(new URL(url).pathname === urlPath ? statusFor(role) : { monitors: [], plugins: [], presets: [] })));
    vi.stubGlobal("fetch", fetcher);
    show(role);
    await waitFor(() => expect(badge()).toHaveTextContent("Connected"));
    expect(fetcher.mock.calls.filter(([url]) => new URL(url).pathname === urlPath)).toHaveLength(1);
    expect(badge()).toHaveTextContent("Last successful API response:");
  });

  it.each(["agent", "hub"] as const)("expires a pending %s refresh at exactly 15 seconds without duplicate requests", async role => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-10-02T12:00:00Z"));
    const path = role === "agent" ? "/control/status" : "/status";
    const pending = deferred<Response>();
    let calls = 0;
    vi.stubGlobal("fetch", vi.fn((url: string) => new URL(url).pathname === path
      ? (++calls === 1 ? Promise.resolve(response(statusFor(role))) : pending.promise)
      : Promise.resolve(response({ monitors: [], plugins: [], presets: [] }))));
    const { unmount } = show(role); await tick();
    expect(badge()).toHaveTextContent("Connected");
    const lastSuccess = within(badge()).getByText(/Last successful API response/).textContent;
    await tick(5000); await tick(9999);
    expect(badge()).toHaveTextContent("Connected");
    expect(badge()).toHaveTextContent(/updating/i);
    expect(within(badge()).getByText(/Last successful API response/)).toHaveTextContent(lastSuccess!);
    await tick(1);
    expect(badge()).toHaveTextContent("Last response outdated");
    expect(calls).toBe(2);
    await act(async () => { pending.resolve(response(statusFor(role))); }); await tick();
    expect(badge()).toHaveTextContent("Connected");
    expect(within(badge()).getByText(/Last successful API response/).textContent).not.toBe(lastSuccess);
    unmount(); const count = calls; await tick(20000);
    expect(calls).toBe(count);
  });

  it("shows a transient failed attempt throughout its automatic retry and recovers", async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-10-02T12:00:00Z"));
    const pending = deferred<Response>();
    let calls = 0;
    vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("/control/status")
      ? (++calls === 1 ? Promise.reject(new Error("private-failure")) : pending.promise)
      : Promise.resolve(response({ monitors: [], plugins: [], presets: [] }))));
    show("agent", true); await tick();
    expect(badge()).toHaveTextContent("Connection failed");
    expect(badge()).not.toHaveTextContent("Last successful API response");
    await tick(1000);
    expect(badge()).toHaveTextContent("Connection failed");
    expect(calls).toBe(2);
    await act(async () => { pending.resolve(response(statusFor("agent"))); }); await tick();
    expect(badge()).toHaveTextContent("Connected");
  });

  it("a failed refresh keeps last success, and a successful events call cannot hide failed status", async () => {
    let fail = false;
    vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("/status")
      ? (fail ? Promise.reject(new Error("offline")) : Promise.resolve(response(statusFor("hub"))))
      : Promise.resolve(response({ events: [{ id: 1, message: "independent history" }], monitors: [], plugins: [], presets: [] }))));
    const { qc } = show("hub");
    await waitFor(() => expect(badge()).toHaveTextContent("Connected"));
    const lastSuccess = within(badge()).getByText(/Last successful API response/).textContent;
    fail = true; await qc.refetchQueries({ queryKey: ["hubStatus"] });
    await waitFor(() => expect(badge()).toHaveTextContent("Connection failed"));
    fireEvent.click(screen.getByRole("tab", { name: "Events" }));
    await screen.findByText("independent history");
    expect(badge()).toHaveTextContent("Connection failed");
    expect(within(badge()).getByText(/Last successful API response/)).toHaveTextContent(lastSuccess!);
    fireEvent.click(screen.getByRole("tab", { name: "Settings" }));
    expect(screen.getByText("Language")).toBeInTheDocument();
  });

  it("recomputes response expiry on resume and clears its deadline on unmount", async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-10-02T12:00:00Z"));
    vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve(response(url.endsWith("/control/status") ? statusFor("agent") : { monitors: [], plugins: [], presets: [] }))));
    const view = show(); await tick();
    expect(badge()).toHaveTextContent("Connected");
    vi.setSystemTime(new Date("2026-10-02T12:01:00Z"));
    fireEvent(window, new Event("focus"));
    expect(badge()).toHaveTextContent("Last response outdated");
    view.unmount(); view.qc.clear(); await tick();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("role switches and late responses cannot share connection evidence", async () => {
    const agent = deferred<Response>();
    vi.stubGlobal("fetch", vi.fn((url: string) => url.endsWith("/control/status") ? agent.promise : url.endsWith("/status") ? Promise.reject(new Error("offline-hub")) : Promise.resolve(response({ monitors: [], plugins: [], presets: [] }))));
    const { roles, qc } = show();
    fireEvent.click(within(roles).getByRole("tab", { name: /Hub/ }));
    await waitFor(() => expect(badge()).toHaveTextContent("Connection failed"));
    await act(async () => { agent.resolve(response(statusFor("agent"))); });
    expect(badge()).toHaveTextContent("Connection failed");
    expect(badge()).not.toHaveTextContent("Last successful API response");
    fireEvent.click(within(roles).getByRole("tab", { name: /Agent/ }));
    await waitFor(() => expect(badge()).toHaveTextContent("Connected"));
    act(() => qc.removeQueries({ queryKey: ["agentStatus"] }));
    expect(badge()).toHaveTextContent("Checking");
    expect(badge()).not.toHaveTextContent("Last successful API response");
  });

  it.each(["agent", "hub"] as const)("%s status 401 gates immediately and does not retry protected requests", async role => {
    vi.useFakeTimers();
    const path = role === "agent" ? "/control/status" : "/status";
    const fetcher = vi.fn((url: string) => Promise.resolve(response({}, new URL(url).pathname === path ? 401 : 200)));
    vi.stubGlobal("fetch", fetcher);
    const { qc } = show(role, true); await tick();
    expect(badge()).toHaveTextContent("Credentials required");
    expect(badge()).not.toHaveTextContent("Last successful API response");
    expect(screen.getByLabelText("Control token")).toBeInTheDocument();
    expect(qc.getQueryCache().getAll()).toHaveLength(0);
    const calls = fetcher.mock.calls.length; await tick(20000);
    expect(fetcher).toHaveBeenCalledTimes(calls);
  });

  it("missing credentials hide cached evidence; a verified reconnect starts fresh", async () => {
    clearControlCredentials("agent");
    vi.stubGlobal("fetch", vi.fn((url: string) => Promise.resolve(response(url.endsWith("/control/status") ? statusFor("agent") : { monitors: [], plugins: [], presets: [] }))));
    const { qc } = show();
    expect(badge()).toHaveTextContent("Credentials required");
    expect(fetch).not.toHaveBeenCalled();
    act(() => setDevControlCredential("agent", "current-fixture"));
    await waitFor(() => expect(badge()).toHaveTextContent("Connected"));
    act(() => clearControlCredentials("agent"));
    expect(badge()).toHaveTextContent("Credentials required");
    expect(qc.getQueryCache().getAll()).toHaveLength(0);
    expect(badge()).not.toHaveTextContent("Last successful API response");
  });

  it("events 401 clears the Hub cache and a late status response cannot restore connection", async () => {
    const pending = deferred<Response>();
    let statusCalls = 0;
    vi.stubGlobal("fetch", vi.fn((url: string) => {
      const path = new URL(url).pathname;
      if (path === "/status") return ++statusCalls === 1 ? Promise.resolve(response(statusFor("hub"))) : pending.promise;
      if (path === "/events") return Promise.resolve(response({}, 401));
      return Promise.resolve(response({ monitors: [], plugins: [], presets: [] }));
    }));
    const { qc } = show("hub", true);
    await waitFor(() => expect(badge()).toHaveTextContent("Connected"));
    const refetch = qc.refetchQueries({ queryKey: ["hubStatus"] });
    fireEvent.click(screen.getByRole("tab", { name: "Events" }));
    await screen.findByLabelText("Control token");
    expect(badge()).toHaveTextContent("Credentials required");
    expect(qc.getQueryCache().getAll()).toHaveLength(0);
    await act(async () => { pending.resolve(response(statusFor("hub"))); await refetch; });
    expect(qc.getQueryCache().getAll()).toHaveLength(0);
    expect(badge()).not.toHaveTextContent("Connected");
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();
  });
});

describe("local API connection evidence", () => {
  it("does not claim Online while the first status response is pending", () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    show();
    expect(screen.queryByText("Online")).not.toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Local API connection" })).toHaveTextContent("Checking");
  });
  it("does not claim Online after the local status request fails", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new Error("offline")));
    show();
    await screen.findByRole("alert");
    expect(screen.queryByText("Online")).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("status", { name: "Local API connection" })).toHaveTextContent("Connection failed"));
  });
});
