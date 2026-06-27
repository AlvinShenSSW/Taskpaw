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
    primary: { main: "#1E293B", contrastText: "#FFFFFF" },
    secondary: { main: "#334155" },
    info: { main: "#0369A1" }, // accent / CTA
    background: { default: "#0F172A", paper: "#111827" },
    error: { main: "#DC2626" },
    warning: { main: "#F59E0B" },
    success: { main: "#22C55E" },
    divider: "#1F2937",
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
    MuiButton: { defaultProps: { disableElevation: true } },
    MuiCssBaseline: {
      styleOverrides: { body: { transition: "background 200ms ease" } },
    },
  },
});
