import { alpha } from "@mui/material/styles";
import { TINT } from "./monitorMetrics.helpers";
export type Tone = "ok" | "soft" | "crit" | "warn" | "idle";
export function toneSx(tone: Tone) {
  switch (tone) {
    case "ok": return { bgcolor: alpha(TINT.ok, 0.14), color: "success.main" };
    case "soft": return { bgcolor: alpha(TINT.ok, 0.08), color: "success.light" };
    case "crit": return { bgcolor: alpha(TINT.crit, 0.14), color: "error.main" };
    case "warn": return { bgcolor: alpha(TINT.warn, 0.14), color: "warning.main" };
    default: return { bgcolor: "rgba(148,163,184,0.1)", color: "text.secondary" };
  }
}

