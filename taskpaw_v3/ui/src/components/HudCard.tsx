import { Card, CardProps } from "@mui/material";

// HUD card (#90): the themed Card (gradient + border from theme.ts) with two
// accent corner ticks (top-left, bottom-right), per the design preview `.card.hud`.
// Static SVG-less detail — no animation, so it's reduced-motion-safe by nature.
// Use it for "instrument panel" surfaces (rails, detail panes, fleet tiles); plain
// <Card> stays available where the ticks would be noise.
export function HudCard({ sx, children, ...rest }: CardProps) {
  return (
    <Card
      {...rest}
      // Array form so caller `sx` keeps working as arrays / theme callbacks
      // (spreading would drop callbacks and turn arrays into numeric keys) (Codex).
      sx={[
        {
          position: "relative",
          // Corner ticks: 12px L-brackets in translucent accent green.
          "&::before, &::after": {
            content: '""',
            position: "absolute",
            width: 12,
            height: 12,
            border: "1.5px solid rgba(34,197,94,.45)",
            pointerEvents: "none",
          },
          "&::before": { top: 8, left: 8, borderRight: 0, borderBottom: 0 },
          "&::after": { bottom: 8, right: 8, borderLeft: 0, borderTop: 0 },
        },
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    >
      {children}
    </Card>
  );
}
