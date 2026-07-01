import {
  Alert, Box, Button, Card, CardContent, Dialog, DialogActions, DialogContent,
  DialogTitle, IconButton, Stack, Switch, TextField, Tooltip, Typography,
} from "@mui/material";
import DeleteIcon from "@mui/icons-material/DeleteOutline";
import EditIcon from "@mui/icons-material/EditOutlined";
import CheckIcon from "@mui/icons-material/Check";
import CloseIcon from "@mui/icons-material/Close";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type HubServer } from "../api";

// Manage the agents the Hub polls, from the dashboard (#124): add / edit (name,
// ip, port) / enable-toggle / delete, plus the polling token. Wraps the Bearer-
// gated Hub mutation endpoints; the 5s hubStatus poll (invalidated on success)
// refreshes the list.
export function HubAgentManager({ servers }: { servers: HubServer[] }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries({ queryKey: ["hubStatus"] });
  const [err, setErr] = useState<string | null>(null);
  const onErr = (e: unknown) => setErr(e instanceof Error ? e.message : String(e));

  const [add, setAdd] = useState({ name: "", ip: "", port: "5680" });
  const [editId, setEditId] = useState<number | null>(null);
  const [edit, setEdit] = useState({ name: "", ip: "", port: "" });
  const [confirmDel, setConfirmDel] = useState<HubServer | null>(null);
  const [token, setToken] = useState("");

  const addMut = useMutation({
    mutationFn: () => api.hubAddServer({ name: add.name.trim(), ip: add.ip.trim(), port: Number(add.port) }),
    onSuccess: () => { setAdd({ name: "", ip: "", port: "5680" }); setErr(null); refresh(); },
    onError: onErr,
  });
  const updMut = useMutation({
    mutationFn: (v: { id: number; patch: Record<string, unknown> }) => api.hubUpdateServer(v.id, v.patch),
    onSuccess: () => { setEditId(null); setErr(null); refresh(); },
    onError: onErr,
  });
  const delMut = useMutation({
    mutationFn: (id: number) => api.hubRemoveServer(id),
    onSuccess: () => { setConfirmDel(null); setErr(null); refresh(); },
    onError: onErr,
  });
  const tokMut = useMutation({
    mutationFn: (v: string) => api.hubSetPollingToken(v),
    onSuccess: () => { setToken(""); setErr(null); },
    onError: onErr,
  });

  const startEdit = (s: HubServer) => {
    setEditId(s.id);
    setEdit({ name: s.name, ip: s.ip, port: String(s.port) });
  };
  // A port field is valid only as a 1–65535 integer — gate the buttons so the UI
  // doesn't fire a request it already knows the backend will 400 (Kimi).
  const validPort = (v: string) => /^\d+$/.test(v.trim()) && +v >= 1 && +v <= 65535;

  return (
    <Card>
      <CardContent>
        <Typography variant="overline" color="text.secondary">{t("hub.manageAgents")}</Typography>
        {err && <Alert severity="error" sx={{ my: 1 }} onClose={() => setErr(null)}>{err}</Alert>}

        <Stack spacing={1} sx={{ mt: 1 }}>
          {servers.map((s) => (
            <Box key={s.id} sx={{ display: "flex", alignItems: "center", gap: 1,
              border: "1px solid", borderColor: "divider", borderRadius: 1, p: 1 }}>
              {editId === s.id ? (
                <>
                  <TextField size="small" label={t("hub.mName")} value={edit.name}
                    onChange={(e) => setEdit({ ...edit, name: e.target.value })} sx={{ flex: 1 }} />
                  <TextField size="small" label={t("hub.mIp")} value={edit.ip}
                    onChange={(e) => setEdit({ ...edit, ip: e.target.value })} sx={{ flex: 1 }} />
                  <TextField size="small" label={t("hub.mPort")} value={edit.port} inputMode="numeric"
                    onChange={(e) => setEdit({ ...edit, port: e.target.value })} sx={{ width: 96 }} />
                  <Tooltip title={t("common.save")}>
                    <span><IconButton size="small" color="primary"
                      disabled={updMut.isPending || !edit.name.trim() || !edit.ip.trim() || !validPort(edit.port)}
                      onClick={() => updMut.mutate({ id: s.id,
                        patch: { name: edit.name.trim(), ip: edit.ip.trim(), port: Number(edit.port) } })}>
                      <CheckIcon fontSize="small" />
                    </IconButton></span>
                  </Tooltip>
                  <IconButton size="small" onClick={() => setEditId(null)}><CloseIcon fontSize="small" /></IconButton>
                </>
              ) : (
                <>
                  <Typography sx={{ fontWeight: 600, flex: 1 }}>{s.name}</Typography>
                  <Typography variant="body2" color="text.secondary" sx={{ flex: 1,
                    fontFamily: '"Fira Code", monospace' }}>{s.ip}:{s.port}</Typography>
                  <Tooltip title={s.enabled ? t("state.enabled") : t("state.disabled")}>
                    <Switch size="small" checked={!!s.enabled} disabled={updMut.isPending}
                      onChange={(e) => updMut.mutate({ id: s.id, patch: { enabled: e.target.checked } })} />
                  </Tooltip>
                  <IconButton size="small" aria-label={t("common.editConfig")} onClick={() => startEdit(s)}>
                    <EditIcon fontSize="small" />
                  </IconButton>
                  <IconButton size="small" color="error" aria-label={t("common.delete")}
                    onClick={() => setConfirmDel(s)}>
                    <DeleteIcon fontSize="small" />
                  </IconButton>
                </>
              )}
            </Box>
          ))}
          {servers.length === 0 && (
            <Typography variant="body2" color="text.secondary">{t("hub.noAgents")}</Typography>
          )}
        </Stack>

        {/* Add a new agent */}
        <Stack direction="row" spacing={1} sx={{ mt: 1.5 }} alignItems="center">
          <TextField size="small" label={t("hub.mName")} value={add.name}
            onChange={(e) => setAdd({ ...add, name: e.target.value })} sx={{ flex: 1 }} />
          <TextField size="small" label={t("hub.mIp")} value={add.ip} placeholder="192.168.1.80"
            onChange={(e) => setAdd({ ...add, ip: e.target.value })} sx={{ flex: 1 }} />
          <TextField size="small" label={t("hub.mPort")} value={add.port} inputMode="numeric"
            onChange={(e) => setAdd({ ...add, port: e.target.value })} sx={{ width: 96 }} />
          <Button variant="contained"
            disabled={addMut.isPending || !add.name.trim() || !add.ip.trim() || !validPort(add.port)}
            onClick={() => addMut.mutate()}>{t("common.add")}</Button>
        </Stack>

        {/* Polling token (matches each agent's api_token) */}
        <Stack direction="row" spacing={1} sx={{ mt: 1.5 }} alignItems="flex-start">
          <TextField size="small" type="password" label={t("hub.pollingToken")} value={token}
            onChange={(e) => setToken(e.target.value)} helperText={t("hub.pollingTokenHint")} sx={{ flex: 1 }} />
          {/* Save only when non-blank (no accidental clear); a separate Clear button
              is the explicit way to remove the token (Codex wants clearable; Kimi
              wants no accidental clear). */}
          <Button variant="outlined" disabled={tokMut.isPending || !token} sx={{ mt: 0.25 }}
            onClick={() => tokMut.mutate(token)}>{t("common.save")}</Button>
          <Button variant="text" color="inherit" disabled={tokMut.isPending} sx={{ mt: 0.25 }}
            onClick={() => tokMut.mutate("")}>{t("hub.clearToken")}</Button>
        </Stack>
      </CardContent>

      <Dialog open={confirmDel !== null} onClose={() => setConfirmDel(null)}>
        <DialogTitle>{t("hub.deleteAgentTitle", { name: confirmDel?.name })}</DialogTitle>
        <DialogContent><Typography>{t("hub.deleteAgentBody")}</Typography></DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmDel(null)}>{t("common.cancel")}</Button>
          <Button color="error" disabled={delMut.isPending}
            onClick={() => confirmDel && delMut.mutate(confirmDel.id)}>{t("common.delete")}</Button>
        </DialogActions>
      </Dialog>
    </Card>
  );
}
