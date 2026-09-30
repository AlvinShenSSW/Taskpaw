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

// The #208 8K VR profile exactly as the backend's static ui_schema carries it
// (`vr_8k.ui:options.taskpawProfile`, built from jasna.py's _VR8K_* constants).
const VR8K_PROFILE = {
  unet4x_4k: false, detection_model: "rfdetr-vr-v1", clip_size_4k: 30, temporal_overlap: 8,
};

// A jasna-shaped plugin (#173): the two unet-4x tickboxes, with the backend's
// defaults (1080p on, 4K off) carried in the json_schema; plus the #177「AV 翻译」
// tickbox (default off); plus the #208「8K VR」tickbox and the fields it locks,
// in the real plugin's ui:order, with the backend's profile in its ui:options.
const jasnaPlugin: apiModule.PluginInfo = {
  type_id: "jasna",
  display_name: "Jasna (video restore)",
  category: "task",
  config_version: 1,
  system: false,
  json_schema: {
    type: "object",
    required: ["name"],
    properties: {
      name: { type: "string", title: "Monitor name" },
      unet4x_1080p: { type: "boolean", title: "Unet4X 1080P", default: true },
      unet4x_4k: { type: "boolean", title: "Unet4X 4K", default: false },
      av_translate: { type: "boolean", title: "AV 翻译", default: false },
      vr_8k: {
        type: "boolean", title: "8K VR", default: false,
        description: "Treat every file of this task as 8K SBS VR.",
      },
      clip_size_4k: {
        type: "integer", title: "Clip Size 4K", default: 60, minimum: 8,
        description: "Frames per clip (--max-clip-size) for the 4K tier.",
      },
      temporal_overlap: {
        type: "integer", title: "Temporal Overlap", default: 8, minimum: 0,
        description: "Frames of overlap between clips (--temporal-overlap).",
      },
      detection_model: {
        type: "string", title: "Detection Model", default: "rfdetr-v6",
        description: "Detection model (--detection-model).",
      },
    },
  },
  ui_schema: {
    "ui:order": [
      "name", "jasna_exe_path", "jasna_input_folder", "jasna_output_folder",
      "unet4x_1080p", "unet4x_4k", "av_translate", "whisperjav_exe_path",
      "whisperjav_engine", "whisperjav_extra_args", "vr_8k", "clip_size_1080p",
      "clip_size_4k", "temporal_overlap", "codec", "cq", "detection_model",
      "process_name", "jasna_extra_args", "jasna_gpu_monitor", "jasna_capture_progress",
      "poll_interval", "timeout", "*",
    ],
    vr_8k: { "ui:options": { taskpawProfile: VR8K_PROFILE } },
  },
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
    fireEvent.change(screen.getByLabelText(/名称|Monitor name/), { target: { value: "lada-1" } });
    fireEvent.click(screen.getByRole("button", { name: /Review|复核/ }));

    // Step 3: review shows the entered name; Add monitor submits.
    await screen.findByText("lada-1");
    fireEvent.click(screen.getByRole("button", { name: /Add monitor|添加监控/ }));

    await waitFor(() =>
      expect(addMonitor).toHaveBeenCalledWith({ type_id: "lada", config: expect.objectContaining({ name: "lada-1" }) }),
    );
    await waitFor(() => expect(onDone).toHaveBeenCalledWith("lada-1"));
  });

  it("jasna: the unet-4x tickboxes render with 1080p on and 4K off by default (#173)", () => {
    wrap(<MonitorWizard mode="add" {...baseProps} plugins={[ladaPlugin, jasnaPlugin]} />);
    fireEvent.click(screen.getByText("Jasna (video restore)"));
    fireEvent.click(screen.getByRole("button", { name: /Continue|继续/ }));

    // zh labels come from schemaI18n; the regex also accepts the schema's English
    // title so the test doesn't depend on the UI language.
    const on = screen.getByLabelText(
      /1080p 档：使用 unet-4x 二次修复|Unet4X 1080P/,
    ) as HTMLInputElement;
    const off = screen.getByLabelText(/4K 档：使用 unet-4x 二次修复|Unet4X 4K/) as HTMLInputElement;
    expect(on.checked).toBe(true);
    expect(off.checked).toBe(false);
  });

  it("jasna: the AV 翻译 tickbox renders unticked by default (#177)", () => {
    wrap(<MonitorWizard mode="add" {...baseProps} plugins={[ladaPlugin, jasnaPlugin]} />);
    fireEvent.click(screen.getByText("Jasna (video restore)"));
    fireEvent.click(screen.getByRole("button", { name: /Continue|继续/ }));

    const av = screen.getByLabelText(/AV 翻译|Av Translate/) as HTMLInputElement;
    expect(av.type).toBe("checkbox");
    expect(av.checked).toBe(false);
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
    const nameInput = screen.getByLabelText(/名称|Monitor name/) as HTMLInputElement;
    expect(nameInput.value).toBe("lada-1");
    expect(nameInput).toBeDisabled();
  });

  it("clears captured config when switching to a different service", async () => {
    const comfy: apiModule.PluginInfo = {
      ...ladaPlugin, type_id: "comfyui", display_name: "ComfyUI",
      json_schema: { type: "object", required: ["name"],
        properties: { name: { type: "string", title: "Monitor name" } } },
    };
    wrap(<MonitorWizard mode="add" {...baseProps} plugins={[ladaPlugin, comfy]} />);

    // Fill Lada's name, go to review, then Back twice to step 1.
    fireEvent.click(screen.getByText("Lada"));
    fireEvent.click(screen.getByRole("button", { name: /Continue|继续/ }));
    fireEvent.change(screen.getByLabelText(/名称|Monitor name/), { target: { value: "lada-1" } });
    fireEvent.click(screen.getByRole("button", { name: /Review|复核/ }));
    await screen.findByText("lada-1");
    fireEvent.click(screen.getByRole("button", { name: /Back|返回/ }));
    fireEvent.click(screen.getByRole("button", { name: /Back|返回/ }));

    // Switch to ComfyUI → its form must NOT carry over "lada-1".
    fireEvent.click(screen.getByText("ComfyUI"));
    fireEvent.click(screen.getByRole("button", { name: /Continue|继续/ }));
    expect((screen.getByLabelText(/名称|Monitor name/) as HTMLInputElement).value).toBe("");
  });

  it("recovers the edit form when the plugin catalog resolves after open", async () => {
    // Edit opens before /control/plugins loaded → no plugins yet.
    const { rerender } = wrap(
      <MonitorWizard
        mode="edit" name="lada-1" existingType="lada"
        existingConfig={{ name: "lada-1" }}
        plugins={[]} presets={[]}
        onClose={vi.fn()} onDone={vi.fn()} onError={vi.fn()}
      />,
    );
    expect(screen.queryByLabelText(/名称|Monitor name/)).not.toBeInTheDocument();
    // Catalog arrives → the config form appears (no reopen needed).
    rerender(
      <ThemeProvider theme={theme}>
        <MonitorWizard
          mode="edit" name="lada-1" existingType="lada"
          existingConfig={{ name: "lada-1" }}
          plugins={[ladaPlugin]} presets={[]}
          onClose={vi.fn()} onDone={vi.fn()} onError={vi.fn()}
        />
      </ThemeProvider>,
    );
    expect(await screen.findByLabelText(/名称|Monitor name/)).toBeInTheDocument();
  });

  it("surfaces a backend error on a failed add (not silent)", async () => {
    vi.spyOn(apiModule.api, "addMonitor").mockRejectedValue(new Error("a monitor named 'lada-1' already exists"));
    wrap(<MonitorWizard mode="add" {...baseProps} />);
    fireEvent.click(screen.getByText("Lada"));
    fireEvent.click(screen.getByRole("button", { name: /Continue|继续/ }));
    fireEvent.change(screen.getByLabelText(/名称|Monitor name/), { target: { value: "lada-1" } });
    fireEvent.click(screen.getByRole("button", { name: /Review|复核/ }));
    await screen.findByText("lada-1");
    fireEvent.click(screen.getByRole("button", { name: /Add monitor|添加监控/ }));
    expect(await screen.findByText(/already exists/)).toBeInTheDocument();
  });
});

