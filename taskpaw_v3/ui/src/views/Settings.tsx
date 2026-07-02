import {
  Alert, Button, Card, CardContent, MenuItem, Stack, TextField, Typography,
} from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../api";
import { Logo } from "../components/Logo";
import { LANGS, type Lang, currentLang, setLang } from "../i18n";

// Settings tab (#79 Language + About, #43 agent config). Config editing shows for
// the agent role only — the Hub's OpenClaw config is a separate surface (#43 f/u).
export function Settings({ role }: { role: "agent" | "hub" }) {
  const { t } = useTranslation();
  return (
    <Stack spacing={2} sx={{ maxWidth: 640 }}>
      <Typography variant="overline" color="text.secondary">{t("settings.title")}</Typography>

      {/* Language */}
      <Card>
        <CardContent>
          <Typography variant="subtitle1" sx={{ mb: 0.5 }}>{t("settings.language")}</Typography>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
            {t("settings.languageHint")}
          </Typography>
          <TextField select size="small" value={currentLang()} sx={{ minWidth: 200 }}
            onChange={(e) => setLang(e.target.value as Lang)}>
            {LANGS.map((l) => (
              <MenuItem key={l.value} value={l.value}>{l.label}</MenuItem>
            ))}
          </TextField>
        </CardContent>
      </Card>

      {/* Agent config (#43) — agent role only */}
      {role === "agent" && <ConfigSection />}

      {/* About */}
      <Card>
        <CardContent>
          <Typography variant="subtitle1" sx={{ mb: 1 }}>{t("settings.about")}</Typography>
          <Stack direction="row" alignItems="center" spacing={1.5} sx={{ mb: 1 }}>
            {/* #120: the brand logo replaces the old 🐾 emoji (MASTER.md: no emoji). */}
            <Logo size={44} alt="TaskPaw" />
            <Stack direction="row" alignItems="baseline" spacing={1}>
              <Typography variant="h6">TaskPaw</Typography>
              <Typography variant="caption" color="text.secondary">v3.0.0-dev</Typography>
            </Stack>
          </Stack>
          <Typography variant="body2" sx={{ mb: 1.5 }}>{t("settings.aboutBody")}</Typography>
          <Typography variant="body2" color="text.secondary">{t("settings.author")}</Typography>
          <Typography variant="caption" color="text.secondary">{t("settings.copyright")}</Typography>
        </CardContent>
      </Card>
    </Stack>
  );
}

type Form = {
  machine: string; bind_host: string; bind_port: string;
  control_host: string; control_port: string; api_token: string;
};

function ConfigSection() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: ["agentConfig"], queryFn: api.config });
  const [form, setForm] = useState<Form | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  // Seed the form once the config arrives (api_token comes masked as "***").
  useEffect(() => {
    if (cfg.data && form === null) {
      const c = cfg.data as Record<string, unknown>;
      setForm({
        machine: String(c.machine ?? ""), bind_host: String(c.bind_host ?? ""),
        bind_port: String(c.bind_port ?? ""), control_host: String(c.control_host ?? ""),
        control_port: String(c.control_port ?? ""), api_token: "",
      });
    }
  }, [cfg.data, form]);

  const save = useMutation({
    mutationFn: () => {
      const f = form!;
      const patch: Record<string, unknown> = {
        machine: f.machine, bind_host: f.bind_host, bind_port: Number(f.bind_port),
        control_host: f.control_host, control_port: Number(f.control_port),
      };
      if (f.api_token.trim()) patch.api_token = f.api_token; // blank → keep current
      return api.updateConfig(patch);
    },
    onSuccess: (res) => {
      // Refresh the shared config cache so the auth-disabled banner (#145) and this
      // form reflect a just-set/changed token immediately, not a stale cached one.
      qc.invalidateQueries({ queryKey: ["agentConfig"] });
      setMsg({ kind: "ok", text: res.restart_required ? t("settings.restartNeeded") : t("settings.saved") });
    },
    onError: (e) => setMsg({ kind: "err", text: e instanceof Error ? e.message : String(e) }),
  });

  const set = (k: keyof Form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((p) => (p ? { ...p, [k]: e.target.value } : p));

  return (
    <Card>
      <CardContent>
        <Typography variant="subtitle1" sx={{ mb: 0.5 }}>{t("settings.config")}</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          {t("settings.configHint")}
        </Typography>
        {cfg.isLoading || !form ? (
          <Typography variant="body2" color="text.secondary">{t("common.loading")}</Typography>
        ) : (
          <Stack spacing={1.5}>
            <TextField size="small" label={t("settings.machine")} value={form.machine} onChange={set("machine")} />
            <Stack direction="row" spacing={1.5}>
              <TextField size="small" label={t("settings.bindHost")} value={form.bind_host}
                onChange={set("bind_host")} sx={{ flex: 1 }} />
              <TextField size="small" label={t("settings.bindPort")} value={form.bind_port}
                onChange={set("bind_port")} sx={{ width: 120 }} inputMode="numeric" />
            </Stack>
            <Stack direction="row" spacing={1.5}>
              <TextField size="small" label={t("settings.controlHost")} value={form.control_host}
                onChange={set("control_host")} sx={{ flex: 1 }} />
              <TextField size="small" label={t("settings.controlPort")} value={form.control_port}
                onChange={set("control_port")} sx={{ width: 120 }} inputMode="numeric" />
            </Stack>
            <TextField size="small" type="password" label={t("settings.apiToken")} value={form.api_token}
              onChange={set("api_token")} placeholder="***" helperText={t("settings.apiTokenHint")} />
            {msg && <Alert severity={msg.kind === "ok" ? "success" : "error"}>{msg.text}</Alert>}
            <Stack direction="row">
              <Button variant="contained" disabled={save.isPending} onClick={() => { setMsg(null); save.mutate(); }}>
                {t("settings.save")}
              </Button>
            </Stack>
          </Stack>
        )}
      </CardContent>
    </Card>
  );
}
