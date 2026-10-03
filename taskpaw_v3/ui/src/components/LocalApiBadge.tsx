import { useCallback, useSyncExternalStore } from "react";
import { Box, Typography } from "@mui/material";
import { useQueryClient } from "@tanstack/react-query";
import { useTranslation } from "react-i18next";
import type { ControlRole } from "../api";
import { StatusDot } from "./StatusDot";
import { requestFailed, requestTime, useResponseOutdated } from "./requestEvidence";

// The role view owns the status query. This subscription adds no fetch owner.
export function LocalApiBadge({ role, ready }: { role: ControlRole; ready: boolean }) {
  const qc = useQueryClient();
  const { t, i18n } = useTranslation();
  const key = role === "agent" ? "agentStatus" : "hubStatus";
  const subscribe = useCallback((notify: () => void) => qc.getQueryCache().subscribe(event => {
    if (event.query.queryKey.length === 1 && event.query.queryKey[0] === key) notify();
  }), [qc, key]);
  const snapshot = useCallback(() => ready ? qc.getQueryState([key]) : undefined, [qc, key, ready]);
  const query = useSyncExternalStore(subscribe, snapshot);
  const lastSuccessAt = query?.data !== undefined ? query.dataUpdatedAt : 0;
  const outdated = useResponseOutdated(lastSuccessAt);
  const failed = query && requestFailed(query.fetchFailureCount, query.errorUpdatedAt, query.dataUpdatedAt, query.error);
  const label = !ready ? "credentials" : failed ? "failed" : outdated ? "outdated" : lastSuccessAt ? "connected" : "checking";
  const state = label === "connected" ? "ok" : label === "failed" ? "error" : "unknown";
  return (
    <Box role="status" aria-label={t("connection.title")} sx={{ display: "inline-flex", alignItems: "center", gap: 0.5 }}>
      <StatusDot state={state} live={label === "connected"} />
      <Box>
        <Typography variant="body2">{t("connection.title")}: {t(`connection.${label}`)}</Typography>
        {ready && query?.fetchStatus === "fetching" && lastSuccessAt > 0 && (
          <Typography variant="caption" sx={{ display: "block" }}>{t("common.updating")}</Typography>
        )}
        {lastSuccessAt > 0 && (
          <Typography variant="caption" sx={{ display: "block" }}>
            {t("connection.lastSuccess", { time: requestTime(lastSuccessAt, i18n.language) })}
          </Typography>
        )}
      </Box>
    </Box>
  );
}
