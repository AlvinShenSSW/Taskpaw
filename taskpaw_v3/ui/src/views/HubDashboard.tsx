import { Alert, Box, Card, CardContent, Chip, Stack, Typography } from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { StatusDot } from "../components/StatusDot";

// Multi-machine observability (design pages/hub-dashboard.md): fleet grid of
// machines + the Hub's own host-health self-monitor. No marketing hero/CTA.
export function HubDashboard() {
  const { data, error, isLoading } = useQuery({ queryKey: ["hubStatus"], queryFn: api.hubStatus });
  if (isLoading) return <Typography>Loading…</Typography>;
  if (error) return <Alert severity="error">Hub unreachable: {String(error)}</Alert>;

  const servers = data?.servers ?? [];
  const self = data?.self ?? {};

  return (
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
  );
}
