import { Box, LinearProgress, Stack, Step, StepLabel, Stepper, Typography } from "@mui/material";
import { useTranslation } from "react-i18next";
import { LTX_STAGES, ltxStage } from "./ltxProgress.helpers";

export function LtxProgress({ metrics }: { metrics: Record<string, unknown> }) {
  const { t } = useTranslation();
  const stage = ltxStage(metrics);
  if (stage === null) return null;
  const percent = typeof metrics.percent === "number" && Number.isFinite(metrics.percent)
    ? Math.max(0, Math.min(100, metrics.percent)) : undefined;
  return <Box data-testid="ltx-progress" sx={{ p: 2, border: "1px solid", borderColor: "divider", borderRadius: 2 }}>
    <Typography fontWeight={600}>{t("jasna.progress")}</Typography>
    {typeof metrics.current_file === "string" && <Typography variant="body2" sx={{ overflowWrap: "anywhere", mt: 1 }}>{metrics.current_file}</Typography>}
    <Stepper activeStep={stage} alternativeLabel sx={{ my: 2 }}>
      {LTX_STAGES.map(s => <Step key={s}><StepLabel>{t(`jasna.${s}`)}</StepLabel></Step>)}
    </Stepper>
    <Stack direction="row" justifyContent="space-between" spacing={1}>
      <Typography variant="body2">{t("jasna.stagePercent", { stage: stage + 1 })}</Typography>
      {percent !== undefined && <Typography variant="body2">{Math.round(percent)}%</Typography>}
    </Stack>
    <LinearProgress aria-label={t("jasna.stagePercent", { stage: stage + 1 })} variant={percent === undefined ? "indeterminate" : "determinate"} value={percent} sx={{ mt: 1, height: 6, borderRadius: 3 }} />
    <Stack direction="row" spacing={2} sx={{ mt: 1 }}>
      {typeof metrics.fps === "number" && Number.isFinite(metrics.fps) && <Typography variant="caption">{t("jasna.stageFps", { fps: metrics.fps.toFixed(1) })}</Typography>}
      {typeof metrics.eta === "string" && <Typography variant="caption">{t("jasna.runEta", { eta: metrics.eta })}</Typography>}
    </Stack>
  </Box>;
}
