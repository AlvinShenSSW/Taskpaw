import {
  Alert, Box, Card, CardContent, Chip, List, ListItemButton, Stack, Typography,
} from "@mui/material";
import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { api, type MonitorSnapshot } from "../api";
import { StatusDot } from "../components/StatusDot";

// Local control panel for ONE machine (design pages/agent-console.md): left rail
// of this machine's monitors, main pane = selected monitor's live status.
export function AgentConsole() {
  const { data, error, isLoading } = useQuery({ queryKey: ["agentStatus"], queryFn: api.agentStatus });
  const [selected, setSelected] = useState<string | null>(null);

  if (isLoading) return <Typography>Loading…</Typography>;
  if (error) return <Alert severity="error">Agent unreachable: {String(error)}</Alert>;

  const monitors = data?.monitors ?? {};
  const names = Object.keys(monitors);
  const current = selected && monitors[selected] ? selected : names[0];

  return (
    <Stack direction="row" spacing={2} sx={{ minHeight: "70dvh" }}>
      <Card sx={{ width: 280, flex: "0 0 auto" }}>
        <CardContent>
          <Typography variant="overline" color="text.secondary">
            {data?.machine} — monitors
          </Typography>
          {names.length === 0 && (
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
              No monitors configured.
            </Typography>
          )}
          <List dense>
            {names.map((n) => (
              <ListItemButton key={n} selected={n === current} onClick={() => setSelected(n)}>
                <StatusDot state={monitors[n].state} />
                <Typography variant="body2" noWrap>{n}</Typography>
              </ListItemButton>
            ))}
          </List>
        </CardContent>
      </Card>

      <Box sx={{ flex: 1 }}>
        {current && monitors[current] ? (
          <MonitorDetail name={current} snap={monitors[current]} />
        ) : (
          <Typography color="text.secondary">Select a monitor.</Typography>
        )}
      </Box>
    </Stack>
  );
}

function MonitorDetail({ name, snap }: { name: string; snap: MonitorSnapshot }) {
  return (
    <Card>
      <CardContent>
        <Stack direction="row" alignItems="center" spacing={1}>
          <StatusDot state={snap.state} />
          <Typography variant="h6">{name}</Typography>
          <Chip size="small" label={snap.state} />
          {snap.degraded && <Chip size="small" color="warning" label="degraded" />}
        </Stack>
        {snap.detail && (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{snap.detail}</Typography>
        )}
        {snap.metrics && Object.keys(snap.metrics).length > 0 && (
          <Box sx={{ mt: 2 }}>
            <Typography variant="overline" color="text.secondary">metrics</Typography>
            <Box component="pre" sx={{ m: 0, fontFamily: '"Fira Code", monospace', fontSize: 13 }}>
              {JSON.stringify(snap.metrics, null, 2)}
            </Box>
          </Box>
        )}
      </CardContent>
    </Card>
  );
}
