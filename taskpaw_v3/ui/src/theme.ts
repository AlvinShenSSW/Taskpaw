import { createTheme } from "@mui/material/styles";

// Derived from design-system/taskpaw-v3/MASTER.md — dark OLED, navy slate +
// blue accent, Fira Code/Sans. The status colors are the agent-console override.
export const statusColors: Record<string, string> = {
  ok: "#22C55E",
  running: "#22C55E",
  idle: "#64748B",
  unknown: "#64748B",
  degraded: "#F59E0B",
  error: "#DC2626",
  stopped: "#475569",
};

export const theme = createTheme({
  palette: {
    mode: "dark",
    // Primary = the MASTER Accent/CTA green. The old #1E293B (dark navy) was
    // nearly invisible on the #0F172A background — contained/outlined buttons
    // washed out. Green with dark text reads clearly on the dark surface.
    primary: { main: "#22C55E", contrastText: "#06210F" },
    secondary: { main: "#94A3B8" },     // visible slate for neutral controls
    info: { main: "#38BDF8" },          // bright sky accent (was a dark, low-contrast blue)
    background: { default: "#0F172A", paper: "#111827" },
    error: { main: "#F87171" },         // lighter red — readable on dark
    warning: { main: "#F59E0B" },
    success: { main: "#22C55E" },
    divider: "#334155",                 // lifted so card/outline borders are visible
    text: { primary: "#F8FAFC", secondary: "#A8B5C7" },
  },
  typography: {
    fontFamily: '"Fira Sans", system-ui, sans-serif',
    // tabular figures for data columns / timers (design §6)
    fontWeightMedium: 500,
    h6: { fontWeight: 600 },
    body2: { fontFamily: '"Fira Code", ui-monospace, monospace' },
  },
  shape: { borderRadius: 10 },
  components: {
    MuiButton: {
      defaultProps: { disableElevation: true },
      styleOverrides: {
        // Legible labels: real casing + weight, and a thicker outline so
        // outlined buttons (Stop / Edit) read clearly on the dark surface.
        root: { fontWeight: 600, textTransform: "none" },
        outlined: { borderWidth: "1.5px", "&:hover": { borderWidth: "1.5px" } },
      },
    },
    MuiChip: { styleOverrides: { root: { fontWeight: 500 } } },
    MuiCssBaseline: {
      styleOverrides: { body: { transition: "background 200ms ease" } },
    },
  },
});
