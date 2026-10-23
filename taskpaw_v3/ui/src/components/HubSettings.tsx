import {
  Alert, Button, Card, CardContent, FormControlLabel, IconButton, InputAdornment, MenuItem, Stack, Switch, TextField, Typography,
} from "@mui/material";
import VisibilityIcon from "@mui/icons-material/Visibility";
import VisibilityOffIcon from "@mui/icons-material/VisibilityOff";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { api, HttpError, type HubSettingsState, type HubSettingsValues, type OpenclawTest } from "../api";
import { RestartBanner } from "./RestartBanner";

// #299 R14/R15 (board Hub-Settings): the Hub's settings — OpenClaw 启用 / 地址 / 令牌 / 测试发送, 主控连接 (the
// servers API + polling_token), and 高级: 轮询间隔, status.md (and where), 日志保留天数, 局域网只读接口.
// Saved as store overrides (hub.yaml is never rewritten); OpenClaw and the polling token apply at once,
// the rest after 立即重启. Tokens are write-only: a blank field keeps the saved one.
const HUB_KEY = ["hubSettings"];
const LABELS: Record<string, string> = {
  openclaw_enabled: "OpenClaw 通知", openclaw_url: "OpenClaw 地址", openclaw_token: "OpenClaw 令牌", polling_token: "主控令牌",
  poll_interval: "轮询间隔", write_status_md: "写入 status.md", status_md_path: "status.md 位置",
  status_log_retention_days: "日志保留天数", lan_access: "局域网只读接口", bind_host: "本机地址", api_token: "只读接口令牌",
};

function openclawResult(r: OpenclawTest): string {
  if (r.ok) return `已送达 · ${r.at} · 用时 ${r.elapsed_s} 秒`;
  if (r.error === "unreachable") return "无法连接";
  if (r.error === "token") return "令牌错误";
  if (r.error === "timeout") return "超时";
  return `HTTP ${r.status ?? ""}`.trim();
}

function newToken(): string {
  const alphabet = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return `tp_${Array.from(bytes, (b) => alphabet[b % 62]).join("")}`;
}

type Form = {
  openclaw_enabled: boolean; openclaw_url: string; openclaw_token: string; poll_interval: number; write_status_md: boolean;
  status_md_path: string; status_log_retention_days: string; lan_access: boolean; api_token: string;
};
const formOf = (v: HubSettingsValues): Form => ({
  openclaw_enabled: v.openclaw_enabled, openclaw_url: v.openclaw_url, openclaw_token: "", poll_interval: v.poll_interval,
  write_status_md: v.write_status_md, status_md_path: v.status_md_path ?? "", status_log_retention_days: String(v.status_log_retention_days),
  lan_access: v.lan_access, api_token: "",
});

export function HubSettings() {
  const state = useQuery({ queryKey: HUB_KEY, queryFn: api.hubSettings, retry: false });
  if (state.isLoading) return <Typography variant="body2" color="text.secondary">正在读取设置…</Typography>;
  if (state.error || !state.data?.saved) return <Alert severity="error">无法读取 Hub 设置</Alert>;
  return <HubForm key={JSON.stringify(state.data.saved)} state={state.data} />;
}

