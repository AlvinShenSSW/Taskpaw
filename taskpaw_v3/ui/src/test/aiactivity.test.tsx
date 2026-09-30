import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { AiActivity, AiBadge } from "../components/AiActivity";
import { isAiMetrics } from "../components/aiActivity.helpers";
import { MonitorMetrics } from "../components/MonitorMetrics";
import { theme } from "../theme";
import "../i18n";

const wrap = (ui: React.ReactNode) =>
  render(<ThemeProvider theme={theme}>{ui}</ThemeProvider>);

// Language is non-deterministic in tests, so match either locale.
const RE = {
  busy: /Running AI|在跑 AI/,
  present: /present · not reported|在场 · 未上报/,
  none: /No AI activity|无 AI 活动/,
};

describe("AiActivity (#154)", () => {
  it("isAiMetrics detects the ai block (needs ai_state + tools)", () => {
    expect(isAiMetrics({ ai_state: "busy", tools: [] })).toBe(true);
    expect(isAiMetrics({ ai_state: "busy" })).toBe(false); // no tools → not it
    expect(isAiMetrics({ cpu_pct: 12 })).toBe(false);
    expect(isAiMetrics(undefined)).toBe(false);
  });

  it("renders the busy headline with tools + per-tool rows + duty", () => {
    wrap(
      <AiActivity
        metrics={{
          ai_state: "busy",
          busy_tools: ["claude"],
          tools: [
            { tool: "claude", state: "busy", present: true, age_s: 5 },
            { tool: "kimi", state: null, present: true, age_s: null },
          ],
          window_s: 1800,
          duty: { busy_s: 600, ratio: 0.33 },
        }}
      />,
    );
    expect(screen.getByText(RE.busy)).toBeInTheDocument();
    expect(screen.getByText("claude")).toBeInTheDocument();
    // kimi present but no state file → "present · not reported", not idle.
    expect(screen.getByText(RE.present)).toBeInTheDocument();
  });

  it("present_only reads as present (the core #154 fix), not idle/none", () => {
    wrap(<AiActivity metrics={{ ai_state: "present_only", tools: [] }} />);
    expect(screen.getByText(/AI present|AI 在场/)).toBeInTheDocument();
  });

  it("MonitorMetrics delegates ai metrics to AiActivity", () => {
    wrap(<MonitorMetrics metrics={{ ai_state: "none", tools: [] }} />);
    expect(screen.getByText(RE.none)).toBeInTheDocument();
  });

  it("AiBadge shows a compact headline", () => {
    wrap(<AiBadge metrics={{ ai_state: "busy", busy_tools: ["codex"] }} />);
    expect(screen.getByText(RE.busy)).toBeInTheDocument();
  });
});

// T8: language is explicit for every provenance assertion.
describe("activity provenance #211", () => {
  it.each(["en", "zh-CN"])("shows session/host, distinct ages and warnings in %s", async (locale) => {
    const { default: i18n } = await import("../i18n");
    await i18n.changeLanguage(locale);
    const m = { ai_state: "busy", busy_tools: ["claude"], probe_errors: [{ tool: "codex", layer: "session", code: "denied" }], probe_limited: true,
      tools: [{ tool: "claude", state: "busy", present: true, age_s: 600, source: "session", host: "vscode", session_age_s: 7, observed: false, cpu: 99 }] };
    const view = wrap(<AiActivity metrics={m} />);
    expect(screen.getByText(locale === "en" ? /Session activity/ : /会话活动/)).toBeInTheDocument();
    expect(screen.getByText(/VS Code/)).toBeInTheDocument();
    expect(screen.getByText(locale === "en" ? /Hook.*600/ : /钩子.*600/)).toBeInTheDocument();
    expect(screen.getByText(locale === "en" ? /Session.*7/ : /会话.*7/)).toBeInTheDocument();
    expect(screen.queryByText("~99%")).not.toBeInTheDocument();
    expect(screen.getByText(locale === "en" ? /probe unavailable/i : /探测不可用/)).toBeInTheDocument();
    view.unmount();
    wrap(<AiBadge metrics={m} />);
    expect(screen.getByText(locale === "en" ? /claude · Session activity · VS Code/ : /claude · 会话活动 · VS Code/)).toBeInTheDocument();
  });
});

it.each(["en", "zh-CN"])("renders legacy CPU, unknown enums and editor waiting in %s", async locale => {
  const { default: i18n } = await import("../i18n");
  await i18n.changeLanguage(locale);
  wrap(<AiActivity metrics={{ai_state:"waiting",tools:[
    {tool:"legacy",state:"busy",present:true,age_s:null,observed:true,cpu:12},
    {tool:"old",state:"idle",present:true,age_s:null},
    {tool:"vscode",state:"waiting",present:true,age_s:null,ai:false,source:"hook",host:"vscode"},
    {tool:"future",state:"new-state",present:true,age_s:null,source:"future",host:"future"},
  ]}} />);
  expect(screen.getByText("~12%")).toBeInTheDocument();
  expect(screen.getByText(locale === "en" ? /Vibe coding · waiting/ : /AI 编程中 · 等待/)).toBeInTheDocument();
  expect(screen.getByText(locale === "en" ? /Unknown source · Host unknown/ : /来源未知 · 宿主未知/)).toBeInTheDocument();
  expect(screen.queryByText("new-state")).not.toBeInTheDocument();
});
