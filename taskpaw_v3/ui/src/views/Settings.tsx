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

      {/* Agent-level LLM API (#178) — agent role only */}
      {role === "agent" && <LlmSection />}

      {/* About */}
      <Card>
        <CardContent>
          <Typography variant="subtitle1" sx={{ mb: 1 }}>{t("settings.about")}</Typography>
          <Stack direction="row" alignItems="center" spacing={1.5} sx={{ mb: 1 }}>
            {/* #120: the brand logo replaces the old 🐾 emoji (MASTER.md: no emoji). */}
            <Logo size={44} alt="TaskPaw" />
            <Stack direction="row" alignItems="baseline" spacing={1}>
              <Typography variant="h6">TaskPaw</Typography>
              <Typography variant="caption" color="text.secondary">{`v${__APP_VERSION__}`}</Typography>
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

type LlmForm = { llm_api_base: string; llm_model: string; llm_api_key: string };

// #178: one agent-level LLM endpoint (base URL, model, key). The key is write-only
// here: GET reports it as "***" plus its source; an env-provided key can't be
// edited or cleared from the UI (TASKPAW_LLM_API_KEY wins).
function LlmSection() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: ["agentConfig"], queryFn: api.config });
  const [form, setForm] = useState<LlmForm | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  // Seed once from the config; the key field always starts blank (blank → keep).
  useEffect(() => {
    if (cfg.data && form === null) {
      const c = cfg.data;
      setForm({
        llm_api_base: String(c.llm_api_base ?? ""), llm_model: String(c.llm_model ?? ""),
        llm_api_key: "",
      });
    }
  }, [cfg.data, form]);

  const source = cfg.data?.llm_api_key_source ?? "none";
  const fromEnv = source === "env";

  // Current form values; a blank key is omitted so the backend uses the stored one.
  const values = () => {
    const f = form!;
    const v: Record<string, unknown> = { llm_api_base: f.llm_api_base, llm_model: f.llm_model };
    if (f.llm_api_key.trim()) v.llm_api_key = f.llm_api_key;
    return v;
  };

  const onSaved = (text: string) => {
    qc.invalidateQueries({ queryKey: ["agentConfig"] });
    setForm((p) => (p ? { ...p, llm_api_key: "" } : p)); // never keep the typed key around
    setMsg({ kind: "ok", text });
  };
  const onError = (e: unknown) =>
    setMsg({ kind: "err", text: e instanceof Error ? e.message : String(e) });

  const save = useMutation({
    mutationFn: () => api.updateConfig(values()),
    onSuccess: () => onSaved(t("settings.llmSaved")),
    onError,
  });
  const clear = useMutation({
    mutationFn: () => api.updateConfig({ llm_api_key: null }),
    onSuccess: () => onSaved(t("settings.llmCleared")),
    onError,
  });
  const test = useMutation({
    mutationFn: () => api.llmTest(values()),
    onSuccess: (r) => {
      if (r.ok) {
        const ok = t("settings.llmTestOk", { model: r.model ?? "", latency: r.latency_ms ?? 0 });
        setMsg({ kind: "ok", text: r.truncated ? `${ok} ${t("settings.llmTestTruncated")}` : ok });
      } else {
        setMsg({ kind: "err", text: t("settings.llmTestFail", { error: r.error ?? "" }) });
      }
    },
    onError: (e) =>
      setMsg({ kind: "err", text: t("settings.llmTestFail", { error: e instanceof Error ? e.message : String(e) }) }),
  });

  const writing = save.isPending || clear.isPending;
  const set = (k: keyof LlmForm) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((p) => (p ? { ...p, [k]: e.target.value } : p));

  return (
    <Card>
      <CardContent>
        <Typography variant="subtitle1" sx={{ mb: 0.5 }}>{t("settings.llm")}</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          {t("settings.llmHint")}
        </Typography>
        {cfg.isLoading || !form ? (
          <Typography variant="body2" color="text.secondary">{t("common.loading")}</Typography>
        ) : (
          <Stack spacing={1.5}>
            <TextField size="small" label={t("settings.llmApiBase")} value={form.llm_api_base}
              onChange={set("llm_api_base")} placeholder="https://openrouter.ai/api/v1" />
            <TextField size="small" label={t("settings.llmModel")} value={form.llm_model}
              onChange={set("llm_model")} placeholder="x-ai/grok-4.1-fast" />
            <TextField size="small" type="password" autoComplete="off" label={t("settings.llmApiKey")}
              value={form.llm_api_key} onChange={set("llm_api_key")} disabled={fromEnv}
              placeholder={source !== "none" ? "***" : undefined}
              helperText={fromEnv ? t("settings.llmApiKeyEnv") : t("settings.llmApiKeyHint")} />
            {msg && <Alert severity={msg.kind === "ok" ? "success" : "error"}>{msg.text}</Alert>}
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
              <Button variant="contained" disabled={writing}
                onClick={() => { setMsg(null); save.mutate(); }}>
                {t("settings.llmSave")}
              </Button>
              <Button variant="outlined" disabled={test.isPending}
                onClick={() => { setMsg(null); test.mutate(); }}>
                {test.isPending ? t("settings.llmTesting") : t("settings.llmTest")}
              </Button>
              {!fromEnv && (
                <Button variant="outlined" color="inherit" disabled={writing}
                  onClick={() => { setMsg(null); clear.mutate(); }}>
                  {t("settings.llmClear")}
                </Button>
              )}
            </Stack>
          </Stack>
        )}
      </CardContent>
    </Card>
  );
}
