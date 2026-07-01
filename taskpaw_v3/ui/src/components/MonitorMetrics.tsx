import { Box, CircularProgress, LinearProgress, Stack, Tooltip, Typography } from "@mui/material";
import { useTranslation } from "react-i18next";

// Live metrics dashboard for a monitor's status pane (design-system
// pages/agent-console.md → StatusHeader: "live metric line … file N/M, fps, %").
// The backend hands us a flat metrics dict (current_file, queue_*, *_pct, gpu_mem_*,
// fps, eta, …); we render the KNOWN keys as a dashboard — current file + progress,
// circular utilization gauges, a VRAM bar, stat tiles — and degrade any unknown
// keys to labelled tiles rather than dumping raw JSON.

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

function fmtGB(mb: number): string {
  return mb >= 1024 ? `${(mb / 1024).toFixed(1)} GB` : `${Math.round(mb)} MB`;
}

// A circular utilization gauge (determinate) with the value at its centre.
function Gauge({ label, pct, sub }: { label: string; pct: number; sub?: string }) {
  const tint = utilTint(pct);
  const v = Math.max(0, Math.min(100, pct));
  return (
    <Stack alignItems="center" spacing={0.75} sx={{ minWidth: 92 }}>
      <Box sx={{ position: "relative", display: "inline-flex" }}>
        {/* track */}
        <CircularProgress variant="determinate" value={100} size={72} thickness={4}
          sx={{ color: "rgba(148,163,184,0.18)", position: "absolute" }} />
        {/* value */}
        <CircularProgress variant="determinate" value={v} size={72} thickness={4}
          sx={{ color: tint, "& .MuiCircularProgress-circle": { strokeLinecap: "round" },
                filter: `drop-shadow(0 0 6px ${tint}55)` }} />
        <Box sx={{ position: "absolute", inset: 0, display: "flex", flexDirection: "column",
                   alignItems: "center", justifyContent: "center" }}>
          <Typography sx={{ fontFamily: '"Fira Code", monospace', fontWeight: 600,
                            fontSize: 16, lineHeight: 1, fontVariantNumeric: "tabular-nums" }}>
            {Math.round(v)}
          </Typography>
          <Typography sx={{ fontSize: 9, color: "text.secondary", lineHeight: 1 }}>%</Typography>
        </Box>
      </Box>
      <Typography variant="caption" sx={{ letterSpacing: 0.6, color: "text.secondary",
                                          textTransform: "uppercase", fontSize: 10 }}>{label}</Typography>
      {sub && (
        <Typography sx={{ fontFamily: '"Fira Code", monospace', fontSize: 11,
                          color: "text.secondary", fontVariantNumeric: "tabular-nums" }}>{sub}</Typography>
      )}
    </Stack>
  );
}

// A labelled value tile (fps, ETA, and any unknown metric).
function Tile({ label, value }: { label: string; value: string }) {
  return (
    <Box sx={{ px: 1.5, py: 1, borderRadius: 2, bgcolor: "rgba(148,163,184,0.06)",
               border: "1px solid", borderColor: "divider", minWidth: 84 }}>
      <Typography variant="caption" sx={{ letterSpacing: 0.6, color: "text.secondary",
                                          textTransform: "uppercase", fontSize: 10, display: "block" }}>
        {label}
      </Typography>
      <Typography sx={{ fontFamily: '"Fira Code", monospace', fontWeight: 600, fontSize: 15,
                        fontVariantNumeric: "tabular-nums", mt: 0.25 }}>{value}</Typography>
    </Box>
  );
}

const KNOWN = new Set([
  "current_file", "queue_completed", "queue_total", "queue_remaining", "percent",
  "fps", "eta", "cpu_pct", "mem_pct", "gpu_pct", "gpu_mem_used_mb", "gpu_mem_total_mb",
  // absolute RAM (shown as the MEM gauge's GB sub-label, not raw tiles)
  "mem_used_mb", "mem_total_mb",
]);

