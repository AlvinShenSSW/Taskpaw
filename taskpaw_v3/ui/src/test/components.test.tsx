import { describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { EventLog } from "../components/EventLog";
import { MonitorMetrics } from "../components/MonitorMetrics";
import { Settings } from "../views/Settings";
import { setLang } from "../i18n";

// Component smoke tests (#45): the pure, prop-driven components render their data
// without crashing. Heavier views (AgentConsole/HubDashboard) need react-query +
// network mocks and are out of scope for these smoke tests.

describe("EventLog", () => {
  it("shows the empty state when there are no events", () => {
    render(<EventLog events={[]} hasData />);
    expect(screen.getByText(/暂无事件|No events yet/)).toBeInTheDocument();
  });

  it("renders an event's message, level and source", () => {
    render(<EventLog events={[{ id: 1, message: "restore done", monitor: "lada",
      level: "done", machine: "box1" }]} hasData />);
    expect(screen.getByText("restore done")).toBeInTheDocument();
    expect(screen.getByText("done")).toBeInTheDocument();       // level chip (raw)
    expect(screen.getByText(/box1.*lada/)).toBeInTheDocument(); // where · monitor
  });

  it.each(["en", "zh-CN"] as const)("expires a successful empty response with accessible old-data copy (%s)", async lang => {
    setLang(lang); vi.useFakeTimers();
    const time = new Date("2026-10-02T12:00:00Z").getTime(); vi.setSystemTime(time);
    try {
      const view = render(<EventLog events={[]} hasData lastSuccessAt={time} fetching />);
      expect(screen.getByText(/暂无事件|No events yet/)).toBeInTheDocument();
      expect(screen.getByRole("status", { name: /Event request status|事件请求状态/ })).toHaveTextContent(/updating|更新中/);
      await act(async () => { await vi.advanceTimersByTimeAsync(14999); });
      expect(screen.getByText(/暂无事件|No events yet/)).toBeInTheDocument();
      await act(async () => { await vi.advanceTimersByTimeAsync(1); });
      expect(screen.queryByText(/暂无事件|No events yet/)).not.toBeInTheDocument();
      expect(screen.getByText(/Showing the last successfully loaded events|显示上次成功读取的事件/)).toBeInTheDocument();
      view.unmount(); expect(vi.getTimerCount()).toBe(0);
    } finally { vi.useRealTimers(); setLang("zh-CN"); }
  });
});

describe("MonitorMetrics", () => {
  it("renders nothing for empty metrics", () => {
    const { container } = render(<MonitorMetrics metrics={{}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders the current file and queue numbers", () => {
    render(<MonitorMetrics metrics={{
      current_file: "clip.mp4", queue_completed: 2, queue_total: 49, gpu_pct: 25,
    }} />);
    expect(screen.getByText("clip.mp4")).toBeInTheDocument();
    expect(screen.getByText(/2 \/ 49/)).toBeInTheDocument();    // queue progress
    expect(screen.getByText("GPU")).toBeInTheDocument();        // utilization gauge
  });
});

describe("Settings · About", () => {
  it("shows the product name and author 304", () => {
    render(<Settings role="hub" />); // hub: no agent-config network fetch
    // "TaskPaw" + "304" each appear more than once (heading/blurb, author/copyright).
    expect(screen.getAllByText(/TaskPaw/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/304/).length).toBeGreaterThan(0);
  });
});
