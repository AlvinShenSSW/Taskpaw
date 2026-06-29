import { describe, expect, it, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { MonitorWizard } from "../views/MonitorWizard";
import { theme } from "../theme";
import * as apiModule from "../api";
import "../i18n";

const ladaPlugin: apiModule.PluginInfo = {
  type_id: "lada",
  display_name: "Lada",
  category: "media",
  config_version: 1,
  system: false,
  json_schema: {
    type: "object",
    required: ["name"],
    properties: {
      name: { type: "string", title: "Monitor name" },
      api_url: { type: "string", title: "Lada API URL" },
    },
  },
  ui_schema: {},
};

const hostMetrics: apiModule.PluginInfo = {
  ...ladaPlugin, type_id: "host_metrics", display_name: "Host metrics", system: true,
};

const moomoo: apiModule.PresetInfo = {
  id: "moomoo",
  display_name: "moomoo (MQT life-signs)",
  description: "pm2 daemon, orchestrator, OpenD, heartbeat",
  monitors: [
    { type_id: "process", name: "pm2", config: { name: "pm2" } },
    { type_id: "process", name: "orchestrator", config: { name: "orchestrator" } },
    { type_id: "tcp_check", name: "opend", config: { name: "opend" } },
    { type_id: "heartbeat", name: "hb", config: { name: "hb" } },
  ],
};

const wrap = (ui: React.ReactNode) =>
  render(<ThemeProvider theme={theme}>{ui}</ThemeProvider>);

const baseProps = {
  plugins: [ladaPlugin, hostMetrics],
  presets: [moomoo],
  onClose: vi.fn(),
  onDone: vi.fn(),
  onError: vi.fn(),
};

describe("MonitorWizard", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("step 1 lists selectable plugins + presets, hides system plugins", () => {
    wrap(<MonitorWizard mode="add" {...baseProps} />);
    expect(screen.getByText("Lada")).toBeInTheDocument();
    expect(screen.getByText("moomoo (MQT life-signs)")).toBeInTheDocument();
    // host_metrics is system → not offered.
    expect(screen.queryByText("Host metrics")).not.toBeInTheDocument();
  });

  it("Continue is disabled until a service is chosen", () => {
    wrap(<MonitorWizard mode="add" {...baseProps} />);
    const cont = screen.getByRole("button", { name: /Continue|继续/ });
    expect(cont).toBeDisabled();
    fireEvent.click(screen.getByText("Lada"));
    expect(cont).toBeEnabled();
  });

  it("Lada flow: choose → configure → review → addMonitor + auto-select", async () => {
    const addMonitor = vi.spyOn(apiModule.api, "addMonitor").mockResolvedValue({} as never);
    const onDone = vi.fn();
    wrap(<MonitorWizard mode="add" {...baseProps} onDone={onDone} />);

    fireEvent.click(screen.getByText("Lada"));
    fireEvent.click(screen.getByRole("button", { name: /Continue|继续/ }));

    // Step 2: fill the required name, submit the form (its button = "Review").
    fireEvent.change(screen.getByLabelText(/Monitor name/), { target: { value: "lada-1" } });
    fireEvent.click(screen.getByRole("button", { name: /Review|复核/ }));

    // Step 3: review shows the entered name; Add monitor submits.
    await screen.findByText("lada-1");
    fireEvent.click(screen.getByRole("button", { name: /Add monitor|添加监控/ }));

    await waitFor(() =>
      expect(addMonitor).toHaveBeenCalledWith({ type_id: "lada", config: expect.objectContaining({ name: "lada-1" }) }),
    );
    await waitFor(() => expect(onDone).toHaveBeenCalledWith("lada-1"));
  });

  it("preset flow: creates every bundled monitor (4 addMonitor calls)", async () => {
    const addMonitor = vi.spyOn(apiModule.api, "addMonitor").mockResolvedValue({} as never);
    wrap(<MonitorWizard mode="add" {...baseProps} />);

    fireEvent.click(screen.getByText("moomoo (MQT life-signs)"));
    fireEvent.click(screen.getByRole("button", { name: /Continue|继续/ }));
    // Preset step 2 → Review → Add.
    fireEvent.click(screen.getByRole("button", { name: /Review|复核/ }));
    fireEvent.click(screen.getByRole("button", { name: /Add monitor|添加监控/ }));

    await waitFor(() => expect(addMonitor).toHaveBeenCalledTimes(4));
  });

  it("edit mode opens on the config step with the type locked + name readonly", () => {
    wrap(
      <MonitorWizard
        mode="edit"
        name="lada-1"
        existingType="lada"
        existingConfig={{ name: "lada-1", api_url: "http://x" }}
        {...baseProps}
      />,
    );
    // No step-1 service grid (jumped to config).
    expect(screen.queryByText("moomoo (MQT life-signs)")).not.toBeInTheDocument();
    // The name field is prefilled and locked (RJSF/mui renders ui:readonly as a
    // disabled input).
    const nameInput = screen.getByLabelText(/Monitor name/) as HTMLInputElement;
    expect(nameInput.value).toBe("lada-1");
    expect(nameInput).toBeDisabled();
  });

  it("surfaces a backend error on a failed add (not silent)", async () => {
    vi.spyOn(apiModule.api, "addMonitor").mockRejectedValue(new Error("a monitor named 'lada-1' already exists"));
    wrap(<MonitorWizard mode="add" {...baseProps} />);
    fireEvent.click(screen.getByText("Lada"));
    fireEvent.click(screen.getByRole("button", { name: /Continue|继续/ }));
    fireEvent.change(screen.getByLabelText(/Monitor name/), { target: { value: "lada-1" } });
    fireEvent.click(screen.getByRole("button", { name: /Review|复核/ }));
    await screen.findByText("lada-1");
    fireEvent.click(screen.getByRole("button", { name: /Add monitor|添加监控/ }));
    expect(await screen.findByText(/already exists/)).toBeInTheDocument();
  });
});
