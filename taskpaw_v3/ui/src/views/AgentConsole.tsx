import {
  Alert, Box, Button, Card, CardContent, Chip, Dialog, DialogActions,
  DialogContent, DialogContentText, DialogTitle,
  Snackbar, Stack, Tab, Tabs, Typography,
} from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type MonitorSnapshot, type PluginInfo, type PresetInfo } from "../api";
import { StatusDot } from "../components/StatusDot";
import { SkeletonRows } from "../components/SkeletonRows";
import { MonitorMetrics } from "../components/MonitorMetrics";
import { EventLog } from "../components/EventLog";
import { MonitorSelector } from "../components/MonitorSelector";
import { MonitorWizard } from "./MonitorWizard";
import { Settings } from "./Settings";

// HH:MM:SS in 24h, from a react-query dataUpdatedAt epoch (ms). Empty until the
// first successful fetch. en-GB is a stable 24h format (the digits are mono via
// body2 anyway, so they stay tabular).
const fmtTime = (ms?: number) =>
  ms ? new Date(ms).toLocaleTimeString("en-GB", { hour12: false }) : "";

// Same HH:MM:SS but from an ISO string (the per-monitor `last_event_at`, #130).
// Returns "" for a missing/unparseable value so the caller renders nothing.
const fmtIso = (iso?: string) => {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString("en-GB", { hour12: false });
};

// Local control panel for ONE machine (design pages/agent-console.md): left rail
// of this machine's monitors + an Add button; main pane = the selected monitor's
// live status + Start/Stop/Edit/Delete; an Add/Edit dialog renders the plugin's
// schema-driven config form (#57).
export function AgentConsole() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["agentStatus"], queryFn: api.agentStatus, refetchInterval: 5000 });
  const plugins = useQuery({ queryKey: ["agentPlugins"], queryFn: api.plugins });
  // #145: surface when the network API has no token (auth disabled). The backend's
  // bind guard keeps this loopback-only, but the operator should see it.
  const config = useQuery({ queryKey: ["agentConfig"], queryFn: api.config });
  const [selected, setSelected] = useState<string | null>(null);
  const [dialog, setDialog] = useState<null | { mode: "add" } | { mode: "edit"; name: string }>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<"monitors" | "events" | "settings">("monitors");
  // Recent local events for the event-log tab (#44); only poll while it's open.
  const events = useQuery({
    queryKey: ["agentEvents"], queryFn: () => api.agentEvents(),
    refetchInterval: 5000, enabled: tab === "events",
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["agentStatus"] });
    qc.invalidateQueries({ queryKey: ["agentConfig"] });
  };
  const onErr = (e: unknown) => setError(e instanceof Error ? e.message : String(e));

  // NOTE: no early return on status loading/error — Settings must stay reachable
  // (it holds the config editor needed to FIX a bad host/port/token) even when the
  // agent is unreachable (#87/Codex). The status states are rendered per-tab below.
  const monitors = status.data?.monitors ?? {};
  const names = Object.keys(monitors);
  const current = selected && monitors[selected] ? selected : names[0];

  return (
    <Stack spacing={1.5}>
      <Tabs value={tab} onChange={(_, v) => setTab(v)} sx={{ minHeight: 0 }}>
        <Tab value="monitors" label={t("agent.monitors")} />
        <Tab value="events" label={t("agent.events")} />
        <Tab value="settings" label={t("settings.title")} />
      </Tabs>

      {Boolean(config.data?.auth_disabled) && (
        <Alert severity="warning">{t("agent.authDisabled")}</Alert>
      )}

      {tab === "settings" ? (
        <Settings role="agent" />
      ) : status.isLoading ? (
        <Card><CardContent><SkeletonRows rows={5} /></CardContent></Card>
      ) : status.error ? (
        <Alert severity="error">{t("agent.unreachable", { error: String(status.error) })}</Alert>
      ) : tab === "events" ? (
        <Card>
          <CardContent>
            <Stack direction="row" alignItems="baseline" justifyContent="space-between">
              <Typography variant="overline" color="text.secondary">
                {t("agent.recentEvents", { machine: status.data?.machine })}
              </Typography>
              {events.isFetching && (
                <Typography variant="caption" color="text.secondary">{t("common.updating")}</Typography>
              )}
            </Stack>
            <EventLog events={events.data?.events} />
          </CardContent>
        </Card>
      ) : names.length === 0 ? (
        // Empty state: the one place a prominent CTA appears (design).
        <Card><CardContent>
          <Stack alignItems="center" spacing={2} sx={{ py: 6 }}>
            <Typography color="text.secondary">{t("agent.noMonitors")}</Typography>
            <Button variant="contained" onClick={() => setDialog({ mode: "add" })}>
              + {t("common.add")}
            </Button>
          </Stack>
        </CardContent></Card>
      ) : names.length === 1 ? (
        // Single monitor (the common case): a full-width hero, no rail — the one
        // monitor fills the window instead of a near-empty 280px rail (design).
        <Stack spacing={1}>
          <Stack direction="row" alignItems="center" justifyContent="space-between">
            <Typography variant="overline" color="text.secondary">
              {t("agent.monitorsTitle", { machine: status.data?.machine })}
            </Typography>
            <Button size="small" variant="outlined" onClick={() => setDialog({ mode: "add" })}>
              + {t("common.add")}
            </Button>
          </Stack>
          <MonitorHero
            name={names[0]} snap={monitors[names[0]]} updatedAt={status.dataUpdatedAt}
            onEdit={() => setDialog({ mode: "edit", name: names[0] })}
            onChanged={invalidate} onError={onErr}
          />
        </Stack>
      ) : (
        // Multiple monitors: a horizontal pill selector (replacing the tall rail)
        // + the selected monitor in the same hero as the single-monitor case (#135).
        <Stack spacing={1.5}>
          <Stack direction="row" alignItems="center" justifyContent="space-between">
            <Typography variant="overline" color="text.secondary">
              {t("agent.monitorsTitle", { machine: status.data?.machine })}
            </Typography>
            {/* Poll freshness for the whole list (a single poll updates every
                monitor); per-monitor last-EVENT time is shown in each hero (#130). */}
            {status.dataUpdatedAt > 0 && (
              <Typography variant="caption" color="text.secondary">
                {t("agent.updated", { time: fmtTime(status.dataUpdatedAt) })}
              </Typography>
            )}
          </Stack>
          <MonitorSelector
            names={names} monitors={monitors} selected={current}
            onSelect={setSelected} onAdd={() => setDialog({ mode: "add" })}
          />
          {current && monitors[current] && (
            <MonitorHero
              name={current} snap={monitors[current]} updatedAt={status.dataUpdatedAt}
              onEdit={() => setDialog({ mode: "edit", name: current })}
              onChanged={invalidate} onError={onErr}
            />
          )}
        </Stack>
      )}

      {dialog && (
        <WizardLauncher
          mode={dialog.mode}
          name={dialog.mode === "edit" ? dialog.name : undefined}
          pluginsData={plugins.data}
          onClose={() => setDialog(null)}
          onDone={(savedName) => { setDialog(null); if (savedName) setSelected(savedName); invalidate(); }}
          onError={onErr}
        />
      )}

      <Snackbar open={!!error} autoHideDuration={6000} onClose={() => setError(null)}>
        <Alert severity="error" onClose={() => setError(null)}>{error}</Alert>
      </Snackbar>
    </Stack>
  );
}

