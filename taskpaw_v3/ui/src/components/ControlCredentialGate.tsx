import { useState } from "react";
import { Alert, Box, Button, TextField, Typography } from "@mui/material";
import { useTranslation } from "react-i18next";
import { connectDevControlCredential, controlBaseForRole, type ControlRole } from "../api";

export function ControlCredentialGate({ role, desktop }: { role: ControlRole; desktop: boolean }) {
  const { t } = useTranslation();
  const [token, setToken] = useState("");
  const [baseUrl, setBaseUrl] = useState(() => controlBaseForRole(role));
  const [busy, setBusy] = useState(false);
  const [failed, setFailed] = useState(false);
  if (desktop) return <Alert severity="error">{t("control.reopen")}</Alert>;
  return <Box component="form" sx={{ maxWidth: 480, mx: "auto", py: 4 }} onSubmit={async e => {
    e.preventDefault(); setBusy(true); setFailed(false);
    const candidate = token; setToken("");
    try { await connectDevControlCredential(role, candidate, baseUrl); }
    catch { setFailed(true); }
    finally { setBusy(false); }
  }}>
    <Typography variant="h6">{t("control.title", { role: t(`app.${role}`) })}</Typography>
    <Typography sx={{ my: 2 }}>{t("control.help")}</Typography>
    {failed && <Alert severity="error">{t("control.failed")}</Alert>}
    <TextField fullWidth label={t("control.endpoint")} value={baseUrl} disabled={busy} autoComplete="off"
      sx={{ my: 2 }} onChange={e => setBaseUrl(e.target.value)} inputProps={{ spellCheck: false }} />
    <TextField fullWidth type="password" label={t("control.token")} value={token}
      autoComplete="off" disabled={busy} onChange={e => setToken(e.target.value)}
      inputProps={{ spellCheck: false, autoCapitalize: "none" }} />
    <Button type="submit" disabled={busy || !token} sx={{ mt: 2 }}>{t("control.connect")}</Button>
  </Box>;
}
