import { afterEach, describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import i18n, { setLang } from "../i18n";
import { ServiceIcon } from "../components/ServiceIcon";
import { fieldLabel, localizeSchema } from "../schemaI18n";

// Standalone「AV 翻译 (subtitles)」task (#179): catalog blurb in both languages,
// its own ServiceIcon glyph, and zh labels for all seven config fields.
describe("avsubs UI strings + icon (#179)", () => {
  afterEach(() => setLang("zh-CN"));

  it("has a services.avsubs blurb in both languages", () => {
    setLang("en");
    const en = i18n.t("services.avsubs");
    expect(en).not.toBe("services.avsubs");
    expect(en).toMatch(/AV translate/);
    setLang("zh-CN");
    const zh = i18n.t("services.avsubs");
    expect(zh).not.toBe("services.avsubs");
    expect(zh).toMatch(/AV 翻译/);
    expect(zh).not.toBe(en);
  });

  it("mentions the standalone AV-translate task in the About blurb (en + zh)", () => {
    setLang("en");
    expect(i18n.t("settings.aboutBody")).toMatch(/standalone task/);
    setLang("zh-CN");
    expect(i18n.t("settings.aboutBody")).toMatch(/独立任务/);
  });

  it("renders the avsubs glyph, not the generic fallback", () => {
    const avsubs = render(<ServiceIcon id="avsubs" />).container.innerHTML;
    const fallback = render(<ServiceIcon id="__unmapped__" />).container.innerHTML;
    expect(avsubs).not.toBe(fallback);
    expect(avsubs).toContain('d="M7 13.5h10M9 16.5h6"'); // the two subtitle lines
  });

  it("translates every avsubs config field to zh", () => {
    const fields = [
      "avsubs_root_folder",
      "avsubs_recursive",
      "avsubs_extensions",
      "whisperjav_exe_path",
      "whisperjav_engine",
      "whisperjav_extra_args",
      "avsubs_gpu_monitor",
    ];
    const s = {
      type: "object" as const,
      properties: Object.fromEntries(
        fields.map((f) => [f, { type: "string" as const, title: f, description: "EN" }]),
      ),
    };
    const p = localizeSchema(s, "avsubs", "zh-CN").properties as Record<
      string,
      { title: string; description: string }
    >;
    for (const f of fields) {
      expect(p[f].title).not.toBe(f);
      expect(p[f].description).not.toBe("EN");
    }
    expect(fieldLabel("avsubs_root_folder", "avsubs", "zh-CN")).toBe("片库文件夹");
    expect(p.avsubs_root_folder.description).toMatch(/\.avsubs\//);
    expect(p.whisperjav_exe_path.description).toContain("C:\\WhisperJAV\\Scripts\\whisperjav.exe");
    expect(p.whisperjav_extra_args.description).toMatch(/--translate\*.*Windows 路径请加引号/);
    // English stays untouched.
    expect(localizeSchema(s, "avsubs", "en")).toBe(s);
  });
});
