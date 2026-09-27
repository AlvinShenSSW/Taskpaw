import { Alert, Box, Button, Stack, Typography } from "@mui/material";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type FfmpegStatus } from "../api";
import { ffmpegState } from "./FfmpegReminder.helpers";

export function FfmpegReminder({ whisperjav = "" }: { whisperjav?: string }) {
  const { t } = useTranslation();
  const [status, setStatus] = useState<FfmpegStatus | null>(null);
  const [failed, setFailed] = useState(false);
  const [copy, setCopy] = useState<"copy" | "copied" | "copyFailed">("copy");

  useEffect(() => {
    let current = true;
    setStatus(null);
    setFailed(false);
    setCopy("copy");
    const timer = window.setTimeout(() => {
      api.ffmpeg(whisperjav).then(
        (result) => { if (current) setStatus(result); },
        () => { if (current) setFailed(true); },
      );
    }, 400);
    return () => {
      current = false;
      window.clearTimeout(timer);
    };
  }, [whisperjav]);

  const state = failed ? "error" : status ? ffmpegState(status) : "checking";
  const neutral = state === "neutral" || state === "error" || state === "checking";
  const severity = state === "path" || state === "bundled" ? "success"
    : state === "windows" || state === "other" ? "warning" : "info";
  const copyScript = async () => {
    try {
      await navigator.clipboard.writeText(status!.script!);
      setCopy("copied");
    } catch {
      setCopy("copyFailed");
    }
  };

  return (
    <Alert severity={severity} role={state === "windows" ? "alert" : "status"}
      sx={{ mt: 2, minWidth: 0, maxWidth: "100%", overflowWrap: "anywhere",
        "& .MuiAlert-message": { minWidth: 0, width: "100%" },
        ...(neutral ? { bgcolor: "action.hover", color: "text.secondary",
          "& .MuiAlert-icon": { color: "text.secondary" } } : {}) }}>
      <Stack spacing={1.5} sx={{ minWidth: 0 }}>
        <Typography variant="body2">{t(`ffmpeg.${state}`, { path: status?.effective })}</Typography>
        {state === "error" && <Typography variant="body2">{t("ffmpeg.readme")}</Typography>}
        {state === "windows" && <>
          {status?.candidates.filter((candidate) => candidate.exists).map((candidate) => (
            <Typography key={candidate.dir} variant="body2">{t("ffmpeg.candidate", { dir: candidate.dir })}</Typography>
          ))}
          {status?.script && <>
            <Typography variant="body2">{t("ffmpeg.instructions")}</Typography>
            <Box component="pre" role="region" aria-label={t("ffmpeg.scriptLabel")} tabIndex={0}
              sx={{ m: 0, p: 1.5, minWidth: 0, maxWidth: "100%", maxHeight: 240,
                overflowX: "auto", overflowY: "auto", whiteSpace: "pre",
                fontFamily: '"Fira Code", ui-monospace, monospace', fontSize: 12,
                bgcolor: "background.default", color: "text.primary", borderRadius: 1,
                "&:focus-visible": { outline: "2px solid", outlineColor: "primary.main" } }}>
              {status.script}
            </Box>
            <Button type="button" variant="outlined" onClick={copyScript}
              sx={{ minHeight: 40, alignSelf: "flex-start", maxWidth: "100%", cursor: "pointer",
                transition: "background-color 200ms, border-color 200ms",
                "&:focus-visible": { outline: "2px solid", outlineColor: "primary.main", outlineOffset: 2 },
                "@media (prefers-reduced-motion: reduce)": { transition: "none" } }}>
              {t(`ffmpeg.${copy === "copyFailed" ? "copy" : copy}`)}
            </Button>
            {copy === "copyFailed" && <Typography variant="body2">{t("ffmpeg.copyFailed")}</Typography>}
            <Typography variant="body2">{t("ffmpeg.restartInstructions")}</Typography>
          </>}
        </>}
      </Stack>
    </Alert>
  );
}
