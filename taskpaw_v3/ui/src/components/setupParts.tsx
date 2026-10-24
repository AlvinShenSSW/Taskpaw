import { Box, Button, Chip, Stack, Typography } from "@mui/material";
import type { GpuFound } from "../api";
import { dirLine, useDirFacts } from "./setupModel";

// #299 §5 R7/R8/R13: what detection found — per-tool rows (path, version / source, 使用 / 更改),
// directory facts (exists / missing, free space, drive) and the GPU card. Shared by 设置 · 媒体工作流
// and the first-run wizard's 主控设置 / GPU 工作机设置 steps.
export function DirFacts({ path, sameDriveAs }: { path: string; sameDriveAs?: string }) {
  const facts = useDirFacts(path);
  const other = useDirFacts(sameDriveAs ?? "");
  const line = dirLine(facts.data, sameDriveAs ? other.data : undefined);
  if (!line) return null;
  return <Typography variant="body2" color={facts.data?.exists ? "text.secondary" : "warning.main"} sx={{ mt: "-8px !important" }}>{line}</Typography>;
}

export function ToolRow({ name, line, path, extra, onUse, onChange }: {
  name: string; line: string; path?: string | null; extra?: string[]; onUse?: () => void; onChange?: () => void;
}) {
  const found = line.startsWith("已找到");
  return (
    <Stack direction="row" spacing={1.5} alignItems="center" sx={{ py: 1, borderBottom: "1px solid", borderColor: "divider" }}>
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
          <Typography variant="subtitle2">{name}</Typography>
          <Typography variant="body2" color={found ? "success.main" : "warning.main"}>{line}</Typography>
        </Stack>
        {path && <Typography variant="body2" color="text.secondary" sx={{ fontFamily: "'Fira Code', monospace", overflowWrap: "anywhere" }}>{path}</Typography>}
        {extra?.map((e) => <Typography key={e} variant="body2" color="text.secondary" sx={{ overflowWrap: "anywhere" }}>{e}</Typography>)}
      </Box>
      {onUse && <Button size="small" variant="outlined" sx={{ minHeight: 44 }} onClick={onUse}>使用</Button>}
      {onChange && <Button size="small" sx={{ minHeight: 44 }} onClick={onChange}>更改</Button>}
    </Stack>
  );
}

export function GpuCard({ gpu }: { gpu: GpuFound | null | undefined }) {
  if (gpu === undefined) return <Typography variant="body2" color="text.secondary">正在检测显卡…</Typography>;
  if (gpu === null) return <Typography variant="body2" color="warning.main">未检测到 NVIDIA 显卡</Typography>;
  const gb = Math.round(gpu.vram_mib / 1024);
  return (
    <Box sx={{ p: 1.5, border: "1px solid", borderColor: "divider", borderRadius: 2 }}>
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
        <Typography variant="subtitle2">{gpu.name} · {gb} GB</Typography>
        <Chip size="small" variant="outlined" color="success" label="已检测" />
      </Stack>
      <Typography variant="body2" color="text.secondary">
        {[gpu.driver && `驱动 ${gpu.driver}`, `显存 ${gpu.vram_mib} MiB`, gpu.power_limit_w != null && `功耗上限 ${gpu.power_limit_w} W`]
          .filter(Boolean).join(" · ")}
      </Typography>
    </Box>
  );
}
