import type { TFunction } from "i18next";

// Types + pure helpers for the dev_activity `ai` metrics block (#154). Kept out of
// AiActivity.tsx so that file exports only components (Fast Refresh friendly).

export type Tool = {
  tool: string;
  state: string | null;
  present: boolean;
  age_s: number | null;
};

export type AiMetrics = {
  ai_state?: string;
  busy_tools?: string[];
  tools?: Tool[];
  window_s?: number;
  duty?: { busy_s?: number; ratio?: number };
};

// headline → the StatusDot state token (busy/waiting are "live" = pulse).
export const HEADLINE_DOT: Record<string, string> = {
  busy: "running",
  waiting: "starting",
  idle: "idle",
  present_only: "idle",
  none: "unknown",
};

export function isAiMetrics(m: Record<string, unknown> | undefined): m is AiMetrics {
  // Require both keys so a monitor that merely emits an `ai_state` metric isn't
  // mistaken for the dev_activity block.
  return !!m && typeof m.ai_state === "string" && Array.isArray(m.tools);
}

export function aiHeadlineLabel(m: AiMetrics, t: TFunction): string {
  const tools = (m.busy_tools ?? []).join(", ");
  switch (m.ai_state) {
    case "busy":
      return t("ai.busy", { tools });
    case "waiting":
      return t("ai.waiting");
    case "idle":
      return t("ai.idle");
    case "present_only":
      return t("ai.presentOnly");
    default:
      return t("ai.none");
  }
}
