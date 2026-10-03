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

  it("carries the 8K VR hint and review-row strings in zh and en (#208)", () => {
    // The values come from the backend profile (i18n params), never from the UI copy.
    const profile = { detection: "model-x", clip: 31, overlap: 9 };
    expect(i18n.t("wizard.vr8kHint", profile)).toBe(
      "已勾选 8K VR：这个任务的所有文件都按 SBS VR 处理（2D 影片请放到另一个任务）。启动时固定使用 model-x 检测模型、--vr-mode sbs、时序重叠 9、4K 档片段长度 31、4K 档不用 unet-4x。灰显字段里保存的原值不会改变，取消勾选后恢复生效。运行中的任务保存后会从头重跑当前影片。",
    );
    expect(i18n.t("wizard.vr8kRow")).toBe("8K VR");
    expect(i18n.t("wizard.vr8kOn")).toBe("已勾选（4K 档不用 unet-4x）");
    setLang("en");
    const en = i18n.t("wizard.vr8kHint", profile);
    for (const part of ["SBS VR", "2D films", "model-x", "--vr-mode sbs", "temporal overlap 9",
      "4K-tier clip size 31", "no unet-4x on the 4K tier", "untick", "restarts the current film"]) {
      expect(en).toContain(part);
    }
    expect(en).not.toMatch(/\{\{|rfdetr|\b30\b|\b8\b/);
    expect(i18n.t("wizard.vr8kRow")).toBe("8K VR");
    expect(i18n.t("wizard.vr8kOn")).toBe("on (4K tier without unet-4x)");
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

describe("#210 Hub observation translations", () => {
  afterEach(() => setLang("zh-CN"));
  it.each(["en", "zh-CN"] as const)("resolves every type and feedback key (%s)", lang => {
    setLang(lang);
    const keys = ["hub.agentVersion", "hub.versionSkew",
      ...["loading", "unavailable", "offline", "disabled", "unknown", "authFailed", "hubAuthFailed", "timeout", "failed", "stale", "resyncing", "noSnapshot", "empty"].map(k => `hub.films.${k}`),
      ...["jasna", "avsubs", "lada", "comfyui", "process", "heartbeat", "tcp_check", "host_metrics", "folder", "custom_cmd", "state_file", "dev_activity"].map(k => `monitorType.${k}`)];
    for (const key of keys) {
      expect(i18n.getResource(lang, "translation", key), key).toBeTypeOf("string");
      const value = i18n.t(key, { version: "3.9.7", hub: "3.9.6", agent: "3.9.7" });
      expect(value).not.toBe(key); expect(value).not.toContain("{{");
    }
    expect(i18n.t("hub.versionSkew", { hub: "3.9.6", agent: "3.9.7" })).toMatch(/Update the Hub|请更新 Hub/);
  });
});

describe("R14 request-state translations", () => {
  afterEach(() => setLang("zh-CN"));
  it.each(["en", "zh-CN"] as const)("resolves local API and event request feedback (%s)", lang => {
    setLang(lang);
    const keys = [...["title", "checking", "connected", "failed", "outdated", "credentials", "lastSuccess"].map(key => `connection.${key}`),
      ...["requestStatus", "loading", "failed", "retry", "stale", "statusUnavailable"].map(key => `events.${key}`)];
    for (const key of keys) {
      expect(i18n.getResource(lang, "translation", key), key).toBeTypeOf("string");
      expect(i18n.t(key, { time: "12:00:00" })).not.toMatch(/\{\{|^connection\.|^events\./);
    }
  });
});
