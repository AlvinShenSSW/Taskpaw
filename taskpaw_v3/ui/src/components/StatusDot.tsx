import { Box, Tooltip } from "@mui/material";
import { statusColors } from "../theme";

// Status conveyed by dot + text (never color alone — design §1 a11y).
export function StatusDot({ state }: { state: string }) {
  const color = statusColors[state] ?? statusColors.unknown;
  return (
    <Tooltip title={state}>
      <Box
        component="span"
        aria-label={`status: ${state}`}
        sx={{
          display: "inline-block",
          width: 10,
          height: 10,
          borderRadius: "50%",
          bgcolor: color,
          mr: 1,
          flex: "0 0 auto",
        }}
      />
    </Tooltip>
  );
}
