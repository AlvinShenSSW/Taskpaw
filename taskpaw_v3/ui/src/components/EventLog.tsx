import { Box, Chip, Stack, Typography } from "@mui/material";
import type { EventItem } from "../api";

// Shared event-log renderer (#44): a dense, newest-first list the operator can
// scan without reading files. Used by the Agent Console (local events) and the
// Hub Dashboard (aggregated history). Tolerates both event shapes.

const LEVEL_COLOR: Record<string, "default" | "info" | "success" | "warning" | "error"> = {
  info: "info",
  done: "success",
  warn: "warning",
  alert: "error",
};

function fmtTime(iso?: string): string {
  if (!iso) return "";
  // Show HH:MM:SS (tabular); fall back to the raw string if it isn't parseable.
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleTimeString();
}

export function EventLog({ events }: { events?: EventItem[] }) {
  const rows = events ?? [];
  if (rows.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>
        No events yet — they appear here as monitors report activity.
      </Typography>
    );
  }
  return (
    <Stack divider={<Box sx={{ borderBottom: 1, borderColor: "divider" }} />} sx={{ mt: 1 }}>
      {rows.map((e, i) => {
        const level = (e.level ?? "info").toLowerCase();
        const where = e.server ?? e.machine; // hub: server name; agent: machine
        // Globally-unique key: a Hub event_id is unique only WITH its server, so
        // compose server_id:event_id (agent events use their monotonic id) (Codex).
        const key = `${e.server_id ?? ""}:${e.event_id ?? e.id ?? i}`;
        return (
          <Stack key={key} direction="row" spacing={1.5}
            alignItems="baseline" sx={{ py: 0.75 }}>
            <Typography sx={{ fontFamily: '"Fira Code", monospace', fontSize: 12,
                              color: "text.secondary", fontVariantNumeric: "tabular-nums",
                              minWidth: 72 }}>
              {fmtTime(e.time ?? e.received_at)}
            </Typography>
            <Chip size="small" label={level} color={LEVEL_COLOR[level] ?? "default"}
              variant="outlined" sx={{ minWidth: 64, textTransform: "lowercase" }} />
            <Box sx={{ minWidth: 0, flex: 1 }}>
              <Typography variant="body2" sx={{ wordBreak: "break-word" }}>{e.message}</Typography>
              <Typography variant="caption" color="text.secondary">
                {[where, e.monitor].filter(Boolean).join(" · ")}
              </Typography>
            </Box>
          </Stack>
        );
      })}
    </Stack>
  );
}
