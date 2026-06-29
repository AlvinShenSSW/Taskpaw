import { createTheme } from "@mui/material/styles";

// Derived from design-system/taskpaw-v3/MASTER.md + the v3-ui-polish preview —
// dark OLED, navy slate + green accent, Fira Code/Sans with a CJK fallback. The
// status colors are the agent-console override.
export const statusColors: Record<string, string> = {
  ok: "#22C55E",
  running: "#22C55E",
  starting: "#38BDF8", // #89: starting/transition (pulse accent)
  idle: "#64748B",
  unknown: "#64748B",
  degraded: "#F59E0B",
  error: "#DC2626",
  stopped: "#475569",
};

// CJK-aware font stacks (#89): Fira first (Latin/numerals), Noto Sans SC + system
// CJK as fallback so Chinese renders in-brand. Numerals stay Fira (tabular).
const SANS = '"Fira Sans","Noto Sans SC",system-ui,"PingFang SC","Microsoft YaHei",sans-serif';
const MONO = '"Fira Code","Noto Sans SC",ui-monospace,monospace';

export const theme = createTheme({
  palette: {
    mode: "dark",
    // Primary = the MASTER Accent/CTA green (dark text reads clearly on dark).
    primary: { main: "#22C55E", contrastText: "#06210F" },
    secondary: { main: "#94A3B8" },     // visible slate for neutral controls
    info: { main: "#38BDF8" },          // bright sky accent
    // #89: deepen the background and layer the cards above it.
    background: { default: "#0B1120", paper: "#111A2E" },
    error: { main: "#F87171" },         // lighter red — readable on dark
    warning: { main: "#F59E0B" },
    success: { main: "#22C55E" },
    divider: "#22324A",                 // visible card/outline borders
    text: { primary: "#F8FAFC", secondary: "#A8B5C7" },
  },
  typography: {
    fontFamily: SANS,
    fontWeightMedium: 500,
    h6: { fontWeight: 600 },
    // body2 is mono (data/timers); keep tabular figures + CJK fallback.
    body2: { fontFamily: MONO },
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
    // #89: cards get a subtle vertical gradient + a visible border (HUD corner
    // ticks are a selective <HudCard>, added in #90).
    MuiCard: {
      styleOverrides: {
        root: {
          backgroundImage: "linear-gradient(180deg, #111A2E, #0D1626)",
          backgroundColor: "#111A2E",
          border: "1px solid #19273C",
        },
      },
    },
    MuiCssBaseline: {
      styleOverrides: {
        // Blueprint grid + cool radial glow behind the app (#89). Static (no
        // animation), so it's reduced-motion-safe by construction.
        body: {
          background:
            "radial-gradient(1200px 600px at 80% -10%, rgba(56,189,248,.06), transparent 60%)," +
            "linear-gradient(transparent 0 31px, rgba(56,189,248,.035) 31px 32px) 0 0/100% 32px," +
            "linear-gradient(90deg, transparent 0 31px, rgba(56,189,248,.035) 31px 32px) 0 0/32px 100%," +
            "#0B1120",
          transition: "background 200ms ease",
        },
        // Chinese is first-class: raise line-height, and drop uppercase + wide
        // tracking (Latin-only treatments) on label variants under lang=zh (#89/§8).
        'html[lang="zh"] body': { lineHeight: 1.7 },
        'html[lang="zh"] .MuiTypography-overline': {
          textTransform: "none",
          letterSpacing: "0.2px",
        },
        // Respect prefers-reduced-motion globally: near-instant animations/
        // transitions (covers the grid transition + later pulse/hover effects).
        "@media (prefers-reduced-motion: reduce)": {
          "*, *::before, *::after": {
            animationDuration: "0.001ms !important",
            animationIterationCount: "1 !important",
            transitionDuration: "0.001ms !important",
            scrollBehavior: "auto !important",
          },
        },
      },
    },
  },
});
