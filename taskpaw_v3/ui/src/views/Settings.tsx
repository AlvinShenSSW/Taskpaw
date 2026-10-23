import {
  Alert, Button, Card, CardContent, FormControlLabel, List, ListItemButton, ListItemText, MenuItem, Stack, Switch, TextField,
  Typography, useMediaQuery,
} from "@mui/material";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { type LlmKeySource, type LlmSlot, api } from "../api";
import { Logo } from "../components/Logo";
import { WorkflowSettingsSection } from "../components/WorkflowSettings";
import { NetworkAccess } from "../components/NetworkAccess";
import { RESTART_KEY, restartText } from "../components/setupModel";
import { RestartBanner } from "../components/RestartBanner";
import { LANGS, type Lang, currentLang, setLang } from "../i18n";

// Settings tab (#79 Language + About, #43 agent config). #299 R12: the agent's sections — 通用
// (language, machine name, LLM), 网络与访问, 媒体工作流, 功耗与主机监控, 关于 — with a side nav on
// desktop and a select at 390 px. No copy names a config file: every setting lives here.
export type SettingsSection = "general" | "network" | "workflow" | "power" | "about";
const SECTIONS: Array<[SettingsSection, string]> = [
  ["general", "通用"], ["network", "网络与访问"], ["workflow", "媒体工作流"], ["power", "功耗与主机监控"], ["about", "关于"],
];

export function Settings({ role, section: initial = "general" }: { role: "agent" | "hub"; section?: SettingsSection }) {
  const { t } = useTranslation();
  const [section, setSection] = useState<SettingsSection>(initial);
  useEffect(() => setSection(initial), [initial]);
  const narrow = useMediaQuery("(max-width:599.95px)");
  if (role === "hub") {
    return (
      <Stack spacing={2} sx={{ maxWidth: 720 }}>
        <Typography variant="overline" color="text.secondary">{t("settings.title")}</Typography>
        <LanguageCard />
        <AboutCard />
      </Stack>
    );
  }
  const body = section === "general" ? (
    <>
      <LanguageCard />
      <MachineSection />
      {/* Agent-level LLM API (#178) + two optional fallbacks and the failover switch (#190/#192) */}
      <LlmSection slot="primary" />
      <LlmSection slot="fallback1" />
      <LlmSection slot="fallback2" />
      <FailoverSection />
    </>
  ) : section === "network" ? (
    <Card><CardContent>
      <Typography variant="subtitle1" component="h2" sx={{ mb: 1.5 }}>网络与访问</Typography>
      <PendingRestart />
      <NetworkAccess />
    </CardContent></Card>
  ) : section === "workflow" ? (
    // P2-4: 设置 · 媒体工作流 (role + fields; 加入 while the running role is a worker)
    <WorkflowSettingsSection />
  ) : section === "power" ? (
    <PowerHostSection />
  ) : <AboutCard />;
  return (
    <Stack direction={narrow ? "column" : "row"} spacing={2} alignItems="flex-start">
      {narrow ? (
        <TextField select size="small" label="设置" value={section} fullWidth
          onChange={(e) => setSection(e.target.value as SettingsSection)}>
          {SECTIONS.map(([v, l]) => <MenuItem key={v} value={v}>{l}</MenuItem>)}
        </TextField>
      ) : (
        <List component="nav" aria-label="设置" dense sx={{ width: 180, flex: "none", position: "sticky", top: 64 }}>
          {SECTIONS.map(([v, l]) => (
            <ListItemButton key={v} selected={section === v} onClick={() => setSection(v)} sx={{ borderRadius: 1, minHeight: 44 }}>
              <ListItemText primary={l} />
            </ListItemButton>
          ))}
        </List>
      )}
      <Stack spacing={2} sx={{ maxWidth: 680, flex: 1, minWidth: 0, width: "100%" }}>{body}</Stack>
    </Stack>
  );
}

function LanguageCard() {
  const { t } = useTranslation();
  return (
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
  );
}

function AboutCard() {
  const { t } = useTranslation();
  return (
    <Card>
      <CardContent>
        <Typography variant="subtitle1" sx={{ mb: 1 }}>{t("settings.about")}</Typography>
        <Stack direction="row" alignItems="center" spacing={1.5} sx={{ mb: 1 }}>
          {/* #120: the brand logo replaces the old emoji (MASTER.md: no emoji). */}
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
  );
}

