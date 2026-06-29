import { Box } from "@mui/material";

// SVG icon paths per monitor type / preset id (#93), traced from the design
// preview's icon map. PluginInfo has no icon field yet (out of scope to add it
// backend-side), so the frontend maps by type_id with a generic fallback.
const PATHS: Record<string, string> = {
  lada: '<path d="M4 5h16v14H4z"/><path d="M4 9h16M8 5v14M16 5v14M8 9v4h8V9"/>',
  comfyui:
    '<circle cx="6" cy="7" r="2"/><circle cx="18" cy="7" r="2"/><circle cx="12" cy="17" r="2"/><path d="M6 9v2a3 3 0 0 0 3 3M18 9v2a3 3 0 0 1-3 3"/>',
  folder_watch: '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
  process:
    '<rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9 9h6v6H9zM2 9h2M2 15h2M20 9h2M20 15h2M9 2v2M15 2v2M9 20v2M15 20v2"/>',
  heartbeat: '<path d="M3 12h4l2-5 4 10 2-5h6"/>',
  moomoo: '<path d="M4 19V5M4 19h16M8 16l3-4 3 2 4-6"/>',
  tcp_check:
    '<rect x="3" y="4" width="18" height="5" rx="1"/><rect x="3" y="15" width="18" height="5" rx="1"/><path d="M7 9v6M12 6.5h.01M12 17.5h.01"/>',
  state_file: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 13h6M9 17h6"/>',
  custom_cmd: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/>',
  // Generic monitor fallback (signal waves) for unmapped types.
  _fallback: '<circle cx="12" cy="12" r="2"/><path d="M5 12a7 7 0 0 1 14 0M8.5 12a3.5 3.5 0 0 1 7 0"/>',
};

export function ServiceIcon({ id, size = 21 }: { id: string; size?: number }) {
  const d = PATHS[id] ?? PATHS._fallback;
  return (
    <Box
      aria-hidden="true"
      sx={{
        width: 38,
        height: 38,
        borderRadius: "10px",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        bgcolor: "rgba(34,197,94,.1)",
        color: "primary.main",
      }}
    >
      <svg
        width={size}
        height={size}
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.6}
        strokeLinecap="round"
        strokeLinejoin="round"
        dangerouslySetInnerHTML={{ __html: d }}
      />
    </Box>
  );
}
