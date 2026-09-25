// Shared metric colours for MonitorMetrics, PipelineProgress (#189) and the Hub
// card mini-bars. Kept out of the component files so each exports only
// components (Fast Refresh friendly, same split as aiActivity.helpers) and
// PipelineProgress does not import MonitorMetrics back (no import cycle).

export const TINT = {
  ok: "#22C55E",      // success green — design Accent
  warn: "#F59E0B",    // amber
  crit: "#EF4444",    // destructive
  idle: "#64748B",    // slate
} as const;

// Utilization colour ramp (CPU/GPU/MEM/VRAM): green → amber → red. Exported so the
// Hub card mini-bars (#113) share the exact 70/90 thresholds + colours.
export function utilTint(pct: number): string {
  if (pct >= 90) return TINT.crit;
  if (pct >= 70) return TINT.warn;
  return TINT.ok;
}