// #299 R11: the saved settings that wait for a restart (GET /control/restart), one banner.
function PendingRestart({ only }: { only?: string[] }) {
  const state = useQuery({ queryKey: RESTART_KEY, queryFn: api.restartState, retry: false });
  const fields = (state.data?.fields ?? []).filter((f) => !only || only.includes(f));
  if (!state.data?.required || fields.length === 0) return null;
  return <Stack sx={{ mb: 2 }}><RestartBanner role="agent" text={restartText(fields)} /></Stack>;
}

// 通用 · 机器名称 (#43's machine field; the address, ports and token moved to 网络与访问)
function MachineSection() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: ["agentConfig"], queryFn: api.config });
  const [machine, setMachine] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  useEffect(() => {
    if (cfg.data && machine === null) setMachine(String(cfg.data.machine ?? ""));
  }, [cfg.data, machine]);
  const save = useMutation({
    mutationFn: () => api.updateConfig({ machine: machine ?? "" }),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["agentConfig"] });
      qc.invalidateQueries({ queryKey: RESTART_KEY });
      setMsg({ kind: "ok", text: res.restart_required ? restartText(["machine"]) : t("settings.saved") });
    },
    onError: (e) => setMsg({ kind: "err", text: e instanceof Error ? e.message : String(e) }),
  });
  return (
    <Card>
      <CardContent>
        <Typography variant="subtitle1" sx={{ mb: 0.5 }}>{t("settings.config")}</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>{t("settings.configHint")}</Typography>
        {cfg.isLoading || machine === null ? (
          <Typography variant="body2" color="text.secondary">{t("common.loading")}</Typography>
        ) : (
          <Stack spacing={1.5}>
            <TextField size="small" label={t("settings.machine")} value={machine} onChange={(e) => setMachine(e.target.value)} />
            {msg && <Alert severity={msg.kind === "ok" ? "success" : "error"}>{msg.text}</Alert>}
            <Stack direction="row">
              <Button variant="contained" sx={{ minHeight: 44 }} disabled={save.isPending || !machine.trim()}
                onClick={() => { setMsg(null); save.mutate(); }}>
                {t("settings.save")}
              </Button>
            </Stack>
          </Stack>
        )}
      </CardContent>
    </Card>
  );
}

// #299 R12: 功耗与主机监控 — the power estimate switch (live) and the host monitor (after a restart).
function PowerHostSection() {
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: ["agentConfig"], queryFn: api.config });
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const save = useMutation({
    mutationFn: (patch: Record<string, unknown>) => api.updateConfig(patch),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agentConfig"] });
      qc.invalidateQueries({ queryKey: RESTART_KEY });
      qc.invalidateQueries({ queryKey: ["power"] });
      setMsg({ kind: "ok", text: "已保存" });
    },
    onError: (e) => setMsg({ kind: "err", text: e instanceof Error ? e.message : String(e) }),
  });
  const power = cfg.data?.power as { enabled?: boolean } | undefined;
  return (
    <Card>
      <CardContent>
        <Typography variant="subtitle1" component="h2" sx={{ mb: 1.5 }}>功耗与主机监控</Typography>
        <PendingRestart only={["host_metrics"]} />
        {cfg.isLoading || !cfg.data ? (
          <Typography variant="body2" color="text.secondary">正在读取设置…</Typography>
        ) : (
          <Stack spacing={1.5}>
            <FormControlLabel label="功耗估算" control={
              <Switch checked={power?.enabled !== false} disabled={save.isPending} inputProps={{ role: "switch" }}
                onChange={(e) => { setMsg(null); save.mutate({ power: { enabled: e.target.checked } }); }} />} />
            <Typography variant="body2" color="text.secondary" sx={{ mt: "-8px !important" }}>
              估算本机用电（显卡读数 + CPU 估算 + 基础功耗），立即生效；估算参数在 功耗 页调整。
            </Typography>
            <FormControlLabel label="主机监控（CPU、内存、显卡）" control={
              <Switch checked={cfg.data.host_metrics !== false} disabled={save.isPending} inputProps={{ role: "switch" }}
                onChange={(e) => { setMsg(null); save.mutate({ host_metrics: e.target.checked }); }} />} />
            <Typography variant="body2" color="text.secondary" sx={{ mt: "-8px !important" }}>重启 TaskPaw 后生效。</Typography>
            {msg && <Alert severity={msg.kind === "ok" ? "success" : "error"}>{msg.text}</Alert>}
          </Stack>
        )}
      </CardContent>
    </Card>
  );
}

