import { Box, Skeleton } from "@mui/material";

// Reusable loading skeleton for list/detail panes (#90): a column of placeholder
// rows (dot + label + trailing timestamp) shown while data loads, instead of a
// bare "Loading…" string. Wired into AgentConsole/HubDashboard in #92/#95.
export function SkeletonRows({ rows = 4 }: { rows?: number }) {
  return (
    <Box aria-busy="true" aria-label="loading" role="status">
      {Array.from({ length: rows }).map((_, i) => (
        <Box
          key={i}
          sx={{ display: "flex", alignItems: "center", gap: 1.5, py: 1 }}
        >
          <Skeleton variant="circular" width={10} height={10} />
          <Skeleton variant="text" sx={{ flex: 1 }} />
          <Skeleton variant="text" width={48} />
        </Box>
      ))}
    </Box>
  );
}
