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

  const BASE = /^(API base URL|API 地址)$/;
  const MODEL = /^(Model|模型)$/;
  const KEY = /^(API key|API 密钥)$/;
  const SAVE = /Save LLM settings|保存 LLM 设置/;
  const CLEAR = /Clear key|清除密钥/;
  const TEST = /Test connection|测试连接/;

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
    await waitFor(() =>
      expect(testSpy).toHaveBeenCalledWith({
        llm_api_base: "http://127.0.0.1:11434/v1", llm_model: "x-ai/grok-4.1-fast",
      }),
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
      }),
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
