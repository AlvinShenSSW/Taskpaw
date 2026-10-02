import { Alert, Box, Button, Chip, Stack, Typography } from "@mui/material";
import { useTranslation } from "react-i18next";
import type { EventItem } from "../api";
import { requestTime, useResponseOutdated } from "./requestEvidence";

// Shared event-log renderer (#44): a dense, newest-first list the operator can
// scan without reading files. HubDashboard owns the query and passes explicit
// request evidence. The row renderer tolerates both event shapes.

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

export function EventLog({ events, hasData, fetching = false, failed = false, lastSuccessAt = 0, onRetry }: {
  events?: EventItem[];
  hasData: boolean;
  fetching?: boolean;
  failed?: boolean;
  lastSuccessAt?: number;
  onRetry?: () => void;
}) {
  const { t, i18n } = useTranslation();
  const outdated = useResponseOutdated(lastSuccessAt);
  const rows = events ?? [];
  return (
    <Stack spacing={1}>
      {failed && <Alert severity="error" action={onRetry && (
        <Button color="inherit" disabled={fetching} onClick={onRetry}>{t("events.retry")}</Button>
      )}>{t("events.failed")}</Alert>}
      <Box role="status" aria-label={t("events.requestStatus")}>
        {fetching && <Typography variant="body2">{t(hasData ? "common.updating" : "events.loading")}</Typography>}
        {hasData && (failed || outdated) && <Typography variant="body2">{t("events.stale")}</Typography>}
        {hasData && lastSuccessAt > 0 && <Typography variant="caption">
          {t("connection.lastSuccess", { time: requestTime(lastSuccessAt, i18n.language) })}
        </Typography>}
      </Box>
      {hasData && rows.length === 0 && !failed && !outdated && (
        <Typography variant="body2" color="text.secondary" sx={{ p: 2 }}>{t("events.none")}</Typography>
      )}
      {hasData && rows.length > 0 && (
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
      )}
    </Stack>
  );
}
