import { Box, LinearProgress, Typography } from "@mui/material";
import type { WorkflowStep } from "../../api";
import { ConsoleFold, DATA, ProgressFrame } from "../progressParts";
import { asrEta, asrLive, elapsedClock } from "./mediaModel";
import { useNow } from "./useNow";

export function AsrLivePanel({ step, machine }: { step: WorkflowStep; machine: string }) {
  const live = asrLive(step);
  const now = useNow(live !== null && live.startedAt !== null);
  if (!live) return null;
  const elapsed = live.startedAt !== null ? elapsedClock(now - live.startedAt)
    : live.elapsed_s == null ? "—" : elapsedClock(live.elapsed_s * 1000);
  const eta = asrEta(live.eta_s) ?? "—";
  return (
    <ProgressFrame testId="asr-live" label="识别进度"
      title={<Box component="span" sx={{ display: "inline-flex", alignItems: "center", gap: 1.25 }}>
        <Box component="span" aria-hidden sx={{ width: 9, height: 9, borderRadius: "50%", bgcolor: "success.main",
          "@keyframes asr-beat": { "0%, 100%": { opacity: 1 }, "50%": { opacity: 0.35 } },
          animation: "asr-beat 1.6s ease-in-out infinite", "@media (prefers-reduced-motion: reduce)": { animation: "none" } }} />
        <Box component="span" sx={DATA}>已用 {elapsed}</Box>
      </Box>}
      position={live.phase == null ? undefined : `第 ${live.phase}/${live.phase_n} 阶段 · ${live.phase_label}`}>
      {live.phase != null && <Box sx={{ px: { xs: 2, sm: 3 }, py: 2.5 }}>
        <Box sx={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 2 }}>
          <Typography sx={{ ...DATA, fontSize: 48, color: "success.light", lineHeight: 1.1 }}>{live.percent}%</Typography>
          <Typography variant="caption" color="text.secondary">预计剩余 {eta}</Typography>
        </Box>
        <LinearProgress aria-label="识别进度" variant="determinate" value={live.percent ?? 0}
          sx={{ mt: 2, height: 10, borderRadius: 5 }} />
        <Box component="ul" aria-label="识别数据" sx={{ listStyle: "none", p: 0, my: 2.5, display: "grid",
          gridTemplateColumns: { xs: "repeat(2, minmax(0, 1fr))", sm: "repeat(4, minmax(0, 1fr))" }, gap: 1 }}>
          {[["场景", live.scene == null ? "—" : `${live.scene} / ${live.scenes}`], ["已用", elapsed],
            ["预计剩余", eta], ["设备", machine]].map(([label, value]) => (
            <Box component="li" key={label} sx={{ p: 1.25, border: "1px solid", borderColor: "divider", borderRadius: 1.5 }}>
              <Typography variant="caption" color="text.secondary">{label}</Typography>
              <Typography sx={{ ...DATA, mt: 0.5, fontSize: 13 }}>{value}</Typography>
            </Box>
          ))}
        </Box>
        <Box component="ol" aria-label="识别阶段" sx={{ listStyle: "none", p: 0, m: 0, display: "grid",
          gridTemplateColumns: "repeat(2, minmax(0, 1fr))", gap: 1.5 }}>
          {live.phases?.map((phase) => (
            <Box component="li" key={phase.n} aria-current={phase.state === "current" ? "step" : undefined}
              sx={{ color: phase.state === "current" ? "success.light" : "text.secondary", minWidth: 0 }}>
              <Typography sx={{ fontSize: 13 }}>{phase.state === "done" ? "✓" : phase.state === "current" ? "●" : "○"} {phase.label}</Typography>
              <Typography variant="caption" sx={DATA}>
                {phase.seconds == null ? "—" : elapsedClock(phase.seconds * 1000)}
                {phase.state === "current" && live.scene != null ? ` · 场景 ${live.scene}/${live.scenes}` : ""}
              </Typography>
            </Box>
          ))}
        </Box>
      </Box>}
      <ConsoleFold raw={live.console ?? undefined} title="最新输出" help="WhisperJAV 控制台最近一行（纯文本）"
        unavailable="WhisperJAV 还没有输出" testIdPrefix="asr-console" />
    </ProgressFrame>
  );
}
