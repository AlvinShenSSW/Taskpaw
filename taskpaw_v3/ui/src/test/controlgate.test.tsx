import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ControlCredentialGate } from "../components/ControlCredentialGate";
import { api, clearControlCredentials, hasControlCredential, setDevControlCredential } from "../api";

beforeEach(() => { clearControlCredentials(); delete window.__TASKPAW__; });
describe("credential gate", () => {
  it("validates dev input with protected status before unlocking the role", async () => {
    let resolve: (value: Response) => void = () => {};
    vi.stubGlobal("fetch", vi.fn(() => new Promise<Response>(done => { resolve = done; })));
    const { container } = render(<ControlCredentialGate role="agent" desktop={false} />);
    const input = screen.getByLabelText(/控制令牌|Control token/);
    expect(input).toHaveAttribute("type", "password"); expect(input).toHaveAttribute("autocomplete", "off");
    fireEvent.change(input, { target: { value: "fake-gate-input" } }); fireEvent.submit(container.querySelector("form")!);
    expect(input).toHaveValue(""); expect(hasControlCredential("agent")).toBe(false);
    resolve(new Response("{}", { status: 200 }));
    await waitFor(() => expect(hasControlCredential("agent")).toBe(true));
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(vi.mocked(fetch).mock.calls[0][0]).toBe("http://127.0.0.1:5681/control/status");
    expect(localStorage.getItem("fake-gate-input")).toBeNull();
  });
  it("rejects expired input without revealing it or unlocking the role", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response("{}", { status: 401 }))));
    const { container } = render(<ControlCredentialGate role="hub" desktop={false} />);
    fireEvent.change(screen.getByLabelText(/控制令牌|Control token/), { target: { value: "fake-expired-secret" } });
    fireEvent.submit(container.querySelector("form")!);
    await screen.findByRole("alert"); expect(container.textContent).not.toContain("fake-expired-secret");
    expect(hasControlCredential("hub")).toBe(false); expect(fetch).toHaveBeenCalledTimes(1);
  });
  it("desktop expiry clears injected credentials and requires reopening without a dev fallback", async () => {
    window.__TASKPAW__ = { role: "agent", baseUrl: "http://127.0.0.1:5681", controlToken: "fake-desktop-key", bootId: "0123456789abcdef0123456789abcdef" };
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response("{}", { status: 401 }))));
    await expect(api.agentStatus()).rejects.toThrow();
    expect(window.__TASKPAW__.controlToken).toBeUndefined(); expect(hasControlCredential("agent")).toBe(false);
    await expect(api.startMonitor("task")).rejects.toThrow(); expect(fetch).toHaveBeenCalledTimes(1);
    render(<ControlCredentialGate role="agent" desktop />);
    expect(screen.queryByLabelText(/控制令牌|Control token/)).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/重新打开|reopen/);
    expect(() => setDevControlCredential("agent", "fake-key")).toThrow();
  });
  it("uses a custom descriptor endpoint and keeps it isolated across role switches", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response("{}", { status: 200 }))));
    const { container, rerender } = render(<ControlCredentialGate key="agent" role="agent" desktop={false} />);
    fireEvent.change(screen.getByLabelText(/控制端点|Control endpoint/), { target: { value: "http://[::1]:15881" } });
    fireEvent.change(screen.getByLabelText(/控制令牌|Control token/), { target: { value: "fake-agent-custom-key" } });
    fireEvent.submit(container.querySelector("form")!);
    await waitFor(() => expect(hasControlCredential("agent")).toBe(true));
    rerender(<ControlCredentialGate key="hub" role="hub" desktop={false} />);
    expect(screen.getByLabelText(/控制端点|Control endpoint/)).toHaveValue("http://127.0.0.1:5691");
    fireEvent.change(screen.getByLabelText(/控制端点|Control endpoint/), { target: { value: "http://127.0.0.1:15991" } });
    fireEvent.change(screen.getByLabelText(/控制令牌|Control token/), { target: { value: "fake-hub-custom-key" } });
    fireEvent.submit(container.querySelector("form")!);
    await waitFor(() => expect(hasControlCredential("hub")).toBe(true));
    await api.agentStatus(); await api.hubStatus();
    expect(vi.mocked(fetch).mock.calls.map(c => c[0])).toEqual(["http://[::1]:15881/control/status", "http://127.0.0.1:15991/status", "http://[::1]:15881/control/status", "http://127.0.0.1:15991/status"]);
    expect(vi.mocked(fetch).mock.calls.map(c => c[1]?.headers)).toEqual([{ Authorization: "Bearer fake-agent-custom-key" }, { Authorization: "Bearer fake-hub-custom-key" }, { Authorization: "Bearer fake-agent-custom-key" }, { Authorization: "Bearer fake-hub-custom-key" }]);
    clearControlCredentials("agent");
    rerender(<ControlCredentialGate key="agent" role="agent" desktop={false} />);
    expect(screen.getByLabelText(/控制端点|Control endpoint/)).toHaveValue("http://[::1]:15881");
    expect(screen.getByLabelText(/控制令牌|Control token/)).toHaveValue("");
  });
  it.each(["http://evil.example:15881", "http://10.0.0.1:15881", "http://127.0.0.1:15881/path", "http://u:p@127.0.0.1:15881", "http://127.1:15881", "http://127.0.0.1:15881?x=1"])("does not send a token to invalid endpoint %s", async endpoint => {
    vi.stubGlobal("fetch", vi.fn());
    const { container } = render(<ControlCredentialGate role="agent" desktop={false} />);
    fireEvent.change(screen.getByLabelText(/控制端点|Control endpoint/), { target: { value: endpoint } });
    fireEvent.change(screen.getByLabelText(/控制令牌|Control token/), { target: { value: "fake-invalid-base-key" } });
    fireEvent.submit(container.querySelector("form")!);
    await screen.findByRole("alert"); expect(fetch).not.toHaveBeenCalled(); expect(hasControlCredential("agent")).toBe(false);
  });

});
