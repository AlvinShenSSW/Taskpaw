// U1c: the drawer's running 修复 step. A heartbeat (已用, ticking from the stage start, plus the
// latest console line) above the phase's card: the mosaic scan, BasicVSR++, then the Jasna
// monitor's own LtxProgress, unchanged.
import { Box, Typography } from "@mui/material";
import type { RestoreParams, WorkflowStep } from "../../api";
import { LtxProgress } from "../LtxProgress";
import { ConsoleFold, DATA, ProgressFrame } from "../progressParts";
import { BasicVsrProgress, MosaicScanProgress } from "../RestorePhaseProgress";
import { heartbeatText, paramRows, presetText, restoreLive, SPLIT_LABEL, SPLIT_NOTE, type RestoreLive } from "./mediaModel";
import { useNow } from "./useNow";

function phaseLabel(live: RestoreLive): string {
  switch (live.phase) {
    case "starting": return "正在启动";
    case "working": return "等待进度条";
    case "split": return live.split ? SPLIT_LABEL[live.split.kind] : "自动分段";
    case "scan": return "扫描马赛克";
    case "basic": return "BasicVSR++ 修复";
    case "ltx": return live.ltx ? `LTX ${live.ltx.stage_index}/${live.ltx.stage_total} ${live.ltx.stage_name}` : "LTX";
  }
}

const PULSE = {
  width: 9, height: 9, borderRadius: "50%", bgcolor: "success.main", flexShrink: 0,
  "@keyframes restore-beat": { "0%, 100%": { opacity: 1 }, "50%": { opacity: 0.35 } },
  animation: "restore-beat 1.6s ease-in-out infinite",
  "@media (prefers-reduced-motion: reduce)": { animation: "none" },
} as const;

// 「本片参数」: what Jasna was launched with for this video, and the full command to copy.
function ParamsBlock({ params }: { params: RestoreParams }) {
  return (
    <ProgressFrame testId="restore-params" label="本片参数" title="本片参数" position={presetText(params) || undefined}>
      <Box component="dl" sx={{ m: 0, px: { xs: 2, sm: 3 }, py: 2, display: "flex", flexWrap: "wrap", gap: 1 }}>
        {paramRows(params).map(([label, value]) => (
          <Box key={label} data-param={label} sx={{ display: "inline-flex", alignItems: "baseline", gap: 0.75, minWidth: 0,
            maxWidth: "100%", px: 1.25, py: 0.5, border: "1px solid", borderColor: "divider", borderRadius: 1.5,
            bgcolor: "background.default" }}>
            <Typography component="dt" variant="caption" color="text.secondary">{label}</Typography>
            <Typography component="dd" sx={{ ...DATA, m: 0, fontSize: 12 }}>{value}</Typography>
          </Box>
        ))}
      </Box>
      <ConsoleFold raw={params.argv} title="完整命令" help="启动 Jasna 的完整命令（路径只保留文件名）" testIdPrefix="restore-argv" />
    </ProgressFrame>
  );
}

export function RestoreLivePanel({ step }: { step: WorkflowStep }) {
  const live = restoreLive(step);
  const now = useNow(live !== null && live.startedAt !== null);
  if (!live) return null;
  const beat = heartbeatText(live, now);
  const raw = live.ltx?.raw_line ?? live.scan?.raw_line;
  const showConsole = live.console !== null && live.console !== raw;
  return (
    <Box sx={{ display: "grid", gap: 2, minWidth: 0 }} data-testid="restore-live">
      <ProgressFrame testId="restore-heartbeat" label="修复进度"
        title={<Box component="span" sx={{ display: "inline-flex", alignItems: "center", gap: 1.25 }}>
          <Box component="span" aria-hidden sx={PULSE} />
          <Box component="span" sx={DATA}>{beat}</Box>
        </Box>}
        position={phaseLabel(live)}>
        {live.phase === "starting" && (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", px: { xs: 2, sm: 3 }, py: 1.5 }}>
            Jasna 正在加载模型；第一行输出出现后，这里显示当前阶段。
          </Typography>
        )}
        {live.split && live.phase !== "split" && (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", px: { xs: 2, sm: 3 }, py: 1.5 }}>
            {SPLIT_NOTE[live.split.kind]}
          </Typography>
        )}
        {showConsole && (
          <ConsoleFold raw={live.console ?? undefined} title="最新输出" help="Jasna 控制台最近一行（纯文本）"
            testIdPrefix="restore-console" />
        )}
      </ProgressFrame>
      {live.params && <ParamsBlock params={live.params} />}
      {live.phase === "ltx" && <LtxProgress metrics={live.metrics} />}
      {live.phase === "scan" && <MosaicScanProgress metrics={live.metrics} />}
      {live.phase === "basic" && <BasicVsrProgress metrics={live.metrics} />}
    </Box>
  );
}
