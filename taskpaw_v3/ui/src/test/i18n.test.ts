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

  it("carries the 1080p LTX hint and review-row strings in zh and en (#263)", () => {
    const zh = i18n.t("wizard.ltxHint");
    for (const part of ["只有 1080p 档的文件用 LTX", "4K 档和其他设置都按原来的配置", "unet-4x",
      "「8K VR」", "灰显", "取消勾选后恢复", "约 9 GB", "约 15 GB", "输入 y", "整片都用 LTX",
      "从头重跑当前影片"]) {
      expect(zh).toContain(part);
    }
    expect(i18n.t("wizard.ltxRow")).toBe("LTX");
    expect(i18n.t("wizard.ltxOn")).toBe("仅 1080p 档（不用 8K VR 和 1080p 档 unet-4x）");
    setLang("en");
    const en = i18n.t("wizard.ltxHint");
    for (const part of ["only the 1080p-tier files use LTX", "4K files and every other setting stay as they are",
      "8K VR", "greyed", "untick", "about 9 GB", "15 GB", "type y", "whole film with LTX",
      "restarts the current film"]) {
      expect(en).toContain(part);
    }
    expect(i18n.t("wizard.ltxRow")).toBe("LTX");
    expect(i18n.t("wizard.ltxOn")).toBe("1080p tier only (without 8K VR and the 1080p unet-4x)");
  });

  it("carries the 4K and both-tiers LTX hint and review strings in zh and en (#265)", () => {
    expect(i18n.t("wizard.ltxHint4k")).toContain("只有 4K 档的文件用 LTX");
    expect(i18n.t("wizard.ltxHint4k")).toContain("8 GB");
    expect(i18n.t("wizard.ltxHintBoth")).toContain("1080p 档和 4K 档的文件都用 LTX");
    expect(i18n.t("wizard.ltxOn4k")).toBe("仅 4K 档（不用 8K VR 和 4K 档 unet-4x）");
    expect(i18n.t("wizard.ltxOnBoth")).toBe("1080p 和 4K 档（不用 8K VR 和 unet-4x）");
    setLang("en");
    expect(i18n.t("wizard.ltxHint4k")).toContain("4K LTX is on: only the 4K-tier files use LTX");
    expect(i18n.t("wizard.ltxHintBoth")).toContain("LTX is on for both tiers");
    expect(i18n.t("wizard.ltxOn4k")).toBe("4K tier only (without 8K VR and the 4K unet-4x)");
    expect(i18n.t("wizard.ltxOnBoth")).toBe("both tiers (without 8K VR and unet-4x)");
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
      ...["jasna", "avsubs", "lada", "comfyui", "process", "heartbeat", "tcp_check", "jellyfin", "host_metrics", "folder", "custom_cmd", "state_file", "dev_activity"].map(k => `monitorType.${k}`), "services.jellyfin"];
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
