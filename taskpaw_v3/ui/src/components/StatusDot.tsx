import { Box, Tooltip } from "@mui/material";
import { statusColors } from "../theme";

// Live states get the pulse ring + glow (design preview `.dot.live`).
const LIVE_STATES = new Set(["ok", "running", "starting"]);

// Status conveyed by dot + text (never color alone — design §1 a11y): the dot
// carries an aria-label, and callers render the human label beside it. When
// `live`, a pulse ring animates outward; it degrades to a static glow under
// prefers-reduced-motion (#90).
export function StatusDot({ state, live }: { state: string; live?: boolean }) {
  const color = statusColors[state] ?? statusColors.unknown;
  const isLive = live ?? LIVE_STATES.has(state);
  return (
    <Tooltip title={state}>
      <Box
        component="span"
        role="img"
        aria-label={`status: ${state}`}
        sx={{
          position: "relative",
          display: "inline-block",
          width: 10,
          height: 10,
          borderRadius: "50%",
          bgcolor: color,
          color, // drives currentColor on the glow + pulse ring
          boxShadow: "0 0 8px 1px currentColor",
          mr: 1,
          flex: "0 0 auto",
          // Pulse ring (design preview `.dot.live::after` + @keyframes pulse).
          "&::after": {
            content: '""',
            position: "absolute",
            inset: "-4px",
            borderRadius: "50%",
            border: "2px solid currentColor",
            opacity: 0,
            animation: isLive ? "tp-status-pulse 1.8s ease-out infinite" : "none",
          },
          "@keyframes tp-status-pulse": {
            "0%": { transform: "scale(0.6)", opacity: 0.55 },
            "100%": { transform: "scale(1.5)", opacity: 0 },
          },
          // Reduced motion: drop the pulse, keep the static color + glow.
          "@media (prefers-reduced-motion: reduce)": {
            "&::after": { animation: "none" },
          },
        }}
      />
    </Tooltip>
  );
}
