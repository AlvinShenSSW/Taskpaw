import { afterEach, describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import type { RJSFSchema } from "@rjsf/utils";
import { SchemaForm } from "../components/SchemaForm";
import { fieldLabel, localizeSchema } from "../schemaI18n";
import { theme } from "../theme";
import i18n from "../i18n";

// A trimmed Lada-like schema: a base field + a plugin field + an untranslated one.
const SCHEMA: RJSFSchema = {
  type: "object",
  properties: {
    name: { type: "string", title: "Name", description: "A unique name for this monitor on this machine." },
    lada_cli_path: { type: "string", title: "Lada Cli Path", description: "Full path to the lada-cli EXECUTABLE FILE" },
    made_up_field: { type: "string", title: "Made Up Field", description: "not translated" },
  },
};

describe("localizeSchema (#121)", () => {
  it("overlays zh title/description for known fields, keeps English otherwise", () => {
    const zh = localizeSchema(SCHEMA, "lada", "zh-CN");
    const p = zh.properties as Record<string, { title: string; description?: string }>;
    expect(p.name.title).toBe("名称");                         // base field translated
    expect(p.lada_cli_path.title).toBe("lada-cli 路径");        // plugin field translated
    expect(p.lada_cli_path.description).toContain("完整路径");
    // Untranslated field keeps its English (never blank).
    expect(p.made_up_field.title).toBe("Made Up Field");
  });

  it("leaves the schema untouched for English (and never mutates the input)", () => {
    const en = localizeSchema(SCHEMA, "lada", "en");
    expect(en).toBe(SCHEMA);                                    // same ref, no work
    const zh = localizeSchema(SCHEMA, "lada", "zh-CN");
    expect(zh).not.toBe(SCHEMA);                                // new object
    // original untouched
    expect((SCHEMA.properties as Record<string, { title: string }>).name.title).toBe("Name");
  });

  it("returns the schema unchanged for a malformed properties value", () => {
    const bad = { type: "object", properties: "nope" } as unknown as RJSFSchema;
    expect(localizeSchema(bad, "lada", "zh-CN")).toBe(bad); // no throw, same ref
    const arr = { type: "object", properties: [] } as unknown as RJSFSchema;
    expect(localizeSchema(arr, "lada", "zh-CN")).toBe(arr);
  });

  it("fieldLabel: zh title, else the schema English title (matching the form), else the key", () => {
    expect(fieldLabel("lada_cli_path", "lada", "zh-CN")).toBe("lada-cli 路径");
    // Untranslated field: zh falls back to the English title, NOT the raw key —
    // consistent with what localizeSchema shows in the form (Kimi).
    expect(fieldLabel("some_new_field", "lada", "zh-CN", "Some New Field")).toBe("Some New Field");
    expect(fieldLabel("some_new_field", "lada", "en", "Some New Field")).toBe("Some New Field");
    expect(fieldLabel("some_new_field", "lada", "zh-CN")).toBe("some_new_field"); // no title → key
  });

  it("translates the jasna fields, including the unet-4x tickboxes (#173)", () => {
    const s: RJSFSchema = {
      type: "object",
      properties: {
        unet4x_1080p: { type: "boolean", title: "Unet4X 1080P" },
        unet4x_4k: { type: "boolean", title: "Unet4X 4K" },
        jasna_exe_path: { type: "string", title: "Jasna Exe Path" },
      },
    };
    const p = localizeSchema(s, "jasna", "zh-CN").properties as Record<
      string,
      { title: string; description?: string }
    >;
    expect(fieldLabel("unet4x_1080p", "jasna", "zh-CN")).toBe("1080p 档：使用 unet-4x 二次修复");
    expect(p.unet4x_1080p.title).toBe("1080p 档：使用 unet-4x 二次修复");
    expect(p.unet4x_1080p.description).toContain("默认开");
    expect(p.unet4x_4k.title).toBe("4K 档：使用 unet-4x 二次修复");
    expect(p.unet4x_4k.description).toContain("默认关");
    expect(p.jasna_exe_path.title).toBe("jasna.exe 路径");
    // Same field name, different plugin → no lada wording leaks in.
    expect(localizeSchema(s, "lada", "zh-CN").properties).toMatchObject({
      unet4x_1080p: { title: "Unet4X 1080P" },
    });
  });

  it("does not cross-attribute a same-named field across plugins", () => {
    const s: RJSFSchema = { type: "object", properties: { host: { type: "string", title: "Host" } } };
    // comfyui.host has a description; tcp_check.host is just 主机 (no ComfyUI wording).
    const comfy = localizeSchema(s, "comfyui", "zh-CN").properties as Record<string, { description?: string }>;
    const tcp = localizeSchema(s, "tcp_check", "zh-CN").properties as Record<string, { description?: string }>;
    expect(comfy.host.description).toContain("ComfyUI");
    expect(tcp.host.description).toBeUndefined();
  });
});

describe("SchemaForm localization (#121)", () => {
  afterEach(async () => { await i18n.changeLanguage("zh-CN"); }); // restore default

  const renderForm = () =>
    render(
      <ThemeProvider theme={theme}>
        <SchemaForm schema={SCHEMA} typeId="lada" />
      </ThemeProvider>,
    );

  it("renders Chinese field labels when the UI language is Chinese", async () => {
    await i18n.changeLanguage("zh-CN"); // await: changeLanguage is async
    renderForm();
    // MUI outlined fields render the label twice (label + notch legend) → findAll.
    expect((await screen.findAllByText("lada-cli 路径")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Lada Cli Path")).not.toBeInTheDocument();
  });

  it("renders English labels when the UI language is English", async () => {
    await i18n.changeLanguage("en");
    renderForm();
    expect((await screen.findAllByText("Lada Cli Path")).length).toBeGreaterThan(0);
  });
});
