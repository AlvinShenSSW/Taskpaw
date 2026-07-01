import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, within, waitFor } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HubAgentManager } from "../components/HubAgentManager";
import { type HubServer } from "../api";
import { theme } from "../theme";
import "../i18n";

const SERVERS: HubServer[] = [
  { id: 1, name: "render-01", ip: "192.168.1.80", port: 5678, enabled: 1 },
  { id: 2, name: "render-02", ip: "192.168.1.81", port: 5680, enabled: 0 },
];

// Capture the requests the manager fires; return ok for mutations.
let calls: { method: string; url: string; body?: unknown }[] = [];
function stubFetch() {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({ method: init?.method ?? "GET", url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
      return Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true }) });
    }),
  );
}

const renderMgr = () => {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <ThemeProvider theme={theme}>
      <QueryClientProvider client={qc}>
        <HubAgentManager servers={SERVERS} />
      </QueryClientProvider>
    </ThemeProvider>,
  );
};

describe("HubAgentManager (#124)", () => {
  beforeEach(stubFetch);

  it("lists the registered agents with ip:port", () => {
    renderMgr();
    expect(screen.getByText("render-01")).toBeInTheDocument();
    expect(screen.getByText("192.168.1.80:5678")).toBeInTheDocument();
    expect(screen.getByText("192.168.1.81:5680")).toBeInTheDocument();
  });

  it("adds a new agent → POST /servers with the entered fields", async () => {
    renderMgr();
    // The add row is the last set of Name/IP/Port fields.
    const names = screen.getAllByLabelText(/Name|名称/);
    const ips = screen.getAllByLabelText(/IP/);
    const ports = screen.getAllByLabelText(/Port|端口/);
    fireEvent.change(names[names.length - 1], { target: { value: "render-03" } });
    fireEvent.change(ips[ips.length - 1], { target: { value: "192.168.1.82" } });
    fireEvent.change(ports[ports.length - 1], { target: { value: "5678" } });
    fireEvent.click(screen.getByRole("button", { name: /Add|添加/ }));
    await waitFor(() => {
      const post = calls.find((c) => c.method === "POST" && c.url.includes("/servers"));
      expect(post).toBeTruthy();
      expect(post!.body).toEqual({ name: "render-03", ip: "192.168.1.82", port: 5678 });
    });
  });

  it("deletes an agent after confirming → DELETE /servers/{id}", async () => {
    renderMgr();
    // Click the delete icon on the first row.
    fireEvent.click(screen.getAllByLabelText(/delete|删除/i)[0]);
    // Confirm in the dialog.
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: /Delete|删除/ }));
    await waitFor(() =>
      expect(calls.some((c) => c.method === "DELETE" && c.url.includes("/servers/1"))).toBe(true),
    );
  });

  it("saves the polling token → PATCH /config", async () => {
    renderMgr();
    const tokenField = screen.getByLabelText(/Polling token|轮询令牌/);
    fireEvent.change(tokenField, { target: { value: "sekret" } });
    // The token row's save button (last "save" button).
    const saves = screen.getAllByRole("button", { name: /Save|保存/ });
    fireEvent.click(saves[saves.length - 1]);
    await waitFor(() => {
      const patch = calls.find((c) => c.method === "PATCH" && c.url.includes("/config"));
      expect(patch).toBeTruthy();
      expect(patch!.body).toEqual({ polling_token: "sekret" });
    });
  });
});
