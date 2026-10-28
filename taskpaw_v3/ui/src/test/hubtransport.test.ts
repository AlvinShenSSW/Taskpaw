import { afterEach, describe, expect, it, vi } from "vitest";
import { api, hasControlCredential } from "../api";

const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); vi.useRealTimers(); });

describe("Hub bounded local transport", () => {
  it("refuses redirects on Hub reads, writes, film reads and images without changing Agent requests", async () => {
    // Node Response and jsdom FileReader otherwise use different Blob realms.
    vi.spyOn(Response.prototype, "blob").mockImplementation(async function (this: Response) {
      return new Blob([await this.arrayBuffer()], { type: this.headers.get("Content-Type") ?? "" });
    });
    const fetcher = vi.fn(() => Promise.resolve(json({ ok: true })));
    vi.stubGlobal("fetch", fetcher);
    await api.hubStatus(); await api.hubSaveSettings({ poll_interval: 30 }); await api.hubFilms(1, "fixture");
    await api.image("/servers/1/workflow/films/fixture/poster", "hub");
    await api.agentStatus();
    for (const call of fetcher.mock.calls.slice(0, 4) as unknown as [string, RequestInit][]) {
      expect(call[1].redirect).toBe("error");
      expect(call[1].signal).toBeInstanceOf(AbortSignal);
      expect(call[1].headers).toMatchObject({ Authorization: "Bearer test-hub-control" });
    }
    expect((fetcher.mock.calls[4] as unknown as [string, RequestInit])[1].redirect).toBeUndefined();
  });

  it("ends a hanging header request after 20 seconds and clears its timer", async () => {
    vi.useFakeTimers();
    const fetcher = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(() => new Promise<Response>(() => {}));
    vi.stubGlobal("fetch", fetcher);
    const result = api.hubStatus().catch(error => error);
    await vi.advanceTimersByTimeAsync(20_000);
    expect(await result).toHaveProperty("message", "Hub request timed out");
    expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("includes slow body reads in the same deadline and cancels the stream", async () => {
    vi.useFakeTimers();
    const cancel = vi.fn();
    const body = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new TextEncoder().encode('{"slow":')); }, cancel });
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(body))));
    const result = api.hubStatus().catch(error => error);
    await vi.advanceTimersByTimeAsync(20_000);
    expect(await result).toHaveProperty("message", "Hub request timed out");
    expect(cancel).toHaveBeenCalledOnce();
    expect(vi.getTimerCount()).toBe(0);
  });

  it("rejects oversized declared or streamed bodies and closes their readers", async () => {
    const cancel = vi.fn();
    const large = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new Uint8Array(8 * 1024 * 1024 + 1)); }, cancel });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response(large))
      .mockResolvedValueOnce(new Response("{}", { headers: { "Content-Length": String(8 * 1024 * 1024 + 1) } })));
    await expect(api.hubStatus()).rejects.toThrow("Hub response is too large");
    expect(cancel).toHaveBeenCalledOnce();
    await expect(api.hubStatus()).rejects.toThrow("Hub response is too large");
  });

  it("revokes rejected credentials without waiting for an untrusted 401 body", async () => {
    const cancel = vi.fn();
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(new ReadableStream({ cancel }), { status: 401 }))));
    await expect(api.hubStatus()).rejects.toThrow("credentials");
    expect(hasControlCredential("hub")).toBe(false);
    expect(hasControlCredential("agent")).toBe(true);
    expect(cancel).toHaveBeenCalledOnce();
  });

  it("retains validation errors and body-less success semantics", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ detail: "Invalid setting", fields: { poll_interval: "unsupported" } }), { status: 422 }))
      .mockResolvedValueOnce(new Response(null, { status: 204 })));
    await expect(api.hubSaveSettings({ poll_interval: 1 })).rejects.toMatchObject({ status: 422, fields: { poll_interval: "unsupported" } });
    await expect(api.hubRemoveServer(1)).resolves.toBeUndefined();
  });
});


it("cancels an unmounted Hub status query without retaining a deadline timer", async () => {
  vi.useFakeTimers();
  const fetcher = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(() => new Promise<Response>(() => {}));
  vi.stubGlobal("fetch", fetcher);
  const controller = new AbortController();
  const result = api.hubStatus({ signal: controller.signal }).catch(error => error);
  controller.abort();
  await vi.advanceTimersByTimeAsync(20_000);
  expect(await result).toHaveProperty("name", "AbortError");
  expect(fetcher.mock.calls[0][1]?.signal?.aborted).toBe(true);
  expect(vi.getTimerCount()).toBe(0);
});
