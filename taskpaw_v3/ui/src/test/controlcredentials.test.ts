import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, clearControlCredentials, setDevControlCredential, hasControlCredential } from "../api";

beforeEach(() => {
  clearControlCredentials(); delete window.__TASKPAW__;
  vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response("{}", { status: 200 }))));
});
describe("local control credentials", () => {
  it("does not send anonymous mutations or accept legacy read keys", async () => {
    window.__TASKPAW__ = { apiKey: "legacy-read-key" } as never;
    await expect(api.stopMonitor("task")).rejects.toThrow(); expect(fetch).not.toHaveBeenCalled();
  });
  it("keeps credentials and endpoints separate by role, including film reads", async () => {
    setDevControlCredential("agent", "agent-fake-key"); setDevControlCredential("hub", "hub-fake-key");
    await api.agentStatus(); await api.hubStatus(); await api.hubFilms(1, "task");
    const calls = vi.mocked(fetch).mock.calls;
    expect(calls.map(c => c[0])).toEqual(["http://127.0.0.1:5681/control/status", "http://127.0.0.1:5691/status", "http://127.0.0.1:5691/servers/1/monitors/films?name=task&size=10"]);
    expect(calls.map(c => c[1]?.headers)).toEqual([{ Authorization: "Bearer agent-fake-key" }, { Authorization: "Bearer hub-fake-key" }, { Authorization: "Bearer hub-fake-key" }]);
  });
  it("clears only the expired role and never replays a mutation", async () => {
    setDevControlCredential("agent", "agent-fake-key"); setDevControlCredential("hub", "hub-fake-key");
    vi.mocked(fetch).mockResolvedValueOnce(new Response('{"detail":"control_unauthorized"}', { status: 401 }));
    await expect(api.stopMonitor("task")).rejects.toThrow(); expect(hasControlCredential("agent")).toBe(false); expect(hasControlCredential("hub")).toBe(true);
    await expect(api.agentStatus()).rejects.toThrow(); expect(fetch).toHaveBeenCalledTimes(1);
  });
  it.each(["http://evil.example:5681", "http://127.0.0.1:5681/path", "http://u:p@127.0.0.1:5681", "http://127.1:5681", "http://localhost:5681", "http://127.0.0.1:5681?x=1"])("refuses malformed or noncanonical endpoint %s before sending a key", async baseUrl => {
    window.__TASKPAW__ = { role: "agent", baseUrl, controlToken: "fake-key", bootId: "0123456789abcdef0123456789abcdef" };
    await expect(api.agentStatus()).rejects.toThrow(); expect(fetch).not.toHaveBeenCalled();
  });
});
