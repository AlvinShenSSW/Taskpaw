import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { EventLog } from "../components/EventLog";
import { MonitorMetrics } from "../components/MonitorMetrics";
import { Settings } from "../views/Settings";

// Component smoke tests (#45): the pure, prop-driven components render their data
// without crashing. Heavier views (AgentConsole/HubDashboard) need react-query +
// network mocks and are out of scope for these smoke tests.

describe("EventLog", () => {
  it("shows the empty state when there are no events", () => {
    render(<EventLog events={[]} />);
    expect(screen.getByText(/暂无事件|No events yet/)).toBeInTheDocument();
  });

  it("renders an event's message, level and source", () => {
    render(<EventLog events={[{ id: 1, message: "restore done", monitor: "lada",
      level: "done", machine: "box1" }]} />);
    expect(screen.getByText("restore done")).toBeInTheDocument();
    expect(screen.getByText("done")).toBeInTheDocument();       // level chip (raw)
    expect(screen.getByText(/box1.*lada/)).toBeInTheDocument(); // where · monitor
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

  it("shows system RAM as used/total GB under the MEM gauge (#128)", () => {
    render(<MonitorMetrics metrics={{ mem_pct: 47, mem_used_mb: 7680, mem_total_mb: 16384 }} />);
    expect(screen.getByText("MEM")).toBeInTheDocument();
    // 7680 MB = 7.5 GB, 16384 MB = 16.0 GB → GB sub-label, not a raw tile.
    expect(screen.getByText("7.5 GB / 16.0 GB")).toBeInTheDocument();
    expect(screen.queryByText(/mem used|mem_used_mb/)).not.toBeInTheDocument();
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