function HubForm({ state }: { state: HubSettingsState }) {
  const qc = useQueryClient();
  const saved = state.saved;
  const [form, setForm] = useState<Form>(() => formOf(saved));
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [showToken, setShowToken] = useState(false);
  const [tested, setTested] = useState<string | null>(null);
  const set = <K extends keyof Form>(k: K, v: Form[K]) => {
    setForm((p) => ({ ...p, [k]: v }));
    setErrors((p) => { const n = { ...p }; delete n[k]; return n; });
  };
  const patch = () => {
    const out: Record<string, unknown> = {};
    if (form.openclaw_enabled !== saved.openclaw_enabled) out.openclaw_enabled = form.openclaw_enabled;
    if (form.openclaw_url.trim() !== saved.openclaw_url) out.openclaw_url = form.openclaw_url.trim();
    if (form.openclaw_token.trim()) out.openclaw_token = form.openclaw_token.trim();
    if (form.poll_interval !== saved.poll_interval) out.poll_interval = form.poll_interval;
    if (form.write_status_md !== saved.write_status_md) out.write_status_md = form.write_status_md;
    if (form.status_md_path.trim() !== (saved.status_md_path ?? "")) out.status_md_path = form.status_md_path.trim() || null;
    const days = form.status_log_retention_days.trim();
    if (days !== String(saved.status_log_retention_days)) out.status_log_retention_days = /^\d+$/.test(days) ? Number(days) : days;
    if (form.lan_access !== saved.lan_access) out.lan_access = form.lan_access;
    if (form.api_token.trim()) out.api_token = form.api_token.trim();
    return out;
  };
  const changes = patch();
  const dirty = Object.keys(changes).length > 0;
  const save = useMutation({
    mutationFn: () => api.hubSaveSettings(changes),
    onSuccess: () => { setMsg({ kind: "ok", text: "已保存" }); qc.invalidateQueries({ queryKey: HUB_KEY }); },
    onError: (e) => {
      if (e instanceof HttpError && e.fields) setErrors(e.fields);
      else setMsg({ kind: "err", text: e instanceof Error ? e.message : String(e) });
    },
  });
  const test = useMutation({
    mutationFn: () => api.hubOpenclawTest(form.openclaw_url.trim(), form.openclaw_token.trim()),
    onSuccess: (r) => setTested(openclawResult(r)),
    onError: (e) => setTested(e instanceof HttpError && e.fields?.openclaw_url ? e.fields.openclaw_url : "无法连接"),
  });
  const restartText = `已保存，重启 TaskPaw Hub 后生效（${state.fields.map((f) => LABELS[f] ?? f).join("、")}）`;
  return (
    <Stack spacing={2}>
      {state.restart_required && <RestartBanner role="hub" text={restartText} />}
      <Stack direction="row" spacing={1} justifyContent="flex-end">
        <Button variant="outlined" color="inherit" sx={{ minHeight: 44 }} disabled={!dirty || save.isPending}
          onClick={() => { setForm(formOf(saved)); setErrors({}); setMsg(null); }}>撤销更改</Button>
        <Button variant="contained" sx={{ minHeight: 44 }} disabled={!dirty || save.isPending}
          onClick={() => { setMsg(null); setErrors({}); save.mutate(); }}>{save.isPending ? "正在保存…" : "保存"}</Button>
      </Stack>
      {Object.keys(errors).length > 0 && (
        <Alert severity="error" role="alert">未保存，请修改：{Object.entries(errors).map(([k, v]) => `${LABELS[k] ?? k}：${v}`).join("；")}</Alert>
      )}
      {msg && <Alert severity={msg.kind === "ok" ? "success" : "error"}>{msg.text}</Alert>}

      <Card><CardContent>
        <Stack spacing={1.5}>
          <Stack direction="row" justifyContent="space-between" alignItems="center">
            <Typography variant="subtitle1" component="h2">OpenClaw 通知</Typography>
            <Switch checked={form.openclaw_enabled} inputProps={{ role: "switch", "aria-label": "启用 OpenClaw 通知" }}
              onChange={(e) => set("openclaw_enabled", e.target.checked)} />
          </Stack>
          <TextField size="small" label="地址" value={form.openclaw_url} onChange={(e) => set("openclaw_url", e.target.value)}
            error={Boolean(errors.openclaw_url)} helperText={errors.openclaw_url} inputProps={{ spellCheck: false }} fullWidth />
          <TextField size="small" label="令牌" type={showToken ? "text" : "password"} value={form.openclaw_token} autoComplete="off"
            onChange={(e) => set("openclaw_token", e.target.value)} placeholder={saved.openclaw_token ? "***" : undefined}
            error={Boolean(errors.openclaw_token)} helperText={errors.openclaw_token ?? "留空则保留已保存的令牌"} fullWidth
            InputProps={{ endAdornment: (
              <InputAdornment position="end">
                <IconButton aria-label={showToken ? "隐藏令牌" : "显示令牌"} sx={{ width: 44, height: 44 }} onClick={() => setShowToken((s) => !s)}>
                  {showToken ? <VisibilityOffIcon fontSize="small" /> : <VisibilityIcon fontSize="small" />}
                </IconButton>
              </InputAdornment>
            ) }} />
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
            <Button variant="outlined" sx={{ minHeight: 44 }} disabled={test.isPending || !form.openclaw_url.trim()}
              onClick={() => { setTested(null); test.mutate(); }}>{test.isPending ? "正在发送…" : "测试发送"}</Button>
            {tested && <Typography variant="body2" role="status" color={tested.startsWith("已送达") ? "success.main" : "warning.main"}>{tested}</Typography>}
          </Stack>
        </Stack>
      </CardContent></Card>

      <ControllerConnection hint={saved.polling_token_hint} tokenSet={Boolean(saved.polling_token)} />

      <Card><CardContent>
        <Stack spacing={1.5}>
          <Typography variant="subtitle1" component="h2">高级</Typography>
          <TextField select size="small" label="轮询间隔" value={form.poll_interval} sx={{ maxWidth: 220 }}
            onChange={(e) => set("poll_interval", Number(e.target.value))} error={Boolean(errors.poll_interval)} helperText={errors.poll_interval}>
            {[15, 30, 60].map((s) => <MenuItem key={s} value={s}>{s} 秒</MenuItem>)}
          </TextField>
          <FormControlLabel label="写入 status.md（供 OpenClaw 读取）" control={
            <Switch checked={form.write_status_md} inputProps={{ role: "switch" }} onChange={(e) => set("write_status_md", e.target.checked)} />} />
          {form.write_status_md && (
            <TextField size="small" label="status.md 位置" value={form.status_md_path} placeholder={saved.status_md_default}
              onChange={(e) => set("status_md_path", e.target.value)} error={Boolean(errors.status_md_path)}
              helperText={errors.status_md_path ?? `留空：${saved.status_md_default}`} inputProps={{ spellCheck: false, "aria-label": "status.md 位置" }} fullWidth />
          )}
          <TextField size="small" label="日志保留天数" value={form.status_log_retention_days} sx={{ maxWidth: 220 }}
            onChange={(e) => set("status_log_retention_days", e.target.value)} error={Boolean(errors.status_log_retention_days)}
            helperText={errors.status_log_retention_days ?? "0 = 一直保留"} inputProps={{ inputMode: "numeric" }} />
          <FormControlLabel label="局域网只读接口（其他设备读取 Hub 状态）" control={
            <Switch checked={form.lan_access} inputProps={{ role: "switch" }} onChange={(e) => set("lan_access", e.target.checked)} />} />
          {form.lan_access && (
            <Stack direction="row" spacing={1} alignItems="flex-start">
              <TextField size="small" label="只读接口令牌" value={form.api_token} onChange={(e) => set("api_token", e.target.value)} fullWidth
                placeholder={saved.api_token ? "***" : undefined} error={Boolean(errors.api_token)}
                helperText={errors.api_token ?? (saved.api_token ? "留空则保留已保存的令牌" : "开启局域网只读接口需要令牌")} inputProps={{ spellCheck: false }} />
              <Button size="small" variant="outlined" sx={{ minHeight: 40 }} onClick={() => set("api_token", newToken())}>生成</Button>
            </Stack>
          )}
        </Stack>
      </CardContent></Card>
    </Stack>
  );
}

