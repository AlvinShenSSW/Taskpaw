import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Settings } from "../views/Settings";
import { theme } from "../theme";
import * as apiModule from "../api";
import "../i18n";

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
    const { container } = render(
      <ThemeProvider theme={theme}>
        <QueryClientProvider client={qc}>
          <Settings role="agent" />
        </QueryClientProvider>
      </ThemeProvider>,
    );

    // Wait for the form to seed from the mocked config, then set a token + save.
    await waitFor(() =>
      expect(container.querySelector('input[type="password"]')).not.toBeNull(),
    );
    const token = container.querySelector('input[type="password"]') as HTMLInputElement;
    fireEvent.change(token, { target: { value: "new-token" } });
    fireEvent.click(screen.getByRole("button", { name: /Save|保存/ }));

    await waitFor(() => expect(updateSpy).toHaveBeenCalled());
    await waitFor(() =>
      expect(invalidateSpy).toHaveBeenCalledWith({ queryKey: ["agentConfig"] }),
    );
  });
});
