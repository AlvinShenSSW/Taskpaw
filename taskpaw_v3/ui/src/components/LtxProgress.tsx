import { useId, useState } from "react";
import { Box, Button, Chip, LinearProgress, Stack, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import ScheduleIcon from "@mui/icons-material/Schedule";
import { useTranslation } from "react-i18next";
import { hasLtxStageOrder, knownLtxStage, LTX_STAGES, readLtxProgress } from "./ltxProgress.helpers";

const MONO = '"Fira Code","Noto Sans SC",ui-monospace,monospace';
const DATA = { fontFamily: MONO, fontVariantNumeric: "tabular-nums", overflowWrap: "anywhere" } as const;

export function LtxProgress({ metrics }: { metrics: Record<string, unknown> }) {
  const { t } = useTranslation();
  const rawId = useId();
  const [copy, setCopy] = useState<{ line: string; result: "ok" | "failed" }>();
  const progress = readLtxProgress(metrics);
  if (!progress) return null;
  const known = knownLtxStage(progress);
  const file = typeof metrics.current_file === "string" ? metrics.current_file : undefined;
  const stageName = progress.stage_name;
  const stageLabel = known ? t(`jasna.${known}`) : undefined;
  const countUnit = known === "denoise" ? t("jasna.countSteps") : known ? t("jasna.countFrames") : t("jasna.countNeutral");
  const stagePosition = t("jasna.stagePosition", { stage: progress.stage_index, total: progress.stage_total });
  const raw = progress.raw_line;
  const feedback = copy && copy.line === raw ? copy.result : undefined;
  const rateHelp = progress.rate_unit ? t(`jasna.rateHelp.${progress.rate_unit}`) : t("jasna.waitingRate");
  const copyRaw = async () => {
    if (!raw) return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(raw);
      setCopy({ line: raw, result: "ok" });
    } catch {
      setCopy({ line: raw, result: "failed" });
    }
  };
  const values = [
    { label: t("jasna.stageRate"), value: progress.rate_value, unit: progress.rate_unit, help: rateHelp },
    { label: t("jasna.stageElapsed"), value: progress.elapsed, help: t("jasna.elapsedHelp") },
    { label: t("jasna.stageEta"), value: progress.eta, help: t("jasna.stageEtaHelp") },
  ];
  return (
    <Box component="section" data-testid="ltx-progress" aria-label={t("jasna.progress")}
      sx={{ border: "1px solid", borderColor: "divider", borderRadius: 2, minWidth: 0, overflow: "hidden",
        bgcolor: "background.paper" }}>
      <Stack direction="row" justifyContent="space-between" alignItems="center"
        sx={{ px: { xs: 2, sm: 3 }, py: 2, gap: 1, flexWrap: "wrap", borderBottom: "1px solid", borderColor: "divider" }}>
        <Typography fontWeight={600} sx={{ fontSize: 14 }}>{t("jasna.progress")}</Typography>
        <Typography variant="caption" color="text.secondary" sx={DATA}>{stagePosition}</Typography>
      </Stack>
      <Box sx={{ p: { xs: 2, sm: 3 } }}>
        {file && <Typography variant="body2" sx={{ ...DATA, mb: 2, fontSize: 13 }}>{file}</Typography>}
        {progress.mode === "speed_test" && (
          <Box sx={{ mb: 2, p: 1.5, borderRadius: 1, bgcolor: theme => alpha(theme.palette.info.main, 0.08) }}>
            <Chip size="small" color="info" variant="outlined" label={t("jasna.speedTest")} />
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.75 }}>{t("jasna.speedTestHelp")}</Typography>
          </Box>
        )}
        {hasLtxStageOrder(progress) && (
          <Box component="ol" aria-label={t("jasna.stageOrder")} sx={{ m: 0, mb: 3, p: 0, listStyle: "none",
            display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: { xs: 0.5, sm: 1 } }}>
            {LTX_STAGES.map((stage, index) => {
              const active = index + 1 === progress.stage_index;
              return <Box component="li" key={stage} aria-current={active ? "step" : undefined}
                sx={{ display: "flex", alignItems: "center", flexDirection: { xs: "column", sm: "row" },
                  gap: 1, p: { xs: 1, sm: 1.5 }, minWidth: 0, border: "1px solid", borderRadius: 1.5,
                  borderColor: active ? theme => alpha(theme.palette.success.main, 0.4) : "transparent",
                  bgcolor: active ? theme => alpha(theme.palette.success.main, 0.07) : "transparent",
                  color: active ? "success.light" : "text.secondary" }}>
                <Box component="span" aria-hidden sx={{ ...DATA, width: 25, height: 25, flexShrink: 0,
                  display: "grid", placeItems: "center", border: "1px solid", borderColor: active ? "success.main" : "divider",
                  borderRadius: "50%", fontSize: 11 }}>{String(index + 1).padStart(2, "0")}</Box>
                <Box sx={{ minWidth: 0, textAlign: { xs: "center", sm: "left" } }}>
                  <Typography sx={{ ...DATA, fontSize: { xs: 10, sm: 12 } }}>{stage}</Typography>
                  <Typography variant="caption" sx={{ display: "block", fontSize: { xs: 10, sm: 11 }, overflowWrap: "anywhere" }}>{t(`jasna.${stage}`)}</Typography>
                </Box>
              </Box>;
            })}
          </Box>
        )}
        <Stack direction="row" justifyContent="space-between" alignItems="flex-end" sx={{ gap: 2 }}>
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.75 }}>{t("jasna.lastConsoleUpdate")}</Typography>
            <Typography component="h3" sx={{ ...DATA, fontSize: { xs: 25, sm: 31 }, fontWeight: 500, lineHeight: 1.3 }}>
              {stageName}{stageLabel && <Box component="span" sx={{ display: { xs: "block", sm: "inline" },
                ml: { xs: 0, sm: 1.5 }, fontFamily: theme => theme.typography.fontFamily, fontSize: { xs: 14, sm: 18 }, color: "text.secondary" }}> {stageLabel}</Box>}
            </Typography>
          </Box>
          <Box sx={{ textAlign: "right", flexShrink: 0, maxWidth: "50%", minWidth: 0 }}>
            <Typography sx={{ ...DATA, fontSize: { xs: 38, sm: 48 }, lineHeight: 1.1, color: "success.light" }}>
              {progress.percent_text === undefined ? "—" : `${progress.percent_text}%`}
            </Typography>
            <Typography variant="caption" color="text.secondary">{t("jasna.stageProgress")}</Typography>
          </Box>
        </Stack>
        <LinearProgress aria-label={`${stageName} · ${t("jasna.stagePercent", { stage: progress.stage_index, total: progress.stage_total })}`}
          variant={progress.percent === undefined ? "indeterminate" : "determinate"} value={progress.percent}
          sx={{ mt: 2.5, height: 10, borderRadius: 5, bgcolor: theme => alpha(theme.palette.text.secondary, 0.15),
            "& .MuiLinearProgress-bar": { borderRadius: 5 },
            "@media (prefers-reduced-motion: reduce)": { "&, & *": { animation: "none", transition: "none" } } }} />
        <Stack direction="row" justifyContent="space-between" sx={{ gap: 1, flexWrap: "wrap", mt: 1.25 }}>
          <Typography variant="caption" color="text.secondary">
            <Box component="span" sx={{ ...DATA, color: "text.primary" }}>{progress.current ?? "—"} / {progress.total ?? "—"}</Box> {countUnit}
          </Typography>
          <Typography variant="caption" color="text.secondary">{t("jasna.stageCounts")}</Typography>
        </Stack>
        <Box sx={{ display: "grid", gridTemplateColumns: { xs: "repeat(2, minmax(0, 1fr))", sm: "1.2fr 1fr 1fr" },
          gap: { xs: 2, sm: 3 }, mt: 3, pt: 2.5, borderTop: "1px solid", borderColor: "divider" }}>
          {values.map((metric, index) => <Box key={metric.label} sx={{ minWidth: 0,
            gridColumn: { xs: index === 0 ? "1 / -1" : "auto", sm: "auto" },
            borderLeft: { xs: index === 2 ? "1px solid" : "none", sm: index > 0 ? "1px solid" : "none" },
            borderColor: "divider", pl: { xs: index === 2 ? 2 : 0, sm: index > 0 ? 3 : 0 } }}>
            <Typography variant="caption" color="text.secondary">{metric.label}</Typography>
            <Typography sx={{ ...DATA, mt: 0.5, fontSize: { xs: 24, sm: 27 }, lineHeight: 1.4 }}>
              {metric.value ?? "—"}{metric.unit && <Box component="span" sx={{ ml: 0.75, fontSize: 14, color: "text.secondary" }}> {metric.unit}</Box>}
            </Typography>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>{metric.help}</Typography>
          </Box>)}
        </Box>
      </Box>
      <Box sx={{ borderTop: "1px solid", borderColor: "divider", px: { xs: 2, sm: 3 }, py: 2,
        display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 1.5,
        bgcolor: theme => alpha(theme.palette.background.default, 0.4) }}>
        <Typography variant="caption" color="text.secondary" sx={{ display: "flex", alignItems: "center", gap: 1 }}>
          <ScheduleIcon aria-hidden sx={{ color: "info.main", fontSize: 17 }} />{t("jasna.runEtaLabel")}
        </Typography>
        <Typography sx={{ ...DATA, fontSize: 22, color: "info.light" }}>{progress.run_eta ?? "—"}</Typography>
      </Box>
      <Box component="details" sx={{ borderTop: "1px solid", borderColor: "divider",
        "&[open] .ltx-disclosure-icon": { transform: "rotate(180deg)" } }}>
        <Box component="summary" data-testid="ltx-console-toggle" aria-controls={rawId}
          sx={{ minHeight: 48, px: { xs: 2, sm: 3 }, py: 1.5, display: "flex", alignItems: "center",
            justifyContent: "space-between", cursor: "pointer", listStyle: "none", fontSize: 12,
            "&::-webkit-details-marker": { display: "none" },
            "&:focus-visible": { outline: "2px solid", outlineColor: "info.main", outlineOffset: -3 } }}>
          {t("jasna.consoleTitle")}<ExpandMoreIcon className="ltx-disclosure-icon" aria-hidden sx={{ fontSize: 20 }} />
        </Box>
        <Box sx={{ px: { xs: 2, sm: 3 }, pb: 2 }}>
          <Box component="pre" id={rawId} data-testid="ltx-console-raw" tabIndex={0} aria-label={t("jasna.consoleTitle")}
            sx={{ ...DATA, m: 0, p: 1.5, fontSize: 11, whiteSpace: "pre-wrap", wordBreak: "break-word", userSelect: "text",
              bgcolor: "background.default", border: "1px solid", borderColor: "divider", borderRadius: 1,
              "&:focus-visible": { outline: "2px solid", outlineColor: "info.main" } }}>{raw ?? "—"}</Box>
          <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ gap: 1, flexWrap: "wrap", mt: 1 }}>
            <Typography variant="caption" color="text.secondary">{raw ? t("jasna.consoleHelp") : t("jasna.consoleUnavailable")}</Typography>
            <Button size="small" startIcon={<ContentCopyIcon />} disabled={!raw} onClick={() => void copyRaw()}
              sx={{ minHeight: 40, fontSize: 12 }}>{t("jasna.copyLine")}</Button>
          </Stack>
          {feedback && <Typography role="status" variant="caption" color={feedback === "ok" ? "success.main" : "text.secondary"}>
            {t(feedback === "ok" ? "jasna.copyOk" : "jasna.copyFailed")}
          </Typography>}
        </Box>
      </Box>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", px: { xs: 2, sm: 3 }, pb: 2 }}>
        {t("jasna.progressScope")}
      </Typography>
    </Box>
  );
}
