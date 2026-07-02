import { Box, LinearProgress, Stack, Typography } from "@mui/material";
import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import { StatusDot } from "./StatusDot";

// Renders the dev_activity monitor's `ai` metrics block (#154) — machine headline,
// per-tool busy/idle/present rows, and a 30-min duty bar. Used on the agent console
// (via MonitorMetrics) and the Hub. Design: pages/ai-activity-monitor.md.
// Status is never colour-only: every dot is paired with a text label.

type Tool = { tool: string; state: string | null; present: boolean; age_s: number | null };
type AiMetrics = {
  ai_state?: string;
  busy_tools?: string[];
  tools?: Tool[];
  window_s?: number;
  duty?: { busy_s?: number; ratio?: number };
};

// headline → the StatusDot state token (busy/waiting are "live" = pulse).
const HEADLINE_DOT: Record<string, string> = {
  busy: "running",
  waiting: "starting",
  idle: "idle",
  present_only: "idle",
  none: "unknown",
};

export function isAiMetrics(m: Record<string, unknown> | undefined): m is AiMetrics {
  return !!m && typeof (m as AiMetrics).ai_state === "string";
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

// Compact badge for the Hub machine-row header (the fleet glance).
export function AiBadge({ metrics }: { metrics: AiMetrics }) {
  const { t } = useTranslation();
  const state = HEADLINE_DOT[metrics.ai_state ?? "none"] ?? "unknown";
  return (
    <Stack direction="row" alignItems="center" spacing={0.5} component="span">
      <StatusDot state={state} live={metrics.ai_state === "busy" || metrics.ai_state === "waiting"} />
      <Typography variant="caption" color="text.secondary">
        {aiHeadlineLabel(metrics, t)}
      </Typography>
    </Stack>
  );
}

function toolLabel(tool: Tool, t: TFunction): string {
  if (tool.state) return t(`ai.tool.${tool.state}`, { defaultValue: tool.state });
  if (tool.present) return t("ai.presentUnreported");
  return t("ai.unknown");
}

function toolDot(tool: Tool): string {
  if (tool.state === "busy") return "running";
  if (tool.state === "waiting") return "starting";
  if (tool.state === "idle") return "idle";
  if (tool.present) return "idle";
  return "unknown";
}

export function AiActivity({ metrics }: { metrics: AiMetrics }) {
  const { t } = useTranslation();
  const tools = metrics.tools ?? [];
  const winMin = Math.round((metrics.window_s ?? 1800) / 60);
  const ratio = metrics.duty?.ratio ?? 0;
  const busyMin = Math.round((metrics.duty?.busy_s ?? 0) / 60);
  const pct = Math.round(ratio * 100);

  return (
    <Box sx={{ mt: 1 }}>
      <Stack direction="row" alignItems="center" spacing={1}>
        <StatusDot
          state={HEADLINE_DOT[metrics.ai_state ?? "none"] ?? "unknown"}
          live={metrics.ai_state === "busy" || metrics.ai_state === "waiting"}
        />
        <Typography variant="body2" sx={{ fontWeight: 600 }}>
          {aiHeadlineLabel(metrics, t)}
        </Typography>
      </Stack>

      {tools.length > 0 && (
        <Stack sx={{ mt: 1 }} spacing={0.25}>
          {tools.map((tl) => (
            <Stack key={tl.tool} direction="row" alignItems="center" spacing={1}>
              <StatusDot state={toolDot(tl)} live={false} />
              <Typography variant="caption" sx={{ minWidth: 64 }}>{tl.tool}</Typography>
              <Typography variant="caption" color="text.secondary" sx={{ flex: 1 }}>
                {toolLabel(tl, t)}
              </Typography>
              {tl.age_s != null && (
                <Typography variant="caption" color="text.secondary"
                  sx={{ fontVariantNumeric: "tabular-nums" }}>
                  {t("ai.ago", { s: Math.round(tl.age_s) })}
                </Typography>
              )}
            </Stack>
          ))}
        </Stack>
      )}

      <Box sx={{ mt: 1 }}>
        <Typography variant="caption" color="text.secondary">
          {t("ai.duty", { win: winMin, busy: busyMin, pct })}
        </Typography>
        <LinearProgress
          variant="determinate"
          value={pct}
          sx={{ mt: 0.25, height: 6, borderRadius: 1 }}
          aria-label={t("ai.duty", { win: winMin, busy: busyMin, pct })}
        />
      </Box>
    </Box>
  );
}
