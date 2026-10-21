// U1c: Jasna's phases before and beside LTX, drawn with the LtxProgress pieces so a film reads the
// same from the scan to the last LTX stage. Shared by the Jasna monitor and the workflow 修复 step.
import { Box, Typography } from "@mui/material";
import { useTranslation } from "react-i18next";
import { ConsoleFold, DATA, MetricGrid, ProgressFrame, ProgressHeadline, ScopeNote } from "./progressParts";
import { readBasicProgress, readScanProgress } from "./restorePhase.helpers";

const fileOf = (metrics: Record<string, unknown>) =>
  typeof metrics.current_file === "string" ? metrics.current_file : undefined;

export function MosaicScanProgress({ metrics }: { metrics: Record<string, unknown> }) {
  const { t } = useTranslation();
  const scan = readScanProgress(metrics);
  if (!scan) return null;
  const file = fileOf(metrics);
  const values = [
    { label: t("jasna.scanRate"), value: scan.rate_value, unit: scan.rate_unit,
      help: scan.rate_unit ? t(`jasna.scanRateHelp.${scan.rate_unit}`) : t("jasna.waitingRate") },
    { label: t("jasna.scanElapsed"), value: scan.elapsed, help: t("jasna.elapsedHelp") },
    { label: t("jasna.scanEta"), value: scan.eta, help: t("jasna.stageEtaHelp") },
  ];
  return (
    <ProgressFrame testId="scan-progress" label={t("jasna.scanTitle")} title={t("jasna.scanTitle")} position={t("jasna.scanPosition")}>
      <Box sx={{ p: { xs: 2, sm: 3 } }}>
        {file && <Typography variant="body2" sx={{ ...DATA, mb: 2, fontSize: 13 }}>{file}</Typography>}
        <ProgressHeadline caption={t("jasna.lastConsoleUpdate")} name="Mosaic scan" label={t("jasna.scanLabel")}
          percent={scan.percent} percentText={scan.percent_text} percentCaption={t("jasna.stageProgress")}
          barLabel={`Mosaic scan · ${t("jasna.scanLabel")}`} current={scan.current} total={scan.total}
          countUnit={t("jasna.countSamples")} countsHelp={t("jasna.scanCounts")} />
        <MetricGrid values={values} />
      </Box>
      <ConsoleFold raw={scan.raw_line} testIdPrefix="scan-console" />
      <ScopeNote>{t("jasna.scanScope")}</ScopeNote>
    </ProgressFrame>
  );
}

// BasicVSR++ `Processing video`: Jasna's own bar, no LTX stage. `raw`: the console line, if known.
export function BasicVsrProgress({ metrics, raw }: { metrics: Record<string, unknown>; raw?: string }) {
  const { t } = useTranslation();
  const basic = readBasicProgress(metrics);
  if (!basic) return null;
  const file = fileOf(metrics);
  const total = basic.processed !== undefined && basic.remaining !== undefined ? basic.processed + basic.remaining : undefined;
  const percentText = String(Math.round(basic.percent));
  const values = [
    { label: t("jasna.basicRate"), value: basic.fps === undefined ? undefined : basic.fps.toFixed(1), unit: "fps",
      help: basic.fps === undefined ? t("jasna.waitingRate") : t("jasna.basicRateHelp") },
    { label: t("jasna.basicElapsed"), value: basic.elapsed, help: t("jasna.basicElapsedHelp") },
    { label: t("jasna.basicEta"), value: basic.eta, help: t("jasna.basicEtaHelp") },
  ];
  return (
    <ProgressFrame testId="basic-progress" label={t("jasna.basicTitle")} title={t("jasna.basicTitle")} position={t("jasna.basicPosition")}>
      <Box sx={{ p: { xs: 2, sm: 3 } }}>
        {file && <Typography variant="body2" sx={{ ...DATA, mb: 2, fontSize: 13 }}>{file}</Typography>}
        <ProgressHeadline caption={t("jasna.lastConsoleUpdate")} name="Processing video" label={t("jasna.basicLabel")}
          percent={basic.percent} percentText={percentText} percentCaption={t("jasna.stageProgress")}
          barLabel={`Processing video · ${t("jasna.basicTitle")}`}
          current={basic.processed === undefined ? undefined : String(basic.processed)}
          total={total === undefined ? undefined : String(total)}
          countUnit={t("jasna.countFrames")} countsHelp={t("jasna.basicCounts")} />
        <MetricGrid values={values} />
      </Box>
      {raw !== undefined && <ConsoleFold raw={raw} testIdPrefix="basic-console" />}
      <ScopeNote>{t("jasna.basicScope")}</ScopeNote>
    </ProgressFrame>
  );
}
