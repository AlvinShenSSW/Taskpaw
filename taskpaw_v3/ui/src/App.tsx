import { AppBar, Box, Tab, Tabs, Toolbar, Typography } from "@mui/material";
import { create } from "zustand";
import { useTranslation } from "react-i18next";
import { AgentConsole } from "./views/AgentConsole";
import { HubDashboard } from "./views/HubDashboard";
import { Logo } from "./components/Logo";
import { StatusDot } from "./components/StatusDot";

// Both role-views ship in one app (design §7); the active one is chosen by role.
type Role = "agent" | "hub";

// Resolve the role the shell injected, normalized exactly like the backend/shell
// (main.rs:101 ui_role()): only "agent"/"hub"; anything else → "agent". Returns
// null when NO role was injected (dev `npm run dev` / plain browser) so the UI
// keeps a role switcher for previewing both views.
function injectedRole(): Role | null {
  const raw = window.__TASKPAW__?.role;
  if (typeof raw !== "string") return null;
  const r = raw.trim().toLowerCase();
  return r === "agent" || r === "hub" ? r : "agent";
}

// Computed once at module load: in a packaged build the role is fixed (single
// view, no tabs); null means dev/browser → show the switcher.
const INJECTED_ROLE = injectedRole();

const useRole = create<{ role: Role; set: (r: Role) => void }>((set) => ({
  role: INJECTED_ROLE ?? "agent",
  set: (role) => set({ role }),
}));

export function App() {
  const { role, set } = useRole();
  const { t } = useTranslation();
  const label = (r: Role) => t(`app.${r}`);
  // Show the Agent/Hub switcher ONLY when no role was injected (dev/browser).
  // A packaged agent build must NOT expose the Hub tab (and vice versa) (#58).
  const showSwitcher = INJECTED_ROLE === null;
  return (
    // Transparent so the body blueprint grid + radial glow (theme.ts #89) shows
    // through; cards/appbar paint their own surfaces over it.
    <Box sx={{ minHeight: "100dvh", bgcolor: "transparent" }}>
      {/* Dark translucent app bar (design preview `.appbar`) with a glow underline,
          replacing the solid-green bar. */}
      <AppBar
        position="sticky"
        elevation={0}
        sx={{
          color: "text.primary",
          backgroundImage: "linear-gradient(180deg, rgba(13,22,38,.92), rgba(13,22,38,.78))",
          backgroundColor: "rgba(13,22,38,.85)",
          backdropFilter: "saturate(140%) blur(10px)",
          borderBottom: "1px solid",
          borderColor: "divider",
          // Accent glow underline (`.appbar::after`).
          "&::after": {
            content: '""',
            position: "absolute",
            left: 0,
            right: 0,
            bottom: -1,
            height: "1px",
            background:
              "linear-gradient(90deg, transparent, rgba(34,197,94,.6), rgba(56,189,248,.4), transparent)",
          },
        }}
      >
        <Toolbar variant="dense" sx={{ gap: 2.25 }}>
          {/* SVG paw mark + wordmark + V3 tag — no emoji (MASTER.md). */}
          <Box sx={{ display: "flex", alignItems: "center", gap: 1.1, fontWeight: 600, letterSpacing: ".3px" }}>
            <Logo />
            TaskPaw
            <Box
              component="span"
              sx={{
                fontSize: 9.5,
                letterSpacing: "1.5px",
                color: "primary.main",
                border: "1px solid rgba(34,197,94,.4)",
                borderRadius: "4px",
                px: 0.6,
                py: "1px",
                ml: 0.3,
              }}
            >
              V3
            </Box>
          </Box>
          {showSwitcher ? (
            // Role switcher as a segmented pill (Tabs keeps tablist/aria-selected +
            // keyboard arrows + visible focus); indicator hidden, selected tab fills.
            <Tabs
              value={role}
              onChange={(_, v) => set(v)}
              textColor="inherit"
              aria-label={t("app.agent") + " / " + t("app.hub")}
              TabIndicatorProps={{ sx: { display: "none" } }}
              sx={{
                minHeight: 0,
                bgcolor: "background.default",
                border: "1px solid",
                borderColor: "divider",
                borderRadius: "8px",
                p: "2px",
                "& .MuiTab-root": {
                  minHeight: 0,
                  textTransform: "none",
                  fontWeight: 600,
                  borderRadius: "6px",
                  px: 2,
                  py: 0.6,
                  color: "text.secondary",
                  transition: "0.18s",
                },
                "& .MuiTab-root.Mui-selected": {
                  bgcolor: "primary.main",
                  color: "primary.contrastText",
                  boxShadow: "0 0 14px -3px rgba(34,197,94,.7)",
                },
              }}
            >
              <Tab value="agent" label={label("agent")} />
              <Tab value="hub" label={label("hub")} />
            </Tabs>
          ) : (
            // Single-role build: no tab strip, just the current view's label.
            <Typography variant="subtitle1" sx={{ opacity: 0.85 }}>
              {label(role)}
            </Typography>
          )}
          <Box sx={{ flexGrow: 1 }} />
          {/* Health badge: live dot + text (status never color-only). The real
              Hub-reachability probe is deferred (issue #91 scope); the dot reflects
              the shell being up. */}
          <Box
            sx={{
              display: "inline-flex",
              alignItems: "center",
              gap: 0.5,
              px: 1.4,
              py: 0.5,
              borderRadius: "999px",
              border: "1px solid",
              borderColor: "divider",
              bgcolor: "background.default",
            }}
          >
            <StatusDot state="ok" live />
            <Typography variant="body2">{t("app.online")}</Typography>
          </Box>
          <Typography variant="body2" sx={{ ml: 1.5, color: "text.secondary" }}>v3.0.0-dev</Typography>
        </Toolbar>
      </AppBar>
      <Box sx={{ p: 2 }}>
        {/* Settings now lives as a tab INSIDE each role view (next to
            Monitors/Events, Fleet/Events) — no app-bar gear (#87). */}
        {role === "agent" ? <AgentConsole /> : <HubDashboard />}
      </Box>
    </Box>
  );
}
