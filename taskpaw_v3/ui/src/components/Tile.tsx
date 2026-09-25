import { Box, Typography } from "@mui/material";

// A labelled value tile (fps, ETA, and any unknown metric) — MonitorMetrics'
// stat tiles and the #189 PipelineProgress step panels.
export function Tile({ label, value }: { label: string; value: string }) {
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
