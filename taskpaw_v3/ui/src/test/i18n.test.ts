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
});