// Inline recent-events panel shown beside the hero metrics (#136): the hero has
// room, and recent events are the most useful fill. Scoped to the hero's monitor
// via the backend `monitor` filter (#130), so the panel shows what's happening on
// the monitor you're looking at rather than the whole agent's stream.
function InlineEvents({ monitor }: { monitor: string }) {
  const { t } = useTranslation();
  const events = useQuery({
    queryKey: ["agentEvents", "inline", monitor],
    queryFn: () => api.agentEvents(8, monitor),
    refetchInterval: 5000,
  });
  return (
    <Card sx={{ height: "100%" }}>
      <CardContent>
        <Typography variant="overline" color="text.secondary">
          {t("agent.recentEventsShort")}
        </Typography>
        <EventLog events={events.data?.events} />
      </CardContent>
    </Card>
  );
}

type MonitorDetailProps = {
  name: string;
  snap: MonitorSnapshot;
  updatedAt?: number;
  onEdit: () => void;
  onChanged: () => void;
  onError: (e: unknown) => void;
};

// The hero: the monitor's detail (metrics) with the inline recent-events column
// beside it on wide screens, stacked on narrow (#136).
function MonitorHero(props: MonitorDetailProps) {
  return (
    <Stack direction={{ xs: "column", lg: "row" }} spacing={2} alignItems="stretch">
      <Box sx={{ flex: 2, minWidth: 0 }}><MonitorDetail {...props} /></Box>
      <Box sx={{ flex: 1, minWidth: 0 }}><InlineEvents monitor={props.name} /></Box>
    </Stack>
  );
}

