import { AppBar, Box, Tab, Tabs, Toolbar, Typography } from "@mui/material";
import { create } from "zustand";
import { AgentConsole } from "./views/AgentConsole";
import { HubDashboard } from "./views/HubDashboard";

// Both role-views ship in one app (design §7); the active one is chosen by role.
type Role = "agent" | "hub";
const useRole = create<{ role: Role; set: (r: Role) => void }>((set) => ({
  role: (window.__TASKPAW__ as any)?.role ?? "agent",
  set: (role) => set({ role }),
}));

export function App() {
  const { role, set } = useRole();
  return (
    <Box sx={{ minHeight: "100dvh", bgcolor: "background.default" }}>
      <AppBar position="sticky" color="primary" elevation={0}
        sx={{ borderBottom: 1, borderColor: "divider" }}>
        <Toolbar variant="dense">
          <Typography variant="h6" sx={{ mr: 3 }}>🐾 TaskPaw</Typography>
          <Tabs value={role} onChange={(_, v) => set(v)} textColor="inherit"
            indicatorColor="secondary">
            <Tab value="agent" label="Agent Console" />
            <Tab value="hub" label="Hub Dashboard" />
          </Tabs>
          <Box sx={{ flexGrow: 1 }} />
          <Typography variant="body2" sx={{ opacity: 0.7 }}>v3.0.0-dev</Typography>
        </Toolbar>
      </AppBar>
      <Box sx={{ p: 2 }}>
        {role === "agent" ? <AgentConsole /> : <HubDashboard />}
      </Box>
    </Box>
  );
}