// Per-slot LLM wiring (#178 primary, #190 fallbacks): the config-field prefix, the
// env var whose key wins over the stored one, and the fallback's 1-based number.
const LLM_SLOTS: Record<LlmSlot, { prefix: string; env: string; fallback?: number }> = {
  primary: { prefix: "llm_", env: "TASKPAW_LLM_API_KEY" },
  fallback1: { prefix: "llm_fallback1_", env: "TASKPAW_LLM_FALLBACK1_API_KEY", fallback: 1 },
  fallback2: { prefix: "llm_fallback2_", env: "TASKPAW_LLM_FALLBACK2_API_KEY", fallback: 2 },
};

const KEY_SOURCES: ReadonlySet<string> = new Set<LlmKeySource>(["env", "config", "none"]);
const keySource = (v: unknown): LlmKeySource =>
  typeof v === "string" && KEY_SOURCES.has(v) ? (v as LlmKeySource) : "none";

type LlmForm = { api_base: string; model: string; api_key: string; thinking_off: boolean | null };

const normalizedBase = (base: string) => base.trim().replace(/\/+$/, "");

// One LLM provider card (#178; #190 reuses it for fallback 1 / 2): base URL, model
// and a write-only key. GET reports the key as "***" plus its source; an
// env-provided key can't be edited or cleared from the UI (its env var wins).
// Save / Clear / Test send only this slot's fields.
function LlmSection({ slot }: { slot: LlmSlot }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: ["agentConfig"], queryFn: api.config });
  const [form, setForm] = useState<LlmForm | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const { prefix, env, fallback } = LLM_SLOTS[slot];

  // Seed once from the config; the key field always starts blank (blank → keep).
  useEffect(() => {
    if (cfg.data && form === null) {
      const c = cfg.data;
      setForm({
        api_base: String(c[`${prefix}api_base`] ?? ""), model: String(c[`${prefix}model`] ?? ""),
        api_key: "", thinking_off: typeof c[`${prefix}thinking_off`] === "boolean" ? c[`${prefix}thinking_off`] as boolean : null,
      });
    }
  }, [cfg.data, form, prefix]);

  const source = keySource(cfg.data?.[`${prefix}api_key_source`]);
  const fromEnv = source === "env";
  // GET owns the automatic result. Hide it for an edited base until Save refetches it.
  const auto = cfg.data?.[`${prefix}thinking_off_auto`];
  const baseSaved = form !== null && normalizedBase(form.api_base) ===
    normalizedBase(String(cfg.data?.[`${prefix}api_base`] ?? ""));
  const autoLabel = !baseSaved || typeof auto !== "boolean" ? t("settings.llmThinkingAuto") :
    auto ? t("settings.llmThinkingAutoOff") : t("settings.llmThinkingAutoDefault");

  // This slot's form values; a blank key is omitted so the backend keeps the stored one.
  const values = () => {
    const f = form!;
    const v: Record<string, unknown> = {
      [`${prefix}api_base`]: f.api_base, [`${prefix}model`]: f.model,
      [`${prefix}thinking_off`]: f.thinking_off,
    };
    if (f.api_key.trim()) v[`${prefix}api_key`] = f.api_key;
    return v;
  };

  const onSaved = (text: string) => {
    qc.invalidateQueries({ queryKey: ["agentConfig"] });
    setForm((p) => (p ? { ...p, api_key: "" } : p)); // never keep the typed key around
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
    mutationFn: () => api.updateConfig({ [`${prefix}api_key`]: null }),
    onSuccess: () => onSaved(t("settings.llmCleared")),
    onError,
  });
  const test = useMutation({
    mutationFn: () => api.llmTest(values(), slot),
    onSuccess: (r) => {
      if (r.ok) {
        const ok = t("settings.llmTestOk", { model: r.model ?? "", latency: r.latency_ms ?? 0 });
        setMsg({ kind: "ok", text: r.note === "thinking_unsupported" ? `${ok} ${t("settings.llmThinkingUnsupported")}` : ok });
      } else {
        setMsg({ kind: "err", text: t("settings.llmTestFail", { error: r.error ?? "" }) });
      }
    },
    onError: (e) =>
      setMsg({ kind: "err", text: t("settings.llmTestFail", { error: e instanceof Error ? e.message : String(e) }) }),
  });

  const writing = save.isPending || clear.isPending;
  const set = (k: "api_base" | "model" | "api_key") => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm((p) => (p ? { ...p, [k]: e.target.value } : p));

  return (
    <Card>
      <CardContent>
        <Typography variant="subtitle1" sx={{ mb: 0.5 }}>
          {fallback ? t("settings.llmFallback", { n: fallback }) : t("settings.llm")}
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          {fallback ? t("settings.llmFallbackHint") : t("settings.llmHint")}
        </Typography>
        {cfg.isLoading || !form ? (
          <Typography variant="body2" color="text.secondary">{t("common.loading")}</Typography>
        ) : (
          <Stack spacing={1.5}>
            {/* Example placeholders on the primary only: a fallback has no default provider. */}
            <TextField size="small" label={t("settings.llmApiBase")} value={form.api_base}
              onChange={set("api_base")} placeholder={fallback ? undefined : "https://api.x.ai/v1"} />
            <TextField size="small" label={t("settings.llmModel")} value={form.model}
              onChange={set("model")} placeholder={fallback ? undefined : "grok-4.3"} />
            <TextField size="small" type="password" autoComplete="off" label={t("settings.llmApiKey")}
              value={form.api_key} onChange={set("api_key")} disabled={fromEnv}
              placeholder={source !== "none" ? "***" : undefined}
              helperText={fromEnv ? t("settings.llmApiKeyEnv", { env }) : t("settings.llmApiKeyHint", { env })} />
            <TextField select size="small" label={t("settings.llmThinking")}
              value={form.thinking_off === null ? "auto" : String(form.thinking_off)}
              onChange={(e) => setForm((p) => p ? {
                ...p, thinking_off: e.target.value === "auto" ? null : e.target.value === "true",
              } : p)} helperText={t("settings.llmThinkingHint")}>
              <MenuItem value="auto">{autoLabel}</MenuItem>
              <MenuItem value="true">{t("settings.llmThinkingOff")}</MenuItem>
              <MenuItem value="false">{t("settings.llmThinkingDefault")}</MenuItem>
            </TextField>
            {msg && <Alert severity={msg.kind === "ok" ? "success" : "error"}>{msg.text}</Alert>}
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
              <Button variant="contained" disabled={writing}
                onClick={() => { setMsg(null); save.mutate(); }}>
                {t("settings.llmSave")}
              </Button>
              {/* A blank base URL or model can't be tested: the probe would fail misleadingly. */}
              <Button variant="outlined" disabled={test.isPending || !form.api_base.trim() || !form.model.trim()}
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

// #192 failover switch (`llm_failover`, default on): while the primary is
// unavailable, lines go to the fallbacks; off → they wait for the primary (lines it
// refuses still go to the fallbacks). Saved on toggle and applied live by the agent;
// the switch flips back if the save fails.
function FailoverSection() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const cfg = useQuery({ queryKey: ["agentConfig"], queryFn: api.config });
  const [on, setOn] = useState<boolean | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);

  // Seed once; an agent that doesn't report the switch has it on (the default).
  useEffect(() => {
    if (cfg.data && on === null) setOn(cfg.data.llm_failover !== false);
  }, [cfg.data, on]);

  const save = useMutation({
    mutationFn: (value: boolean) => api.updateConfig({ llm_failover: value }),
    onMutate: (value) => {
      setMsg(null);
      setOn(value);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["agentConfig"] });
      setMsg({ kind: "ok", text: t("settings.saved") });
    },
    onError: (e, value) => {
      setOn(!value);
      setMsg({ kind: "err", text: e instanceof Error ? e.message : String(e) });
    },
  });

  return (
    <Card>
      <CardContent>
        <Typography variant="subtitle1" sx={{ mb: 0.5 }}>{t("settings.llmFailoverTitle")}</Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
          {t("settings.llmFailoverHint")}
        </Typography>
        {cfg.isLoading || on === null ? (
          <Typography variant="body2" color="text.secondary">{t("common.loading")}</Typography>
        ) : (
          <Stack spacing={1} alignItems="flex-start">
            <FormControlLabel label={t("settings.llmFailover")}
              control={<Switch checked={on} disabled={save.isPending}
                onChange={(e) => save.mutate(e.target.checked)} />} />
            {msg && (
              <Alert severity={msg.kind === "ok" ? "success" : "error"} sx={{ alignSelf: "stretch" }}>
                {msg.text}
              </Alert>
            )}
          </Stack>
        )}
      </CardContent>
    </Card>
  );
}
