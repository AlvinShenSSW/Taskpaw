import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Settings } from "../views/Settings";
import { theme } from "../theme";
import * as apiModule from "../api";
import "../i18n";

// The card (MUI Card root) whose heading matches `title` — the page has several
// cards and two Save buttons (agent config + LLM API, #178), so queries are scoped.
const card = (title: RegExp) =>
  screen.getByText(title).closest(".MuiCard-root") as HTMLElement;
const AGENT_CARD = /^(Agent configuration|Agent 配置)$/;
const LLM_CARD = /^LLM API$/;

// Field / action labels shared by the primary and both fallback LLM cards (#178/#190).
const BASE = /^(API base URL|API 地址)$/;
const MODEL = /^(Model|模型)$/;
const KEY = /^(API key|API 密钥)$/;
const SAVE = /Save LLM settings|保存 LLM 设置/;
const CLEAR = /Clear key|清除密钥/;
const TEST = /Test connection|测试连接/;

// #145: after the operator sets a token, the shared ["agentConfig"] query must be
// invalidated so the auth-disabled banner (and this form) refresh immediately,
// instead of showing a stale cached /control/config (Codex 外门).
describe("Settings config save", () => {
  it("invalidates the agentConfig query on save", async () => {
    vi.spyOn(apiModule.api, "config").mockResolvedValue({
      monitors: [], machine: "m", bind_host: "127.0.0.1", bind_port: 5680,
      control_host: "127.0.0.1", control_port: 5699, auth_disabled: true,
    } as never);
    const updateSpy = vi
      .spyOn(apiModule.api, "updateConfig")
      .mockResolvedValue({ ok: true, restart_required: false } as never);

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    render(
      <ThemeProvider theme={theme}>
        <QueryClientProvider client={qc}>
          <Settings role="agent" />
        </QueryClientProvider>
      </ThemeProvider>,
    );

    // Wait for the form to seed from the mocked config, then set a token + save.
    // T-U0 (#178): scoped to the agent-config card — the LLM card has its own
    // password field and Save button.
    await waitFor(() =>
      expect(card(AGENT_CARD).querySelector('input[type="password"]')).not.toBeNull(),
    );
    const agentCard = card(AGENT_CARD);
    const token = agentCard.querySelector('input[type="password"]') as HTMLInputElement;
    fireEvent.change(token, { target: { value: "new-token" } });
    fireEvent.click(within(agentCard).getByRole("button", { name: /Save|保存/ }));

    await waitFor(() => expect(updateSpy).toHaveBeenCalled());
    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agentConfig"] }),
    );
  });

  it("shows the app version from the single injected source (#170)", async () => {
    vi.spyOn(apiModule.api, "config").mockResolvedValue({
      monitors: [], machine: "m", bind_host: "127.0.0.1", bind_port: 5680,
      control_host: "127.0.0.1", control_port: 5699, auth_disabled: true,
    } as never);
    render(
      <ThemeProvider theme={theme}>
        <QueryClientProvider client={new QueryClient()}>
          <Settings role="agent" />
        </QueryClientProvider>
      </ThemeProvider>,
    );
    // Asserted against __APP_VERSION__ itself (not a literal), so it can never drift
    // from the packaged version — the whole point of the fix.
    expect(await screen.findByText(`v${__APP_VERSION__}`)).toBeTruthy();
  });
});

