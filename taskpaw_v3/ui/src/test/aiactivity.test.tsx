import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { ThemeProvider } from "@mui/material/styles";
import { AiActivity, AiBadge, isAiMetrics } from "../components/AiActivity";
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
  it("isAiMetrics detects the ai block", () => {
    expect(isAiMetrics({ ai_state: "busy" })).toBe(true);
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