// 主控连接 — the first polled agent (the servers API) and the polling token (live).
function ControllerConnection({ hint, tokenSet }: { hint: string | null; tokenSet: boolean }) {
  const qc = useQueryClient();
  const status = useQuery({ queryKey: ["hubStatus"], queryFn: api.hubStatus, retry: false, refetchInterval: 15000 });
  const server = status.data?.servers[0];
  const [editing, setEditing] = useState(false);
  const [ip, setIp] = useState("");
  const [port, setPort] = useState("5680");
  const [token, setToken] = useState("");
  const [error, setError] = useState<string | null>(null);
  const save = useMutation({
    mutationFn: async () => {
      const p = Number(port);
      if (server) await api.hubUpdateServer(server.id, { ip: ip.trim(), port: p });
      else await api.hubAddServer({ name: "主控", ip: ip.trim(), port: p });
      if (token.trim()) await api.hubSaveSettings({ polling_token: token.trim() });
    },
    onSuccess: () => {
      setEditing(false); setToken("");
      qc.invalidateQueries({ queryKey: ["hubStatus"] }); qc.invalidateQueries({ queryKey: HUB_KEY });
    },
    onError: (e) => setError(e instanceof Error ? e.message : String(e)),
  });
  const ago = server?.last_seen ? Math.max(0, Math.round((Date.now() - new Date(server.last_seen).getTime()) / 1000)) : null;
  return (
    <Card><CardContent>
      <Stack spacing={1}>
        <Stack direction="row" justifyContent="space-between" alignItems="center">
          <Typography variant="subtitle1" component="h2">主控连接</Typography>
          {server && (
            <Typography variant="body2" color={server.online ? "success.main" : "warning.main"}>
              {server.online ? `已连接${ago != null ? ` · ${ago} 秒前` : ""}` : "未连接"}
            </Typography>
          )}
        </Stack>
        {server ? (
          <>
            <Typography variant="body2">主控 {server.name}</Typography>
            <Typography variant="body2">地址 {server.ip}:{server.port}</Typography>
          </>
        ) : <Typography variant="body2" color="text.secondary">还没有主控</Typography>}
        <Typography variant="body2">令牌 {tokenSet ? `tp_••••••${hint ?? ""}` : "未设置"}</Typography>
        {editing ? (
          <Stack spacing={1.5} sx={{ pt: 1 }}>
            <TextField size="small" label="主控地址" value={ip} onChange={(e) => setIp(e.target.value)} placeholder="192.168.1.20" />
            <TextField size="small" label="端口" value={port} onChange={(e) => setPort(e.target.value)} sx={{ maxWidth: 160 }} inputProps={{ inputMode: "numeric" }} />
            <TextField size="small" label="主控令牌" type="password" value={token} onChange={(e) => setToken(e.target.value)}
              helperText="主控 设置 → 网络与访问 里的访问令牌；留空则保留" autoComplete="off" />
            {error && <Alert severity="error">{error}</Alert>}
            <Stack direction="row" spacing={1}>
              <Button variant="contained" sx={{ minHeight: 44 }} disabled={save.isPending || !ip.trim()} onClick={() => { setError(null); save.mutate(); }}>保存</Button>
              <Button variant="outlined" color="inherit" sx={{ minHeight: 44 }} onClick={() => setEditing(false)}>取消</Button>
            </Stack>
          </Stack>
        ) : (
          <div>
            <Button variant="outlined" sx={{ minHeight: 44 }} onClick={() => {
              setIp(server?.ip ?? ""); setPort(String(server?.port ?? 5680)); setEditing(true);
            }}>{server ? "更换主控" : "添加主控"}</Button>
          </div>
        )}
      </Stack>
    </CardContent></Card>
  );
}