// #178: the agent-level LLM API card (base URL, model, key; save / clear / test).
describe("Settings LLM API card", () => {
  afterEach(() => vi.restoreAllMocks());

  function mockConfig(source: "env" | "config" | "none") {
    vi.spyOn(apiModule.api, "config").mockResolvedValue({
      monitors: [], machine: "m", bind_host: "127.0.0.1", bind_port: 5680,
      control_host: "127.0.0.1", control_port: 5699, auth_disabled: true,
      llm_api_base: "https://openrouter.ai/api/v1", llm_model: "x-ai/grok-4.1-fast",
      llm_api_key: source === "none" ? "" : "***", llm_api_key_source: source,
    } as never);
  }

  async function renderLlm() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    render(
      <ThemeProvider theme={theme}>
        <QueryClientProvider client={qc}>
          <Settings role="agent" />
        </QueryClientProvider>
      </ThemeProvider>,
    );
    await waitFor(() => expect(within(card(LLM_CARD)).getByLabelText(MODEL)).toBeInTheDocument());
    return { llm: within(card(LLM_CARD)), invalidateSpy };
  }

  it("T-U1 seeds base/model and renders the key as a masked password field", async () => {
    mockConfig("config");
    const { llm } = await renderLlm();
    expect(llm.getByLabelText(BASE)).toHaveValue("https://openrouter.ai/api/v1");
    expect(llm.getByLabelText(MODEL)).toHaveValue("x-ai/grok-4.1-fast");
    const key = llm.getByLabelText(KEY) as HTMLInputElement;
    expect(key.type).toBe("password");
    expect(key).toHaveValue("");
    expect(key).toHaveAttribute("placeholder", "***");
    expect(key).not.toBeDisabled();
    expect(llm.getByRole("button", { name: CLEAR })).toBeInTheDocument();
  });

  it("T-U1 disables the key field with the env hint and hides Clear when the key comes from env", async () => {
    mockConfig("env");
    const { llm } = await renderLlm();
    const key = llm.getByLabelText(KEY) as HTMLInputElement;
    expect(key.type).toBe("password");
    expect(key).toBeDisabled();
    expect(llm.getByText(/TASKPAW_LLM_API_KEY/)).toBeInTheDocument();
    expect(llm.queryByRole("button", { name: CLEAR })).toBeNull();
  });

  it("T-U2 Save LLM settings patches the three fields (blank key omitted) and invalidates agentConfig", async () => {
    mockConfig("config");
    const updateSpy = vi
      .spyOn(apiModule.api, "updateConfig")
      .mockResolvedValue({ ok: true, restart_required: false } as never);
    const { llm, invalidateSpy } = await renderLlm();

    fireEvent.change(llm.getByLabelText(MODEL), { target: { value: "openai/gpt-5-mini" } });
    fireEvent.click(llm.getByRole("button", { name: SAVE }));
    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(1));
    expect(updateSpy).toHaveBeenLastCalledWith({
      llm_api_base: "https://openrouter.ai/api/v1", llm_model: "openai/gpt-5-mini",
    });
    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agentConfig"] }),
    );
    expect(await llm.findByText(/LLM settings saved|LLM 设置已保存/)).toBeInTheDocument();

    fireEvent.change(llm.getByLabelText(KEY), { target: { value: "sk-new" } });
    fireEvent.click(llm.getByRole("button", { name: SAVE }));
    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(2));
    expect(updateSpy).toHaveBeenLastCalledWith({
      llm_api_base: "https://openrouter.ai/api/v1", llm_model: "openai/gpt-5-mini",
      llm_api_key: "sk-new",
    });
    // The typed key is not kept in the field after a successful save.
    await waitFor(() => expect(llm.getByLabelText(KEY)).toHaveValue(""));
  });

  it("T-U2 Clear key patches llm_api_key: null", async () => {
    mockConfig("config");
    const updateSpy = vi
      .spyOn(apiModule.api, "updateConfig")
      .mockResolvedValue({ ok: true, restart_required: false } as never);
    const { llm, invalidateSpy } = await renderLlm();

    fireEvent.click(llm.getByRole("button", { name: CLEAR }));
    await waitFor(() => expect(updateSpy).toHaveBeenCalledWith({ llm_api_key: null }));
    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agentConfig"] }),
    );
    expect(await llm.findByText(/Stored API key cleared|已清除保存的 API 密钥/)).toBeInTheDocument();
  });

  it("T-U3 Test connection sends the current form values (blank key omitted) and shows model + latency", async () => {
    mockConfig("config");
    const testSpy = vi
      .spyOn(apiModule.api, "llmTest")
      .mockResolvedValue({ ok: true, model: "x-ai/grok-4.1-fast", latency_ms: 812, truncated: false });
    const { llm } = await renderLlm();

    fireEvent.change(llm.getByLabelText(BASE), { target: { value: "http://127.0.0.1:11434/v1" } });
    fireEvent.click(llm.getByRole("button", { name: TEST }));
    // #190: the primary card tests the "primary" slot.
    await waitFor(() =>
      expect(testSpy).toHaveBeenCalledWith({
        llm_api_base: "http://127.0.0.1:11434/v1", llm_model: "x-ai/grok-4.1-fast",
      }, "primary"),
    );
    const alert = await llm.findByRole("alert");
    expect(alert).toHaveTextContent("x-ai/grok-4.1-fast");
    expect(alert).toHaveTextContent("812");
    expect(alert).not.toHaveTextContent(/truncated|截断/);
  });

  it("T-U3 Test connection includes a typed key and notes a truncated reply", async () => {
    mockConfig("config");
    const testSpy = vi
      .spyOn(apiModule.api, "llmTest")
      .mockResolvedValue({ ok: true, model: "m1", latency_ms: 40, truncated: true });
    const { llm } = await renderLlm();

    fireEvent.change(llm.getByLabelText(KEY), { target: { value: "sk-try" } });
    fireEvent.click(llm.getByRole("button", { name: TEST }));
    await waitFor(() =>
      expect(testSpy).toHaveBeenCalledWith({
        llm_api_base: "https://openrouter.ai/api/v1", llm_model: "x-ai/grok-4.1-fast",
        llm_api_key: "sk-try",
      }, "primary"),
    );
    const alert = await llm.findByRole("alert");
    expect(alert).toHaveTextContent("m1");
    expect(alert).toHaveTextContent(/truncated|截断/);
  });

  it("T-U3 Test connection shows the backend error string", async () => {
    mockConfig("config");
    vi.spyOn(apiModule.api, "llmTest")
      .mockResolvedValue({ ok: false, error: "auth: authentication failed (HTTP 401)" });
    const { llm } = await renderLlm();

    fireEvent.click(llm.getByRole("button", { name: TEST }));
    const alert = await llm.findByRole("alert");
    expect(alert).toHaveTextContent("auth: authentication failed (HTTP 401)");
  });
});

