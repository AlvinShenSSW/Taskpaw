import { Box } from "@mui/material";

// Inline SVG paw mark (#91), replacing the forbidden `🐾` emoji-as-icon
// (MASTER.md anti-pattern). Decorative — the adjacent "TaskPaw" wordmark carries
// the accessible name, so the svg is aria-hidden. Path traced from the design
// preview brand mark.
export function Logo({ size = 22 }: { size?: number }) {
  return (
    <Box component="span" sx={{ display: "inline-flex", color: "primary.main" }}>
      <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="7.5" cy="8" r="1.9" fill="currentColor" />
        <circle cx="12" cy="6.4" r="1.9" fill="currentColor" />
        <circle cx="16.5" cy="8" r="1.9" fill="currentColor" />
        <circle cx="18.4" cy="12.2" r="1.7" fill="currentColor" />
        <path
          d="M12 10.4c2.8 0 5 2.1 5 4.4 0 1.9-1.6 3-3.4 2.6-.9-.2-1.4-.2-1.6-.2s-.7 0-1.6.2C8.6 17.8 7 16.7 7 14.8c0-2.3 2.2-4.4 5-4.4Z"
          fill="currentColor"
        />
      </svg>
    </Box>
  );
}
