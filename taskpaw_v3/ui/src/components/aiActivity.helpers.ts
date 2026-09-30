import type { TFunction } from "i18next";

// Types + pure helpers for the dev_activity `ai` metrics block (#154). Kept out of
// AiActivity.tsx so that file exports only components (Fast Refresh friendly).

export type Tool = {
  tool: string;
  state: string | null;
  present: boolean;
  age_s: number | null;
  // #163 external CPU probe: state was inferred from observed CPU (not a hook), and
  // the subtree CPU% at that check. Absent/false for hook-reported or presence-only rows.
  ai?: boolean;
  source?: string;
  host?: string;
  session_age_s?: number | null;
  vscode_state?: string | null;
  observed?: boolean;
  cpu?: number | null;
};

export type AiMetrics = {
  ai_state?: string;
  busy_tools?: string[];
  tools?: Tool[];
  probe_errors?: { tool: string; layer: string; code: string }[];
  probe_limited?: boolean;
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

export function activitySource(tool: Tool): string | undefined {
  return tool.source ?? (tool.observed ? "cpu" : undefined);
}

export function activityProvenance(tool: Tool, t: TFunction): string {
  const source = activitySource(tool);
  const sourceKey = ["hook", "session", "cpu", "presence"].includes(source ?? "") ? source : "unknown";
  const hostKey = ["vscode", "other", "mixed", "unknown"].includes(tool.host ?? "") ? tool.host : "unknown";
  return [source ? t(`ai.source.${sourceKey}`) : "", tool.host ? t(`ai.host.${hostKey}`) : ""].filter(Boolean).join(" · ");
}
