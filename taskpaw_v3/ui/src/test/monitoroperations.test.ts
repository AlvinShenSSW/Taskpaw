import { afterEach, describe, expect, it, vi } from "vitest";
import { api, hasControlCredential } from "../api";

const partial = {
  ok: false, name: "owned", operation: "stop", outcome: "applied_not_persisted",
  persistence: "failed", runtime: "stopped", retryable: true, error_code: "persistence_failed",
};
afterEach(() => vi.unstubAllGlobals());

describe("monitor operation envelopes", () => {
  it.each([200, 409])("decodes a typed partial before generic HTTP %s handling", async status => {
    const body = status === 409 ? { ...partial, outcome: "busy", error_code: "operation_busy", retryable: false } : partial;
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(body), { status })));
    await expect(api.stopMonitor("owned")).rejects.toMatchObject({ result: body });
  });
  it("keeps successful legacy responses and accepted envelopes", async () => {
    const body = { ...partial, ok: true, outcome: "applied", persistence: "saved", error_code: null };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(body))));
    await expect(api.stopMonitor("owned")).resolves.toEqual(body);
  });
  it("401 clears credentials before decoding a partial", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(partial), { status: 401 })));
    await expect(api.stopMonitor("owned")).rejects.toThrow("credentials");
    expect(hasControlCredential("agent")).toBe(false);
  });
});
