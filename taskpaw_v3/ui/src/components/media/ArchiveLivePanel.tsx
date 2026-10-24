import { Box, LinearProgress, Typography } from "@mui/material";
import type { WorkflowStep } from "../../api";
import { DATA, ProgressFrame } from "../progressParts";
import { archiveBytes, elapsedClock } from "./mediaModel";
import { useArchiveProgress } from "./useArchiveProgress";
import { useNow } from "./useNow";

export function ArchiveLivePanel({ step }: { step: WorkflowStep }) {
  const { live, speed, eta } = useArchiveProgress(step);
  const now = useNow(Boolean(live));
  if (!live) return null;
  return (
    <ProgressFrame testId="archive-live" label="归档进度"
      title={<Box component="span" sx={{ display: "inline-flex", alignItems: "center", gap: 1.25 }}>
        <Box component="span" aria-hidden sx={{ width: 9, height: 9, borderRadius: "50%", bgcolor: "success.main",
          "@keyframes archive-beat": { "0%, 100%": { opacity: 1 }, "50%": { opacity: 0.35 } },
          animation: "archive-beat 1.6s ease-in-out infinite", "@media (prefers-reduced-motion: reduce)": { animation: "none" } }} />
        <Box component="span" sx={DATA}>已用 {elapsedClock(now - Date.parse(live.started_at))}</Box>
      </Box>} position="复制 → 校验">
      <Box sx={{ px: { xs: 2, sm: 3 }, py: 2.5 }}>
        <Box sx={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 2 }}>
          <Typography sx={{ ...DATA, fontSize: 48, color: "success.light", lineHeight: 1.1 }}>{live.percent == null ? "—" : `${live.percent}%`}</Typography>
          <Typography variant="caption" color="text.secondary">预计剩余 {eta == null ? "—" : `约 ${eta} 秒`}</Typography>
        </Box>
        <LinearProgress aria-label="归档进度" variant={live.percent == null ? "indeterminate" : "determinate"} value={live.percent ?? 0}
          sx={{ mt: 2, height: 10, borderRadius: 5 }} />
        <Box component="ul" aria-label="归档数据" sx={{ listStyle: "none", p: 0, my: 2.5, display: "grid",
          gridTemplateColumns: { xs: "repeat(2, minmax(0, 1fr))", sm: "repeat(4, minmax(0, 1fr))" }, gap: 1 }}>
          {[["已复制", archiveBytes(live.copied_bytes, live.total_bytes)], ["已校验", archiveBytes(live.verified_bytes, live.total_bytes)],
            ["文件夹", `${live.files_done} / ${live.files_total}`], ["速度", speed ? `${(speed / 1e6).toFixed(1)} MB/s` : "—"]].map(([label, value]) => (
            <Box component="li" key={label} sx={{ p: 1.25, border: "1px solid", borderColor: "divider", borderRadius: 1.5 }}>
              <Typography variant="caption" color="text.secondary">{label}</Typography>
              <Typography sx={{ ...DATA, mt: 0.5, fontSize: 13 }}>{value}</Typography>
            </Box>
          ))}
        </Box>
        <Typography variant="body2" sx={{ overflowWrap: "anywhere" }}>{live.target}</Typography>
        <Box component="ul" aria-label="归档文件" sx={{ listStyle: "none", px: 0 }}>
          {live.items.map((item) => <Box component="li" key={item.name} sx={{ my: 1.5 }}>
            <Typography variant="body2" sx={{ overflowWrap: "anywhere" }}>{item.bytes >= item.total && item.total > 0 ? "✓ " : ""}{item.name}</Typography>
            {item.bytes < item.total && <LinearProgress aria-label={`${item.name} 复制进度`} variant="determinate"
              value={item.bytes / item.total * 100} sx={{ mt: 0.75, height: 4, borderRadius: 2 }} />}
          </Box>)}
        </Box>
        <Typography variant="caption" color="text.secondary">复制完会逐个校验；校验通过后才清理主控上的工作副本。</Typography>
      </Box>
    </ProgressFrame>
  );
}