export function MonitorMetrics({ metrics }: { metrics?: Record<string, unknown> }) {
  const { t } = useTranslation();
  const m = metrics ?? {};
  // Finite only — a malformed metric (NaN/Infinity) must not render as "NaN" in a
  // gauge/bar, matching the Number.isFinite guard used elsewhere (Kimi).
  const num = (k: string) =>
    typeof m[k] === "number" && Number.isFinite(m[k] as number) ? (m[k] as number) : undefined;
  const str = (k: string) => (typeof m[k] === "string" ? (m[k] as string) : undefined);

  if (Object.keys(m).length === 0) return null;

  const currentFile = str("current_file");
  const qDone = num("queue_completed");
  const qTotal = num("queue_total");
  const qRem = num("queue_remaining");
  const percent = num("percent");
  const fps = num("fps");
  const eta = str("eta");
  const cpu = num("cpu_pct");
  const mem = num("mem_pct");
  const gpu = num("gpu_pct");
  const vramUsed = num("gpu_mem_used_mb");
  const vramTotal = num("gpu_mem_total_mb");
  const ramUsed = num("mem_used_mb");
  const ramTotal = num("mem_total_mb");

  const gauges = [
    gpu !== undefined ? { label: "GPU", pct: gpu } : null,
    cpu !== undefined ? { label: "CPU", pct: cpu } : null,
    mem !== undefined ? { label: "MEM", pct: mem } : null,
  ].filter(Boolean) as { label: string; pct: number }[];

  const tiles: { label: string; value: string }[] = [];
  if (fps !== undefined) tiles.push({ label: t("events.fps"), value: fps.toFixed(fps < 10 ? 1 : 0) });
  if (eta) tiles.push({ label: t("events.eta"), value: eta });
  // Unknown keys → tiles (so nothing is silently hidden, nothing is raw JSON).
  for (const [k, val] of Object.entries(m)) {
    if (KNOWN.has(k)) continue;
    tiles.push({ label: k.replace(/_/g, " "), value: typeof val === "number" ? String(val) : String(val) });
  }

  const vramPct = vramUsed !== undefined && vramTotal ? (vramUsed / vramTotal) * 100 : undefined;

  return (
    <Stack spacing={2} sx={{ mt: 2 }}>
      {/* Now-processing banner + current-file progress */}
      {currentFile && (
        <Box sx={{ p: 1.5, borderRadius: 2, bgcolor: "rgba(34,197,94,0.06)",
                   border: "1px solid", borderColor: "rgba(34,197,94,0.25)" }}>
          <Typography variant="caption" sx={{ color: "text.secondary", textTransform: "uppercase",
                                              letterSpacing: 0.6, fontSize: 10 }}>
            {t("events.nowProcessing")}
          </Typography>
          <Typography sx={{ fontFamily: '"Fira Code", monospace', fontWeight: 600, fontSize: 14,
                            wordBreak: "break-all", mt: 0.25 }}>{currentFile}</Typography>
          {percent !== undefined && (
            <Box sx={{ mt: 1 }}>
              <Stack direction="row" justifyContent="space-between" sx={{ mb: 0.5 }}>
                <Typography variant="caption" color="text.secondary">{t("events.currentFile")}</Typography>
                <Typography variant="caption" sx={{ fontFamily: '"Fira Code", monospace',
                            fontVariantNumeric: "tabular-nums" }}>{Math.round(percent)}%</Typography>
              </Stack>
              <LinearProgress variant="determinate" value={Math.max(0, Math.min(100, percent))}
                sx={{ height: 6, borderRadius: 3,
                      "& .MuiLinearProgress-bar": { bgcolor: TINT.ok, borderRadius: 3 },
                      bgcolor: "rgba(148,163,184,0.15)" }} />
            </Box>
          )}
        </Box>
      )}

      {/* Queue progress */}
      {qTotal !== undefined && qTotal > 0 && qDone !== undefined && (
        <Box>
          <Stack direction="row" justifyContent="space-between" alignItems="baseline" sx={{ mb: 0.5 }}>
            <Typography variant="overline" color="text.secondary">{t("events.queue")}</Typography>
            <Typography sx={{ fontFamily: '"Fira Code", monospace', fontSize: 13,
                              fontVariantNumeric: "tabular-nums" }}>
              {t("events.queueDone", { done: qDone, total: qTotal })}
              {qRem ? t("events.queueLeft", { n: qRem }) : ""}
            </Typography>
          </Stack>
          <LinearProgress variant="determinate" value={(qDone / qTotal) * 100}
            sx={{ height: 10, borderRadius: 5,
                  "& .MuiLinearProgress-bar": { bgcolor: TINT.ok, borderRadius: 5 },
                  bgcolor: "rgba(148,163,184,0.15)" }} />
        </Box>
      )}

      {/* Utilization gauges */}
      {gauges.length > 0 && (
        <Stack direction="row" spacing={2} sx={{ flexWrap: "wrap", justifyContent: "flex-start" }}>
          {gauges.map((g) => (
            <Gauge key={g.label} label={g.label} pct={g.pct}
              sub={g.label === "GPU" && vramUsed !== undefined && vramTotal
                ? `${fmtGB(vramUsed)} / ${fmtGB(vramTotal)}`
                : g.label === "MEM" && ramUsed !== undefined && ramTotal
                  ? `${fmtGB(ramUsed)} / ${fmtGB(ramTotal)}` : undefined} />
          ))}
        </Stack>
      )}

      {/* VRAM bar (when GPU gauge is shown it carries the sub-label; show the bar too) */}
      {vramPct !== undefined && (
        <Box>
          <Stack direction="row" justifyContent="space-between" sx={{ mb: 0.5 }}>
            <Typography variant="overline" color="text.secondary">{t("events.vram")}</Typography>
            <Typography sx={{ fontFamily: '"Fira Code", monospace', fontSize: 13,
                              fontVariantNumeric: "tabular-nums" }}>
              {fmtGB(vramUsed!)} / {fmtGB(vramTotal!)}
            </Typography>
          </Stack>
          <Tooltip title={`${Math.round(vramPct)}% VRAM`}>
            <LinearProgress variant="determinate" value={Math.min(100, vramPct)}
              sx={{ height: 8, borderRadius: 4,
                    "& .MuiLinearProgress-bar": { bgcolor: utilTint(vramPct), borderRadius: 4 },
                    bgcolor: "rgba(148,163,184,0.15)" }} />
          </Tooltip>
        </Box>
      )}

      {/* Stat tiles (fps / ETA / anything else) */}
      {tiles.length > 0 && (
        <Stack direction="row" spacing={1.5} sx={{ flexWrap: "wrap", gap: 1.5 }}>
          {tiles.map((tile) => <Tile key={tile.label} label={tile.label} value={tile.value} />)}
        </Stack>
      )}
    </Stack>
  );
}
