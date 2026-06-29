import { AppBar, Box, Tab, Tabs, Toolbar, Typography } from "@mui/material";
import { create } from "zustand";
import { useTranslation } from "react-i18next";
import { AgentConsole } from "./views/AgentConsole";
import { HubDashboard } from "./views/HubDashboard";

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
      <AppBar position="sticky" color="primary" elevation={0}
        sx={{ borderBottom: 1, borderColor: "divider" }}>
        <Toolbar variant="dense">
          <Typography variant="h6" sx={{ mr: 3 }}>🐾 TaskPaw</Typography>
          {showSwitcher ? (
            <Tabs value={role} onChange={(_, v) => set(v)}
              textColor="inherit" indicatorColor="secondary">
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
          <Typography variant="body2" sx={{ opacity: 0.7 }}>v3.0.0-dev</Typography>
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