// #208: the Jasna「8K VR」tickbox. The lock travels by React context (a uiSchema
// change would reset the rjsf form), so the four profile fields show decorative,
// disabled display controls while the real rjsf fields stay mounted but hidden.
// Queries go by role: getByLabelText would also match the hidden real input.
const VR8K = /8K VR/;
const UNET4K = /4K 档：使用 unet-4x 二次修复|Unet4X 4K/;
const CLIP4K = /4K 档片段长度|Clip Size 4K/;
const OVERLAP = /时序重叠帧数|Temporal Overlap/;
const DETECTION = /检测模型|Detection Model/;
const HINT = /已勾选 8K VR：这个任务的所有文件都按 SBS VR 处理|8K VR is on: every file of this task/;
const NAME = /名称|Monitor name/;
const lockedIds = () => [...document.querySelectorAll("input[id^='locked_']")].map((el) => el.id).sort();
// The rjsf field around a real input: visibility is asserted there, because a MUI
// checkbox input is always transparent (opacity 0) and never "visible" itself.
const fieldOf = (input: HTMLElement) => input.closest(".MuiFormControl-root") as HTMLElement;

function chooseJasna(plugins = [ladaPlugin, jasnaPlugin], label = "Jasna (video restore)") {
  wrap(<MonitorWizard mode="add" {...baseProps} plugins={plugins} />);
  fireEvent.click(screen.getByText(label));
  fireEvent.click(screen.getByRole("button", { name: /Continue|继续/ }));
}

