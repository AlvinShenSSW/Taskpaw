import { afterEach, describe, expect, it } from "vitest";
import i18n, { currentLang, setLang } from "../i18n";

// i18n machinery smoke tests (#45/#78): default zh-CN, switch + persistence.
describe("i18n", () => {
  afterEach(() => setLang("zh-CN"));

  it("defaults to Simplified Chinese", () => {
    expect(currentLang()).toBe("zh-CN");
    expect(i18n.t("common.start")).toBe("启动");
  });

  it("switches language and persists the choice", () => {
    setLang("en");
    expect(currentLang()).toBe("en");
    expect(i18n.t("common.start")).toBe("Start");
    expect(localStorage.getItem("taskpaw.lang")).toBe("en");
    expect(document.documentElement.lang).toBe("en");
  });

  it("interpolates variables", () => {
    setLang("en");
    expect(i18n.t("agent.monitorsTitle", { machine: "box1" })).toContain("box1");
  });

  it("does not throw when reporting the language outside a Tauri shell (#108)", () => {
    // No window.__TASKPAW__ in the test env → the shell sync (set_ui_lang) must
    // be a safe no-op rather than blowing up the browser/dev path.
    expect(window.__TASKPAW__).toBeUndefined();
    expect(() => {
      setLang("en");
      setLang("zh-CN");
    }).not.toThrow();
  });
});