// #190 / #192 (AC11): two fallback LLM cards under the primary — the same fields,
// write-only key and Save / Clear / Test, each scoped to its own slot — plus the
// failover switch (llm_failover, default on).
describe("Settings fallback models + failover (#190/#192)", () => {
  afterEach(() => vi.restoreAllMocks());

  const FB1_CARD = /^(Fallback model 1|备用模型 1)$/;
  const FB2_CARD = /^(Fallback model 2|备用模型 2)$/;
  const FAILOVER = /^(Use the fallback models while the primary is unavailable|主模型不可用时改用备用模型)$/;

  type Src = "env" | "config" | "none";
  function mockConfig(opts: { fb1?: Src; fb2?: Src; failover?: boolean } = {}) {
    const fb1 = opts.fb1 ?? "config";
    const fb2 = opts.fb2 ?? "none";
    const cfg: Record<string, unknown> = {
      monitors: [], machine: "m", bind_host: "127.0.0.1", bind_port: 5680,
      control_host: "127.0.0.1", control_port: 5699, auth_disabled: true,
      llm_api_base: "https://api.x.ai/v1", llm_model: "grok-4.3",
      llm_api_key: "***", llm_api_key_source: "config",
      llm_fallback1_api_base: "https://fb1.example/v1", llm_fallback1_model: "fb1-chat",
      llm_fallback1_api_key: fb1 === "none" ? "" : "***", llm_fallback1_api_key_source: fb1,
      llm_fallback2_api_base: "", llm_fallback2_model: "",
      llm_fallback2_api_key: fb2 === "none" ? "" : "***", llm_fallback2_api_key_source: fb2,
    };
    if (opts.failover !== undefined) cfg.llm_failover = opts.failover;
    vi.spyOn(apiModule.api, "config").mockResolvedValue(cfg as never);
  }

  async function renderSettings() {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    render(
      <ThemeProvider theme={theme}>
        <QueryClientProvider client={qc}>
          <Settings role="agent" />
        </QueryClientProvider>
      </ThemeProvider>,
    );
    await waitFor(() => expect(within(card(FB2_CARD)).getByLabelText(MODEL)).toBeInTheDocument());
    return { fb1: within(card(FB1_CARD)), fb2: within(card(FB2_CARD)), invalidateSpy };
  }

  it("renders the primary + both fallback cards, each with base / model / masked key and its actions", async () => {
    mockConfig();
    const { fb1, fb2 } = await renderSettings();
    for (const title of [LLM_CARD, FB1_CARD, FB2_CARD]) {
      const c = within(card(title));
      expect(c.getByLabelText(BASE)).toBeInTheDocument();
      expect(c.getByLabelText(MODEL)).toBeInTheDocument();
      expect((c.getByLabelText(KEY) as HTMLInputElement).type).toBe("password");
      expect(c.getByRole("button", { name: SAVE })).toBeInTheDocument();
      expect(c.getByRole("button", { name: TEST })).toBeInTheDocument();
    }
    // Fallback 1 is configured (stored key → "***" placeholder, Clear offered).
    expect(fb1.getByLabelText(BASE)).toHaveValue("https://fb1.example/v1");
    expect(fb1.getByLabelText(MODEL)).toHaveValue("fb1-chat");
    expect(fb1.getByLabelText(KEY)).toHaveValue("");
    expect(fb1.getByLabelText(KEY)).toHaveAttribute("placeholder", "***");
    expect(fb1.getByLabelText(KEY)).not.toBeDisabled();
    expect(fb1.getByRole("button", { name: CLEAR })).toBeInTheDocument();
    // Fallback 2 is unset: empty fields — no provider is pre-filled as a default.
    expect(fb2.getByLabelText(BASE)).toHaveValue("");
    expect(fb2.getByLabelText(MODEL)).toHaveValue("");
    expect(fb2.getByLabelText(KEY)).not.toHaveAttribute("placeholder", "***");
    // The fallback hint says when a fallback is used (refused lines; failover).
    expect(fb1.getByText(/refuses|拒绝/)).toBeInTheDocument();
    // No stored key value is ever echoed into a field (write-only keys).
    const secrets = Array.from(document.querySelectorAll<HTMLInputElement>('input[type="password"]'));
    expect(secrets.length).toBeGreaterThanOrEqual(4); // api_token + three LLM keys
    for (const input of secrets) expect(input.value).toBe("");
  });

  it("an env-provided fallback key disables the field, names its env var and hides Clear", async () => {
    mockConfig({ fb1: "config", fb2: "env" });
    const { fb1, fb2 } = await renderSettings();
    expect(fb2.getByLabelText(KEY)).toBeDisabled();
    expect(fb2.getByLabelText(KEY)).toHaveAttribute("placeholder", "***");
    expect(fb2.getByText(/TASKPAW_LLM_FALLBACK2_API_KEY/)).toBeInTheDocument();
    expect(fb2.queryByRole("button", { name: CLEAR })).toBeNull();
    // Fallback 1's key comes from the config file: editable, clearable.
    expect(fb1.getByLabelText(KEY)).not.toBeDisabled();
    expect(fb1.getByText(/TASKPAW_LLM_FALLBACK1_API_KEY/)).toBeInTheDocument();
    expect(fb1.getByRole("button", { name: CLEAR })).toBeInTheDocument();
  });

  it("Save in a fallback card sends only that slot's fields (blank key kept, typed key sent)", async () => {
    mockConfig();
    const updateSpy = vi
      .spyOn(apiModule.api, "updateConfig")
      .mockResolvedValue({ ok: true, restart_required: false } as never);
    const { fb1, invalidateSpy } = await renderSettings();

    fireEvent.change(fb1.getByLabelText(MODEL), { target: { value: "fb1-reasoner" } });
    fireEvent.click(fb1.getByRole("button", { name: SAVE }));
    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(1));
    expect(updateSpy).toHaveBeenLastCalledWith({
      llm_fallback1_api_base: "https://fb1.example/v1", llm_fallback1_model: "fb1-reasoner",
    });
    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agentConfig"] }),
    );
    expect(await fb1.findByText(/LLM settings saved|LLM 设置已保存/)).toBeInTheDocument();

    fireEvent.change(fb1.getByLabelText(KEY), { target: { value: "sk-fb1" } });
    fireEvent.click(fb1.getByRole("button", { name: SAVE }));
    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(2));
    expect(updateSpy).toHaveBeenLastCalledWith({
      llm_fallback1_api_base: "https://fb1.example/v1", llm_fallback1_model: "fb1-reasoner",
      llm_fallback1_api_key: "sk-fb1",
    });
    // The typed key is not kept in the field after a successful save.
    await waitFor(() => expect(fb1.getByLabelText(KEY)).toHaveValue(""));
  });

  it("Clear in a fallback card sends only that slot's key: null", async () => {
    mockConfig({ fb1: "config", fb2: "config" });
    const updateSpy = vi
      .spyOn(apiModule.api, "updateConfig")
      .mockResolvedValue({ ok: true, restart_required: false } as never);
    const { fb2 } = await renderSettings();

    fireEvent.click(fb2.getByRole("button", { name: CLEAR }));
    await waitFor(() => expect(updateSpy).toHaveBeenCalledTimes(1));
    expect(updateSpy).toHaveBeenLastCalledWith({ llm_fallback2_api_key: null });
    expect(await fb2.findByText(/Stored API key cleared|已清除保存的 API 密钥/)).toBeInTheDocument();
  });

  it("Test in a fallback card sends that slot's candidate fields + its slot", async () => {
    mockConfig();
    const testSpy = vi
      .spyOn(apiModule.api, "llmTest")
      .mockResolvedValue({ ok: true, model: "fb2-chat", latency_ms: 321, truncated: false });
    const { fb2 } = await renderSettings();

    fireEvent.change(fb2.getByLabelText(BASE), { target: { value: "https://fb2.example/v1" } });
    fireEvent.change(fb2.getByLabelText(MODEL), { target: { value: "fb2-chat" } });
    fireEvent.change(fb2.getByLabelText(KEY), { target: { value: "sk-fb2" } });
    fireEvent.click(fb2.getByRole("button", { name: TEST }));
    await waitFor(() =>
      expect(testSpy).toHaveBeenCalledWith({
        llm_fallback2_api_base: "https://fb2.example/v1", llm_fallback2_model: "fb2-chat",
        llm_fallback2_api_key: "sk-fb2",
      }, "fallback2"),
    );
    const alert = await fb2.findByRole("alert");
    expect(alert).toHaveTextContent("fb2-chat");
    expect(alert).toHaveTextContent("321");
  });

  it("the failover switch is on by default and saves llm_failover on each toggle", async () => {
    mockConfig(); // no llm_failover in the response → default on
    const updateSpy = vi
      .spyOn(apiModule.api, "updateConfig")
      .mockResolvedValue({ ok: true, restart_required: false } as never);
    const { invalidateSpy } = await renderSettings();

    const sw = await screen.findByLabelText(FAILOVER);
    expect(sw).toBeChecked();
    fireEvent.click(sw);
    await waitFor(() => expect(updateSpy).toHaveBeenLastCalledWith({ llm_failover: false }));
    await waitFor(() => expect(screen.getByLabelText(FAILOVER)).not.toBeChecked());
    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agentConfig"] }),
    );

    await waitFor(() => expect(screen.getByLabelText(FAILOVER)).not.toBeDisabled());
    fireEvent.click(screen.getByLabelText(FAILOVER));
    await waitFor(() => expect(updateSpy).toHaveBeenLastCalledWith({ llm_failover: true }));
    await waitFor(() => expect(screen.getByLabelText(FAILOVER)).toBeChecked());
    expect(updateSpy).toHaveBeenCalledTimes(2);
  });

  it("the failover switch shows a stored off and reverts when the save fails", async () => {
    mockConfig({ failover: false });
    const updateSpy = vi
      .spyOn(apiModule.api, "updateConfig")
      .mockRejectedValue(new Error("config write failed"));
    await renderSettings();

    const sw = await screen.findByLabelText(FAILOVER);
    await waitFor(() => expect(sw).not.toBeChecked());
    fireEvent.click(sw);
    await waitFor(() => expect(updateSpy).toHaveBeenCalledWith({ llm_failover: true }));
    expect(await screen.findByText("config write failed")).toBeInTheDocument();
    expect(screen.getByLabelText(FAILOVER)).not.toBeChecked();
  });
});

// #190 (AC11): api.llmTest posts the slot's candidate fields plus `slot`.
describe("api.llmTest", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("POSTs the candidate fields with the slot to /control/llm-test", async () => {
    const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<unknown>>(() =>
      Promise.resolve({ ok: true, json: () => Promise.resolve({ ok: true, model: "m" }) }),
    );
    vi.stubGlobal("fetch", fetchMock);
    const r = await apiModule.api.llmTest({ llm_fallback1_model: "m" }, "fallback1");
    expect(r.ok).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toMatch(/\/control\/llm-test$/);
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ llm_fallback1_model: "m", slot: "fallback1" });
  });
});
