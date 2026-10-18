import { Alert, Box, Button, LinearProgress, Stack, Typography } from "@mui/material";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api";

export function LtxDownload({ value, name, disabled, onChanged, onError }: {
  value: unknown; name?: string; disabled?: boolean; onChanged?: () => void; onError?: (e: unknown) => void;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [submitted, setSubmitted] = useState<string>();
  if (!value || typeof value !== "object") return null;
  const d = value as Record<string, unknown>;
  if (typeof d.state !== "string" || typeof d.request_id !== "string" || !["awaiting", "approved", "downloading", "paused", "error"].includes(d.state)) return null;
  const requestId = d.request_id;
  const respond = async (approved: boolean) => {
    if (!name || busy || submitted === requestId) return;
    setBusy(true);
    try {
      await api.respondPrompt(name, requestId, approved);
      setSubmitted(requestId);
      onChanged?.();
    } catch (e) { onError?.(e); } finally { setBusy(false); }
  };
  const downloading = d.state === "downloading" || d.state === "approved";
  const bytes = (v: unknown) => typeof v === "number" && Number.isFinite(v) && v >= 0 ? v : 0;
  const total = bytes(d.total_bytes), done = Math.min(total, bytes(d.downloaded_bytes));
  return <Box sx={{ mt: 2, p: 2, border: "1px solid", borderColor: "divider", borderRadius: 2 }}>
    <Typography fontWeight={600}>{t("jasna.downloadTitle")}</Typography>
    <Typography variant="body2" sx={{ my: 1 }}>{t(`jasna.download_${d.state}`)}</Typography>
    <Typography variant="body2">{String(d.variant ?? "LTX")} · {(done / 1e9).toFixed(2)} / {(total / 1e9).toFixed(2)} GB</Typography>
    <Typography variant="caption" sx={{ display: "block", overflowWrap: "anywhere" }}>{typeof d.destination === "string" ? d.destination : ""}</Typography>
    <Typography variant="caption" sx={{ display: "block", overflowWrap: "anywhere" }}>{Array.isArray(d.files) ? d.files.filter(f => typeof f === "string").join(" · ") : ""}</Typography>
    {typeof d.error === "string" && <Alert severity="error" sx={{ mt: 1 }}>{d.error}</Alert>}
    {downloading && <LinearProgress aria-label={t("jasna.downloadTitle")} variant={total ? "determinate" : "indeterminate"} value={total ? done / total * 100 : undefined} sx={{ mt: 1 }} />}
    {name && !downloading && <Stack direction="row" spacing={1} sx={{ mt: 1.5 }}>
      <Button variant="contained" disabled={disabled || busy || submitted === requestId} onClick={() => void respond(true)}>{t(d.state === "error" ? "jasna.retryDownload" : "jasna.download")}</Button>
      {d.state !== "paused" && <Button disabled={disabled || busy || submitted === requestId} onClick={() => void respond(false)}>{t("jasna.notNow")}</Button>}
    </Stack>}
    <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>{t("jasna.downloadHelp")}</Typography>
  </Box>;
}
