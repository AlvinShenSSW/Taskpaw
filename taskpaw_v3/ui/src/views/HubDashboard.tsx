import {
  Alert, Box, Card, CardContent, Chip, MenuItem, Stack, Tab, Tabs, TextField, Typography,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api } from "../api";
import { StatusDot } from "../components/StatusDot";
import { EventLog } from "../components/EventLog";

// Multi-machine observability (design pages/hub-dashboard.md): fleet grid of
// machines + the Hub's own host-health self-monitor, and an aggregated event log
// (#44). No marketing hero/CTA.
export function HubDashboard() {
  const { data, error, isLoading } = useQuery({ queryKey: ["hubStatus"], queryFn: api.hubStatus });
  const [tab, setTab] = useState<"fleet" | "events">("fleet");
  const [level, setLevel] = useState<string>("");
  // Aggregated durable history from all polled agents; only poll while open.
  const events = useQuery({
    queryKey: ["hubEvents", level],
    queryFn: () => api.hubEvents({ level: level || undefined }),
    refetchInterval: 5000, enabled: tab === "events",
  });

  if (isLoading) return <Typography>Loading…</Typography>;
  if (error) return <Alert severity="error">Hub unreachable: {String(error)}</Alert>;

  const servers = data?.servers ?? [];
  const self = data?.self ?? {};

  return (
    <Stack spacing={1.5}>
      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ minHeight: 0 }}>
        <Tab value="fleet" label="Fleet" />
        <Tab value="events" label="Events" />
      </Tabs>

      {tab === "events" ? (
        <Card>
          <CardContent>
            <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1 }}>
              <Typography variant="overline" color="text.secondary">event history</Typography>
              <TextField select size="small" label="Level" value={level}
                onChange={(e) => setLevel(e.target.value)} sx={{ minWidth: 140 }}>
                <MenuItem value="">All levels</MenuItem>
                {["info", "done", "warn", "alert"].map((l) => (
                  <MenuItem key={l} value={l}>{l}</MenuItem>
                ))}
              </TextField>
            </Stack>
            <EventLog events={events.data?.events} />
          </CardContent>
        </Card>
      ) : (
        <Stack spacing={2}>
          <Typography variant="overline" color="text.secondary">
            {data?.machine} — fleet ({servers.length} {servers.length === 1 ? "agent" : "agents"})
          </Typography>
          <Box sx={{ display: "flex", flexWrap: "wrap", gap: 2 }}>
            {servers.map((s) => (
              <Card key={s.id} sx={{ width: { xs: "100%", sm: 280 } }}>
                <CardContent>
                  <Stack direction="row" alignItems="center" spacing={1}>
                    <StatusDot state={s.enabled ? "ok" : "stopped"} />
                    <Typography variant="subtitle1">{s.name}</Typography>
                  </Stack>
                  <Typography variant="body2" color="text.secondary">
                    {s.ip}:{s.port}
                  </Typography>
                  <Chip size="small" sx={{ mt: 1 }} label={s.enabled ? "enabled" : "disabled"} />
                </CardContent>
              </Card>
            ))}
            {servers.length === 0 && (
              <Typography color="text.secondary">No agents registered yet.</Typography>
            )}
          </Box>

          {Object.keys(self).length > 0 && (
            <Card>
              <CardContent>
                <Typography variant="overline" color="text.secondary">Hub host (self-monitor)</Typography>
                {Object.entries(self).map(([name, snap]) => (
                  <Box key={name} sx={{ mt: 1 }}>
                    <Stack direction="row" alignItems="center" spacing={1}>
                      <StatusDot state={snap.state} />
                      <Typography variant="body2">{name}</Typography>
                    </Stack>
                    {snap.metrics && (
                      <Box component="pre" sx={{ m: 0, fontFamily: '"Fira Code", monospace', fontSize: 12 }}>
                        {JSON.stringify(snap.metrics, null, 2)}
                      </Box>
                    )}
                  </Box>
                ))}
              </CardContent>
            </Card>
          )}
        </Stack>
      )}
    </Stack>
  );
}
