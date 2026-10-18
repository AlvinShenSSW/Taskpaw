export const LTX_STAGES = ["scan", "encode", "denoise", "compose"] as const;
export function ltxStage(metrics: Record<string, unknown>): number | null {
  if (typeof metrics.ltx_stage !== "string") return null;
  const m = /^([1-4])\/4 (scan|encode|denoise|compose)$/.exec(metrics.ltx_stage);
  return m && LTX_STAGES[Number(m[1]) - 1] === m[2] ? Number(m[1]) - 1 : null;
}