function MonitorDetail({
  name, snap, updatedAt, onEdit, onChanged, onError,
}: MonitorDetailProps) {
  const { t } = useTranslation();
  const [confirmDel, setConfirmDel] = useState(false);
  // Live-state, not the persisted `enabled`: a managed Lada is launched per
  // session (Start) without persisting enabled, so "running" must follow whether
  // it's actually live (anything but stopped), else it'd show Start while running.
  const running = snap.state !== "stopped";
  // Only operator-configured monitors are mutable. The auto-injected host_metrics
  // self-monitor is live but NOT in config (no type_id from merge_status), so the
  // control API can't start/stop/edit/delete it — don't show controls that would
  // always fail (Codex #57b).
  const manageable = !!snap.type_id;

  // Hooks at the top level (rules of hooks) — one mutation per action.
  const start = useMutation({ mutationFn: () => api.startMonitor(name), onSuccess: onChanged, onError });
  const stop = useMutation({ mutationFn: () => api.stopMonitor(name), onSuccess: onChanged, onError });
  const del = useMutation({
    mutationFn: () => api.removeMonitor(name),
    onSuccess: () => { setConfirmDel(false); onChanged(); },
    onError,
  });

  return (
    <Card>
      <CardContent>
        <Stack direction="row" alignItems="center" spacing={1}>
          <StatusDot state={snap.state} />
          <Typography variant="h6" sx={{ flex: 1 }}>{name}</Typography>
          <Chip size="small" label={t(`state.${snap.state}`, { defaultValue: snap.state })} />
          {snap.degraded && <Chip size="small" color="warning" label={t("state.degraded")} />}
          {snap.last_event_at && fmtIso(snap.last_event_at) ? (
            <Typography variant="body2" color="text.secondary" sx={{ ml: 0.5 }}>
              {t("agent.lastEvent", { time: fmtIso(snap.last_event_at) })}
            </Typography>
          ) : null}
          {updatedAt ? (
            <Typography variant="body2" color="text.secondary" sx={{ ml: 0.5 }}>
              {t("agent.updated", { time: fmtTime(updatedAt) })}
            </Typography>
          ) : null}
        </Stack>

        {snap.detail && (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{snap.detail}</Typography>
        )}

        {manageable ? (
          <>
            <Stack direction="row" spacing={1} sx={{ mt: 2 }}>
              {running ? (
                <Button size="small" variant="outlined" color="inherit" disabled={stop.isPending}
                  onClick={() => stop.mutate()}>{t("common.stop")}</Button>
              ) : (
                <Button size="small" variant="contained" color="primary" disabled={start.isPending}
                  onClick={() => start.mutate()}>{t("common.start")}</Button>
              )}
              <Button size="small" variant="outlined" color="info" onClick={onEdit}>{t("common.editConfig")}</Button>
              <Box sx={{ flex: 1 }} />
              <Button size="small" color="error" variant="outlined"
                onClick={() => setConfirmDel(true)}>{t("common.delete")}</Button>
            </Stack>
            {!running && (
              <Typography variant="caption" color="text.secondary" sx={{ mt: 1, display: "block" }}>
                {t("agent.stoppedHint")}
              </Typography>
            )}
          </>
        ) : (
          <Typography variant="caption" color="text.secondary" sx={{ mt: 2, display: "block" }}>
            {t("agent.autoManaged")}
          </Typography>
        )}

        <MonitorMetrics metrics={snap.metrics} />
      </CardContent>

      <Dialog open={confirmDel} onClose={() => setConfirmDel(false)}>
        <DialogTitle>{t("agent.deleteTitle", { name })}</DialogTitle>
        <DialogContent>
          <DialogContentText>{t("agent.deleteBody")}</DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmDel(false)}>{t("common.cancel")}</Button>
          <Button color="error" disabled={del.isPending} onClick={() => del.mutate()}>{t("common.delete")}</Button>
        </DialogActions>
      </Dialog>
    </Card>
  );
}

// Bridges the console's add/edit triggers to the MonitorWizard (#93). In edit
// mode it loads the agent config to pre-fill the form (and to know the locked
// type) before showing the wizard; add mode opens straight on step 1.
function WizardLauncher({
  mode, name, pluginsData, onClose, onDone, onError,
}: {
  mode: "add" | "edit";
  name?: string;
  pluginsData?: { plugins: PluginInfo[]; presets: PresetInfo[] };
  onClose: () => void;
  onDone: (savedName?: string) => void;
  onError: (e: unknown) => void;
}) {
  const { t } = useTranslation();
  const config = useQuery({
    queryKey: ["agentConfig"], queryFn: api.config, enabled: mode === "edit",
  });
  const existing = mode === "edit"
    ? config.data?.monitors?.find((m) => (m.config?.name ?? m.name) === name)
    : undefined;

  // Wait for the edit config so the form prefills + the type is known.
  if (mode === "edit" && config.isLoading) {
    return (
      <Dialog open onClose={onClose} maxWidth="sm" fullWidth>
        <DialogTitle>{t("agent.editMonitor", { name })}</DialogTitle>
        <DialogContent><Typography>{t("agent.loadingConfig")}</Typography></DialogContent>
      </Dialog>
    );
  }

  return (
    <MonitorWizard
      mode={mode}
      name={name}
      existingConfig={existing?.config}
      existingType={existing?.type_id ?? undefined}
      plugins={pluginsData?.plugins ?? []}
      presets={pluginsData?.presets ?? []}
      onClose={onClose}
      onDone={onDone}
      onError={onError}
    />
  );
}