describe("MonitorWizard jasna 8K VR (#208)", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("ticking keeps the typed name and locks the four fields to the profile; unticking restores them", () => {
    chooseJasna();
    fireEvent.change(screen.getByRole("textbox", { name: NAME }), { target: { value: "vr-1" } });
    // The operator's own values, distinct from the profile where the defaults match it.
    fireEvent.change(screen.getByRole("spinbutton", { name: OVERLAP }), { target: { value: "12" } });
    fireEvent.click(screen.getByRole("checkbox", { name: UNET4K }));
    const realClip = screen.getByRole("spinbutton", { name: CLIP4K });
    const realOverlap = screen.getByRole("spinbutton", { name: OVERLAP });
    const realDetection = screen.getByRole("textbox", { name: DETECTION });
    const realUnet = screen.getByRole("checkbox", { name: UNET4K });
    expect(screen.queryByText(HINT)).not.toBeInTheDocument();
    expect(lockedIds()).toEqual([]);

    fireEvent.click(screen.getByRole("checkbox", { name: VR8K }));

    // No form reset (the #204 N3-1 trap): the tick and the typed name survive.
    expect(screen.getByRole("checkbox", { name: VR8K })).toBeChecked();
    expect(screen.getByRole("textbox", { name: NAME })).toHaveValue("vr-1");
    // The display controls: disabled, the profile values, their own ids.
    const clip = screen.getByRole("textbox", { name: CLIP4K });
    const overlap = screen.getByRole("textbox", { name: OVERLAP });
    const detection = screen.getByRole("textbox", { name: DETECTION });
    const unet = screen.getByRole("checkbox", { name: UNET4K });
    expect(clip).toHaveAttribute("id", "locked_clip_size_4k");
    expect(detection).toHaveAttribute("id", "locked_detection_model");
    expect(unet).toHaveAttribute("id", "locked_unet4x_4k");
    for (const control of [clip, overlap, detection, unet]) expect(control).toBeDisabled();
    expect(clip).toHaveValue("30");
    expect(overlap).toHaveValue("8");
    expect(detection).toHaveValue("rfdetr-vr-v1");
    expect(unet).not.toBeChecked();
    expect(lockedIds()).toEqual([
      "locked_clip_size_4k", "locked_detection_model", "locked_temporal_overlap", "locked_unet4x_4k",
    ]);
    // The localized help (with its "8K VR overrides this" clause) is the display control's helper text.
    expect(clip).toHaveAccessibleDescription(/启动时由 8K VR 覆盖此项/);
    // The real fields stay mounted (rjsf keeps and submits them) but hidden.
    for (const real of [realClip, realOverlap, realDetection, realUnet]) {
      expect(real).toBeInTheDocument();
      expect(fieldOf(real)).not.toBeVisible();
    }
    // The hint sits in its own row right under the switch, with the profile values.
    const hint = screen.getByText(HINT);
    expect(hint).toBeVisible();
    expect(hint).toHaveTextContent(/rfdetr-vr-v1/);
    expect(hint).toHaveTextContent(/时序重叠 8|temporal overlap 8/);
    expect(hint).toHaveTextContent(/4K 档片段长度 30|4K-tier clip size 30/);
    expect(
      screen.getByRole("checkbox", { name: VR8K }).compareDocumentPosition(hint) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(hint.compareDocumentPosition(clip) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    fireEvent.click(screen.getByRole("checkbox", { name: VR8K }));

    expect(screen.queryByText(HINT)).not.toBeInTheDocument();
    expect(lockedIds()).toEqual([]);
    // The same DOM nodes come back (never remounted), enabled, with the stored values.
    expect(screen.getByRole("spinbutton", { name: CLIP4K })).toBe(realClip);
    expect(screen.getByRole("spinbutton", { name: OVERLAP })).toBe(realOverlap);
    expect(screen.getByRole("textbox", { name: DETECTION })).toBe(realDetection);
    expect(screen.getByRole("checkbox", { name: UNET4K })).toBe(realUnet);
    for (const real of [realClip, realOverlap, realDetection, realUnet]) {
      expect(fieldOf(real)).toBeVisible();
      expect(real).toBeEnabled();
    }
    expect(realClip).toHaveValue(60);
    expect(realOverlap).toHaveValue(12);
    expect(realDetection).toHaveValue("rfdetr-v6");
    expect(realUnet).toBeChecked();
    expect(screen.getByRole("textbox", { name: NAME })).toHaveValue("vr-1");
  });

  it("keeps an invalid typed value visible instead of locking it, and Review does not advance", async () => {
    const addMonitor = vi.spyOn(apiModule.api, "addMonitor").mockResolvedValue({} as never);
    chooseJasna();
    fireEvent.change(screen.getByRole("textbox", { name: NAME }), { target: { value: "vr-1" } });
    const realClip = screen.getByRole("spinbutton", { name: CLIP4K });
    fireEvent.change(realClip, { target: { value: "5" } }); // minimum 8
    fireEvent.click(screen.getByRole("checkbox", { name: VR8K }));

    // The invalid field is not hidden (a hidden invalid input blocks submit silently).
    expect(screen.getByRole("spinbutton", { name: CLIP4K })).toBe(realClip);
    expect(realClip).toBeVisible();
    expect(realClip).toBeEnabled();
    expect(realClip).toBeInvalid();
    expect(screen.queryByRole("textbox", { name: CLIP4K })).not.toBeInTheDocument();
    // The other three are locked.
    expect(lockedIds()).toEqual(["locked_detection_model", "locked_temporal_overlap", "locked_unet4x_4k"]);
    expect(screen.getByRole("textbox", { name: OVERLAP })).toBeDisabled();
    expect(screen.getByText(HINT)).toBeVisible();

    fireEvent.click(screen.getByRole("button", { name: /Review|复核/ }));
    await Promise.resolve();
    expect(screen.queryByRole("button", { name: /Add monitor|添加监控/ })).not.toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: VR8K })).toBeChecked();
    expect(addMonitor).not.toHaveBeenCalled();

    // A valid value locks it again, and the hidden valid field no longer blocks Review.
    fireEvent.change(realClip, { target: { value: "45" } });
    expect(screen.getByRole("textbox", { name: CLIP4K })).toHaveValue("30");
    expect(realClip).not.toBeVisible();
    expect(realClip).toBeValid();
    fireEvent.click(screen.getByRole("button", { name: /Review|复核/ }));
    expect(await screen.findByRole("button", { name: /Add monitor|添加监控/ })).toBeInTheDocument();
    expect(addMonitor).not.toHaveBeenCalled();
  });

  it("edit mode: a saved tick locks on open, and Save submits the stored values untouched", async () => {
    const updateMonitor = vi.spyOn(apiModule.api, "updateMonitor").mockResolvedValue({} as never);
    const saved = {
      name: "vr-1", vr_8k: true, clip_size_4k: 45, unet4x_4k: true,
      detection_model: "rfdetr-v6", temporal_overlap: 10,
    };
    wrap(
      <MonitorWizard mode="edit" name="vr-1" existingType="jasna" existingConfig={saved}
        {...baseProps} plugins={[jasnaPlugin]} />,
    );
    expect(screen.getByText(HINT)).toBeVisible();
    expect(screen.getByRole("textbox", { name: CLIP4K })).toHaveValue("30");
    expect(screen.getByRole("textbox", { name: CLIP4K })).toBeDisabled();
    expect(screen.getByRole("checkbox", { name: UNET4K })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: UNET4K })).toBeDisabled();

    fireEvent.click(screen.getByRole("button", { name: /Save changes|保存更改/ }));

    await waitFor(() => expect(updateMonitor).toHaveBeenCalledWith("vr-1", {
      config: expect.objectContaining({
        vr_8k: true, clip_size_4k: 45, unet4x_4k: true, detection_model: "rfdetr-v6", temporal_overlap: 10,
      }),
    }));
  });

  it("add-mode review lists the 8K VR row first and the overridden values as stored → profile", async () => {
    chooseJasna();
    fireEvent.change(screen.getByRole("textbox", { name: NAME }), { target: { value: "vr-1" } });
    fireEvent.click(screen.getByRole("checkbox", { name: VR8K }));
    fireEvent.click(screen.getByRole("button", { name: /Review|复核/ }));
    await screen.findByRole("button", { name: /Add monitor|添加监控/ });

    const typeRow = screen.getByText(/服务类型|Service type/).parentElement as HTMLElement;
    const vrRow = typeRow.nextElementSibling as HTMLElement;
    expect(within(vrRow).getByText("8K VR")).toBeInTheDocument();
    expect(within(vrRow).getByText(/已勾选（4K 档不用 unet-4x）|on \(4K tier without unet-4x\)/)).toBeInTheDocument();
    expect(screen.getByText("60 → 30")).toBeInTheDocument();
    expect(screen.getByText("rfdetr-v6 → rfdetr-vr-v1")).toBeInTheDocument();
    // An equal pair (overlap 8 = profile 8) shows the value once.
    const overlapRow = screen.getByText(OVERLAP).parentElement as HTMLElement;
    expect(within(overlapRow).getByText("8")).toBeInTheDocument();
    expect(screen.getAllByText(/→/)).toHaveLength(2);

    // Back, untick, Review → neither the row nor an arrow.
    fireEvent.click(screen.getByRole("button", { name: /Back|返回/ }));
    fireEvent.click(screen.getByRole("checkbox", { name: VR8K }));
    fireEvent.click(screen.getByRole("button", { name: /Review|复核/ }));
    await screen.findByRole("button", { name: /Add monitor|添加监控/ });
    expect(screen.queryByText("8K VR")).not.toBeInTheDocument();
    expect(screen.queryByText(/→/)).not.toBeInTheDocument();
    expect(screen.getByText("60")).toBeInTheDocument();
  });

  it.each([
    ["a non-jasna plugin", { ...jasnaPlugin, type_id: "lada", display_name: "Lada VR" }, "Lada VR"],
    ["an older jasna catalog without the profile", {
      ...jasnaPlugin, ui_schema: { "ui:order": jasnaPlugin.ui_schema["ui:order"] },
    }, "Jasna (video restore)"],
  ])("no lock, hint or review row for %s", async (_case, plugin, label) => {
    chooseJasna([plugin], label);
    fireEvent.change(screen.getByRole("textbox", { name: NAME }), { target: { value: "vr-1" } });
    fireEvent.click(screen.getByRole("checkbox", { name: VR8K }));
    expect(screen.getByRole("checkbox", { name: VR8K })).toBeChecked();
    expect(screen.queryByText(HINT)).not.toBeInTheDocument();
    expect(lockedIds()).toEqual([]);
    expect(screen.getByRole("spinbutton", { name: CLIP4K })).toBeVisible();
    expect(screen.getByRole("spinbutton", { name: CLIP4K })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: /Review|复核/ }));
    await screen.findByRole("button", { name: /Add monitor|添加监控/ });
    expect(screen.queryByText("8K VR")).not.toBeInTheDocument();
    expect(screen.queryByText(/→/)).not.toBeInTheDocument();
  });
});
