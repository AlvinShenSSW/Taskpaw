import { Box, Chip, Typography } from "@mui/material";
import { alpha } from "@mui/material/styles";
import { useTranslation } from "react-i18next";
import { hasLtxStageOrder, knownLtxStage, LTX_STAGES, readLtxProgress } from "./ltxProgress.helpers";
import { ConsoleFold, DATA, MetricGrid, ProgressFrame, ProgressHeadline, RunEtaRow, ScopeNote } from "./progressParts";

export function LtxProgress({ metrics }: { metrics: Record<string, unknown> }) {
  const { t } = useTranslation();
  const progress = readLtxProgress(metrics);
  if (!progress) return null;
  const known = knownLtxStage(progress);
  const file = typeof metrics.current_file === "string" ? metrics.current_file : undefined;
  const stageName = progress.stage_name;
  const stageLabel = known ? t(`jasna.${known}`) : undefined;
  const countUnit = known === "denoise" ? t("jasna.countSteps") : known ? t("jasna.countFrames") : t("jasna.countNeutral");
  const stagePosition = t("jasna.stagePosition", { stage: progress.stage_index, total: progress.stage_total });
  const rateHelp = progress.rate_unit ? t(`jasna.rateHelp.${progress.rate_unit}`) : t("jasna.waitingRate");
  const values = [
    { label: t("jasna.stageRate"), value: progress.rate_value, unit: progress.rate_unit, help: rateHelp },
    { label: t("jasna.stageElapsed"), value: progress.elapsed, help: t("jasna.elapsedHelp") },
    { label: t("jasna.stageEta"), value: progress.eta, help: t("jasna.stageEtaHelp") },
  ];
  return (
    <ProgressFrame testId="ltx-progress" label={t("jasna.progress")} title={t("jasna.progress")} position={stagePosition}>
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
        <ProgressHeadline caption={t("jasna.lastConsoleUpdate")} name={stageName} label={stageLabel}
          percent={progress.percent} percentText={progress.percent_text} percentCaption={t("jasna.stageProgress")}
          barLabel={`${stageName} · ${t("jasna.stagePercent", { stage: progress.stage_index, total: progress.stage_total })}`}
          current={progress.current} total={progress.total} countUnit={countUnit} countsHelp={t("jasna.stageCounts")} />
        <MetricGrid values={values} />
      </Box>
      <RunEtaRow label={t("jasna.runEtaLabel")} value={progress.run_eta} />
      <ConsoleFold raw={progress.raw_line} />
      <ScopeNote>{t("jasna.progressScope")}</ScopeNote>
    </ProgressFrame>
  );
}
