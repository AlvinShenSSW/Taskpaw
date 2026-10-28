import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "../api";
import { HubDashboard } from "../views/HubDashboard";
import i18n from "../i18n";

const clients: QueryClient[] = [];
const fixture = (over: Record<string, unknown> = {}) => ({
  machine: "hub-health-fixture", acks: {}, self: { "hub-host": { state: "ok", metrics: { cpu_pct: 42 } } },
  servers: [{ id: 1, name: "mac-fixture", ip: "10.0.0.1", port: 5680, enabled: 1, online: true,
    last_seen: "2026-10-26T01:00:00Z", snapshot: { machine: "mac-fixture", monitors: { host: { state: "ok", metrics: { cpu_pct: 27 } } } },
    status_health: { state: "ok", error_code: null, age_seconds: 2, last_good_at: "2026-10-26T01:00:00Z" },
    event_channel: { state: "ok", reason: null }, ...over }],
  notifications: { state: "never_sent", pending_count: 0, failed_count: 0, dead_letter_count: 0, quarantined_count: 0,
    oldest_pending_age_seconds: null, oldest_pending_at: null, last_success_at: null, last_failure_at: null, last_failure_reason: null },
});
function show(body = fixture()) {
  vi.spyOn(api, "hubStatus").mockResolvedValue(body);
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } }); clients.push(qc);
  return { ...render(<QueryClientProvider client={qc}><HubDashboard /></QueryClientProvider>), qc };
}
beforeEach(() => { void i18n.changeLanguage("en"); });
afterEach(() => { cleanup(); clients.splice(0).forEach(qc => qc.clear()); vi.restoreAllMocks(); vi.useRealTimers(); void i18n.changeLanguage("zh-CN"); });

describe("Hub status evidence", () => {
  it("shows actual sample age, last success, event channel failure and notification backlog", async () => {
    const body = fixture({ event_channel: { state: "error", reason: "auth_failed" } });
    Object.assign(body.notifications, { state: "dead_letter", pending_count: 7, failed_count: 2, dead_letter_count: 3, quarantined_count: 1,
      oldest_pending_age_seconds: 120, last_success_at: "2026-10-26T00:00:00Z", last_failure_at: "2026-10-26T00:30:00Z", last_failure_reason: "upstream_timeout" });
    show(body);
    expect(await screen.findByText(/Sample age: 2s/)).toBeInTheDocument();
    expect(screen.getByText(/Last successful sample:/)).toHaveTextContent("10/26/2026");
    expect(screen.getByText(/Events: error · auth_failed/)).toBeInTheDocument();
    const notifications = screen.getByRole("region", { name: "OpenClaw delivery" });
    expect(notifications).toHaveTextContent("Pending: 7");
    expect(notifications).toHaveTextContent("Oldest pending: 2m");
    expect(notifications).toHaveTextContent("Failed: 2");
    expect(notifications).toHaveTextContent("Dead letters: 3");
    expect(notifications).toHaveTextContent("Quarantined: 1");
    expect(notifications).toHaveTextContent("upstream_timeout");
  });

  it.each(["auth_failed", "invalid_status", "network_unreachable"])("retains last success and explains %s without showing healthy metrics", async reason => {
    show(fixture({ online: false, status_health: { state: "error", error_code: reason, age_seconds: 90, last_good_at: "2026-10-26T01:00:00Z" } }));
    await screen.findByText("mac-fixture");
    expect(screen.getByText(new RegExp(`Reason: ${reason}`))).toBeInTheDocument();
    expect(screen.getByText(/Sample age: 1m/)).toBeInTheDocument();
    expect(screen.queryByText("27%")).not.toBeInTheDocument();
  });

  it("ages cached data across a suspended clock and marks it stale before another reply", async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-10-26T01:00:02Z"));
    show(); await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(screen.getByText("online")).toBeInTheDocument();
    vi.setSystemTime(new Date("2026-10-26T01:02:02Z"));
    fireEvent.focus(window);
    expect(screen.queryByText("online")).not.toBeInTheDocument();
    expect(screen.getByText(/Sample age: 2m/)).toBeInTheDocument();
    expect(screen.queryByText("27%")).not.toBeInTheDocument();
    expect(screen.queryByText("42%")).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("Hub response is outdated");
  });

  it("does not make a sleeping cached sample fresh again after a wall-clock rollback", async () => {
    vi.useFakeTimers(); vi.setSystemTime(new Date("2026-10-26T01:00:02Z"));
    show(); await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    vi.setSystemTime(new Date("2026-10-26T01:02:02Z")); fireEvent.focus(window);
    expect(screen.queryByText("online")).not.toBeInTheDocument();
    vi.setSystemTime(new Date("2026-10-26T01:00:07Z")); fireEvent.focus(window);
    expect(screen.queryByText("online")).not.toBeInTheDocument();
    expect(screen.getByText(/Sample age: 2m/)).toBeInTheDocument();
  });

  it("retains cached rows on failed sampling and recovers only after success", async () => {
    const { qc } = show(); await screen.findByText("online");
    vi.mocked(api.hubStatus).mockRejectedValue(new Error("network unavailable"));
    await act(async () => { await qc.refetchQueries({ queryKey: ["hubStatus"] }); });
    expect(screen.getByText("mac-fixture")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByText("online")).not.toBeInTheDocument());
    expect(screen.queryByText("27%")).not.toBeInTheDocument();
    vi.mocked(api.hubStatus).mockResolvedValue(fixture());
    await act(async () => { await qc.refetchQueries({ queryKey: ["hubStatus"] }); });
    expect(await screen.findByText("online")).toBeInTheDocument();
  });

  it.each(["disabled", "missing_token"])("explains %s notifications while retaining local history evidence", async state => {
    const body = fixture(); body.notifications.state = state; show(body);
    const notifications = await screen.findByRole("region", { name: "OpenClaw delivery" });
    expect(notifications).toHaveTextContent(state === "disabled" ? "Notifications disabled" : "Token not configured");
    expect(notifications).toHaveTextContent("Local Hub events are still retained");
    expect(notifications).toHaveTextContent("Last delivery: never");
  });

  it("separates intentionally disabled machines from offline failures", async () => {
    show(fixture({ enabled: 0, online: false })); await screen.findByText("mac-fixture");
    const counts = screen.getByLabelText("Fleet health");
    expect(within(counts).getByText("disabled")).toHaveTextContent("1 disabled");
    expect(within(counts).getByText("offline")).toHaveTextContent("0 offline");
  });

  it("releases freshness timers and listeners on unmount", async () => {
    vi.useFakeTimers(); const view = show();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    view.unmount(); view.qc.clear();
    expect(vi.getTimerCount()).toBe(0);
  });
});
