import { afterEach, describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import type { RJSFSchema } from "@rjsf/utils";
import { SchemaForm } from "../components/SchemaForm";
import { ServiceIcon } from "../components/ServiceIcon";
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

  it("translates the jasna 8K VR tickbox and the clauses on the fields it overrides (#208)", () => {
    const s: RJSFSchema = {
      type: "object",
      properties: Object.fromEntries(
        ["vr_8k", "unet4x_4k", "clip_size_4k", "temporal_overlap", "detection_model", "jasna_extra_args"]
          .map((k) => [k, { type: "string", title: k, description: "English" }]),
      ),
    };
    const p = localizeSchema(s, "jasna", "zh-CN").properties as Record<
      string,
      { title: string; description?: string }
    >;
    expect(fieldLabel("vr_8k", "jasna", "zh-CN")).toBe("8K VR");
    expect(p.vr_8k.title).toBe("8K VR");
    for (const part of [
      "所有文件都按 8K SBS VR 处理", "rfdetr-vr-v1", "--vr-mode sbs", "时序重叠 8", "4K 档片段长度 30",
      "4K 档不用 unet-4x", "保存的值", "取消勾选", "2D 影片", "从头重跑当前影片",
    ]) {
      expect(p.vr_8k.description).toContain(part);
    }
    // C12: the fields the tick overrides say so, in their own help text.
    for (const k of ["unet4x_4k", "clip_size_4k", "temporal_overlap"]) {
      expect(p[k].description).toContain("勾选「8K VR」时，启动时由 8K VR 覆盖此项");
    }
    expect(p.unet4x_4k.description).toContain("默认关");
    expect(p.detection_model.description).toContain("勾选「8K VR」时忽略此项（使用 rfdetr-vr-v1）");
    expect(p.jasna_extra_args.description).toContain("勾选「8K VR」时，--vr-mode 和 --detection-model-path 也会被拒绝");
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

it("localizes activity session controls without changing English", () => {
  expect(fieldLabel("session_activity", "dev_activity", "zh-CN")).toBe("会话活动探测");
  expect(fieldLabel("session_roots", "dev_activity", "zh-CN")).toBe("会话目录");
  const schema: RJSFSchema = { type: "object", properties: { session_activity: { type: "boolean", title: "Session activity" } } };
  expect(localizeSchema(schema,"dev_activity","en")).toEqual(schema);
});

it("localizes the jellyfin base URL field and gives it its own glyph (#257)", () => {
  expect(fieldLabel("base_url", "jellyfin", "zh-CN")).toBe("服务地址");
  const schema: RJSFSchema = { type: "object", properties: { base_url: { type: "string", title: "Base URL" } } };
  const zh = localizeSchema(schema, "jellyfin", "zh-CN").properties as Record<string, { description?: string }>;
  expect(zh.base_url.description).toContain("Jellyfin");
  expect(localizeSchema(schema, "jellyfin", "en")).toEqual(schema);
  const icon = render(<ServiceIcon id="jellyfin" />).container.innerHTML;
  expect(icon).not.toBe(render(<ServiceIcon id="__unmapped__" />).container.innerHTML);
});
