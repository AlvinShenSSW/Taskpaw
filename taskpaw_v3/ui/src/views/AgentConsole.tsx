import {
  Alert, Box, Button, Card, CardContent, Chip, Dialog, DialogActions,
  DialogContent, DialogContentText, DialogTitle, List, ListItemButton,
  MenuItem, Snackbar, Stack, TextField, Typography,
} from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";
import { api, type MonitorSnapshot, type PluginInfo } from "../api";
import { SchemaForm } from "../components/SchemaForm";
import { StatusDot } from "../components/StatusDot";
import { MonitorMetrics } from "../components/MonitorMetrics";

// Local control panel for ONE machine (design pages/agent-console.md): left rail
// of this machine's monitors + an Add button; main pane = the selected monitor's
// live status + Start/Stop/Edit/Delete; an Add/Edit dialog renders the plugin's
// schema-driven config form (#57).
export function AgentConsole() {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["agentStatus"], queryFn: api.agentStatus, refetchInterval: 5000 });
  const plugins = useQuery({ queryKey: ["agentPlugins"], queryFn: api.plugins });
  const [selected, setSelected] = useState<string | null>(null);
  const [dialog, setDialog] = useState<null | { mode: "add" } | { mode: "edit"; name: string }>(null);
  const [error, setError] = useState<string | null>(null);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["agentStatus"] });
    qc.invalidateQueries({ queryKey: ["agentConfig"] });
  };
  const onErr = (e: unknown) => setError(e instanceof Error ? e.message : String(e));

  if (status.isLoading) return <Typography>Loading…</Typography>;
  if (status.error) return <Alert severity="error">Agent unreachable: {String(status.error)}</Alert>;

  const monitors = status.data?.monitors ?? {};
  const names = Object.keys(monitors);
  const current = selected && monitors[selected] ? selected : names[0];

  return (
    <Stack direction="row" spacing={2} sx={{ minHeight: "70dvh" }}>
      <Card sx={{ width: 280, flex: "0 0 auto" }}>
        <CardContent>
          <Stack direction="row" alignItems="center" justifyContent="space-between" sx={{ mb: 1 }}>
            <Typography variant="overline" color="text.secondary">
              {status.data?.machine} — monitors
            </Typography>
            <Button size="small" variant="contained" onClick={() => setDialog({ mode: "add" })}>
              + Add
            </Button>
          </Stack>
          {names.length === 0 && (
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>
              No monitors yet — add one to start watching this machine.
            </Typography>
          )}
          <List dense>
            {names.map((n) => (
              <ListItemButton key={n} selected={n === current} onClick={() => setSelected(n)}>
                <StatusDot state={monitors[n].state} />
                <Typography variant="body2" noWrap sx={{ flex: 1 }}>{n}</Typography>
                {monitors[n].type_id && (
                  <Chip size="small" label={monitors[n].type_id} sx={{ ml: 0.5 }} />
                )}
              </ListItemButton>
            ))}
          </List>
        </CardContent>
      </Card>

      <Box sx={{ flex: 1 }}>
        {current && monitors[current] ? (
          <MonitorDetail
            name={current}
            snap={monitors[current]}
            onEdit={() => setDialog({ mode: "edit", name: current })}
            onChanged={invalidate}
            onError={onErr}
          />
        ) : (
          <Typography color="text.secondary">Select or add a monitor.</Typography>
        )}
      </Box>

      {dialog && (
        <MonitorDialog
          mode={dialog.mode}
          name={dialog.mode === "edit" ? dialog.name : undefined}
          plugins={plugins.data?.plugins ?? []}
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

function MonitorDetail({
  name, snap, onEdit, onChanged, onError,
}: {
  name: string;
  snap: MonitorSnapshot;
  onEdit: () => void;
  onChanged: () => void;
  onError: (e: unknown) => void;
}) {
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
          <Chip size="small" label={snap.state} />
          {snap.degraded && <Chip size="small" color="warning" label="degraded" />}
        </Stack>

        {snap.detail && (
          <Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{snap.detail}</Typography>
        )}

        {manageable ? (
          <>
            <Stack direction="row" spacing={1} sx={{ mt: 2 }}>
              {running ? (
                <Button size="small" variant="outlined" color="inherit" disabled={stop.isPending}
                  onClick={() => stop.mutate()}>Stop</Button>
              ) : (
                <Button size="small" variant="contained" color="primary" disabled={start.isPending}
                  onClick={() => start.mutate()}>Start</Button>
              )}
              <Button size="small" variant="outlined" color="info" onClick={onEdit}>Edit config</Button>
              <Box sx={{ flex: 1 }} />
              <Button size="small" color="error" variant="outlined"
                onClick={() => setConfirmDel(true)}>Delete</Button>
            </Stack>
            {!running && (
              <Typography variant="caption" color="text.secondary" sx={{ mt: 1, display: "block" }}>
                Stopped — click <b>Start</b> to run it, or <b>Edit config</b> to change settings.
              </Typography>
            )}
          </>
        ) : (
          <Typography variant="caption" color="text.secondary" sx={{ mt: 2, display: "block" }}>
            Auto-managed system monitor — always on.
          </Typography>
        )}

        <MonitorMetrics metrics={snap.metrics} />
      </CardContent>

      <Dialog open={confirmDel} onClose={() => setConfirmDel(false)}>
        <DialogTitle>Delete monitor “{name}”?</DialogTitle>
        <DialogContent>
          <DialogContentText>This removes it from this agent's config. It can't be undone.</DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmDel(false)}>Cancel</Button>
          <Button color="error" disabled={del.isPending} onClick={() => del.mutate()}>Delete</Button>
        </DialogActions>
      </Dialog>
    </Card>
  );
}

function MonitorDialog({
  mode, name, plugins, onClose, onDone, onError,
}: {
  mode: "add" | "edit";
  name?: string;
  plugins: PluginInfo[];
  onClose: () => void;
  onDone: (savedName?: string) => void;
  onError: (e: unknown) => void;
}) {
  // Operator-selectable plugins only (host_metrics etc. are system/auto-injected).
  const selectable = useMemo(() => plugins.filter((p) => !p.system), [plugins]);
  const [typeId, setTypeId] = useState<string>(selectable[0]?.type_id ?? "");
  // The dialog can open before /control/plugins resolves (selectable empty →
  // typeId ""); the useState initializer won't re-run, so select the first type
  // once the catalog arrives, otherwise the form never appears (Codex #57b).
  useEffect(() => {
    if (!typeId && selectable.length > 0) setTypeId(selectable[0].type_id);
  }, [selectable, typeId]);

  // Edit mode: load the current config to pre-fill the form.
  const config = useQuery({
    queryKey: ["agentConfig"],
    queryFn: api.config,
    enabled: mode === "edit",
  });
  const existing = mode === "edit"
    ? config.data?.monitors?.find((m) => (m.config?.name ?? m.name) === name)
    : undefined;
  const editType = existing?.type_id;
  const plugin = plugins.find((p) => p.type_id === (mode === "edit" ? editType : typeId));

  const save = useMutation({
    mutationFn: (formData: Record<string, unknown>) =>
      mode === "add"
        ? api.addMonitor({ type_id: typeId, config: formData })
        : api.updateMonitor(name as string, { config: formData }),
    // Hand back the saved monitor's name so the console can auto-select it — the
    // operator lands on its detail pane (Start / Edit config) without hunting.
    onSuccess: (_res, formData) =>
      onDone(mode === "add" ? String(formData.name ?? "") || undefined : name),
    onError,
  });

  // Plugin ui_schema + a clear submit label + (edit) lock the stable `name`
  // (the backend ignores name changes on update; show it read-only) (#70).
  const formUiSchema = useMemo(() => {
    const base = (plugin?.ui_schema as Record<string, unknown>) ?? {};
    return {
      ...base,
      "ui:submitButtonOptions": { submitText: mode === "add" ? "Add monitor" : "Save changes" },
      ...(mode === "edit"
        ? { name: { ...(base.name as object), "ui:readonly": true } }
        : {}),
    };
  }, [plugin, mode]);

  return (
    <Dialog open onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{mode === "add" ? "Add monitor" : `Edit “${name}”`}</DialogTitle>
      <DialogContent>
        {mode === "add" && (
          <TextField select fullWidth label="Type" value={typeId} sx={{ my: 1 }}
            onChange={(e) => setTypeId(e.target.value)}>
            {selectable.map((p) => (
              <MenuItem key={p.type_id} value={p.type_id}>{p.display_name}</MenuItem>
            ))}
          </TextField>
        )}
        {mode === "edit" && config.isLoading && <Typography>Loading config…</Typography>}
        {plugin ? (
          <SchemaForm
            schema={plugin.json_schema}
            uiSchema={formUiSchema}
            formData={mode === "edit" ? existing?.config : undefined}
            onSubmit={(d) => save.mutate(d as Record<string, unknown>)}
          />
        ) : (
          mode === "add" && <Typography color="text.secondary">No selectable monitor types.</Typography>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose}>Cancel</Button>
      </DialogActions>
    </Dialog>
  );
}
